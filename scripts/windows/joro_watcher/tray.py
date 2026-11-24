"""Independent tray observer: never derives activity from open application windows."""

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from joro_watcher.event_log import failure as error_fields, record
from joro_watcher.power import system_execution_state
from joro_watcher import power_snapshot, snapshot_viewer


def presentation(value):
    if value.get("error"):
        return "warning", "Joro: warning - " + value["error"]
    if value.get("diagnostic_error"):
        return "warning", "Joro: warning - " + value["diagnostic_error"]
    if not value.get("running"):
        return "warning", "Joro: monitoring stopped - Windows sleep requests unknown"
    if value.get("uncertain_claims"):
        return "warning", "Joro: activity uncertain - keeping awake conservatively"
    if value.get("power_requested"):
        names = {"claude": "Claude Code", "opencode": "OpenCode"}
        clients = ", ".join(names.get(c, c) for c in value.get("clients", []))
        return "awake", "Joro: keeping awake" + (": " + clients if clients else "")
    if value.get("windows_sleep_requested") is True:
        return "blocked", "Joro: idle; Windows reports a sleep-prevention request from other apps"
    if value.get("windows_sleep_requested") is not False:
        return "warning", "Joro: idle; Windows sleep requests unknown"
    return "idle", "Joro: idle; no Windows sleep-prevention request detected"


def observe_sleep_requests(value):
    """Only query when healthy and AI-idle; query failures must never look idle."""
    if (value.get("error") or value.get("diagnostic_error") or not value.get("running")
            or value.get("uncertain_claims") or value.get("power_requested")):
        return value
    try:
        # EXECUTION_STATE flags, not POWER_REQUEST_TYPE enum values.
        # Display-only requests do not mean automatic system sleep is prevented.
        flags = system_execution_state()
        return {**value, "windows_sleep_requested": bool(flags & (0x1 | 0x40))}
    except Exception:
        return {**value, "windows_sleep_requested": None}


def observe(helper):
    """Read the helper's own report; unlike status(), do not reconcile claims."""
    try:
        value = json.loads((helper.ROOT / "helper.json").read_text(encoding="utf-8"))
        if helper.birth(value["pid"]) != value["birth"]:
            return {"running": False}
        if time.time() - value["updated_at"] > 10:
            return {"error": "helper is not reporting"}
        return {**value, "running": True}
    except FileNotFoundError:
        error = helper.ROOT / "helper-error.json"
        if error.exists():
            return {"error": "helper failed; see ~/.joro/keep-awake.log"}
        return {"running": False}
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "cannot read helper status"}


def images():
    from PIL import Image, ImageDraw, ImageFont
    result = {}
    for name in ("idle", "blocked", "awake", "warning"):
        image = Image.new("RGBA", (64, 64))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, 62, 62), radius=14,
                               fill={"idle": "#334155", "blocked": "#eab308",
                                     "awake": "#047857", "warning": "#92400e"}[name])
        if name == "awake":
            draw.ellipse((9, 20, 55, 44), fill="white")
            draw.ellipse((24, 23, 40, 41), fill="#064e3b")
        elif name == "warning":
            draw.polygon(((32, 9), (57, 53), (7, 53)), fill="#fef3c7")
            draw.line((32, 23, 32, 36), fill="#92400e", width=5)
            draw.ellipse((30, 42, 34, 46), fill="#92400e")
        else:
            font = ImageFont.load_default(size=23)
            draw.text((6, 18), "Zzz", fill="#422006" if name == "blocked" else "white", font=font)
        result[name] = image
    return result


def stop_helper(helper):
    result = subprocess.run([sys.executable, str(Path(helper.__file__).resolve()), "stop"],
                            capture_output=True, text=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    value = json.loads(result.stdout)
    if value.get("running") is not False:
        raise RuntimeError("Helper shutdown was not confirmed")


def update(icon, icons, value):
    state, title = presentation(value)
    icon.icon, icon.title = icons[state], title[:127]
    return state, title


def exit_monitoring(helper, icon, icons, stopped, failure):
    try:
        stop_helper(helper)
    except Exception as exc:
        failure["error"] = "could not stop helper; retry Exit"
        update(icon, icons, failure)
        record("exit_failed", source="tray", **error_fields(exc))
        return False
    record("exit", source="tray")
    stopped.set()
    icon.stop()
    return True


def observe_once(helper, icon, icons, failure, previous):
    try:
        value = observe_sleep_requests(failure or observe(helper))
        state, title = presentation(value)
        signature = (state, title)
        if signature != previous:
            update(icon, icons, value)
            log_error = record("tray_state", state=state, detail=title)
            if log_error:
                update(icon, icons, {"error": log_error})
                return None  # Retry logging on the next tick.
        return signature
    except Exception as exc:
        # Never leave an eye frozen on screen after the observer has failed.
        try:
            update(icon, icons, {"error": "observer failed; retrying"})
        except Exception:
            pass  # A shell/UI failure is retried on the next observer tick too.
        record("observer_failed", source="tray", **error_fields(exc))
        return None


def beat(helper, identity, healthy):
    """Refresh the tray's heartbeat; a write failure is recorded once and never ends the observer."""
    try:
        helper.write(helper.ROOT / "tray.json", {**identity, "updated_at": time.time()})
        return True
    except Exception as exc:
        if healthy:
            record("heartbeat_failed", source="tray", **error_fields(exc, message=False))
        return False


def startup_menu_item(pystray, helper, failure):
    """Read Windows when rebuilding the menu or clicking, not a saved preference."""
    message = "could not read or change startup; retry Run at startup"

    def checked(item):
        try:
            return helper.startup_enabled()
        except Exception as exc:
            failure["error"] = message
            record("startup_failed", source="tray", **error_fields(exc))
            return None  # Unknown, not a successful read of disabled.

    def toggle(icon, item):
        try:
            enabled = not helper.startup_enabled()
            helper.startup(enabled)
            if helper.startup_enabled() != enabled:
                raise RuntimeError("Startup change was not confirmed")
            if failure.get("error") == message:
                failure.pop("error")
            record("startup_changed", source="tray", enabled=enabled)
        except Exception as exc:
            failure["error"] = message
            record("startup_failed", source="tray", **error_fields(exc))
        icon.update_menu()

    return pystray.MenuItem("Run at startup", toggle, checked=checked)


def refresh_startup_menu(helper, icon, previous):
    """Windows caches native menus; rebuild when the registry changes externally."""
    try:
        current = helper.startup_enabled()
    except Exception:
        current = None  # The checked callback records the failure and warns.
    if current != previous:
        try:
            icon.update_menu()
        except Exception as exc:
            record("startup_menu_failed", source="tray", **error_fields(exc))
            return previous  # Retry without ending the heartbeat/activity observer.
    return current


def show_power_snapshot(stopped, output, error):
    summary, full = power_snapshot.snapshot_text(output, error, datetime.now().astimezone())
    snapshot_viewer.show(stopped, summary, full)


def snapshot_menu_item(pystray, stopped, show):
    busy = threading.Lock()

    def run_check(icon):
        try:
            if stopped.is_set():
                return
            try:
                output, error = power_snapshot.check(), None
            except power_snapshot.CheckCancelled:
                output, error = None, "The administrator prompt was cancelled."
            except (OSError, ValueError):
                output, error = None, "Windows did not return a complete power-request report."
            if not stopped.is_set():
                try:
                    show(stopped, output, error)
                except (OSError, ValueError):
                    if not stopped.is_set():
                        try:
                            icon.notify("Check didn’t complete.\n" + (error or "Could not open the snapshot window."),
                                        "Joro · Power requests")
                        except Exception:
                            pass  # A notification failure must not affect the tray or the guard.
        finally:
            busy.release()

    def start(icon, item):
        if stopped.is_set() or not busy.acquire(blocking=False):
            return
        try:
            threading.Thread(target=run_check, args=(icon,), name="joro-power-snapshot", daemon=True).start()
        except RuntimeError:
            busy.release()
            raise

    return pystray.MenuItem("Check power requests…", start)


def run(helper):
    import pystray
    mutex = helper.K.CreateMutexW(None, False, "Local\\JoroTray-" + helper.KEY)
    if not mutex:
        raise OSError("Cannot create tray mutex")
    if helper.K.WaitForSingleObject(mutex, 0) not in (0, 0x80):
        helper.K.CloseHandle(mutex)
        return
    stopped = threading.Event()
    exit_event = helper.named_event("-tray-exit")
    helper.K.ResetEvent(exit_event)
    icons = images()
    failure = {}
    icon = pystray.Icon("Joro", icons["warning"], "Joro: starting",
                        menu=pystray.Menu(pystray.MenuItem(
                                             "Joro Awake · v" + helper.version(), None, enabled=False),
                                          pystray.Menu.SEPARATOR,
                                          startup_menu_item(pystray, helper, failure),
                                          snapshot_menu_item(pystray, stopped, show_power_snapshot),
                                          pystray.Menu.SEPARATOR,
                                          pystray.MenuItem("Exit", lambda: exit_monitoring(
                            helper, icon, icons, stopped, failure))))
    peer = helper.Peer("helper", "helper.json")
    identity = {"pid": os.getpid(), "birth": helper.birth(os.getpid())}

    def monitor():
        previous, healthy = None, True
        previous_startup = object()
        while not stopped.is_set():
            if helper.K.WaitForSingleObject(exit_event, 0) == 0:
                record("exit", source="tray", reason="command")
                stopped.set()
                icon.stop()
                return
            previous_startup = refresh_startup_menu(helper, icon, previous_startup)
            previous = observe_once(helper, icon, icons, failure, previous)
            peer.poll("tray")
            healthy = beat(helper, identity, healthy)
            stopped.wait(1)

    thread = threading.Thread(target=monitor, name="joro-tray-observer", daemon=True)
    try:
        helper.ended_without_stop("tray", "tray.json")
        record("start", source="tray", pid=os.getpid(), version=helper.version(),
               python=sys.version.split()[0])
        def setup(ready_icon):
            try:
                ready_icon.visible = True
                beat(helper, identity, True)
                thread.start()
                ready = helper.named_event("-tray-ready")
                try:
                    helper.K.SetEvent(ready)
                finally:
                    helper.K.CloseHandle(ready)
            except Exception as exc:
                record("tray_setup_failed", source="tray", **error_fields(exc))
                stopped.set()
                icon.stop()
        icon.run(setup=setup)
        if not stopped.is_set():
            record("tray_loop_ended", source="tray", reason="unexpected")
    finally:
        stopped.set()
        if thread.ident is not None:
            thread.join(timeout=3)
        try:
            helper.remove_claim(helper.ROOT / "tray.json")
        except Exception as exc:
            # A locked status file is not a reason for an otherwise clean UI exit
            # to fail, or to skip releasing the remaining native resources.
            record("tray_cleanup_failed", source="tray", **error_fields(exc))
        finally:
            peer.close()
            helper.K.ReleaseMutex(mutex)
            helper.K.CloseHandle(mutex)
            helper.K.CloseHandle(exit_event)
