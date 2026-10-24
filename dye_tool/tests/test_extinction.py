# -*- coding: utf-8 -*-
"""``core.extinction`` 单元测试（P6）：Beer–Lambert 浓度系列 ε 计算器。

设计原则：**全部用已知真值回归**。每条用例都先按 ``A = ε·c·l`` 正向合成光谱
（真值 ε 已知），再断言反算结果；不写"看起来对"的断言。数值口径上的外部对照：

* 未加权带截距 → 与 ``scipy.stats.linregress`` 逐项一致（= ``core/kinetics.py`` 的口径）；
* 加权 → 与 ``np.polyfit(c, A, deg, w=np.sqrt(1/A))`` 一致；
* 手算钉住插值（501.5 nm → 0.3 这类必须能人工复算的数）。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

import numpy as np
import pytest
from scipy import stats

from core.config import default_config
from core.extinction import compute_extinction, write_extinction_table
from core.io import Spectrum

#: 合成谱的默认波长轴（1 nm 步长，峰位 520 nm 恰好落在格点上）
WL = np.arange(300.0, 801.0, 1.0)
PEAK = 520.0
SIG = 25.0
#: 真值：已知摩尔消光系数（M⁻¹cm⁻¹）
EPS_TRUE = 25000.0

#: 单位 → mol/L 换算（正向造谱用，只有真值需要它）
_TO_MOL = {"uM": 1e-6, "mM": 1e-3, "M": 1.0, "nM": 1e-9}

#: M9 修复新增的"选点偏差披露"在 issues 里的识别片段。
#: 它是**说明**而非**告警**，因此"有没有告警"的断言要先把它滤掉。
_BIAS_NOTE = "波长是**由数据选出来的**"


def _quality_issues(res: dict) -> list:
    """``issues`` 里**排除 M9 选点偏差披露**后的部分（= 真正的告警/剔除说明）。"""
    return [x for x in (res.get("issues") or []) if _BIAS_NOTE not in x]


# ═══════════════════════════ 合成真值数据 ═══════════════════════════
def _shape(wl, peak: float = PEAK, sig: float = SIG) -> np.ndarray:
    wl = np.asarray(wl, float)
    return np.exp(-0.5 * ((wl - float(peak)) / float(sig)) ** 2)


def _series(concs, unit: str = "uM", eps_true: float = EPS_TRUE, l: float = 1.0,
            wl=WL, peak=PEAK, sig: float = SIG, noise: float = 0.0, seed: int = 20261015,
            offset: float = 0.0, kinds=None, labels=None) -> list:
    """按 ``A = ε·c·l`` 正向合成一组吸收谱（已知真值）。

    :param peak: 峰位（标量或逐谱序列；给序列可造出"逐谱 λmax 不同"的用例）。
    :param noise: 相对高斯噪声幅度（0.01 = 1%，标准差 = noise·|A|）。
    :param offset: A 轴常数偏移（造"带截距"用例）。
    """
    rng = np.random.default_rng(seed)
    to_mol = _TO_MOL[unit]
    out = []
    for i, c in enumerate(concs):
        pk = float(np.asarray(peak, float).ravel()[i]) if np.size(peak) > 1 else float(peak)
        y = eps_true * (float(c) * to_mol) * l * _shape(wl, pk, sig) + offset
        if noise:
            y = y + rng.normal(0.0, noise * np.abs(y), size=np.size(y))
        lab = labels[i] if labels else f"c{i + 1:02d}"
        out.append(Spectrum(path=Path(f"{lab}.txt"), wl=np.asarray(wl, float),
                            y=np.asarray(y, float),
                            kind=(kinds[i] if kinds else "abs"), label=lab))
    return out


def _cfg(concs, **over) -> dict:
    """默认配置 + ``extinction`` 覆盖（不碰 config.py，只改测试内的副本）。"""
    cfg = default_config()
    et = dict(cfg["extinction"])
    et["concentrations"] = list(concs)
    et.update(over)
    cfg["extinction"] = et
    return cfg


def _is_chinese(s: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


# ═══════════════════════════ ① 已知真值回归 ═══════════════════════════
def test_eps_matches_known_truth_exactly() -> None:
    """无噪声：反算 ε 与真值的相对误差应几乎为 0（任务要求 < 1e-6）。"""
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    res = compute_extinction(_series(concs), _cfg(concs))

    assert res["ok"] is True and res["reason"] == ""
    assert res["wavelength"] == PEAK                 # 1 nm 格点上的真峰位，无插值误差
    assert res["wavelength_mode"] == "lambda_max"
    assert res["n_points"] == 5 and res["n_dropped"] == 0 and res["n_total"] == 5

    rel = abs(res["eps"] - EPS_TRUE) / EPS_TRUE
    assert rel < 1e-9                                # 实际 ~1e-16；任务阈值 1e-6
    # 斜率是"换算前"的量：slope = ε·l·(c 单位→M 的因子)
    assert res["slope"] == pytest.approx(EPS_TRUE * 1e-6, rel=1e-12)
    assert res["eps"] == pytest.approx(res["slope"] / 1.0 * 1e6, rel=1e-12)
    # 逐点 A 与解析式 A = ε·c[M]·l 一致
    assert np.allclose(res["a"], [EPS_TRUE * c * 1e-6 for c in concs], rtol=1e-12)
    assert np.array_equal(res["concentrations"], np.asarray(concs))
    assert res["r2"] == pytest.approx(1.0, abs=1e-12)
    assert res["eps_stderr"] < 1e-6                  # 完美线性 → 标准误 ≈ 0
    # M9 修复后，lambda_max 模式**必然**带一条"波长由数据选出 → ε 会系统性偏高"的
    # 说明（那是披露，不是告警）。所以判据是"除该说明外没有任何告警"。
    assert _quality_issues(res) == []


def test_path_length_enters_eps_inversely() -> None:
    """ε = slope/l：A 与 l 同倍放大 → ε 不变；A 不变而 l 翻倍 → ε 恰好减半。"""
    concs = [1.0, 5.0]
    a1 = compute_extinction(_series(concs, l=1.0), _cfg(concs, path_length_cm=1.0))
    a2 = compute_extinction(_series(concs, l=2.0), _cfg(concs, path_length_cm=2.0))
    a3 = compute_extinction(_series(concs, l=1.0), _cfg(concs, path_length_cm=2.0))

    assert a1["eps"] == pytest.approx(EPS_TRUE, rel=1e-9)
    assert a2["eps"] == pytest.approx(EPS_TRUE, rel=1e-9)
    assert a3["eps"] == pytest.approx(a1["eps"] / 2.0, rel=1e-12)


def test_one_percent_noise_recovers_eps_within_5pct() -> None:
    """1% 高斯噪声（固定 seed）：ε 相对误差 < 5%，R² > 0.99。"""
    concs = [1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0]
    res = compute_extinction(_series(concs, noise=0.01, seed=20261015), _cfg(concs))

    assert res["ok"] is True and res["n_points"] == 7
    assert abs(res["eps"] - EPS_TRUE) / EPS_TRUE < 0.05
    assert res["r2"] > 0.99
    assert np.isfinite(res["eps_stderr"]) and res["eps_stderr"] > 0
    # 共同 λmax 恰好落在真峰（格点）上 → 没有系统性偏差，故"误差在统计标准误的若干倍内"
    # 成立；这条同时是"波长取错"的探测器（错 1 个格点就会引入约 0.1% 的系统偏差）。
    assert abs(res["eps"] - EPS_TRUE) < 5.0 * res["eps_stderr"]


# ═══════════════════════════ ② 量纲（µM → M⁻¹cm⁻¹） ═══════════════════════════
def test_uM_converts_to_molar_unit_and_mM_does_not() -> None:
    """conc_unit=uM → ε = A/(c[µM]·l)×1e6、单位 M⁻¹cm⁻¹；mM → 原样、不乘 1e6。"""
    concs_uM = [1.0, 2.0, 5.0, 10.0]
    res_uM = compute_extinction(_series(concs_uM, unit="uM"),
                                _cfg(concs_uM, conc_unit="uM"))
    assert res_uM["eps_unit"] == "M⁻¹cm⁻¹"
    assert res_uM["molar_converted"] is True
    for a_i, c_i in zip(res_uM["a"], concs_uM):          # 逐点核 A/(c·l)·1e6
        assert res_uM["eps"] == pytest.approx(a_i / (c_i * 1.0) * 1e6, rel=1e-12)
    assert not any("未做摩尔换算" in s for s in res_uM["issues"])

    # 同一杯溶液用 mM 表示（数值小 1000 倍）→ 不换算，单位随浓度单位走
    concs_mM = [c / 1000.0 for c in concs_uM]
    res_mM = compute_extinction(_series(concs_mM, unit="mM"),
                                _cfg(concs_mM, conc_unit="mM"))
    assert res_mM["eps_unit"] == "mM⁻¹cm⁻¹"
    assert res_mM["molar_converted"] is False
    for a_i, c_i in zip(res_mM["a"], concs_mM):
        assert res_mM["eps"] == pytest.approx(a_i / (c_i * 1.0), rel=1e-12)
    # 两者的 A 完全一样（同一物理溶液）→ ε 只差"浓度单位换算因子"这一个比例：
    #   报告的 ε_X = A/(c[X]·l) = f_X·ε_molar（f_X = X→mol/L），而 µM 那一支额外乘了 1e6，
    #   所以 ε_µM = ε_mM/1e-3 = 1000·ε_mM，ε_µM = ε_nM/1e-9 = 1e9·ε_nM。
    assert res_uM["a"] == pytest.approx(res_mM["a"], rel=1e-12)
    assert res_uM["eps"] == pytest.approx(res_mM["eps"] / _TO_MOL["mM"], rel=1e-12)
    assert any("未做摩尔换算" in s for s in res_mM["issues"])

    # µM 的两种写法（micro sign / 希腊 mu）等同 uM
    for variant in ("µM", "μM"):
        res_v = compute_extinction(_series(concs_uM, unit="uM"),
                                   _cfg(concs_uM, conc_unit=variant))
        assert res_v["eps_unit"] == "M⁻¹cm⁻¹"
        assert res_v["eps"] == pytest.approx(res_uM["eps"], rel=1e-12)

    # nM 也不许瞎换算：ε 仍是 A/(c[nM]·l)
    concs_nM = [c * 1000.0 for c in concs_uM]
    res_nM = compute_extinction(_series(concs_nM, unit="nM"),
                                _cfg(concs_nM, conc_unit="nM"))
    assert res_nM["eps_unit"] == "nM⁻¹cm⁻¹"
    assert res_nM["eps"] == pytest.approx(res_uM["eps"] / 1e9, rel=1e-12)
    assert res_uM["eps"] == pytest.approx(res_nM["eps"] / _TO_MOL["nM"], rel=1e-12)


# ═══════════════════════════ ③ 过原点 vs 带截距 ═══════════════════════════
def test_force_origin_and_intercept_paths() -> None:
    """两条拟合路径都走一遍；过原点时 intercept 必须**恰好** 0。"""
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]

    res0 = compute_extinction(_series(concs), _cfg(concs, force_origin=True))
    assert res0["intercept"] == 0                    # 精确相等，不是 approx
    assert res0["force_origin"] is True and res0["weighted"] is False
    assert abs(res0["eps"] - EPS_TRUE) / EPS_TRUE < 1e-9
    assert res0["eps"] == pytest.approx(res0["slope"] / 1.0 * 1e6, rel=1e-12)

    # 已知截距 0.03 AU：带截距拟合应把截距与斜率分开，两者都回到真值
    res1 = compute_extinction(_series(concs, offset=0.03),
                              _cfg(concs, force_origin=False))
    assert res1["intercept"] == pytest.approx(0.03, rel=1e-9)
    assert abs(res1["eps"] - EPS_TRUE) / EPS_TRUE < 1e-9
    assert any("force_origin=False" in s for s in res1["issues"])

    # 同一批带截距数据强行过原点 → 斜率被截距推高（这正是 force_origin 的取舍）
    res2 = compute_extinction(_series(concs, offset=0.03),
                              _cfg(concs, force_origin=True))
    assert res2["intercept"] == 0
    assert res2["eps"] > EPS_TRUE * 1.01


def test_intercept_fit_matches_scipy_linregress() -> None:
    """未加权带截距 ⇒ 与 linregress 的 slope/intercept/stderr/r² 逐项一致。"""
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    res = compute_extinction(_series(concs, noise=0.01, seed=99),
                             _cfg(concs, force_origin=False))
    c = np.asarray(res["concentrations"], float)
    a = np.asarray(res["a"], float)
    lr = stats.linregress(c, a)

    assert res["slope"] == pytest.approx(float(lr.slope), rel=1e-10)
    assert res["intercept"] == pytest.approx(float(lr.intercept), rel=1e-9, abs=1e-12)
    assert res["slope_stderr"] == pytest.approx(float(lr.stderr), rel=1e-8)
    assert res["r2"] == pytest.approx(float(lr.rvalue) ** 2, rel=1e-10)


def test_weighted_fit_matches_numpy_polyfit_sqrt_weights() -> None:
    """加权口径 = 最小化 Σ(1/A)·r² ⇔ ``np.polyfit(..., w=np.sqrt(1/A))``。"""
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    specs = _series(concs, noise=0.02, seed=7)

    res_w = compute_extinction(specs, _cfg(concs, weighted=True, force_origin=False))
    assert res_w["ok"] is True and res_w["weighted"] is True
    c = np.asarray(res_w["concentrations"], float)
    a = np.asarray(res_w["a"], float)
    slope_np, inter_np = np.polyfit(c, a, 1, w=np.sqrt(1.0 / a))
    assert res_w["slope"] == pytest.approx(float(slope_np), rel=1e-10)
    assert res_w["intercept"] == pytest.approx(float(inter_np), rel=1e-9, abs=1e-12)
    assert res_w["eps"] == pytest.approx(float(slope_np) * 1e6, rel=1e-10)

    # 过原点 + 加权：闭式解 Σ(w·c·A)/Σ(w·c²)，w = 1/A
    res_o = compute_extinction(specs, _cfg(concs, weighted=True, force_origin=True))
    w = 1.0 / a
    assert res_o["slope"] == pytest.approx(float(np.sum(w * c * a) / np.sum(w * c * c)),
                                           rel=1e-10)
    assert res_o["intercept"] == 0

    # 负向对照：加权确实改变了结果（不加权不是同一个数）
    res_p = compute_extinction(specs, _cfg(concs, weighted=False))
    assert res_p["weighted"] is False
    assert abs(res_p["slope"] - res_o["slope"]) / abs(res_o["slope"]) > 1e-6

    # A ≤ 0 时不能取 1/A 权重 → 自动退回不加权，并在 issues 里说明
    neg = _series(concs)
    neg[0].y = neg[0].y - float(np.max(neg[0].y)) - 0.01      # 该谱出现负 A
    res_n = compute_extinction(neg, _cfg(concs, weighted=True))
    assert res_n["ok"] is True and res_n["weighted"] is False
    assert any("退回不加权" in s for s in res_n["issues"])


# ═══════════════════════════ ④ 共同波长策略 ═══════════════════════════
def test_single_common_wavelength_not_each_spectrum_peak() -> None:
    """所有点都在**同一个**共同波长取 A，且该波长来自均值谱。

    这组合成谱的峰位逐支不同（500/520/540 nm）——即"每支谱各自找 λmax"会得到三个
    不同波长。注意该数据**故意**不满足 Beer–Lambert 的同形状前提，所以这里只断言
    取值口径（结构），不断言 ε 的真值。
    """
    concs = [5.0, 5.0, 5.0]                      # 等浓度 → 均值谱对称，峰位不被高浓度谱拉偏
    peaks = [500.0, 520.0, 540.0]
    specs = _series(concs, peak=peaks, labels=["lo", "mid", "hi"])
    res = compute_extinction(specs, _cfg(concs))

    assert res["ok"] is True
    assert res["wavelength_per_spectrum"] == [500.0, 520.0, 540.0]   # 逐谱峰位确实不同
    assert len(set(res["wavelength_per_spectrum"])) == 3
    # 均值谱（三个对称峰叠加）的峰位 = 520，且工具**只**用这一个波长
    assert res["wavelength_mode"] == "lambda_max"
    assert res["wavelength"] == PEAK
    for sp, pp in zip(specs, res["per_point"]):
        # 每个点的 A 都等于"在该共同波长上"的插值值（独立重算，与实现无关）
        assert pp["a"] == pytest.approx(float(np.interp(res["wavelength"], sp.wl, sp.y)),
                                        rel=1e-12)
    # 负向对照：三支谱里有两支的 A **不是**它自己的峰值（证明没各取各的峰）
    peak_a = [float(np.interp(pk, sp.wl, sp.y)) for pk, sp in zip(peaks, specs)]
    n_not_peak = sum(1 for pp, pa in zip(res["per_point"], peak_a)
                     if abs(pp["a"] - pa) > 1e-9)
    assert n_not_peak == 2

    # 已知取舍（写进模块 docstring）：均值谱按 A 加权，浓度不同时高浓度谱会把共同峰位
    # 往自己那边拉（540 nm 那支浓度最高 → 共同峰位 > 520），但仍是**同一个**波长。
    concs_w = [5.0, 10.0, 20.0]
    specs_w = _series(concs_w, peak=peaks, labels=["lo", "mid", "hi"])
    res_w = compute_extinction(specs_w, _cfg(concs_w))
    assert PEAK < res_w["wavelength"] <= 540.0
    for sp, pp in zip(specs_w, res_w["per_point"]):
        assert pp["a"] == pytest.approx(float(np.interp(res_w["wavelength"], sp.wl, sp.y)),
                                        rel=1e-12)


def test_fixed_wavelength_mode_uses_config_wavelength() -> None:
    """给定 wavelength → 固定波长模式，λmax 搜索窗不再参与取 A。"""
    concs = [1.0, 4.0]
    res = compute_extinction(_series(concs), _cfg(concs, wavelength=600.0))
    assert res["wavelength"] == 600.0 and res["wavelength_mode"] == "fixed"
    # 600 nm 处 A = ε·c·l·exp(-0.5·((600-520)/25)²)（不是峰值处的 A）
    fac = float(_shape(600.0))
    assert res["a"] == pytest.approx([EPS_TRUE * c * 1e-6 * fac for c in concs],
                                     rel=1e-12)
    assert res["eps"] == pytest.approx(res["slope"] / 1.0 * 1e6, rel=1e-12)
    # λmax 本身仍逐谱给出（供核对峰位），但不参与取 A
    assert res["wavelength_per_spectrum"] == [PEAK, PEAK]


# ═══════════════════════════ ⑤ 插值边界（手算钉住） ═══════════════════════════
def test_endpoint_and_between_step_interpolation_hand_checked() -> None:
    """端点取端点值；两步长之间线性插值到**手算**的 0.3/0.25。"""
    wl = np.array([500.0, 501.0, 502.0])
    base = np.array([0.1, 0.2, 0.4])
    concs = [1.0, 2.0]
    specs = [Spectrum(path=Path(f"s{i}.txt"), wl=wl, y=base * c, kind="abs",
                      label=f"s{i}") for i, c in enumerate(concs)]

    # 501.5 = 501 与 502 的中点 → 0.2 + 0.5·(0.4−0.2) = 0.300；A = 0.3·c
    res = compute_extinction(specs, _cfg(concs, wavelength=501.5))
    assert res["ok"] is True and res["n_points"] == 2
    assert res["a"] == pytest.approx([0.3, 0.6], rel=1e-15)
    assert res["slope"] == pytest.approx(0.3, rel=1e-15)             # 手算钉住
    assert res["eps"] == pytest.approx(0.3 * 1e6, rel=1e-15)

    # 恰好落在谱端点 → 取端点自身（不是 NaN）
    res_lo = compute_extinction(specs, _cfg(concs, wavelength=500.0))
    assert res_lo["a"] == pytest.approx([0.1, 0.2], rel=1e-15)
    res_hi = compute_extinction(specs, _cfg(concs, wavelength=502.0))
    assert res_hi["a"] == pytest.approx([0.4, 0.8], rel=1e-15)

    # 0.5 nm 步长 + 非格点目标 = 501.25 → 0.2 + 0.5·(0.3−0.2) = 0.25
    wl2 = np.arange(500.0, 502.0 + 1e-9, 0.5)
    base2 = np.array([0.0, 0.1, 0.2, 0.3, 0.4])
    specs2 = [Spectrum(path=Path(f"t{i}.txt"), wl=wl2, y=base2 * c, kind="abs",
                       label=f"t{i}") for i, c in enumerate(concs)]
    res2 = compute_extinction(specs2, _cfg(concs, wavelength=501.25))
    assert res2["a"] == pytest.approx([0.25, 0.5], rel=1e-15)

    # 超出范围 → 该点 NaN 且被剔除（这里第 3 支谱只覆盖 600–700 nm）
    specs3 = specs + [Spectrum(path=Path("far.txt"), wl=np.arange(600.0, 701.0),
                               y=np.full(101, 0.3), kind="abs", label="far")]
    res3 = compute_extinction(specs3, _cfg([1.0, 2.0, 3.0], wavelength=501.5))
    assert res3["ok"] is True and res3["n_points"] == 2 and res3["n_dropped"] == 1
    assert res3["per_point"][2]["used"] is False
    assert np.isnan(res3["per_point"][2]["a"])
    assert "超出该谱波长范围" in res3["per_point"][2]["note"]
    assert any("未参与拟合" in s for s in res3["issues"])


# ═══════════════════════════ ⑥ 坏输入：ok=False + 中文 reason，无异常 ═══════════════════════════
def test_fail_count_mismatch_returns_chinese_reason() -> None:
    concs = [1.0, 2.0, 5.0]
    res = compute_extinction(_series(concs), _cfg([1.0, 2.0]))
    assert res["ok"] is False
    assert _is_chinese(res["reason"]) and "3" in res["reason"] and "2" in res["reason"]
    assert np.isnan(res["eps"]) and res["n_points"] == 0
    assert res["wavelength_per_spectrum"] == []       # 骨架完整，下游不 KeyError


@pytest.mark.parametrize("bad", [0.0, -3.0, float("nan"), float("inf")])
def test_fail_nonpositive_or_nonfinite_concentration(bad) -> None:
    concs = [1.0, bad, 5.0]
    res = compute_extinction(_series([1.0, 1.0, 5.0]), _cfg(concs))
    assert res["ok"] is False and _is_chinese(res["reason"])
    assert np.isnan(res["eps"])


def test_fail_non_list_concentrations() -> None:
    cfg = default_config()
    cfg["extinction"]["concentrations"] = "1,2,5"        # 字符串而非数字列表
    res = compute_extinction(_series([1.0, 2.0]), cfg)
    assert res["ok"] is False and _is_chinese(res["reason"])
    assert "concentrations" in res["reason"]


def test_fail_fewer_than_two_usable_points() -> None:
    """三支谱里两支只覆盖 300–500 nm → 固定波长 520 nm 只有 1 个有效点。"""
    short = np.arange(300.0, 501.0)
    long_ = WL
    specs = [Spectrum(path=Path("a.txt"), wl=short, y=np.full(short.size, 0.2),
                      kind="abs", label="short-a"),
             Spectrum(path=Path("b.txt"), wl=short, y=np.full(short.size, 0.4),
                      kind="abs", label="short-b"),
             Spectrum(path=Path("c.txt"), wl=long_, y=_shape(long_) * 0.5,
                      kind="abs", label="long")]
    res = compute_extinction(specs, _cfg([1.0, 2.0, 5.0], wavelength=520.0))
    assert res["ok"] is False and _is_chinese(res["reason"])
    assert "2" in res["reason"]
    assert [pp["used"] for pp in res["per_point"]] == [False, False, True]
    assert "超出该谱波长范围" in res["per_point"][0]["note"]


def test_fail_wavelength_out_of_every_spectrum() -> None:
    concs = [1.0, 2.0, 5.0]
    res = compute_extinction(_series(concs), _cfg(concs, wavelength=1000.0))
    assert res["ok"] is False and _is_chinese(res["reason"])
    assert all(not pp["used"] for pp in res["per_point"])
    assert res["a"] == [] and res["concentrations"] == [] and res["n_points"] == 0


def test_fail_lambda_max_window_outside_data() -> None:
    concs = [1.0, 2.0]
    res = compute_extinction(_series(concs), _cfg(concs, lambda_max_range=[900.0, 1000.0]))
    assert res["ok"] is False and _is_chinese(res["reason"])
    assert "λmax" in res["reason"] or "900" in res["reason"]


def test_fail_bad_path_length_and_bad_wavelength_config() -> None:
    concs = [1.0, 2.0]
    specs = _series(concs)
    for over in ({"path_length_cm": 0.0}, {"path_length_cm": -1.0},
                 {"path_length_cm": "abc"}, {"wavelength": "abc"},
                 {"wavelength": -10.0}):
        res = compute_extinction(specs, _cfg(concs, **over))
        assert res["ok"] is False, over
        assert _is_chinese(res["reason"]), over
    # 光程**留空** → 取默认 1.0 cm（能算，不是失败）。
    # L26（评审记录）：代码在 fnum 之前就把 None 换成 1.0，而原注释却说"留空要能被
    # 校验抓住"——两者矛盾。结论是**代码对、注释错**：YAML 里 `path_length_cm:`
    # （留空）与"根本没写这一行"在 safe_load 之后都是 None，无法区分；"写了一行留空"
    # 最自然的语义就是"用默认值"。真正该被抓住的是上面那批**写错的非 None 值**。
    res_ok = compute_extinction(specs, _cfg(concs, path_length_cm=None))
    assert res_ok["ok"] is True and res_ok["path_length_cm"] == 1.0


def test_fail_empty_or_all_nan_or_identical_concentrations() -> None:
    empty = compute_extinction([], _cfg([]))
    assert empty["ok"] is False and _is_chinese(empty["reason"])

    concs = [1.0, 2.0, 5.0]
    all_nan = _series(concs)
    for sp in all_nan:
        sp.y = np.full(sp.y.size, np.nan)
    res_nan = compute_extinction(all_nan, _cfg(concs))
    assert res_nan["ok"] is False and _is_chinese(res_nan["reason"])

    # 浓度全相同：过原点仍**有解**（slope = Σ(cA)/Σc²，只是没有浓度跨度可比），
    # 带截距时法方程奇异 → 明确失败并给中文原因（这才是真正的退化）
    same = _series([5.0, 5.0, 5.0])
    res_o = compute_extinction(same, _cfg([5.0, 5.0, 5.0]))
    assert res_o["ok"] is True
    assert res_o["eps"] == pytest.approx(EPS_TRUE, rel=1e-9)
    res_i = compute_extinction(same, _cfg([5.0, 5.0, 5.0], force_origin=False))
    assert res_i["ok"] is False and _is_chinese(res_i["reason"])


def test_fail_does_not_raise_on_exotic_spectra() -> None:
    """坏对象（None / 长度不符 / 非数值 / 缺字段）一律不抛异常。"""
    concs = [1.0, 2.0, 5.0]
    odd = [None,
           Spectrum(path=Path("m.txt"), wl=np.arange(5.0), y=np.arange(3.0),
                    kind="abs", label="mismatch"),
           Spectrum(path=Path("s.txt"), wl=np.arange(300.0, 401.0),
                    y=np.full(101, 0.1), kind="abs", label="ok")]
    for case in ([None, None, None], odd, [object(), object(), object()]):
        res = compute_extinction(case, _cfg(concs))
        assert res["ok"] is False and _is_chinese(res["reason"])
    # 空 y 的谱单独走一遍：不抛异常
    res = compute_extinction([Spectrum(path=Path("z.txt"), wl=np.array([]),
                                       y=np.array([]), kind="abs")], _cfg([1.0]))
    assert res["ok"] is False and _is_chinese(res["reason"])


# ═══════════════════════════ ⑦ 告警（R² / 单点偏差点名到浓度） ═══════════════════════════
def test_r2_and_point_deviation_warnings_name_the_concentration() -> None:
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    specs = _series(concs)
    specs[-1].y = specs[-1].y * 0.5                  # 20 µM 点人为压低 50%
    res = compute_extinction(specs, _cfg(concs))

    assert res["ok"] is True
    assert res["r2"] < 0.995
    assert any("线性性差" in s and "0.995" in s for s in res["issues"])
    assert any(("相对偏差" in s) and ("20" in s) for s in res["issues"])   # 点名到浓度
    assert res["per_point"][-1]["over"] is True
    assert res["per_point"][-1]["dev"] > 0.15

    # 负向对照：干净数据不产生这两类告警
    clean = compute_extinction(_series(concs), _cfg(concs))
    assert not any("线性性差" in s for s in clean["issues"])
    assert all(not pp["over"] for pp in clean["per_point"])
    assert _quality_issues(clean) == []


def test_dev_warn_frac_and_r2_warn_are_read_from_config() -> None:
    """阈值来自 cfg（不是硬编码）：放宽阈值后同一条数据不再告警。"""
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    specs = _series(concs)
    specs[-1].y = specs[-1].y * 0.5
    strict = compute_extinction(specs, _cfg(concs))
    loose = compute_extinction(specs, _cfg(concs, r2_warn=0.1, dev_warn_frac=10.0))
    assert _quality_issues(strict) and _quality_issues(loose) == []
    assert loose["r2"] == pytest.approx(strict["r2"], rel=1e-12)


def test_lambda_max_mode_discloses_selection_bias() -> None:
    """M9：lambda_max 模式必须**在 issues 里**披露"选点偏差"，fixed 模式不该有。

    评审记录的实测：1% 噪声、5 支谱下 ε 均值 **+0.774%**（sd 0.514%，≈21σ）——
    这是"在均值谱上取 argmax"带来的**选择效应**（选中的波长处各谱噪声同号为正），
    不是计算错误。原先只在 docstring 里写了别的取舍、没写这一条，用户看不到。
    """
    concs = [1.0, 2.0, 5.0, 10.0, 20.0]
    specs = _series(concs, noise=0.01)
    lam = compute_extinction(specs, _cfg(concs))
    assert lam["wavelength_mode"] == "lambda_max"
    assert any(_BIAS_NOTE in s for s in lam["issues"]), \
        "lambda_max 模式应披露选点偏差"

    fixed = compute_extinction(specs, _cfg(concs, wavelength=PEAK))
    assert fixed["wavelength_mode"] == "fixed"
    assert not any(_BIAS_NOTE in s for s in fixed["issues"]), \
        "fixed 模式的波长是外部给定的，没有选点偏差，不该提示"
    # 两条路都必须仍能算出有意义的结果（提示不影响数值）
    assert lam["ok"] and fixed["ok"]
    assert abs(lam["eps"] / fixed["eps"] - 1.0) < 0.05


# ═══════════════════════════ ⑧ 种类过滤与通用对象 ═══════════════════════════
def test_emission_spectra_excluded_and_unknown_kind_accepted() -> None:
    concs = [1.0, 2.0, 5.0]
    res = compute_extinction(_series(concs, kinds=["abs", "em", "abs"]), _cfg(concs))
    assert res["ok"] is True and res["n_points"] == 2 and res["n_dropped"] == 1
    assert res["per_point"][1]["used"] is False
    assert "荧光" in res["per_point"][1]["note"]
    assert any("未参与拟合" in s for s in res["issues"])

    res_em = compute_extinction(_series(concs, kinds=["em"] * 3), _cfg(concs))
    assert res_em["ok"] is False and _is_chinese(res_em["reason"])

    res_unk = compute_extinction(_series(concs, kinds=["unknown"] * 3), _cfg(concs))
    assert res_unk["ok"] is True and res_unk["n_points"] == 3


def test_duck_typed_spectrum_objects_are_accepted() -> None:
    """不要求一定是 core.io.Spectrum：只要暴露 wl/y/kind/ok/label 即可。"""
    class _Sp:
        def __init__(self, name: str, y: np.ndarray) -> None:
            self.wl, self.y = WL, y
            self.kind, self.label, self.ok = "abs", name, True

    concs = [1.0, 5.0]
    specs = [_Sp("a", _shape(WL) * 0.1), _Sp("b", _shape(WL) * 0.5)]
    res = compute_extinction(specs, _cfg(concs))
    assert res["ok"] is True and res["n_points"] == 2
    assert res["labels"] == ["a", "b"]


# ═══════════════════════════ ⑨ CSV 写出 ═══════════════════════════
def test_write_csv_is_utf8_sig_with_summary_and_issues(tmp_path) -> None:
    concs = [1.0, 2.0, 5.0]
    res = compute_extinction(_series(concs), _cfg(concs))
    out = write_extinction_table(res, tmp_path / "sub" / "extinction.csv")

    assert isinstance(out, Path) and out.exists()
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")        # utf-8-sig（Excel 友好）
    rows = list(csv.reader(out.read_text(encoding="utf-8-sig").splitlines()))
    head = rows[0]
    for col in ("波长(nm)", "浓度(uM)", "A", "拟合A", "残差", "相对偏差(%)"):
        assert col in head
    data = [r for r in rows[1:] if len(r) == len(head) and r[0].isdigit()]
    assert len(data) == len(concs)
    assert float(data[0][head.index("A")]) == pytest.approx(res["a"][0], rel=1e-5)
    assert float(data[0][head.index("波长(nm)")]) == pytest.approx(res["wavelength"])
    assert all(r[head.index("参与拟合")] == "是" for r in data)

    summary = {r[0]: r[1] for r in rows if len(r) == 2}
    assert summary["状态"] == "成功"
    assert summary["参与拟合点数"] == str(res["n_points"])
    assert float(summary["ε (M⁻¹cm⁻¹)"]) == pytest.approx(res["eps"], rel=1e-5)
    assert summary["摩尔换算（µM→M ×1e6）"] == "是"
    assert "告警与说明" in [r[0] for r in rows if r]        # 末尾必须带 issues 段


def test_write_csv_for_failed_result_keeps_reason(tmp_path) -> None:
    concs = [1.0, 2.0]
    bad = compute_extinction(_series(concs), _cfg([1.0]))
    out = write_extinction_table(bad, tmp_path / "bad.csv")
    txt = out.read_text(encoding="utf-8-sig")
    assert "失败" in txt and bad["reason"] in txt
    assert "N/A" in txt                                        # ε 等字段写 N/A 而不是崩
