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

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView,
                               QComboBox, QDoubleSpinBox, QLabel, QPushButton,
                               QScrollArea, QSplitter,
                               QTabBar, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from core.io import collect_scans
from core.pipeline import rerender_figures, run_directory
from ui.pages.base_page import PageBase
from ui.widgets.dye_time_panel import DyeTimePanel
from ui.widgets.export_bar import ExportBar, open_folder
from ui.widgets.file_table import ROLE_ZH, FileTable
from ui.widgets.mpl_canvas import CanvasPanel
from ui.widgets.param_panel import ParamPanel
from ui.workers import Job
from ui import theme

log = logging.getLogger("dye_tool.ui.phi")

#: A13（V2.x 审查）：补渲线程在 `wait()` 超时后**脱离页面生命周期**的强引用表。
#: 为什么需要：`_cancel_fig_job` 已经"超时就不丢引用"，但那个引用挂在**页面上**；
#: 用户关窗时页面析构 → 子 QThread 连同父对象一起被销毁 → Qt 报
#: "QThread: Destroyed while thread is still running" 并可能崩进程（同 0xC0000409 类）。
#: 这里把超时线程 `setParent(None)` 后存到模块级列表，让它活到自然结束。
_ORPHAN_JOBS: list = []

RESULT_HEADERS = ["组别", "角色", "k ± σk (s⁻¹)", "R²", "n", "A(0)",
                  "Φ_Δ ± σ", "状态 / 备注"]




#: 页面宽度低于它时，三栏改为二栏（右侧出图设置与左侧参数共用一栏，按钮切换）
NARROW_PAGE_W = 1180


class UnifiedSettingsPanel(QWidget):
    """V2.9.4 FR-70：统一页右侧设置区（可滚动/最小宽，兼容 ComparePage 设置项）。"""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QFormLayout
        self._form = QFormLayout()
        self.cmb_fig_style = QComboBox()
        self.cmb_fig_style.addItem('默认（柱状/森林）', None)
        self.cmb_fig_style.addItem('柱状', 'bar')
        self.cmb_fig_style.addItem('曲线', 'line')
        self.cmb_fig_style.addItem('散点', 'scatter')
        self.cmb_fig_style.setToolTip('工作台/定稿 fig c/d 图式；默认与 2.8.0 一致')
        self.chk_err = QCheckBox('误差棒')
        self.chk_err.setChecked(True)
        self.chk_annot = QCheckBox('关键值标注')
        self.chk_transparent = QCheckBox('透明背景')
        self.chk_ci = QCheckBox('Φ_Δ bootstrap 带')
        self.chk_ci.setToolTip('需结果含 bootstrap CI；无 CI 时自动不画')
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(['s', 'min', 'h'])
        self._form.addRow('图式（c/d）', self.cmb_fig_style)
        self._form.addRow('', self.chk_err)
        self._form.addRow('', self.chk_annot)
        self._form.addRow('', self.chk_transparent)
        self._form.addRow('', self.chk_ci)
        self._form.addRow('时间轴单位', self.cmb_unit)
        from ui.widgets.param_panel import FormatsPicker
        self.ed_formats = FormatsPicker()
        self.ed_formats.setToolTip('导出格式（png/svg/jpg/pdf）；与左侧参数面板同步')
        self._form.addRow('导出格式', self.ed_formats)
        self.ed_formats.textChanged.connect(self._apply)
        inner = QWidget()
        inner.setLayout(self._form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        self.setMinimumWidth(320)
        self.setMaximumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel('统一页设置（热更新）'))
        lay.addWidget(scroll, 1)
        for w in (self.cmb_fig_style, self.cmb_unit):
            w.currentIndexChanged.connect(self._apply)
        for w in (self.chk_err, self.chk_annot, self.chk_transparent, self.chk_ci):
            w.toggled.connect(self._apply)

    def _apply(self, *_args) -> None:
        from core.render import plotting_jacs
        plotting_jacs.set_figure_session(
            style=self.cmb_fig_style.currentData(),
            error_bars=self.chk_err.isChecked(),
            annotate=self.chk_annot.isChecked(),
            transparent=self.chk_transparent.isChecked(),
            time_unit=self.cmb_unit.currentText(),
            ci_band=self.chk_ci.isChecked())
        self.changed.emit()

    def values(self) -> dict:
        return {'style': self.cmb_fig_style.currentData(),
                'error_bars': self.chk_err.isChecked(),
                'annotate': self.chk_annot.isChecked(),
                'transparent': self.chk_transparent.isChecked(),
                'time_unit': self.cmb_unit.currentText(),
                'ci_band': self.chk_ci.isChecked()}


class ResultActions(QWidget):
    """V2.6 FR-40.5：结果区动作条（Φ_Δ / ε / A@λ / kobs 共用一套组件）。

    只做"产物入口 + 下一步 + 失败重试"三件事，不改任何业务逻辑：

    * 「打开输出文件」「复制结果」——有产物/结果文本时才可用；
    * 「下一步：③ 图件 …」——任务成功后可用，点击发 next_step_requested；
    * 「重试」——任务失败时可用，点击发 retry_requested（页面连到自己的 _run）；
    * lbl_fix 显示中文"如何修复"提示（非模态）。
    """

    message = Signal(str)
    next_step_requested = Signal(str)
    retry_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 next_target: str = "plot",
                 next_text: str = "下一步：③ 图件 / 定稿出图"):
        super().__init__(parent)
        self.next_target = str(next_target)
        self._output: Optional[Path] = None
        self._result_text = ""
        self.btn_open_output = QPushButton("打开输出文件")
        self.btn_copy_result = QPushButton("复制结果")
        self.btn_next_step = QPushButton(next_text)
        self.btn_next_step.setToolTip(
            "跳到下一步：③ 图件预览 / 定稿出图（或 ④ 档案查看历史结果）")
        self.btn_retry = QPushButton("重试")
        self.btn_retry.setToolTip("用当前参数重新运行（走同一条既有分析链路）")
        self.lbl_fix = QLabel("")
        self.lbl_fix.setWordWrap(True)
        theme.mark(self.lbl_fix, "warnHint")
        self.btn_open_output.clicked.connect(self._open_output)
        self.btn_copy_result.clicked.connect(self._copy_result)
        self.btn_next_step.clicked.connect(
            lambda: self.next_step_requested.emit(self.next_target))
        self.btn_retry.clicked.connect(self.retry_requested.emit)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.lbl_fix, 1)
        lay.addWidget(self.btn_open_output)
        lay.addWidget(self.btn_copy_result)
        lay.addWidget(self.btn_next_step)
        lay.addWidget(self.btn_retry)
        self.reset()

    # ── 对外状态接口 ──
    def set_output(self, path) -> None:
        """登记可打开的输出（文件或目录；None = 无产物 → 置灰）。"""
        self._output = Path(path) if path else None
        self.btn_open_output.setEnabled(self._output is not None)

    def set_result_text(self, text: str) -> None:
        """登记"复制结果"的文本（空串 → 置灰）。"""
        self._result_text = str(text or "")
        self.btn_copy_result.setEnabled(bool(self._result_text.strip()))

    def set_state(self, ok: bool, *, failure_hint: str = "") -> None:
        """成功后开放"下一步"；失败后开放"重试"并显示中文修复提示。"""
        ok = bool(ok)
        self.btn_next_step.setEnabled(ok)
        self.btn_retry.setEnabled(not ok)
        if ok:
            self.lbl_fix.setText("")
            self.lbl_fix.setVisible(False)
        else:
            self.lbl_fix.setText(str(failure_hint or ""))
            self.lbl_fix.setVisible(bool(failure_hint))

    def reset(self) -> None:
        """运行开始/尚无结果：四个动作全部置灰（不动 lbl_fix 文本）。"""
        self._output = None
        self._result_text = ""
        for b in (self.btn_open_output, self.btn_copy_result,
                  self.btn_next_step, self.btn_retry):
            b.setEnabled(False)

    # ── 动作 ──
    def _open_output(self) -> None:
        if self._output is None:
            self.message.emit("还没有可打开的输出文件")
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        try:
            opened = QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._output)))
        except Exception as err:                       # noqa: BLE001
            self.message.emit(f"打开输出文件失败：{err}")
            return
        self.message.emit(f"已打开输出文件：{self._output}" if opened
                          else f"无法自动打开，请手动前往：{self._output}")

    def _copy_result(self) -> None:
        from PySide6.QtGui import QGuiApplication
        text = self._result_text.strip()
        if not text:
            self.message.emit("当前没有可复制的结果")
            return
        try:
            QGuiApplication.clipboard().setText(self._result_text)
        except Exception as err:                       # noqa: BLE001
            self.message.emit(f"复制结果失败：{err}")
            return
        self.message.emit("结果已复制到剪贴板")


class _PreviewRenderJob(Job):
    """预览档后台补渲（P1 懒渲染）。

    与普通 ``Job`` 的唯一区别：**每画完一张图就发一次** ``wrote(str)``，让画布
    能"渲染完一张、出现一张"，而不是等整批跑完才刷新。它只跑绘图阶段
    （:func:`core.pipeline.rerender_figures`），不重新读文件、不重算数值，
    因此补渲出的图与"一次跑完"完全一致。
    """

    wrote = Signal(str)
    step_done = Signal(str)

    def run(self) -> None:                              # noqa: D102 —— QThread 约定
        try:
            from core import plotting_jacs
            steps = [s for s in (self._steps or [])
                     if s in plotting_jacs.STEP_ORDER]
            if not steps:
                steps = list(plotting_jacs.STEP_ORDER)
            figdir = self._rr.figures_dir(self._mode)
            before = {p.name for p in figdir.glob("*.png")} if figdir else set()
            ok = True
            for i, s in enumerate(steps):
                if self._cancel:
                    ok = False
                    break
                self._on_progress(int(100 * i / len(steps)),
                                  f"后台补渲 {s}（{i + 1}/{len(steps)}）")
                self._on_log("INFO", f"补渲图 {s} …")
                rerender_figures(self._rr.scans, self._rr.results, self._rr.phi,
                                 self._rr.controls, self._cfg, self._rr.run_dir,
                                 export_mode=self._mode, only=[s],
                                 group_dye=self._dye)
                now = {p.name for p in figdir.glob("*.png")} if figdir else set()
                for nm in sorted(now - before):
                    self.wrote.emit(str(figdir / nm))
                before = now
                # 本步骤渲染完毕：即使一张新图都没有（如图 d 无可用 Φ_Δ）也要报，
                # 好让界面收掉它名下"渲染中"的占位
                if not self._cancel:
                    self.step_done.emit(str(s))
            self._on_progress(100, "补渲完成" if ok else "补渲已取消")
            if self._cancel:
                self.cancelled.emit()          # S6：取消也要有明确终态
                return
            self.done.emit(ok)
        except Exception as err:                        # noqa: BLE001 —— 顶层兜底
            import traceback
            self.failed.emit(f"{type(err).__name__}: {err}", traceback.format_exc())

    def configure(self, rr, cfg: dict, steps, dye: dict,
                  mode: str = "preview") -> None:
        self._rr, self._cfg, self._steps = rr, cfg, list(steps or [])
        self._dye, self._mode = dict(dye or {}), mode


class PhiPage(PageBase):
    """Φ_Δ 全流程页。

    V1.1 增量：左侧多一个「预分析整定」页签（染料命名 + 时间工作表，FR-11.5/FR-12），
    「开始分析」默认只出**预览档**（FR-6.1），另有「定稿出图」按钮产出投稿图件。
    """

    TITLE = "① 单线态氧（k_obs → Φ_Δ）"
    SUBTITLE = ("选择数据 → 逐组拟合 k_obs → 指定参比与 Φ_ref → 得到 Φ_Δ 与图件。"
                "改参比只重算 Φ_Δ，不会重新拟合 k_obs。")

    #: V2.6 FR-40.5：结果区「下一步」→ ③ 图件 / 定稿出图
    next_step_requested = Signal(str)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.scans: list = []
        self._last_rr = None
        self._last_result_text: str = ""
        self._time_overrides: dict = {}
        self._fig_job: Optional[_PreviewRenderJob] = None
        self._last_fig_job = None               # M5：收尾判断用的"任务身份"
        self._steps_tried: set[str] = set()     # M5：本次 run 已排过的补渲步骤
        self._promised_figs: set[str] = set()      # 本轮"已排期"的图件名
        # A 道 FR-23：组页签（视图过滤；默认关闭，flow 布局由 main_window 开启）
        self._group_tabs_enabled = False
        self._group_tab_groups: list = []          # [None(对比), {组1}, {组2}, …]
        # FR-24.9：①数据页整定单向同步的预置载荷（下一次 _on_loaded 消费）
        self._pending_overrides: dict = {}
        # A25.1（V2.3 §12）：上次分析时的配置快照 + "结果过期"横幅状态
        self._cfg_snapshot: Optional[dict] = None
        self._banner_kind: str = ""        # "" | "plot" | "numeric"
        self._banner_sections: list = []
        self._banner_keys: list = []       # plot.jacs 内变过的叶子键（M5 细分用）
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
        # ── 顶部：目录选择 ──
        self.btn_dir = QPushButton("选择数据目录…")
        self.btn_reload = QPushButton("重新载入")
        self.btn_run = QPushButton("▶ 开始分析")
        theme.mark(self.btn_run, "primary")
        self.btn_run.setToolTip("跑完整流水线并出预览图（低分辨率、秒级完成）")
        self.btn_final = QPushButton("定稿出图")
        self.btn_final.setToolTip(
            "用刚才的数据直接重渲 600 dpi 的 png 到 figures/（不再重新读文件）\n"
            "产物与「直接全量跑」完全一致，是投稿用的那一份。")
        self.btn_final.setEnabled(False)
        # V2.9.1 FR-61：显式参比组（默认自动按 ref- 前缀/角色识别；UI 指定优先）
        self.cmb_reference = QComboBox()
        self.cmb_reference.setMaximumWidth(230)
        self.cmb_reference.setToolTip(
            "自动识别（默认）沿用 ref- 前缀/角色；也可显式指定任一组为参比。")
        self.cmb_reference.currentIndexChanged.connect(self._on_reference_changed)
        self._reference_group = ""
        # V2.9.1 O1：Φ_Δ 页自己的 Φ_ref（默认读 config，可记住到 dye_batch.yaml）
        self.sp_phi_ref = QDoubleSpinBox()
        self.sp_phi_ref.setRange(0.0001, 10.0)
        self.sp_phi_ref.setDecimals(4)
        self.sp_phi_ref.setSingleStep(0.05)
        self.sp_phi_ref.setMaximumWidth(90)
        self.sp_phi_ref.setValue(float((self.cfg.get('quantum_yield') or {}).get('phi_ref', 0.75)))
        self.sp_phi_ref.setToolTip("参比样 Φ_Δ（Φ_ref）；随「记住参比」写入 dye_batch.yaml")
        self.btn_remember_ref = QPushButton("记住参比…")
        self.btn_remember_ref.setMaximumWidth(100)
        self.btn_remember_ref.setToolTip("把显式参比组与 Φ_ref 写入本目录 dye_batch.yaml（显式动作）")
        self.btn_remember_ref.clicked.connect(self._remember_reference)
        self.sp_phi_ref.valueChanged.connect(self._recompute_phi_only)
        self.chk_lazy = QCheckBox("预览先出首图")
        self.chk_lazy.setToolTip(
            "勾选（默认，P1 懒渲染）：预览档先只渲首批图件（默认图 a，谱图最直观），\n"
            "让你尽快看到结果；其余图件在后台按顺序补渲，画完一张出现一张。\n"
            "取消勾选：仍按老行为一次画完整批预览图（图件多时要多等几秒）。\n"
            "两种方式的最终图件集合完全一样；「定稿出图」始终整批出 600 dpi。")
        self.lbl_dir = QLabel("未选择目录")
        theme.mark(self.lbl_dir, "muted")
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
        # V2.9.4 M2 修复：顶部工具条拆两行（1024 宽不裁切）
        top1 = QHBoxLayout()
        top1.addWidget(self.btn_dir)
        top1.addWidget(self.btn_reload)
        top1.addWidget(self.lbl_dir, 1)
        top2 = QHBoxLayout()
        top2.addWidget(QLabel("参比"))
        top2.addWidget(self.cmb_reference)
        top2.addWidget(QLabel("Φ_ref"))
        top2.addWidget(self.sp_phi_ref)
        top2.addWidget(self.btn_remember_ref)
        top3 = QHBoxLayout()
        top3.addWidget(self.chk_lazy)
        self.chk_live = QCheckBox("实时生图预览")
        self.chk_live.setChecked(True)
        self.chk_live.setToolTip("调整图式/标注/透明等生图参数时，用最终渲染器低 dpi 预览；不写正式产物")
        self.lbl_live = QLabel("预览待命")
        theme.mark(self.lbl_live, "hint")
        top3.addWidget(self.chk_live)
        top3.addWidget(self.lbl_live)
        self.btn_settings_toggle = QPushButton("出图设置")
        self.btn_settings_toggle.setCheckable(True)
        self.btn_settings_toggle.setToolTip("窗口较窄时左栏只放一组：按下显示出图设置（图式/误差棒/导出格式），"
                                            "再按一次回到分析参数。")
        top3.addWidget(self.btn_settings_toggle)
        top3.addStretch(1)
        top3.addWidget(self.btn_final)
        top3.addWidget(self.btn_run)

        # ── 左：参数面板 + 预分析整定（染料命名 / 时间工作表） ──
        self.params = ParamPanel()
        self.params.changed.connect(self._on_params_changed)
        scroll_p = QScrollArea()
        scroll_p.setWidget(self.params)
        scroll_p.setWidgetResizable(True)
        scroll_p.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.dye_panel = DyeTimePanel()
        self.dye_panel.dye_changed.connect(self._on_dye_changed)
        self.dye_panel.remember_changed.connect(self._on_remember)
        self.dye_panel.times_changed.connect(self._on_times_changed)

        left_tabs = QTabWidget()
        left_tabs.addTab(scroll_p, "分析参数")
        left_tabs.addTab(self.dye_panel, "预分析整定")
        left_tabs.setToolTip(
            "「预分析整定」= 分析之前给数据组起染料名、修不正常的时间；\n"
            "两个动作都只影响本次分析与 run 记录，不会改动原始文件。")
        left_tabs.setMinimumWidth(320)
        left_tabs.setMaximumWidth(380)

        # ── 右：文件清单 / 图件 / 结果表 ──
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
        box_files = QGroupBox("文件清单")
        lb = QVBoxLayout(box_files)
        lb.addWidget(self.files)

        self.canvas = CanvasPanel()
        # V2.3 FR-24.6：画布状态（复制成功等）→ 页面状态栏
        self.canvas.status_message.connect(self.status)
        # V2.5 FR-32.5（吸收 FR-31.2）：位图画布显示"当前图信息"（源文件/组/档位，
        # 元数据反查，零像素操作）。
        self.canvas.figure_changed.connect(self._on_figure_info)
        box_fig = QGroupBox("图件预览（图 a–e 与 JACS 堆叠图）")
        lf = QVBoxLayout(box_fig)
        lf.addWidget(self.canvas)

        self.table = QTableWidget(0, len(RESULT_HEADERS))
        self.table.setHorizontalHeaderLabels(RESULT_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        # FR-23.6b：点结果表某行 → 跳到该组页签（组页签未启用时该方法直接返回）
        self.table.cellClicked.connect(self._on_result_row_clicked)
        self.lbl_stage_bd = QLabel("先逐组拟合 k_obs，再由同一批 k 换算 Φ_Δ")
        theme.mark(self.lbl_stage_bd, "hint")
        self.lbl_stage_bd.setWordWrap(True)
        box_res = QGroupBox("结果：k_obs 与 Φ_Δ")
        lr = QVBoxLayout(box_res)
        lr.addWidget(self.lbl_stage_bd)
        lr.addWidget(self.table)

        # V2.9.5 FR-72：窄窗口下文件清单/图件纵向排列，画布获得完整宽度
        upper = QSplitter(Qt.Vertical)
        upper.addWidget(box_files)
        upper.addWidget(box_fig)
        upper.setSizes([220, 420])
        upper.setChildrenCollapsible(False)

        # A 道 FR-23.1：组页签条（QTabBar 只切换**视图过滤**——文件表与画布
        # 始终是同一实例，绝不按组复制控件/状态）
        self.group_tabs = QTabBar()
        self.group_tabs.setExpanding(False)
        self.group_tabs.setUsesScrollButtons(True)
        self.group_tabs.setVisible(False)
        self.group_tabs.currentChanged.connect(self._on_group_tab_changed)
        self._group_tab_host = QWidget()
        lh = QVBoxLayout(self._group_tab_host)
        lh.setContentsMargins(0, 0, 0, 0)
        lh.addWidget(self.group_tabs)
        lh.addWidget(upper, 1)

        right = QSplitter(Qt.Vertical)
        right.addWidget(self._group_tab_host)
        right.addWidget(box_res)
        right.setSizes([520, 240])

        self.settings_panel = UnifiedSettingsPanel()
        self.settings_panel.ed_formats.setText(self.params.ed_formats.text())
        self.settings_panel.ed_formats.textChanged.connect(
            lambda: self.params.ed_formats.setText(self.settings_panel.ed_formats.text()))
        self.settings_panel.changed.connect(lambda: self.status.emit("统一页设置已更新（下一次出图/重算生效）"))
        from ui.widgets.live_render_preview import LiveRenderPreview
        self.live_preview = LiveRenderPreview(self, self._render_live_preview)
        self.live_preview.updated.connect(self.canvas.set_images)
        self.live_preview.state.connect(self.lbl_live.setText)
        self.live_preview.failed.connect(lambda m: self.logged.emit("WARNING", f"实时预览失败：{m}"))
        self.chk_live.toggled.connect(self.live_preview.set_enabled)
        self.settings_panel.changed.connect(self._schedule_live_preview)
        body = QSplitter(Qt.Horizontal)
        right.setMinimumWidth(380)
        self.settings_panel.setMinimumWidth(260)
        self.settings_panel.setMaximumWidth(280)
        body.addWidget(left_tabs)
        body.addWidget(right)
        body.addWidget(self.settings_panel)
        body.setChildrenCollapsible(False)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setStretchFactor(2, 0)
        body.setSizes([300, 454, 270])
        self._left_tabs = left_tabs
        self._right = right
        self._body = body
        self.btn_settings_toggle.toggled.connect(lambda _: self._apply_responsive_layout())
        self._apply_responsive_layout()

        # ── 底：进度 + 导出 ──
        self.export = ExportBar(on_workbook=self._export_workbook,
                                on_dashboard=self._export_dashboard)
        # V2.3 FR-24.2/24.6：导出条的常规反馈走状态栏（不弹模态）。
        # A15（V2.x 审查）：`canvas.status_message` 已在创建画布处连接过一次，
        # 这里**再连一次**会让每条画布消息在状态栏出现两遍（与 F2 同类）。
        self.export.message.connect(self.status)
        # V2.6 FR-40.5：结果区动作条（打开输出 / 复制结果 / 下一步 / 重试）
        self.actions = ResultActions(
            self, next_target="plot",
            next_text="下一步：③ 图件预览 / 定稿出图")
        self.actions.message.connect(self.status)
        self.actions.next_step_requested.connect(self.next_step_requested.emit)
        self.actions.retry_requested.connect(self._run)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        lay.addLayout(top1)
        lay.addLayout(top2)
        lay.addLayout(top3)
        # A25.1（V2.3 §12）：结果过期横幅（参数改了但结果没重算时出现，非模态）
        self.banner = QFrame()
        self.banner.setObjectName("staleBanner")
        theme.mark(self.banner, "banner")
        self.banner.setVisible(False)
        bh = QHBoxLayout(self.banner)
        bh.setContentsMargins(8, 6, 8, 6)
        self.lbl_banner = QLabel("")
        self.lbl_banner.setWordWrap(True)
        self.btn_banner_action = QPushButton("…")
        self.btn_banner_hide = QPushButton("忽略")
        self.btn_banner_hide.setToolTip("只隐藏提示；下次改参数会再次出现")
        bh.addWidget(self.lbl_banner, 1)
        bh.addWidget(self.btn_banner_action)
        bh.addWidget(self.btn_banner_hide)
        self.btn_banner_action.clicked.connect(self._on_banner_action)
        self.btn_banner_hide.clicked.connect(self._dismiss_banner)
        lay.addWidget(self.banner)
        lay.addWidget(body, 1)
        lay.addWidget(self.progress_row())
        lay.addWidget(self.actions)
        lay.addWidget(self.export)

        # A25.2（V2.3 §12）：结果表 / 组页签右键菜单；文件表与画布的右键由各自组件实现
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_result_context_menu)
        self.group_tabs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.group_tabs.customContextMenuRequested.connect(
            self._on_group_tab_context_menu)
        self.files.status_message.connect(self.status)
        # 勾选状态来自配置（gui.lazy_preview），改动即持久化到 config.yaml
        self.chk_lazy.blockSignals(True)
        self.chk_lazy.setChecked(self._lazy_enabled())
        self.chk_lazy.blockSignals(False)

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

    def dragEnterEvent(self, e) -> None:               # noqa: N802 —— Qt 接口
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                    # noqa: N802 —— Qt 接口
        """支持拖入目录或文件（FR-1.1）。"""
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        dirs = [p for p in urls if p.is_dir()]
        if dirs:
            self._load_dir(dirs[0])
        elif urls:
            self._load_dir(urls[0].parent)
        e.acceptProposedAction()

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
        # A 道 FR-23.1：载入后重建组页签（保持当前选中组；<2 组则整条隐藏）
        self._rebuild_group_tabs()

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
            self._refresh_group_titles()
            self.logged.emit("INFO", f"染料命名：{group} → {dye}")
            return
        # 恢复自动命名：``set_dye("", "")`` 只留一个空键，显示侧据此回落自动值
        self.files.set_dye("", "")
        self._refresh_group_titles()

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
        self._refresh_group_titles()
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
        self._update_stale_banner()        # A25.1：让"结果已过期"可见

    # ══════════════ V2.3 §12 C 道 FR-25：交互逻辑 ══════════════
    #: 只影响**出图**的分区（变化 → 只需重渲，不重算数值）。
    _PLOT_LIKE = ("plot",)
    #: 只影响**导出**、或对本页图件**无影响**的分区（变化 → 不提示过期）。
    #: A10（V2.x 审查）：`spectra` 原来是 `_PLOT_LIKE`——那是作图台（FR-4）的参数，
    #: Φ_Δ 的图 a 用的是 `plot.jacs.spectra_overlay`，与它无关 → 误报"仅出图参数变了"。
    _EXPORT_ONLY = ("excel", "qc_dashboard", "spectra")

    def _cfg_sections(self) -> dict:
        """把 cfg 拆成"分区 → 内容"（排除纯 UI 会话态，避免误报过期）。"""
        import json
        out: dict = {}
        for k, v in (self.cfg or {}).items():
            if str(k).startswith("_") or k == "gui":
                continue
            try:
                out[str(k)] = json.dumps(v, sort_keys=True, default=str)
            except Exception:                              # noqa: BLE001
                out[str(k)] = repr(v)
        return out

    def stale_sections(self) -> list:
        """相对**上次分析**变过的分区名（无快照/无结果 → 空）。"""
        if not self._cfg_snapshot or self._last_rr is None:
            return []
        old = {k: v for k, v in self._cfg_snapshot.items()}
        now = self._cfg_sections()
        # 快照里也按同一口径序列化，保证可比
        snap: dict = {}
        for k, v in old.items():
            if str(k).startswith("_") or k == "gui":
                continue
            try:
                import json
                snap[str(k)] = json.dumps(v, sort_keys=True, default=str)
            except Exception:                              # noqa: BLE001
                snap[str(k)] = repr(v)
        changed = [k for k in sorted(set(snap) | set(now))
                   if snap.get(k) != now.get(k)]
        return [k for k in changed if k not in self._EXPORT_ONLY]

    def _flatten_jacs(self, node, prefix: str = "") -> dict:
        """把 ``plot.jacs`` 展平成 ``{点分键: 值}``（M5 归因用）。"""
        out: dict = {}
        for k, v in (node or {}).items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(self._flatten_jacs(v, path))
            else:
                out[path] = repr(v)
        return out

    def stale_keys(self) -> list:
        """``plot.jacs`` 内相对上次分析变过的**叶子键**（M5 细分用）。"""
        if not self._cfg_snapshot or self._last_rr is None:
            return []
        try:
            old = self._flatten_jacs(
                ((self._cfg_snapshot.get("plot") or {}).get("jacs")) or {})
            now = self._flatten_jacs(
                ((self.cfg.get("plot") or {}).get("jacs")) or {})
        except Exception:                              # noqa: BLE001
            return []
        return [k for k in sorted(set(old) | set(now))
                if old.get(k) != now.get(k)]

    def on_cfg_changed(self) -> None:
        """外部（模板套用/批次载入）换了 cfg 后刷新"结果过期"横幅（V2.4 FR-26.3③）。

        只更新提示，**不自动重跑**：用户按 F5 才真正重算。
        """
        try:
            self._update_stale_banner()
        except Exception as err:                       # noqa: BLE001 —— 横幅不得废页
            log.warning("刷新过期横幅失败：%s", err)

    def _update_stale_banner(self) -> None:
        """按"变的是哪些分区"决定横幅文案与动作（A25.1 分层重算）。"""
        secs = self.stale_sections()
        if not secs:
            self._hide_banner()
            return
        self._banner_sections = secs
        self._banner_keys = self.stale_keys() if "plot" in secs else []
        only_plot = all(s in self._PLOT_LIKE for s in secs)
        if only_plot:
            self._banner_kind = "plot"
            self.lbl_banner.setText(
                "⚠ 结果已过期：仅出图参数变了（数值未变）——可只重渲受影响图，"
                "不必重算。")
            self.btn_banner_action.setText("重渲受影响图")
        else:
            self._banner_kind = "numeric"
            numeric = [s for s in secs if s not in self._PLOT_LIKE]
            self.lbl_banner.setText(
                "⚠ 结果已过期：数值相关参数变了（"
                + "、".join(numeric[:4]) + ("…" if len(numeric) > 4 else "")
                + "）——需要重算分析。")
            self.btn_banner_action.setText("重算分析")
        self.banner.setVisible(True)

    def _hide_banner(self) -> None:
        self._banner_kind = ""
        self._banner_sections = []
        if hasattr(self, "banner"):
            self.banner.setVisible(False)

    def _dismiss_banner(self) -> None:
        """用户点「忽略」：只隐藏（不改快照，下次再改参数仍会提示）。"""
        if hasattr(self, "banner"):
            self.banner.setVisible(False)

    # ── 自动化契约（A25.1/A25.2 验收用） ──
    def banner_visible(self) -> bool:
        return bool(hasattr(self, "banner") and not self.banner.isHidden())

    def banner_kind(self) -> str:
        return self._banner_kind

    def _on_banner_action(self) -> None:
        """横幅按钮：仅出图参数变 → 只重渲受影响图；数值变 → 走完整重算。"""
        if self._banner_kind == "plot":
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
        self._cancel_fig_job()          # 先停后台补渲（同一 run_dir，不能并发写）
        cfg = self.params.config(self.cfg)
        secs = self.stale_sections() or ["plot"]
        keys = self._banner_keys or (self.stale_keys() if "plot" in secs else [])
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
            if not self.group_tabs.isHidden():
                self._on_group_tab_changed(self.group_tabs.currentIndex())
            self._cfg_snapshot = copy.deepcopy(cfg)
            self._hide_banner()
            self.logged.emit("INFO",
                             f"已重渲 {len(made)} 张受影响图件（数值未重算）")

        self.run_task(task, "重渲受影响图", done,
                      busy_widgets=[self.btn_run, self.btn_banner_action])

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
        act_jump = menu.addAction("跳到该组页签")
        act_jump.setEnabled(not self.group_tabs.isHidden()
                            and len(self._group_tab_groups) > 1)
        act_jump.triggered.connect(lambda *_: self._on_result_row_clicked(row))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_result_row(self, row: int) -> None:
        cells = self._result_row_cells(row)
        try:
            QApplication.clipboard().setText("\t".join(cells))
            self.status.emit(f"已复制结果行：{cells[0] if cells else ''}")
        except Exception as err:                           # noqa: BLE001
            self.status.emit(f"复制失败：{err}")

    def _on_group_tab_context_menu(self, pos) -> None:      # noqa: ANN001
        from PySide6.QtWidgets import QMenu
        idx = self.group_tabs.tabAt(pos)
        if idx <= 0 or idx >= len(self._group_tab_groups):
            return                                  # 「全部组（对比）」没有"本组"动作
        gs = self._group_tab_groups[idx]
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
        menu.exec(self.group_tabs.mapToGlobal(pos))

    # ══════════════ A 道 FR-23：按染料组分页（视图过滤） ══════════════
    def set_group_tabs(self, enabled: bool) -> None:
        """启用/关闭「按染料组分页」。

        flow 布局由 ``main_window`` 显式开启；classic 保持默认关闭（V1.x 外观
        一字不变）。开启后仍需 ≥2 个组才会出现页签条（FR-23.1）。
        """
        self._group_tabs_enabled = bool(enabled)
        if not enabled:
            self.group_tabs.setVisible(False)
            self.files.set_group_filter(None)
            self.canvas.set_visible_filter(None)
            self._group_tab_groups = []
            return
        if self.scans:
            self._rebuild_group_tabs()

    def group_tabs_enabled(self) -> bool:
        """组页签能力是否开启（自动化契约）。"""
        return bool(getattr(self, "_group_tabs_enabled", False))

    def group_tab_labels(self) -> list[str]:
        """当前页签标题（测试用；未显示时返回空列表）。

        注意用 ``isHidden()`` 而不是 ``isVisible()``：离屏测试/页签尚未显示时
        ``isVisible()`` 会因祖先隐藏而恒假。
        """
        if self.group_tabs.isHidden():
            return []
        return [self.group_tabs.tabText(i)
                for i in range(self.group_tabs.count())]

    def _groups(self) -> list[str]:
        """当前 scans 的组名（排序）。"""
        return sorted({s.group for s in (getattr(self, "scans", None) or [])})

    def _group_title(self, group: str) -> str:
        """页签标题：染料名 · 组名（两者相同时只显示一个）。"""
        try:
            from core.dyes import auto_dye_name
            dye = str(self.files.dye_map().get(group) or auto_dye_name(group))
        except Exception:                                  # noqa: BLE001
            dye = group
        return group if dye == group else f"{dye} · {group}"

    def _group_figure_names(self, groups: set) -> set:
        """这些组对应的图件名集合（命名与 ``plotting_jacs`` 同源，防漂移）。"""
        try:
            from ui.prep.names import fig_a_names, stack_tag
            from core.render.plotting import _safe
        except Exception:                                  # noqa: BLE001
            return set()
        # A03（V2.x 审查）：原来用 `_groups()`（含已剔除/background）判"单组"，
        # 而图 a 的命名只看**出图组**。于是"1 样品组 + 1 背景组"时图 a 实际叫
        # `fig_a_spectra_overlay`（单组特例），组页签却按多组算 → 该图**哪个组页签
        # 都看不见**。这里与 `_fig_a_names` 同口径：只数可出图的样品组。
        draw_groups = sorted({str(s.group) for s in (self.scans or [])
                              if not s.excluded
                              and str(getattr(s, "role", "")) not in
                              ("background", "dark")})
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

    def _rebuild_group_tabs(self) -> None:
        """按当前 scans 重建页签：``[全部组（对比）]`` + 每组一个。

        组数 < 2 或能力未开启 → 整条页签条隐藏、过滤复位（界面与 V1.x 一致）。
        """
        groups = self._groups()
        prev = self.group_tabs.currentIndex()
        # A04（V2.x 审查）：原来只保留 index。重载后组集合/排序变了 → 落在**错的组**。
        # 改为按"当前页签代表的组集合"保位；找不到就回「全部组（对比）」。
        prev_groups = None
        if 0 < prev < len(self._group_tab_groups):
            prev_groups = set(self._group_tab_groups[prev] or ())
        self.group_tabs.blockSignals(True)
        while self.group_tabs.count():
            self.group_tabs.removeTab(0)
        self._group_tab_groups = [None]                     # 第 0 页 = 全部组（对比）
        self.group_tabs.addTab("全部组（对比）")
        for g in groups:
            self.group_tabs.addTab(self._group_title(g))
            self._group_tab_groups.append({g})
        self.group_tabs.blockSignals(False)
        show = bool(self._group_tabs_enabled) and len(groups) >= 2
        self.group_tabs.setVisible(show)
        if not show:
            self.files.set_group_filter(None)
            self.canvas.set_visible_filter(None)
            return
        idx = 0
        if prev_groups:
            for i, gs in enumerate(self._group_tab_groups):
                if gs and set(gs) == prev_groups:
                    idx = i
                    break
        idx = min(max(idx, 0), self.group_tabs.count() - 1)
        self.group_tabs.setCurrentIndex(idx)
        self._on_group_tab_changed(idx)

    def _refresh_group_titles(self) -> None:
        """染料命名变化后只刷新页签标题（不重建、不动选中）。"""
        if self.group_tabs.isHidden():
            return
        self.group_tabs.blockSignals(True)
        try:
            for i in range(1, self.group_tabs.count()):
                gs = self._group_tab_groups[i] if i < len(self._group_tab_groups) else None
                if gs:
                    self.group_tabs.setTabText(i, self._group_title(next(iter(gs))))
        finally:
            self.group_tabs.blockSignals(False)

    def _on_group_tab_changed(self, idx: int) -> None:
        """切页签：只改文件表**行可见性**与画布**可见集**，不重建任何控件。"""
        if not self._group_tabs_enabled:
            return
        groups = None
        if 0 < idx < len(self._group_tab_groups):
            groups = self._group_tab_groups[idx]
        self.files.set_group_filter(groups)
        if groups is None:
            self.canvas.set_visible_filter(None)
        else:
            names = self._group_figure_names(groups)
            # 画布字典键是**文件名**（含扩展名），这里按 stem 比较（png/svg 都适用）
            self.canvas.set_visible_filter(lambda n: Path(n).stem in names)

    def _on_result_row_clicked(self, row: int, _col: int = 0) -> None:
        """FR-23.6b：点结果表某行 → 切到该组的页签。"""
        if not self._group_tabs_enabled or self.group_tabs.isHidden():
            return
        rr = getattr(self, "_last_rr", None)
        if rr is None or row < 0:
            return
        rows = sorted((getattr(rr, "results", None) or {}).items())
        if row >= len(rows):
            return
        group = rows[row][0]
        for i, gs in enumerate(self._group_tab_groups):
            if gs and group in gs:
                self.group_tabs.setCurrentIndex(i)
                return

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
        out_root = self._out_root()
        self._last_export_mode = "preview"       # A09：本次产物档位
        self.table.setRowCount(0)
        self._cancel_fig_job()                     # 新分析 → 旧的补渲作废
        d = self.data_dir
        dye = self.dye_panel.dye_map()
        tov = dict(self._time_overrides)
        # P1：预览档只先画首批（默认图 a），其余交给后台补渲
        lazy = self._lazy_enabled()
        first = self._first_batch() if lazy else None
        self.logged.emit("INFO",
                         f"预览懒渲染：先出 {'|'.join(first)}，其余后台补渲" if lazy
                         else "预览懒渲染已关：预览档一次画完整批")
        self._update_figs_progress()

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
        self._cancel_fig_job()                     # 定稿整批出图 → 补渲不必再跑
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

    def _out_root(self) -> Path:
        """输出根目录（V2.5 D10：唯一解析在 core.paths.resolve_out_root）。"""
        from core.paths import resolve_out_root
        return resolve_out_root(self.cfg)

    def _on_analyzed(self, rr) -> None:
        self._last_rr = rr
        # V2.6 FR-40.5：结果区动作条（打开输出 / 复制结果 / 下一步 / 重试）
        try:
            self._update_result_actions(rr)
        except Exception as err:                       # noqa: BLE001 —— 动作条不得废页
            log.warning("结果区动作条刷新失败：%s", err)
        if getattr(rr, "ok", False):
            # A25.1：分析成功 → 记下本次用的配置；之后参数再变才算"过期"
            self._cfg_snapshot = copy.deepcopy(self.params.config(self.cfg))
            self._hide_banner()
        self._fill_table(rr)
        if rr.run_dir:
            self.export.set_run_dir(rr.run_dir)
            self._promised_figs = set()
            self._steps_tried = set()      # M5：新的一轮分析 → 补渲步骤重新计
            # 预览档产物在 figures_preview/；定稿后才在 figures/
            figs = rr.figures or self._preview_pngs(rr)
            self.canvas.set_images(figs, keep_index=True)
            # A 道 FR-23.5：整批换图后重放当前组过滤（懒渲染排队占位不受影响）
            if not self.group_tabs.isHidden():
                self._on_group_tab_changed(self.group_tabs.currentIndex())
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
        self._render_rest()                       # P1：后台补渲其余图件
        self._update_figs_progress()
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
            for group, entry in sorted(results.items()):
                pooled = (entry or {}).get("pooled") \
                    if isinstance(entry, dict) else None
                pooled = pooled or {}
                status = str(pooled.get("status") or "—")
                lines.append("\t".join([
                    str(group), status,
                    self._fmt_num(pooled.get("k")),
                    self._fmt_num(pooled.get("R2")),
                    self._fmt_num(pooled.get("Phi"))]))
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
        ok = bool(getattr(rr, "ok", False))
        if ok:
            run_dir = str(getattr(rr, "run_dir", "") or "")
            text = self._build_result_text(rr)
            self._last_result_text = text
            self.actions.set_output(run_dir or None)
            self.actions.set_result_text(text)
            self.actions.set_state(True)
            return
        self.actions.set_output(None)
        self.actions.set_result_text("")
        self.actions.set_state(False, failure_hint=self._failure_hint(rr))

    # ══════════════ P5 / P8：导出条上的两个出口 ══════════════
    # M3（评审记录）：这两件事**都必须放到后台线程**。
    # 原先直接在 UI 线程里跑：大 run 的 openpyxl 写盘（秒级）与 300 dpi 整页渲染
    # 会把界面冻住，而且 `_export_dashboard` 末尾的 `except Exception: pass`
    # **没有任何日志**——画布刷新失败用户无从知晓。
    # 本页本来就有 `run_task` 范式（见 `_run`），照搬即可。
    #
    # G16（第二轮审查）：两个导出都必须先过 `_jobs_busy()`。`run_task` 自己只看
    # `self._busy`，看**不到** `_fig_job`（后台补渲）——于是导出可与补渲并行，
    # 而两边都动 matplotlib 全局态（qc_dashboard 在线程里 `matplotlib.use("Agg")`
    # + pyplot），存在 rcParams/后端竞态。`_jobs_busy()` 本来就是为此写的（:539），
    # 定义完一直没人调。
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
        if self._jobs_busy():
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
        if self._jobs_busy():                       # G16：见 _export_workbook 的说明
            self.logged.emit("WARNING", "还有后台任务在跑（分析或补渲），稍候再导出")
            return
        # 给目录 → 引擎自己按 cfg["qc_dashboard"]["export"] 决定文件名与格式
        #
        # G17（第二轮审查）：原来写 `Path(self.export.run_dir() or
        # self.cfg["gui"].get("out_root","runs"))` —— 相对 out_root **不经 `_out_root()`**，
        # 于是 run_dir 为 None 时目录会落在**进程 CWD**（取决于从哪儿启动 exe），
        # 与其它产物各走各的。这里复用现成的 `_out_root()`（:431）。
        out_dir = Path(self.export.run_dir() or self._out_root())
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
    def _jobs_busy(self) -> bool:
        """是否还有本页发起的后台任务在跑（主任务或补渲）。"""
        if self._busy:
            return True
        return bool(self._fig_job is not None and self._fig_job.isRunning())

    def _render_rest(self) -> None:
        """把"还没画的预览图件"排进后台任务；首图已可看，其余画完一张出现一张。"""
        rr = self._last_rr
        if rr is None or not rr.run_dir or not rr.ok:
            return
        if self._lazy_enabled() is False:
            return                                  # 用户关掉了懒渲染 → 一次已画完
        # A07（V2.x 审查）：`expected_figures` 返回**不带扩展名**的名字，而
        # `_preview_pngs` 返回文件名（带 .png）——原来直接比较导致"已画出的图"
        # 永远不匹配：日志"其余 N 张"偏大、`_promised_figs` 去重也失效。
        # 统一按词干比较。
        have = {p.stem for p in self._preview_pngs(rr)}
        try:
            from core.plotting_jacs import expected_figures
            exp = expected_figures(rr.scans, rr.results, rr.phi, rr.cfg or self.cfg,
                                   "preview")
        except Exception:                           # noqa: BLE001
            return
        need = [n for n in exp if n not in have]
        steps = self._remaining_steps(have, exp)
        if not need or not steps:
            self._update_figs_progress()
            return
        if self._fig_job is not None and self._fig_job.isRunning():
            return                                  # 已有补渲在跑，不重复排
        # 先占好"渲染中"的图位：否则补渲期间下拉里的图件数量会忽多忽少
        extra = [n for n in need if n not in self._promised_figs]
        if extra:
            from core.plotting_jacs import _step_of
            self.canvas.queued([f"{n}.png" for n in extra],
                               steps={f"{n}.png": _step_of(n) for n in extra})
            self._promised_figs |= set(extra)
        self.logged.emit("INFO", f"预览档先出 {len(have)} 张；其余 {len(need)} 张"
                                 "转入后台补渲（画完一张出现一张，不影响已出结果）")
        job = _PreviewRenderJob(lambda p, l: None, "后台补渲", self)
        job.configure(rr, self.params.config(self.cfg), steps,
                      self.dye_panel.dye_map(), "preview")
        job.wrote.connect(self._on_fig_written)
        job.step_done.connect(self._on_fig_step_done)
        job.progress.connect(self._on_progress)
        job.logged.connect(self.logged)
        job.failed.connect(self._on_failed)
        job.done.connect(self._on_figs_done)
        job.cancelled.connect(self._on_figs_cancelled)
        # QThread.finished 在 done/wrote 全部投递之后才发出 → 用它做"这一轮真的画完了"
        # 的屏障，否则末图的 wrote 可能排在 done 后面（事件队列顺序）而被漏掉
        job.finished.connect(lambda j=job: self._figs_settle(j))
        self._fig_job = job
        self._last_fig_job = job          # M5：done 会清 _fig_job，收尾判断用这个
        self._steps_tried |= set(steps)   # M5：排过就算试过（见 _remaining_steps）
        job.start()

    def _remaining_steps(self, have: set[str], exp=None) -> list[str]:
        """还有哪些**步骤**需要补渲（按 STEP_ORDER 顺序）。

        "剩余"= 该步骤**本次还没排过**，且它名下确实还有图没画出来。

        M5：原来只看"哪一步没有产物文件"，于是**画不出东西的步骤永远算剩余**
        （典型：没有参比组时图 d 一张都不产出）→ 收尾逻辑每轮都再排一个任务，
        每次秒退、无限循环。所以两件事都要判：
        ① ``exp``（预期清单）说明该步骤名下是否还有缺失的图位；
        ② ``_steps_tried`` 说明该步骤是否已经排过（排过就不再重排）。
        """
        from core.plotting_jacs import STEP_ORDER, _step_of
        done = {_step_of(n) for n in have}
        names = list(exp) if exp is not None else list(have)
        active: set = set()
        for s in STEP_ORDER:
            if s in done:
                continue
            want = [n for n in names if _step_of(n) == s]
            if want and not any(n in have for n in want):
                active.add(s)                # 该步骤确实还有没画出来的图
        return [s for s in STEP_ORDER
                if s in active and s not in self._steps_tried]

    def _expected_now(self, rr) -> list:
        """当前数据/参数下的预期图件名清单（失败 → 空表）。"""
        try:
            from core.plotting_jacs import expected_figures
            return expected_figures(rr.scans, rr.results, rr.phi,
                                    rr.cfg or self.cfg, "preview")
        except Exception:                           # noqa: BLE001
            return []

    def pending_count(self) -> int:
        """画布上还有多少个"渲染中"的图位（供收尾判断）。"""
        return int(self.canvas.pending_count())

    def _figs_settle(self, job=None) -> None:
        """一轮补渲收尾：决定"继续补下一批"还是"清占位并结束"。

        由 ``QThread.finished`` 触发（那时 ``wrote``/``done`` 都已投递完），因此
        不会漏掉末图的信号。**绝不会重复补渲**：只有还存在"没试过且有内容"的
        步骤时才续跑，否则只清占位——图 d 在无可用 Φ_Δ 时正是后者。

        M5：判断"还是不是当前这一轮"**不能只看 ``job is self._fig_job``**——
        ``done`` 信号先于 ``finished`` 投递（实测顺序 ``[done, finished]``），
        ``_on_figs_done`` 已经把 ``_fig_job`` 置 None，于是那个判断恒真、收尾/续渲
        永远提前返回（当前靠 wrote/step_done 信号"碰巧"覆盖清理；一旦补渲中途抛
        异常，占位就永远留在界面上）。现在按**任务身份**判断：仍持有这个任务对象
        即视为本轮（``_last_fig_job`` 不会被 done 清掉）。
        """
        if job is not None and job is not getattr(self, "_last_fig_job", None):
            return                                # 确实换了一轮 → 本次收尾作废
        rr = self._last_rr
        if rr is None or not rr.run_dir:
            return
        have = {p.name for p in self._preview_pngs(rr)}
        if self._remaining_steps(have, self._expected_now(rr)):
            self._render_rest()                   # 还有步骤没试过 → 补下一批
        else:
            self._figs_settle_done()

    def _figs_settle_done(self) -> None:
        """真正收尾：清掉"到期没人认领"的排队位并刷新计数。"""
        dropped = self.canvas.clear_queued()
        n = self._update_figs_progress()
        if dropped:
            self.logged.emit("INFO", f"预览档共 {n} 张图件"
                                     f"（{dropped} 个图位本次无内容，已从列表移除）")

    def _on_fig_written(self, path: str) -> None:
        """后台每画完一张就替换掉对应的"渲染中"占位。"""
        self.canvas.mark_ready(path)
        self._promised_figs.discard(Path(path).stem)   # A07：promised 存的是词干
        self._update_figs_progress()

    def _on_fig_step_done(self, step: str) -> None:
        """某一步渲染完毕：收掉它名下"不会再有内容"的占位（如图 d 无可用 Φ_Δ）。"""
        if self.canvas.resolve_step(step):
            alive = set(self.canvas.queued_names())
            self._promised_figs &= alive
        self._update_figs_progress()

    def _on_figs_done(self, ok) -> None:
        self._fig_job = None
        n = self._update_figs_progress()
        if ok:
            self.logged.emit("INFO", f"后台补渲结束：预览档现有 {n} 张图件"
                                     "（「定稿出图」出 600 dpi 投稿图件）")
        # 后续动作（续补下一批 / 清占位）交给 _figs_settle，它由 QThread.finished
        # 触发——那里才是"本轮信号全部投递完毕"的可靠时点

    def _on_figs_cancelled(self) -> None:
        """补渲被取消：收掉占位、把按钮放回来（S6，别让界面卡在"渲染中"）。"""
        self._fig_job = None
        dropped = self.canvas.clear_queued()
        self._promised_figs.clear()
        self._update_figs_progress()
        self.logged.emit("WARNING", f"后台补渲已取消（{dropped} 个未出图的占位已移除）；"
                                    "已画好的图件不受影响，可随时再点「定稿出图」")

    def _cancel_fig_job(self) -> None:
        """取消进行中的补渲（窗口关闭或用户发起新任务时用）。

        O7：``wait(3000)`` 对"单步出图"可能不够（一张 600 dpi 大图 + 多组时能超过
        3 秒），而**超时后直接丢引用**会留下正在运行的 QThread → Qt 报
        "QThread: Destroyed while thread is still running" 并可能崩进程。
        这里等更久；仍不退就**保留引用**（不置 None），让它在后台自然结束——
        宁可多留一个对象，也不能让线程被 GC 掉。
        """
        j = self._fig_job
        if j is None:
            return
        if j.isRunning():
            j.cancel()
            if not j.wait(15000):
                self.logged.emit("WARNING",
                                 "后台补渲超时未停（已脱离页面，让它自然结束；"
                                 "期间请勿继续操作）")
                # A13：**脱离页面**再交给模块级强引用。只保留 self._fig_job 不够——
                # 页面析构时 QThread 作为子对象会被一起销毁，那正是要避免的崩溃面。
                try:
                    j.setParent(None)
                except (RuntimeError, AttributeError):
                    pass
                try:
                    j.finished.connect(
                        lambda jj=j: (_ORPHAN_JOBS.remove(jj)
                                      if jj in _ORPHAN_JOBS else None))
                except (RuntimeError, TypeError):
                    pass
                _ORPHAN_JOBS.append(j)
                self._fig_job = None
                return
        self._fig_job = None

    def _update_figs_progress(self) -> int:
        """刷新"已出 n 张 / 共 m 张"提示，并决定「定稿出图」是否可用。"""
        rr = self._last_rr
        if rr is None or not rr.run_dir:
            return 0
        have = self._preview_pngs(rr)
        try:
            from core.plotting_jacs import expected_figures
            exp = expected_figures(rr.scans, rr.results, rr.phi,
                                   rr.cfg or self.cfg, "preview")
        except Exception:                           # noqa: BLE001
            exp = []
        n, m = len(have), max(len(exp), len(have))
        pending = self.canvas.pending_count()
        busy = self._jobs_busy() or pending > 0
        # 还明确有图在排队就先别放开"定稿"：图集不全也能出定稿（定稿整批重画），
        # 但锁住一会儿能避免用户在补渲中途反复点、把两批 600 dpi 叠在一起
        self.btn_final.setEnabled(bool(rr.ok and rr.scans and rr.run_dir and not busy))
        self.btn_final.setToolTip(
            ("预览图件仍在后台渲染（%d/%d，排队 %d），完成后可点此出 600 dpi 投稿图件"
             % (n, m, pending)) if busy else
            "用刚才的数据直接重渲 600 dpi 的 png 到 figures/（不再重新读文件）\n"
            "产物与「直接全量跑」完全一致，是投稿用的那一份。")
        if m:
            self.status.emit(f"预览图件 {n}/{m}" + ("（后台渲染中…）" if busy else ""))
        return n

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
        self._update_figs_progress()

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
        """V2.9.6 M4：窄页三栏最小宽超页宽时，隐藏设置区/改为二栏，避免叠加。"""
        if not hasattr(self, "_body"):
            return
        # 三栏实际最小宽（含中栏按钮行）约 1000，留出余量：低于 1180 就改二栏 + 切换按钮
        narrow = self.width() < NARROW_PAGE_W
        try:
            if narrow:
                self._body.setChildrenCollapsible(True)
                self._left_tabs.setMinimumWidth(320)
                self._left_tabs.setMaximumWidth(340)
                self._right.setMinimumWidth(220)
                if self.btn_settings_toggle.isChecked():
                    self._left_tabs.setVisible(False)
                    self.settings_panel.setVisible(True)
                    self._body.setSizes([0, 372, 280])
                else:
                    self.settings_panel.setVisible(False)
                    self._left_tabs.setVisible(True)
                    self._body.setSizes([330, 352, 0])
            else:
                self._body.setChildrenCollapsible(False)
                self._left_tabs.setVisible(True)
                self.settings_panel.setVisible(True)
                self._left_tabs.setMinimumWidth(320)
                self._left_tabs.setMaximumWidth(380)
                self._right.setMinimumWidth(380)
                self.settings_panel.setMinimumWidth(260)
                self.settings_panel.setMaximumWidth(280)
                self._body.setSizes([330, 454, 270])
        except RuntimeError:
            pass

    def on_activated(self) -> None:
        """切到本页时，若已有目录但清单为空则重新载入。"""
        if self.data_dir and not self.scans:
            self._load_dir(self.data_dir)

    def shutdown(self) -> None:
        """退出前收尾：停掉主任务与后台补渲线程。"""
        self._cancel_fig_job()
        super().shutdown()


def orphan_render_jobs() -> list:
    """仍在跑但已脱离页面的补渲线程（A13；测试/审计用）。"""
    return list(_ORPHAN_JOBS)
