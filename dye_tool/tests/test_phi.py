# -*- coding: utf-8 -*-
"""``core.phi`` 单元测试：吸收占比、Φ 公式与误差传递、T3 互易性、对照判定、F5 批量。"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import make_fit
from core.kinetics import FitResult
from core.phi import (bootstrap_phi, calc_phi, compute_phi, iabs,
                      judge_controls, phi_sigma)

PHI_REF = 0.75
A_EX = 0.15


def _entry(role: str, k: float, k_err: float = 1e-5, a_ex_auto=None,
           status: str = "OK") -> dict:
    """构造 compute_phi/judge_controls 所需的 results 条目（最小契约）。"""
    return {"role": role, "reps": {}, "a_ex_auto": a_ex_auto,
            "pooled": FitResult(k=k, k_err=k_err, r2=0.999, n=10, status=status)}


# ═══════════════════════════ M5/M7: I_abs 与 Φ ═══════════════════════════
def test_iabs_exact_and_linear_modes() -> None:
    """``exact`` = 1−10^(−A)；``linear`` = 2.303·A（仅小 A 近似）。"""
    assert iabs(A_EX) == pytest.approx(1.0 - 10.0 ** (-A_EX))
    assert iabs(A_EX, "exact") == 1.0 - 10.0 ** (-A_EX)
    assert iabs(A_EX, "linear") == pytest.approx(2.303 * A_EX)
    assert iabs(0.0) == 0.0
    # 极小 A 时两者接近（线性近似的适用域：A ~1e-5 时相对差约 2e-4）
    assert iabs(1e-5, "linear") == pytest.approx(iabs(1e-5, "exact"), rel=1e-3)
    assert iabs(0.15, "linear") > iabs(0.15, "exact")      # 大 A 时线性近似偏高


def test_calc_phi_matches_closed_formula() -> None:
    """Φ = Φ_ref × (k_s/k_r) × (I_abs,r/I_abs,s) —— 逐项对照闭式公式。"""
    ks, kr = 2.0e-3, 5.0e-3
    got = calc_phi(ks, kr, A_EX, 0.20, PHI_REF)
    want = PHI_REF * (ks / kr) * (iabs(0.20) / iabs(A_EX))
    assert got == pytest.approx(want, rel=1e-15)
    # 两侧 A_ex 相同时退化为纯 k 比值 × Φ_ref
    assert calc_phi(ks, kr, A_EX, A_EX, PHI_REF) == pytest.approx(PHI_REF * ks / kr)
    # linear 模式
    got_lin = calc_phi(ks, kr, A_EX, 0.20, PHI_REF, mode="linear")
    assert got_lin == pytest.approx(PHI_REF * (ks / kr) * (0.20 / A_EX))


def test_calc_phi_aex_reciprocity_scaling_by_ten() -> None:
    """T3 自检：任一侧 A_ex ×10 → Φ 严格按 ``I_abs(10a)/I_abs(a)`` 缩放（1e-9）。"""
    ks, kr = 5.0e-3, 6.25e-3
    p0 = calc_phi(ks, kr, A_EX, A_EX, PHI_REF)
    p_ref10 = calc_phi(ks, kr, A_EX, 10 * A_EX, PHI_REF)   # 参比侧 A_ex ×10
    p_smp10 = calc_phi(ks, kr, 10 * A_EX, A_EX, PHI_REF)   # 样品侧 A_ex ×10
    ratio = iabs(10 * A_EX) / iabs(A_EX)

    assert abs(p_ref10 / p0 - ratio) < 1e-9
    assert abs(p_smp10 / p0 - 1.0 / ratio) < 1e-9


def test_phi_sigma_scales_with_phi_and_grows_with_uncertainties() -> None:
    """σ_Φ 与 Φ 成正比；任一输入不确定度增大 → σ_Φ 增大。"""
    kw = dict(k_s=2.0e-3, k_s_err=1.0e-4, k_r=5.0e-3, k_r_err=2.0e-4,
              aex_s=A_EX, aex_r=0.20, phi_ref=PHI_REF, phi_ref_err=0.075,
              a_ex_err=0.005)
    base = phi_sigma(0.4, **kw)
    assert phi_sigma(0.8, **kw) == pytest.approx(2.0 * base, rel=1e-12)

    assert phi_sigma(0.4, **{**kw, "k_s_err": 2.0e-4}) > base
    assert phi_sigma(0.4, **{**kw, "k_r_err": 4.0e-4}) > base
    assert phi_sigma(0.4, **{**kw, "phi_ref_err": 0.15}) > base
    assert phi_sigma(0.4, **{**kw, "a_ex_err": 0.02}) > base
    # 全零不确定度 → σ_Φ = 0
    zero = {**kw, "k_s_err": 0.0, "k_r_err": 0.0, "phi_ref_err": 0.0, "a_ex_err": 0.0}
    assert phi_sigma(0.4, **zero) == 0.0
    # linear 模式的 A_ex 项使用相对误差
    lin = phi_sigma(0.4, **{**kw, "a_ex_err": 0.01}, mode="linear")
    assert lin > 0 and np.isfinite(lin)


def test_phi_sigma_matches_error_propagation_formula() -> None:
    """与手写误差传递公式逐项一致。"""
    kw = dict(k_s=2.0e-3, k_s_err=1.0e-4, k_r=5.0e-3, k_r_err=2.0e-4,
              aex_s=A_EX, aex_r=0.20, phi_ref=PHI_REF, phi_ref_err=0.075,
              a_ex_err=0.005)
    phi = calc_phi(kw["k_s"], kw["k_r"], kw["aex_s"], kw["aex_r"], PHI_REF)
    rel = ((kw["k_s_err"] / kw["k_s"]) ** 2 + (kw["k_r_err"] / kw["k_r"]) ** 2
           + (kw["phi_ref_err"] / PHI_REF) ** 2)
    for ax in (kw["aex_s"], kw["aex_r"]):
        rel += (np.log(10.0) * 10.0 ** (-ax) * kw["a_ex_err"] / iabs(ax)) ** 2
    assert phi_sigma(phi, **kw) == pytest.approx(phi * np.sqrt(rel), rel=1e-15)


# ═══════════════════════════ bootstrap ═══════════════════════════
def test_bootstrap_phi_returns_finite_mean_and_std(cfg) -> None:
    """成对 bootstrap 重采样：均值/标准差有限；点太少 → (NaN, NaN)。"""
    rng = np.random.default_rng(7)
    t = np.arange(0.0, 110.0, 10.0)
    R_s = np.exp(-0.01 * t) * (1 + 0.02 * rng.normal(size=t.size))
    R_r = np.exp(-0.008 * t) * (1 + 0.02 * rng.normal(size=t.size))
    mean, sd = bootstrap_phi(t, R_s, t, R_r, A_EX, A_EX, PHI_REF, cfg,
                             n=200, seed=42)
    assert np.isfinite(mean) and np.isfinite(sd) and sd > 0
    assert mean == pytest.approx(PHI_REF * 0.01 / 0.008, rel=0.2)

    # 相同输入 + 相同 seed → 结果可复现
    mean2, sd2 = bootstrap_phi(t, R_s, t, R_r, A_EX, A_EX, PHI_REF, cfg,
                               n=200, seed=42)
    assert mean2 == mean and sd2 == sd

    nan_pair = bootstrap_phi(np.array([0.0, 1.0]), np.array([1.0, 0.9]),
                             t, R_r, A_EX, A_EX, PHI_REF, cfg, n=10)
    assert np.isnan(nan_pair[0]) and np.isnan(nan_pair[1])


# ═══════════════════════════ F6: 对照判定 ═══════════════════════════
def test_judge_controls_dark_pass_and_doubt(cfg) -> None:
    """暗对照：|k_dark| ≤ 5%·k_sample → 通过，否则存疑。"""
    res = {"sample": _entry("sample", 1.0e-3),
           "dark": _entry("dark", 1.0e-5)}
    jc = judge_controls(res, cfg)
    assert jc["dark"][0] == "通过"
    assert "|k_dark|=1.00e-05" in jc["dark"][1]

    res2 = {"sample": _entry("sample", 1.0e-3),
            "dark": _entry("dark", 5.0e-4)}
    assert judge_controls(res2, cfg)["dark"][0] == "存疑"
    # 恰好在阈值上（≤）→ 通过
    res3 = {"sample": _entry("sample", 1.0e-3),
            "dark": _entry("dark", 5.0e-5)}
    assert judge_controls(res3, cfg)["dark"][0] == "通过"


def test_judge_controls_n2_and_quencher_three_levels(cfg) -> None:
    """N₂/淬灭剂抑制率三档：≥70% 通过、≥35% 存疑、否则失败。"""
    res = {"sample": _entry("sample", 1.0e-3),
           "n2": _entry("n2", 2.0e-4),              # 抑制 80% → 通过
           "nan3": _entry("quencher", 9.0e-4)}      # 抑制 10% → 失败
    jc = judge_controls(res, cfg)
    assert jc["n2"][0] == "通过"
    assert "80%" in jc["n2"][1]
    assert jc["nan3"][0] == "失败"

    res2 = {"sample": _entry("sample", 1.0e-3),
            "n2": _entry("n2", 5.0e-4),              # 抑制 50% → 存疑
            "nan3": _entry("quencher", 4.0e-4)}      # 抑制 60% → 存疑
    jc2 = judge_controls(res2, cfg)
    assert jc2["n2"][0] == "存疑"
    assert jc2["nan3"][0] == "存疑"


def test_judge_controls_edge_cases(cfg) -> None:
    """无 sample / 全部拟合失败 / 关闭开关 / 非对照角色 → 空或跳过。"""
    assert judge_controls({}, cfg) == {}
    assert judge_controls({"s": _entry("sample", 1.0e-3)}, cfg) == {}
    assert judge_controls({"s": _entry("sample", np.nan, status="FAILED"),
                           "dark": _entry("dark", 1e-5)}, cfg) == {}
    off = {**cfg, "controls": {**cfg["controls"], "enable": False}}
    assert judge_controls({"s": _entry("sample", 1e-3),
                           "dark": _entry("dark", 1e-5)}, off) == {}
    # reference 角色不参与判定
    assert judge_controls({"s": _entry("sample", 1e-3),
                           "ref": _entry("reference", 6e-3)}, cfg) == {}
    # 对照组自身拟合失败 → 不判定
    assert judge_controls({"s": _entry("sample", 1e-3),
                           "dark": _entry("dark", np.nan, status="FAILED")}, cfg) == {}


def test_judge_controls_accepts_duck_typed_pooled(cfg) -> None:
    """只需 ``.k`` 与 ``.status`` 的“鸭子类型”对象（GUI 场景）。"""
    res = {"sample": {"role": "sample", "pooled": make_fit(1.0e-3)},
           "dark": {"role": "dark", "pooled": make_fit(1.0e-6)}}
    assert judge_controls(res, cfg)["dark"][0] == "通过"


# ═══════════════════════════ F5: 批量 Φ ═══════════════════════════
def test_compute_phi_no_ref_returns_no_ref_status(cfg) -> None:
    """无 ``reference`` 角色 → status=NO_REF（NaN），且不抛异常。"""
    res = {"s": _entry("sample", 1.0e-3, a_ex_auto=0.3)}
    phi = compute_phi(res, cfg)
    assert set(phi) == {"s"}
    assert phi["s"]["status"] == "NO_REF"
    assert np.isnan(phi["s"]["phi"]) and np.isnan(phi["s"]["phi_err"])
    assert phi["s"]["pairs"] == []


def test_compute_phi_failed_sample_reported_failed(cfg) -> None:
    """样品组拟合失败 → status=FAILED（即使有参比）。"""
    res = {"s": _entry("sample", np.nan, status="FAILED"),
           "ref": _entry("reference", 6.25e-3, a_ex_auto=0.15)}
    phi = compute_phi(res, cfg)
    assert phi["s"]["status"] == "FAILED"


def test_compute_phi_with_reference_close_to_truth(cfg) -> None:
    """构造参比组 → Φ 有限且接近真值（synthetic 真值 Φ=0.60）。"""
    cfg["quantum_yield"]["a_ex"] = {"sample": A_EX, "ref": A_EX}
    cfg["quantum_yield"]["phi_ref"] = PHI_REF
    res = {"sample": _entry("sample", 5.0e-3, k_err=1.0e-5, a_ex_auto=np.nan),
           "ref": _entry("reference", 6.25e-3, k_err=1.0e-5, a_ex_auto=np.nan)}
    phi = compute_phi(res, cfg)
    assert phi["sample"]["status"] == "OK"
    assert phi["sample"]["phi"] == pytest.approx(0.60, rel=1e-12)
    assert phi["sample"]["phi_err"] > 0 and np.isfinite(phi["sample"]["phi_err"])
    assert len(phi["sample"]["pairs"]) == 1
    assert phi["sample"]["pairs"][0]["ref"] == "ref"
    # reference 组自身不出现在输出里
    assert set(phi) == {"sample"}


def test_compute_phi_uses_auto_aex_when_not_declared(cfg) -> None:
    """config 未声明 A_ex 时用 ``a_ex_auto``（最早光谱 λ_ex 处吸光度）。"""
    res = {"sample": _entry("sample", 1.0e-3, a_ex_auto=0.30),
           "ref": _entry("reference", 2.0e-3, a_ex_auto=0.20)}
    phi = compute_phi(res, cfg)
    want = PHI_REF * (1.0e-3 / 2.0e-3) * (iabs(0.20) / iabs(0.30))
    assert phi["sample"]["status"] == "OK"
    assert phi["sample"]["phi"] == pytest.approx(want, rel=1e-12)


def test_compute_phi_averages_multiple_references(cfg) -> None:
    """多个参比组 → Φ 取均值，误差合并。"""
    res = {"sample": _entry("sample", 1.0e-3, a_ex_auto=0.15),
           "ref1": _entry("reference", 2.0e-3, a_ex_auto=0.15),
           "ref2": _entry("reference", 4.0e-3, a_ex_auto=0.15)}
    phi = compute_phi(res, cfg)
    want = PHI_REF * (1.0e-3 / 2.0e-3) * (iabs(0.15) / iabs(0.15))
    want2 = PHI_REF * (1.0e-3 / 4.0e-3)
    assert len(phi["sample"]["pairs"]) == 2
    assert phi["sample"]["phi"] == pytest.approx(0.5 * (want + want2), rel=1e-12)


def test_compute_phi_skips_unusable_pairs(cfg) -> None:
    """k≤0 或 A_ex 非有限 → 该配对跳过；全部跳过 → FAILED。"""
    res = {"sample": _entry("sample", 1.0e-3, a_ex_auto=0.15),
           "ref_bad_k": _entry("reference", -1.0e-3, a_ex_auto=0.15)}
    assert compute_phi(res, cfg)["sample"]["status"] == "FAILED"

    res2 = {"sample": _entry("sample", 1.0e-3, a_ex_auto=np.nan),
            "ref": _entry("reference", 2.0e-3, a_ex_auto=0.15)}
    assert compute_phi(res2, cfg)["sample"]["status"] == "FAILED"

    # 无参比组本身拟合失败 → 视作无参比 → NO_REF
    res3 = {"sample": _entry("sample", 1.0e-3, a_ex_auto=0.15),
            "ref": _entry("reference", np.nan, status="FAILED")}
    assert compute_phi(res3, cfg)["sample"]["status"] == "NO_REF"


def test_compute_phi_bootstrap_entry(cfg) -> None:
    """``quantum_yield.bootstrap=True`` → 输出附带 bootstrap 统计。"""
    cfg["quantum_yield"]["bootstrap"] = True
    cfg["quantum_yield"]["bootstrap_n"] = 100
    t = np.arange(0.0, 110.0, 10.0)
    R = np.exp(-0.01 * t)
    res = {"sample": {**_entry("sample", 1.0e-3, a_ex_auto=0.15),
                      "reps": {"": {"t": t, "R": R}}},
           "ref": {**_entry("reference", 6.0e-3, a_ex_auto=0.15),
                   "reps": {"": {"t": t, "R": np.exp(-0.008 * t)}}}}
    phi = compute_phi(res, cfg)
    assert "bootstrap" in phi["sample"]
    assert np.isfinite(phi["sample"]["bootstrap"]["mean"])
