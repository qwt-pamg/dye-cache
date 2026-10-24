# -*- coding: utf-8 -*-
"""左侧任务流导航栏（FR-16）：图标（文字）+ 名称 + 徽标。

为什么不用 ``QTabWidget``：页签是"并列的容器"，而任务流有**先后顺序**与
"当前批次"这个共享状态。左侧列表能天然表达顺序，也方便在主页之外常驻显示
"当前批次"摘要（见 :class:`core:NavBar.set_context`）。

**自动化契约**（``--gui-smoke`` 与 A16 测试都靠它驱动）：

* :meth:`NavBar.select` 按 key 切换（幂等）；
* :attr:`NavBar.current_key` 与信号 ``navigated(str)``；
* 徽标用 :meth:`NavBar.set_badge`（0 隐藏），A16 用它确认"数据已就绪"。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QVBoxLayout, QWidget)

__all__ = ["NAV_ITEMS", "NavBar", "NavBadgeRow"]

#: 导航项：``(key, 显示名, 一句话说明)``。顺序 = 用户做实验的顺序。
NAV_ITEMS: tuple = (
    ("home", "主页", "从这里开始：最近批次、一键演示、命名速查"),
    ("data", "① 数据", "批次工作区：导入 → 整定 → QC 体检"),
    ("analysis", "② 分析", "Φ_Δ / 动力学对比 / ε / A@λ"),
    ("plot", "③ 图件", "堆叠、叠加、荧光与紫外常规作图"),
    ("archive", "④ 档案", "染料库、历史 run、结果直达"),
    ("settings", "⑤ 设置", "参数、配置校验、目录整理"),
)


class NavBar(QWidget):
    """左侧导航（列表 + 底部"当前批次"摘要）。"""

    navigated = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedWidth(176)
        self._keys: list[str] = []
        self._badges: dict[str, str] = {}
        self._build()

    # ── 构建 ──
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        title = QLabel("dye_tool")
        title.setObjectName("navTitle")
        title.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        title.setContentsMargins(14, 16, 8, 2)
        lay.addWidget(title)
        self.lbl_context = QLabel("未选择批次")
        self.lbl_context.setObjectName("navContext")
        self.lbl_context.setWordWrap(True)
        self.lbl_context.setContentsMargins(14, 0, 8, 10)
        lay.addWidget(self.lbl_context)

        self.list = QListWidget()
        self.list.setObjectName("navList")
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setSpacing(2)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for key, label, tip in NAV_ITEMS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setToolTip(tip)
            self.list.addItem(item)
            self._keys.append(key)
        self.list.currentRowChanged.connect(self._on_row)
        lay.addWidget(self.list, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("navStatus")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setContentsMargins(14, 6, 8, 12)
        lay.addWidget(self.lbl_status)

    # ── 对外 API ──
    @property
    def current_key(self) -> str:
        row = self.list.currentRow()
        return self._keys[row] if 0 <= row < len(self._keys) else ""

    def select(self, key: str) -> bool:
        """按 key 切换导航（幂等）；key 不存在返回 False。"""
        if key not in self._keys:
            return False
        row = self._keys.index(key)
        if self.list.currentRow() != row:
            self.list.setCurrentRow(row)         # 触发 _on_row → navigated
        else:
            self.navigated.emit(key)             # 已经在这一页：仍通知（幂等刷新）
        return True

    def set_badge(self, key: str, text: str) -> None:
        """给某项加徽标（空串 = 清除），如数据页的"35 个文件 / 2 剔除"。"""
        if key not in self._keys:
            return
        self._badges[key] = text
        row = self._keys.index(key)
        item = self.list.item(row)
        labels = {k: lb for k, lb, _t in NAV_ITEMS}
        tips = {k: t for k, _l, t in NAV_ITEMS}
        label = labels[key]
        item.setText(f"{label}   {text}" if text else label)
        item.setToolTip(f"{tips[key]}\n{text}" if text else tips[key])

    def set_context(self, text: str) -> None:
        """底部"当前批次"摘要（数据页载入后由壳更新）。"""
        self.lbl_context.setText(text or "未选择批次")

    def _on_row(self, row: int) -> None:
        if 0 <= row < len(self._keys):
            self.navigated.emit(self._keys[row])


class NavBadgeRow(QWidget):
    """（预留）导航项内联徽标行——当前用文本 suffix，保留类名避免未来重排破坏导入。"""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text)
        lay.addWidget(self.label)
        lay.addStretch(1)
