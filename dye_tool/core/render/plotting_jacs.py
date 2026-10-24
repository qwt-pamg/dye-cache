# -*- coding: utf-8 -*-
"""JACS（J. Am. Chem. Soc.）期刊风格渲染层。

设计文档要求（本模块的实现依据）
--------------------------------
【整体】单栏图，成品宽 **3.25 in = 82.5 mm**；dpi **≥ 600**；导出 **png**
（V1.2 起不再输出 PDF：TrueType 子集化占导出耗时一半以上；需要矢量图请用 svg）；
Arial/Helvetica；轴标签 7–9 pt、刻度数字 7 pt；坐标轴线宽 0.8–1.0 pt；
**刻度朝内**（direction: in）；**上下左右四边框完整**；数据曲线 1.2–1.5 pt；
**图内不出现标题**，说明性文字一律进图注（``figures/captions.md``）。

【数据曲线】不画 colorbar，改用**箭头**表示时间趋势 + 图注交代时间范围。

【标注】一律**水平**放置、7–8 pt：
  * 379 nm —— 灰色细虚线（``ls='--', lw=0.8, color='gray'``），顶端附近水平标 ``379``；
    不再画粉色阴影带，也不再保留洋红粗虚线；
  * 532 nm —— 默认**不画**（写进图注）；置 ``plot.jacs.mark_excitation=true`` 时
    改画灰色点划线并水平标 ``532 ex.``；
  * ~571 nm —— 删除竖排"荧光团最大吸收"，改为峰侧水平标 ``Resorufin``。

【坐标轴】x ``Wavelength / nm``（300–700，主刻度 100）；y ``Absorbance``
（自 0 起、顶部留 5% 余量）；刻度数字 7 pt 且无科学计数法偏移。

工程要点
--------
**精确画布尺寸**：本模块**不使用** ``bbox_inches="tight"``。若用 tight，
保存时会被裁到内容外接框，成品宽度就不再是 82.5 mm 而是随内容浮动。
这里把图画在固定 ``3.25 in × h`` 画布内，用 ``layout="constrained"`` 把
文字压进画布，于是 600 dpi 下 PNG 恰好 **1950 px** 宽，满足期刊硬性限宽。

**与 legacy 的隔离**：legacy 逐像素渲染路径在 :mod:`core.plotting` 中**原样保留**
（``plot.style='legacy'``），A4.2 回归比对因此仍然成立；本模块只负责新风格。
"""
from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from .plotting import _safe, apply_jacs_style, plot_style
from .plotstyle import JACS_SCHEMA, jacs_defaults

log = logging.getLogger("dye_tool.plotting.jacs")

#: V2.9.3 FR-66/67：会话态图式/透明底（不写入 config.yaml；默认=2.7.0 原形态）
_FIG_SESSION = {"style": None, "error_bars": True, "annotate": False,
                "transparent": False, "time_unit": "s", "ci_band": False}


def set_figure_session(style=None, error_bars=None, annotate=None, transparent=None,
                       time_unit=None, ci_band=None):
    """设置 jacs 图 c/d 的会话图式；``None`` = 不改（默认柱状/森林图）。"""
    if style is not None:
        style = str(style).lower()
        _FIG_SESSION["style"] = style if style in ("bar", "line", "scatter") else None
    if error_bars is not None:
        _FIG_SESSION["error_bars"] = bool(error_bars)
    if annotate is not None:
        _FIG_SESSION["annotate"] = bool(annotate)
    if transparent is not None:
        _FIG_SESSION["transparent"] = bool(transparent)
    if time_unit is not None:
        u = str(time_unit).lower()
        _FIG_SESSION["time_unit"] = u if u in ("s", "min", "h") else "s"
    if ci_band is not None:
        _FIG_SESSION["ci_band"] = bool(ci_band)
    return dict(_FIG_SESSION)


def get_figure_session() -> dict:
    return dict(_FIG_SESSION)


def _time_factor() -> tuple[float, str]:
    u = str(_FIG_SESSION.get("time_unit", "s"))
    return {"s": (1.0, "s"), "min": (60.0, "min"), "h": (3600.0, "h")}.get(u, (1.0, "s"))


def _fig_style(kind: str) -> str:
    st = _FIG_SESSION.get("style")
    if st in ("bar", "line", "scatter"):
        return st
    return "bar" if kind == "c" else "scatter"

__all__ = ["plot_all_jacs", "jacs_stack_figure", "jacs_stack_all",
           "ensure_font", "save_jacs", "new_figure", "finish_axes",
           "fig_height", "JACS_DEFAULTS", "unknown_key_warnings",
           "expected_figures", "figure_steps", "STEP_ORDER", "FIGURE_LABELS",
           "_preview_first"]


#: 找不到配置时的兜底（键位真相在 :mod:`core.plotstyle`）
JACS_DEFAULTS: dict = jacs_defaults()

#: 已就"未知键"告警过的键（同一进程只吵一次）
_WARNED_KEYS: set[str] = set()


def unknown_key_warnings(cfg: Optional[dict]) -> list[str]:
    """返回 ``plot.jacs`` 里**当前代码不认识**的键名列表（FR-7.4 / R4）。

    V1.0 是静默忽略——手改 yaml 打错字毫无反馈。这里返回键名供 GUI 红字提示；
    ``strict: false`` 时回到旧的静默行为。
    """
    got = ((cfg or {}).get("plot") or {}).get("jacs") or {}
    if not isinstance(got, dict):
        return []
    if not bool(got.get("strict", True)):
        return []
    # M12：下划线私有键（`_preset`、`_dye_session`…）是程序自己写的元数据，不是手误
    return sorted(k for k in got
                  if k not in JACS_SCHEMA and not str(k).startswith("_"))


def jcfg(cfg: Optional[dict]) -> dict:
    """取 JACS 绘图参数（缺项按 :data:`JACS_DEFAULTS` 补齐）。

    未知键记一次 WARNING（不再静默忽略，设计文档 §9 R4），不影响渲染。
    """
    got = ((cfg or {}).get("plot") or {}).get("jacs") or {}
    unknown = unknown_key_warnings(cfg)
    fresh = [k for k in unknown if k not in _WARNED_KEYS]
    if fresh:
        _WARNED_KEYS.update(fresh)
        # 重复逻辑 #4：建议逻辑与 plotstyle.validate_jacs **共用同一份**
        # （句式与 cutoff 原先各写一遍，已抽到 suggest_key）。
        # `tip` 只在有近似键时拼进日志，避免"删掉即可"在渲染日志里显得像告警。
        from .plotstyle import suggest_key
        for k in fresh:
            tip = suggest_key(k)
            if tip.startswith("是否想写"):
                tip = "，" + tip
            else:
                tip = ""
            log.warning("plot.jacs 里存在未知键 `%s`（不会被任何代码读取）%s",
                        k, tip)
    return {**JACS_DEFAULTS, **got}


def fig_height(cfg: Optional[dict], which: str,
               fallback: Optional[float] = None) -> float:
    """取某张图的画布高度：``fig_heights[which]`` → ``height_in`` → 兜底。

    ``fig_heights`` 里填 ``null`` 表示"按内容自动"，此时由调用方给 fallback。
    """
    j = jcfg(cfg)
    heights = j.get("fig_heights") or {}
    val = heights.get(which) if isinstance(heights, dict) else None
    if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
        return float(val)
    if fallback is not None:
        return float(fallback)
    return float(j["height_in"])


def _plt():
    """Agg 后端下的 pyplot（延迟导入，避免 import 期绑定后端）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _mfc():
    from matplotlib import font_manager
    return font_manager


# ═══════════════════════════ 字体与画布 ═══════════════════════════
def ensure_font(cfg: Optional[dict] = None) -> str:
    """把 rcParams 的 sans-serif 钉到 Arial（缺失则按回退表降级）并返回实际字体。

    与 :func:`core.plotting.setup_mpl` 不同：**这里不切换到中文字体**——
    JACS 图件的文字全为英文，混入 CJK 字体会让 Arial 失效。
    """
    import matplotlib
    j = jcfg(cfg)
    want = [str(j.get("font", "Arial"))] + [str(x) for x in j.get("font_fallback", [])]
    avail = {f.name for f in _mfc().fontManager.ttflist}
    picked = next((n for n in want if n in avail), "DejaVu Sans")
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = [picked] + [n for n in want if n != picked]
    if picked != want[0]:
        log.warning("未找到字体 %s → JACS 图件回退为 %s", want[0], picked)
    return picked


def new_figure(cfg: Optional[dict], height_in: Optional[float] = None,
               nrows: int = 1, which: Optional[str] = None):
    """新建固定宽画布 → ``(fig, [ax, ...])``（宽 = 单栏 82.5 mm）。

    ``which`` ∈ {a,b,c,d,e,stack,fr4_*} 时按 ``fig_heights`` 取高度；
    ``height_in`` 显式给出时优先（自动高度公式用）。
    """
    plt = _plt()
    j = jcfg(cfg)
    w = float(j["width_in"])
    if height_in is None:
        h = fig_height(cfg, which) if which else float(j["height_in"])
    else:
        h = float(height_in)
    fig, axes = plt.subplots(nrows, 1, figsize=(w, h), layout="constrained",
                             squeeze=False)
    return fig, list(axes[:, 0])


def finish_axes(ax, cfg: Optional[dict], *, xlabel: Optional[str] = None,
                ylabel: Optional[str] = None,
                xlim: Optional[Sequence[float]] = None,
                ylim: Optional[Sequence[float]] = None,
                xticks: Optional[Sequence[float]] = None,
                xstep: Optional[float] = None) -> None:
    """统一施加 JACS 轴样式：四边完整框 / 刻度朝内 / 7 pt / 无科学计数法偏移。

    ``xlabel``/``ylabel`` 传 None 时用配置里的默认文本（``plot.jacs.xlabel``
    等），传空串 ``""`` 表示**不要标题**。
    """
    from matplotlib.ticker import MultipleLocator, ScalarFormatter
    j = jcfg(cfg)
    sw = float(j["spine_width"])
    for sp in ax.spines.values():
        sp.set_visible(bool(j.get("box", True)))
        sp.set_linewidth(sw)
    ax.tick_params(direction=str(j["tick_direction"]),
                   width=float(j["tick_width"]),
                   length=float(j["tick_length"]),
                   labelsize=float(j["tick_size"]),
                   top=True, right=True, which="major")
    if bool(j.get("minor_ticks", True)):
        ax.tick_params(width=float(j["tick_width"]) * float(j["minor_w_frac"]),
                       length=float(j["tick_length"]) * float(j["minor_len_frac"]),
                       top=True, right=True, which="minor")
        step = float(j.get("minor_tick_step", 0.0) or 0.0)
        if step > 0:
            ax.xaxis.set_minor_locator(MultipleLocator(step))
    else:
        ax.minorticks_off()
    fx = ScalarFormatter(useOffset=False)
    fx.set_scientific(False)
    ax.xaxis.set_major_formatter(fx)
    ax.xaxis.get_offset_text().set_visible(False)
    ax.yaxis.get_offset_text().set_visible(False)
    if xlim is not None:
        ax.set_xlim(float(xlim[0]), float(xlim[1]))
    if ylim is not None:
        ax.set_ylim(float(ylim[0]), float(ylim[1]))
    if xticks is not None:
        ax.set_xticks([float(v) for v in xticks])
    elif xstep:
        ax.xaxis.set_major_locator(MultipleLocator(float(xstep)))
    if bool(j.get("grid", False)):
        ax.grid(True, which="major", color="0.85", lw=0.4, zorder=0)
    if xlabel is None:
        xlabel = str(j.get("xlabel", "Wavelength / nm"))
    if ylabel is None:
        ylabel = str(j.get("ylabel", "Absorbance"))
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=float(j["label_size"]))
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=float(j["label_size"]))


def save_jacs(fig, out_dir: Path | str, name: str,
              cfg: Optional[dict] = None,
              formats: Optional[Sequence[str]] = None,
              dpi: Optional[int] = None) -> list[Path]:
    """按 JACS 参数导出（默认 **png** @600 dpi），**不裁剪**以保持画布宽度精确。

    ``formats``/``dpi`` 可覆盖（FR-4 用 png+svg；预览档用低 dpi）。

    V1.2 起**不再输出 PDF**：PDF 的 TrueType 子集化实测占导出耗时一半以上，而
    600 dpi 的 png 已满足投稿要求；需要矢量图时用 svg（文字可编辑、几乎不耗时）。
    """
    plt = _plt()
    j = jcfg(cfg)
    dpi = int(dpi if dpi is not None else j["dpi"])
    from .plotstyle import filter_formats
    # FR-67.3：多选导出允许 pdf；legacy 路径仍由 filter_formats 默认拦截
    fmts = filter_formats(formats if formats is not None else j["formats"],
                          allow_pdf=True)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for ext in fmts:
        p = out_dir / f"{name}.{ext}"
        _fc = "none" if _FIG_SESSION.get("transparent") else "white"
        fig.savefig(p, dpi=dpi, bbox_inches=None, facecolor=_fc,
                    edgecolor="none")
        made.append(p)
    plt.close(fig)
    log.info("已保存 JACS 图: %s.(%s) @%ddpi", name, "|".join(fmts), dpi)
    return made


def _cmap_colors(cfg: Optional[dict], n: int) -> list:
    """单色顺序色带（默认 Blues，浅→深 = 早→晚），取代 legacy 的 viridis。

    V2.x 审查 M1：实现**委托** :func:`core.render.plotstyle.cmap_colors`
    ——渲染与对话框预览从此用同一套校验/回退，不会再出现"预览色序≠出图色序"。
    """
    from .plotstyle import cmap_colors
    j = jcfg(cfg)
    return cmap_colors(j.get("cmap"), j.get("cmap_lo"), j.get("cmap_hi"), n)


# ═══════════════════════════ 小工具 ═══════════════════════════
def _fmt_range(vals: Sequence[float], unit: str = "") -> str:
    """``[0, 600]`` → ``"0–600 s"``（用于图注）。"""
    a = np.asarray(list(vals), float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return "n/a"
    return f"{float(a.min()):g}–{float(a.max()):g}{(' ' + unit) if unit else ''}"


def _annot(ax, x, y, text, cfg, color=None, ha="left", va="bottom", **kw):
    """水平标注（绝不用 rotation）。颜色默认取 ``annot_color``。"""
    j = jcfg(cfg)
    if color is None:
        color = str(j.get("annot_color", "0.25"))
    return ax.text(x, y, text, fontsize=float(j["annot_size"]), color=color,
                   ha=ha, va=va, rotation=0, clip_on=False, **kw)


def _trend_sign(entries: Sequence[tuple], probe: Optional[float] = None,
                window: float = 2.0) -> int:
    """由数据判断时间趋势：+1 吸光度随 t 上升，-1 下降。

    优先用**探针波长**（默认 379 nm，即整个 Φ_Δ 分析所依据的量）的吸光度序列
    判断 —— 这样箭头方向与报告出来的 ``k`` 符号天然一致。

    ⚠️ 不要用"最早一条 vs 最晚一条的宽带平均"判断：本数据集的 t=0 文件是
    **导出被截断**的（300–399 nm 无数据），它在 400 nm 处只有 0.052，会把
    整体趋势误判成"上升"，而实际探针吸光度是随时间下降的（k > 0）。

    探针有效点不足 2 个时退回 350–600 nm 平均吸光度；再不行按光降解处理。
    """
    if len(entries) < 2:
        return -1
    if probe is not None:
        ts: list[float] = []
        As: list[float] = []
        for t, wl, a in entries:
            wl, a = np.asarray(wl, float), np.asarray(a, float)
            m = np.abs(wl - float(probe)) <= float(window)
            v = a[m] if m.any() else np.asarray([], float)
            v = v[np.isfinite(v)]
            if v.size:
                ts.append(float(t))
                As.append(float(np.mean(v)))
        if len(ts) >= 2 and min(As) > 0.0:
            slope = float(np.polyfit(ts, np.log(np.asarray(As, float)), 1)[0])
            if slope != 0.0:
                return -1 if slope < 0 else 1

    def _mean(e) -> float:
        wl, a = np.asarray(e[1], float), np.asarray(e[2], float)
        m = (wl >= 350.0) & (wl <= 600.0) & np.isfinite(a)
        return float(np.mean(a[m])) if m.any() else float("nan")
    lo, hi = _mean(entries[0]), _mean(entries[-1])
    if not (np.isfinite(lo) and np.isfinite(hi)):
        return -1
    return 1 if hi > lo else -1


def _trend_arrow(ax, x, y_lo, y_hi, cfg, label: Optional[str] = None) -> None:
    """竖直趋势箭头（取代 colorbar 表达时间方向）+ 水平短标签。

    标签放在箭身**中点右侧**（不放箭头尖端），避免与顶部边框/坐标轴重叠。
    颜色/样式/标签文本/偏移全部来自配置（H4 键化）。
    """
    j = jcfg(cfg)
    lw = float(j["annot_lw"])
    col = str(j.get("arrow_color", "0.25"))
    ax.annotate("", xy=(x, y_hi), xytext=(x, y_lo),
                arrowprops=dict(arrowstyle=str(j.get("arrow_style", "-|>")),
                                color=col, lw=lw, shrinkA=0.0, shrinkB=0.0,
                                mutation_scale=5.0),
                annotation_clip=False)
    if label is None:
        label = str(j.get("arrow_label", "t"))
    if label:
        _annot(ax, x + float(j.get("arrow_label_dx_frac", 0.020))
               * (ax.get_xlim()[1] - ax.get_xlim()[0]),
               0.5 * (y_lo + y_hi), label, cfg, color=col, ha="left",
               va="center")


def _free_column(cands: np.ndarray, spec_list, half: float = 3.0) -> tuple:
    """在候选波长里找"最空的一列"→ ``(波长, 该列最高吸光度)``，用于放趋势箭头。"""
    best_x, best_top = float(cands[0]), np.inf
    for x0 in cands:
        top = 0.0
        seen = False
        for wl, a in spec_list:
            wl, a = np.asarray(wl, float), np.asarray(a, float)
            m = np.abs(wl - x0) <= half
            if m.any():
                v = np.nanmax(a[m]) if np.isfinite(a[m]).any() else np.nan
                if np.isfinite(v):
                    top, seen = max(top, float(v)), True
        if seen and top < best_top:
            best_top, best_x = top, float(x0)
    return best_x, (0.0 if not np.isfinite(best_top) else best_top)


def _search_window(cfg: Optional[dict], wl_cfg: dict, j: dict) -> tuple:
    """染料吸收带搜索区间（H2/R2）：``[lo, hi]`` 或 ``"auto"``。

    ``auto`` = 在 x 轴范围内找全局最大峰、**排除探针波长 ± 20 nm**
    （不预设任何染料带位置——泛用性红线 §1.3-9）。
    """
    raw = j.get("fluorophore_search", [540.0, 620.0])
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        xl = j.get("xlim") or [300.0, 700.0]
        probe = float(wl_cfg.get("probe", 379.0))
        guard = 20.0
        lo, hi = float(xl[0]), float(xl[1])
        if probe - guard <= lo:
            lo = probe + guard
        elif probe + guard >= hi:
            hi = probe - guard
        else:
            # 峰在探针左侧还是右侧未知 → 取较宽的一侧
            if (probe - lo) >= (hi - probe):
                hi = probe - guard
            else:
                lo = probe + guard
        return lo, hi
    try:
        return float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError):
        return 540.0, 620.0


def _fluorophore_label(cfg: Optional[dict], group: str,
                       session: Optional[dict] = None) -> str:
    """图内染料标注文本（H1/R1）：与输出目录、历史档案**同源**解析。

    ``session`` 显式给出时优先（P2 多进程渲染用：每个渲染单元共用同一份 cfg，
    不能就地改 ``cfg["_dye_session"]`` 来传会话命名）。
    """
    try:
        from ..report.dyes import fluorophore_label_for
        return fluorophore_label_for(cfg, group, session=session)
    except Exception as err:                           # noqa: BLE001
        log.debug("染料名解析失败（回退组名）：%s", err)
        return str(group).split("-", 1)[0] if group else ""


#: 渲染复用缓存（FR-6.7）：``键 → 已写出的文件路径``。
#: 键 = (数据指纹, 步骤, 文件名, 相关参数, dpi, 格式) —— 任一变化都不会命中，
#: 因此**绝不可能**复用过期或不同分辨率的图。只存路径，不存 Figure 对象。
#:
#: T4（评审记录）：原先是个**无上限**的普通 dict。长会话里反复改出图参数/换目录
#: 会一直涨（每键只存几个路径，所以不是内存灾难，但没有上界就不该叫"缓存"）。
#: 改为 ``OrderedDict`` + LRU，与 ``core/io._SCAN_CACHE`` **同一套写法**（那边
#: 已在 V1.2 的 O2 里验证过）。上限取得比"一个 run 的图数"高两个数量级，
#: 正常使用**永远不会触发淘汰**——它只防"无限会话"。
_RENDER_CACHE: "OrderedDict" = OrderedDict()

#: 渲染缓存条目上限（一个 30 组的 run 约 90 张图 → 2000 条约等于 20 个 run 的图）
RENDER_CACHE_MAX = 2000

#: 文件名前缀 → 步骤名（用于取该图受影响参数的最小集合）
_NAME_STEP = (("fig_a", "a"), ("fig_b", "b"), ("fig_c", "c"), ("fig_d", "d"),
              ("fig_e", "e"), ("jacs_stack", "stack"))


def clear_render_cache() -> None:
    """清空渲染复用缓存（GUI「重新载入」/测试用）。"""
    _RENDER_CACHE.clear()


def render_cache_stats() -> dict:
    """``{"entries": n, "max": n}``。"""
    return {"entries": len(_RENDER_CACHE), "max": RENDER_CACHE_MAX}


def _rcache_get(key):
    """读渲染缓存（命中即标记为最近使用）。未命中返回 ``None``。"""
    got = _RENDER_CACHE.get(key)
    if got is not None:
        _RENDER_CACHE.move_to_end(key)
    return got


def _rcache_put(key, value) -> None:
    """写渲染缓存，并按 LRU 淘汰超额条目（T4）。"""
    _RENDER_CACHE[key] = value
    _RENDER_CACHE.move_to_end(key)
    while len(_RENDER_CACHE) > RENDER_CACHE_MAX:
        _RENDER_CACHE.popitem(last=False)


def _step_of(name: str) -> str:
    """由文件名反推步骤（``fig_b_kinetics_x`` → ``b``）。"""
    for pref, step in _NAME_STEP:
        if str(name).startswith(pref):
            return step
    return "?"


def _data_fingerprint(scans, results: dict, phi: dict) -> str:
    """对参与出图的**全部数值**取指纹：数据一变，缓存必然失效。"""
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    for s in scans or []:
        h.update(str(getattr(s, "path", "")).encode("utf-8", "replace"))
        h.update(f"{float(getattr(s, 't', 0.0)):.9g}".encode())
        h.update(str(getattr(s, "group", "")).encode("utf-8", "replace"))
        # A.M10（V2.x 审查）：指纹缺 role/excluded → 改批次 role（sample→background）
        # 或某文件被剔除后补渲会命中旧图：图 a/stack 仍画应排除的谱，而图注重算 →
        # **图文不一致**。这两项直接决定"哪些谱参与出图"，必须进指纹。
        h.update(str(getattr(s, "role", "")).encode("utf-8", "replace"))
        h.update(str(getattr(s, "excluded", "") or "").encode("utf-8", "replace"))
        a = getattr(s, "a", None)
        if a is not None:
            h.update(np.nan_to_num(np.asarray(a, float),
                                   nan=-1.0).tobytes())
    for g, e in sorted((results or {}).items()):
        f = (e or {}).get("pooled")
        if f is not None:
            h.update(f"{g}|{getattr(f, 'k', '')!r}|{getattr(f, 'r2', '')!r}|"
                     f"{getattr(f, 'n', '')!r}|{getattr(f, 'truncated', '')!r}"
                     .encode("utf-8", "replace"))
    for g, p in sorted((phi or {}).items()):
        h.update(f"{g}|{p.get('phi')!r}|{p.get('phi_err')!r}|"
                 f"{p.get('status')!r}".encode("utf-8", "replace"))
    return h.hexdigest()


def _label_key(scans, results: Optional[dict], cfg: Optional[dict],
               session: Optional[dict] = None) -> str:
    """参与出图的所有组的**染料标注文本**指纹（S5）。

    图 a 里的染料名来自 ``_fluorophore_label(cfg, g, session)``，它会去查
    ``cfg["_dye_session"]`` + ``dyes.yaml`` 注册表 + auto 启发式。这些输入**都不在**
    ``_param_key`` 里，于是"改染料名后点补渲"会命中旧图：返回 0 个文件、PNG 还是
    旧名字，而 ``captions.md`` 每次重算 → 图与图注不一致（FR-6.7 的核心用例被破坏）。

    这里把每组解析出的标签文本一起纳入缓存键：名字一变，键就变，必然重画。
    """
    import json
    groups: set = set()
    for s in scans or []:
        if not getattr(s, "excluded", "") and s.role not in ("background", "dark"):
            groups.add(str(s.group))
    for g in (results or {}):
        groups.add(str(g))
    if not groups:
        return ""
    pairs = {g: _fluorophore_label(cfg, g, session) for g in sorted(groups)}
    try:
        return json.dumps(pairs, sort_keys=True, default=str)
    except Exception:                                  # noqa: BLE001
        return str(pairs)


def _param_key(cfg: Optional[dict], step: str) -> str:
    """该步骤**相关的**参数指纹（改无关参数不会让它失效，FR-6.7）。"""
    import json
    c = cfg or {}
    parts = {"jacs": ((c.get("plot") or {}).get("jacs") or {}),
             "wavelength": c.get("wavelength"),
             "baseline": c.get("baseline"),
             "background": c.get("background"),
             "a0": c.get("a0"),
             "kinetics": c.get("kinetics"),
             "quantum_yield": c.get("quantum_yield")}
    secs = set(FIGURE_DEPS.get(step, ())) | {"wavelength", "plot"}
    keep = {k: v for k, v in parts.items()
            if k == "jacs" or k in secs}
    sel = (c.get("plot") or {}).get("spectra_overlay") if step in ("a", "stack") \
        else None
    if sel is not None:
        keep["spectra_overlay"] = sel
    try:
        return json.dumps(keep, sort_keys=True, default=str)
    except Exception:                                  # noqa: BLE001
        return str(keep)


def _pick_arrow_column(cfg: Optional[dict], spec_list, xlim) -> tuple:
    """选定箭头所放的波长列 → ``(x, 该列顶端吸光度)``（H4：区间 + data 回退）。

    优先用配置的 ``arrow_x_range``；该区间在当前数据上**被曲线占据**
    （最高吸光度 > ``arrow_lane_threshold``）且 ``arrow_auto_fallback`` 打开时，
    自动回退到"全 x 范围数据扫描找空闲列"并记日志——换探针/换染料后箭头不会
    栽进谱线里（§9 R3）。
    """
    j = jcfg(cfg)
    margin = float(j.get("arrow_lane_margin_nm", 3.0))
    lo, hi = float(xlim[0]), float(xlim[1])
    rng = j.get("arrow_x_range", [432.0, 556.0])
    if rng == "data" or rng is None:
        cands = np.arange(lo + 0.05 * (hi - lo), hi - 0.05 * (hi - lo), 4.0)
        x, top = _free_column(cands, spec_list, margin)
        log.debug("箭头避让：data 模式 → %.0f nm（列顶 %.3f）", x, top)
        return x, top
    try:
        a, b = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        a, b = 432.0, 556.0
    cands = np.arange(a, b, 4.0)
    if cands.size == 0:
        cands = np.array([0.5 * (a + b)])
    x, top = _free_column(cands, spec_list, margin)
    thr = float(j.get("arrow_lane_threshold", 0.06))
    if top <= thr or not bool(j.get("arrow_auto_fallback", True)):
        return x, top
    log.info("箭头避让区间 [%.0f, %.0f] nm 被曲线占据（列顶 %.3f > 阈值 %.3f）"
             "→ 自动回退数据扫描（可用 plot.jacs.arrow_x_range 显式指定）",
             a, b, top, thr)
    cands2 = np.arange(lo + 0.05 * (hi - lo), hi - 0.05 * (hi - lo), 4.0)
    x2, top2 = _free_column(cands2, spec_list, margin)
    return x2, top2


# ═══════════════════════════ 图 a：时间演化叠加 ═══════════════════════════
def fig_a_overlay(scans, cfg: dict, figdir: Path,
                  captions: list, save=None,
                  groups: Optional[Sequence[str]] = None,
                  session: Optional[dict] = None) -> list[Path]:
    """每组一张 JACS 单栏叠加图（无 colorbar / 无标题 / 标注全水平）。

    ``groups`` 限定只画这些组（P2 多进程渲染按组切分任务用；None = 全部）。
    ``session`` 是会话染料命名覆盖（不改 cfg，见 :func:`_fluorophore_label`）。
    """
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    ov = (cfg.get("plot") or {}).get("spectra_overlay") or {}
    j = jcfg(cfg)
    wl_cfg = cfg["wavelength"]
    only = {str(g) for g in groups} if groups else None
    by_group: dict = {}
    for s in scans:
        if getattr(s, "excluded", "") or s.role == "background":
            continue
        if only is not None and s.group not in only:
            continue
        by_group.setdefault(s.group, []).append(s)
    if not by_group:
        log.info("图a: 无可用光谱, 跳过")
        return []

    single = len(by_group) == 1
    made: list[Path] = []
    for g, ss in sorted(by_group.items()):
        ss = sorted(ss, key=lambda s: s.t)
        sel = ov.get("select_times", "all")
        if isinstance(sel, (list, tuple)) and sel:
            keep = [float(v) for v in sel]
            ss2 = [s for s in ss if any(abs(s.t - k) < 1e-6 for k in keep)] or ss
        elif isinstance(sel, dict):
            lo, hi = float(sel.get("tmin", -1e18)), float(sel.get("tmax", 1e18))
            ss2 = [s for s in ss if lo <= s.t <= hi] or ss
        else:
            ss2 = ss

        fig, (ax,) = new_figure(cfg, which="a")
        colors = _cmap_colors(cfg, len(ss2))
        ymax = 0.0
        for s, c in zip(ss2, colors):
            ax.plot(s.wl, s.a, lw=float(j["curve_lw"]), color=c,
                    solid_joinstyle="round", zorder=3)
            a = np.asarray(s.a, float)
            if np.isfinite(a).any():
                ymax = max(ymax, float(np.nanmax(a)))
        if not np.isfinite(ymax) or ymax <= 0:
            ymax = 1.0
        ytop = ymax * (1.0 + float(j["ylim_top_headroom"]))     # 顶部留 5% 余量

        # ── 探针波长：灰细虚线 + 顶端水平数字（无阴影带、无洋红粗线）──
        if j.get("mark_probe", True):
            lp = float(wl_cfg.get("probe", 379.0))
            ax.axvline(lp, ls="--", lw=float(j["annot_lw"]), color="gray",
                       zorder=2)
            _annot(ax, lp + float(j["probe_label_dx_nm"]),
                   ytop * float(j["probe_label_y_frac"]), f"{lp:.0f}", cfg,
                   color="gray", ha="left", va="top")

        # ── 染料吸收带：水平标注（取代竖排"荧光团最大吸收"）──
        if j.get("mark_fluorophore", True):
            lo, hi = _search_window(cfg, wl_cfg, j)
            s0 = ss2[0]
            m = ((s0.wl >= float(lo)) & (s0.wl <= float(hi))
                 & np.isfinite(np.asarray(s0.a, float)))
            if m.any():
                fpv = float(s0.wl[m][int(np.argmax(np.asarray(s0.a, float)[m]))])
                win = float(j.get("fluorophore_window", 4.0))
                pk = 0.0
                for s in ss2:
                    wl, a = np.asarray(s.wl, float), np.asarray(s.a, float)
                    w = np.abs(wl - fpv) <= win
                    if w.any() and np.isfinite(a[w]).any():
                        pk = max(pk, float(np.nanmax(a[w])))
                ax.axvline(fpv, ls=":", lw=float(j["annot_lw"]), color="gray",
                           zorder=2)
                label = _fluorophore_label(cfg, g, session)
                if label:
                    _annot(ax, fpv + float(j["fluorophore_label_dx_nm"]),
                           pk + float(j["fluorophore_label_dy_frac"]) * ytop,
                           label, cfg, color="0.25", ha="left", va="bottom",
                           zorder=4)
            else:
                log.warning(
                    "在 %.0f–%.0f nm 未找到可用的染料吸收峰（组「%s」）→ 不画该标注。"
                    "换染料体系时请修改 plot.jacs.fluorophore_search"
                    "（可填 auto 自动找全局最大峰）", float(lo), float(hi), g)

        # ── 激发波长：默认不画（写进图注）；开启时灰点划线 + "532 ex." ──
        if j.get("mark_excitation", False):
            le = float(wl_cfg.get("excitation", 532.0))
            ax.axvline(le, ls="-.", lw=float(j["annot_lw"]), color="gray",
                       zorder=2)
            _annot(ax, le + float(j["excitation_label_dx_nm"]),
                   ytop * float(j["excitation_label_y_frac"]),
                   f"{le:.0f} ex.", cfg, color="gray", ha="left", va="top")

        # ── 趋势箭头（取代 colorbar）──
        pairs = [(s.wl, s.a) for s in ss2]
        if j.get("trend_arrow", True) and len(ss2) >= 2:
            x0, top = _pick_arrow_column(cfg, pairs, j["xlim"])
            lo_y = top + float(j["arrow_y_lo_frac"]) * (ytop - top)
            hi_y = top + float(j["arrow_y_hi_frac"]) * (ytop - top)
            sign = _trend_sign([(float(s.t), s.wl, s.a) for s in ss2],
                               probe=float(wl_cfg.get("probe", 379.0)),
                               window=float(wl_cfg.get("probe_window", 2.0)))
            forced = str(j.get("trend_sign", "auto"))
            if forced == "up":
                sign = 1
            elif forced == "down":
                sign = -1
            if sign < 0:
                _trend_arrow(ax, x0, hi_y, lo_y, cfg)
            else:
                _trend_arrow(ax, x0, lo_y, hi_y, cfg)

        finish_axes(ax, cfg, xlim=j["xlim"], ylim=(0.0, ytop),
                    xstep=float(j["xtick_step"]))
        name = ("fig_a_spectra_overlay" if single
                else f"fig_a_spectra_{_safe(g)}")
        # T1：图注**先于**落盘收集 —— 这样 `save=lambda: []`（只要图注、不要文件）
        # 不会因为 `made` 为空而提前 return 把图注丢掉。
        if j.get("captions", True):
            captions.append(_caption_a(g, ss2, cfg, name, session))
        made += _save(fig, name)

    return made


def _caption_a(g: str, ss2: Sequence, cfg: dict, name: str,
               session: Optional[dict] = None) -> tuple:
    ov = (cfg.get("plot") or {}).get("spectra_overlay") or {}
    j = jcfg(cfg)
    ts = [float(s.t) for s in ss2]
    wl_cfg = cfg["wavelength"]
    parts = [
        f"**{name}**  UV–vis absorption spectra of *{g}* "
        f"({len(ss2)} spectra, t = {_fmt_range(ts, 's')}).",
        "Curves are coloured from light (earliest) to dark (latest). The vertical "
        "arrow indicates the direction in which the absorbance changes as "
        "irradiation proceeds (evaluated at the probe wavelength); there is "
        "deliberately no colour bar.",
    ]
    if j.get("mark_probe", True):
        parts.append(f"The dashed grey line marks the ABDA probe at "
                     f"{float(wl_cfg.get('probe', 379.0)):.0f} nm.")
    if not j.get("mark_excitation", False):
        parts.append(f"Excitation wavelength: "
                     f"{float(wl_cfg.get('excitation', 532.0)):.0f} nm.")
    if j.get("mark_fluorophore", True):
        lab = _fluorophore_label(cfg, g, session)
        if lab:
            parts.append(f'"{lab}" marks the dye absorption maximum.')
    b = cfg.get("baseline") or {}
    if b.get("enable", True):
        rng = b.get("wl_range", [650.0, 700.0])
        parts.append(f"All spectra were baseline-corrected on the "
                     f"{float(rng[0]):.0f}–{float(rng[1]):.0f} nm window.")
    ov_note = ov.get("select_times", "all")
    if ov_note != "all":
        parts.append(f"Time points plotted: {ov_note}.")
    return name, " ".join(parts)


# ═══════════════════════════ 图 b：动力学拟合 ═══════════════════════════
def fig_b_fit(g: str, e: dict, cfg: dict, figdir: Path,
              captions: list, save=None) -> list[Path]:
    from .plotting import _xvals
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    f = e.get("pooled")
    if f is None or f.status == "FAILED" or f.x is None or len(f.x) == 0:
        return []
    j = jcfg(cfg)
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    unit = "J$^{-1}$ cm$^{2}$" if dose else "s$^{-1}$"
    _tfac, _tunit = _time_factor()
    xlab = "Dose / J cm$^{-2}$" if dose else f"Time / {_tunit}"
    if dose:
        _tfac = 1.0

    fig, (ax,) = new_figure(cfg, which="b",
                            height_in=fig_height(cfg, "b",
                                                 float(j["height_in"]) * 0.98))
    x_all = _xvals(e, cfg) / _tfac
    R_all = np.concatenate([v["R"] for v in e["reps"].values()])
    m = np.isfinite(R_all)
    ax.plot(x_all[m], np.log(R_all[m]), ls="none", marker="o",
            ms=float(j["marker_size"]), mfc="none",
            mec=str(j["marker_color"]), mew=float(j["marker_edge_w"]), zorder=3)
    xx, yy = np.asarray(f.x, float) / _tfac, np.asarray(f.lnR, float)
    pred = f.intercept - f.k * xx * _tfac
    fit_c = str(j["fit_color"])
    ax.plot(xx, pred, "-", color=fit_c, lw=float(j["curve_lw"]), zorder=4)
    if f.n > 2:
        s = float(np.sqrt(np.sum((yy - pred) ** 2) / (f.n - 2)))
        xbar = float(xx.mean())
        sxx = float(np.sum((xx - xbar) ** 2))
        if sxx > 0:
            se = s * np.sqrt(1.0 / f.n + (xx - xbar) ** 2 / sxx)
            ax.fill_between(xx, pred - 1.96 * se, pred + 1.96 * se,
                            color=fit_c, alpha=float(j["band_alpha"]), lw=0,
                            zorder=2)
    if f.truncated:
        xcut, xmax = float(xx.max()), float(np.nanmax(x_all[m]))
        if xmax > xcut:
            ax.axvspan(xcut, xmax, color=str(j["trunc_shade_color"]),
                       alpha=float(j["trunc_shade_alpha"]), lw=0, zorder=1)
    if j.get("show_fit_stats_in_panel", False):
        _annot(ax, 0.04, 0.96,
               f"$k$ = {f.k:.3g} ± {f.k_err:.1g} {unit}\n$R^2$ = {f.r2:.4f}",
               cfg, ha="left", va="top", color="0.15",
               transform=ax.transAxes)
    finish_axes(ax, cfg, xlabel=xlab, ylabel=r"$\ln\,(A/A_0)$")
    name = f"fig_b_kinetics_{_safe(g)}"
    if j.get("captions", True):                 # T1：图注先收集（见 fig_a 注释）
        captions.append(_caption_b(g, e, f, cfg, name))
    made = _save(fig, name)
    return made


def _caption_b(g: str, e: dict, f, cfg: dict, name: str) -> tuple:
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    unit = "J$^{-1}$ cm$^{2}$" if dose else "s$^{-1}$"
    t = np.concatenate([v["t"] for v in e["reps"].values()])
    parts = [
        f"**{name}**  Pseudo-first-order kinetic analysis of *{g}* "
        f"(t = {_fmt_range(t, 's')}, n = {int(f.n)} points).",
        "Open circles are the measured $\\ln(A/A_0)$ values; the solid line is the "
        "least-squares fit and the shaded band is its 95% confidence interval.",
        f"$k_{{\\rm obs}}$ = {f.k:.4g} ± {f.k_err:.2g} {unit}, "
        f"$R^2$ = {f.r2:.4f}.",
    ]
    if f.truncated:
        parts.append("The grey-shaded region was automatically truncated: the "
                     "residuals curved significantly (quadratic term p < "
                     f"{float(cfg['kinetics'].get('curvature_p', 0.05)):g}), so only "
                     f"the earliest {float(cfg['kinetics'].get('truncate_frac', 0.7)):.0%} "
                     "of the points were used for the fit.")
    return name, " ".join(parts)


# ═══════════════════════════ 图 c：k 横向条形 ═══════════════════════════
def fig_c_bars(results: dict, cfg: dict, figdir: Path,
               captions: list, save=None) -> list[Path]:
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    items = [(g, e["pooled"]) for g, e in sorted(results.items())
             if e.get("pooled")]
    if not items:
        return []
    j = jcfg(cfg)
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    unit = "J$^{-1}$ cm$^{2}$" if dose else "s$^{-1}$"
    fig, (ax,) = new_figure(cfg, which="c",
                            height_in=max(1.6, float(j["bar_row_in"]) * len(items)
                                          + float(j["bar_base_in"])))
    colors = dict(j.get("status_colors") or {})
    colors.setdefault("OK", "steelblue")
    colors.setdefault("WARN", "darkorange")
    colors.setdefault("FAILED", "crimson")
    ys = np.arange(len(items))
    _style = _fig_style("c")
    _errs = bool(_FIG_SESSION.get("error_bars", True))
    _ann = bool(_FIG_SESSION.get("annotate", False))
    for y, (g, f) in zip(ys, items):
        if f.status != "FAILED" and np.isfinite(f.k):
            _err = (f.k_err if (np.isfinite(f.k_err) and _errs) else None)
            if _style == "line":
                ax.errorbar(f.k, y, xerr=_err, fmt="o-", ms=3.0,
                            color=colors.get(f.status, "gray"),
                            elinewidth=float(j["annot_lw"]), capsize=1.8)
            elif _style == "scatter":
                ax.errorbar(f.k, y, xerr=_err, fmt="o", ms=3.0,
                            color=colors.get(f.status, "gray"),
                            elinewidth=float(j["annot_lw"]), capsize=1.8)
            else:
                ax.barh(y, f.k, height=0.62, xerr=_err,
                        color=colors.get(f.status, "gray"), alpha=0.9,
                        error_kw=dict(lw=float(j["annot_lw"]), capsize=1.8,
                                      ecolor="0.25"))
            if _ann:
                _annot(ax, float(f.k), y, f"{float(f.k):.3g}", cfg,
                       color="0.25", ha="left", va="center")
        else:
            _annot(ax, 0.01, y, "fit failed", cfg,
                   color=str(j["fit_failed_color"]),
                   ha="left", va="center")
    ax.set_yticks(ys)
    ax.set_yticklabels([g for g, _ in items])
    ax.set_ylim(-0.6, len(items) - 0.4)
    finish_axes(ax, cfg, xlabel=f"$k_{{\\rm obs}}$ / {unit}", ylabel="")
    name = "fig_c_k_obs_bars"
    if j.get("captions", True):                 # T1：图注先收集（见 fig_a 注释）
        captions.append(_caption_c(items, name, unit))
    made = _save(fig, name)
    return made


def _caption_c(items: Sequence, name: str, unit: str) -> tuple:
    rows = "; ".join(
        f"*{g}*: $k_{{\\rm obs}}$ = {f.k:.4g} {unit}, $R^2$ = {f.r2:.4f}"
        + (" (flagged WARN)" if f.status == "WARN" else "")
        + (" (fit failed)" if f.status == "FAILED" else "")
        for g, f in items)
    return name, (f"**{name}**  Pseudo-first-order rate constants $k_{{\\rm obs}}$ "
                  f"for each group; error bars are the fit standard errors. {rows}.")


# ═══════════════════════════ 图 d：Φ 森林图 ═══════════════════════════
def fig_d_phi(phi: dict, cfg: dict, figdir: Path,
              captions: list, save=None) -> list[Path]:
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    items = [(g, p) for g, p in sorted((phi or {}).items())
             if p.get("status") == "OK" and np.isfinite(p.get("phi", np.nan))]
    if not items:
        log.info("图d: 无有效 Φ_Δ(缺参比组或拟合失败) → 跳过")
        return []
    j = jcfg(cfg)
    fig, (ax,) = new_figure(cfg, which="d",
                            height_in=max(1.6, float(j["bar_row_in"]) * len(items)
                                          + float(j["bar_base_in"])))
    ys = np.arange(len(items))
    dot_c = str(j["fit_color"])
    _style = _fig_style("d")
    _errs = bool(_FIG_SESSION.get("error_bars", True))
    _ann = bool(_FIG_SESSION.get("annotate", False))
    _vals = [float(p["phi"]) for _, p in items]
    _err = [float(p.get("phi_err") or 0.0) for _, p in items] if _errs else None
    if _style == "bar":
        ax.barh(ys, _vals, height=0.62, xerr=_err, color=dot_c, alpha=0.85,
                error_kw=dict(lw=float(j["annot_lw"]), capsize=1.8,
                              ecolor="0.25"))
    elif _style == "line":
        ax.errorbar(_vals, ys, xerr=_err, fmt="o-", ms=float(j["marker_size"]) + 0.3,
                    mfc=dot_c, mec=dot_c, mew=float(j["marker_edge_w"]),
                    ecolor=dot_c, elinewidth=float(j["annot_lw"]), capsize=1.8,
                    zorder=3)
    else:
        ax.errorbar(_vals, ys, xerr=_err, fmt="o",
                    ms=float(j["marker_size"]) + 0.3, mfc=dot_c,
                    mec=dot_c, mew=float(j["marker_edge_w"]),
                    ecolor=dot_c, elinewidth=float(j["annot_lw"]), capsize=1.8,
                    ls="none", zorder=3)
    if _ann:
        for _x, _y in zip(_vals, ys):
            _annot(ax, _x, _y, f"{_x:.3g}", cfg, color="0.25",
                   ha="left", va="center")
    phi_ref = float(cfg["quantum_yield"].get("phi_ref", 0.75))
    ax.axvline(phi_ref, ls="--", lw=float(j["annot_lw"]), color="gray", zorder=2)
    if _FIG_SESSION.get("ci_band"):
        lows = [float(p.get("phi_ci_low")) if p.get("phi_ci_low") is not None else np.nan
                for _, p in items]
        highs = [float(p.get("phi_ci_high")) if p.get("phi_ci_high") is not None else np.nan
                 for _, p in items]
        if np.isfinite(lows).any() and np.isfinite(highs).any():
            ax.fill_betweenx(ys, lows, highs, color=dot_c, alpha=0.16, zorder=1)
    ax.set_yticks(ys)
    ax.set_yticklabels([g for g, _ in items])
    ax.set_ylim(-0.6, len(items) - 0.4)
    finish_axes(ax, cfg, xlabel="Relative quantum yield $\\Phi_\\Delta$",
                ylabel="")
    _annot(ax, phi_ref, len(items) - 0.45, r" $\Phi_{\rm ref}$", cfg,
           color="gray", ha="left", va="center")
    name = "fig_d_phi_forest"
    if j.get("captions", True):                 # T1：图注先收集（见 fig_a 注释）
        captions.append(_caption_d(items, name, phi_ref))
    made = _save(fig, name)
    return made


def _caption_d(items: Sequence, name: str, phi_ref: float) -> tuple:
    rows = "; ".join(
        f"*{g}*: $\\Phi_\\Delta$ = {p['phi']:.3f} ± "
        f"{float(p.get('phi_err') or 0):.3f}" for g, p in items)
    return name, (f"**{name}**  Relative singlet-oxygen quantum yields "
                  f"$\\Phi_\\Delta$ (ABDA probe). Error bars propagate the "
                  f"uncertainties of $k$, the absorbed fraction and the reference. "
                  f"The dashed line is the reference value "
                  f"$\\Phi_{{\\rm ref}}$ = {phi_ref:g}. {rows}.")


# ═══════════════════════════ 图 e：残差 ═══════════════════════════
def fig_e_residuals(results: dict, cfg: dict, figdir: Path,
                    captions: list, save=None) -> list[Path]:
    from .plotting import _xvals
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    fits = [(g, e["pooled"]) for g, e in sorted(results.items())
            if e.get("pooled") and e["pooled"].status != "FAILED"
            and e["pooled"].x is not None and len(e["pooled"].x)]
    if not fits:
        return []
    j = jcfg(cfg)
    dose = bool(cfg["kinetics"].get("dose_normalize", False))
    fig, axes = new_figure(cfg, which="e",
                           height_in=max(1.7, 1.55 * len(fits)),
                           nrows=len(fits))
    _tfac, _tunit = _time_factor()
    if dose:
        _tfac = 1.0
    for k, (ax, (g, f)) in enumerate(zip(axes, fits)):
        xx_raw = np.asarray(f.x, float)
        xx, yy = xx_raw / _tfac, np.asarray(f.lnR, float)
        ax.plot(xx, yy - (f.intercept - f.k * xx_raw), "o-", ms=float(j["marker_size"]),
                lw=float(j["curve_lw"]) * float(j["second_lw_frac"]),
                color=str(j["marker_color"]), zorder=3)
        ax.axhline(0.0, color=str(j["zero_line_color"]),
                   lw=float(j["annot_lw"]) * float(j["zero_lw_frac"]), zorder=2)
        if f.truncated:
            x_all_raw = _xvals(results[g], cfg)
            x_all = x_all_raw / _tfac
            R_all = np.concatenate([v["R"] for v in results[g]["reps"].values()])
            mm = np.isfinite(R_all) & (x_all > float(xx.max()))
            if mm.any():
                ax.plot(x_all[mm],
                        np.log(R_all[mm]) - (f.intercept - f.k * x_all_raw[mm]),
                        ls="none", marker="o", ms=float(j["marker_size"]),
                        mfc="none", mec=str(j["trunc_marker_color"]),
                        mew=float(j["marker_edge_w"]), zorder=4)
            ax.axvspan(float(xx.max()), float(np.nanmax(x_all)),
                       color=str(j["trunc_shade_color"]),
                       alpha=float(j["trunc_shade_alpha"]), lw=0, zorder=1)
        # 面板编号 (a)(b)(c)… 取代 legacy 的图内标题，组名对应关系写进图注
        _annot(ax, 0.015, 0.95, str(j["panel_label_fmt"]).format(chr(ord('a') + k)),
               cfg, ha="left", va="top", color="0.15", transform=ax.transAxes)
        finish_axes(ax, cfg, ylabel="Residual",
                    xlabel=("Dose / J cm$^{-2}$" if dose else f"Time / {_tunit}")
                    if k == len(fits) - 1 else "")
    name = "fig_e_residuals"
    made = _save(fig, name)
    if j.get("captions", True):
        panel = ", ".join(f"({chr(ord('a') + i)}) *{g}*"
                          for i, (g, _) in enumerate(fits))
        txt = (f"**{name}**  Residuals of the pseudo-first-order fits "
               f"($\\ln(A/A_0)$ minus the fitted line). Panels: {panel}. "
               "Open circles are data points that were automatically truncated and "
               "therefore excluded from the final fit; the grey-shaded region marks "
               "the truncated interval.")
        captions.append((name, txt))
    return made


# ═══════════════════════════ JACS 堆叠图 ═══════════════════════════
def jacs_stack_figure(entries: Sequence[tuple], lam: float, figdir: Path,
                      cfg: Optional[dict] = None, tag: str = "group",
                      captions: Optional[list] = None,
                      group: Optional[str] = None, save=None) -> list[Path]:
    """``entries: [(t, wl, ab)]`` → JACS 单栏堆叠图（无 colorbar，箭头表示时间）。

    与 legacy 版本的差别：去 colorbar、去标题、刻度朝内、四边完整框、
    固定 82.5 mm 宽、同时出 png+pdf。``group`` 给出时染料标注按注册表解析。
    """
    if not entries:
        return []
    j = jcfg(cfg)
    entries = sorted(entries, key=lambda e: float(e[0]))
    n = len(entries)
    step = 1.0 / max(n - 1, 1)
    colors = _cmap_colors(cfg, n)

    fig, (ax,) = new_figure(cfg, which="stack")
    ymax_seen = 0.0
    for i, (t, wl, ab) in enumerate(entries):
        wl, ab = np.asarray(wl, float), np.asarray(ab, float)
        ax.plot(wl, ab + i * step, lw=float(j["curve_lw"]), color=colors[i],
                solid_joinstyle="round", zorder=3)
        if np.isfinite(ab).any():
            ymax_seen = max(ymax_seen, float(np.nanmax(ab)) + i * step)
    if not np.isfinite(ymax_seen) or ymax_seen <= 0:
        ymax_seen = (n - 1) * step + 1.0
    # 顶部留出箭头空间（无 colorbar 后时间信息由箭头承担）
    ytop = ymax_seen * float(j.get("stack_top_frac", 1.15))

    if j.get("mark_probe", True):
        ax.axvline(float(lam), ls="--", lw=float(j["annot_lw"]), color="gray",
                   zorder=2)
        _annot(ax, float(lam) + float(j["probe_label_dx_nm"]),
               ytop * float(j["probe_label_y_frac"]), f"{float(lam):.0f}",
               cfg, color="gray", ha="left", va="top")

    if j.get("trend_arrow", True) and n >= 2:
        x_lo, x_hi = float(j["xlim"][0]), float(j["xlim"][1])
        xa = x_lo + float(j.get("stack_arrow_dx_frac", 0.46)) * (x_hi - x_lo)
        _trend_arrow(ax, xa, ymax_seen + 0.02 * (ytop - ymax_seen),
                     ytop * 0.97, cfg)

    finish_axes(ax, cfg, ylabel=str(j.get("ylabel_stack", "Absorbance (a.u.)")),
                xlim=j["xlim"], ylim=(0.0, ytop), xstep=float(j["xtick_step"]))
    figdir = Path(figdir)
    figdir.mkdir(parents=True, exist_ok=True)
    name = f"jacs_stack_{tag}"
    made = (save or (lambda fig, nm: save_jacs(fig, figdir, nm, cfg)))(fig, name)
    if captions is not None and j.get("captions", True):
        ts = [float(e[0]) for e in entries]
        captions.append((
            name,
            f"**{name}**  Stacked UV–vis absorption spectra "
            f"(t = {_fmt_range(ts, 's')}, {n} spectra). Successive spectra are "
            f"offset vertically by a constant amount for clarity, so the ordinate "
            f"is in arbitrary units; the arrow marks the direction of increasing "
            f"irradiation time. The dashed grey line marks "
            f"{float(lam):.0f} nm."))
    return made


def _stack_tag(group: str) -> str:
    """堆叠图文件名里的组名片段（``jacs_stack_<tag>``）。"""
    return _safe(group)[:30] or "group"


def jacs_stack_all(scans, cfg: dict, run_dir: Path,
                   captions: Optional[list] = None,
                   figdir: Optional[Path] = None,
                   save=None, group_dye: Optional[dict] = None,
                   groups: Optional[Sequence[str]] = None) -> list[Path]:
    """每组一张 JACS 堆叠图 → ``figdir``（默认 ``run_dir/figures``）。

    ``groups`` 限定只画这些组（P2 多进程渲染按组切分任务用；None = 全部）。
    """
    from ..inputs.io import spectrum_arrays
    lam = cfg["wavelength"]["probe"]
    only = {str(g) for g in groups} if groups else None
    by_g: dict = {}
    for s in scans:
        if getattr(s, "excluded", "") or s.role in ("background", "dark"):
            continue
        if only is not None and s.group not in only:
            continue
        by_g.setdefault(s.group, []).append(s)
    figdir = Path(figdir) if figdir is not None else Path(run_dir) / "figures"
    _save = save or (lambda fig, name: save_jacs(fig, figdir, name, cfg))
    need = int(jcfg(cfg).get("stack_min_spectra", 2))
    made: list[Path] = []
    for g in sorted(by_g):
        arrs = []
        for s in by_g[g]:
            a = spectrum_arrays(s)
            if a is not None:
                arrs.append((float(s.t), a[0], a[1]))
        if len(arrs) < need:
            continue
        made += jacs_stack_figure(arrs, lam, figdir, cfg=cfg,
                                  tag=_stack_tag(g),
                                  captions=captions, group=g, save=_save)
    if made:
        log.info("JACS 堆叠图 x%d -> %s", len(made), figdir)
    return made


# ═══════════════════════════ 图注落盘 ═══════════════════════════
def write_captions(captions: Sequence[tuple], figdir: Path) -> Optional[Path]:
    """把图注写到 ``figures/captions.md``（投稿时直接复制进正文）。"""
    if not captions:
        return None
    figdir = Path(figdir)
    figdir.mkdir(parents=True, exist_ok=True)
    p = figdir / "captions.md"
    lines = [
        "# 图注 / Figure captions",
        "",
        "> 本文件由 `core/plotting_jacs.py` 自动生成（JACS 风格图件不含图内标题，",
        "> 说明性文字统一放在这里）。投稿时按需翻译/润色后复制进正文。",
        "",
    ]
    for name, text in captions:
        lines += [f"## {name}", "", text, ""]
    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("已写出图注: %s (%d 条)", p, len(captions))
    return p


# ═══════════════════════════ 总入口 ═══════════════════════════
#: 参数区 → 受影响图的映射（FR-6.7 增量重渲用）
FIGURE_DEPS: dict[str, tuple] = {
    "a": ("wavelength", "baseline", "background", "a0", "io", "filename"),
    "b": ("wavelength", "baseline", "background", "a0", "kinetics"),
    "c": ("kinetics",),
    "d": ("quantum_yield", "kinetics"),
    "e": ("kinetics",),
    "stack": ("wavelength", "baseline", "background", "io"),
}

#: 全部图步骤（预览档按此顺序补齐；"a" 永远排第一 → 首图最快可见）
STEP_ORDER = ("a", "b", "c", "d", "e", "stack")

#: 步骤 → 图内展示名（GUI 图件下拉/懒渲染进度提示用）
FIGURE_LABELS: dict[str, str] = {
    "a": "图 a 光谱叠加",
    "b": "图 b 动力学拟合",
    "c": "图 c k_obs 柱状",
    "d": "图 d Φ_Δ 森林图",
    "e": "图 e 残差",
    "stack": "图 堆叠光谱",
}


def _preview_first(cfg: Optional[dict]) -> list[str]:
    """懒渲染首批画哪些步骤（``plot.jacs.preview_first``，默认 ``"a"``）。

    支持逗号分隔的多步骤（如 ``"a,stack"``）；非法值一律回退 ``["a"]``。
    """
    raw = str(jcfg(cfg).get("preview_first", "a") or "a")
    want = [x.strip().lower() for x in raw.replace("，", ",").split(",") if x.strip()]
    out = [s for s in want if s in STEP_ORDER]
    return out or ["a"]


def figure_steps(scans, results: Optional[dict] = None,
                 cfg: Optional[dict] = None) -> list[str]:
    """**将要**渲染的步骤序列（按渲染优先级），不碰 matplotlib。

    与 :func:`expected_figures` 的区别：这里只回答"哪些步骤"，用于决定懒渲染
    第二批要不要跑图 d/e（没有 Φ_Δ 就没什么可画）。legacy 风格返回 ``[]``
    （它画什么由 legacy 自己的循环决定，且为 A4.2 回归红线必须整批渲染）。
    """
    if plot_style(cfg) != "jacs":
        return []
    j = jcfg(cfg)
    by_g: dict = {}
    for s in scans or []:
        if getattr(s, "excluded", "") or s.role in ("background", "dark"):
            continue
        by_g.setdefault(s.group, []).append(s)
    need = int(j.get("stack_min_spectra", 2))
    steps = ["a"] if by_g else []
    if results:
        steps.append("b")
    if results or by_g:
        steps.append("c")
    steps.append("d")          # 渲染器自己判断有没有可用 Φ_Δ
    steps.append("e")
    if any(len(v) >= need for v in by_g.values()):
        steps.append("stack")
    return steps


def _fig_a_names(groups: Sequence[str]) -> list[str]:
    """图 a 会写出的图件名（**唯一真相**，供渲染、并行切分与预期清单共用）。

    注意命名特例：只画**一个**组时文件名是 ``fig_a_spectra_overlay``（历史行为，
    与"叠加所有组"的语义区分）；多组时才是 ``fig_a_spectra_<组名>``。

    ⚠ **由此产生一条约束（M14）**：图 a **不能按组切分渲染**——两组分别渲染时
    各自都是"单组"，文件名都会是 ``fig_a_spectra_overlay``，后画的那张会覆盖前一张。
    所以 :func:`core.parallel_render.render_units` 对 ``a`` 只切单个任务，
    ``groups=[g]`` 过滤也只在确实只画一组时才用（父进程调用不受影响）。
    """
    gs = sorted(str(g) for g in groups)
    if not gs:
        return []
    if len(gs) == 1:
        return ["fig_a_spectra_overlay"]
    return [f"fig_a_spectra_{_safe(g)}" for g in gs]


def _ui_groups(scans, results: Optional[dict] = None) -> set:
    """参与出图的组名集合。

    与渲染器 :func:`fig_a_overlay` / :func:`jacs_stack_all` 的口径**必须一致**：
    只剔 ``excluded`` 与 ``background``（**dark 组仍然出图**——``_fit_all`` 会让
    dark 组进 results）。

    M17：``expected_figures`` 原来多剔了 dark，于是有 dark 组的 run 里"预期清单"
    会给出用不上的特例名 ``fig_a_spectra_overlay`` 并漏掉图 a，实际却多出若干张、
    名字全对不上 → 懒渲染的"共 m 张"计数与占位整体错位。
    """
    groups = {str(s.group) for s in scans or []
              if not getattr(s, "excluded", "") and s.role != "background"}
    if not groups:
        groups = {str(g) for g in (results or {})}
    return groups


def expected_figures(scans, results: Optional[dict] = None,
                     phi: Optional[dict] = None, cfg: Optional[dict] = None,
                     export_mode: str = "preview") -> list[str]:
    """本次运行**完整**的图件名清单（按渲染优先级排序，去重）。

    用途有且只有一个：GUI 在懒渲染时能先把"排队中"的图位占好（下拉里显示
    "（渲染中…）"），渲染完一张替换一张——否则用户在补渲期间看到的图件数量会
    忽多忽少。这里给的是**保守超集**：图 d 只在真有可用 Φ_Δ 时才可能产出，
    故仅在 ``phi`` 里确有 ``status == "OK"`` 的组时列出；其余步骤按现有数据
    推断（宁可多列一个空位，也不要在补渲后凭空多出一张图）。
    """
    if plot_style(cfg) != "jacs":
        return []
    j = jcfg(cfg)
    names: list[tuple[str, str]] = []
    by_g: dict = {}                            # 图 a 口径（dark 组也画）
    for s in scans or []:
        # M17：与渲染器同口径（只剔 excluded/background，dark 组照样出图 a）
        if getattr(s, "excluded", "") or s.role == "background":
            continue
        by_g.setdefault(s.group, []).append(s)
    # 堆叠图另有一套口径：`jacs_stack_all` 连 dark 也剔掉（见其实现）
    st_g: dict = {}
    for s in scans or []:
        if getattr(s, "excluded", "") or s.role in ("background", "dark"):
            continue
        st_g.setdefault(s.group, []).append(s)

    # 图 a：每组一张（单组时文件名特例见 _fig_a_names）
    for nm in _fig_a_names(sorted(by_g)):
        names.append(("a", nm))
    # 图 b：每个有拟合结果的组一张
    for g in sorted(results or {}):
        names.append(("b", f"fig_b_kinetics_{_safe(g)}"))
    names.append(("c", "fig_c_k_obs_bars"))
    # 图 d：仅当确有可用的 Φ_Δ（否则渲染器会跳过、不留空位）
    if any((p or {}).get("status") == "OK" for p in (phi or {}).values()):
        names.append(("d", "fig_d_phi_forest"))
    names.append(("e", "fig_e_residuals"))
    # 堆叠图：每组 ≥ stack_min_spectra 张有效谱才画（按堆叠图自己的口径数）
    need = int(j.get("stack_min_spectra", 2))
    for g in sorted(st_g):
        if len(st_g[g]) >= need:
            names.append(("stack", f"jacs_stack_{_stack_tag(g)}"))

    rank = {s: i for i, s in enumerate(STEP_ORDER)}
    names.sort(key=lambda t: (rank.get(t[0], 99), t[1]))
    seen: set[str] = set()
    return [n for _s, n in names if not (n in seen or seen.add(n))]


#: ``plot.jacs`` 键前缀 → 受影响的图步骤（V2.x 审查 M5 / 优化清单 #4）。
#:
#: 历史实现：**任何** plot 改动一律返回全部图 → V2.3 C 道"只重渲受影响图"实际
#: 是全量重渲（只省了数值重算），按钮文案偏乐观。
#: 这里只对**语义明确**的键做细分；**任何未列出的键**（以及 size/font/dpi 这类
#: 全局版式键）一律回落到"全部图"——**漏渲的代价（用户看到旧图）远大于多渲几张**。
JACS_KEY_STEPS: tuple = (
    # 逐图高度：只影响那一张的高度
    ("fig_heights.a", ("a",)),
    ("fig_heights.b", ("b",)),
    ("fig_heights.c", ("c",)),
    ("fig_heights.d", ("d",)),
    ("fig_heights.e", ("e",)),
    ("fig_heights.stack", ("stack",)),
    # 堆叠图专属版式
    ("stack_", ("stack",)),
    # 色带只用于谱线着色（图 a 的叠加图 + 堆叠图）
    ("cmap", ("a", "stack")),
    ("cmap_lo", ("a", "stack")),
    ("cmap_hi", ("a", "stack")),
    # 趋势箭头画在图 a 与堆叠图上
    ("arrow_", ("a", "stack")),
)


def _steps_for_jacs_keys(keys: Sequence[str]) -> Optional[list]:
    """``plot.jacs`` 的若干个改动键 → 受影响步骤；**任一键无法归因 → None（全部）**。

    Args:
        keys: ``plot.jacs`` 下的**叶子键名或点分路径**（如 ``"cmap"``、
            ``"fig_heights.a"``、``"stack_top_frac"``）。

    Returns:
        步骤列表；``None`` 表示"保守起见按全部图处理"。
    """
    if not keys:
        return None
    steps: set = set()
    for k in keys:
        hit = None
        for prefix, step_names in JACS_KEY_STEPS:
            if k == prefix or k.startswith(prefix if prefix.endswith(".")
                                           else prefix):
                hit = step_names
                break
        if hit is None:
            return None                    # 有一个归不因就不赌，全量重渲
        steps |= set(hit)
    return [s for s in STEP_ORDER if s in steps] or None


def affected_figures(cfg: dict, changed_sections: Sequence[str],
                     changed_keys: Optional[Sequence[str]] = None) -> list[str]:
    """哪些图需要重渲（改参数后"再看一眼"时用，FR-6.7）。

    策略（V2.x 审查 M5 起）：

    * 数值区改动 → 按 :data:`FIGURE_DEPS` 判定；
    * ``spectra`` 改动 → 全部图（作图台参数影响面未细分，保守）；
    * ``plot`` 改动 → 若给了 ``changed_keys``（``plot.jacs`` 叶子键）且**每个键
      都能归因**，只返回对应步骤；否则全部图。

    Args:
        cfg: 完整配置（当前实现不读它，保留给将来按值判定）。
        changed_sections: 顶层分区名列表。
        changed_keys: 可选，``plot.jacs`` 内的叶子键/点分路径列表。
    """
    _ = cfg
    secs = set(changed_sections or ())
    if not secs:
        return list(STEP_ORDER)
    if "spectra" in secs:
        return list(STEP_ORDER)
    if "plot" in secs:
        only = _steps_for_jacs_keys(list(changed_keys or ()))
        return only if only else list(STEP_ORDER)
    out = [s for s in STEP_ORDER if secs & set(FIGURE_DEPS.get(s, ()))]
    return out or list(STEP_ORDER)


def _render_key(figdir, ctx_key: str, name: str, cfg: dict, dpi: int,
                fmts: Sequence[str], label_key: str = "") -> tuple:
    """渲染复用缓存的键（**串行与并行必须用同一个**，见 ``_save``）。

    键里带了输出目录、dpi 与格式：换 run 目录或换档位都不会误用旧图。
    S5 起还带 ``label_key``（每组解析出的染料标注文本）——否则改染料名后会命中
    旧图，PNG 与新图注不一致。
    """
    return (str(figdir), ctx_key, _step_of(name), str(name),
            _param_key(cfg, _step_of(name)), int(dpi), tuple(fmts), label_key)


def _should_parallel(j: dict, want: set, results: dict, scans, export_mode: str,
                     only_groups) -> bool:
    """要不要走多进程出图：既要开开关，也要**估得出收益**。

    Windows 上每个子进程要重新 import matplotlib+numpy（实测约 0.7 s），所以
    小批量（样例 2 组 8 张）并行反而更慢。这里用任务数与粗估工时双门槛拦住它们：
    只有"任务够多、串行够久"（默认 ≥ ``parallel_min_tasks`` 且估算 ≥ 3.0 s）才并行。
    """
    if not bool(j.get("parallel", True)):
        return False
    if os.environ.get("DYE_NO_PARALLEL") == "1":       # 排查用：强制串行
        return False
    if only_groups is not None:
        return False                                   # 已经是子任务 → 不再分叉
    from .parallel_render import estimate_seconds, render_units
    units = render_units(sorted(want), results, scans)
    if len(units) < int(j.get("parallel_min_tasks", 8)):
        return False
    return estimate_seconds(units, export_mode) >= float(
        j.get("parallel_min_seconds", 3.0))


def _close_all_figures() -> int:
    """关掉当前进程里所有打开的 Figure，返回关掉的数量。

    M2：``_write_captions_text`` 只为拼图注而调用各个 ``fig_*``，它们内部会
    ``new_figure()``；用 ``save=lambda *a: []`` 收掉返回值后**没人 close**，
    于是每跑一次并行定稿就泄漏"组数 + 图b + 堆叠图"个 Figure（实测残留 8 个），
    触发 matplotlib 的 "More than 20 figures have been opened" 警告、长会话内存增长。
    渲染路径本来就会在 ``save_jacs`` 里 close，所以这里统一清一次是安全的。
    """
    try:
        plt = _plt()
        n = 0
        for num in list(plt.get_fignums()):
            try:
                plt.close(plt.figure(num))
                n += 1
            except Exception:                       # noqa: BLE001
                pass
        return n
    except Exception:                               # noqa: BLE001
        return 0


def _write_captions_text(scans, results: dict, phi: dict, cfg: dict,
                         figdir, session: Optional[dict] = None) -> Optional[Path]:
    """**不画图**、只按当前数据与参数重新生成图注并落盘（并行路径专用）。

    并行路径里每个子进程只画一步，图注列表天然只是"半份"；让子进程写会互相覆盖
    成残缺版本。所以并行档的图注统一由父进程用本函数补写——只跑生成图注所需的
    计算（谱数组统计、拟合数字格式化），不建任何长期存活的 Figure。
    """
    j = jcfg(cfg)
    _close_all_figures()                        # 干净起点，便于下面精确收尾
    caps: list[tuple] = []
    try:
        fig_a_overlay(scans, cfg, Path(figdir), caps, save=lambda *a: [],
                      session=session)
    except Exception as err:                            # noqa: BLE001
        log.debug("图注重算（图 a）失败: %s", err)
    for g, e in (results or {}).items():
        try:
            fig_b_fit(g, e, cfg, Path(figdir), caps, save=lambda *a: [])
        except Exception as err:                        # noqa: BLE001
            log.debug("图注重算（图 b/%s）失败: %s", g, err)
    for fn, args in ((fig_c_bars, (results, cfg, Path(figdir), caps)),
                     (fig_d_phi, (phi, cfg, Path(figdir), caps)),
                     (fig_e_residuals, (results, cfg, Path(figdir), caps))):
        try:
            fn(*args, save=lambda *a: [])
        except Exception as err:                        # noqa: BLE001
            log.debug("图注重算（%s）失败: %s", fn.__name__, err)
    try:
        jacs_stack_all(scans, cfg, Path(figdir), caps, figdir=Path(figdir),
                       save=lambda *a: [])
    except Exception as err:                            # noqa: BLE001
        log.debug("图注重算（堆叠图）失败: %s", err)
    leaked = _close_all_figures()               # M2：把上面为拼图注而建的 Figure 全清掉
    if leaked:
        log.debug("图注重算已关闭 %d 个临时 Figure（避免泄漏）", leaked)
    if not j.get("captions", True) or not caps:
        return None
    return write_captions(caps, Path(figdir))


def _render_parallel(scans, results: dict, phi: dict, controls: dict, cfg: dict,
                     run_dir, export_mode: str, session: Optional[dict],
                     only_groups, want: set) -> Optional[list[Path]]:
    """多进程整批出图；返回写出的文件（``None`` = 并行不可用，请调用方走串行）。

    缓存语义与串行一致：命中父进程渲染缓存的**任务不派发**（FR-6.7），子进程的结果
    回填进同一份缓存。单任务失败只记账、不影响其余图（与串行约定相同）。
    """
    from .parallel_render import render_units, render_units_parallel
    j = jcfg(cfg)
    try:
        import multiprocessing as _mp
        if _mp.current_process().daemon:
            log.info("当前进程是 daemon → 不出图到子进程（多进程子进程不能再 fork）")
            return None
    except Exception as err:                            # noqa: BLE001
        log.info("多进程不可用（%s）→ 回退串行出图", err)
        return None
    preview = str(export_mode).lower() == "preview"
    figdir = Path(run_dir) / ("figures_preview" if preview else "figures")
    from .plotstyle import filter_formats as _ff
    fmts = ["png"] if preview else _ff(j["formats"])
    dpi = min(int(j["dpi"]), int(j.get("preview_dpi", 150))) if preview \
        else int(j["dpi"])
    reuse = bool(j.get("reuse_rendered", True))
    ctx = _data_fingerprint(scans, results, phi) if reuse else ""
    lab = _label_key(scans, results, cfg, session) if reuse else ""

    units = render_units(sorted(want), results, scans)
    # ① 先把命中缓存的任务挑出来（不派发）；缓存键与串行路径共用 _render_key
    todo: list = []
    cached_made: list[Path] = []
    for u in units:
        names = _unit_figure_names(u, scans, results, cfg, j) if reuse else []
        hit = bool(names)
        got: list[Path] = []
        for nm in names:
            cached = _rcache_get(
                _render_key(figdir, ctx, nm, cfg, dpi, fmts, lab))
            if not cached or not all(Path(p).exists() for p in cached):
                hit = False
                break
            got += list(cached)
        if hit:
            cached_made += got
        else:
            todo.append(u)

    if not todo:
        log.info("并行出图：%d 个任务全部命中渲染缓存，未派发子进程", len(units))
        return cached_made

    log.info("并行出图：%d 个任务（%d 个命中缓存未派发）", len(todo),
             len(units) - len(todo))
    try:
        got_map, errs = render_units_parallel(
            todo, scans, results, phi, controls, cfg, run_dir,
            export_mode=export_mode, group_dye=session,
            workers=int(j.get("parallel_workers", 0) or 0))
    except Exception as err:                            # noqa: BLE001 —— 并行失败退回串行
        log.warning("并行出图不可用（%s）→ 回退串行", err)
        return None
    for e in errs:
        log.error("并行出图任务失败（继续其余图）: %s", e)

    # ② 按**步骤顺序**汇总结果（顺序决定"哪张图先出现"），并回填缓存
    made: list[Path] = []
    by_stem: dict = {}                       # 图名 → 本次写出的全部格式
    for u in units:
        paths = got_map.get(u)
        if paths is None:
            continue
        for p in paths:
            if Path(p).exists():
                made.append(Path(p))
                by_stem.setdefault(Path(p).stem, []).append(Path(p))
    # M1：缓存**值**是"这一步写出的全部文件"，必须按图名整体回填。原来是逐文件
    # 用同一个键覆盖 → 多格式（png+svg）时只剩最后一种，之后复用会缺一半清单。
    if reuse:
        for stem, plist in by_stem.items():
            _rcache_put(_render_key(figdir, ctx, stem, cfg, dpi, fmts, lab),
                        sorted(plist, key=lambda q: q.suffix))
    return made + [p for p in cached_made if p not in made]


def _unit_figure_names(unit, scans, results: dict, cfg: dict,
                       j: dict) -> list[str]:
    """一个并行任务**会**写出的图件名（用于预查渲染缓存；宁多勿少）。"""
    from ..inputs.io import spectrum_arrays  # noqa: F401
    step, group = str(unit[0]), unit[1]
    if step == "a":
        gs = [group] if group else sorted(
            {s.group for s in scans or []
             if not getattr(s, "excluded", "") and s.role != "background"})
        return _fig_a_names(gs)
    if step == "b":
        return [f"fig_b_kinetics_{_safe(group)}"] if group else [
            f"fig_b_kinetics_{_safe(g)}" for g in sorted(results or {})]
    if step == "c":
        return ["fig_c_k_obs_bars"]
    if step == "d":
        return ["fig_d_phi_forest"]
    if step == "e":
        return ["fig_e_residuals"]
    if step == "stack":
        gs = [group] if group else sorted(
            {s.group for s in scans or []
             if not getattr(s, "excluded", "") and s.role not in ("background", "dark")})
        need = int(j.get("stack_min_spectra", 2))
        out = []
        for g in gs:
            n = sum(1 for s in scans or [] if s.group == g
                    and not getattr(s, "excluded", "")
                    and s.role not in ("background", "dark")
                    and spectrum_arrays(s) is not None)
            if n >= need:
                out.append(f"jacs_stack_{_stack_tag(g)}")
        return out
    return []


def plot_all_jacs(scans, results: dict, phi: dict, controls: dict, cfg: dict,
                  run_dir: Path, want_paths: bool = False,
                  export_mode: str = "final",
                  only: Optional[Sequence[str]] = None,
                  group_dye: Optional[dict] = None,
                  groups: Optional[Sequence[str]] = None,
                  with_captions: bool = True) -> Optional[list[Path]]:
    """JACS 风格出图总入口：图 a–e + 堆叠图 + ``captions.md``。

    Args:
        export_mode: ``"final"``（默认，600 dpi png + captions，与 V1.0 逐像素一致）
            或 ``"preview"``（低 dpi png、无图注 → ``figures_preview/``）。
        only: 只渲这些步骤（``{"a","b",...}``）；None = 全部。用于懒渲染/增量重渲。
            图 b 是**逐组**的：给了 ``"b"`` 就渲所有组的图 b（只挑一组请配合 ``groups``）。
        group_dye: ``{组名: 染料名}`` 覆盖（GUI 会话命名；默认从 cfg 取）。
        groups: 只渲这些组（P2 多进程按组切分任务用；None = 全部组）。
        with_captions: ``False`` 时不写 ``captions.md``。**名字不能叫
            ``write_captions``**——那会遮蔽本模块同名的写图注函数（踩过这个坑）。

    与 legacy 路径一样，单张图失败不中断其余图。
    **不再输出 PDF**（V1.2）：PDF 的 TrueType 子集化占导出耗时一半以上。
    """
    apply_jacs_style()
    ensure_font(cfg)
    j = jcfg(cfg)
    preview = str(export_mode).lower() == "preview"
    figdir = Path(run_dir) / ("figures_preview" if preview else "figures")
    figdir.mkdir(parents=True, exist_ok=True)
    captions: list[tuple] = []
    made: list[Path] = []
    only_groups = {str(g) for g in groups} if groups else None
    # 会话染料命名：$cfg 保持不可变（P2 并行时每个渲染单元共用同一份 cfg 对象，
    # 就地写私有键会互相干扰），改由参数逐层传递
    session = dict(group_dye) if group_dye else None

    from .plotstyle import filter_formats as _ff
    fmts = ["png"] if preview else _ff(j["formats"])
    dpi = min(int(j["dpi"]), int(j.get("preview_dpi", 150))) if preview \
        else int(j["dpi"])
    if preview:
        log.info("预览档（非投稿分辨率）：%d dpi、仅 png、不写图注", dpi)
    else:
        log.info("定稿档：%d dpi、格式 %s", dpi, "|".join(fmts))
    # 渲染复用（FR-6.7 / FR-6.4）：改参数"再看一眼"时不重画未受影响的图
    reuse = bool(j.get("reuse_rendered", True))
    ctx_key = _data_fingerprint(scans, results, phi) if reuse else ""
    # S5：染料标注文本也是"参数"——改了名字必须重画（否则图与图注不一致）
    lab_key = _label_key(scans, results, cfg, session) if reuse else ""

    def _save(fig, name):
        """写图；命中渲染缓存则**连 draw 都省掉**直接复用（FR-6.7）。

        键由 :func:`_render_key` 统一构造——**串行与并行必须同键**，否则"改参数
        再看一眼"时并行批次会认不出串行批次已画的图（也就白画一遍）。
        """
        key = _render_key(figdir, ctx_key, name, cfg, dpi, fmts, lab_key)
        if reuse:
            hit = _rcache_get(key)
            if hit and all(Path(p).exists() for p in hit):
                _plt().close(fig)
                log.info("复用已渲染的图：%s（未重画）", name)
                return list(hit)
        made = save_jacs(fig, figdir, name, cfg, formats=fmts, dpi=dpi)
        if reuse:
            _rcache_put(key, list(made))
        return made

    want = set(only) if only else set(STEP_ORDER)
    # 懒渲染（P1）：只画首批步骤时不做整批的收尾（图注按定稿档另跑一次即可）
    steps = [
        ("a", lambda: fig_a_overlay(scans, cfg, figdir, captions, save=_save,
                                    groups=only_groups, session=session)),
        *[("b", (lambda g=g, e=e: fig_b_fit(g, e, cfg, figdir, captions, save=_save)))
          for g, e in results.items() if only_groups is None or g in only_groups],
        ("c", lambda: fig_c_bars(results, cfg, figdir, captions, save=_save)),
        ("d", lambda: fig_d_phi(phi, cfg, figdir, captions, save=_save)),
        ("e", lambda: fig_e_residuals(results, cfg, figdir, captions, save=_save)),
        ("stack", lambda: jacs_stack_all(scans, cfg, run_dir, captions,
                                         figdir=figdir, save=_save,
                                         groups=only_groups)),
    ]

    # ── P2：整批出图在多进程里跑（小批量并行更慢，所以先估工作量再决定）──
    if want and _should_parallel(j, want, results, scans, export_mode, only_groups):
        got = _render_parallel(scans, results, phi, controls, cfg, run_dir,
                               export_mode, session, only_groups, want)
        if got is not None:
            # 图注在**父进程**补写：子进程各自只画一步，图注列表天然是"半份"，
            # 让它们写会互相覆盖成残缺版本（并行路径的坑，A8.6 守着）
            if (not preview) and with_captions and j.get("captions", True) \
                    and want == set(STEP_ORDER) and only_groups is None:
                _write_captions_text(scans, results, phi, cfg, figdir)
            return got if want_paths else None

    for name, fn in steps:
        if name not in want:
            continue
        try:
            made += list(fn() or [])
        except Exception as err:                       # noqa: BLE001
            log.error("JACS 图 %s 绘制失败（继续其余图）: %s", name, err)
            import traceback
            log.debug("%s", traceback.format_exc())

    if (not preview) and with_captions and j.get("captions", True):
        # 只渲了部分步骤时不写图注：否则会把"半份"图注落盘，覆盖掉上一轮的完整版
        if want == set(STEP_ORDER) and only_groups is None:
            try:
                write_captions(captions, figdir)
            except Exception as err:                   # noqa: BLE001
                log.warning("图注写出失败: %s", err)
        else:
            log.info("本次只渲了 %s%s → 跳过图注（补齐全部步骤后才会写 captions.md）",
                     "|".join(sorted(want)),
                     "" if only_groups is None else f"，组 {sorted(only_groups)}")
    return made if want_paths else None
