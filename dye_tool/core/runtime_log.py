# -*- coding: utf-8 -*-
"""Public runtime log for diagnosing frozen EXE startup/hang/crash.

The log lives next to the executable (or project root in source runs) and is
opened before QApplication, so crashes during import/startup are captured.
It is intentionally independent from the GUI logging setup.
"""
from __future__ import annotations

import atexit
import faulthandler
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

LOG_NAME = "dye_tool_runtime.log"
_HANDLE = None
_PATH: Optional[Path] = None
_START = time.perf_counter()


def app_dir() -> Path:
    """Directory holding the EXE (frozen) or project root (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def path() -> Path:
    return _PATH or (app_dir() / LOG_NAME)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _write(line: str) -> None:
    if _HANDLE is None:
        return
    try:
        _HANDLE.write(line + "\n")
        _HANDLE.flush()
    except Exception:
        pass


def start_runtime_log() -> Path:
    """Open the runtime log and enable faulthandler/uncaught-exception logging."""
    global _HANDLE, _PATH
    if _HANDLE is not None:
        return path()
    _PATH = app_dir() / LOG_NAME
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _HANDLE = open(_PATH, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception as err:
        # Last-resort: never prevent the application from starting.
        _HANDLE = None
        sys.__stderr__ and print(f"[runtime-log] cannot open {_PATH}: {err}")
        return _PATH
    _write("=" * 72)
    _write(f"dye_tool runtime log start {_now()} pid={os.getpid()}")
    _write(f"frozen={getattr(sys, 'frozen', False)} python={sys.version.split()[0]}")
    try:
        _write(f"executable={sys.executable}")
        _write(f"argv={sys.argv!r}")
        _write(f"cwd={os.getcwd()}")
    except Exception:
        pass
    try:
        faulthandler.enable(file=_HANDLE, all_threads=True)
    except Exception:
        pass
    _install_excepthooks()
    atexit.register(_on_exit)
    return _PATH


def _install_excepthooks() -> None:
    prev = sys.excepthook

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            prev(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        _write(f"[EXCEPTION] {_now()} thread={threading.current_thread().name}")
        _write(detail.rstrip())
        try:
            prev(exc_type, exc, tb)
        except Exception:
            pass

    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        prev_thread = threading.excepthook

        def thread_hook(args):
            detail = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
            _write(f"[THREAD-EXCEPTION] {_now()} thread={getattr(args.thread, 'name', '?')}")
            _write(detail.rstrip())
            try:
                prev_thread(args)
            except Exception:
                pass

        threading.excepthook = thread_hook


def _on_exit() -> None:
    _write(f"[EXIT] {_now()} elapsed_ms={int((time.perf_counter() - _START) * 1000)}")
    _write("")
    try:
        _HANDLE.flush()
    except Exception:
        pass


def event(name: str, **fields) -> None:
    """Append a structured event; safe from any thread."""
    parts = [f"[EVENT] {_now()}", f"thread={threading.current_thread().name}", f"event={name}"]
    for key, value in fields.items():
        try:
            text = str(value).replace("\n", "\\n")
        except Exception:
            text = "<unprintable>"
        parts.append(f"{key}={text}")
    _write(" ".join(parts))


def startup_phase(name: str, mark: float) -> None:
    event("startup_phase", phase=name, elapsed_ms=int((time.perf_counter() - mark) * 1000))


def exception(context: str, exc: BaseException) -> None:
    _write(f"[ERROR] {_now()} context={context} type={type(exc).__name__}: {exc}")
    _write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
