# joro-awake — Required Inputs for the Packaged-Installer Boundary

> Every input the Step 1.1 inventory and the Step 1.2 packaged probe need, which step each blocks, and where it comes from. Placeholders only — never record a secret, password or identity value here.

**Stage** says when an input is actually needed. Nothing later is pulled forward: Step 1.2 runs on a development package identity on a disposable boundary, so the owner's reserved Store identity is not a Step 1.2 blocker.

| Input | Stage | Status | Acquisition step / source |
|---|---|---|---|
| Windows 10/11, x64 | Step 1.1 inventory | observed | Windows 11 25H2, build 26200.9974 on this host |
| Windows SDK `makeappx`, `signtool`, `appcert` | Step 1.1 inventory | observed | Installed under `C:\Program Files (x86)\Windows Kits\10` (SDK 10.0.19041.0). Not on `PATH`; the probe resolves the x64 binaries |
| Python 3.12+ (source specimen only) | Step 1.1 inventory | observed | 3.14.0 on this host. The packaged build must not depend on it |
| Developer mode / trusted sideload | Step 1.2 | observed | `HKLM\...\AppModelUnlock\AllowDevelopmentWithoutDevLicense = 1` |
| Claude Code, local Windows CLI | Step 1.1 inventory | observed | 2.1.278; Joro hooks already configured |
| OpenCode, local Windows CLI | Step 1.1 inventory | observed | 2.0.24; Joro plugin already configured |
| Codex, local Windows CLI | Step 1.1 inventory | observed, unconfigured | 0.155.1; `~/.codex/hooks.json` does not exist (expected first-run state) |
| Antigravity App/IDE | Step 1.1 inventory | observed, unconfigured | App 2.12.2 and IDE 2.5.5; `~/.gemini/config/hooks.json` does not exist |
| Cursor desktop app | Step 1.1 inventory | **missing** | No `cursor` command, no Cursor app path, no `~/.cursor` |
| GitHub Copilot in VS Code | Step 1.1 inventory | **missing** | VS Code 1.147.0 is present; no Copilot extension is reported by `code --list-extensions` (a stale `github.copilot-1.388.0` folder is not an installation) |
| `JORO_QA_ARM64_TARGET` | declaration, Step 1.2 | **not declared** | Operator pointer, e.g. `VM: win11-arm-qa-01 (2026-11-20)`. A declaration is not verification |
| `JORO_QA_STORE_IDENTITY_REF` | declaration, Step 3.2 / 6.2 | **not declared** | Operator pointer, e.g. `Partner Center reservation confirmed by owner 2026-11-20`. Nonsecret reserved values belong in the Step 3.2 manifest, not here |
| Native ARM64 test target | Step 1.2 and later native evidence | **missing** | Physical ARM64 device or ARM64 VM on ARM64 hardware. A `windows-11-arm` CI runner builds but is not standard-user desktop proof |
| Cursor test account | Step 1.2 | **missing** | Sign in on a disposable boundary |
| GitHub Copilot subscription and sign-in | Step 1.2 | **missing** | Subscription, extension install, sign-in, preview-hooks acceptance |
| Disposable Codex home and login | Step 1.2 | **missing** | Scratch `CODEX_HOME` plus test login, so `~/.codex` stays untouched |
| Separate Windows user for Antigravity | Step 1.2 | **missing** | Its hooks live in `~/.gemini/config/` with no documented per-user home override, so isolation needs a second Windows user (or explicit scoped consent) |
| Development package identity + test certificate | Step 1.2 | **missing, created by Step 1.2** | Generated for the disposable test boundary and installed there only; never installed on the live workstation or asked of marketed users |
| Owner authorization + reserved Store identity | Step 3.2 manifest, Step 6.2 submission | **missing** | Brother authorizes and supplies nonsecret `Name` / `Publisher` / `PublisherDisplayName`; he performs operator-only verification and submission |

## Acquisition Notes

- **Observation is not verification, and declaration is not verification.** The probe observes files, commands and OS facts; it echoes `JORO_QA_*` declarations with `declared = true, verified = false`. Neither makes the boundary ready, which is why every probe run says `readiness.status = not_established`.
- **Development identity is enough for Step 1.2.** The plan's packaged probe is disposable and must use its own identity and certificate; the Store identity is a production and submission input. Requiring it earlier would move a later gate forward without a reason.
- **Installed is not test access.** Cursor, Copilot, Codex and Antigravity each write hooks into a per-user profile. Testing them without changing Alvi's live setup needs a separate Windows user, a scratch home where the tool documents one, or explicit scoped consent.
- **A missing hook file is expected.** Codex and Antigravity are installed and unconfigured; that is the normal first-run state the setup flow exists to handle, so the inventory lists it as `pending_configuration` rather than a blocker.
