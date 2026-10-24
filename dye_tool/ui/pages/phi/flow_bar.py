# -*- coding: utf-8 -*-
"""Φ_Δ 页顶部的流程条：``导入 → 分组与 QC → 拟合 k_obs → Φ_Δ → 出图导出``。

不是强制向导：每一步都能直接点，点了只发 ``step_clicked(idx)``，切不切页、
高亮哪一步由页面决定（:meth:`FlowBar.set_current` 不会再发信号）。

每步有三种状态（:data:`STEP_TODO` / :data:`STEP_DONE` / :data:`STEP_STALE`），
外加"当前步"高亮与"不可用"置灰。圆圈、连线都是自绘，颜色在绘制时现取
:func:`ui.theme.color`，切主题后重绘即生效。
"""
from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget

from ui import theme

STEP_TODO, STEP_DONE, STEP_STALE = "todo", "done", "stale"
STEP_STATES = (STEP_TODO, STEP_DONE, STEP_STALE)
STALE_TIP = "参数改过，需要重算"
_STATE_WORDS = {STEP_TODO: "未完成", STEP_DONE: "已完成", STEP_STALE: "需要重算"}


class _Step(QWidget):
    """一步：编号圆圈 + 步骤名；当前步垫一块 accent_soft 圆角底。"""

    clicked = Signal()
    #: 键盘左右键：-1 / +1，由流程条把焦点挪到相邻步
    nav = Signal(int)

    H, D, PAD, GAP = 30, 20, 9, 7      # 高、圆直径、左右内边距、圆与字间距

    def __init__(self, idx: int, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("phiFlowStep")
        self.idx, self.name = idx, str(name)
        self.state = STEP_TODO
        self.current = False
        self.reason = ""
        self._kbd_focus = False         # 只有键盘带来的焦点才画焦点环
        self._pressed = False
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._refresh_meta()

    # ── 尺寸：按粗体量宽，当前步切换时不跳动 ──
    def _bold_font(self) -> QFont:
        f = QFont(self.font())
        f.setBold(True)
        return f

    def sizeHint(self) -> QSize:
        w = QFontMetrics(self._bold_font()).horizontalAdvance(self.name)
        return QSize(self.PAD + self.D + self.GAP + w + self.PAD + 2, self.H)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # ── 状态 ──
    def _refresh_meta(self) -> None:
        tips = []
        if self.state == STEP_STALE:
            tips.append(STALE_TIP)
        if not self.isEnabled() and self.reason:
            tips.append(self.reason)
        self.setToolTip("\n".join(tips))
        words = [_STATE_WORDS[self.state]]
        if self.current:
            words.insert(0, "当前")
        if not self.isEnabled():
            words.append("不可用")
        self.setAccessibleName(f"第 {self.idx + 1} 步 {self.name}（{'，'.join(words)}）")
        self.update()

    # ── 交互 ──
    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._pressed = True
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._pressed:
            self._pressed = False
            if self.rect().contains(e.position().toPoint()):
                self.clicked.emit()
            e.accept()
        else:
            super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:
        key = e.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
        elif key in (Qt.Key_Left, Qt.Key_Right):
            self.nav.emit(-1 if key == Qt.Key_Left else 1)
        else:
            super().keyPressEvent(e)

    def focusInEvent(self, e) -> None:
        self._kbd_focus = e.reason() not in (Qt.MouseFocusReason, Qt.ActiveWindowFocusReason,
                                             Qt.PopupFocusReason)
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e) -> None:
        self._kbd_focus = False
        super().focusOutEvent(e)
        self.update()

    # ── 绘制 ──
    def paintEvent(self, _e) -> None:
        c = theme.color
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        rr = r.height() / 2
        enabled = self.isEnabled()

        # 底：当前步 accent_soft 胶囊；悬停给一层浅底
        if self.current or (enabled and self.underMouse()):
            p.setPen(Qt.NoPen)
            p.setBrush(c("accent_soft") if self.current else c("surface_alt"))
            p.drawRoundedRect(r, rr, rr)
        if self.hasFocus() and self._kbd_focus:
            p.setPen(QPen(c("accent"), 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(0.75, 0.75, -0.75, -0.75), rr, rr)

        # 圆圈：填充 / 描边 / 字形 / 字色，按 不可用 > 当前 > 过期 > 完成 > 待办
        num = str(self.idx + 1)
        fill = None
        if not enabled:
            border, glyph, gcol, tcol = c("border"), num, c("text_subtle"), c("text_subtle")
        elif self.current:
            fill, border, glyph, gcol, tcol = c("accent"), c("accent"), num, c("accent_text"), c("text")
        elif self.state == STEP_STALE:
            fill, border, glyph, gcol, tcol = c("warn_bg"), c("warn"), "!", c("warn"), c("warn")
        elif self.state == STEP_DONE:
            fill, border, glyph, gcol, tcol = c("accent_soft"), c("accent"), "✓", c("accent"), c("text")
        else:
            border, glyph, gcol, tcol = c("border_strong"), num, c("text_muted"), c("text_muted")

        circ = QRectF(self.PAD, (self.H - self.D) / 2, self.D, self.D)
        p.setPen(QPen(border, 1.3))
        p.setBrush(fill if fill is not None else Qt.NoBrush)
        p.drawEllipse(circ.adjusted(0.65, 0.65, -0.65, -0.65))
        gf = QFont(self.font())
        gf.setBold(True)
        gf.setPixelSize(11)
        p.setFont(gf)
        p.setPen(gcol)
        p.drawText(circ, Qt.AlignCenter, glyph)

        # 当前步同时过期：圆圈右上角一枚 warn 小点（外圈用胶囊底色隔开）
        if enabled and self.current and self.state == STEP_STALE:
            p.setPen(QPen(c("accent_soft"), 1.5))
            p.setBrush(c("warn"))
            p.drawEllipse(QRectF(circ.right() - 6, circ.top() - 1, 8, 8))

        p.setFont(self._bold_font() if self.current else self.font())
        p.setPen(tcol)
        x = self.PAD + self.D + self.GAP
        p.drawText(QRectF(x, 0, self.width() - x, self.H), Qt.AlignVCenter | Qt.AlignLeft, self.name)


class _Connector(QWidget):
    """两步之间的短线：前一步已完成为 accent，否则为边框色。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.done = False
        self.setFixedSize(20, _Step.H)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(theme.color("accent" if self.done else "border"), 2, Qt.SolidLine, Qt.RoundCap))
        y = self.height() / 2
        p.drawLine(QPointF(3, y), QPointF(self.width() - 3, y))


class FlowBar(QWidget):
    """横向步骤条。页面监听 :attr:`step_clicked`，自己决定切到哪一步。"""

    #: 用户点了第 idx 步（已是当前步也照发）
    step_clicked = Signal(int)

    def __init__(self, steps: Sequence[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("phiFlowBar")
        self._current = -1
        self._steps: list[_Step] = []
        self._links: list[_Connector] = []      # _links[i] 接在第 i 步之后
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        for i, name in enumerate(steps):
            if i:
                link = _Connector(self)
                self._links.append(link)
                lay.addWidget(link)
            st = _Step(i, name, self)
            st.clicked.connect(lambda i=i: self.step_clicked.emit(i))
            st.nav.connect(lambda d, i=i: self._focus_step(i + d))
            self._steps.append(st)
            lay.addWidget(st)
        lay.addStretch(1)

    def _check(self, idx: int) -> _Step:
        if not 0 <= idx < len(self._steps):
            raise IndexError(f"流程条没有第 {idx} 步（共 {len(self._steps)} 步）")
        return self._steps[idx]

    def _focus_step(self, idx: int) -> None:
        # 跳过不可用的步，越界就停在原地
        step = 1 if idx > 0 else -1
        while 0 <= idx < len(self._steps):
            if self._steps[idx].isEnabled():
                self._steps[idx].setFocus(Qt.TabFocusReason)
                return
            idx += step

    # ── 公开接口 ──
    def count(self) -> int:
        return len(self._steps)

    def current(self) -> int:
        return self._current

    def set_current(self, idx: int) -> None:
        """高亮第 idx 步（``-1`` = 都不高亮）；不发 :attr:`step_clicked`。"""
        if idx != -1:
            self._check(idx)
        self._current = idx
        for st in self._steps:
            st.current = st.idx == idx
            st._refresh_meta()

    def state(self, idx: int) -> str:
        return self._check(idx).state

    def set_state(self, idx: int, state: str) -> None:
        if state not in STEP_STATES:
            raise ValueError(f"未知的步骤状态：{state!r}（应为 {'/'.join(STEP_STATES)}）")
        st = self._check(idx)
        st.state = state
        st._refresh_meta()
        if idx < len(self._links):
            self._links[idx].done = state == STEP_DONE
            self._links[idx].update()

    def set_step_enabled(self, idx: int, on: bool, reason: str = "") -> None:
        """不可用 = 置灰、点了不响应；悬停提示 ``reason``。"""
        st = self._check(idx)
        st.reason = "" if on else str(reason or "")
        st.setEnabled(bool(on))
        st.setCursor(Qt.PointingHandCursor if on else Qt.ArrowCursor)
        st._refresh_meta()

    def step_enabled(self, idx: int) -> bool:
        return self._check(idx).isEnabled()
