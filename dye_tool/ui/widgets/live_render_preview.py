# -*- coding: utf-8 -*-
"""V2.9.5 FR-73：作图台生图实时预览（最终渲染器低 dpi + 防抖 + 临时目录）。"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional

from core.paths import app_root

from PySide6.QtCore import QObject, QTimer, Signal

from ui.workers import Job


class LiveRenderPreview(QObject):
    """单页面预览控制器：300–500ms 防抖；同时只跑一个低 dpi 最终渲染任务。"""

    updated = Signal(list)
    state = Signal(str)
    failed = Signal(str)

    def __init__(self, parent: QObject, render_fn: Callable[[Path], object],
                 debounce_ms: int = 400, enabled: bool = True):
        super().__init__(parent)
        self._render_fn = render_fn
        self._enabled = bool(enabled)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(int(debounce_ms))
        self._timer.timeout.connect(self._fire)
        self._job: Optional[Job] = None
        self._token = 0
        self._pending = False
        self._tmp_dirs: list[Path] = []

    def set_enabled(self, on: bool) -> None:
        self._enabled = bool(on)
        if not self._enabled:
            self._token += 1
            self._timer.stop()
            self.state.emit("实时预览已关闭")
        else:
            self.state.emit("实时预览已开启")

    def is_enabled(self) -> bool:
        return self._enabled

    def schedule(self) -> None:
        if not self._enabled:
            return
        self._pending = True
        self._token += 1
        self._timer.start()

    def _fire(self) -> None:
        if not self._enabled or not self._pending:
            return
        if self._job is not None and self._job.isRunning():
            return
        self._pending = False
        token = self._token
        # R3-O2：显式临时根目录，绝不让 tempfile 回退到 cwd（A 机 TEMP 不可写时
        # gettempdir() 会回落 cwd，曾把 dye_preview_* 写进仓库根）。
        root = Path(os.environ.get("DYE_TOOL_PREVIEW_ROOT") or (app_root() / ".preview_tmp"))
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception:
            root = app_root() / ".preview_tmp"
            root.mkdir(parents=True, exist_ok=True)
        out = Path(tempfile.mkdtemp(prefix="dye_preview_", dir=str(root)))
        self.state.emit("预览中…")
        job = Job(lambda _p, _l: self._render_fn(out), "实时生图预览", self)
        self._job = job
        job.done.connect(lambda res, t=token, o=out: self._done(res, t, o))
        job.failed.connect(lambda msg, tb, t=token: self._failed(msg, t))
        job.finished.connect(self._finished)
        job.start()

    def _cleanup(self, out: Path) -> None:
        try:
            shutil.rmtree(out, ignore_errors=True)
        except Exception:
            pass

    def _done(self, res, token: int, out: Path) -> None:
        if token != self._token:
            self._cleanup(out)
            self.state.emit("旧预览已丢弃")
            return
        files = []
        try:
            if hasattr(res, "files"):
                files = [Path(p) for p in (res.files or [])]
            elif isinstance(res, (list, tuple)):
                files = [Path(p) for p in res]
            elif isinstance(res, (str, Path)):
                files = [Path(res)]
        except Exception:
            files = []
        files = [p for p in files if p.is_file()]
        if files:
            self.updated.emit(files)
            self.state.emit("预览已更新")
            QTimer.singleShot(2500, lambda: self._cleanup(out))
        else:
            self._cleanup(out)
            self.state.emit("预览不可用")

    def _failed(self, msg: str, token: int) -> None:
        if token == self._token:
            self.state.emit("预览失败")
            self.failed.emit(str(msg))

    def _finished(self) -> None:
        self._job = None
        if self._pending and self._enabled:
            self._timer.start()

    def shutdown(self) -> None:
        self._timer.stop()
        if self._job is not None and self._job.isRunning():
            self._job.cancel()
