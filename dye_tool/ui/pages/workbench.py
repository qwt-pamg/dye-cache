# -*- coding: utf-8 -*-
"""作图台页面的共同部分（堆叠/时间演化页 :mod:`ui.pages.stack`、紫外/荧光页
:mod:`ui.pages.spectra`）。

两页都是混搭工作台：顶部流程条 ``载入 → 样式 → 导出``；左栏是载入的文件清单，
中栏是结论条 + 工作视图（活画布）/定稿预览 + 结果区，右栏是分步检查器（只展开当前
这一步的设置，主按钮「▶ 生成定稿图」常驻底部）。下面这些行为两页共用：

* 三栏骨架、流程条与检查器切步、每步摘要、完成/需要重算标记、结论条；
* 工作视图 ⇄ 定稿预览切换、按当前视图导出；
* 样式面板：播种可映射键、即时预览、撤销、写入出图参数之外的"另存为预设 / 套用预设 /
  高级…"；
* 显示预设（presets.yaml 的 ``stack`` / ``spectra`` 节）的列出、套用、保存。

子类设置 :attr:`PRESET_SECTION` 与 :attr:`PAGE_MARK`，先建自己的数据控件，再调
:meth:`_build_shared` 与 :meth:`_assemble`；需要在套用预设时同步额外控件（如堆叠页的
色图框）就覆盖 :meth:`_apply_loaded_style`。

作图台壳页（:mod:`ui.pages.plot`）用 :meth:`top_bar_widget` 把流程条挪进自己的顶部细条；
经典布局直接把本页当页签用，流程条留在页内顶部。
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QGridLayout,
                               QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QPushButton, QScrollArea,
                               QStackedWidget, QVBoxLayout, QWidget)

from ui.pages.base import PageBase
from ui.widgets.export_bar import ExportBar
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.live_plot import LivePlotCanvas
from ui.widgets.live_render_preview import LiveRenderPreview
from ui.widgets.mpl_canvas import CanvasPanel
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.style_panel import MAPPED_KEYS, StylePanel
from ui.widgets.verdict import VerdictCard
from ui import theme

#: 流程条三步；检查器第 i 页、:meth:`WorkbenchPage._step_summary` 都按这个顺序
STEPS = ("载入", "样式", "导出")
STEP_LOAD, STEP_STYLE, STEP_EXPORT = range(len(STEPS))
STEP_HINTS = (
    "选数据，再定画哪些组、画哪种图；载入的文件列在左栏",
    "曲线样式与显示预设只改工作视图；要带进定稿图，到「导出」写入出图参数",
    "定稿图由分析流水线生成（JACS 版式）；工作视图也能另存 png/svg",
)
#: 左栏（文件清单）宽度范围
LEFT_MIN_W, LEFT_MAX_W = 260, 420
#: 顶部细条高度（与分析页顶部细条一致）
TOP_BAR_H = 44


def scroll_page(widget: QWidget) -> QScrollArea:
    """检查器页的统一外壳：只竖向滚动、无边框（高度不够时整页滚，不裁按钮）。"""
    sc = QScrollArea()
    sc.setWidget(widget)
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return sc


class WorkbenchPage(PageBase):
    """作图台页面基类（不单独实例化）。"""

    #: presets.yaml 里的显示预设分区名，也用作工作视图导出文件名的一部分
    PRESET_SECTION = ""
    #: 状态栏文案里的页序号（如「③」）
    PAGE_MARK = ""
    #: 左栏（文件清单）初始宽度
    LEFT_W = 300
    #: 「写入出图参数」后向主窗口冒泡（FR-33.3：广播 cfg + Φ_Δ 过期横幅）
    plot_params_written = Signal(dict)

    # ══════════════ 子类钩子 ══════════════
    def _apply_loaded_style(self, vals: dict) -> None:
        """套用预设 / 高级对话框返回后同步页面专有控件（默认无）。"""

    def _has_data(self) -> bool:
        """是否已载入可作图的数据（流程条「载入」步是否完成）。"""
        return False

    def _load_summary(self) -> str:
        """「载入」步的一行摘要。"""
        return ""

    def _style_summary(self) -> str:
        """「样式」步的一行摘要。"""
        return ""

    def _export_spec(self) -> tuple[list, int]:
        """定稿图的 ``(格式表, dpi)``（只读 cfg，用于导出步的说明行）。"""
        jacs = ((self.cfg or {}).get("plot") or {}).get("jacs") or {}
        return list(jacs.get("formats") or ["png"]), int(jacs.get("dpi") or 600)

    def _update_verdict(self) -> None:
        """按当前数据刷新结论条（默认：空态 = 本页标题 + 说明）。"""
        self._verdict_empty()

    # ══════════════ 构建 ══════════════
    def _build_shared(self, *, with_spectra: bool = False) -> None:
        """两页共用的控件：主按钮、实时预览、工作视图/定稿预览、样式面板、显示预设、导出条。

        子类先建自己的数据控件，再调本方法，最后调 :meth:`_assemble` 摆成三栏。
        """
        self._final_figs: list = []
        self._final_stale = False
        self.btn_run = QPushButton("▶ 生成定稿图")
        theme.mark(self.btn_run, "primary")
        self.btn_run.clicked.connect(self._run)
        self.btn_run.setEnabled(False)
        self.chk_live = QCheckBox("实时生图预览")
        self.chk_live.setChecked(True)
        self.lbl_live = QLabel("预览待命")
        theme.mark(self.lbl_live, "hint")

        # V2.5 FR-32/34：工作视图（活画布）与定稿预览分离
        self.live = LivePlotCanvas(cfg=self.cfg)
        self.live.readout.connect(self.status)
        self.canvas = CanvasPanel()
        self.live_preview = LiveRenderPreview(self, self._render_live_preview)
        self.live_preview.updated.connect(self.canvas.set_images)
        self.live_preview.state.connect(self.lbl_live.setText)
        self.live_preview.failed.connect(lambda m: self.logged.emit("WARNING", f"实时预览失败：{m}"))
        self.chk_live.toggled.connect(self.live_preview.set_enabled)
        self.view_stack = QStackedWidget()
        self.view_stack.addWidget(self.live)            # 0 = 工作视图
        self.view_stack.addWidget(self.canvas)          # 1 = 定稿预览
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
            f"把工作视图（含标注）导出为 workspace_{self.PRESET_SECTION}_<时间>.png/svg；\n"
            "不进 run 产物、绝不覆盖 fig_*/jacs_*、零 PDF（filter_formats 守）")
        self.btn_export_view.clicked.connect(self._export_working_view)

        # 样式面板：面板主体可滚动；动作行一行六个按钮，右栏放不下 → 拆成两组
        self.style_panel = StylePanel(with_spectra=with_spectra)
        self.style_scroll = scroll_page(self.style_panel)
        # 曲线表与可映射键表单比右栏略宽：允许横向滚动，别把字段裁掉
        self.style_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.style_edit_actions, self.style_actions = self._split_style_actions()
        self.style_panel.style_changed.connect(self._on_live_style)
        self.style_panel.mapped_changed.connect(self._on_mapped_preview)
        for _sig in (self.style_panel.style_changed, self.style_panel.mapped_changed):
            _sig.connect(lambda *_: self._schedule_preview())
        self.style_panel.write_requested.connect(self._on_write_plot_params)
        self.style_panel.preset_requested.connect(self._on_save_plot_preset)
        self.style_panel.advanced_requested.connect(self._on_advanced_style)
        # V25-B4：非法颜色/样式被拒绝 → 状态栏 + 日志提示（不崩、不改画布）
        self.live.style_rejected.connect(self._on_style_rejected)
        self.style_panel.edit_rejected.connect(self._on_style_rejected)

        # V2.8 FR-53：显示预设（presets.yaml 的本页分区；config.yaml 零新键）
        self.cmb_display_preset = QComboBox()
        self.cmb_display_preset.setToolTip(
            f"套用 presets.yaml 的 {self.PRESET_SECTION} 节显示预设（只影响本页视图）")
        self.btn_display_preset_save = QPushButton("保存…")
        self.btn_display_preset_save.setToolTip(
            f"把当前{self.PAGE_MARK}显示设置存为命名预设（presets.yaml）")
        self.btn_display_preset_save.clicked.connect(self._save_display_preset_dialog)
        self.cmb_display_preset.currentIndexChanged.connect(self._on_display_preset_chosen)
        self._refresh_display_presets()

        # V2.3 FR-24.2/24.6：导出条与画布的常规反馈走状态栏（不弹模态）
        self.export = ExportBar()
        self.export.message.connect(self.status)
        self.canvas.status_message.connect(self.status)
        self.verdict = VerdictCard()
        self.lbl_export_spec = QLabel("")
        theme.mark(self.lbl_export_spec, "muted")
        self.lbl_export_spec.setWordWrap(True)

    def _split_style_actions(self) -> tuple[QWidget, QWidget]:
        """样式面板的动作行拆成两组：编辑（撤销 / 重置）留在「样式」，出图参数去「导出」。"""
        sp = self.style_panel
        old = sp.take_actions_widget()
        edit = QWidget()
        le = QHBoxLayout(edit)
        le.setContentsMargins(0, 0, 0, 0)
        for b in (sp.btn_undo, sp.btn_reset_current, sp.btn_reset_all):
            le.addWidget(b)
        params = QWidget()
        lp = QGridLayout(params)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.addWidget(sp.btn_write, 0, 0, 1, 2)
        lp.addWidget(sp.btn_preset, 1, 0)
        lp.addWidget(sp.btn_advanced, 1, 1)
        old.deleteLater()
        return edit, params

    def _assemble(self, *, left: QWidget, load_page: QWidget,
                  style_top: Optional[QWidget] = None,
                  results: Optional[QWidget] = None) -> None:
        """摆三栏：顶部流程条 / 左文件清单 · 中结论条+图件+结果区 · 右分步检查器。

        ``load_page`` 是「载入」步的检查器内容；``style_top`` 放在「样式」步最上面
        （如堆叠页的色带与标注）；``results`` 放在中栏图件下方、导出条之上。
        """
        self._build_top_bar()
        left.setMinimumWidth(LEFT_MIN_W)
        left.setMaximumWidth(LEFT_MAX_W)
        center = self._build_center(results)
        inspector = self._build_inspector(load_page, style_top)
        self.body = three_columns(left, center, inspector)
        self.body.setSizes([self.LEFT_W, 820, 330])
        self._left = left
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 8)
        lay.setSpacing(6)
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        lay.addWidget(self.top_bar)
        lay.addWidget(self.body, 1)
        lay.addWidget(self.progress_row())
        self._go_step(STEP_LOAD)
        self._update_export_spec()
        self._update_verdict()
        self._update_flow()

    def _build_top_bar(self) -> None:
        """流程条（作图台壳页把整条挪进它自己的顶部细条；经典布局留在页内）。"""
        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        self.top_bar = QWidget()
        self.top_bar.setObjectName("workbenchTopBar")
        self.top_bar.setFixedHeight(TOP_BAR_H)
        lay = QHBoxLayout(self.top_bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.flow_bar, 1)

    def top_bar_widget(self) -> QWidget:
        """顶部细条插槽的约定：交出流程条（同一对象，每次都返回它）。"""
        return self.top_bar

    def _build_center(self, results: Optional[QWidget]) -> QWidget:
        """中栏：结论条 / 视图工具行 + 工作视图或定稿预览 / 结果区 + 导出条。"""
        view_bar = QHBoxLayout()
        view_bar.addWidget(QLabel("视图"))
        view_bar.addWidget(self.cmb_view)
        view_bar.addWidget(self.chk_readout)
        view_bar.addWidget(self.cmb_annot)
        view_bar.addStretch(1)
        box_fig = QGroupBox("图件")
        lfig = QVBoxLayout(box_fig)
        lfig.addLayout(view_bar)
        lfig.addWidget(self.view_stack, 1)
        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(box_fig, 1)
        if results is not None:
            lay.addWidget(results)
        lay.addWidget(self.export)
        center.setMinimumWidth(420)
        return center

    def _build_inspector(self, load_page: QWidget,
                         style_top: Optional[QWidget]) -> StepInspector:
        """右栏检查器：载入（子类给）/ 样式（显示预设 + 曲线样式）/ 导出（出图参数 + 定稿）。"""
        # ② 样式
        p_style = QWidget()
        ls = QVBoxLayout(p_style)
        ls.setContentsMargins(0, 0, 0, 0)
        if style_top is not None:
            ls.addWidget(style_top)
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("显示预设"))
        preset_row.addWidget(self.cmb_display_preset, 1)
        preset_row.addWidget(self.btn_display_preset_save)
        ls.addLayout(preset_row)
        lbl = QLabel("曲线样式（仅本次会话）")
        theme.mark(lbl, "strong")
        ls.addWidget(lbl)
        ls.addWidget(self.style_scroll, 1)
        ls.addWidget(self.style_edit_actions)
        # ③ 导出
        p_export = QWidget()
        lx = QVBoxLayout(p_export)
        lx.setContentsMargins(0, 0, 0, 0)
        box_params = QGroupBox("出图参数")
        lb = QVBoxLayout(box_params)
        lb.addWidget(self.lbl_export_spec)
        lb.addWidget(self.style_actions)
        lx.addWidget(box_params)
        box_view = QGroupBox("工作视图")
        lv = QVBoxLayout(box_view)
        note = QLabel("把当前工作视图（含标注）另存 png/svg，不进 run 产物。")
        theme.mark(note, "hint")
        note.setWordWrap(True)
        lv.addWidget(note)
        lv.addWidget(self.btn_export_view)
        lx.addWidget(box_view)
        live = QHBoxLayout()
        live.addWidget(self.chk_live)
        live.addWidget(self.lbl_live, 1)
        lx.addLayout(live)
        lx.addStretch(1)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((scroll_page(load_page), p_style, scroll_page(p_export))):
            ins.set_page(i, w)
        ins.add_action(self.btn_run)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        return ins

    # ══════════════ 流程条 / 检查器 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：流程条高亮、检查器换页（图件始终留在中栏）。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        self.flow_bar.set_current(idx)
        self.inspector.set_current(idx, next_enabled=idx + 1 < len(STEPS))

    def _step_summary(self, idx: int) -> str:
        """某一步当前设置的一行摘要（只读已有状态）。"""
        if idx == STEP_LOAD:
            return self._load_summary()
        if idx == STEP_STYLE:
            return self._style_summary()
        fmts, dpi = self._export_spec()
        n = len(getattr(self, "_final_figs", None) or [])
        head = f"已出 {n} 张" if n else "未生成定稿图"
        return f"{head} · {'+'.join(fmts)} {dpi} dpi"

    def _update_export_spec(self) -> None:
        from core.render.plotstyle import filter_formats
        fmts, dpi = self._export_spec()
        self.lbl_export_spec.setText(
            f"定稿图：{'、'.join(filter_formats(fmts))} · {dpi} dpi（在「高级…」里改）。\n"
            "「写入出图参数」把样式里的可映射键存进 config.yaml；"
            "「另存为预设」存进 presets.yaml。")

    def _update_flow(self) -> None:
        """按数据 / 工作视图 / 定稿图状态给三步打 完成 / 需要重算 / 未完成 标记。"""
        if not hasattr(self, "flow_bar"):
            return
        loaded = self._has_data()
        drawn = loaded and bool(self.live.curve_info())
        final = bool(getattr(self, "_final_figs", None))
        self.flow_bar.set_state(STEP_LOAD, STEP_DONE if loaded else STEP_TODO)
        self.flow_bar.set_state(STEP_STYLE, STEP_DONE if drawn else STEP_TODO)
        self.flow_bar.set_state(
            STEP_EXPORT, (STEP_STALE if self._final_stale else STEP_DONE) if final else STEP_TODO)
        self.inspector.refresh_summaries()

    def _after_working_view(self) -> None:
        """工作视图重画后：定稿图已出则标"需要重算"，并刷新流程条与结论条。"""
        if getattr(self, "_final_figs", None):
            self._final_stale = True
        self._update_verdict()
        self._update_flow()

    def _mark_final(self, figs: list) -> None:
        """定稿图出完：记下图件、清掉"需要重算"。"""
        self._final_figs = list(figs or [])
        self._final_stale = False
        self._update_verdict()
        self._update_flow()

    def _verdict_empty(self) -> None:
        self.verdict.set_empty(self.TITLE.lstrip("①②③④⑤ "), self.SUBTITLE)

    def _verdict_final_line(self) -> str:
        """结论条里关于定稿图的一行（没出过则为空）。"""
        n = len(getattr(self, "_final_figs", None) or [])
        if not n:
            return ""
        if self._final_stale:
            return f"定稿图 {n} 张已过期：参数改过，需要重新生成"
        return f"定稿图 {n} 张已生成（切到「定稿预览」查看）"

    def on_cfg_changed(self) -> None:
        """主窗口换了 cfg：导出步的格式 / dpi 说明跟着刷新。"""
        if hasattr(self, "lbl_export_spec"):
            self._update_export_spec()
            self._update_flow()

    # ══════════════ 共用动作 ══════════════
    def _choose_dir(self) -> None:
        d = self.pick_dir()
        if d:
            self._load_dir(d)

    def _jacs_section(self) -> dict:
        plot = self.cfg.setdefault("plot", {})
        return plot.setdefault("jacs", {})

    def _on_view_changed(self, idx: int) -> None:
        """工作视图 ⇄ 定稿预览（定稿预览**不触发**生成）。"""
        self.view_stack.setCurrentIndex(1 if idx == 1 else 0)
        if idx == 1 and not getattr(self, "_final_figs", None):
            self.status.emit("尚未生图：请先点「生成定稿图」")

    def _working_cmap_range(self) -> tuple:
        """色带区间：默认 0.05/0.95；风格栏「可映射键」即时预览优先（会话级）。"""
        values = getattr(self, "_mapped_preview_values", {}) or {}
        try:
            lo = float(values.get("cmap_lo", 0.05))
            hi = float(values.get("cmap_hi", 0.95))
        except (TypeError, ValueError):
            lo, hi = 0.05, 0.95
        if not (0.0 <= lo < hi <= 1.0):
            lo, hi = 0.05, 0.95
        return lo, hi

    def _export_working_view(self) -> None:
        """按当前工作视图导出 workspace_<页>_*.png/svg（零 PDF、不覆盖 fig_*）。"""
        from datetime import datetime

        from core.paths import resolve_out_root
        from core.render.plotstyle import filter_formats
        out_root = resolve_out_root(self.cfg)
        stem = f"workspace_{self.PRESET_SECTION}_" + datetime.now().strftime("%H%M%S")
        made = []
        for ext in filter_formats(["png", "svg"]):
            p = out_root / f"{stem}.{ext}"
            i = 2
            while p.exists():
                p = out_root / f"{stem}-{i}.{ext}"
                i += 1
            got = self.live.save_figure(p)
            if got:
                made.append(got)
        if made:
            names = "、".join(Path(x).name for x in made)
            self.logged.emit("INFO", f"工作视图已导出：{names} → {out_root}")
            self.status.emit(f"工作视图导出完成：{names}")
        else:
            self.logged.emit("WARNING", "工作视图导出失败（无数据或写盘失败）")

    def _sync_style_panel(self) -> None:
        if hasattr(self, "style_panel"):
            self.style_panel.set_curves(self.live.curve_info())
        # V25-B3：用 cfg 播种可映射键（用户已编辑过则不覆盖）
        self._seed_style_panel()

    def _on_live_style(self, key: str, style: dict) -> None:
        """编辑 → 活画布 artist 属性（不触发渲染器；FR-33.2）。"""
        self.live.set_style(key, **(style or {}))
        if getattr(self, "_final_figs", None):
            self._final_stale = True
            self._update_verdict()
            self._update_flow()

    def _on_style_rejected(self, msg: str) -> None:
        """非法样式/颜色被拒绝 → 状态栏 + 日志提示（V25-B4：不崩、不改画布）。"""
        self.logged.emit("WARNING", str(msg))
        self.status.emit(str(msg))

    def _on_mapped_preview(self, values: dict) -> None:
        """可映射键即时预览（会话级；不写 cfg，需显式「写入出图参数」）。"""
        self._mapped_preview_values = dict(values or {})
        self._rebuild_working_view()

    def _on_save_plot_preset(self, values: dict) -> None:
        """FR-33.4：另存为 presets.yaml v2 的 plot 预设（V2.4 机制复用）。"""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "另存为预设", "预设名：")
        name = str(name or "").strip()
        if not ok or not name:
            return
        try:
            from core.render import plotstyle
            plotstyle.save_user_preset(name, dict(values or {}))
            self.logged.emit("INFO", f"出图预设已另存：{name}"
                                     "（presets.yaml v2 plot 节）")
            self._apply_preset_to_working(name)
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("ERROR", f"另存为预设失败：{err}")

    def _apply_preset_to_working(self, name: str) -> None:
        """套用预设到工作视图（只影响显示；不触发渲染器）。

        V2.5 修复（FR-33.4）：改用 plotstyle.load_preset（先内置、后用户
        presets.yaml）。原先用 apply_preset 只认内置预设，用户「另存为预设」写进
        presets.yaml 的名字会 KeyError 被 except 吞掉 → 套用实际失效。
        """
        try:
            from core.render import plotstyle
            loaded = plotstyle.load_preset(name)
            if not isinstance(loaded, dict):
                raise ValueError(f"未知预设：{name}")
            base = dict(plotstyle.jacs_defaults())
            base.update(loaded)
            values = {k: base[k] for k in MAPPED_KEYS if k in base}
            if values:
                self.style_panel.set_mapped_values(values)
                self._mapped_preview_values = dict(values)
            self._apply_loaded_style(loaded)
            self._rebuild_working_view()
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("WARNING", f"套用预设到工作视图失败：{err}")

    def _on_advanced_style(self) -> None:
        """「高级…」→ 复用既有出图风格对话框（完整 JACS_SCHEMA）。"""
        try:
            from ui.widgets.plot_style_dialog import PlotStyleDialog
            dlg = PlotStyleDialog(copy.deepcopy(self.cfg), self)
            try:
                dlg.exec()
                vals = dlg.values() if hasattr(dlg, "values") else {}
            finally:
                dlg.deleteLater()
            if isinstance(vals, dict) and vals:
                picked = {k: vals[k] for k in MAPPED_KEYS if k in vals}
                if picked:
                    self.style_panel.set_mapped_values(picked)
                    self._mapped_preview_values = dict(picked)
                self._apply_loaded_style(vals)
                self._rebuild_working_view()
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("WARNING", f"高级出图对话框不可用：{err}")

    def _refresh_display_presets(self) -> None:
        from core.render.plotstyle import list_display_presets
        self.cmb_display_preset.blockSignals(True)
        self.cmb_display_preset.clear()
        self.cmb_display_preset.addItem("（当前设置）")
        self.cmb_display_preset.addItems(list_display_presets(self.PRESET_SECTION))
        self.cmb_display_preset.blockSignals(False)

    def _on_display_preset_chosen(self, idx: int) -> None:
        if idx <= 0:
            return
        from core.render.plotstyle import load_display_preset
        name = self.cmb_display_preset.itemText(idx)
        vals = load_display_preset(self.PRESET_SECTION, name)
        if vals is not None:
            self.apply_display_preset(vals)
            self.status.emit(f"已套用{self.PAGE_MARK}显示预设：{name}")

    def _save_display_preset_dialog(self) -> None:
        from core.render.plotstyle import save_display_preset
        name, ok = QInputDialog.getText(self, f"保存{self.PAGE_MARK}显示预设", "预设名：")
        if not ok or not name.strip():
            return
        save_display_preset(self.PRESET_SECTION, name.strip(), self.display_preset_values())
        self._refresh_display_presets()
        self.status.emit(f"已保存{self.PAGE_MARK}显示预设：{name.strip()}")
