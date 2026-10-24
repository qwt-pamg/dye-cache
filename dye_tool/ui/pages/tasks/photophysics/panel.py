# -*- coding: utf-8 -*-
"""② 分析页第 7 卡「光物理表征」：三个子页的外壳。

子页各自一个模块（:mod:`.quantum_yield`、:mod:`.photobleach`、:mod:`.char_table`）；
本卡只负责：页头与进度条、后台任务调度（子页经 ``self.panel.run_task`` 调用）、
外部数据源注入（既有 ε 结果 / Stokes 行），以及把子页结果汇入表征总表。

全部 **on-demand**：不进流水线、不写 cfg；不改 Φ_Δ/ε/动力学算法。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from ui.pages.base import PageBase

from .char_table import CharacterizationTab
from .photobleach import PhotobleachTab
from .quantum_yield import QuantumYieldTab


class PhotophysicsPanel(PageBase):
    """光物理表征：量子产率 φ / 光漂白 / 表征总表。"""

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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        self.tabs = QTabWidget()
        self.tab_phi = QuantumYieldTab(self)
        self.tab_bleach = PhotobleachTab(self)
        self.tab_char = CharacterizationTab(self)
        self.tabs.addTab(self.tab_phi, "量子产率 φ")
        self.tabs.addTab(self.tab_bleach, "光漂白")
        self.tabs.addTab(self.tab_char, "表征总表")
        lay.addWidget(self.tabs, 1)
        lay.addWidget(self.progress_row())

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

    # ── 表征总表（子页结果汇入此处） ──
    def rows(self) -> list:
        return self.tab_char.rows()

    def add_row(self, row: dict) -> None:
        self.tab_char.add_row(row)
