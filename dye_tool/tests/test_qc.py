# -*- coding: utf-8 -*-
"""``core.qc`` 单元测试：波长轴对齐、饱和判定、去尖峰、基线校正、插值。"""
from __future__ import annotations


import numpy as np
import pytest

from conftest import make_scan
from core.qc import (correct_baseline, despike, flag_negative, flag_saturation,
                     interp_valid, qc_wavelength)

WL = np.arange(300.0, 701.0)


# ═══════════════════════════ 去尖峰 ═══════════════════════════
def test_despike_kills_single_spike_but_keeps_flat_baseline(cfg) -> None:
    """注入单点尖峰 → NaN；平坦基线不受影响。"""
    a = np.full(WL.size, 0.5)
    a[100] = 0.001                                       # 向下尖峰（真实数据的“突然接近 0”）
    a[200] = 5.000                                       # 向上尖峰
    out = despike(a, WL, cfg["qc"], "spike")
    assert np.isnan(out[100]) and np.isnan(out[200])
    assert np.isnan(out).sum() == 2
    keep = ~np.isnan(out)
    assert np.allclose(out[keep], 0.5)


def test_despike_abs_floor_guard_keeps_small_deviation(cfg) -> None:
    """偏离 ≤ ``abs_floor`` 的点即使 MAD=0 也保留（abs_floor 保护）。"""
    a = np.full(WL.size, 0.5)
    a[100] = 0.48                                        # 偏离 0.02 < abs_floor 0.05
    a[200] = 0.54                                        # 偏离 0.04 < abs_floor 0.05
    out = despike(a, WL, cfg["qc"], "floor")
    assert not np.isnan(out).any()
    assert np.allclose(out, a)


def test_despike_threshold_is_abs_floor_plus_mad(cfg) -> None:
    """偏离一旦超过 ``abs_floor`` 且 MAD=0（孤立点）→ 判为尖峰。"""
    a = np.full(WL.size, 0.5)
    a[100] = 0.56                                        # 偏离 0.06 > abs_floor 0.05
    out = despike(a, WL, cfg["qc"], "above-floor")
    assert np.isnan(out[100])
    assert np.isnan(out).sum() == 1


def test_despike_keeps_genuine_steep_absorption_edge(cfg) -> None:
    """真实的陡峭吸收边（连续上升）不得被削：单调信号邻域中位数即中心点。

    这是 ``abs_floor``+MAD 双重保护要守住的核心场景（Soret/荧光团吸收边）。
    """
    edge = np.where(WL < 500.0, 0.02,                        # 无吸收区
                    np.clip(0.02 + 2.0 * (WL - 500.0) / 4.0, 0.0, 1.8))
    out = despike(edge, WL, cfg["qc"], "edge")
    assert not np.isnan(out).any()
    assert np.array_equal(out, edge)
    assert np.nanmax(out) == pytest.approx(1.8)


def test_despike_disabled_returns_copy(cfg) -> None:
    """``outlier.enable=False`` → 原样返回（且是副本，不改入参）。"""
    qc_cfg = {**cfg["qc"], "outlier": {**cfg["qc"]["outlier"], "enable": False}}
    a = np.full(20, 0.5)
    a[10] = 0.0
    out = despike(a, np.arange(300.0, 320.0), qc_cfg, "off")
    assert out is not a
    assert np.array_equal(out, a)


def test_despike_short_window_and_nan_input(cfg) -> None:
    """有效邻域 <3 点或输入本身含 NaN 时安全跳过。"""
    a = np.array([0.5, 0.5, 9.9, np.nan, 0.5, 0.5])
    out = despike(a, np.arange(300.0, 306.0), cfg["qc"], "short")
    assert np.isnan(out[3])                              # 原 NaN 保持
    assert np.isfinite(out[0]) and np.isfinite(out[5])


# ═══════════════════════════ 基线校正 ═══════════════════════════
def test_correct_baseline_removes_known_constant_offset(cfg) -> None:
    """650–700 nm 无吸收窗内的已知常数偏移被扣除，并报告该偏移量。"""
    signal = np.where(WL >= 650.0, 0.0, 0.4 * np.exp(-0.5 * ((WL - 400.0) / 30.0) ** 2))
    offset = 0.05
    a = signal + offset
    corr, base = correct_baseline(WL, a, cfg["baseline"], "offset")
    assert base == pytest.approx(offset, abs=1e-12)
    assert np.allclose(corr, signal, atol=1e-12)
    assert np.nanmax(corr) == pytest.approx(np.max(signal))


def test_correct_baseline_mean_and_linear_methods(cfg) -> None:
    """mean / linear 两种基线方法同样扣除常数偏移。"""
    signal = np.where(WL >= 650.0, 0.0, 1.0)
    a = signal + 0.02
    corr_m, base_m = correct_baseline(
        WL, a, {**cfg["baseline"], "method": "mean"}, "mean")
    assert base_m == pytest.approx(0.02, abs=1e-12)
    assert np.allclose(corr_m, signal, atol=1e-12)

    corr_l, base_l = correct_baseline(
        WL, a, {**cfg["baseline"], "method": "linear"}, "linear")
    assert base_l == pytest.approx(0.02, abs=1e-9)
    assert np.allclose(corr_l, signal, atol=1e-9)


def test_correct_baseline_skips_when_window_has_too_few_points(cfg) -> None:
    """基线窗有效点 <3 → 数据原样返回、基线为 NaN。"""
    wl = np.arange(300.0, 500.0)                         # 完全不覆盖 650–700
    a = np.linspace(0.1, 0.9, wl.size)
    corr, base = correct_baseline(wl, a, cfg["baseline"], "short-window")
    assert corr is a                                     # 未做任何改动
    assert np.isnan(base)

    # 窗口内仅 2 个有效点 → 同样跳过
    wl2 = np.concatenate([np.arange(300.0, 649.0), [650.0, 700.0]])
    a2 = np.concatenate([np.linspace(0.1, 0.2, 349), [0.0, 0.0]])
    corr2, base2 = correct_baseline(wl2, a2, cfg["baseline"], "two-points")
    assert corr2 is a2 and np.isnan(base2)


def test_correct_baseline_disabled(cfg) -> None:
    """``baseline.enable=False`` → 原样返回且基线 0。"""
    a = np.full(WL.size, 0.5)
    off = {**cfg["baseline"], "enable": False}
    corr, base = correct_baseline(WL, a, off, "off")
    assert corr is a and base == 0.0


# ═══════════════════════════ 波长轴对齐 ═══════════════════════════
def test_qc_wavelength_drops_minority_axis_with_reason(cfg) -> None:
    """少数派波长轴整组剔除，原因写入 ``excluded`` 并返回被剔除列表。"""
    good = [make_scan(f"g{i}-{i}.txt", WL, np.full(WL.size, 0.5)) for i in range(4)]
    weird_wl = np.arange(300.0, 701.0, 2.0)               # 201 点
    weird = make_scan("weird-1.txt", weird_wl, np.full(weird_wl.size, 0.5))
    scans = [*good, weird]

    dropped = qc_wavelength(scans, cfg)
    assert len(dropped) == 1 and dropped[0] is weird
    assert weird.excluded
    assert "波长轴与多数文件不一致" in weird.excluded
    assert "201" in weird.excluded and "300.0-700.0" in weird.excluded
    assert all(not s.excluded for s in good)


def test_qc_wavelength_drops_pointwise_mismatch(cfg) -> None:
    """签名相同但逐点对齐超差（>align_tol）的文件被剔除。"""
    ref1 = make_scan("a-1.txt", WL, np.full(WL.size, 0.5))
    ref2 = make_scan("b-1.txt", WL, np.full(WL.size, 0.5))
    shifted = WL.copy()
    shifted[100] += 1.0                                   # > align_tol=0.51
    bad = make_scan("c-1.txt", shifted, np.full(WL.size, 0.5))

    dropped = qc_wavelength([ref1, ref2, bad], cfg)
    assert len(dropped) == 1 and dropped[0] is bad
    assert bad.excluded == "波长轴与参考文件点对点不一致"
    assert not ref1.excluded and not ref2.excluded


def test_qc_wavelength_returns_empty_without_usable_scans(cfg) -> None:
    """全部已剔除 / 无 wl → 返回 []（不抛异常）。"""
    assert qc_wavelength([], cfg) == []
    assert qc_wavelength([make_scan("bad-1.txt", None, None, excluded="坏")], cfg) == []


# ═══════════════════════════ 饱和 / 负值 ═══════════════════════════
def test_flag_saturation_drops_file_above_threshold(cfg) -> None:
    """最大吸光度 > ``qc.saturation``（默认 3.0）→ 剔除并记录原因。"""
    a_hi = np.full(WL.size, 0.5)
    a_hi[379 - 300] = 3.51
    hi = make_scan("hi-1.txt", WL, a_hi)
    lo = make_scan("lo-1.txt", WL, np.full(WL.size, 2.9))
    nan_only = make_scan("nan-1.txt", WL, np.full(WL.size, np.nan))
    dropped = flag_saturation([hi, lo, nan_only], cfg)
    assert len(dropped) == 1 and dropped[0] is hi
    assert not hi.ok and "吸光度饱和" in hi.excluded
    assert "3.51" in hi.excluded
    assert lo.ok and not nan_only.excluded


def test_flag_saturation_respects_config_threshold(cfg) -> None:
    """阈值可配置（``qc.saturation``）。"""
    s = make_scan("s-1.txt", WL, np.full(WL.size, 1.2))
    assert flag_saturation([s], cfg) == []
    dropped = flag_saturation([s], {**cfg, "qc": {**cfg["qc"], "saturation": 1.0}})
    assert len(dropped) == 1 and dropped[0] is s


def test_flag_negative_only_logs_and_keeps_data(cfg) -> None:
    """负吸光度点只记日志，不改数据、不剔除（legacy 行为）。"""
    a = np.full(WL.size, 0.5)
    a[:5] = -0.01
    s = make_scan("neg-1.txt", WL, a)
    flag_negative([s], cfg)
    assert not s.excluded
    assert (s.a < 0).sum() == 5
    # 关闭开关时不报错
    flag_negative([s], {**cfg, "qc": {**cfg["qc"], "flag_negative": False}})


# ═══════════════════════════ 插值 ═══════════════════════════
def test_interp_valid_linear_and_degenerate() -> None:
    """在有效点上线性插值；有效点 <2 → 全 0。"""
    wl = np.array([300.0, 400.0, 500.0])
    a = np.array([0.0, np.nan, 1.0])
    out = interp_valid(wl, a, np.array([350.0, 450.0]))
    assert np.allclose(out, [0.25, 0.75])
    assert np.array_equal(interp_valid(wl, np.array([np.nan, 1.0, np.nan]),
                                       np.array([1.0, 2.0])), [0.0, 0.0])
    # 有效点 (300,0) 与 (500,1) 之间线性插值 → 400 nm 处 = 0.5
    assert interp_valid(wl, a, wl).tolist() == [0.0, 0.5, 1.0]


# ═══════════════ V2.6 工程收尾 D13：>200k 点预筛 ═══════════════
def test_despike_skips_spectra_above_200k_points(caplog) -> None:
    """D13：>200,000 点谱跳过 despike（本机 200k 实测 3.44 s/谱 vs 401 点 8 ms）。

    异常大谱保留原值（含尖峰）并给 WARNING；常规 401 点行为不受影响
    （既有 A2 红线用例覆盖）。
    """
    from core.analysis.qc import MAX_DESPIKE_POINTS

    n = MAX_DESPIKE_POINTS + 1
    wl = np.linspace(300.0, 700.0, n)
    a = np.full(n, 0.5)
    a[n // 2] = 0.001                                    # 若走正常路径会被剔除
    out = despike(a, wl, {"outlier": {"enable": True}}, "big")
    assert np.array_equal(out, a)                        # 原值原样返回
    assert any("跳过 despike" in r.getMessage() for r in caplog.records)
