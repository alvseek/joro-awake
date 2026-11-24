"""One on-demand powercfg query, with no privileged filesystem output."""

import base64
import codecs
from collections import Counter, defaultdict
import ctypes
from ctypes import wintypes
import ntpath
from pathlib import Path
import re
import time
from uuid import uuid4

from joro_watcher.power import JORO_REASON


TIMEOUT = 15
MAX_OUTPUT = 1024 * 1024
ERROR_CANCELLED = 1223

# No profiles, modules, script files, PATH lookup, or user-supplied commands.
# Anonymous SQOS is essential: the non-elevated pipe server must never have
# access to the elevated client's impersonation token.
QUERY = r"""
$ErrorActionPreference = 'Stop'
$PSModuleAutoLoadingPreference = 'None'
$pipe = $null
$process = $null
try {
    $pipe = [IO.Pipes.NamedPipeClientStream]::new(
        '.', 'PIPE_NAME', [IO.Pipes.PipeDirection]::Out,
        [IO.Pipes.PipeOptions]::Asynchronous,
        [Security.Principal.TokenImpersonationLevel]::Anonymous)
    $pipe.Connect(3000)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = [IO.Path]::Combine([Environment]::SystemDirectory, 'powercfg.exe')
    $start.WorkingDirectory = [Environment]::SystemDirectory
    $start.Arguments = '/requests'
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($start)
    $copy = $process.StandardOutput.BaseStream.CopyToAsync($pipe)
    $errors = $process.StandardError.ReadToEndAsync()
    if (!$process.WaitForExit(10000)) {
        $process.Kill()
        exit 1
    }
    if (!$copy.Wait(2000) -or !$errors.Wait(1000) -or $errors.Result.Length -or $process.ExitCode) {
        exit 1
    }
    exit 0
} catch {
    exit 1
} finally {
    if ($process) { $process.Dispose() }
    if ($pipe) { $pipe.Dispose() }
}
"""


class CheckCancelled(Exception):
    pass


class ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def system_directory():
    get_directory = ctypes.WinDLL("kernel32", use_last_error=True).GetSystemDirectoryW
    get_directory.argtypes = (wintypes.LPWSTR, wintypes.UINT)
    get_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    if not 0 < get_directory(buffer, len(buffer)) < len(buffer):
        raise OSError("Cannot resolve Windows system directory")
    return Path(buffer.value)


def query_arguments(pipe):
    name = pipe.rsplit("\\", 1)[-1]
    if not re.fullmatch(r"JoroPower-[a-f0-9]{32}", name) or pipe != rf"\\.\pipe\{name}":
        raise ValueError("Unexpected diagnostic pipe")
    command = base64.b64encode(QUERY.replace("PIPE_NAME", name).encode("utf-16-le")).decode("ascii")
    return f"-NoLogo -NoProfile -NonInteractive -EncodedCommand {command}"


def launch_elevated(directory, arguments):
    shell = ctypes.WinDLL("shell32", use_last_error=True).ShellExecuteExW
    shell.argtypes = (ctypes.POINTER(ShellExecuteInfo),)
    shell.restype = wintypes.BOOL
    request = ShellExecuteInfo(
        cbSize=ctypes.sizeof(ShellExecuteInfo),
        fMask=0x40 | 0x100 | 0x400,  # NOCLOSEPROCESS | NOASYNC | FLAG_NO_UI
        lpVerb="runas", lpFile=str(directory / "WindowsPowerShell/v1.0/powershell.exe"),
        lpParameters=arguments, lpDirectory=str(directory), nShow=0,
    )
    ole = ctypes.WinDLL("ole32")
    if ole.CoInitializeEx(None, 0x2 | 0x4) < 0:  # STA, no OLE1 DDE
        raise OSError("Cannot initialize shell caller")
    try:
        if not shell(ctypes.byref(request)):
            error = ctypes.get_last_error()
            if error == ERROR_CANCELLED:
                raise CheckCancelled from None
            raise ctypes.WinError(error)
        if not request.hProcess:
            raise OSError("Windows did not return a diagnostic process")
        return request.hProcess
    finally:
        ole.CoUninitialize()


def remaining(deadline):
    milliseconds = int((deadline - time.monotonic()) * 1000)
    if milliseconds <= 0:
        raise TimeoutError
    return milliseconds


def complete(win, operation, deadline):
    try:
        if win.WaitForSingleObject(operation.event, remaining(deadline)) != 0:
            raise TimeoutError
    except BaseException:
        operation.cancel()
        operation.GetOverlappedResult(True)
        raise
    return operation.GetOverlappedResult(False)


def verify_writer(pipe, process):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    get_client = kernel.GetNamedPipeClientProcessId
    get_client.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
    get_client.restype = wintypes.BOOL
    get_pid = kernel.GetProcessId
    get_pid.argtypes = (wintypes.HANDLE,)
    get_pid.restype = wintypes.DWORD
    pid = wintypes.ULONG()
    if not get_client(pipe, ctypes.byref(pid)) or pid.value != get_pid(process):
        raise OSError("Unexpected diagnostic pipe writer")


def capture(launch):
    """Start listening before launch; the child writes only to this local pipe."""
    import _winapi as win

    name = rf"\\.\pipe\JoroPower-{uuid4().hex}"
    pipe = win.CreateNamedPipe(
        name, win.PIPE_ACCESS_INBOUND | win.FILE_FLAG_OVERLAPPED | win.FILE_FLAG_FIRST_PIPE_INSTANCE,
        0x8,  # Byte stream, PIPE_REJECT_REMOTE_CLIENTS
        1, 0, 65536, 0, win.NULL,
    )
    try:
        connection = win.ConnectNamedPipe(pipe, overlapped=True)
        try:
            process = launch(name)
        except BaseException:
            connection.cancel()
            connection.GetOverlappedResult(True)
            raise
        try:
            deadline = time.monotonic() + TIMEOUT  # Do not time the user's UAC decision.
            if complete(win, connection, deadline)[1]:
                raise OSError("Cannot connect diagnostic pipe")
            verify_writer(pipe, process)
            output = bytearray()
            while len(output) <= MAX_OUTPUT:
                try:
                    read, _ = win.ReadFile(pipe, 65536, overlapped=True)
                    count, error = complete(win, read, deadline)
                except BrokenPipeError:
                    break
                if error == win.ERROR_BROKEN_PIPE:
                    break
                if error:
                    raise ctypes.WinError(error)
                if not count:
                    break
                output.extend(read.getbuffer()[:count])
            if (len(output) > MAX_OUTPUT
                    or win.WaitForSingleObject(process, remaining(deadline)) != 0
                    or win.GetExitCodeProcess(process) != 0 or not output.strip()):
                raise OSError("Windows did not return a complete diagnostic")
            return bytes(output)
        finally:
            win.CloseHandle(process)
    finally:
        win.CloseHandle(pipe)


def check():
    directory = system_directory()
    return capture(lambda pipe: launch_elevated(directory, query_arguments(pipe)))


SECTION_GROUP = {
    "SYSTEM:": "System sleep",
    "AWAYMODE:": "System sleep",
    "DISPLAY:": "Display",
    "EXECUTION:": "Other",
    "PERFBOOST:": "Other",
    "ACTIVELOCKSCREEN:": "Other",
}
ENTRY = re.compile(r"^\[[^\]]+\](?:\s|$)")
HEADING = re.compile(r"^[^\W\d_][\w /-]*[:：]$")


def decode_report(output, fallback="oem"):
    newline = output.find(b"\n")
    if output.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif output.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    elif newline >= 0 and newline % 2 == 0 and output[newline:newline + 2] == b"\n\x00":
        encoding = "utf-16-le"
    elif newline > 0 and newline % 2 == 1 and output[newline - 1:newline + 1] == b"\x00\n":
        encoding = "utf-16-be"
    else:
        encoding = fallback
    return output.decode(encoding, errors="replace").replace("\x00", r"\0")


def summarize(report):
    groups = {name: Counter() for name in ("System sleep", "Display", "Other")}
    paths_by_name = defaultdict(set)
    section, entry, unmatched = None, [], False

    def finish_entry():
        if entry:
            groups[SECTION_GROUP.get(section, "Other")][section, tuple(entry)] += 1
            if entry[0].startswith("[PROCESS] "):
                path = entry[0].removeprefix("[PROCESS] ")
                paths_by_name[ntpath.basename(path).casefold()].add(path)
            entry.clear()

    for line in report.splitlines():
        if line in SECTION_GROUP or HEADING.fullmatch(line):
            finish_entry()
            section = line
            unmatched |= section not in SECTION_GROUP
        elif ENTRY.match(line):
            finish_entry()
            entry.append(line)
            unmatched |= section not in SECTION_GROUP
        elif not line.strip():
            finish_entry()
        elif entry:
            entry.append(line)
        elif section in SECTION_GROUP and line == "None.":
            continue
        else:
            unmatched = True
    finish_entry()

    def format_entry(section, lines, count):
        owner, *reasons = lines
        if reasons == [JORO_REASON]:
            owner = "Joro"
        elif owner.startswith("[PROCESS] "):
            path = owner.removeprefix("[PROCESS] ")
            name = ntpath.basename(path)
            owner = name if name and len(paths_by_name[name.casefold()]) == 1 else path
        label = " — ".join([owner, *reasons])
        if section and section not in ("SYSTEM:", "DISPLAY:"):
            label = f"{section} {label}"
        return f"{label} ×{count}" if count > 1 else label

    blocks = []
    for title, counts in groups.items():
        rows = [format_entry(kind, lines, n) for (kind, lines), n in counts.items()]
        if unmatched and title == "Other":
            rows.append("Some output could not be grouped. See Full report.")
        blocks.append(title + "\n" + ("\n".join(rows) or "No entries recognized in this group."))
    return "\n\n".join(blocks)


def snapshot_text(output, error, timestamp):
    header = f"Snapshot · {timestamp.isoformat(sep=' ', timespec='seconds')}\n\n"
    if error is not None:
        return header + f"Check didn’t complete.\n{error}", None
    full = decode_report(output)
    if not full.strip():
        return header + "Check didn’t complete.\nWindows returned an empty report.", None
    return header + summarize(full), full
