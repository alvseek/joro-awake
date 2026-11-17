"""Focused helper-state tests; no global client config or startup changes."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

WINDOWS = Path(__file__).resolve().parents[1] / "scripts/windows"
sys.path.insert(0, str(WINDOWS))
spec = importlib.util.spec_from_file_location("keep_awake", WINDOWS / "keep_awake.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class KeepAwakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="joro-helper-test-")
        self.root = Path(self.temp.name)
        self.patches = [patch.object(helper, "ROOT", self.root),
                        patch.object(helper, "CLAIMS", self.root / "claims")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def event(self, **changes):
        return {"client": "opencode", "producer": "test", "work": "one",
                "active": True, "pid": os.getpid(), **changes}

    def test_one_work_item_cannot_release_another(self):
        helper.claim(self.event())
        helper.claim(self.event(work="two"))
        helper.claim(self.event(active=False))
        self.assertEqual(helper.state()["active_count"], 1)
        helper.claim(self.event(work="two", active=False))
        self.assertEqual(helper.state()["active_count"], 0)

    def test_producer_instances_are_independent(self):
        helper.claim(self.event())
        helper.claim(self.event(producer="another"))
        self.assertEqual(helper.state()["active_count"], 1)
        self.assertEqual(helper.state()["claim_count"], 2)
        helper.claim(self.event(active=False))
        self.assertEqual(helper.state()["active_count"], 1)

    def test_stale_idle_notification_cannot_clear_new_prompt(self):
        helper.claim(self.event(client="claude", prompt="new"))
        helper.claim(self.event(client="claude", prompt="old", active=False, check_prompt=True))
        self.assertEqual(helper.state()["active_count"], 1)
        helper.claim(self.event(client="claude", prompt="new", active=False, check_prompt=True))
        self.assertEqual(helper.state()["active_count"], 0)

    def test_unknown_prompt_does_not_clear_active_work(self):
        helper.claim(self.event(prompt="new"))
        helper.claim(self.event(active=False, check_prompt=True))
        self.assertEqual(helper.state()["active_count"], 1)

    def test_dead_or_reused_pid_is_removed_without_signalling(self):
        helper.claim(self.event())
        with patch.object(helper, "birth", return_value=None):
            self.assertEqual(helper.state()["active_count"], 0)
        self.assertEqual(list((self.root / "claims").glob("*.json")), [])

    def test_unknown_owner_is_reported_not_called_idle(self):
        helper.claim(self.event())
        with patch.object(helper, "birth", side_effect=OSError("access denied")):
            value = helper.state()
        self.assertEqual(value["uncertain_claims"], 1)
        self.assertTrue(list((self.root / "claims").glob("*.json")))

    def test_claude_stop_is_not_a_terminal_event(self):
        with patch.object(helper, "claude_owner", return_value=os.getpid()):
            common = {"session_id": "qa", "prompt_id": "turn"}
            helper.claude_hook({**common, "hook_event_name": "UserPromptSubmit"})
            helper.claude_hook({**common, "hook_event_name": "Stop"})
            self.assertEqual(helper.state()["active_count"], 1)
            helper.claude_hook({**common, "hook_event_name": "Notification", "notification_type": "idle_prompt"})
            self.assertEqual(helper.state()["active_count"], 0)

    def test_claude_session_end_clears_its_claim(self):
        with patch.object(helper, "claude_owner", return_value=os.getpid()):
            helper.claude_hook({"session_id": "qa", "hook_event_name": "UserPromptSubmit"})
            helper.claude_hook({"session_id": "qa", "hook_event_name": "SessionEnd"})
        self.assertEqual(helper.state()["active_count"], 0)

    def test_repeated_release_is_safe(self):
        helper.claim(self.event())
        helper.claim(self.event(active=False))
        helper.claim(self.event(active=False))
        self.assertEqual(helper.state()["active_count"], 0)

    def test_release_retries_a_windows_reader_lock(self):
        helper.claim(self.event())
        original = Path.unlink
        attempts = []

        def locked_once(path, **kwargs):
            attempts.append(path)
            if len(attempts) == 1:
                raise PermissionError("reader holds claim")
            return original(path, **kwargs)

        with patch.object(Path, "unlink", locked_once), patch.object(helper.time, "sleep") as sleep:
            helper.claim(self.event(active=False))
        self.assertEqual(len(attempts), 2)
        sleep.assert_called_once_with(0.05)
        self.assertEqual(helper.state()["active_count"], 0)

    def test_release_reports_persistent_lock_and_preserves_claim(self):
        helper.claim(self.event())
        with patch.object(Path, "unlink", side_effect=PermissionError("locked")) as unlink, \
             patch.object(helper.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                helper.claim(self.event(active=False))
        self.assertEqual(unlink.call_count, 40)
        self.assertEqual(sleep.call_count, 39)
        self.assertEqual(helper.state()["active_count"], 1)

    def test_release_survives_a_real_open_windows_reader(self):
        helper.claim(self.event())
        (path,) = (self.root / "claims").glob("*.json")
        with path.open("rb") as reader:
            with self.assertRaises(PermissionError):
                path.unlink()
            with patch.object(helper.time, "sleep", side_effect=lambda _: reader.close()) as sleep:
                helper.claim(self.event(active=False))
            sleep.assert_called_once_with(0.05)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
