# -*- coding: utf-8 -*-
"""文件清单组件（FR-1.1/1.2、FR-5、FR-11.5、FR-12）。

共享给所有页面。坏文件（``excluded`` 非空）自动置灰、禁用勾选、悬浮提示原因。

V1.1 新增（预分析数据整定，设计文档 D5）：

* **染料列**（可编辑，组级属性）——改一行的染料名 = 改整组；
* **时间列**（可编辑，行级）——改的是"生效时间"，文件名解析原值仍保留在提示里；
* 两个编辑入口与 ``DyeTimePanel`` 等价（FR-11.5 / FR-12.2）；
* 非法输入（负值/非数）**拒绝写入**并提示（FR-12.3 输入层校验）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from core.io import Scan
from ui.widgets.time_units import format_time_value, normalize_unit, parse_time_input

#: 角色 → 中文（复用 legacy ``run_menu.ROLE_ZH``）
ROLE_ZH = {"sample": "样品", "reference": "参比", "background": "背景",
           "dark": "暗对照", "n2": "N2", "quencher": "淬灭剂", "blank": "空白"}

(COL_CHECK, COL_FILE, COL_GROUP, COL_DYE, COL_ROLE, COL_TIME, COL_STATUS,
 COL_REASON) = range(8)
HEADERS = ["参与", "文件名", "组别", "染料", "角色", "时间 (s)", "状态",
           "剔除原因 / 备注"]

#: V2.6 FR-40.1：可视图级排序的列 → 排序键（其余列点击不排序）
#: Q-E13：排序/检索只改**视图**，``_scans``/解析顺序/勾选路径集合都不动。
SORT_KEY_BY_COL = {COL_FILE: "file", COL_GROUP: "group",
                    COL_TIME: "time", COL_STATUS: "status"}
SORT_COL_BY_KEY = {v: k for k, v in SORT_KEY_BY_COL.items()}


class FileTable(QWidget):
    """带勾选列的文件清单表（染料 / 时间可直接编辑）。

    信号：
        ``selection_changed(int)`` —— 当前勾选数量；
        ``dye_changed(str, str)``  —— ``(组名, 新染料名)``，整组一起改；
        ``time_changed(str, float)`` —— ``(文件名, 新生效时间)``。
    """

    selection_changed = Signal(int)
    #: V2.3 §12（A25.2）：右键动作的反馈走状态栏（页面把它接到 ``status``）
    status_message = Signal(str)
    dye_changed = Signal(str, str)
    time_changed = Signal(str, float)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._scans: list[Scan] = []
        self._loading = False
        self._dye_of: dict[str, str] = {}      # 组名 → 染料名（会话覆盖）
        #: A 道 FR-23.4：当前组过滤（``None`` = 全部；只影响行可见性）
        self._group_filter: Optional[set] = None
        #: V2.6 FR-40.1：视图状态（检索/排序只影响显示，全部仅会话内存）
        self._view_order: list[int] = []      # 视图行 → _scans 下标（默认恒等）
        self._sort_key: Optional[str] = None  # None = 解析顺序（默认）
        self._sort_desc = False
        self._search_tokens: list[str] = []   # 空格分隔多关键词（AND）
        #: V2.9 FR-60.3：时间列显示单位（内部 Scan.t 恒为秒；切换只改显示）
        self._time_unit = "s"
        self._build()
        self._build_context_menu()

    # ── 构建 ──
    def _build(self) -> None:
        self.table = QTableWidget(0, len(HEADERS), self)
        self.table.setHorizontalHeaderLabels(HEADERS)
        # 只有染料/时间两列可编辑（其余只读），编辑在 _on_item_changed 里校验
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked
                                   | QAbstractItemView.EditKeyPressed)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        for c in (COL_GROUP, COL_DYE, COL_ROLE, COL_TIME, COL_STATUS):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_REASON, QHeaderView.Interactive)
        self.table.setColumnWidth(COL_REASON, 220)
        self.table.itemChanged.connect(self._on_item_changed)
        # V2.6 FR-40.1：表头点击 = **视图级**排序（不启用 QTableWidget 内置排序，
        # 否则行号与 _scans 下标脱钩——勾选/解析顺序会被物理重排，违反 Q-E13）。
        hdr.setSectionsClickable(True)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.setSortIndicatorShown(False)
        self.table.setSortingEnabled(False)
        self.table.installEventFilter(self)       # 空格勾选 / Ctrl+A 视图全选
        self.table.setToolTip(
            "勾选参与分析的文件；灰色行 = 坏文件，已自动剔除。\n"
            "「染料」双击可改（**整组一起改**，决定输出目录与图内标注）；\n"
            "「时间 (s)」双击可改（只改这一行；鼠标悬停可看到文件名里的原值）。")

        self.lbl = QLabel("尚未载入数据")
        self.btn_all = QPushButton("全选")
        self.btn_none = QPushButton("全不选")
        self.btn_ok = QPushButton("仅选可用")
        self.btn_all.setToolTip("勾选所有可用文件")
        self.btn_none.setToolTip("取消勾选全部文件")
        self.btn_ok.setToolTip("只勾选可用文件（坏文件本来就无法勾选）")
        for b, fn in ((self.btn_all, lambda: self.set_all(True)),
                      (self.btn_none, lambda: self.set_all(False)),
                      (self.btn_ok, self._select_ok)):
            b.clicked.connect(fn)
            b.setMaximumWidth(90)

        top = QHBoxLayout()
        top.addWidget(self.lbl, 1)
        top.addWidget(self.btn_ok)
        top.addWidget(self.btn_all)
        top.addWidget(self.btn_none)
        # V2.6 FR-40.1：表上方检索框（文件名/组别/染料；空格多关键词 = AND；
        # 与组页签过滤叠加）。状态仅本会话内存，零新配置键。
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText(
            "检索：文件名 / 组别 / 染料（空格分隔多关键词；清空恢复全部）")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setToolTip(
            "与组页签过滤**叠加**（AND）；匹配文件名/组别/染料，大小写不敏感。\n"
            "只影响表格显示，不改勾选与解析顺序（仅本会话记忆）。")
        self.ed_search.textChanged.connect(self._on_search_changed)
        self.lbl_search = QLabel("")
        self.lbl_search.setStyleSheet("color: #6b7280; font-size: 11px;")
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("检索"))
        search_row.addWidget(self.ed_search, 1)
        search_row.addWidget(self.lbl_search)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addLayout(search_row)
        lay.addWidget(self.table)

    # ── 载入 ──
    def set_time_unit(self, unit: str) -> None:
        """V2.9 FR-60.3：切换时间列显示单位（只改显示，不改 Scan.t）。"""
        u = normalize_unit(unit)
        if u == self._time_unit:
            return
        self._time_unit = u
        h = self.table.horizontalHeaderItem(COL_TIME)
        if h is not None:
            h.setText(f"时间 ({u})")
        self._rebuild_rows()

    def set_scans(self, scans: Sequence[Scan],
                  dye_of: Optional[dict] = None) -> None:
        """填充表格（**解析顺序** = 传入顺序；视图按当前检索/排序重排显示）。

        Args:
            scans: ``core.io.Scan`` 列表（按数据目录顺序）。
            dye_of: ``{组名: 染料名}`` 会话覆盖（FR-11.5）；缺省时按组名启发式
                显示，并标注"（自动）"。

        V2.6 FR-40.1/Q-E13：``_scans`` 永远保持解析顺序；排序/检索只重排/隐藏
        **视图行**（``_view_order`` 映射）。A01 语义保留：按**路径**沿用旧勾选。
        """
        if dye_of is not None:
            self._dye_of = dict(dye_of)
        # A01（V2.x 审查）：按**路径**记住旧勾选；新文件仍按"可用即勾选"的默认。
        prev_checked = self._view_checks()
        self._scans = list(scans)
        self._rebuild_rows(prev_checked)
        self._update_label()
        self.selection_changed.emit(self.count_selected())

    # ── V2.6 FR-40.1：视图层（视图行 → _scans 下标映射的唯一入口） ──
    def _iter_view_index(self):
        """遍历 ``(视图行, _scans 下标)``；防御式跳过越界项。"""
        for vr, di in enumerate(self._view_order):
            if vr >= self.table.rowCount():
                break
            if 0 <= di < len(self._scans):
                yield vr, di

    def _view_checks(self) -> dict:
        """当前视图里的勾选态 ``{路径: 是否勾选}``（重建视图前保存）。"""
        out: dict = {}
        for vr, di in self._iter_view_index():
            it = self.table.item(vr, COL_CHECK)
            if it is not None:
                out[str(self._scans[di].path)] = (it.checkState() == Qt.Checked)
        return out

    def _current_data_index(self) -> Optional[int]:
        row = self.table.currentRow()
        if 0 <= row < len(self._view_order):
            return self._view_order[row]
        return None

    def _sorted_indices(self) -> list[int]:
        """按当前排序键生成视图顺序（稳定排序：同值保持解析顺序）。"""
        idx = list(range(len(self._scans)))
        key = self._sort_key
        if not key:
            return idx

        def sort_key(di: int):
            s = self._scans[di]
            name = str(s.path.name).lower()
            if key == "file":
                return (name,)
            if key == "group":
                return (str(s.group).lower(), name)
            if key == "time":
                return (float(s.t), name)
            if key == "status":
                return (0 if not s.excluded else 1, name)
            return (name,)

        return sorted(idx, key=sort_key, reverse=self._sort_desc)

    def _rebuild_rows(self, prev_checked: Optional[dict] = None) -> None:
        """按当前视图顺序重建表格行（``_scans`` 与勾选按路径保持）。"""
        cur = self._current_data_index()
        cur_col = max(0, self.table.currentColumn())
        if prev_checked is None:
            prev_checked = self._view_checks()
        order = self._sorted_indices()
        self._loading = True
        try:
            self._view_order = order
            self.table.setRowCount(len(order))
            for vr, di in enumerate(order):
                self._fill_row(vr, self._scans[di], prev_checked)
        finally:
            self._loading = False
        self._apply_visibility()
        if cur is not None and cur in self._view_order:
            self.table.setCurrentCell(self._view_order.index(cur), cur_col)

    def _fill_row(self, row: int, s: Scan, prev_checked: dict) -> None:
        """把一支谱渲染到**视图行** ``row``（按路径沿用旧勾选）。"""
        chk = QTableWidgetItem()
        good = not s.excluded
        chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                     if good else Qt.ItemIsEnabled)
        was = prev_checked.get(str(s.path))
        on = good if was is None else bool(was and good)
        chk.setCheckState(Qt.Checked if on else Qt.Unchecked)
        if not good:
            chk.setToolTip(f"已剔除：{s.excluded}")
        self.table.setItem(row, COL_CHECK, chk)

        edited = bool(getattr(s, "t_edited", False))
        t_src = float(getattr(s, "t_original", s.t))
        blank = bool(getattr(s, "time_blank", False))
        time_text = "" if blank else format_time_value(s.t, self._time_unit)
        status_text = "OK" if good else "剔除"
        if blank:
            status_text = "时间未定"
        no_time_suffix = good and not bool(getattr(s, "time_suffix", True))
        if no_time_suffix:
            status_text = "文件名未含时间（按 0 s）"
        vals = [s.path.name, s.group, self._dye_text(s.group),
                ROLE_ZH.get(s.role, s.role), time_text, status_text,
                s.excluded or ""]
        for c, text in zip(range(COL_FILE, COL_REASON + 1), vals):
            it = QTableWidgetItem(text)
            if c in (COL_DYE, COL_TIME) and good:
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                            | Qt.ItemIsEditable)
            if not good:
                it.setForeground(QBrush(QColor(150, 150, 150)))
                it.setToolTip(f"已剔除：{s.excluded}")
            elif c == COL_STATUS:
                it.setForeground(QBrush(QColor(30, 120, 60)) if not no_time_suffix
                                 else QBrush(QColor(180, 100, 20)))
                if no_time_suffix:
                    it.setToolTip(
                        "文件名未含时间（按 0 s）——这不是错误：可在右侧时间工作表"
                        "直接填写，或按「文件命名建议」命名以自动识别。")
                elif blank:
                    it.setToolTip("时间未定：在时间工作表填写后自动恢复参与下游")
            elif c == COL_DYE:
                it.setToolTip(
                    "染料名（整组属性）：改这里会同时改同组所有文件，"
                    "并决定输出目录 runs/<染料>/ 与图内标注。\n"
                    "在「整定」面板里勾选「记住」可写入 dyes.yaml 长期生效。")
            elif c == COL_TIME:
                tip = (f"生效时间 = {format_time_value(s.t, self._time_unit)} {self._time_unit}"
                       f"（{float(s.t):g} s）；文件名解析值 = {t_src:g} s")
                if edited:
                    tip += "\n（已被手动整定，双击可再改；" \
                           "在「整定」面板可一键恢复）"
                    it.setForeground(QBrush(QColor(20, 20, 20)))
                    fnt = QFont()
                    fnt.setBold(True)
                    it.setFont(fnt)
                it.setToolTip(tip)
            self.table.setItem(row, c, it)

    def _dye_text(self, group: str) -> str:
        """染料列显示文本：会话覆盖 → ``自动: X``。"""
        if group in self._dye_of and str(self._dye_of[group]).strip():
            return str(self._dye_of[group])
        try:
            from core.dyes import auto_dye_name
            return f"{auto_dye_name(group)}（自动）"
        except Exception:                                  # noqa: BLE001
            return group

    # ── 查询 / 操作 ──
    def scans(self) -> list[Scan]:
        """当前清单（按表格顺序）。"""
        return list(self._scans)

    def checked_paths(self) -> list[Path]:
        """勾选且可用的文件路径（**解析顺序**；传给 ``collect_scans(only_paths=...)``）。

        Q-E13：勾选按路径记，排序/检索（视图）不改变这里的顺序。
        """
        picked: set[int] = set()
        for vr, di in self._iter_view_index():
            it = self.table.item(vr, COL_CHECK)
            s = self._scans[di]
            if it is not None and it.checkState() == Qt.Checked and not s.excluded:
                picked.add(di)
        return [self._scans[di].path for di in range(len(self._scans))
                if di in picked]

    def count_selected(self) -> int:
        """勾选且可用的文件数。"""
        return len(self.checked_paths())

    # ── A 道 FR-23.4：组过滤（只影响显示，不改勾选/数据） ──
    def set_group_filter(self, groups: Optional[set] = None) -> None:
        """按组名过滤**行可见性**（``None`` = 显示全部）。

        只隐藏行，**不重建、不改勾选**：``_scans`` / ``_dye_of`` / 勾选 state
        原样保留，``checked_paths()`` 与统计口径都不受影响（过滤是纯视图）。
        """
        self._group_filter = None if groups is None else {str(g) for g in groups}
        self._apply_group_filter()

    def _apply_group_filter(self) -> None:
        """兼容旧名：等价于按当前（组过滤 + 检索）刷新可见性。"""
        self._apply_visibility()

    def _apply_visibility(self) -> None:
        """按**组过滤 + 检索**刷新行可见性（纯视图；不改勾选/数据）。"""
        tokens = self._search_tokens
        keep = self._group_filter
        shown = 0
        for vr, di in self._iter_view_index():
            s = self._scans[di]
            hidden = False
            if keep is not None and s.group not in keep:
                hidden = True
            elif tokens and not self._matches(s, tokens):
                hidden = True
            self.table.setRowHidden(vr, hidden)
            if not hidden:
                shown += 1
        if tokens:
            self.lbl_search.setText(f"命中 {shown}/{len(self._scans)}")
        elif keep is not None:
            self.lbl_search.setText(f"组过滤 {shown}/{len(self._scans)}")
        else:
            self.lbl_search.setText("")

    def group_filter(self) -> Optional[set]:
        """当前组过滤（``None`` = 全部；测试/调试用）。"""
        return None if self._group_filter is None else set(self._group_filter)

    # ── V2.6 FR-40.1：检索（文件名/组别/染料；空格 = AND；与组过滤叠加） ──
    def _scan_haystack(self, s: Scan) -> str:
        return " ".join([str(s.path.name), str(s.group),
                         self._dye_text(s.group)]).lower()

    def _matches(self, s: Scan, tokens: list) -> bool:
        hay = self._scan_haystack(s)
        return all(t in hay for t in tokens)

    def _on_search_changed(self, text: str) -> None:
        self._search_tokens = [t for t in str(text or "").lower().split() if t]
        self._apply_visibility()

    def search_text(self) -> str:
        """当前检索词（原样；测试/调试用）。"""
        return self.ed_search.text()

    def set_search_text(self, text: str) -> None:
        """程序化设置检索词（等价用户在框里输入；空串恢复全部）。"""
        self.ed_search.setText(str(text or ""))

    # ── V2.6 FR-40.1：表头点击 = 视图级排序（解析序/勾选不变，Q-E13） ──
    def sort_by(self, key: str, descending: bool = False) -> None:
        """按 ``file/group/time/status`` 排序**视图**（解析顺序 ``_scans`` 不变）。"""
        key = str(key or "").strip().lower()
        if key not in SORT_COL_BY_KEY:
            raise ValueError(f"不支持的排序键：{key!r}（file/group/time/status）")
        prev = self._view_checks()
        self._sort_key, self._sort_desc = key, bool(descending)
        self._apply_sort_indicator()
        self._rebuild_rows(prev)
        self._update_label()

    def sort_state(self):
        """当前排序 ``("file", False)`` 等；未排序返回 ``None``。"""
        if not self._sort_key:
            return None
        return (self._sort_key, bool(self._sort_desc))

    def _on_header_clicked(self, col: int) -> None:
        key = SORT_KEY_BY_COL.get(int(col))
        if not key:
            return
        descending = (self._sort_key == key and not self._sort_desc)
        self.sort_by(key, descending=descending)
        self.status_message.emit(
            f"视图按「{HEADERS[col]}」{'降序' if descending else '升序'}排序"
            "（解析顺序 / 勾选不变）")

    def _apply_sort_indicator(self) -> None:
        hdr = self.table.horizontalHeader()
        if not self._sort_key:
            hdr.setSortIndicatorShown(False)
            return
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(
            SORT_COL_BY_KEY[self._sort_key],
            Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder)

    # ── 视图查询（测试/调试用） ──
    def view_order(self) -> list[int]:
        """视图行 → ``_scans`` 下标（默认恒等；排序后为排序结果）。"""
        return list(self._view_order)

    def visible_scans(self) -> list[Scan]:
        """当前视图顺序下**未被隐藏**的谱（组过滤 + 检索之后）。"""
        out: list[Scan] = []
        for vr, di in self._iter_view_index():
            if not self.table.isRowHidden(vr):
                out.append(self._scans[di])
        return out

    def visible_count(self) -> int:
        """当前视图可见行数。"""
        return len(self.visible_scans())

    # ── V2.6 FR-40.1：键盘（空格勾选 / Ctrl+A 视图全选；↑↓ 走 Qt 默认） ──
    def eventFilter(self, obj, event):                # noqa: N802 —— Qt 约定
        if obj is self.table and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if (key == Qt.Key_Space
                    and self.table.state() != QAbstractItemView.EditingState):
                self.toggle_current_check()
                return True
            if (key == Qt.Key_A
                    and bool(event.modifiers() & Qt.ControlModifier)):
                self.check_visible_all(True)
                return True
        return super().eventFilter(obj, event)

    def toggle_current_check(self) -> bool:
        """空格：切换当前行勾选（坏文件不可勾选；返回是否发生变化）。"""
        row = self.table.currentRow()
        if not (0 <= row < len(self._view_order)):
            return False
        s = self._scans[self._view_order[row]]
        if s.excluded:
            self.status_message.emit(f"坏文件不可勾选：{s.path.name}（{s.excluded}）")
            return False
        it = self.table.item(row, COL_CHECK)
        if it is None:
            return False
        on = it.checkState() != Qt.Checked
        self.table.itemChanged.disconnect(self._on_item_changed)
        try:
            it.setCheckState(Qt.Checked if on else Qt.Unchecked)
        finally:
            self.table.itemChanged.connect(self._on_item_changed)
        self._update_label()
        self.selection_changed.emit(self.count_selected())
        self.status_message.emit(
            f"已{'勾选' if on else '取消勾选'}：{s.path.name}")
        return True

    def check_visible_all(self, flag: bool = True) -> int:
        """Ctrl+A：勾选/取消勾选**当前视图**内全部可用文件（返回受影响行数）。"""
        n = 0
        self._loading = True
        try:
            for vr, di in self._iter_view_index():
                if self.table.isRowHidden(vr):
                    continue
                s = self._scans[di]
                if s.excluded:
                    continue
                it = self.table.item(vr, COL_CHECK)
                if it is None:
                    continue
                it.setCheckState(Qt.Checked if flag else Qt.Unchecked)
                n += 1
        finally:
            self._loading = False
        self._update_label()
        self.selection_changed.emit(self.count_selected())
        self.status_message.emit(
            f"已{'勾选' if flag else '取消勾选'}当前视图 {n} 个文件")
        return n
    def dye_map(self) -> dict:
        """当前会话的 ``{组名: 染料名}``（只含被手工指定过的组）。"""
        return dict(self._dye_of)

    def set_dye(self, group: str, dye: str) -> None:
        """外部（整定面板）设定某组染料名并刷新显示。"""
        self._dye_of[str(group)] = str(dye)
        self.refresh_dye_column()

    def refresh_dye_column(self) -> None:
        """只刷新染料列文本（不重建整表；按当前视图顺序）。"""
        self._loading = True
        try:
            for vr, di in self._iter_view_index():
                it = self.table.item(vr, COL_DYE)
                if it is not None:
                    it.setText(self._dye_text(self._scans[di].group))
        finally:
            self._loading = False

    # ── A25.2（V2.3 §12）：右键菜单 ──
    def _build_context_menu(self) -> None:
        """文件表右键：复制文件路径 / 在资源管理器中显示 / 查看剔除原因。"""
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)


    def _row_context_menu(self, s):                          # noqa: ANN001
        """按行构建右键菜单（抽出来：离屏测试直接检查动作，不弹模态 loop）。"""
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_copy = menu.addAction("复制文件路径")
        act_copy.triggered.connect(lambda *_, p=s.path: self._copy_path(p))
        act_reveal = menu.addAction("在资源管理器中显示")
        act_reveal.triggered.connect(lambda *_, p=s.path: self._reveal(p))
        menu.addSeparator()
        # A27（审查附录 C）：行级"勾选/取消本组"（FR-25.2 当初补了两项，这是缺的
        # 第三项——按行快速切换整组参与状态，不必先切组页签）。
        grp = str(getattr(s, "group", "") or "")
        act_on = menu.addAction(f"勾选本组「{grp}」" if grp else "勾选本组")
        act_on.setEnabled(bool(grp))
        act_on.triggered.connect(
            lambda *_, g=grp: self.check_group(g, True))
        act_off = menu.addAction(f"取消勾选本组「{grp}」" if grp else "取消勾选本组")
        act_off.setEnabled(bool(grp))
        act_off.triggered.connect(
            lambda *_, g=grp: self.check_group(g, False))
        menu.addSeparator()
        act_why = menu.addAction("查看剔除原因")
        act_why.setEnabled(bool(s.excluded))
        act_why.triggered.connect(lambda *_, t=str(s.excluded): self.status_message.emit(
            f"剔除原因：{t}" if t else "该文件未被剔除"))
        return menu

    def _show_context_menu(self, pos) -> None:              # noqa: ANN001
        row = self.table.rowAt(pos.y())
        if not (0 <= row < len(self._view_order)):
            return
        di = self._view_order[row]
        if not (0 <= di < len(self._scans)):
            return
        menu = self._row_context_menu(self._scans[di])
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def check_group(self, group: str, on: bool = True) -> int:
        """按组勾选/取消勾选（A27；返回受影响的行数）。

        与 :meth:`check_only_group` 的区别：这个**只动本组**、不影响其它组的勾选
        （"勾选本组"= 把本组全勾上；"取消勾选本组"= 把本组全取消）。
        """
        g = str(group)
        self._loading = True
        n = 0
        try:
            for vr, di in self._iter_view_index():
                s = self._scans[di]
                if str(getattr(s, "group", "")) != g or s.excluded:
                    continue
                it = self.table.item(vr, COL_CHECK)
                if it is None:
                    continue
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                n += 1
        finally:
            self._loading = False
        self._update_label()
        self.selection_changed.emit(self.count_selected())
        self.status_message.emit(
            f"已{'勾选' if on else '取消勾选'}组「{g}」：{n} 个文件")
        return n

    def _copy_path(self, p: Path) -> None:
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(str(p))
            self.status_message.emit(f"已复制路径：{p}")
        except Exception as err:                            # noqa: BLE001
            self.status_message.emit(f"复制失败：{err}")

    def _reveal(self, p: Path) -> None:
        try:
            import subprocess
            import sys as _sys
            if _sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(Path(p))])
            else:                                           # pragma: no cover
                subprocess.Popen(["xdg-open", str(Path(p).parent)])
            self.status_message.emit(f"已在资源管理器中定位：{Path(p).name}")
        except Exception as err:                            # noqa: BLE001
            self.status_message.emit(f"无法打开资源管理器：{err}")

    # ── A25.3（V2.3 §12）：组级勾选（不改过滤、不动别的组） ──
    def check_only_group(self, group: str) -> int:
        """只勾选某组（其余取消勾选）；返回勾上的文件数。

        与"只分析本组"的区别：这个**改全局勾选状态**（用户可见的选择），
        ``phi_page.analyze_group`` 则不动勾选、直接按组跑一次。两者都在
        FR-25.3 的右键菜单里。
        """
        g = str(group)
        self._loading = True
        n = 0
        try:
            for vr, di in self._iter_view_index():
                s = self._scans[di]
                it = self.table.item(vr, COL_CHECK)
                if it is None:
                    continue
                on = (s.group == g) and (not s.excluded)
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)
                n += 1 if on else 0
        finally:
            self._loading = False
        self._update_label()
        self.selection_changed.emit(self.count_selected())
        self.status_message.emit(f"已只勾选组「{g}」：{n} 个文件")
        return n

    def set_all(self, flag: bool) -> None:
        """全选 / 全不选（坏文件始终不可勾选）。"""
        self._loading = True
        try:
            for vr, di in self._iter_view_index():
                s = self._scans[di]
                it = self.table.item(vr, COL_CHECK)
                if it is None or s.excluded:
                    continue
                it.setCheckState(Qt.Checked if flag else Qt.Unchecked)
        finally:
            self._loading = False
        self._update_label()
        self.selection_changed.emit(self.count_selected())

    def _select_ok(self) -> None:
        self.set_all(True)

    # ── 编辑校验（FR-12.3 输入层：非法值根本进不了表） ──
    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        col, row = item.column(), item.row()
        if not (0 <= row < len(self._view_order)):
            return
        di = self._view_order[row]
        if not (0 <= di < len(self._scans)):
            return
        s = self._scans[di]
        if col == COL_CHECK:
            self._update_label()
            self.selection_changed.emit(self.count_selected())
            return
        if col == COL_DYE:
            text = item.text().strip()
            if not text:
                self._revert(item, self._dye_text(s.group),
                             "染料名不能为空（要恢复自动命名请用「整定」面板）")
                return
            group = s.group
            self._dye_of[group] = text
            self._loading = True
            try:                                  # 同组所有行一起改（视图行）
                for vr2, di2 in self._iter_view_index():
                    if self._scans[di2].group == group:
                        it2 = self.table.item(vr2, COL_DYE)
                        if it2 is not None:
                            it2.setText(text)
            finally:
                self._loading = False
            self.dye_changed.emit(group, text)
            self._update_label()
            return
        if col == COL_TIME:
            text = item.text().strip()
            val = parse_time_input(text)
            if val is None:
                self._revert(item, f"{format_time_value(s.t, self._time_unit)}",
                             f"「{text}」不是合法时间——可输入秒数（如 300），"
                             "或带单位（5min / 1.5h）")
                return
            if s.t_src is None:
                s.t_src = float(s.t)
            s.t = float(val)
            self._loading = True
            try:
                it = self.table.item(row, COL_TIME)
                if it is not None:
                    it.setText(format_time_value(val, self._time_unit))
                    f = QFont()
                    f.setBold(True)
                    it.setFont(f)
                    it.setForeground(QBrush(QColor(20, 20, 20)))
                    it.setToolTip(f"生效时间 = {val:g} s；"
                                  f"文件名解析值 = {float(s.t_src):g} s\n"
                                  "（已被手动整定）")
                ri = self.table.item(row, COL_REASON)
                if ri is not None and not ri.text():
                    ri.setText("时间已手动整定")
            finally:
                self._loading = False
            self.time_changed.emit(s.path.name, float(val))
            self._update_label()

    def _revert(self, item: QTableWidgetItem, text: str, why: str) -> None:
        """把非法编辑还原并给出中文提示。"""
        self._loading = True
        try:
            item.setText(text)
        finally:
            self._loading = False
        QMessageBox.warning(self, "输入无效", why)

    def _update_label(self) -> None:
        n_ok = sum(1 for s in self._scans if not s.excluded)
        n_bad = len(self._scans) - n_ok
        groups = len({s.group for s in self._scans if not s.excluded})
        n_edit = sum(1 for s in self._scans if getattr(s, "t_edited", False))
        extra = f" · 时间已整定 {n_edit}" if n_edit else ""
        self.lbl.setText(
            f"共 {len(self._scans)} 个文件 · 可用 {n_ok} · 剔除 {n_bad} · "
            f"{groups} 个组 · 已勾选 {self.count_selected()}{extra}")
