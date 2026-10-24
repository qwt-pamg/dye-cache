"""② 分析页：左侧任务卡片列表 + 右侧嵌入的任务页（Φ_Δ、对比与 :mod:`ui.pages.tasks` 各卡）。"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtCore import QTimer
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QStackedWidget
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from ui.pages.compare import ComparePage
from ui.pages.phi import PhiPage
from ui.pages.tasks.extinction import ExtinctionPanel
from ui.pages.tasks.peaks import PeaksPanel
from ui.pages.tasks.photophysics import PhotophysicsPanel
from ui.pages.tasks.queue import BatchQueuePanel
from ui.pages.tasks.stokes import StokesPanel
from ui.pages.tasks.wavelength import WavelengthPanel
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

__all__ = ["TASKS", "AnalysisPage"]

#: 任务卡片：``(key, 名称, 一句话, 需要的输入)``
TASKS: tuple = (
    ("phi", "单线态氧（k_obs → Φ_Δ）", "ABDA 探针法：一次载入/分组/拟合，k_obs 中间结果 → 参比 → Φ_Δ 与 JACS 图件", "动力学时间序列"),
    ("extinction", "摩尔消光系数 ε", "由浓度序列求 ε（λmax 模式），并给出选择效应偏差提示", "浓度已知的光谱序列"),
    ("wavelength", "任意波长表 A@λ", "固定波长 / 插值 / 容差判据，导出 A@λ 表", "任意光谱"),
    ("queue", "批量队列", "多目录串行跑 Φ_Δ（失败继续）→ 队列汇总 xlsx", "多个数据目录"),
    #: V2.6 FR-41.1：第 6 卡；Ctrl+6 由下方动态 QShortcut 循环按 TASKS 顺序自动绑定。
    ("stokes", "斯托克斯位移", "一对一测量吸收/发射谱并做跨组对比表", "光谱目录"),
    #: V2.6 FR-42/43/44（G 道）：第 7 卡；Ctrl+7 同样动态绑定
    #: （D 道 FR-37 未批准，故光物理表征为第 7 卡，见设计文档 Q-E16）。
    ("photophysics", "光物理表征",
     "φ/亮度 · 光漂白 k/t½ · 表征总表（论文数据链）", "光谱目录 / 两列 CSV"),
    #: V2.7 A 道（V2.6 D 道结转，Q-E5 已批准）：第 8 卡；Ctrl+8 动态绑定
    ("peaks", "峰分析",
     "逐谱找峰（峰位/强度/prominence/宽度），导出峰表 CSV", "光谱目录"),
)

class AnalysisPage(QWidget):
    """任务卡片 + 右侧嵌入页。信号 ``page_run_finished(object)`` 冒泡给壳。"""

    page_run_finished = Signal(object)
    #: V2.6 FR-40.5：「下一步」→ ③ 图件 / 定稿出图（由主窗口接线跳页）
    next_step_requested = Signal(str)
    #: V2.6 FR-40.4：空态「去①数据页载入数据…」（由主窗口切页并聚焦入口）
    request_data_page = Signal()
    logged = Signal(str, str)
    status = Signal(str)
    progress = Signal(int, str)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None, *,
                 phi_page=None, compare_page=None):
        """``phi_page`` / ``compare_page`` 可由主窗口注入（**同一对象**，不重复构造）。"""
        super().__init__(parent)
        self.cfg = cfg or {}
        self.data_dir: Optional[Path] = None
        self._injected = {"phi": phi_page, "compare": compare_page}
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        self.split = split

        left = QWidget()
        self.left_panel = left
        lv = QVBoxLayout(left)
        lv.setContentsMargins(10, 10, 6, 10)
        title = QLabel("我要做什么？")
        theme.mark(title, "section")
        lv.addWidget(title)
        self.list = QListWidget()
        self.list.setObjectName("taskList")
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        # V2.6 FR-38.2：任务项压成 2 行（名称 + 需要），描述移 tooltip —— 窄栏不再挤成 4–6 行
        for key, name, desc, needs in TASKS:
            it = QListWidgetItem(f"{name}\n需要：{needs}")
            it.setData(Qt.UserRole, key)
            it.setToolTip(desc)
            self.list.addItem(it)
        self.list.setWordWrap(True)
        # 纵向滚动用 QListWidget 默认行为；列表吃满剩余高度，窗口变矮时不裁底
        lv.addWidget(self.list, 1)
        self.lbl_batch = QLabel("当前批次：未载入")
        self.lbl_batch.setWordWrap(True)
        theme.mark(self.lbl_batch, "hint")
        lv.addWidget(self.lbl_batch)
        # V2.6 FR-40.4：空态可点击引导 → ①数据页载入数据（并聚焦入口）
        self.btn_empty_data = QPushButton("去①数据页载入数据…")
        self.btn_empty_data.setToolTip(
            "当前还没有批次数据：切到 ① 数据页选择/拖入数据目录")
        self.btn_empty_data.clicked.connect(self.request_data_page.emit)
        self.btn_empty_data.setVisible(True)
        # 左栏 200–260：任务项是"名称 + 需要"两行，再宽只是把工作区挤窄
        left.setMinimumWidth(200)
        left.setMaximumWidth(260)
        split.addWidget(left)

        self.stack = QStackedWidget()
        self.phi_page = self._injected.get("phi") or PhiPage(self.cfg)
        self.compare_page = self._injected.get("compare") or ComparePage(self.cfg)
        self.extinction_panel = ExtinctionPanel(self.cfg)
        self.wavelength_panel = WavelengthPanel(self.cfg)
        self.queue_panel = BatchQueuePanel(self.cfg)
        # V2.6 FR-41.1：第 6 卡「斯托克斯位移」
        self.stokes_panel = StokesPanel(self.cfg)
        # V2.6 FR-42/43/44（G 道）：第 7 卡「光物理表征」（φ / 光漂白 / 总表）
        self.photophysics_panel = PhotophysicsPanel(self.cfg)
        # V2.7 A 道（V2.6 D 道结转）：第 8 卡「峰分析」（Q-E5 已批准）
        self.peaks_panel = PeaksPanel(self.cfg)
        # 数据源注入：亮度复用既有 ε 结果；总表可导入 FR-41 Stokes 行（Q-E19）
        self.photophysics_panel.set_eps_provider(
            lambda: dict(getattr(self.extinction_panel, "last_result", {}) or {}))
        self.photophysics_panel.set_stokes_provider(
            lambda: list(self.stokes_panel.rows()))
        self._pages = {"phi": self.phi_page,
                       "extinction": self.extinction_panel,
                       "wavelength": self.wavelength_panel,
                       "queue": self.queue_panel,
                       "stokes": self.stokes_panel,
                       "photophysics": self.photophysics_panel,
                       "peaks": self.peaks_panel}
        for p in self._pages.values():
            self.stack.addWidget(p)
            for sig in ("logged", "status", "progress"):
                getattr(p, sig).connect(getattr(self, sig))
            p.run_finished.connect(self.page_run_finished)
            # V2.6 FR-40.5：各面板结果区「下一步」统一冒泡给主窗口跳页
            _next = getattr(p, "next_step_requested", None)
            if _next is not None:
                _next.connect(
                    lambda target="plot", s=self.next_step_requested:
                    s.emit(str(target)))
        # FR-29.4：队列与手动任务双向互斥
        self.queue_panel.busy_changed.connect(self._on_queue_busy)
        for p in (self.phi_page,
                  self.extinction_panel, self.wavelength_panel,
                  self.stokes_panel, self.photophysics_panel,
                  self.peaks_panel):
            p.run_finished.connect(lambda *_: self._refresh_queue_mutex())
            p.progress.connect(lambda *_: self._refresh_queue_mutex())
        split.addWidget(self.stack)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 1200])       # 左栏初值 220px
        lay.addWidget(split, 1)

        self.list.currentRowChanged.connect(self._on_task)
        self.list.setCurrentRow(0)          # 默认 Φ_Δ —— 最常用的任务
        self.list.item(0).setSelected(True)
        # V2.3 FR-24.1 / V2.7 A 道：Ctrl+1..8 动态绑定（按 TASKS 顺序）
        from PySide6.QtGui import QKeySequence, QShortcut
        for _i, (_key, *_rest) in enumerate(TASKS):
            QShortcut(QKeySequence(f"Ctrl+{_i + 1}"), self,
                      activated=lambda k=_key: self.select_task(k))

    # ── V2.6 FR-38.2：左栏初值（构造期页面宽度未知，首次显示后校正为 260px） ──
    def showEvent(self, event) -> None:                # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_layout_inited", False):
            QTimer.singleShot(0, self._init_layout_sizes)

    def resizeEvent(self, event) -> None:              # noqa: N802
        super().resizeEvent(event)
        if not getattr(self, "_layout_inited", False):
            QTimer.singleShot(0, self._init_layout_sizes)

    def _init_layout_sizes(self) -> None:
        """首次真正显示后把左栏设为 260px（QSplitter 按总宽度归一化，构造期 640px 做不到）。"""
        if getattr(self, "_layout_inited", False):
            return
        if not self.isVisible() or self.split.width() < 500:
            return                  # 未真正显示 / 尺寸未定 → 等下一次 show/resize
        self._layout_inited = True
        try:
            total = self.split.width()
            self.split.setSizes([220, max(1, total - 220)])
        except Exception:                              # noqa: BLE001 —— 布局未就绪时保持构造初值
            pass

    # ── 生命周期（V2.x 审查 S2：关窗漏 shutdown 这两个面板） ──
    def shutdown(self) -> None:
        """退出前取消本页**自有**面板的后台任务。

        为什么必须单独实现：``AnalysisPage`` 不是 ``PageBase`` 子类（它是壳页），
        `main_window.closeEvent` 原来只遍历 ``self.pages`` 与 data/settings——
        而 ``ExtinctionPanel``/``WavelengthPanel`` 是**本页自己创建**的、不在
        ``self.pages`` 里。ε/A@λ 计算中关窗 → 父面板先析构、QThread 仍在跑 →
        ``QThread: Destroyed while running``（与 G14 同型的 0xC0000409 风险面，
        三路审计独立指认）。

        **只收自有面板**（extinction/wavelength）：phi/compare 是注入对象，
        已在 ``main_window.pages`` 里统一 shutdown（避免同一线程被等两次）。
        """
        for key in ("extinction", "wavelength", "queue", "photophysics", "peaks"):
            page = self._pages.get(key)
            hook = getattr(page, "shutdown", None)
            if callable(hook):
                try:
                    hook()
                except Exception as err:                # noqa: BLE001 —— 关窗不许抛
                    log.warning("关闭 %s 面板失败：%s", key, err)

    def shutdown_targets(self) -> list:
        """本页 shutdown 会覆盖的面板键（测试/审计用）。"""
        return ["extinction", "wavelength", "queue", "photophysics", "peaks"]

    # ── 对外 ──
    def current_key(self) -> str:
        row = self.list.currentRow()
        return TASKS[row][0] if 0 <= row < len(TASKS) else ""

    def select_task(self, key: str) -> bool:
        for i, (k, *_rest) in enumerate(TASKS):
            if k == key:
                self.list.setCurrentRow(i)
                return True
        return False

    def prefill_stokes_pair(self, abs_path, em_path) -> bool:
        """V2.6 FR-41.5 ③→②桥接：切到斯托克斯卡并预选一对（**不自动入表**）。

        由主窗口壳层把③作图台「在分析页记录」信号接到这里；预填后预览即时刷新，
        是否落一行由用户点「加入对比表」决定（Q-E16：不自动配对/入表）。
        """
        self.select_task("stokes")
        panel = self._pages.get("stokes")
        hook = getattr(panel, "prefill_pair", None)
        if not callable(hook):
            return False
        try:
            ok = bool(hook(abs_path, em_path))
        except Exception as err:                       # noqa: BLE001
            log.warning("斯托克斯卡预填失败：%s", err)
            ok = False
        if ok:
            self.status.emit("已预填斯托克斯位移配对（点「加入对比表」确认）")
        return ok

    def set_batch(self, scans: list, data_dir: Optional[Path] = None,
                  overrides: Optional[dict] = None) -> None:
        """UX-3：把数据页的当前批次灌给各任务页（换页不重选）。

        FR-24.9：``overrides`` = 数据页的整定（染料名 / 时间），随批次一起预置给
        Φ_Δ 页（单向同步；F1 的批次文件链路与之配套）。
        """
        self.data_dir = Path(data_dir) if data_dir else self.data_dir
        n = len(scans or [])
        good = sum(1 for s in (scans or []) if not s.excluded)
        self.lbl_batch.setText(
            f"当前批次：{self.data_dir or '未指定'}　|　{n} 个文件（有效 {good}）")
        # V2.6 FR-40.4：有批次后隐藏空态引导
        self.btn_empty_data.setVisible(n == 0)
        if self.data_dir is not None and n:
            # FR-24.9：先预置①数据页的整定，再让 Φ_Δ 页自己载入（载入完成时合并）
            try:
                self.phi_page.set_pending_overrides(overrides)
            except Exception as err:                       # noqa: BLE001
                log.warning("分析页整定同步失败：%s", err)
            # 各页自己载入（走自己的缓存与校验路径，不绕过既有逻辑）
            try:
                self.phi_page._load_dir(self.data_dir)
            except Exception as err:                       # noqa: BLE001
                log.warning("Φ_Δ 页接批次失败：%s", err)
            try:
                self.extinction_panel.set_data_dir(self.data_dir)
            except Exception as err:                       # noqa: BLE001
                log.warning("ε 面板接批次目录失败：%s", err)
            self.wavelength_panel.data_dir = self.data_dir
            # 斯托克斯卡不随批次自动重解析（避免与③重复读盘）；只记目录供默认对话框
            self.stokes_panel.data_dir = self.data_dir
            # 光物理表征卡同理：on-demand，只把批次目录作为默认候选
            try:
                self.photophysics_panel.set_default_dir(self.data_dir)
            except Exception as err:                   # noqa: BLE001
                log.warning("光物理表征卡接批次目录失败：%s", err)

    # ── FR-29.4：队列与手动任务互斥 ──
    def _refresh_queue_mutex(self) -> None:
        """手动任务忙 → 禁止「开始排队」（队列运行中由 _on_queue_busy 处理）。"""
        others = (self.phi_page,
                  self.extinction_panel, self.wavelength_panel,
                  self.stokes_panel, self.photophysics_panel,
                  self.peaks_panel)
        self.queue_panel.set_external_busy(
            any(bool(getattr(p, "_busy", False)) for p in others))

    def _on_queue_busy(self, busy: bool) -> None:
        """队列忙 → 禁用/恢复其它任务卡的运行按钮（FR-29.4 双向互斥）。"""
        for p in (self.phi_page,
                  self.extinction_panel, self.wavelength_panel,
                  self.stokes_panel, self.photophysics_panel,
                  self.peaks_panel):
            btn = getattr(p, "btn_run", None)
            if btn is not None:
                btn.setEnabled(not busy and not bool(getattr(p, "_busy", False)))
        self._refresh_queue_mutex()

    # ── 内部 ──
    def _on_task(self, row: int) -> None:
        if not (0 <= row < len(TASKS)):
            return
        key = TASKS[row][0]
        self.stack.setCurrentWidget(self._pages[key])
        self.status.emit(f"任务：{TASKS[row][1]}")
