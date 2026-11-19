"""Contract and determinism tests for the read-only prerequisite inventory probe.

Two layers:

- ``ProbeContract`` runs the probe against the live machine. It asserts the report
  shape, the exit-code contract, and that a second run preserves the *observed*
  fingerprint. It deliberately asserts nothing about which clients or config files
  exist: a clean account with no client configuration is a valid result.
- ``ProbeDeterminism`` points the probe at scratch roots under the system temp
  directory, so absent configuration, a changed file, an unversioned command and a
  hung command are exercised without writing to any live profile.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).with_name("probe-prerequisites.ps1")
POWERSHELL = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass"]
CLIENT_FIELDS = (
    "name", "surface", "installed", "install_evidence", "version", "version_status",
    "version_raw", "config_label", "config_path", "config_state", "status", "blocking",
)


def run_probe(home: Path | None = None, appdata: Path | None = None, out_file: Path | None = None):
    arguments = POWERSHELL + ["-File", str(SCRIPT)]
    if home is not None:
        arguments += ["-HomeRoot", str(home)]
    if appdata is not None:
        arguments += ["-AppDataRoot", str(appdata)]
    if out_file is not None:
        arguments += ["-OutFile", str(out_file)]
    completed = subprocess.run(arguments, capture_output=True, text=True, timeout=240)
    return completed.returncode, json.loads(completed.stdout)


def evaluate(expression: str):
    """Dot-source the probe (which skips its main body) and evaluate one expression."""
    command = f". '{SCRIPT}'; {expression} | ConvertTo-Json -Depth 6 -Compress"
    completed = subprocess.run(POWERSHELL + ["-Command", command], capture_output=True, text=True, timeout=120)
    if completed.returncode != 0:
        raise AssertionError(f"PowerShell failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout)


def client_observation(**overrides):
    base = {
        "name": "Fixture",
        "surface": "test surface",
        "installed": "$true",
        "evidence": "@('app: fixture.exe')",
        "config_label": "hook file",
        "config_path": "C:\\scratch\\hooks.json",
        "config_state": "'present'",
        "version_status": "'reported'",
        "version": "'1.2.3'",
        "version_raw": "'1.2.3'",
    }
    base.update(overrides)
    return (
        "New-ClientObservation -Name '{name}' -Surface '{surface}' -Installed {installed} "
        "-InstallEvidence {evidence} -ConfigLabel '{config_label}' -ConfigPath '{config_path}' "
        "-ConfigState {config_state} -VersionStatus {version_status} -Version {version} "
        "-VersionRaw {version_raw}".format(**base)
    )


class ProbeContract(unittest.TestCase):
    """Live-machine contract: shape and preservation, not a particular inventory."""

    @classmethod
    def setUpClass(cls):
        cls.first_code, cls.first = run_probe()
        cls.second_code, cls.second = run_probe()

    def test_report_shape(self):
        for key in ("probe", "mode", "writes", "host", "clients", "build_tools",
                    "inventory", "inventory_complete", "readiness", "live_install", "fingerprint"):
            self.assertIn(key, self.first)
        self.assertEqual(self.first["mode"], "inventory_only")
        self.assertEqual(self.first["writes"], [])
        self.assertIsInstance(self.first["inventory"]["blocking"], list)
        self.assertIsInstance(self.first["inventory"]["pending_configuration"], list)
        self.assertEqual(self.first["inventory"]["required_clients"], 6)
        self.assertEqual(len(self.first["clients"]), 6)
        for client in self.first["clients"]:
            for field in CLIENT_FIELDS:
                self.assertIn(field, client, f"{client.get('name')} has no {field}")
            self.assertIsInstance(client["installed"], bool)
            self.assertIsInstance(client["blocking"], list)

    def test_exit_code_matches_inventory(self):
        complete = self.first["inventory_complete"]
        self.assertEqual(complete, not self.first["inventory"]["blocking"])
        self.assertEqual(self.first_code, 0 if complete else 1)
        self.assertEqual(self.second_code, self.first_code)

    def test_readiness_is_never_declared_from_observations(self):
        readiness = self.first["readiness"]
        self.assertEqual(readiness["status"], "not_established")
        self.assertTrue(readiness["unverified_inputs"])
        for item in readiness["unverified_inputs"]:
            self.assertIs(item["verified"], False)
            self.assertIn("blocks", item)

    def test_second_run_preserves_observed_fields(self):
        # Equality is claimed only for the fields the probe actually observes.
        self.assertEqual(self.first["fingerprint"], self.second["fingerprint"])
        for name, observation in self.first["fingerprint"]["files"].items():
            self.assertEqual(observation["state"], self.second["fingerprint"]["files"][name]["state"])
            if observation["state"] == "present":
                self.assertIsNotNone(observation["sha256"])
            else:
                self.assertIsNone(observation["sha256"])

    def test_unconfigured_client_is_not_treated_as_absent(self):
        for client in self.first["clients"]:
            if client["installed"] and client["config_state"] == "missing":
                self.assertTrue(client["status"].startswith("present_"), client["status"])
                self.assertNotIn("client:", " ".join(client["blocking"]))


class ProbeDeterminism(unittest.TestCase):
    """Scratch-root cases that must not depend on this machine's profile."""

    def test_absent_configuration_is_missing_not_unreadable(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "home"
            appdata = Path(scratch) / "appdata"
            home.mkdir()
            appdata.mkdir()
            code, report = run_probe(home=home, appdata=appdata)
            for name, observation in report["fingerprint"]["files"].items():
                self.assertEqual(observation["state"], "missing", name)
                self.assertIsNone(observation["sha256"], name)
            self.assertEqual(code, 0 if report["inventory_complete"] else 1)

    def test_changed_file_is_observed_across_runs(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "home"
            appdata = Path(scratch) / "appdata"
            (home / ".claude").mkdir(parents=True)
            appdata.mkdir()
            settings = home / ".claude" / "settings.json"
            settings.write_text("{}", encoding="utf-8")
            _, first = run_probe(home=home, appdata=appdata)
            settings.write_text('{"hooks": {}}', encoding="utf-8")
            _, second = run_probe(home=home, appdata=appdata)
            first_file = first["fingerprint"]["files"]["claude_settings"]
            second_file = second["fingerprint"]["files"]["claude_settings"]
            self.assertEqual(first_file["state"], "present")
            self.assertEqual(second_file["state"], "present")
            self.assertNotEqual(first_file["sha256"], second_file["sha256"])
            # Nothing else in the observed fingerprint moved.
            other_first = {k: v for k, v in first["fingerprint"]["files"].items() if k != "claude_settings"}
            other_second = {k: v for k, v in second["fingerprint"]["files"].items() if k != "claude_settings"}
            self.assertEqual(other_first, other_second)

    def test_unversioned_command_is_qualified_not_satisfied(self):
        report = evaluate(client_observation(version_status="'not_reported'", version="$null", version_raw="$null"))
        self.assertEqual(report["status"], "present_configured_version_unverified")
        self.assertTrue(any(item.startswith("version:") for item in report["blocking"]))

    def test_installed_app_without_cli_is_not_absent(self):
        report = evaluate(client_observation(
            name="Cursor",
            version_status="'unavailable'",
            version="$null",
            version_raw="$null",
            config_state="'missing'",
        ))
        self.assertIs(report["installed"], True)
        self.assertNotEqual(report["status"], "absent")
        self.assertEqual(report["status"], "present_unconfigured_version_unverified")

    def test_hung_command_is_bounded(self):
        started = time.monotonic()
        result = evaluate(
            "$r = Invoke-BoundedCommand -File 'powershell.exe' "
            "-Arguments @('-NoProfile','-Command','Start-Sleep -Seconds 30') -TimeoutMs 800; "
            "[pscustomobject]@{ status = $r.status }"
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "timeout")
        self.assertLess(elapsed, 20, "the bounded command did not return promptly")

    def test_out_file_is_reported_as_a_write(self):
        with tempfile.TemporaryDirectory() as scratch:
            home = Path(scratch) / "home"
            appdata = Path(scratch) / "appdata"
            home.mkdir()
            appdata.mkdir()
            report_path = Path(scratch) / "inventory.json"
            _, report = run_probe(home=home, appdata=appdata, out_file=report_path)
            self.assertEqual(len(report["writes"]), 1)
            self.assertIn("inventory.json", report["writes"][0])
            self.assertTrue(report_path.exists())


if __name__ == "__main__":
    unittest.main()
