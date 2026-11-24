"""Snapshot fixtures; never launch UAC, a UI, or a live powercfg query."""

import base64
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/windows"))
from joro_watcher import power_snapshot as snapshot, snapshot_viewer, tray


STAMP = datetime(2026, 11, 24, 12, 30, tzinfo=timezone.utc)


class SnapshotTests(unittest.TestCase):
    def test_groups_and_only_marks_the_exact_reason(self):
        raw = """DISPLAY:
[PROCESS] video.exe
SYSTEM:
[PROCESS] python.exe
Joro agent work is active
AWAYMODE:
[DRIVER] media driver
EXECUTION:
[PROCESS] python.exe
Another Joro agent work is active
[PROCESS] helper.exe
Joro agent work is active
another line
[PROCESS] Joro agent work is active
PERFBOOST:
[DRIVER] performance
ACTIVELOCKSCREEN:
None.
"""
        summary, full = snapshot.snapshot_text(raw.encode("utf-16"), None, STAMP)
        self.assertEqual(full, raw)
        self.assertIn("2026-11-24 12:30:00+00:00", summary)
        self.assertIn("System sleep\nJoro — Joro agent work is active", summary)
        self.assertIn("AWAYMODE: [DRIVER] media driver\n\nDisplay", summary)
        self.assertIn("Display\nvideo.exe", summary)
        self.assertIn("Other\nEXECUTION: python.exe", summary)
        self.assertIn("[DRIVER] performance", summary)
        self.assertEqual(summary.count("Joro —"), 1)

    def test_repeated_requests_counted_per_kind_and_reason_but_full_report_keeps_every_entry(self):
        league = "[PROCESS] \\Device\\Disk\\Riot Games\\LeagueClientUx.exe\r\nVideo Wake Lock\r\n"
        joro = "[PROCESS] \\Device\\Disk\\Python\\pythonw.exe\r\nJoro agent work is active\r\n"
        raw = ("DISPLAY:\r\n" + league * 3 + "SYSTEM:\r\n" + league + joro + league
               + league.replace("Video Wake Lock", "Audio") + "EXECUTION:\r\n" + joro)
        summary, full = snapshot.snapshot_text(raw.encode("utf-16"), None, STAMP)
        self.assertEqual(raw, full)
        self.assertEqual(summary.split("\n\n", 1)[1], (
            "System sleep\nLeagueClientUx.exe — Video Wake Lock ×2\nJoro — Joro agent work is active\n"
            "LeagueClientUx.exe — Audio\n\nDisplay\nLeagueClientUx.exe — Video Wake Lock ×3\n\n"
            "Other\nEXECUTION: Joro — Joro agent work is active"))

    def test_same_named_processes_from_different_paths_remain_distinguishable(self):
        a, b = r"C:\First\client.exe", r"D:\Second\client.exe"
        row = lambda path: f"[PROCESS] {path}\nVideo Wake Lock\n"
        summary = snapshot.summarize("SYSTEM:\n" + row(a) * 2 + row(b) + "AWAYMODE:\n" + row(a))
        self.assertIn(f"{a} — Video Wake Lock ×2\n{b} — Video Wake Lock\n", summary)
        self.assertIn(f"AWAYMODE: {a} — Video Wake Lock\n", summary)

    def test_preserves_unfamiliar_sections_and_localized_text(self):
        raw = ("SYSTEM:\r\n[PROCESS] editor.exe\r\n"
               "FUTURE FEATURE:\r\n[PROZESS] vidéo.exe\r\n未分類\r\n")
        for encoding in ("utf-16-le", "utf-16-be", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                summary, full = snapshot.snapshot_text(raw.encode(encoding), None, STAMP)
                self.assertEqual(full, raw)
                self.assertIn("Other\nFUTURE FEATURE: [PROZESS] vidéo.exe — 未分類", summary)
                self.assertIn("Some output could not be grouped", summary)
        self.assertEqual(snapshot.decode_report(b"[PROCESS] vid\x82o.exe", fallback="cp437"),
                         "[PROCESS] vidéo.exe")

    def test_empty_output_is_not_a_successful_empty_report(self):
        summary, full = snapshot.snapshot_text("  ".encode("utf-16"), None, STAMP)
        self.assertIn("Check didn’t complete", summary)
        self.assertIsNone(full)
        raw = "DISPLAY:\nNone.\nSYSTEM:\nNone.\nEXECUTION:\nNone.\n"
        summary, full = snapshot.snapshot_text(raw.encode("utf-8-sig"), None, STAMP)
        self.assertEqual(full, raw)
        self.assertNotIn("didn’t complete", summary)
        self.assertNotIn("could not be grouped", summary)

    def test_only_generated_local_pipe_can_enter_fixed_command(self):
        pipe = r"\\.\pipe\JoroPower-" + "0" * 32
        flags, encoded = snapshot.query_arguments(pipe).rsplit(" ", 1)
        self.assertEqual(flags, "-NoLogo -NoProfile -NonInteractive -EncodedCommand")
        command = base64.b64decode(encoded).decode("utf-16-le")
        self.assertIn("[Environment]::SystemDirectory, 'powercfg.exe'", command)
        self.assertIn("$start.Arguments = '/requests'", command)
        self.assertIn("[Security.Principal.TokenImpersonationLevel]::Anonymous", command)
        for bad in (pipe + "'; exit 0", pipe.replace(r"\\.", r"\\remote"), pipe[9:]):
            with self.assertRaises(ValueError):
                snapshot.query_arguments(bad)


class TraySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.stopped = threading.Event()
        self.show = Mock()
        self.icon = Mock()
        self.item = tray.snapshot_menu_item(SimpleNamespace(MenuItem=lambda text, action: action),
                                            self.stopped, self.show)

    def test_cancellation_and_failure_have_distinct_messages_and_never_leak_raw_errors(self):
        for error, message in (
            (snapshot.CheckCancelled(), "The administrator prompt was cancelled."),
            (OSError("local process name"), "Windows did not return a complete power-request report."),
        ):
            with patch.object(snapshot, "check", side_effect=error), \
                 patch.object(tray.threading, "Thread") as worker:
                self.item(self.icon, None)
                worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
            self.show.assert_called_with(self.stopped, None, message)
            summary, full = snapshot.snapshot_text(None, message, STAMP)
            self.assertIn("Check didn’t complete", summary)
            self.assertNotIn("local process name", summary)
            self.assertIsNone(full)

    def test_only_one_worker_during_query_or_result_and_none_after_exit(self):
        def try_again(*args):
            self.item(self.icon, None)

        self.show.side_effect = try_again
        with patch.object(snapshot, "check", return_value=b"report") as check, \
             patch.object(tray.threading, "Thread") as worker:
            self.item(self.icon, None)
            self.item(self.icon, None)
            worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
            check.assert_called_once()
            worker.assert_called_once()
            self.item(self.icon, None)
            self.assertEqual(worker.call_count, 2)
            worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
            self.stopped.set()
            self.item(self.icon, None)
            self.assertEqual(worker.call_count, 2)

    def test_viewer_failure_notifies_without_report_and_releases_guard(self):
        self.show.side_effect = OSError("synthetic private report")
        with patch.object(snapshot, "check", return_value=b"synthetic private report"), \
             patch.object(tray.threading, "Thread") as worker:
            self.item(self.icon, None)
            worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
            self.item(self.icon, None)
            self.assertEqual(worker.call_count, 2)
            worker.call_args.kwargs["target"](*worker.call_args.kwargs["args"])
        self.icon.notify.assert_called_with("Check didn’t complete.\nCould not open the snapshot window.",
                                          "Joro · Power requests")


class ViewerTests(unittest.TestCase):
    def test_tk_is_created_run_and_destroyed_on_main_thread(self):
        steps = []
        root = Mock()

        def note(step, result=None):
            def call():
                self.assertIs(threading.current_thread(), threading.main_thread())
                steps.append(step)
                return result
            return call

        root.mainloop.side_effect = note("run")
        root.destroy.side_effect = note("destroy")
        fake_tk = SimpleNamespace(Tk=note("create", root), scrolledtext=Mock(), ttk=Mock())
        with patch.dict(sys.modules, {"tkinter": fake_tk}):
            snapshot_viewer.display("fixture", "original", threading.Event())
        self.assertEqual(steps, ["create", "run", "destroy"])

    def test_viewer_command_contains_only_interpreter_and_fixed_script(self):
        self.assertEqual(snapshot_viewer.viewer_command(),
                         [sys.executable, "-I", snapshot_viewer.__file__])


@unittest.skipUnless(os.name == "nt", "Windows viewer subprocess required")
class ViewerProcessTests(unittest.TestCase):
    CHILD = """
import hashlib, os, runpy, sys, threading
path, parent, expected, mode = sys.argv[1:]
app = runpy.run_path(path)
def render(summary, full, tray_gone):
    assert os.getpid() != int(parent)
    assert threading.current_thread() is threading.main_thread()
    assert summary == 'headless fixture'
    assert hashlib.sha256(full.encode('utf-8')).hexdigest() == expected
    if mode == 'wait':
        assert tray_gone.wait(5)
    if mode == 'fail':
        raise SystemExit(7)
raise SystemExit(app['main'](sys.stdin.buffer, render))
"""

    def setUp(self):
        self.stopped = threading.Event()
        self.full = "unfamiliar heading:\r\n[PROCESS] vidéo.exe\r\n未分類\r\n"

    def command(self, mode):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        return [str(pythonw) if pythonw.is_file() else sys.executable,
                "-I", "-c", self.CHILD, snapshot_viewer.__file__, str(os.getpid()),
                hashlib.sha256(self.full.encode("utf-8")).hexdigest(), mode]

    def test_real_unelevated_child_receives_original_report_and_normal_or_failed_exit(self):
        for mode in ("normal", "fail"):
            with self.subTest(mode=mode), patch.object(snapshot_viewer, "viewer_command",
                                                       return_value=self.command(mode)):
                if mode == "fail":
                    with self.assertRaisesRegex(OSError, "viewer failed"):
                        snapshot_viewer.show(self.stopped, "headless fixture", self.full)
                else:
                    snapshot_viewer.show(self.stopped, "headless fixture", self.full)

    def test_actual_viewer_entrypoint_makes_no_window_without_a_payload(self):
        result = subprocess.run(snapshot_viewer.viewer_command(), input=b"", capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))

    def test_stop_signals_real_child_through_stdin_and_worker_reaps_it(self):
        result = []
        sent = threading.Event()
        original_popen = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            poll = child.poll

            def poll_after_write():
                sent.set()
                return poll()

            child.poll = poll_after_write
            return child

        def show():
            try:
                snapshot_viewer.show(self.stopped, "headless fixture", self.full)
                result.append("closed")
            except Exception as exc:
                result.append(exc)

        worker = threading.Thread(target=show, daemon=True)
        with patch.object(snapshot_viewer, "viewer_command", return_value=self.command("wait")), \
             patch.object(snapshot_viewer.subprocess, "Popen", side_effect=spawn):
            worker.start()
            try:
                self.assertTrue(sent.wait(3))
                self.assertTrue(worker.is_alive())
            finally:
                self.stopped.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, ["closed"])
        self.assertEqual([child.returncode for child in children], [0])


@unittest.skipUnless(os.name == "nt", "Windows transport required")
class WindowsTransportTests(unittest.TestCase):
    def test_shell_cancellation_is_mapped_without_launching(self):
        shell = Mock(return_value=False)
        ole = SimpleNamespace(CoInitializeEx=Mock(return_value=0), CoUninitialize=Mock())
        with patch.object(snapshot.ctypes, "WinDLL", side_effect=[
            SimpleNamespace(ShellExecuteExW=shell), ole,
        ]), patch.object(snapshot.ctypes, "get_last_error", return_value=1223):
            with self.assertRaises(snapshot.CheckCancelled):
                snapshot.launch_elevated(Path(r"D:\Test Windows\System32"), "fixture")
        ole.CoUninitialize.assert_called_once()

    def test_real_pipe_preserves_output_and_rejects_failed_exit(self):
        import _winapi as win

        children = []

        def launch(pipe):
            executable = snapshot.system_directory() / "WindowsPowerShell/v1.0/powershell.exe"
            child = subprocess.Popen([str(executable), *snapshot.query_arguments(pipe).split()],
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            children.append(child)
            current = win.GetCurrentProcess()
            return win.DuplicateHandle(current, int(child._handle), current, 0, False,
                                       win.DUPLICATE_SAME_ACCESS)

        fixture = snapshot.QUERY.replace("powercfg.exe", "cmd.exe")
        try:
            with patch.object(snapshot, "QUERY", fixture.replace("/requests", "/d /c echo DISPLAY:")):
                self.assertEqual(snapshot.capture(launch), b"DISPLAY:\r\n")
            failing = fixture.replace("/requests", "/d /c echo DISPLAY: & exit /b 2")
            with patch.object(snapshot, "QUERY", failing):
                with self.assertRaisesRegex(OSError, "complete diagnostic"):
                    snapshot.capture(launch)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    def test_cancelled_launch_cleans_up_pending_connection(self):
        launch = Mock(side_effect=snapshot.CheckCancelled)
        with self.assertRaises(snapshot.CheckCancelled):
            snapshot.capture(launch)


if __name__ == "__main__":
    unittest.main()
