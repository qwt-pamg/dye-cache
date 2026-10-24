# -*- coding: utf-8 -*-
"""页面基类：统一后台执行、日志、状态栏、结果目录登记与异常兜底。

所有功能页继承 ``PageBase``，从而共享：
    * ``Job`` 后台线程执行 + 进度/日志信号；
    * 顶层异常弹窗（含 traceback 与"复制错误信息"按钮，设计文档 §7）；
    * 计算期间禁用控件，避免中途改参；
    * 选中数据目录的持久化（``gui.recent_dirs``）。
"""
from __future__ import annotations

import logging
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout,
                               QLabel, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from core.config import default_config, load_config, save_config
from core.paths import app_root, config_path as _cfg_path
from ui.workers import Job
from ui import theme

log = logging.getLogger("dye_tool.ui.page")

#: 可写应用目录（打包后 = exe 所在目录；开发时 = 项目根）
PROJECT_ROOT = app_root()


class ErrorDialog(QDialog):
    """顶层异常对话框：显示摘要 + 完整 traceback，并提供"复制错误信息"。"""

    def __init__(self, summary: str, detail: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("发生意外错误")
        self.resize(860, 560)
        self._text = f"{summary}\n\n{detail}"
        head = QLabel(summary)
        head.setWordWrap(True)
        theme.mark(head, "danger")
        self.view = QPlainTextEdit(detail)
        self.view.setReadOnly(True)
        theme.mark(self.view, "mono")
        btn_copy = QPushButton("复制错误信息")
        btn_copy.clicked.connect(self._copy)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(btn_copy)
        bar.addWidget(btn_close)
        lay = QVBoxLayout(self)
        lay.addWidget(head)
        lay.addWidget(self.view, 1)
        lay.addLayout(bar)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._text)


class PageBase(QWidget):
    """功能页基类。

    信号 ``status(str)`` / ``progress(int, str)`` / ``logged(str, str)``
    由主窗口接到状态栏与共享日志区。
    """

    status = Signal(str)
    progress = Signal(int, str)
    logged = Signal(str, str)
    run_finished = Signal(object)          # 流水线对象（RunResult / CompareResult / …）

    #: 子类覆盖：页面标题与说明
    TITLE = "功能页"
    SUBTITLE = ""

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg or default_config()
        self.job: Optional[Job] = None
        self._busy = False
        self._busy_widgets: list = []      # S6：失败/取消时也要能放回来

    # ══════════════ 通用 UI 片段 ══════════════
    def header(self) -> QWidget:
        """页头（标题 + 说明）。"""
        w = QWidget()
        lbl_t = QLabel(self.TITLE)
        theme.mark(lbl_t, "title")
        lbl_s = QLabel(self.SUBTITLE)
        theme.mark(lbl_s, "muted")
        lbl_s.setWordWrap(True)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(lbl_t)
        lay.addWidget(lbl_s)
        return w

    def progress_row(self) -> QWidget:
        """进度条 + 阶段标签 + 取消按钮。

        只在任务运行时出现；成功后收起（结果本身和状态栏已经说明），失败或取消时
        留下来显示原因。「取消」只在任务运行中出现。
        """
        w = QWidget()
        self._progress_widget = w
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.bar.setFormat("%p%")
        self.lbl_stage = QLabel("就绪")
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_job)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.bar, 1)
        lay.addWidget(self.lbl_stage)
        lay.addWidget(self.btn_cancel)
        self.btn_cancel.setVisible(False)
        w.setVisible(False)
        return w

    def _show_progress(self, on: bool = True) -> None:
        w = getattr(self, "_progress_widget", None)
        if w is not None:
            w.setVisible(on)

    # ══════════════ 目录 / 配置 ══════════════
    def pick_dir(self, title: str = "选择数据目录") -> Optional[Path]:
        """目录选择框：从本页当前目录 → 最近目录 → 程序目录依次取起点；选中即记入最近。"""
        start = str(getattr(self, "data_dir", None)
                    or (self.cfg.get("gui") or {}).get("last_dir") or PROJECT_ROOT)
        d = QFileDialog.getExistingDirectory(self, title, start)
        if not d:
            return None
        p = Path(d)
        self.remember_dir(p)
        return p

    def remember_dir(self, p: Path) -> None:
        """把目录写入最近列表（**只改内存**，关窗/改参数时才落盘）。

        B2/A26（V2.x 审查）：原实现每次目录载入都 `persist_cfg()` —— 一次批次
        载入里 data/analysis/plot 各页各记一次，叠加 M3 的重复解析，形成
        "一次载入 4–5 次扫描 + 4–5 次写盘"。最近目录是**会话态**，没有"必须立刻
        落盘"的理由；`closeEvent` 与参数保存都会把整份 cfg 写回，关窗时自然带上。
        好处：载入路径不再有磁盘写，且 `config.yaml` 不会被频繁改动。
        """
        gui = self.cfg.setdefault("gui", {})
        recents = [x for x in gui.get("recent_dirs", []) if x != str(p)]
        recents.insert(0, str(p))
        gui["recent_dirs"] = recents[:int(gui.get("max_recent", 8))]
        gui["last_dir"] = str(p)

    def out_root(self) -> Path:
        """输出根目录（唯一解析在 :func:`core.paths.resolve_out_root`）。"""
        from core.paths import resolve_out_root
        return resolve_out_root(self.cfg)

    # ══════════════ 拖入目录 ══════════════
    def dragEnterEvent(self, e) -> None:               # noqa: N802 —— Qt 接口
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                    # noqa: N802 —— Qt 接口
        """拖入目录（拖入文件按其所在目录）→ :meth:`on_dir_dropped`。

        页面需自己 ``setAcceptDrops(True)``；要按文件处理的页面（如光谱作图页）
        覆盖本方法即可。
        """
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        dirs = [p for p in urls if p.is_dir()]
        target = dirs[0] if dirs else (urls[0].parent if urls else None)
        if target is not None:
            self.on_dir_dropped(target)
        e.acceptProposedAction()

    def on_dir_dropped(self, d: Path) -> None:
        """子类覆盖：接收拖入的数据目录。"""

    @staticmethod
    def config_path() -> Path:
        """用户级配置文件位置（打包后 = exe 同级的 ``config.yaml``）。"""
        return _cfg_path()

    def load_cfg(self) -> dict:
        """从磁盘重新读配置（与 DEFAULT_CFG 深合并）。"""
        p = self.config_path()
        self.cfg = load_config(p if p.exists() else None)
        return self.cfg

    def persist_cfg(self) -> None:
        """把当前 cfg 写回 config.yaml（失败只记日志，不打扰用户）。

        ε 批护栏（冒烟模式不写交付配置）**已下沉到** :func:`core.config.save_config`
        ——V2.3 起这里不再重复判断（《代码评审记录.md》第3部分·F3：逐点补漏不如单点收紧）。
        """
        try:
            save_config(self.cfg, self.config_path())
        except Exception as err:                       # noqa: BLE001
            log.warning("保存配置失败: %s", err)

    # ══════════════ 后台任务 ══════════════
    def run_task(self, task: Callable[..., Any], label: str,
                 on_done: Callable[[Any], None],
                 busy_widgets: Optional[list[QWidget]] = None) -> None:
        """在后台线程执行 ``task(progress_cb, log_cb)``。

        ``on_done`` 在 UI 线程被调用；失败时弹 ``ErrorDialog``（冒烟模式下改为日志）。

        S6 修复：**无论成功、失败还是取消，界面一定恢复可用**。做法是把解锁逻辑
        收敛到 :meth:`_unlock_ui`，并挂在三个终点上（成功/失败/线程结束兜底）——
        原先取消路径不发任何信号，页面会被永久砖化。
        """
        if self._busy:
            self.status.emit("已有任务在运行，请等待完成或取消")
            return
        self._busy = True
        self._busy_widgets = list(busy_widgets or [])
        self.bar.setValue(0)
        self.lbl_stage.setText(f"{label}…")
        self._show_progress()
        self.btn_cancel.setEnabled(True)
        self.btn_cancel.setVisible(True)
        for w in self._busy_widgets:
            w.setEnabled(False)

        job = Job(task, label, self)
        self.job = job
        job.progress.connect(self._on_progress)
        job.logged.connect(self.logged)
        job.failed.connect(self._on_failed)
        job.cancelled.connect(self._on_cancelled)
        job.done.connect(lambda res: self._on_done(res, on_done))
        # 兜底：线程真正结束时再解锁一次（幂等）。取消/异常路径都不会漏。
        job.finished.connect(self._unlock_ui)
        job.start()

    def _unlock_ui(self) -> None:
        """恢复界面可用（幂等）：清忙标志、放回按钮、取消键失效。"""
        self._busy = False
        for w in getattr(self, "_busy_widgets", []) or []:
            try:
                w.setEnabled(True)
            except RuntimeError:                   # 控件已被销毁（页面关闭）
                pass
        self._busy_widgets = []
        try:
            self.btn_cancel.setEnabled(False)
            self.btn_cancel.setVisible(False)
        except RuntimeError:                       # pragma: no cover
            pass

    def _on_progress(self, pct: int, stage: str) -> None:
        self._show_progress()
        self.bar.setValue(max(0, min(100, pct)))
        self.lbl_stage.setText(stage)
        self.progress.emit(pct, stage)

    def _on_done(self, res: Any, on_done: Callable[[Any], None]) -> None:
        self._unlock_ui()
        self.bar.setValue(100)
        self.lbl_stage.setText("完成")
        self._show_progress(False)
        try:
            on_done(res)
        except Exception as err:                       # noqa: BLE001
            self._on_failed(f"{type(err).__name__}: {err}", traceback.format_exc())
            return
        self.run_finished.emit(res)

    def _on_cancelled(self) -> None:
        """用户取消：恢复界面并说明（S6）。"""
        self._unlock_ui()
        self._show_progress()
        self.bar.setValue(0)
        self.lbl_stage.setText("已取消")
        self.logged.emit("WARNING", "任务已取消：本次结果被丢弃，界面已恢复可用")

    def _report_modal(self, summary: str, detail: str) -> None:
        """弹错误对话框；**冒烟/自动化模式下只记日志**。

        M16：``DYE_TOOL_SMOKE=1`` 时模态框会阻塞事件循环（欢迎卡片早就跳过了，
        这里原来没跳）→ 自动化跑失败路径会永久挂住。
        """
        if os.environ.get("DYE_TOOL_SMOKE") == "1":
            log.warning("冒烟模式：不弹错误对话框。%s\n%s", summary, detail)
            return
        ErrorDialog(summary, detail, self).exec()

    def _on_failed(self, summary: str, detail: str) -> None:
        """顶层异常兜底：恢复界面 + 弹窗（设计文档 §7）。"""
        self._unlock_ui()
        self._show_progress()
        self.bar.setValue(0)
        self.lbl_stage.setText("失败")
        self.logged.emit("ERROR", summary)
        log.error("%s", summary)
        self._report_modal(summary, detail)

    def cancel_job(self) -> None:
        """请求取消当前任务。"""
        if self.job is not None and self.job.isRunning():
            self.job.cancel()
            self.lbl_stage.setText("已取消（计算在当前阶段结束后停止上报）")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.setVisible(False)

    # ══════════════ 钩子 ══════════════
    def on_activated(self) -> None:
        """页签被切到时调用（子类可刷新数据）。"""

    def shutdown(self) -> None:
        """退出前调用：等待后台线程结束。"""
        if self.job is not None and self.job.isRunning():
            self.job.cancel()
            self.job.wait(4000)
