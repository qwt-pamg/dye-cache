"""② 斯托克斯位移任务卡（V2.6 FR-41）"""

from __future__ import annotations

import logging

import math
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.paths import app_root
from core.paths import resolve_out_root
from core.report.stokes_table import STOKES_COLUMNS
from core.report.stokes_table import default_stokes_name
from core.report.stokes_table import fmt_nm
from core.report.stokes_table import fmt_wavenumber
from core.report.stokes_table import group_from_path
from core.report.stokes_table import row_from_spectra
from core.report.stokes_table import write_stokes_csv
from ui.pages.base import PageBase
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

class StokesPanel(PageBase):
    """V2.6 FR-41：斯托克斯位移一对一测量 + 跨组对比表（会话级台账）。

    口径（用户反馈 + Q-E16）：斯托克斯位移是**一种染料**的属性 → 每次只允许
    一对谱（吸收 × 发射）；不自动配对、不自动入表。数值口径复用
    core.analysis.spectra.stokes_shift（本卡不重写算法）。
    """

    TITLE = "斯托克斯位移（一对一测量）"
    SUBTITLE = ("吸收谱与发射谱各选一支（单选，点击替换）→ 即时预览 "
                "λabs/λem/Δλ/Δν̃ → 「加入对比表」形成跨组台账。"
                "不自动配对、不自动入表；对比表仅本次会话（导出 CSV 持久化）。")

    #: 对比表列（与 core/report/stokes_table.py 的 STOKES_COLUMNS 同源）
    COLUMNS = list(STOKES_COLUMNS)
    (COL_GROUP, COL_ABS, COL_EM, COL_LAM_ABS,
     COL_LAM_EM, COL_DLAM, COL_DNU, COL_TIME) = range(8)

    #: 表头列 → 排序键（表头点击与 sort_by 共用）
    _SORT_KEY_BY_COL = {0: "group", 1: "abs_file", 2: "em_file",
                        3: "lam_abs", 4: "lam_em", 5: "delta_lambda",
                        6: "delta_wavenumber", 7: "recorded_at"}
    #: sort_by 的键别名（宽容：英文/中文/表头文本都收）
    _SORT_ALIASES = {
        "group": "group", "组别": "group",
        "abs": "abs_file", "abs_file": "abs_file", "file": "abs_file",
        "吸收谱": "abs_file", "吸收谱文件": "abs_file",
        "em": "em_file", "em_file": "em_file", "发射谱": "em_file",
        "发射谱文件": "em_file",
        "lam_abs": "lam_abs", "lambda_abs": "lam_abs", "λabs": "lam_abs",
        "λabs(nm)": "lam_abs",
        "lam_em": "lam_em", "lambda_em": "lam_em", "λem": "lam_em",
        "λem(nm)": "lam_em",
        "delta_lambda": "delta_lambda", "dlambda": "delta_lambda",
        "d_lam": "delta_lambda", "Δλ": "delta_lambda",
        "Δλ(nm)": "delta_lambda",
        "delta_wavenumber": "delta_wavenumber", "dnu": "delta_wavenumber",
        "d_nu": "delta_wavenumber", "Δν": "delta_wavenumber",
        "Δν̃": "delta_wavenumber", "Δν̃(cm⁻¹)": "delta_wavenumber",
        "recorded_at": "recorded_at", "time": "recorded_at",
        "录入时间": "recorded_at"}
    _NUMERIC_KEYS = ("lam_abs", "lam_em", "delta_lambda", "delta_wavenumber")

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.spectra: list = []
        self._abs: list = []
        self._em: list = []
        self._rows: list = []          # 录入顺序（会话级台账）
        self._view: list[int] = []     # 表行 → _rows 下标
        self._sort_key: Optional[str] = None
        self._sort_desc = False
        self._group_filter: Optional[set] = None
        self._filling = False
        self._last_csv: Optional[Path] = None
        self._preview: dict = {}
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择光谱目录…")
        self.lbl_dir = QLabel("未选择目录")
        theme.mark(self.lbl_dir, "muted")
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.lbl_dir, 1)
        lay.addLayout(bar)
        hint = QLabel(
            "每次只选一对：左侧吸收谱 + 右侧发射谱各一支（单选，点击第二个即替换）；"
            "未选齐时「加入对比表」禁用，也不会自动配对/自动入表。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        self.lst_abs = QListWidget()
        self.lst_abs.setObjectName("stokesAbsList")
        self.lst_em = QListWidget()
        self.lst_em.setObjectName("stokesEmList")
        for lst in (self.lst_abs, self.lst_em):
            lst.setSelectionMode(QAbstractItemView.SingleSelection)
            lst.setAlternatingRowColors(True)
        split = QSplitter(Qt.Horizontal)
        for lst, title in ((self.lst_abs, "吸收谱（kind=abs）"),
                           (self.lst_em, "发射谱（kind=em）")):
            box = QGroupBox(title)
            vb = QVBoxLayout(box)
            vb.addWidget(lst)
            split.addWidget(box)
        split.setSizes([400, 400])
        lay.addWidget(split, 2)

        preview_row = QHBoxLayout()
        self.lbl_abs = QLabel("λabs = —")
        self.lbl_em = QLabel("λem = —")
        self.lbl_dlam = QLabel("Δλ = —")
        self.lbl_dnu = QLabel("Δν̃ = —")
        for lbl in (self.lbl_abs, self.lbl_em, self.lbl_dlam, self.lbl_dnu):
            theme.mark(lbl, "strong")
            preview_row.addWidget(lbl)
        preview_row.addStretch(1)
        lay.addLayout(preview_row)
        self.lbl_preview = QLabel("未选齐：请各选一支吸收谱与发射谱")
        self.lbl_preview.setWordWrap(True)
        theme.mark(self.lbl_preview, "muted")
        lay.addWidget(self.lbl_preview)

        row = QHBoxLayout()
        self.btn_add = QPushButton("加入对比表")
        self.btn_add.setEnabled(False)
        self.btn_delete = QPushButton("删除选中")
        self.btn_clear = QPushButton("清空")
        self.btn_export = QPushButton("导出 CSV…")
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_delete)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        row.addWidget(self.btn_export)
        lay.addLayout(row)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("组过滤"))
        self.cmb_group_filter = QComboBox()
        self.cmb_group_filter.addItem("全部组")
        filt.addWidget(self.cmb_group_filter)
        filt.addStretch(1)
        lay.addLayout(filt)

        self.tbl = QTableWidget(0, len(self.COLUMNS))
        self.tbl.setObjectName("stokesTable")
        self.tbl.setHorizontalHeaderLabels(self.COLUMNS)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.DoubleClicked
                                 | QAbstractItemView.EditKeyPressed)
        hdr = self.tbl.horizontalHeader()
        for c in (self.COL_GROUP, self.COL_ABS, self.COL_EM, self.COL_TIME):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
        for c in (self.COL_LAM_ABS, self.COL_LAM_EM, self.COL_DLAM,
                  self.COL_DNU):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl.setColumnWidth(self.COL_GROUP, 110)
        self.tbl.setColumnWidth(self.COL_ABS, 150)
        self.tbl.setColumnWidth(self.COL_EM, 150)
        self.tbl.setColumnWidth(self.COL_TIME, 140)
        hdr.setSectionsClickable(True)
        hdr.sectionClicked.connect(self._on_header_clicked)
        hdr.setSortIndicatorShown(False)
        self.tbl.setSortingEnabled(False)
        self.tbl.itemChanged.connect(self._on_item_changed)
        lay.addWidget(self.tbl, 2)

        self.lbl_csv = QLabel("导出：—")
        self.lbl_csv.setWordWrap(True)
        theme.mark(self.lbl_csv, "hint")
        lay.addWidget(self.lbl_csv)
        lay.addWidget(self.progress_row())

        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_add.clicked.connect(self._add_row)
        self.btn_delete.clicked.connect(self._delete_selected)
        self.btn_clear.clicked.connect(self.clear_rows)
        self.btn_export.clicked.connect(lambda: self.export_csv())
        self.cmb_group_filter.currentIndexChanged.connect(
            self._on_group_filter_changed)
        self.lst_abs.currentRowChanged.connect(self._update_preview)
        self.lst_em.currentRowChanged.connect(self._update_preview)

    # ── 载入（FR-41.1：与③作图台同一加载器 / 同一 kind 判定） ──
    def set_data_dir(self, data_dir: Path | str, *, force: bool = False) -> None:
        """选择光谱目录 → run_task 载入（忙禁用/可取消；换目录重置候选、表保留）。"""
        d = Path(data_dir)
        if not force and d == self.data_dir and self.spectra:
            return
        self.data_dir = d
        self.lbl_dir.setText(str(d))
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.cfg)
        except Exception as err:                       # noqa: BLE001
            files = []
            log.warning("枚举斯托克斯候选目录失败：%s", err)
        if not files:
            self.spectra = []
            self._populate_lists()
            self.status.emit(f"{d} 下没有可解析的光谱文件")
            self.logged.emit("WARNING", f"{d} 下没有找到光谱文件")
            return
        from core.inputs.io import load_spectra_files

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            spectra = load_spectra_files(files, self.cfg)
            progress_cb(90, "载入完成")
            return spectra

        self.run_task(task, "载入光谱", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.lst_abs, self.lst_em])

    def _choose_dir(self) -> None:
        start = str(self.data_dir or (self.cfg.get("gui") or {}).get("last_dir")
                    or app_root())
        d = QFileDialog.getExistingDirectory(self, "选择光谱目录", start)
        if d:
            self.remember_dir(Path(d))
            self.set_data_dir(Path(d), force=True)

    def _on_loaded(self, spectra) -> None:
        had_old = bool(self.spectra)
        self.spectra = list(spectra or [])
        self._populate_lists()
        fails = [s for s in self.spectra if not getattr(s, "ok", True)]
        for s in fails[:20]:
            name = Path(str(getattr(s, "path", ""))).name
            why = getattr(s, "excluded", "") or "读取失败"
            self.logged.emit("WARNING", f"剔除 {name}：{why}")
        extra = f"，失败 {len(fails)}" if fails else ""
        self.lbl_dir.setText(f"{self.data_dir}（吸收 {len(self._abs)} / "
                             f"发射 {len(self._em)}{extra}）")
        self.status.emit(f"已载入 {len(self.spectra)} 支光谱：吸收 "
                         f"{len(self._abs)} / 发射 {len(self._em)}{extra}")
        self.logged.emit("INFO", f"斯托克斯候选已载入：吸收 {len(self._abs)} 支，"
                                 f"发射 {len(self._em)} 支；剔除 {len(fails)} 支")
        if had_old:
            # FR-41.5：换目录必须提示"候选/预览已重置、对比表保留"
            self.status.emit("换目录：配对候选与预览已重置（对比表保留）")
            self.logged.emit("INFO",
                             "换目录：配对候选与预览已重置（已录入的对比表保留）")

    def _populate_lists(self) -> None:
        """按 kind 分左右候选列表（只列可用谱；item = 组别 / 文件名 / 点数）。"""
        self._abs = [s for s in self.spectra
                     if getattr(s, "ok", True) and getattr(s, "kind", "") == "abs"]
        self._em = [s for s in self.spectra
                    if getattr(s, "ok", True) and getattr(s, "kind", "") == "em"]
        for lst, items in ((self.lst_abs, self._abs), (self.lst_em, self._em)):
            lst.blockSignals(True)
            try:
                lst.clear()
                lst.setCurrentRow(-1)
                for i, s in enumerate(items):
                    group = group_from_path(getattr(s, "path", ""), self.cfg)
                    wl = getattr(s, "wl", None)
                    n_pts = 0 if wl is None else len(wl)
                    it = QListWidgetItem(
                        f"{group} / {Path(str(s.path)).name} / {n_pts} 点")
                    it.setData(Qt.UserRole, i)
                    it.setToolTip(str(getattr(s, "path", "")))
                    lst.addItem(it)
            finally:
                lst.blockSignals(False)
        self._update_preview()

    # ── 一对一选择 + 即时预览（FR-41.2） ──
    @staticmethod
    def _pick_list(lst: QListWidget, items: list):
        row = lst.currentRow()
        return items[row] if 0 <= row < len(items) else None

    def selected_pair(self):
        """当前选中 (吸收谱, 发射谱)；未选齐返回 (None, None)。"""
        return (self._pick_list(self.lst_abs, self._abs),
                self._pick_list(self.lst_em, self._em))

    def preview_values(self) -> dict:
        """即时预览数值 {lam_abs, lam_em, delta_lambda, delta_wavenumber}。

        NaN 原样返回（未选齐时全 NaN）；中文原因见 lbl_preview。
        """
        keys = ("lam_abs", "lam_em", "delta_lambda", "delta_wavenumber")
        return {k: float(self._preview.get(k, float("nan"))) for k in keys}

    def _update_preview(self, *args) -> None:          # noqa: ANN002 —— Qt 信号参数
        a, e = self.selected_pair()
        nan = float("nan")
        self._preview = {"lam_abs": nan, "lam_em": nan,
                         "delta_lambda": nan, "delta_wavenumber": nan}
        reason = ""
        if a is None or e is None:
            reason = "未选齐：请各选一支吸收谱与发射谱"
        else:
            row = row_from_spectra(a, e)
            self._preview = {"lam_abs": row.lam_abs, "lam_em": row.lam_em,
                             "delta_lambda": row.delta_lambda,
                             "delta_wavenumber": row.delta_wavenumber}
            reason = self._row_reason(row)
        self.lbl_abs.setText(f"λabs = {fmt_nm(self._preview['lam_abs'])} nm")
        self.lbl_em.setText(f"λem = {fmt_nm(self._preview['lam_em'])} nm")
        self.lbl_dlam.setText(f"Δλ = {fmt_nm(self._preview['delta_lambda'])} nm")
        self.lbl_dnu.setText(
            f"Δν̃ = {fmt_wavenumber(self._preview['delta_wavenumber'])} cm⁻¹")
        if reason:
            self.lbl_preview.setText(f"不可计算：{reason}")
        else:
            self.lbl_preview.setText(
                f"λabs={fmt_nm(self._preview['lam_abs'])} nm　"
                f"λem={fmt_nm(self._preview['lam_em'])} nm　"
                f"Δλ={fmt_nm(self._preview['delta_lambda'])} nm　"
                f"Δν̃={fmt_wavenumber(self._preview['delta_wavenumber'])} cm⁻¹")
        self.btn_add.setEnabled(a is not None and e is not None)

    @staticmethod
    def _row_reason(row) -> str:
        if not math.isfinite(float(row.lam_abs)):
            return "吸收谱无可取峰（λmax 不可得）"
        if not math.isfinite(float(row.lam_em)):
            return "发射谱无可取峰（λmax 不可得）"
        if not math.isfinite(float(row.delta_lambda)):
            return "Δλ 不可计算"
        return ""

    def _add_row(self) -> None:
        """「加入对比表」= 一次计算落一行（NaN 行也入库并标不可计算）。"""
        a, e = self.selected_pair()
        if a is None or e is None:
            self.status.emit("请各选一支吸收谱与发射谱后再加入对比表")
            return
        row = row_from_spectra(a, e)
        self._rows.append(row)
        self._rebuild_table()
        if row.ok:
            self.status.emit(
                f"已加入对比表：组「{row.group}」 Δλ={fmt_nm(row.delta_lambda)} nm")
            self.logged.emit(
                "INFO", f"斯托克斯位移入表：组 {row.group} "
                f"λabs={fmt_nm(row.lam_abs)} λem={fmt_nm(row.lam_em)} "
                f"Δλ={fmt_nm(row.delta_lambda)} nm "
                f"Δν̃={fmt_wavenumber(row.delta_wavenumber)} cm⁻¹")
        else:
            reason = self._row_reason(row)
            self.status.emit(f"已加入对比表：组「{row.group}」不可计算（{reason}）")
            self.logged.emit("WARNING", f"斯托克斯位移不可计算：{reason}")

    # ── 对比表（FR-41.4：会话级台账；跨组行共存即对比） ──
    def rows(self) -> list:
        """当前对比表记录（录入顺序；测试/导出共用）。"""
        return list(self._rows)

    def _sorted_indices(self, base: list) -> list:
        key = self._sort_key
        if not key:
            return list(base)

        def sk(i: int):
            row = self._rows[i]
            val = getattr(row, key, "")
            if key in self._NUMERIC_KEYS:
                v = float(val)
                return (1, 0.0) if not math.isfinite(v) else (0, v)
            return (0, str(val).lower())

        return sorted(base, key=sk, reverse=self._sort_desc)

    def _visible_indices(self) -> list:
        keep = self._group_filter
        base = [i for i, r in enumerate(self._rows)
                if keep is None or str(r.group) in keep]
        return self._sorted_indices(base)

    def _rebuild_table(self) -> None:
        self._view = self._visible_indices()
        self._filling = True
        try:
            self.tbl.setRowCount(len(self._view))
            for vr, idx in enumerate(self._view):
                row = self._rows[idx]
                reason = self._row_reason(row)
                cells = [row.group, row.abs_file, row.em_file,
                         fmt_nm(row.lam_abs), fmt_nm(row.lam_em),
                         fmt_nm(row.delta_lambda),
                         fmt_wavenumber(row.delta_wavenumber),
                         row.recorded_at]
                for c, text in enumerate(cells):
                    it = QTableWidgetItem(text)
                    it.setData(Qt.UserRole, idx)
                    if c == self.COL_GROUP:
                        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                                    | Qt.ItemIsEditable)
                    else:
                        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    if reason and c in (self.COL_LAM_ABS, self.COL_LAM_EM,
                                        self.COL_DLAM, self.COL_DNU):
                        it.setForeground(theme.color("text_subtle"))
                        it.setToolTip(f"不可计算：{reason}")
                    self.tbl.setItem(vr, c, it)
        finally:
            self._filling = False
        self._refresh_filter_combo()

    def _refresh_filter_combo(self) -> None:
        groups = sorted({str(r.group) for r in self._rows})
        current = self.cmb_group_filter.currentText()
        self.cmb_group_filter.blockSignals(True)
        try:
            self.cmb_group_filter.clear()
            self.cmb_group_filter.addItem("全部组")
            self.cmb_group_filter.addItems(groups)
            if current and current in groups:
                self.cmb_group_filter.setCurrentText(current)
            else:
                self.cmb_group_filter.setCurrentIndex(0)
        finally:
            self.cmb_group_filter.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """组别列可编辑（人工归组用于对比，Q-E17）；空值回退并提示。"""
        if self._filling or item is None or item.column() != self.COL_GROUP:
            return
        vr = item.row()
        if not (0 <= vr < len(self._view)):
            return
        row = self._rows[self._view[vr]]
        text = item.text().strip()
        if not text:
            self._filling = True
            try:
                item.setText(row.group)
            finally:
                self._filling = False
            self.status.emit("组别不能为空（请填一个组名）")
            return
        if text != row.group:
            row.group = text
            self._refresh_filter_combo()
            self.status.emit(f"已改组别为「{text}」")
            self.logged.emit("INFO", f"对比表组别改为「{text}」")

    def _delete_selected(self) -> int:
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        idxs = sorted({self._view[r] for r in rows
                       if 0 <= r < len(self._view)}, reverse=True)
        if not idxs:
            self.status.emit("没有选中要删除的对比表行")
            return 0
        for idx in idxs:
            if 0 <= idx < len(self._rows):
                del self._rows[idx]
        self._rebuild_table()
        self.status.emit(f"已删除 {len(idxs)} 行对比记录")
        return len(idxs)

    def clear_rows(self) -> bool:
        """清空对比表（**破坏性确认保留模态**；与 data_page 删除模板同款）。"""
        if not self._rows:
            self.status.emit("对比表已为空")
            return False
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self, "清空对比表",
            f"确定清空 {len(self._rows)} 行斯托克斯对比记录吗？"
            "（不可撤销；如需留存请先导出 CSV）")
        if ans != QMessageBox.Yes:
            self.status.emit("已取消清空")
            return False
        n = len(self._rows)
        self._rows = []
        self._rebuild_table()
        self.status.emit(f"已清空 {n} 行对比记录")
        self.logged.emit("INFO", f"斯托克斯对比表已清空（{n} 行）")
        return True

    # ── 排序 / 过滤（与文件表同风格；视图级） ──
    def sort_by(self, col_key: str, descending: bool = False) -> None:
        """按列排序**视图**：group/abs_file/em_file/λabs/λem/Δλ/Δν̃/录入时间。"""
        key = self._SORT_ALIASES.get(str(col_key or "").strip())
        if key is None:
            raise ValueError(f"不支持的排序键：{col_key!r}")
        self._sort_key, self._sort_desc = key, bool(descending)
        self._rebuild_table()
        self._apply_sort_indicator()

    def sort_state(self):
        if not self._sort_key:
            return None
        return (self._sort_key, bool(self._sort_desc))

    def _on_header_clicked(self, col: int) -> None:
        key = self._SORT_KEY_BY_COL.get(int(col))
        if not key:
            return
        desc = (self._sort_key == key and not self._sort_desc)
        self.sort_by(key, descending=desc)
        direction = "降序" if desc else "升序"
        self.status.emit(
            f"对比表按「{self.COLUMNS[col]}」{direction}排序（会话台账顺序不变）")

    def _apply_sort_indicator(self) -> None:
        hdr = self.tbl.horizontalHeader()
        if not self._sort_key:
            hdr.setSortIndicatorShown(False)
            return
        col = next((c for c, k in self._SORT_KEY_BY_COL.items()
                    if k == self._sort_key), self.COL_GROUP)
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(
            col, Qt.DescendingOrder if self._sort_desc else Qt.AscendingOrder)

    def set_group_filter(self, groups=None) -> None:
        """按组过滤视图（None = 全部；只影响显示，不删记录）。"""
        if groups is None:
            self._group_filter = None
        elif isinstance(groups, str):
            self._group_filter = {groups}
        else:
            self._group_filter = {str(g) for g in groups}
        self._rebuild_table()

    def group_filter(self) -> Optional[set]:
        return None if self._group_filter is None else set(self._group_filter)

    def _on_group_filter_changed(self, idx: int) -> None:
        text = self.cmb_group_filter.currentText()
        self.set_group_filter(None if idx <= 0 or text == "全部组" else {text})

    # ── 导出（FR-41.6：UTF-8-SIG / 中文列名 / 不覆盖 / 空表不产文件） ──
    def export_csv(self, path=None, *, now=None):
        """导出对比表 CSV：path 给定直接写；None 弹另存对话框（默认落 out_root）。"""
        if not self._rows:
            self.status.emit("对比表为空：没有可导出的记录（未生成文件）")
            self.logged.emit("WARNING", "斯托克斯对比表为空，未导出 CSV")
            return None
        if path is None:
            out_root = resolve_out_root(self.cfg)
            default = out_root / default_stokes_name(now)
            path, _ = QFileDialog.getSaveFileName(
                self, "导出斯托克斯位移 CSV", str(default), "CSV 文件 (*.csv)")
            if not path:
                return None
        try:
            p = write_stokes_csv(self._rows, path, now=now)
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"导出 CSV 失败：{err}")
            self.logged.emit("ERROR", f"导出斯托克斯 CSV 失败：{err}")
            return None
        if p is None:                        # 防御：空表（正常走不到）
            self.status.emit("对比表为空：未生成文件")
            return None
        self._last_csv = p
        self.lbl_csv.setText(f"导出：{p}")
        self.status.emit(f"已导出 {len(self._rows)} 行 → {p}")
        self.logged.emit("INFO", f"斯托克斯对比表已导出：{p}（{len(self._rows)} 行）")
        return p

    # ── FR-41.5 桥接：③「在分析页记录」预填（不自动入表） ──
    @staticmethod
    def _find_index(items: list, want) -> Optional[int]:
        target = str(want)
        try:
            target_name = Path(target).name
        except (TypeError, ValueError):
            target_name = target
        for i, s in enumerate(items or []):
            p = str(getattr(s, "path", ""))
            if not p:
                continue
            if p == target:
                return i
            try:
                if Path(p).name == target_name:
                    return i
            except (TypeError, ValueError):
                continue
        return None

    def prefill_pair(self, abs_path, em_path) -> bool:
        """预选一对（③→②桥接）：必要时同步补载这 2 个文件；**不自动入表**。"""
        ia = self._find_index(self._abs, abs_path)
        ie = self._find_index(self._em, em_path)
        if ia is None or ie is None:
            try:
                from core.inputs.io import load_spectra_files
                specs = load_spectra_files([Path(str(abs_path)),
                                            Path(str(em_path))], self.cfg)
                known = {str(getattr(s, "path", "")) for s in self.spectra}
                for s in specs:
                    if str(getattr(s, "path", "")) not in known:
                        self.spectra.append(s)
                self._populate_lists()
                self.logged.emit("INFO", "③桥接：已补载一对光谱到斯托克斯卡")
            except Exception as err:                   # noqa: BLE001
                log.warning("斯托克斯桥接补载失败：%s", err)
            ia = self._find_index(self._abs, abs_path)
            ie = self._find_index(self._em, em_path)
        if ia is None or ie is None:
            msg = "发送到图件页失败：找不到匹配的吸收/发射谱（kind 不符或文件无效）"
            self.status.emit(msg)
            self.logged.emit("WARNING", msg)
            return False
        for lst, row in ((self.lst_abs, ia), (self.lst_em, ie)):
            lst.blockSignals(True)
            try:
                lst.setCurrentRow(int(row))
            finally:
                lst.blockSignals(False)
        self._update_preview()
        self.status.emit(f"已预填配对：{Path(str(abs_path)).name} × "
                         f"{Path(str(em_path)).name}（点「加入对比表」确认）")
        return True


# ═══════════════════ V2.6 G 道：光物理表征（FR-42/43/44） ═══════════════════
#: FR-44 表征总表列（唯一来源 = core/report/characterization.py；未落地时兜底同序）
