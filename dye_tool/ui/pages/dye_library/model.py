# -*- coding: utf-8 -*-
"""染料库的数据侧：表头常量与纯函数小工具（无 Qt 依赖，便于复用/测试）。

数值缺省一律显示 :data:`DASH`（**不是** 0，避免误读为"结果为零"）；解析不出的日期、
数字都返回 ``None``，由界面决定怎么降级。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from core.dyes import HISTORY_COLUMNS

#: 左侧染料表列名（FR-11.6）
DYE_HEADERS = ["染料名", "run 数", "最近日期", "最近 k", "最近 Φ"]
#: 历史表里 ``dir`` 列的索引（拼版导出据此定位 run 目录）
try:
    HIST_COL_DIR = list(HISTORY_COLUMNS).index("dir")
except ValueError:                                      # pragma: no cover
    HIST_COL_DIR = 1

#: 数值缺省时显示的占位符（**不是** 0，避免误读为"结果为零"）
DASH = "—"


def to_float(value: object) -> Optional[float]:
    """任意值 → float；``None`` / 空串 / 非数 / NaN / inf 一律返回 None。"""
    if value is None:
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def fmt_num(value: object, digits: int = 4) -> str:
    """数值 → 定长字符串；解析不出数字时返回 ``DASH``（界面占位）。"""
    f = to_float(value)
    return DASH if f is None else f"{f:.{digits}g}"


def run_date_text(run_id: object) -> str:
    """``20261014-222104`` → ``2026-10-14``；解析不出时原样返回。"""
    s = str(run_id or "").strip()
    if not s:
        return DASH
    head = s.split("-", 1)[0]
    if len(head) == 8 and head.isdigit():
        return f"{head[0:4]}-{head[4:6]}-{head[6:8]}"
    return s


def run_dt(run_id: object) -> Optional[datetime]:
    """``run_id`` → :class:`datetime`（趋势图横轴用）；解析失败返回 None。

    同时兼容 ``20261014-222104`` 与 ``20261014`` 两种写法。
    """
    s = str(run_id or "").strip()
    for fmt in ("%Y%m%d-%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def looks_like_run(p: Path) -> bool:
    """``p`` 是否真的是一次 run 的目录（而非染料目录本身）。

    判据与 :func:`core.dyes.list_dyes` 统计 run 数时一致：
    含 ``results.csv`` 或 ``figures/``。染料目录（``runs/<染料>/``）不含这两样，
    因此这条判据能把"染料目录"从候选中剔掉，避免把参数快照读到错误层级。
    """
    try:
        return p.is_dir() and ((p / "results.csv").exists()
                               or (p / "figures").is_dir())
    except OSError:
        return False


def trend_points(rows: list[dict]) -> list[dict]:
    """历史行 → 趋势点（按时间升序，只保留能解析出日期的行）。

    每个点形如 ``{"when": datetime, "k": float|None, "k_se": float|None,
    "phi": float|None, "phi_se": float|None, "run_id": str}``；
    连 k 都读不出来的行直接跳过（不画空洞）。
    """
    pts: list[dict] = []
    for r in rows or []:
        when = run_dt(r.get("run_id"))
        if when is None:
            continue
        k = to_float(r.get("k"))
        phi = to_float(r.get("Phi"))
        if k is None and phi is None:
            continue
        pts.append({"when": when, "k": k, "k_se": to_float(r.get("k_se")),
                    "phi": phi, "phi_se": to_float(r.get("Phi_se")),
                    "run_id": str(r.get("run_id", ""))})
    pts.sort(key=lambda p: p["when"])
    return pts


def latest_run_dir(entry: Optional[dict]) -> Optional[Path]:
    """最近一次 run 的目录；找不到真正的 run 目录时返回 ``None``。

    规则（对应 FR-11.6）：

    1. 候选 = ``scan_runs()`` 的 ``dirs`` + ``dir``（**都是染料目录**，
       如 ``runs/<染料>/``、``runs/<功能>/<染料>/``），再展开它们**下面一层**
       的 run 子目录 —— ``run_params.yaml`` 就在那一层；
    2. 只保留 :func:`looks_like_run` 为真的候选，染料目录本身因此被自动排除；
    3. 目录名等于 ``last_run``（或 ``run_<last_run>``，V1.0 扁平命名）优先；
    4. 否则取目录名最大的那个（目录名即时间戳，字符串序 == 时间序）。

    读不到任何 run 目录时返回 ``None``（调用方据此置灰按钮，不弹错）。
    """
    e = entry
    if not e:
        return None
    bases = [Path(p) for p in (e.get("dirs") or []) if p]
    if e.get("dir"):
        bases.append(Path(e["dir"]))
    cands: list[Path] = []
    for base in bases:
        cands.append(base)
        try:                                        # 展开染料目录下的 run 子目录
            cands.extend(c for c in base.iterdir() if c.is_dir())
        except OSError:
            continue
    runs = [p for p in cands if looks_like_run(p)]
    if not runs:
        return None
    last = str(e.get("last_run") or "")
    if last:
        for p in runs:
            if p.name in (last, f"run_{last}"):
                return p
    return sorted(runs, key=lambda p: p.name)[-1]


def figures_of(run_dir: Optional[Path]) -> tuple[list[Path], str]:
    """某 run 的图件清单（定稿 ``figures/`` 优先，退回 ``figures_preview/``）。"""
    if run_dir is None:
        return [], ""
    for sub, label in (("figures", "定稿 600 dpi"), ("figures_preview", "预览档")):
        d = run_dir / sub
        if d.is_dir():
            pngs = sorted(d.glob("*.png"))
            if pngs:
                return pngs, f"{label}（{d}）"
    return [], ""
