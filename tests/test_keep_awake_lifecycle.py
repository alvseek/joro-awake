"""Lifecycle records: peer exit codes, unclean ends, stalls; real folders and child processes."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from contextlib import nullcontext
import time
import unittest
from unittest.mock import MagicMock, patch

WINDOWS = Path(__file__).resolve().parents[1] / "scripts/windows"
sys.path.insert(0, str(WINDOWS))
import keep_awake as helper


def spawn(code):
    return subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def events(record, name):
    return [call.kwargs for call in record.call_args_list if call.args[0] == name]


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="joro-lifecycle-test-")
        self.root = Path(self.temp.name)
        patcher = patch.object(helper, "ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child in self.children:
            child.kill()
            child.wait()
        self.temp.cleanup()

    def child(self, code, filename=None):
        child = spawn(code)
        self.children.append(child)
        started = helper.birth(child.pid)
        if filename:
            helper.write(self.root / filename, {"pid": child.pid, "birth": started, "updated_at": time.time()})
        return child, started

    def poll_until_reported(self, peer, record, source, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline and not events(record, "peer_exited"):
            peer.poll(source)
            time.sleep(0.05)

    def test_peer_exit_code_of_a_real_child_is_recorded_in_hex(self):
        child, _ = self.child("import sys, time; time.sleep(1); sys.exit(3)", "tray.json")
        peer = helper.Peer("tray", "tray.json")
        with patch.object(helper, "record") as record:
            peer.poll("helper")
            self.assertIsNotNone(peer.handle)
            self.poll_until_reported(peer, record, "helper")
            peer.poll("helper")
        (entry,) = events(record, "peer_exited")
        self.assertEqual((entry["source"], entry["peer"], entry["pid"]), ("helper", "tray", child.pid))
        self.assertEqual(entry["exit_code"], "0x00000003")
        self.assertIsNone(peer.handle)

    def test_killed_peer_is_named_by_its_exit_code(self):
        child, _ = self.child("import time; time.sleep(60)", "helper.json")
        peer = helper.Peer("helper", "helper.json")
        with patch.object(helper, "record") as record:
            peer.poll("tray")
            child.kill()
            child.wait()
            self.poll_until_reported(peer, record, "tray")
        (entry,) = events(record, "peer_exited")
        self.assertEqual(entry["source"], "tray")
        self.assertEqual(entry["exit_code"], "0x00000001")
        self.assertIn("taskkill", entry["exit_meaning"])

    def test_peer_with_another_birth_is_not_watched(self):
        child, started = self.child("import time; time.sleep(60)")
        helper.write(self.root / "tray.json", {"pid": child.pid, "birth": started + 1})
        peer = helper.Peer("tray", "tray.json")
        peer.poll("helper")
        self.assertIsNone(peer.handle)

    def test_missing_or_unreadable_state_file_is_quiet(self):
        peer = helper.Peer("tray", "tray.json")
        with patch.object(helper, "record") as record:
            peer.poll("helper")
            (self.root / "tray.json").write_text("not json")
            peer.poll("helper")
        record.assert_not_called()

    def test_watch_failure_is_recorded_once_without_the_message(self):
        helper.write(self.root / "tray.json", {"pid": 1, "birth": 2})
        peer = helper.Peer("tray", "tray.json")
        with patch.object(helper, "record") as record, \
             patch.object(helper, "watch", side_effect=OSError("secret prompt")):
            peer.poll("helper")
            peer.poll("helper")
        (entry,) = events(record, "peer_watch_failed")
        self.assertEqual(entry["error_type"], "OSError")
        self.assertNotIn("secret prompt", json.dumps(entry))

    def test_leftover_state_of_a_dead_process_is_an_unclean_end(self):
        child, _ = self.child("pass", "helper.json")
        child.wait()
        heartbeat = json.loads((self.root / "helper.json").read_text())["updated_at"]
        with patch.object(helper, "record") as record:
            helper.ended_without_stop("helper", "helper.json")
        (entry,) = events(record, "previous_run_ended_without_stop")
        self.assertEqual(entry["previous_pid"], child.pid)
        self.assertEqual(entry["last_heartbeat"], helper.timestamp(heartbeat))

    def test_no_leftover_state_or_a_live_process_is_not_an_unclean_end(self):
        with patch.object(helper, "record") as record:
            helper.ended_without_stop("helper", "helper.json")
            self.child("import time; time.sleep(60)", "helper.json")
            helper.ended_without_stop("helper", "helper.json")
        record.assert_not_called()

    def run_helper(self, wait_results, clock, state=None, wall=None):
        kernel, request, ticks = MagicMock(), MagicMock(), iter(clock)
        walls = iter(wall or clock)
        kernel.WaitForSingleObject.side_effect = wait_results
        patches = [patch.object(helper, "K", kernel), patch.object(helper, "birth", return_value=123),
                   patch.object(helper, "write"), patch.object(helper, "start_tray"),
                   patch.object(helper, "CLOCK", lambda: next(ticks)),
                   patch.object(helper, "WALL", lambda: next(walls)),
                   patch.object(helper, "PowerRequest", return_value=request),
                   patch.object(helper, "state", state or MagicMock(return_value={
                       "active_count": 1, "clients": ["opencode"], "uncertain_claims": 0}))]
        error = None
        with patch.object(helper, "record") as record:
            for p in patches:
                p.start()
            try:
                helper.run()
            except Exception as exc:
                error = exc
            finally:
                for p in reversed(patches):
                    p.stop()
        return record, request, error

    def test_gap_over_ten_seconds_between_ticks_is_a_stall(self):
        record, _, _ = self.run_helper([0, 0x102, 0x102, 0], [0, 1.0, 31.5, 32.5])
        (entry,) = events(record, "stall")
        self.assertEqual(entry["gap_seconds"], 30.5)

    def test_wall_clock_gap_alone_is_a_stall(self):
        record, _, _ = self.run_helper([0, 0x102, 0x102, 0], [0, 1.0, 2.0, 3.0], wall=[0, 1.0, 3601.0, 3602.0])
        (entry,) = events(record, "stall")
        self.assertEqual((entry["gap_seconds"], entry["wall_gap_seconds"]), (1.0, 3600.0))

    def test_unreadable_leftover_state_is_recorded_without_its_message(self):
        (self.root / "helper.json").write_text("{not json")
        with patch.object(helper, "record") as record:
            helper.ended_without_stop("helper", "helper.json")
        (entry,) = events(record, "unclean_check_failed")
        self.assertEqual(entry["error_type"], "JSONDecodeError")
        self.assertNotIn("error", entry)

    def test_normal_ticks_are_not_stalls(self):
        record, _, _ = self.run_helper([0, 0x102, 0], [0, 1.0, 2.0])
        self.assertEqual(events(record, "stall"), [])

    def test_start_line_carries_versions_and_requested_stop_says_so(self):
        record, _, _ = self.run_helper([0, 0], [0, 1.0])
        (start,) = events(record, "start")
        self.assertEqual(start["version"], (WINDOWS.parents[1] / "VERSION").read_text().strip())
        self.assertEqual(start["python"], sys.version.split()[0])
        self.assertEqual(events(record, "stop")[0]["reason"], "requested")

    def test_stop_after_an_error_says_so_and_still_releases_power(self):
        state = MagicMock(side_effect=[{"active_count": 1, "clients": [], "uncertain_claims": 0},
                                       RuntimeError("boom")])
        record, request, error = self.run_helper([0, 0x102], [0, 1.0, 2.0], state)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(events(record, "stop")[0]["reason"], "error")
        request.close.assert_called_once()

    def command(self, argv, stdin):
        with patch.object(sys, "argv", ["keep_awake.py", *argv]), patch.object(sys, "stdin", io.StringIO(stdin)), \
             patch.object(helper, "record") as record, patch.object(helper.sys, "stderr", io.StringIO()):
            code = helper.main()
        return code, record

    def test_publisher_failure_records_type_and_frames_never_the_message(self):
        hook = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt_id": "p"})
        with patch.object(helper, "claude_owner", side_effect=RuntimeError("secret prompt text")):
            code, record = self.command(["hook"], hook)
        self.assertEqual(code, 1)
        (entry,) = events(record, "publisher_error")
        self.assertEqual(entry["error_type"], "RuntimeError")
        self.assertIn("claude_hook", entry["traceback"])
        self.assertNotIn("secret prompt text", json.dumps(entry))

    def test_event_failure_never_records_the_message(self):
        code, record = self.command(["event"], json.dumps({"client": "other"}))
        self.assertEqual(code, 1)
        (entry,) = events(record, "publisher_error")
        self.assertEqual(entry["error_type"], "ValueError")
        self.assertNotIn("error", entry)

    def test_command_error_records_type_message_and_traceback(self):
        with patch.object(helper, "run", side_effect=RuntimeError("boom")), patch.object(helper, "write"), \
             patch.object(helper, "install_hooks") as hooks, patch.object(helper, "enable_fault_log") as faults:
            code, record = self.command(["run"], "")
        self.assertEqual(code, 1)
        (entry,) = events(record, "error")
        self.assertEqual((entry["error_type"], entry["error"]), ("RuntimeError", "boom"))
        self.assertIn("main", entry["traceback"])
        hooks.assert_called_once_with("helper")
        faults.assert_called_once_with("helper")

    def test_tray_system_exit_is_recorded_before_it_leaves_the_process(self):
        from joro_watcher import tray
        with patch.object(tray, "run", side_effect=SystemExit(1)), \
             patch.object(helper, "install_hooks"), patch.object(helper, "enable_fault_log"), \
             patch.object(helper.event_log, "tray_diagnostics", return_value=nullcontext()), \
             patch.object(helper, "record") as record, \
             patch.object(sys, "argv", ["keep_awake.py", "tray"]):
            with self.assertRaises(SystemExit) as result:
                helper.main()
        self.assertEqual(result.exception.code, 1)
        (entry,) = events(record, "tray_failed")
        self.assertEqual(entry["error_type"], "SystemExit")
        self.assertEqual(entry["error"], "1")


if __name__ == "__main__":
    unittest.main()
