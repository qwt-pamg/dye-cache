# -*- coding: utf-8 -*-
"""染料库页（设计文档 V1.1 FR-11.6）：一屏看完"每个染料跑过几次、结果怎么变"。

界面分三块：

* **顶部** —— runs 根目录选择（默认 :func:`core.paths.default_out_root`）+ 刷新；
* **左栏** —— 染料列表（染料名 / run 数 / 最近日期 / 最近 k / 最近 Φ）与
  只读分组「未归档 (V1.0)」（旧 ``run_<时间戳>`` 目录，**只浏览、只打开，
  绝不改名或移动**，见设计文档 §1.3-11）；
* **右栏** —— 所选染料的历史表（``history.csv`` 全部列，只读）、
  k±σ 与 Φ±σ 对 run 日期的**趋势小图**（≥2 次 run 才画）、按钮组
  「打开染料文件夹 / 载入最新 run 参数 / 导出 history.csv / 刷新」。

「载入最新 run 参数」通过 :attr:`DyeLibraryPage.params_loaded` 信号把
``read_run_params()`` 与 ``read_time_overrides()`` 交给主窗口 / Φ_Δ 页套用，
本页**只发信号、不写任何文件**（整定是会话层）。

健壮性约定（与 ``core.dyes`` 一致）：``history.csv`` 缺失/损坏、``dir`` 为
None、根目录不存在、某项没有 run —— 一律降级为空表 / 占位文字并记日志，
**永不抛异常**、永不改用户目录。
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSplitter, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import matplotlib
matplotlib.use("QtAgg")                                 # 与 PySide6 配套的交互后端
import matplotlib.dates as mdates                       # noqa: E402 —— 须在 use() 之后
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure                    # noqa: E402
from matplotlib.ticker import (FuncFormatter, LogLocator, MaxNLocator,  # noqa: E402
                                NullLocator)

from core.dyes import (HISTORY_COLUMNS, HISTORY_NAME, is_legacy_run_dir,
                       read_history, read_run_params, read_time_overrides,
                       scan_runs)
from core.explain import explain_exclusion
from core.paths import default_out_root
from ui import theme

log = logging.getLogger("dye_tool.ui.dye_library")

#: 左侧染料表列名（FR-11.6）
DYE_HEADERS = ["染料名", "run 数", "最近日期", "最近 k", "最近 Φ"]
#: 历史表里 ``dir`` 列的索引（拼版导出据此定位 run 目录）
try:
    _HIST_COL_DIR = list(HISTORY_COLUMNS).index("dir")
except ValueError:                                      # pragma: no cover
    _HIST_COL_DIR = 1

#: 数值缺省时显示的占位符（**不是** 0，避免误读为"结果为零"）
DASH = "—"
#: 趋势小图尺寸（英寸）与分辨率 —— 与 CanvasPanel 的 8.5×5.5 相比刻意做小
TREND_SIZE = (3.25, 2.6)
TREND_DPI = 110

#: 本页图内可能出现的少量中文（标题 / 占位文字）所需字体。
#: 与 ``core.plotting.ZH_FONTS`` 同一候选表，但只挂在**本页自家 artist** 上
#: （见 :func:`_font_prop`），不写全局 rcParams，免得多页互相影响。
_ZH_FONTS = ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
             "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS",
             "DejaVu Sans"]


def _pick_font() -> list[str]:
    """挑出系统里实际可用的中文字体（都没有时退回 DejaVu Sans，中文会显示为方框）。"""
    try:
        from matplotlib import font_manager
        available = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:                                    # noqa: BLE001 —— 探测失败不致命
        return ["DejaVu Sans"]
    found = [n for n in _ZH_FONTS if n in available]
    for fallback in ("DejaVu Sans", "Arial"):
        if fallback in available and fallback not in found:
            found.append(fallback)
    return found or ["DejaVu Sans"]


#: 进程内只探测一次（字体列表加载有点慢）
_MPL_FONTS = _pick_font()


def _font_prop():
    """本页所有文字共用的 :class:`~matplotlib.font_manager.FontProperties`。

    **不**写全局 ``rcParams``：只把字体挂在自家 artist 上，因此不会影响其它页面
    的图（那些图由 ``core.plotting`` 统一管风格）。
    """
    from matplotlib.font_manager import FontProperties
    return FontProperties(family=_MPL_FONTS)


# ═══════════════════════════ 纯函数小工具（无 Qt 依赖，便于复用/测试） ═══════════════════════════
def _to_float(value: object) -> Optional[float]:
    """任意值 → float；``None`` / 空串 / 非数 / NaN / inf 一律返回 None。"""
    if value is None:
        return None
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _fmt_num(value: object, digits: int = 4) -> str:
    """数值 → 定长字符串；解析不出数字时返回 ``DASH``（界面占位）。"""
    f = _to_float(value)
    return DASH if f is None else f"{f:.{digits}g}"


def _run_date_text(run_id: object) -> str:
    """``20261014-222104`` → ``2026-10-14``；解析不出时原样返回。"""
    s = str(run_id or "").strip()
    if not s:
        return DASH
    head = s.split("-", 1)[0]
    if len(head) == 8 and head.isdigit():
        return f"{head[0:4]}-{head[4:6]}-{head[6:8]}"
    return s


def _run_dt(run_id: object) -> Optional[datetime]:
    """``run_id`` → :class:`datetime`（趋势图横轴用）；解析失败返回 None。

    同时兼容 ``20261014-222104`` 与 ``20261014`` 两种写法。
    """
    s = str(run_id or "").strip()
    for fmt in ("%Y%m%d-%H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _looks_like_run(p: Path) -> bool:
    """``p`` 是否真的是一次 run 的目录（而非染料目录本身）。

    判据与 :func:`core.dyes.list_dyes` 统计 run 数时一致：
    含 ``results.csv`` 或 ``figures/``。染料目录（``runs/<染料>/``）不含这两样，
    因此这条判据能把"染料目录"从候选中剔掉，避免把参数快照读到错误层级。
    """
    try:
        return p.is_dir() and ((p / "results.csv").exists()
                               or (p / "figures").is_dir())
    except OSError:
        return False


def _trend_points(rows: list[dict]) -> list[dict]:
    """历史行 → 趋势点（按时间升序，只保留能解析出日期的行）。

    每个点形如 ``{"when": datetime, "k": float|None, "k_se": float|None,
    "phi": float|None, "phi_se": float|None, "run_id": str}``；
    连 k 都读不出来的行直接跳过（不画空洞）。
    """
    pts: list[dict] = []
    for r in rows or []:
        when = _run_dt(r.get("run_id"))
        if when is None:
            continue
        k = _to_float(r.get("k"))
        phi = _to_float(r.get("Phi"))
        if k is None and phi is None:
            continue
        pts.append({"when": when, "k": k, "k_se": _to_float(r.get("k_se")),
                    "phi": phi, "phi_se": _to_float(r.get("Phi_se")),
                    "run_id": str(r.get("run_id", ""))})
    pts.sort(key=lambda p: p["when"])
    return pts


# ═══════════════════════════ 趋势小图 ═══════════════════════════
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
                fontproperties=_font_prop())
        self.draw_idle()

    def render_trend(self, dye: str, rows: list[dict]) -> None:
        """画该染料的 k / Φ 趋势；``rows`` 来自 :func:`core.dyes.read_history`。"""
        pts = _trend_points(rows)
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
                    fontproperties=_font_prop())
            return 0
        xs = [p["when"] for p in use]
        ys = [p[key] for p in use]
        es = [p.get(err_key) or 0.0 for p in use]
        ax.errorbar(xs, ys, yerr=es, fmt=marker, ms=3.2, lw=0.9, capsize=2,
                    color=color, ecolor=ecolor)
        ax.set_ylabel(ylabel, fontsize=7, fontproperties=_font_prop())
        return len(use)

    def _draw(self, dye: str, pts: list[dict]) -> None:
        """真正绘图：上下两栏，共用横轴（run 日期）。"""
        self._pts = list(pts)
        self.fig.clear()
        ax_k, ax_p = self.fig.subplots(2, 1, sharex=True)
        self._ax_k, self._ax_p = ax_k, ax_p
        fp = _font_prop()

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


class DyeLibraryPage(QWidget):
    """「染料库」页：浏览归档结论 + 沿用某次 run 的参数。

    信号：
        ``params_loaded(dict, dict)`` —— ``(run_params, time_overrides)``，
        由「载入最新 run 参数」按钮触发，供主窗口 / Φ_Δ 页套用。

    对外方法：
        :meth:`reload` —— 重新扫描当前 runs 根目录并刷新左右两栏。
    """

    params_loaded = Signal(dict, dict)
    #: V2.3（FR-24.2）：常规提示改走日志/状态栏，不再弹模态框。
    #: 主窗口对它有 ``hasattr`` 守卫——**原来本页没有这个信号**，所以从未接上；
    #: 现在补上，`main_window._build_library_page` 的 `logged.connect` 才真正生效。
    logged = Signal(str, str)
    status = Signal(str)

    def _notify(self, msg: str, level: str = "INFO") -> None:
        """非模态反馈：发信号（主窗口→日志区/状态栏），页面本身不弹窗。

        分级纪律（设计文档 FR-24.2）：只有**破坏性确认**与**错误/异常**保留模态；
        本页原来 11 处 ``QMessageBox`` 里 10 处属于"提示/成功"，全部改走这里。
        """
        try:
            self.logged.emit(level, msg)
            self.status.emit(msg)
        except Exception:                              # noqa: BLE001 —— UI 反馈不许崩
            log.debug("提示信号发送失败：%s", msg)

    def __init__(self, parent: Optional[QWidget] = None,
                 out_root: Optional[Path | str] = None):
        super().__init__(parent)
        self.out_root: Path = Path(out_root) if out_root else Path(default_out_root())
        self._dyes: list[dict] = []
        self._legacy: list[Path] = []
        self._cur: Optional[dict] = None       # 当前选中的染料条目
        self._syncing = False                  # 抑制"选中联动"造成的信号回环
        self._run_figs: list[Path] = []
        self._run_fig_dir: Optional[Path] = None
        self._compose_job = None               # V2.4-B：拼版后台 Job
        self._stats_job = None                 # V2.5 FR-30：统计报表后台 Job
        self._last_stats_path = ""             # 最近一次统计报表产物
        self._build()
        # B3（V2.x 审查）：构造期就 `reload()` → 启动时同步扫 `runs/` 全树（读全部
        # history.csv）。改成**懒加载**：首次切到本页（on_activated / reload 调用）时
        # 才扫。`ArchivePage` 的导航切换与 classic 页签切换都会调用 reload。
        self._loaded = False
        self._lazy_pending = True       # 首次需要显示时再扫（B3）

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        # ── 顶部：runs 根目录 ──
        self.lbl_title = QLabel("染料库")
        theme.mark(self.lbl_title, "title")
        self.lbl_sub = QLabel(
            "按染料汇总历次 run 的档案（history.csv）。左栏选染料 → 右栏看历史表与趋势。"
            "「未归档 (V1.0)」里的旧 run 目录只读浏览，程序绝不改名或移动它们。")
        theme.mark(self.lbl_sub, "muted")
        self.lbl_sub.setWordWrap(True)

        self.edit_root = QLineEdit(str(self.out_root))
        self.edit_root.setToolTip(
            "runs 根目录：所有归档结果都在这下面（每个染料一个子目录，"
            "各次 run 再分子目录）。\n改完按回车或点「刷新」即可重新扫描。")
        self.btn_browse = QPushButton("浏览…")
        self.btn_browse.setToolTip("选择另一个 runs 根目录（不会移动或修改任何已有结果）")
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新扫描当前 runs 根目录，更新染料列表与历史表")
        self.btn_browse.clicked.connect(self._browse_root)
        self.btn_refresh.clicked.connect(lambda: self.reload())
        self.edit_root.returnPressed.connect(lambda: self.reload())
        top = QHBoxLayout()
        top.addWidget(QLabel("runs 根目录"))
        top.addWidget(self.edit_root, 1)
        top.addWidget(self.btn_browse)
        top.addWidget(self.btn_refresh)

        # ── 左栏：染料列表 ──
        self.table_dyes = QTableWidget(0, len(DYE_HEADERS))
        self.table_dyes.setHorizontalHeaderLabels(DYE_HEADERS)
        self.table_dyes.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_dyes.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_dyes.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_dyes.setAlternatingRowColors(True)
        self.table_dyes.verticalHeader().setVisible(False)
        hdr = self.table_dyes.horizontalHeader()
        for c in range(len(DYE_HEADERS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        self.table_dyes.setToolTip(
            "每个染料一行：run 数 = 该染料目录下的结果目录个数；"
            "最近日期 / 最近 k / 最近 Φ 取自 history.csv 的最后一行。\n"
            "选中一行即可在右侧查看完整历史与趋势（表格只读，不会改动任何数据）。")
        self.table_dyes.itemSelectionChanged.connect(self._on_dye_selected)

        box_dyes = QGroupBox("染料列表")
        lay_d = QVBoxLayout(box_dyes)
        lay_d.addWidget(self.table_dyes)

        # ── 左下：未归档（V1.0）只读分组 ──
        self.lbl_legacy = QLabel("未归档 (V1.0)：0 个目录")
        theme.mark(self.lbl_legacy, "muted")
        self.list_legacy = QListWidget()
        self.list_legacy.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list_legacy.setToolTip(
            "V1.0 的旧式 run_<时间戳> 目录：**只能浏览和打开**。\n"
            "程序绝不改名、移动或迁移它们（设计文档 §1.3-11）；双击可在文件管理器中打开。")
        self.list_legacy.itemDoubleClicked.connect(self._open_legacy_item)
        self.list_legacy.itemSelectionChanged.connect(self._on_legacy_selected)

        box_legacy = QGroupBox("未归档 (V1.0)")
        lay_lg = QVBoxLayout(box_legacy)
        lay_lg.addWidget(self.lbl_legacy)
        lay_lg.addWidget(self.list_legacy, 1)

        left_split = QSplitter(Qt.Vertical)
        left_split.addWidget(box_dyes)
        left_split.addWidget(box_legacy)
        left_split.setSizes([440, 220])

        # ── 右上：历史表 ──
        self.lbl_hist = QLabel("尚未选择染料")
        theme.mark(self.lbl_hist, "muted")
        self.table_hist = QTableWidget(0, len(HISTORY_COLUMNS))
        self.table_hist.setHorizontalHeaderLabels(list(HISTORY_COLUMNS))
        self.table_hist.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_hist.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_hist.setAlternatingRowColors(True)
        self.table_hist.verticalHeader().setVisible(False)
        self.table_hist.setToolTip(
            "history.csv 的全部列（只读）。每行 = 一次 run；"
            "「dir」列是这次 run 的结果目录，可用按钮组里的「打开染料文件夹」去看图。\n"
            "表头可拖动调整列宽，双击单元格可选中复制。")
        self.table_hist.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents)
        self.table_hist.horizontalHeader().setStretchLastSection(True)
        self.table_hist.itemSelectionChanged.connect(self._on_hist_selected)
        # A27（审查附录 C）：历史表行右键——"载入该 run 参数"（原来只能载入最新
        # run；评审记录说的"结果表行"即这条 run 级动作，按档案页历史表实现）。
        self.table_hist.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table_hist.customContextMenuRequested.connect(
            self._on_hist_context_menu)

        box_hist = QGroupBox("历史档案（history.csv）")
        lay_h = QVBoxLayout(box_hist)
        lay_h.addWidget(self.lbl_hist)
        lay_h.addWidget(self.table_hist, 1)

        # ── 右下：趋势小图 ──
        self.canvas = TrendCanvas(self)
        self.canvas.readout.connect(self.status)
        box_trend = QGroupBox("趋势（k ± σ 与 Φ_Δ ± σ 对 run 日期）")
        lay_t = QVBoxLayout(box_trend)
        lay_t.addWidget(self.canvas, 1)

        # ── 右中：该 run 的图件（V2.4-B 拼版导出的输入；多选） ──
        box_figs = QGroupBox("该 run 的图件（拼版导出用；可多选）")
        lay_fg = QVBoxLayout(box_figs)
        self.lbl_figs = QLabel("先在历史表里选一次 run")
        theme.mark(self.lbl_figs, "muted")
        self.lbl_figs.setWordWrap(True)
        self.list_figs = QListWidget()
        self.list_figs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_figs.setToolTip(
            "选中历史表某一行 run 后，这里列出该 run 的图件（定稿 figures/ 优先）。\n"
            "多选后可「拼版导出…」按 1×2 / 2×2 / 3×3 等规格拼成一张论文图（PNG/SVG，零 PDF）。")
        self.list_figs.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_figs.customContextMenuRequested.connect(self._on_figs_context_menu)
        self.btn_compose = QPushButton("拼版导出…")
        self.btn_compose.setToolTip(
            "把选中的图件拼成一张组合图（面板标签 (a)(b)…、默认 600 dpi、零 PDF）\n"
            "默认写到当前 run 的 figures/，文件名 composite_<行>x<列>_<时间>，"
            "不会覆盖既有图件")
        self.btn_compose.setEnabled(False)
        self.btn_compose.clicked.connect(self.compose_selected)
        row_fg = QHBoxLayout()
        row_fg.addWidget(self.lbl_figs, 1)
        row_fg.addWidget(self.btn_compose)
        lay_fg.addWidget(self.lbl_figs)
        lay_fg.addWidget(self.list_figs, 1)
        lay_fg.addLayout(row_fg)

        right_split = QSplitter(Qt.Vertical)
        right_split.addWidget(box_hist)
        right_split.addWidget(box_figs)
        right_split.addWidget(box_trend)
        right_split.setSizes([420, 240, 240])

        body = QSplitter(Qt.Horizontal)
        body.addWidget(left_split)
        body.addWidget(right_split)
        body.setSizes([520, 1000])

        # ── 按钮组 ──
        self.btn_open = QPushButton("打开染料文件夹")
        self.btn_params = QPushButton("载入最新 run 参数")
        self.btn_export = QPushButton("导出 history.csv")
        self.btn_reload = QPushButton("刷新")
        self.btn_open.setToolTip("在文件管理器中打开该染料的归档目录（只读浏览，不改动内容）")
        self.btn_params.setToolTip(
            "读取最近一次 run 里的 run_params.yaml 与 time_overrides.yaml，"
            "并发信号给主窗口 / Φ_Δ 页套用。\n本页只读参数，不会覆盖或写入任何文件。")
        self.btn_export.setToolTip("把该染料的 history.csv 另存到你选择的位置（原档案保持不动）")
        self.btn_reload.setToolTip("重新扫描 runs 根目录并刷新本页显示")
        self.btn_open.clicked.connect(self._open_dye_folder)
        self.btn_params.clicked.connect(self._emit_latest_params)
        self.btn_export.clicked.connect(self._export_history)
        self.btn_reload.clicked.connect(lambda: self.reload())
        self.btn_open.setEnabled(False)
        self.btn_params.setEnabled(False)
        self.btn_export.setEnabled(False)
        # V2.5 FR-30.3：统计报表入口 + 范围选择（全部 runs / 仅当前染料）
        self.cmb_stat_scope = QComboBox()
        self.cmb_stat_scope.addItem("统计范围：全部 runs", "")
        self.cmb_stat_scope.addItem("统计范围：仅当前染料的 run", "current")
        self.cmb_stat_scope.setToolTip(
            "统计报表的扫描范围：\n"
            "· 全部 runs：扫当前 runs 根目录下所有 run（与④档案同源发现逻辑）\n"
            "· 仅当前染料：只统计左栏选中的染料（含 runs/<功能>/<染料>/ 与 _multi）")
        self.btn_stats = QPushButton("统计报表…")
        self.btn_stats.setToolTip(
            "扫描 runs 里各 run 的 results.csv/fit_summary.json → 生成\n"
            "「明细 + 统计」两 sheet 的 statistics_<时间>.xlsx（默认写 runs 根目录，\n"
            "可另选；只读，绝不改动 run 产物；后台 Job 可取消）")
        self.btn_stats.clicked.connect(self._on_stats_clicked)
        self.btn_open_stats = QPushButton("打开统计报表")
        self.btn_open_stats.setEnabled(False)
        self.btn_open_stats.clicked.connect(self._open_stats)
        btns = QHBoxLayout()
        btns.addWidget(self.btn_open)
        btns.addWidget(self.btn_params)
        btns.addWidget(self.btn_export)
        btns.addWidget(self.cmb_stat_scope)
        btns.addWidget(self.btn_stats)
        btns.addWidget(self.btn_open_stats)
        btns.addStretch(1)
        btns.addWidget(self.btn_reload)

        lay = QVBoxLayout(self)
        lay.addWidget(self.lbl_title)
        lay.addWidget(self.lbl_sub)
        lay.addLayout(top)
        lay.addWidget(body, 1)
        lay.addLayout(btns)

    # ══════════════ 刷新（对外主入口） ══════════════
    def reload(self, out_root: Optional[Path | str] = None) -> None:
        """重新扫描 runs 根目录并刷新左右两栏。

        Args:
            out_root: 新的 runs 根目录；``None`` 表示沿用当前目录（或顶部输入框
                里手改的路径）。

        任何扫描异常都被吞掉并降级为空列表（记日志），**不弹窗、不抛异常**。
        """
        target = Path(out_root) if out_root else Path(self.edit_root.text().strip()
                                                     or self.out_root)
        self.out_root = target
        if self.edit_root.text() != str(target):
            self.edit_root.blockSignals(True)
            self.edit_root.setText(str(target))
            self.edit_root.blockSignals(False)

        data: dict = {"dyes": [], "legacy": []}
        try:
            data = scan_runs(target) or {}
        except Exception as err:                        # noqa: BLE001 —— 扫描失败也不崩页面
            log.warning("扫描 runs 根目录失败 %s: %s", target, err)
        prev = str((self._cur or {}).get("dye") or "")
        self._dyes = list(data.get("dyes") or [])
        self._legacy = [Path(p) for p in (data.get("legacy") or [])]
        self._cur = None

        self._fill_dye_table()
        self._fill_legacy_list()
        self._clear_detail("请选择左侧染料查看历史档案与趋势")
        self._restore_selection(prev)

    def _restore_selection(self, prev_dye: str) -> None:
        """刷新后恢复选中：原染料还在就选它，否则选第一行。

        注意 ``selectRow()`` 在同一行上**不会**再发 ``itemSelectionChanged``，
        所以这里在选中未变时显式调用 :meth:`_show_dye`，避免"刷新后右栏还是旧的"。
        """
        if not self._dyes:
            self._on_dye_selected()                      # 无染料：复位右栏
            return
        row = next((i for i, e in enumerate(self._dyes)
                    if str(e.get("dye") or "") == prev_dye), 0)
        if self.table_dyes.currentRow() == row:
            self._show_dye(self._dyes[row])
            return
        self.table_dyes.selectRow(row)                   # 触发 _on_dye_selected

    # ══════════════ 左栏填充 ══════════════
    def _fill_dye_table(self) -> None:
        """按 ``scan_runs()`` 结果填充染料列表（染料名/run 数/最近日期/k/Φ）。"""
        self.table_dyes.blockSignals(True)
        try:
            self.table_dyes.setRowCount(len(self._dyes))
            for r, e in enumerate(self._dyes):
                dye = str(e.get("dye") or "")
                d = e.get("dir")
                where = str(d) if d else "（无归档目录，仅见于 _multi 登记）"
                n_runs = e.get("runs")
                cells = [
                    (dye, f"染料名（归档目录名）：{dye}\n路径：{where}"),
                    (str(int(n_runs)) if isinstance(n_runs, (int, float))
                     else str(n_runs or 0),
                     "该染料目录下含 results.csv 或 figures/ 的 run 目录个数"),
                    (_run_date_text(e.get("last_run")),
                     "最近一次 run 的日期（取自 history.csv 的 run_id；"
                     "无档案时取最新的 run 目录名）"),
                    (_fmt_num(e.get("last_k"), 4),
                     "最近一次 run 的准一级速率常数 k（s⁻¹）"),
                    (_fmt_num(e.get("last_phi"), 4),
                     "最近一次 run 的单线态氧量子产率 Φ_Δ；空 = 本次没算出来（如缺参比）"),
                ]
                for c, (text, tip) in enumerate(cells):
                    it = QTableWidgetItem(str(text))
                    it.setToolTip(tip)
                    if c and str(text) == DASH:
                        it.setForeground(theme.color("text_subtle"))
                    self.table_dyes.setItem(r, c, it)
        finally:
            self.table_dyes.blockSignals(False)

    def _fill_legacy_list(self) -> None:
        """填充「未归档 (V1.0)」只读列表（只浏览/打开，绝不动文件）。

        每项都再用 :func:`core.dyes.is_legacy_run_dir` 复核一次，并用
        :func:`core.explain.explain_exclusion` 把人话原因写进悬浮提示
        （FR-9.2：界面只翻译，不新增判断）。
        """
        #: V1.0 旧目录的"原因 + 建议"（借用 FR-9.2 的翻译层，不新增判断逻辑）
        reason = "缺少 history.csv 档案（V1.0 旧式 run 目录）"
        why, action = explain_exclusion(reason)
        why = why or reason
        action = action or "只读浏览；如需归档请重新跑一次分析，程序不会移动旧目录"
        self.list_legacy.blockSignals(True)
        try:
            self.list_legacy.clear()
            for p in self._legacy:
                it = QListWidgetItem(p.name)
                still_legacy = is_legacy_run_dir(p)
                tail = ("只读浏览：程序绝不改名、移动或迁移；双击可在文件管理器中打开。"
                        if still_legacy else
                        "（复核时该目录已不满足「未归档」条件，可能刚被归档过，请刷新。）")
                it.setToolTip(f"{p}\n{why}\n建议：{action}\n{tail}")
                self.list_legacy.addItem(it)
        finally:
            self.list_legacy.blockSignals(False)
        n = len(self._legacy)
        self.lbl_legacy.setText(f"未归档 (V1.0)：{n} 个目录"
                                + ("" if n else "（没有旧版目录）"))

    # ══════════════ 选中联动 ══════════════
    def _on_dye_selected(self) -> None:
        """选中某染料 → 填历史表 + 画趋势 + 启用按钮组。"""
        if self._syncing:
            return
        row = self.table_dyes.currentRow()
        if not (0 <= row < len(self._dyes)):
            self._clear_detail("请选择左侧染料查看历史档案与趋势")
            return
        self._syncing = True
        try:
            self.list_legacy.clearSelection()            # 两侧选中互斥，避免误读
        finally:
            self._syncing = False
        self._show_dye(self._dyes[row])

    def _on_legacy_selected(self) -> None:
        """选中旧目录 → 右栏给出说明（旧目录没有档案，不参与趋势）。"""
        if self._syncing:
            return
        rows = self.list_legacy.selectionModel().selectedRows() \
            if self.list_legacy.selectionModel() else []
        if not rows:
            return
        self._syncing = True
        try:
            self.table_dyes.clearSelection()
            self.table_dyes.setCurrentCell(-1, -1)
        finally:
            self._syncing = False
        self._cur = None
        name = self.list_legacy.currentItem().text() \
            if self.list_legacy.currentItem() else "未归档目录"
        self._clear_detail(f"未归档 (V1.0) 目录：{name}", keep_history=False)
        self.lbl_hist.setText(
            f"V1.0 旧目录 {name} 没有 history.csv 档案，因此没有历史表与趋势。"
            "程序只做只读识别，不会改名、移动或迁移它。")
        for b in (self.btn_params, self.btn_export):
            b.setEnabled(False)
        self.btn_open.setEnabled(True)                   # 允许"只浏览/打开"

    def _show_dye(self, entry: dict) -> None:
        """填充右栏：历史表（全部列）+ 趋势小图 + 按钮可用性。"""
        dye = str(entry.get("dye") or "")
        d = entry.get("dir")
        dye_dir = Path(d) if d else None
        rows = read_history(dye_dir) if dye_dir is not None else []

        self._cur = entry
        self._fill_history(dye_dir, rows)
        self.canvas.render_trend(dye, rows)

        has_dir = bool(dye_dir is not None and dye_dir.is_dir())
        self.btn_open.setEnabled(has_dir)
        self.btn_params.setEnabled(self._latest_run_dir(entry) is not None)
        # 导出按钮只在"确实有可读记录"时可用；损坏的 history.csv 会读成 0 行 → 置灰
        self.btn_export.setEnabled(bool(rows) and dye_dir is not None
                                   and (dye_dir / HISTORY_NAME).is_file())

    def _fill_history(self, dye_dir: Optional[Path], rows: list[dict]) -> None:
        """历史表：``history.csv`` 全部列（按 ``HISTORY_COLUMNS`` 顺序），只读。"""
        self.table_hist.setRowCount(0)
        if dye_dir is None:
            self.lbl_hist.setText("该染料只出现在 _multi 的登记里，暂无自己的归档目录。")
            return
        if not rows:
            self.lbl_hist.setText(
                f"{dye_dir.name}：没有可读的 history.csv 记录"
                "（文件缺失、内容损坏或还没有完成过一次分析）。")
            return
        self.table_hist.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, key in enumerate(HISTORY_COLUMNS):
                text = "" if row.get(key) is None else str(row.get(key))
                it = QTableWidgetItem(text)
                it.setToolTip(f"{key} = {text or '(空)'}")
                self.table_hist.setItem(r, c, it)
        self.lbl_hist.setText(f"{dye_dir.name}：{len(rows)} 次 run — {dye_dir}")
        if self.table_hist.rowCount():
            self.table_hist.selectRow(0)        # 选最新一次 run → 触发图件清单刷新

    def _clear_detail(self, hint: str, keep_history: bool = True) -> None:
        """右栏复位（未选染料 / 扫描为空时用）。"""
        self._cur = None
        if not keep_history:
            self.table_hist.setRowCount(0)
        self.lbl_hist.setText(hint)
        self.canvas.show_placeholder("尚未选择染料")
        try:
            self._run_figs = []
            self._run_fig_dir = None
            self.list_figs.clear()
            self.lbl_figs.setText("先在历史表里选一次 run")
            self.btn_compose.setEnabled(False)
        except Exception:                              # noqa: BLE001 —— 复位不崩页
            pass
        for b in (self.btn_open, self.btn_params, self.btn_export):
            b.setEnabled(False)

    # ══════════════ 按钮动作 ══════════════
    def _browse_root(self) -> None:
        """选择另一个 runs 根目录并刷新。"""
        start = str(self.out_root if self.out_root.is_dir() else default_out_root())
        d = QFileDialog.getExistingDirectory(self, "选择 runs 结果根目录", start)
        if d:
            self.reload(d)

    def _latest_run_dir(self, entry: Optional[dict] = None) -> Optional[Path]:
        """最近一次 run 的目录；找不到真正的 run 目录时返回 ``None``。

        规则（对应 FR-11.6）：

        1. 候选 = ``scan_runs()`` 的 ``dirs`` + ``dir``（**都是染料目录**，
           如 ``runs/<染料>/``、``runs/<功能>/<染料>/``），再展开它们**下面一层**
           的 run 子目录 —— ``run_params.yaml`` 就在那一层；
        2. 只保留 :func:`_looks_like_run` 为真的候选，染料目录本身因此被自动排除；
        3. 目录名等于 ``last_run``（或 ``run_<last_run>``，V1.0 扁平命名）优先；
        4. 否则取目录名最大的那个（目录名即时间戳，字符串序 == 时间序）。

        读不到任何 run 目录时返回 ``None``（调用方据此置灰按钮，不弹错）。
        """
        e = entry or self._cur
        if not e:
            return None
        bases = [Path(p) for p in (e.get("dirs") or []) if p]
        if e.get("dir"):
            bases.append(Path(e["dir"]))
        cands: list[Path] = []
        for base in bases:
            cands.append(base)
            try:                                        # 展开染料目录下的 run 子目录
                cands.extend(c for c in base.iterdir() if c.is_dir())
            except OSError:
                continue
        runs = [p for p in cands if _looks_like_run(p)]
        if not runs:
            return None
        last = str(e.get("last_run") or "")
        if last:
            for p in runs:
                if p.name in (last, f"run_{last}"):
                    return p
        return sorted(runs, key=lambda p: p.name)[-1]

    def _open_dye_folder(self) -> None:
        """在系统文件管理器中打开该染料目录（只浏览，不改动）。"""
        e = self._cur
        d = e.get("dir") if e else None
        if not d:
            self._notify("该染料没有对应的归档目录（先选中左侧一个染料）", "WARNING")
            return
        p = Path(d)
        if not p.is_dir():
            self._notify(f"目录不存在：{p}", "WARNING")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
            self._notify(f"无法自动打开，请手动前往：{p}", "WARNING")

    def _emit_latest_params(self) -> None:
        """读最近一次 run 的参数快照并发出 :attr:`params_loaded` 信号。"""
        run_dir = self._latest_run_dir()
        if run_dir is None:
            self._notify("没有找到该染料的 run 目录", "WARNING")
            return
        try:
            params = read_run_params(run_dir)
            overrides = read_time_overrides(run_dir)
        except Exception as err:                        # noqa: BLE001 —— 快照损坏不能崩页面
            log.warning("读取 run 参数快照失败 %s: %s", run_dir, err)
            params, overrides = {}, {}
        if not params and not overrides:
            self._notify(
                f"没有参数快照：{run_dir}"
                "（多为 V1.0 旧结果；请改选有快照的 run，或重新跑一次分析）",
                "WARNING")
            return
        self.params_loaded.emit(dict(params), dict(overrides))
        self._notify(
            f"已载入最近一次 run 的参数（run 参数 {len(params)} 项、"
            f"时间整定 {len(overrides)} 项）→ 可直接「开始分析」复现")

    def _export_history(self) -> None:
        """把 history.csv 另存到用户指定位置（原档案不动）。"""
        e = self._cur
        d = e.get("dir") if e else None
        src = Path(d) / HISTORY_NAME if d else None
        if src is None or not src.is_file():
            self._notify("该染料没有 history.csv 可导出", "WARNING")
            return
        default_name = f"{(e or {}).get('dye') or src.parent.name}_{HISTORY_NAME}"
        dest, _ = QFileDialog.getSaveFileName(self, "导出 history.csv",
                                              str(src.parent / default_name),
                                              "CSV 文件 (*.csv);;所有文件 (*)")
        if not dest:
            return
        try:
            target = Path(dest)
            if target.resolve() == src.resolve():       # 防呆：别让它覆盖原档案
                self._notify("目标就是原档案本身，已取消（原档案保持不动）", "WARNING")
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        except Exception as err:                        # noqa: BLE001 —— 写失败只提示
            QMessageBox.critical(self, "导出失败", f"{type(err).__name__}: {err}")
            return
        self._notify(f"history.csv 已导出：{target}")

    def _open_legacy_item(self, item: QListWidgetItem) -> None:
        """双击旧目录 → 在文件管理器中打开（**只浏览，不改名不移动**）。"""
        for p in self._legacy:
            if p.name == item.text() and p.is_dir():
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
                    self._notify(f"无法自动打开，请手动前往：{p}", "WARNING")
                return


    # ══════════════ V2.4-B（FR-27.3）：拼版导出（④档案页入口） ══════════════
    def _selected_run_dir(self) -> Optional[Path]:
        """当前历史表选中行对应的 run 目录（没有则回落到"最近一次 run"）。"""
        row = self.table_hist.currentRow()
        if 0 <= row < self.table_hist.rowCount():
            it = self.table_hist.item(row, _HIST_COL_DIR)
            txt = (it.text() if it is not None else "").strip()
            if txt:
                p = Path(txt)
                if p.is_dir():
                    return p
        return self._latest_run_dir()

    def _figures_of(self, run_dir: Optional[Path]) -> tuple[list[Path], str]:
        """某 run 的图件清单（定稿 ``figures/`` 优先，退回 ``figures_preview/``）。"""
        if run_dir is None:
            return [], ""
        for sub, label in (("figures", "定稿 600 dpi"), ("figures_preview", "预览档")):
            d = run_dir / sub
            if d.is_dir():
                pngs = sorted(d.glob("*.png"))
                if pngs:
                    return pngs, f"{label}（{d}）"
        return [], ""

    def _fill_run_figures(self, run_dir: Optional[Path] = None) -> None:
        """填充"该 run 的图件"多选清单（拼版导出的输入）。"""
        d = run_dir if run_dir is not None else self._selected_run_dir()
        pngs, label = self._figures_of(d)
        self._run_figs = list(pngs)
        self._run_fig_dir = (Path(d) / ("figures" if (Path(d) / "figures").is_dir()
                                        else "figures_preview")) if d else None
        self.list_figs.blockSignals(True)
        self.list_figs.clear()
        for p in pngs:
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, str(p))
            it.setToolTip(str(p))
            self.list_figs.addItem(it)
            it.setSelected(True)                 # 默认全选（常见：4 张拼 2×2）
        self.list_figs.blockSignals(False)
        self.lbl_figs.setText(
            (f"该 run 的图件：{len(pngs)} 张 — {label}" if pngs
             else "该 run 没有可拼版的图件（figures/ 与 figures_preview/ 都未找到 png）"))
        self.btn_compose.setEnabled(bool(pngs))

    def _on_hist_context_menu(self, pos) -> None:
        """历史表行右键菜单（A27）：载入该 run 参数 / 打开该 run 目录。"""
        from PySide6.QtWidgets import QMenu
        row = self.table_hist.rowAt(pos.y())
        if row < 0:
            return
        self.table_hist.selectRow(row)
        menu = QMenu(self)
        act_load = menu.addAction("载入该 run 参数")
        act_load.triggered.connect(lambda *_, r=row: self.load_run_params(r))
        act_open = menu.addAction("打开该 run 目录")
        act_open.triggered.connect(
            lambda *_, r=row: self._open_path(self._run_dir_of_row(r)))
        menu.exec(self.table_hist.viewport().mapToGlobal(pos))

    def _run_dir_of_row(self, row: int) -> Optional[Path]:
        """历史表某行的 run 目录（``dir`` 列；不存在返回 None）。"""
        it = self.table_hist.item(row, _HIST_COL_DIR)
        txt = (it.text() if it is not None else "").strip()
        if not txt:
            return None
        p = Path(txt)
        return p if p.is_dir() else None

    def load_run_params(self, row: int) -> bool:
        """载入**该行 run** 的参数快照（A27；= 行级版的"载入 run 参数"）。"""
        run_dir = self._run_dir_of_row(row)
        if run_dir is None:
            self._notify("该行没有可用的 run 目录", "WARNING")
            return False
        try:
            params = read_run_params(run_dir)
            overrides = read_time_overrides(run_dir)
        except Exception as err:                       # noqa: BLE001 —— 快照坏不崩页
            log.warning("读取 run 参数快照失败 %s: %s", run_dir, err)
            params, overrides = {}, {}
        if not params and not overrides:
            self._notify(f"没有参数快照：{run_dir}", "WARNING")
            return False
        self.params_loaded.emit(dict(params), dict(overrides))
        self._notify(f"已载入该 run 参数：{run_dir.name}"
                     f"（run 参数 {len(params)} 项、时间整定 {len(overrides)} 项）")
        return True

    def _on_hist_selected(self) -> None:
        """历史表换行 → 刷新图件清单。"""
        try:
            self._fill_run_figures(self._selected_run_dir())
        except Exception as err:                       # noqa: BLE001 —— 刷新不得崩页
            log.warning("刷新图件清单失败：%s", err)

    def _selected_figures(self) -> list[Path]:
        out: list[Path] = []
        for it in self.list_figs.selectedItems():
            p = it.data(Qt.UserRole)
            if p:
                out.append(Path(str(p)))
        return out

    def compose_selected(self) -> bool:
        """把勾选的图件拼版导出（对话框选规格 → 后台 Job，可取消）。"""
        if self._compose_job is not None and self._compose_job.isRunning():
            self._notify("已有拼版任务在运行，请等待完成或先取消", "WARNING")
            return False
        imgs = self._selected_figures()
        if not imgs:
            self._notify("请先在「该 run 的图件」里勾选至少一张", "WARNING")
            return False
        from PySide6.QtWidgets import QDialog
        from ui.widgets.layout_dialog import LayoutDialog

        dlg = LayoutDialog(images=imgs, default_dir=self._run_fig_dir
                           or (imgs[0].parent), parent=self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            spec = dlg.spec()
            out_dir = dlg.out_dir()
        finally:
            dlg.deleteLater()
        from ui.workers import Job

        from core.render.layout import compose_layout

        def task(progress_cb, log_cb):
            job = self._compose_job
            return compose_layout(
                imgs, spec, getattr(self, "cfg", None), out_dir,
                cancel_cb=(lambda: bool(job is not None
                                        and job.is_cancel_requested())),
                progress_cb=progress_cb)

        job = Job(task, "拼版导出", self)
        self._compose_job = job

        def _progress(pct, stage):
            self.status.emit(f"拼版导出：{stage}（{pct}%）")

        def _failed(msg, tb):
            self._notify(f"拼版导出失败：{msg}" + (f"\n{tb.splitlines()[-1]}"
                                                   if tb else ""), "ERROR")

        job.progress.connect(_progress)
        job.logged.connect(self.logged.emit)
        job.done.connect(self._on_compose_done)
        job.failed.connect(_failed)
        job.cancelled.connect(lambda: self._notify("拼版导出已取消（未写出文件）"))
        job.finished.connect(self._on_compose_finished)
        self.btn_compose.setEnabled(False)
        self.setBusyCompose(True)
        job.start()
        return True

    def setBusyCompose(self, busy: bool) -> None:      # noqa: N802 —— 与 Qt 命名风格无关的公共钩子
        """拼版进行中禁用入口（Job 结束/失败/取消时恢复）。"""
        self.btn_compose.setEnabled(not busy and bool(self._run_figs))
        self.btn_compose.setText("拼版导出中…" if busy else "拼版导出…")

    def _on_compose_done(self, made) -> None:
        names = "、".join(Path(p).name for p in (made or []))
        self._notify(f"拼版导出完成：{names}" if names else "拼版导出完成（无文件）")

    def _on_compose_finished(self) -> None:
        self._compose_job = None
        self.setBusyCompose(False)

    def _on_figs_context_menu(self, pos) -> None:
        from PySide6.QtWidgets import QMenu
        item = self.list_figs.itemAt(pos)
        if item is not None and not item.isSelected():
            item.setSelected(True)
        menu = QMenu(self)
        act = menu.addAction("拼版导出…")
        act.setEnabled(bool(self._selected_figures()))
        act.triggered.connect(lambda *_: self.compose_selected())
        act_reveal = menu.addAction("在资源管理器中显示")
        act_reveal.setEnabled(self._run_fig_dir is not None)
        act_reveal.triggered.connect(
            lambda *_: self._open_path(self._run_fig_dir))
        menu.exec(self.list_figs.viewport().mapToGlobal(pos))

    def _open_path(self, p: Optional[Path]) -> None:
        """在资源管理器中打开目录（离屏测试不可断言，沿用 Q-B5 降级口径）。"""
        if p is None:
            return
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
            self._notify(f"已在文件管理器中打开：{p}")
        except Exception as err:                       # noqa: BLE001
            self._notify(f"无法打开资源管理器：{err}", "WARNING")

    # ══════════════ V2.5 FR-30：多 run 统计报表（只读、后台 Job） ══════════════
    def _stat_scope(self):
        """当前统计范围 → dye 过滤值（None = 全部 runs）。"""
        if self.cmb_stat_scope.currentData() != "current":
            return None
        cur = self._cur or {}
        return str(cur.get("dye") or "") or None

    def _on_stats_clicked(self) -> bool:
        """按钮入口：选保存位置（默认 runs 根目录的时间戳文件名）→ 后台 Job。"""
        if self._stats_job is not None and self._stats_job.isRunning():
            self._notify("已有统计报表任务在运行，请等待完成或先取消", "WARNING")
            return False
        if self.cmb_stat_scope.currentData() == "current" and self._stat_scope() is None:
            self._notify("请先在左侧选择一个染料，或把范围改为「全部 runs」", "WARNING")
            return False
        from core.report.summary import default_output_path
        default_path = default_output_path(self.out_root, "statistics")
        p, _flt = QFileDialog.getSaveFileName(
            self, "保存统计报表", str(default_path), "Excel 工作簿 (*.xlsx)")
        if not p:
            return False
        return self.run_statistics(p)

    def run_statistics(self, out_path=None) -> bool:
        """扫描 runs → 明细 + 统计两 sheet（V2.5 FR-30.2）。

        全程只读（A30.2：扫描前后 runs/ 逐字节不变）；后台 Job 可取消、忙禁用。
        """
        if self._stats_job is not None and self._stats_job.isRunning():
            self._notify("已有统计报表任务在运行，请等待完成或先取消", "WARNING")
            return False
        if self.cmb_stat_scope.currentData() == "current" and self._stat_scope() is None:
            self._notify("请先在左侧选择一个染料，或把范围改为「全部 runs」", "WARNING")
            return False
        scope = self._stat_scope()
        root = Path(self.out_root)
        if out_path is None:
            from core.report.summary import default_output_path
            out_path = default_output_path(root, "statistics")
        from ui.workers import Job
        from core.report.summary import collect_statistics
        from core.report.excel_export import write_statistics_workbook

        def task(progress_cb, log_cb):
            job = self._stats_job
            if progress_cb:
                progress_cb(10, f"扫描 runs（{root}）")
            info = collect_statistics(root, dye_filter=scope)
            if job is not None and job.is_cancel_requested():
                return {"info": info, "path": ""}
            for e in (info.get("errors") or [])[:20]:
                if log_cb:
                    log_cb("WARNING", f"[统计] 跳过坏 run {e.get('run')}: {e.get('错误')}")
            if progress_cb:
                progress_cb(70, "聚合统计")
            if not info.get("detail"):
                return {"info": info, "path": ""}
            p = write_statistics_workbook(info["detail"], info["stat"], out_path,
                                          getattr(self, "cfg", None))
            return {"info": info, "path": str(p or "")}

        job = Job(task, "统计报表", self)
        self._stats_job = job

        def _progress(pct, stage):
            self.status.emit(f"统计报表：{stage}（{pct}%）")

        def _failed(msg, tb):
            self._notify("统计报表失败：" + str(msg), "ERROR")

        job.progress.connect(_progress)
        job.logged.connect(self.logged.emit)
        job.done.connect(self._on_stats_done)
        job.failed.connect(_failed)
        job.cancelled.connect(lambda: self._notify("统计报表已取消（未写出文件）"))
        job.finished.connect(self._on_stats_finished)
        self._set_busy_stats(True)
        job.start()
        return True

    def _set_busy_stats(self, busy: bool) -> None:
        self.btn_stats.setEnabled(not busy)
        self.cmb_stat_scope.setEnabled(not busy)
        self.btn_stats.setText("统计中…" if busy else "统计报表…")

    def _on_stats_done(self, res) -> None:
        res = res if isinstance(res, dict) else {}
        info = res.get("info") or {}
        path = str(res.get("path") or "")
        if not path:
            self._notify("没有可统计的 run（或写盘失败）——未生成空文件", "WARNING")
            return
        self._last_stats_path = path
        self.btn_open_stats.setEnabled(True)
        self._notify(
            f"统计报表：{path}（明细 {len(info.get('detail') or [])} 行 / "
            f"统计 {len(info.get('stat') or [])} 组 / 跳过坏 run "
            f"{len(info.get('errors') or [])} 个）")

    def _on_stats_finished(self) -> None:
        self._stats_job = None
        self._set_busy_stats(False)

    def _open_stats(self) -> None:
        if not self._last_stats_path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_stats_path))
            self._notify(f"已打开：{self._last_stats_path}")
        except Exception as err:                       # noqa: BLE001
            self._notify(f"无法打开统计报表：{err}", "WARNING")

    # ══════════════ 页面钩子 ══════════════
    def on_activated(self) -> None:
        """页签被切到时重新扫描（若根目录刚刚新增了 run，一进来就是最新的）。"""
        self.reload()

    def shutdown(self) -> None:
        """退出前钩子：停掉可能仍在跑的拼版/统计 Job（V2.4-B / V2.5 FR-30）。"""
        for attr in ("_compose_job", "_stats_job"):
            job = getattr(self, attr, None)
            if job is not None and job.isRunning():
                job.cancel()
                job.wait(4000)
