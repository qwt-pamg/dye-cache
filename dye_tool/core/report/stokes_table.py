# -*- coding: utf-8 -*-
"""V2.6 FR-41 薄别名（Q-E19：Stokes 表已合并进 core.report.characterization）。

本文件**不再含任何实现**：所有类 / 常量 / 函数从 characterization 转发，保证
FR-41 与 FR-44 只有一套单源（列名 / 格式化 / CSV 行为一致）。保留本模块是为了
让 F 道先行的调用点（ui/pages/analysis.py 等）与 A41 测试零改动。
"""
from __future__ import annotations

from core.report.characterization import (  # noqa: F401 —— 纯转发
    STOKES_COLUMNS,
    TIME_FMT,
    CharacterizationRow,
    StokesRow,
    default_stokes_name,
    default_stokes_path,
    delta_wavenumber,
    fmt_nm,
    fmt_wavenumber,
    group_from_path,
    make_stokes_row,
    row_from_spectra,
    write_stokes_csv,
)

__all__ = [
    "STOKES_COLUMNS", "TIME_FMT", "CharacterizationRow", "StokesRow",
    "default_stokes_name", "default_stokes_path", "delta_wavenumber",
    "fmt_nm", "fmt_wavenumber", "group_from_path", "make_stokes_row",
    "row_from_spectra", "write_stokes_csv",
]
