# -*- coding: utf-8 -*-
"""绘图层：JACS 风格图 a–e、JACS 堆叠图、两组对比图、FR-4 常规谱图。

迁移自 legacy ``singlet_o2_phi`` 绘图段与 ``compare_kobs`` 出图段，
统一使用 Agg 后端（GUI 内嵌预览另用 Qt 画布复制这些函数）。

⚠️ 相对 legacy 的**唯一算法外修正**：legacy ``_fig_b_fit`` 中

    xbar, sxx = float(xx.mean()), float(np.sum((xx - xbar) ** 2))

在赋值前引用了 ``xbar``，必然抛 ``UnboundLocalError``，导致 legacy 的
``plot_all`` 在图 b 处崩溃（因此 legacy 历史 run 目录里只有 ``fig_a``）。
本模块按显然意图拆成两步赋值，其余逐行保持不变，使图 b–e 得以产出。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

import numpy as np

# 全局: 图表是否用中文(由字体探测决定)
_ZH = [True]

#: 中文字体候选（legacy 列表原样保留）
ZH_FONTS = ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
            "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS"]

#: **legacy** 风格 rcParams（迁移前原样）。⚠️ 改这里会让 A4.2 的逐像素回归失真。
LEGACY_RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans",
                         "Microsoft YaHei", "SimHei"],
    "font.size": 7.0, "axes.labelsize": 7.5, "axes.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.direction": "out", "ytick.direction": "out",
    "xtick.major.size": 2.5, "ytick.major.size": 2.5,
    "xtick.major.width": 0.6, "ytick.major.width": 0.6,
    "xtick.labelsize": 6.5, "ytick.labelsize": 6.5,
    "legend.frameon": False, "legend.fontsize": 6.5,
    "lines.linewidth": 0.9, "savefig.dpi": 600,
    "pdf.fonttype": 42, "ps.fonttype": 42,
}

#: **JACS 期刊风格** rcParams（设计文档：单栏 82.5 mm / Arial / 刻度朝内 /
#: 四边完整框 / ≥600 dpi / png+pdf）。由 :mod:`core.plotting_jacs` 渲染。
JACS_RC = {
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "font.size": 7.0,
    "axes.labelsize": 7.5,                 # 轴标签 7–9 pt
    "axes.linewidth": 0.9,                 # 坐标轴线宽 0.8–1.0 pt
    "axes.spines.top": True,               # 四边框完整
    "axes.spines.right": True,
    "axes.grid": False,
    "axes.titlelocation": "center",
    "xtick.direction": "in",               # 刻度朝内
    "ytick.direction": "in",
    "xtick.top": True, "ytick.right": True,     # 上下左右都出刻度
    "xtick.major.size": 2.8, "ytick.major.size": 2.8,
    "xtick.minor.size": 1.5, "ytick.minor.size": 1.5,
    "xtick.major.width": 0.8, "ytick.major.width": 0.8,
    "xtick.minor.width": 0.6, "ytick.minor.width": 0.6,
    "xtick.labelsize": 7.0, "ytick.labelsize": 7.0,     # 刻度数字 7 pt
    "legend.frameon": False, "legend.fontsize": 6.5,
    "legend.handlelength": 1.6, "legend.handletextpad": 0.5,
    "legend.borderpad": 0.2, "legend.labelspacing": 0.3,
    "lines.linewidth": 1.3,                # 数据曲线 1.2–1.5 pt
    "lines.markersize": 3.0,
    "errorbar.capsize": 2.0,
    "savefig.dpi": 600,                    # ≥ 600 dpi
    "savefig.bbox": "standard",            # 不 tight 裁剪 → 画布尺寸精确可控
    "savefig.pad_inches": 0.0,
    "pdf.fonttype": 42, "ps.fonttype": 42,  # TrueType 嵌入，PDF 文字可编辑
    "axes.unicode_minus": False,
    # 变量/上下标统一用 mathtext（dejavusans 恒可用，避免 Arial 缺字形）
    "mathtext.fontset": "dejavusans",
    "mathtext.default": "it",
}


def T(zh: str, en: str) -> str:
    """图表文字：有中文字体用中文，否则回退英文（legacy ``T``）。"""
    return zh if _ZH[0] else en


def use_chinese(flag: bool) -> None:
    """手动覆盖中文字体可用标志（测试/CI 用）。"""
    _ZH[0] = bool(flag)


def apply_legacy_style() -> None:
    """切回迁移前的老风格（朝外刻度/仅左下轴/300 dpi 由 cfg 决定）。

    **仅供 A4.2 逐像素回归比对与需要复现旧图时使用**；日常出图走 JACS。
    """
    import matplotlib as mpl
    mpl.rcParams.update(LEGACY_RC)


def apply_jacs_style() -> None:
    """应用 JACS 期刊风格 rcParams（单栏/Arial/刻度朝内/四边完整框/600 dpi）。"""
    import matplotlib as mpl
    mpl.rcParams.update(JACS_RC)


#: 全部受支持的出图风格
PLOT_STYLES = ("jacs", "legacy")


def plot_style(cfg: Optional[dict] = None) -> str:
    """读取 ``cfg['plot']['style']``，非法值回退 ``jacs``。"""
    raw = str(((cfg or {}).get("plot") or {}).get("style", "jacs")).strip().lower()
    return raw if raw in PLOT_STYLES else "jacs"


def apply_plot_style(cfg: Optional[dict] = None) -> str:
    """按 cfg 应用对应风格的 rcParams，返回实际生效的风格名。

    所有出图入口都应调用本函数（而非直接 :func:`apply_jacs_style`），
    这样 ``style=legacy`` 时全套图件都能保持与 legacy 逐像素一致。
    """
    style = plot_style(cfg)
    if style == "legacy":
        apply_legacy_style()
    else:
        apply_jacs_style()
    return style


def setup_mpl(cfg: dict):
    """选 Agg 后端 + 探测中文字体；返回 ``pyplot``。找不到中文字体→英文+WARNING。"""
    import logging
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in ZH_FONTS:
        if name in available:
            matplotlib.rcParams["font.sans-serif"] = [name]
            _ZH[0] = True
            break
    else:
        _ZH[0] = False
        logging.getLogger("dye_tool.plotting").warning(
            "未找到中文字体 → 图表标签改用英文(安装 微软雅黑/SimHei 可恢复中文)")
    matplotlib.rcParams["axes.unicode_minus"] = False
    return plt


def _cmap(plt, name: str):
    """兼容新旧 matplotlib 的 colormap 取用。"""
    import matplotlib
    try:
        return matplotlib.colormaps[name]
    except Exception:                                  # noqa: BLE001
        return plt.get_cmap(name)


def _save(fig, figdir: Path, name: str, cfg: dict,
          formats: Optional[Sequence[str]] = None) -> list[Path]:
    """按 cfg 导出图片（默认 png+svg @dpi=300），返回写出的路径列表。

    V1.2：走 :func:`filter_formats`，即便用户在 config.yaml 里写了 pdf 也不出。
    """
    from .plotstyle import filter_formats
    figdir = Path(figdir)
    figdir.mkdir(parents=True, exist_ok=True)
    exts = filter_formats(formats or cfg["plot"].get("formats", ["svg", "png"]))
    out: list[Path] = []
    for ext in exts:
        p = figdir / f"{name}.{ext}"
        fig.savefig(p, dpi=int(cfg["plot"].get("dpi", 300)), bbox_inches="tight")
        out.append(p)
    fig.clf()
    import matplotlib.pyplot as plt
    plt.close(fig)
    import logging
    logging.getLogger("dye_tool.plotting").info(
        "已保存图: %s.(%s)", name, "|".join(exts))
    return out


def _safe(name: str) -> str:
    """组名 → 文件名安全串。

    评审记录「重复逻辑 #1」点名"文件名清洗三套"。**实测澄清**：三套并不等价——

    * :func:`core.results.safe` 与**本函数**逐字相同（``[^\\w\\-.]+`` → ``_``）；
    * ``core.pipeline._safe`` 是**另一份契约**：额外截断 30 字符、空串回退
      ``"series"``、且保留 ``+ * ?`` 等字符（compare 的 tag 需要）。11 组样本实测
      4 组不同（``''`` / 超长 / NUL / ``+``）——**合并它会改变 compare 的输出文件名**。

    所以这里只消除"真重复"的那一份：改为**委托** :func:`core.results.safe`，
    行为不变（同一正则），但从此只有一处真相。
    """
    from ..report.results import safe as _results_safe
    return _results_safe(name)


def _xvals(e: dict, cfg: dict) -> np.ndarray:
    """该组全部时间点对应的拟合横轴（t/s 或剂量 J/cm²）。"""
    t = np.concatenate([v["t"] for v in e["reps"].values()])
    if bool(cfg["kinetics"].get("dose_normalize", False)):
        return t * float(e.get("irradiance", 1.0)) / 1000.0
    return t


# ═══════════════════════════ 图 a: 光谱叠加 ═══════════════════════════
def _fig_a_spectra(plt, scans, cfg, figdir) -> list[Path]:
    import logging
    log = logging.getLogger("dye_tool.plotting")
    ov = cfg.get("plot", {}).get("spectra_overlay", {})
    by_group: dict = {}
    for s in scans:
        if not s.excluded and s.role != "background":
            by_group.setdefault(s.group, []).append(s)
    if not by_group:
        log.info("图a: 无可用光谱, 跳过")
        return []
    wl_cfg = cfg["wavelength"]
    single = len(by_group) == 1
    made: list[Path] = []
    for g, ss in sorted(by_group.items()):
        ss.sort(key=lambda s: s.t)
        sel = ov.get("select_times", "all")
        if isinstance(sel, (list, tuple)) and sel:
            keep = [float(v) for v in sel]
            ss2 = [s for s in ss if any(abs(s.t - k) < 1e-6 for k in keep)] or ss
        elif isinstance(sel, dict):
            lo, hi = float(sel.get("tmin", -1e18)), float(sel.get("tmax", 1e18))
            ss2 = [s for s in ss if lo <= s.t <= hi] or ss
        else:
            ss2 = ss
        cmap = _cmap(plt, ov.get("cmap", "viridis"))
        ts = np.array([s.t for s in ss2])
        norm = plt.Normalize(vmin=float(ts.min()), vmax=float(ts.max()))
        fig, ax = plt.subplots(figsize=(8.5, 5.5))
        for s in ss2:
            ax.plot(s.wl, s.a, lw=0.9, color=cmap(norm(s.t)))
        sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
        sm.set_array([])
        fig.colorbar(sm, ax=ax).set_label(T("辐照时间 t (s)", "Irradiation time (s)"))
        ymax = max(float(np.nanmax(s.a)) for s in ss2)
        if ov.get("mark_probe", True):                       # ★ ABDA 吸收峰
            lp = float(wl_cfg.get("probe", 379.0))
            win = float(wl_cfg.get("probe_window", 2.0))
            if win > 0:
                ax.axvspan(lp - win, lp + win, color="crimson", alpha=0.08)
            ax.axvline(lp, ls="--", color="crimson", lw=1.2)
            ax.text(lp - 4, ymax * 0.98, T(f"ABDA 吸收峰 {lp:.0f} nm", f"ABDA {lp:.0f} nm"),
                    rotation=90, va="top", ha="right", color="crimson", fontsize=9)
        if ov.get("mark_excitation", True):                  # ★ 激发波长
            le = float(wl_cfg.get("excitation", 532.0))
            ax.axvline(le, ls="--", color="darkorange", lw=1.2)
            ax.text(le - 4, ymax * 0.98, T(f"激发波长 {le:.0f} nm", f"Exc. {le:.0f} nm"),
                    rotation=90, va="top", ha="right", color="darkorange", fontsize=9)
        fp = ov.get("mark_fluorophore", "auto")              # ★ 荧光团最大吸收
        if fp:
            if isinstance(fp, str):
                lo, hi = ov.get("fluorophore_search", [540.0, 620.0])
                s0 = ss2[0]
                m = (s0.wl >= float(lo)) & (s0.wl <= float(hi)) & np.isfinite(s0.a)
                fpv = float(s0.wl[m][int(np.argmax(s0.a[m]))]) if m.any() else None
            else:
                fpv = float(fp)
            if fpv:
                ax.axvline(fpv, ls=":", color="purple", lw=1.4)
                ax.text(fpv + 4, ymax * 0.98,
                        T(f"荧光团最大吸收 {fpv:.0f} nm", f"Fluorophore λmax {fpv:.0f} nm"),
                        rotation=90, va="top", ha="left", color="purple", fontsize=9)
        ax.set_xlabel(T("波长 λ (nm)", "Wavelength (nm)"))
        ax.set_ylabel(T("吸光度 A (AU)", "Absorbance (AU)"))
        ax.set_title((g + " — " if not single else "")
                     + T("紫外-可见吸收光谱随辐照时间的变化(基线校正后)",
                         "UV-vis spectra vs irradiation time"))
        made += _save(fig, figdir,
                      "fig_a_spectra_overlay" if single else f"fig_a_spectra_{_safe(g)}",
                      cfg)
    return made


# ═══════════════════════════ 图 b: 动力学拟合 ═══════════════════════════
def _fig_b_fit(plt, g: str, e: dict, cfg: dict, figdir) -> list[Path]:
    f = e.get("pooled")
    if f is None or f.status == "FAILED" or f.x is None or len(f.x) == 0:
        return []
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    unit = "J⁻¹·cm²" if dose else "s⁻¹"
    fig, ax = plt.subplots(figsize=(7.5, 5))
    x_all = _xvals(e, cfg)
    R_all = np.concatenate([v["R"] for v in e["reps"].values()])
    m = np.isfinite(R_all)
    ax.scatter(x_all[m], np.log(R_all[m]), s=30, facecolor="none", edgecolor="steelblue",
               label=T("数据点 ln(A/A₀)", "ln(A/A0) data"))
    xx, yy = np.asarray(f.x, float), np.asarray(f.lnR, float)
    pred = f.intercept - f.k * xx
    ax.plot(xx, pred, "-", color="crimson", lw=1.8, label=T("准一级拟合", "First-order fit"))
    if f.n > 2:                                              # 95% 置信带
        s = float(np.sqrt(np.sum((yy - pred) ** 2) / (f.n - 2)))
        # ↓↓↓ legacy 此处为 `xbar, sxx = float(xx.mean()), float(np.sum((xx - xbar) ** 2))`
        #     在赋值前引用 xbar → UnboundLocalError。按显然意图拆成两步（本次修正）。
        xbar = float(xx.mean())
        sxx = float(np.sum((xx - xbar) ** 2))
        if sxx > 0:
            se = s * np.sqrt(1.0 / f.n + (xx - xbar) ** 2 / sxx)
            ax.fill_between(xx, pred - 1.96 * se, pred + 1.96 * se, color="crimson",
                            alpha=0.15, label=T("95% 置信带", "95% CI"))
    if f.truncated:                                          # 截断区间标注
        xcut, xmax = float(xx.max()), float(np.nanmax(x_all[m]))
        if xmax > xcut:
            ax.axvspan(xcut, xmax, color="gray", alpha=0.15)
            ax.text(0.5 * (xcut + xmax), float(ax.get_ylim()[0]) * 0.92,
                    T("自动截断\n弃用区间", "auto-truncated"), ha="center",
                    color="dimgray", fontsize=9)
    ax.set_xlabel(T("累计剂量 (J/cm²)", "Dose (J/cm²)") if dose
                  else T("辐照时间 t (s)", "Time (s)"))
    ax.set_ylabel(T("ln( A(t)/A₀ )", "ln(A/A0)"))
    ax.set_title(f"{g} — " + T("准一级动力学拟合", "pseudo-first-order kinetics"))
    ax.text(0.03, 0.05, f"k = {f.k:.4e} ± {f.k_err:.1e} {unit}\nR² = {f.r2:.4f}, n = {f.n}"
            + (T("（已自动截断）", " (truncated)") if f.truncated else ""),
            transform=ax.transAxes, fontsize=9, va="bottom",
            bbox=dict(facecolor="white", edgecolor="0.7", alpha=0.85))
    ax.legend(loc="upper right", fontsize=9)
    return _save(fig, figdir, f"fig_b_kinetics_{_safe(g)}", cfg)


# ═══════════════════════════ 图 c: k 柱状 ═══════════════════════════
def _fig_c_bars(plt, results: dict, cfg: dict, figdir) -> list[Path]:
    items = [(g, e["pooled"]) for g, e in sorted(results.items()) if e.get("pooled")]
    if not items:
        return []
    fig, ax = plt.subplots(figsize=(max(4.5, 1.2 * len(items)), 4.5))
    colors = {"OK": "steelblue", "WARN": "orange", "FAILED": "crimson"}
    for i, (g, f) in enumerate(items):
        if f.status != "FAILED" and np.isfinite(f.k):
            ax.bar(i, f.k, yerr=(f.k_err if np.isfinite(f.k_err) else None),
                   capsize=3, color=colors.get(f.status, "gray"), alpha=0.85)
        else:
            ax.text(i, 0.02, "FAILED", ha="center", fontsize=8, color="crimson")
    ax.set_xticks(np.arange(len(items)))
    ax.set_xticklabels([g for g, _ in items], rotation=30, ha="right", fontsize=9)
    ax.set_ylabel(T("k_obs (s⁻¹)", "k_obs (s⁻¹)"))
    ax.set_title(T("各组准一级速率常数 k_obs", "k_obs by group"))
    return _save(fig, figdir, "fig_c_k_obs_bars", cfg)


# ═══════════════════════════ 图 d: Φ 森林 ═══════════════════════════
def _fig_d_phi(plt, phi: dict, cfg: dict, figdir) -> list[Path]:
    import logging
    log = logging.getLogger("dye_tool.plotting")
    items = [(g, p) for g, p in sorted((phi or {}).items())
             if p.get("status") == "OK" and np.isfinite(p.get("phi", np.nan))]
    if not items:
        log.info("图d: 无有效 Φ_Δ(缺参比组或拟合失败) → 跳过")
        return []
    fig, ax = plt.subplots(figsize=(6.5, max(2.5, 0.9 * len(items) + 1.5)))
    ax.errorbar([p["phi"] for _, p in items], np.arange(len(items)),
                xerr=[float(p.get("phi_err") or 0.0) for _, p in items],
                fmt="o", color="crimson", capsize=3, lw=1.5)
    ax.set_yticks(np.arange(len(items)))
    ax.set_yticklabels([g for g, _ in items])
    phi_ref = float(cfg["quantum_yield"].get("phi_ref", 0.75))
    ax.axvline(phi_ref, ls="--", color="gray", lw=1)
    ax.text(phi_ref, len(items) - 0.4, f" Φ_ref={phi_ref:g}", color="gray", fontsize=9)
    ax.set_xlabel(T("相对量子产率 Φ_Δ", "Relative quantum yield Φ_Δ"))
    ax.set_title(T("Φ_Δ 森林图(误差含 k、A_ex、Φ_ref 传递)", "Φ_Δ forest plot"))
    return _save(fig, figdir, "fig_d_phi_forest", cfg)


# ═══════════════════════════ 图 e: 残差 ═══════════════════════════
def _fig_e_residuals(plt, results: dict, cfg: dict, figdir) -> list[Path]:
    fits = [(g, e["pooled"]) for g, e in sorted(results.items())
            if e.get("pooled") and e["pooled"].status != "FAILED"
            and e["pooled"].x is not None and len(e["pooled"].x)]
    if not fits:
        return []
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    fig, axes = plt.subplots(len(fits), 1, figsize=(7.5, 3.0 * len(fits)), squeeze=False)
    for ax, (g, f) in zip(axes.ravel(), fits):
        xx, yy = np.asarray(f.x, float), np.asarray(f.lnR, float)
        ax.plot(xx, yy - (f.intercept - f.k * xx), "o-", ms=4, lw=0.8,
                color="steelblue", label=T("拟合残差", "residuals"))
        ax.axhline(0, color="k", lw=0.8)
        if f.truncated:                       # 截断弃用点对最终拟合线的残差(红圈)
            x_all = _xvals(results[g], cfg)
            R_all = np.concatenate([v["R"] for v in results[g]["reps"].values()])
            mm = np.isfinite(R_all) & (x_all > float(xx.max()))
            if mm.any():
                ax.plot(x_all[mm], np.log(R_all[mm]) - (f.intercept - f.k * x_all[mm]),
                        "o", mfc="none", mec="crimson",
                        label=T("截断弃用点", "truncated pts"))
        ax.set_title(g, fontsize=10)
        ax.set_ylabel(T("残差", "resid"))
        ax.legend(fontsize=8)
    axes.ravel()[-1].set_xlabel(T("累计剂量 (J/cm²)", "Dose (J/cm²)") if dose
                                else T("辐照时间 t (s)", "Time (s)"))
    fig.tight_layout()
    return _save(fig, figdir, "fig_e_residuals", cfg)


# ═══════════════════════════ JACS 堆叠图 ═══════════════════════════
def jacs_stack_figure(entries: Sequence[tuple], lam: float, figdir: Path,
                      tag: str, formats: Optional[Sequence[str]] = None
                      ) -> list[Path]:
    """``entries: [(t, wl, ab)]`` 按 t 排序 → 迁移前的堆叠图版式（带 colorbar）。

    迁移自 legacy ``jacs_stack_figure``，尺寸/线宽/色标/**savefig 参数**与 legacy
    一致（PNG 逐像素可比，A4.2 的比对对象）。

    V1.2 起不再硬编码 ``("png","pdf")``：经 :func:`filter_formats` 过滤，默认
    只出 png；``formats`` 参数 / ``cfg['plot']['formats']`` 需要矢量时可加 svg。
    """
    import matplotlib.pyplot as plt
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
    from .plotstyle import filter_formats
    if not entries:
        return []
    n = len(entries)
    tmax = max(max(e[0] for e in entries), 1.0)
    norm = Normalize(vmin=0.0, vmax=tmax)
    cmap = plt.cm.viridis
    step = 1.0 / max(n - 1, 1)
    fig, ax = plt.subplots(figsize=(4.0, 2.6))
    for i, (t, wl, ab) in enumerate(entries):
        ax.plot(wl, ab + i * step, lw=0.6, color=cmap(norm(t)))
    ax.axvline(lam, color="0.6", lw=0.6, ls=(0, (4, 3)))
    ax.annotate(f"{lam:.0f} nm", xy=(lam, 1.02),
                xycoords=("data", "axes fraction"),
                ha="center", va="bottom", fontsize=6, color="0.35")
    ax.set_xlabel("Wavelength (nm)")
    ax.set_ylabel("Absorbance (a.u.)")
    sm = ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, pad=0.02)
    cb.set_label("Time (s)", fontsize=7)
    cb.ax.tick_params(labelsize=6, width=0.6, length=2.5)
    cb.outline.set_linewidth(0.6)
    fig.tight_layout(pad=0.4)
    figdir = Path(figdir)
    figdir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    # ⚠ savefig 不带 dpi/bbox —— 与 legacy 完全一致，A4.2 的像素可比性依赖它
    for ext in filter_formats(formats):
        p = figdir / f"jacs_stack_{tag}.{ext}"
        fig.savefig(p)
        out.append(p)
    plt.close(fig)
    return out


def jacs_stack_all(scans, cfg: dict, run_dir: Path) -> list[Path]:
    """每个组一张 JACS 堆叠图 → ``run_dir/figures/``（plot_all 末尾调用）。"""
    import logging
    from ..inputs.io import spectrum_arrays
    log = logging.getLogger("dye_tool.plotting")
    lam = cfg["wavelength"]["probe"]
    by_g: dict = {}
    for s in scans:
        if getattr(s, "excluded", "") or s.role in ("background", "dark"):
            continue
        by_g.setdefault(s.group, []).append(s)
    figdir = Path(run_dir) / "figures"
    made: list[Path] = []
    for g in sorted(by_g):
        arrs = []
        for s in by_g[g]:
            a = spectrum_arrays(s)
            if a is not None:
                arrs.append((float(s.t), a[0], a[1]))
        if len(arrs) < 2:
            continue
        arrs.sort(key=lambda e: e[0])
        made += jacs_stack_figure(arrs, lam, figdir,
                                  _safe(g)[:30] or "group",
                                  formats=cfg.get("plot", {}).get("formats"))
    if made:
        log.info("JACS 堆叠图 x%d -> %s", len(made), figdir)
    return made


# ═══════════════════════════ FR-2 对比图 ═══════════════════════════
def _mnum(v, sig: int = 3) -> str:
    """mathtext 数字格式化（legacy ``compare_kobs._mnum``）。"""
    if v == 0:
        return "0"
    a = abs(v)
    if 1e-3 <= a < 1e4:
        return f"{v:.{sig}g}"
    e = int(np.floor(np.log10(a)))
    return f"{v / 10 ** e:.{sig}g}\\times10^{{{e}}}"


def _equation_mathtext(f: dict) -> str:
    k, A0 = _mnum(f["k"]), _mnum(f["A0"])
    if f["model"] == "exp3":
        return f"$A={A0}\\,e^{{-{k}\\,t}}+{_mnum(f['c'])}$"
    return f"$A={A0}\\,e^{{-{k}\\,t}}$"


def make_compare_figure(s1: dict, f1: dict, s2: dict, f2: dict, ratio,
                        out_dir: Path, lam: float, cfg: Optional[dict] = None,
                        captions: Optional[list] = None) -> list[Path]:
    """两组 kobs 对比图 → png+pdf。

    ``cfg`` 给出且 ``plot.style='jacs'`` 时：单栏 3.25 in、四边完整框、刻度朝内、
    无科学计数法偏移，公式/R²/k₁k₂ 一律移入图注（设计文档：图内不放说明性文字）。
    否则走迁移前的 legacy 版式（3.35×2.7 in，公式内嵌）。
    """
    from ..analysis.kinetics import _exp2, _exp3
    jacs = plot_style(cfg) == "jacs" and cfg is not None
    if jacs:
        from .plotting_jacs import finish_axes, jcfg, new_figure, save_jacs
        j = jcfg(cfg)
        fig, (ax,) = new_figure(cfg, height_in=2.5)
    else:
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(3.35, 2.7))
    cols = ("#000000", "#C81E1E")
    for s, f, c, m in ((s1, f1, cols[0], "o"), (s2, f2, cols[1], "s")):
        ax.plot(s["t"], s["A"], ls="none", marker=m,
                ms=(3.0 if jacs else 3.4), mfc="none", mec=c, mew=0.7,
                label=s["label"])
        if f["ok"]:
            tt = np.linspace(float(np.min(s["t"])), float(np.max(s["t"])), 240)
            yy = (_exp3(tt, f["A0"], f["k"], f["c"]) if f["model"] == "exp3"
                  else _exp2(tt, f["A0"], f["k"]))
            ax.plot(tt, yy, color=c, lw=(float(j["curve_lw"]) if jacs else 1.0))
    ax.set_xlabel("Time / s" if jacs else "Time (s)")
    ax.set_ylabel(f"Absorbance at {lam:.0f} nm")
    ax.legend(loc="upper right", handletextpad=0.4)

    if jacs:
        finish_axes(ax, cfg)
        out_dir = Path(out_dir)
        name = f"compare_{lam:.0f}nm"
        made = save_jacs(fig, out_dir, name, cfg)
        if captions is not None:
            captions.append(_caption_compare(name, s1, f1, s2, f2, ratio, lam))
        return made

    y = 0.16
    for s, f, c in ((s1, f1, cols[0]), (s2, f2, cols[1])):
        if f["ok"]:
            r2 = min(f["R2"], 0.9999)
            ax.text(0.03, y, _equation_mathtext(f) + f"   ($R^2={r2:.4f}$)",
                    transform=ax.transAxes, fontsize=6.3, color=c)
        else:
            ax.text(0.03, y, f"{s['label']}: fit failed",
                    transform=ax.transAxes, fontsize=6.3, color=c)
        y -= 0.075
    if ratio is not None:
        ax.text(0.03, 0.012, f"$k_1/k_2={_mnum(ratio)}$",
                transform=ax.transAxes, fontsize=6.3, color="0.15")

    fig.tight_layout(pad=0.4)
    from .plotstyle import filter_formats
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    # ⚠ savefig 不带 dpi/bbox —— 与 legacy 完全一致（A4.2 的像素可比性依赖它）
    legacy_fmts = (cfg.get("plot", {}).get("formats")
                   if isinstance(cfg, dict) else None)
    for ext in filter_formats(legacy_fmts):
        p = out_dir / f"compare_{lam:.0f}nm.{ext}"
        fig.savefig(p)
        made.append(p)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return made


def _caption_compare(name: str, s1: dict, f1: dict, s2: dict, f2: dict,
                     ratio, lam: float) -> tuple:
    """FR-2 对比图图注（JACS 版式把公式从图内移到这里）。"""
    parts = [f"**{name}**  Decay of the absorbance at {lam:.0f} nm for two groups."]
    for s, f, tag in ((s1, f1, "$k_1$"), (s2, f2, "$k_2$")):
        if f["ok"]:
            r2 = min(f["R2"], 0.9999)
            parts.append(f"*{s['label']}*: {_equation_mathtext(f).strip('$')}, "
                         f"{tag} = {f['k']:.4g} s$^{{-1}}$, $R^2$ = {r2:.4f}.")
        else:
            parts.append(f"*{s['label']}*: fit failed.")
    if ratio is not None:
        parts.append(f"$k_1/k_2$ = {ratio:.4g}.")
    return name, " ".join(parts)


# ═══════════════════════════ 总入口 ═══════════════════════════
def plot_all(scans, results: dict, phi: dict, controls: dict, cfg: dict,
             run_dir: Path, want_paths: bool = False,
             export_mode: str = "final",
             only=None, group_dye: Optional[dict] = None,
             groups=None):
    """画出图 a–e + 堆叠图，按 ``cfg['plot']['style']`` 选择渲染风格。

    * ``jacs``（默认）—— 交 :mod:`core.plotting_jacs` 渲染 JACS 单栏风格，
      并额外写出 ``figures/captions.md`` 图注；
    * ``legacy``      —— 本模块内的迁移前渲染路径，与 legacy 逐像素一致
      （并把绘图参数钉回 legacy 的 dpi/formats，保证回归可比）。

    ``want_paths=True`` 时返回写出的全部路径列表（GUI 预览/导出需要），
    否则返回 ``None``（与 legacy 行为兼容）。

    ``export_mode="preview"``（FR-6.1）只出低 dpi png 到 ``figures_preview/``；
    ``legacy`` 路径不受该参数影响（回归红线）。

    ``only``（P1 懒渲染）只画指定步骤；``groups``（P2 多进程）只画指定组。
    """
    import logging
    log = logging.getLogger("dye_tool.plotting")
    if plot_style(cfg) == "jacs":
        try:
            from .plotting_jacs import plot_all_jacs
            return plot_all_jacs(scans, results, phi, controls, cfg,
                                 run_dir, want_paths=want_paths,
                                 export_mode=export_mode, only=only,
                                 group_dye=group_dye, groups=groups)
        except ImportError:
            log.error("未安装 matplotlib → 跳过绘图(pip install matplotlib)")
            return [] if want_paths else None

    if only or groups:
        # legacy 路径不支持分批/分组渲染，且它必须**整批**画出才是 A4.2 的回归基准
        log.info("legacy 风格不支持分批/分组渲染 → 本次整批出图"
                 "（only=%s groups=%s 已忽略）",
                 "|".join(sorted(only or ())) or "-",
                 "|".join(sorted(groups or ())) or "-")

    # ── legacy 回归路径：钉住 legacy 自己的绘图参数，保证逐像素可比 ──
    cfg = dict(cfg or {})
    cfg["plot"] = {**cfg.get("plot", {}), "dpi": 300, "formats": ["svg", "png"]}
    apply_legacy_style()
    try:
        plt = setup_mpl(cfg)
    except ImportError:
        log.error("未安装 matplotlib → 跳过绘图(pip install matplotlib)")
        return [] if want_paths else None
    figdir = Path(run_dir) / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    # 单张图失败不应中断其余图（legacy 在图 b 崩溃导致 c–e 全丢）
    steps = [
        ("a", lambda: _fig_a_spectra(plt, scans, cfg, figdir)),
        *[("b", (lambda g=g, e=e: _fig_b_fit(plt, g, e, cfg, figdir)))
          for g, e in results.items()],
        ("c", lambda: _fig_c_bars(plt, results, cfg, figdir)),
        ("d", lambda: _fig_d_phi(plt, phi, cfg, figdir)),
        ("e", lambda: _fig_e_residuals(plt, results, cfg, figdir)),
        ("stack", lambda: jacs_stack_all(scans, cfg, run_dir)),
    ]
    for name, fn in steps:
        try:
            made += list(fn() or [])
        except Exception as err:                       # noqa: BLE001
            log.error("图 %s 绘制失败（继续其余图）: %s", name, err)
            import traceback
            log.debug("%s", traceback.format_exc())
    return made if want_paths else None
