# -*- coding: utf-8 -*-
"""ui.pages —— 所有页面。

任务流（flow）导航的六个入口：:mod:`.home`、:mod:`.data`、:mod:`.analysis`、
:mod:`.plot`、:mod:`.archive`、:mod:`.settings`；它们内嵌的功能页
（:mod:`.phi`、:mod:`.compare`、:mod:`.stack`、:mod:`.spectra`、:mod:`.dye_library`）
在经典（classic）布局下直接作为页签使用。② 分析页的其余任务卡在 :mod:`.tasks`。
共同基类与后台任务管理见 :mod:`.base`。
"""
from __future__ import annotations
