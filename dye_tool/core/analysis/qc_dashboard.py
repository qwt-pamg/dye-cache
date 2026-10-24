# -*- coding: utf-8 -*-
"""P8 QC 体检仪表盘：跑正式分析**之前**，一页看清数据质量。

四件事：**饱和**、**无效点（NaN/±inf）**、**波长轴错位**、**基线漂移**。

设计要点
--------
1. **判定口径与 :mod:`core.qc` 同源**（不是"看起来像"，而是调用同一批函数）：

   ==================  ==========================================================
   量                  复用的 qc 判据
   ==================  ==========================================================
   尖峰 ``n_outlier``  :func:`core.qc._point_outlier`（逐点）+ ``_despike_candidates``
                        （预筛），与 :func:`core.qc.despike` 的循环**逐条对齐**
   饱和 ``n_saturated`` ``a > cfg["qc"]["saturation"]``，与 :func:`core.qc.flag_saturation`
                        的 ``nanmax(a) > saturation`` 同判据（见 :func:`_count_saturated`）
   无效点 ``n_nan``/``n_inf``  ``qc.despike`` 的 S1 口径：**±inf 与 NaN 是两类坏点，
                        分开计数**；占比口径见 :func:`_analyze_file`
   基线漂移            ``cfg["baseline"]["wl_range"]`` 窗口（与
                        :func:`core.qc.correct_baseline` 同一窗口、同样要求 ≥3 个有限点）
   ==================  ==========================================================

2. **阈值全部来自 cfg**，本模块不写死任何判定数字（``qc_dashboard.*`` /
   ``qc.saturation`` / ``qc.outlier.*`` / ``baseline.wl_range``）。

3. **纯计算与出图分离**：:func:`check_scans` 只算不画（GUI 表格/日志可直接消费），
   :func:`render_dashboard` 画一页图并导出。

4. **顶层不 import matplotlib**（与本项目其它绘图模块一致）：并行出图的子进程
   不该为"只用 check_scans"白加载一遍 mpl。

5. **绝不产出 PDF**：导出格式一律过 :func:`core.plotstyle.filter_formats`。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..config import section_defaults
from ..render.plotstyle import ConfigIssue
from ..render.plotting import ZH_FONTS          # 纯常量表（该模块顶层不 import matplotlib）
from .qc import _despike_candidates, _point_outlier

log = logging.getLogger("dye_tool.qc.dashboard")

__all__ = ["check_scans", "render_dashboard", "QD_DEFAULTS", "FLAG_ASCII"]


# ═══════════════════════════ 常量 ═══════════════════════════
#: ``qc_dashboard`` 节的兜底值（**旧 config.yaml 缺这一节**时的降级）。
#: V2.0 δ 批（FS2）起**不再是手抄的字面量**：直接取 :func:`core.config.section_defaults`，
#: 于是"默认值"只有 ``DEFAULT_CFG`` 一处真相——手抄的那份当下恰好相等，
#: 一旦默认值改了就会静默分叉（表里的数与会话里的 cfg 不同）。
#: 保留这个名字是因为它是**公开导出**（``__all__``）且有测试引用。
QD_DEFAULTS: dict = section_defaults("qc_dashboard")

#: 中文短标签 → 图内 ASCII 代号（图内文字保持 ASCII，Arial 恒有字形）
FLAG_ASCII: dict = {
    "饱和严重": "sat",
    "无效点多": "NaN/inf",
    "基线漂移": "drift",
    "波长轴不一致": "wl",
    "已剔除": "excluded",
    "无法读取": "unreadable",
    "长度不一致": "len",
    "检查失败": "error",
}

#: 按组着色的循环色板（前两色取自 ``config`` 的 ``spectra.abs_color``/``em_color``）
GROUP_COLORS: tuple = ("#1f4e79", "#C81E1E", "#2E7D32", "#6A1B9A", "#EF6C00",
                       "#00838F", "#4E342E", "#455A64")

#: 三类问题的高亮配色（饱和=红带 / 无效点=黑色空心圈 / 漂移=紫箭头）
#: 三类**必须互不相同**：右侧堆叠条要把"饱和"与"无效点"并排画出来。
SAT_COLOR = "#C81E1E"
NAN_COLOR = "#111111"
#: ±inf 单独一色（L13：+inf 同时算"无效点"与"饱和点"，堆叠图上必须切开画，
#: 否则条形总宽被重复计入而虚高）
INF_COLOR = "#EF6C00"
DRIFT_COLOR = "#6A1B9A"

#: 仪表盘导出 dpi（设计文档要求 ≥300；600 dpi 会让 png 体积与耗时翻两番，屏阅够用）
DASH_DPI = 300

#: 图内元素上限（防"整条谱都饱和"时画出几千个色块把一页图拖成几十秒）
_MAX_BANDS = 120        # 饱和色带
_MAX_BAD_MARKS = 400    # 无效点空心标记
_MAX_ARROWS = 8         # 漂移双向箭头
_MAX_NAMES = 6          # 图上点名的文件数
_MAX_BARS = 12          # 右侧两个小面板的条目数
_PANEL_MIN_ROWS = 6     # 小面板的最少行数（行数太少时留白，避免巨型色块）
_MAX_ISSUES = 20        # 逐文件告警条数上限（其余走 files 明细）


# ═══════════════════════════ 小工具 ═══════════════════════════
def _num(value, default: float) -> float:
    """宽松取数值：None/布尔/字符串/非有限 → ``default``（绝不抛异常）。

    yaml 里把 ``sat_frac_warn:`` 留空会得到 ``None``——这类"配置写坏"不该让
    体检整个失败，回退到默认值并照常给出结论。
    """
    if value is None or isinstance(value, bool):
        return float(default)
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float(default)
    return out if np.isfinite(out) else float(default)


def _int(value, default: int) -> int:
    """宽松取整数（非法值回退 ``default``）。"""
    got = _num(value, float(default))
    return int(got) if got > 0 else int(default)


def _qd(cfg: dict) -> dict:
    """``qc_dashboard`` 节 + 兜底值（缺节/写坏都安全降级）。"""
    got = (cfg or {}).get("qc_dashboard") if isinstance(cfg, dict) else None
    return {**QD_DEFAULTS, **(got if isinstance(got, dict) else {})}


def _baseline_range(base_cfg: dict) -> tuple:
    """基线窗 ``(lo, hi)``（与 :func:`core.qc.correct_baseline` 读的是同一个键）。"""
    rng = (base_cfg or {}).get("wl_range", [650.0, 700.0])
    try:
        lo, hi = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError, KeyError):
        lo, hi = 650.0, 700.0
    return (lo, hi) if lo <= hi else (hi, lo)


def _coerce(x) -> Optional[np.ndarray]:
    """任意输入 → 一维 float 数组；不可转换返回 ``None``（不抛异常）。"""
    if x is None:
        return None
    try:
        return np.asarray(x, float).ravel()
    except (TypeError, ValueError):
        return None


def _name(scan) -> str:
    """文件名（basename）；无 ``path`` 时退化为空串（不抛异常）。"""
    p = getattr(scan, "path", None)
    if p is None:
        return ""
    try:
        return Path(p).name
    except (TypeError, ValueError):
        return str(p)


def _key_of(scan) -> str:
    """**唯一**标识一个谱：解析后的完整路径（小写）。

    L14：basename 会撞（不同子目录同名文件），所以行字典的键必须用它。
    取不到路径时退化为 basename，再不行给空串——保证**永远返回字符串**，
    不会因为 `Path("")` 变成 `"."` 而让所有无路径的谱挤到同一个键上。
    """
    p = getattr(scan, "path", None)
    if p is None:
        return ""
    try:
        s = str(p)
        if not s:
            return ""
        return str(Path(p).resolve()).lower()
    except (TypeError, ValueError, OSError):
        try:
            return _name(scan)
        except Exception:                               # noqa: BLE001
            return ""


def _t_of(scan) -> float:
    """该谱的时间（秒）；缺失/非法 → 0.0。"""
    try:
        return _num(getattr(scan, "t", 0.0), 0.0)
    except Exception:                                    # noqa: BLE001 —— 属性可能抛
        return 0.0


def _usable(scan) -> bool:
    """该谱是否可用：优先用 ``Scan.ok``，没有该属性时按 wl/a 是否齐备判断。"""
    ok = getattr(scan, "ok", None)
    if isinstance(ok, bool):
        return ok
    if str(getattr(scan, "excluded", "") or ""):
        return False
    return _coerce(getattr(scan, "wl", None)) is not None and \
        _coerce(getattr(scan, "a", None)) is not None


def _arrays(scan) -> tuple:
    """取 ``(wl, a)``；长度不等/为空/不可转换 → ``(None, None)``。"""
    wl = _coerce(getattr(scan, "wl", None))
    a = _coerce(getattr(scan, "a", None))
    if wl is None or a is None or wl.size == 0 or wl.size != a.size:
        return None, None
    return wl, a


def _as_scan_list(scans) -> list:
    """把入参安全地变成 list：``None``/不可迭代/单支谱都能接受。"""
    if scans is None:
        return []
    if isinstance(scans, (list, tuple)):
        return list(scans)
    try:
        return list(scans)
    except TypeError:
        return [scans]


def _mode(vals: Sequence[float]) -> float:
    """众数（**平票取先出现者**，与 :func:`core.qc.qc_wavelength` 的"多数签名"一致）。

    组内波长轴的参考值必须用众数而不是"第一个文件"——否则"第一个文件恰好是
    异类"时会把整组正常文件判成异类。点数 <3 的组本就没有统计意义上的众数，
    此时平票规则退回"先出现者"，与 qc 层行为一致。
    """
    count: dict = {}
    order: list = []
    for v in vals:
        k = round(float(v), 6)                            # 抹掉浮点噪声再数
        if k not in count:
            count[k] = 0
            order.append(k)
        count[k] += 1
    if not order:
        return float("nan")
    return max(order, key=lambda k: count[k])              # max 取首个最大 → 先出现者


def _runs(idx: np.ndarray) -> list:
    """连续下标 → ``[(起, 止), ...]``（含端点）。"""
    if idx.size == 0:
        return []
    brk = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate([[0], brk + 1])
    ends = np.concatenate([brk, [idx.size - 1]])
    return [(int(idx[a]), int(idx[b])) for a, b in zip(starts, ends)]


def _filled_y(a: np.ndarray) -> np.ndarray:
    """用最近有限点的值填充无效点的 y（只为把空心标记画在曲线上）。"""
    out = np.array(a, float)
    good = np.isfinite(out)
    if not good.any():
        return np.full(out.shape, np.nan)
    if not good.all():
        idx = np.arange(out.size)
        out[~good] = np.interp(idx[~good], idx[good], out[good])
    return out


def _T(zh: str, en: str, use_zh: bool = True) -> str:
    """中/英二选一（有中文字体用中文，否则英文——与 ``core.plotting.T`` 同一约定）。"""
    return zh if use_zh else en


def _short(text: str, n: int, use_zh: bool = True) -> str:
    """截断过长文本（中文用 ``…``、ASCII 用 ``...``）。"""
    s = str(text)
    if len(s) <= n:
        return s
    return s[:n - 1] + ("…" if use_zh else ".")


def _ascii(text: str) -> str:
    """非 ASCII 字符 → ``?``（无中文字体时不画 tofu 方框）。"""
    return str(text).encode("ascii", "replace").decode("ascii")


def _cjk_font(available: set) -> str:
    """可用的第一个中文字体名（``""`` = 一个都没有 → 图内退回英文）。"""
    for name in ZH_FONTS:
        if name in available:
            return name
    return ""


def _fp_for(text, cjk: str, size: Optional[float] = None):
    """**含非 ASCII 的字符串**才换用中文字体，否则返回 ``None``（走 Arial 默认）。

    为什么要按字符串挑字体：本机 matplotlib（3.11）**没有** ``font.sans-serif``
    的逐字形回退（实测缺字形仍报 ``missing from font(s) Arial``），把中文字体
    追加进字体链并不能让中文出字。项目约定又是"Arial 优先、JACS 图件全英文"，
    所以：**ASCII 一律 Arial，只有真正含中文的那个文本对象**改成中文字体。
    """
    if not cjk or not text:
        return None
    if all(ord(ch) < 128 for ch in str(text)):
        return None
    from matplotlib.font_manager import FontProperties
    return FontProperties(family=cjk, size=size) if size else \
        FontProperties(family=cjk)


def _fig_text(text, use_zh: bool) -> str:
    """图内文字：有中文字体就原样，否则把非 ASCII 换成 ``?``（不出豆腐块）。"""
    return str(text) if use_zh else _ascii(text)


def _wrap(text: str, width: int) -> str:
    """按长度硬换行（图内右下角小字用）。"""
    s = str(text)
    return "\n".join(s[i:i + width] for i in range(0, len(s), width)) if s else ""


def _names_text(names: Sequence[str], limit: int = 5) -> str:
    """``a、b、c 等``（告警里点名用）。"""
    shown = "、".join(str(x) for x in list(names)[:limit])
    rest = len(names) - limit
    return f"{shown} 等 {len(names)} 个" if rest > 0 else shown


# ═══════════════════════════ 逐文件指标（复用 qc 判据） ═══════════════════════════
def _axis_stats(wl: Optional[np.ndarray]) -> tuple:
    """``(wl_min, wl_max, wl_step)``；不可用 → 全 NaN。

    ``wl_step`` = 排序后相邻波长差的中位数（对不规则轴也稳健；点数 <2 → NaN）。
    """
    nan = float("nan")
    if wl is None or wl.size == 0:
        return nan, nan, nan
    f = np.sort(wl[np.isfinite(wl)])
    if f.size == 0:
        return nan, nan, nan
    if f.size < 2:
        return float(f[0]), float(f[0]), nan
    return float(f[0]), float(f[-1]), float(np.median(np.diff(f)))


def _sat_threshold(scan, cfg: dict) -> float:
    """按数据来源取饱和阈值（V2.0 γ 批）——薄薄一层，真相在 :mod:`core.qc`。

    体检模块必须和 :func:`core.qc.flag_saturation` 用**同一个**阈值，否则
    "被剔除的文件 n_saturated=0" 这类自相矛盾又会回来（L23 修过一次同型问题）。
    """
    from .qc import effective_saturation
    return effective_saturation(scan, cfg)


def _count_saturated(a: Optional[np.ndarray], qc_cfg: dict,
                     sat: Optional[float] = None) -> int:
    """饱和点数：``a > 该来源的饱和阈值``。

    **与 :func:`core.qc.flag_saturation` 同判据**：那边判 ``nanmax(a) > sat``
    （NaN 被 nanmax 忽略），这里逐点判同一个不等式，于是两边恒有

        该文件被 flag_saturation 剔除  ⟺  ``n_saturated > 0``

    ``+inf > 阈值`` 为真 → **±inf 里的 +inf 同时计入饱和点数**（-inf 不计）；
    这是刻意的：饱和与"无效"是两件独立的事，一个 +inf 点两处都要看得见。

    V2.0 γ 批：阈值由调用方按**数据来源**算好传进来（
    :func:`core.qc.effective_saturation`）；不传则退回 ``qc.saturation``
    ——保持本函数原有的数组口径，兼容"只看一批数组"的调用方。
    """
    if a is None or a.size == 0:
        return 0
    if sat is None:
        sat = _num((qc_cfg or {}).get("saturation", 3.0), 3.0)
    return int(np.count_nonzero(a > sat))


def _count_outliers(a: Optional[np.ndarray], qc_cfg: dict) -> int:
    """尖峰点数：**与 :func:`core.qc.despike` 同一循环、同一判据**数出来的被剔除数。

    逐点调 :func:`core.qc._point_outlier`（qc 的尖峰判据本体），并用
    :func:`core.qc._despike_candidates` 做与 despike 相同的加速预筛——
    "预筛为空 ⇒ 循环零剔除"这一条在 qc.py 里有证明，所以快路径与慢路径同判。

    ⚠ 口径：这里数的是"``despike`` 会把该点置成 NaN"的点数，因此恒有

        ``np.isnan(despike(a, wl, qc_cfg, tag)).sum() == n_nan + n_inf + n_outlier``

    （±inf 在 despike 里先按 NaN 处理，见 qc.py 的 S1 修复。）
    """
    oc = (qc_cfg or {}).get("outlier") or {}
    if not bool(oc.get("enable", True)):
        return 0
    if a is None or a.size == 0:
        return 0
    out = np.array(a, float)
    out[np.isinf(out)] = np.nan                          # 与 despike 一致
    # L15：与 core.qc 共用同一个口径（原先两处对畸形 window 的处理不同：
    # `despike` 直接抛 ValueError，这里静默回退 7 → "体检说有尖峰、正式分析零剔除"）
    from .qc import window_half
    half = window_half(oc.get("window", 7))
    k_mad = _num(oc.get("k_mad", 6.0), 6.0)
    floor = _num(oc.get("abs_floor", 0.05), 0.05)
    if out.size <= 200_000:                              # 与 despike 同一门槛
        try:
            if _despike_candidates(out, half, k_mad, floor).size == 0:
                return 0
        except Exception as err:                         # noqa: BLE001 —— 预筛只是加速
            log.debug("QC 体检：despike 预筛异常（退回逐点）: %s", err)
    n = 0
    for i in range(out.size):
        if np.isnan(out[i]):
            continue
        drop, _med, _d = _point_outlier(out, i, half, k_mad, floor)
        if drop:
            out[i] = np.nan
            n += 1
    return n


def _baseline_drift(wl: Optional[np.ndarray], a: Optional[np.ndarray],
                    base_cfg: dict) -> float:
    """基线区（``baseline.wl_range``）内的**抬升量** = max − min（AU）。

    与 :func:`core.qc.correct_baseline` 用同一窗口、同一"有效点"口径
    （``isfinite``），同样要求 ≥3 个有限点，否则返回 NaN（= 未判定，不报漂移）。

    为什么是 max−min 而不是窗口内绝对水平：**水平**由 ``baseline.warn_above``
    管（未执行自动调零），**漂移**是"同一个窗口在谱内被抬起来了多少"。
    """
    if wl is None or a is None or a.size == 0 or wl.size != a.size:
        return float("nan")
    lo, hi = _baseline_range(base_cfg)
    m = (wl >= lo) & (wl <= hi) & np.isfinite(a)
    if int(np.count_nonzero(m)) < 3:
        return float("nan")
    v = a[m]
    return float(np.max(v) - np.min(v))


def _analyze_file(scan, cfg: dict, qd: dict) -> dict:
    """单个文件的体检行（**纯计算**）。

    无效点计数口径（V1.2 S1 起全项目统一，本模块照抄）：

    * ``n_nan`` —— ``np.isnan`` 的点数；
    * ``n_inf`` —— ``np.isinf`` 的点数（**+inf 与 −inf 合并**，见下）；
    * ``nan_frac = (n_nan + n_inf) / n_points`` —— 即 ``qc_dashboard.nan_frac_warn``
      注释里的"**无效点**占比"：``qc.despike`` 把 ±inf 与 NaN 同等当作"没有有效
      数据"（qc.py 里显式 ``out[isinf] = nan`` 并记警告），所以占比也按同一口径合并，
      但两类坏点仍**分开计数**供用户定位（"是溢出还是缺失"）。
    """
    wl = _coerce(getattr(scan, "wl", None))
    a = _coerce(getattr(scan, "a", None))
    qc_cfg = (cfg or {}).get("qc") or {}
    base_cfg = (cfg or {}).get("baseline") or {}
    excluded = str(getattr(scan, "excluded", "") or "")

    n_points = int(a.size) if a is not None else 0
    n_nan = int(np.isnan(a).sum()) if a is not None else 0
    n_inf = int(np.isinf(a).sum()) if a is not None else 0
    n_bad = n_nan + n_inf
    n_sat = _count_saturated(a, qc_cfg, _sat_threshold(scan, cfg))
    n_out = _count_outliers(a, qc_cfg)
    drift = _baseline_drift(wl, a, base_cfg)
    wl_min, wl_max, wl_step = _axis_stats(wl)

    nan_frac = (n_bad / n_points) if n_points else 0.0
    sat_frac = (n_sat / n_points) if n_points else 0.0

    flags: list = []
    if excluded:
        flags.append("已剔除")
    if wl is None or a is None:
        flags.append("无法读取")
    elif wl.size != a.size:
        flags.append("长度不一致")
    if sat_frac > _num(qd.get("sat_frac_warn"), 0.05):
        flags.append("饱和严重")
    if nan_frac > _num(qd.get("nan_frac_warn"), 0.05):
        flags.append("无效点多")
    if np.isfinite(drift) and abs(drift) > _num(qd.get("drift_warn"), 0.05):
        flags.append("基线漂移")

    return {
        # L14（评审记录）：`file` 是 basename，**不同子目录下可能同名**；
        # 拿它当字典键会让"点名/漂移箭头"配到另一支谱上。`key` 是唯一标识
        # （完整路径），图内继续用 basename 显示（好看），但**索引一律走 key**。
        "key": _key_of(scan),
        "file": _name(scan),
        "path": str(getattr(scan, "path", "") or ""),
        "group": str(getattr(scan, "group", "") or ""),
        "t": _t_of(scan),
        "n_points": n_points,
        "n_nan": n_nan,
        "n_inf": n_inf,
        "nan_frac": nan_frac,
        "n_saturated": n_sat,
        "sat_frac": sat_frac,
        # L13：±inf 的单列占比（堆叠图要切开画，见 `_draw_frac_panel`）
        "inf_frac": (n_inf / n_points) if n_points else 0.0,
        "n_outlier": n_out,
        "baseline_drift": drift,
        "wl_min": wl_min,
        "wl_max": wl_max,
        "wl_step": wl_step,
        "flags": flags,
        "excluded": excluded,                            # 上游剔除原因（告警里带上）
    }


def _error_row(scan, err: Exception) -> dict:
    """分析本身失败时的降级行（不抛异常，但也**不假装数据是干净的**）。"""
    nan = float("nan")
    return {
        "file": _name(scan), "path": str(getattr(scan, "path", "") or ""),
        "group": str(getattr(scan, "group", "") or ""), "t": _t_of(scan),
        "n_points": 0, "n_nan": 0, "n_inf": 0, "nan_frac": 0.0,
        "n_saturated": 0, "sat_frac": 0.0, "n_outlier": 0,
        "baseline_drift": nan, "wl_min": nan, "wl_max": nan, "wl_step": nan,
        "flags": ["检查失败"], "excluded": "",
        "error": f"{type(err).__name__}: {err}",
    }


# ═══════════════════════════ 组级检查 ═══════════════════════════
def _analyze_groups(items: list, rows: list, qd: dict) -> list:
    """逐组检查：波长轴一致性（**众数为参考**）+ 时间单调性/重复点。

    时间检查与 :func:`core.io.validate_times` 同口径：跳过已剔除的文件与
    ``background``/``dark`` 角色（它们本来就不参与动力学时间轴）。
    """
    buckets: dict = {}
    for i, s in enumerate(items):
        buckets.setdefault(str(getattr(s, "group", "") or ""), []).append(i)
    tol = _num(qd.get("wl_mismatch_tol"), 0.05)
    out: list = []
    for group in sorted(buckets):
        idxs = buckets[group]
        steps, los, his, sig_idx = [], [], [], []
        for i in idxs:
            r = rows[i]
            if not (np.isfinite(r["wl_step"]) and np.isfinite(r["wl_min"])
                    and np.isfinite(r["wl_max"])):
                continue                                 # wl 缺失/单点 → 不参与比对
            steps.append(r["wl_step"])
            los.append(r["wl_min"])
            his.append(r["wl_max"])
            sig_idx.append(i)
        ref_step, ref_lo, ref_hi = _mode(steps), _mode(los), _mode(his)
        mismatch: list = []
        for i in sig_idx:
            r = rows[i]
            if (abs(r["wl_step"] - ref_step) > tol
                    or abs(r["wl_min"] - ref_lo) > tol
                    or abs(r["wl_max"] - ref_hi) > tol):
                mismatch.append(r["file"])
                if "波长轴不一致" not in r["flags"]:
                    r["flags"].append("波长轴不一致")
        mismatch.sort()

        ts: list = []
        _rep_of_t: dict = {}
        for i in idxs:
            s = items[i]
            if str(getattr(s, "excluded", "") or ""):
                continue
            if str(getattr(s, "role", "sample") or "") in ("background", "dark"):
                continue
            _t = round(_t_of(s), 6)
            ts.append(_t)
            _rep_of_t.setdefault(_t, []).append(str(getattr(s, "rep", "") or ""))
        out.append({
            "group": group,
            "n_files": len(idxs),
            "n_usable": sum(1 for i in idxs if _usable(items[i])),
            "wl_consistent": not mismatch,
            "ref_wl_step": ref_step,
            "mismatch_files": mismatch,
            "t_sorted": ts == sorted(ts),
            # M2（V2.x 审查）：与 io.validate_times 同口径——同 t **同 rep** 才算重复；
            # 多数据集 `.spc` 的同 t 不同 rep 是合法数据。
            "dup_times": sorted(
                t for t, reps in _rep_of_t.items()
                if len(reps) > len(set(reps))),
        })
    return out


# ═══════════════════════════ 中文告警（ConfigIssue 风格） ═══════════════════════════
def _severity(row: dict) -> tuple:
    """最差文件排序键：标记数 → 无效+饱和占比 → |漂移|（全部可直接读出）。"""
    drift = abs(row["baseline_drift"]) if np.isfinite(row["baseline_drift"]) else 0.0
    return (len(row["flags"]), float(row["nan_frac"]) + float(row["sat_frac"]), drift)


def _file_message(row: dict, qd: dict) -> str:
    """把一个文件的全部标记写成一行中文（带精确数字，便于日志/表格核对）。"""
    parts: list = []
    if "饱和严重" in row["flags"]:
        parts.append(f"饱和点 {row['n_saturated']}/{row['n_points']} "
                     f"({row['sat_frac']:.1%} > 阈值 "
                     f"{_num(qd.get('sat_frac_warn'), 0.05):.1%})")
    if "无效点多" in row["flags"]:
        parts.append(f"无效点 {row['n_nan'] + row['n_inf']}/{row['n_points']} "
                     f"({row['nan_frac']:.1%}，其中 NaN {row['n_nan']}、"
                     f"±inf {row['n_inf']}；阈值 "
                     f"{_num(qd.get('nan_frac_warn'), 0.05):.1%})")
    if "基线漂移" in row["flags"]:
        parts.append(f"基线漂移 {row['baseline_drift']:.4f} AU "
                     f"(> {_num(qd.get('drift_warn'), 0.05):g})")
    if "波长轴不一致" in row["flags"]:
        parts.append(f"波长轴与组内多数不一致（步长 {row['wl_step']:g} nm）")
    if "无法读取" in row["flags"]:
        parts.append("没有可用的波长/吸光度数组")
    if "长度不一致" in row["flags"]:
        parts.append("波长与吸光度长度不一致")
    if "检查失败" in row["flags"]:
        parts.append(f"分析异常：{row.get('error', '')}")
    if "已剔除" in row["flags"]:
        parts.append(f"已被上游 QC 剔除（{row.get('excluded', '')}）")
    return f"文件「{row['file']}」：" + "；".join(parts) if parts else \
        f"文件「{row['file']}」：有未归类的标记 {row['flags']}"


def _group_issues(groups: list, qd: dict) -> list:
    """组级告警（波长轴 / 时间轴）。"""
    issues: list = []
    tol = _num(qd.get("wl_mismatch_tol"), 0.05)
    for g in groups:
        if g["mismatch_files"]:
            issues.append(ConfigIssue(
                "WARNING", f"qc_dashboard.wavelength.{g['group']}",
                f"组「{g['group']}」内 {len(g['mismatch_files'])} 个文件的波长轴与组内"
                f"多数不一致（众数参考步长 {g['ref_wl_step']:g} nm，容差 {tol:g} nm）："
                f"{_names_text(g['mismatch_files'])}",
                "确认这些文件是否用了不同的步长/波段；正式分析前建议先排除"))
        if not g["t_sorted"]:
            issues.append(ConfigIssue(
                "WARNING", f"qc_dashboard.time.{g['group']}",
                f"组「{g['group']}」的时间非单调（拟合会按时间排序后处理）",
                "检查文件名时间，或用时间工作表批量修正"))
        if g["dup_times"]:
            issues.append(ConfigIssue(
                "WARNING", f"qc_dashboard.time.{g['group']}",
                f"组「{g['group']}」存在重复时间点 {g['dup_times']}"
                f"（这些点会全部参与拟合）",
                "若为重复测量可保留；若是命名重复请用时间工作表区分"))
    return issues


# ═══════════════════════════ 公开接口 ①：纯计算 ═══════════════════════════
def check_scans(scans: list, cfg: dict) -> dict:
    """QC 体检（**纯计算，不画图，不改动入参**）。

    Returns:
        ``{"ok", "reason", "files", "groups", "summary", "issues", "config_issues"}``

        * ``ok``/``reason`` —— 总体结论与中文理由；
          ``ok=True`` 仅当"每个文件都可用、且没有任何标记、且各组波长轴一致"；
        * ``files`` —— 逐文件指标（见 :func:`_analyze_file`）与中文短标签 ``flags``；
        * ``groups`` —— 逐组波长轴/时间检查（见 :func:`_analyze_groups`）；
        * ``summary`` —— 全局计数与最差文件；
        * ``issues`` —— **中文告警字符串**（``[级别] 键 — 说明``，便于直接贴日志）；
        * ``config_issues`` —— 同一批告警的 :class:`core.plotstyle.ConfigIssue`
          结构化形态（GUI 问题面板可直接消费）。

    阈值全部取自 ``cfg``：``qc_dashboard.*``（占比/漂移/波长容差）、
    ``qc.saturation``、``qc.outlier.*``、``baseline.wl_range``。
    坏输入（空列表、全 NaN、``wl=None``、非谱对象）一律安全降级，绝不抛异常。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    qd = _qd(cfg)
    items = _as_scan_list(scans)

    rows: list = []
    for s in items:
        try:
            rows.append(_analyze_file(s, cfg, qd))
        except Exception as err:                         # noqa: BLE001 —— 坏文件不得打断体检
            log.warning("QC 体检：文件 %s 分析失败（降级记录）: %s", _name(s), err)
            rows.append(_error_row(s, err))

    try:
        groups = _analyze_groups(items, rows, qd)
    except Exception as err:                             # noqa: BLE001 —— 同上
        log.warning("QC 体检：组级检查失败（已跳过）: %s", err)
        groups = []

    n_files = len(rows)
    n_usable = sum(1 for s in items if _usable(s))
    flagged = [r for r in rows if r["flags"]]
    n_points = sum(r["n_points"] for r in rows)
    n_nan = sum(r["n_nan"] for r in rows)
    n_inf = sum(r["n_inf"] for r in rows)
    n_sat = sum(r["n_saturated"] for r in rows)
    n_out = sum(r["n_outlier"] for r in rows)
    drifts = [abs(r["baseline_drift"]) for r in rows
              if np.isfinite(r["baseline_drift"])]
    worst = (max(sorted(flagged, key=lambda r: r["file"]), key=_severity)
             if flagged else None)
    bad_groups = [g["group"] for g in groups if g["mismatch_files"]]

    summary = {
        "n_files": n_files,
        "n_usable": n_usable,
        "n_flagged": len(flagged),
        "n_groups": len(groups),
        "n_points": n_points,
        "n_nan": n_nan,
        "n_inf": n_inf,
        "n_saturated": n_sat,
        "n_outlier": n_out,
        "nan_frac": (n_nan + n_inf) / n_points if n_points else 0.0,
        "sat_frac": n_sat / n_points if n_points else 0.0,
        # L13：±inf 单列一个占比，供堆叠图**切开**画（+inf 同时属于"无效点"与
        # "饱和点"，不切开就会重复计入、条总宽虚高）
        "inf_frac": n_inf / n_points if n_points else 0.0,
        "max_drift": max(drifts) if drifts else float("nan"),
        "worst_file": worst["file"] if worst else "",
        "worst_reasons": list(worst["flags"]) if worst else [],
        "wl_inconsistent_groups": bad_groups,
    }

    # ── 中文告警：错误在前，其次逐文件，最后逐组 ──────────────────────
    issues: list = []
    if n_files == 0:
        issues.append(ConfigIssue(
            "ERROR", "qc_dashboard.scans", "没有可检查的光谱（扫描列表为空）",
            "请先在数据目录里载入光谱文件"))
    else:
        unreadable = [r for r in rows if "无法读取" in r["flags"]]
        if unreadable:
            issues.append(ConfigIssue(
                "ERROR", "qc_dashboard.read",
                f"{len(unreadable)} 个文件没有可用的波长/吸光度数组："
                f"{_names_text([r['file'] for r in unreadable])}",
                "检查文件格式、分隔符与 io.ncols 配置"))
        if n_usable == 0:
            issues.append(ConfigIssue(
                "ERROR", "qc_dashboard.read",
                f"{n_files} 个文件全部不可用（未读出波长/吸光度）",
                "先确认数据目录与该批文件的读取配置"))
        for r in sorted(flagged, key=lambda r: (-_severity(r)[0],
                                                -_severity(r)[1],
                                                r["file"]))[:_MAX_ISSUES]:
            if "检查失败" in r["flags"]:
                level = "ERROR"
            else:
                level = "WARNING"
            issues.append(ConfigIssue(level, f"qc_dashboard.{r['file']}",
                                      _file_message(r, qd),
                                      "详见 QC 体检仪表盘图与 files 明细"))
        if len(flagged) > _MAX_ISSUES:
            issues.append(ConfigIssue(
                "WARNING", "qc_dashboard.summary",
                f"另有 {len(flagged) - _MAX_ISSUES} 个文件有标记未逐条列出",
                "见返回值 files 明细或仪表盘右侧面板"))
    issues.extend(_group_issues(groups, qd))

    # ── 总结论 ────────────────────────────────────────────────────
    n_sat_files = sum(1 for r in rows if "饱和严重" in r["flags"])
    n_bad_files = sum(1 for r in rows if "无效点多" in r["flags"])
    n_drift_files = sum(1 for r in rows if "基线漂移" in r["flags"])
    if n_files == 0:
        ok = False
        reason = "没有可检查的光谱：扫描列表为空（ok=False）"
    elif n_usable == 0:
        ok = False
        reason = (f"{n_files} 个文件全部不可用（未读出波长/吸光度）——"
                  f"请检查文件格式与读取配置")
    elif flagged or bad_groups:
        ok = False
        reason = (f"发现 {len(flagged)}/{n_files} 个文件有质量问题"
                  f"（饱和 {n_sat_files} 个、无效点 {n_bad_files} 个、"
                  f"基线漂移 {n_drift_files} 个）"
                  + (f"；{len(bad_groups)} 个组的波长轴不一致："
                     f"{'、'.join(bad_groups)}" if bad_groups else "")
                  + "——详见 issues 与 files")
    else:
        ok = True
        reason = (f"体检通过：{n_files} 个文件 / {len(groups)} 组，未发现饱和、"
                  f"无效点、基线漂移或波长轴不一致")

    plain = [f"[{i.level}] {i.key} — {i.message}"
             + (f"（建议：{i.suggestion}）" if i.suggestion else "")
             for i in issues]
    log.info("P8 QC 体检：%s", reason)
    return {
        "ok": bool(ok),
        "reason": reason,
        "files": rows,
        "groups": groups,
        "summary": summary,
        "issues": plain,
        "config_issues": issues,
    }


# ═══════════════════════════ 公开接口 ②：一页图 ═══════════════════════════
def _select_display(scans, max_tiles: int) -> tuple:
    """挑要画的谱：**按输入顺序取前 ``max_tiles`` 条可画谱**。

    Returns:
        ``(选中列表, 可画总数)``；总数 > 选中数时调用方在图内注明"仅显示前 N 条"。
    """
    drawable = []
    for s in _as_scan_list(scans):
        wl, a = _arrays(s)
        if wl is not None:
            drawable.append(s)
    return drawable[:max(1, int(max_tiles))], len(drawable)


def _summary_caption(rep: dict, shown: int, total: int, use_zh: bool = True) -> str:
    """右下角小字（**无图内标题**，与 JACS 图注约定一致）。

    含：全局计数、最差文件、±inf 数、以及"仅显示前 N 条（共 M 条）"截断提示。
    """
    s = rep.get("summary") or {}
    cap = _T(f"QC 体检：{s.get('n_files', 0)} 个文件 / "
             f"{s.get('n_groups', 0)} 组，异常 {s.get('n_flagged', 0)} 个",
             f"QC check: {s.get('n_files', 0)} files / "
             f"{s.get('n_groups', 0)} groups, flagged {s.get('n_flagged', 0)}",
             use_zh)
    if s.get("worst_file"):
        cap += _T(f"；最差 {s['worst_file']}", f"; worst {s['worst_file']}", use_zh)
    if int(s.get("n_inf") or 0):
        cap += _T(f"；±inf {s['n_inf']} 个", f"; {s['n_inf']} inf", use_zh)
    if total > shown:
        cap += _T(f"；仅显示前 {shown} 条（共 {total} 条）",
                  f"; showing first {shown} of {total}", use_zh)
    return cap


def _out_base(out_path) -> Path:
    """导出路径 → 无扩展名的**主干**（给目录则写 ``<目录>/qc_dashboard.<ext>``）。"""
    out = Path(out_path)
    if out.is_dir():
        return out / "qc_dashboard"
    return out.with_suffix("") if out.suffix else out


def render_dashboard(scans: list, cfg: dict, out_path) -> Optional[Path]:
    """把 :func:`check_scans` 的结果画成**一页**仪表盘并导出。

    版面（固定画布、不用 ``bbox_inches="tight"``，与 JACS 一致）：

    * **左/主体**：全部谱按组着色叠加（最多 ``max_tiles`` 条，超出在图内注明
      "仅显示前 N 条"）；**饱和段**画醒目色带、**无效点**画空心标记（落在曲线
      缺失处）、**基线漂移**画双向箭头 + 基线窗浅阴影；有问题的文件在图上**点名**；
    * **右上**：逐文件"无效点 / 饱和点"占比（堆叠横条 + 阈值虚线）；
    * **右下**：逐文件基线漂移 AU（横条 + 阈值虚线）。

    图内**不出现标题**；说明信息走右下角小字（caption）与 ``check_scans["issues"]``。
    Arial 字体、刻度朝内、四边完整框、``DASH_DPI``=300 dpi。

    导出格式经 :func:`core.plotstyle.filter_formats`（**全项目唯一的不出-PDF
    出口**）——``export: pdf`` 会被静默替换成 png，绝不产出 PDF。

    Args:
        scans: ``Scan`` 列表（也接受单支谱/``None``）。
        cfg: 全量配置 dict。
        out_path: 输出路径。给目录 → ``<dir>/qc_dashboard.<ext>``；
            给带后缀的文件名 → 换/补后缀；给无后缀主干 → 追加后缀。

    Returns:
        写出的**第一个**路径；``enable=False`` 或任何失败 → ``None`` 并记
        ``log.warning``（**绝不抛异常**）。
    """
    qd = _qd(cfg)
    if not bool(qd.get("enable", True)):
        log.info("P8 QC 仪表盘已禁用（qc_dashboard.enable=false）→ 不出图")
        return None
    try:
        rep = check_scans(scans, cfg)
        path = _draw(rep, scans, cfg, qd, out_path)
        if path is None:
            log.warning("P8 QC 仪表盘：没有写出任何文件（格式被过滤或路径不可用）")
        return path
    except Exception as err:                             # noqa: BLE001 —— 顶层不得抛
        log.warning("P8 QC 仪表盘出图失败（已跳过，不影响主流程）: %s", err,
                    exc_info=True)
        return None


def _draw(rep: dict, scans, cfg: dict, qd: dict, out_path) -> Optional[Path]:
    """真正出图（**延迟 import matplotlib**：本模块顶层不碰 mpl）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    from ..render.plotting import apply_jacs_style
    from ..render.plotting_jacs import ensure_font, jcfg
    from ..render.plotstyle import filter_formats

    apply_jacs_style()                                   # JACS 版式：刻度朝内/四边框/小字号
    ensure_font(cfg)                                     # Arial 优先（JACS 约定）
    # matplotlib 3.11 实测**没有**逐字形回退（中文字体塞进 font.sans-serif 也照样
    # 报 missing glyph）→ 这里只"探测"中文字体是否可用：可用时给**含中文的文本
    # 对象**单独指定字体（见 _fp_for），不可用时图内文字整体退回英文（_T）。
    cjk = _cjk_font({f.name for f in font_manager.fontManager.ttflist})
    use_zh = bool(cjk)
    j = jcfg(cfg)

    selected, n_drawable = _select_display(scans, _int(qd.get("max_tiles"), 60))
    h = _num(qd.get("fig_height_in"), 6.5)
    w = 2.0 * _num(j.get("width_in"), 3.25)              # 双栏宽：仪表盘是屏阅文档

    fig = plt.figure(figsize=(w, h), layout="constrained")
    try:
        # 右列三格 = 占比面板 / 漂移面板 / 说明文字（说明独立成格，**不压数据**）
        gs = fig.add_gridspec(3, 2, width_ratios=(2.7, 1.0),
                              height_ratios=(1.0, 1.0, 0.26))
        ax = fig.add_subplot(gs[0:2, 0])
        _draw_overlay(ax, selected, scans, rep, cfg, j, use_zh, cjk)
        _draw_frac_panel(fig.add_subplot(gs[0, 1]), rep["files"], cfg, qd, j,
                         use_zh, cjk)
        _draw_drift_panel(fig.add_subplot(gs[1, 1]), rep["files"], cfg, qd, j,
                          use_zh, cjk)
        ax_cap = fig.add_subplot(gs[2, 1])
        ax_cap.set_axis_off()
        cap = _summary_caption(rep, len(selected), n_drawable, use_zh)
        ax_cap.text(0.0, 0.95, _wrap(cap, 30 if use_zh else 42),
                    transform=ax_cap.transAxes, ha="left", va="top",
                    fontsize=float(j["annot_size"]) * 0.8, color="0.35",
                    linespacing=1.6, fontproperties=_fp_for(cap, cjk))

        base = _out_base(out_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        raw_fmt = qd.get("export", "png")                  # 允许写成列表（宽容）
        fmts = filter_formats(raw_fmt if isinstance(raw_fmt, (list, tuple))
                              else [raw_fmt])              # ← 唯一的不出-PDF 出口
        targets = [base.with_name(f"{base.name}.{ext}") for ext in fmts]
        for p in targets:
            fig.savefig(p, dpi=DASH_DPI, facecolor="white", edgecolor="none")
        log.info("P8 QC 仪表盘已保存: %s（%s @%ddpi）",
                 "、".join(x.name for x in targets), "|".join(str(e) for e in fmts),
                 DASH_DPI)
        return targets[0] if targets else None
    finally:
        plt.close(fig)


def _draw_overlay(ax, selected: list, scans, rep: dict, cfg: dict,
                  j: dict, use_zh: bool, cjk: str) -> None:
    """主体：按组着色叠加 + 三类问题高亮 + 点名（说明文字在右下角单独一格）。"""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    from ..render.plotting_jacs import finish_axes

    rows = {r.get("key") or r["file"]: r for r in rep["files"]}      # L14：键用唯一标识
    by_name = {}
    for s in _as_scan_list(scans):
        by_name.setdefault(_key_of(s), s)      # L14：唯一键，不用 basename
    groups = [g["group"] for g in rep["groups"]]
    color_of = {g: GROUP_COLORS[i % len(GROUP_COLORS)] for i, g in enumerate(groups)}
    lo_wl, hi_wl = _baseline_range((cfg or {}).get("baseline") or {})

    ax.axvspan(lo_wl, hi_wl, color="0.9", alpha=0.7, lw=0, zorder=0)  # 基线窗阴影

    xs: list = []
    ys: list = []
    for s in selected:                                   # ① 全部谱按组着色叠加
        wl, a = _arrays(s)
        if wl is None:
            continue
        g = str(getattr(s, "group", "") or "")
        ax.plot(wl, a, color=color_of.get(g, "0.45"), lw=0.65, alpha=0.8, zorder=3)
        xs.append(wl[np.isfinite(wl)])
        ys.append(a[np.isfinite(a)])

    xparts = [v for v in xs if v.size]
    yparts = [v for v in ys if v.size]
    allx = np.concatenate(xparts) if xparts else np.array([])
    ally = np.concatenate(yparts) if yparts else np.array([])
    if allx.size:                                        # 固定 x 范围：色带/阴影不改变量程
        x0, x1 = float(np.min(allx)), float(np.max(allx))
        pad = 0.01 * (x1 - x0) if x1 > x0 else 1.0
        ax.set_xlim(x0 - pad, x1 + pad)
    if ally.size:
        y0, y1 = float(np.min(ally)), float(np.max(ally))
        span = (y1 - y0) if y1 > y0 else 1.0
        ax.set_ylim(min(0.0, y0), y1 + _num(j.get("ylim_top_headroom"), 0.05) * span)

    if not allx.size:                                    # 无可画谱 → 安全降级
        reason = _T(rep["reason"], _ascii(rep["reason"]), use_zh)
        ax.text(0.5, 0.5, _wrap(reason, 46), transform=ax.transAxes, ha="center",
                va="center", fontsize=float(j["annot_size"]), color="0.35",
                linespacing=1.5, fontproperties=_fp_for(reason, cjk))

    n_bands = n_marks = 0
    for s in selected:                                   # ② 饱和段 + 无效点
        wl, a = _arrays(s)
        if wl is None:
            continue
        # V2.0 γ：饱和阈值**按文件的数据来源**取（荧光原始强度 vs 吸光度），
        # 否则 FDS 的整条谱都会被涂成饱和色带（实测：默认阈值 3.0 时 100% 点）。
        sat_s = _sat_threshold(s, cfg)
        if n_bands < _MAX_BANDS:
            hot = np.isfinite(a) & (a > sat_s)
            if hot.any():
                hw = (0.5 * float(np.median(np.abs(np.diff(wl))))
                      if wl.size > 1 else 0.5)
                hw = hw if np.isfinite(hw) and hw > 0 else 0.5
                for i0, i1 in _runs(np.flatnonzero(hot)):
                    if n_bands >= _MAX_BANDS:
                        break
                    ax.axvspan(float(wl[i0]) - hw, float(wl[i1]) + hw,
                               color=SAT_COLOR, alpha=0.22, lw=0, zorder=1)
                    n_bands += 1
        if n_marks < _MAX_BAD_MARKS:
            bad = ~np.isfinite(a)
            if bad.any():
                yq = _filled_y(a)
                sel = np.flatnonzero(bad)[:_MAX_BAD_MARKS - n_marks]
                keep = np.isfinite(yq[sel])
                if keep.any():
                    ax.plot(wl[sel][keep], yq[sel][keep], ls="none", marker="o",
                            ms=2.6, mfc="none", mec=NAN_COLOR, mew=0.7, zorder=6)
                    n_marks += int(np.count_nonzero(keep))

    drift_rows = sorted((r for r in rows.values()
                         if "基线漂移" in r["flags"]
                         and np.isfinite(r["baseline_drift"])),
                        key=lambda r: (-abs(r["baseline_drift"]), r["file"]))
    shown_arrows = drift_rows[:_MAX_ARROWS]
    for k, row in enumerate(shown_arrows):               # ③ 基线漂移双向箭头
        s = by_name.get(row.get("key") or "")
        wl, a = _arrays(s) if s is not None else (None, None)
        if wl is None:
            continue
        m = (wl >= lo_wl) & (wl <= hi_wl) & np.isfinite(a)
        if int(np.count_nonzero(m)) < 2:
            continue
        v = a[m]
        xc = 0.5 * (lo_wl + hi_wl) + (k - (len(shown_arrows) - 1) / 2.0) * \
            0.03 * (hi_wl - lo_wl)                       # 左右错开，避免箭头叠在一起
        if allx.size:
            xc = min(max(xc, float(np.min(allx))), float(np.max(allx)))
        ax.annotate("", xy=(xc, float(np.max(v))), xytext=(xc, float(np.min(v))),
                    zorder=7, arrowprops=dict(arrowstyle="<->", color=DRIFT_COLOR,
                                              lw=0.8, shrinkA=0, shrinkB=0))

    named = sorted((r for r in rows.values() if r["flags"]), key=_severity,
                   reverse=True)[:_MAX_NAMES]
    for k, row in enumerate(named):                      # ④ 有问题的文件点名
        s = by_name.get(row.get("key") or "")
        wl, a = _arrays(s) if s is not None else (None, None)
        if wl is None or not np.isfinite(a).any():
            continue
        jmax = int(np.argmax(np.where(np.isfinite(a), a, -np.inf)))
        xpk, ypk = float(wl[jmax]), float(a[jmax])
        right = allx.size and xpk > float(np.min(allx)) + 0.65 * \
            (float(np.max(allx)) - float(np.min(allx)))
        codes = ",".join(FLAG_ASCII.get(f, "?") for f in row["flags"])
        label = _short(_fig_text(row["file"], use_zh), 24, use_zh) + f" [{codes}]"
        ax.annotate(label, xy=(xpk, ypk),
                    xytext=(-5 if right else 5, 4 + 6 * (k % 3)),
                    textcoords="offset points", ha="right" if right else "left",
                    va="bottom", fontsize=float(j["annot_size"]) * 0.8,
                    color="0.15", zorder=9, fontproperties=_fp_for(label, cjk),
                    arrowprops=dict(arrowstyle="-", color="0.5", lw=0.5,
                                    shrinkA=0, shrinkB=1))

    handles = [Line2D([], [], color=color_of[g], lw=1.2,
                      label=_short(_fig_text(g, use_zh), 16, use_zh))
               for g in groups[:5]]
    if len(groups) > 5:
        handles.append(Line2D([], [], color="none",
                              label=_T(f"…另 {len(groups) - 5} 组",
                                       f"...+{len(groups) - 5} groups", use_zh)))
    handles += [
        Patch(facecolor=SAT_COLOR, alpha=0.22, edgecolor="none",
              label=_T("饱和段", "saturated", use_zh)),
        Line2D([], [], ls="none", marker="o", ms=3.0, mfc="none", mec=NAN_COLOR,
               mew=0.7, label=_T("无效点(NaN/±inf)", "invalid (NaN/inf)", use_zh)),
        Line2D([], [], color=DRIFT_COLOR, lw=0.9, marker=">", ms=2.5,
               label=_T("基线漂移", "baseline drift", use_zh)),
    ]
    # 图例里有中文 → 整块换中文字体（legend 只能一个 prop）；否则保持 Arial
    legend_prop = _fp_for("".join(str(h.get_label()) for h in handles), cjk,
                          float(j.get("legend_size", 6.5)))
    ax.legend(handles=handles, loc=str(j.get("legend_loc", "best")),
              frameon=False, handlelength=1.4, labelspacing=0.25, borderpad=0.2,
              **({"prop": legend_prop} if legend_prop is not None
                 else {"fontsize": float(j.get("legend_size", 6.5))}))

    finish_axes(ax, cfg)                                 # 刻度朝内/四边完整框/无标题


def _panel_common(ax, vals: list, j: dict, cfg: dict, use_zh: bool, cjk: str,
                  xlabel: str, thr: float, empty_note: str) -> np.ndarray:
    """右侧小面板的公共部分：横条 + y 轴文件名 + 阈值虚线 + JACS 轴样式。

    ``vals`` 只放**非零**条目（全是 0 时画一条中文/英文说明，比 12 根零长度条
    清楚得多）。
    """
    from ..render.plotting_jacs import finish_axes

    idx = np.arange(len(vals))
    if vals:
        ax.set_yticks(idx)
        ax.set_yticklabels([_short(_fig_text(v[0], use_zh), 16, use_zh)
                            for v in vals])
        for lbl, v in zip(ax.get_yticklabels(), vals):    # 中文文件名单独换字体
            fp = _fp_for(v[0], cjk)
            if fp is not None:
                lbl.set_fontproperties(fp)
        # 行数少于 _PANEL_MIN_ROWS 时补足量程，免得两根条目撑成两块巨型色块
        ax.set_ylim(max(len(vals), _PANEL_MIN_ROWS) - 0.5, -0.5)
    else:
        ax.text(0.5, 0.5, empty_note, transform=ax.transAxes, ha="center",
                va="center", fontsize=float(j["annot_size"]), color="0.4",
                fontproperties=_fp_for(empty_note, cjk))
    ax.set_xlim(0.0, max(1e-3, thr * 1.6, max((v[1] for v in vals), default=0.0) * 1.2))
    ax.axvline(thr, color="0.35", ls="--", lw=0.7, zorder=4)
    finish_axes(ax, cfg, xlabel=xlabel, ylabel="")
    fp_x = _fp_for(xlabel, cjk, float(j["label_size"]))
    if fp_x is not None:                                 # 中文轴标签同样单独换字体
        ax.xaxis.label.set_fontproperties(fp_x)
    ax.tick_params(axis="y", labelsize=float(j["tick_size"]) * 0.8)
    return idx


def _draw_frac_panel(ax, rows: list, cfg: dict, qd: dict, j: dict,
                     use_zh: bool, cjk: str) -> None:
    """右上：逐文件"无效点 / 饱和点"占比（堆叠横条）。"""
    top = [r for r in sorted(rows, key=lambda r: (-(r["nan_frac"] + r["sat_frac"]),
                                                  r["file"]))
           if (r["nan_frac"] + r["sat_frac"]) > 0][:_MAX_BARS]
    vals = [(r["file"], r["nan_frac"] + r["sat_frac"]) for r in top]
    idx = _panel_common(ax, vals, j, cfg, use_zh, cjk,
                        _T("无效点 / 饱和点占比", "invalid / saturated fraction",
                           use_zh),
                        _num(qd.get("nan_frac_warn"), 0.05),
                        _T("未发现无效点或饱和点", "no invalid / saturated points",
                           use_zh))
    if top:
        # L13（评审记录）：`nan_frac` 已含 ±inf，而 `+inf` 又同时计入 `sat_frac`
        # （因为 inf > 阈值 为真），于是**含 inf 的文件条形总宽被放大**——多画了
        # 那部分 +inf。修法：把 +inf 单独切出来，三段互不重叠地堆叠。
        inf_f = np.array([r.get("inf_frac", 0.0) for r in top], float)
        bad = np.clip(np.array([r["nan_frac"] for r in top], float) - inf_f, 0.0, None)
        satv = np.clip(np.array([r["sat_frac"] for r in top], float)
                       - inf_f, 0.0, None)
        base = np.zeros_like(bad)
        for seg, col in ((bad, NAN_COLOR), (inf_f, INF_COLOR),
                         (satv, SAT_COLOR)):
            if float(np.nansum(seg)) > 0:
                ax.barh(idx, seg, left=base, height=0.62, color=col,
                        alpha=0.75, lw=0, zorder=5)
            base = base + np.nan_to_num(seg)


def _draw_drift_panel(ax, rows: list, cfg: dict, qd: dict, j: dict,
                      use_zh: bool, cjk: str) -> None:
    """右下：逐文件基线漂移 |AU|（横条）。"""
    finite = [r for r in rows if np.isfinite(r["baseline_drift"])]
    top = [r for r in sorted(finite, key=lambda r: (-abs(r["baseline_drift"]),
                                                    r["file"]))
           if abs(r["baseline_drift"]) > 0][:_MAX_BARS]
    vals = [(r["file"], abs(r["baseline_drift"])) for r in top]
    idx = _panel_common(ax, vals, j, cfg, use_zh, cjk,
                        _T("基线漂移 / AU", "baseline drift / AU", use_zh),
                        _num(qd.get("drift_warn"), 0.05),
                        _T("基线区无漂移（或有效点不足未判定）",
                           "no baseline drift (or window too sparse)", use_zh))
    if top:
        ax.barh(idx, [abs(r["baseline_drift"]) for r in top], height=0.62,
                color=DRIFT_COLOR, alpha=0.7, lw=0, zorder=5)
