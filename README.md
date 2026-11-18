# Joro Awake

Joro Awake keeps a Windows PC awake while Claude Code or OpenCode work is in
progress, shows the actual state in the system tray, and keeps a bounded local
diagnostic log. It asks Windows for a native system/execution request; it does
not edit power plans, hold the display on, or run a network service.

**Status: source preview.** This is a small, source-only Python release. It is
usable today, but see [Limits](#limits) before relying on it. A no-Python
Windows executable is required before marketing this to non-developers and is
not part of this release.

## Requirements

- Windows 10 or 11.
- Python 3.12 or newer (developed and tested on Python 3.14).
- Claude Code and/or OpenCode, if you want activity detection for them.
- Internet access on the first install so the installer can download the tray
  dependencies into a private environment.

## Install

Download the release ZIP (`joro-awake-<version>.zip`), verify it against the
published `SHA256SUMS`, extract it, and run the installer from the extracted
folder:

```powershell
python setup-scripts/install_keep_awake.py
```

The installer copies the helper, tray, OpenCode plugin, procedure and itself
into `%LOCALAPPDATA%\Joro\keep-awake`, creates a private `.venv` there with
pystray and Pillow, adds Joro's Claude Code hooks, and installs a
`joro-keep-awake` skill for both clients. It preserves unrelated Claude settings
and hooks, does not edit OpenCode's JSONC configuration, does not start the
helper, and does not enable sign-in startup.

Then start monitoring before beginning fresh work:

```powershell
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" start
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" status
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" stop
```

Start a new Claude Code session after installing its hooks. OpenCode discovers
the installed global plugin by itself. Work that was already in flight when the
adapters loaded is not discovered retroactively.

## Tray states

| State | Meaning |
|---|---|
| Zzz | Joro is idle; Windows' aggregate query reports no sleep-prevention request. This is not a guarantee that the PC will sleep. |
| Yellow Zzz | Joro is idle, but Windows reports a sleep-prevention request. Its owner is unknown. |
| Green eye | A native keep-awake request is active for the named clients. |
| Warning | Monitoring stopped, failed, or cannot read activity or Windows sleep requests. Hover for the reason. |

An open client window alone never causes the eye: the state comes from the
helper's own report. While Joro is healthy and idle, the tray also reads Windows'
aggregate execution state with `CallNtPowerInformation(SystemExecutionState)`,
without administrator permissions. System/away-mode requirements turn Zzz yellow;
display-only requirements do not. This query does not name apps or drivers,
diagnose their cause, or detect every reason for a failed sleep/display timeout.
A query failure shows a warning and retries on the next tick. It never changes
power requests or settings. Repeated starts do not create a second icon. `stop` stops
monitoring and leaves the warning visible; the tray's **Exit** or the `exit`
command stops both the helper and the tray, but only after helper shutdown is
confirmed. If shutdown fails, the tray stays visible with a warning. Windows may
put the icon in the tray overflow area at first.

## Controls

Right-click the tray icon to see **Joro Awake · v&lt;version&gt;** in a disabled
header above a separator. The version comes from the installed `VERSION` file;
the hover tooltip stays focused on live monitoring status.
Click **Run at startup** below the separator to enable or disable
launching Joro at future Windows sign-ins. Its checkmark reads the current
Windows startup entry, including changes made through the commands below.
Changing it does not stop monitoring or start another helper. A failed read or
change leaves a warning in the tooltip and a diagnostic in the log; retry the
menu item after resolving the error.

```powershell
# Installed helper
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" start
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" status
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" stop
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" exit

# Optional: launch at future Windows sign-ins (does not start it now)
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" startup-on
python "$env:LOCALAPPDATA/Joro/keep-awake/scripts/windows/keep_awake.py" startup-off

# Refresh after an update; use exit, then start, to reload both processes
python "$env:LOCALAPPDATA/Joro/keep-awake/setup-scripts/install_keep_awake.py"
```

Uninstall only after closing active clients, from a separate PowerShell window.
The installer refuses while work is active:

```powershell
python "$env:LOCALAPPDATA/Joro/keep-awake/setup-scripts/install_keep_awake.py" --uninstall
```

Uninstall removes only files it recorded as its own and leaves modified files,
backups, runtime state, logs and the private Python environment in place.

## Log

Diagnostics go to `~/.joro/keep-awake.log`, rotated across five files of at most
1 MiB each, including the current one. Entries are lifecycle events, state
changes and errors. They never contain prompts, tool contents or credentials.

Read the log before restarting a helper or tray that disappears. It records
the paths below, but an exit code alone cannot distinguish a Python error from
an external kill, and a forced termination may leave no traceback:

| Entry | What it tells you |
|---|---|
| `start` | Which process started, with the Joro and Python versions. |
| `process_launch` | Whether startup used direct spawning or the desktop shell to avoid inheriting a terminal's Windows job. |
| `stop` | The helper ended in an orderly way, and whether that was requested or followed an error. |
| `error`, `uncaught_exception`, `uncaught_thread_exception` | A Python failure, with its exception type and traceback. |
| `library_error` | A warning/error caught inside pystray, including its traceback when available. |
| `process_output` | Tray stdout/stderr, including callback diagnostics, in bounded fragments in the same rotating log. |
| `unraisable_exception`, `tray_failed` | A callback/finalizer failure or an exception escaping the tray, including `SystemExit`. |
| `tray_loop_ended` | The tray's message loop returned without a requested exit. |
| `tray_cleanup_failed` | Tray state-file removal still failed after bounded retries; native resource cleanup continues. |
| `publisher_error` | A hook or activity report failed. It keeps the type and stack frames but never the message. |
| `peer_exited` | The helper saw the tray end, or the tray saw the helper end, with the exit code in hex and its usual meaning: clean exit, killed, native crash, logoff or shutdown. |
| `previous_run_ended_without_stop` | A start found the state file of an earlier run whose process is dead, with that run's PID and last heartbeat. |
| `stall` | Two of the helper's one-second ticks were more than ten seconds apart, as after a sleep or a freeze. |

A crash below Python may leave a stack dump in `~/.joro/crash-helper.log` or
`~/.joro/crash-tray.log`, each cut back at start once it passes 256 KiB.
Tracebacks contain local file paths. Nothing restarts a dead helper or tray;
the log records what happened.

## How it works

| Piece | Job |
|---|---|
| `scripts/windows/keep_awake.py` | Tracks live, process-owned activity files and requests that Windows remain awake while any covered work remains. Dead process identities are cleaned up. |
| `scripts/windows/joro_watcher/power.py` | Acquires, clears and closes owned Windows system/execution requests. No power-policy changes. |
| `scripts/windows/joro_watcher/tray.py` | Observes the helper's report separately, so the icon can still show a warning if the helper fails. |
| `scripts/windows/joro_watcher/event_log.py` | Serializes writers and rotation for the bounded log. |
| `integrations/opencode/index.js` | Tracks model/compaction work and managed shells; permission and form waits suspend the foreground contribution. |
| `setup-scripts/install_keep_awake.py` | Installs owned files, merges Claude hooks, installs the clients' skill and supports ownership-checked uninstall. |
| `procedures/keep-awake.md` | The agent-facing install, control, startup and uninstall procedure. |

The helper and tray are separate processes. Activity records are atomic local
files, not a database or network service. Independent work items cannot release
one another. `status` distinguishes distinct work (`active_count`) from
duplicate producer records (`claim_count`), and reports `power_requested` for a
native request that was issued, not for a guarantee that Windows will honor it.
When started inside a Windows job, Joro asks the existing desktop Explorer shell
to launch its background processes. Detaching the console alone does not escape
a terminal's kill-on-close job, so ordinary terminal or agent-host cleanup could
otherwise terminate them without a Python traceback. This needs an interactive
Windows desktop; a failed broker launch is reported rather than falling back to
the same unsafe job. No scheduled task, service or startup setting is created.

## Limits

- **Modern Standby battery protection is not endurance-verified.** Windows can
  withdraw system/execution requests on battery power. Do not claim that this
  helper guarantees unlocked, unplugged operation.
- Claude Code release depends on a matching idle-notification prompt identity.
  If the client omits it, protection stays on conservatively until session end,
  process exit or manual `stop`. Permission prompts intentionally keep
  protection.
- Only work observed after the adapters load is covered. OpenCode-managed shells
  are tracked; detached descendants are not discovered automatically. Treat a
  monitoring error as a reason to investigate, not as evidence of protection.
- Explicit sleep, lid close and critical-battery actions are not overridden.
  There is no display hold and no sleep/hibernate-timeout override.
- Tray menu interaction, tooltip rendering, other visual states, Explorer
  restart, future sign-in startup and uninstall are not fully verified on this
  release yet.

## Development

The focused suite touches only temporary directories and never changes installed
state:

```powershell
python -m unittest discover -s tests -p "test_keep_awake*.py" -v
```

The filesystem tests exercise real rotation and real report files while process
identity, native locking and the desktop shell are substituted. They are real
filesystem coverage, not native desktop proof.

## License

MIT. See [LICENSE](LICENSE). Copyright (c) 2026 Alvi (alvseek).

The installer downloads pystray, Pillow and six into a private virtual
environment; they are not bundled in this ZIP. Their licenses and the full
verbatim texts are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and
[`licenses/`](licenses/).
