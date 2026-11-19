# Packaged Windows Installer — QA Runbook

> Tells the whole story of QA-ing this module. Read top-to-bottom first time; jump-to-section later.

## Goal

Take an honest inventory of the inputs the packaged Windows installer boundary needs — six client surfaces, a native ARM64 target, MSIX build tools, a development package identity and the later Store identity — and name exactly which observed input is missing, without changing the machine.

## Preconditions

- Windows 10 or 11. The boundary is per-user desktop behaviour, so a container, WSL or CI runner is not a substitute.
- The client surfaces under test installed for the same user the helper would run as.
- The Windows SDK, for the packaging build path: `makeappx`, `signtool`, `appcert`.
- Required inputs, their stage and current state: [config/REQUIRED.md](../config/REQUIRED.md).
- Nothing to bring up first: the probe reads an already-running system. The ACT mechanism is the probe itself ([qa/README.md](../README.md)).

## Reset → Clean State

None, on purpose. This boundary is observed, not reset: the live Joro installation, the client configurations and the startup entry are the specimen, and resetting them would destroy evidence the plan forbids changing.

The probe records a fingerprint instead — installed version, helper/tray PID and creation time, the `JoroKeepAwake` startup value, and the state and SHA-256 of four config files. Comparing two fingerprints is evidence about **those fields only**. It does not prove that no other file changed, and it says nothing about whether a process is healthy.

## Inject → Realistic Data

None. Seed strategy is `none` ([seeds map](../seeds/qa-seeds-map.md)): realistic data here is a real installed client at a real version, which a fixture cannot fake. A fixture that claimed a Codex hook ran would be exactly the false evidence the plan's Step 1.2 negative controls exist to catch.

## Daily Loop / Quick Start

```powershell
# from the repository root; observes only, safe to repeat
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1

# also write the report — this is the probe's only write
powershell -NoProfile -ExecutionPolicy Bypass -File qa/scripts/probe-prerequisites.ps1 -OutFile .\inventory.json

# the probe's contract and determinism tests (scratch roots; no live profile touched)
python -m unittest qa/scripts/test_probe_prerequisites.py -v
```

Read the report in three layers:

| Field | Meaning |
|---|---|
| `inventory.blocking` | An observed gap: a client that is not installed, a version the client did not report, a config file that cannot be read, or missing SDK tools. |
| `inventory.pending_configuration` | An installed client whose hook file does not exist yet. This is the expected first-run state, not a failure. |
| `readiness.unverified_inputs` | Inputs no probe can check — accounts, disposable test boundaries, development identity, ARM64 hardware, Store authority. Each names the step it blocks. |

`readiness.status` is always `not_established`. A complete inventory means "nothing observable is missing", never "the boundary is ready".

## Act → Exercise the System

## Observe → Confirm Result

## Config Switching

Not applicable: the probe takes no configuration file. Two operator declarations arrive as environment variables and only prove that a declaration was made — see [config/REQUIRED.md](../config/REQUIRED.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `client: X not installed` | Neither the command nor the app path was found. | Install it for this user; the row's `install_evidence` says what was looked for. |
| `client: GitHub Copilot in VS Code not installed` | VS Code exists but no Copilot extension is reported by `code --list-extensions`. A stale extension folder is not an installation. | Install the Copilot extension, sign in, and accept the preview hooks feature. |
| `version: X <status>` | The client is installed but the version could not be resolved (`timeout`, `not_reported`, `unreported_format`, `unavailable`). | Fix the client's version command or raise `-CommandTimeoutMs`; do not read the row as version-verified until it reports. |
| `config: X unreadable` | The hook file exists but the hash could not be read, usually a lock. | Re-run; a persistent `unreadable` means something holds the file open. |
| `pending_configuration` entries | The client is installed and its hook file is absent. | Expected before first-run setup; it is not a blocker. |
| `build tools:` in `blocking` | `makeappx`/`signtool`/`appcert` not found under Windows Kits. | Install the Windows SDK; being off `PATH` is fine, the probe resolves SDK paths. |
| Probe exits 0 but readiness is `not_established` | Normal. The exit code reports the inventory only. | Judge readiness from `readiness.unverified_inputs`. |
| Probe fails to parse `helper.json` | The helper was mid-write. | Re-run. A persistent `unreadable` means the helper is wedged; inspect `~/.joro/keep-awake.log` before restarting anything. |

## Known Gotchas

- The probe hashes `~/.claude/settings.json`, `~/.config/opencode/opencode.jsonc`, the OpenCode plugin and `~/.codex/config.toml`; it never prints their contents. A state of `missing` is recorded, not treated as an error.
- `signtool.exe` ships per architecture. A recursive search can pick the ARM64 copy on an x64 host; the probe prefers the host architecture deliberately.
- A desktop app's version comes from its executable metadata, so a missing CLI (Cursor, Antigravity) is not a missing version and not a missing app.
- Version and extension commands run in a child PowerShell with a hard timeout; on expiry the process tree is killed, so a hung client cannot hang the probe.
- An installed client is not test access. Codex, Cursor, Copilot and Antigravity each need a disposable home or user account before their hooks can be exercised without editing the live setup.
- **Development identity vs Store identity.** Step 1.2 needs only a development package identity and a test certificate on a disposable boundary. The brother's reserved Store identity is a Step 3.2 (production manifest) and Step 6.2 (submission) input, and is deliberately not pulled forward here.
