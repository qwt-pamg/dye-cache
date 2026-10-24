# -*- coding: utf-8 -*-
"""③ 图件页（作图台）：紫外吸收（叠图 / 堆叠时间演化）与荧光发射两组作图页。

合并只在界面层：渲染路径与导出命名沿用 :mod:`ui.pages.stack` / :mod:`ui.pages.spectra`。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QTabWidget, QVBoxLayout, QWidget

from ui.pages.spectra import SpectraPage
from ui.pages.stack import StackPage
from ui import theme

log = logging.getLogger("dye_tool.ui.pages")


class PlotPage(QWidget):
    """作图台：按**数据类型**分「紫外（吸收谱）」与「荧光（发射谱）」两页。

    V2.1 调整（用户实拍反馈）：原先把"堆叠/时间演化"与"紫外/荧光常规"按**页面功能**
    分两个页签，用户面对同一批数据要先想"我该去哪个功能页"。现在按**数据类型**分工：

    * 紫外（吸收）：归一化吸收谱叠图 + ε 计算器 + 堆叠/时间演化（ABDA 动力学）；
    * 荧光（发射）：发射谱归一化叠图 + 吸收-发射镜像图/上下两栏/自动配对。

    业务逻辑仍全部复用既有 ``StackPage`` / ``SpectraPage``（Q5：只在 UI 层重组，
    渲染路径与导出命名不动）；经典布局（``gui.nav=classic``）继续用原生两页。
    """

    logged = Signal(str, str)
    status = Signal(str)
    progress = Signal(int, str)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg or {}
        self._batch_dir: Optional[Path] = None
        self._batch_dirty: bool = False
        self._batch_overrides: dict = {}
        self._batch_recursive: Optional[bool] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        head = QLabel("作图台")
        theme.mark(head, "title")
        sub = QLabel("按数据类型分工：紫外（吸收谱）与荧光（发射谱）。"
                     "")
        sub.setWordWrap(True)
        theme.mark(sub, "muted")
        lay.addWidget(head)
        lay.addWidget(sub)

        # ── 紫外（吸收）──
        self.uv_overlay = SpectraPage(
            self.cfg, modes=("overlay",), overlay_kinds="abs", show_eps=True,
            title="紫外 · 归一化吸收谱叠图",
            subtitle="载入两列吸收谱（.txt/.csv/.spc）→ 同一坐标系归一化叠图；"
                     "可算 ε = A/(c·l)。时间演化见「堆叠 / 时间演化」子页签。")
        self.uv_stack = StackPage(self.cfg)
        uv_tabs = QTabWidget()
        uv_tabs.addTab(self.uv_overlay, "归一化叠图")
        uv_tabs.addTab(self.uv_stack, "堆叠 / 时间演化")
        self.uv_tabs = uv_tabs

        # ── 荧光（发射）──
        self.fluor = SpectraPage(
            self.cfg, modes=("overlay", "mirror", "stacked", "auto"),
            overlay_kinds="em", show_eps=False,
            title="荧光 · 发射谱作图",
            subtitle="发射谱（仪器 FL 文本 / .fds）归一化叠图；"
                     "镜像图/上下两栏/自动配对需同时载入吸收谱。")

        self.tabs = QTabWidget()
        self.tabs.addTab(uv_tabs, "紫外（吸收）")
        self.tabs.addTab(self.fluor, "荧光（发射）")
        for p in (self.uv_overlay, self.uv_stack, self.fluor):
            for sig in ("logged", "status", "progress"):
                getattr(p, sig).connect(getattr(self, sig))
        lay.addWidget(self.tabs, 1)

    def set_batch(self, scans: list, data_dir: Optional[Path] = None,
                  overrides: Optional[dict] = None,
                  recursive: Optional[bool] = None) -> None:
        """UX-3：记录当前批次（**懒加载**，M3）。

        M3（V2.x 审查）：原实现在数据页刚载入时**立刻**对作图台两个页面各跑一次
        `_load_dir` → 同一目录被解析 2 次以上（0.13 s / `.spc` 目录，196 个 `.fds`
        外推约 0.65 s），而用户此刻还在①数据页，看不到作图台。
        现在只记目录并标脏，等用户**真的切到③图件**（``on_activated``）再载入。
        """
        _ = scans
        self._batch_dir = Path(data_dir) if data_dir else None
        # A19（审查附录 C）：连同①数据页的扫描口径与整定一起记下，
        # 在 on_activated 首次解析时应用（M3 懒加载**不回归**：仍不预先解析）。
        self._batch_overrides = dict(overrides or {})
        self._batch_recursive = recursive
        self._batch_dirty = self._batch_dir is not None

    def on_activated(self) -> None:
        """切到③图件页时才把当前批次灌进紫外侧两页（M3 懒加载）。"""
        if not getattr(self, "_batch_dirty", False) or self._batch_dir is None:
            return
        d = self._batch_dir
        self._batch_dirty = False
        for page in (getattr(self, "uv_overlay", None),
                     getattr(self, "uv_stack", None)):
            if page is None:
                continue
            try:
                # A19：两页都继承 recursive；时间/染料整定只对"吸收时间序列"
                # 有意义（stack），荧光叠加页接受但忽略。
                page._load_dir(d, recursive=self._batch_recursive,
                               overrides=self._batch_overrides)
            except Exception as err:                       # noqa: BLE001
                log.warning("作图页接批次失败：%s", err)
                self._batch_dirty = True                   # 下次再试
