# joro-awake — QA Instrument

> **What this is**: the QA instrument for Joro Awake — how to take a read-only inventory of the inputs the packaged Windows installer boundary needs, where each QA artifact lives, and what is honestly still unknown.

---

## The R/I/A/O Loop

Every QA cycle here is one turn of **RESET → INJECT → ACT → OBSERVE**. This bench is unusual and says so: it *observes* a machine, it does not prepare one.

| Phase | What it means in Joro Awake | Mechanism | Status |
|---|---|---|---|
| **RESET** — back to known-clean | Nothing. The live installation, client configs and startup entry are the specimen; resetting them destroys the evidence and is forbidden by the plan. | — | not-applicable (by design) |
| **INJECT** — realistic data in | Nothing. Realistic data is a real client at a real version; a fixture would only imitate the evidence. | — | not-applicable (seed strategy `none`) |
| **ACT** — exercise the system | Run the inventory probe against the machine. | [`scripts/probe-prerequisites.ps1`](scripts/probe-prerequisites.ps1) | documented |
| **OBSERVE** — see what happened | Read the inventory: which clients are installed, what version each reports, whether its hook file exists, and which observed inputs are missing. | [`scripts/probe-prerequisites.ps1`](scripts/probe-prerequisites.ps1) · [`scripts/test_probe_prerequisites.py`](scripts/test_probe_prerequisites.py) | documented |

**The probe is an inventory, not a verdict.** It always reports `readiness.status = not_established` and lists the inputs only a person can supply. A green inventory means "nothing this probe can see is missing", not "the boundary is ready".

---

## First-Time Setup

**Prerequisites**: Windows 10/11 desktop, PowerShell 5.1+, and whichever clients the inventory should see.

1. Clone this repository (or the parent, with this submodule initialized).
2. Confirm the input list and each item's stage: [`config/REQUIRED.md`](config/REQUIRED.md).
3. Run ACT: `powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1`.
4. Read OBSERVE: `inventory.blocking` names every observed gap; `inventory.pending_configuration` names installed clients whose hooks are simply not configured yet; `readiness.unverified_inputs` names what no probe can check.

---

## Daily Loop

```powershell
# ACT — observes only; writes nothing unless -OutFile is given
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1

# same, and write the report (the probe's only write)
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1 -OutFile .\inventory.json

# OBSERVE — the probe's contract and determinism tests
python -m unittest qa/scripts/test_probe_prerequisites.py -v

# product regression, unchanged by this bench
python -m unittest discover -s tests -p "test_keep_awake*.py" -v
```

---

## Where Everything Lives

| Layer | What | Where | Built by |
|---|---|---|---|
| Per-module "how to run" | Runbooks | [runbooks/](runbooks/) | /build-qa-bench |
| Per-feature verification | Checklists | [checklists/](checklists/) | /generate-qa-checklist |
| Per-stage test preconditions | Fixtures | [fixtures/](fixtures/) | /integration-test |
| R/I/A/O scripts | Scripts | [scripts/](scripts/) | /build-qa-bench |
| Required inputs + templates | Config | [config/](config/) | /build-qa-bench |
| Test-data sources | Seeds | [seeds/](seeds/) | /build-qa-bench |
| What exists + maturity audit | Map | [qa-map.md](qa-map.md) | /map-qa-instrument |

There is no `playbook.md`: one application, one runbook.

---

## Config Switching

Not applicable. The probe takes no configuration file and has no local↔deployed swap. Two environment variables (`JORO_QA_ARM64_TARGET`, `JORO_QA_STORE_IDENTITY_REF`) carry operator *declarations*; they are echoed as `declared = true, verified = false` and never change the inventory result. See [`config/REQUIRED.md`](config/REQUIRED.md).

---

## Known Gaps / Debts

- The inventory is red: Cursor is not installed, and no active GitHub Copilot extension exists in VS Code. Both are human inputs, not probe defects.
- Codex and Antigravity are installed but unconfigured. That is the expected first-run state and is reported as `pending_configuration`, not as a failure.
- Readiness is not established and this probe cannot establish it: account access, hook trust, disposable test isolation and Store authority are outside its reach. → human inputs in `config/REQUIRED.md`.
- The probe covers the Step 1.1 inventory only. It is not a packaged-integration test and does not attempt Step 1.2.
- Two-run fingerprint equality covers only the listed files, PIDs, version and startup value. It is not proof that no live file changed or that a process is healthy; nothing observed here is a health check.
- Fixtures are tribal (inline in `tests/`) and no per-feature checklist exists yet. → /integration-test and /generate-qa-checklist, when the packaged path exists.

---

## Where To Go Next

- Operate the boundary → the [packaged Windows installer runbook](runbooks/packaged-windows-installer.md).
- See what exists and how mature → [qa-map.md](qa-map.md).
- Build a missing piece → `/build-qa-bench` (rig, runbooks) or `/integration-test` (fixtures, tests).
- Run the verification → `/run-qa-test`.
