# -*- coding: utf-8 -*-
"""Φ_Δ 工作台左栏的「批次 / 组 / 文件」树（只读导航）。

取代旧的 :class:`GroupTabs` 页签条：选中「全部组（对比）」= 不过滤，选中某组（或
组下的某个文件）= 只看这一组，选中变化时发出 ``groups_selected(None | set[str])``，
过滤由页面应用到文件表与画布（本控件不碰它们）。

每个文件一行：时间标签 + 状态词。出结果前状态来自 Scan 本身（可用 / 已剔除 /
QC 原因），``set_result(rr)`` 之后改为按拟合口径标注（参与拟合 / 截断 / 参比 /
已剔除）。可编辑的完整文件表仍在页面别处，这里只读、只导航。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QTreeWidget,
                               QTreeWidgetItem, QWidget)

from ui import theme
from ui.widgets.time_units import format_time_value


#: 除"首个样品组"（accent）与参比组（series_ref）之外的组按序轮换这几色；
#: 都取中等明度，浅色白底与深色底上都读得清，且避开 accent 的青绿与参比的橙
GROUP_COLORS = ("#5b7fd6", "#a463c8", "#c58b1c", "#d0587e", "#7a8a52", "#5f9ea8")

#: 状态词 → 前景色令牌（主题切换时按令牌重取颜色）
STATUS_TOKEN = {
    "可用": "success", "时间未定": "warn", "已剔除": "text_subtle",
    "参与拟合": "success", "截断": "warn", "参比": "series_ref",
    "拟合失败": "danger", "未拟合": "text_subtle",
}
#: QC 原因原文在状态列里最多显示这么多字，全文放在悬浮提示里
QC_TEXT_MAX = 8


ALL_GROUPS_TITLE = "全部组（对比）"

_KIND, _GROUP, _PATH, _TOKENS = (Qt.UserRole, Qt.UserRole + 1, Qt.UserRole + 2,
                                 Qt.UserRole + 3)
KIND_ALL, KIND_GROUP, KIND_FILE = "all", "group", "file"
_COL_TEXT, _COL_SIDE = 0, 1
_DOT = 10


@theme.register_qss
def _qss(p: dict) -> str:
    return f"""
QTreeWidget#batchTree {{ background: {p['surface']}; border: none; outline: none; }}
QTreeWidget#batchTree::item {{ height: 22px; padding: 0 2px; border: none; }}
QTreeWidget#batchTree::item:hover {{ background: {p['surface_alt']}; }}
QTreeWidget#batchTree::item:selected {{ background: {p['accent_soft']}; }}
"""


def _mono_font(base: QFont) -> QFont:
    f = QFont(base)
    f.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono"])
    f.setStyleHint(QFont.Monospace)
    return f


def _time_label(s) -> str:
    """与 FileTable 时间列同源：生效时间 ``s.t``（秒）；``time_blank`` 时显示 —。"""
    if bool(getattr(s, "time_blank", False)):
        return "—"
    text = f"{format_time_value(s.t, 's')} s"
    rep = str(getattr(s, "rep", "") or "")
    return f"{text} · {rep}" if rep else text


class BatchTree(QTreeWidget):
    """「全部组（对比）」+ 每组一个节点（色点 · 标题 · 文件数）+ 组下每个文件一行。"""

    #: ``None`` = 全部组；否则为选中组名的集合
    groups_selected = Signal(object)
    #: 双击文件行 → 文件名（``Path.name``）
    file_activated = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("batchTree")
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setRootIsDecorated(True)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setIconSize(QSize(_DOT, _DOT))
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setTextElideMode(Qt.ElideMiddle)
        hdr = self.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(_COL_TEXT, QHeaderView.Stretch)
        hdr.setSectionResizeMode(_COL_SIDE, QHeaderView.ResizeToContents)
        self._mono = _mono_font(self.font())

        self._scans: list = []
        self._title_of: Callable[[str], str] = str
        self._reference = ""
        self._excluded: set[str] = set()
        self._groups: list[str] = []
        self._colors: dict[str, str] = {}      # 组名 → 颜色（令牌名或 #hex）
        self._group_items: dict[str, QTreeWidgetItem] = {}
        self._rr = None
        self._last_emitted: object = ...       # 哨兵：尚未发过
        self._recoloring = False

        self._all_item = QTreeWidgetItem([ALL_GROUPS_TITLE, ""])
        self._all_item.setData(0, _KIND, KIND_ALL)
        self.addTopLevelItem(self._all_item)
        self.setCurrentItem(self._all_item)

        self.itemSelectionChanged.connect(self._on_selection_changed)
        self.itemDoubleClicked.connect(self._on_double_clicked)

    # ══════════════ 公开 API ══════════════
    def set_scans(self, scans: list, title_of: Callable[[str], str], *,
                  reference: str = "", excluded: Iterable[str] = ()) -> None:
        """按 scans 重建整棵树。

        ``reference`` 为显式参比组（空 = 按 Scan.role 判断）；``excluded`` 是用户
        取消勾选的文件（路径字符串或文件名均可），显示为"已剔除"。
        组集合不变时沿用之前的选中项（文件 → 按路径，组 → 按组名）与展开状态；
        重建后总会发一次 ``groups_selected``（同 GroupTabs.rebuild，页面据此重放过滤）。
        """
        prev_groups = list(self._groups)
        prev_sel = self._selection_key()
        prev_expanded = {g: it.isExpanded() for g, it in self._group_items.items()}

        self._scans = list(scans or [])
        self._title_of = title_of or str
        self._reference = str(reference or "")
        self._excluded = {str(x) for x in (excluded or ())}
        self._groups = sorted({str(s.group) for s in self._scans})
        self._colors = self._assign_colors()

        same = self._groups == prev_groups
        sel = prev_sel if same else (KIND_ALL, None)
        self.blockSignals(True)
        try:
            self._rebuild(sel, prev_expanded if same else {})
        finally:
            self.blockSignals(False)
        self._emit(force=True)

    def set_result(self, rr) -> None:
        """按一次运行结果更新每个文件的状态词；``rr=None`` 回到出结果前的口径。"""
        self._rr = rr
        self._refresh_status()

    def select_group(self, group: Optional[str]) -> None:
        """程序化选中某组（``None`` = 全部组），展开该组并发出 ``groups_selected``。"""
        it = self._group_items.get(str(group)) if group is not None else None
        target = it if it is not None else self._all_item
        self.blockSignals(True)
        try:
            self.setCurrentItem(target)
            target.setSelected(True)
            if it is not None:
                it.setExpanded(True)
        finally:
            self.blockSignals(False)
        self.scrollToItem(target)
        self._emit(force=True)

    def selected_groups(self) -> Optional[set]:
        items = self.selectedItems()
        if not items:
            return None
        it = items[0]
        g = it.data(0, _GROUP)
        return {str(g)} if it.data(0, _KIND) in (KIND_GROUP, KIND_FILE) and g else None

    def group_color(self, group: str) -> str:
        c = self._colors.get(str(group), GROUP_COLORS[0])
        return theme.hexc(c) if not c.startswith("#") else c

    def groups(self) -> list[str]:
        return list(self._groups)

    # ══════════════ 构建 ══════════════
    def _group_role(self, group: str) -> str:
        """组角色：显式参比优先（同 _apply_reference_group），否则看 Scan.role。"""
        if self._reference and group == self._reference:
            return "reference"
        roles = {str(getattr(s, "role", "sample")) for s in self._scans
                 if str(s.group) == group}
        for r in ("reference", "background", "dark"):
            if r in roles and not (r == "reference" and self._reference):
                return r
        return "sample"

    def _assign_colors(self) -> dict:
        out: dict = {}
        first_sample = True
        k = 0
        for g in self._groups:
            role = self._group_role(g)
            if role == "reference":
                out[g] = "series_ref"
            elif role == "sample" and first_sample:
                out[g] = "accent"
                first_sample = False
            else:
                out[g] = GROUP_COLORS[k % len(GROUP_COLORS)]
                k += 1
        return out

    def _rebuild(self, sel: tuple, expanded: dict) -> None:
        while self._all_item.childCount():
            self._all_item.takeChild(0)
        for i in range(self.topLevelItemCount() - 1, 0, -1):
            self.takeTopLevelItem(i)
        self._group_items = {}
        by_group: dict[str, list] = {g: [] for g in self._groups}
        for s in self._scans:
            by_group[str(s.group)].append(s)
        for g in self._groups:
            files = sorted(by_group[g], key=lambda s: (float(s.t), s.path.name))
            gi = QTreeWidgetItem([self._title_of(g), str(len(files))])
            gi.setData(0, _KIND, KIND_GROUP)
            gi.setData(0, _GROUP, g)
            gi.setFont(_COL_SIDE, self._mono)
            gi.setTextAlignment(_COL_SIDE, Qt.AlignRight | Qt.AlignVCenter)
            n_ok = sum(1 for s in files if not s.excluded)
            gi.setToolTip(_COL_TEXT, f"{g}（{len(files)} 个文件，可用 {n_ok} 个）")
            gi.setToolTip(_COL_SIDE, f"{len(files)} 个文件")
            for s in files:
                fi = QTreeWidgetItem([_time_label(s), ""])
                fi.setData(0, _KIND, KIND_FILE)
                fi.setData(0, _GROUP, g)
                fi.setData(0, _PATH, str(s.path))
                fi.setFont(_COL_TEXT, self._mono)
                fi.setTextAlignment(_COL_SIDE, Qt.AlignRight | Qt.AlignVCenter)
                gi.addChild(fi)
            self.addTopLevelItem(gi)
            self._group_items[g] = gi
        self._refresh_status()

        target = self._find(sel) or self._all_item
        self.setCurrentItem(target)
        target.setSelected(True)
        sel_group = target.data(0, _GROUP)
        for g, gi in self._group_items.items():
            if g in expanded:
                gi.setExpanded(expanded[g] or g == sel_group)
            else:
                gi.setExpanded(g == sel_group or gi.childCount() <= 12)

    def _find(self, sel: tuple) -> Optional[QTreeWidgetItem]:
        kind, key = sel
        if kind == KIND_GROUP:
            return self._group_items.get(key)
        if kind == KIND_FILE:
            for gi in self._group_items.values():
                for i in range(gi.childCount()):
                    if gi.child(i).data(0, _PATH) == key:
                        return gi.child(i)
        return None

    def _selection_key(self) -> tuple:
        items = self.selectedItems()
        if not items:
            return (KIND_ALL, None)
        it = items[0]
        kind = it.data(0, _KIND)
        if kind == KIND_GROUP:
            return (KIND_GROUP, it.data(0, _GROUP))
        if kind == KIND_FILE:
            return (KIND_FILE, it.data(0, _PATH))
        return (KIND_ALL, None)

    # ══════════════ 状态词 ══════════════
    def _is_unchecked(self, s) -> bool:
        return str(s.path) in self._excluded or s.path.name in self._excluded

    def _pre_status(self, s) -> tuple[str, str, str]:
        """出结果前：``(状态词, 颜色令牌, 提示)``。"""
        if s.excluded:
            reason = str(s.excluded)
            short = reason if len(reason) <= QC_TEXT_MAX else reason[:QC_TEXT_MAX] + "…"
            return short, "danger", f"已剔除：{reason}"
        if self._is_unchecked(s):
            return "已剔除", STATUS_TOKEN["已剔除"], "未勾选，不参与分析"
        if bool(getattr(s, "time_blank", False)):
            return "时间未定", STATUS_TOKEN["时间未定"], "时间未定：填写后才参与下游分析"
        return "可用", STATUS_TOKEN["可用"], ""

    def _result_statuses(self) -> dict:
        """出结果后：``{路径: (状态词, 提示)}``。

        结果里没有逐文件的"是否用于拟合"标记，这里按 core 的口径推算：
        ``_fit_all`` 把组内未剔除的文件（各重复合并）按横轴排序后交给
        ``fit_kinetics``；auto-truncate 只保留**最早**的 ``n`` 个点（FitResult.n）。
        所以有 ``pooled.x``（截断后实际用到的横轴）时取其最大值作截止，
        横轴晚于截止的文件 = 截断；没有 x 时退回按时间排名 ≥ n 判截断。
        R 超出拟合窗而被 window_mask 去掉的点不单独区分（仍记"参与拟合"）。
        """
        rr = self._rr
        results = getattr(rr, "results", None) or {}
        analyzed = {str(s.path): s for s in (getattr(rr, "scans", None) or [])}
        kin = ((getattr(rr, "cfg", None) or {}).get("kinetics") or {})
        dose = bool(kin.get("dose_normalize", False))
        out: dict = {}
        by_group: dict[str, list] = {}
        for s in self._scans:
            a = analyzed.get(str(s.path))
            if a is None or a.excluded or self._is_unchecked(s):
                why = f"已剔除：{a.excluded}" if a is not None and a.excluded \
                    else "本次未参与分析（未勾选或未载入）"
                out[str(s.path)] = ("已剔除", why)
                continue
            by_group.setdefault(str(a.group), []).append(a)
        for g, files in by_group.items():
            entry = results.get(g)
            pooled = entry.get("pooled") if isinstance(entry, dict) else None
            if entry is None:
                for a in files:
                    out[str(a.path)] = ("未拟合", "该组不参与拟合（背景组或无结果）")
                continue
            if pooled is None or getattr(pooled, "status", "") == "FAILED":
                msg = str(getattr(pooled, "message", "") or "拟合失败")
                for a in files:
                    out[str(a.path)] = ("拟合失败", msg)
                continue
            is_ref = (entry.get("role") == "reference"
                      or (self._reference and g == self._reference))
            files = sorted(files, key=lambda a: (float(a.t), a.path.name))
            cut = None
            if getattr(pooled, "truncated", False):
                x = getattr(pooled, "x", None)
                if x is not None and len(x):
                    I_g = float(entry.get("irradiance", 1.0) or 1.0)
                    cut = (float(max(x)), (I_g / 1000.0) if dose else 1.0)
            n = int(getattr(pooled, "n", 0) or 0)
            trunc_msg = str(getattr(pooled, "message", "") or "auto_truncate")
            for rank, a in enumerate(files):
                if cut is not None:
                    dropped = float(a.t) * cut[1] > cut[0] * (1 + 1e-9) + 1e-12
                else:
                    dropped = bool(getattr(pooled, "truncated", False)) and rank >= n
                if dropped:
                    out[str(a.path)] = ("截断", f"自动截断未用到此点（{trunc_msg}）")
                elif is_ref:
                    out[str(a.path)] = ("参比", "参比组，参与拟合")
                else:
                    out[str(a.path)] = ("参与拟合", "")
        return out

    def _refresh_status(self) -> None:
        post = self._result_statuses() if self._rr is not None else None
        by_path = {str(s.path): s for s in self._scans}
        for gi in self._group_items.values():
            for i in range(gi.childCount()):
                fi = gi.child(i)
                s = by_path.get(fi.data(0, _PATH))
                if s is None:
                    continue
                if post is not None:
                    word, tip = post.get(str(s.path), ("已剔除", ""))
                    token = STATUS_TOKEN.get(word, "text_muted")
                else:
                    word, token, tip = self._pre_status(s)
                fi.setText(_COL_SIDE, word)
                tip_full = s.path.name + (f"\n{tip}" if tip else "")
                fi.setToolTip(_COL_TEXT, tip_full)
                fi.setToolTip(_COL_SIDE, tip_full)
                time_token = "text_subtle" if word in ("已剔除", "未拟合") \
                    or s.excluded else "text"
                fi.setData(0, _TOKENS, (time_token, token))
        self._recolor()

    # ══════════════ 主题 ══════════════
    def _dot(self, color: str) -> QIcon:
        dpr = max(1.0, self.devicePixelRatioF())
        pm = QPixmap(int(_DOT * dpr), int(_DOT * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawEllipse(1, 1, _DOT - 2, _DOT - 2)
        p.end()
        return QIcon(pm)

    def _recolor(self) -> None:
        """按当前色板重设每个条目的前景色与组色点（填充时与主题切换后都走这里）。"""
        if self._recoloring:
            return
        self._recoloring = True
        try:
            self._all_item.setForeground(_COL_TEXT, QBrush(theme.color("text")))
            for g, gi in self._group_items.items():
                gi.setIcon(_COL_TEXT, self._dot(self.group_color(g)))
                gi.setForeground(_COL_TEXT, QBrush(theme.color("text")))
                gi.setForeground(_COL_SIDE, QBrush(theme.color("text_muted")))
                for i in range(gi.childCount()):
                    fi = gi.child(i)
                    tokens = fi.data(0, _TOKENS) or ("text", "text_muted")
                    fi.setForeground(_COL_TEXT, QBrush(theme.color(tokens[0])))
                    fi.setForeground(_COL_SIDE, QBrush(theme.color(tokens[1])))
        finally:
            self._recoloring = False

    def changeEvent(self, event) -> None:                 # noqa: N802 —— Qt 约定
        super().changeEvent(event)
        if event.type() in (QEvent.PaletteChange, QEvent.StyleChange):
            self._recolor()

    # ══════════════ 交互 ══════════════
    def _on_selection_changed(self) -> None:
        self._emit(force=False)

    def _emit(self, force: bool) -> None:
        """选中的组集合变了才发（同组内换文件不重复发）；``force`` 时总发。"""
        groups = self.selected_groups()
        if not force and groups == self._last_emitted:
            return
        self._last_emitted = groups
        self.groups_selected.emit(groups)

    def _on_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if item.data(0, _KIND) == KIND_FILE:
            self.file_activated.emit(Path(item.data(0, _PATH)).name)
