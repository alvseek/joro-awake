---
name: joro-keep-awake
description: Install or control Joro's local Windows keep-awake helper when the user asks to keep the PC awake during Claude Code or OpenCode work, start/stop monitoring, inspect status, configure sign-in startup, or uninstall it.
---

# Joro keep-awake

Use the installed helper, not Windows power-plan edits:

```powershell
& "[JORO-PYTHON]" "[JORO-HELPER]" status
& "[JORO-PYTHON]" "[JORO-HELPER]" start
& "[JORO-PYTHON]" "[JORO-HELPER]" stop
& "[JORO-PYTHON]" "[JORO-HELPER]" exit
```

Install or refresh the local adapters with:

```powershell
& "[JORO-PYTHON]" "[JORO-INSTALLER]"
```

Only when requested, enable or disable sign-in startup:

```powershell
& "[JORO-PYTHON]" "[JORO-HELPER]" startup-on
& "[JORO-PYTHON]" "[JORO-HELPER]" startup-off
```

Uninstall only when requested, after closing active clients. Run this from a separate PowerShell window; the installer refuses to remove integrations while work is active:

```powershell
& "[JORO-PYTHON]" "[JORO-INSTALLER]" --uninstall
```

Installation creates a private tray environment with pystray/Pillow but does not start the helper or enable startup. After updating, use `exit` then `start` to reload the helper and tray. Claude hooks apply to newly started sessions; OpenCode discovers the local plugin. Start monitoring before beginning fresh work. Existing in-flight jobs are not retroactively discovered by this small version.

The tray shows Zzz when Joro is idle and Windows reports no sleep-prevention request, yellow Zzz when Joro is idle but Windows reports a system/away-mode requirement (owner unknown), and an eye for AI keep-awake work. Stopped monitoring, uncertain activity or a failed Windows query shows a warning; hover for details. The aggregate Windows check needs no administrator rights, does not name blockers, ignores display-only requirements and is not a guarantee of sleep. `stop` leaves the warning icon visible. The tray's Exit or the `exit` command stops both processes; shutdown failure leaves the warning visible. Logs are in `~/.joro/keep-awake.log`, rotated across five files of at most 1 MiB each. They record state transitions, errors with their traceback, how each process started and ended, and the exit code each of the helper and tray sees when the other ends, not prompts or tool contents. A native crash leaves a stack dump in `~/.joro/crash-helper.log` or `~/.joro/crash-tray.log`. Uninstall preserves logs and the private Python environment.

Claude protection spans permission prompts. A Stop hook is not proof of completion, so Claude releases on its idle notification (normally about a minute later, with a matching prompt ID) or session end/process exit. Missing idle identity is handled conservatively: retain protection until session end, or use manual stop. OpenCode tracks new model/compaction work and new managed shells; permission/form waits suspend the foreground contribution, not independently running shells.

To see named power requests, use **Check power requests…** in the tray and approve the separate Windows administrator prompt. Joro keeps monitoring while the check runs. The timestamped window shows a grouped summary and the full Windows report, which is not logged. Cancelling the prompt is an incomplete check, not proof that no requests exist.

`power_requested` reports a native request, not proof that Windows will honor it indefinitely. Modern Standby battery behavior is **not endurance-verified**. Never promise reliable battery/locked-screen protection from status alone. No display hold, power-plan edit, forced lock/sleep, network listener or MCP server is used.
