# -*- coding: utf-8 -*-
"""功能① Φ_Δ 一站式分析（FR-1）。

流水线：QC → 去尖峰 → 基线 → 背景扣除 → A(t) → 归一化 → 准一级拟合
（ln 空间，自动截断）→ Φ_Δ（误差传递）→ 对照判定，随后出图 a–e +
JACS 堆叠图，并导出 ``results.csv`` / ``fit_summary.json`` / ``report.md`` /
``run.log`` / ``figures/``。
"""
from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QComboBox, QDoubleSpinBox, QFormLayout, QFrame,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton, QScrollArea, QSplitter, QStackedWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.io import collect_scans
from core.pipeline import rerender_figures, run_directory
from ui.pages.base import PageBase
from ui.widgets.batch_tree import BatchTree
from ui.widgets.dye_time_panel import DyeTimePanel
from ui.widgets.export_bar import ExportBar, open_folder
from ui.widgets.file_table import ROLE_ZH, FileTable
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.mpl_canvas import CanvasPanel
from ui.widgets.param_panel import ParamPanel
from ui.widgets.result_actions import ResultActions
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.verdict import VerdictCard
from .backfill import FigureBackfill
from .figures import figure_names
from .settings_panel import UnifiedSettingsPanel
from .stale import StaleBanner
from ui import theme

log = logging.getLogger("dye_tool.ui.phi")


RESULT_HEADERS = ["组别", "角色", "k ± σk (s⁻¹)", "R²", "n", "A(0)",
                  "Φ_Δ ± σ", "状态 / 备注"]


#: 流程条的五步；检查器第 i 页、:meth:`PhiPage._step_summary` 都按这个顺序
STEPS = ("导入", "分组与 QC", "拟合 k_obs", "Φ_Δ", "出图导出")
STEP_HINTS = (
    "选数据目录；文件清单里可以勾掉不要的谱",
    "给每组起染料名、修正识别错的时间；只影响本次分析",
    "改了这里的参数需要重新分析（F5）",
    "这里改的只重算 Φ_Δ，不会重新拟合 k_obs",
    "图式与导出格式；定稿出图生成 600 dpi 的投稿图",
)

#: 页面宽度低于它时批次树默认收起（顶部「批次」按钮可展开）
NARROW_PAGE_W = 1180


class PhiPage(PageBase):
    """Φ_Δ 全流程页：流程条 + 批次树 / 结论卡与图件 / 分步检查器。

    流程条只决定"现在看哪一步"，不强制顺序：检查器换成该步能改的参数，中栏在
    导入/分组两步显示文件清单，其余步显示图件。「开始分析」默认只出**预览档**
    （FR-6.1），「定稿出图」产出投稿图件。
    """

    TITLE = "① 单线态氧（k_obs → Φ_Δ）"
    SUBTITLE = ("选择数据 → 逐组拟合 k_obs → 指定参比与 Φ_ref → 得到 Φ_Δ 与图件。"
                "改参比只重算 Φ_Δ，不会重新拟合 k_obs。")

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.scans: list = []
        self._last_rr = None
        self._last_result_text: str = ""
        self._time_overrides: dict = {}
        self.backfill = FigureBackfill(self)    # P1：预览档其余图件的后台补渲
        # FR-24.9：①数据页整定单向同步的预置载荷（下一次 _on_loaded 消费）
        self._pending_overrides: dict = {}
        #: 最近一次产物档位（"preview" | "final"）：重渲时按它选输出目录，
        #: 避免"已定稿后再重渲把画布降回预览分辨率"（A09）。
        self._last_export_mode: str = "preview"
        self._pending_dye: dict = {}
        self._build()
        self.setAcceptDrops(True)
        # V2.9.3 FR-68：参比/Φ_ref 悬停说明 + 灰按钮动态原因
        from ui.help_texts import PARAM_HELP, bind_disabled_reason
        self.cmb_reference.setToolTip("选任一组为参比（UI 指定优先于 ref- 前缀）")
        self.sp_phi_ref.setToolTip(PARAM_HELP["phi_ref"])
        self.btn_run.setToolTip("跑 Φ_Δ 流水线（预览图；不重新计算对比链）")
        self._run_reason = bind_disabled_reason(
            self.btn_run, lambda: ("" if self.data_dir else "先选择数据目录")
            or ("" if self.scans else "没有可用的光谱数据"))

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条 / 左批次树 · 中结论卡+画布+结果 · 右检查器。"""
        self._build_controls()
        self._build_top_bar()
        left = self._build_tree_column()
        center = self._build_center_column()
        self._build_settings_column()
        inspector = self._build_inspector()
        body = self._build_body(left, center, inspector)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        lay.addWidget(self.top_bar)
        # A25.1（V2.3 §12）：结果过期横幅（参数改了但结果没重算时出现，非模态）
        self.banner = StaleBanner()
        self.banner.action_requested.connect(self._on_banner_action)
        lay.addWidget(self.banner)
        lay.addWidget(body, 1)
        lay.addWidget(self.progress_row())

        # A25.2（V2.3 §12）：结果表 / 批次树右键菜单；文件表与画布的右键由各自组件实现
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_result_context_menu)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.files.status_message.connect(self.status)
        # 勾选状态来自配置（gui.lazy_preview），改动即持久化到 config.yaml
        self.chk_lazy.blockSignals(True)
        self.chk_lazy.setChecked(self._lazy_enabled())
        self.chk_lazy.blockSignals(False)
        self._go_step(0)
        self._update_flow()

    def _build_controls(self) -> None:
        """检查器各步用到的按钮与输入（目录 / 参比与 Φ_ref / 预览开关 / 主按钮）。"""
        self.btn_dir = QPushButton("选择数据目录…")
        self.btn_reload = QPushButton("重新载入")
        self.btn_reload.setToolTip("清空解析缓存后重新读取当前目录（Ctrl+R）")
        self.btn_run = QPushButton("▶ 开始分析")
        theme.mark(self.btn_run, "primary")
        self.btn_final = QPushButton("定稿出图")
        theme.mark(self.btn_final, "primary")
        self.btn_final.setToolTip(
            "用刚才的数据直接重渲 600 dpi 的 png 到 figures/（不再重新读文件）\n"
            "产物与「直接全量跑」完全一致，是投稿用的那一份。")
        self.btn_final.setEnabled(False)
        # V2.9.1 FR-61：显式参比组（默认自动按 ref- 前缀/角色识别；UI 指定优先）
        self.cmb_reference = QComboBox()
        self.cmb_reference.currentIndexChanged.connect(self._on_reference_changed)
        self._reference_group = ""
        # V2.9.1 O1：Φ_Δ 页自己的 Φ_ref（默认读 config，可记住到 dye_batch.yaml）
        self.sp_phi_ref = QDoubleSpinBox()
        self.sp_phi_ref.setRange(0.0001, 10.0)
        self.sp_phi_ref.setDecimals(4)
        self.sp_phi_ref.setSingleStep(0.05)
        self.sp_phi_ref.setValue(float((self.cfg.get('quantum_yield') or {}).get('phi_ref', 0.75)))
        self.btn_remember_ref = QPushButton("记住参比…")
        self.btn_remember_ref.setToolTip("把显式参比组与 Φ_ref 写入本目录 dye_batch.yaml（显式动作）")
        self.btn_remember_ref.clicked.connect(self._remember_reference)
        self.sp_phi_ref.valueChanged.connect(self._recompute_phi_only)
        self.chk_lazy = QCheckBox("预览先出首图")
        self.chk_lazy.setToolTip(
            "勾选（默认）：预览先只渲首批图件（默认图 a，谱图最直观），\n"
            "让你尽快看到结果；其余图件在后台按顺序补渲，画完一张出现一张。\n"
            "取消勾选：一次画完整批预览图（图件多时要多等几秒）。\n"
            "两种方式的最终图件集合完全一样；「定稿出图」始终整批出 600 dpi。")
        self.chk_live = QCheckBox("实时生图预览")
        self.chk_live.setChecked(True)
        self.chk_live.setToolTip("调整图式/标注/透明等生图参数时，用最终渲染器低 dpi 预览；不写正式产物")
        self.lbl_live = QLabel("预览待命")
        theme.mark(self.lbl_live, "hint")
        self.lbl_dir = QLabel("未选择目录")
        theme.mark(self.lbl_dir, "muted")
        self.lbl_dir.setWordWrap(True)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_reload.clicked.connect(self._reload_dir)
        self.btn_run.clicked.connect(self._run)
        self.btn_final.clicked.connect(self._export_final)
        # V2.3 FR-24.1：键盘主路径（F5 开始分析 / Ctrl+R 重新载入）
        QShortcut(QKeySequence("F5"), self, activated=self._run)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self._reload_dir)
        self.chk_lazy.toggled.connect(self._on_lazy_toggled)
        self.btn_reload.setEnabled(False)
        self.btn_run.setEnabled(False)

    def _build_top_bar(self) -> None:
        """流程条 + 批次树开关。分析页会把整条挂到它自己的顶部细条里。"""
        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        self.btn_tree_toggle = QPushButton("批次")
        self.btn_tree_toggle.setCheckable(True)
        self.btn_tree_toggle.setToolTip("窗口较窄时批次树默认收起：按下显示，再按一次收起")
        self.top_bar = QWidget()
        self.top_bar.setObjectName("phiTopBar")
        lay = QHBoxLayout(self.top_bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.flow_bar, 1)
        lay.addWidget(self.btn_tree_toggle)

    def top_bar_widget(self) -> QWidget:
        """分析页顶部细条插槽的约定：交出流程条（同一对象，切走时会还回来）。"""
        return self.top_bar

    def _build_tree_column(self) -> QWidget:
        """左栏：批次树（组 → 文件，只读导航，选组即过滤画布与文件表）。"""
        self.tree = BatchTree()
        self.tree.groups_selected.connect(self._apply_group_filter)
        self.lbl_tree = QLabel("批次")
        theme.mark(self.lbl_tree, "section")
        col = QWidget()
        col.setObjectName("phiTreeColumn")
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.lbl_tree)
        lay.addWidget(self.tree, 1)
        col.setMinimumWidth(200)
        col.setMaximumWidth(300)
        return col

    def _build_center_column(self) -> QWidget:
        """中栏：结论卡 / 文件清单或图件 / 结果表（含动作条与导出条）。"""
        self.verdict = VerdictCard()
        self.verdict.set_empty("还没有结果", "选择数据目录，再按 F5 开始分析")

        self.files = FileTable()
        self.files.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # V2.9.5R2：窄窗口下列头不被均匀挤压；文件名/剔除原因各占可伸缩列
        from ui.widgets.file_table import (COL_CHECK, COL_FILE, COL_GROUP, COL_DYE,
                                           COL_ROLE, COL_TIME, COL_STATUS, COL_REASON)
        _hdr = self.files.table.horizontalHeader()
        _hdr.setSectionResizeMode(COL_CHECK, QHeaderView.ResizeToContents)
        _hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        for _c in (COL_GROUP, COL_DYE, COL_ROLE, COL_TIME, COL_STATUS):
            _hdr.setSectionResizeMode(_c, QHeaderView.ResizeToContents)
        _hdr.setSectionResizeMode(COL_REASON, QHeaderView.Stretch)
        self.files.selection_changed.connect(self._on_selection)
        # A02（V2.x 审查）：文件清单里改染料/时间原来**没有任何接收者**——分析只读
        # 整定面板，于是"表里改了、分析没变"。接到同一份状态（单向：表 → 面板）。
        self.files.dye_changed.connect(self._on_file_dye_changed)
        self.files.time_changed.connect(self._on_file_time_changed)
        self.files.selection_changed.connect(lambda *_: self._refresh_tree())
        box_files = QGroupBox("文件清单")
        lb = QVBoxLayout(box_files)
        lb.addWidget(self.files)

        self.canvas = CanvasPanel()
        # V2.3 FR-24.6：画布状态（复制成功等）→ 页面状态栏
        self.canvas.status_message.connect(self.status)
        # V2.5 FR-32.5：位图画布显示"当前图信息"（源文件/组/档位，元数据反查）
        self.canvas.figure_changed.connect(self._on_figure_info)
        box_fig = QGroupBox("图件（图 a–e 与 JACS 堆叠图）")
        lf = QVBoxLayout(box_fig)
        lf.addWidget(self.canvas)

        # 文件清单与图件共用中栏主区：导入/分组两步看清单，其余步看图
        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(box_files)
        self.center_stack.addWidget(box_fig)

        self.table = QTableWidget(0, len(RESULT_HEADERS))
        self.table.setHorizontalHeaderLabels(RESULT_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        # FR-23.6b：点结果表某行 → 批次树选中该组
        self.table.cellClicked.connect(self._on_result_row_clicked)
        # V2.6 FR-40.5：结果区动作条（打开输出 / 复制结果 / 下一步 / 重试）
        self.actions = ResultActions(self, next_target="figures",
                                     next_text="下一步：出图导出")
        self.actions.message.connect(self.status)
        # 「下一步」留在本页：流程条切到出图导出（定稿 600 dpi 在那一步）
        self.actions.next_step_requested.connect(lambda _t: self._go_step(len(STEPS) - 1))
        self.actions.retry_requested.connect(self._run)
        self.export = ExportBar(on_workbook=self._export_workbook,
                                on_dashboard=self._export_dashboard)
        # V2.3 FR-24.2/24.6：导出条的常规反馈走状态栏（不弹模态）
        self.export.message.connect(self.status)
        box_res = QGroupBox("结果：k_obs 与 Φ_Δ")
        lr = QVBoxLayout(box_res)
        lr.addWidget(self.table, 1)
        lr.addWidget(self.actions)
        lr.addWidget(self.export)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.center_stack)
        split.addWidget(box_res)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([520, 170])          # 图件优先：结果表够看几行即可

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(split, 1)
        center.setMinimumWidth(380)
        return center

    def _build_settings_column(self) -> None:
        """出图设置（检查器第 5 步）+ 实时生图预览接线。"""
        self.settings_panel = UnifiedSettingsPanel()
        self.settings_panel.setMinimumWidth(260)
        self.settings_panel.setMaximumWidth(280)
        self.settings_panel.changed.connect(lambda: self.status.emit("出图设置已更新（下一次出图/重算生效）"))
        from ui.widgets.live_render_preview import LiveRenderPreview
        self.live_preview = LiveRenderPreview(self, self._render_live_preview)
        self.live_preview.updated.connect(self.canvas.set_images)
        self.live_preview.state.connect(self.lbl_live.setText)
        self.live_preview.failed.connect(lambda m: self.logged.emit("WARNING", f"实时预览失败：{m}"))
        self.chk_live.toggled.connect(self.live_preview.set_enabled)
        self.settings_panel.changed.connect(self._schedule_live_preview)

    def _build_inspector(self) -> QWidget:
        """右栏检查器：只展开当前步能改的东西，其余步折成一行摘要。"""
        self.params = ParamPanel()
        self.params.changed.connect(self._on_params_changed)
        self.settings_panel.ed_formats.setText(self.params.ed_formats.text())
        self.settings_panel.ed_formats.textChanged.connect(
            lambda: self.params.ed_formats.setText(self.settings_panel.ed_formats.text()))
        scroll_p = QScrollArea()
        scroll_p.setWidget(self.params)
        scroll_p.setWidgetResizable(True)
        scroll_p.setFrameShape(QFrame.NoFrame)
        scroll_p.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.dye_panel = DyeTimePanel()
        self.dye_panel.dye_changed.connect(self._on_dye_changed)
        self.dye_panel.remember_changed.connect(self._on_remember)
        self.dye_panel.times_changed.connect(self._on_times_changed)

        # ① 导入
        p_import = QWidget()
        li = QVBoxLayout(p_import)
        li.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(self.btn_dir)
        row.addWidget(self.btn_reload)
        li.addLayout(row)
        li.addWidget(self.lbl_dir)
        hint = QLabel("也可以把数据目录直接拖进窗口。文件名里的组名和时间会自动识别，"
                      "识别不对的在下一步改。")
        theme.mark(hint, "hint")
        hint.setWordWrap(True)
        li.addWidget(hint)
        li.addWidget(self.chk_lazy)
        li.addStretch(1)
        # ② 分组与 QC：染料命名 + 时间工作表
        # ③ 拟合 k_obs：分析参数
        # ④ Φ_Δ：参比与 Φ_ref
        p_phi = QWidget()
        lp = QVBoxLayout(p_phi)
        lp.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.addRow("参比组", self.cmb_reference)
        form.addRow("Φ_ref", self.sp_phi_ref)
        lp.addLayout(form)
        lp.addWidget(self.btn_remember_ref, 0, Qt.AlignLeft)
        note = QLabel("改参比或 Φ_ref 只重算 Φ_Δ，不会重新拟合 k_obs。")
        theme.mark(note, "hint")
        note.setWordWrap(True)
        lp.addWidget(note)
        lp.addStretch(1)
        # ⑤ 出图导出：出图设置 + 实时预览 + 定稿
        p_fig = QWidget()
        lfg = QVBoxLayout(p_fig)
        lfg.setContentsMargins(0, 0, 0, 0)
        lfg.addWidget(self.settings_panel, 1)
        live = QHBoxLayout()
        live.addWidget(self.chk_live)
        live.addWidget(self.lbl_live, 1)
        lfg.addLayout(live)
        lfg.addWidget(self.btn_final)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((p_import, self.dye_panel, scroll_p, p_phi, p_fig)):
            ins.set_page(i, w)
        ins.add_action(self.btn_run)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        self.inspector_stack = ins.stack
        return ins

    def _build_body(self, left: QWidget, center: QWidget, inspector: QWidget) -> QSplitter:
        """三栏 splitter；窄窗口时批次树默认收起（:meth:`_apply_responsive_layout`）。"""
        body = three_columns(left, center, inspector)
        self._tree_col = left
        self._center = center
        self._inspector = inspector
        self._body = body
        self.btn_tree_toggle.toggled.connect(lambda _: self._apply_responsive_layout())
        self._apply_responsive_layout()
        return body

    # ══════════════ 流程条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：检查器换页、中栏换成清单或图件、摘要与前后按钮跟着变。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        if idx and not self.flow_bar.step_enabled(idx):
            idx = 0
        self.flow_bar.set_current(idx)
        self.center_stack.setCurrentIndex(0 if idx < 2 else 1)
        nxt = idx + 1 < len(STEPS) and self.flow_bar.step_enabled(idx + 1)
        self.inspector.set_current(idx, next_enabled=nxt)

    def _step_summary(self, idx: int) -> str:
        """某一步当前设置的一行摘要（只读已有状态）。"""
        if idx == 0:
            if not self.data_dir:
                return "还没选数据目录"
            return f"{self.data_dir.name} · {len(self.scans)} 个谱"
        if idx == 1:
            groups = self._groups()
            n_ex = sum(1 for s in self.scans if s.excluded)
            return f"{len(groups)} 组 · 剔除 {n_ex} 个" if self.scans else "载入后可改组名和时间"
        if idx == 2:
            p = self.params
            return (f"{p.sp_probe.value():.1f} nm · ±{p.sp_win.value():g} nm · "
                    f"激发 {p.sp_exc.value():g} nm")
        if idx == 3:
            ref = self._reference_group or "自动识别"
            return f"参比 {ref} · Φ_ref {self.sp_phi_ref.value():.3f}"
        mode = "已定稿" if self._last_export_mode == "final" else "预览档"
        return f"{mode} · 格式 {self.params.ed_formats.text() or 'png'}"

    def _refresh_summaries(self) -> None:
        self.inspector.refresh_summaries()

    def _update_flow(self) -> None:
        """按当前数据/结果/过期状态给每一步打 完成 / 待更新 / 未做 标记。"""
        loaded = bool(self.scans)
        has_ok = any(not s.excluded for s in self.scans)
        rr = self._last_rr
        ok = bool(rr is not None and getattr(rr, "ok", False))
        phi_ok = ok and any((p or {}).get("status") == "OK"
                            for p in (getattr(rr, "phi", None) or {}).values())
        done = [loaded, has_ok, ok, phi_ok, ok and self._last_export_mode == "final"]
        stale: set = set()
        if self.banner.is_shown():
            stale = {4} if self.banner.kind() == "plot" else {2, 3, 4}
        for i in range(len(STEPS)):
            if i:
                self.flow_bar.set_step_enabled(i, loaded, "先选择数据目录")
            state = STEP_STALE if (i in stale and done[i]) else (STEP_DONE if done[i] else STEP_TODO)
            self.flow_bar.set_state(i, state)
        cur = self.flow_bar.current()
        self.inspector.set_next_enabled(
            cur + 1 < len(STEPS) and self.flow_bar.step_enabled(cur + 1))
        self._refresh_summaries()

    # ══════════════ 结论卡 ══════════════
    def _update_verdict(self, rr) -> None:
        """把本次结果压成一句结论 + 一个大数字（只复述已有字段，不新增判断）。"""
        if rr is None:
            self.verdict.set_empty(
                "还没有结果",
                "按 F5 或「开始分析」拟合 k_obs" if self.scans
                else "选择数据目录，再按 F5 开始分析")
            return
        if not getattr(rr, "ok", False):
            self.verdict.set_failed("这次分析没有得到结果",
                                    str(getattr(rr, "message", "") or "详见运行日志"))
            return
        results = getattr(rr, "results", None) or {}
        phi = getattr(rr, "phi", None) or {}
        fits = {g: (e or {}).get("pooled") for g, e in results.items()}
        good = {g: f for g, f in fits.items()
                if f is not None and getattr(f, "status", "") != "FAILED"}
        roles = {g: (e or {}).get("role") for g, e in results.items()}
        ref = next((g for g, r in roles.items() if r == "reference"), "")
        groups = []
        for g in sorted(results):
            f = fits.get(g)
            failed = g not in good
            groups.append({
                "name": g,
                "sub": "拟合失败" if failed else f"{f.k:.3g} s⁻¹",
                "pill": "失败" if failed else ROLE_ZH.get(roles.get(g), roles.get(g) or "样品"),
                "kind": "warn" if failed or g == ref else "ok",
                "color": self.tree.group_color(g),
            })
        lines = []
        if good:
            lines.append(f"{len(good)}/{len(results)} 组拟合成功，"
                         f"R² ≥ {min(f.r2 for f in good.values()):.3f}")
        cut = [g for g, f in good.items() if getattr(f, "truncated", False)]
        if cut:
            lines.append("自动截断：" + "、".join(f"{g}（n={good[g].n}）" for g in cut))
        hits = sorted((g for g, p in phi.items()
                       if (p or {}).get("status") == "OK" and g != ref and g in good))
        if hits:
            g = hits[0]
            p = phi[g]
            phi_ref = float(self.sp_phi_ref.value())
            headline = f"Φ_Δ = {p['phi']:.2f}"
            if ref in good and phi_ref > 0:
                diff = (p["phi"] / phi_ref - 1.0) * 100.0
                word = "高" if diff >= 0 else "低"
                headline = f"比参比 {ref} {word} {abs(diff):.0f}%"
                # 只摆事实：k 比值与 Φ_ref；Φ_Δ 本身还含吸光校正，不在这里写成等式
                lines.insert(0, f"k_样品 / k_参比 = {good[g].k / good[ref].k:.3f} · "
                                f"Φ_ref = {phi_ref:.3f}")
            if len(hits) > 1:
                lines.append(f"共 {len(hits)} 个样品组有 Φ_Δ，逐组数值见下方结果表")
            self.verdict.set_verdict(label=f"Φ_Δ · {g}",
                                     value=f"{p['phi']:.2f}",
                                     error=f"± {p.get('phi_err', 0.0):.2f}",
                                     headline=headline, lines=lines[:3], groups=groups)
            return
        if good:
            g = sorted(good)[0]
            no_ref = any((p or {}).get("status") == "NO_REF" for p in phi.values())
            self.verdict.set_verdict(
                label=f"k_obs · {g}", value=f"{good[g].k:.3g}",
                error="s⁻¹",
                headline=("k_obs 已拟合，还缺参比组" if no_ref else "k_obs 已拟合，Φ_Δ 没算出来"),
                lines=(["在「Φ_Δ」这一步指定参比组和 Φ_ref 即可算出 Φ_Δ"] + lines)[:3],
                groups=groups)
            return
        self.verdict.set_failed("所有组都拟合失败", self._failure_hint(rr))

    # ══════════════ 懒渲染开关（P1） ══════════════
    def _lazy_enabled(self) -> bool:
        """配置里的 ``gui.lazy_preview``（默认 True）。"""
        return bool((self.cfg.get("gui") or {}).get("lazy_preview", True))

    def _first_batch(self) -> list[str]:
        """预览档首批步骤：``plot.jacs.preview_first``（默认 ``["a"]``）。

        键位真相在 ``core.plotstyle.JACS_SCHEMA``（"高级"组），所以这里只做解析，
        不在 UI 里另立一份默认值——否则会出现"配置改了界面不认"的老毛病。
        """
        try:
            from core.plotting_jacs import _preview_first
            return _preview_first(self.cfg)
        except Exception:                                  # noqa: BLE001
            return ["a"]

    def _on_lazy_toggled(self, on: bool) -> None:
        """勾/取消「预览先出首图」→ 写回配置并说明差别。"""
        self.cfg.setdefault("gui", {})["lazy_preview"] = bool(on)
        self.persist_cfg()
        self.logged.emit("INFO",
                         "预览懒渲染：开（先出首批图，其余后台补渲）" if on
                         else "预览懒渲染：关（预览档一次画完整批）")

    # ══════════════ 目录与载入 ══════════════
    def _choose_dir(self) -> None:
        d = self.pick_dir()
        if d:
            self._load_dir(d)

    def on_dir_dropped(self, d: Path) -> None:
        self._load_dir(d)

    def _load_dir(self, d: Optional[Path]) -> None:
        """后台扫描目录并填充文件清单（防界面冻结）。"""
        if not d:
            return
        self.data_dir = Path(d)
        self.lbl_dir.setText(f"数据目录：{self.data_dir}")
        self.remember_dir(self.data_dir)
        cfg = self.params.config(self.cfg)

        def task(progress_cb, log_cb):
            return collect_scans(self.data_dir, cfg, progress_cb=progress_cb)

        self.run_task(task, "载入文件清单", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.btn_reload, self.btn_run])

    def set_pending_overrides(self, overrides: Optional[dict]) -> None:
        """FR-24.9：预置①数据页的整定（染料名 / 时间），下一次载入时合并。

        单向同步：只把数据页的状态带过来，**不在两页之间复制控件**（单一状态源）。
        """
        self._pending_overrides = dict(overrides or {})

    def _apply_pending_overrides(self, scans: list) -> list:
        """把预置整定合并进本次 scans：时间写回 Scan，染料名留待面板合并。"""
        ov = dict(self._pending_overrides or {})
        self._pending_overrides = {}
        if not ov:
            return scans
        times = ov.get("times") or {}
        for s in scans:
            t = times.get(s.path.name)
            if t is None:
                continue
            if s.t_src is None:
                s.t_src = float(s.t)
            s.t = float(t)
        dye_of = dict(ov.get("dye_of") or {})
        if dye_of:
            self._pending_dye = dye_of
        return scans

    def _on_loaded(self, scans: list) -> None:
        scans = self._apply_pending_overrides(scans)   # FR-24.9：①数据页整定同步
        self.scans = scans
        dye_of = {**self.dye_panel.dye_map(), **getattr(self, "_pending_dye", {})}
        self._pending_dye = {}
        self.files.set_scans(scans, dye_of=dye_of)
        # G18：把**本次目录**一并交给面板——换目录时会清掉上一批的染料命名会话覆盖
        # （原先只 update 不清理，跨目录同名组会被旧名字静默盖住）。
        self.dye_panel.set_scans(scans, dye_of=dye_of,
                                 data_dir=self.data_dir)
        self.btn_reload.setEnabled(True)
        has_ok = any(not s.excluded for s in scans)
        self.btn_run.setEnabled(has_ok)
        # P8：QC 体检只需内存里的 scans，不等整条流水线跑完就该能点
        self.export.set_data_ready(has_ok)
        if not scans:
            from core.io import scan_extensions
            _ex = "/".join(scan_extensions(self.cfg.get("io") or {}))
            self.logged.emit("WARNING", f"该目录下没有找到光谱文件（扩展名 {_ex}）")
        elif not has_ok:
            self.logged.emit("ERROR", "所有文件都解析失败，请检查文件格式与编码")
        else:
            self.logged.emit("INFO", f"已载入 {len(scans)} 个文件"
                                     f"（可用 {sum(1 for s in scans if not s.excluded)} 个）")
        # FR-12.3：解析完就报一次时间告警（重复/非单调/单点），不阻塞
        try:
            from core.io import validate_times
            for iss in validate_times(scans):
                self.logged.emit("WARNING", f"[时间检查] {iss.message}"
                                            + (f"　建议：{iss.suggestion}"
                                               if iss.suggestion else ""))
        except Exception as err:                           # noqa: BLE001
            # G20：原来是 `pass`——时间检查自己坏掉时用户什么都看不到，
            # 而"时间不对"恰恰是最需要被提醒的一类问题。留痕但不阻断载入。
            self.logged.emit("WARNING", f"[时间检查] 未能完成（不影响载入）：{err}")
        # V2.9.1 FR-61：批次文件里的显式参比可作为初值（UI 选择仍优先）
        try:
            from core.inputs.batch_file import load_batch
            _batch = load_batch(self.data_dir) if self.data_dir else None
        except Exception:                           # noqa: BLE001
            _batch = None
        if (_batch is not None and getattr(_batch, "reference_group", "")
                and not self._reference_group):
            self._reference_group = str(_batch.reference_group)
        _phi = ((_batch.phi_ref if _batch is not None and _batch.phi_ref is not None
                 else (self.cfg.get("quantum_yield") or {}).get("phi_ref", 0.75)))
        self.sp_phi_ref.setValue(float(_phi))
        # V2.9.1 FR-61：参比下拉 = 自动 + 当前组；已有显式选择若仍存在则保留
        groups = sorted({s.group for s in scans if not s.excluded})
        self.cmb_reference.blockSignals(True)
        self.cmb_reference.clear()
        self.cmb_reference.addItem("自动（ref- 前缀/角色）", "")
        for g in groups:
            self.cmb_reference.addItem(g, g)
        if self._reference_group in groups:
            self.cmb_reference.setCurrentIndex(groups.index(self._reference_group) + 1)
        else:
            self._reference_group = ""
        self.cmb_reference.blockSignals(False)
        self._refresh_tree()
        self._update_flow()
        if self._last_rr is None:
            self._update_verdict(None)
        if self.flow_bar.current() == 0 and has_ok:
            self._go_step(1)

    def _reload_dir(self) -> None:
        """重新载入：强制清空解析缓存（FR-6.8 的失效入口）。"""
        try:
            from core.io import clear_scan_cache
            clear_scan_cache()
        except Exception:                                  # noqa: BLE001
            pass
        self._load_dir(self.data_dir)

    # ══════════════ 预分析整定（FR-11.5 / FR-12） ══════════════
    def _on_dye_changed(self, group: str, dye: str) -> None:
        """整定面板改了染料名 → 同步文件清单的染料列。

        两种信号：

        * ``(组名, 新名字)``：用户手改 → 通知文件清单刷新该组显示；
        * ``("", "")``：:meth:`DyeTimePanel._restore_auto` 的**哨兵**，表示"已恢复
          自动命名"。以前这个信号被直接丢掉，文件清单于是仍显示旧的手工名
          （G18 的"恢复自动命名后两处不一致"）。用同一条路径把它改回自动态即可
          —— ``FileTable._dye_text`` 对空值本来就回落到 ``自动: X``。

        G23：这里原来还写 ``self._dye_session[group] = dye``——那个属性**零读者**
        （真正生效的是 ``cfg["_dye_session"]``，由 ``run_phi_pipeline`` 的
        ``group_dye`` 参数填），已删。
        """
        if group and dye:
            self.files.set_dye(group, dye)
            self._refresh_tree()
            self.logged.emit("INFO", f"染料命名：{group} → {dye}")
            return
        # 恢复自动命名：``set_dye("", "")`` 只留一个空键，显示侧据此回落自动值
        self.files.set_dye("", "")
        self._refresh_tree()

    def _on_remember(self, group: str, remember: bool) -> None:
        """勾/取消「记住」→ 写/删 dyes.yaml（持久化，FR-11.1）。"""
        if not group:
            return
        dye = self.dye_panel.dye_map().get(group, "")
        try:
            from core.dyes import forget_mapping, remember_mapping
            if remember and dye:
                remember_mapping(group, dye)
                self.logged.emit("INFO", f"已记住映射：{group} → {dye}（dyes.yaml）")
            elif not remember:
                forget_mapping(group)
                self.logged.emit("INFO", f"已取消记住：{group}（回到自动/会话命名）")
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("WARNING", f"写入染料注册表失败：{err}")

    def _on_file_dye_changed(self, group: str, dye: str) -> None:
        """文件清单双击染料列 → 同步整定面板并刷新页签标题（A02）。"""
        hook = getattr(self.dye_panel, "set_group_dye", None)
        if callable(hook):
            hook(group, dye)
        self._refresh_tree()
        self.logged.emit("INFO", f"染料命名（来自文件清单）：{group} → {dye}")

    def _on_file_time_changed(self, name: str, t: float) -> None:
        """文件清单双击时间列 → 记进本次 run 的时间整定并刷新面板（A02）。"""
        self._time_overrides[str(name)] = float(t)
        hook = getattr(self.dye_panel, "refresh_time_table", None)
        if callable(hook):
            hook()
        self.logged.emit("INFO", f"时间整定（来自文件清单）：{name} → {t:g} s")

    def _on_times_changed(self, overrides: dict) -> None:
        """时间工作表改动 → 记录并在日志里提示影响面（FR-12.4）。"""
        self._time_overrides = dict(overrides or {})
        if self._time_overrides:
            self.logged.emit("INFO",
                             f"时间整定：{len(self._time_overrides)} 个时间点已被手动修改"
                             "（拟合与图 b 横轴将使用新值）")
        else:
            self.logged.emit("INFO", "时间整定已清空，回到文件名解析的时间")

    def _on_selection(self, n: int) -> None:
        self.btn_run.setEnabled(n > 0 and not self._busy)

    def _on_params_changed(self) -> None:
        # 参数变化即持久化（FR-5）；不自动重跑（"显式动作"哲学）
        self.cfg = self.params.config(self.cfg)
        self.persist_cfg()
        self._refresh_banner()             # A25.1：让"结果已过期"可见

    # ══════════════ 结果过期横幅（A25.1） ══════════════
    def _refresh_banner(self) -> None:
        self.banner.refresh(self.cfg, has_result=self._last_rr is not None)
        self._update_flow()

    def on_cfg_changed(self) -> None:
        """外部（模板套用/批次载入）换了 cfg 后刷新"结果过期"横幅（V2.4 FR-26.3③）。

        只更新提示，**不自动重跑**：用户按 F5 才真正重算。
        """
        try:
            self._refresh_banner()
        except Exception as err:                       # noqa: BLE001 —— 横幅不得废页
            log.warning("刷新过期横幅失败：%s", err)

    def _on_banner_action(self, kind: str) -> None:
        """横幅按钮：仅出图参数变 → 只重渲受影响图；数值变 → 走完整重算。"""
        if kind == "plot":
            self._rerender_affected()
        else:
            self._run()

    def _rerender_affected(self) -> None:
        """A25.1：只重渲受影响的图（不重算数值、不重写 csv/json/report）。"""
        if self._busy:
            # A08（V2.x 审查）：横幅按钮不查忙 → 可能与主任务/后台补渲同时写同一
            # run_dir。主任务在跑就直接拒绝。
            self.status.emit("已有任务在运行，请等它结束再重渲")
            return
        rr = self._last_rr
        if rr is None or not getattr(rr, "run_dir", None) or not rr.ok:
            self.status.emit("请先完成一次分析")
            return
        self.backfill.cancel()          # 先停后台补渲（同一 run_dir，不能并发写）
        cfg = self.params.config(self.cfg)
        secs = self.banner.sections(self.cfg) or ["plot"]
        keys = self.banner.pending_keys or (self.banner.changed_keys(self.cfg)
                                            if "plot" in secs else [])
        try:
            from core.render.plotting_jacs import affected_figures
            steps = affected_figures(cfg, secs, changed_keys=keys)
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("WARNING", f"无法判定受影响图，改按全部图处理：{err}")
            steps = None
        run_dir = Path(rr.run_dir)
        dye_map = self.dye_panel.dye_map()
        # A09：按**最近一次产物档位**重渲（已定稿就按 final 出 600 dpi，
        # 否则"重渲"会把用户刚定稿的画布降回预览分辨率）。
        mode = self._last_export_mode or "preview"
        self.logged.emit("INFO", f"只重渲受影响图（步骤 {steps or '全部'}，"
                                 f"档位 {mode}），数值结果保持不变…")

        def task(progress_cb, log_cb):
            if progress_cb:
                progress_cb(20, "重渲受影响图")
            made = rerender_figures(rr.scans, rr.results, rr.phi, rr.controls,
                                    cfg, run_dir, export_mode=mode,
                                    only=steps, group_dye=dye_map)
            return made or []

        def done(made) -> None:
            figs = self._preview_pngs(rr)
            self.canvas.set_images(figs, keep_index=True)
            self._apply_group_filter(self.tree.selected_groups())
            self.banner.set_snapshot(cfg)
            self._refresh_banner()
            self.logged.emit("INFO",
                             f"已重渲 {len(made)} 张受影响图件（数值未重算）")

        self.run_task(task, "重渲受影响图", done,
                      busy_widgets=[self.btn_run, self.banner.btn_action])

    # ── A25.3：只分析本组（不改全局勾选） ──
    def analyze_group(self, group: str) -> int:
        """对某组**不改变全局勾选**地跑一次分析；返回参与文件数。"""
        paths = [s.path for s in (self.scans or [])
                 if s.group == str(group) and not s.excluded]
        if not paths:
            self.status.emit(f"组「{group}」没有可用文件")
            return 0
        self.logged.emit("INFO",
                         f"只分析本组「{group}」：{len(paths)} 个文件"
                         "（全局勾选状态保持不变）")
        self._run(only_paths=paths)
        return len(paths)

    # ── A25.2：结果表 / 组页签右键菜单 ──
    def _result_row_cells(self, row: int) -> list:
        out: list = []
        for c in range(self.table.columnCount()):
            it = self.table.item(row, c)
            out.append(it.text() if it is not None else "")
        return out

    def _on_result_context_menu(self, pos) -> None:        # noqa: ANN001
        from PySide6.QtWidgets import QMenu
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        act_copy = menu.addAction("复制本行")
        act_copy.triggered.connect(lambda *_: self._copy_result_row(row))
        act_jump = menu.addAction("在批次树里选中该组")
        act_jump.setEnabled(len(self.tree.groups()) > 1)
        act_jump.triggered.connect(lambda *_: self._on_result_row_clicked(row))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_result_row(self, row: int) -> None:
        cells = self._result_row_cells(row)
        try:
            QApplication.clipboard().setText("\t".join(cells))
            self.status.emit(f"已复制结果行：{cells[0] if cells else ''}")
        except Exception as err:                           # noqa: BLE001
            self.status.emit(f"复制失败：{err}")

    def _on_tree_context_menu(self, pos) -> None:          # noqa: ANN001
        from PySide6.QtWidgets import QMenu
        it = self.tree.itemAt(pos)
        if it is None:
            return
        self.tree.setCurrentItem(it)
        gs = self.tree.selected_groups()           # 「全部组（对比）」没有"本组"动作
        if not gs:
            return
        group = next(iter(gs))
        menu = QMenu(self)
        act_only = menu.addAction("只分析本组（不改勾选）")
        act_only.triggered.connect(lambda *_: self.analyze_group(group))
        act_check = menu.addAction("只勾选本组")
        act_check.triggered.connect(lambda *_: self.files.check_only_group(group))
        menu.addSeparator()
        act_open = menu.addAction("打开数据目录")
        act_open.setEnabled(bool(self.data_dir))
        act_open.triggered.connect(lambda *_: open_folder(self.data_dir))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ══════════════ 批次树：选组即过滤（视图过滤，不复制控件） ══════════════
    def _refresh_tree(self) -> None:
        """按当前 scans / 勾选 / 参比 / 结果重建批次树（保持选中组）。"""
        checked = {str(p) for p in self.files.checked_paths()} if self.scans else set()
        excluded = [str(s.path) for s in self.scans
                    if not s.excluded and str(s.path) not in checked]
        self.tree.set_scans(self.scans, self._group_title,
                            reference=self._reference_group, excluded=excluded)
        self.lbl_tree.setText(f"批次 {self.data_dir.name}" if self.data_dir else "批次")
        if self._last_rr is not None:
            self.tree.set_result(self._last_rr)

    def _apply_group_filter(self, groups) -> None:
        """批次树选中变化：只改文件表**行可见性**与画布**可见集**，不重建任何控件。"""
        self.files.set_group_filter(groups)
        if groups is None:
            self.canvas.set_visible_filter(None)
            return
        names = figure_names(groups, self.scans)
        # 画布字典键是**文件名**（含扩展名），这里按 stem 比较（png/svg 都适用）
        self.canvas.set_visible_filter(lambda n: Path(n).stem in names)

    def _groups(self) -> list[str]:
        """当前 scans 的组名（排序）。"""
        return sorted({s.group for s in (getattr(self, "scans", None) or [])})

    def _group_title(self, group: str) -> str:
        """组标题：染料名 · 组名（两者相同时只显示一个）。"""
        try:
            from core.dyes import auto_dye_name
            dye = str(self.files.dye_map().get(group) or auto_dye_name(group))
        except Exception:                                  # noqa: BLE001
            dye = group
        return group if dye == group else f"{dye} · {group}"

    def _on_result_row_clicked(self, row: int, _col: int = 0) -> None:
        """FR-23.6b：点结果表某行 → 批次树选中该组（画布与清单随之过滤）。"""
        rr = getattr(self, "_last_rr", None)
        if rr is None or row < 0:
            return
        rows = sorted((getattr(rr, "results", None) or {}).items())
        if row < len(rows):
            self.tree.select_group(rows[row][0])

    # ══════════════ 分析 ══════════════
    def _remember_reference(self) -> bool:
        """V2.9.1 O1：显式保存参比组/Φ_ref 到 dye_batch.yaml。"""
        if not self.data_dir:
            self.status.emit("请先载入数据目录")
            return False
        ref = str(self.cmb_reference.currentData() or "")
        from core.inputs.batch_file import load_batch, save_batch
        try:
            batch = load_batch(self.data_dir)
            batch.reference_group = ref
            batch.phi_ref = float(self.sp_phi_ref.value())
            ok, why = save_batch(batch, self.data_dir)
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("WARNING", f"记住参比失败：{err}")
            return False
        if ok:
            self.logged.emit("INFO", f"[批次] 已记住参比={ref or '自动'} / Φ_ref={self.sp_phi_ref.value():g}")
            self.status.emit(why)
        else:
            self.logged.emit("WARNING", why)
        return bool(ok)

    def _on_reference_changed(self, _idx: int) -> None:
        self._reference_group = str(self.cmb_reference.currentData() or "")
        if self._reference_group:
            self.logged.emit("INFO", f"显式参比组：{self._reference_group}（UI 指定优先于前缀）")
        else:
            self.logged.emit("INFO", "参比组：自动（ref- 前缀/角色）")
        self._recompute_phi_only()
        if self.scans:
            self._refresh_tree()
        self._refresh_summaries()

    def _recompute_phi_only(self, *_args) -> None:
        """V2.9.4 FR-69：k_obs 已拟合时，改参比/Φ_ref 只重算 Φ_Δ，不重新拟合 k。"""
        rr = getattr(self, "_last_rr", None)
        if rr is None or not getattr(rr, "results", None):
            return
        import copy
        from core.analysis.pipeline import _apply_reference_group
        from core.analysis.phi import compute_phi
        results = {g: copy.copy(e) for g, e in rr.results.items()}
        _apply_reference_group(results, self._reference_group)
        cfg = self.params.config(self.cfg)
        cfg["quantum_yield"]["phi_ref"] = float(self.sp_phi_ref.value())
        rr.phi = compute_phi(results, cfg)
        rr.results = results
        self._fill_table(rr)
        self.tree.set_result(rr)
        self._update_verdict(rr)
        self._update_flow()
        self.status.emit("参比/Φ_ref 已更新：仅重算 Φ_Δ（k_obs 拟合结果复用）")

    def _run(self, only_paths=None) -> None:
        """跑分析。``only_paths`` 非空时**只用这些文件**（A25.3「只分析本组」），
        否则用文件表的勾选集合——全局勾选状态不被"只分析本组"改动。"""
        if self._busy:
            # A11（V2.x 审查）：运行中按 F5 会先清空结果表 + 取消补渲，然后把
            # "已有任务"提示弹在后面 —— 用户屏幕上刚看到的结果被清掉。
            # 忙检查必须**放在任何破坏性动作之前**。
            self.status.emit("已有任务在运行，请等待完成或取消后再开始新的分析")
            return
        if not self.data_dir:
            return
        only = list(only_paths) if only_paths else self.files.checked_paths()
        if not only:
            self.logged.emit("WARNING", "没有勾选任何可用文件")
            return
        self.actions.reset()                       # FR-40.5：新一轮 → 动作条复位
        cfg = self.params.config(self.cfg)
        try:
            self.settings_panel._apply()
        except Exception as err:                           # noqa: BLE001
            log.warning("统一页设置应用失败：%s", err)
        out_root = self.out_root()
        self._last_export_mode = "preview"       # A09：本次产物档位
        self.table.setRowCount(0)
        self.backfill.cancel()                     # 新分析 → 旧的补渲作废
        d = self.data_dir
        dye = self.dye_panel.dye_map()
        tov = dict(self._time_overrides)
        # P1：预览档只先画首批（默认图 a），其余交给后台补渲
        lazy = self._lazy_enabled()
        first = self._first_batch() if lazy else None
        self.logged.emit("INFO",
                         f"预览懒渲染：先出 {'|'.join(first)}，其余后台补渲" if lazy
                         else "预览懒渲染已关：预览档一次画完整批")
        self.backfill.update_progress()

        def task(progress_cb, log_cb):
            # V2.5 A29.3：与批量队列**共用**单目录编排 helper（含 dye_batch.yaml
            # 载入与 analysis_preset 套用），防两套逻辑分叉。
            # FR-6.1：GUI 默认走预览档（低 dpi、不写图注），秒级反馈
            return run_directory(d, cfg, progress_cb=progress_cb, log_cb=log_cb,
                                 only_paths=only, out_root=out_root, write=True,
                                 export_mode="preview", time_overrides=tov,
                                 group_dye=dye, only=first,
                                 reference_group=self._reference_group,
                                 phi_ref=float(self.sp_phi_ref.value()))

        self.run_task(task, "Φ_Δ 分析", self._on_analyzed,
                      busy_widgets=[self.btn_run, self.btn_dir, self.btn_reload,
                                    self.files, self.params, self.dye_panel])

    def _export_final(self) -> None:
        """定稿出图：复用已加载数据重渲 600 dpi png（FR-6.1）。"""
        rr = self._last_rr
        if rr is None or not rr.scans or not rr.run_dir:
            self.status.emit("请先完成一次分析")
            return
        cfg = self.params.config(self.cfg)
        run_dir = Path(rr.run_dir)
        self.backfill.cancel()                     # 定稿整批出图 → 补渲不必再跑
        self.logged.emit("INFO", "开始定稿出图（600 dpi png，不重新解析文件）…")

        # G13（第二轮审查）：**线程亲和**——`dye_panel.dye_map()` 读的是
        # QTableWidget，只能在 UI 线程调；原先写在 `task` 体内，等于让工作线程
        # 访问 widget（Qt 明确禁止，现在"能用"是运气）。正确范式就在本文件
        # `_on_preview_done`：进 task 之前先把值取出来。
        dye_map = self.dye_panel.dye_map()

        def task(progress_cb, log_cb):
            if progress_cb:
                progress_cb(20, "定稿出图")
            made = rerender_figures(rr.scans, rr.results, rr.phi, rr.controls,
                                    cfg, run_dir, export_mode="final",
                                    group_dye=dye_map)
            if progress_cb:
                progress_cb(100, "定稿出图完成")
            return made or []

        self.run_task(task, "定稿出图", self._on_final_done,
                      busy_widgets=[self.btn_run, self.btn_final, self.params,
                                    self.dye_panel])

    def _on_figure_info(self, name: str) -> None:
        """状态栏显示当前图信息（V2.5 FR-32.5：元数据反查，不做像素读数）。"""
        try:
            rr = self._last_rr
            run_dir = str(getattr(rr, "run_dir", "") or "")
            mode = getattr(self, "_last_export_mode", "preview")
            label = "预览档" if mode == "preview" else "定稿"
            figs = [Path(p) for p in (getattr(rr, "figures", None) or [])] if rr else []
            hit = next((p for p in figs if p.name == name), None)
            src = hit.stem if hit is not None else str(name or "—")
            self.status.emit(f"当前图：{src} ｜ {label} ｜ {run_dir or '（未运行）'}")
        except Exception:                                  # noqa: BLE001 —— 状态栏不许崩
            pass

    def _on_analyzed(self, rr) -> None:
        self._last_rr = rr
        # V2.6 FR-40.5：结果区动作条（打开输出 / 复制结果 / 下一步 / 重试）
        try:
            self._update_result_actions(rr)
        except Exception as err:                       # noqa: BLE001 —— 动作条不得废页
            log.warning("结果区动作条刷新失败：%s", err)
        if getattr(rr, "ok", False):
            # A25.1：分析成功 → 记下本次用的配置；之后参数再变才算"过期"
            self.banner.set_snapshot(self.params.config(self.cfg))
        self._fill_table(rr)
        self.tree.set_result(rr)
        self._update_verdict(rr)
        self._update_flow()
        if rr.ok and self.flow_bar.current() < 3:
            self._go_step(3)
        if rr.run_dir:
            self.export.set_run_dir(rr.run_dir)
            self.backfill.reset()          # M5：新的一轮分析 → 补渲步骤重新计
            # 预览档产物在 figures_preview/；定稿后才在 figures/
            figs = rr.figures or self._preview_pngs(rr)
            self.canvas.set_images(figs, keep_index=True)
            # A 道 FR-23.5：整批换图后重放当前组过滤（懒渲染排队占位不受影响）
            self._apply_group_filter(self.tree.selected_groups())
        if not rr.ok:
            self.btn_final.setEnabled(False)
            self.logged.emit("ERROR", rr.message or "分析未产生结果")
            return
        n_ok = sum(1 for g, e in rr.results.items()
                   if e.get("pooled") and e["pooled"].status != "FAILED")
        self.logged.emit("INFO", f"分析完成：{len(rr.results)} 个组，其中 {n_ok} 个拟合成功；"
                                 f"输出目录 {rr.run_dir}")
        self._emit_interpretation(rr)
        if self.dye_panel.tbl_time.rowCount():
            self.dye_panel.refresh()
        self.backfill.start()                       # P1：后台补渲其余图件
        self.backfill.update_progress()
        if hasattr(self, "live_preview"):
            self._schedule_live_preview()

    # ══════════════ V2.6 FR-40.5：结果区动作（打开 / 复制 / 下一步 / 重试） ══════════════
    @staticmethod
    def _fmt_num(v, spec: str = ".4g") -> str:
        try:
            return format(float(v), spec)
        except (TypeError, ValueError):
            return "—"

    def _build_result_text(self, rr) -> str:
        """把本次结果拼成可复制的纯文本（不新增判断，只复述已有字段）。"""
        lines = ["Φ_Δ 分析结果"]
        run_dir = str(getattr(rr, "run_dir", "") or "")
        if run_dir:
            lines.append(f"输出目录：{run_dir}")
        results = getattr(rr, "results", None) or {}
        if isinstance(results, dict) and results:
            lines.append("组别\t状态\tk (s⁻¹)\tR²\tΦ_Δ")
            phi = getattr(rr, "phi", None) or {}
            for group, entry in sorted(results.items()):
                # pooled 是 kinetics.FitResult（属性，不是 dict）；Φ_Δ 在 rr.phi[组]
                pooled = (entry or {}).get("pooled") if isinstance(entry, dict) else None
                p = phi.get(group) or {}
                lines.append("\t".join([
                    str(group), str(getattr(pooled, "status", None) or "—"),
                    self._fmt_num(getattr(pooled, "k", None)),
                    self._fmt_num(getattr(pooled, "r2", None)),
                    self._fmt_num(p.get("phi")) if p.get("status") == "OK" else "—"]))
        if len(lines) <= (2 if run_dir else 1):        # 退化：直接抄结果表
            for r in range(self.table.rowCount()):
                cells = []
                for c in range(self.table.columnCount()):
                    it = self.table.item(r, c)
                    cells.append(it.text() if it is not None else "")
                lines.append("  ".join(cells))
        return "\n".join(lines)

    def _failure_hint(self, rr) -> str:
        msg = str(getattr(rr, "message", "") or "分析未产生结果")
        return ("如何修复：" + msg
                + "；检查是否缺少参比（ref）组、探针波长处是否有有效数据，"
                  "必要时在「预分析整定」里修正时间/染料名，然后点「重试」重新分析。")

    def _update_result_actions(self, rr) -> None:
        if not getattr(rr, "ok", False):
            self.actions.show_result(False, failure_hint=self._failure_hint(rr))
            return
        self._last_result_text = self._build_result_text(rr)
        self.actions.show_result(True, output=str(getattr(rr, "run_dir", "") or "") or None,
                                 text=self._last_result_text)

    # ══════════════ P5 / P8：导出条上的两个出口 ══════════════
    # M3（评审记录）：这两件事**都必须放到后台线程**。
    # 原先直接在 UI 线程里跑：大 run 的 openpyxl 写盘（秒级）与 300 dpi 整页渲染
    # 会把界面冻住，而且 `_export_dashboard` 末尾的 `except Exception: pass`
    # **没有任何日志**——画布刷新失败用户无从知晓。
    # 本页本来就有 `run_task` 范式（见 `_run`），照搬即可。
    #
    # G16（第二轮审查）：两个导出都必须先过 `backfill.busy()`。`run_task` 自己只看
    # `self._busy`，看**不到**后台补渲——于是导出可与补渲并行，
    # 而两边都动 matplotlib 全局态（qc_dashboard 在线程里 `matplotlib.use("Agg")`
    # + pyplot），存在 rcParams/后端竞态。`backfill.busy()` 两者都看。
    def _export_workbook(self) -> None:
        """P5：把本次 run 汇总导出成多 sheet 工作簿（写到结果目录）——后台执行。

        ``run_task`` 自带"已有任务在跑就拦住"的闸（``self._busy``），所以这里
        **不需要**自己再存一个 Job 引用：它返回 ``None``，存了反而是我第一版写错
        的地方（守卫永远为假）。两个按钮只在忙时被禁用，任务结束由 ``_unlock_ui``
        统一恢复。
        """
        rr = self._last_rr
        if rr is None:
            self.logged.emit("WARNING", "还没有分析结果，先点「开始分析」")
            return
        if self.backfill.busy():
            self.logged.emit("WARNING", "还有后台任务在跑（分析或补渲），稍候再导出")
            return

        def task(progress_cb, log_cb):
            from core.excel_export import export_workbook
            return export_workbook(rr, self.cfg)

        def done(p) -> None:
            if not p:
                self.logged.emit("WARNING", "Excel 导出未产生文件（详见日志）")
                return
            self.logged.emit("INFO", f"已导出 Excel 汇总：{p}")
            open_folder(Path(p).parent)

        self.run_task(task, "导出 Excel 汇总", done,
                      busy_widgets=[self.export.btn_workbook,
                                    self.export.btn_dashboard])

    def _export_dashboard(self) -> None:
        """P8：出一页 QC 体检图——后台执行（300 dpi 整页渲染很贵）。"""
        scans = [s for s in (self.scans or []) if not s.excluded]
        if not scans:
            self.logged.emit("WARNING", "没有可用数据，无法生成 QC 体检图")
            return
        if self.backfill.busy():                       # G16：见 _export_workbook 的说明
            self.logged.emit("WARNING", "还有后台任务在跑（分析或补渲），稍候再导出")
            return
        # 给目录 → 引擎自己按 cfg["qc_dashboard"]["export"] 决定文件名与格式
        #
        # G17（第二轮审查）：原来写 `Path(self.export.run_dir() or
        # self.cfg["gui"].get("out_root","runs"))` —— 相对 out_root **不经 `out_root()`**，
        # 于是 run_dir 为 None 时目录会落在**进程 CWD**（取决于从哪儿启动 exe），
        # 与其它产物各走各的。这里复用现成的 `out_root()`。
        out_dir = Path(self.export.run_dir() or self.out_root())
        cfg = self.cfg

        def task(progress_cb, log_cb):
            out_dir.mkdir(parents=True, exist_ok=True)
            from core.qc_dashboard import render_dashboard
            return render_dashboard(scans, cfg, out_dir)

        def done(p) -> None:
            if not p:
                self.logged.emit("WARNING", "QC 体检图未生成（详见日志）")
                return
            self.logged.emit("INFO", f"QC 体检图已写出：{p}")
            # 顺手显示在画布上，用户不用再翻目录（用公开接口，不碰内部字段）
            figs = sorted(Path(p).parent.glob(Path(p).stem + ".*"))
            if figs:
                self.canvas.show_image(figs[0])
            else:
                # M3：原来这里静默 pass —— 用户看不到"图出了但没显示出来"
                self.logged.emit("WARNING",
                                 f"体检图已生成但没能在画布上显示（未匹配到 {p.name}）")

        self.run_task(task, "生成 QC 体检图", done,
                      busy_widgets=[self.export.btn_workbook,
                                    self.export.btn_dashboard])

    def _preview_pngs(self, rr) -> list:
        """预览档已写出的 png（``figures_preview/`` 优先，退回 ``figures/``）。"""
        prev = rr.figures_dir("preview")
        figs = sorted(prev.glob("*.png")) if prev and prev.is_dir() else []
        if figs:
            return figs
        fin = rr.figures_dir("final")
        return sorted(fin.glob("*.png")) if fin and fin.is_dir() else []

    # ══════════════ 后台补渲（P1 懒渲染） ══════════════


        # 后续动作（续补下一批 / 清占位）交给 _figs_settle，它由 QThread.finished
        # 触发——那里才是"本轮信号全部投递完毕"的可靠时点

    def _on_final_done(self, made: list) -> None:
        """定稿完成：把画布切到 figures/ 的高分辨率图件。"""
        self._last_export_mode = "final"         # A09：之后的重渲按 600 dpi 走
        n_png = sum(1 for p in made if str(p).lower().endswith(".png"))
        self.logged.emit("INFO", f"定稿完成：{len(made)} 个文件（其中 png {n_png} 张）"
                                 "→ figures/")
        rr = self._last_rr
        if rr and rr.run_dir:
            d = rr.figures_dir("final")
            figs = sorted(d.glob("*.png")) if d and d.is_dir() else []
            if figs:
                self.canvas.set_images(figs, keep_index=True)
        self.backfill.update_progress()
        self._update_flow()

    def _emit_interpretation(self, rr) -> None:
        """把人话解读打到日志区（FR-9.3；只复述已有结果，不新增判断）。

        M4 修了两处：
        ① ``status.emit`` 原来误缩进在 ``except`` 里 → 正常路径永远不发"分析完成"
           状态（状态栏一直停在最后一条进度上）；
        ② ``set_scans(rr.scans)`` 会用**只含勾选文件**的 rr.scans 重建文件表 →
           用户没勾的文件在分析后从表里消失（实测 3 行变 2 行）。现在按文件名把
           新状态**合并**回完整清单，只更新剔除原因，不删行。
        """
        try:
            from core.explain import result_notes
            for line in result_notes(rr.results, rr.phi, rr.controls):
                self.logged.emit("INFO", line)
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("WARNING", f"结果解读生成失败（不影响结果）: {err}")
        self.status.emit(f"分析完成 — {rr.run_dir}")
        # 用"分析后的状态"刷新清单，但**保持完整行数**（未勾选的文件仍在表里）
        if rr.scans:
            self._merge_scans_into_table(rr.scans)

    def _merge_scans_into_table(self, analyzed: list) -> None:
        """把分析后的 Scan 状态合并回文件清单（按路径匹配，绝不丢行）。"""
        by_path = {str(getattr(s, "path", "")): s for s in analyzed}
        merged = []
        for s in self.scans or []:
            new = by_path.get(str(getattr(s, "path", "")))
            merged.append(new if new is not None else s)
        if not merged:
            merged = list(analyzed)
        self.scans = merged
        self.files.set_scans(merged)

    def _fill_table(self, rr) -> None:
        """填充结果表：组 | 角色 | k ± σk | R² | n | A(0) | ΦΔ ± σ | 状态/备注。"""
        rows = sorted(rr.results.items())
        self.table.setRowCount(len(rows))
        for r, (g, e) in enumerate(rows):
            f = e.get("pooled")
            p = rr.phi.get(g, {})
            if f is None or f.status == "FAILED":
                k_txt = "FAILED"
                r2 = n = a0 = "-"
            else:
                k_txt = f"{f.k:.6g} ± {f.k_err:.2g}"
                if f.truncated:
                    k_txt += "（已截断）"
                r2 = f"{f.r2:.5f}"
                n = str(f.n)
                a0 = f"{e.get('A0_pooled', float('nan')):.4f}"
            if p.get("status") == "OK":
                phi_txt = f"{p['phi']:.4f} ± {p['phi_err']:.4f}"
            elif p.get("status") == "NO_REF":
                phi_txt = "N/A（无参比组）"
            else:
                phi_txt = "N/A"
            note = (f.message if f else "")
            jd = rr.controls.get(g)
            if jd:
                note = (note + f" | 对照判定：{jd[0]}（{jd[1]}）").strip(" |")
            vals = [g, ROLE_ZH.get(e.get("role"), e.get("role")),
                    k_txt, r2, n, a0, phi_txt, note]
            for c, text in enumerate(vals):
                it = QTableWidgetItem(str(text))
                it.setToolTip(str(text))
                self.table.setItem(r, c, it)

    def _schedule_live_preview(self) -> None:
        try:
            self._preview_cfg = self.params.config(copy.deepcopy(self.cfg))
        except Exception:
            return
        self.live_preview.schedule()

    def _render_live_preview(self, out_dir) -> list:
        """最终渲染器低 dpi 预览（不拟合、不写正式 run）。"""
        rr = getattr(self, "_last_rr", None)
        if rr is None or not getattr(rr, "results", None) or not getattr(rr, "scans", None):
            return []
        cfg = copy.deepcopy(getattr(self, "_preview_cfg",
                                    self.params.config(copy.deepcopy(self.cfg))))
        cfg.setdefault("plot", {}).setdefault("jacs", {})["dpi"] = 100
        cfg["plot"]["jacs"]["formats"] = ["png"]
        try:
            return list(rerender_figures(rr.scans, rr.results, rr.phi, rr.controls,
                                         cfg, out_dir, export_mode="preview", only=None) or [])
        except Exception as err:                           # noqa: BLE001
            log.warning("实时预览渲染失败：%s", err)
            return []

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _apply_responsive_layout(self) -> None:
        """窄页（< NARROW_PAGE_W）批次树默认收起，顶部「批次」按钮可临时展开。"""
        if not hasattr(self, "_body"):
            return
        narrow = self.width() < NARROW_PAGE_W
        show = (not narrow) or self.btn_tree_toggle.isChecked()
        self.btn_tree_toggle.setVisible(narrow)
        if show == getattr(self, "_tree_shown", None):
            return
        self._tree_shown = show
        try:
            self._tree_col.setVisible(show)
            rest = max(380, self.width() - 320 - (230 if show else 0))
            self._body.setSizes([230 if show else 0, rest, 320])
        except RuntimeError:
            pass

    def on_activated(self) -> None:
        """切到本页时，若已有目录但清单为空则重新载入。"""
        if self.data_dir and not self.scans:
            self._load_dir(self.data_dir)

    def shutdown(self) -> None:
        """退出前收尾：停掉主任务与后台补渲线程。"""
        self.backfill.cancel()
        super().shutdown()


