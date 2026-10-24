# -*- coding: utf-8 -*-
"""动力学层：背景扣除、探针信号提取、归一化、准一级拟合、exp2/exp3 拟合。

迁移自 legacy ``singlet_o2_phi`` 的 M1–M4 段与 ``compare_kobs`` 的
探针/拟合段。**算法一字未改**，仅把 ``print`` 换成 ``logging``。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from ..inputs.io import Scan
from .qc import interp_valid

log = logging.getLogger("dye_tool.kinetics")

#: 优化 #7（V2.x 审查 W4）：``scipy.stats`` 约占启动 import 1.8 s，而 GUI 启动
#: 不需要它——只有"真的拟合"才需要。改为**首次使用时导入**（模块级缓存：
#: 第二次起只是属性查找）。这也让出图子进程不必为拟合依赖付出启动成本。
_STATS = None
_CURVE_FIT = None


def _stats():
    """惰性拿 ``scipy.stats``（首次调用才 import）。"""
    global _STATS
    if _STATS is None:
        from scipy import stats as _s
        _STATS = _s
    return _STATS


def _curve_fit():
    """惰性拿 ``scipy.optimize.curve_fit``。"""
    global _CURVE_FIT
    if _CURVE_FIT is None:
        from scipy.optimize import curve_fit as _cf
        _CURVE_FIT = _cf
    return _CURVE_FIT


# ═══════════════════════════ 拟合结果结构 ═══════════════════════════
@dataclass
class FitResult:
    """准一级拟合结果。``status`` ∈ OK/WARN/FAILED。"""
    k: float = float("nan")
    k_err: float = float("nan")
    r2: float = float("nan")
    n: int = 0
    intercept: float = float("nan")
    truncated: bool = False
    status: str = "OK"
    message: str = ""
    pre_k: float = float("nan")        # 截断前的全窗口拟合
    pre_r2: float = float("nan")
    x: Optional[np.ndarray] = None     # 拟合所用横轴(t/s 或剂量 J/cm²)
    lnR: Optional[np.ndarray] = None   # 拟合所用 ln R


# ═══════════════════════════ M1: 背景扣除 ═══════════════════════════
def subtract_background(scans: Sequence[Scan], bg_cfg: dict) -> dict:
    """M1: ``A_corr = A_sample − A_background``（matched=按时间最近 / 其余=平均谱）。"""
    if not bg_cfg.get("enable", True):
        return {}
    bgs = [s for s in scans if s.role == "background" and not s.excluded]
    if not bgs:
        log.warning("M1: 未找到背景组→ 跳过背景扣除")
        return {}
    mode = bg_cfg.get("mode", "matched")
    mean_wl = mean_a = None
    if mode != "matched":
        mean_wl = bgs[0].wl
        mean_a = np.mean([interp_valid(b.wl, b.a, mean_wl) for b in bgs], axis=0)
    for s in scans:
        if s.excluded or s.role == "background":
            continue
        if mode == "matched":
            b = min(bgs, key=lambda bb: abs(bb.t - s.t))
            bg_a, src = interp_valid(b.wl, b.a, s.wl), b.path.name
        else:
            bg_a, src = interp_valid(mean_wl, mean_a, s.wl), "背景平均谱"
        s.a = s.a - bg_a
        log.debug("%s: 已扣除背景 %s", s.path.name, src)
    log.info("M1: 已对 %d 个文件扣除背景(模式=%s)",
             sum(1 for s in scans if not s.excluded and s.role != "background"), mode)
    return {"mode": mode, "n_bg": len(bgs)}


# ═══════════════════════════ M2: 探针提取 ═══════════════════════════
def extract_probe(wl: np.ndarray, a: np.ndarray, wl_cfg: dict, tag: str) -> float:
    """M2: ``A(t)`` = λ_probe±window 内最大值（抗峰漂移）；window≤0 → 严格单点。"""
    lp = float(wl_cfg.get("probe", 379.0))
    win = float(wl_cfg.get("probe_window", 2.0))
    if win <= 0:
        i = int(np.argmin(np.abs(wl - lp)))
        if not np.isfinite(a[i]):
            log.warning("%s: λ=%.1f nm 值缺失 → A(t)=NaN", tag, float(wl[i]))
            return float("nan")
        return float(a[i])
    m = (wl >= lp - win) & (wl <= lp + win) & np.isfinite(a)
    if not m.any():
        log.warning("%s: 探针窗口 %.0f±%.0f nm 无有效数据 → A(t)=NaN", tag, lp, win)
        return float("nan")
    return float(np.max(a[m]))


def probe_abs(wl: np.ndarray, ab: np.ndarray, lam: float, win: float) -> Optional[float]:
    """``lam±win`` 内有效数据均值；无有效数据 → ``None``（legacy compare_kobs 语义）。"""
    wl = np.asarray(wl, float)
    ab = np.asarray(ab, float)
    m = (wl >= lam - win) & (wl <= lam + win) & np.isfinite(ab)
    return float(np.mean(ab[m])) if m.sum() else None


# ═══════════════════════════ M3: 归一化 ═══════════════════════════
def normalize_series(t: np.ndarray, A: np.ndarray, a0_cfg: dict, tag: str,
                     a_floor: float = 0.0) -> tuple[float, np.ndarray]:
    """M3: A(0)=暗态点均值；无暗态 → 最早 n_early 点均值 + WARNING。

    ``A < a_floor`` 的点掩码为 NaN，防止 ln 出现 ``-inf``。
    """
    t, A = np.asarray(t, float), np.asarray(A, float)
    dark = np.isfinite(A) & (t <= float(a0_cfg.get("dark_max_time", 0.0)))
    if dark.any():
        A0 = float(np.mean(A[dark]))
        log.info("%s: A(0)=%.4f (%d 个暗态点均值, t≤%.0f s)", tag, A0,
                 int(dark.sum()), float(a0_cfg.get("dark_max_time", 0.0)))
    else:
        n_e = max(1, int(a0_cfg.get("n_early", 2)))
        idx = [int(i) for i in np.argsort(t) if np.isfinite(A[i])][:n_e]
        A0 = float(np.mean(A[idx])) if idx else float("nan")
        log.warning("%s: 无暗态点(t≤%.0f s) → A(0)=%.4f 取最早 %d 点均值(t=%s s)",
                    tag, float(a0_cfg.get("dark_max_time", 0.0)), A0, len(idx),
                    "/".join(f"{t[i]:.0f}" for i in idx))
    if not np.isfinite(A0) or A0 == 0:
        log.error("%s: A(0)=%s 无效, R 全部置 NaN", tag, A0)
        return A0, np.full(A.shape, np.nan)
    R = A / A0
    for i in np.where(np.isfinite(A) & (A < a_floor))[0]:
        log.info("%s: t=%.0f s A=%.4f < a_floor=%g → 掩码(防 ln→-inf)",
                 tag, t[i], A[i], a_floor)
        R[i] = np.nan
    return A0, R


# ═══════════════════════════ M4: 准一级拟合 + 自动截断 ═══════════════════════════
def _linfit(x: np.ndarray, y: np.ndarray, tag: str, label: str) -> FitResult:
    """单次 ln 空间线性回归（scipy.stats.linregress，最小二乘）。"""
    lr = _stats().linregress(x, y)
    log.info("%s[%s]: k=%.6e ± %.2e, R²=%.5f, n=%d",
             tag, label, -lr.slope, lr.stderr, lr.rvalue ** 2, x.size)
    return FitResult(k=float(-lr.slope), k_err=float(lr.stderr),
                     r2=float(lr.rvalue ** 2), n=int(x.size),
                     intercept=float(lr.intercept),
                     x=np.asarray(x), lnR=np.asarray(y))


def _quad_p(x: np.ndarray, resid: np.ndarray) -> float:
    """残差对 x 二次项显著性 p 值（系统性弯曲检验）。"""
    if resid.size < 6 or float(np.max(np.abs(resid))) < 1e-10:
        return 1.0
    X = np.column_stack([np.ones_like(x), x, x * x])
    try:
        beta, *_ = np.linalg.lstsq(X, resid, rcond=None)
        r = resid - X @ beta
        dof = resid.size - 3
        s2 = float(r @ r) / dof
        if s2 <= 0:
            return 1.0
        cov = s2 * np.linalg.inv(X.T @ X)
        tstat = float(beta[2] / np.sqrt(cov[2, 2]))
        return float(2.0 * _stats().t.sf(abs(tstat), dof))
    except np.linalg.LinAlgError:
        return 1.0


def window_params(kin: dict) -> tuple[float, float]:
    """``kinetics`` 子配置 → ``(r_min, r_max)``（含 ``r_max_tol``）。

    与 :func:`window_mask` 一起构成"拟合窗"的**唯一**定义（评审记录「重复逻辑 #5」）。
    原先 ``excel_export._kinetics_rows`` 手工复刻了同一套判据——两处各写一遍必然
    漂移，于是"表里说参与了拟合、拟合其实没用到该点"这种错位迟早出现。
    """
    r_min = float(kin.get("r_min", 0.30))
    r_max = float(kin.get("r_max", 1.0)) + float(kin.get("r_max_tol", 0.05))
    return r_min, r_max


def window_mask(t: np.ndarray, R: np.ndarray, kin: dict,
                x: Optional[np.ndarray] = None) -> np.ndarray:
    """拟合窗布尔掩码：``R ∈ [r_min, r_max+tol]`` 且 t/x 有限。

    **判据与 :func:`fit_kinetics` 内部逐字一致**（那支现在也调本函数），
    所以导出侧与拟合侧不可能再分叉。
    """
    r_min, r_max = window_params(kin)
    t = np.asarray(t, float)
    R = np.asarray(R, float)
    x = np.asarray(t if x is None else x, float)
    return np.isfinite(R) & np.isfinite(x) & (R >= r_min) & (R <= r_max)


def fit_kinetics(t: np.ndarray, R: np.ndarray, cfg: dict, tag: str,
                 x: Optional[np.ndarray] = None) -> FitResult:
    """M4: ``ln R = −k·x``（**自然对数**）。

    仅拟合 ``R ∈ [r_min, r_max+tol]``；残差系统性弯曲（二次项 p 显著）时
    自动截断到最早的 ``truncate_frac`` 比例点。
    """
    kin = cfg["kinetics"]
    t, R = np.asarray(t, float), np.asarray(R, float)
    x = np.asarray(t if x is None else x, float)
    r_min, r_max = window_params(kin)
    fin = np.isfinite(R) & np.isfinite(x)
    for i in np.where(fin & ((R < r_min) | (R > r_max)))[0]:
        why = (f"R={R[i]:.3f}<{r_min:.2f}(信号过弱)" if R[i] < r_min
               else f"R={R[i]:.3f}>{r_max:.2f}(超出线性段)")
        log.info("%s: 剔除 t=%.0f s 数据点(%s)", tag, t[i], why)
    m = window_mask(t, R, kin, x)
    xf, y = x[m], np.log(R[m])                     # ← 自然对数, 严禁 log10
    if xf.size < 3 or float(np.ptp(xf)) == 0:
        return FitResult(status="FAILED", message=f"有效点不足或横轴无跨度(n={xf.size})")
    o = np.argsort(xf, kind="stable")
    xf, y = xf[o], y[o]
    res = _linfit(xf, y, tag, "全窗口")
    if bool(kin.get("auto_truncate", True)) and res.n >= 6:
        p = _quad_p(xf, y - (res.intercept - res.k * xf))
        if p < float(kin.get("curvature_p", 0.05)):
            nkeep = max(3, int(round(float(kin.get("truncate_frac", 0.70)) * xf.size)))
            res2 = _linfit(xf[:nkeep], y[:nkeep], tag, "截断后")
            log.warning("%s: 残差系统性弯曲(二次项 p=%.3g < %.2g) → 自动截断, "
                        "保留最早 %d/%d 点(横轴≤%.3g); 全窗口 k=%.4e → 截断后 k=%.4e",
                        tag, p, float(kin.get("curvature_p", 0.05)), nkeep, xf.size,
                        float(xf[nkeep - 1]), res.k, res2.k)
            res2.truncated, res2.pre_k, res2.pre_r2 = True, res.k, res.r2
            res2.message = f"auto_truncate: p={p:.3g}, 保留 {nkeep}/{xf.size} 点"
            res = res2
    if res.k <= 0:
        res.status, res.message = "WARN", f"k={res.k:.3e} ≤ 0(无有效衰减)"
    elif res.r2 < float(cfg["qc"].get("r2_warn", 0.98)):
        res.status, res.message = "WARN", \
            f"R²={res.r2:.4f} < {float(cfg['qc'].get('r2_warn', 0.98)):.2f}"
    return res


# ═══════════════════════════ exp2 / exp3 自动选择（FR-2） ═══════════════════════════
def _exp2(t: np.ndarray, A0: float, k: float) -> np.ndarray:
    """两参数指数衰减 ``A0·exp(-k t)``。"""
    return A0 * np.exp(-k * t)


def _exp3(t: np.ndarray, A0: float, k: float, c: float) -> np.ndarray:
    """三参数指数衰减 + 常数底 ``A0·exp(-k t) + c``。"""
    return A0 * np.exp(-k * t) + c


def fit_series(t: np.ndarray, A: np.ndarray) -> dict:
    """``A(t) = A0·e^(-kt) (+c)``；自动在 2/3 参数模型里选（legacy 原样）。

    返回 dict：``{ok, model, A0, k, c, k_err, R2, msg}``。
    """
    r = {"ok": False, "model": "", "A0": None, "k": None, "c": 0.0,
         "k_err": None, "R2": None, "msg": ""}
    t = np.asarray(t, float)
    A = np.asarray(A, float)
    good = np.isfinite(t) & np.isfinite(A)
    t, A = t[good], A[good]
    if len(t) < 4:
        r["msg"] = "点数<4"
        return r
    span = float(t.max() - t.min())
    if span <= 0:
        r["msg"] = "时间无跨度"
        return r
    if A.max() - A.min() < 1e-6:
        r["msg"] = "吸光度无变化"
        return r

    cmax = max(float(A.min()), 0.0)
    A0max = max(float(A.max()), 1e-6)
    kmax = 50.0 / span
    c0 = 0.3 * cmax
    A00 = max(float(A[np.argmin(t)]) - c0, 0.5 * A0max)
    den = max(float(A[np.argmax(t)]) - c0, 1e-9)
    k0 = float(np.log(max(A00, 1e-9) / den)) / span
    if not np.isfinite(k0) or k0 <= 0:
        k0 = 0.7 / span
    k0 = min(max(k0, 1e-6), 0.9 * kmax)
    A00 = min(max(A00, 1e-6), A0max)

    best = None
    if cmax >= 5e-3:
        try:
            p, pc = _curve_fit()(_exp3, t, A, p0=(A00, k0, c0), maxfev=20000,
                              bounds=([1e-9, 1e-9, 0.0], [A0max, kmax, cmax]))
            ss = float(np.sum((A - _exp3(t, *p)) ** 2))
            st = float(np.sum((A - A.mean()) ** 2))
            best = ("exp3", p, np.sqrt(np.diag(pc)),
                    1 - ss / st if st > 0 else 1.0)
        except Exception:                              # noqa: BLE001
            best = None
    try:
        p2, pc2 = _curve_fit()(_exp2, t, A, p0=(A00, k0), maxfev=20000,
                            bounds=([1e-9, 1e-9], [A0max, kmax]))
        ss = float(np.sum((A - _exp2(t, *p2)) ** 2))
        st = float(np.sum((A - A.mean()) ** 2))
        r2 = 1 - ss / st if st > 0 else 1.0
        perr2 = np.sqrt(np.diag(pc2))
    except Exception:                                  # noqa: BLE001
        p2, r2, perr2 = None, None, None

    if best and (r2 is None or best[3] > r2 + 0.005):
        model, p, perr, rr = best
    elif p2 is not None:
        model, p, perr, rr = "exp2", p2, perr2, r2
    else:
        r["msg"] = "拟合不收敛, 请检查数据"
        return r

    r.update(ok=True, model=model, A0=float(p[0]), k=float(p[1]),
             c=float(p[2]) if model == "exp3" else 0.0,
             k_err=float(perr[1]) if np.isfinite(perr[1]) else None,
             R2=float(rr))
    if r["R2"] < 0.9:
        r["msg"] = "R²偏低, 拟合仅供参考"
    return r


# ═══════════════════════════ FR-2 序列汇总 ═══════════════════════════
def group_kinetics(group: dict, cfg: dict) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """组 → ``(t, A@探针波长, 被跳过的文件名)``（legacy compare_kobs 语义）。"""
    lam = cfg["wavelength"]["probe"]
    win = cfg["wavelength"]["probe_window"]
    pts, skipped = [], []
    for m in group["members"]:
        a = probe_abs(m["wl"], m["ab"], lam, win)
        if a is None or not np.isfinite(a):
            skipped.append(m["name"])
            continue
        pts.append((m["t"], a))
    pts.sort(key=lambda p: p[0])
    t = np.array([p[0] for p in pts], float)
    A = np.array([p[1] for p in pts], float)
    if len(set(t.tolist())) < len(t):
        log.warning("[%s] 时间点重复, 请检查是否混入别的实验", group.get("label", "?"))
    return t, A, skipped


def excitation_abs(group: dict, cfg: dict) -> tuple[Optional[float], float]:
    """最早一张谱在激发波长的吸光度 → ``(A_exc, 被吸收占比 f)``。

    顺带做**内滤效应（IFC）提示**（V1.2 E2）：``A_ex > 0.1`` 时激发光在
    光路内被显著吸收，荧光强度不再正比于浓度/产率，相对法会系统性偏低。
    这里只**提示**、不修改任何数值——是否稀释重测由实验者决定。
    """
    lam = cfg["wavelength"]["excitation"]
    win = cfg["wavelength"]["probe_window"]
    m0 = group["members"][0]                      # members 已按 t 排序
    a = probe_abs(m0["wl"], m0["ab"], lam, win)
    if a is None:
        return None, 0.0
    thr = float((cfg.get("quantum_yield") or {}).get("ifc_warn_above", 0.10))
    if thr > 0 and a > thr:
        log.warning(
            "内滤效应提示：λ_ex=%.0f nm 处 A=%.3f > %.2f（组 %s）——激发光在光路内"
            "被明显吸收，荧光强度不再与浓度/产率成正比，相对法结果会系统性偏低。"
            "建议稀释到 A<%.2f 重测，或按 A_corr = A·10^(A_ex/2) 校正并在文中说明",
            lam, a, thr, group.get("label") or group.get("group") or "?", thr)
    return a, (1 - 10 ** (-a) if a > 0 else 0.0)
