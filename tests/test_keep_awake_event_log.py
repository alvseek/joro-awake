"""Event log behaviour against real temporary folders and real child processes."""

import faulthandler
import json
import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

WINDOWS = Path(__file__).resolve().parents[1] / "scripts/windows"
sys.path.insert(0, str(WINDOWS))
from joro_watcher import event_log


def raises(message="secret prompt"):
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="joro-log-test-")
        self.directory = Path(self.temp.name)

    def tearDown(self):
        faulthandler.disable()
        for handle in event_log.FAULT_FILES:
            handle.close()
        event_log.FAULT_FILES.clear()
        self.temp.cleanup()

    def lines(self):
        text = (self.directory / "keep-awake.log").read_text(encoding="utf-8")
        return [json.loads(line.split(" ", 2)[2]) for line in text.splitlines()]

    def test_failure_carries_type_traceback_and_message(self):
        value = event_log.failure(raises())
        self.assertEqual(value["error_type"], "ValueError")
        self.assertIn("raises", value["traceback"])
        self.assertEqual(value["error"], "secret prompt")

    def test_failure_without_message_never_holds_the_message(self):
        value = event_log.failure(raises(), message=False)
        self.assertNotIn("error", value)
        self.assertNotIn("secret prompt", json.dumps(value))

    def test_recorded_error_keeps_its_traceback(self):
        event_log.record("error", directory=self.directory, source="test", **event_log.failure(raises()))
        entry = self.lines()[0]
        self.assertEqual((entry["event"], entry["error_type"]), ("error", "ValueError"))
        self.assertIn("raises", entry["traceback"])

    def test_oversized_entry_keeps_event_and_end_of_traceback(self):
        trace = "frame\n" * 5000 + "innermost frame\n"
        event_log.record("error", directory=self.directory, source="x" * 5000, traceback=trace)
        entry = self.lines()[0]
        self.assertEqual(entry["event"], "error")
        self.assertTrue(entry["truncated"])
        self.assertTrue(entry["traceback"].endswith("innermost frame\n"))
        self.assertLessEqual(len(json.dumps(entry)), event_log.ENTRY_LIMIT)

    def test_oversized_entry_without_traceback_keeps_event_name(self):
        event_log.record("error", directory=self.directory, detail="x" * 20000)
        self.assertEqual(self.lines()[0]["event"], "error")

    def test_log_stays_within_five_files_of_one_mebibyte(self):
        trace = "t" * 4000
        for _ in range(600):
            event_log.record("error", directory=self.directory, traceback=trace)
        files = list(self.directory.glob("keep-awake.log*"))
        self.assertLessEqual(len(files), 5)
        self.assertTrue(all(f.stat().st_size <= 1024 * 1024 for f in files))

    def test_uncaught_exceptions_are_recorded_before_the_process_ends(self):
        old_top, old_thread, old_unraisable = sys.excepthook, threading.excepthook, sys.unraisablehook
        top, thread = MagicMock(), MagicMock()
        sys.excepthook, threading.excepthook = top, thread
        try:
            event_log.install_hooks("test", directory=self.directory)
            exc = raises()
            sys.excepthook(type(exc), exc, exc.__traceback__)
            worker = threading.Thread(target=lambda: (_ for _ in ()).throw(KeyError("k")), name="worker")
            worker.start()
            worker.join()
        finally:
            sys.excepthook, threading.excepthook, sys.unraisablehook = old_top, old_thread, old_unraisable
        events = {entry["event"]: entry for entry in self.lines()}
        self.assertEqual(events["uncaught_exception"]["error_type"], "ValueError")
        self.assertEqual(events["uncaught_thread_exception"]["thread"], "worker")
        self.assertEqual(events["uncaught_thread_exception"]["error_type"], "KeyError")
        top.assert_called_once()
        thread.assert_called_once()

    def test_library_caught_error_and_process_output_reach_rotating_log(self):
        stdout, stderr = sys.stdout, sys.stderr
        logger = logging.getLogger("pystray._win32")
        with event_log.tray_diagnostics(directory=self.directory):
            try:
                raise OSError("shell failed")
            except OSError:
                logger.error("An error occurred in the main loop", exc_info=True)
            print("callback failed", file=sys.stderr)
            print("output without newline", end="")
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)
        entries = self.lines()
        library = next(e for e in entries if e["event"] == "library_error")
        self.assertEqual(library["error_type"], "OSError")
        self.assertIn("shell failed", library["error"])
        self.assertIn("test_library_caught_error", library["traceback"])
        self.assertTrue(any(e.get("detail") == "callback failed" and e["stream"] == "stderr" for e in entries))
        self.assertTrue(any(e.get("detail") == "output without newline" and e["stream"] == "stdout" for e in entries))

    def test_diagnostic_stream_bounds_unterminated_output_and_avoids_recursion(self):
        stream = event_log.DiagnosticStream("tray", "stderr", self.directory)
        text = "x" * 20000
        self.assertEqual(stream.write(text), len(text))
        self.assertLess(len(stream.pending), 2048)
        stream.flush()
        self.assertEqual("".join(e["detail"] for e in self.lines()), text)
        with patch.object(event_log, "_record", side_effect=OSError("disk")), \
             patch.object(sys, "stderr", stream):
            stream.write("failure\n")
        self.assertEqual(stream.pending, "")

    def test_unraisable_callback_error_is_captured(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1])"
                "\nfrom joro_watcher import event_log\n"
                "event_log.install_hooks('tray', directory=sys.argv[2])\n"
                "class Broken:\n"
                " def __del__(self): raise RuntimeError('callback failure')\n"
                "x = Broken()\ndel x\n")
        result = subprocess.run([sys.executable, "-c", code, str(WINDOWS), str(self.directory)],
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        (entry,) = self.lines()
        self.assertEqual(entry["event"], "unraisable_exception")
        self.assertEqual(entry["error_type"], "RuntimeError")
        self.assertIn("__del__", entry["traceback"])

    def test_tray_diagnostics_restore_streams_and_logger_after_failure(self):
        stdout, stderr = sys.stdout, sys.stderr
        logger = logging.getLogger("pystray")
        handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
        with self.assertRaises(SystemExit):
            with event_log.tray_diagnostics(directory=self.directory):
                print("last fragment", end="", file=sys.stderr)
                raise SystemExit(1)
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)
        self.assertEqual((logger.handlers, logger.level, logger.propagate), (handlers, level, propagate))
        self.assertEqual(self.lines()[0]["detail"], "last fragment")

    def test_unusable_heartbeat_time_becomes_none(self):
        self.assertIsNone(event_log.timestamp(None))
        self.assertIsNone(event_log.timestamp(1e30))
        self.assertIn("T", event_log.timestamp(1700000000))

    def test_exit_codes_are_named_in_hex(self):
        self.assertEqual(event_log.describe_exit(0)["exit_code"], "0x00000000")
        self.assertIn("clean", event_log.describe_exit(0)["exit_meaning"])
        self.assertIn("taskkill", event_log.describe_exit(1)["exit_meaning"])
        self.assertEqual(event_log.describe_exit(-1073741819)["exit_code"], "0xC0000005")
        self.assertIn("access violation", event_log.describe_exit(0xC0000005)["exit_meaning"])
        self.assertIn("shutdown", event_log.describe_exit(0xC000013A)["exit_meaning"])
        self.assertEqual(event_log.describe_exit(77)["exit_meaning"], "unrecognized exit code")

    def test_fault_file_is_cut_back_once_it_passes_the_limit(self):
        path = self.directory / "crash-helper.log"
        path.write_bytes(b"x" * (event_log.FAULT_LIMIT + 1))
        self.assertIsNone(event_log.enable_fault_log("helper", directory=self.directory))
        self.assertLess(path.stat().st_size, event_log.FAULT_LIMIT)
        self.assertTrue(faulthandler.is_enabled())

    def test_fault_file_below_the_limit_is_kept(self):
        path = self.directory / "crash-tray.log"
        path.write_bytes(b"earlier fault\n")
        event_log.enable_fault_log("tray", directory=self.directory)
        self.assertTrue(path.read_bytes().startswith(b"earlier fault\n"))

    def test_fault_log_failure_is_reported_without_raising(self):
        blocker = self.directory / "blocked"
        blocker.write_text("file, not a folder")
        with patch.object(event_log.sys, "stderr"):
            self.assertIn("Cannot enable", event_log.enable_fault_log("helper", directory=blocker))

    def test_native_fault_leaves_a_stack_dump(self):
        code = ("import sys, ctypes; sys.path.insert(0, sys.argv[1])\n"
                "from joro_watcher import event_log\n"
                "event_log.enable_fault_log('helper', directory=sys.argv[2])\n"
                "ctypes.string_at(0)\n")
        result = subprocess.run([sys.executable, "-c", code, str(WINDOWS), str(self.directory)],
                                capture_output=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        dump = (self.directory / "crash-helper.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("Windows fatal exception", dump)
        self.assertIn("string_at", dump)


if __name__ == "__main__":
    unittest.main()
