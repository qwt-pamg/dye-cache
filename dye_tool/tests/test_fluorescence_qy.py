# -*- coding: utf-8 -*-
"""V2.6 G 道 FR-42 / A42.1–A42.2：荧光量子产率 φ（相对法）+ 亮度。

相对法公式（论文口径，设计文档 §5.7 FR-42）：

    φ = φ_ref × (I / I_ref) × (A_ref / A) × (n² / n_ref²)

I = 发射谱积分强度；A = 激发波长 λex 处吸光度；n = 样品溶剂折射率，
n_ref = 参比折射率；参比三参数（I_ref/A_ref/φ_ref）与 λex 均为显式输入。

覆盖：
* A42.1 φ 相对法：手算对照（含 n²/n_ref²）；λex 越界 / 参比或折射率缺失、非正
  → 拒绝并给中文原因；A > 0.05 给 WARNING 但不阻断；
* A42.2 亮度：brightness = ε × φ 数值正确；ε 由调用方显式传入（既有多来源）。
"""
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════ 模块/夹具 ═══════════════════════
def _mod():
    import core.analysis.fluorescence_qy as m
    return m


def _fn(mod, *names):
    for n in names:
        f = getattr(mod, n, None)
        if callable(f):
            return f
    raise AssertionError(
        f"fluorescence_qy 缺函数 {names}；现有公开名："
        f"{[n for n in dir(mod) if not n.startswith('_')]}")


def _is_zh(text) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _is_ok(res) -> bool:
    return bool(res.get("ok")) if isinstance(res, dict) else False


def _warnings(res) -> list:
    if not isinstance(res, dict):
        return []
    out = []
    for key in ("warnings", "issues", "warning"):
        val = res.get(key)
        if isinstance(val, (list, tuple)):
            out.extend(str(v) for v in val)
        elif isinstance(val, str) and val.strip():
            out.append(val)
    return out


def _spec(wl, y, name: str = "spec.txt", kind: str = "abs"):
    return SimpleNamespace(wl=np.asarray(wl, float), y=np.asarray(y, float),
                           path=Path(name), kind=kind, ok=True,
                           label=Path(name).stem)


def _abs_spec(lam_ex: float = 450.0, a_ex: float = 0.04):
    wl = np.arange(380.0, 701.0, 1.0)
    y = a_ex * np.exp(-0.5 * ((wl - lam_ex) / 40.0) ** 2)
    return _spec(wl, y, "abs-450.txt", "abs")


def _em_spec(lam_em: float = 520.0):
    wl = np.arange(430.0, 801.0, 1.0)
    y = 1.0 * np.exp(-0.5 * ((wl - lam_em) / 35.0) ** 2)
    return _spec(wl, y, "em-520.txt", "em")


def _valid_kwargs() -> dict:
    """一组合法参比参数（论文例：Cresyl Violet 0.54 in MeOH，n_ref=1.36）。"""
    return dict(i_ref=1.0, a_ref=0.08, phi_ref=0.54, n_ref=1.36, n=1.33)


# ═══════════════════════ A42.1 相对法公式 ═══════════════════════
def test_a42_1_relative_formula_matches_hand_calculation() -> None:
    """A42.1：relative_qy 与手算逐项一致（含 n²/n_ref²，容差 1e-12）。"""
    rel = _fn(_mod(), "relative_qy", "qy_relative", "compute_relative_qy")
    phi_ref = 0.54
    i, i_ref = 2.0, 1.0
    a, a_ref = 0.04, 0.08
    n, n_ref = 1.33, 1.36
    want = phi_ref * (i / i_ref) * (a_ref / a) * ((n * n) / (n_ref * n_ref))
    got = rel(i, a, n, i_ref, a_ref, phi_ref, n_ref)
    assert float(got) == pytest.approx(want, rel=1e-12), (got, want)

    # n == n_ref → 折射率修正项 = 1
    want_same_n = phi_ref * (i / i_ref) * (a_ref / a)
    assert float(rel(i, a, n_ref, i_ref, a_ref, phi_ref, n_ref)) == pytest.approx(
        want_same_n, rel=1e-12)

    # 坏输入 → NaN（不抛）：非有限 I / A≤0 / 参比或折射率≤0
    for bad in ((float("nan"), a, n, i_ref, a_ref, phi_ref, n_ref),
                (i, 0.0, n, i_ref, a_ref, phi_ref, n_ref),
                (i, a, n, 0.0, a_ref, phi_ref, n_ref),
                (i, a, n, i_ref, a_ref, 0.0, n_ref),
                (i, a, n, i_ref, a_ref, phi_ref, 0.0)):
        val = rel(*bad)
        assert math.isnan(float(val)), f"{bad} 应返回 NaN"


def test_a42_1_compute_from_spectra_matches_hand_and_rejects_bad_lambdaex() -> None:
    """A42.1：spectra 入口与积分/取点手算一致；λex 越界 → 中文拒绝。"""
    mod = _mod()
    compute = _fn(mod, "compute_fluorescence_qy", "compute_qy",
                  "fluorescence_quantum_yield")
    integrate = _fn(mod, "emission_integral", "integral_intensity")
    absorb = _fn(mod, "absorbance_at", "absorbance_at_wavelength")
    abs_s = _abs_spec(450.0, 0.04)
    em_s = _em_spec(520.0)
    kw = _valid_kwargs()
    res = compute(abs_s, em_s, lam_ex=450.0, **kw)
    assert _is_ok(res), f"合法输入计算失败：{res.get('reason') if isinstance(res, dict) else res}"
    i_em = float(integrate(em_s))
    a_ex = float(absorb(abs_s, 450.0))
    want = (kw["phi_ref"] * (i_em / kw["i_ref"]) * (kw["a_ref"] / a_ex)
            * (kw["n"] ** 2 / kw["n_ref"] ** 2))
    assert float(res["phi"]) == pytest.approx(want, rel=1e-12)
    if "a_ex" in res:
        assert float(res["a_ex"]) == pytest.approx(a_ex, rel=1e-12)
    if "i_em" in res:
        assert float(res["i_em"]) == pytest.approx(i_em, rel=1e-12)

    # λex 只需落在**吸收谱**范围内；越界（200/900）→ 中文拒绝
    for bad_ex in (900.0, 200.0):
        bad = compute(abs_s, em_s, lam_ex=bad_ex, **kw)
        assert _is_ok(bad) is False, f"λex={bad_ex} 应拒绝（越界）"
        assert _is_zh(bad.get("reason")), f"λex={bad_ex} 缺中文原因：{bad.get('reason')!r}"

    # V26-B1：真实仪器 EM 扫描常从 λex 之后开始（无重叠区）——λex 在吸收谱内、
    # 在发射谱外时必须仍可计算（发射积分取发射谱自身范围）。
    em_base = _em_spec()
    keep = em_base.wl >= 520.0
    wl, y = em_base.wl, em_base.y
    em_late = _spec(wl[keep], y[keep], "em-520-start.txt", "em")
    late = compute(abs_s, em_late, lam_ex=450.0, **kw)
    assert _is_ok(late), (
        "λex 在吸收谱内、发射谱从 520 nm 起时应可计算（V26-B1）："
        f"{late.get('reason')}")
    i_late = float(integrate(em_late))
    a_late = float(absorb(abs_s, 450.0))
    want_late = (kw["phi_ref"] * (i_late / kw["i_ref"]) * (kw["a_ref"] / a_late)
                 * (kw["n"] ** 2 / kw["n_ref"] ** 2))
    assert float(late["phi"]) == pytest.approx(want_late, rel=1e-12)


def test_a42_1_reference_and_refractive_index_validation() -> None:
    """A42.1：参比/折射率缺失或非正 → 不计算 + 中文原因；合法组合仍通过。"""
    compute = _fn(_mod(), "compute_fluorescence_qy", "compute_qy",
                  "fluorescence_quantum_yield")
    abs_s, em_s = _abs_spec(), _em_spec()
    base = _valid_kwargs()
    assert _is_ok(compute(abs_s, em_s, lam_ex=450.0, **base))

    bad_cases = [("i_ref", 0.0), ("i_ref", -1.0), ("a_ref", 0.0),
                 ("a_ref", None), ("phi_ref", 0.0), ("phi_ref", None),
                 ("n_ref", 0.0), ("n", 0.0), ("n", None), ("n_ref", None)]
    failures = []
    for key, val in bad_cases:
        kw = dict(base)
        kw[key] = val
        res = compute(abs_s, em_s, lam_ex=450.0, **kw)
        if _is_ok(res):
            failures.append(f"{key}={val!r} 竟算出结果（应拒绝）")
        elif not _is_zh(res.get("reason")):
            failures.append(f"{key}={val!r} 缺中文原因：{res.get('reason')!r}")
    assert not failures, "校验失败：" + "；".join(failures)


def test_a42_1_high_absorbance_warns_but_still_computes() -> None:
    """A42.1：A(λex) > 0.05 → WARNING 但不阻断；≤ 0.05 不给该警告。"""
    mod = _mod()
    compute = _fn(mod, "compute_fluorescence_qy", "compute_qy",
                  "fluorescence_quantum_yield")
    kw = _valid_kwargs()
    high = compute(_abs_spec(450.0, 0.20), _em_spec(), lam_ex=450.0, **kw)
    assert _is_ok(high), f"A=0.20 不应阻断计算：{high.get('reason')}"
    warns = _warnings(high)
    assert any(_is_zh(w) and ("0.05" in w or "吸光度" in w or "A" in w)
               for w in warns), f"A>0.05 没有 WARNING：{warns}"

    low = compute(_abs_spec(450.0, 0.03), _em_spec(), lam_ex=450.0, **kw)
    assert _is_ok(low)
    assert not any("0.05" in w for w in _warnings(low)), (
        f"A≤0.05 仍给 0.05 警告：{_warnings(low)}")


# ═══════════════════════ A42.2 亮度 ═══════════════════════
def test_a42_2_brightness_equals_epsilon_times_phi() -> None:
    """A42.2：brightness = ε × φ；ε 由调用方显式传入（既有 ε 结果/手填）。"""
    bright = _fn(_mod(), "brightness", "compute_brightness")
    eps, phi = 5.0e4, 0.54
    assert float(bright(eps, phi)) == pytest.approx(eps * phi)
    assert float(bright(0.0, phi)) == pytest.approx(0.0)
    assert math.isnan(float(bright(float("nan"), phi)))
    assert math.isnan(float(bright(eps, float("nan"))))
