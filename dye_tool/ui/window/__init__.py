# -*- coding: utf-8 -*-
"""ui.window —— 主窗口外壳：装配、菜单/状态栏、左侧导航与页面间接线。

* :mod:`.main_window` —— ``MainWindow``（flow 任务流 / classic 平铺页签两种布局）
* :mod:`.nav` —— 左侧导航栏与 ``NAV_ITEMS``
* :mod:`.actions` —— 菜单、状态栏、日志区、窗口几何、配置读写
* :mod:`.navigation` —— 切页、快捷键、页面之间的信号接线
* :mod:`.parts` —— 尺寸常量、快捷键清单、帮助文本
"""
from __future__ import annotations
