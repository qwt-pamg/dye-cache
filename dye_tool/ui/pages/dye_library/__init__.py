# -*- coding: utf-8 -*-
"""④ 档案 · 染料库：按染料汇总历次 run 的档案（history.csv）、趋势与图件。

* :mod:`.page`    —— :class:`DyeLibraryPage`（三栏布局、扫描刷新、选中联动）；
* :mod:`.actions` —— 按钮背后的动作与拼版 / 统计两件后台活（混入页面类）；
* :mod:`.trend`   —— k / Φ_Δ 趋势小图 :class:`TrendCanvas`；
* :mod:`.model`   —— 表头常量与纯函数小工具（无 Qt 依赖）。
"""
from .page import DyeLibraryPage

__all__ = ["DyeLibraryPage"]
