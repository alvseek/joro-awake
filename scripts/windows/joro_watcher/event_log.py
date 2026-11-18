"""Bounded, process-safe lifecycle history; callers supply metadata, never content."""

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import datetime
import faulthandler
import hashlib
import io
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading
import time
import traceback

LAST_ERROR = None
FAULT_LIMIT = 256 * 1024
ENTRY_LIMIT = 8192
FAULT_FILES = []
STREAM_GUARD = threading.local()
EXIT_MEANINGS = {
    0x0: "clean exit",
    0x1: "exited with an error, or killed with taskkill /F",
    0x40010004: "ended by Windows at logoff or shutdown",
    0xC0000005: "access violation (native crash)",
    0xC00000FD: "stack overflow (native crash)",
    0xC0000409: "fast-fail abort (native crash)",
    0xC0000374: "heap corruption (native crash)",
    0xC000013A: "closed by Ctrl+C, logoff or shutdown",
    0xC0000142: "a DLL failed to initialize, typical while Windows logs off or shuts down",
}


class StrictRotatingHandler(RotatingFileHandler):
    def handleError(self, entry):
        # logging normally prints and swallows these errors, which hides disk failures.
        raise


@contextmanager
def locked(name):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = False
    try:
        result = kernel.WaitForSingleObject(handle, 5000)
        if result not in (0, 0x80):
            raise RuntimeError("Timed out acquiring Joro log lock")
        acquired = True
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def failure(exc, message=True):
    """Type and stack frames; the message only when the caller knows it carries no content."""
    result = {"error_type": type(exc).__name__,
              "traceback": "".join(traceback.format_tb(exc.__traceback__))}
    if message:
        result["error"] = str(exc)
    return result


def describe_exit(code):
    code &= 0xFFFFFFFF
    return {"exit_code": f"0x{code:08X}", "exit_meaning": EXIT_MEANINGS.get(code, "unrecognized exit code")}


def timestamp(seconds):
    try:
        return datetime.datetime.fromtimestamp(seconds).astimezone().isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError, TypeError):
        return None


def _fit(event, fields):
    """Keep the event name and the end of the traceback, the part that names the failing frame."""
    message = json.dumps({"event": event, **fields}, ensure_ascii=True)
    if len(message) <= ENTRY_LIMIT:
        return message
    slim = {key: value[:256] if isinstance(value, str) and key != "traceback" else value
            for key, value in fields.items()}
    slim["truncated"] = True
    tail = slim.pop("traceback", "")
    event = str(event)[:128]
    while True:
        message = json.dumps({"event": event, **slim, **({"traceback": tail} if tail else {})}, ensure_ascii=True)
        if len(message) <= ENTRY_LIMIT:
            return message
        if not tail:
            return json.dumps({"event": event, "truncated": True})
        tail = tail[len(tail) // 4 + 1:]


def _record(event, *, directory=None, **fields):
    """Open/close under one lock so no other process holds a rotated file open."""
    directory = Path(directory) if directory is not None else Path.home() / ".joro"
    key = hashlib.sha256(str(directory.resolve()).casefold().encode()).hexdigest()[:20]
    with locked("Local\\JoroLog-" + key):
        directory.mkdir(parents=True, exist_ok=True)
        handler = StrictRotatingHandler(directory / "keep-awake.log",
                                      maxBytes=1024 * 1024, backupCount=4, encoding="utf-8")
        try:
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            # Prevent one unexpectedly large error from defeating the file bound.
            message = _fit(event, fields)
            entry = logging.LogRecord("joro", logging.INFO, "", 0, message, (), None)
            handler.emit(entry)
        finally:
            handler.close()


def record(event, *, directory=None, **fields):
    """Diagnostics must never release a power request or kill the observer."""
    global LAST_ERROR
    try:
        _record(event, directory=directory, **fields)
    except Exception as exc:
        LAST_ERROR = f"Cannot write Joro log: {type(exc).__name__}"
        try:
            print(LAST_ERROR, file=sys.stderr)
        except Exception:
            pass
        return LAST_ERROR
    LAST_ERROR = None
    return None


def install_hooks(source, *, directory=None):
    """Record an exception that escapes the top level or a thread before the process ends."""
    previous, previous_thread, previous_unraisable = sys.excepthook, threading.excepthook, sys.unraisablehook

    def top(kind, exc, trace):
        exc.__traceback__ = trace
        record("uncaught_exception", directory=directory, source=source, **failure(exc))
        previous(kind, exc, trace)

    def thread(args):
        args.exc_value.__traceback__ = args.exc_traceback
        record("uncaught_thread_exception", directory=directory, source=source,
               thread=getattr(args.thread, "name", None), **failure(args.exc_value))
        previous_thread(args)

    def unraisable(args):
        exc = args.exc_value
        if exc is not None:
            exc.__traceback__ = args.exc_traceback
            record("unraisable_exception", directory=directory, source=source, **failure(exc))
        previous_unraisable(args)

    sys.excepthook, threading.excepthook, sys.unraisablehook = top, thread, unraisable


class DiagnosticStream(io.TextIOBase):
    """Bound each output fragment and send it through the existing rotating log."""

    def __init__(self, source, stream, directory=None):
        self.source, self.stream, self.directory = source, stream, directory
        self.pending = ""
        self.lock = threading.RLock()

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, text):
        if getattr(STREAM_GUARD, "active", False):
            return len(text)  # record() can report its own failure to stderr.
        with self.lock:
            STREAM_GUARD.active = True
            try:
                # Walk bounded slices: neither a missing newline nor one huge write
                # may leave an unbounded pending buffer.
                for offset in range(0, len(text), 2048):
                    self.pending += text[offset:offset + 2048]
                    while "\n" in self.pending or len(self.pending) >= 2048:
                        newline = self.pending.find("\n")
                        size = newline + 1 if 0 <= newline < 2048 else 2048
                        fragment, self.pending = self.pending[:size], self.pending[size:]
                        record("process_output", directory=self.directory, source=self.source,
                               stream=self.stream, detail=fragment.rstrip("\r\n"))
            finally:
                STREAM_GUARD.active = False
        return len(text)

    def flush(self):
        with self.lock:
            if self.pending:
                text, self.pending = self.pending, ""
                self.write(text + "\n")


class LibraryLogHandler(logging.Handler):
    def __init__(self, source, directory=None):
        super().__init__(logging.WARNING)
        self.source, self.directory = source, directory

    def emit(self, entry):
        fields = {"source": self.source, "logger": entry.name,
                  "level": entry.levelname, "detail": entry.getMessage()}
        if entry.exc_info and entry.exc_info[1] is not None:
            fields.update(failure(entry.exc_info[1]))
        record("library_error", directory=self.directory, **fields)


@contextmanager
def tray_diagnostics(*, directory=None):
    """pystray catches errors internally; pythonw otherwise discards their output."""
    stdout, stderr = sys.stdout, sys.stderr
    logger = logging.getLogger("pystray")
    old_handlers, old_level, old_propagate = logger.handlers[:], logger.level, logger.propagate
    handler = LibraryLogHandler("tray", directory)
    output = DiagnosticStream("tray", "stdout", directory)
    errors = DiagnosticStream("tray", "stderr", directory)
    try:
        sys.stdout, sys.stderr = output, errors
        logger.handlers, logger.level, logger.propagate = [handler], logging.WARNING, False
        yield
    finally:
        output.flush()
        errors.flush()
        sys.stdout, sys.stderr = stdout, stderr
        logger.handlers, logger.level, logger.propagate = old_handlers, old_level, old_propagate
        handler.close()


def enable_fault_log(name, *, directory=None):
    """A native fault cannot take the log lock, so each process dumps its stacks into its own file."""
    try:
        directory = Path(directory) if directory is not None else Path.home() / ".joro"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"crash-{name}.log"
        if path.exists() and path.stat().st_size > FAULT_LIMIT:
            path.write_bytes(path.read_bytes()[-FAULT_LIMIT // 2:])
        handle = open(path, "ab", buffering=0)
        FAULT_FILES.append(handle)
        handle.write(f"--- {name} started {timestamp(time.time())}\n".encode())
        faulthandler.enable(file=handle, all_threads=True)
    except Exception as exc:
        record("fault_log_failed", directory=directory, source=name, **failure(exc, message=False))
        return f"Cannot enable Joro fault log: {type(exc).__name__}"
    return None
