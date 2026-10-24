# -*- coding: utf-8 -*-
"""左侧任务流导航：深色图标栏（rail，56px）。

为什么不用 ``QTabWidget``：页签是"并列的容器"，而任务流有**先后顺序**。
竖排图标栏天然表达顺序，又只占 56px，把宽度留给工作区。"当前批次"摘要
放不进窄栏，由主窗口状态栏常驻显示（见 ``MainWindow.lbl_context``）。

**自动化契约**（``--gui-smoke`` 与 A16 测试都靠它驱动）：

* :meth:`NavBar.select` 按 key 切换（幂等）；
* :attr:`NavBar.current_key` 与信号 ``navigated(str)``；
* 徽标用 :meth:`NavBar.set_badge`（空串清除）、:meth:`NavBar.badge` 读回，
  A16 用它确认"数据已就绪"。
"""
from __future__ import annotations

import re
from typing import Optional

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath,
                           QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QVBoxLayout,
                               QWidget)

from ui import theme

__all__ = ["NAV_ITEMS", "NavBar", "rail_icon"]

#: 导航项：``(key, 短名, 一句话说明)``。顺序 = 用户做实验的顺序；最后一项钉在栏底。
NAV_ITEMS: tuple = (
    ("home", "主页", "从这里开始：最近批次、一键演示、命名速查"),
    ("data", "数据", "批次工作区：导入 → 整定 → QC 体检"),
    ("analysis", "分析", "Φ_Δ / 动力学对比 / ε / A@λ"),
    ("plot", "图件", "堆叠、叠加、荧光与紫外常规作图"),
    ("archive", "档案", "染料库、历史 run、结果直达"),
    ("settings", "设置", "参数、配置校验、目录整理"),
)

#: 线条图标（24×24 视框，``{c}`` 处填描边色）
_ICON_SVG: dict[str, str] = {
    "home": '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h5v-6h3v6h5V9.5"/>',
    "data": '<path d="M3 7.5A2 2 0 0 1 5 5.5h4l2 2.2h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5'
            'a2 2 0 0 1-2-2z"/>',
    "analysis": '<path d="M4 4v16h16"/><path d="M7 15.5l3.5-4.5 3 3L19 7"/>',
    "plot": '<rect x="3" y="4.5" width="18" height="15" rx="2"/>'
            '<circle cx="8.5" cy="9.5" r="1.6"/><path d="M21 15.5l-5-4.5-9.5 8.5"/>',
    "archive": '<rect x="3" y="4" width="18" height="4.5" rx="1"/>'
               '<path d="M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5"/><path d="M10 12.5h4"/>',
    "settings": '<circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2.3"/>'
                '<path d="M12 2.5v3.5M12 18v3.5M2.5 12H6M18 12h3.5M5.3 5.3l2.5 2.5'
                'M16.2 16.2l2.5 2.5M5.3 18.7l2.5-2.5M16.2 7.8l2.5-2.5"/>',
}

_TILE = 40          # 图标底块边长
_RADIUS = 9
_ICON = 20
_LABEL_H = 14

_icon_cache: dict[tuple, QIcon] = {}


def rail_icon(key: str, color: QColor, size: int = _ICON, dpr: float = 1.0) -> QIcon:
    """把 ``key`` 的线条图标按 ``color`` 着色渲染成 QIcon（按颜色/尺寸缓存）。"""
    ck = (key, color.name(QColor.HexArgb), size, round(dpr, 2))
    icon = _icon_cache.get(ck)
    if icon is not None:
        return icon
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           'stroke="{c}" stroke-width="1.8" stroke-linecap="round" '
           'stroke-linejoin="round">' + _ICON_SVG[key] + '</svg>').replace(
               "{c}", color.name())
    px = int(round(size * dpr))
    pix = QPixmap(px, px)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, px, px))
    p.end()
    pix.setDevicePixelRatio(dpr)
    icon = QIcon(pix)
    _icon_cache[ck] = icon
    return icon


def _rail_tok(token: str) -> QColor:
    """图标栏配色。

    ``rail_bg`` / ``rail_text`` 跟随当前色板；强调色两套主题都取**深色色板**的
    ``accent`` / ``accent_text``——栏本身在浅色主题下也是深底，浅色色板的深青
    强调色压在深底上对比度不够（色板里还没有专门的 ``rail_accent`` 令牌）。
    """
    if token.startswith("rail_"):
        return theme.color(token)
    return QColor(theme.PALETTES["dark"][token])


def _badge_short(text: str) -> str:
    """徽标全文 → 角标：取第一个整数（>99 记作 99+）；没有数字返回空串（画圆点）。"""
    m = re.search(r"\d+", text or "")
    if not m:
        return ""
    n = int(m.group())
    return "99+" if n > 99 else str(n)


class _RailButton(QAbstractButton):
    """图标栏的一项：40×40 圆角底块 + 着色图标 + 10px 短名 + 角标（自绘）。"""

    def __init__(self, key: str, label: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.key = key
        self.setText(label)
        self.setAccessibleName(label)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFixedSize(56, 4 + _TILE + 2 + _LABEL_H + 2)
        self.badge = ""
        self._kb_focus = False          # 只在键盘聚焦时画焦点环（鼠标点击不画）

    def sizeHint(self) -> QSize:                              # noqa: N802
        return QSize(56, 4 + _TILE + 2 + _LABEL_H + 2)

    def set_badge(self, text: str) -> None:
        self.badge = text or ""
        self.update()

    def focusInEvent(self, event) -> None:                    # noqa: N802
        self._kb_focus = event.reason() != Qt.MouseFocusReason
        super().focusInEvent(event)

    def focusOutEvent(self, event) -> None:                   # noqa: N802
        self._kb_focus = False
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:                   # noqa: N802
        bar = self.parentWidget()
        if event.key() in (Qt.Key_Up, Qt.Key_Down) and isinstance(bar, NavBar):
            bar._step(-1 if event.key() == Qt.Key_Up else 1)
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event) -> None:                     # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        tile = QRectF((self.width() - _TILE) / 2, 4, _TILE, _TILE)
        accent = _rail_tok("accent")
        fg = _rail_tok("rail_text")
        hover = self.underMouse()
        if self.isChecked():
            bg = QColor(accent)
            bg.setAlphaF(0.20)
            fg = accent
        elif hover:
            bg = QColor(fg)
            bg.setAlphaF(0.12)
            fg = fg.lighter(135)
        else:
            bg = None
        if bg is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(bg)
            p.drawRoundedRect(tile, _RADIUS, _RADIUS)
        if self.hasFocus() and self._kb_focus:
            pen = p.pen()
            pen.setColor(accent)
            pen.setWidthF(1.6)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(tile.adjusted(0.8, 0.8, -0.8, -0.8), _RADIUS, _RADIUS)
        # 图标
        ir = QRectF(tile.center().x() - _ICON / 2, tile.center().y() - _ICON / 2,
                    _ICON, _ICON).toRect()
        rail_icon(self.key, fg, _ICON, self.devicePixelRatioF()).paint(p, ir)
        # 短名
        f = QFont(self.font())
        f.setPixelSize(10)
        f.setBold(self.isChecked())
        p.setFont(f)
        p.setPen(fg)
        p.drawText(QRectF(0, tile.bottom() + 2, self.width(), _LABEL_H),
                   Qt.AlignHCenter | Qt.AlignTop, self.text())
        # 角标：数字气泡 / 圆点，压在底块右上角
        if self.badge:
            self._paint_badge(p, tile, accent)
        p.end()

    def _paint_badge(self, p: QPainter, tile: QRectF, accent: QColor) -> None:
        short = _badge_short(self.badge)
        ring = _rail_tok("rail_bg")
        p.setPen(Qt.NoPen)
        if not short:
            d = 8.0
            r = QRectF(tile.right() - d + 1, tile.top() - 1, d, d)
            p.setBrush(ring)
            p.drawEllipse(r.adjusted(-1.5, -1.5, 1.5, 1.5))
            p.setBrush(accent)
            p.drawEllipse(r)
            return
        f = QFont(self.font())
        f.setPixelSize(9)
        f.setBold(True)
        p.setFont(f)
        h = 14.0
        w = max(h, p.fontMetrics().horizontalAdvance(short) + 8.0)
        r = QRectF(tile.right() - w + 5, tile.top() - 4, w, h)
        path = QPainterPath()
        path.addRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), h / 2 + 1.5, h / 2 + 1.5)
        p.fillPath(path, ring)
        path = QPainterPath()
        path.addRoundedRect(r, h / 2, h / 2)
        p.fillPath(path, accent)
        p.setPen(_rail_tok("accent_text"))
        p.drawText(r, Qt.AlignCenter, short)


@theme.register_qss
def _rail_qss(p: dict[str, str]) -> str:
    return f"""
/* ── 左侧图标栏（ui/window/nav.py） ── */
NavBar {{ background: {p['rail_bg']}; border: none; }}
"""


class NavBar(QWidget):
    """左侧深色图标栏：每个 NAV_ITEMS 一项，"设置"钉在栏底。"""

    navigated = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedWidth(56)
        # 让 QSS 的 background 对这个自定义 QWidget 子类生效
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._keys: list[str] = [key for key, _l, _t in NAV_ITEMS]
        self._tips: dict[str, str] = {key: tip for key, _l, tip in NAV_ITEMS}
        self._badges: dict[str, str] = {}
        self._shortcuts: dict[str, str] = {}
        self._current = ""
        self.buttons: dict[str, _RailButton] = {}
        self._build()

    # ── 构建 ──
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 10, 0, 10)
        lay.setSpacing(6)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        last = NAV_ITEMS[-1][0]
        for key, label, _tip in NAV_ITEMS:
            btn = _RailButton(key, label, self)
            btn.clicked.connect(lambda _=False, k=key: self.select(k))
            self._group.addButton(btn)
            self.buttons[key] = btn
            if key == last:
                lay.addStretch(1)
            lay.addWidget(btn, 0, Qt.AlignHCenter)
            self._refresh_tip(key)

    # ── 对外 API ──
    @property
    def current_key(self) -> str:
        return self._current

    def select(self, key: str) -> bool:
        """按 key 切换导航（幂等）；key 不存在返回 False。"""
        if key not in self._keys:
            return False
        self.buttons[key].setChecked(True)
        self._current = key
        self.navigated.emit(key)                # 已经在这一页也通知（幂等刷新）
        return True

    def set_badge(self, key: str, text: str) -> None:
        """给某项加徽标（空串 = 清除），如数据页的"35 个文件 / 2 剔除"。

        图标角上只画数字（或圆点），全文进该项的 tooltip。
        """
        if key not in self._keys:
            return
        self._badges[key] = text or ""
        self.buttons[key].set_badge(text or "")
        self._refresh_tip(key)

    def badge(self, key: str) -> str:
        """读回某项徽标全文（未设置为空串）。"""
        return self._badges.get(key, "")

    def set_shortcut(self, key: str, seq: str) -> None:
        """在该项 tooltip 末尾标注快捷键（FR-40.3）。"""
        if key not in self._keys:
            return
        self._shortcuts[key] = seq
        self._refresh_tip(key)

    # ── 内部 ──
    def _refresh_tip(self, key: str) -> None:
        label = self.buttons[key].text()
        lines = [f"{label}：{self._tips[key]}"]
        if self._badges.get(key):
            lines.append(self._badges[key])
        if self._shortcuts.get(key):
            lines.append(f"快捷键：{self._shortcuts[key]}")
        self.buttons[key].setToolTip("\n".join(lines))

    def _step(self, delta: int) -> None:
        """↑/↓：切到上/下一项并把焦点（与焦点环）移过去。"""
        cur = self._current if self._current in self._keys else self._keys[0]
        i = max(0, min(len(self._keys) - 1, self._keys.index(cur) + delta))
        key = self._keys[i]
        self.select(key)
        self.buttons[key].setFocus(Qt.TabFocusReason)
