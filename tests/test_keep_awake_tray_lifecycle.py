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

    def run(self, setup=None):
        setup(self)
        self.ended.wait(60)

    def stop(self):
        self.ended.set()


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
                       Icon=FakeIcon, Menu=lambda *a: None, MenuItem=lambda *a: None)}),
                   patch.object(tray, "system_execution_state", return_value=0),
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


if __name__ == "__main__":
    unittest.main()
