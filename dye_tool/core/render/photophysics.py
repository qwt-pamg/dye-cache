# -*- coding: utf-8 -*-
"""V2.6 G 道 FR-45：论文风格新图型（光谱演化 / 衰减曲线 / 分组柱状）。

边界（设计文档 §5.7 FR-45 / §7.5 A42.5 / §10 Q-E20）：

* **只新增图型**：不调用、不修改 legacy JACS fig a–e 的渲染函数与 rcParams，
  也不碰任何数据/数值路径（A42.6 红线）；legacy 渲染仍走
  core.render.plotting_jacs 原样。
* 样式从 core.render.plotstyle 的 JACS_SCHEMA 派生（Arial / 单栏宽 /
  600 dpi / 轴标签字号与线宽 / 内向刻度 / 四边框 / 无网格），但**不更新全局
  rcParams**：每个图用 matplotlib.rc_context 局部生效，逐轴显式设置，避免
  污染 legacy 渲染（A4.2 逐像素回归）。
* 面板字母用论文式**裸粗体小写 a/b/c**（左上外侧），文本/绘制单一来源在
  core.render.layout 的 panel_label_text / apply_panel_label（Q-E20；
  默认 bare，传 paren 则回退 (a)）。
* 输出格式一律过 core.render.plotstyle.filter_formats（零 PDF）；
  plot_* 返回写出的主文件 Path（PNG），dpi 取 JACS 参数（默认 600）。

三类图型（每类 = build_* 纯函数 + plot_* 导出函数）：

* 光谱演化 plot_spectral_evolution：时间序蓝系渐变细线 + 首末包络
  填充 + t=0 红色高亮 + hν 方向箭头；
* 衰减曲线 plot_decay_curve：散点 + 拟合线，k/t½ 内联图例；
* 分组柱状 plot_grouped_bars：双色 + 黑色误差棒 + 无网格 + 旋转标签。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

log = logging.getLogger("dye_tool.photophysics")

__all__ = [
    "SERIES_COLORS", "T0_COLOR",
    "build_spectral_evolution_figure", "build_decay_figure",
    "build_grouped_bars_figure",
    "plot_spectral_evolution", "plot_decay_curve", "plot_grouped_bars",
]

#: 论文双色（蓝 / 粉紫；设计文档 §2.7 取证）。可用 series 的 color 覆盖。
SERIES_COLORS = ("#2E5EAA", "#C77BB4")

#: t=0 高亮色（演化图第一帧；论文里的红色起点曲线）。
T0_COLOR = "#C81E1E"

#: 拟合线色（与 t=0 红区分开）。
FIT_COLOR = "#1F4E79"

#: 三类图型默认画布高度（英寸；宽取 JACS_SCHEMA 的 width_in）。
_HEIGHTS = {"evolution": 2.5, "decay": 2.5, "bars": 2.8}


# ═══════════════════════ 样式（JACS_SCHEMA 派生） ═══════════════════════
def _jcs(cfg: Optional[dict]) -> dict:
    """取 JACS_SCHEMA 派生的绘图参数（缺项补默认；零新配置键）。"""
    from .plotting_jacs import jcfg
    return jcfg(cfg)


def _plt():
    """Agg 后端下的 pyplot（延迟导入，避免 import 期绑定后端）。"""
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt
    return plt


def _paper_rc(j: dict) -> dict:
    """局部 rc_context：字体 / mathtext / 减号（不写全局 rcParams）。"""
    family = [str(j.get("font", "Arial"))]
    family += [str(x) for x in (j.get("font_fallback") or [])
               if str(x) not in family]
    return {
        "font.family": "sans-serif",
        "font.sans-serif": family,
        "mathtext.fontset": "dejavusans",
        "mathtext.default": "it",
        "axes.unicode_minus": False,
        "savefig.dpi": float(j.get("dpi", 600)),
    }


def _new_paper_figure(j: dict, kind: str):
    """固定单栏宽 + 图型默认高的画布（白底、Agg）。"""
    plt = _plt()
    width = float(j.get("width_in", 3.25))
    height = float(_HEIGHTS.get(kind, 2.5))
    fig, ax = plt.subplots(figsize=(width, height),
                           dpi=float(j.get("dpi", 600)))
    fig.patch.set_facecolor("white")
    return fig, ax


def _style_axes(ax, j: dict) -> None:
    """JACS_SCHEMA 派生的论文坐标轴：内向刻度（主+次）/ 四边框 / 无网格 /
    轴标签粗体；不碰全局 rcParams。"""
    ax.grid(False)
    ax.minorticks_on()
    major_len = float(j.get("tick_length", 2.8))
    major_w = float(j.get("tick_width", 0.8))
    ax.tick_params(axis="both", which="major", direction="in",
                   top=True, right=True, length=major_len, width=major_w,
                   labelsize=float(j.get("tick_size", 7.0)), color="black")
    ax.tick_params(axis="both", which="minor", direction="in",
                   top=True, right=True,
                   length=major_len * float(j.get("minor_len_frac", 0.55)),
                   width=major_w * float(j.get("minor_w_frac", 0.75)),
                   color="black")
    for spine in ax.spines.values():
        spine.set_visible(True)
        spine.set_linewidth(float(j.get("spine_width", 0.9)))
        spine.set_color("black")
    for axis in (ax.xaxis, ax.yaxis):
        axis.label.set_fontsize(float(j.get("label_size", 7.5)))
        axis.label.set_fontweight("bold")
        axis.label.set_color("black")


def _draw_panel_label(ax, panel_label, style: str, j: dict) -> None:
    """画论文式面板字母（bare=裸粗体小写；paren=(a) 兼容）。"""
    if panel_label is None:
        return
    from .layout import apply_panel_label
    apply_panel_label(ax, panel_label, style=str(style or "bare"),
                      fontsize=float(j.get("label_size", 7.5)) + 2.0)


def _save_paper(fig, out_path, j: dict,
                formats: Optional[Sequence[str]] = None) -> Path:
    """按 filter_formats 写盘（零 PDF），返回主文件（优先 PNG）路径。"""
    from .plotstyle import filter_formats

    p = Path(out_path)
    if p.suffix.lower() not in (".png", ".svg"):
        p = p.with_suffix(".png")
    p.parent.mkdir(parents=True, exist_ok=True)
    fmts = filter_formats(formats if formats is not None
                          else (j.get("formats") or ["png"]))
    stem = p.with_suffix("")
    made: list[Path] = []
    for ext in fmts:
        q = stem.with_suffix("." + ext)
        fig.savefig(q, dpi=float(j.get("dpi", 600)),
                    facecolor="white", edgecolor="none", bbox_inches=None)
        made.append(q)
    primary = stem.with_suffix(".png") if "png" in fmts else made[0]
    log.info("论文风格图型已写出：%s（%s）", primary.name, "/".join(fmts))
    return Path(primary)


# ═══════════════════════ 数据归一化 ═══════════════════════
def _is_scan(obj) -> bool:
    """像 Scan/Spectrum 一样带 wl/a 两列数组。"""
    return hasattr(obj, "wl") and hasattr(obj, "a")


def _as_xy(obj) -> tuple:
    """把 (x, y) / dict / Scan 统一成两个等长有限值数组。"""
    if isinstance(obj, dict):
        x = obj.get("x", obj.get("wl"))
        y = obj.get("y", obj.get("a"))
    elif _is_scan(obj):
        x, y = obj.wl, obj.a
    elif isinstance(obj, (list, tuple, np.ndarray)) and len(obj) == 2:
        x, y = obj[0], obj[1]
    else:
        raise ValueError(
            f"无法识别的光谱数据：{type(obj).__name__}（需要 (x, y) / dict / Scan）")
    try:
        xa = np.asarray(x, float).ravel()
        ya = np.asarray(y, float).ravel()
    except (TypeError, ValueError) as err:
        raise ValueError(f"光谱数据不是数值数组：{err}") from err
    if xa.size == 0 or xa.size != ya.size:
        raise ValueError(f"光谱 x/y 长度不匹配或为空：x={xa.size} y={ya.size}")
    m = np.isfinite(xa) & np.isfinite(ya)
    if not bool(m.any()):
        raise ValueError("光谱数据全为 NaN/inf，无法作图")
    return xa[m], ya[m]


def _is_frames_pair(frames) -> bool:
    """(times, spectra) 双元组判定（避免把"两帧 dict"误判成时间序列）。"""
    if not (isinstance(frames, tuple) and len(frames) == 2):
        return False
    times, spectra = frames
    if isinstance(times, dict) or isinstance(spectra, dict) or _is_scan(times):
        return False
    try:
        return len(times) > 0 and len(spectra) > 0
    except TypeError:
        return False


def _normalize_frames(frames) -> list:
    """光谱演化输入 -> [{x, y, t, label, index}]（乱序/缺标签都给中文原因）。"""
    if frames is None:
        raise ValueError("光谱演化图没有输入数据")
    out: list[dict] = []
    if _is_frames_pair(frames):
        times, spectra = frames
        for i, (t, spec) in enumerate(zip(list(times), list(spectra))):
            xa, ya = _as_xy(spec)
            try:
                tv = float(t)
            except (TypeError, ValueError):
                raise ValueError(f"第 {i + 1} 帧的时间不是数值：{t!r}") from None
            out.append({"x": xa, "y": ya, "t": tv,
                        "label": f"{tv:g} s", "index": i})
    else:
        try:
            raw = list(frames)
        except TypeError:
            raise ValueError("光谱演化图输入必须是序列") from None
        if not raw:
            raise ValueError("光谱演化图没有输入数据")
        for i, item in enumerate(raw):
            if isinstance(item, dict):
                xa, ya = _as_xy(item)
                tv = item.get("t")
                label = item.get("label")
            elif _is_scan(item):
                xa, ya = _as_xy(item)
                tv = getattr(item, "t", None)
                label = None
            else:
                xa, ya = _as_xy(item)
                tv, label = None, None
            if tv is not None:
                try:
                    tv = float(tv)
                except (TypeError, ValueError):
                    tv = None
            if not label:
                label = f"{tv:g} s" if tv is not None else f"{i + 1}"
            out.append({"x": xa, "y": ya, "t": tv,
                        "label": str(label), "index": i})
    if not out:
        raise ValueError("光谱演化图没有输入数据")
    return out


def _envelope_arrays(f0: dict, f1: dict):
    """首/末帧 -> (公共 x, 首帧 y, 末帧 y)；x 不同时把首帧线性插值到末帧网格。"""
    xa, ya = np.asarray(f0["x"], float), np.asarray(f0["y"], float)
    xb, yb = np.asarray(f1["x"], float), np.asarray(f1["y"], float)
    if xa.size == xb.size and np.allclose(xa, xb):
        return xb, ya, yb
    lo = max(float(xa.min()), float(xb.min()))
    hi = min(float(xa.max()), float(xb.max()))
    m = (xb >= lo) & (xb <= hi)
    if int(m.sum()) < 2 or float(hi) <= float(lo):
        return None
    return xb[m], np.interp(xb[m], xa, ya), yb[m]


# ═══════════════════════ ① 光谱演化图 ═══════════════════════
def _evolution_axes(frames, j: dict, panel_label, panel_label_style):
    from .plotstyle import cmap_colors

    frs = _normalize_frames(frames)
    fig, ax = _new_paper_figure(j, "evolution")
    n = len(frs)
    colors = cmap_colors(str(j.get("cmap", "Blues")),
                         j.get("cmap_lo"), j.get("cmap_hi"), max(1, n))
    lw = float(j.get("curve_lw", 1.3)) * 0.85
    lines = []
    for i, fr in enumerate(frs):
        ln, = ax.plot(fr["x"], fr["y"], color=colors[i % len(colors)],
                      lw=lw, zorder=3, label=fr["label"])
        lines.append(ln)
    lines[0].set_color(T0_COLOR)
    lines[0].set_linewidth(lw * 1.6)
    lines[0].set_zorder(5)
    envelope = None
    if n >= 2:
        env = _envelope_arrays(frs[0], frs[-1])
        if env is not None:
            ex, ey0, ey1 = env
            envelope = ax.fill_between(ex, ey0, ey1, color=colors[-1],
                                       alpha=0.16, linewidth=0, zorder=2)
    xs = np.concatenate([np.asarray(fr["x"], float) for fr in frs])
    ys = np.concatenate([np.asarray(fr["y"], float) for fr in frs])
    x_lo, x_hi = float(np.nanmin(xs)), float(np.nanmax(xs))
    y_hi = float(np.nanmax(ys))
    if not np.isfinite(y_hi) or y_hi <= 0:
        y_hi = 1.0
    ax_annot = r"$h\nu$"
    arrow = ax.annotate(
        ax_annot,
        xy=(x_lo + 0.15 * (x_hi - x_lo), y_hi * 0.80),
        xytext=(x_lo + 0.15 * (x_hi - x_lo), y_hi * 1.04),
        arrowprops={"arrowstyle": "-|>", "lw": 0.8, "color": "black"},
        fontsize=float(j.get("annot_size", 7.5)), ha="center", va="bottom",
        annotation_clip=False)
    legend_text = ""
    if n >= 2:
        handles = [lines[0], lines[-1]]
        labels = [frs[0]["label"], frs[-1]["label"]]
        ax.legend(handles, labels, loc=str(j.get("legend_loc", "best")),
                  fontsize=float(j.get("legend_size", 6.5)), frameon=False)
        legend_text = " / ".join(labels)
    ax.set_xlabel(r"$\lambda$ / nm")
    ax.set_ylabel(r"$A$ (a.u.)")
    _style_axes(ax, j)
    _draw_panel_label(ax, panel_label, panel_label_style, j)
    meta = {
        "kind": "spectral_evolution",
        "axes": [ax],
        "fig": fig,
        "panel_label": panel_label,
        "panel_label_style": panel_label_style,
        "line_colors": [str(ln.get_color()) for ln in lines],
        "t0_color": T0_COLOR,
        "envelope": envelope,
        "arrow_artist": arrow,
        "arrow_text": ax_annot,
        "legend_text": legend_text,
        "n_frames": n,
    }
    return fig, meta


def build_spectral_evolution_figure(frames, *, cfg: Optional[dict] = None,
                                    panel_label: Optional[str] = "a",
                                    panel_label_style: str = "bare"):
    """构建（不写盘）光谱演化图，返回 (Figure, meta)；结构断言用。"""
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        return _evolution_axes(frames, j, panel_label, panel_label_style)


def plot_spectral_evolution(frames, out_path, *, cfg: Optional[dict] = None,
                            panel_label: Optional[str] = "a",
                            panel_label_style: str = "bare",
                            formats: Optional[Sequence[str]] = None) -> Path:
    """光谱演化图 -> 写盘（零 PDF），返回主文件（PNG）Path。"""
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        fig, _meta = _evolution_axes(frames, j, panel_label, panel_label_style)
        try:
            return _save_paper(fig, out_path, j, formats)
        finally:
            plt.close(fig)


# ═══════════════════════ ② 衰减曲线 ═══════════════════════
def _normalize_decay(t, y, errors=None) -> tuple:
    try:
        at = np.asarray(t, float).ravel()
        ay = np.asarray(y, float).ravel()
    except (TypeError, ValueError) as err:
        raise ValueError(f"衰减曲线数据不是数值数组：{err}") from err
    if at.size != ay.size:
        raise ValueError(f"衰减曲线 t/y 长度不匹配：t={at.size} y={ay.size}")
    if at.size < 2:
        raise ValueError("衰减曲线至少需要 2 个数据点")
    m = np.isfinite(at) & np.isfinite(ay)
    ae = None
    if errors is not None:
        try:
            ae = np.asarray(errors, float).ravel()
        except (TypeError, ValueError) as err:
            raise ValueError(f"误差棒数据不是数值数组：{err}") from err
        if ae.size != at.size:
            raise ValueError(f"误差棒长度不匹配：{ae.size} != {at.size}")
        ae = ae[m]
    at, ay = at[m], ay[m]
    if at.size < 2:
        raise ValueError("衰减曲线有效数据点不足 2 个")
    order = np.argsort(at)
    at, ay = at[order], ay[order]
    if ae is not None:
        ae = ae[order]
    return at, ay, ae


def _fit_spec(fit, t, y, k=None, t_half=None, r2=None) -> dict:
    """拟合参数归一化：dict / callable / (k, t_half) 都转成统一字典。"""
    d: dict = dict(fit) if isinstance(fit, dict) else {}
    if fit is not None and not isinstance(fit, dict) and not callable(fit):
        raise ValueError("fit 必须是 dict、可调用 object 或 None")
    kk = d.get("k", k)
    th = d.get("t_half", t_half)
    rr = d.get("r2", r2)
    try:
        kk = None if kk is None else float(kk)
        th = None if th is None else float(th)
        rr = None if rr is None else float(rr)
    except (TypeError, ValueError):
        raise ValueError("拟合参数 k / t_half / r2 必须是数值或 None") from None
    if kk is None and th is None and not callable(fit):
        raise ValueError("衰减曲线缺少拟合参数：需要 fit dict 或 k / t_half")
    if kk is not None and kk <= 0:
        raise ValueError(f"衰减速率 k 必须为正：{kk}")
    if kk is not None and th is None:
        th = float(np.log(2.0) / float(kk))
    model = str(d.get("model", "exp_plateau"))
    i0 = d.get("I0", float(np.max(y)))
    if model == "exp":
        i_inf = d.get("I_inf", 0.0)
    else:
        i_inf = d.get("I_inf", float(np.min(y)))
    try:
        i0 = float(i0)
        i_inf = float(i_inf)
    except (TypeError, ValueError):
        raise ValueError("拟合参数 I0 / I_inf 必须是数值") from None
    return {"k": kk, "t_half": th, "r2": rr, "model": model,
            "I0": i0, "I_inf": i_inf, "callable": fit if callable(fit) else None}


def _fit_line(t: np.ndarray, spec: dict) -> np.ndarray:
    """按归一化拟合参数算拟合线 y（支持 callable 拟合函数）。"""
    fn = spec.get("callable")
    if fn is not None:
        try:
            return np.asarray(fn(t), float)
        except Exception as err:                       # noqa: BLE001
            raise ValueError(f"拟合函数调用失败：{err}") from err
    kk = spec["k"]
    if kk is None:
        raise ValueError("拟合参数不足：既无 fit callable 也无 k")
    return spec["I_inf"] + (spec["I0"] - spec["I_inf"]) * np.exp(-kk * t)


def _decay_axes(t, y, fit=None, *, errors=None, k=None, t_half=None,
                r2=None, j: dict, panel_label=None,
                panel_label_style: str = "bare"):
    at, ay, ae = _normalize_decay(t, y, errors)
    spec = _fit_spec(fit, at, ay, k=k, t_half=t_half, r2=r2)
    fig, ax = _new_paper_figure(j, "decay")
    ms = float(j.get("marker_size", 2.9)) + 1.6
    scatter = None
    if ae is not None:
        ax.errorbar(at, ay, yerr=ae, fmt="o", ms=ms, mfc="white", mec="black",
                    mew=0.6, ecolor="black", capsize=1.8, elinewidth=0.7,
                    zorder=3, label="data")
        scatter = ax.lines[-1] if ax.lines else None
    else:
        scatter, = ax.plot(at, ay, "o", ms=ms, mfc="white", mec="black",
                           mew=0.6, zorder=3, label="data")
    tg = np.linspace(float(at.min()), float(at.max()), 200)
    yg = _fit_line(tg, spec)
    fit_line, = ax.plot(tg, yg, color=FIT_COLOR,
                        lw=float(j.get("curve_lw", 1.3)), zorder=4)
    parts = []
    if spec["k"] is not None:
        parts.append(f"$k$ = {spec['k']:.4g} min$^{{-1}}$")
    if spec["t_half"] is not None:
        parts.append(f"$t_{{1/2}}$ = {spec['t_half']:.4g} min")
    if spec["r2"] is not None:
        parts.append(f"$R^2$ = {spec['r2']:.4f}")
    legend_text = ", ".join(parts) if parts else "fit"
    fit_line.set_label(legend_text)
    ax.legend(loc=str(j.get("legend_loc", "best")),
              fontsize=float(j.get("legend_size", 6.5)), frameon=False)
    ax.set_xlabel("Time / min")
    ax.set_ylabel(r"$I$ (a.u.)")
    _style_axes(ax, j)
    _draw_panel_label(ax, panel_label, panel_label_style, j)
    meta = {
        "kind": "decay",
        "axes": [ax],
        "fig": fig,
        "panel_label": panel_label,
        "panel_label_style": panel_label_style,
        "scatter": scatter,
        "fit_line": fit_line,
        "fit_text": legend_text,
        "fit": spec,
        "k": spec["k"],
        "t_half": spec["t_half"],
        "r2": spec["r2"],
    }
    return fig, meta


def build_decay_figure(t, y, fit=None, *, errors=None, k=None, t_half=None,
                       r2=None, cfg: Optional[dict] = None,
                       panel_label: Optional[str] = "a",
                       panel_label_style: str = "bare"):
    """构建（不写盘）衰减曲线，返回 (Figure, meta)。"""
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        return _decay_axes(t, y, fit, errors=errors, k=k, t_half=t_half,
                           r2=r2, j=j, panel_label=panel_label,
                           panel_label_style=panel_label_style)


def plot_decay_curve(t, y, fit=None, out_path=None, *, errors=None, k=None,
                     t_half=None, r2=None, cfg: Optional[dict] = None,
                     panel_label: Optional[str] = "a",
                     panel_label_style: str = "bare",
                     formats: Optional[Sequence[str]] = None) -> Path:
    """衰减曲线 -> 写盘（零 PDF），返回主文件（PNG）Path。"""
    if out_path is None:
        raise ValueError("衰减曲线缺少输出路径 out_path")
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        fig, _meta = _decay_axes(t, y, fit, errors=errors, k=k, t_half=t_half,
                                 r2=r2, j=j, panel_label=panel_label,
                                 panel_label_style=panel_label_style)
        try:
            return _save_paper(fig, out_path, j, formats)
        finally:
            plt.close(fig)


# ═══════════════════════ ③ 分组柱状图 ═══════════════════════
def _normalize_series(series, n_groups: int) -> list:
    """series -> [{label, values, errors, color}]（dict / list 两种写法都接受）。"""
    items: list[dict] = []
    if isinstance(series, dict):
        for label, spec in series.items():
            if isinstance(spec, dict):
                items.append({"label": str(label), "values": spec.get("values"),
                              "errors": spec.get("errors"),
                              "color": spec.get("color")})
            else:
                items.append({"label": str(label), "values": spec,
                              "errors": None, "color": None})
    else:
        for i, spec in enumerate(series or []):
            if isinstance(spec, dict):
                items.append({
                    "label": str(spec.get("label") or f"series {i + 1}"),
                    "values": spec.get("values"),
                    "errors": spec.get("errors"),
                    "color": spec.get("color")})
            else:
                items.append({"label": f"series {i + 1}", "values": spec,
                              "errors": None, "color": None})
    if not items:
        raise ValueError("分组柱状图没有 series 数据")
    out: list[dict] = []
    for item in items:
        try:
            vals = np.asarray(item["values"], float).ravel()
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"series {item['label']} 的 values 不是数值数组：{err}") from err
        if vals.size != n_groups:
            raise ValueError(
                f"series {item['label']} 的 values 长度 {vals.size} != 组数 {n_groups}")
        errs = None
        if item["errors"] is not None:
            try:
                errs = np.asarray(item["errors"], float).ravel()
            except (TypeError, ValueError) as err:
                raise ValueError(
                    f"series {item['label']} 的 errors 不是数值数组：{err}") from err
            if errs.size != n_groups:
                raise ValueError(
                    f"series {item['label']} 的 errors 长度 {errs.size} != 组数 {n_groups}")
        out.append({"label": item["label"], "values": vals,
                    "errors": errs, "color": item["color"]})
    return out


def _bars_axes(groups, series, j: dict, panel_label,
               panel_label_style: str = "bare"):
    group_names = [str(g) for g in (groups or [])]
    if not group_names:
        raise ValueError("分组柱状图没有 groups 数据")
    n = len(group_names)
    items = _normalize_series(series, n)
    m = len(items)
    width = 0.8 / m
    x0 = np.arange(n, dtype=float)
    fig, ax = _new_paper_figure(j, "bars")
    bar_containers: list = []
    err_containers: list = []
    colors: list = []
    for si, item in enumerate(items):
        color = str(item["color"] or SERIES_COLORS[si % len(SERIES_COLORS)])
        colors.append(color)
        xs = x0 + (si - (m - 1) / 2.0) * width
        bars = ax.bar(xs, item["values"], width=width * 0.92, color=color,
                      edgecolor="black", linewidth=0.5,
                      label=item["label"], zorder=3)
        bar_containers.append(bars)
        if item["errors"] is not None:
            eb = ax.errorbar(xs, item["values"], yerr=item["errors"],
                             fmt="none", ecolor="black", elinewidth=0.8,
                             capsize=2.0, capthick=0.8, zorder=4)
            err_containers.append(eb)
    ax.set_xticks(x0)
    ax.set_xticklabels(group_names, rotation=45, ha="right",
                       rotation_mode="anchor")
    ax.grid(False)
    if m > 1:
        ax.legend(loc=str(j.get("legend_loc", "best")),
                  fontsize=float(j.get("legend_size", 6.5)), frameon=False)
    ax.set_ylabel("Value")
    _style_axes(ax, j)
    _draw_panel_label(ax, panel_label, panel_label_style, j)
    meta = {
        "kind": "grouped_bars",
        "axes": [ax],
        "fig": fig,
        "panel_label": panel_label,
        "panel_label_style": panel_label_style,
        "groups": group_names,
        "bar_containers": bar_containers,
        "errorbar_containers": err_containers,
        "colors": colors,
        "xtick_rotation": 45.0,
    }
    return fig, meta


def build_grouped_bars_figure(groups, series, *, cfg: Optional[dict] = None,
                              panel_label: Optional[str] = "a",
                              panel_label_style: str = "bare"):
    """构建（不写盘）分组柱状图，返回 (Figure, meta)。"""
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        return _bars_axes(groups, series, j, panel_label, panel_label_style)


def plot_grouped_bars(groups, series, out_path, *, cfg: Optional[dict] = None,
                      panel_label: Optional[str] = "a",
                      panel_label_style: str = "bare",
                      formats: Optional[Sequence[str]] = None) -> Path:
    """分组柱状图 -> 写盘（零 PDF），返回主文件（PNG）Path。"""
    j = _jcs(cfg)
    plt = _plt()
    with plt.rc_context(_paper_rc(j)):
        fig, _meta = _bars_axes(groups, series, j, panel_label,
                                panel_label_style)
        try:
            return _save_paper(fig, out_path, j, formats)
        finally:
            plt.close(fig)
