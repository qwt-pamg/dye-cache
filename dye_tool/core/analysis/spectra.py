# -*- coding: utf-8 -*-
"""FR-4 模块：紫外/荧光常规作图（设计文档 §4 FR-4）。

> 设计文档说明："紫外荧光作图脚本"未随包提供，FR-4 按 §4 规格新写，
> 接口预留替换（§10 Q1）。

本模块把**纯数值部分**（归一化、λmax、斯托克斯位移、摩尔消光系数）与
**绘图部分**（归一化叠图、吸收+发射镜像图）放在一起，但 matplotlib 仅在
绘图函数内部延迟 import，因此数值函数可在无 matplotlib 环境下单测。

风格与 FR-1~3 一致：复用 ``plotting.apply_jacs_style`` 与 ``_save``。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..inputs.io import Spectrum, load_spectra_files  # noqa: F401  (re-export 便于 UI 单点导入)

log = logging.getLogger("dye_tool.spectra")

__all__ = [
    "normalize", "lambda_max", "stokes_shift", "pair_by_name",
    "molar_extinction", "plot_normalized_overlay", "plot_mirror",
    "plot_spectra_panel", "load_spectra_files",
]


# ═══════════════════════════ 纯数值部分 ═══════════════════════════
def normalize(y: np.ndarray, mode: str = "max") -> np.ndarray:
    """归一化：``max`` → 最大峰归一；``none`` → 原样返回。

    只按有限值的最大值归一（NaN 保持 NaN），避免荧光谱负值区干扰。

    M7：**全负谱**（荧光扣背景后常见）若直接除以"最大值"（= 最接近 0 的那个），
    会把整条谱**翻号**、并让 λmax 指向最深处。这里改用**绝对值峰**归一：全负谱
    保号、形状不变，λmax 仍指向真正的峰位。正谱（含全正、混合）行为一字未变。
    """
    y = np.asarray(y, float)
    if mode in (None, "none", False):
        return y.copy()
    m = np.isfinite(y)
    if not m.any():
        return y.copy()
    peak = float(np.max(y[m]))
    if peak > 0:
        return y / peak
    # 最大值 ≤ 0：用绝对值峰；全零 → 原样返回
    peak_abs = float(np.max(np.abs(y[m])))
    if peak_abs == 0:
        return y.copy()
    return y / peak_abs


def lambda_max(wl: np.ndarray, y: np.ndarray,
               lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    """``[lo, hi]`` 内最大强度对应波长（nm）；无有效点 → NaN。"""
    wl = np.asarray(wl, float)
    y = np.asarray(y, float)
    m = np.isfinite(wl) & np.isfinite(y)
    if lo is not None:
        m &= wl >= float(lo)
    if hi is not None:
        m &= wl <= float(hi)
    if not m.any():
        return float("nan")
    return float(wl[m][int(np.argmax(y[m]))])


def stokes_shift(abs_wl: np.ndarray, abs_y: np.ndarray,
                 em_wl: np.ndarray, em_y: np.ndarray,
                 abs_range: Optional[Sequence[float]] = None,
                 em_range: Optional[Sequence[float]] = None
                 ) -> tuple[float, float, float]:
    """斯托克斯位移 = λ_em(max) − λ_abs(max)。

    返回 ``(λ_abs_max, λ_em_max, 位移)``；任一 λmax 不可得 → 位移为 NaN。
    ``abs_range`` / ``em_range`` 用于限定搜索窗（避免散射峰/二级衍射峰干扰）。
    """
    lo_a, hi_a = (abs_range if abs_range else (None, None))
    lo_e, hi_e = (em_range if em_range else (None, None))
    lam_a = lambda_max(abs_wl, abs_y, lo_a, hi_a)
    lam_e = lambda_max(em_wl, em_y, lo_e, hi_e)
    if not (np.isfinite(lam_a) and np.isfinite(lam_e)):
        return lam_a, lam_e, float("nan")
    return lam_a, lam_e, float(lam_e - lam_a)


def molar_extinction(a: float, c: float, l: float) -> float:
    """摩尔消光系数 ε = A / (c·l)（FR-4 二期接口，先提供纯函数）。

    :param a: 吸光度 A（AU，无量纲）
    :param c: 浓度（mol/L）
    :param l: 光程（cm）
    :return:  ε（L·mol⁻¹·cm⁻¹）；参数非法 → NaN
    """
    try:
        a, c, l = float(a), float(c), float(l)
    except (TypeError, ValueError):
        return float("nan")
    if c <= 0 or l <= 0:
        return float("nan")
    return a / (c * l)


def pair_by_name(spectra: Sequence[Spectrum],
                 keywords: Sequence[str] = ("pbs", "dcm", "dmso", "etoh", "ch3cn")
                 ) -> list[tuple[Spectrum, Spectrum]]:
    """按文件名溶剂关键词把吸收谱与发射谱配对（FR-4 镜像图的便利功能）。

    返回 ``[(abs_spec, em_spec), ...]``；同名关键词无配对则跳过。
    """
    abs_specs = [s for s in spectra if s.ok and s.kind == "abs"]
    em_specs = [s for s in spectra if s.ok and s.kind == "em"]
    pairs: list[tuple[Spectrum, Spectrum]] = []
    used: set[int] = set()
    for a in abs_specs:
        an = a.path.name.lower()
        # M6：原来命中**第一个**关键词就无条件 break，即使那一轮没配到发射谱也放弃，
        # 于是 "dcm_etoh_abs" + "etoh_em" 配不成对（FR-4 auto 模式静默失效）。
        # 现在按关键词顺序继续尝试，直到真正配上或试完所有关键词。
        for k in keywords:
            if k not in an:
                continue
            for e in em_specs:
                if id(e) in used:
                    continue
                if k in e.path.name.lower():
                    pairs.append((a, e))
                    used.add(id(e))
                    break
            else:
                continue                    # 该关键词没配到 → 试下一个关键词
            break                           # 配上了 → 换下一支吸收谱
    if len(pairs) < max(len(abs_specs), 1):
        log.info("按关键词配对: %d 对（吸收 %d 支 / 发射 %d 支）",
                 len(pairs), len(abs_specs), len(em_specs))
    return pairs


# ═══════════════════════════ 绘图部分（FR-4） ═══════════════════════════
def _prep(cfg: dict):
    """统一到 JACS 期刊风格，并返回 ``(pyplot, 导出格式表)``。

    FR-4 是本次迁移新增模块（legacy 无对应出图），因此**固定使用 JACS 单栏风格**：
    3.25 in 宽、四边完整框、刻度朝内、7 pt 刻度、图内无标题。
    """
    from ..render.plotting_jacs import ensure_font
    from ..render.plotting import apply_jacs_style, setup_mpl
    apply_jacs_style()
    ensure_font(cfg)
    scfg = cfg.get("spectra", {})
    return setup_mpl(cfg), scfg.get("export_formats", ["png", "svg"])


def _label_of(s: Spectrum) -> str:
    """曲线标签：优先用文件名主干（GUI 里可读性最好）。"""
    return (s.label or s.path.stem).strip() or s.path.stem


def plot_normalized_overlay(spectra: Sequence[Spectrum], cfg: dict, out_dir: Path,
                            name: str = "spectra_normalized_overlay",
                            xlabel: Optional[str] = None,
                            ylabel: Optional[str] = None,
                            title: Optional[str] = None) -> list[Path]:
    """FR-4-1: 多文件归一化叠图（最大峰归一），标注每曲线 λmax。

    JACS 版式：单栏 3.25 in、无图内标题（``title`` 仅作为图注文字写入
    ``captions.md``）、λmax 用水平短标签标注。
    """
    from ..render.plotting_jacs import finish_axes, jcfg, new_figure, save_jacs
    import matplotlib
    good = [s for s in spectra if s.ok]
    if not good:
        log.warning("FR-4 归一化叠图: 无可用光谱")
        return []
    _plt, fmts = _prep(cfg)
    scfg = cfg.get("spectra", {})
    mode = scfg.get("normalize", "max")
    kinds = {s.kind for s in good}
    xl = xlabel or "Wavelength / nm"
    if ylabel is None:
        ylabel = ("Normalized emission intensity (a.u.)" if kinds == {"em"}
                  else "Normalized absorbance (a.u.)")
    fig, (ax,) = new_figure(cfg, which="fr4_overlay")
    cmap = matplotlib.colormaps["tab10"]
    colors = [cmap(v) for v in np.linspace(0.0, 0.9, max(len(good), 1))]
    caps: list = []
    for s, c in zip(good, colors):
        y = normalize(s.y, mode)
        ax.plot(s.wl, y, lw=float(jcfg(cfg)["curve_lw"]), color=c,
                label=_label_of(s))
        lm = lambda_max(s.wl, y)
        if np.isfinite(lm):
            m = np.isfinite(s.y) & (s.wl == lm)
            yv = float(y[m][0]) if m.any() else float(np.nanmax(y))
            ax.plot([lm], [yv], "o", ms=float(jcfg(cfg)["fr4_marker_ms"]),
                    color=c)
            ax.text(lm, yv + float(jcfg(cfg)["fr4_label_dy"]), f"{lm:.0f}",
                    ha="center", va="bottom",
                    fontsize=float(jcfg(cfg)["annot_size"]), color=c)
    finish_axes(ax, cfg, xlabel=xl, ylabel=ylabel, ylim=(0.0, 1.08))
    ax.legend(loc=str(jcfg(cfg)["fr4_legend_loc_overlay"]),
              fontsize=float(jcfg(cfg)["legend_size"]))
    made = save_jacs(fig, Path(out_dir), name, cfg, formats=fmts)
    if jcfg(cfg).get("captions", True) and made:
        caps.append((name, (title or "Normalized spectra") +
                     ". Curves are normalized to their maximum; open circles mark "
                     "each curve's $\\lambda_{\\max}$ (in nm). "
                     "Legend: " + "; ".join(_label_of(s) for s in good) + "."))
        from ..render.plotting_jacs import write_captions
        write_captions(caps, Path(out_dir))
    return made


def plot_mirror(abs_spec: Spectrum, em_spec: Spectrum, cfg: dict, out_dir: Path,
                name: Optional[str] = None, abs_range: Optional[Sequence[float]] = None,
                em_range: Optional[Sequence[float]] = None) -> tuple[list[Path], tuple[float, float, float]]:
    """FR-4-2: 吸收 + 发射**镜像**归一化叠图，标注斯托克斯位移。

    返回 ``(写出的文件列表, (λ_abs_max, λ_em_max, 位移))``；
    位移数值严格等于 ``λ_em(max) − λ_abs(max)``（验收 A4）。
    """
    from ..render.plotting_jacs import finish_axes, jcfg, new_figure, save_jacs
    scfg = cfg.get("spectra", {})
    if em_range is None:
        em_range = scfg.get("emission_wl_range")
    lam_a, lam_e, shift = stokes_shift(abs_spec.wl, abs_spec.y,
                                       em_spec.wl, em_spec.y, abs_range, em_range)
    ya = normalize(abs_spec.y, "max")
    ye = normalize(em_spec.y, "max")
    c_abs = scfg.get("abs_color", "#1f4e79")
    c_em = scfg.get("em_color", "#C81E1E")
    j = jcfg(cfg)
    fig, (ax,) = new_figure(cfg, which="fr4_mirror")
    ax.plot(abs_spec.wl, ya, lw=float(j["curve_lw"]), color=c_abs,
            label="Absorption" + f"  ({_label_of(abs_spec)})")
    ax.plot(em_spec.wl, -ye, lw=float(j["curve_lw"]), color=c_em,
            label="Emission" + f"  ({_label_of(em_spec)})")
    ax.axhline(0.0, color="0.7", lw=float(j["annot_lw"]) * 0.75)
    _dx = float(j["fr4_label_dx_nm"])
    if np.isfinite(lam_a):
        ax.axvline(lam_a, ls="--", lw=float(j["annot_lw"]), color=c_abs, alpha=0.85)
        ax.text(lam_a - _dx, 1.0, f"{lam_a:.0f}", ha="right", va="bottom",
                fontsize=float(j["annot_size"]), color=c_abs)
    if np.isfinite(lam_e):
        ax.axvline(lam_e, ls="--", lw=float(j["annot_lw"]), color=c_em, alpha=0.85)
        ax.text(lam_e + _dx, 1.0, f"{lam_e:.0f}", ha="left", va="bottom",
                fontsize=float(j["annot_size"]), color=c_em)
    if np.isfinite(shift) and np.isfinite(lam_a) and np.isfinite(lam_e):
        # 位移箭头：从 λ_abs 到 λ_em（水平文字，绝不竖排）
        ax.annotate("", xy=(lam_e, 0.45), xytext=(lam_a, 0.45),
                    arrowprops=dict(arrowstyle="<->", lw=float(j["annot_lw"]),
                                    color="0.25"))
        ax.text(0.5 * (lam_a + lam_e), 0.48, f"Stokes shift = {shift:.0f} nm",
                ha="center", va="bottom", fontsize=float(j["annot_size"]),
                color="0.15")
    finish_axes(ax, cfg, xlabel="Wavelength / nm",
                ylabel="Normalized intensity\n(top: abs / bottom: em)",
                ylim=(-1.12, 1.12))
    ax.legend(loc=str(j["fr4_legend_loc_mirror"]),
              fontsize=float(j["legend_size"]))
    fname = name or f"mirror_{_label_of(abs_spec)}".replace(" ", "_")
    files = save_jacs(fig, Path(out_dir), fname, cfg,
                      formats=scfg.get("export_formats", ["png", "svg"]))
    log.info("斯托克斯位移 = %.1f nm (λ_abs=%.1f, λ_em=%.1f)", shift, lam_a, lam_e)
    if j.get("captions", True) and files:
        from ..render.plotting_jacs import write_captions
        write_captions([(fname, (
            f"Normalized absorption (top) and emission (bottom) spectra of "
            f"{_label_of(abs_spec)} and {_label_of(em_spec)}. "
            f"$\\lambda_{{\\rm abs}}$ = {lam_a:.0f} nm, "
            f"$\\lambda_{{\\rm em}}$ = {lam_e:.0f} nm; the horizontal double arrow "
            f"spans the Stokes shift of {shift:.0f} nm."))], Path(out_dir))
    return files, (lam_a, lam_e, shift)


def plot_spectra_panel(abs_spec: Spectrum, em_spec: Spectrum, cfg: dict,
                       out_dir: Path, name: Optional[str] = None
                       ) -> tuple[list[Path], tuple[float, float, float]]:
    """FR-4-2 的另一版式：吸收与发射**上下两栏**归一化叠放（``mirror_axis=stacked``）。"""
    from ..render.plotting_jacs import finish_axes, jcfg, new_figure, save_jacs
    scfg = cfg.get("spectra", {})
    lam_a, lam_e, shift = stokes_shift(abs_spec.wl, abs_spec.y, em_spec.wl,
                                       em_spec.y, None, scfg.get("emission_wl_range"))
    j = jcfg(cfg)
    fig, (ax1, ax2) = new_figure(cfg, which="fr4_panel", nrows=2)
    _dx = float(j["fr4_label_dx_nm"])
    _ly = float(j["fr4_panel_label_y"])
    ax1.plot(abs_spec.wl, normalize(abs_spec.y, "max"), lw=float(j["curve_lw"]),
             color=scfg.get("abs_color", "#1f4e79"))
    if np.isfinite(lam_a):
        ax1.axvline(lam_a, ls="--", lw=float(j["annot_lw"]),
                    color=scfg.get("abs_color", "#1f4e79"))
        ax1.text(lam_a + _dx, _ly, f"{lam_a:.0f}",
                 fontsize=float(j["annot_size"]), ha="left", va="bottom")
    ax2.plot(em_spec.wl, normalize(em_spec.y, "max"), lw=float(j["curve_lw"]),
             color=scfg.get("em_color", "#C81E1E"))
    if np.isfinite(lam_e):
        ax2.axvline(lam_e, ls="--", lw=float(j["annot_lw"]),
                    color=scfg.get("em_color", "#C81E1E"))
        ax2.text(lam_e + _dx, _ly, f"{lam_e:.0f}",
                 fontsize=float(j["annot_size"]), ha="left", va="bottom")
    finish_axes(ax1, cfg, ylabel="Norm. abs", ylim=(0.0, 1.08))
    finish_axes(ax2, cfg, xlabel="Wavelength / nm", ylabel="Norm. FL",
                ylim=(0.0, 1.08))
    fname = name or f"stacked_{_label_of(abs_spec)}".replace(" ", "_")
    files = save_jacs(fig, Path(out_dir), fname, cfg,
                      formats=scfg.get("export_formats", ["png", "svg"]))
    if j.get("captions", True) and files:
        from ..render.plotting_jacs import write_captions
        write_captions([(fname, (
            f"Normalized absorption (top) and emission (bottom) spectra of "
            f"{_label_of(abs_spec)} and {_label_of(em_spec)}; "
            f"Stokes shift = {shift:.0f} nm "
            f"($\\lambda_{{\\rm abs}}$ = {lam_a:.0f} nm, "
            f"$\\lambda_{{\\rm em}}$ = {lam_e:.0f} nm)."))], Path(out_dir))
    return files, (lam_a, lam_e, shift)
