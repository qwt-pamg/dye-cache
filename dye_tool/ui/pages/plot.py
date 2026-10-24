# -*- coding: utf-8 -*-
"""③ 图件页（作图台）：紫外吸收（叠图 / 堆叠时间演化）与荧光发射三个工作台。

合并只在界面层：渲染路径与导出命名沿用 :mod:`ui.pages.stack` / :mod:`ui.pages.spectra`。
页面顶部是一条细条：``图件 [吸收叠图 | 堆叠 · 时间演化 | 荧光发射] | 当前工作台的流程条``，
流程条由各工作台的 ``top_bar_widget()`` 交上来（同分析页的任务工具条插槽）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from ui.pages.spectra import SpectraPage
from ui.pages.stack import StackPage
from ui.pages.workbench import TOP_BAR_H
from ui import theme

log = logging.getLogger("dye_tool.ui.pages")

#: 三个工作台：属性名 → (切换按钮文字, 悬停说明)；顺序即按钮顺序
VIEWS = (
    ("uv_overlay", "吸收叠图", "紫外吸收谱归一化叠图，可算 ε = A/(c·l)"),
    ("uv_stack", "堆叠 · 时间演化", "每组一张 JACS 堆叠图 + 探针峰的时间演化叠加图"),
    ("fluor", "荧光发射", "发射谱叠图；镜像图 / 上下两栏 / 自动配对需同时载入吸收谱"),
)


@theme.register_qss
def _plot_qss(p: dict[str, str]) -> str:
    return f"""
/* ── ③ 图件页顶部细条（ui/pages/plot.py） ── */
QFrame#plotTopBar {{ background: {p['surface']}; border: none;
                    border-bottom: 1px solid {p['border']}; }}
QFrame#plotTopBarSep {{ background: {p['border']}; border: none; }}
QPushButton#plotViewSeg {{ background: {p['surface']}; color: {p['text_muted']};
                          border: 1px solid {p['border']}; border-radius: 0;
                          padding: 4px 12px; margin: 0; }}
QPushButton#plotViewSeg:hover {{ color: {p['accent']}; }}
QPushButton#plotViewSeg:checked {{ background: {p['accent_soft']}; color: {p['accent']};
                                  border-color: {p['accent']}; font-weight: bold; }}
QPushButton#plotViewSeg[segPos="first"] {{ border-top-left-radius: 6px;
                                          border-bottom-left-radius: 6px; }}
QPushButton#plotViewSeg[segPos="last"] {{ border-top-right-radius: 6px;
                                         border-bottom-right-radius: 6px; }}
"""


class PlotPage(QWidget):
    """作图台：按**数据类型**分三个工作台，顶部细条切换。

    V2.1 调整（用户实拍反馈）：按数据类型分工，而不是按页面功能——

    * 紫外（吸收）：归一化吸收谱叠图 + ε 计算器；堆叠/时间演化（ABDA 动力学）；
    * 荧光（发射）：发射谱归一化叠图 + 吸收-发射镜像图/上下两栏/自动配对。

    业务逻辑仍全部复用既有 ``StackPage`` / ``SpectraPage``（Q5：只在 UI 层重组，
    渲染路径与导出命名不动）；经典布局（``gui.nav=classic``）继续用原生两页。
    :attr:`tabs` 是三个工作台的叠放容器，``tabs.currentWidget()`` 即当前工作台。
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

        # ── 紫外（吸收）──
        self.uv_overlay = SpectraPage(
            self.cfg, modes=("overlay",), overlay_kinds="abs", show_eps=True,
            title="紫外 · 归一化吸收谱叠图",
            subtitle="载入两列吸收谱（.txt/.csv/.spc）→ 同一坐标系归一化叠图；"
                     "可算 ε = A/(c·l)。时间演化在「堆叠 · 时间演化」。")
        self.uv_stack = StackPage(self.cfg)
        # ── 荧光（发射）──
        self.fluor = SpectraPage(
            self.cfg, modes=("overlay", "mirror", "stacked", "auto"),
            overlay_kinds="em", show_eps=False,
            title="荧光 · 发射谱作图",
            subtitle="发射谱（仪器 FL 文本 / .fds）归一化叠图；"
                     "镜像图/上下两栏/自动配对需同时载入吸收谱。")
        self.views = {key: getattr(self, key) for key, _t, _d in VIEWS}
        for p in self.views.values():
            for sig in ("logged", "status", "progress"):
                getattr(p, sig).connect(getattr(self, sig))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self._build_top_bar())
        self.tabs = QStackedWidget()
        for p in self.views.values():
            self.tabs.addWidget(p)
        lay.addWidget(self.tabs, 1)
        self.select_view(VIEWS[0][0])

    def _build_top_bar(self) -> QFrame:
        """顶部细条：图件 [三段切换] | 当前工作台的流程条（各工作台交上来的同一对象）。"""
        bar = QFrame()
        bar.setObjectName("plotTopBar")
        bar.setFixedHeight(TOP_BAR_H)
        self.top_bar = bar
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 0, 10, 0)
        hb.setSpacing(8)
        hb.addWidget(theme.mark(QLabel("图件"), "muted"))
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_buttons: dict[str, QPushButton] = {}
        for i, (key, text, desc) in enumerate(VIEWS):
            b = QPushButton(text)
            b.setObjectName("plotViewSeg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(desc)
            b.setProperty("segPos", "first" if i == 0 else ("last" if i == len(VIEWS) - 1 else "mid"))
            b.clicked.connect(lambda _=False, k=key: self.select_view(k))
            self.view_group.addButton(b)
            self.view_buttons[key] = b
            seg.addWidget(b)
        hb.addLayout(seg)
        sep = QFrame()
        sep.setObjectName("plotTopBarSep")
        sep.setFixedSize(1, 24)
        hb.addWidget(sep)
        # 各工作台的流程条叠放在插槽里，跟着切换
        self.top_slot = QStackedWidget()
        for p in self.views.values():
            self.top_slot.addWidget(p.top_bar_widget())
        hb.addWidget(self.top_slot, 1)
        return bar

    # ── 切换工作台 ──
    def select_view(self, key: str) -> bool:
        """切到某个工作台（``uv_overlay`` / ``uv_stack`` / ``fluor``）；未知键返回 False。"""
        page = self.views.get(key)
        if page is None:
            return False
        self.tabs.setCurrentWidget(page)
        self.top_slot.setCurrentWidget(page.top_bar_widget())
        self.view_buttons[key].setChecked(True)
        return True

    def current_view(self) -> str:
        page = self.tabs.currentWidget()
        return next((k for k, p in self.views.items() if p is page), "")

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
