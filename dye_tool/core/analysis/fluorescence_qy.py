# -*- coding: utf-8 -*-
"""V2.6 FR-42：荧光量子产率 φ（相对法）+ 亮度（纯函数，零新配置键）。

论文口径（设计文档 §5.7 FR-42 / Q-E21）：

    φ = φ_ref × (I / I_ref) × (A_ref / A) × (n² / n_ref²)

* I = 样品发射谱积分强度（积分区间可设，默认全谱）；
* A = 样品在激发波长 λex 处的吸光度（建议 A ≤ 0.05，超限只给 WARNING 不阻断）；
* n / n_ref = 样品 / 参比溶剂折射率（显式填写，默认 1.36 可改）；
* 参比三参数（I_ref / A_ref / φ_ref）必填且必须为正数；缺失/非法 → 不计算并给中文原因；
* brightness = ε × φ（ε 由调用方从既有 ε 结果或手填传入，本模块只做乘法）。

本模块纯函数、不抛异常（输入非法一律 ok=False / NaN），不 import GUI，
不新增配置键，不改 Φ_Δ / ε / 动力学算法。
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

#: 相对法建议的最大吸光度（超过只加 WARNING，不阻断计算）
A_MAX = 0.05
ABS_MAX_WARN = A_MAX


# ═══════════════════════════ 小工具 ═══════════════════════════
def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _as_array(values) -> np.ndarray:
    try:
        return np.asarray(values, float).ravel()
    except (TypeError, ValueError):
        return np.array([], float)


def _spec_arrays(spec, y=None):
    """谱对象或 (wl, y) 数组 → (wl ndarray, y ndarray)；缺项返回空数组。"""
    if y is not None:
        return _as_array(spec), _as_array(y)
    wl = getattr(spec, "wl", None)
    vals = getattr(spec, "y", None)
    if vals is None:
        vals = getattr(spec, "a", None)          # Scan 用 .a 存吸光度
    return _as_array(wl), _as_array(vals)


def _finite_pairs(wl, y):
    """只保留 wl/y 都有限并递增排序的点。"""
    wl = _as_array(wl)
    y = _as_array(y)
    if wl.size == 0 or wl.size != y.size:
        return np.array([], float), np.array([], float)
    mask = np.isfinite(wl) & np.isfinite(y)
    if not mask.any():
        return np.array([], float), np.array([], float)
    w, v = wl[mask], y[mask]
    order = np.argsort(w, kind="stable")
    return w[order], v[order]


def _trapz(y, x) -> float:
    if x.size < 2 or y.size != x.size:
        return float("nan")
    fn = getattr(np, "trapezoid", None) or getattr(np, "trapz", None)
    if fn is None:                                   # pragma: no cover —— 理论不可达
        return float("nan")
    try:
        return float(fn(y, x))
    except (TypeError, ValueError):
        return float("nan")


# ═══════════════════════════ 基础量 ═══════════════════════════
def emission_integral(spec_or_wl, y=None, lo=None, hi=None) -> float:
    """发射谱积分强度 I = ∫ y dλ（默认全谱；区间由 lo/hi 限定）。

    支持 emission_integral(spec) 或 emission_integral(wl, y)；
    有效点 < 2 / 全 NaN → NaN（不抛）。
    """
    wl, vals = _spec_arrays(spec_or_wl, y)
    w, v = _finite_pairs(wl, vals)
    if w.size < 2:
        return float("nan")
    try:
        if lo is not None:
            m = w >= float(lo)
            w, v = w[m], v[m]
        if hi is not None:
            m = w <= float(hi)
            w, v = w[m], v[m]
    except (TypeError, ValueError):
        return float("nan")
    return _trapz(v, w)


#: UI 侧命名别名
integral_intensity = emission_integral


def absorbance_at(spec_or_wl, lam_or_y=None, lam=None) -> float:
    """λex 处的吸光度 A（线性插值；两种调用形式都支持）。

    * absorbance_at(spec, lam_ex)
    * absorbance_at(wl, y, lam)

    越界 / 有效点不足 / 非法 λ → NaN（不抛）。
    """
    if lam is None:
        spec, lam_ex = spec_or_wl, _as_float(lam_or_y)
        wl, vals = _spec_arrays(spec)
    else:
        lam_ex = _as_float(lam)
        wl, vals = _spec_arrays(spec_or_wl, lam_or_y)
    if not math.isfinite(lam_ex):
        return float("nan")
    w, v = _finite_pairs(wl, vals)
    if w.size < 2:
        return float("nan")
    if lam_ex < float(w[0]) or lam_ex > float(w[-1]):
        return float("nan")
    try:
        return float(np.interp(float(lam_ex), w, v))
    except (TypeError, ValueError):
        return float("nan")


def brightness(eps, phi) -> float:
    """亮度 brightness = ε × φ（任一非有限 → NaN）。"""
    e, p = _as_float(eps), _as_float(phi)
    return e * p if (math.isfinite(e) and math.isfinite(p)) else float("nan")


# ═══════════════════════════ 相对法公式 ═══════════════════════════
def relative_qy(i, a, n, i_ref, a_ref, phi_ref, n_ref) -> float:
    """相对法公式核：φ = φ_ref × (I/I_ref) × (A_ref/A) × (n²/n_ref²)。

    任一参数非有限 / 参比或折射率 ≤ 0 / A ≤ 0 → NaN（不抛）。
    """
    vals = [_as_float(x) for x in (i, a, n, i_ref, a_ref, phi_ref, n_ref)]
    iv, aw, nn, ir, ar, pr, nr = vals
    for value in vals:
        if not math.isfinite(value):
            return float("nan")
    if aw <= 0 or ir <= 0 or ar <= 0 or pr <= 0 or nn <= 0 or nr <= 0:
        return float("nan")
    return pr * (iv / ir) * (ar / aw) * (nn * nn) / (nr * nr)


def _ref_error(i_ref, a_ref, phi_ref, n_ref) -> str:
    for name, value in (("I_ref", i_ref), ("A_ref", a_ref),
                        ("φ_ref", phi_ref), ("n_ref", n_ref)):
        v = _as_float(value)
        if not math.isfinite(v) or v <= 0:
            return f"参比参数 {name} 必须是正的有限数（当前 {value!r}）"
    return ""


def fmt_phi(phi) -> str:
    """φ 的论文式显示：3 位有效数字；NaN → 破折号。"""
    v = _as_float(phi)
    return "—" if not math.isfinite(v) else f"{v:.3g}"


def _range_text(wl) -> str:
    w, _v = _finite_pairs(wl, np.ones_like(_as_array(wl)))
    if w.size == 0:
        return "无有效点"
    return f"{float(w[0]):.1f}–{float(w[-1]):.1f} nm"


def _fail(out: dict, reason: str) -> dict:
    out["ok"] = False
    out["error"] = str(reason)
    out["reason"] = str(reason)
    return out


# ═══════════════════════════ 数组级入口 ═══════════════════════════
def relative_quantum_yield(em_wl, em_y, abs_wl, abs_a, *, lam_ex,
                           i_ref, a_ref, phi_ref,
                           n_sample: float = 1.36, n_ref: float = 1.36,
                           int_range: Optional[Sequence[float]] = None
                           ) -> dict:
    """数组级相对法 φ：给定发射谱与吸收谱数组 + 参比参数。

    返回 dict：ok / phi / brightness(None) / a_ex / i_em /
    warnings(中文 list) / error / reason(=error)。
    """
    out = {"ok": False, "phi": float("nan"), "brightness": None,
           "a_ex": float("nan"), "i_em": float("nan"),
           "warnings": [], "error": "", "reason": ""}
    em_w, em_v = _finite_pairs(em_wl, em_y)
    ab_w, ab_v = _finite_pairs(abs_wl, abs_a)
    lam = _as_float(lam_ex)
    if not math.isfinite(lam):
        return _fail(out, f"激发波长 λex 不是有限数值（当前 {lam_ex!r}）")
    if ab_w.size < 2:
        return _fail(out, "吸收谱没有足够的有效点，无法取 λex 处吸光度")
    if em_w.size < 2:
        return _fail(out, "发射谱没有足够的有效点，无法积分")
    if lam < float(ab_w[0]) or lam > float(ab_w[-1]):
        return _fail(out, f"λex={lam:.1f} nm 不在吸收谱范围（{_range_text(ab_w)}）")
    # V26-B1（评审复核）：λex 只要求落在**吸收谱**范围内——A(λex) 从吸收谱取点；
    # 发射谱积分在其自身范围进行，与 λex 无关。仪器 EM 扫描常从 λex 之后开始（如
    # 吸收 300–700 nm、发射 706–848 nm），旧实现额外要求 λex ∈ 发射谱范围会让这类
    # 真实样品全部无法计算 φ。此限制已移除（不改积分/公式/参比语义）。
    lo = hi = None
    if int_range is not None:
        try:
            lo, hi = int_range[0], int_range[1]
        except (TypeError, ValueError, IndexError):
            return _fail(out, f"积分区间无效：{int_range!r}（应为 (lo, hi)）")
    i_em = emission_integral(em_w, em_v, lo, hi)
    a_ex = absorbance_at(ab_w, ab_v, lam)
    out["i_em"], out["a_ex"] = i_em, a_ex
    if not math.isfinite(i_em):
        return _fail(out, "发射谱积分强度不可得（有效点不足）")
    if not math.isfinite(a_ex):
        return _fail(out, f"λex={lam:.1f} nm 处吸光度不可得（插值失败）")
    ref_err = _ref_error(i_ref, a_ref, phi_ref, n_ref)
    if ref_err:
        return _fail(out, ref_err)
    phi = relative_qy(i_em, a_ex, n_sample, i_ref, a_ref, phi_ref, n_ref)
    if not math.isfinite(phi):
        return _fail(out, "φ 计算得到非有限值（请检查参比/折射率/吸光度输入）")
    if a_ex > A_MAX:
        out["warnings"].append(
            f"WARNING：激发波长处吸光度 A={a_ex:.3g} 超过建议值 {A_MAX:g}"
            "（相对法在稀溶液成立，A>0.05 会有内滤效应误差；结果仅供参考）")
    out["ok"] = True
    out["phi"] = float(phi)
    return out


# ═══════════════════════════ Spectrum 级入口 ═══════════════════════════
def _file_name(spec) -> str:
    path = getattr(spec, "path", None)
    if path:
        try:
            from pathlib import Path
            return Path(str(path)).name
        except (TypeError, ValueError):
            pass
    return str(getattr(spec, "label", "") or "")


def compute_fluorescence_qy(abs_spec, em_spec, *, lam_ex, i_ref, a_ref,
                            phi_ref, n_ref: float = 1.36,
                            n: float = 1.36,
                            em_range: Optional[Sequence[float]] = None
                            ) -> dict:
    """Spectrum 级相对法 φ（吸收谱 + 发射谱 + 参比三参数 + λex）。

    返回键在 relative_quantum_yield 基础上追加：
    phi_display（3 位有效数字字符串；失败为破折号）。
    """
    ab_wl, ab_y = _spec_arrays(abs_spec)
    em_wl, em_y = _spec_arrays(em_spec)
    out = relative_quantum_yield(
        em_wl, em_y, ab_wl, ab_y, lam_ex=lam_ex, i_ref=i_ref,
        a_ref=a_ref, phi_ref=phi_ref, n_sample=n, n_ref=n_ref,
        int_range=em_range)
    out["phi_display"] = fmt_phi(out.get("phi"))
    out["abs_file"] = _file_name(abs_spec)
    out["em_file"] = _file_name(em_spec)
    return out


# ═══════════════════════════ UI 便捷入口（已算好 I/A 的场景） ═══════════════════════════
def compute_phi_relative(*, i_sample=None, i_ref=None, a_sample=None,
                         a_ref=None, phi_ref=None, n_sample: float = 1.36,
                         n_ref: float = 1.36, i=None, a=None) -> dict:
    """由已算好的 I/A 求 φ（i/a 是 i_sample/a_sample 的别名）。"""
    i_val = i_sample if i_sample is not None else i
    a_val = a_sample if a_sample is not None else a
    warnings: list = []
    reason = ""
    phi = relative_qy(i_val, a_val, n_sample, i_ref, a_ref, phi_ref, n_ref)
    if not math.isfinite(phi):
        a_f = _as_float(a_val)
        ref_err = _ref_error(i_ref, a_ref, phi_ref, n_ref)
        reason = ref_err or "φ 计算得到非有限值（请检查 I/A/参比/折射率输入）"
        if math.isfinite(a_f) and a_f <= 0:
            reason = "样品在 λex 处的吸光度 A 必须为正数"
    else:
        a_f = _as_float(a_val)
        if math.isfinite(a_f) and a_f > A_MAX:
            warnings.append(
                f"WARNING：激发波长处吸光度 A={a_f:.3g} 超过建议值 {A_MAX:g}"
                "（相对法在稀溶液成立，A>0.05 会有内滤效应误差；结果仅供参考）")
    return {"ok": bool(math.isfinite(phi)), "phi": float(phi),
            "reason": reason, "error": reason, "warnings": warnings,
            "i_em": _as_float(i_val), "a_ex": _as_float(a_val)}


__all__ = [
    "A_MAX", "ABS_MAX_WARN", "relative_qy", "relative_quantum_yield",
    "compute_fluorescence_qy", "compute_phi_relative", "emission_integral",
    "integral_intensity", "absorbance_at", "brightness", "fmt_phi",
]
