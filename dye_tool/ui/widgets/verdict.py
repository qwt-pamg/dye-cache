# -*- coding: utf-8 -*-
"""Φ_Δ 页画布上方的「结论条」：一眼看到数、一句话结论、各组角色。

一行三块：

* 左：小标签（``Φ_Δ · 样品名``）+ 大号等宽数值，误差（``± 0.09``）小一号、弱色；
* 中：粗体结论句 + 1–3 行弱色说明；
* 右：各组一行 —— 色点、组名、等宽小字（k_obs 等）、角色胶囊（ok / warn）。

三种模式：:meth:`VerdictCard.set_empty`（还没结果）、:meth:`VerdictCard.set_failed`
（失败，结论句用 danger 色）、:meth:`VerdictCard.set_verdict`。整张卡最高约 120px；
窄了文字按单行省略，悬停看全文，不撑高卡片。
"""
from __future__ import annotations

from typing import Optional, Sequence

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QSizePolicy,
                               QVBoxLayout, QWidget)

from ui import theme

MAX_LINES = 3
MAX_GROUP_ROWS = 4
LABEL_MAX_W = 170


@theme.register_qss
def _qss(p: dict) -> str:
    return f"""
QFrame#phiVerdict {{ background: {p['surface']}; border: 1px solid {p['border']};
                    border-radius: 8px; }}
QFrame#phiVerdict[failed="true"] {{ border-color: {p['danger']}; background: {p['danger_bg']}; }}
QFrame#phiVerdict QLabel {{ background: transparent; border: none; }}
QFrame#phiVerdictSep {{ background: {p['border']}; border: none; }}
QLabel#phiVerdictLabel {{ color: {p['text_muted']}; font-size: 11px; }}
QLabel#phiVerdictValue {{ color: {p['accent']}; font-family: {theme.MONO_FONTS};
                         font-size: 30px; font-weight: bold; }}
QLabel#phiVerdictError {{ color: {p['text_muted']}; font-family: {theme.MONO_FONTS};
                         font-size: 14px; }}
QLabel#phiVerdictHeadline {{ color: {p['text']}; font-size: 15px; font-weight: bold; }}
QLabel#phiVerdictHeadline[empty="true"] {{ color: {p['text_muted']}; font-weight: normal; }}
QLabel#phiVerdictHeadline[failed="true"] {{ color: {p['danger']}; }}
QLabel#phiVerdictLine {{ color: {p['text_muted']}; font-size: 12px; }}
QLabel#phiVerdictGroup {{ color: {p['text']}; font-size: 12px; }}
QLabel#phiVerdictSub {{ color: {p['text_muted']}; font-family: {theme.MONO_FONTS};
                       font-size: 11px; }}
QLabel#phiVerdictPill {{ color: {p['success']}; border: 1px solid {p['success']};
                        border-radius: 8px; padding: 0 6px; font-size: 11px; }}
QLabel#phiVerdictPill[warn="true"] {{ color: {p['warn']}; background: {p['warn_bg']};
                                     border-color: {p['warn_border']}; }}
"""


class _ElidedLabel(QLabel):
    """单行、放不下就省略号收尾；被省略时悬停提示全文。宽度由布局给，不按文字撑宽。"""

    def __init__(self, name: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._full = ""

    def set_full_text(self, text: str) -> None:
        self._full = str(text or "")
        self._refit()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._refit()

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._refit()

    def _refit(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(self.width(), 1))
        if shown != self.text():
            self.setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class _Dot(QWidget):
    """组色点：``color`` 为色板令牌名（``accent`` / ``series_ref``…）或 ``#rrggbb``，绘制时现取。"""

    def __init__(self, color: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.color_spec = str(color or "")
        self.setFixedSize(10, 10)

    def _qcolor(self) -> QColor:
        spec = self.color_spec
        if spec.startswith("#"):
            col = QColor(spec)
            if col.isValid():
                return col
        elif spec in theme.PALETTES[theme.current()]:
            return theme.color(spec)
        return theme.color("text_muted")

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._qcolor())
        p.drawEllipse(QRectF(1, 1, 8, 8))


def _sep(parent: QWidget) -> QFrame:
    s = QFrame(parent)
    s.setObjectName("phiVerdictSep")
    s.setFixedWidth(1)
    return s


class VerdictCard(QFrame):
    """结论条。页面按分析结果调 ``set_empty`` / ``set_failed`` / ``set_verdict``。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("phiVerdict")
        self.setMaximumHeight(120)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        self._mode = "empty"
        self._value = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(12)

        # 左：标签 + 数值
        self.box_value = QWidget(self)
        lv = QVBoxLayout(self.box_value)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.setSpacing(0)
        self.lbl_label = _ElidedLabel("phiVerdictLabel", self.box_value)
        num = QHBoxLayout()
        num.setContentsMargins(0, 0, 0, 0)
        num.setSpacing(6)
        self.lbl_value = QLabel(self.box_value)
        self.lbl_value.setObjectName("phiVerdictValue")
        self.lbl_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_error = QLabel(self.box_value)
        self.lbl_error.setObjectName("phiVerdictError")
        self.lbl_error.setContentsMargins(0, 0, 0, 5)      # 底对齐后略抬，贴近大数的基线
        num.addWidget(self.lbl_value, 0, Qt.AlignBottom)
        num.addWidget(self.lbl_error, 0, Qt.AlignBottom)
        num.addStretch(1)
        lv.addWidget(self.lbl_label)
        lv.addLayout(num)
        lv.addStretch(1)
        self.box_value.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.sep_left = _sep(self)

        # 中：结论句 + 说明行
        self.box_text = QWidget(self)
        lt = QVBoxLayout(self.box_text)
        lt.setContentsMargins(0, 0, 0, 0)
        lt.setSpacing(2)
        self.lbl_headline = _ElidedLabel("phiVerdictHeadline", self.box_text)
        lt.addWidget(self.lbl_headline)
        self.lbl_lines = [_ElidedLabel("phiVerdictLine", self.box_text) for _ in range(MAX_LINES)]
        for lbl in self.lbl_lines:
            lt.addWidget(lbl)
        lt.addStretch(1)
        self.box_text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.sep_right = _sep(self)

        # 右：各组
        self.box_groups = QWidget(self)
        self._grid = QGridLayout(self.box_groups)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(6)
        self._grid.setVerticalSpacing(4)
        self.box_groups.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)

        row.addWidget(self.box_value)
        row.addWidget(self.sep_left)
        row.addWidget(self.box_text, 1)
        row.addWidget(self.sep_right)
        row.addWidget(self.box_groups, 0, Qt.AlignVCenter)

        self.set_empty("还没有结果")

    # ── 查询（测试用） ──
    def mode(self) -> str:
        return self._mode

    def value_text(self) -> str:
        """大号数值（不含误差）；没有数值时为空串。"""
        return self._value

    def headline_text(self) -> str:
        return self.lbl_headline.full_text()

    # ── 三种模式 ──
    def set_empty(self, headline: str, text: str = "") -> None:
        """还没有结果：只留弱色结论句 + 说明。"""
        self._set_mode("empty", headline, _split(text))

    def set_failed(self, headline: str, text: str = "") -> None:
        """分析失败：结论句用 danger 色，不显示数值与组。"""
        self._set_mode("failed", headline, _split(text))

    def set_verdict(self, *, label: str, value: str, error: str = "", headline: str,
                    lines: Sequence[str] = (), groups: Sequence[dict] = ()) -> None:
        """有结果。``groups`` 每项 ``{"name", "sub", "pill", "kind": "ok"|"warn", "color"}``，
        ``color`` 为色板令牌名或 ``#rrggbb``。"""
        self.lbl_label.set_full_text(label)
        # 标签尽量完整显示（它说明这是哪个样品），但最多占 LABEL_MAX_W，再长就省略
        self.lbl_label.ensurePolished()
        need = self.lbl_label.fontMetrics().horizontalAdvance(self.lbl_label.full_text())
        self.box_value.setMinimumWidth(min(need, LABEL_MAX_W))
        self._value = str(value or "")
        self.lbl_value.setText(self._value)
        self.lbl_error.setText(str(error or ""))
        self.lbl_error.setVisible(bool(error))
        self._fill_groups(groups)
        self._set_mode("verdict", headline, [str(s) for s in lines if str(s).strip()])

    # ── 内部 ──
    def _set_mode(self, mode: str, headline: str, lines: list[str]) -> None:
        self._mode = mode
        if mode != "verdict":
            self._value = ""
            self._fill_groups(())
        has_value = mode == "verdict"
        has_groups = has_value and self._grid.count() > 0
        self.box_value.setVisible(has_value)
        self.sep_left.setVisible(has_value)
        self.box_groups.setVisible(has_groups)
        self.sep_right.setVisible(has_groups)
        theme.set_state(self, "failed", mode == "failed")
        theme.set_state(self.lbl_headline, "failed", mode == "failed")
        theme.set_state(self.lbl_headline, "empty", mode == "empty")
        self.lbl_headline.set_full_text(headline)
        for i, lbl in enumerate(self.lbl_lines):
            lbl.set_full_text(lines[i] if i < len(lines) else "")
            lbl.setVisible(i < len(lines))

    def _fill_groups(self, groups: Sequence[dict]) -> None:
        while self._grid.count():
            w = self._grid.takeAt(0).widget()
            if w is not None:
                w.setParent(None)              # 立即摘掉，别等 deleteLater 才离开子控件树
                w.deleteLater()
        groups = list(groups)
        shown = groups if len(groups) <= MAX_GROUP_ROWS else groups[:MAX_GROUP_ROWS - 1]
        for r, g in enumerate(shown):
            dot = _Dot(g.get("color", ""), self.box_groups)
            name = QLabel(str(g.get("name", "")), self.box_groups)
            name.setObjectName("phiVerdictGroup")
            sub = QLabel(str(g.get("sub", "")), self.box_groups)
            sub.setObjectName("phiVerdictSub")
            pill = QLabel(str(g.get("pill", "")), self.box_groups)
            pill.setObjectName("phiVerdictPill")
            pill.setAlignment(Qt.AlignCenter)
            theme.set_state(pill, "warn", g.get("kind") == "warn")
            self._grid.addWidget(dot, r, 0, Qt.AlignVCenter)
            self._grid.addWidget(name, r, 1)
            self._grid.addWidget(sub, r, 2)
            if pill.text():
                self._grid.addWidget(pill, r, 3, Qt.AlignVCenter)
            else:
                pill.setParent(None)
                pill.deleteLater()
        if len(shown) < len(groups):
            more = QLabel(f"另有 {len(groups) - len(shown)} 组", self.box_groups)
            more.setObjectName("phiVerdictSub")
            self._grid.addWidget(more, len(shown), 1, 1, 3)


def _split(text: str) -> list[str]:
    return [s for s in str(text or "").splitlines() if s.strip()]
