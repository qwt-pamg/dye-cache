# -*- coding: utf-8 -*-
"""染料库的趋势小图：k ± σ 与 Φ_Δ ± σ 对 run 日期（自绘，不走 core 的 JACS 渲染）。

图保持白底（与出版图一致），文字字体只挂在自家 artist 上，不写全局 ``rcParams``。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

import matplotlib
matplotlib.use("QtAgg")                                 # 与 PySide6 配套的交互后端
import matplotlib.dates as mdates                       # noqa: E402 —— 须在 use() 之后
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure                    # noqa: E402
from matplotlib.ticker import (FuncFormatter, LogLocator, MaxNLocator,  # noqa: E402
                                NullLocator)

from ui.widgets.cjk_fonts import cjk_family
from .model import trend_points

log = logging.getLogger("dye_tool.ui.dye_library")

#: 趋势小图尺寸（英寸）与分辨率 —— 与 CanvasPanel 的 8.5×5.5 相比刻意做小
TREND_SIZE = (3.25, 2.6)
TREND_DPI = 110


def font_prop():
    """本页所有文字共用的 :class:`~matplotlib.font_manager.FontProperties`。

    **不**写全局 ``rcParams``：只把字体挂在自家 artist 上，因此不会影响其它页面
    的图（那些图由 ``core.plotting`` 统一管风格）。
    """
    from matplotlib.font_manager import FontProperties
    fam = cjk_family()
    return FontProperties(family=([fam] if fam else []) + ["DejaVu Sans"])


class TrendCanvas(FigureCanvasQTAgg):
    """k±σ / Φ±σ 对 run 日期的上下两栏点图（自绘，不走 core 的 JACS 渲染）。

    刻意用 :class:`FigureCanvasQTAgg` 而非 ``CanvasPanel.show_figure``：
    后者只搬运**第一个**子图的折线，会丢掉误差棒与第二个子图。
    """

    readout = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        self.fig = Figure(figsize=TREND_SIZE, dpi=TREND_DPI, tight_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.setMinimumHeight(200)
        self._pts: list = []
        self._ax_k = None
        self._ax_p = None
        self.mpl_connect("motion_notify_event", self._on_motion)
        self.setToolTip(
            "趋势小图：上栏 = 每次 run 的 k（带 ±σ 误差棒，纵轴对数刻度）；"
            "下栏 = 每次 run 的 Φ_Δ（带 ±σ）。\n"
            "只有 ≥2 次 run 才有意义；点数太少时显示占位文字。")
        self.show_placeholder("该染料还没有足够的历史数据")

    # ── 对外接口 ──
    def show_placeholder(self, text: str = "暂无可绘制的历史数据",
                         rows: Optional[list[dict]] = None) -> None:
        """显示占位文字（无图）。``rows`` 给出时补一行"当前几条记录"的说明。"""
        self._pts = []
        self._ax_k = self._ax_p = None
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        ax.set_axis_off()
        body = text
        if rows:
            body += f"\n（档案中已有 {len(rows)} 条记录）"
        ax.text(0.5, 0.5, body, ha="center", va="center", color="0.45",
                fontsize=7.5, transform=ax.transAxes, wrap=True,
                fontproperties=font_prop())
        self.draw_idle()

    def render_trend(self, dye: str, rows: list[dict]) -> None:
        """画该染料的 k / Φ 趋势；``rows`` 来自 :func:`core.dyes.read_history`。"""
        pts = trend_points(rows)
        if len(pts) < 2:
            self.show_placeholder("仅 1 次 run，尚不足以看趋势", rows)
            return
        try:
            self._draw(dye, pts)
        except Exception as err:                        # noqa: BLE001 —— 画图失败不能崩页面
            log.warning("趋势小图绘制失败: %s", err)
            self.show_placeholder(f"趋势图绘制失败：{err}")

    # ── 内部 ──
    @staticmethod
    def _panel(ax, pts: list[dict], key: str, err_key: str, *, marker: str,
               color: str, ecolor: str, ylabel: str) -> int:
        """往 ``ax`` 画一栏 ``值 ± σ`` 点图，返回实际画出的点数。

        关键：**逐点筛掉缺失值** —— ``history.csv`` 里没算出来的项是空串（读成
        ``None``），而 matplotlib 的 ``errorbar`` 遇到 ``None`` 会直接抛
        ``TypeError``。因此这里只取 ``值`` 非 None 的点，缺的自然不画。
        """
        use = [p for p in pts if p.get(key) is not None]
        if not use:
            ax.set_axis_off()
            ax.text(0.5, 0.5, "（历次 run 均无此结果）", ha="center", va="center",
                    fontsize=7, color="0.55", transform=ax.transAxes,
                    fontproperties=font_prop())
            return 0
        xs = [p["when"] for p in use]
        ys = [p[key] for p in use]
        es = [p.get(err_key) or 0.0 for p in use]
        ax.errorbar(xs, ys, yerr=es, fmt=marker, ms=3.2, lw=0.9, capsize=2,
                    color=color, ecolor=ecolor)
        ax.set_ylabel(ylabel, fontsize=7, fontproperties=font_prop())
        return len(use)

    def _draw(self, dye: str, pts: list[dict]) -> None:
        """真正绘图：上下两栏，共用横轴（run 日期）。"""
        self._pts = list(pts)
        self.fig.clear()
        ax_k, ax_p = self.fig.subplots(2, 1, sharex=True)
        self._ax_k, self._ax_p = ax_k, ax_p
        fp = font_prop()

        when = [p["when"] for p in pts]
        n_k = self._panel(ax_k, pts, "k", "k_se", marker="o",
                          color="#1f4e96", ecolor="#8aa8cd", ylabel="k (s⁻¹)")
        n_p = self._panel(ax_p, pts, "phi", "phi_se", marker="s",
                          color="#a3401f", ecolor="#d9a893", ylabel="Φ_Δ")
        if n_k and n_p:
            # 版面很窄（3.25 in），标题刻意短，避免被裁掉
            ax_k.set_title(f"{dye}：{len(pts)} 次 run 的趋势", fontsize=7.5,
                           fontproperties=fp)

        # 上栏 k 用对数纵轴更能看清倍率差异；有非正值时退回线性
        k_vals = [p["k"] for p in pts if p["k"] is not None]
        if k_vals and all(v > 0 for v in k_vals):
            # 2026-10-19 GUI 目检（1440×900）：小图高度仅约 200 px。原实现一律
            # log 轴 → 同期 k 值彼此接近时，对数轴刻度全落在同一十倍区间之外/过密，
            # 出现刻度文字叠字。策略：跨倍率 ≥5 才用对数轴（限 4 个大刻度、关次刻度），
            # 否则线性轴 + 3 个刻度；两者都只保留 2 位有效数字，保证小图可读。
            positive = [float(v) for v in k_vals if v > 0]
            ratio = (max(positive) / min(positive)) if positive else 1.0
            if ratio >= 5.0:
                ax_k.set_yscale("log")
                ax_k.yaxis.set_major_locator(LogLocator(base=10, numticks=4))
                ax_k.yaxis.set_minor_locator(NullLocator())
            else:
                ax_k.yaxis.set_major_locator(MaxNLocator(nbins=3))
            ax_k.yaxis.set_major_formatter(
                FuncFormatter(lambda v, _pos: f"{v:.2g}"))

        for ax in (ax_k, ax_p):
            if not ax.axison:                            # 该栏没数据（已写占位文字）
                continue
            ax.tick_params(labelsize=6.5)
            ax.grid(True, ls=":", lw=0.5, color="0.82", alpha=0.9)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
        ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
        # 刻度就落在"确实跑过的日期"上：run 是稀疏、不等距的，按日历等距打刻度
        # 会挤出一堆空刻度、还会漏掉首末日期。超过 5 次 run 则均匀抽稀。
        ticks = when if len(when) <= 5 else [when[i] for i in
                                             range(0, len(when), max(1, len(when) // 4))]
        ax_p.set_xticks(ticks)
        # 底部一句话说明：几条 run、跨了多久、两栏各画了几个点（小白看得懂的上下文）
        span = (f"{when[0]:%Y-%m-%d}~{when[-1]:%Y-%m-%d}" if len(when) > 1
                else f"{when[0]:%Y-%m-%d}")
        self.fig.text(0.5, 0.005, f"共 {len(pts)} 次 run（{span}）·  "
                                  f"k {n_k} 点 / Φ_Δ {n_p} 点",
                      ha="center", va="bottom", fontsize=6, color="0.4",
                      fontproperties=fp)
        self.draw_idle()


# ═══════════════════════════ 页面 ═══════════════════════════
    # ── V2.5 FR-32.5：趋势图读数（最近数据点；会话级，只读） ──
    def point_at_display(self, x: float, y: float) -> Optional[dict]:
        """屏幕坐标 → 最近趋势点（run/日期/值）；超过 32 px 视为未命中。"""
        best = None
        for ax, key, label in ((self._ax_k, "k", "k"), (self._ax_p, "phi", "Φ_Δ")):
            if ax is None or not ax.axison:
                continue
            for p in self._pts:
                val = p.get(key)
                if val is None:
                    continue
                try:
                    px, py = ax.transData.transform((mdates.date2num(p["when"]),
                                                     float(val)))
                except Exception:                       # noqa: BLE001
                    continue
                d = ((px - float(x)) ** 2 + (py - float(y)) ** 2) ** 0.5
                if best is None or d < best["_dist"]:
                    best = {"run_id": p.get("run_id", ""),
                            "when": p["when"].strftime("%Y-%m-%d"),
                            "kind": label, "value": float(val), "_dist": d}
        if best is None or best["_dist"] > 32:
            return None
        best.pop("_dist", None)
        return best

    def _on_motion(self, event) -> None:                # noqa: ANN001 —— mpl 回调
        if event.x is None or event.y is None:
            return
        p = self.point_at_display(float(event.x), float(event.y))
        if p is not None:
            self.readout.emit(f"读数：{p['when']} · {p['kind']}={p['value']:.6g}"
                              f" · run {p['run_id']}")
