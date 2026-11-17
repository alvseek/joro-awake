"""Small local keep-awake helper. Native requests are not a battery guarantee."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import winreg

from joro_watcher.power import PowerRequest
from joro_watcher.event_log import describe_exit, enable_fault_log, failure, install_hooks, record, timestamp
from joro_watcher import event_log

ROOT = Path(os.environ["LOCALAPPDATA"]) / "Joro" / "keep-awake-state"
CLAIMS = ROOT / "claims"
KEY = hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:20]
CLOCK, WALL = time.monotonic, time.time  # a sleep must show even if one clock pauses through it
STALL_LIMIT = 10  # the staleness limit the tray already applies to the helper's heartbeat
K = ctypes.WinDLL("kernel32", use_last_error=True)
for _name, _args, _result in (
    ("OpenProcess", [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
    ("GetProcessTimes", [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
    ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ("CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
    ("ReleaseMutex", [wintypes.HANDLE], wintypes.BOOL),
    ("CreateEventW", [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
    ("SetEvent", [wintypes.HANDLE], wintypes.BOOL),
    ("ResetEvent", [wintypes.HANDLE], wintypes.BOOL),
    ("WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    ("CreateToolhelp32Snapshot", [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
    ("GetExitCodeProcess", [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
):
    _fn = getattr(K, _name)
    _fn.argtypes, _fn.restype = _args, _result


def birth(pid):
    if type(pid) is not int or pid <= 0:
        raise ValueError("Invalid owner PID")
    handle = K.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        waited = K.WaitForSingleObject(handle, 0)
        if waited == 0:
            return None
        if waited != 0x102:
            raise ctypes.WinError(ctypes.get_last_error())
        return created(handle)
    finally:
        K.CloseHandle(handle)


def created(handle):
    times = [wintypes.FILETIME() for _ in range(4)]
    if not K.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return times[0].dwLowDateTime | times[0].dwHighDateTime << 32


def watch(pid, started):
    """Keep a handle to the process so its exit code can still be read after it ends."""
    handle = K.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if created(handle) == started:
            return handle
    except BaseException:
        K.CloseHandle(handle)
        raise
    K.CloseHandle(handle)
    return None


class Peer:
    """The other Joro process: its identity comes from a state file, its end from its handle."""

    def __init__(self, name, filename):
        self.name, self.filename, self.handle, self.pid, self.seen = name, filename, None, None, None

    def poll(self, source):
        try:
            if self.handle is None:
                self.attach()
            elif K.WaitForSingleObject(self.handle, 0) == 0:
                self.report(source)
        except Exception as exc:
            record("peer_watch_failed", source=source, peer=self.name, **failure(exc, message=False))

    def attach(self):
        try:
            value = json.loads((ROOT / self.filename).read_text(encoding="utf-8"))
            identity = (value["pid"], value["birth"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        if identity == self.seen:
            return
        self.seen = identity  # one attempt per process identity, so a failure is not retried every tick
        self.handle, self.pid = watch(*identity), identity[0]

    def report(self, source):
        code, handle, self.handle = wintypes.DWORD(), self.handle, None
        try:
            if not K.GetExitCodeProcess(handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            K.CloseHandle(handle)
        record("peer_exited", source=source, peer=self.name, pid=self.pid, **describe_exit(code.value))

    def close(self):
        if self.handle is not None:
            K.CloseHandle(self.handle)
            self.handle = None


def ended_without_stop(source, filename):
    """A clean stop deletes the state file, so a leftover one whose process is dead is exact evidence."""
    try:
        value = json.loads((ROOT / filename).read_text(encoding="utf-8"))
        if birth(value["pid"]) != value["birth"]:
            record("previous_run_ended_without_stop", source=source, previous_pid=value["pid"],
                   last_heartbeat=timestamp(value.get("updated_at")))
    except FileNotFoundError:
        pass
    except Exception as exc:
        record("unclean_check_failed", source=source, **failure(exc, message=False))


def version():
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes((json.dumps(value, indent=2) + "\n").encode())
    # Any process holding the target open — a status call, antivirus, an editor — makes the
    # replace fail with a sharing violation on Windows, so retry briefly rather than end the helper.
    for attempt in range(40):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 39:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def remove_claim(path):
    # The helper reads claims every second; Windows can refuse deletion while a
    # reader holds the file. Use the same bounded retry as atomic publication.
    for attempt in range(40):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def claim(data):
    if data.get("client") not in ("claude", "opencode") or type(data.get("active")) is not bool:
        raise ValueError("Invalid activity event")
    if any(not isinstance(data.get(k), str) or not 0 < len(data[k]) <= 512 for k in ("producer", "work")):
        raise ValueError("Invalid work identity")
    started = birth(data.get("pid"))
    if started is None:
        return  # A dead publisher cannot create a live claim.
    identity = [data["client"], data["pid"], started, data["producer"], data["work"]]
    name = hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ".json"
    path = CLAIMS / name
    if data["active"]:
        write(path, {"client": data["client"], "pid": data["pid"], "birth": started,
                     "producer": data["producer"], "work": data["work"],
                     "prompt": data.get("prompt")})
    elif path.exists():
        # A late idle notification must not erase a newer prompt's active claim.
        if data.get("check_prompt"):
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            if not data.get("prompt") or previous.get("prompt") != data["prompt"]:
                return
        remove_claim(path)


class ProcessEntry(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                ("exe", wintypes.WCHAR * 260)]


def claude_owner():
    snapshot = K.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    parents = {}
    try:
        entry = ProcessEntry(size=ctypes.sizeof(ProcessEntry))
        for name in ("Process32FirstW", "Process32NextW"):
            fn = getattr(K, name)
            fn.argtypes, fn.restype = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL
        more = K.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            parents[entry.pid] = (entry.parent, entry.exe.casefold())
            more = K.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        K.CloseHandle(snapshot)
    pid, seen = os.getppid(), set()
    while pid in parents and pid not in seen:
        seen.add(pid)
        parent, exe = parents[pid]
        if exe == "claude.exe":
            return pid
        pid = parent
    raise RuntimeError("Cannot identify the owning Claude Code process")


def claude_hook(data):
    event = data.get("hook_event_name")
    if event not in ("UserPromptSubmit", "PreToolUse", "Notification", "SessionEnd"):
        return
    if event == "Notification" and data.get("notification_type") != "idle_prompt":
        return
    claim({"client": "claude", "producer": "hooks", "work": data["session_id"],
           "pid": claude_owner(), "active": event in ("UserPromptSubmit", "PreToolUse"),
           "prompt": data.get("prompt_id"), "check_prompt": event == "Notification"})


def state():
    active, uncertain = [], 0
    CLAIMS.mkdir(parents=True, exist_ok=True)
    for path in CLAIMS.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("client") not in ("claude", "opencode") or type(value.get("birth")) is not int:
                raise ValueError("Invalid claim")
            if birth(value["pid"]) != value["birth"]:
                remove_claim(path)
            else:
                active.append(value)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            uncertain += 1  # Be conservative, but do not report failed observation as healthy.
    distinct = {(v["client"], v["pid"], v["birth"], v["work"]) for v in active}
    return {"active_count": len(distinct), "claim_count": len(active),
            "clients": sorted({v["client"] for v in active}),
            "uncertain_claims": uncertain}


def named_event(suffix):
    handle = K.CreateEventW(None, True, False, "Global\\JoroKeepAwake-" + KEY + suffix)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def status():
    try:
        value = json.loads((ROOT / "helper.json").read_text(encoding="utf-8"))
        if birth(value["pid"]) == value["birth"]:
            return {**value, **state(), "running": True}
    except (FileNotFoundError, KeyError):
        pass
    return {"running": False, **state(), "power_requested": False}


def run():
    ROOT.mkdir(parents=True, exist_ok=True)
    mutex = K.CreateMutexW(None, False, "Global\\JoroKeepAwake-" + KEY)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if K.WaitForSingleObject(mutex, 0) not in (0, 0x80):
        K.CloseHandle(mutex)
        return
    stop, ready, request, reason = None, None, None, "error"
    peer = Peer("tray", "tray.json")
    try:
        stop, ready = named_event("-stop"), named_event("-ready")
        K.ResetEvent(stop)
        (ROOT / "helper-error.json").unlink(missing_ok=True)
        ended_without_stop("helper", "helper.json")
        record("start", source="helper", pid=os.getpid(), version=version(),
               python=sys.version.split()[0])
        info = {"pid": os.getpid(), "birth": birth(os.getpid()), "power_requested": False,
                "battery_protection": "unverified; Modern Standby battery limits still apply"}
        write(ROOT / "helper.json", {**info, "updated_at": time.time()})
        try:
            start_tray()
        except Exception as exc:
            # The visual observer is not a prerequisite for keeping the machine awake.
            record("tray_start_failed", source="helper", **failure(exc))
        K.SetEvent(ready)
        previous, last_tick, last_wall = None, CLOCK(), WALL()
        while True:
            now, wall = CLOCK(), WALL()
            if max(now - last_tick, wall - last_wall) > STALL_LIMIT:
                record("stall", source="helper", gap_seconds=round(now - last_tick, 1),
                       wall_gap_seconds=round(wall - last_wall, 1))
            last_tick, last_wall = now, wall
            current = state()
            wanted = current["active_count"] > 0 or current["uncertain_claims"] > 0
            if wanted and request is None:
                request = PowerRequest()
                request.acquire()
            elif not wanted and request is not None:
                request.close()
                request = None
            signature = (request is not None, tuple(current["clients"]), current["uncertain_claims"])
            if signature != previous:
                record("helper_state", power_requested=request is not None,
                       clients=current["clients"], uncertain_claims=current["uncertain_claims"])
                previous = signature
            write(ROOT / "helper.json", {**info, **current, "power_requested": request is not None,
                                         "updated_at": time.time(),
                                         "diagnostic_error": event_log.LAST_ERROR})
            peer.poll("helper")
            if K.WaitForSingleObject(stop, 1000) == 0:
                reason = "requested"
                break
    finally:
        try:
            if request is not None:
                request.close()
        finally:
            (ROOT / "helper.json").unlink(missing_ok=True)
            peer.close()
            for handle in (stop, ready):
                if handle:
                    K.CloseHandle(handle)
            K.ReleaseMutex(mutex)
            K.CloseHandle(mutex)
            record("stop", source="helper", reason=reason)


def start_tray():
    try:
        tray = json.loads((ROOT / "tray.json").read_text(encoding="utf-8"))
        if birth(tray["pid"]) == tray["birth"]:
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    executable = Path(__file__).resolve().parents[2] / ".venv/Scripts/pythonw.exe"
    if not executable.exists():
        raise RuntimeError("Joro tray environment missing; run the keep-awake installer")
    ready = named_event("-tray-ready")
    try:
        K.ResetEvent(ready)
        child = subprocess.Popen([str(executable), str(Path(__file__).resolve()), "tray"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        if K.WaitForSingleObject(ready, 5000) != 0:
            raise RuntimeError(f"Tray did not become ready (PID {child.pid}); inspect ~/.joro/keep-awake.log")
    finally:
        K.CloseHandle(ready)


def start():
    if status()["running"]:
        start_tray()
        return status()
    ready = named_event("-ready")
    try:
        K.ResetEvent(ready)
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        executable = str(pythonw) if pythonw.exists() else sys.executable
        subprocess.Popen([executable, str(Path(__file__).resolve()), "run"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        K.WaitForSingleObject(ready, 5000)
        result = status()
        if not result["running"]:
            raise RuntimeError("Helper did not become ready; inspect keep-awake-state/helper-error.json")
        return result
    finally:
        K.CloseHandle(ready)


def startup(enabled):
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
        if enabled:
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            command = subprocess.list2cmdline([str(pythonw) if pythonw.exists() else sys.executable,
                                               str(Path(__file__).resolve()), "run"])
            winreg.SetValueEx(key, "JoroKeepAwake", 0, winreg.REG_SZ, command)
        else:
            try:
                current, _ = winreg.QueryValueEx(key, "JoroKeepAwake")
                if str(Path(__file__).resolve()).casefold() not in current.casefold():
                    raise RuntimeError("Startup entry was changed outside Joro; preserving it")
                winreg.DeleteValue(key, "JoroKeepAwake")
            except FileNotFoundError:
                pass
    return {"startup_enabled": enabled}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "start", "stop", "exit", "tray", "status", "hook", "event", "startup-on", "startup-off"))
    args = parser.parse_args()
    if args.command in ("run", "tray"):
        install_hooks("helper" if args.command == "run" else "tray")
        enable_fault_log("helper" if args.command == "run" else "tray")
    try:
        if args.command == "run":
            run()
            return 0
        if args.command == "tray":
            from joro_watcher.tray import run as run_tray
            run_tray(sys.modules[__name__])
            return 0
        if args.command in ("hook", "event"):
            value = json.load(sys.stdin)
            (claude_hook if args.command == "hook" else claim)(value)
            return 0  # Hook output must not inject bookkeeping into the conversation.
        if args.command == "start":
            value = start()
        elif args.command in ("stop", "exit"):
            existing = status()
            process = K.OpenProcess(0x100000, False, existing["pid"]) if existing["running"] else None
            handle = named_event("-stop")
            try:
                K.SetEvent(handle)
                if process and K.WaitForSingleObject(process, 5000) != 0:
                    raise RuntimeError("Helper did not stop within five seconds")
            finally:
                K.CloseHandle(handle)
                if process:
                    K.CloseHandle(process)
            value = {"stop_requested": True, "running": status()["running"]}
            if value["running"]:
                raise RuntimeError("Stop was requested but helper exit is not confirmed")
            if args.command == "exit":
                try:
                    tray = json.loads((ROOT / "tray.json").read_text(encoding="utf-8"))
                    tray_process = (K.OpenProcess(0x100000, False, tray["pid"])
                                    if birth(tray["pid"]) == tray["birth"] else None)
                except (FileNotFoundError, KeyError):
                    tray_process = None
                tray_exit = named_event("-tray-exit")
                try:
                    K.SetEvent(tray_exit)
                    if tray_process and K.WaitForSingleObject(tray_process, 5000) != 0:
                        raise RuntimeError("Tray did not exit within five seconds")
                finally:
                    K.CloseHandle(tray_exit)
                    if tray_process:
                        K.CloseHandle(tray_process)
        elif args.command.startswith("startup-"):
            value = startup(args.command == "startup-on")
        else:
            value = status()
        print(json.dumps(value))
        return 0
    except Exception as exc:
        if args.command not in ("hook", "event"):
            record("error", source=args.command, **failure(exc))
        else:
            record("publisher_error", source=args.command, **failure(exc, message=False))
        if args.command == "run":
            write(ROOT / "helper-error.json", {"error": str(exc)})
        print(f"Joro keep-awake: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
