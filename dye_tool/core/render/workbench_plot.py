# -*- coding: utf-8 -*-
"""V2.9.3 FR-66/67：单线态氧工作台图（独立于 legacy/jacs 渲染路径）。"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

import numpy as np

_TIME_FACTOR = {"s": 1.0, "min": 60.0, "h": 3600.0}
_FORMATS = ("png", "svg", "pdf", "jpg")


def normalize_time_unit(unit: str) -> str:
    u = str(unit or "s").strip().lower()
    return u if u in _TIME_FACTOR else "s"


def normalize_formats(formats: Sequence[str] | str | None) -> tuple[str, ...]:
    if isinstance(formats, str):
        formats = [f for f in formats.replace(";", ",").replace(" ", ",").split(",") if f]
    out: list[str] = []
    for f in formats or ("png",):
        k = str(f).strip().lower().lstrip(".")
        if k == "jpeg":
            k = "jpg"
        if k in _FORMATS and k not in out:
            out.append(k)
    return tuple(out or ("png",))


def _save(fig, base: Path, formats, transparent: bool) -> list[Path]:
    files = []
    for fmt in normalize_formats(formats):
        p = base.with_suffix("." + fmt)
        fig.savefig(p, dpi=160, bbox_inches="tight",
                    facecolor="none" if transparent else "white",
                    transparent=bool(transparent))
        files.append(p)
    return files


def plot_workbench(rows: Sequence[dict], series: dict, out_dir: Path | str, *,
                   style: str = "bar", error_bars: bool = True,
                   time_unit: str = "s", annotate: bool = False,
                   transparent: bool = False, formats: Sequence[str] | str | None = ("png",),
                   ci_band: bool = False) -> list[Path]:
    """画工作台两张图（动力学 R–t + Φ_Δ）。默认形态 = 现有柱状/点+误差棒。"""
    import matplotlib.pyplot as plt

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    style = str(style or "bar").lower()
    if style not in ("bar", "line", "scatter"):
        style = "bar"
    unit = normalize_time_unit(time_unit)
    fac = _TIME_FACTOR[unit]
    files: list[Path] = []

    # 图 1：R–t（原始点 + 拟合线；line 图式画线，柱/散点不画线）
    fig1, ax1 = plt.subplots(figsize=(7.0, 4.4))
    for r in rows:
        s = (series or {}).get(str(r.get("label", ""))) or {}
        t = np.asarray(s.get("t", []), float)
        y = np.asarray(s.get("A", []), float)
        if not t.size:
            continue
        x = t / fac
        if style == "line":
            ax1.plot(x, y, "o-", ms=3.2, lw=1.2, label=str(r["label"]))
            f = s.get("fit") or {}
            if f.get("ok") and f.get("k") is not None:
                tt = np.linspace(float(np.nanmin(t)), float(np.nanmax(t)), 160)
                yy = float(f["A0"]) * np.exp(-float(f["k"]) * tt) + float(f.get("c", 0.0))
                ax1.plot(tt / fac, yy, "--", lw=1.0)
        else:
            ax1.plot(x, y, "o", ms=3.2, label=str(r["label"]))
        if annotate and r.get("k_obs") is not None:
            try:
                ax1.annotate(f"k={float(r['k_obs']):.2e}", (float(np.nanmean(x)), float(np.nanmean(y))),
                             fontsize=7, alpha=0.85)
            except (TypeError, ValueError):
                pass
    ax1.set_xlabel(f"t ({unit})")
    ax1.set_ylabel("A(probe)")
    ax1.set_title("k_obs 拟合叠加")
    if rows:
        ax1.legend(fontsize=7, frameon=False)
    ax1.grid(alpha=0.18)
    files += _save(fig1, out / "workbench_kinetics", formats, transparent)
    plt.close(fig1)

    # 图 2：Φ_Δ（默认柱状；无 Φ 的组跳过）
    valid = [r for r in rows if r.get("phi_delta") is not None and np.isfinite(float(r["phi_delta"]))]
    fig2, ax2 = plt.subplots(figsize=(7.0, 4.2))
    if valid:
        xs = np.arange(len(valid), dtype=float)
        vals = np.array([float(r["phi_delta"]) for r in valid])
        errs = np.array([float(r["phi_err"]) if r.get("phi_err") is not None else np.nan for r in valid])
        labels = [str(r["label"]) for r in valid]
        if style == "bar":
            ax2.bar(xs, vals, yerr=(errs if error_bars else None), capsize=4, width=0.62,
                    color="#3b6ea5", alpha=0.85)
        else:
            if error_bars:
                ax2.errorbar(xs, vals, yerr=errs, fmt="o", capsize=4, color="#3b6ea5")
            else:
                ax2.plot(xs, vals, "o", color="#3b6ea5")
            if style == "line":
                ax2.plot(xs, vals, "-", color="#3b6ea5", lw=1.0, alpha=0.6)
        if ci_band:
            lows = [r.get("phi_ci_low") for r in valid]
            highs = [r.get("phi_ci_high") for r in valid]
            if all(v is not None and np.isfinite(float(v)) for v in lows + highs):
                ax2.fill_between(xs, [float(v) for v in lows], [float(v) for v in highs],
                                 color="#3b6ea5", alpha=0.16, label="bootstrap CI")
                ax2.legend(fontsize=7, frameon=False)
        if annotate:
            for x, v in zip(xs, vals):
                ax2.annotate(f"{v:.3g}", (x, v), textcoords="offset points", xytext=(0, 6),
                             ha="center", fontsize=7)
        ax2.set_xticks(xs)
        ax2.set_xticklabels(labels, rotation=18, ha="right", fontsize=7)
    else:
        ax2.text(0.5, 0.5, "无 Φ_Δ（未指定参比或拟合失败）", ha="center", va="center",
                 transform=ax2.transAxes)
    ax2.set_ylabel("Φ_Δ")
    ax2.set_title("Φ_Δ ± σ")
    ax2.grid(alpha=0.18, axis="y")
    files += _save(fig2, out / "workbench_phi", formats, transparent)
    plt.close(fig2)
    return files
