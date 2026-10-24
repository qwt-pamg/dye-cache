"""② 光物理表征任务卡（V2.6 FR-42–45）"""

from __future__ import annotations

import logging

import importlib
import math
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDoubleSpinBox
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QTabWidget
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.paths import app_root
from core.paths import resolve_out_root
from core.report.stokes_table import group_from_path
from ui.pages.base_page import PageBase
from ui.widgets.live_plot import LivePlotCanvas

log = logging.getLogger("dye_tool.ui.analysis")
from core.report.stokes_table import row_from_spectra
from ui import theme

_CHAR_COLUMNS_FALLBACK = (
    "条件/溶剂", "化合物/组别", "吸收谱文件", "发射谱文件",
    "λabs(nm)", "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)",
    "ε(M⁻¹cm⁻¹)", "φ", "brightness(M⁻¹cm⁻¹)",
    "k_photo(min⁻¹)", "t½(min)", "备注",
)
#: 表列 → 行字段键（与 characterization.CharacterizationRow 同键，FR-44 原序）
_CHAR_KEYS = ("condition", "compound", "abs_file", "em_file",
              "lam_abs", "lam_em", "delta_lambda", "delta_wavenumber",
              "epsilon", "phi", "brightness", "k_photo", "t_half", "note")
_CHAR_NUMERIC_KEYS = frozenset(("lam_abs", "lam_em", "delta_lambda",
                                "delta_wavenumber", "epsilon", "phi",
                                "brightness", "k_photo", "t_half"))
_CHAR_FMT = {
    "lam_abs": ".1f", "lam_em": ".1f", "delta_lambda": ".1f",
    "delta_wavenumber": ".0f", "epsilon": ".4g", "phi": ".3g",
    "brightness": ".4g", "k_photo": ".4g", "t_half": ".2f",
}
_BLEACH_UNITS = (("min", "分钟 (min)"), ("s", "秒 (s)"), ("h", "小时 (h)"))


def _as_float(value, default=float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_num(value, spec: str) -> str:
    """数值单元格文本：非有限 → 空串（FR-44「缺项留空」）。"""
    return "" if not _finite(value) else format(float(value), spec)


def _fmt_sig3(value) -> str:
    return "—" if not _finite(value) else f"{float(value):.3g}"


def _load_optional(dotted: str):
    """可选依赖懒导入（G-A 核心模块尚未落地时不阻塞 UI 构建）。"""
    return importlib.import_module(dotted)


def _spin(lo: float, hi: float, val: float, step: float = 1.0,
          dec: int = 2, suffix: str = "") -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(float(lo), float(hi))
    w.setDecimals(int(dec))
    w.setSingleStep(float(step))
    w.setValue(float(val))
    if suffix:
        w.setSuffix(suffix)
    return w


def _empty_char_row() -> dict:
    row = {k: "" for k in _CHAR_KEYS}
    for k in _CHAR_NUMERIC_KEYS:
        row[k] = float("nan")
    return row


class PhotophysicsPanel(PageBase):
    """V2.6 G 道 FR-42/43/44：②分析页第 7 卡「光物理表征」。

    三个子页（全部 **on-demand**，不进流水线、不写 cfg）：

    * 量子产率：φ（相对法，复用 core.analysis.fluorescence_qy）+ 亮度 = ε×φ；
    * 光漂白：时间序列谱 / 两列 CSV → 单指数拟合（复用 core.analysis.photobleach）；
    * 表征总表：FR-44 列超集，行=化合物×条件，可编辑/排序/过滤/导出
      （导出走 core.report.characterization；与 FR-41 Stokes 表同一模块，Q-E19）。

    红线：不改 Φ_Δ/ε/动力学算法、零新配置键、不自动配对/自动入表。
    """

    TITLE = "光物理表征（φ / 光漂白 / 表征总表）"
    SUBTITLE = ("论文数据链：相对法荧光量子产率 φ + 亮度；体外/成像光漂白单指数；"
                "多条件表征总表（与 Stokes 表同一模块）。全部按需计算，不改动配置。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        # ── 量子产率（φ）子页状态 ──
        self._phi_spectra: list = []
        self._phi_abs: list = []
        self._phi_em: list = []
        self._last_phi: dict = {}
        self._eps_source = "手填"
        # ── 光漂白子页状态 ──
        self._bleach_spectra: list = []
        self._bleach_t: list = []
        self._bleach_y: list = []
        self._bleach_label = ""
        self._last_bleach: dict = {}
        self._bleach_runs: list = []
        # ── 表征总表状态（会话级台账） ──
        self._rows: list = []
        self._view: list = []
        self._filling = False
        self._sort_key: Optional[str] = None
        self._sort_desc = False
        self._filter_condition = ""
        self._filter_compound = ""
        self._last_csv: Optional[Path] = None
        self._last_xlsx: Optional[Path] = None
        #: 外部数据源（AnalysisPage 注入）：既有 ε 结果 / FR-41 Stokes 行
        self._eps_provider = None
        self._stokes_provider = None
        self._build()

    # ── 构建 ──
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        self.tabs = QTabWidget()
        self.tab_phi = QWidget()
        self.tab_bleach = QWidget()
        self.tab_char = QWidget()
        self.tabs.addTab(self.tab_phi, "量子产率 φ")
        self.tabs.addTab(self.tab_bleach, "光漂白")
        self.tabs.addTab(self.tab_char, "表征总表")
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.progress_row())
        self._build_phi_tab()
        self._build_bleach_tab()
        self._build_char_tab()

    # ── 子页 1：量子产率 φ（FR-42） ──
    def _build_phi_tab(self) -> None:
        lay = QVBoxLayout(self.tab_phi)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        self.btn_phi_dir = QPushButton("选择光谱目录…")
        self.lbl_phi_dir = QLabel("未选择目录")
        theme.mark(self.lbl_phi_dir, "muted")
        bar.addWidget(self.btn_phi_dir)
        bar.addWidget(self.lbl_phi_dir, 1)
        lay.addLayout(bar)
        hint = QLabel("相对法：φ = φ_ref × (I/I_ref) × (A_ref/A) × (n²/n_ref²)；"
                      "I = 发射谱积分强度，A = λex 处吸光度（建议 A ≤ 0.05）。"
                      "数值全部复用 core.analysis.fluorescence_qy，不重写公式。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        split = QSplitter(Qt.Horizontal)
        self.lst_phi_abs = QListWidget()
        self.lst_phi_em = QListWidget()
        for lst, title in ((self.lst_phi_abs, "吸收谱（kind=abs）"),
                           (self.lst_phi_em, "发射谱（kind=em）")):
            lst.setSelectionMode(QAbstractItemView.SingleSelection)
            lst.setAlternatingRowColors(True)
            box = QGroupBox(title)
            vb = QVBoxLayout(box)
            vb.addWidget(lst)
            split.addWidget(box)
        split.setSizes([380, 380])
        lay.addWidget(split, 2)

        box = QGroupBox("测量条件与参比")
        form = QFormLayout(box)
        self.sp_lam_ex = _spin(200.0, 1200.0, 450.0, 1.0, 1, " nm")
        self.sp_phi_ref = _spin(0.0, 5.0, 0.54, 0.01, 4)
        self.sp_i_ref = _spin(0.0, 1.0e12, 1.0, 0.1, 6)
        self.sp_a_ref = _spin(0.0, 10.0, 0.05, 0.01, 4)
        self.sp_n_ref = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_n = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_eps = _spin(0.0, 1.0e12, 0.0, 1000.0, 3)
        self.btn_eps_from = QPushButton("从 ε 结果填入")
        self.btn_eps_from.setToolTip("从②分析页「摩尔消光系数 ε」卡最近一次成功结果取 ε")
        self.lbl_eps_source = QLabel("ε 来源：手填")
        theme.mark(self.lbl_eps_source, "hint")
        eps_row = QHBoxLayout()
        eps_row.addWidget(self.sp_eps, 1)
        eps_row.addWidget(self.btn_eps_from)
        eps_row.addWidget(self.lbl_eps_source)
        eps_wrap = QWidget()
        eps_wrap.setLayout(eps_row)
        self.ed_cond_phi = QLineEdit()
        self.ed_cond_phi.setPlaceholderText("如 PBS / MeOH（缺项留空）")
        self.ed_comp_phi = QLineEdit()
        self.ed_comp_phi.setPlaceholderText("默认取吸收文件名组别；可手改")
        form.addRow("激发波长 λex", self.sp_lam_ex)
        self.lbl_lam_note = QLabel(
            "λex 只需落在吸收谱范围；发射积分取发射谱自身范围"
            "（仪器 EM 扫描可从 λex 之后开始，V2.6 修正）")
        theme.mark(self.lbl_lam_note, "hint")
        self.lbl_lam_note.setWordWrap(True)
        form.addRow("", self.lbl_lam_note)
        from ui.help_texts import PARAM_HELP
        for _key, _w in (("excitation", self.sp_lam_ex),
                         ("phi_ref", self.sp_phi_ref), ("I_ref", self.sp_i_ref),
                         ("A_ref", self.sp_a_ref), ("n_ref", self.sp_n_ref),
                         ("epsilon", self.sp_eps), ("n", self.sp_n),
                         ("condition", self.ed_cond_phi),
                         ("compound", self.ed_comp_phi)):
            if _key in PARAM_HELP:
                _w.setToolTip(PARAM_HELP[_key])
        eps_wrap.setToolTip(PARAM_HELP["epsilon"])
        form.addRow("参比 φ_ref", self.sp_phi_ref)
        form.addRow("参比积分 I_ref", self.sp_i_ref)
        form.addRow("参比 A_ref", self.sp_a_ref)
        form.addRow("参比折射率 n_ref", self.sp_n_ref)
        form.addRow("样品折射率 n", self.sp_n)
        form.addRow("ε (M⁻¹cm⁻¹)", eps_wrap)
        form.addRow("条件/溶剂", self.ed_cond_phi)
        form.addRow("化合物/组别", self.ed_comp_phi)
        lay.addWidget(box)

        preview = QHBoxLayout()
        self.lbl_phi_a = QLabel("A(λex) = —")
        self.lbl_phi_i = QLabel("I = —")
        self.lbl_phi = QLabel("φ = —")
        self.lbl_brightness = QLabel("亮度 = —")
        for lb in (self.lbl_phi_a, self.lbl_phi_i, self.lbl_phi, self.lbl_brightness):
            theme.mark(lb, "strong")
            preview.addWidget(lb)
        preview.addStretch(1)
        lay.addLayout(preview)
        self.lbl_phi_warn = QLabel("")
        self.lbl_phi_warn.setWordWrap(True)
        theme.mark(self.lbl_phi_warn, "warnHint")
        lay.addWidget(self.lbl_phi_warn)
        self.lbl_phi_msg = QLabel("未选择配对：请在左右列表各选一支谱")
        self.lbl_phi_msg.setWordWrap(True)
        theme.mark(self.lbl_phi_msg, "muted")
        lay.addWidget(self.lbl_phi_msg)
        row = QHBoxLayout()
        self.btn_compute_phi = QPushButton("计算 φ")
        self.btn_add_phi = QPushButton("加入表征表")
        self.btn_add_phi.setEnabled(False)
        row.addWidget(self.btn_compute_phi)
        row.addWidget(self.btn_add_phi)
        row.addStretch(1)
        lay.addLayout(row)

        self.btn_phi_dir.clicked.connect(self._choose_phi_dir)
        self.btn_compute_phi.clicked.connect(lambda *_: self.compute_phi())
        self.btn_add_phi.clicked.connect(lambda *_: self.add_phi_to_table())
        self.btn_eps_from.clicked.connect(lambda *_: self._on_eps_from_result())
        for lst in (self.lst_phi_abs, self.lst_phi_em):
            lst.currentRowChanged.connect(lambda *_: self._phi_preview())
        for sp in (self.sp_lam_ex, self.sp_phi_ref, self.sp_i_ref,
                   self.sp_a_ref, self.sp_n_ref, self.sp_n, self.sp_eps):
            sp.valueChanged.connect(lambda *_: self._phi_preview())

    # ── 子页 2：光漂白（FR-43） ──
    def _build_bleach_tab(self) -> None:
        lay = QVBoxLayout(self.tab_bleach)
        lay.setContentsMargins(8, 8, 8, 8)
        hint = QLabel("单指数衰减 I(t)=I∞+(I0−I∞)·e^(−k·t)；可关平台项退化为 I0·e^(−k·t)。"
                      "时间统一换算为分钟；k/t½/loss%/R² 复用 core.analysis.photobleach。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("输入模式"))
        self.cmb_bleach_src = QComboBox()
        self.cmb_bleach_src.addItem("① 时间序列谱目录", "series")
        self.cmb_bleach_src.addItem("② 两列 CSV（时间,强度）", "csv")
        src_row.addWidget(self.cmb_bleach_src)
        src_row.addStretch(1)
        lay.addLayout(src_row)

        self.box_series = QGroupBox("时间序列谱目录")
        sl = QHBoxLayout(self.box_series)
        self.btn_bleach_dir = QPushButton("选择谱目录…")
        self.lbl_bleach_dir = QLabel("未选择目录")
        self.cmb_bleach_value = QComboBox()
        self.cmb_bleach_value.addItem("峰值吸光度", "peak_abs")
        self.cmb_bleach_value.addItem("积分强度", "integral")
        sl.addWidget(self.btn_bleach_dir)
        sl.addWidget(self.lbl_bleach_dir, 1)
        sl.addWidget(QLabel("取值"))
        sl.addWidget(self.cmb_bleach_value)
        lay.addWidget(self.box_series)

        self.box_csv = QGroupBox("两列 CSV（时间,强度；外部成像 ROI 导出）")
        cl = QHBoxLayout(self.box_csv)
        self.btn_bleach_csv = QPushButton("选择 CSV…")
        self.lbl_bleach_csv = QLabel("未选择文件")
        cl.addWidget(self.btn_bleach_csv)
        cl.addWidget(self.lbl_bleach_csv, 1)
        lay.addWidget(self.box_csv)

        opt = QHBoxLayout()
        self.chk_plateau = QCheckBox("含平台项 I∞")
        self.chk_plateau.setChecked(True)
        self.chk_norm = QCheckBox("归一化对比")
        self.chk_norm.setChecked(True)
        self.cmb_unit = QComboBox()
        for unit, label in _BLEACH_UNITS:
            self.cmb_unit.addItem(label, unit)
        opt.addWidget(self.chk_plateau)
        opt.addWidget(self.chk_norm)
        opt.addWidget(QLabel("输入时间单位"))
        opt.addWidget(self.cmb_unit)
        opt.addStretch(1)
        lay.addLayout(opt)

        ident = QHBoxLayout()
        self.ed_cond_bleach = QLineEdit()
        self.ed_cond_bleach.setPlaceholderText("条件/溶剂（入表用；缺项留空）")
        self.ed_comp_bleach = QLineEdit()
        self.ed_comp_bleach.setPlaceholderText("化合物/组别（默认取目录/文件名）")
        ident.addWidget(QLabel("条件/溶剂"))
        ident.addWidget(self.ed_cond_bleach, 1)
        ident.addWidget(QLabel("化合物/组别"))
        ident.addWidget(self.ed_comp_bleach, 1)
        lay.addLayout(ident)

        preview = QHBoxLayout()
        self.lbl_bleach_k = QLabel("k = —")
        self.lbl_bleach_thalf = QLabel("t½ = —")
        self.lbl_bleach_loss = QLabel("loss = —")
        self.lbl_bleach_r2 = QLabel("R² = —")
        for lb in (self.lbl_bleach_k, self.lbl_bleach_thalf,
                   self.lbl_bleach_loss, self.lbl_bleach_r2):
            theme.mark(lb, "strong")
            preview.addWidget(lb)
        preview.addStretch(1)
        lay.addLayout(preview)
        self.lbl_bleach_msg = QLabel("请先载入时间序列谱目录或两列 CSV")
        self.lbl_bleach_msg.setWordWrap(True)
        theme.mark(self.lbl_bleach_msg, "muted")
        lay.addWidget(self.lbl_bleach_msg)

        row = QHBoxLayout()
        self.btn_fit_bleach = QPushButton("拟合 / 预览")
        self.btn_add_bleach = QPushButton("加入表征表")
        self.btn_add_bleach.setEnabled(False)
        self.btn_export_decay = QPushButton("导出衰减图…")
        self.btn_export_decay.setToolTip(
            "用 core/render/photophysics 的新图型出口（未落地时提示暂不可用）")
        self.btn_export_evolution = QPushButton("导出光谱演化图…")
        self.btn_export_evolution.setToolTip(
            "时间序列谱 → 光谱演化图（时间序蓝系渐变 + 首末包络 + t=0 高亮）")
        row.addWidget(self.btn_fit_bleach)
        row.addWidget(self.btn_add_bleach)
        row.addWidget(self.btn_export_decay)
        row.addWidget(self.btn_export_evolution)
        row.addStretch(1)
        lay.addLayout(row)

        self.bleach_plot = LivePlotCanvas(figsize=(6.0, 3.0), dpi=90)
        lay.addWidget(self.bleach_plot, 3)

        self.btn_bleach_dir.clicked.connect(self._choose_bleach_dir)
        self.btn_bleach_csv.clicked.connect(self._choose_bleach_csv)
        self.btn_fit_bleach.clicked.connect(lambda *_: self.compute_bleach())
        self.btn_add_bleach.clicked.connect(lambda *_: self.add_bleach_to_table())
        self.btn_export_decay.clicked.connect(lambda *_: self.export_decay_figure())
        self.btn_export_evolution.clicked.connect(
            lambda *_: self.export_evolution_figure())
        self.cmb_bleach_src.currentIndexChanged.connect(self._on_bleach_src_changed)
        self.cmb_bleach_value.currentIndexChanged.connect(
            lambda *_: self._reload_bleach_series())
        self._on_bleach_src_changed()

    # ── 子页 3：表征总表（FR-44） ──
    def _build_char_tab(self) -> None:
        lay = QVBoxLayout(self.tab_char)
        lay.setContentsMargins(8, 8, 8, 8)
        hint = QLabel("行 = 化合物 × 条件；缺项留空可编辑。与 Stokes 表同一模块"
                      "（core/report/characterization.py），可导入 Stokes 行后统一导出。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        filt = QHBoxLayout()
        filt.addWidget(QLabel("条件过滤"))
        self.cmb_filter_condition = QComboBox()
        self.cmb_filter_condition.addItem("全部", "")
        filt.addWidget(self.cmb_filter_condition)
        filt.addWidget(QLabel("化合物过滤"))
        self.cmb_filter_compound = QComboBox()
        self.cmb_filter_compound.addItem("全部", "")
        filt.addWidget(self.cmb_filter_compound)
        filt.addStretch(1)
        lay.addLayout(filt)

        row = QHBoxLayout()
        self.btn_char_delete = QPushButton("删除选中")
        self.btn_char_clear = QPushButton("清空")
        self.btn_char_import = QPushButton("从 Stokes 表导入")
        self.btn_char_csv = QPushButton("导出 CSV…")
        self.btn_char_xlsx = QPushButton("导出 Excel…")
        self.btn_char_figure = QPushButton("导出柱状图…")
        self.btn_char_figure.setToolTip("用 core/render/photophysics 的新图型出口（未落地时提示暂不可用）")
        for b in (self.btn_char_delete, self.btn_char_clear,
                  self.btn_char_import, self.btn_char_csv,
                  self.btn_char_xlsx, self.btn_char_figure):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        try:
            cols = list(_load_optional(
                "core.report.characterization").CHARACTERIZATION_COLUMNS)
        except Exception:                              # noqa: BLE001 —— G 核心未落地
            cols = list(_CHAR_COLUMNS_FALLBACK)
        self._columns = cols
        self.tbl = QTableWidget(0, len(cols))
        self.tbl.setObjectName("charTable")
        self.tbl.setHorizontalHeaderLabels([str(c) for c in cols])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.DoubleClicked
                                 | QAbstractItemView.EditKeyPressed)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)
        hdr.sectionClicked.connect(self._on_char_header_clicked)
        for c in range(len(cols)):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
        self.tbl.setColumnWidth(0, 110)
        self.tbl.setColumnWidth(1, 120)
        self.tbl.setColumnWidth(2, 150)
        self.tbl.setColumnWidth(3, 150)
        self.tbl.itemChanged.connect(self._on_char_item_changed)
        lay.addWidget(self.tbl, 1)
        self.lbl_char_export = QLabel("导出：—")
        self.lbl_char_export.setWordWrap(True)
        theme.mark(self.lbl_char_export, "hint")
        lay.addWidget(self.lbl_char_export)

        self.btn_char_delete.clicked.connect(lambda *_: self.delete_selected_rows())
        self.btn_char_clear.clicked.connect(lambda *_: self.clear_rows())
        self.btn_char_import.clicked.connect(lambda *_: self.import_stokes_rows())
        self.btn_char_csv.clicked.connect(lambda *_: self.export_csv())
        self.btn_char_xlsx.clicked.connect(lambda *_: self.export_xlsx())
        self.btn_char_figure.clicked.connect(lambda *_: self.export_bar_figure())
        self.cmb_filter_condition.currentIndexChanged.connect(
            lambda *_: self._refresh_char_view())
        self.cmb_filter_compound.currentIndexChanged.connect(
            lambda *_: self._refresh_char_view())
        self._rebuild_char_table()

    # ── 外部数据源注入（AnalysisPage 接线；均为只读回调） ──
    def set_eps_provider(self, provider) -> None:
        """注入「最近一次 ε 结果」回调（A42.2：亮度=ε×φ 的 ε 来源明示）。"""
        self._eps_provider = provider

    def set_stokes_provider(self, provider) -> None:
        """注入 FR-41 Stokes 行回调（FR-44：两表同一模块，可导入后统一导出）。"""
        self._stokes_provider = provider

    def set_default_dir(self, data_dir) -> None:
        """批次目录作为默认候选目录（不自动载入：本卡保持 on-demand）。"""
        if data_dir:
            self.data_dir = Path(data_dir)
            if not self._phi_spectra:
                self.lbl_phi_dir.setText(str(self.data_dir))
            if not self._bleach_t:
                self.lbl_bleach_dir.setText(str(self.data_dir))

    # ══════════════ 量子产率 φ（FR-42） ══════════════
    def _choose_phi_dir(self) -> None:
        start = str(self.data_dir or (self.cfg.get("gui") or {}).get("last_dir")
                    or app_root())
        d = QFileDialog.getExistingDirectory(self, "选择光谱目录（φ）", start)
        if d:
            self.remember_dir(Path(d))
            self.set_phi_data_dir(Path(d), force=True)

    def set_phi_data_dir(self, data_dir, *, force: bool = False) -> None:
        """选择光谱目录 → run_task 载入吸收/发射候选（忙禁用、可取消）。"""
        d = Path(data_dir)
        if not force and d == self.data_dir and self._phi_spectra:
            return
        self.data_dir = d
        self.lbl_phi_dir.setText(str(d))
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.cfg)
        except Exception as err:                       # noqa: BLE001
            files = []
            log.warning("枚举 φ 候选目录失败：%s", err)
        if not files:
            self._phi_spectra = []
            self._phi_abs = []
            self._phi_em = []
            self._populate_phi_lists()
            self.status.emit(f"{d} 下没有可解析的光谱文件")
            self.logged.emit("WARNING", f"{d} 下没有找到光谱文件")
            return
        from core.inputs.io import load_spectra_files

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            spectra = load_spectra_files(files, self.cfg)
            progress_cb(90, "载入完成")
            return spectra

        self.run_task(task, "载入光谱（φ）", self._on_phi_loaded,
                      busy_widgets=[self.btn_phi_dir, self.lst_phi_abs,
                                    self.lst_phi_em])

    def _on_phi_loaded(self, spectra) -> None:
        self._phi_spectra = list(spectra or [])
        fails = [s for s in self._phi_spectra
                 if not getattr(s, "ok", True)]
        for s in fails[:20]:
            name = Path(str(getattr(s, "path", ""))).name
            self.logged.emit("WARNING",
                             f"剔除 {name}：{getattr(s, 'excluded', '') or '读取失败'}")
        self._populate_phi_lists()
        n_abs, n_em = len(self._phi_abs), len(self._phi_em)
        self.lbl_phi_dir.setText(f"{self.data_dir}（吸收 {n_abs} / 发射 {n_em}"
                                 + (f" / 失败 {len(fails)}" if fails else "") + "）")
        self.status.emit(f"φ 候选已载入：吸收 {n_abs} 支 / 发射 {n_em} 支")
        self.logged.emit("INFO", f"φ 候选载入：吸收 {n_abs} 支，发射 {n_em} 支，"
                                 f"剔除 {len(fails)} 支")

    def _populate_phi_lists(self) -> None:
        self._phi_abs = [s for s in self._phi_spectra
                         if getattr(s, "ok", True)
                         and getattr(s, "kind", "") == "abs"]
        self._phi_em = [s for s in self._phi_spectra
                        if getattr(s, "ok", True)
                        and getattr(s, "kind", "") == "em"]
        for lst, items in ((self.lst_phi_abs, self._phi_abs),
                           (self.lst_phi_em, self._phi_em)):
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
                if items:
                    lst.setCurrentRow(0)
            finally:
                lst.blockSignals(False)
        self._phi_preview()

    def _phi_selected(self):
        a = self._phi_abs[self.lst_phi_abs.currentRow()] \
            if 0 <= self.lst_phi_abs.currentRow() < len(self._phi_abs) else None
        e = self._phi_em[self.lst_phi_em.currentRow()] \
            if 0 <= self.lst_phi_em.currentRow() < len(self._phi_em) else None
        return a, e

    def _phi_preview(self) -> dict:
        """即时刷新 A/I/φ/亮度预览（计算口径全部来自 fluorescence_qy）。"""
        self._last_phi = {}
        a, e = self._phi_selected()
        self.btn_add_phi.setEnabled(False)
        if a is None or e is None:
            for lb, txt in ((self.lbl_phi_a, "A(λex) = —"), (self.lbl_phi_i, "I = —"),
                            (self.lbl_phi, "φ = —"),
                            (self.lbl_brightness, "亮度 = —")):
                lb.setText(txt)
            self.lbl_phi_warn.setText("")
            self.lbl_phi_msg.setText("未选择配对：请在左右列表各选一支吸收谱与发射谱")
            return {}
        try:
            qy = _load_optional("core.analysis.fluorescence_qy")
        except Exception:                              # noqa: BLE001 —— G-A 未落地
            self.lbl_phi_msg.setText(
                "核心模块 core.analysis.fluorescence_qy 尚未落地：φ 暂不可算"
                "（候选与参数已就绪，落地后自动可用）")
            self.lbl_phi_warn.setText("")
            return {}
        try:
            res = qy.compute_fluorescence_qy(
                a, e, lam_ex=float(self.sp_lam_ex.value()),
                i_ref=float(self.sp_i_ref.value()),
                a_ref=float(self.sp_a_ref.value()),
                phi_ref=float(self.sp_phi_ref.value()),
                n_ref=float(self.sp_n_ref.value()),
                n=float(self.sp_n.value()))
        except Exception as err:                       # noqa: BLE001
            res = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        self._last_phi = dict(res or {})
        self._apply_phi_result(self._last_phi)
        return self._last_phi

    def _apply_phi_result(self, res: dict) -> None:
        ok = bool((res or {}).get("ok"))
        if not ok:
            for lb, txt in ((self.lbl_phi_a, "A(λex) = —"), (self.lbl_phi_i, "I = —"),
                            (self.lbl_phi, "φ = —"),
                            (self.lbl_brightness, "亮度 = —")):
                lb.setText(txt)
            self.lbl_phi_warn.setText("")
            msg = (res or {}).get("error") or (res or {}).get("reason") or "无法计算"
            self.lbl_phi_msg.setText(f"不可计算：{msg}")
            self.btn_add_phi.setEnabled(False)
            return
        a_ex, i_em = res.get("a_ex"), res.get("i_em")
        phi = res.get("phi")
        self.lbl_phi_a.setText(f"A(λex) = {_as_float(a_ex):.4f}"
                               if _finite(a_ex) else "A(λex) = —")
        self.lbl_phi_i.setText(f"I = {_as_float(i_em):.6g}"
                               if _finite(i_em) else "I = —")
        display = res.get("phi_display") or _fmt_sig3(phi)
        self.lbl_phi.setText(f"φ = {display}")
        eps = float(self.sp_eps.value())
        br = float("nan")
        if eps > 0 and _finite(phi):
            try:
                qy = _load_optional("core.analysis.fluorescence_qy")
                br = float(qy.brightness(eps, phi))
            except Exception:                          # noqa: BLE001 —— 退化为同义乘法
                br = eps * float(phi)
        self.lbl_brightness.setText(
            f"亮度 = {br:.4g} M⁻¹cm⁻¹" if _finite(br) else "亮度 = —（请填 ε）")
        warnings = [str(w) for w in (res.get("warnings") or [])]
        self.lbl_phi_warn.setText("；".join(warnings))
        self.lbl_phi_msg.setText("可「加入表征表」（不自动入表；ε 来源见上方）")
        self.btn_add_phi.setEnabled(True)

    def _on_eps_from_result(self) -> None:
        res = {}
        provider = self._eps_provider
        if callable(provider):
            try:
                res = dict(provider() or {})
            except Exception as err:                    # noqa: BLE001
                log.warning("读取既有 ε 结果失败：%s", err)
                res = {}
        eps = _as_float(res.get("eps"))
        if _finite(eps) and eps > 0:
            self.sp_eps.setValue(eps)
            self._eps_source = "既有 ε 结果"
            unit = str(res.get("eps_unit") or "M⁻¹cm⁻¹")
            self.lbl_eps_source.setText(f"ε 来源：既有 ε 结果（{eps:.4g} {unit}）")
            self.status.emit("已从「摩尔消光系数 ε」结果填入 ε")
            self._phi_preview()
            return
        self.status.emit("没有可用的 ε 结果：请先在「摩尔消光系数 ε」卡成功计算一次，或手填 ε")
        self.logged.emit("WARNING", "光物理表征：没有可用的既有 ε 结果")

    def compute_phi(self, checked: bool = False):       # noqa: ANN001 —— Qt 槽兼容
        """显式计算并刷新 φ/亮度预览；返回结果 dict（失败 ok=False + 中文原因）。"""
        _ = checked
        return dict(self._phi_preview() or {})

    def add_phi_to_table(self) -> bool:
        """把当前 φ 结果落一行到表征总表（显式动作；不自动入表）。"""
        res = self._last_phi or {}
        if not res.get("ok"):
            self.status.emit("请先成功计算 φ 再加入表征表")
            return False
        a, e = self._phi_selected()
        if a is None or e is None:
            return False
        try:
            sr = row_from_spectra(a, e)
            lam_a, lam_e = sr.lam_abs, sr.lam_em
            d_lam, d_nu = sr.delta_lambda, sr.delta_wavenumber
            group = sr.group
        except Exception as err:                       # noqa: BLE001 —— 退化不阻断
            log.warning("φ 入表取峰失败：%s", err)
            lam_a = lam_e = d_lam = d_nu = float("nan")
            group = group_from_path(getattr(a, "path", ""), self.cfg)
        eps = float(self.sp_eps.value())
        phi = res.get("phi")
        br = float("nan")
        if eps > 0 and _finite(phi):
            try:
                br = float(_load_optional(
                    "core.analysis.fluorescence_qy").brightness(eps, phi))
            except Exception:                          # noqa: BLE001
                br = eps * float(phi)
        row = _empty_char_row()
        row.update({
            "condition": self.ed_cond_phi.text().strip(),
            "compound": self.ed_comp_phi.text().strip() or group,
            "abs_file": Path(str(getattr(a, "path", ""))).name,
            "em_file": Path(str(getattr(e, "path", ""))).name,
            "lam_abs": lam_a, "lam_em": lam_e,
            "delta_lambda": d_lam, "delta_wavenumber": d_nu,
            "epsilon": eps if eps > 0 else float("nan"),
            "phi": float(phi) if _finite(phi) else float("nan"),
            "brightness": br,
            "note": "；".join([str(w) for w in (res.get("warnings") or [])]),
        })
        self.add_row(row)
        self.tabs.setCurrentWidget(self.tab_char)
        return True

    # ══════════════ 光漂白（FR-43） ══════════════
    def _on_bleach_src_changed(self, *_args) -> None:     # noqa: ANN002 —— Qt 槽
        src = self.cmb_bleach_src.currentData()
        self.box_series.setVisible(src == "series")
        self.box_csv.setVisible(src == "csv")

    def _choose_bleach_dir(self) -> None:
        start = str(self.data_dir or (self.cfg.get("gui") or {}).get("last_dir")
                    or app_root())
        d = QFileDialog.getExistingDirectory(self, "选择时间序列谱目录", start)
        if d:
            self.remember_dir(Path(d))
            self.set_bleach_data_dir(Path(d), force=True)

    def set_bleach_data_dir(self, data_dir, *, force: bool = False) -> None:
        d = Path(data_dir)
        if not force and d == self.data_dir and self._bleach_spectra:
            return
        self.data_dir = d
        self.lbl_bleach_dir.setText(str(d))
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.cfg)
        except Exception as err:                       # noqa: BLE001
            files = []
            log.warning("枚举报漂白目录失败：%s", err)
        if not files:
            self._bleach_spectra = []
            self._bleach_t = []
            self._bleach_y = []
            self.status.emit(f"{d} 下没有可解析的光谱文件")
            return
        from core.inputs.io import load_spectra_files

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            return load_spectra_files(files, self.cfg)

        self.run_task(task, "载入时间序列", self._on_bleach_loaded,
                      busy_widgets=[self.btn_bleach_dir])

    def _on_bleach_loaded(self, spectra) -> None:
        self._bleach_spectra = list(spectra or [])
        fails = [s for s in self._bleach_spectra
                 if not getattr(s, "ok", True)]
        for s in fails[:20]:
            self.logged.emit("WARNING",
                             f"剔除 {Path(str(getattr(s, 'path', ''))).name}："
                             f"{getattr(s, 'excluded', '') or '读取失败'}")
        n = len(self._bleach_spectra) - len(fails)
        self.lbl_bleach_dir.setText(f"{self.data_dir}（可用 {n} 支）")
        self._reload_bleach_series()

    def _reload_bleach_series(self) -> None:
        if not getattr(self, "_bleach_spectra", None):
            return
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001 —— G-A 未落地
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：光漂白拟合暂不可用")
            return
        mode = self.cmb_bleach_value.currentData() or "peak_abs"
        try:
            t, y = ble.series_from_spectra(self._bleach_spectra, mode=mode)
            self._bleach_t = [float(v) for v in t]
            self._bleach_y = [float(v) for v in y]
        except Exception as err:                       # noqa: BLE001
            self._bleach_t = []
            self._bleach_y = []
            self.lbl_bleach_msg.setText(f"时间序列提取失败：{err}")
            self.logged.emit("WARNING", f"光漂白时间序列提取失败：{err}")
            return
        self._bleach_label = (self.data_dir.name if self.data_dir
                              else "时间序列")
        self.lbl_bleach_msg.setText(
            f"已载入 {len(self._bleach_y)} 个时间点（{mode}）→ 点「拟合 / 预览」")

    def _choose_bleach_csv(self) -> None:
        start = str(self.data_dir or app_root())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择两列 CSV（时间,强度）", start, "CSV 文件 (*.csv *.txt);;所有文件 (*)")
        if path:
            self.load_bleach_csv(Path(path))

    def load_bleach_csv(self, path) -> bool:
        """载入成像 ROI 两列 CSV（时间,强度）；失败给中文原因不崩。"""
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：CSV 导入暂不可用")
            return False
        try:
            t, y = ble.read_two_column_csv(Path(str(path)))
        except ValueError as err:
            self.lbl_bleach_msg.setText(f"CSV 读取失败：{err}")
            self.status.emit(f"CSV 读取失败：{err}")
            return False
        except Exception as err:                       # noqa: BLE001
            self.lbl_bleach_msg.setText(f"CSV 读取失败：{type(err).__name__}: {err}")
            return False
        self._bleach_t = [float(v) for v in t]
        self._bleach_y = [float(v) for v in y]
        self._bleach_label = Path(str(path)).name
        self.lbl_bleach_csv.setText(str(path))
        self.lbl_bleach_msg.setText(
            f"已载入 CSV：{len(self._bleach_y)} 个点 → 点「拟合 / 预览」")
        self.status.emit(f"已载入光漂白 CSV：{Path(str(path)).name}")
        return True

    def compute_bleach(self, checked: bool = False):    # noqa: ANN001 —— Qt 槽兼容
        """单指数拟合并刷新 k/t½/loss%/R² + 衰减曲线（复用 photobleach）。"""
        _ = checked
        if not self._bleach_t:
            self.lbl_bleach_msg.setText("请先载入时间序列谱目录或两列 CSV")
            self.btn_add_bleach.setEnabled(False)
            return {"ok": False, "reason": "没有可拟合的时间序列"}
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：拟合暂不可用")
            return {"ok": False, "reason": "core 未落地"}
        unit = self.cmb_unit.currentData() or "min"
        try:
            res = ble.fit_photobleaching(
                self._bleach_t, self._bleach_y,
                plateau=self.chk_plateau.isChecked(), t_unit=unit)
        except Exception as err:                       # noqa: BLE001
            res = {"ok": False, "reason": f"{type(err).__name__}: {err}"}
        self._last_bleach = dict(res or {})
        self._apply_bleach_result(self._last_bleach)
        return self._last_bleach

    def _bleach_t_minutes(self) -> list:
        unit = self.cmb_unit.currentData() or "min"
        try:
            ble = _load_optional("core.analysis.photobleach")
            return [float(v) for v in ble.time_to_minutes(self._bleach_t, unit)]
        except Exception:                              # noqa: BLE001 —— 退化同值
            return [float(v) for v in self._bleach_t]

    def _apply_bleach_result(self, res: dict) -> None:
        ok = bool((res or {}).get("ok"))
        if not ok:
            for lb, txt in ((self.lbl_bleach_k, "k = —"),
                            (self.lbl_bleach_thalf, "t½ = —"),
                            (self.lbl_bleach_loss, "loss = —"),
                            (self.lbl_bleach_r2, "R² = —")):
                lb.setText(txt)
            self.lbl_bleach_msg.setText(
                "拟合失败：" + str((res or {}).get("reason")
                                   or (res or {}).get("error") or "原因未知"))
            self.btn_add_bleach.setEnabled(False)
            self._plot_bleach(res or {})
            return
        k_min = _as_float(res.get("k_min", res.get("k")))
        t_half = _as_float(res.get("t_half_min", res.get("t_half")))
        loss = _as_float(res.get("loss_pct"))
        r2 = _as_float(res.get("r2"))
        self.lbl_bleach_k.setText(f"k = {k_min:.4g} min⁻¹" if _finite(k_min) else "k = —")
        self.lbl_bleach_thalf.setText(f"t½ = {t_half:.3g} min" if _finite(t_half) else "t½ = —")
        self.lbl_bleach_loss.setText(f"loss = {loss:.1f}%" if _finite(loss) else "loss = —")
        self.lbl_bleach_r2.setText(f"R² = {r2:.4f}" if _finite(r2) else "R² = —")
        issues = [str(i) for i in (res.get("issues") or [])]
        msg = "拟合完成：可「加入表征表」"
        if issues:
            msg += "；" + "；".join(issues)
        self.lbl_bleach_msg.setText(msg)
        self.btn_add_bleach.setEnabled(True)
        # 归一化对比：缓存本次成功结果（同 label 覆盖，最多 6 组）
        try:
            run = {"label": self._bleach_label or "序列",
                   "t": list(self._bleach_t_minutes()),
                   "y": [float(v) for v in self._bleach_y],
                   "res": dict(res)}
            self._bleach_runs = [r for r in self._bleach_runs
                                 if r.get("label") != run["label"]]
            self._bleach_runs.append(run)
            if len(self._bleach_runs) > 6:
                self._bleach_runs = self._bleach_runs[-6:]
        except Exception as err:                       # noqa: BLE001
            log.warning("光漂白对比缓存失败：%s", err)
        self._plot_bleach(res)

    def _plot_bleach(self, res: dict) -> None:
        """衰减散点 + 拟合线（归一化对比可选）；仅显示，不改数值路径。"""
        try:
            t_min = self._bleach_t_minutes()
            y = [float(v) for v in self._bleach_y]
            norm = bool(self.chk_norm.isChecked())
            scale = 1.0
            if norm and y:
                scale = max(y) or 1.0
                y = [v / scale for v in y]
            curves = [{"key": "data", "label": "数据",
                       "x": t_min, "y": y, "color": "#1f4e79"}]
            ok = bool((res or {}).get("ok"))
            if ok:
                k = _as_float(res.get("k_min", res.get("k")))
                i0 = _as_float(res.get("i0"))
                i_inf = _as_float(res.get("i_inf"))
                if _finite(k) and _finite(i0):
                    if not _finite(i_inf):
                        i_inf = 0.0
                    y_fit = []
                    for tv in t_min:
                        try:
                            val = i_inf + (i0 - i_inf) * math.exp(-k * tv)
                        except OverflowError:          # pragma: no cover
                            val = i_inf
                        y_fit.append(val / scale if norm else val)
                    curves.append({"key": "fit", "label": "拟合",
                                   "x": t_min, "y": y_fit, "color": "#C81E1E"})
            if norm:
                for j, run in enumerate(self._bleach_runs[:-1], 1):
                    ry = [float(v) for v in run.get("y") or []]
                    m = max(ry) if ry else 0.0
                    if not m:
                        continue
                    curves.append({"key": f"cmp{j}",
                                   "label": f"对比 {run.get('label')}",
                                   "x": [float(v) for v in run.get("t") or []],
                                   "y": [v / m for v in ry],
                                   "color": "#9ca3af"})
            self.bleach_plot.set_panels([{
                "title": f"光漂白：{self._bleach_label or '序列'}",
                "xlabel": "时间 / min",
                "ylabel": "归一化强度" if norm else "强度",
                "curves": curves}])
            self.bleach_plot.set_style("data", linestyle="None", marker="o",
                                       markersize=4.5)
        except Exception as err:                       # noqa: BLE001 —— 绘图不废页
            log.warning("光漂白曲线绘制失败：%s", err)
            self.lbl_bleach_msg.setText(f"曲线绘制失败（数值结果不受影响）：{err}")

    def add_bleach_to_table(self) -> bool:
        """把当前拟合结果落一行（k_photo / t½）；不自动入表。"""
        res = self._last_bleach or {}
        if not res.get("ok"):
            self.status.emit("请先成功拟合再加入表征表")
            return False
        k_min = _as_float(res.get("k_min", res.get("k")))
        t_half = _as_float(res.get("t_half_min", res.get("t_half")))
        r2 = _as_float(res.get("r2"))
        row = _empty_char_row()
        row.update({
            "condition": self.ed_cond_bleach.text().strip(),
            "compound": self.ed_comp_bleach.text().strip() or (self._bleach_label or ""),
            "k_photo": k_min if _finite(k_min) else float("nan"),
            "t_half": t_half if _finite(t_half) else float("nan"),
            "note": (f"R²={r2:.4f}; n={int(_as_float(res.get('n_points'), 0) or 0)}"
                     if _finite(r2) else ""),
        })
        self.add_row(row)
        self.tabs.setCurrentWidget(self.tab_char)
        return True

    def export_decay_figure(self):
        """FR-45 衰减曲线新图型出口（core/render/photophysics.plot_decay_curve）。"""
        if not self._bleach_t:
            self.status.emit("请先载入并拟合光漂白数据，再导出衰减图")
            return None
        try:
            mod = _load_optional("core.render.photophysics")
            fn = getattr(mod, "plot_decay_curve")
        except Exception:                              # noqa: BLE001 —— G-C 未落地
            msg = ("core/render/photophysics 尚未落地：衰减图导出暂不可用"
                   "（数值结果不受影响）")
            self.status.emit(msg)
            self.logged.emit("WARNING", msg)
            return None
        res = dict(self._last_bleach or {})
        plateau = bool(self.chk_plateau.isChecked())
        fit_spec = {
            "model": "exp_plateau" if plateau else "exp",
            "k": _as_float(res.get("k_min", res.get("k"))),
            "t_half": _as_float(res.get("t_half_min", res.get("t_half"))),
            "r2": _as_float(res.get("r2")),
            "I0": _as_float(res.get("i0")),
            "I_inf": _as_float(res.get("i_inf")) if plateau else 0.0,
        } if (res.get("ok") and _finite(res.get("k_min", res.get("k")))) else None
        if fit_spec is None:
            self.status.emit("还没有成功的拟合结果：请先「拟合 / 预览」再导出衰减图")
            return None
        out_path = self._photo_out_path("decay")
        try:
            got = fn(self._bleach_t_minutes(),
                     [float(v) for v in self._bleach_y],
                     fit=fit_spec, out_path=out_path, cfg=self.cfg)
        except TypeError:
            try:
                got = fn(self._bleach_t_minutes(),
                         [float(v) for v in self._bleach_y],
                         fit=fit_spec, out_path=out_path)
            except Exception as err:                   # noqa: BLE001
                self.status.emit(f"衰减图导出失败：{err}")
                self.logged.emit("ERROR", f"衰减图导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"衰减图导出失败：{err}")
            self.logged.emit("ERROR", f"衰减图导出失败：{err}")
            return None
        if got:
            self.status.emit(f"已导出衰减图：{got}")
            self.logged.emit("INFO", f"光物理表征：衰减图已导出 {got}")
        return got

    def export_evolution_figure(self):
        """FR-45 光谱演化图出口（core/render/photophysics.plot_spectral_evolution）。"""
        spectra = [s for s in (self._bleach_spectra or [])
                   if getattr(s, "ok", True)]
        if not spectra:
            self.status.emit("请先在「光漂白」子页载入时间序列谱目录，再导出光谱演化图")
            return None
        try:
            mod = _load_optional("core.render.photophysics")
            fn = getattr(mod, "plot_spectral_evolution")
        except Exception:                              # noqa: BLE001 —— G-C 未落地
            msg = ("core/render/photophysics 尚未落地：光谱演化图导出暂不可用")
            self.status.emit(msg)
            self.logged.emit("WARNING", msg)
            return None
        from core.inputs.io import parse_filename
        fn_cfg = (self.cfg or {}).get("filename") or {}
        frames = []
        for i, s in enumerate(spectra):
            try:
                t_val = float(parse_filename(
                    Path(str(getattr(s, "path", ""))).stem, fn_cfg).get("t") or 0.0)
            except Exception:                          # noqa: BLE001 —— 无时间后缀退序号
                t_val = float(i)
            frames.append({"x": getattr(s, "wl", None),
                           "y": getattr(s, "y", None),
                           "t": t_val,
                           "label": Path(str(getattr(s, "path", ""))).name})
        out_path = self._photo_out_path("evolution")
        try:
            got = fn(frames, out_path, cfg=self.cfg)
        except TypeError:
            try:
                got = fn(frames, out_path)
            except Exception as err:                   # noqa: BLE001
                self.status.emit(f"光谱演化图导出失败：{err}")
                self.logged.emit("ERROR", f"光谱演化图导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"光谱演化图导出失败：{err}")
            self.logged.emit("ERROR", f"光谱演化图导出失败：{err}")
            return None
        if got:
            self.status.emit(f"已导出光谱演化图：{got}")
            self.logged.emit("INFO", f"光物理表征：光谱演化图已导出 {got}")
        return got

    def _photo_out_path(self, kind: str) -> Path:
        """新图型输出路径：out_root/photophysics_<kind>_<HHMMSS>.png（不覆盖）。"""
        import datetime as _dt
        out_root = resolve_out_root(self.cfg)
        out_root.mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%H%M%S")
        p = out_root / f"photophysics_{kind}_{stamp}.png"
        i = 2
        while p.exists():
            p = out_root / f"photophysics_{kind}_{stamp}-{i}.png"
            i += 1
        return p

    def _bar_payload(self):
        """表征总表 → (groups, series)（分组柱状图数据；缺列自动跳过）。"""
        rows = list(self._rows or [])
        if not rows:
            return [], []
        groups: list = []
        for i, r in enumerate(rows):
            name = str(r.get("compound") or "").strip() or f"行{i + 1}"
            cond = str(r.get("condition") or "").strip()
            label = name if not cond else f"{name}\n{cond}"
            groups.append(label)
        series = []
        for label, key in (("φ", "phi"), ("brightness", "brightness"),
                           ("ε", "epsilon"), ("k_photo", "k_photo")):
            vals = [_as_float(r.get(key)) for r in rows]
            if any(_finite(v) for v in vals):
                series.append({"label": label, "values": vals})
        return groups, series

    # ══════════════ 表征总表（FR-44） ══════════════
    def rows(self) -> list:
        """会话级表征台账（深拷贝，供测试/导出）。"""
        return [dict(r) for r in self._rows]

    def add_row(self, row: dict) -> None:
        """追加一行（缺项留空；不臆造数值）。"""
        base = _empty_char_row()
        for key in _CHAR_KEYS:
            if key in (row or {}) and (row or {}).get(key) is not None:
                base[key] = (row or {})[key]
        self._rows.append(base)
        self._update_char_filters()
        self._rebuild_char_table()
        self.status.emit(f"已加入表征总表（共 {len(self._rows)} 行）")
        self.logged.emit("INFO", f"表征总表 +1 行（共 {len(self._rows)} 行）")

    def _update_char_filters(self) -> None:
        for cmb, key in ((self.cmb_filter_condition, "condition"),
                         (self.cmb_filter_compound, "compound")):
            cur = cmb.currentData() or ""
            cmb.blockSignals(True)
            try:
                cmb.clear()
                cmb.addItem("全部", "")
                vals = sorted({str(r.get(key) or "").strip()
                               for r in self._rows
                               if str(r.get(key) or "").strip()})
                for v in vals:
                    cmb.addItem(v, v)
                idx = cmb.findData(cur)
                cmb.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                cmb.blockSignals(False)
        self._filter_condition = self.cmb_filter_condition.currentData() or ""
        self._filter_compound = self.cmb_filter_compound.currentData() or ""

    def _rebuild_char_table(self) -> None:
        """按 过滤 + 排序 重建表格视图（纯视图级：不改行序/编辑值）。"""
        if not hasattr(self, "tbl"):
            return
        view = [i for i, r in enumerate(self._rows)
                if (not self._filter_condition
                    or str(r.get("condition") or "") == self._filter_condition)
                and (not self._filter_compound
                     or str(r.get("compound") or "") == self._filter_compound)]
        if self._sort_key in _CHAR_KEYS:
            numeric = self._sort_key in _CHAR_NUMERIC_KEYS

            def _keyf(i: int):
                v = self._rows[i].get(self._sort_key)
                if numeric:
                    return (0, float(v)) if _finite(v) else (1, 0.0)
                return (0, str(v or ""))

            view.sort(key=_keyf, reverse=self._sort_desc)
        self._view = view
        self._filling = True
        try:
            self.tbl.setRowCount(len(view))
            for row_i, src in enumerate(view):
                row = self._rows[src]
                for col, key in enumerate(_CHAR_KEYS):
                    if key in _CHAR_NUMERIC_KEYS:
                        text = _fmt_num(row.get(key), _CHAR_FMT.get(key, ".4g"))
                    else:
                        text = str(row.get(key) or "")
                    it = QTableWidgetItem(text)
                    it.setData(Qt.UserRole, (src, key))
                    self.tbl.setItem(row_i, col, it)
        finally:
            self._filling = False

    def _refresh_char_view(self) -> None:
        self._filter_condition = self.cmb_filter_condition.currentData() or ""
        self._filter_compound = self.cmb_filter_compound.currentData() or ""
        self._rebuild_char_table()

    def _on_char_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item is None:
            return
        info = item.data(Qt.UserRole)
        if not info:
            return
        src, key = info
        if not (0 <= src < len(self._rows)):
            return
        text = (item.text() or "").strip()
        if key in _CHAR_NUMERIC_KEYS:
            if not text:
                val = float("nan")
            else:
                try:
                    val = float(text)
                except ValueError:
                    self.status.emit(
                        f"第 {item.row() + 1} 行「{self.tbl.horizontalHeaderItem(item.column()).text()}"
                        "」不是数值，已恢复原值")
                    self._filling = True
                    try:
                        item.setText(_fmt_num(self._rows[src].get(key),
                                              _CHAR_FMT.get(key, ".4g")))
                    finally:
                        self._filling = False
                    return
            self._rows[src][key] = val
        else:
            self._rows[src][key] = text
        self._update_char_filters()

    def _on_char_header_clicked(self, col: int) -> None:
        if not (0 <= col < len(_CHAR_KEYS)):
            return
        key = _CHAR_KEYS[col]
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = False
        hdr = self.tbl.horizontalHeader()
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(col, Qt.DescendingOrder if self._sort_desc
                             else Qt.AscendingOrder)
        self._rebuild_char_table()

    def delete_selected_rows(self) -> int:
        """删除表格中选中的行（映射回台账源行）。"""
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        srcs = sorted({self._view[r] for r in rows
                       if 0 <= r < len(self._view)}, reverse=True)
        for src in srcs:
            if 0 <= src < len(self._rows):
                self._rows.pop(src)
        if srcs:
            self._update_char_filters()
            self._rebuild_char_table()
            self.status.emit(f"已删除 {len(srcs)} 行（剩余 {len(self._rows)} 行）")
        return len(srcs)

    def clear_rows(self) -> int:
        """清空表征总表（破坏性动作，二次确认；返回清掉的行数）。"""
        if not self._rows:
            self.status.emit("表征总表为空")
            return 0
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self, "清空表征总表",
            f"将删除全部 {len(self._rows)} 行（仅本次会话台账；导出文件不受影响）。"
            "确定清空吗？")
        if ans != QMessageBox.Yes:
            return 0
        n = len(self._rows)
        self._rows = []
        self._update_char_filters()
        self._rebuild_char_table()
        self.status.emit(f"已清空表征总表（{n} 行）")
        return n

    def import_stokes_rows(self) -> int:
        """把 FR-41 Stokes 对比表行导入表征总表（同一模块，Q-E19）。"""
        provider = self._stokes_provider
        rows = []
        if callable(provider):
            try:
                rows = list(provider() or [])
            except Exception as err:                   # noqa: BLE001
                log.warning("读取 Stokes 行失败：%s", err)
                rows = []
        if not rows:
            self.status.emit("Stokes 对比表为空：没有可导入的行")
            return 0
        added = 0
        for sr in rows:
            row = _empty_char_row()
            row.update({
                "compound": str(getattr(sr, "group", "") or ""),
                "abs_file": str(getattr(sr, "abs_file", "") or ""),
                "em_file": str(getattr(sr, "em_file", "") or ""),
                "lam_abs": _as_float(getattr(sr, "lam_abs", None)),
                "lam_em": _as_float(getattr(sr, "lam_em", None)),
                "delta_lambda": _as_float(getattr(sr, "delta_lambda", None)),
                "delta_wavenumber": _as_float(getattr(sr, "delta_wavenumber", None)),
                "note": "来自 Stokes 对比表",
            })
            for key in _CHAR_KEYS:                     # 只保留已知键
                if key not in row:
                    row[key] = ""
            self._rows.append(row)
            added += 1
        self._update_char_filters()
        self._rebuild_char_table()
        self.status.emit(f"已从 Stokes 表导入 {added} 行（可编辑后统一导出）")
        self.logged.emit("INFO", f"表征总表导入 FR-41 Stokes 行 {added} 条")
        return added

    def _char_row_objects(self) -> list:
        """转成 characterization.CharacterizationRow（缺模块时退 dict）。"""
        try:
            mod = _load_optional("core.report.characterization")
            make = getattr(mod, "make_characterization_row", None) \
                or getattr(mod, "make_row", None)
            if callable(make):
                out = []
                for r in self._rows:
                    fields = {k: r.get(k) for k in _CHAR_KEYS
                              if r.get(k) is not None and r.get(k) != ""}
                    out.append(make(**fields))
                return out
        except Exception as err:                       # noqa: BLE001
            log.warning("表征行对象构造失败（退 dict 导出）：%s", err)
        return [dict(r) for r in self._rows]

    def _char_default_path(self, suffix: str) -> Path:
        out_root = resolve_out_root(self.cfg)
        try:
            mod = _load_optional("core.report.characterization")
            name = (mod.default_characterization_name()
                    if suffix == ".csv" else
                    str(mod.default_characterization_name()).replace(".csv", ".xlsx"))
        except Exception:                              # noqa: BLE001
            import datetime as _dt
            name = f"characterization_{_dt.datetime.now():%H%M%S}{suffix}"
        return out_root / name

    def export_csv(self, path=None):
        """导出表征总表 CSV（UTF-8-SIG / 中文列名 / 空表不产文件）。"""
        if not self._rows:
            self.status.emit("表征总表为空：未导出 CSV（未生成文件）")
            self.logged.emit("WARNING", "表征总表为空，未导出 CSV")
            return None
        try:
            mod = _load_optional("core.report.characterization")
            writer = getattr(mod, "write_characterization_csv", None)
        except Exception:                              # noqa: BLE001
            writer = None
        if not callable(writer):
            self.status.emit("核心模块 core.report.characterization 尚未落地：CSV 导出暂不可用")
            self.logged.emit("WARNING", "表征 CSV 导出失败：core 模块缺失")
            return None
        if path is None:
            default = self._char_default_path(".csv")
            path, _ = QFileDialog.getSaveFileName(
                self, "导出表征总表 CSV", str(default), "CSV 文件 (*.csv)")
            if not path:
                return None
        try:
            got = writer(self._char_row_objects(), path)
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"导出 CSV 失败：{err}")
            self.logged.emit("ERROR", f"表征 CSV 导出失败：{err}")
            return None
        if got is None:
            self.status.emit("空表：未生成 CSV")
            return None
        self._last_csv = Path(str(got))
        self.lbl_char_export.setText(f"导出：{self._last_csv}")
        self.status.emit(f"已导出表征总表 CSV：{self._last_csv}")
        self.logged.emit("INFO", f"表征总表 CSV 已导出：{self._last_csv}")
        return got

    def export_xlsx(self, path=None):
        """导出表征总表 Excel（复用 excel_export 样式单源）。"""
        if not self._rows:
            self.status.emit("表征总表为空：未导出 Excel")
            return None
        try:
            mod = _load_optional("core.report.characterization")
            writer = (getattr(mod, "write_characterization_xlsx", None)
                      or getattr(mod, "write_characterization_excel", None))
        except Exception:                              # noqa: BLE001
            writer = None
        if not callable(writer):
            self.status.emit("核心模块 core.report.characterization 尚未落地：Excel 导出暂不可用")
            self.logged.emit("WARNING", "表征 Excel 导出失败：core 模块缺失")
            return None
        if path is None:
            default = self._char_default_path(".xlsx")
            path, _ = QFileDialog.getSaveFileName(
                self, "导出表征总表 Excel", str(default),
                "Excel 工作簿 (*.xlsx)")
            if not path:
                return None
        try:
            got = writer(self._char_row_objects(), path, cfg=self.cfg)
        except TypeError:
            try:
                got = writer(self._char_row_objects(), path)
            except Exception as err:                   # noqa: BLE001
                self.status.emit(f"导出 Excel 失败：{err}")
                self.logged.emit("ERROR", f"表征 Excel 导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"导出 Excel 失败：{err}")
            self.logged.emit("ERROR", f"表征 Excel 导出失败：{err}")
            return None
        if got is None:
            self.status.emit("Excel 导出失败（openpyxl 缺失或写盘失败）")
            return None
        self._last_xlsx = Path(str(got))
        self.lbl_char_export.setText(f"导出：{self._last_xlsx}")
        self.status.emit(f"已导出表征总表 Excel：{self._last_xlsx}")
        self.logged.emit("INFO", f"表征总表 Excel 已导出：{self._last_xlsx}")
        return got

    def export_bar_figure(self):
        """FR-45 分组柱状图新图型出口（core/render/photophysics.plot_grouped_bars）。"""
        groups, series = self._bar_payload()
        if not groups or not series:
            self.status.emit("表征总表为空（或没有可画的数值列）：无法导出柱状图")
            return None
        try:
            mod = _load_optional("core.render.photophysics")
            fn = getattr(mod, "plot_grouped_bars")
        except Exception:                              # noqa: BLE001 —— G-C 未落地
            msg = ("core/render/photophysics 尚未落地：柱状图导出暂不可用"
                   "（表征表 CSV/Excel 导出不受影响）")
            self.status.emit(msg)
            self.logged.emit("WARNING", msg)
            return None
        out_path = self._photo_out_path("bars")
        try:
            got = fn(groups, series, out_path, cfg=self.cfg)
        except TypeError:
            try:
                got = fn(groups, series, out_path)
            except Exception as err:                   # noqa: BLE001
                self.status.emit(f"柱状图导出失败：{err}")
                self.logged.emit("ERROR", f"柱状图导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.status.emit(f"柱状图导出失败：{err}")
            self.logged.emit("ERROR", f"柱状图导出失败：{err}")
            return None
        if got:
            self.status.emit(f"已导出分组柱状图：{got}")
            self.logged.emit("INFO", f"光物理表征：分组柱状图已导出 {got}")
        return got


#: 队列条目内部状态 → 界面文案（失败原因单独拼接）
