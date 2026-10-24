# -*- coding: utf-8 -*-
"""V2.7 A 道（V2.6 D 道结转）：FR-37 峰分析（纯函数 + CSV 写出）。

判据默认值（V2.6 §10 Q-E5，用户 2026-10-20 批准）：
* prominence_rel=0.05：峰突出度 ≥ 动态范围的 5%；
* height_rel=0.02：峰高（相对谱最小值）≥ 动态范围的 2%；
* min_distance_pts=5：相邻峰至少间隔 5 个采样点；
* max_peaks=20：单谱最多输出 20 个峰（按突出度降序截断，输出按波长升序）；
* NaN 处理：分段（断点两侧独立检测，不跨段合并/插值）；
* 范围：全谱，不预设波长窗。

与既有 λmax 取点不重复实现；本模块只依赖 numpy/scipy，不进流水线
（results.csv/fit_summary/fig_* 结构性不变，见 FR-37.4/A43.6）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
from scipy.signal import find_peaks, peak_widths

#: Q-E5 默认判据（唯一键位真相；GUI/测试/验收都引用这里，不各写一份）
DEFAULT_PROMINENCE_REL = 0.05
DEFAULT_HEIGHT_REL = 0.02
DEFAULT_MIN_DISTANCE_PTS = 5
DEFAULT_MAX_PEAKS = 20

#: CSV 列名（UTF-8-SIG；中文表头与既有人工表一致）
CSV_HEADERS = ("文件", "峰序号", "波长_nm", "强度", "突出度_rel", "宽_pts", "宽_nm")


@dataclass(frozen=True)
class Peak:
    """单谱单峰（FR-37.1 字段）。"""

    wl_nm: float
    intensity: float
    prominence_rel: float
    width_pts: int
    width_nm: float


def _require_int(name: str, value) -> int:
    """整数参数严格校验（V27-O2）：整数值 float 接受，非整数/布尔拒绝（不静默截断）。"""
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须为整数（收到布尔 {value!r}）")
    if isinstance(value, (float, np.floating)):
        if not np.isfinite(value) or not float(value).is_integer():
            raise ValueError(f"{name} 必须为整数（收到 {value!r}）")
        return int(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    raise ValueError(f"{name} 必须为整数（收到 {type(value).__name__}）")


def _validate(prominence_rel: float, height_rel: float,
              min_distance_pts: int, max_peaks: int) -> None:
    """参数边界（A43.4 / V27-O2）：prominence>0、height>0、整数参数严格整数。"""
    try:
        pr = float(prominence_rel)
        hr = float(height_rel)
    except (TypeError, ValueError) as err:
        raise ValueError(f"参数不是数值：{err}") from err
    if not np.isfinite(pr) or pr <= 0:
        raise ValueError(f"prominence_rel 必须 > 0（收到 {prominence_rel!r}）")
    if not np.isfinite(hr) or hr <= 0:
        raise ValueError(f"height_rel 必须 > 0（收到 {height_rel!r}）")
    dist = _require_int("min_distance_pts", min_distance_pts)
    kmax = _require_int("max_peaks", max_peaks)
    if dist < 1:
        raise ValueError(f"min_distance_pts 必须 ≥ 1（收到 {min_distance_pts!r}）")
    if kmax < 1:
        raise ValueError(f"max_peaks 必须 ≥ 1（收到 {max_peaks!r}）")


def _finite_segments(mask) -> list:
    """连续 True 段 [(start, end_exclusive), ...]。"""
    segs = []
    start = None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            segs.append((start, i))
            start = None
    if start is not None:
        segs.append((start, len(mask)))
    return segs


def find_peaks_spectrum(wl, y, *, prominence_rel: float = DEFAULT_PROMINENCE_REL,
                        min_distance_pts: int = DEFAULT_MIN_DISTANCE_PTS,
                        height_rel: float = DEFAULT_HEIGHT_REL,
                        max_peaks: int = DEFAULT_MAX_PEAKS) -> list:
    """在一条光谱上找峰（V2.6 FR-37.1）。

    :param wl: 波长数组（nm）；与 y 等长。
    :param y: 强度/吸光度数组；NaN/非有限值视为断点（分段独立检测）。
    :returns: list[Peak]，按波长升序；最多 max_peaks 个（按突出度降序截断）。
    :raises ValueError: 参数越界 / 数组长度不一致（中文原因，供 GUI 提示）。
    """
    _validate(prominence_rel, height_rel, min_distance_pts, max_peaks)
    a_wl = np.asarray(wl, dtype=float).ravel()
    a_y = np.asarray(y, dtype=float).ravel()
    if a_wl.size != a_y.size:
        raise ValueError(f"波长与强度长度不一致：{a_wl.size} vs {a_y.size}")
    if a_wl.size == 0:
        return []

    finite = np.isfinite(a_y)
    if not finite.any():
        return []
    y_min = float(a_y[finite].min())
    y_max = float(a_y[finite].max())
    dyn = y_max - y_min
    if not np.isfinite(dyn) or dyn <= 0:
        return []

    abs_height = y_min + float(height_rel) * dyn   # 峰高相对谱最小值 ≥ height_rel·动态范围
    peaks: list = []
    for start, end in _finite_segments(finite):
        seg = a_y[start:end]
        if seg.size < 3:
            continue
        idx, props = find_peaks(
            seg, prominence=float(prominence_rel) * dyn,
            height=abs_height, distance=int(min_distance_pts))
        if idx.size == 0:
            continue
        widths, _w_heights, _left, _right = peak_widths(seg, idx, rel_height=0.5)
        wl_seg = a_wl[start:end]
        diffs = np.diff(wl_seg)
        diffs = diffs[np.isfinite(diffs) & (diffs != 0)]
        step = float(np.median(np.abs(diffs))) if diffs.size else 0.0
        for k, i in enumerate(idx):
            w_pts = int(round(float(widths[k])))
            peaks.append(Peak(
                wl_nm=float(wl_seg[int(i)]),
                intensity=float(seg[int(i)]),
                prominence_rel=float(props["prominences"][k]) / dyn,
                width_pts=max(1, w_pts),
                width_nm=float(max(1, w_pts) * step),
            ))

    # max_peaks：按突出度降序截断（A43.4）；最终输出按波长升序，便于人工核对
    peaks.sort(key=lambda p: p.prominence_rel, reverse=True)
    peaks = peaks[:int(max_peaks)]
    peaks.sort(key=lambda p: p.wl_nm)
    return peaks


def _unique_path(path: Path) -> Path:
    """同名不覆盖：已存在则追加 -2 / -3 …（与既有导出风格一致）。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = path.with_name(f"{stem}-{i}{suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"无法为 {path} 找到不覆盖的文件名")


def write_peaks_csv(rows: Iterable, path) -> Optional[Path]:
    """写峰表 CSV（UTF-8-SIG、原子写、同名不覆盖、空表不产文件）。

    rows：可迭代的 7 列序列（文件, 峰序号, 波长_nm, 强度, 突出度_rel, 宽_pts, 宽_nm）
    或含同名列的 dict。返回真正写出的 Path；空表返回 None（A43.3/A43.5）。
    """
    from ..report.results import atomic_write

    data = list(rows)
    if not data:
        return None
    path = _unique_path(Path(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    import csv
    with atomic_write(path, newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(list(CSV_HEADERS))
        for row in data:
            if isinstance(row, dict):
                w.writerow([row.get(h, "") for h in CSV_HEADERS])
            else:
                w.writerow(list(row))
    return path


def peak_rows(file_name: str, peaks: list) -> list:
    """把 list[Peak] 展成 CSV 行（峰序号从 1 起）。"""
    return [(file_name, i + 1, f"{p.wl_nm:.4f}", f"{p.intensity:.6g}",
             f"{p.prominence_rel:.6f}", f"{p.width_pts}", f"{p.width_nm:.4f}")
            for i, p in enumerate(peaks)]
