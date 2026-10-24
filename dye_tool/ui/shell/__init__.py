# -*- coding: utf-8 -*-
"""``ui.shell`` —— **V2.0 任务流导航壳**（FR-16 / ε 批）。

设计取舍（为什么要新壳而不是翻新旧页签）：

* 旧五页签按 **legacy 脚本** 组织（Φ_Δ / 对比 / 堆叠 / 光谱 / 染料库），新人得先学
  "Φ_Δ 是什么页"；新壳按 **用户要做的事** 组织（主页 / 数据 / 分析 / 图件 / 档案 / 设置）。
* **业务逻辑一行不重写**：每个壳页面内部都嵌入既有的功能页/组件
  （``PhiPage`` / ``ComparePage`` / ``StackPage`` / ``SpectraPage`` / ``DyeLibraryPage``），
  壳只负责"把它们串成一条任务流 + 共享当前批次"。
* 经典模式（``gui.nav=classic``）原样保留一个版本周期（Q1 默认决策）：
  同一批页面对象在两种模式下复用，只是容器不同。

模块：

* :mod:`ui.shell.nav` —— 左侧导航栏（带徽标）
* :mod:`ui.shell.home_page` —— 主页（欢迎 / 最近 / 一键演示 / **命名速查**，FR-19）
* :mod:`ui.shell.data_page` —— ① 数据（批次工作区：文件表 + 整定 + QC 体检 + 批次保存）
* :mod:`ui.shell.analysis_page` —— ② 分析（任务卡片：Φ_Δ / kobs / ε / A@λ）
* :mod:`ui.shell.plot_page` —— ③ 图件（堆叠/叠加 + 光谱常规作图合并）
* :mod:`ui.shell.archive_page` —— ④ 档案（染料库 + 最近结果直达）
* :mod:`ui.shell.settings_page` —— ⑤ 设置（参数 / 配置校验 / 目录整理 P9）
"""
from __future__ import annotations

__all__ = ["NAV_ITEMS"]

#: 导航项：``(key, 显示名, 一句话说明)``。顺序 = 用户做实验的顺序。
NAV_ITEMS: tuple = (
    ("home", "主页", "从这里开始：最近批次、一键演示、命名速查"),
    ("data", "① 数据", "批次工作区：导入 → 整定 → QC 体检"),
    ("analysis", "② 分析", "Φ_Δ / 动力学对比 / ε / A@λ"),
    ("plot", "③ 图件", "堆叠、叠加、荧光与紫外常规作图"),
    ("archive", "④ 档案", "染料库、历史 run、结果直达"),
    ("settings", "⑤ 设置", "参数、配置校验、目录整理"),
)
