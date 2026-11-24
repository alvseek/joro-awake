"""The tray's own lifecycle records, run against a fake icon, a temporary state folder and real children."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest.mock import MagicMock, patch

WINDOWS = Path(__file__).resolve().parents[1] / "scripts/windows"
sys.path.insert(0, str(WINDOWS))
from joro_watcher import tray
import keep_awake as helper


class FakeIcon:
    def __init__(self, name, image, title, menu=None):
        self.icon, self.title, self.visible, self.ended = image, title, False, threading.Event()
        self.menu = menu

    def run(self, setup=None):
        setup(self)
        self.ended.wait(60)

    def stop(self):
        self.ended.set()

    def update_menu(self):
        pass


def fake_menu(*items):
    return items


fake_menu.SEPARATOR = object()


def child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def events(record, name):
    return [call.kwargs for call in record.call_args_list if call.args[0] == name]


def until(condition, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline and not condition():
        time.sleep(0.1)
    return condition()


class TrayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="joro-tray-test-")
        self.root = Path(self.temp.name)
        self.children = []
        for p in (patch.object(helper, "ROOT", self.root), patch.object(helper, "KEY", uuid.uuid4().hex[:20]),
                   patch.dict(sys.modules, {"pystray": types.SimpleNamespace(
                        Icon=FakeIcon, Menu=fake_menu, MenuItem=lambda *a, **kw: None)}),
                   patch.object(tray, "system_execution_state", return_value=0),
                   patch.object(helper, "startup_enabled", return_value=False),
                   patch.object(tray, "images", return_value={n: n for n in ("idle", "blocked", "awake", "warning")})):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for process in self.children:
            process.kill()
            process.wait()
        self.temp.cleanup()

    def live_child(self, filename):
        process = child()
        self.children.append(process)
        helper.write(self.root / filename, {"pid": process.pid, "birth": helper.birth(process.pid),
                                            "updated_at": time.time()})
        return process

    def run_tray(self, record):
        thread = threading.Thread(target=tray.run, args=(helper,), daemon=True)
        with patch.object(tray, "record", record), patch.object(helper, "record", record):
            thread.start()
            yield thread
            exit_event = helper.named_event("-tray-exit")
            helper.K.SetEvent(exit_event)
            helper.K.CloseHandle(exit_event)
            thread.join(30)
        self.assertFalse(thread.is_alive())

    def test_tray_records_the_helper_exit_code_heartbeats_and_leaves_no_state(self):
        record = MagicMock(return_value=None)
        dead, beat = child(), time.time() - 3600
        helper.write(self.root / "tray.json", {"pid": dead.pid, "birth": helper.birth(dead.pid), "updated_at": beat})
        dead.kill()
        dead.wait()
        process = self.live_child("helper.json")
        for _ in self.run_tray(record):
            self.assertTrue(until(lambda: (self.root / "tray.json").exists()
                                  and json.loads((self.root / "tray.json").read_text()).get("pid") != dead.pid))
            first = json.loads((self.root / "tray.json").read_text())["updated_at"]
            self.assertTrue(until(lambda: json.loads((self.root / "tray.json").read_text())["updated_at"] > first))
            time.sleep(1.5)
            process.kill()
            process.wait()
            self.assertTrue(until(lambda: events(record, "peer_exited")))
        (entry,) = events(record, "peer_exited")
        self.assertEqual((entry["source"], entry["peer"], entry["pid"]), ("tray", "helper", process.pid))
        self.assertEqual(entry["exit_code"], "0x00000001")
        (unclean,) = events(record, "previous_run_ended_without_stop")
        self.assertEqual((unclean["source"], unclean["previous_pid"]), ("tray", dead.pid))
        self.assertEqual(unclean["last_heartbeat"], helper.timestamp(beat))
        (start,) = events(record, "start")
        self.assertEqual(start["version"], (WINDOWS.parents[1] / "VERSION").read_text().strip())
        self.assertFalse((self.root / "tray.json").exists())

    def test_heartbeat_failure_is_recorded_once_and_never_raises(self):
        record = MagicMock()
        with patch.object(tray, "record", record), patch.object(helper, "write", side_effect=PermissionError("secret")):
            self.assertFalse(tray.beat(helper, {"pid": 1, "birth": 2}, True))
            self.assertFalse(tray.beat(helper, {"pid": 1, "birth": 2}, False))
        (entry,) = events(record, "heartbeat_failed")
        self.assertEqual(entry["error_type"], "PermissionError")
        self.assertNotIn("secret", json.dumps(entry))

    def test_heartbeat_writes_identity_and_time(self):
        self.assertTrue(tray.beat(helper, {"pid": 7, "birth": 8}, True))
        value = json.loads((self.root / "tray.json").read_text())
        self.assertEqual((value["pid"], value["birth"]), (7, 8))
        self.assertAlmostEqual(value["updated_at"], time.time(), delta=5)

    def test_observer_failure_records_type_and_traceback(self):
        record = MagicMock(return_value=None)
        with patch.object(tray, "observe", side_effect=RuntimeError("unexpected")), patch.object(tray, "record", record):
            tray.observe_once(None, MagicMock(), {"warning": "warning"}, {}, None)
        (entry,) = events(record, "observer_failed")
        self.assertEqual((entry["error_type"], entry["error"]), ("RuntimeError", "unexpected"))
        self.assertIn("observe_once", entry["traceback"])

    def test_unexpected_icon_loop_end_is_not_reported_as_a_requested_exit(self):
        record = MagicMock(return_value=None)
        with patch.object(FakeIcon, "run"), patch.object(tray, "record", record):
            tray.run(helper)
        (entry,) = events(record, "tray_loop_ended")
        self.assertEqual(entry["reason"], "unexpected")
        self.assertEqual(events(record, "exit"), [])

    def test_locked_state_cleanup_records_failure_and_releases_resources(self):
        record = MagicMock(return_value=None)
        with patch.object(FakeIcon, "run"), patch.object(tray, "record", record), \
             patch.object(helper, "remove_claim", side_effect=PermissionError("locked")), \
             patch.object(helper, "birth", return_value=123), \
             patch.object(helper.Peer, "close") as close, \
             patch.object(helper, "K", wraps=helper.K) as kernel:
            tray.run(helper)
        (entry,) = events(record, "tray_cleanup_failed")
        self.assertEqual(entry["error_type"], "PermissionError")
        close.assert_called_once()
        kernel.ReleaseMutex.assert_called_once()
        self.assertEqual(kernel.CloseHandle.call_count, 2)

    def test_menu_identifies_version_with_disabled_header_and_separator(self):
        pystray = sys.modules["pystray"]
        with patch.object(FakeIcon, "run"), patch.object(pystray, "MenuItem") as item, \
             patch.object(pystray, "Menu", wraps=fake_menu) as menu, \
             patch.object(helper, "version", return_value="9.8.7"), patch.object(tray, "record"):
            menu.SEPARATOR = fake_menu.SEPARATOR
            tray.run(helper)
        self.assertEqual(item.call_args_list[0].args, ("Joro Awake · v9.8.7", None))
        self.assertEqual(item.call_args_list[0].kwargs, {"enabled": False})
        self.assertIs(menu.call_args.args[1], fake_menu.SEPARATOR)
        self.assertEqual(item.call_args_list[1].args[0], "Run at startup")
        self.assertEqual(item.call_args_list[2].args[0], "Check power requests…")
        self.assertIs(menu.call_args.args[4], fake_menu.SEPARATOR)
        self.assertEqual(item.call_args_list[3].args[0], "Exit")

    def test_tray_exit_does_not_wait_for_snapshot_worker(self):
        pystray = sys.modules["pystray"]
        entered, finish, finished = threading.Event(), threading.Event(), threading.Event()

        def hold_viewer(stopped, output, error):
            entered.set()
            finish.wait(15)
            finished.set()

        def run_icon(icon, setup):
            setup(icon)
            item = next(item for item in icon.menu if isinstance(item, tuple)
                        and item[0] == "Check power requests…")
            item[1](icon, item)
            icon.ended.wait(15)

        record = MagicMock(return_value=None)
        try:
            with patch.object(FakeIcon, "run", run_icon), \
                 patch.object(pystray, "MenuItem", side_effect=lambda title, action, **kw: (title, action)), \
                 patch.object(tray.power_snapshot, "check", return_value=b"fixture"), \
                 patch.object(tray, "show_power_snapshot", side_effect=hold_viewer):
                for _ in self.run_tray(record):
                    self.assertTrue(entered.wait(5))
                self.assertFalse(finished.is_set())
                self.assertFalse((self.root / "tray.json").exists())
        finally:
            finish.set()
            if entered.is_set():
                self.assertTrue(finished.wait(3))


class StartupMenuTests(unittest.TestCase):
    def setUp(self):
        self.helper, self.icon, self.failure = MagicMock(), MagicMock(), {}
        self.enabled = False
        self.helper.startup_enabled.side_effect = lambda: self.enabled
        def set_enabled(enabled):
            self.enabled = enabled
        self.helper.startup.side_effect = set_enabled
        pystray = types.SimpleNamespace(MenuItem=lambda text, action, **kw:
                                       types.SimpleNamespace(text=text, action=action, **kw))
        self.item = tray.startup_menu_item(pystray, self.helper, self.failure)

    def test_checkbox_reads_external_changes_and_click_toggles_both_directions(self):
        self.assertEqual(self.item.text, "Run at startup")
        self.assertFalse(self.item.checked(self.item))
        with patch.object(tray, "record"):
            self.item.action(self.icon, self.item)
            self.assertTrue(self.item.checked(self.item))
            self.item.action(self.icon, self.item)
        self.assertFalse(self.item.checked(self.item))
        self.assertEqual([c.args for c in self.helper.startup.call_args_list], [(True,), (False,)])
        self.enabled = True  # A CLI/external edit, not a click.
        self.assertTrue(self.item.checked(self.item))
        self.assertEqual(self.icon.update_menu.call_count, 2)

    def test_write_failure_preserves_check_and_warns_until_successful_retry(self):
        self.helper.startup.side_effect = PermissionError("denied")
        with patch.object(tray, "record") as record:
            self.item.action(self.icon, self.item)
        self.assertFalse(self.item.checked(self.item))
        self.assertIn("startup", self.failure["error"])
        self.assertEqual(events(record, "startup_failed")[0]["error_type"], "PermissionError")
        self.helper.startup.side_effect = lambda enabled: setattr(self, "enabled", enabled)
        with patch.object(tray, "record"):
            self.item.action(self.icon, self.item)
        self.assertTrue(self.item.checked(self.item))
        self.assertEqual(self.failure, {})

    def test_read_failure_is_unknown_and_does_not_write(self):
        self.helper.startup_enabled.side_effect = PermissionError("denied")
        with patch.object(tray, "record"):
            self.assertIsNone(self.item.checked(self.item))
            self.item.action(self.icon, self.item)
        self.helper.startup.assert_not_called()
        self.assertIn("error", self.failure)

    def test_unconfirmed_write_does_not_report_success(self):
        self.helper.startup.side_effect = None
        with patch.object(tray, "record") as record:
            self.item.action(self.icon, self.item)
        self.assertIn("error", self.failure)
        self.assertEqual(events(record, "startup_changed"), [])

    def test_external_registry_changes_refresh_windows_cached_menu_only_when_changed(self):
        previous = tray.refresh_startup_menu(self.helper, self.icon, object())
        self.assertFalse(previous)
        previous = tray.refresh_startup_menu(self.helper, self.icon, previous)
        self.assertEqual(self.icon.update_menu.call_count, 1)
        self.enabled = True
        previous = tray.refresh_startup_menu(self.helper, self.icon, previous)
        self.assertTrue(previous)
        self.assertEqual(self.icon.update_menu.call_count, 2)
        self.helper.startup_enabled.side_effect = PermissionError("denied")
        self.assertIsNone(tray.refresh_startup_menu(self.helper, self.icon, previous))
        self.assertEqual(self.icon.update_menu.call_count, 3)

    def test_failed_menu_refresh_retries_without_ending_observer(self):
        self.icon.update_menu.side_effect = OSError("menu unavailable")
        previous = object()
        with patch.object(tray, "record") as record:
            self.assertIs(tray.refresh_startup_menu(self.helper, self.icon, previous), previous)
        self.assertEqual(events(record, "startup_menu_failed")[0]["error_type"], "OSError")
        self.icon.update_menu.side_effect = None
        self.assertFalse(tray.refresh_startup_menu(self.helper, self.icon, previous))


if __name__ == "__main__":
    unittest.main()
