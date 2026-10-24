# -*- coding: utf-8 -*-
"""``core.spectra`` 单元测试（FR-4）：归一化、λmax、斯托克斯位移、ε、配对与出图。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import SAMPLES_FL, SAMPLES_UVVIS
from core.io import Spectrum, load_spectra_files
from core.spectra import (lambda_max, molar_extinction, normalize, pair_by_name,
                          plot_mirror, plot_normalized_overlay,
                          plot_spectra_panel, stokes_shift)

WL = np.arange(300.0, 701.0)


def _gauss(wl: np.ndarray, mu: float, sig: float, amp: float = 1.0) -> np.ndarray:
    return amp * np.exp(-0.5 * ((wl - mu) / sig) ** 2)


def _spec(name: str, wl: np.ndarray, y: np.ndarray, kind: str = "abs") -> Spectrum:
    return Spectrum(path=Path(name), wl=np.asarray(wl, float),
                    y=np.asarray(y, float), kind=kind, label=Path(name).stem)


# ═══════════════════════════ 纯数值部分 ═══════════════════════════
def test_normalize_max_mode_and_none_mode() -> None:
    """max 模式峰值归一为 1.0；none 模式原样（副本）；全 NaN/零峰不变。"""
    y = np.array([0.0, 0.25, 1.0, np.nan, -0.5])
    out = normalize(y)
    assert np.nanmax(out) == pytest.approx(1.0)
    assert out[1] == pytest.approx(0.25) and out[2] == pytest.approx(1.0)
    assert np.isnan(out[3])

    raw = normalize(y, "none")
    assert raw is not y
    assert np.allclose(raw[:3], y[:3], equal_nan=True)
    assert normalize(y, False) is not y

    all_nan = np.array([np.nan, np.nan])
    out_nan = normalize(all_nan)
    assert out_nan is not all_nan and np.isnan(out_nan).all()
    zeros = normalize(np.zeros(3))
    assert np.array_equal(zeros, np.zeros(3))


def test_lambda_max_respects_search_window() -> None:
    """λmax 取搜索窗内最大值所在波长；窗外全局峰被忽略。"""
    y = _gauss(WL, 600.0, 20.0, 1.0) + _gauss(WL, 520.0, 8.0, 2.0)
    assert lambda_max(WL, y) == pytest.approx(520.0)
    assert lambda_max(WL, y, lo=560.0, hi=650.0) == pytest.approx(600.0)
    assert lambda_max(WL, y, lo=560.0) == pytest.approx(600.0)
    assert lambda_max(WL, y, hi=540.0) == pytest.approx(520.0)
    assert np.isnan(lambda_max(WL, y, lo=800.0, hi=900.0))
    assert np.isnan(lambda_max(WL, np.full(WL.size, np.nan)))


def test_stokes_shift_is_exactly_lambda_em_minus_lambda_abs() -> None:
    """验收 A4：``shift == lam_em - lam_abs``（恒等式，非近似）。"""
    abs_wl = np.arange(300.0, 501.0)
    em_wl = np.arange(500.0, 801.0)
    abs_y = _gauss(abs_wl, 450.0, 25.0)
    em_y = _gauss(em_wl, 600.0, 30.0)

    lam_a, lam_e, shift = stokes_shift(abs_wl, abs_y, em_wl, em_y)
    assert lam_a == 450.0 and lam_e == 600.0
    assert shift == lam_e - lam_a == 150.0                # 恒等，不用 approx

    # 带搜索窗（避开散射/二级衍射峰）
    lam_a2, lam_e2, shift2 = stokes_shift(abs_wl, abs_y, em_wl, em_y,
                                          abs_range=(400.0, 500.0),
                                          em_range=(560.0, 700.0))
    assert shift2 == lam_e2 - lam_a2
    assert lam_a2 == 450.0 and lam_e2 == 600.0

    # 两侧不同网格时仍严格成立
    lam_a3, lam_e3, shift3 = stokes_shift(np.array([400.0, 420.0, 440.0]),
                                          np.array([0.1, 0.9, 0.3]),
                                          np.array([500.0, 555.5, 600.0]),
                                          np.array([0.2, 0.3, 0.8]))
    assert lam_a3 == 420.0 and lam_e3 == 600.0
    assert shift3 == lam_e3 - lam_a3


def test_stokes_shift_nan_when_peak_unavailable() -> None:
    """任一侧 λmax 不可得 → 位移为 NaN。"""
    abs_wl = np.arange(300.0, 501.0)
    em_wl = np.arange(500.0, 801.0)
    lam_a, lam_e, shift = stokes_shift(abs_wl, _gauss(abs_wl, 450.0, 25.0),
                                       em_wl, np.full(em_wl.size, np.nan))
    assert lam_a == 450.0 and np.isnan(lam_e) and np.isnan(shift)


def test_molar_extinction_formula_and_guards() -> None:
    """ε = A/(c·l)；c≤0 或 l≤0 或非数值 → NaN。"""
    assert molar_extinction(1.0, 1.0e-5, 1.0) == pytest.approx(1.0e5)
    assert molar_extinction(0.5, 2.0e-5, 2.0) == pytest.approx(0.5 / 4.0e-5)
    assert np.isnan(molar_extinction(1.0, 0.0, 1.0))       # c≤0 → NaN
    assert np.isnan(molar_extinction(1.0, -1e-5, 1.0))
    assert np.isnan(molar_extinction(1.0, 1e-5, 0.0))
    assert np.isnan(molar_extinction("x", 1e-5, 1.0))


def test_pair_by_name_matches_solvent_keyword() -> None:
    """按文件名溶剂关键词把吸收/发射谱配对；无关键词或无同关键词 → 不配对。"""
    specs = [
        _spec("dye in PBS-1.txt", WL, _gauss(WL, 450.0, 25.0), "abs"),
        _spec("dye in DMSO-1.txt", WL, _gauss(WL, 460.0, 25.0), "abs"),
        _spec("dye in PBS em.TXT", np.arange(500.0, 801.0),
              _gauss(np.arange(500.0, 801.0), 600.0, 30.0), "em"),
        _spec("dye in EtOH em.TXT", np.arange(500.0, 801.0),
              _gauss(np.arange(500.0, 801.0), 610.0, 30.0), "em"),
    ]
    pairs = pair_by_name(specs)
    assert len(pairs) == 1
    a, e = pairs[0]
    assert a.path.name == "dye in PBS-1.txt"
    assert e.path.name == "dye in PBS em.TXT"
    assert pair_by_name([]) == []
    # 只有一个吸收谱、没有发射谱 → 无配对
    assert pair_by_name(specs[:1]) == []


# ═══════════════════════════ 绘图部分 ═══════════════════════════
def test_plot_normalized_overlay_writes_png_svg(tmp_path, cfg) -> None:
    """归一化叠图写出 png+svg（不再出 pdf），文件存在且非空。"""
    paths = sorted(SAMPLES_UVVIS.glob("*.txt"))[:3]
    spectra = load_spectra_files(paths, cfg)
    assert all(s.ok for s in spectra)

    files = plot_normalized_overlay(spectra, cfg, tmp_path)
    assert {p.suffix for p in files} == {".png", ".svg"}
    for p in files:
        assert p.exists() and p.stat().st_size > 0
        assert p.parent == tmp_path
    assert (tmp_path / "spectra_normalized_overlay.png").exists()

    # 无可用光谱 → 空列表且不抛
    assert plot_normalized_overlay([], cfg, tmp_path) == []
    bad = _spec("bad.txt", WL, _gauss(WL, 450.0, 25.0))
    bad.excluded = "读取失败"
    assert plot_normalized_overlay([bad], cfg, tmp_path) == []


def test_plot_mirror_with_real_absorption_and_emission(tmp_path, cfg) -> None:
    """真实吸收+发射文件：返回 (文件, (λ_abs, λ_em, shift))，shift 恒等且文件非空。"""
    abs_sp = load_spectra_files([SAMPLES_UVVIS / "Resorufin-2I-pbs-TEST-1.txt"], cfg)[0]
    em_sp = load_spectra_files([SAMPLES_FL / "5uM in PBS.TXT"], cfg)[0]
    assert abs_sp.kind == "abs" and em_sp.kind == "em"

    files, (lam_a, lam_e, shift) = plot_mirror(abs_sp, em_sp, cfg, tmp_path)
    assert files and all(p.exists() and p.stat().st_size > 0 for p in files)
    assert {p.suffix for p in files} == {".png", ".svg"}
    assert np.isfinite(lam_a) and np.isfinite(lam_e)
    assert shift == lam_e - lam_a                          # A4 恒等式
    assert lam_e > lam_a                                   # 发射峰在吸收峰红侧
    # 与独立调用 stokes_shift 的结果完全一致
    _, _, shift_ref = stokes_shift(abs_sp.wl, abs_sp.y, em_sp.wl, em_sp.y,
                                   None, cfg["spectra"]["emission_wl_range"])
    assert shift == shift_ref


def test_plot_mirror_with_synthetic_pair(tmp_path, cfg) -> None:
    """构造 λabs < λem 的合成谱对：shift 严格等于峰位差。"""
    abs_wl = np.arange(300.0, 551.0)
    em_wl = np.arange(400.0, 801.0)
    abs_sp = _spec("synthetic abs.txt", abs_wl, _gauss(abs_wl, 430.0, 20.0), "abs")
    em_sp = _spec("synthetic em.txt", em_wl, _gauss(em_wl, 560.0, 30.0), "em")

    files, (lam_a, lam_e, shift) = plot_mirror(abs_sp, em_sp, cfg, tmp_path,
                                               name="mirror_synthetic",
                                               abs_range=(380.0, 480.0),
                                               em_range=(500.0, 650.0))
    assert lam_a == 430.0 and lam_e == 560.0
    assert shift == 130.0 == lam_e - lam_a
    assert (tmp_path / "mirror_synthetic.png").exists()
    assert files and all(p.stat().st_size > 0 for p in files)


def test_plot_spectra_panel_stacked_layout(tmp_path, cfg) -> None:
    """上下两栏版式同样返回文件与恒等的斯托克斯位移。"""
    abs_sp = _spec("abs.txt", WL, _gauss(WL, 430.0, 20.0), "abs")
    em_wl = np.arange(400.0, 801.0)
    em_sp = _spec("em.txt", em_wl, _gauss(em_wl, 570.0, 30.0), "em")
    files, (lam_a, lam_e, shift) = plot_spectra_panel(abs_sp, em_sp, cfg, tmp_path,
                                                      name="panel_test")
    assert files and all(p.exists() and p.stat().st_size > 0 for p in files)
    assert shift == lam_e - lam_a
    assert (tmp_path / "panel_test.png").exists()


def test_spectrum_lambda_max_used_by_spectra_lambda_max() -> None:
    """``core.spectra.lambda_max`` 与 ``Spectrum.lambda_max`` 对同一数据结果一致。"""
    y = _gauss(WL, 520.0, 10.0)
    sp = _spec("x.txt", WL, y)
    assert lambda_max(WL, y, 500.0, 540.0) == sp.lambda_max(500.0, 540.0)
