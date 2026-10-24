# -*- coding: utf-8 -*-
"""V2.9 FR-60.3：时间列单位（s / min / h）的 UI 层换算工具。

内部唯一真相仍是 ``Scan.t``（秒）；本模块只做显示/输入换算，不参与数值分析。
"""
from __future__ import annotations

import re

#: 单位 → 秒倍率（与 core.analysis.photobleach 的既有口径同源：s=1/60、min=1、h=60）
UNIT_SECONDS = {"s": 1.0, "min": 60.0, "h": 3600.0}
UNIT_LABELS = ("s", "min", "h")

_TIME_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(s|sec|min|m|h|hr|秒|分钟|小时)?$",
                      re.IGNORECASE)


def normalize_unit(unit: str) -> str:
    """把 UI 单位归一成 s/min/h（未知回退 s）。"""
    u = str(unit or "s").strip().lower()
    return u if u in UNIT_SECONDS else "s"


def parse_time_input(text: str) -> "float | None":
    """解析时间输入 → 秒；非法/负值返回 None。

    无单位按**秒**（既有纯数字行为不变）；支持 ``5min`` / ``1.5h`` / ``5 分钟``。
    """
    s = str(text or "").strip().replace("，", "").replace(",", "")
    m = _TIME_RE.match(s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "s").lower()
    if unit in ("s", "sec", "秒"):
        mul = 1.0
    elif unit in ("min", "m", "分钟"):
        mul = 60.0
    elif unit in ("h", "hr", "小时"):
        mul = 3600.0
    else:
        return None
    sec = val * mul
    if sec < 0:
        return None
    return sec


def format_time_value(seconds: float, unit: str = "s") -> str:
    """秒 → 当前显示单位（去掉无意义的尾零；非数原样）。"""
    u = normalize_unit(unit)
    try:
        val = float(seconds) / UNIT_SECONDS[u]
    except (TypeError, ValueError):
        return str(seconds)
    return f"{val:g}"