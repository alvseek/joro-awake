"""Unelevated, one-report viewer. The tray sends display data on stdin only."""

import json
from pathlib import Path
import subprocess
import sys
import threading


def viewer_command():
    return [sys.executable, "-I", str(Path(__file__).resolve())]


def show(stopped, summary, full):
    """Hold the worker until the child exits; never wait on the tray loop."""
    payload = (json.dumps([summary, full], ensure_ascii=True) + "\n").encode("ascii")
    if stopped.is_set():
        return
    with subprocess.Popen(viewer_command(), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW) as child:
        try:
            if not stopped.is_set():
                child.stdin.write(payload)
                child.stdin.flush()
                while child.poll() is None and not stopped.wait(0.1):
                    pass
        finally:
            try:
                child.stdin.close()  # EOF requests graceful shutdown, including if the tray dies.
            finally:
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        if not stopped.is_set() and child.returncode != 0:
            raise OSError("Snapshot viewer failed")


def display(summary, full, tray_gone):
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    if tray_gone.is_set():
        return
    root = tk.Tk()
    try:
        root.title("Joro · Power requests")
        root.geometry("740x490")
        tabs = ttk.Notebook(root)
        tabs.pack(fill="both", expand=True, padx=12, pady=(12, 0))
        for name, text in (("Summary", summary), ("Full report", full)):
            if text is None:
                continue
            pane = scrolledtext.ScrolledText(tabs, wrap="word", padx=8, pady=8)
            pane.insert("1.0", text.replace("\r\n", "\n"))
            pane.configure(state="disabled")
            tabs.add(pane, text=name)
        ttk.Button(root, text="Close", command=root.quit).pack(pady=12)
        root.bind("<Escape>", lambda event: root.quit())
        root.protocol("WM_DELETE_WINDOW", root.quit)

        def close_if_tray_gone():
            if tray_gone.is_set():
                root.quit()
            else:
                root.after(100, close_if_tray_gone)

        root.after(100, close_if_tray_gone)
        root.mainloop()
    finally:
        root.destroy()


def main(stream, render=display):
    line = stream.readline()
    if not line:
        return 0  # Tray exited before it sent a snapshot.
    summary, full = json.loads(line)
    tray_gone = threading.Event()

    def watch_tray():
        # No Tk calls off the main thread. Use the raw pipe so this daemon
        # cannot hold a buffered-stdin lock during interpreter shutdown.
        try:
            stream.raw.read(1)
        finally:
            tray_gone.set()

    threading.Thread(target=watch_tray, name="joro-viewer-tray-watch", daemon=True).start()
    render(summary, full, tray_gone)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.stdin.buffer))
    except Exception:
        # The tray handles viewer failure; never emit the payload or exceptions.
        raise SystemExit(1) from None
