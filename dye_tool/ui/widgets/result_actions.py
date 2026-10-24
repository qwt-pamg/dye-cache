# -*- coding: utf-8 -*-
"""结果区动作条：打开输出 / 复制结果 / 下一步 / 重试（Φ_Δ、对比、ε、A@λ 共用）。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui import theme


class ResultActions(QWidget):
    """V2.6 FR-40.5：结果区动作条（Φ_Δ / ε / A@λ / kobs 共用一套组件）。

    只做"产物入口 + 下一步 + 失败重试"三件事，不改任何业务逻辑：

    * 「打开输出文件」「复制结果」——有产物/结果文本时才可用；
    * 「下一步：③ 图件 …」——任务成功后可用，点击发 next_step_requested；
    * 「重试」——任务失败时可用，点击发 retry_requested（页面连到自己的 _run）；
    * lbl_fix 显示中文"如何修复"提示（非模态）。
    """

    message = Signal(str)
    next_step_requested = Signal(str)
    retry_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 next_target: str = "plot",
                 next_text: str = "下一步：③ 图件 / 定稿出图"):
        super().__init__(parent)
        self.next_target = str(next_target)
        self._output: Optional[Path] = None
        self._result_text = ""
        self.btn_open_output = QPushButton("打开输出文件")
        self.btn_copy_result = QPushButton("复制结果")
        self.btn_next_step = QPushButton(next_text)
        self.btn_next_step.setToolTip(
            "跳到下一步：③ 图件预览 / 定稿出图（或 ④ 档案查看历史结果）")
        self.btn_retry = QPushButton("重试")
        self.btn_retry.setToolTip("用当前参数重新运行（走同一条既有分析链路）")
        self.lbl_fix = QLabel("")
        self.lbl_fix.setWordWrap(True)
        theme.mark(self.lbl_fix, "warnHint")
        self.btn_open_output.clicked.connect(self._open_output)
        self.btn_copy_result.clicked.connect(self._copy_result)
        self.btn_next_step.clicked.connect(
            lambda: self.next_step_requested.emit(self.next_target))
        self.btn_retry.clicked.connect(self.retry_requested.emit)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.lbl_fix, 1)
        lay.addWidget(self.btn_open_output)
        lay.addWidget(self.btn_copy_result)
        lay.addWidget(self.btn_next_step)
        lay.addWidget(self.btn_retry)
        self.reset()

    # ── 对外状态接口 ──
    def set_output(self, path) -> None:
        """登记可打开的输出（文件或目录；None = 无产物 → 置灰）。"""
        self._output = Path(path) if path else None
        self.btn_open_output.setEnabled(self._output is not None)

    def set_result_text(self, text: str) -> None:
        """登记"复制结果"的文本（空串 → 置灰）。"""
        self._result_text = str(text or "")
        self.btn_copy_result.setEnabled(bool(self._result_text.strip()))

    def set_state(self, ok: bool, *, failure_hint: str = "") -> None:
        """成功后开放"下一步"；失败后开放"重试"并显示中文修复提示。"""
        ok = bool(ok)
        self.btn_next_step.setEnabled(ok)
        self.btn_retry.setEnabled(not ok)
        if ok:
            self.lbl_fix.setText("")
            self.lbl_fix.setVisible(False)
        else:
            self.lbl_fix.setText(str(failure_hint or ""))
            self.lbl_fix.setVisible(bool(failure_hint))

    def reset(self) -> None:
        """运行开始/尚无结果：四个动作全部置灰（不动 lbl_fix 文本）。"""
        self._output = None
        self._result_text = ""
        for b in (self.btn_open_output, self.btn_copy_result,
                  self.btn_next_step, self.btn_retry):
            b.setEnabled(False)

    # ── 动作 ──
    def _open_output(self) -> None:
        if self._output is None:
            self.message.emit("还没有可打开的输出文件")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        try:
            opened = QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._output)))
        except Exception as err:                       # noqa: BLE001
            self.message.emit(f"打开输出文件失败：{err}")
            return
        self.message.emit(f"已打开输出文件：{self._output}" if opened
                          else f"无法自动打开，请手动前往：{self._output}")

    def _copy_result(self) -> None:
        from PySide6.QtGui import QGuiApplication
        text = self._result_text.strip()
        if not text:
            self.message.emit("当前没有可复制的结果")
            return
        try:
            QGuiApplication.clipboard().setText(self._result_text)
        except Exception as err:                       # noqa: BLE001
            self.message.emit(f"复制结果失败：{err}")
            return
        self.message.emit("结果已复制到剪贴板")
