# -*- coding: utf-8 -*-
"""V2.6 G 道 FR-43 / A42.3：光漂白动力学（单指数衰减拟合）。

口径（设计文档 §5.7 FR-43 / Q-E22）：

    I(t) = I∞ + (I0 − I∞)·exp(−k·t)     （默认带平台项）
    I(t) = I0·exp(−k·t)                 （plateau=False 退化）

输出 k（min⁻¹）、t½ = ln2/k、t 时刻损失率 %、拟合 R²；时间输入可为
min/s/h（自动换算并标注）。输入模式 1 = 按时间命名的光谱批（取峰/积分），
模式 2 = 两列 CSV（时间, 强度）。

覆盖：
* 合成单指数（已知 k/t½）无噪声 → 拟合精度 / R² / 损失率；
* 平台项开/关行为正确（含无平台数据不退化失败）；
* 时间单位 h/s → min 换算；
* 两列 CSV 导入；模式 1 的 series_from_spectra；
* 退化输入（<4 点/全 NaN/全相同/上升）→ ok=False + 中文原因、不抛。
"""
from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════ 模块/工具 ═══════════════════════
def _mod():
    import core.analysis.photobleach as m
    return m


def _fn(mod, *names):
    for n in names:
        f = getattr(mod, n, None)
        if callable(f):
            return f
    raise AssertionError(
        f"photobleach 缺函数 {names}；现有公开名："
        f"{[n for n in dir(mod) if not n.startswith('_')]}")


def _is_zh(text) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _fit(t, y, **kwargs) -> dict:
    """调用拟合入口（兼容 plateau/with_platform、t_unit/time_unit 两套 kw 名）。"""
    fn = _fn(_mod(), "fit_photobleaching", "fit_photobleach",
             "fit_single_exponential")
    try:
        return fn(t, y, **kwargs)
    except TypeError:
        alt = dict(kwargs)
        if "with_platform" in alt:
            alt["plateau"] = alt.pop("with_platform")
        if "t_unit" in alt:
            alt["time_unit"] = alt.pop("t_unit")
        return fn(t, y, **alt)


def _ok(res) -> bool:
    return bool(res.get("ok")) if isinstance(res, dict) else False


def _pick(res: dict, *keys, default=float("nan")) -> float:
    for key in keys:
        if key in res and res[key] is not None:
            try:
                return float(res[key])
            except (TypeError, ValueError):
                continue
    return default


def _k(res):        # min⁻¹
    return _pick(res, "k_min", "k")


def _t_half(res):   # min
    return _pick(res, "t_half_min", "t_half")


def _reason(res) -> str:
    if not isinstance(res, dict):
        return ""
    return str(res.get("reason") or res.get("error") or res.get("message") or "")


def _loss_pct(res, y0: float, y_last: float) -> None:
    """损失率口径：有 loss_pct 用 loss_pct；否则 loss_frac×100。"""
    want = (float(y0) - float(y_last)) / float(y0) * 100.0
    got = _pick(res, "loss_pct", default=float("nan"))
    if not math.isfinite(got):
        frac = _pick(res, "loss_frac", default=float("nan"))
        got = frac * 100.0 if math.isfinite(frac) else got
    assert got == pytest.approx(want, rel=1e-6, abs=1e-4), (got, want)


def _decay(t, k, i0=100.0, i_inf=0.0):
    t = np.asarray(t, float)
    return i_inf + (i0 - i_inf) * np.exp(-k * t)


# ═══════════════════════ A42.3 拟合精度 ═══════════════════════
def test_a42_3_single_exponential_platform_precision() -> None:
    """A42.3：带平台单指数（已知 k/t½）无噪声 → k/t½/R²/损失率精度。"""
    k_true = 0.05          # min^-1
    i0, i_inf = 100.0, 5.0
    t = np.linspace(0.0, 30.0, 61)
    y = _decay(t, k_true, i0, i_inf)
    res = _fit(t, y, with_platform=True)
    assert _ok(res), f"有效数据拟合失败：{_reason(res)}"
    assert _k(res) == pytest.approx(k_true, rel=1e-6), _k(res)
    assert _t_half(res) == pytest.approx(math.log(2.0) / k_true, rel=1e-6)
    assert _pick(res, "r2") >= 0.999999, _pick(res, "r2")
    assert _pick(res, "i_inf") == pytest.approx(i_inf, abs=1e-6)
    assert _pick(res, "i0") == pytest.approx(i0, rel=1e-6)
    _loss_pct(res, y[0], y[-1])
    assert _pick(res, "n_points") == pytest.approx(len(t))


def test_a42_3_platform_off_and_no_platform_degeneration() -> None:
    """A42.3：无平台数据在 plateau=False 下精确；plateau=True 也不退化失败。"""
    k_true = 0.08
    i0 = 80.0
    t = np.linspace(0.0, 20.0, 41)
    y = _decay(t, k_true, i0, i_inf=0.0)

    off = _fit(t, y, with_platform=False)
    assert _ok(off), f"plateau=False 拟合失败：{_reason(off)}"
    assert _k(off) == pytest.approx(k_true, rel=1e-6)
    assert _pick(off, "i_inf", default=0.0) == pytest.approx(0.0, abs=1e-6)
    _loss_pct(off, y[0], y[-1])

    on_same = _fit(t, y, with_platform=True)
    assert _ok(on_same), f"无平台数据 + plateau=True 退化失败：{_reason(on_same)}"
    assert _k(on_same) == pytest.approx(k_true, rel=1e-3)
    assert abs(_pick(on_same, "i_inf")) < 0.5, _pick(on_same, "i_inf")


def test_a42_3_platform_toggle_changes_fit_correctly() -> None:
    """A42.3：同一带平台数据——开平台精确、关平台产生系统偏差（仍 ok）。"""
    k_true, i0, i_inf = 0.05, 100.0, 8.0
    t = np.linspace(0.0, 40.0, 81)
    y = _decay(t, k_true, i0, i_inf)
    on = _fit(t, y, with_platform=True)
    off = _fit(t, y, with_platform=False)
    assert _ok(on) and _ok(off)
    assert _k(on) == pytest.approx(k_true, rel=1e-6)
    assert _pick(on, "i_inf") == pytest.approx(i_inf, abs=1e-6)
    # 关掉平台项 → 强行把非零平台压进 I0·exp(-kt)，k 必偏（不是"恰好相同"）
    assert abs(_k(off) - k_true) > 1e-3, (
        f"plateau=False 对带平台数据竟给出相同 k={_k(off)}（平台项没生效）")
    assert _pick(on, "r2") >= _pick(off, "r2") - 1e-12


# ═══════════════════════ A42.3 单位换算 ═══════════════════════
def test_a42_3_time_unit_conversion_to_minutes() -> None:
    """A42.3：输入单位 s / h 自动换算成 min⁻¹ 并在结果标注。"""
    k_true = 0.05                           # min^-1
    t_min = np.linspace(0.0, 30.0, 61)
    y = _decay(t_min, k_true, 100.0, 5.0)

    res_s = _fit(t_min * 60.0, y, with_platform=True, t_unit="s")
    assert _ok(res_s), _reason(res_s)
    assert _k(res_s) == pytest.approx(k_true, rel=1e-6)
    assert str(res_s.get("unit_used", "min")) == "min"
    assert str(res_s.get("unit_in", "")).lower() in ("s", "sec")

    res_h = _fit(t_min / 60.0, y, with_platform=True, t_unit="h")
    assert _ok(res_h), _reason(res_h)
    assert _k(res_h) == pytest.approx(k_true, rel=1e-6)
    assert str(res_h.get("unit_used", "min")) == "min"
    assert str(res_h.get("unit_in", "")).lower() in ("h", "hr")

    # 默认单位 = min
    res_default = _fit(t_min, y, with_platform=True)
    assert _k(res_default) == pytest.approx(k_true, rel=1e-6)
    assert str(res_default.get("unit_used", "min")) == "min"


# ═══════════════════════ A42.3 输入模式 ═══════════════════════
def test_a42_3_csv_import_and_spectra_series(tmp_path) -> None:
    """A42.3：两列 CSV 导入（逗号/制表符）与模式 1 的 series_from_spectra。"""
    read_csv = _fn(_mod(), "read_two_column_csv", "load_decay_csv",
                   "load_intensity_csv")
    from_spectra = _fn(_mod(), "series_from_spectra", "from_spectra")
    t = np.linspace(0.0, 10.0, 11)
    y = _decay(t, 0.1, 100.0, 1.0)

    # 逗号 + 表头
    p1 = tmp_path / "decay_comma.csv"
    p1.write_text("time,intensity\n" + "\n".join(
        f"{a:.6f},{b:.6f}" for a, b in zip(t, y)) + "\n", encoding="utf-8")
    got_t, got_y = read_csv(p1)
    assert np.allclose(got_t, t) and np.allclose(got_y, y)

    # 制表符（成像软件常见导出）
    p2 = tmp_path / "decay_tab.csv"
    p2.write_text("\n".join(f"{a:.6f}\t{b:.6f}" for a, b in zip(t, y)) + "\n",
                  encoding="utf-8")
    got_t2, got_y2 = read_csv(p2)
    assert np.allclose(got_t2, t) and np.allclose(got_y2, y)

    # 读不了的坏文件 → ValueError(中文)，不静默空表
    p3 = tmp_path / "bad.csv"
    p3.write_text("not a table\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        read_csv(p3)
    assert _is_zh(str(exc.value)), f"坏 CSV 缺中文原因：{exc.value!r}"

    # 模式 1：光谱序列 → (t, y)；峰强度与积分强度双口径
    peak0 = 0.9
    specs = []
    for i, tt in enumerate((0.0, 60.0, 120.0, 180.0)):
        amp = peak0 * (0.8 ** i)
        wl = np.arange(450.0, 551.0, 1.0)
        yy = amp * np.exp(-0.5 * ((wl - 500.0) / 20.0) ** 2)
        specs.append(SimpleNamespace(wl=wl, y=yy, a=yy, t=float(tt),
                                     path=Path(f"s-{tt:.0f}.txt"), ok=True))
    got_t3, got_y3 = from_spectra(specs, mode="peak_abs")
    assert np.allclose(got_t3, [0.0, 60.0, 120.0, 180.0])
    assert np.allclose(got_y3, [s.y.max() for s in specs], rtol=1e-12)

    got_t4, got_y4 = from_spectra(specs, mode="integral")
    assert np.allclose(got_t4, got_t3)
    want_int = [float(np.trapezoid(s.y, s.wl)) for s in specs]
    assert np.allclose(got_y4, want_int, rtol=1e-12)


# ═══════════════════════ A42.3 退化输入 ═══════════════════════
def test_a42_3_degenerate_inputs_return_chinese_reason_without_raise() -> None:
    """A42.3：<4 点/全 NaN/全相同/整体上升 → ok=False + 中文原因、不抛。"""
    t_full = np.linspace(0.0, 10.0, 21)
    cases = {
        "too_few_points": (t_full[:3], _decay(t_full[:3], 0.1)),
        "all_nan": (t_full, np.full(t_full.size, np.nan)),
        "all_identical": (t_full, np.full(t_full.size, 50.0)),
        "increasing": (t_full, _decay(t_full, -0.1)),   # 整体上升，非衰减
    }
    failures = []
    for name, (tt, yy) in cases.items():
        try:
            res = _fit(tt, yy, with_platform=True)
        except Exception as err:                      # noqa: BLE001
            failures.append(f"{name}: 抛异常 {type(err).__name__}: {err}")
            continue
        if _ok(res):
            failures.append(f"{name}: 竟返回 ok=True（应拒绝）")
        elif not _is_zh(_reason(res)):
            failures.append(f"{name}: 缺中文原因 {_reason(res)!r}")
    assert not failures, "退化输入处理失败：" + "；".join(failures)
