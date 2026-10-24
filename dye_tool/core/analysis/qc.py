# -*- coding: utf-8 -*-
"""质量检查层：波长轴对齐、饱和判定、单点去尖峰、基线校正。

逐函数迁移自 legacy ``singlet_o2_phi`` 的 F2/QC 段，数值行为保持不变。
"""
from __future__ import annotations

import logging
import warnings
from typing import Sequence

import numpy as np

from ..inputs.io import Scan

log = logging.getLogger("dye_tool.qc")


# ═══════════════════════════ 配置口径（唯一实现，供本模块与 qc_dashboard 共用）═══
def window_half(raw, default: int = 7) -> int:
    """``qc.outlier.window`` → 滚动窗**半宽**（``window // 2``，下限 0）。

    L15（评审记录）：这里原来直接 ``int(oc.get("window", 7)) // 2``——
    ``window: "abc"`` 会抛 ``ValueError`` 穿透整条流水线，而 ``qc_dashboard``
    的同名判定却静默回退 7 → 出现"体检说有尖峰、正式分析直接崩/零剔除"的错位。
    现在两处共用本函数：坏值**统一回退默认值**（并记一条 WARNING 让用户知道），
    口径不可能再分叉。

    ``window <= 0`` → 半宽 0（窗口只有中心点，永不剔除），与旧行为一致。
    """
    try:
        v = int(raw)
    except (TypeError, ValueError):
        if raw is not None and str(raw).strip():
            log.warning("qc.outlier.window 配置无效（%r）→ 回退 %d", raw, default)
        v = default
    return max(0, v // 2)


# ═══════════════════════════ 波长轴对齐 ═══════════════════════════
def qc_wavelength(scans: Sequence[Scan], cfg: dict) -> list[Scan]:
    """波长轴对齐检查：以多数签名（点数+首尾波长）为参考。

    超差文件整组剔除并把原因写入 ``scan.excluded``；返回被剔除的列表
    供 GUI 文件清单展示。
    """
    tol = float(cfg["wavelength"].get("align_tol", 0.51))
    sigs: dict = {}
    for s in scans:
        if s.excluded or s.wl is None:
            continue
        key = (s.wl.size, round(float(s.wl[0]), 1), round(float(s.wl[-1]), 1))
        sigs.setdefault(key, []).append(s)
    if not sigs:
        return []
    ref_key = max(sigs, key=lambda k: len(sigs[k]))
    dropped: list[Scan] = []
    for key, ss in sigs.items():
        if key != ref_key:
            for s in ss:
                s.excluded = (f"波长轴与多数文件不一致"
                              f"({key[1]:.1f}-{key[2]:.1f} nm/{key[0]} 点)")
                log.warning("剔除 %s: %s", s.path.name, s.excluded)
                dropped.append(s)
    ref = sigs[ref_key][0]
    for s in sigs[ref_key][1:]:
        if float(np.max(np.abs(s.wl - ref.wl))) > tol:
            s.excluded = "波长轴与参考文件点对点不一致"
            log.warning("剔除 %s: %s", s.path.name, s.excluded)
            dropped.append(s)
    return dropped


#: **按仪器原始强度**（非吸光度）看待的数据来源。除此之外**一律**按吸光度处理。
#:
#: ⚠ 写成"白名单否定"而不是"吸光度白名单肯定"是**故意的**（实测踩过）：
#: 第一版写 ``src in ("txt","csv","spc")`` → 任何**未知来源**（新格式、旧对象没有
#: source 字段、拼错的值）都会掉进 else 分支，拿走 `saturation_counts` 的 1e9，
#: 等于**默默把门禁关了**。而那次测试还"通过"了——因为未知来源取到了配置里那个
#: 1e9，与 fallback 撞了个正着。现在只有明确的 ``"fds"`` 才放宽。
_COUNTS_SOURCES = ("fds",)

#: 已就"FDS 用吸光度阈值"提示过的进程级标记（一次性提示，别每个文件刷一条）
_COUNTS_HINTED = False


def effective_saturation(scan, cfg: dict) -> float:
    """按**数据来源**选饱和阈值（V2.0 γ 批 / FR-15，U3）。

    为什么需要（实测，2026-10-17）：`qc.saturation` 默认 3.0 是**吸光度**阈值，
    而 `.fds` 存的是荧光仪**原始强度**（夹具实测 174.9–9986.4）——默认配置下
    3/3 个夹具**全部被剔除**，且每条谱 100% 的点都被标成"饱和"
    （`n_saturated` = 点数）。用户必须先去 `config.yaml` 手改 `qc.saturation=1e9`
    才能用，这正是 UX-2 要消灭的"必须读文档才能用"。

    路由规则（严格约束见设计文档 FR-15）：

    * ``source == "fds"``（荧光**原始强度**）→ ``qc.saturation_counts``（默认 1e9≈关闭）；
    * **其余一切**（``txt``/``csv``/``spc``，以及未知来源与缺字段的老对象）
      → ``qc.saturation``，**判据、默认值、行为一字不改**（A2/A4.2 红线的结构性保证）。
      保守优先：新格式默认**按吸光度**处理，要放宽必须显式加进
      :data:`_COUNTS_SOURCES`，绝不"因为不认识就默默放行"。

    签名不动的是 :func:`saturation_mask`（纯数组口径，见其 docstring）——路由只能
    在**持有 Scan** 的这一层做（core/README.md 的口径表约束）。
    """
    qc = (cfg or {}).get("qc") or {}
    src = str(getattr(scan, "source", "txt") or "txt")
    if src in _COUNTS_SOURCES:
        key, fallback = "saturation_counts", 1e9
    else:
        key, fallback = "saturation", 3.0
    try:
        val = float(qc.get(key, fallback))
    except (TypeError, ValueError):
        log.warning("qc.%s 配置无效（%r）→ 回退 %g", key, qc.get(key), fallback)
        return fallback
    # **一次性现场提示**（UX-2：不弹模态框，只留日志）：用户仍用吸光度阈值时说明一下
    global _COUNTS_HINTED
    if key == "saturation_counts" and not _COUNTS_HINTED:
        _COUNTS_HINTED = True
        log.info("检测到荧光原始强度数据（source=%s）：已按荧光量纲判定饱和"
                 "（阈值 qc.saturation_counts=%g），**不需要**手改 "
                 "qc.saturation=%g", src, val, float(qc.get("saturation", 3.0)))
    return val


def flag_saturation(scans: Sequence[Scan], cfg: dict) -> list[Scan]:
    """饱和判定：最大吸光度/强度 > 该来源的饱和阈值 → 剔除（legacy run_core 内联）。

    V2.0 γ 批：阈值按 :func:`effective_saturation` **按数据来源**取——
    吸光度路径（txt/csv/spc）仍旧是 ``qc.saturation``（默认 3.0），
    荧光原始强度（fds）走 ``qc.saturation_counts``（默认 1e9）。
    """
    dropped: list[Scan] = []
    for s in scans:
        if s.excluded or s.a is None:
            continue
        sat = effective_saturation(s, cfg)
        if np.isfinite(s.a).any() and float(np.nanmax(s.a)) > sat:
            s.excluded = f"吸光度饱和(max={float(np.nanmax(s.a)):.2f}>{sat:g})"
            log.warning("剔除 %s: %s", s.path.name, s.excluded)
            dropped.append(s)
    return dropped


#: 本模块公开函数的**参数口径**（L10，评审记录点名"同一模块三种口径"）：
#:
#: ==========================  ==================  ==============================
#: 函数                        第二个参数          口径
#: ==========================  ==================  ==============================
#: ``qc_wavelength``           ``cfg``             **全量**配置（读 ``qc`` 节）
#: ``flag_saturation``         ``cfg``             **全量**配置（读 ``qc`` 节）
#: ``flag_negative``           ``cfg``             **全量**配置（读 ``qc`` 节）
#: ``despike``                 ``qc_cfg``          **子配置**（= ``cfg["qc"]``）
#: ``correct_baseline``        ``base_cfg``        **子配置**（= ``cfg["baseline"]``）
#: ``interp_valid``            ——                  纯数组函数，不读配置
#: ==========================  ==================  ==============================
#:
#: 这个不一致是**继承 legacy** 的（迁移时逐函数保持签名），改动它会波及
#: ``pipeline`` 与全部测试，收益只是"好看"。所以选择**写清楚**而不是改签名：
#: 需要"只看数组、不传 cfg"时用下面两个 ``*_mask`` 辅助函数。
def saturation_mask(a, qc_cfg: dict):
    """[历史辅助] 按**吸光度**阈值判断饱和点（O12：无生产调用方）。

    V2.x 审查 O12：gamma 批起饱和度判据按数据源路由（``flag_saturation``，
    荧光原始强度用另一套），本函数**只服务旧 API/测试**。新代码请用
    :func:`flag_saturation`——本函数永远读 ``qc.saturation`` 的吸光度口径，
    对荧光强度谱会误判（见 ``excel_export`` 的 L23 注释）。
    """
    """``a`` 里超过饱和阈值的布尔掩码（``qc.saturation``；``<=0`` 视为不检查）。

    给"只想判数组、不想构造 Scan"的调用方（如 QC 体检图、外部脚本）用。
    判据与 :func:`flag_saturation` **同一不等式**（``nanmax(a) > sat``），
    不另立一套。

    ⚠ **签名与语义刻意不动**（V2.0 γ 批 / FR-15 的严格约束）：本函数是**纯数组
    口径**，手上没有"数据来源"，所以它**永远**读 ``qc.saturation``。
    按来源路由只能在持有 ``Scan`` 的那一层做——见 :func:`effective_saturation`
    与 ``core/README.md`` 的参数口径表。需要按来源判的调用方请显式传入
    已经选好的阈值。
    """
    try:
        sat = float((qc_cfg or {}).get("saturation", 3.0))
    except (TypeError, ValueError):
        return np.zeros(np.asarray(a).shape, dtype=bool)
    arr = np.asarray(a, float)
    if not (sat > 0) or arr.size == 0:
        return np.zeros(arr.shape, dtype=bool)
    with np.errstate(all="ignore"):
        return np.asarray(arr > sat, dtype=bool)


def flag_negative(scans: Sequence[Scan], cfg: dict) -> None:
    """基线校正后残留负值点：只记 INFO/标记，不剔除（legacy 行为）。"""
    if not cfg["qc"].get("flag_negative", True):
        return
    for s in scans:
        if s.excluded or s.a is None:
            continue
        n_neg = int((s.a < 0).sum())
        if n_neg:
            log.info("%s: %d 个负吸光度点(基线校正后残留, 标记不剔除, 最小 %.4f)",
                     s.path.name, n_neg, float(np.nanmin(s.a)))


# ═══════════════════════════ 去尖峰 ═══════════════════════════
def _point_outlier(out: np.ndarray, i: int, half: int, k_mad: float,
                   floor: float) -> tuple[bool, float, float]:
    """逐点判定（与 :func:`despike` 的循环**同一判据**，供两者共用）。

    Returns:
        ``(是否剔除, 邻域中位数, 偏离量)``。
    """
    seg = out[max(0, i - half): i + half + 1]
    seg = seg[~np.isnan(seg)]
    if seg.size < 3:
        return False, float("nan"), float("nan")
    med = float(np.median(seg))
    d = abs(float(out[i]) - med)
    if d <= floor:                       # 陡峭光谱边缘由 abs_floor+MAD 双重保护
        return False, med, d
    mad = float(np.median(np.abs(seg - med)))
    return (d > k_mad * mad), med, d


def _despike_candidates(out: np.ndarray, half: int, k_mad: float,
                        floor: float) -> np.ndarray:
    """向量化预筛：返回**可能**被剔除的下标（关于逐点循环的保守超集）。

    判据必须与 :func:`_point_outlier` **逐条对齐**，否则"预筛为空 ⇒ 循环零剔除"
    这个提前返回就不成立。S1 修的就是这里的不对齐：预筛原先用 ``isfinite`` 计窗口
    有效点、用 ``~isnan`` 排除自身，而循环用 ``~isnan``——含 ±inf 的窗口于是出现
    "预筛为空但循环会剔除"的漏判（``inf`` 被静默保留，``-inf`` 还会污染基线）。

    现在两边统一为 ``~isnan``（±inf 算有效值），并且：

    * 自身是 NaN → 不判（循环第一句就 ``continue``）；
    * ``med`` 本身是 ±inf 或 NaN → 不判（循环里 ``d`` 非有限，两个比较都为 False）；
    * ``mad`` 是 NaN 或 0 → 用 ``k_mad * 0 = 0`` 判定（与循环的 ``d > 0`` 一致）。

    ⚠ **O14：本函数是"逐点循环"的保守超集，但整个循环本身并不是"原始数组判定的
    超集"**——早先 docstring 里把这两件事混为一谈。反例（fuzz 实测约 5%）：
    某个点被剔除后，**后续窗口的 MAD 会收缩**，于是原本够不到阈值的点反而超阈而被
    剔除。所以严格命题只有下面这一条，也正是代码真正依赖的那一条：

        **预筛（基于原始数组）为空 ⇒ 循环一个点都不会剔除。**

    证明还是一句话的归纳：预筛是所有"循环可能剔除点"的超集；预筛既然为空，就
    不存在"第一个被置 NaN 的点"，于是循环第 i 步看到的窗口与原始数组一致、判定
    与预筛一致 → 一个都不剔除。非有限值（±inf）已在上游归为 NaN，故不构成例外。
    """
    n = out.size
    off = np.arange(-half, half + 1)
    idx = np.arange(n)[:, None] + off[None, :]
    valid = (idx >= 0) & (idx < n)
    win = np.where(valid, out[np.clip(idx, 0, n - 1)], np.nan)
    keep = ~np.isnan(win)                     # 与循环一致：±inf 算有效值
    cnt = keep.sum(axis=1)
    with warnings.catch_warnings():
        # 全 NaN 行（原始数据本来就是 NaN）会让 nanmedian 报 All-NaN slice；
        # 那些行的 cnt < 3 本来就轮不到判定，这里静音即可。
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(all="ignore"):
            med = np.nanmedian(win, axis=1)
            mad = np.nanmedian(np.abs(win - med[:, None]), axis=1)
            d = np.abs(out - med)
            # d 非有限（含 med 为 ±inf/NaN 的情形）→ 一律不判，交给循环
            thr = np.where(np.isnan(mad), 0.0, k_mad * mad)
            cand = ((cnt >= 3) & (~np.isnan(out)) & np.isfinite(d)
                    & (d > floor) & (d > thr))
    return np.flatnonzero(cand)


#: V2.6 工程收尾项 D13（先测量再决定）：超过该点数的光谱直接跳过 despike。
#: 本机实测（2026-10-18）：401 点 8.1 ms；200,000 点 **3.44 s/谱**（预筛向量化
#: 后仍 O(n·window)），而真实仪器谱最大约 800 点（.spc 801/601、txt 401）。
#: 该阈值分支只影响 >200k 的异常大谱：仍保留 enable/NaN/±inf 语义，但不做
#: 单点剔除（改由 WARNING 明示）——数值红线 A2/A4.2 全为 401 点，不经过该分支。
MAX_DESPIKE_POINTS = 200_000


def despike(a: np.ndarray, wl: np.ndarray, qc_cfg: dict, tag: str) -> np.ndarray:
    """单点异常剔除（如突然接近 0）：滚动中位数 ± k×MAD，逐点记日志。

    陡峭光谱边缘由 ``abs_floor`` 与 MAD 双重保护，避免把真实吸收边削掉。

    **性能（V1.2）**：原实现对每个点调两次 ``np.median``——300 文件（每组
    401 点）实测占 core 总耗时的 **89%**（12 万次 median）。这里先做一次
    向量化预筛：

    * 若**没有任何候选点** → 逐点循环必然一个都不剔除，直接返回副本。
      该结论是可证明的：预筛是"循环可能剔除点"的**超集**（所有窗口都基于原始
      数组，不考虑"前面刚被置 NaN 的点会缩小后面的窗口"）。预筛既然为空，就不
      存在第一个被置 NaN 的点，归纳可知循环与预筛逐点同判。干净数据（绝大多数）
      因此省掉全部 median。
    * 若**有候选点** → 回退到原来的逐点循环，保证语义与日志一字不差
      （异常点本来就罕见，慢一点无所谓）。

    ⚠ 注意别把上面的命题读成"循环是原始数组判定的超集"：**不是**（O14）。
    被剔除的点会让后续窗口的 MAD 收缩，从而多剔掉一些原本不超阈的点。本函数
    依赖的只有"预筛空 ⇒ 零剔除"这一条，它成立。
    """
    oc = qc_cfg.get("outlier", {})
    out = a.astype(float).copy()
    if not oc.get("enable", True) or out.size == 0:
        return out
    # A.M9（V2.x 审查）：`k_mad`/`abs_floor` 原来直接 `float()`，而 `qc_dashboard`
    # 同口径用 `_num()` 静默回退 → 坏配置下"体检正常、正式分析中断"。
    # 这里统一成"非法值回退默认 + WARNING"，与体检口径一致（宁可用默认值也别中断）。
    try:
        _k_mad = float(oc.get("k_mad", 6.0))
    except (TypeError, ValueError):
        log.warning("qc.outlier.k_mad=%r 不是数值 → 回退 6.0", oc.get("k_mad"))
        _k_mad = 6.0
    try:
        _abs_floor = float(oc.get("abs_floor", 0.05))
    except (TypeError, ValueError):
        log.warning("qc.outlier.abs_floor=%r 不是数值 → 回退 0.05",
                    oc.get("abs_floor"))
        _abs_floor = 0.05
    # ±inf 在 despike/基线里都是"污染源"：median 一旦吃到 inf 就全变 inf，
    # 而且 flag_saturation 只查正向饱和、拦不住 -inf。按 NaN 同等对待（两者
    # 在整条流水线里都表示"这一点没有有效数据"），并明确告知用户。
    n_inf = int(np.isinf(out).sum())
    if n_inf:
        log.warning("%s: %d 个非有限值(±inf) → 按无效点(NaN)处理", tag, n_inf)
        out[np.isinf(out)] = np.nan
    if out.size > MAX_DESPIKE_POINTS:
        # D13：>200k 点谱在本机实测 3.4 s/谱（401 点仅 8 ms），不值得为异常大谱
        # 卡住整批；跳过剔除并明确告知（不改 enable/NaN/inf 语义）。
        log.warning("%s: %d 点 > %d，跳过 despike（D13 预筛；实测 200k 约 3.4 s/谱）",
                    tag, int(out.size), MAX_DESPIKE_POINTS)
        return out
    half = window_half(oc.get("window", 7))
    k_mad, floor = _k_mad, _abs_floor
    if out.size <= 200_000:                        # 超大谱退回逐点，避免大分配
        try:
            if _despike_candidates(out, half, k_mad, floor).size == 0:
                return out
        except Exception as err:                   # noqa: BLE001 —— 预筛只是加速
            log.debug("despike 预筛异常（本谱退回逐点）: %s", err)
    for i in range(out.size):
        if np.isnan(out[i]):
            continue
        drop, med, d = _point_outlier(out, i, half, k_mad, floor)
        if drop:
            log.info("弃用异常数据点 %s: λ=%.2f nm, A=%.4f (邻域中位数 %.4f, 偏离 %.4f → NaN)",
                     tag, float(wl[i]), out[i], med, d)
            out[i] = np.nan
    return out


# ═══════════════════════════ 基线校正 ═══════════════════════════
def correct_baseline(wl: np.ndarray, a: np.ndarray, base_cfg: dict, tag: str
                     ) -> tuple[np.ndarray, float]:
    """基线扣除（应对仪器忽略自动调零）：长波无吸收窗 median/mean/linear。

    返回 ``(校正后吸光度, 基线值)``；窗口有效点不足 3 时跳过并返回 NaN 基线。
    """
    if not base_cfg.get("enable", True):
        return a, 0.0
    lo, hi = base_cfg.get("wl_range", [650.0, 700.0])
    m = (wl >= float(lo)) & (wl <= float(hi)) & np.isfinite(a)
    if int(m.sum()) < 3:
        log.warning("%s: 基线窗口 %.0f-%.0f nm 有效点不足(%d) → 跳过校正",
                    tag, lo, hi, int(m.sum()))
        return a, float("nan")
    method = base_cfg.get("method", "median")
    if method == "linear":
        b1, b0 = np.polyfit(wl[m].astype(float), a[m], 1)
        bl = b0 + b1 * wl
        base, corr = float(np.mean(bl)), a - bl
    else:
        base = float(np.median(a[m])) if method != "mean" else float(np.mean(a[m]))
        corr = a - base
    if abs(base) > float(base_cfg.get("warn_above", 0.01)):
        log.warning("%s: 基线不为零(%.4f AU, 疑未执行自动调零) → 已扣除 [%s]",
                    tag, base, method)
    else:
        log.debug("%s: 基线 %.4f AU 已扣除", tag, base)
    return corr, base


# ═══════════════════════════ 插值工具 ═══════════════════════════
def interp_valid(wl: np.ndarray, a: np.ndarray, wl_new: np.ndarray) -> np.ndarray:
    """在有效点（非 NaN）上线性插值到 ``wl_new``；有效点 <2 → 全 0。

    注：legacy 中名为 ``_interp_valid``，迁移后改为公开函数以便单测覆盖。
    """
    m = np.isfinite(a)
    if int(m.sum()) < 2:
        return np.zeros_like(wl_new, dtype=float)
    return np.interp(wl_new, wl[m], a[m])
