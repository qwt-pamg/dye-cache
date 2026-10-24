# -*- coding: utf-8 -*-
"""``core.kinetics`` 单元测试：背景扣除、探针提取、归一化、准一级拟合、exp2/exp3。"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import make_scan
from core.kinetics import (FitResult, _exp2, _exp3, excitation_abs,
                           extract_probe, fit_kinetics, fit_series,
                           group_kinetics, normalize_series, probe_abs,
                           subtract_background)

WL = np.arange(300.0, 701.0)


def _gauss(mu: float, sig: float, amp: float = 1.0) -> np.ndarray:
    return amp * np.exp(-0.5 * ((WL - mu) / sig) ** 2)


# ═══════════════════════════ M1: 背景扣除 ═══════════════════════════
def test_subtract_background_matched_mode(cfg) -> None:
    """matched 模式：按时间最近扣背景，样品吸光度整体下移背景值。"""
    bg = make_scan("bg-0.txt", WL, np.full(WL.size, 0.10), group="bg",
                   role="background", t=0.0)
    s1 = make_scan("s-10.txt", WL, np.full(WL.size, 0.50), t=10.0)
    s2 = make_scan("s-100.txt", WL, np.full(WL.size, 0.80), t=100.0)
    info = subtract_background([bg, s1, s2], cfg["background"])
    assert info == {"mode": "matched", "n_bg": 1}
    assert np.allclose(s1.a, 0.40)
    assert np.allclose(s2.a, 0.70)
    assert np.allclose(bg.a, 0.10)                        # 背景自身不被扣


def test_subtract_background_mean_mode_and_no_bg(cfg) -> None:
    """非 matched 模式用背景平均谱；无背景组时返回 {} 且不改数据。"""
    b1 = make_scan("bg-0.txt", WL, np.full(WL.size, 0.10), group="bg",
                   role="background")
    b2 = make_scan("bg-2.txt", WL, np.full(WL.size, 0.30), group="bg",
                   role="background", t=2.0)
    s = make_scan("s-1.txt", WL, np.full(WL.size, 1.00), t=1.0)
    info = subtract_background([b1, b2, s], {**cfg["background"], "mode": "mean"})
    assert info["n_bg"] == 2
    assert np.allclose(s.a, 0.80)

    s2 = make_scan("s-3.txt", WL, np.full(WL.size, 1.00), t=3.0)
    assert subtract_background([s2], cfg["background"]) == {}
    assert np.allclose(s2.a, 1.00)

    off = {**cfg["background"], "enable": False}
    assert subtract_background([b1, s2], off) == {}


# ═══════════════════════════ M2: 探针提取 ═══════════════════════════
def test_extract_probe_takes_max_inside_window(cfg) -> None:
    """窗口内取最大值（抗峰漂移）。"""
    a = _gauss(380.0, 3.0, 0.8)
    val = extract_probe(WL, a, cfg["wavelength"], "probe")
    m = (WL >= 377.0) & (WL <= 381.0)
    assert val == pytest.approx(float(np.max(a[m])))
    assert val == pytest.approx(float(np.max(a)))


def test_extract_probe_strict_point_when_window_disabled(cfg) -> None:
    """``probe_window <= 0`` → 严格取最近单点（不取窗口最大值）。"""
    a = _gauss(380.0, 3.0, 0.8)
    wl_cfg = {**cfg["wavelength"], "probe_window": 0.0}
    val = extract_probe(WL, a, wl_cfg, "probe")
    assert val == pytest.approx(float(a[79]))             # λ=379 nm
    assert val < float(np.max(a))                         # 与窗口最大值不同


def test_extract_probe_nan_paths(cfg) -> None:
    """窗口内无有效数据 / 严格单点为 NaN → 返回 NaN 并记警告。"""
    a = np.full(WL.size, np.nan)
    assert np.isnan(extract_probe(WL, a, cfg["wavelength"], "all-nan"))
    wl_cfg = {**cfg["wavelength"], "probe_window": 0.0}
    assert np.isnan(extract_probe(WL, a, wl_cfg, "point-nan"))


def test_probe_abs_returns_mean_or_none() -> None:
    """``probe_abs``：窗口均值；无有效数据 → None。"""
    a = _gauss(379.0, 20.0)
    m = (WL >= 377.0) & (WL <= 381.0)
    assert probe_abs(WL, a, 379.0, 2.0) == pytest.approx(float(np.mean(a[m])))
    assert probe_abs(WL, np.full(WL.size, np.nan), 379.0, 2.0) is None


# ═══════════════════════════ M3: 归一化 ═══════════════════════════
def test_normalize_series_prefers_dark_points(cfg) -> None:
    """存在 t≤0 的暗态点 → A(0) 取暗态点均值，R=1 at t=0。"""
    t = np.array([0.0, 0.0, 10.0, 20.0, 30.0])
    A = np.array([1.00, 1.02, 0.80, 0.60, 0.40])
    A0, R = normalize_series(t, A, cfg["a0"], "dark")
    assert A0 == pytest.approx(1.01)
    assert R[0] == pytest.approx(1.00 / 1.01)
    assert R[2] == pytest.approx(0.80 / 1.01)


def test_normalize_series_falls_back_to_earliest_two_points(cfg) -> None:
    """无暗态点 → A(0) 取最早 2 个有限点均值（引擎记 WARNING）。"""
    t = np.array([1.0, 5.0, 10.0, 20.0, 30.0])
    A = np.array([1.10, 1.12, np.nan, 0.80, 0.60])
    A0, R = normalize_series(t, A, cfg["a0"], "early")
    assert A0 == pytest.approx(1.11)                       # (1.10+1.12)/2
    assert R[3] == pytest.approx(0.80 / 1.11)
    assert np.isnan(R[2])

    # 单点序列（n_early=2 但只有 1 个有限点）
    A0b, Rb = normalize_series(np.array([7.0]), np.array([0.5]), cfg["a0"], "one")
    assert A0b == pytest.approx(0.5) and Rb[0] == pytest.approx(1.0)


def test_normalize_series_masks_below_a_floor(cfg) -> None:
    """``A < a_floor`` 的点掩码为 NaN（防 ln→-inf）。"""
    t = np.array([0.0, 10.0, 20.0, 30.0])
    A = np.array([1.0, 0.005, 0.5, -0.02])
    A0, R = normalize_series(t, A, cfg["a0"], "floor", a_floor=0.01)
    assert A0 == pytest.approx(1.0)
    assert np.isnan(R[1]) and np.isnan(R[3])
    assert R[2] == pytest.approx(0.5)


def test_normalize_series_invalid_a0_masks_everything(cfg) -> None:
    """A(0) 非有限或为 0 → R 全 NaN。"""
    A0, R = normalize_series(np.array([0.0, 1.0]), np.array([0.0, 0.5]),
                             cfg["a0"], "zero")
    assert A0 == 0.0 and np.isnan(R).all()
    A0b, Rb = normalize_series(np.array([0.0, 1.0]),
                               np.array([np.nan, np.nan]), cfg["a0"], "nan")
    assert not np.isfinite(A0b) and np.isnan(Rb).all()


# ═══════════════════════════ M4: 准一级拟合 ═══════════════════════════
def test_fit_kinetics_recovers_k_on_noise_free_exponential(cfg) -> None:
    """无噪声指数数据 → k 反算偏差 <0.1%，R²=1，不触发截断。"""
    k_true = 1.0e-2
    t = np.arange(0.0, 205.0, 10.0)
    R = np.exp(-k_true * t)
    res = fit_kinetics(t, R, cfg, "ideal")
    assert res.status == "OK"
    assert not res.truncated
    assert abs(res.k - k_true) / k_true < 1e-3
    assert res.r2 == pytest.approx(1.0, abs=1e-12)
    assert res.n == int(np.sum((R >= 0.3) & (R <= 1.05)))
    assert res.k_err == pytest.approx(0.0, abs=1e-12)
    assert res.x is not None and res.lnR is not None
    assert res.lnR[0] == pytest.approx(0.0)


def test_fit_kinetics_truncates_on_curved_biexponential(cfg) -> None:
    """双指数（系统性弯曲）→ 自动截断：truncated=True 且记录 pre_k/pre_r2。"""
    t = np.arange(0.0, 305.0, 5.0)
    R = 0.6 * np.exp(-0.05 * t) + 0.4 * np.exp(-0.005 * t)
    res = fit_kinetics(t, R, cfg, "biexp")
    assert res.truncated is True
    assert res.status == "OK"
    assert "auto_truncate" in res.message
    assert "保留 10/14 点" in res.message
    assert np.isfinite(res.pre_k) and np.isfinite(res.pre_r2)
    assert res.n == 10                                     # 14 点 × 0.70 → 10 点
    assert res.k == pytest.approx(0.021220864493452566, rel=1e-6)
    assert res.pre_k == pytest.approx(0.01752163949883565, rel=1e-6)
    assert res.pre_r2 == pytest.approx(0.9639708900392222, rel=1e-6)
    assert res.pre_k != res.k


def test_fit_kinetics_autotruncate_can_be_disabled(cfg) -> None:
    """``auto_truncate=False`` → 不截断，仅报一次全窗口拟合。"""
    t = np.arange(0.0, 305.0, 5.0)
    R = 0.6 * np.exp(-0.05 * t) + 0.4 * np.exp(-0.005 * t)
    kin = {**cfg["kinetics"], "auto_truncate": False}
    res = fit_kinetics(t, R, {**cfg, "kinetics": kin}, "biexp-off")
    assert res.truncated is False
    assert not np.isfinite(res.pre_k)                      # pre_* 只在截断时写入


def test_fit_kinetics_failed_when_too_few_usable_points(cfg) -> None:
    """可用点 <3（或横轴无跨度）→ status=FAILED，k 为 NaN。"""
    res = fit_kinetics(np.array([0.0, 10.0]), np.array([1.0, 0.5]), cfg, "few")
    assert res.status == "FAILED"
    assert "有效点不足" in res.message
    assert res.n == 0 and np.isnan(res.k)

    flat = fit_kinetics(np.array([5.0] * 8), np.linspace(1.0, 0.3, 8), cfg, "flat")
    assert flat.status == "FAILED"

    outside = fit_kinetics(np.arange(0.0, 100.0, 10.0), np.full(10, 0.05), cfg, "low")
    assert outside.status == "FAILED"


def test_fit_kinetics_warns_when_k_nonpositive_or_r2_low(cfg) -> None:
    """k≤0 → WARN；R² < r2_warn → WARN（状态语义）。"""
    t = np.arange(0.0, 105.0, 10.0)
    rising = 0.3 + 0.7 * t / 100.0                         # R 单调上升 → lnR 上斜 → k<0
    res = fit_kinetics(t, rising, cfg, "rising")
    assert res.status == "WARN" and res.k < 0

    rng = np.random.default_rng(0)
    noisy = np.exp(-0.01 * t) * (1.0 + 0.35 * rng.normal(size=t.size))
    noisy = np.clip(noisy, 0.2, 1.2)
    res2 = fit_kinetics(t, noisy, cfg, "noisy")
    assert res2.status != "FAILED"
    assert np.isfinite(res2.k) and res2.r2 <= 1.0 + 1e-12


def test_fit_kinetics_dose_axis_changes_k(cfg) -> None:
    """横轴 ``x``（剂量）与 ``t`` 不同比例时，k 按比例缩放。"""
    t = np.arange(0.0, 205.0, 10.0)
    R = np.exp(-0.01 * t)
    plain = fit_kinetics(t, R, cfg, "t")
    dosed = fit_kinetics(t, R, cfg, "dose", x=t * 0.5)
    assert dosed.k == pytest.approx(2.0 * plain.k, rel=1e-12)


def test_fit_result_defaults() -> None:
    """``FitResult`` 默认值契约（GUI 结果表依赖）。"""
    f = FitResult()
    assert not np.isfinite(f.k) and not np.isfinite(f.k_err)
    assert not np.isfinite(f.r2) and f.n == 0 and f.truncated is False
    assert f.status == "OK"
    assert np.isnan(f.pre_k) and np.isnan(f.pre_r2)


# ═══════════════════════════ exp2 / exp3 ═══════════════════════════
def test_exp2_exp3_numerical_sanity() -> None:
    """``_exp2``/``_exp3`` 的基本数值行为。"""
    t = np.array([0.0, 1.0, 2.0])
    assert np.allclose(_exp2(t, 2.0, 0.0), [2.0, 2.0, 2.0])
    assert np.allclose(_exp2(t, 2.0, 0.5), 2.0 * np.exp(-0.5 * t))
    assert np.allclose(_exp3(t, 2.0, 0.5, 0.3), 2.0 * np.exp(-0.5 * t) + 0.3)
    assert _exp3(np.array([1.0e6]), 2.0, 0.1, 0.3)[0] == pytest.approx(0.3)


def test_fit_series_picks_exp3_only_when_clearly_better() -> None:
    """带常数底的数据 → exp3；纯指数 → exp2（R² 提升不足 0.005 时不选 exp3）。"""
    t = np.arange(0.0, 205.0, 10.0)
    s3 = fit_series(t, 0.5 * np.exp(-0.02 * t) + 0.3)
    assert s3["ok"] and s3["model"] == "exp3"
    assert s3["k"] == pytest.approx(0.02, rel=1e-6)
    assert s3["c"] == pytest.approx(0.3, abs=1e-9)
    assert s3["A0"] == pytest.approx(0.5, rel=1e-9)
    assert s3["R2"] == pytest.approx(1.0, abs=1e-9)

    s2 = fit_series(t, 0.9 * np.exp(-0.02 * t))
    assert s2["ok"] and s2["model"] == "exp2"
    assert s2["k"] == pytest.approx(0.02, rel=1e-3)
    assert s2["c"] == 0.0
    assert s2["A0"] == pytest.approx(0.9, rel=1e-3)


def test_fit_series_guard_conditions() -> None:
    """点数 <4 / 时间无跨度 / 吸光度无变化 → ok=False 并给出 msg。"""
    assert fit_series(np.arange(3.0), np.arange(3.0))["msg"] == "点数<4"
    assert fit_series(np.array([5.0] * 6), np.linspace(1.0, 0.1, 6))["msg"] == "时间无跨度"
    assert fit_series(np.arange(6.0), np.full(6, 0.7))["msg"] == "吸光度无变化"
    r = fit_series(np.arange(3.0), np.arange(3.0))
    assert r["ok"] is False and r["A0"] is None and r["k"] is None


def test_fit_series_ignores_nan_rows() -> None:
    """含 NaN 的行被丢弃后仍可拟合（曲线拟合收敛容差 → k 判据取 0.1%）。"""
    t = np.arange(0.0, 110.0, 10.0)
    A = 0.8 * np.exp(-0.03 * t)
    A[3] = np.nan
    r = fit_series(t, A)
    assert r["ok"] and r["model"] == "exp2"
    assert r["k"] == pytest.approx(0.03, rel=1e-3)
    assert r["A0"] == pytest.approx(0.8, rel=1e-3)
    assert r["R2"] > 0.9999
    assert r["k_err"] is not None and r["k_err"] < 1e-3


def test_fit_series_low_r2_sets_message() -> None:
    """R² 偏低时给出提示信息（仍返回 ok=True）。"""
    t = np.arange(0.0, 110.0, 10.0)
    A = 0.5 + 0.4 * np.sin(t / 7.0)                        # 振荡数据，指数模型必然拟合差
    r = fit_series(t, A)
    assert r["ok"] is True
    assert r["R2"] < 0.9
    assert r["msg"] == "R²偏低, 拟合仅供参考"


# ═══════════════════════════ FR-2 序列汇总 ═══════════════════════════
def _member(name: str, t: float, a_at_probe: float, wl=None) -> dict:
    wl = WL if wl is None else wl
    a = _gauss(379.0, 20.0, 1.0) * a_at_probe
    return {"name": name, "t": float(t), "wl": wl, "ab": a}


def test_group_kinetics_sorts_and_reports_skipped(cfg) -> None:
    """组 → (t 升序, A@探针, 被跳过文件名)；探针无数据者进入 skipped。"""
    bad = {"name": "bad.txt", "t": 5.0, "wl": WL, "ab": np.full(WL.size, np.nan)}
    group = {"label": "demo", "members": [_member("b-20.txt", 20.0, 0.5),
                                          bad,
                                          _member("a-10.txt", 10.0, 1.0)]}
    t, A, skipped = group_kinetics(group, cfg)
    assert t.tolist() == [10.0, 20.0]
    assert skipped == ["bad.txt"]
    assert A[0] == pytest.approx(float(np.mean(
        _gauss(379.0, 20.0, 1.0)[(WL >= 377.0) & (WL <= 381.0)])))
    assert A[1] == pytest.approx(0.5 * A[0], rel=1e-12)


def test_excitation_abs_returns_aexc_and_fraction(cfg) -> None:
    """激发波长处吸光度与吸收占比 ``1-10^(-A)``。"""
    wl = WL
    ab = _gauss(532.0, 20.0, 1.0) * 0.30
    group = {"label": "g", "members": [{"name": "a-0.txt", "t": 0.0,
                                        "wl": wl, "ab": ab}]}
    a, f = excitation_abs(group, cfg)
    assert a == pytest.approx(float(np.mean(ab[(wl >= 530.0) & (wl <= 534.0)])))
    assert f == pytest.approx(1 - 10 ** (-a))

    nan_group = {"label": "g", "members": [{"name": "a-0.txt", "t": 0.0,
                                            "wl": wl,
                                            "ab": np.full(wl.size, np.nan)}]}
    assert excitation_abs(nan_group, cfg) == (None, 0.0)
