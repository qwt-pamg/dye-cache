# -*- coding: utf-8 -*-
"""后台执行器：把长任务放进 QThread，避免界面冻结（设计文档 §6 / §7）。

约定：
    * ``Job`` 接收一个形如 ``task(progress_cb, log_cb) -> Any`` 的可调用对象；
    * core 的 ``progress_cb`` / ``log_cb`` 在工作线程里被调用，本类**只做
      ``Signal.emit``**，Qt 会自动排队到 UI 线程执行槽函数，因此线程安全；
    * 顶层异常一律捕获为 ``failed(message, traceback)`` 信号，绝不抛到 UI 顶层
      （设计文档 §7 健壮性：顶层异常捕获并弹窗显示 traceback + 复制按钮）。
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal

log = logging.getLogger("dye_tool.ui.workers")

#: 任务签名：``(progress_cb, log_cb) -> Any``
Task = Callable[[Callable[[int, str], None], Callable[[str, str], None]], Any]


class Job(QThread):
    """在后台线程里执行一个 core 调用。

    信号：
        ``progress(int, str)`` —— 百分比 + 阶段名
        ``logged(str, str)``   —— 级别 + 消息（转发 core 日志到 UI 日志区）
        ``done(object)``       —— 返回值（**取消时不发**）
        ``failed(str, str)``   —— 错误摘要 + 完整 traceback
        ``cancelled()``        —— 用户取消且任务已结束（S6：让页面能恢复可用状态）
    """

    progress = Signal(int, str)
    logged = Signal(str, str)
    done = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()

    def __init__(self, task: Task, label: str = "任务", parent: Optional[QObject] = None):
        super().__init__(parent)
        self._task = task
        self.label = label
        self._cancel = False

    # ── core 回调（在工作线程内被调用） ──
    def _on_progress(self, pct: int, stage: str) -> None:
        if not self._cancel:
            self.progress.emit(int(pct), str(stage))

    def _on_log(self, level: str, msg: str) -> None:
        if not self._cancel:
            self.logged.emit(str(level), str(msg))

    # ── 生命周期 ──
    def run(self) -> None:                              # noqa: D102 —— QThread 约定
        try:
            result = self._task(self._on_progress, self._on_log)
        except Exception as err:                        # noqa: BLE001 —— 顶层兜底
            tb = traceback.format_exc()
            log.error("%s 失败: %s", self.label, err)
            self.failed.emit(f"{type(err).__name__}: {err}", tb)
            return
        if self._cancel:
            # S6：取消后必须给页面一个**明确的终态信号**。原先直接不发任何信号，
            # 于是 _busy 永远为 True、按钮永远禁用、再点运行只弹"已有任务在运行"
            # ——页面被永久砖化。
            self.cancelled.emit()
            return
        self.done.emit(result)

    def is_cancel_requested(self) -> bool:
        """用户是否已请求取消（供可中断任务在循环里协作式退出）。"""
        return bool(self._cancel)

    def cancel(self) -> None:
        """尽力取消：置位后不再向 UI 投递进度/日志/结果。

        说明：core 的数值流水线没有中途退出钩子（保持与 legacy 数值行为一致），
        因此取消是"丢弃结果"式而非"中断计算"式；界面会立刻恢复可用
        （``cancelled`` 信号 + 页面的 ``finished`` 兜底恢复）。
        """
        self._cancel = True
        log.info("%s 已请求取消（计算将在当前阶段结束后停止上报）", self.label)
