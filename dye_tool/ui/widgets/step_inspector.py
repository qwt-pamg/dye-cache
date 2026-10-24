# -*- coding: utf-8 -*-
"""分步工作台的右栏检查器 + 三栏骨架（混搭稿：B 的骨架 + A 的流程条）。

检查器只展开**当前这一步**能改的东西，其余步各折成一行摘要（点一下跳过去），
底部是本页的主按钮与「← 上一步 / 下一步 →」。它不自己决定切步：所有切步请求都
经 :attr:`StepInspector.step_requested` 交给页面，由页面同时切流程条、中栏与检查器。

用法（页面侧）::

    self.flow_bar = FlowBar(STEPS)
    self.inspector = StepInspector(STEPS, HINTS)
    self.inspector.set_page(0, page0_widget) ...
    self.inspector.add_action(self.btn_run)
    self.flow_bar.step_clicked.connect(self._go_step)
    self.inspector.step_requested.connect(self._go_step)
    body = three_columns(left, center, self.inspector)

    def _go_step(self, i):
        self.flow_bar.set_current(i)
        self.inspector.set_current(i, next_enabled=self.flow_bar.step_enabled(i + 1))
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ui import theme

#: 三栏默认宽度（左栏 / 右栏）；中栏吃剩下的
LEFT_W, RIGHT_W = 230, 320


@theme.register_qss
def _qss(p: dict) -> str:
    return f"""
QFrame#stepInspector {{ background: {p['surface']}; border: 1px solid {p['border']};
                       border-radius: 8px; }}
QPushButton#stepSummary {{ text-align: left; border: none; border-top: 1px solid {p['border']};
                          border-radius: 0; background: transparent; padding: 6px 2px;
                          color: {p['text_muted']}; }}
QPushButton#stepSummary:hover {{ color: {p['accent']}; }}
"""


class StepInspector(QFrame):
    """右栏：第 N 步标题 + 说明 / 该步页面 / 其余步摘要 / 主按钮 / 前后步。"""

    #: 用户在检查器里要求切到第 idx 步（摘要行、上一步、下一步）
    step_requested = Signal(int)

    def __init__(self, steps: Sequence[str], hints: Sequence[str] = (),
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("stepInspector")
        self._steps = list(steps)
        self._hints = list(hints) + [""] * (len(self._steps) - len(hints))
        self._current = -1
        self._summary_of: Callable[[int], str] = lambda _i: ""
        #: 主按钮及其所属步（None = 每步都显示）
        self._step_actions: list[tuple[QWidget, Optional[frozenset]]] = []

        self.lbl_step = QLabel("")
        theme.mark(self.lbl_step, "strong")
        self.lbl_hint = QLabel("")
        theme.mark(self.lbl_hint, "hint")
        self.lbl_hint.setWordWrap(True)
        self.stack = QStackedWidget()
        for _ in self._steps:
            self.stack.addWidget(QWidget())     # 占位：页面用 set_page 换成真内容

        self.summaries: list[QPushButton] = []
        sums = QVBoxLayout()
        sums.setSpacing(2)
        for i, name in enumerate(self._steps):
            b = QPushButton()
            b.setObjectName("stepSummary")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"切到「{name}」这一步")
            b.clicked.connect(lambda _=False, i=i: self.step_requested.emit(i))
            self.summaries.append(b)
            sums.addWidget(b)

        self._actions = QVBoxLayout()
        self._actions.setSpacing(6)
        self.btn_prev = QPushButton("←")
        self.btn_prev.setToolTip("回到上一步")
        self.btn_prev.clicked.connect(lambda: self.step_requested.emit(self._current - 1))
        self.btn_next = QPushButton("下一步 →")
        self.btn_next.setToolTip("去下一步")
        self.btn_next.clicked.connect(lambda: self.step_requested.emit(self._current + 1))
        nav = QHBoxLayout()
        nav.addWidget(self.btn_prev)
        nav.addWidget(self.btn_next, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)
        lay.addWidget(self.lbl_step)
        lay.addWidget(self.lbl_hint)
        lay.addWidget(self.stack, 1)
        lay.addLayout(sums)
        lay.addLayout(self._actions)
        lay.addLayout(nav)
        self.setMinimumWidth(300)
        self.setMaximumWidth(360)
        # 只有一步时没有"其余步"，也没有前后步
        if len(self._steps) <= 1:
            for b in self.summaries:
                b.hide()
            self.btn_prev.hide()
            self.btn_next.hide()

    # ── 内容 ──
    def set_page(self, idx: int, widget: QWidget) -> None:
        """第 idx 步的检查器内容（替换占位页）。"""
        old = self.stack.widget(idx)
        self.stack.insertWidget(idx, widget)
        self.stack.removeWidget(old)
        old.deleteLater()
        if idx == self._current:
            self.stack.setCurrentIndex(idx)

    def page(self, idx: int) -> QWidget:
        return self.stack.widget(idx)

    def add_action(self, widget: QWidget,
                   steps: Union[int, Iterable[int], None] = None) -> None:
        """主按钮（如「▶ 开始分析」）放在摘要下方、前后步上方。

        ``steps`` 给定时只在这些步显示（如光物理每步一套主按钮、出图那一步的
        「定稿出图」）；不给则每步都显示。
        """
        own = None
        if steps is not None:
            own = frozenset([steps] if isinstance(steps, int) else steps)
        self._step_actions.append((widget, own))
        self._actions.addWidget(widget)
        if own is not None:
            widget.setVisible(self._current in own)

    def set_summary_provider(self, fn: Callable[[int], str]) -> None:
        """``fn(i) -> str``：第 i 步当前设置的一行摘要（只读已有状态）。"""
        self._summary_of = fn
        self.refresh_summaries()

    # ── 切步 ──
    def current(self) -> int:
        return self._current

    def count(self) -> int:
        return len(self._steps)

    def set_current(self, idx: int, *, next_enabled: bool = True) -> None:
        """切到第 idx 步（不发信号）：标题、说明、页面、摘要与前后步跟着变。"""
        idx = max(0, min(len(self._steps) - 1, int(idx)))
        self._current = idx
        self.stack.setCurrentIndex(idx)
        self.lbl_step.setText(f"第 {idx + 1} 步 · {self._steps[idx]}"
                              if len(self._steps) > 1 else self._steps[idx])
        self.lbl_hint.setText(self._hints[idx])
        self.lbl_hint.setVisible(bool(self._hints[idx]))
        if len(self._steps) > 1:
            self.btn_prev.setVisible(idx > 0)
            if idx > 0:
                self.btn_prev.setText(f"← {self._steps[idx - 1]}")
            last = idx >= len(self._steps) - 1
            self.btn_next.setVisible(not last)
            if not last:
                self.btn_next.setText(f"下一步：{self._steps[idx + 1]} →")
        for w, own in self._step_actions:
            if own is not None:
                w.setVisible(idx in own)
        self.set_next_enabled(next_enabled)
        self.refresh_summaries()

    def set_next_enabled(self, on: bool) -> None:
        self.btn_next.setEnabled(bool(on))

    def refresh_summaries(self) -> None:
        if len(self._steps) <= 1:
            return
        for i, b in enumerate(self.summaries):
            b.setVisible(i != self._current)
            text = self._summary_of(i)
            b.setText(f"{self._steps[i]}　{text}" if text else self._steps[i])


def three_columns(left: Optional[QWidget], center: QWidget, right: QWidget) -> QSplitter:
    """三栏 splitter：左（可为 None）/ 中（伸缩）/ 右（检查器）。"""
    body = QSplitter(Qt.Horizontal)
    widgets = [w for w in (left, center, right) if w is not None]
    for w in widgets:
        body.addWidget(w)
    body.setChildrenCollapsible(False)
    for i, w in enumerate(widgets):
        body.setStretchFactor(i, 1 if w is center else 0)
    body.setSizes([LEFT_W if w is left else (RIGHT_W if w is right else 600) for w in widgets])
    return body
