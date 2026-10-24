# -*- coding: utf-8 -*-
"""V2.6 FR-43：光漂白动力学（单指数衰减拟合；两种输入模式）。

模型（论文 Figure 3/4 口径 / Q-E22）：

    I(t) = I∞ + (I0 − I∞) · exp(−k · t)      （默认，带平台项）
    I(t) = I0 · exp(−k · t)                  （plateau=False 退化）

输出：k（min⁻¹）、t½ = ln2/k（min）、末点损失率 %、拟合 R²；时间单位统一按
分钟，输入为 s/h 时自动换算并在结果里标注（unit_in / unit_used）。

输入模式：

1. 体外时间序列谱 → series_from_spectra（峰值吸光度 / 峰值强度 / 积分 vs t）；
2. 成像 ROI 两列 CSV（时间, 强度）→ read_two_column_csv；
   本工具不做图像分割，只消费外部软件导出的时间-强度表。

纯函数、不抛异常（read_two_column_csv 的文件读取错误按契约抛 ValueError 中文原因）；
不新增配置键；不改 Φ_Δ / ε / 动力学算法。
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("dye_tool.photobleach")

#: 时间单位 → 换算到分钟的倍数（键统一小写；别名覆盖常见写法）
TIME_FACTORS_TO_MIN = {
    "min": 1.0, "mins": 1.0, "minute": 1.0, "minutes": 1.0, "m": 1.0,
    "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "secs": 1.0 / 60.0,
    "second": 1.0 / 60.0, "seconds": 1.0 / 60.0,
    "h": 60.0, "hr": 60.0, "hrs": 60.0, "hour": 60.0, "hours": 60.0,
}

#: 有效数据点下限（3 参数模型 + 1 自由度；少于 4 点一律拒绝）
MIN_POINTS = 4

#: 判定"明显不是指数衰减"的 R² 下限（更低的拟合不接受为 ok）
R2_MIN_ACCEPT = 0.2


# ═══════════════════════════ 单位 / 输入 ═══════════════════════════
def unit_scale(unit: str) -> float:
    """时间单位 → 换算到分钟的倍数（s=1/60，h=60，min=1）。未知单位抛 ValueError。"""
    key = str(unit or "").strip().lower()
    if key not in TIME_FACTORS_TO_MIN:
        raise ValueError(f"未知时间单位：{unit!r}（支持 min/s/h 及其常见写法）")
    return float(TIME_FACTORS_TO_MIN[key])


def time_to_minutes(t, unit: str = "min") -> list:
    """把时间序列换算成分钟（返回 list[float]；非有限保持 NaN）。"""
    scale = unit_scale(unit)
    arr = np.asarray(t, float).ravel()
    return [(float(v) * scale) if math.isfinite(float(v)) else float("nan")
            for v in arr]


def _split_numbers(line: str) -> list:
    line = line.strip()
    if not line or line.startswith("#"):
        return []
    parts = None
    for sep in (",", ";", "\t"):
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            break
    if parts is None:
        parts = line.split()
    out = []
    for token in parts:
        if not token:
            continue
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


def read_two_column_csv(path) -> tuple:
    """读两列 CSV/TXT（时间, 强度）→ (t ndarray, y ndarray)。

    逗号 / 制表符 / 分号 / 空白分隔；自动跳过表头与非数值坏行；
    文件不存在 / 读取失败 / 有效点 < 2 → 抛 ValueError（中文原因）。
    """
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"文件不存在：{p}")
    try:
        raw = p.read_bytes()
    except OSError as err:
        raise ValueError(f"读取失败：{p}（{err}）") from err
    text = ""
    for encoding in ("utf-8-sig", "gbk", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        raise ValueError(f"文件无法解码为文本：{p.name}")
    ts: list = []
    ys: list = []
    for line in text.splitlines():
        nums = _split_numbers(line)
        if len(nums) < 2:
            continue
        t_val, y_val = float(nums[0]), float(nums[1])
        if math.isfinite(t_val) and math.isfinite(y_val):
            ts.append(t_val)
            ys.append(y_val)
    if len(ts) < 2:
        raise ValueError(f"有效数据点不足 2 个：{p.name}（需要两列：时间, 强度）")
    return np.asarray(ts, float), np.asarray(ys, float)


#: 别名（任务卡命名）
load_decay_csv = read_two_column_csv


def load_intensity_csv(path) -> tuple:
    """同 read_two_column_csv，但返回 Python list（UI 侧习惯）。"""
    t, y = read_two_column_csv(path)
    return [float(v) for v in t], [float(v) for v in y]


def _spec_wl_y(spec):
    if isinstance(spec, dict):
        return spec.get("wl"), spec.get("y", spec.get("a"))
    y = getattr(spec, "y", None)
    if y is None:
        y = getattr(spec, "a", None)
    return getattr(spec, "wl", None), y


def _spec_time(spec):
    if isinstance(spec, dict):
        t, path = spec.get("t"), spec.get("path")
    else:
        t, path = getattr(spec, "t", None), getattr(spec, "path", None)
    try:
        value = float(t) if t is not None else float("nan")
    except (TypeError, ValueError):
        value = float("nan")
    if math.isfinite(value):
        return value
    if not path:
        return float("nan")
    try:
        from core.inputs.io import parse_filename
        stem = Path(str(path)).stem
        return float(parse_filename(stem, {}).get("t", float("nan")))
    except Exception:                                  # noqa: BLE001
        return float("nan")


def series_from_spectra(scans_or_spectra, mode: str = "peak_abs") -> tuple:
    """时间序列谱 → (t, intensity)：峰值吸光度 / 峰值强度 / 积分 vs 时间。

    mode 收 peak_abs / peak / peak_int / peak_intensity / peak_em /
    peak_absorbance / max（峰 = 该谱 y 最大）与 integral / area（积分）；
    按 t 升序、重复 t 保留首个（并跳过 t 不可得/被剔除的项）。
    """
    key = str(mode or "").strip().lower()
    peak_modes = {"peak", "peak_abs", "peak_int", "peak_intensity",
                  "peak_em", "peak_absorbance", "peak_emission", "max"}
    if key in peak_modes:
        use_integral = False
    elif key in ("integral", "area", "sum"):
        use_integral = True
    else:
        raise ValueError(f"未知取点模式：{mode!r}（peak_abs / peak_int / integral）")
    pairs: list = []
    for spec in list(scans_or_spectra or []):
        try:
            if isinstance(spec, dict):
                excluded = spec.get("excluded", "")
            else:
                excluded = getattr(spec, "excluded", "")
            if excluded:
                continue
            t_val = _spec_time(spec)
            if not math.isfinite(t_val):
                continue
            wl_raw, y_raw = _spec_wl_y(spec)
            y = np.asarray(y_raw, float).ravel()
            if y.size == 0:
                continue
            mask = np.isfinite(y)
            if not mask.any():
                continue
            if use_integral:
                wl = np.asarray(wl_raw, float).ravel() if wl_raw is not None else None
                if wl is None or wl.size != y.size:
                    value = float("nan")
                else:
                    m2 = mask & np.isfinite(wl)
                    if int(m2.sum()) < 2:
                        value = float("nan")
                    else:
                        w, v = wl[m2], y[m2]
                        order = np.argsort(w, kind="stable")
                        fn = (getattr(np, "trapezoid", None)
                              or getattr(np, "trapz", None))
                        value = float(fn(v[order], w[order]))
            else:
                value = float(np.max(y[mask]))
            if not math.isfinite(value):
                continue
            pairs.append((t_val, value))
        except Exception as err:                       # noqa: BLE001
            log.warning("时间序列取点跳过一支谱：%s", err)
            continue
    if not pairs:
        return np.array([], float), np.array([], float)
    pairs.sort(key=lambda item: item[0])
    ts: list = []
    ys: list = []
    seen: set = set()
    for t_val, y_val in pairs:
        if t_val in seen:
            continue
        seen.add(t_val)
        ts.append(t_val)
        ys.append(y_val)
    return np.asarray(ts, float), np.asarray(ys, float)


#: 别名（UI 侧命名习惯）
from_spectra = series_from_spectra


# ═══════════════════════════ 拟合 ═══════════════════════════
def _model_plateau(t, i_inf, i0, k):
    return i_inf + (i0 - i_inf) * np.exp(-k * t)


def _model_plain(t, i0, k):
    return i0 * np.exp(-k * t)


def _skeleton(reason: str = "") -> dict:
    nan = float("nan")
    return {"ok": False, "k": nan, "k_min": nan, "t_half": nan,
            "t_half_min": nan, "loss_pct": nan, "loss_frac": nan,
            "r2": nan, "i0": nan, "i_inf": nan, "n_points": 0,
            "unit_in": "", "unit_used": "min", "issues": [],
            "reason": str(reason), "error": str(reason)}


def _fail(out: dict, reason: str) -> dict:
    out["ok"] = False
    out["reason"] = str(reason)
    out["error"] = str(reason)
    return out


def fit_photobleaching(t, intensity, *, plateau: bool = True,
                       t_unit: str = "min",
                       with_platform: Optional[bool] = None,
                       time_unit: Optional[str] = None) -> dict:
    """单指数衰减拟合（k/t½/loss%/R²）；with_platform/time_unit 是 plateau/t_unit 的别名。

    返回 dict：ok / k / k_min(min⁻¹) / t_half / t_half_min(min) / loss_pct(0–100) /
    loss_frac(0–1) / r2 / i0 / i_inf / n_points / unit_in / unit_used /
    issues(中文 list) / reason / error。失败时数值字段一律 NaN。
    """
    use_platform = bool(plateau if with_platform is None else with_platform)
    unit = str(time_unit if time_unit is not None else (t_unit or "min"))
    out = _skeleton()
    out["unit_in"] = unit
    out["unit_used"] = "min"
    try:
        scale = unit_scale(unit)
    except ValueError as err:
        return _fail(out, str(err))
    t_arr = np.asarray(t, float).ravel()
    y_arr = np.asarray(intensity, float).ravel()
    if t_arr.size != y_arr.size:
        return _fail(out, f"时间与强度长度不一致（{t_arr.size} vs {y_arr.size}）")
    mask = np.isfinite(t_arr) & np.isfinite(y_arr)
    ts, ys = t_arr[mask] * scale, y_arr[mask]
    if ts.size < MIN_POINTS:
        return _fail(out, f"有效数据点不足 {MIN_POINTS} 个（当前 {ts.size} 个），无法拟合")
    order = np.argsort(ts, kind="stable")
    ts, ys = ts[order], ys[order]
    uniq_t: list = []
    uniq_y: list = []
    i = 0
    while i < ts.size:
        j = i + 1
        while j < ts.size and ts[j] == ts[i]:
            j += 1
        uniq_t.append(float(ts[i]))
        uniq_y.append(float(np.mean(ys[i:j])))
        i = j
    ts = np.asarray(uniq_t, float)
    ys = np.asarray(uniq_y, float)
    out["n_points"] = int(ts.size)
    if ts.size < MIN_POINTS:
        return _fail(out, f"去重后有效时间点不足 {MIN_POINTS} 个（当前 {ts.size} 个）")
    span = float(ts[-1] - ts[0])
    if span <= 0:
        return _fail(out, "时间跨度为 0，无法拟合衰减速率")
    spread = float(np.ptp(ys))
    if spread <= 0 or spread < max(abs(float(np.mean(ys))), 1.0) * 1e-12:
        return _fail(out, "强度近似恒定，无法拟合衰减")
    if ys[-1] > ys[0]:
        return _fail(out, "数据整体呈上升趋势，不是衰减型曲线，无法做光漂白拟合")
    try:
        from scipy.optimize import curve_fit
    except ImportError as err:                         # pragma: no cover
        return _fail(out, f"缺少 scipy，无法拟合：{err}")
    try:
        if use_platform:
            i_inf0 = max(0.0, min(float(np.min(ys)), float(ys[-1])))
            i00 = max(float(ys[0]), float(np.max(ys)))
            popt, _pcov = curve_fit(
                _model_plateau, ts, ys, p0=(i_inf0, i00, 1.0 / span),
                bounds=([0.0, 0.0, 0.0], [np.inf, np.inf, np.inf]),
                maxfev=50000)
            i_inf, i0, k = float(popt[0]), float(popt[1]), float(popt[2])
        else:
            i00 = max(float(ys[0]), float(np.max(ys)))
            popt, _pcov = curve_fit(
                _model_plain, ts, ys, p0=(i00, 1.0 / span),
                bounds=([0.0, 0.0], [np.inf, np.inf]), maxfev=50000)
            i0, k = float(popt[0]), float(popt[1])
            i_inf = 0.0
    except Exception as err:                           # noqa: BLE001
        return _fail(out, f"单指数拟合未收敛：{type(err).__name__}: {err}")
    if not (math.isfinite(k) and math.isfinite(i0) and math.isfinite(i_inf)):
        return _fail(out, "拟合参数非有限，无法给出 k/t½")
    if k <= 0:
        return _fail(out, "拟合得到 k≤0（没有可测的衰减），无法给出光漂白速率")
    if use_platform:
        y_hat = _model_plateau(ts, i_inf, i0, k)
        i_start = float(_model_plateau(float(ts[0]), i_inf, i0, k))
        i_end = float(_model_plateau(float(ts[-1]), i_inf, i0, k))
    else:
        y_hat = _model_plain(ts, i0, k)
        i_start = float(_model_plain(float(ts[0]), i0, k))
        i_end = float(_model_plain(float(ts[-1]), i0, k))
    ss_res = float(np.sum((ys - y_hat) ** 2))
    ss_tot = float(np.sum((ys - float(np.mean(ys))) ** 2))
    r2 = float("nan") if ss_tot <= 0 else 1.0 - ss_res / ss_tot
    if math.isfinite(r2) and r2 < R2_MIN_ACCEPT:
        return _fail(out, f"单指数拟合质量过低（R²={r2:.3f}），数据不呈指数衰减")
    loss_frac = (max(0.0, min(1.0, (i_start - i_end) / i_start))
                 if i_start > 0 else float("nan"))
    issues: list = []
    if math.isfinite(r2) and r2 < 0.9:
        issues.append(f"WARNING：拟合 R²={r2:.3f} 偏低（<0.9），请检查数据质量")
    if use_platform and i0 > 0 and abs(i_inf) < 0.01 * abs(i0):
        issues.append("说明：平台项拟合值≈0，可关闭平台项得到双参数模型")
    if str(unit).strip().lower() not in ("min", "m", "minute", "minutes"):
        issues.append(f"说明：时间单位 {unit} 已换算为分钟（k 单位 min⁻¹）")
    out.update({
        "ok": True, "k": k, "k_min": k,
        "t_half": math.log(2.0) / k, "t_half_min": math.log(2.0) / k,
        "loss_frac": float(loss_frac),
        "loss_pct": (float(loss_frac) * 100.0
                     if math.isfinite(loss_frac) else float("nan")),
        "r2": r2, "i0": i0, "i_inf": i_inf,
        "n_points": int(ts.size), "issues": issues,
        "reason": "", "error": "",
    })
    return out


#: 别名（任务卡 / UI 侧命名）
fit_photobleach = fit_photobleaching
fit_single_exponential = fit_photobleaching


__all__ = [
    "TIME_FACTORS_TO_MIN", "MIN_POINTS", "unit_scale", "time_to_minutes",
    "read_two_column_csv", "load_decay_csv", "load_intensity_csv",
    "series_from_spectra", "from_spectra", "fit_photobleaching",
    "fit_photobleach", "fit_single_exponential",
]
