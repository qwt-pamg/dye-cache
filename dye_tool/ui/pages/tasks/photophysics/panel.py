# -*- coding: utf-8 -*-
"""② 分析页第 7 卡「光物理表征」：量子产率 φ → 光漂白 → 表征总表 三步的外壳。

每一步各自一个模块（:mod:`.quantum_yield`、:mod:`.photobleach`、:mod:`.char_table`），
每步对象本身是中栏内容，另带检查器页 ``settings`` 与主按钮 ``primary_actions``。本卡只负责：
流程条 / 结论条 / 三栏骨架、后台任务调度（各步经 ``self.panel.run_task`` 调用）、
外部数据源注入（既有 ε 结果 / Stokes 行），以及把各步结果汇入表征总表。

全部 **on-demand**：不进流水线、不写 cfg；不改 Φ_Δ/ε/动力学算法。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QStackedWidget, QVBoxLayout, QWidget

from ui.pages.base import PageBase
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.verdict import VerdictCard

from .char_table import CharacterizationTab
from .photobleach import PhotobleachTab
from .quantum_yield import QuantumYieldTab

#: 流程条三步；中栏、检查器页、主按钮都按这个顺序
STEPS = ("量子产率 φ", "光漂白", "表征总表")
STEP_HINTS = (
    "选光谱目录，在中间各选一支吸收谱和发射谱；参数一改 φ 就重算",
    "载入时间序列谱目录或两列 CSV，按单指数拟合出 k 与 t½",
    "前两步「加入表征表」的行都在这里，可编辑、筛选后导出",
)
STEP_PHI, STEP_BLEACH, STEP_CHAR = range(3)


def _scrolled(w: QWidget) -> QScrollArea:
    """检查器页放进竖向滚动区（窄栏里表单较长）。"""
    sc = QScrollArea()
    sc.setWidget(w)
    sc.setWidgetResizable(True)
    sc.setFrameShape(QFrame.NoFrame)
    sc.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    return sc


class PhotophysicsPanel(PageBase):
    """光物理表征：量子产率 φ / 光漂白 / 表征总表（流程条 + 结论条 + 分步检查器）。"""

    TITLE = "光物理表征（φ / 光漂白 / 表征总表）"
    SUBTITLE = ("论文数据链：相对法荧光量子产率 φ + 亮度；体外/成像光漂白单指数；"
                "多条件表征总表（与 Stokes 表同一模块）。全部按需计算，不改动配置。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        #: 外部数据源（AnalysisPage 注入）：既有 ε 结果 / Stokes 行
        self._eps_provider = None
        self._stokes_provider = None
        self._build()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条（挂到分析页顶栏）/ 中栏结论条 + 当前步内容 · 右栏检查器。"""
        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        self.verdict = VerdictCard()
        self.inspector = StepInspector(STEPS, STEP_HINTS)

        self.tab_phi = QuantumYieldTab(self)
        self.tab_bleach = PhotobleachTab(self)
        self.tab_char = CharacterizationTab(self)
        self._tabs = (self.tab_phi, self.tab_bleach, self.tab_char)

        self.center_stack = QStackedWidget()
        for i, tab in enumerate(self._tabs):
            self.center_stack.addWidget(tab)
            self.inspector.set_page(i, _scrolled(tab.settings))
            self.inspector.add_action(tab.primary_actions, steps=i)   # 每步自己的主按钮
        self.inspector.step_requested.connect(self._go_step)
        self.inspector.set_summary_provider(self._step_summary)

        center = QWidget()
        lc = QVBoxLayout(center)
        lc.setContentsMargins(0, 0, 0, 0)
        lc.setSpacing(6)
        lc.addWidget(self.verdict)
        lc.addWidget(self.center_stack, 1)
        center.setMinimumWidth(380)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(three_columns(None, center, self.inspector), 1)
        lay.addWidget(self.progress_row())
        self._go_step(STEP_PHI)

    def top_bar_widget(self) -> QWidget:
        """分析页顶部细条插槽的约定：交出流程条（同一对象，切走时会还回来）。"""
        return self.flow_bar

    # ══════════════ 流程条 / 结论条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：流程条、中栏、检查器页与主按钮一起换，结论条换成该步的数。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        self.flow_bar.set_current(idx)
        self.center_stack.setCurrentIndex(idx)
        self.inspector.set_current(idx)
        self.refresh()

    def current_step(self) -> int:
        return self.flow_bar.current()

    def show_char_table(self) -> None:
        """入表之后切到「表征总表」这一步。"""
        self._go_step(STEP_CHAR)

    def _step_summary(self, idx: int) -> str:
        return self._tabs[idx].summary()

    def refresh(self) -> None:
        """各步状态变了：重打流程条标记、摘要，结论条换成当前步的结论。"""
        done = (self.tab_phi.has_result(), self.tab_bleach.has_result(),
                bool(self.tab_char.rows()))
        for i, ok in enumerate(done):
            stale = i == STEP_BLEACH and self.tab_bleach.is_stale()
            self.flow_bar.set_state(i, STEP_STALE if stale else (STEP_DONE if ok else STEP_TODO))
        self.inspector.refresh_summaries()
        cur = self.flow_bar.current()
        if 0 <= cur < len(self._tabs):
            self._tabs[cur].fill_verdict(self.verdict)

    # ── 外部数据源 ──
    def set_eps_provider(self, provider) -> None:
        """注入「最近一次 ε 结果」回调（A42.2：亮度=ε×φ 的 ε 来源明示）。"""
        self._eps_provider = provider

    def set_stokes_provider(self, provider) -> None:
        """注入 Stokes 行回调（两表同一模块，可导入后统一导出）。"""
        self._stokes_provider = provider

    def set_default_dir(self, data_dir) -> None:
        """批次目录作为默认候选目录（不自动载入：本卡保持 on-demand）。"""
        if data_dir:
            self.data_dir = Path(data_dir)
            if not self.tab_phi._phi_spectra:
                self.tab_phi.lbl_phi_dir.setText(str(self.data_dir))
            if not self.tab_bleach._bleach_t:
                self.tab_bleach.lbl_bleach_dir.setText(str(self.data_dir))

    # ── 表征总表（各步结果汇入此处） ──
    def rows(self) -> list:
        return self.tab_char.rows()

    def add_row(self, row: dict) -> None:
        self.tab_char.add_row(row)
