# -*- coding: utf-8 -*-
"""V2.5 FR-32：交互式活画布（工作视图）。

设计边界（设计文档 V2.5 §0 边界 / §4 设计原则）：

* **工作视图 ≠ 定稿产物**：本画布直接从数组绘制，**不调用** run_stack /
  run_spectra_plots / plotting_jacs（A33.2 用结构断言钉住）；定稿图仍由既有
  流水线渲染器产出（FR-34「生成定稿图」），A2/A4.2 保护的定稿路径零接触。
* 不承诺与定稿图逐像素一致（字体/布局细节差异由 UI 明示为"工作视图"）。
* 标注 / 曲线级样式覆写**仅会话级**：不写 run 产物、不进 cfg（FR-33.4）。

组件能力：

* FigureCanvasQTAgg + NavigationToolbar2QT（缩放 / 平移 / Home）；
* 从数组绘制多面板（一组一个 panel），曲线身份稳定（key → Line2D）；
* 读数：最近数据点的物理坐标（不插值）；
* 标注：文本 / 箭头 / 区域（画在 axes 上，随"工作台导出"自然带出）；
* 会话级样式：颜色 / 线型 / 线宽(pt) / 标记 / 标记大小；
* 导出当前视图（由页面决定文件名与格式过滤，零 PDF 由 plotstyle.filter_formats 守）。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QVBoxLayout, QWidget)

from matplotlib import font_manager
from matplotlib.backends.backend_qtagg import (FigureCanvasQTAgg,
                                               NavigationToolbar2QT)
from matplotlib.colors import is_color_like, to_hex
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

#: 工作视图提示（UI 明示"不等于定稿产物"）
WORK_VIEW_HINT = "工作视图（会话级，≠ 定稿产物；定稿请点「生成定稿图」）"

#: 曲线默认样式（会话级可改；与 JACS_SCHEMA 的键名同义，便于桥接）
DEFAULT_STYLE = {
    "color": None,
    "linewidth": 1.0,
    "linestyle": "-",
    "marker": "",
    "markersize": 4.0,
}

#: CJK 字体候选（按顺序回退；V25-B1：活画布中文标题/轴标签/标注缺字形）
CJK_FONT_CANDIDATES = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                       "Noto Sans SC", "Source Han Sans SC",
                       "WenQuanYi Zen Hei")

#: {resolved: bool, path: Optional[str]} —— findfont 只做一次（字体缓存较慢）
_cjk_path_cache: dict = {"resolved": False, "path": None}


def _cjk_font_path() -> Optional[str]:
    """找一款可用 CJK 字体文件；找不到返回 None（结果缓存，不抛异常）。"""
    if _cjk_path_cache["resolved"]:
        return _cjk_path_cache["path"]
    _cjk_path_cache["resolved"] = True
    for name in CJK_FONT_CANDIDATES:
        try:
            path = font_manager.findfont(
                FontProperties(family=name), fallback_to_default=False)
        except Exception:                              # noqa: BLE001 —— 没装这款
            continue
        try:
            if path and Path(path).is_file():
                _cjk_path_cache["path"] = str(path)
                break
        except OSError:                                # pragma: no cover
            continue
    return _cjk_path_cache["path"]


def has_cjk_font() -> bool:
    """当前环境是否有可用 CJK 字体（没有 → 页面应退英文标签；V25-B1）。"""
    return _cjk_font_path() is not None


def label_text(zh: str, en: str) -> str:
    """有 CJK 字体用中文标签，否则退英文（避免 Glyph missing 警告/豆腐块）。"""
    return zh if has_cjk_font() else en


def _cjk_font(size: Optional[float] = None) -> Optional[FontProperties]:
    """CJK 字体属性（找不到字体返回 None；size=None 用 matplotlib 默认）。"""
    path = _cjk_font_path()
    if not path:
        return None
    fp = FontProperties(fname=path)
    if size is not None:
        fp.set_size(float(size))
    return fp


class LivePlotCanvas(QWidget):
    """活画布组件（V2.5 FR-32.1）：数组 → 图，交互不触发渲染器。"""

    #: 读数文本（页面接到状态栏）
    readout = Signal(str)
    #: 非法样式输入被拒绝（页面接状态栏/日志；V25-B4：不抛异常、不污染状态）
    style_rejected = Signal(str)
    #: 标注落点（工作线程不用；供页面/测试监听）
    point_picked = Signal(float, float)

    def __init__(self, parent: Optional[QWidget] = None, *,
                 cfg: Optional[dict] = None,
                 figsize: tuple = (7.6, 5.6), dpi: int = 110):
        super().__init__(parent)
        self.cfg = cfg or {}
        self.figure = Figure(figsize=figsize, dpi=dpi, facecolor="white")
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        self.lbl_hint = QLabel(WORK_VIEW_HINT)
        self.lbl_hint.setStyleSheet("color: #6b7280; font-size: 11px;")
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.toolbar)
        bar.addWidget(self.lbl_hint, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(bar)
        lay.addWidget(self.canvas, 1)

        self._panels: list[dict] = []
        self._axes: list[Any] = []
        self._artists: dict[str, Any] = {}
        self._curves: dict[str, dict] = {}
        self._styles: dict[str, dict] = {}
        #: 重建时记录的每条曲线初始颜色（V25-O3：clear_styles 恢复用）
        self._orig_colors: dict[str, str] = {}
        self._annotations: list[dict] = []
        self._annot_mode: Optional[str] = None
        self._readout_on = False
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("button_press_event", self._on_click)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)

    # ═══════════════════ 数据接口 ═══════════════════
    def set_panels(self, panels: Sequence[dict], *, suptitle: str = "") -> None:
        """设置工作视图面板并重绘。

        Args:
            panels: 每项 = dict，键：
                title / xlabel / ylabel（可选），
                curves: [{"key": str, "label": str, "x": seq, "y": seq,
                          "color": 可选（默认按顺序取色）}]。
            suptitle: 总标题（可选）。
        """
        clean: list[dict] = []
        for p in panels or []:
            if not isinstance(p, dict):
                continue
            curves = [c for c in (p.get("curves") or []) if isinstance(c, dict)]
            clean.append({"title": str(p.get("title") or ""),
                          "xlabel": str(p.get("xlabel") or ""),
                          "ylabel": str(p.get("ylabel") or ""),
                          "curves": curves})
        self._panels = clean
        self._suptitle = str(suptitle or "")
        self._rebuild()

    def clear(self) -> None:
        self._panels = []
        self._artists = {}
        self._curves = {}
        self._orig_colors = {}
        self._annotations = []
        self._rebuild()

    def panels(self) -> list[dict]:
        return list(self._panels)

    def curve_keys(self) -> list[str]:
        return list(self._artists.keys())

    def curve_xy(self, key: str) -> tuple:
        artist = self._artists.get(key)
        if artist is None:
            return np.asarray([]), np.asarray([])
        return (np.asarray(artist.get_xdata(), float),
                np.asarray(artist.get_ydata(), float))

    def artist(self, key: str):
        return self._artists.get(key)

    # ═══════════════════ 绘制 ═══════════════════
    def _colors(self, curves: list[dict]) -> list:
        """按顺序取色（默认用 plotstyle 的 cmap_colors 单一来源）。"""
        out: list = []
        explicit = [c.get("color") for c in curves]
        if all(explicit):
            return list(explicit)
        from core.render.plotstyle import cmap_colors
        try:
            seq = cmap_colors("viridis", 0.05, 0.95, max(1, len(curves)))
        except Exception:                                  # noqa: BLE001
            seq = [(0.2, 0.2, 0.2, 1.0)] * max(1, len(curves))
        for i, c in enumerate(curves):
            out.append(c.get("color") or seq[i % len(seq)])
        return out

    def _rebuild(self) -> None:
        fig = self.figure
        fig.clear()
        self._axes = []
        self._artists = {}
        self._curves = {}
        self._orig_colors = {}
        panels = self._panels
        if not panels:
            fig.text(0.5, 0.5,
                     label_text("（工作视图无数据：请载入数据）",
                                "(No data: load a dataset)"),
                     ha="center", va="center", color="#6b7280",
                     fontproperties=_cjk_font(10))
            self.canvas.draw_idle()
            return
        n = len(panels)
        for i, p in enumerate(panels):
            ax = fig.add_subplot(n, 1, i + 1)
            self._axes.append(ax)
            curves = p.get("curves") or []
            colors = self._colors(curves)
            for c, color in zip(curves, colors):
                key = str(c.get("key") or f"curve{i}-{len(self._artists)}")
                x = np.asarray(c.get("x"), float).ravel()
                y = np.asarray(c.get("y"), float).ravel()
                style = dict(DEFAULT_STYLE)
                style.update({k: v for k, v in (self._styles.get(key) or {}).items()
                              if v is not None})
                line, = ax.plot(x, y, color=style["color"] or color,
                                linewidth=float(style["linewidth"]),
                                linestyle=str(style["linestyle"]),
                                marker=str(style["marker"]),
                                markersize=float(style["markersize"]),
                                label=str(c.get("label") or key))
                self._artists[key] = line
                try:
                    self._orig_colors[key] = to_hex(line.get_color())
                except (TypeError, ValueError):     # 罕见色值：保留 repr 即可
                    self._orig_colors[key] = str(line.get_color())
                self._curves[key] = {"key": key,
                                     "label": str(c.get("label") or key),
                                     "panel": i}
            if p.get("title"):
                ax.set_title(str(p["title"]), fontsize=9,
                             fontproperties=_cjk_font(9))
            if p.get("xlabel"):
                ax.set_xlabel(str(p["xlabel"]), fontsize=8,
                              fontproperties=_cjk_font(8))
            if p.get("ylabel"):
                ax.set_ylabel(str(p["ylabel"]), fontsize=8,
                              fontproperties=_cjk_font(8))
            ax.tick_params(labelsize=7)
            for spine in ax.spines.values():
                spine.set_linewidth(0.8)
            if len(curves) <= 6:
                _legend_kw: dict = {"fontsize": 6, "frameon": False,
                                    "loc": "best"}
                _legend_fp = _cjk_font(6)
                if _legend_fp is not None:
                    _legend_kw["prop"] = _legend_fp
                ax.legend(**_legend_kw)
        if getattr(self, "_suptitle", ""):
            fig.suptitle(self._suptitle, fontsize=10,
                         fontproperties=_cjk_font(10))
        # V25-B2：set_panels/clear 重建后重放标注（轴数不足则丢弃，保持一致）
        self._replay_annotations()
        try:
            fig.tight_layout()
        except Exception:                                  # noqa: BLE001
            pass
        self.canvas.draw_idle()

    # ═══════════════════ 会话级样式（FR-33） ═══════════════════
    @staticmethod
    def _validate_style(style: dict) -> tuple[bool, str]:
        """校验一套会话级样式；返回 (ok, 说明)，绝不抛异常（V25-B4）。"""
        probe = Line2D([], [])
        try:
            if "color" in style:
                color = style["color"]
                if color is None or not is_color_like(color):
                    return False, f"颜色无法解析：{color!r}"
                probe.set_color(color)
            if "linewidth" in style:
                lw = float(style["linewidth"])
                if not math.isfinite(lw) or lw <= 0.0:
                    return False, f"线宽必须为正数：{style['linewidth']!r}"
                probe.set_linewidth(lw)
            if "linestyle" in style:
                probe.set_linestyle(style["linestyle"])
            if "marker" in style:
                marker = style["marker"]
                if marker is None:
                    return False, "标记不能为 None（无标记请用空串）"
                probe.set_marker(str(marker))
            if "markersize" in style:
                ms = float(style["markersize"])
                if not math.isfinite(ms) or ms < 0.0:
                    return False, f"标记大小必须 ≥ 0：{style['markersize']!r}"
                probe.set_markersize(ms)
        except (TypeError, ValueError) as err:
            return False, f"样式取值非法：{err}"
        return True, ""

    def set_style(self, key: str, **style: Any) -> bool:
        """改某条曲线的会话级样式（不写 cfg、不影响生图产物）。

        V25-B4：**先校验、并确认 artist 应用成功后才写** _styles；
        非法取值返回 False 并发 style_rejected（不抛异常、不污染状态）。
        """
        artist = self._artists.get(key)
        if artist is None:
            return False
        cur = dict(DEFAULT_STYLE)
        cur.update(self._styles.get(key) or {})
        changes: dict[str, Any] = {}
        for name, value in style.items():
            if name not in DEFAULT_STYLE:
                continue
            if name == "color" and value is not None:
                try:
                    value = to_hex(value)
                except (TypeError, ValueError):
                    pass                                    # 交给校验统一报错
            if cur.get(name) != value:
                changes[name] = value
        if not changes:
            return False
        ok, msg = self._validate_style(changes)
        if not ok:
            self.style_rejected.emit(f"样式未生效（{key}）：{msg}")
            return False
        try:
            if "color" in changes:
                artist.set_color(changes["color"])
            if "linewidth" in changes:
                artist.set_linewidth(float(changes["linewidth"]))
            if "linestyle" in changes:
                artist.set_linestyle(changes["linestyle"])
            if "marker" in changes:
                artist.set_marker(str(changes["marker"]))
            if "markersize" in changes:
                artist.set_markersize(float(changes["markersize"]))
        except (TypeError, ValueError) as err:              # 理论到不了：已校验
            self.style_rejected.emit(f"样式未生效（{key}）：{err}")
            return False
        cur.update(changes)
        self._styles[key] = cur
        self.canvas.draw_idle()
        return True

    def curve_styles(self) -> dict:
        """当前会话级覆写 {key: {color,linewidth,linestyle,marker,markersize}}。"""
        out = {}
        for key in self._artists:
            cur = dict(DEFAULT_STYLE)
            cur.update(self._styles.get(key) or {})
            out[key] = cur
        return out

    def curve_info(self) -> list:
        """当前视图每条曲线的 (key,label,color,linewidth,linestyle,marker,markersize)。"""
        out = []
        for key, artist in self._artists.items():
            label = (self._curves.get(key) or {}).get("label") or key
            out.append({"key": key, "label": label,
                        "color": artist.get_color(),
                        "linewidth": float(artist.get_linewidth()),
                        "linestyle": str(artist.get_linestyle()),
                        "marker": str(artist.get_marker()),
                        "markersize": float(artist.get_markersize())})
        return out

    def clear_styles(self) -> None:
        """清空会话级 per-curve 覆写（重载数据/关闭会话即失效，FR-33.4）。

        V25-O3：颜色也恢复到重建时的初始值（_orig_colors，由 _rebuild
        在每次重建时按新数据刷新），此前只清 dict 不恢复 artist 颜色。
        """
        self._styles = {}
        for key, artist in self._artists.items():
            color = self._orig_colors.get(key)
            if color is not None:
                try:
                    artist.set_color(color)
                except (TypeError, ValueError):            # 罕见色值：保持现状
                    pass
            try:
                artist.set_linewidth(float(DEFAULT_STYLE["linewidth"]))
                artist.set_linestyle(str(DEFAULT_STYLE["linestyle"]))
                artist.set_marker(str(DEFAULT_STYLE["marker"]))
                artist.set_markersize(float(DEFAULT_STYLE["markersize"]))
            except (TypeError, ValueError):                # pragma: no cover
                pass
        self.canvas.draw_idle()

    # ═══════════════════ 读数（FR-32.5） ═══════════════════
    def set_readout_enabled(self, enabled: bool) -> None:
        self._readout_on = bool(enabled)
        if not enabled:
            self.readout.emit("")

    def readout_enabled(self) -> bool:
        return self._readout_on

    def nearest_point(self, x: float, y: float) -> Optional[dict]:
        """最近数据点的物理坐标（**不插值**）；无数据返回 None。"""
        best = None
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            return None
        for key, artist in self._artists.items():
            xd = np.asarray(artist.get_xdata(), float).ravel()
            yd = np.asarray(artist.get_ydata(), float).ravel()
            if not xd.size or xd.size != yd.size:
                continue
            idx = int(np.abs(xd - x).argmin())
            d = math.hypot(float(xd[idx]) - x, float(yd[idx]) - y)
            if best is None or d < best["_dist"]:
                label = (self._curves.get(key) or {}).get("label") or key
                best = {"key": key, "label": label,
                        "x": float(xd[idx]), "y": float(yd[idx]),
                        "_dist": d}
        if best is not None:
            best.pop("_dist", None)
        return best

    def _on_motion(self, event) -> None:                 # noqa: ANN001 —— mpl 回调
        if not self._readout_on or event.inaxes is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        p = self.nearest_point(event.xdata, event.ydata)
        if p is not None:
            self.readout.emit("读数：" + p["label"] +
                              f"  x={p['x']:.6g}  y={p['y']:.6g}")
        else:
            self.readout.emit("读数：—")

    # ═══════════════════ 标注（FR-32.5） ═══════════════════
    def set_annotation_mode(self, mode: Optional[str]) -> None:
        """标注模式：None / text / arrow / rect（会话级，不写 run 产物）。"""
        mode = None if mode in (None, "", "none") else str(mode)
        self._annot_mode = mode

    def annotation_mode(self) -> Optional[str]:
        return self._annot_mode

    @staticmethod
    def _annotation_artist(ax, kind: str, text: str, x: float, y: float):
        """在指定 axes 上画一个标注；失败返回 None（标注失败不崩）。"""
        font = _cjk_font(8)
        if kind == "arrow":
            artist = ax.annotate(
                str(text), xy=(x, y),
                xytext=(x, y + 0.05 * (ax.get_ylim()[1] - ax.get_ylim()[0])),
                arrowprops={"arrowstyle": "->", "color": "#333333"},
                fontsize=8, fontproperties=font)
        elif kind == "rect":
            x0, x1 = ax.get_xlim()
            y0, y1 = ax.get_ylim()
            w = (x1 - x0) * 0.06
            h = (y1 - y0) * 0.06
            artist = Rectangle((x - w / 2, y - h / 2), w, h,
                               fill=False, edgecolor="#c0392b",
                               linewidth=1.2)
            ax.add_patch(artist)
        else:
            kind = "text"
            artist = ax.annotate(
                str(text or label_text("标注", "Annotation")),
                xy=(x, y), fontsize=8, fontproperties=font)
        return artist

    def _axes_for_point(self, x: float, y: float):
        """点 (x, y) 落在哪个 axes 的数据范围内；都不在则回退第 1 个。"""
        for ax in self._axes:
            try:
                xl, yl = ax.get_xlim(), ax.get_ylim()
                if xl[0] <= x <= xl[1] and yl[0] <= y <= yl[1]:
                    return ax
            except (TypeError, ValueError):                # pragma: no cover
                continue
        return self._axes[0]

    def add_annotation(self, kind: str, text: str, xy: tuple,
                       *, ax: Any = None) -> Optional[dict]:
        """添加标注并记录 owner axes index（V25-B2）。

        Args:
            ax: 目标 axes（鼠标点击传 event.inaxes）；None = 按点击点命中。
        """
        if not self._axes:
            return None
        try:
            x, y = float(xy[0]), float(xy[1])
        except (TypeError, ValueError, IndexError):
            return None
        kind = str(kind or "text")
        target = ax if (ax is not None and ax in self._axes) \
            else self._axes_for_point(x, y)
        try:
            owner = self._axes.index(target)
        except ValueError:                                 # pragma: no cover
            return None
        try:
            artist = self._annotation_artist(target, kind, str(text or ""), x, y)
        except Exception:                                  # noqa: BLE001 —— 失败不崩
            return None
        self._annotations.append({"kind": kind, "text": str(text or ""),
                                  "x": x, "y": y, "axes": owner,
                                  "artist": artist})
        self.canvas.draw_idle()
        return {"kind": kind, "text": str(text or ""), "x": x, "y": y,
                "axes": owner}

    def _replay_annotations(self) -> None:
        """重建后把标注重放到新的 axes 上；轴数不足 → 丢弃（保持状态一致）。"""
        kept: list[dict] = []
        for rec in self._annotations:
            owner = rec.get("axes")
            if not isinstance(owner, int) or not (0 <= owner < len(self._axes)):
                continue
            try:
                artist = self._annotation_artist(
                    self._axes[owner], rec["kind"], rec["text"],
                    float(rec["x"]), float(rec["y"]))
            except Exception:                              # noqa: BLE001
                artist = None
            if artist is None:
                continue
            new_rec = dict(rec)
            new_rec["artist"] = artist
            kept.append(new_rec)
        self._annotations = kept

    def annotations(self) -> list[dict]:
        """当前画布上可见的标注（重建后与重放结果一致；V25-B2）。"""
        return [{"kind": a["kind"], "text": a["text"],
                 "x": a["x"], "y": a["y"], "axes": a.get("axes", 0)}
                for a in self._annotations]

    def clear_annotations(self) -> None:
        for a in list(self._annotations):
            try:
                artist = a.get("artist")
                if artist is not None:
                    artist.remove()
            except Exception:                              # noqa: BLE001
                pass
        self._annotations = []
        self.canvas.draw_idle()

    def _on_click(self, event) -> None:                  # noqa: ANN001 —— mpl 回调
        if self._annot_mode is None or event.inaxes is None:
            return
        if event.xdata is None or event.ydata is None:
            return
        if event.button != 1:
            return
        # V25-B2：把点击所在 axes 传下去（此前恒落第 1 个面板）
        self.add_annotation(self._annot_mode, "",
                            (event.xdata, event.ydata), ax=event.inaxes)
        self.point_picked.emit(float(event.xdata), float(event.ydata))

    # ═══════════════════ 滚轮缩放（Origin 式；不重画数据） ═══════════════════
    def _on_scroll(self, event) -> None:                 # noqa: ANN001 —— mpl 回调
        ax = getattr(event, "inaxes", None)
        if ax is None or event.xdata is None or event.ydata is None:
            return
        factor = 1.25 if str(getattr(event, "button", "")) == "up" else 1 / 1.25
        x, y = float(event.xdata), float(event.ydata)
        xl, yl = ax.get_xlim(), ax.get_ylim()
        ax.set_xlim(x + (xl[0] - x) / factor, x + (xl[1] - x) / factor)
        ax.set_ylim(y + (yl[0] - y) / factor, y + (yl[1] - y) / factor)
        self.canvas.draw_idle()

    # ═══════════════════ 导出当前视图（FR-32.6） ═══════════════════
    def save_figure(self, path, *, dpi: int = 300) -> Optional[Path]:
        """把**当前工作视图**保存为图片（调用方负责文件名/格式过滤）。"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            self.figure.savefig(p, dpi=dpi, bbox_inches="tight",
                                facecolor=self.figure.get_facecolor())
            return p if p.is_file() else None
        except Exception:                                  # noqa: BLE001
            return None
