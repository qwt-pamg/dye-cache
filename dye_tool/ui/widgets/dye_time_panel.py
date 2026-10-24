# -*- coding: utf-8 -*-
"""预分析数据整定面板（设计文档 V1.1 FR-11.5 染料命名 + FR-12 Origin 式时间工作表）。

一个面板两个页签，都作用于**分析之前**、且都**不写回原始数据文件**（§1.3-11）：

* **染料命名**：按组列出 ``[组名 | 染料名(可编辑) | 来源 | ☑记住]``；改一行 = 改整组；
  勾「记住」写入 ``dyes.yaml``（持久），不勾只在本会话有效。
* **时间工作表**：按组列出 ``[☑ | 文件名 | t(文件名) | t(生效) | 备注]``；
  支持单行编辑与四种批量模式（常量 / 起点+步长 / 平移 / 锚点插值）、
  恢复文件名时间、Ctrl+Z 撤销。

面板只发出信号，真正的应用由页面/主窗口完成（保证 UI 不持有 core 状态）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog,
                               QDialogButtonBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from core.io import Scan
from ui.widgets.time_units import (UNIT_LABELS, format_time_value,
                                    normalize_unit, parse_time_input)
from ui import theme

log = logging.getLogger("dye_tool.ui.dye_time_panel")

#: V2.9 FR-60.4：时间工作表三态
TIME_MODE_LABELS = ["自动识别（默认）", "全部留空待填", "恢复文件名时间"]
_TIME_MODES = ("auto", "blank", "restore")
_TIME_BLANK_REASON = "时间未定（请在时间工作表填写）"

#: 时间工作表列
(T_CHECK, T_FILE, T_SRC, T_EFF, T_NOTE) = range(5)
T_HEADERS = ["参与", "文件名", "t(文件名)", "t(生效)", "备注"]

#: 染料命名表列
(D_GROUP, D_DYE, D_FROM, D_REMEMBER) = range(4)
D_HEADERS = ["组名", "染料名（可编辑）", "来源", "记住"]

_SRC_ZH = {"auto": "自动", "session": "本次会话", "registry": "注册表"}


class _BatchDialog(QDialog):
    """批量改时间的参数对话框（四种模式，FR-12.2）。"""

    def __init__(self, parent=None, scope: str = "选中行"):
        super().__init__(parent)
        self.setWindowTitle("批量设置时间")
        self.cmb = QComboBox()
        self.cmb.addItems(["常量 c（全部设为同一值）",
                           "起点 t₀ + 步长 Δt（按行序递增）",
                           "平移 ±δ（在现值上加减）",
                           "锚点插值（首尾为锚，中间线性）"])
        self.sp_c = QDoubleSpinBox()
        self.sp_c.setRange(0.0, 1e7)
        self.sp_c.setValue(0.0)
        self.sp_c.setDecimals(3)
        self.sp_t0 = QDoubleSpinBox()
        self.sp_t0.setRange(0.0, 1e7)
        self.sp_t0.setDecimals(3)
        self.sp_dt = QDoubleSpinBox()
        self.sp_dt.setRange(-1e7, 1e7)
        self.sp_dt.setValue(60.0)
        self.sp_dt.setDecimals(3)
        self.sp_shift = QDoubleSpinBox()
        self.sp_shift.setRange(-1e7, 1e7)
        self.sp_shift.setDecimals(3)
        self.sp_a0 = QDoubleSpinBox()
        self.sp_a0.setRange(0.0, 1e7)
        self.sp_a1 = QDoubleSpinBox()
        self.sp_a1.setRange(0.0, 1e7)
        self.sp_a1.setValue(600.0)
        self.cmb.currentIndexChanged.connect(self._sync)
        form = QFormLayout()
        form.addRow("模式", self.cmb)
        form.addRow("常量 c (s)", self.sp_c)
        form.addRow("起点 t₀ (s)", self.sp_t0)
        form.addRow("步长 Δt (s)", self.sp_dt)
        form.addRow("平移 δ (s)", self.sp_shift)
        form.addRow("首锚 (s)", self.sp_a0)
        form.addRow("尾锚 (s)", self.sp_a1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"应用范围：<b>{scope}</b>（在表里选中若干行可缩小范围）"))
        lay.addLayout(form)
        lay.addWidget(bb)
        self._sync()
        self.setToolTip("四种模式可组合使用：先批量铺一遍，再逐行微调。")

    def _sync(self) -> None:
        i = self.cmb.currentIndex()
        for w in (self.sp_c, self.sp_t0, self.sp_dt, self.sp_shift,
                  self.sp_a0, self.sp_a1):
            w.setEnabled(False)
        if i == 0:
            self.sp_c.setEnabled(True)
        elif i == 1:
            self.sp_t0.setEnabled(True)
            self.sp_dt.setEnabled(True)
        elif i == 2:
            self.sp_shift.setEnabled(True)
        else:
            self.sp_a0.setEnabled(True)
            self.sp_a1.setEnabled(True)

    def values(self) -> dict:
        return {"mode": self.cmb.currentIndex(), "c": self.sp_c.value(),
                "t0": self.sp_t0.value(), "dt": self.sp_dt.value(),
                "shift": self.sp_shift.value(), "a0": self.sp_a0.value(),
                "a1": self.sp_a1.value()}


class DyeTimePanel(QWidget):
    """染料命名 + 时间工作表（FR-11.5 / FR-12）。

    信号：
        ``dye_changed(str, str)``       —— ``(组名, 新染料名)``
        ``remember_changed(str, bool)`` —— ``(组名, 是否记住到 dyes.yaml)``
        ``times_changed(dict)``         —— ``{文件名: t}`` 全量覆盖表
        ``restore_requested()``         —— 请求整组恢复文件名时间
    """

    dye_changed = Signal(str, str)
    remember_changed = Signal(str, bool)
    times_changed = Signal(dict)
    restore_requested = Signal()
    #: V2.9 FR-60.3/60.4：单位/三态/留空变化 → 页面刷新文件表与下游
    state_changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._scans: list[Scan] = []
        self._dye_of: dict[str, str] = {}
        self._remember: dict[str, bool] = {}
        self._registry: dict = {}
        self._undo: list[dict] = []          # 撤销栈（FR-12.2 ≥10 步）
        self._redo: list[dict] = []          # V2.3 FR-24.7：重做栈（新编辑清空）
        self._loading = False
        #: V2.9 FR-60.3：时间列显示单位（会话记忆；Scan.t 恒为秒）
        self._time_unit = "s"
        #: V2.9 FR-60.4：auto / blank / restore(瞬时动作后回 auto)
        self._time_mode = "auto"
        self._blanked: set[str] = set()
        # G18：本面板的会话覆盖属于**哪个数据目录**。换目录时必须清掉，
        # 否则上一批数据的"用户改名"会盖到新目录里同名组上（见 set_scans）。
        self._dye_dir: Optional[Path] = None
        self._build()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_dye_tab(), "① 染料命名")
        self.tabs.addTab(self._build_time_tab(), "② 时间工作表")
        self.tabs.setToolTip(
            "整定只影响本次分析与 run 目录里的记录，不会修改你的原始数据文件。\n"
            "①：给每个数据组起染料名（决定输出目录与图内标注）。\n"
            "②：时间可在表里直接改（常规路径，按 Origin 习惯）；文件名符合命名建议时自动识别只是加速器。")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)

    def _build_dye_tab(self) -> QWidget:
        w = QWidget()
        self.tbl_dye = QTableWidget(0, len(D_HEADERS))
        self.tbl_dye.setHorizontalHeaderLabels(D_HEADERS)
        self.tbl_dye.setEditTriggers(QAbstractItemView.DoubleClicked
                                     | QAbstractItemView.EditKeyPressed)
        self.tbl_dye.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_dye.verticalHeader().setVisible(False)
        h = self.tbl_dye.horizontalHeader()
        h.setSectionResizeMode(D_GROUP, QHeaderView.Stretch)
        h.setSectionResizeMode(D_DYE, QHeaderView.Stretch)
        h.setSectionResizeMode(D_FROM, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(D_REMEMBER, QHeaderView.ResizeToContents)
        self.tbl_dye.itemChanged.connect(self._on_dye_item)
        self.tbl_dye.setToolTip(
            "双击「染料名」即可改名（整组一起改）。\n"
            "勾选「记住」→ 写入 dyes.yaml，下次打开仍然生效；不勾则只影响本次。")

        self.btn_auto = QPushButton("恢复自动命名")
        self.btn_auto.setToolTip("清掉本组的手工名字，回到组名首字段的自动结果")
        self.btn_auto.clicked.connect(self._restore_auto)
        self.btn_remember_all = QPushButton("记住全部")
        self.btn_remember_all.setToolTip("把所有组当前的染料名都写入 dyes.yaml")
        self.btn_remember_all.clicked.connect(self._remember_all)
        row = QHBoxLayout()
        row.addWidget(QLabel("提示：染料名决定输出目录 runs/&lt;染料&gt;/ 与图内标注"))
        row.addStretch(1)
        row.addWidget(self.btn_auto)
        row.addWidget(self.btn_remember_all)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tbl_dye, 1)
        lay.addLayout(row)
        return w

    def _build_time_tab(self) -> QWidget:
        w = QWidget()
        self.tbl_time = QTableWidget(0, len(T_HEADERS))
        self.tbl_time.setHorizontalHeaderLabels(T_HEADERS)
        hdr0 = self.tbl_time.horizontalHeaderItem(T_EFF)
        if hdr0 is not None:
            hdr0.setText(f"t(生效) [{self._time_unit}]")
        self.tbl_time.setEditTriggers(QAbstractItemView.DoubleClicked
                                      | QAbstractItemView.EditKeyPressed)
        self.tbl_time.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_time.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_time.verticalHeader().setVisible(False)
        h = self.tbl_time.horizontalHeader()
        h.setSectionResizeMode(T_FILE, QHeaderView.Stretch)
        for c in (T_CHECK, T_SRC, T_EFF, T_NOTE):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl_time.itemChanged.connect(self._on_time_item)
        self.tbl_time.setToolTip(
            "「t(文件名)」是解析出来的原值（只读，用于核对）；\n"
            "双击「t(生效)」直接改（时间编辑是常规路径；文件名符合约定只是可选加速器）。\n"
            "可输入秒数（如 300）或带单位（5min / 1.5h）；选中多行后可用下面的批量按钮；Ctrl+Z 撤销。")

        self.btn_batch = QPushButton("批量设置…")
        self.btn_batch.setToolTip("常量 / 起点+步长 / 平移 / 锚点插值 四种模式")
        self.btn_batch.clicked.connect(self._batch)
        self.btn_restore = QPushButton("恢复文件名时间")
        self.btn_restore.setToolTip("把选中行（或全部）还原成文件名解析出的时间")
        self.btn_restore.clicked.connect(self._restore_times)
        self.btn_undo = QPushButton("撤销 (Ctrl+Z)")
        self.btn_undo.clicked.connect(self.undo)
        self.btn_redo = QPushButton("重做 (Ctrl+Y)")
        self.btn_redo.setToolTip("重做上一步撤销（新编辑会清空重做栈）")
        self.btn_redo.clicked.connect(self.redo)
        self.lbl_time = QLabel("")
        for b in (self.btn_batch, self.btn_restore, self.btn_undo, self.btn_redo):
            b.setMaximumWidth(150)
        # V2.9 FR-60.3：单位下拉（列头等效入口；只改显示）
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(list(UNIT_LABELS))
        self.cmb_unit.setMaximumWidth(70)
        self.cmb_unit.setToolTip("时间显示单位（s / min / h）；内部仍按秒计算，只改显示")
        self.cmb_unit.currentTextChanged.connect(self.set_time_unit)
        # V2.9 FR-60.4：三态（自动识别 / 全部留空待填 / 恢复文件名时间）
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(TIME_MODE_LABELS)
        self.cmb_mode.setMaximumWidth(170)
        self.cmb_mode.setToolTip(
            "自动识别（默认）：文件名含时间后缀就自动识别；\n"
            "全部留空待填：新载入文件先不参与下游，填好后自动恢复；\n"
            "恢复文件名时间：把手动整定恢复为文件名解析值（瞬时动作）。")
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.btn_restore.setVisible(False)          # 已并入三态下拉，不另设第二入口
        row = QHBoxLayout()
        row.addWidget(self.lbl_time, 1)
        row.addWidget(QLabel("单位"))
        row.addWidget(self.cmb_unit)
        row.addWidget(self.cmb_mode)
        row.addWidget(self.btn_batch)
        row.addWidget(self.btn_undo)
        row.addWidget(self.btn_redo)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tbl_time, 1)
        lay.addLayout(row)
        # A14（V2.x 审查）：默认 WindowShortcut 会在**整个窗口**内抢按键——
        # 用户在文件表的单元格编辑器里按 Ctrl+Z（原生撤销）也会触发时间表的 undo。
        # 改成 WidgetWithChildrenShortcut：只有焦点在时间表（或其子控件）时才生效。
        for _seq, _slot in ((QKeySequence.Undo, self.undo),
                            (QKeySequence("Ctrl+Shift+Z"), self.redo),
                            (QKeySequence("Ctrl+Y"), self.redo)):
            _sc = QShortcut(_seq, self.tbl_time)
            _sc.setContext(Qt.WidgetWithChildrenShortcut)
            _sc.activated.connect(_slot)
        return w

    # ══════════════ 载入 ══════════════
    def set_scans(self, scans: Sequence[Scan],
                  dye_of: Optional[dict] = None,
                  data_dir: Optional[Path | str] = None,
                  registry: Optional[dict] = None) -> None:
        """填充两个页签。

        ``dye_of`` 为会话覆盖，``registry`` 为 dyes.yaml 内容。

        G18（第二轮审查）——**跨目录残留**：这里原来是
        ``self._dye_of.update(dye_of)``，只增不减；而 ``phi_page`` 又把
        ``dye_panel.dye_map()``（**含自动值**的整表）回喂进来，于是每载入一个目录，
        上一目录的全部命名（含自动推导出来的）都变成"会话覆盖"沉下来。
        换到另一个目录后，同名组会被旧名字静默盖住（新目录的实际归属被忽略）。

        修法两条，缺一不可：

        1. ``data_dir`` 与上次不同 → **清空**会话覆盖（同一目录重载仍保留，
           行为不变）；调用方在载入后记住本次目录（:meth:`remember_dir`）；
        2. 只有 ``dye_of`` 里**与自动命名不同**的项才算真覆盖——自动值不再被
           固化成覆盖（这是上面那条"回喂"路径的根治）。

        时间/重复号整定（``t_override`` / ``dup_index``）不受影响：那是逐文件状态，
        每次 ``set_scans`` 都重建。
        """
        changed = False
        if data_dir is not None:
            d = Path(data_dir)
            if self._dye_dir is not None and d != self._dye_dir:
                self._dye_of.clear()
                changed = True      # 换目录：下面的 dye_of **不能**再回填
            self._dye_dir = d
        if dye_of and not changed:
            # ⚠️ 换目录时**必须跳过**这一步：``change`` 的调用方回喂的正是
            # ``self.dye_map()``（含上一目录的旧名字），刚清空又被填回去，
            # 修复就变成了空操作——第一版就是这么写的，测试当场报红。
            from core.dyes import auto_dye_name
            for k, v in dye_of.items():
                v = str(v or "").strip()
                if not v:
                    continue
                if v == str(auto_dye_name(k)).strip():
                    self._dye_of.pop(k, None)      # 自动值不是"覆盖"
                else:
                    self._dye_of[k] = v
        if registry is not None:
            self._registry = dict(registry)
        self._scans = list(scans)
        self._undo.clear()
        self._blanked.clear()
        if self._time_mode == "blank":
            self._apply_blank_mode()
        self._fill_dye()
        self._fill_time()

    def _group_dye(self, group: str) -> tuple:
        """``(染料名, 来源标记, 是否已记住)``。"""
        from core.dyes import auto_dye_name, sanitize_dye_name
        if group in self._dye_of and str(self._dye_of[group]).strip():
            return (str(self._dye_of[group]), "session",
                    bool(self._remember.get(group, False)))
        ent = self._registry.get(group)
        if isinstance(ent, dict) and ent.get("dye"):
            return (sanitize_dye_name(ent["dye"]), "registry", True)
        if isinstance(ent, str) and ent:
            return (sanitize_dye_name(ent), "registry", True)
        return (sanitize_dye_name(auto_dye_name(group)), "auto", False)

    def _fill_dye(self) -> None:
        groups = sorted({s.group for s in self._scans if not s.excluded})
        self._loading = True
        try:
            self.tbl_dye.setRowCount(len(groups))
            for r, g in enumerate(groups):
                name, src, remembered = self._group_dye(g)
                it_g = QTableWidgetItem(g)
                it_g.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.tbl_dye.setItem(r, D_GROUP, it_g)
                it_d = QTableWidgetItem(name)
                it_d.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                              | Qt.ItemIsEditable)
                if src == "auto":
                    f = QFont()
                    f.setItalic(True)
                    it_d.setFont(f)
                    it_d.setForeground(QBrush(theme.color("text_subtle")))
                    it_d.setToolTip("自动从组名首字段推导；双击可改")
                else:
                    it_d.setToolTip("手工指定；双击可改，「恢复自动命名」可还原")
                self.tbl_dye.setItem(r, D_DYE, it_d)
                it_s = QTableWidgetItem(_SRC_ZH.get(src, src))
                it_s.setFlags(Qt.ItemIsEnabled)
                self.tbl_dye.setItem(r, D_FROM, it_s)
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Checked if remembered else Qt.Unchecked)
                chk.setToolTip("勾选 = 写入 dyes.yaml，下次打开同一组自动用这个名字")
                self.tbl_dye.setItem(r, D_REMEMBER, chk)
        finally:
            self._loading = False

    def _fill_time(self) -> None:
        # V2.9 FR-60.4：留空行虽然 excluded（暂不参与下游），仍必须留在工作表里供填写。
        good = [s for s in self._scans
                if not s.excluded or getattr(s, "time_blank", False)]
        good.sort(key=lambda s: (s.group, s.path.name))
        self._loading = True
        try:
            self.tbl_time.setRowCount(len(good))
            for r, s in enumerate(good):
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                chk.setCheckState(Qt.Checked)
                self.tbl_time.setItem(r, T_CHECK, chk)
                it_f = QTableWidgetItem(s.path.name)
                it_f.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it_f.setToolTip(f"组：{s.group}　角色：{s.role}")
                self.tbl_time.setItem(r, T_FILE, it_f)
                it_s = QTableWidgetItem(self._fmt_time(float(s.t_original)))
                it_s.setFlags(Qt.ItemIsEnabled)
                it_s.setForeground(QBrush(theme.color("text_subtle")))
                it_s.setToolTip(f"文件名解析出的原始时间（只读；{float(s.t_original):g} s）")
                self.tbl_time.setItem(r, T_SRC, it_s)
                blank = bool(getattr(s, "time_blank", False))
                it_e = QTableWidgetItem("" if blank else self._fmt_time(float(s.t)))
                it_e.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                              | Qt.ItemIsEditable)
                if blank:
                    it_e.setToolTip("时间未定：双击填写（填好后自动恢复参与下游）")
                self.tbl_time.setItem(r, T_EFF, it_e)
                it_n = QTableWidgetItem(
                    "时间未定" if blank else ("手动" if s.t_edited else ""))
                it_n.setFlags(Qt.ItemIsEnabled)
                self.tbl_time.setItem(r, T_NOTE, it_n)
                self._style_eff(r, s.t_edited)
        finally:
            self._loading = False
        self._refresh_time_label()

    def _style_eff(self, row: int, edited: bool) -> None:
        it = self.tbl_time.item(row, T_EFF)
        if it is None:
            return
        f = QFont()
        f.setBold(bool(edited))
        it.setFont(f)
        it.setForeground(QBrush(theme.color("text") if edited
                                else theme.color("text_muted")))

    # ══════════════ 染料命名 ══════════════
    def set_group_dye(self, group: str, dye: str) -> None:
        """外部（文件清单双击染料列）设置某组命名并刷新本面板（A02）。

        为什么需要：文件表与整定面板是两个控件，但**只能有一套状态**。
        文件表编辑后经信号调到本方法，把命名写进同一份 ``_dye_of`` 并刷新显示，
        否则"表里显示新名、分析还用旧名"。
        """
        g, d = str(group), str(dye or "").strip()
        if not g or not d:
            return
        self._dye_of[g] = d
        self._fill_dye()

    def refresh_time_table(self) -> None:
        """外部（文件清单改时间列）后刷新时间表显示（A02）。"""
        self._fill_time()

    def dye_map(self) -> dict:
        """本次会话的 ``{组名: 染料名}``（含自动值，供输出目录与标注使用）。"""
        out: dict = {}
        for r in range(self.tbl_dye.rowCount()):
            g = self.tbl_dye.item(r, D_GROUP)
            d = self.tbl_dye.item(r, D_DYE)
            if g is not None and d is not None and d.text().strip():
                out[g.text()] = d.text().strip()
        return out

    def _on_dye_item(self, item: QTableWidgetItem) -> None:
        if self._loading:
            return
        r, c = item.row(), item.column()
        g = self.tbl_dye.item(r, D_GROUP)
        if g is None:
            return
        group = g.text()
        if c == D_DYE:
            name = item.text().strip()
            if not name:
                self._loading = True
                try:
                    item.setText(self._group_dye(group)[0])
                finally:
                    self._loading = False
                QMessageBox.warning(self, "输入无效",
                                    "染料名不能为空。要恢复自动命名请点「恢复自动命名」。")
                return
            self._dye_of[group] = name
            self._loading = True
            try:
                it_s = self.tbl_dye.item(r, D_FROM)
                if it_s is not None:
                    it_s.setText(_SRC_ZH["session"])
                f = item.font()
                f.setItalic(False)
                item.setFont(f)
                item.setForeground(QBrush(theme.color("text")))
            finally:
                self._loading = False
            self.dye_changed.emit(group, name)
        elif c == D_REMEMBER:
            on = item.checkState() == Qt.Checked
            self._remember[group] = on
            self.remember_changed.emit(group, on)

    def _restore_auto(self) -> None:
        """把选中组（或全部）恢复成自动命名。"""
        rows = sorted({i.row() for i in self.tbl_dye.selectedIndexes()})
        if not rows:
            rows = list(range(self.tbl_dye.rowCount()))
        for r in rows:
            g = self.tbl_dye.item(r, D_GROUP)
            if g is None:
                continue
            group = g.text()
            self._dye_of.pop(group, None)
            self._remember.pop(group, None)
            self.remember_changed.emit(group, False)
        self._fill_dye()
        self.dye_changed.emit("", "")

    def _remember_all(self) -> None:
        for r in range(self.tbl_dye.rowCount()):
            c = self.tbl_dye.item(r, D_REMEMBER)
            if c is not None:
                c.setCheckState(Qt.Checked)

    # ══════════════ 时间工作表 ══════════════
    def overrides(self) -> dict:
        """``{文件名: t}``——只含**被改过**的项（FR-12.4）。"""
        out: dict = {}
        for s in self._scans:
            if getattr(s, "t_edited", False):
                out[s.path.name] = float(s.t)
        return out

    def _selected_rows(self) -> list:
        rows = sorted({i.row() for i in self.tbl_time.selectedIndexes()})
        return rows or []

    def _rows_in_scope(self) -> list:
        """批量操作范围：选中的行；没选则全部。"""
        rows = self._selected_rows()
        return rows if rows else list(range(self.tbl_time.rowCount()))

    def _push_undo(self) -> None:
        snap = {s.path.name: float(s.t) for s in self._scans}
        self._undo.append(snap)
        if len(self._undo) > 20:
            self._undo.pop(0)
        self._redo.clear()                   # V2.3 FR-24.7：新编辑使重做失效

    def undo(self) -> None:
        """撤销上一步时间整定（Ctrl+Z）。"""
        if not self._undo:
            self._refresh_time_label("没有可撤销的改动")
            return
        self._redo.append({s.path.name: float(s.t) for s in self._scans})
        snap = self._undo.pop()
        for s in self._scans:
            if s.path.name in snap:
                s.t = float(snap[s.path.name])
        self._loading = True
        try:
            for r in range(self.tbl_time.rowCount()):
                it_f = self.tbl_time.item(r, T_FILE)
                if it_f is None:
                    continue
                val = snap.get(it_f.text())
                if val is None:
                    continue
                it_e = self.tbl_time.item(r, T_EFF)
                if it_e is not None:
                    it_e.setText(self._fmt_time(float(val)))
                s = self._scan_of(it_f.text())
                self._style_eff(r, bool(s.t_edited) if s else False)
                it_n = self.tbl_time.item(r, T_NOTE)
                if it_n is not None and s is not None:
                    it_n.setText("手动" if s.t_edited else "")
        finally:
            self._loading = False
        self._emit_times("已撤销")
        self._refresh_time_label("已撤销一步")

    def redo(self) -> None:
        """V2.3 FR-24.7：重做上一步时间整定（Ctrl+Shift+Z / Ctrl+Y）。"""
        if not self._redo:
            self._refresh_time_label("没有可重做的改动")
            return
        self._undo.append({s.path.name: float(s.t) for s in self._scans})
        snap = self._redo.pop()
        self._loading = True
        try:
            for s in self._scans:
                if s.path.name in snap:
                    s.t = float(snap[s.path.name])
            for r in range(self.tbl_time.rowCount()):
                it_f = self.tbl_time.item(r, T_FILE)
                if it_f is None:
                    continue
                val = snap.get(it_f.text())
                if val is None:
                    continue
                it_e = self.tbl_time.item(r, T_EFF)
                if it_e is not None:
                    it_e.setText(self._fmt_time(float(val)))
                s = self._scan_of(it_f.text())
                self._style_eff(r, bool(s.t_edited) if s else False)
                it_n = self.tbl_time.item(r, T_NOTE)
                if it_n is not None and s is not None:
                    it_n.setText("手动" if s.t_edited else "")
        finally:
            self._loading = False
        self._emit_times("已重做")
        self._refresh_time_label("已重做一步")

    def _scan_of(self, fname: str) -> Optional[Scan]:
        for s in self._scans:
            if s.path.name == fname:
                return s
        return None

    def _batch(self) -> None:
        rows = self._rows_in_scope()
        if not rows:
            QMessageBox.information(self, "提示", "表里没有数据行")
            return
        scope = f"选中 {len(rows)} 行" if self._selected_rows() else "全部行"
        dlg = _BatchDialog(self, scope)
        if dlg.exec() != QDialog.Accepted:
            return
        v = dlg.values()
        self._push_undo()
        n = len(rows)
        for i, r in enumerate(rows):
            it_f = self.tbl_time.item(r, T_FILE)
            if it_f is None:
                continue
            s = self._scan_of(it_f.text())
            if s is None:
                continue
            cur = float(s.t)
            if v["mode"] == 0:
                val = v["c"]
            elif v["mode"] == 1:
                val = v["t0"] + v["dt"] * i
            elif v["mode"] == 2:
                val = cur + v["shift"]
            else:
                val = (v["a0"] + (v["a1"] - v["a0"]) * i / max(n - 1, 1))
            val = max(0.0, float(val))
            if s.t_src is None:
                s.t_src = float(s.t)
            s.t = val
            s.time_blank = False
            self._blanked.discard(str(s.path))
            if s.excluded == _TIME_BLANK_REASON:
                s.excluded = ""
        self._refresh_time_rows(rows)
        self._emit_times(f"批量设置 {len(rows)} 行")

    def _restore_times(self) -> None:
        rows = self._rows_in_scope()
        self._push_undo()
        for r in rows:
            it_f = self.tbl_time.item(r, T_FILE)
            if it_f is None:
                continue
            s = self._scan_of(it_f.text())
            if s is None:
                continue
            s.t = float(s.t_original)
            s.time_blank = False
            if s.excluded == _TIME_BLANK_REASON:
                s.excluded = ""
        self._blanked.clear()
        self._refresh_time_rows(rows)
        self.restore_requested.emit()
        self._emit_times("已恢复文件名时间")

    def _refresh_time_rows(self, rows: Sequence[int]) -> None:
        self._loading = True
        try:
            for r in rows:
                it_f = self.tbl_time.item(r, T_FILE)
                if it_f is None:
                    continue
                s = self._scan_of(it_f.text())
                if s is None:
                    continue
                it_e = self.tbl_time.item(r, T_EFF)
                if it_e is not None:
                    blank = bool(getattr(s, "time_blank", False))
                    it_e.setText("" if blank else self._fmt_time(float(s.t)))
                self._style_eff(r, s.t_edited)
                it_n = self.tbl_time.item(r, T_NOTE)
                if it_n is not None:
                    blank = bool(getattr(s, "time_blank", False))
                    it_n.setText("时间未定" if blank else ("手动" if s.t_edited else ""))
        finally:
            self._loading = False

    def _on_time_item(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != T_EFF:
            return
        r = item.row()
        it_f = self.tbl_time.item(r, T_FILE)
        if it_f is None:
            return
        s = self._scan_of(it_f.text())
        if s is None:
            return
        text = item.text().strip()
        val = parse_time_input(text)
        if val is None:
            self._revert_cell(r, s, f"「{text}」不是合法时间——可输入秒数（如 300）"
                                  "或带单位（5min / 1.5h）")
            return
        self._push_undo()
        if s.t_src is None:
            s.t_src = float(s.t)
        s.t = float(val)
        s.time_blank = False
        self._blanked.discard(str(s.path))
        if s.excluded == _TIME_BLANK_REASON:
            s.excluded = ""
        self._loading = True
        try:
            item.setText(self._fmt_time(val))
            self._style_eff(r, s.t_edited)
            it_n = self.tbl_time.item(r, T_NOTE)
            if it_n is not None:
                it_n.setText("手动" if s.t_edited else "")
        finally:
            self._loading = False
        self._emit_times(f"{s.path.name} → {val:g} s")
        self.state_changed.emit()

    def _revert_cell(self, row: int, s: Scan, why: str) -> None:
        self._loading = True
        try:
            it = self.tbl_time.item(row, T_EFF)
            if it is not None:
                it.setText(f"{float(s.t):g}")
        finally:
            self._loading = False
        QMessageBox.warning(self, "输入无效", why)

    def _emit_times(self, note: str = "") -> None:
        self.times_changed.emit(self.overrides())
        self._refresh_time_label(note)
        self.state_changed.emit()

    def _refresh_time_label(self, extra: str = "") -> None:
        n = sum(1 for s in self._scans if getattr(s, "t_edited", False))
        from core.io import validate_times
        try:
            issues = validate_times(self._scans)
        except Exception as err:                           # noqa: BLE001
            # G20：原来是 `issues = []`——校验器坏掉时标签显示"0 条告警"，
            # 与"真的没问题"完全同形（假绿）。留痕，标签仍不阻断。
            log.warning("时间校验失败（标签按 0 条告警显示）：%s", err)
            issues = []
        warn = f" · ⚠ {len(issues)} 条时间告警（见备注/日志）" if issues else ""
        n_blank = sum(1 for s in self._scans if getattr(s, "time_blank", False))
        blank = f" · 时间未定 {n_blank} 行（已暂不参与下游）" if n_blank else ""
        txt = f"已手动整定 {n} 个时间点{warn}{blank}"
        if extra:
            txt = f"{extra} · {txt}"
        self.lbl_time.setText(txt)
        self._show_time_issues(issues)


    # ══════════════ V2.9 FR-60.3/60.4：单位与三态 ══════════════
    @property
    def time_unit(self) -> str:
        return self._time_unit

    def _fmt_time(self, seconds: float) -> str:
        """按当前显示单位格式化秒值（不改 Scan.t）。"""
        return format_time_value(seconds, self._time_unit)

    def set_time_unit(self, unit: str) -> None:
        """切换时间列显示单位（FR-60.3；只改显示，Scan.t 不变）。"""
        u = normalize_unit(unit)
        if u == self._time_unit:
            return
        self._time_unit = u
        self.cmb_unit.blockSignals(True)
        self.cmb_unit.setCurrentText(u)
        self.cmb_unit.blockSignals(False)
        h = self.tbl_time.horizontalHeaderItem(T_EFF)
        if h is not None:
            h.setText(f"t(生效) [{u}]")
        self._fill_time()
        self.state_changed.emit()

    def set_time_mode(self, mode: str) -> None:
        """设置三态（auto / blank / restore）；restore 为瞬时动作后回 auto。"""
        m = str(mode or "auto")
        if m not in _TIME_MODES:
            m = "auto"
        if m == "restore":
            self._clear_blank()
            self._restore_times()
            self._time_mode = "auto"
            self.cmb_mode.blockSignals(True)
            self.cmb_mode.setCurrentIndex(0)
            self.cmb_mode.blockSignals(False)
            self.state_changed.emit()
            return
        self._time_mode = m
        self.cmb_mode.blockSignals(True)
        self.cmb_mode.setCurrentIndex(1 if m == "blank" else 0)
        self.cmb_mode.blockSignals(False)
        if m == "blank":
            self._apply_blank_mode()
        else:
            self._clear_blank()
        self._fill_time()
        self._emit_times()
        self.state_changed.emit()

    def _on_mode_changed(self, idx: int) -> None:
        if self._loading:
            return
        if idx == 1:
            self.set_time_mode("blank")
        elif idx == 2:
            self.set_time_mode("restore")
        else:
            self.set_time_mode("auto")

    def _apply_blank_mode(self) -> None:
        """「全部留空待填」：只影响**无手动整定**的可用行；不覆盖已有整定。"""
        self._blanked.clear()
        for s in self._scans:
            if s.excluded and s.excluded != _TIME_BLANK_REASON:
                continue
            if getattr(s, "t_edited", False):
                continue
            if s.excluded == _TIME_BLANK_REASON:
                self._blanked.add(str(s.path))
                continue
            s.time_blank = True
            s.excluded = _TIME_BLANK_REASON
            self._blanked.add(str(s.path))

    def _clear_blank(self) -> None:
        for s in self._scans:
            if getattr(s, "time_blank", False) or s.excluded == _TIME_BLANK_REASON:
                s.time_blank = False
                if s.excluded == _TIME_BLANK_REASON:
                    s.excluded = ""
        self._blanked.clear()

    def _show_time_issues(self, issues: Sequence) -> None:
        """把校验问题写进备注列（FR-12.3：只警告不阻塞）。"""
        if not issues:
            return
        self._loading = True
        try:
            for iss in issues:
                for r in range(self.tbl_time.rowCount()):
                    it_f = self.tbl_time.item(r, T_FILE)
                    if it_f is None:
                        continue
                    s = self._scan_of(it_f.text())
                    if s is None or f".{s.group}" not in str(iss.key):
                        continue
                    it_n = self.tbl_time.item(r, T_NOTE)
                    if it_n is None:
                        continue
                    base = it_n.text().replace("⚠ ", "")
                    if "重复时间" in iss.message or "非单调" in iss.message:
                        it_n.setText(f"⚠ {base}".strip())
                        it_n.setToolTip(iss.message + "\n建议：" + iss.suggestion)
        finally:
            self._loading = False

    # ══════════════ 便捷：整表刷新 ══════════════
    def refresh(self) -> None:
        """重新按当前 scans 填表（外部改了 scans 后调用）。"""
        self._fill_dye()
        self._fill_time()

