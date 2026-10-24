# -*- coding: utf-8 -*-
"""Φ_Δ 页的「按染料组分页」页签条（A 道 FR-23）。

页签只切换**视图过滤**：文件表与画布始终是同一实例，绝不按组复制控件/状态。
本控件只管页签本身（重建、保位、标题），选中变化时发出
``groups_selected(None | set[str])``，由页面把过滤应用到文件表与画布。
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QTabBar, QWidget

ALL_GROUPS_TITLE = "全部组（对比）"


class GroupTabs(QTabBar):
    """``[全部组（对比）]`` + 每组一个页签；不足 2 组或能力关闭时整条隐藏。"""

    #: ``None`` = 全部组；否则为该页签代表的组名集合
    groups_selected = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        self.setVisible(False)
        self._enabled = False              # flow 布局由主窗口开启；classic 保持关闭
        self._tab_groups: list = []        # [None(对比), {组1}, {组2}, …]
        self.currentChanged.connect(self._emit)

    # ── 能力开关 ──
    def set_feature_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        if not enabled:
            self.setVisible(False)
            self._tab_groups = []
            self.groups_selected.emit(None)

    def feature_enabled(self) -> bool:
        return self._enabled

    # ── 查询 ──
    def labels(self) -> list[str]:
        """当前页签标题（未显示时为空；用 ``isHidden`` 以兼容离屏/祖先隐藏）。"""
        if self.isHidden():
            return []
        return [self.tabText(i) for i in range(self.count())]

    def groups_at(self, idx: int) -> Optional[set]:
        if 0 < idx < len(self._tab_groups):
            return self._tab_groups[idx]
        return None

    # ── 重建 / 刷新 ──
    def rebuild(self, groups: Iterable[str], title_of: Callable[[str], str]) -> None:
        """按组名重建页签，并按"之前选中的组集合"保位（A04：不按下标保位）。"""
        groups = list(groups)
        prev = self.currentIndex()
        prev_groups = set(self._tab_groups[prev] or ()) if 0 < prev < len(self._tab_groups) else None
        self.blockSignals(True)
        while self.count():
            self.removeTab(0)
        self._tab_groups = [None]
        self.addTab(ALL_GROUPS_TITLE)
        for g in groups:
            self.addTab(title_of(g))
            self._tab_groups.append({g})
        self.blockSignals(False)
        show = self._enabled and len(groups) >= 2
        self.setVisible(show)
        if not show:
            self.groups_selected.emit(None)
            return
        idx = 0
        if prev_groups:
            for i, gs in enumerate(self._tab_groups):
                if gs and set(gs) == prev_groups:
                    idx = i
                    break
        idx = min(max(idx, 0), self.count() - 1)
        self.setCurrentIndex(idx)
        self._emit(idx)

    def refresh_titles(self, title_of: Callable[[str], str]) -> None:
        """染料命名变化后只刷新标题（不重建、不动选中）。"""
        if self.isHidden():
            return
        self.blockSignals(True)
        try:
            for i in range(1, self.count()):
                gs = self.groups_at(i)
                if gs:
                    self.setTabText(i, title_of(next(iter(gs))))
        finally:
            self.blockSignals(False)

    def reapply(self) -> None:
        """整批换图后重放当前过滤（页签可见时）。"""
        if not self.isHidden():
            self._emit(self.currentIndex())

    def select_group(self, group: str) -> bool:
        """切到包含 ``group`` 的页签；找不到返回 False。"""
        if not self._enabled or self.isHidden():
            return False
        for i, gs in enumerate(self._tab_groups):
            if gs and group in gs:
                self.setCurrentIndex(i)
                return True
        return False

    def _emit(self, idx: int) -> None:
        if self._enabled:
            self.groups_selected.emit(self.groups_at(idx))


def figure_names(groups: set, scans: list) -> set:
    """这些组对应的图件名集合（命名与 ``plotting_jacs`` 同源，防漂移）。

    A03（V2.x 审查）："单组"只数**可出图的样品组**（不含已剔除/background/dark），
    与图 a 的命名口径一致；否则"1 样品组 + 1 背景组"时图 a 哪个页签都看不见。
    """
    try:
        from ui.prep.names import fig_a_names, stack_tag
        from core.render.plotting import _safe
    except Exception:                                  # noqa: BLE001
        return set()
    draw_groups = sorted({str(s.group) for s in (scans or [])
                          if not s.excluded
                          and str(getattr(s, "role", "")) not in ("background", "dark")})
    single = len(draw_groups) <= 1
    names: set = set()
    for g in groups:
        if single:
            names.update(fig_a_names([g]))
        else:
            names.add(f"fig_a_spectra_{_safe(g)}")
        names.add(f"fig_b_kinetics_{_safe(g)}")
        names.add(f"jacs_stack_{stack_tag(g)}")
    return names
