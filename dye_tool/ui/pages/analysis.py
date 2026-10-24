"""② 分析页：顶部细条（任务选择 + 当前任务自己的工具条）+ 下方嵌入的任务页。

任务页（Φ_Δ、对比与 :mod:`ui.pages.tasks` 各卡）可以把自己的流程条/工具条交给
顶部细条显示，约定见 :meth:`AnalysisPage._mount_top_widget`。
"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import QPoint
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
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

__all__ = ["TASKS", "AnalysisPage", "TaskPicker"]

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


#: 顶部细条高度（任务页交上来的工具条要能放进这个高度）
TOP_BAR_H = 44


@theme.register_qss
def _analysis_qss(p: dict[str, str]) -> str:
    return f"""
/* ── ② 分析页顶部细条（ui/pages/analysis.py） ── */
QFrame#analysisTopBar {{ background: {p['surface']}; border: none;
                        border-bottom: 1px solid {p['border']}; }}
QFrame#topBarSep {{ background: {p['border']}; border: none; }}
QPushButton#taskSelector {{ background: {p['surface_alt']}; color: {p['text']};
                           font-weight: bold; padding: 4px 10px; border-radius: 6px; }}
QPushButton#taskSelector:hover {{ border-color: {p['accent']}; }}
QFrame#taskPicker {{ background: {p['surface']}; border: 1px solid {p['border_strong']}; }}
QListWidget#taskPickerList {{ background: transparent; border: none; outline: none; }}
QListWidget#taskPickerList::item {{ border-radius: 6px; margin: 1px 0; }}
QListWidget#taskPickerList::item:hover {{ background: {p['surface_alt']}; }}
QListWidget#taskPickerList::item:selected {{ background: {p['accent_soft']}; }}
"""


class TaskPicker(QFrame):
    """任务选择弹层：每项"名称（粗体）+ 需要：…（灰字）"，tooltip 是长说明。

    ↑/↓ 移动、回车或单击选中、Esc 关闭；选中发 ``picked(key)``。
    """

    picked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("taskPicker")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.list = QListWidget()
        self.list.setObjectName("taskPickerList")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for key, name, desc, needs in TASKS:
            it = QListWidgetItem()
            it.setData(Qt.UserRole, key)
            it.setToolTip(desc)
            row = QWidget()
            row.setToolTip(desc)
            rv = QVBoxLayout(row)
            rv.setContentsMargins(10, 6, 10, 6)
            rv.setSpacing(1)
            rv.addWidget(theme.mark(QLabel(name), "strong"))
            rv.addWidget(theme.mark(QLabel(f"需要：{needs}"), "muted"))
            it.setSizeHint(row.sizeHint())
            self.list.addItem(it)
            self.list.setItemWidget(it, row)
        self.list.itemClicked.connect(self._on_item)
        self.list.itemActivated.connect(self._on_item)
        lay.addWidget(self.list)

    def count(self) -> int:
        return self.list.count()

    def keys(self) -> list[str]:
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def set_current(self, key: str) -> None:
        keys = self.keys()
        if key in keys:
            self.list.setCurrentRow(keys.index(key))

    def popup_under(self, anchor: QWidget) -> None:
        """贴在 ``anchor`` 下方弹出，高度刚好放下全部任务。"""
        rows = sum(self.list.sizeHintForRow(i) + 2 for i in range(self.list.count()))
        m = self.layout().contentsMargins()
        self.list.setFixedHeight(rows + 4)
        self.resize(max(360, anchor.width()), rows + 4 + m.top() + m.bottom())
        self.move(anchor.mapToGlobal(QPoint(0, anchor.height() + 4)))
        self.show()
        self.list.setFocus(Qt.PopupFocusReason)

    def _on_item(self, item: QListWidgetItem) -> None:
        self.hide()
        self.picked.emit(str(item.data(Qt.UserRole)))


class AnalysisPage(QWidget):
    """顶部细条（任务选择 + 任务工具条）+ 下方嵌入页。信号 ``page_run_finished(object)`` 冒泡给壳。"""

    page_run_finished = Signal(object)
    #: V2.6 FR-40.5：「下一步」→ ③ 图件 / 定稿出图（由主窗口接线跳页）
    next_step_requested = Signal(str)
    #: V2.6 FR-40.4：空态「去数据页载入数据…」（由主窗口切页并聚焦入口）
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
        lay.setSpacing(0)

        # ── 顶部细条：分析 [任务 ▾] | 当前任务自己的工具条 ──
        bar = QFrame()
        bar.setObjectName("analysisTopBar")
        bar.setFixedHeight(TOP_BAR_H)
        self.top_bar = bar
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 0, 10, 0)
        hb.setSpacing(8)
        hb.addWidget(theme.mark(QLabel("分析"), "muted"))
        self.btn_task = QPushButton()
        self.btn_task.setObjectName("taskSelector")
        self.btn_task.setCursor(Qt.PointingHandCursor)
        self.task_picker = TaskPicker(self)
        self.task_picker.picked.connect(self.select_task)
        self.btn_task.clicked.connect(lambda: self.task_picker.popup_under(self.btn_task))
        hb.addWidget(self.btn_task)
        self.top_sep = QFrame()
        self.top_sep.setObjectName("topBarSep")
        self.top_sep.setFixedSize(1, 24)
        self.top_sep.setVisible(False)
        hb.addWidget(self.top_sep)
        # 任务页工具条的插槽：吃满剩余宽度
        self.top_slot = QWidget()
        self._slot_lay = QHBoxLayout(self.top_slot)
        self._slot_lay.setContentsMargins(0, 0, 0, 0)
        self._slot_lay.setSpacing(0)
        hb.addWidget(self.top_slot, 1)
        # V2.6 FR-40.4：空态可点击引导 → 数据页载入数据（并聚焦入口）
        self.btn_empty_data = QPushButton("去数据页载入数据…")
        self.btn_empty_data.setToolTip(
            "当前还没有批次数据：切到 数据 页选择/拖入数据目录")
        self.btn_empty_data.clicked.connect(self.request_data_page.emit)
        hb.addWidget(self.btn_empty_data)
        lay.addWidget(bar)

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
        lay.addWidget(self.stack, 1)

        self._current = ""
        self._top_widget: Optional[QWidget] = None
        self._top_owner: Optional[QWidget] = None
        self.select_task("phi")             # 默认 Φ_Δ —— 最常用的任务
        # V2.3 FR-24.1 / V2.7 A 道：Ctrl+1..8 动态绑定（按 TASKS 顺序）
        from PySide6.QtGui import QKeySequence, QShortcut
        for _i, (_key, *_rest) in enumerate(TASKS):
            QShortcut(QKeySequence(f"Ctrl+{_i + 1}"), self,
                      activated=lambda k=_key: self.select_task(k))

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
        return self._current

    def select_task(self, key: str) -> bool:
        """切到某个任务（幂等）；key 不在 TASKS 里返回 False。"""
        names = {k: name for k, name, *_rest in TASKS}
        if key not in names:
            return False
        if key == self._current:
            return True
        self._current = key
        panel = self._pages[key]
        self.stack.setCurrentWidget(panel)
        self._mount_top_widget(panel)
        desc = {k: d for k, _n, d, _r in TASKS}[key]
        self.btn_task.setText(f"{names[key]}  ▾")
        self.btn_task.setToolTip(
            f"切换分析任务（Ctrl+1..{len(TASKS)}）\n当前：{names[key]}——{desc}")
        self.task_picker.set_current(key)
        self.status.emit(f"任务：{names[key]}")
        return True

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
        # V2.6 FR-40.4：有批次后隐藏空态引导（批次摘要在主窗口状态栏）
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
    def _mount_top_widget(self, panel: QWidget) -> None:
        """把 ``panel.top_bar_widget()`` 交上来的控件挂进顶部细条的插槽。

        约定：任务页可实现 ``top_bar_widget(self) -> Optional[QWidget]``，返回自己的
        流程条/工具条（建议每次返回同一个对象，高度不超过 TOP_BAR_H）。切走时
        这里只摘下、不删除：``hide()`` 后 ``setParent(原任务页)`` 还给它；没有该方法
        或返回 None 的任务页，插槽留空。
        """
        old, owner = self._top_widget, self._top_owner
        if old is not None:
            self._slot_lay.removeWidget(old)
            old.hide()
            old.setParent(owner)
        self._top_widget = self._top_owner = None
        hook = getattr(panel, "top_bar_widget", None)
        w = None
        if callable(hook):
            try:
                w = hook()
            except Exception as err:                   # noqa: BLE001 —— 工具条出错不挡切页
                log.warning("任务页顶部工具条构建失败：%s", err)
        if w is not None:
            self._slot_lay.addWidget(w, 1)
            w.show()
            self._top_widget, self._top_owner = w, panel
        self.top_sep.setVisible(w is not None)
