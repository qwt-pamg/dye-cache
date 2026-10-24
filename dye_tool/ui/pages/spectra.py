# -*- coding: utf-8 -*-
"""功能④ 紫外/荧光常规作图（FR-4，本次新增模块）。

> 设计文档说明："紫外荧光作图脚本"未随包提供，本页按 §4 FR-4 规格新写，
> 数值接口集中在 ``core.spectra``，便于日后用真实脚本的算法替换（§10 Q1）。

1. 多文件归一化叠图（最大峰归一），自动标注每曲线 λmax；
2. 吸收+发射镜像归一化叠图，自动计算并标注**斯托克斯位移**；
3. 风格与 FR-1~3 一致（JACS 单栏 82.5 mm / 刻度朝内 / 四边完整框 / 图内无标题），
   导出格式见 spectra.export_formats（默认 png+svg @600dpi）；
4. 摩尔消光系数计算器（二期接口的先行实现，ε = A/(c·l)）。
"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.io import Spectrum, load_spectra_files
from core.pipeline import run_spectra_plots
from core.spectra import molar_extinction
from ui.pages.workbench import WorkbenchPage
from ui.widgets.export_bar import ExportBar
from ui.widgets.live_plot import LivePlotCanvas, label_text
from ui.widgets.mpl_canvas import CanvasPanel
from ui.widgets.style_panel import (MAPPED_KEYS, SPECTRA_MAPPED_KEYS,
                                    StylePanel)
from ui import theme

KIND_ZH = {"abs": "吸收", "em": "发射", "unknown": "未知"}

#: 作图模式键 → 界面文案（默认顺序 = V1.x 顺序；作图台按数据类型复用时只取子集）
MODE_LABELS = {
    "overlay": "多文件归一化叠图",
    "mirror": "吸收+发射镜像图",
    "stacked": "吸收/发射上下两栏",
    "auto": "自动配对出镜像图",
}

#: V2.6 FR-38.3：作图台布局适配基线（参数表最小高度 / 水平 body 左栏 360–470）
PARAM_TABLE_MIN_H = 140


class SpectraPage(WorkbenchPage):
    """紫外/荧光常规作图页（V2.1：同一页类按**数据类型**复用）。"""

    TITLE = "④ 紫外 / 荧光常规作图"
    SUBTITLE = ("载入两列光谱文本（紫外吸收）或仪器导出的荧光发射谱 → "
                "归一化叠图 / 吸收-发射镜像图（自动标注斯托克斯位移）。"
                "工作视图可直接交互；定稿图用「生成定稿图」。")
    PRESET_SECTION = "spectra"
    PAGE_MARK = "④"
    BODY_LEFT_MIN_W = 390
    #: V2.6 FR-41.5：「在分析页记录」→ (abs 文件路径, em 文件路径)；
    #: 主窗口把该对预填给②分析页的「斯托克斯位移」测量卡并切页。
    record_pair_requested = Signal(str, str)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None,
                 *, modes: Optional[tuple] = None, overlay_kinds: str = "all",
                 show_eps: bool = True, title: Optional[str] = None,
                 subtitle: Optional[str] = None):
        """V2.1 作图台拆分：同一页类按数据类型复用（**默认参数 = V1.x 行为**）。

        Args:
            modes: 允许的作图模式键（``MODE_LABELS`` 子集）；默认全部四种。
            overlay_kinds: 叠图模式只画哪类谱（``"abs"`` / ``"em"`` / ``"all"``）。
            show_eps: 是否显示 ε 计算器（吸收页显示、发射页隐藏）。
            title / subtitle: 覆盖页头文案；不传 = 类默认（经典布局一字不变）。
        """
        self._modes = tuple(modes) if modes else tuple(MODE_LABELS)
        self._overlay_kinds = str(overlay_kinds or "all")
        self._show_eps = bool(show_eps)
        if title:
            self.TITLE = title
        if subtitle:
            self.SUBTITLE = subtitle
        super().__init__(cfg, parent)
        self.spectra: list[Spectrum] = []
        #: auto 模式多对逐行标签（仅可视化；逐对入表在②分析页，Q-E16）
        self._auto_pair_lines: list[str] = []
        self._had_spectra = False
        self._build()
        self.setAcceptDrops(True)

    def _build(self) -> None:
        self.btn_files = QPushButton("选择光谱文件…")
        self.btn_dir = QPushButton("选择目录…")
        self.btn_run = QPushButton("▶ 生成定稿图")
        theme.mark(self.btn_run, "primary")
        self.btn_run.setToolTip(
            "定稿图由分析流水线生成（JACS 版式）。载入/切换不再自动生图；\n"
            "探索性交互在工作视图（活画布）里完成。")
        self.lbl_info = QLabel("尚未载入光谱")
        theme.mark(self.lbl_info, "muted")
        self.btn_files.clicked.connect(self._choose_files)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_run.clicked.connect(self._run)
        self.btn_run.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(self.btn_files)
        top.addWidget(self.btn_dir)
        top.addWidget(self.lbl_info, 1)
        self.chk_live = QCheckBox("实时生图预览")
        self.chk_live.setChecked(True)
        self.chk_live.setToolTip("调整出图/光谱参数时用最终渲染器低 dpi 预览；不写正式 run")
        self.lbl_live = QLabel("预览待命")
        theme.mark(self.lbl_live, "hint")
        top.addWidget(self.chk_live)
        top.addWidget(self.lbl_live)
        top.addWidget(self.btn_run)

        # ── 选项 ──
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems([MODE_LABELS[m] for m in self._modes])
        self.cmb_norm = QComboBox()
        self.cmb_norm.addItems(["最大峰归一", "不归一"])
        # 用户需求：作图前预处理——与 Φ_Δ 同一 despike 判据；只影响作图，不改数据
        self.chk_despike = QCheckBox("去尖峰（作图前预处理，与 Φ_Δ 同判据）")
        self.chk_despike.setChecked(True)
        self.chk_despike.setToolTip(
            "按 Φ_Δ 的同一判据（滚动中位数 ± k×MAD）剔除单点异常后再作图；\n"
            "只影响本页图件与导出图，不修改原始数据文件、不参与数值分析。\n"
            "取消勾选 = 按原始数据作图（旧行为）。")
        self.cmb_abs = QComboBox()
        self.cmb_em = QComboBox()
        self.chk_mark = QLabel("λmax 与斯托克斯位移将自动标注")
        theme.mark(self.chk_mark, "muted")
        box = QGroupBox("作图设置")
        f = QFormLayout(box)
        f.addRow("作图模式", self.cmb_mode)
        f.addRow("归一化方式", self.cmb_norm)
        f.addRow("", self.chk_despike)
        self.cmb_display_preset = QComboBox()
        self.cmb_display_preset.setToolTip(
            "套用 presets.yaml 的 spectra 节显示预设（只影响本页视图）")
        self.btn_display_preset_save = QPushButton("保存…")
        self.btn_display_preset_save.setToolTip("把当前④作图设置存为命名预设（presets.yaml）")
        self.btn_display_preset_save.clicked.connect(self._save_display_preset_dialog)
        self.cmb_display_preset.currentIndexChanged.connect(self._on_display_preset_chosen)
        preset_row = QHBoxLayout()
        preset_row.addWidget(self.cmb_display_preset, 1)
        preset_row.addWidget(self.btn_display_preset_save)
        f.addRow("显示预设", preset_row)
        self._refresh_display_presets()
        f.addRow("吸收谱（镜像用）", self.cmb_abs)
        f.addRow("发射谱（镜像用）", self.cmb_em)
        f.addRow("", self.chk_mark)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode)

        # ── 摩尔消光系数（可选，二期） ──
        self.sp_a = QDoubleSpinBox()
        self.sp_a.setRange(0, 10)
        self.sp_a.setDecimals(4)
        self.sp_a.setValue(0.5)
        self.sp_c = QDoubleSpinBox()
        self.sp_c.setRange(0, 1e-3)
        self.sp_c.setDecimals(9)
        self.sp_c.setSingleStep(1e-6)
        self.sp_c.setValue(5e-6)
        self.sp_l = QDoubleSpinBox()
        self.sp_l.setRange(0, 100)
        self.sp_l.setDecimals(3)
        self.sp_l.setValue(1.0)
        self.btn_eps = QPushButton("计算 ε")
        self.btn_eps.clicked.connect(self._calc_eps)
        self.lbl_eps = QLabel("ε = —")
        theme.mark(self.lbl_eps, "strong")
        box_eps = QGroupBox("摩尔消光系数 ε = A / (c·l)（可选）")
        self.box_eps = box_eps
        fe = QFormLayout(box_eps)
        fe.addRow("吸光度 A", self.sp_a)
        fe.addRow("浓度 c (mol/L)", self.sp_c)
        fe.addRow("光程 l (cm)", self.sp_l)
        row = QHBoxLayout()
        row.addWidget(self.btn_eps)
        row.addWidget(self.lbl_eps, 1)
        fe.addRow("", self._wrap(row))

        # ── 光谱清单 ──
        self.tbl = QTableWidget(0, 4)
        self.tbl.setHorizontalHeaderLabels(["文件名", "类型", "λmax (nm)", "点数"])
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.setMinimumHeight(PARAM_TABLE_MIN_H)   # V2.6 FR-38.3：表格最小高度 120–160
        box_t = QGroupBox("已载入光谱")
        lt = QVBoxLayout(box_t)
        lt.addWidget(self.tbl)

        # V2.6 FR-41.5：单对语义标签（当前配对 / 未选择配对 / auto 多对逐行）
        self.lbl_stokes = QLabel("先选择光谱目录")
        theme.mark(self.lbl_stokes, "section")
        self.lbl_stokes.setWordWrap(True)
        self.btn_record_pair = QPushButton("在分析页记录")
        self.btn_record_pair.setToolTip(
            "把当前配对（吸收/发射各一支）带到②分析页的「斯托克斯位移」测量卡：\n"
            "自动预填该对 → 确认后点「加入对比表」。仅镜像/上下两栏模式、已配对时可用。\n"
            "")
        self.btn_record_pair.setEnabled(False)
        self.btn_record_pair.clicked.connect(self._record_current_pair)
        box_s = QGroupBox("结果")
        ls = QVBoxLayout(box_s)
        ls.addWidget(self.lbl_stokes)
        ls.addWidget(self.btn_record_pair)

        # V2.6 FR-38.3：左栏参数区改 QScrollArea(widgetResizable) —— 高度不足时**整栏滚动**，
        # 不再用「固定高度垂直 splitter」把底部「结果/ε」裁掉（1006px 高度必现）。
        left = QWidget()
        self.left_panel = left
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(box)
        if self._show_eps:
            lv.addWidget(box_eps)
        lv.addWidget(box_t)
        lv.addWidget(box_s)
        lv.addStretch(0)
        left_scroll = QScrollArea()
        self.left_scroll = left_scroll
        left_scroll.setWidget(left)
        left_scroll.setWidgetResizable(True)
        left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left_scroll.setMinimumWidth(self.BODY_LEFT_MIN_W)
        left_scroll.setMaximumWidth(self.BODY_LEFT_MAX_W)
        left = left_scroll

        # V2.5 FR-32/34：工作视图（活画布）与定稿预览分离
        self.live = LivePlotCanvas(cfg=self.cfg)
        self.live.readout.connect(self.status)
        self.canvas = CanvasPanel()
        from ui.widgets.live_render_preview import LiveRenderPreview
        self.live_preview = LiveRenderPreview(self, self._render_live_preview)
        self.live_preview.updated.connect(self.canvas.set_images)
        self.live_preview.state.connect(self.lbl_live.setText)
        self.live_preview.failed.connect(lambda m: self.logged.emit("WARNING", f"实时预览失败：{m}"))
        self.chk_live.toggled.connect(self.live_preview.set_enabled)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.live)
        self.view_stack.addWidget(self.canvas)
        self.cmb_view = QComboBox()
        self.cmb_view.addItems(["工作视图", "定稿预览"])
        self.cmb_view.setToolTip(
            "工作视图 = 临时交互（缩放/平移/读数/标注/实时样式），不等于定稿产物；\n"
            "定稿预览只显示已生成图件，不触发生成")
        self.cmb_view.currentIndexChanged.connect(self._on_view_changed)
        self.chk_readout = QCheckBox("读数模式")
        self.chk_readout.setToolTip(
            "鼠标移动时在状态栏显示最近数据点的物理坐标（不插值；仅本次会话）")
        self.chk_readout.toggled.connect(self.live.set_readout_enabled)
        self.cmb_annot = QComboBox()
        self.cmb_annot.addItem("无标注", "")
        self.cmb_annot.addItem("文本", "text")
        self.cmb_annot.addItem("箭头", "arrow")
        self.cmb_annot.addItem("区域", "rect")
        self.cmb_annot.setToolTip("标注模式（仅本次会话；随「按当前视图导出」带出，不写入结果目录）")
        self.cmb_annot.currentIndexChanged.connect(
            lambda i: self.live.set_annotation_mode(self.cmb_annot.itemData(i)))
        self.btn_export_view = QPushButton("按当前视图导出…")
        self.btn_export_view.setToolTip(
            "把工作视图（含标注）导出为 workspace_spectra_<时间>.png/svg；\n"
            "不进 run 产物、绝不覆盖 fig_*、零 PDF（filter_formats 守）")
        self.btn_export_view.clicked.connect(self._export_working_view)
        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("视图"))
        view_bar.addWidget(self.cmb_view)
        view_bar.addWidget(self.chk_readout)
        view_bar.addWidget(self.cmb_annot)
        view_bar.addStretch(1)
        view_bar.addWidget(self.btn_export_view)
        box_fig = QGroupBox("图件视图")
        lfig = QVBoxLayout(box_fig)
        lfig.addLayout(view_bar)
        lfig.addWidget(self.view_stack, 1)
        box_style = QGroupBox("曲线样式（仅本次会话；「写入出图参数」后才保存）")
        ls = QVBoxLayout(box_style)
        self.style_panel = StylePanel(with_spectra=True)   # V25-D1：spectra 键区
        # V2.6 FR-38.3：风格面板自身约 500px 高 → 独立 QScrollArea（必须可滚，不把底部按钮顶出屏幕）
        style_scroll = QScrollArea()
        self.style_scroll = style_scroll
        style_scroll.setWidget(self.style_panel)
        style_scroll.setWidgetResizable(True)
        style_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        ls.addWidget(style_scroll, 1)
        # V2.6 FR-38.3 + Lead 仲裁：动作行做成**固定页脚**（始终可见、不被窗口裁剪），
        # 滚动区只承载说明/曲线表/可映射键（A38.2 的「写入出图参数」严格初始可见）。
        self.style_actions = self.style_panel.take_actions_widget()
        ls.addWidget(self.style_actions, 0)
        self.style_panel.style_changed.connect(self._on_live_style)
        self.style_panel.mapped_changed.connect(self._on_mapped_preview)
        self.style_panel.spectra_changed.connect(self._on_spectra_preview)
        for _sig in (self.style_panel.style_changed, self.style_panel.mapped_changed,
                     self.style_panel.spectra_changed):
            _sig.connect(lambda *_: self._schedule_preview())
        for _w in (self.cmb_mode, self.cmb_norm):
            _w.currentIndexChanged.connect(lambda *_: self._schedule_preview())
        self.chk_despike.toggled.connect(lambda *_: self._schedule_preview())
        self.style_panel.write_requested.connect(self._on_write_plot_params)
        self.style_panel.preset_requested.connect(self._on_save_plot_preset)
        self.style_panel.advanced_requested.connect(self._on_advanced_style)
        # V25-B4/D1：非法颜色/样式被拒绝 → 状态栏 + 日志提示（不崩、不改画布）
        self.live.style_rejected.connect(self._on_style_rejected)
        self.style_panel.edit_rejected.connect(self._on_style_rejected)

        # V2.6 FR-38.3：右栏 = 画布 + 实时参数编辑的**垂直 QSplitter**（可拖动/可折叠，画布 stretch 大）
        right = QSplitter(Qt.Vertical)
        self.right_split = right
        right.addWidget(box_fig)
        right.addWidget(box_style)
        right.setStretchFactor(0, 1)
        right.setStretchFactor(1, 0)
        right.setSizes([560, 300])

        # V2.6 FR-38.3：水平 body splitter —— 左栏初值按页面宽度自适应（360–470），右栏 stretch 1，
        # 窗口变窄时优先压缩画布而不是把左栏按钮挤出可视区。
        body = QSplitter(Qt.Horizontal)
        self.body = body
        body.addWidget(left)
        body.addWidget(right)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([self._body_left_width(), 1200])

        self.export = ExportBar()
        # V2.3 FR-24.2/24.6：导出条与画布的常规反馈走状态栏（不弹模态）
        self.export.message.connect(self.status)
        self.canvas.status_message.connect(self.status)
        lay = QVBoxLayout(self)
        lay.addWidget(self.header())
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        lay.addLayout(top)
        lay.addWidget(body, 1)
        lay.addWidget(self.progress_row())
        lay.addWidget(self.export)
        self._on_mode(0)
        # V25-B3/D1：载入前先用 cfg 播种可映射键（用户编辑过则不覆盖）
        self._seed_style_panel()
        # V2.5 FR-32：选项变化 → 活画布即时重绘（不触发渲染器）
        for w in (self.cmb_mode, self.cmb_norm, self.cmb_abs, self.cmb_em):
            w.currentIndexChanged.connect(lambda *_: self._rebuild_working_view())
        # V2.6 FR-41.5：候选变化即时刷新单对标签/记录按钮；空态给"先选择光谱目录"
        for w in (self.cmb_abs, self.cmb_em):
            w.currentIndexChanged.connect(lambda *_: self.refresh_stokes_label())
        self.refresh_stokes_label()
        self.chk_despike.toggled.connect(lambda *_: self._rebuild_working_view())

    # ══════════════ V2.6 FR-38.3：水平 body splitter 自适应 ══════════════

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ══════════════ 载入 ══════════════
    def _exts(self) -> tuple[str, ...]:
        """可选扩展名 —— 与 ``collect_scans`` 共用同一真相源（V1.3 FR-13.3）。

        原来这里硬编码 ``(".txt", ".csv", ".dat")``：既漏了 ``.fds``，
        又和 ``core.io.scan_extensions()`` 各说各话（改一处忘一处）。
        """
        from core.io import scan_extensions
        exts = set(scan_extensions(self.cfg.get("io") or {}))
        exts.add(".dat")                       # 常规仪器导出，历史沿用
        return tuple(sorted(exts))

    def _choose_files(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        start = str(self.cfg.get("gui", {}).get("last_dir") or "")
        pat = " ".join(f"*{e}" for e in self._exts())
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择光谱文件（可多选）", start,
            f"光谱文件 ({pat});;所有文件 (*.*)")
        if files:
            self._load([Path(f) for f in files])

    def _load_dir(self, d: Optional[Path],
                  recursive: Optional[bool] = None,
                  overrides: Optional[dict] = None) -> None:
        """载入整个目录（作图台/批量接线用；等价于"选择目录"但不开对话框）。

        A19（V2.x 审查，最小修复）：接受 ①数据页的 ``recursive`` 口径（原来这里
        **恒用 rglob**，与数据页"含子目录"开关不一致）；``overrides`` 对荧光谱
        无意义（本页不做时间整定），接受但忽略，保持调用侧统一。
        """
        _ = overrides
        if not d:
            return
        d = Path(d)
        self.remember_dir(d)
        exts = self._exts()
        rec = True if recursive is None else bool(recursive)
        it = d.rglob("*") if rec else d.glob("*")
        files = sorted(p for p in it
                       if p.is_file() and p.suffix.lower() in exts
                       and not p.name.startswith("_"))
        if not files:
            self.logged.emit("WARNING",
                             f"{d} 下没有找到光谱文件（{'/'.join(exts)}）")
            return
        self._load(files)

    def dragEnterEvent(self, e) -> None:               # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                    # noqa: N802
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        exts = self._exts()
        files: list[Path] = []
        for p in urls:
            if p.is_dir():
                files += sorted(x for x in p.rglob("*")
                                if x.is_file() and x.suffix.lower() in exts)
            elif p.is_file():
                files.append(p)
        if files:
            self._load(files)
        e.acceptProposedAction()

    def _load(self, paths: list[Path]) -> None:
        cfg = self.cfg

        def task(progress_cb, log_cb):
            return load_spectra_files(paths, cfg)

        self.run_task(task, "载入光谱", self._on_loaded,
                      busy_widgets=[self.btn_files, self.btn_dir, self.btn_run])

    # ══════════════ V2.6 FR-41.5：单对语义标签 + 「在分析页记录」桥接 ══════════════
    @staticmethod
    def _fmt_lam(v) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
        return f"{f:.1f}" if np.isfinite(f) else "—"

    @staticmethod
    def _fmt_nu(v) -> str:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "—"
        return f"{f:.0f}" if np.isfinite(f) else "—"

    def _group_of(self, s) -> str:
        """文件名 → 组别（复用 parse_filename 同源逻辑；失败返回空串）。"""
        try:
            from core.inputs.io import parse_filename
            fn = (self.cfg or {}).get("filename") or {}
            meta = parse_filename(Path(s.path).stem, fn)
            return str(meta.get("group") or "")
        except Exception:                              # noqa: BLE001 —— 显示层不崩
            return ""

    def _pair_metrics(self, abs_s, em_s) -> tuple:
        """当前配对 → (λabs, λem, Δλ, Δν̃)；数值复用 core.stokes_shift（不另写公式）。"""
        from core.analysis.spectra import stokes_shift
        scfg = (self.cfg or {}).get("spectra") or {}
        em_range = scfg.get("emission_wl_range")
        la, le, delta = stokes_shift(abs_s.wl, abs_s.y, em_s.wl, em_s.y,
                                     None, em_range)
        nu = float("nan")
        try:
            if np.isfinite(la) and np.isfinite(le) and la > 0 and le > 0:
                nu = 1.0e7 / float(la) - 1.0e7 / float(le)
        except Exception:                              # noqa: BLE001
            nu = float("nan")
        return float(la), float(le), float(delta), nu

    def current_pair(self):
        """当前模式下的配对（仅 mirror/stacked 且两支都选到）；否则 None。"""
        if self._mode_key(self.cmb_mode.currentIndex()) not in ("mirror", "stacked"):
            return None
        a = self._pick(self.cmb_abs, "abs")
        e = self._pick(self.cmb_em, "em")
        if a is None or e is None:
            return None
        return a, e

    def set_mirror_pair(self, abs_index: int, em_index: int) -> bool:
        """按 kind 过滤后的序号设置镜像配对（测试/脚本入口）。"""
        def _specs(kind: str) -> list:
            return [s for s in (self.spectra or []) if s.ok and s.kind == kind]
        abs_list, em_list = _specs("abs"), _specs("em")
        try:
            ai, ei = int(abs_index), int(em_index)
        except (TypeError, ValueError):
            return False
        if not (0 <= ai < len(abs_list) and 0 <= ei < len(em_list)):
            self.refresh_stokes_label()
            return False
        mode = self._mode_key(self.cmb_mode.currentIndex())
        if mode not in ("mirror", "stacked") and "mirror" in self._modes:
            self.cmb_mode.setCurrentIndex(self._modes.index("mirror"))
        # combo 项 = kind 过滤后的顺序（与 abs_list/em_list 同序）
        self.cmb_abs.blockSignals(True)
        self.cmb_em.blockSignals(True)
        try:
            self.cmb_abs.setCurrentIndex(ai)
            self.cmb_em.setCurrentIndex(ei)
        finally:
            self.cmb_abs.blockSignals(False)
            self.cmb_em.blockSignals(False)
        self._rebuild_working_view()
        self.refresh_stokes_label()
        return True

    def _record_current_pair(self) -> bool:
        """「在分析页记录」：发当前配对文件路径；未配对时提示不动作。"""
        pair = self.current_pair()
        if pair is None:
            self.status.emit("请先在镜像/上下两栏模式下选定一对吸收/发射谱")
            return False
        a, e = pair
        self.record_pair_requested.emit(str(a.path), str(e.path))
        self.status.emit(f"已发送到②分析页：{Path(a.path).name} × {Path(e.path).name}")
        self.logged.emit("INFO", "在分析页记录：当前配对 → ②斯托克斯位移测量卡")
        return True

    def refresh_stokes_label(self) -> str:
        """刷新结果区标签：单对语义；auto 多对逐行；空态引导（FR-41.5/Q-E16）。"""
        good = [s for s in (self.spectra or []) if s.ok]
        mode = self._mode_key(self.cmb_mode.currentIndex())
        if not good:
            text = "先选择光谱目录"
        elif mode == "auto":
            if self._auto_pair_lines:
                text = "\n".join(self._auto_pair_lines) + "\n逐对入表请去②分析页"
            else:
                text = ("未选择配对（自动配对结果在「生成定稿图」后逐行显示；"
                        "逐对入表请去②分析页）")
        elif mode in ("mirror", "stacked"):
            pair = self.current_pair()
            if pair is None:
                text = "未选择配对"
            else:
                a, e = pair
                la, le, delta, nu = self._pair_metrics(a, e)
                ga, ge = self._group_of(a), self._group_of(e)
                label_a = f"{ga}/{Path(a.path).name}" if ga else Path(a.path).name
                label_e = f"{ge}/{Path(e.path).name}" if ge else Path(e.path).name
                text = ("当前配对（组/文件）："
                        f"{label_a} × {label_e}  |  "
                        f"λabs={self._fmt_lam(la)} nm, "
                        f"λem={self._fmt_lam(le)} nm, "
                        f"Δλ={self._fmt_lam(delta)} nm,  "
                        f"Δν̃={self._fmt_nu(nu)} cm⁻¹")
        else:
            text = "未选择配对（当前为归一化叠图；镜像/上下两栏显示配对语义）"
        self.lbl_stokes.setText(text)
        pair = self.current_pair() if mode in ("mirror", "stacked") else None
        try:
            self.btn_record_pair.setEnabled(pair is not None
                                            and not bool(self._busy))
        except Exception:                              # noqa: BLE001
            pass
        return text

    def _auto_pair_label_lines(self, stokes) -> list:
        """auto 模式：结果逐对 → 「配对 i（组）」标签行（不自动入表，Q-E16）。"""
        lines: list[str] = []
        pairs: list = []
        try:
            from core.analysis.spectra import pair_by_name
            scfg = (self.cfg or {}).get("spectra") or {}
            good = self._working_spectra()
            pairs = pair_by_name(good, scfg.get("auto_pair_keywords", []))
        except Exception:                              # noqa: BLE001 —— 标签失败不崩
            pairs = []
        for i, item in enumerate(list(stokes or []), 1):
            try:
                la, le, delta = (float(item[0]), float(item[1]), float(item[2]))
            except (TypeError, ValueError, IndexError):
                continue
            group = ""
            if 0 <= i - 1 < len(pairs):
                group = self._group_of(pairs[i - 1][0])
            nu = (1.0e7 / la - 1.0e7 / le
                  if np.isfinite(la) and np.isfinite(le) and la > 0 and le > 0
                  else float("nan"))
            head = f"配对 {i}" + (f"（{group}）" if group else "")
            lines.append(f"{head}：λabs={self._fmt_lam(la)} nm, "
                         f"λem={self._fmt_lam(le)} nm, Δλ={self._fmt_lam(delta)} nm, "
                         f"Δν̃={self._fmt_nu(nu)} cm⁻¹")
        return lines

    def _on_loaded(self, spectra: list) -> None:
        had = bool(self._had_spectra)
        self.spectra = spectra
        self._had_spectra = bool(spectra)
        # V2.6 FR-41.5：换目录 → 配对候选/预览重置（②分析页对比表由那边保留）
        self._auto_pair_lines = []
        # FR-33.4：重载数据 → per-curve 会话覆写失效
        if hasattr(self, "live"):
            self.live.clear_styles()
        good = [s for s in spectra if s.ok]
        self._fill_table(spectra)
        n_abs = sum(1 for s in good if s.kind == "abs")
        n_em = sum(1 for s in good if s.kind == "em")
        self.lbl_info.setText(f"共 {len(spectra)} 支 · 可用 {len(good)} · "
                              f"吸收 {n_abs} · 发射 {n_em} · "
                              f"失败 {len(spectra) - len(good)}")
        self._update_run_enabled()
        for cmb, kind in ((self.cmb_abs, "abs"), (self.cmb_em, "em")):
            cmb.clear()
            names = [f"{s.path.name}" for s in good if s.kind == kind]
            cmb.addItems(names or ["（无）"])
        self.logged.emit("INFO", f"已载入 {len(spectra)} 支光谱，可用 {len(good)} 支")
        if not good:
            self.logged.emit("ERROR", "没有可解析的光谱，请确认是两列文本或 荧光 荧光格式")
        self._rebuild_working_view()
        self.refresh_stokes_label()
        if good and hasattr(self, "live_preview"):
            self._schedule_preview()
        if had:
            self.status.emit("已按新目录重置配对候选与预览（②分析页对比表保留）")
            self.logged.emit("INFO", "目录已切换：配对候选/预览已重置；②分析页对比表保留")

    def _fill_table(self, spectra: list) -> None:
        self.tbl.setRowCount(len(spectra))
        for r, s in enumerate(spectra):
            if s.ok:
                lm = s.lambda_max()
                vals = [s.path.name, KIND_ZH.get(s.kind, s.kind),
                        f"{lm:.1f}" if lm == lm else "—", str(s.wl.size)]
            else:
                vals = [s.path.name, "失败", "—", s.excluded]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(str(t))
                it.setToolTip(str(t) if c != 3 or not s.ok else s.excluded or "")
                if not s.ok:
                    it.setForeground(Qt.gray)
                self.tbl.setItem(r, c, it)

    # ══════════════ V2.5 FR-32/34：工作视图（活画布） ══════════════

    def _working_spectra(self) -> list:
        good = [s for s in (self.spectra or []) if s.ok]
        if self.chk_despike.isChecked():
            cfg = self._cfg_for_plot()
            good = self._despike_spectra(good, dict(cfg.get("qc") or {}))
        return good

    def _norm_y(self, y):
        from ui.prep.display import normalize_y
        if self.cmb_norm.currentIndex() == 1:
            import numpy as np
            return np.asarray(y, float)
        return normalize_y(y)

    # ── V25-D1：spectra 可映射键 → 工作视图（abs/em 颜色 + 叠加色带） ──
    def _spectra_cfg_values(self) -> dict:
        """cfg 中的 spectra 可映射键（唯一真相源，缺省值同 core.config）。"""
        sp = (self.cfg or {}).get("spectra") or {}
        ov = ((self.cfg or {}).get("plot") or {}).get("spectra_overlay") or {}
        return {"abs_color": str(sp.get("abs_color", "#1f4e79")),
                "em_color": str(sp.get("em_color", "#C81E1E")),
                "spectra_overlay.cmap": str(ov.get("cmap", "viridis"))}

    def _spectra_style(self) -> dict:
        """工作视图样式：cfg 为底，风格栏即时预览优先（会话级）；非法值回退。"""
        from matplotlib import colormaps
        from matplotlib.colors import is_color_like
        cfg_vals = self._spectra_cfg_values()
        out = dict(cfg_vals)
        preview = getattr(self, "_spectra_preview_values", {}) or {}
        for key, val in preview.items():
            if key in out and str(val or "").strip():
                out[key] = str(val)
        if out["spectra_overlay.cmap"] not in colormaps:
            fallback = cfg_vals["spectra_overlay.cmap"]
            out["spectra_overlay.cmap"] = (fallback
                                           if fallback in colormaps
                                           else "viridis")
        for key in ("abs_color", "em_color"):
            if not is_color_like(out[key]):
                out[key] = cfg_vals[key]
        return out

    @staticmethod
    def _kind_label_en(kind: str) -> str:
        return {"abs": "UV-vis absorbance",
                "em": "fluorescence emission"}.get(kind, "all spectra")

    def _rebuild_working_view(self) -> None:
        """从页面数组直接绘制工作视图（不调用渲染器；A32.1 数据等价）。

        V25-D1：overlay 用 cfg/预览的 spectra_overlay.cmap 色带，镜像/上下栏用
        spectra.abs_color / em_color 着色（此前 overlay 恒用 viridis、镜像无色）。
        """
        if not hasattr(self, "live"):
            return
        good = self._working_spectra()
        mode = self._mode_key(self.cmb_mode.currentIndex())
        if not good:
            self.live.clear()
            return
        panels: list[dict] = []
        style = self._spectra_style()
        xlabel = label_text("波长 / nm", "Wavelength / nm")
        if mode == "overlay":
            target = self._overlay_spectra()
            # overlay 过滤基于未 despike 的原列表；映射回 despike 后的同名对象
            by_name = {s.path.name: s for s in good}
            target = [by_name.get(s.path.name, s) for s in target if s.path.name in by_name]
            from core.render.plotstyle import cmap_colors
            lo, hi = self._working_cmap_range()
            colors = cmap_colors(style["spectra_overlay.cmap"], lo, hi,
                                 max(1, len(target)))
            curves = []
            for i, s in enumerate(target):
                curves.append({"key": s.path.name,
                               "label": s.path.name,
                               "x": np.asarray(s.wl, float).ravel(),
                               "y": self._norm_y(s.y),
                               "color": colors[i % len(colors)]})
            if curves:
                panels.append({"title": label_text(
                                   f"归一化叠图（{self._kind_label(self._overlay_kinds)}）",
                                   f"Normalized overlay "
                                   f"({self._kind_label_en(self._overlay_kinds)})"),
                               "xlabel": xlabel,
                               "ylabel": label_text("归一化强度",
                                                    "Normalized intensity"),
                               "curves": curves})
        else:
            if mode == "auto":
                abs_s = next((s for s in good if s.kind == "abs"), None)
                em_s = next((s for s in good if s.kind == "em"), None)
            else:
                abs_s = self._pick(self.cmb_abs, "abs")
                em_s = self._pick(self.cmb_em, "em")
                if abs_s is not None:
                    abs_s = {s.path.name: s for s in good}.get(abs_s.path.name, abs_s)
                if em_s is not None:
                    em_s = {s.path.name: s for s in good}.get(em_s.path.name, em_s)
            if abs_s is not None:
                panels.append({"title": label_text(
                                   f"吸收谱：{abs_s.path.name}",
                                   f"Absorbance: {abs_s.path.name}"),
                               "xlabel": xlabel, "ylabel": "A",
                               "curves": [{"key": f"abs|{abs_s.path.name}",
                                           "label": abs_s.path.name,
                                           "color": style["abs_color"],
                                           "x": np.asarray(abs_s.wl, float).ravel(),
                                           "y": self._norm_y(abs_s.y)}]})
            if em_s is not None:
                panels.append({"title": label_text(
                                   f"发射谱：{em_s.path.name}",
                                   f"Emission: {em_s.path.name}"),
                               "xlabel": xlabel,
                               "ylabel": label_text("发射强度",
                                                    "Emission intensity"),
                               "curves": [{"key": f"em|{em_s.path.name}",
                                           "label": em_s.path.name,
                                           "color": style["em_color"],
                                           "x": np.asarray(em_s.wl, float).ravel(),
                                           "y": self._norm_y(em_s.y)}]})
        self.live.set_panels(
            panels, suptitle=label_text("工作视图",
                                        "Working view"))
        self._sync_style_panel()
        self.cmb_view.setCurrentIndex(0)
        self.view_stack.setCurrentIndex(0)

    # ── V2.5 FR-33：风格栏桥接（实时=重绘；写入/预设=显式动作） ──

    def _seed_style_panel(self) -> None:
        """用 cfg 播种可映射键（V25-B3）与 spectra 键（V25-D1）。"""
        if not hasattr(self, "style_panel"):
            return
        jacs = ((self.cfg or {}).get("plot") or {}).get("jacs") or {}
        values = {k: jacs[k] for k in MAPPED_KEYS if k in jacs}
        if values and self.style_panel.seed_mapped_values(values):
            self._mapped_preview_values = dict(values)
        spectra_values = self._spectra_cfg_values()
        if self.style_panel.seed_spectra_values(spectra_values):
            self._spectra_preview_values = dict(spectra_values)

    def _on_spectra_preview(self, values: dict) -> None:
        """spectra 键即时预览（会话级；不写 cfg，需显式「写入出图参数」）。"""
        self._spectra_preview_values = dict(values or {})
        self._rebuild_working_view()

    def _on_write_plot_params(self, values: dict) -> None:
        """FR-33.3：显式写入 cfg + 保存 → 冒泡给主窗口广播/Φ_Δ 横幅。

        V25-B3：写入前校验（cmap_lo<cmap_hi、颜色可解析、数值范围），失败不写
        不 persist；V25-D1：spectra 键写回 cfg["spectra"] 与
        cfg["plot"]["spectra_overlay"]["cmap"]（FR-33.1 可映射键落地）。
        """
        values = dict(values or {})
        ok, msg = StylePanel.validate_values(values)
        if not ok:
            self.logged.emit("ERROR", f"写入出图参数被拒绝：{msg}")
            self.status.emit(f"出图参数未写入：{msg}")
            return
        try:
            jacs_values = {k: v for k, v in values.items() if k in MAPPED_KEYS}
            if jacs_values:
                self._jacs_section().update(jacs_values)
            spectra_values = {k: values[k] for k in SPECTRA_MAPPED_KEYS
                              if k in values and k != "spectra_overlay.cmap"}
            if spectra_values:
                self.cfg.setdefault("spectra", {}).update(spectra_values)
            spectra_cmap = values.get("spectra_overlay.cmap")
            if spectra_cmap:
                self.cfg.setdefault("plot", {}).setdefault(
                    "spectra_overlay", {})["cmap"] = str(spectra_cmap)
            self.persist_cfg()
            self.plot_params_written.emit(dict(values))
            self.logged.emit(
                "INFO", f"出图参数已写入 cfg（可映射键 {len(values)} 个，"
                        f"其中 spectra {len(spectra_values) + (1 if spectra_cmap else 0)} 个）"
                        " → Φ_Δ 页提示重渲受影响图")
            self.status.emit("出图参数已写入并广播")
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("ERROR", f"写入出图参数失败：{err}")

    # ══════════════ V2.9.5 FR-73：实时生图预览 ══════════════
    def _schedule_preview(self) -> None:
        try:
            self._preview_cfg = self._cfg_for_plot()
            self._preview_mode = self._mode_key(self.cmb_mode.currentIndex())
            self._preview_abs = self._pick(self.cmb_abs, "abs")
            self._preview_em = self._pick(self.cmb_em, "em")
            self._preview_despike = self.chk_despike.isChecked()
        except Exception:
            return
        self.live_preview.schedule()

    def _render_live_preview(self, out_dir) -> list:
        cfg = copy.deepcopy(getattr(self, "_preview_cfg", self._cfg_for_plot()))
        jacs = cfg.setdefault("plot", {}).setdefault("jacs", {})
        jacs.update({k: v for k, v in dict(getattr(self, "_mapped_preview_values", {}) or {}).items()
                     if k in MAPPED_KEYS})
        spectra_vals = {k: v for k, v in dict(getattr(self, "_spectra_preview_values", {}) or {}).items()
                        if k in SPECTRA_MAPPED_KEYS and k != "spectra_overlay.cmap"}
        if spectra_vals:
            cfg.setdefault("spectra", {}).update(spectra_vals)
        _cmap = dict(getattr(self, "_spectra_preview_values", {}) or {}).get("spectra_overlay.cmap")
        if _cmap:
            cfg.setdefault("plot", {}).setdefault("spectra_overlay", {})["cmap"] = str(_cmap)
        jacs["dpi"] = 100
        cfg["plot"]["jacs"]["formats"] = ["png"]
        mode = getattr(self, "_preview_mode", "overlay")
        good = [s for s in (self.spectra or []) if s.ok]
        if mode == "overlay":
            good = self._overlay_spectra()
        a, e = getattr(self, "_preview_abs", None), getattr(self, "_preview_em", None)
        if getattr(self, "_preview_despike", False):
            good = self._despike_spectra(good, dict(cfg.get("qc") or {}))
        res = run_spectra_plots(good, cfg, out_dir=out_dir, mode=mode,
                                abs_spec=a, em_spec=e)
        return list(getattr(res, "files", []) or [])

    # ══════════════ 出图 ══════════════
    def _on_mode(self, idx: int) -> None:
        need_pair = self._mode_key(idx) in ("mirror", "stacked")
        self.cmb_abs.setEnabled(need_pair)
        self.cmb_em.setEnabled(need_pair)
        self._update_run_enabled()
        # V2.6 FR-41.5：模式切换 → 单对标签/记录按钮同步
        if hasattr(self, "lbl_stokes"):
            self.refresh_stokes_label()

    def _update_run_enabled(self) -> None:
        """A18（审查附录 C）：**overlay 过滤后空集 → 出图按钮置灰 + 状态栏提示**。

        原实现只在 `_run` 里 `if not good: return`——按钮仍可点、点了毫无反应，
        属于"可预防的前置条件"没预防（对齐 FR-24.2 的反馈分级：常规提示走状态栏）。
        """
        mode = self._mode_key(self.cmb_mode.currentIndex())
        good = [s for s in (self.spectra or []) if s.ok]
        if mode == "overlay":
            n = len(self._overlay_spectra())
            hint = (f"当前过滤（{self._kind_label(self._overlay_kinds)}）下"
                    "没有可画的谱：请换过滤类型或载入对应数据")
        else:
            n = len(good)
            hint = "还没有可画的谱：请先载入数据目录"
        self.btn_run.setEnabled(n > 0)
        self.btn_run.setToolTip("" if n else hint)
        if n == 0 and good:
            self.status.emit(hint)

    def _mode_key(self, idx: int) -> str:
        """下拉索引 → 模式键（``_modes`` 为子集时索引与默认顺序不同）。"""
        return self._modes[idx] if 0 <= idx < len(self._modes) else "overlay"

    def _overlay_spectra(self) -> list:
        """叠图模式候选集：按 ``overlay_kinds`` 过滤（作图台按类型拆分用）。"""
        good = [s for s in self.spectra if s.ok]
        if self._overlay_kinds in ("all", "", None):
            return good
        return [s for s in good if s.kind == self._overlay_kinds]

    @staticmethod
    def _despike_spectra(spectra: list, qc_cfg: dict) -> list:
        """作图前去尖峰（与 Φ_Δ 同判据）：返回**浅拷贝**，不改原 Spectrum 数组。

        用 ``ui.prep.display.despiked_for_plot``：剔除点插值补上，原有 NaN 保持断点。
        """
        from ui.prep.display import despiked_for_plot
        out = []
        for s in spectra:
            try:
                if not s.ok:
                    out.append(s)
                    continue
                n = copy.copy(s)
                n.y = despiked_for_plot(s.y, s.wl, qc_cfg or {}, s.path.name)
                out.append(n)
            except Exception:                              # noqa: BLE001 —— 失败保留原谱
                out.append(s)
        return out

    def _pick(self, cmb: QComboBox, kind: str) -> Optional[Spectrum]:
        name = cmb.currentText()
        if not name or name == "（无）":
            return None
        for s in self.spectra:
            if s.ok and s.kind == kind and s.path.name == name:
                return s
        return None

    def _run(self) -> None:
        mode = self._mode_key(self.cmb_mode.currentIndex())
        good = [s for s in self.spectra if s.ok]
        if mode == "overlay":
            target = self._overlay_spectra()
            skipped = len(good) - len(target)
            if skipped > 0:
                self.logged.emit(
                    "INFO", f"本页只画{self._kind_label(self._overlay_kinds)}："
                            f"已忽略 {skipped} 支其他类型光谱")
            good = target
        if not good:
            self.status.emit("没有可画的谱：请先载入数据或调整过滤类型")
            return
        cfg = self._cfg_for_plot()
        abs_s = em_s = None
        if mode in ("mirror", "stacked"):
            abs_s = self._pick(self.cmb_abs, "abs")
            em_s = self._pick(self.cmb_em, "em")
            if abs_s is None or em_s is None:
                self.logged.emit("ERROR", "镜像图需要同时指定一支吸收谱与一支发射谱")
                return
        # G13 教训：控件状态在 UI 线程先取好，再进工作线程
        do_despike = self.chk_despike.isChecked()
        qc_cfg = dict(cfg.get("qc") or {})

        def task(progress_cb, log_cb):
            plots = good
            a, e = abs_s, em_s
            if do_despike:
                plots = self._despike_spectra(plots, qc_cfg)
                if a is not None and e is not None:
                    by_name = {s.path.name: s for s in plots}
                    a = by_name.get(a.path.name, a)
                    e = by_name.get(e.path.name, e)
            return run_spectra_plots(plots, cfg, mode=mode, abs_spec=a,
                                     em_spec=e, progress_cb=progress_cb,
                                     log_cb=log_cb)

        self.run_task(task, "生成定稿图", self._on_run_done,
                      busy_widgets=self.generation_busy_widgets())
        # V25-B5：任务结束（成功/失败/取消）后恢复依赖关系控件状态
        if getattr(self, "job", None) is not None:
            self.job.finished.connect(self._restore_generation_lock_state)

    def generation_busy_widgets(self) -> list:
        """生图期间要锁定的控件清单（V25-B5；验收/测试可直接断言内容）。"""
        return [self.btn_run, self.btn_files, self.btn_dir,
                self.live, self.cmb_view, self.cmb_mode, self.cmb_norm,
                self.chk_despike, self.btn_export_view,
                self.style_panel, self.chk_readout, self.cmb_annot,
                self.btn_record_pair]

    def _restore_generation_lock_state(self) -> None:
        """生图结束（成功/失败/取消）后恢复依赖关系控件的状态（V25-B5）。"""
        try:
            self._on_mode(self.cmb_mode.currentIndex())
        except RuntimeError:                           # 控件已销毁（页面关闭）
            pass
        try:
            self.refresh_stokes_label()                # 记录按钮按忙闲/配对恢复
        except Exception:                              # noqa: BLE001
            pass

    @staticmethod
    def _kind_label(kind: str) -> str:
        return {"abs": "紫外吸收谱", "em": "荧光发射谱"}.get(kind, "全部类型光谱")

    # ══════════════ V2.8 FR-53：④ 显示预设（presets.yaml spectra 节；config.yaml 零新键） ══════════════
    def display_preset_values(self) -> dict:
        """当前 ④光谱图显示设置（差量 dict；与 presets.yaml spectra 节同形）。"""
        return {
            "mode": self.cmb_mode.currentText(),
            "norm": self.cmb_norm.currentText(),
            "view": self.cmb_view.currentText(),
            "despike": bool(self.chk_despike.isChecked()),
        }

    def apply_display_preset(self, values: dict) -> None:
        """套用 ④ 显示预设（未知键忽略；不写 config.yaml）。"""
        v = dict(values or {})
        for key, widget in (("mode", self.cmb_mode),
                            ("norm", self.cmb_norm),
                            ("view", self.cmb_view)):
            if key in v:
                idx = widget.findText(str(v[key]))
                if idx >= 0:
                    widget.setCurrentIndex(idx)
        if "despike" in v:
            self.chk_despike.setChecked(bool(v["despike"]))
        if getattr(self, "working_scans", None) is not None:
            try:
                self._rebuild_working_view()
            except Exception:                    # noqa: BLE001 —— 无数据时忽略
                pass

    def _cfg_for_plot(self) -> dict:
        """把本页选项写进 cfg（**深拷贝**，绝不改共享主 cfg）。

        A17（V2.x 审查，V2.1 新功能缺陷）：原实现只复制 ``spectra/plot/gui/io``
        四节 → **丢 ``qc``**，作图台"去尖峰"于是用默认 ``qc.outlier`` 判据而不是
        用户配置；更糟的是复制的是**引用**，随后写 ``cfg["spectra"]["normalize"]``
        **原地污染了共享主 cfg**（本页选项泄漏到分析/导出）。
        现在：整份深拷贝 + 只改副本。
        """
        import copy as _copy

        cfg = _copy.deepcopy(self.cfg)
        # V2.5-C：保留 gui 节（out_root 等）——生图产物跟随用户输出根
        cfg["gui"] = _copy.deepcopy((self.cfg or {}).get("gui") or {})
        cfg.setdefault("spectra", {})
        cfg["spectra"]["normalize"] = ("none"
                                       if self.cmb_norm.currentIndex() == 1
                                       else "max")
        return cfg

    def _on_run_done(self, res) -> None:
        """出图完成的收尾（不能叫 `_on_done`——见 stack_page 同名说明）。"""
        if res.out_dir:
            self.export.set_run_dir(res.out_dir)
            self._final_figs = list(Path(res.out_dir).glob("*.png"))
            self.canvas.set_images(self._final_figs)
            self.cmb_view.setCurrentIndex(1)          # 显式生图完成 → 展示定稿预览
        if not res.ok:
            self.logged.emit("ERROR", res.message or "出图失败")
            return
        # V2.6 FR-41.5：单对语义标签（mirror/stacked）或 auto 多对逐行
        mode = self._mode_key(self.cmb_mode.currentIndex())
        stokes = list(getattr(res, "stokes", None) or [])
        if mode == "auto":
            self._auto_pair_lines = self._auto_pair_label_lines(stokes)
        else:
            self._auto_pair_lines = []
        self.refresh_stokes_label()
        for item in stokes:
            try:
                la, le, sh = float(item[0]), float(item[1]), float(item[2])
            except (TypeError, ValueError, IndexError):
                continue
            nu = (1.0e7 / la - 1.0e7 / le
                  if np.isfinite(la) and np.isfinite(le) and la > 0 and le > 0
                  else float("nan"))
            self.logged.emit("INFO",
                             f"斯托克斯位移 = {sh:.1f} nm "
                             f"(= λem {le:.1f} − λabs {la:.1f})；"
                             f"Δν̃ = {self._fmt_nu(nu)} cm⁻¹")
        self.logged.emit("INFO", f"出图完成，共 {len(res.files)} 个文件 → {res.out_dir}")
        self.status.emit(f"常规作图完成 — {res.out_dir}")

    def _calc_eps(self) -> None:
        """FR-4.4（可选二期）：ε = A/(c·l)。"""
        eps = molar_extinction(self.sp_a.value(), self.sp_c.value(), self.sp_l.value())
        txt = f"ε = {eps:.4g} L·mol⁻¹·cm⁻¹" if eps == eps else "ε = —（请检查 c、l > 0）"
        self.lbl_eps.setText(txt)
        self.logged.emit("INFO", txt)
