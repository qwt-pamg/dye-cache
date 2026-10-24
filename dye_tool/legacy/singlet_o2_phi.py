#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
singlet_o2_phi.py — 单线态氧 (¹O₂) 相对量子产率 Φ_Δ 一站式计算工具  v1.0.0

用法:
    python singlet_o2_phi.py --self-test               # 内置验收自检(无需数据)
    python singlet_o2_phi.py <数据目录>                # 正式分析(默认参数已调好)
    python singlet_o2_phi.py <数据目录> --dry-run      # 只解析+QC, 不拟合
    python singlet_o2_phi.py --make-synthetic [目录]   # 生成 synthetic 测试数据
    python singlet_o2_phi.py --plot-only <run目录>     # 复用已有结果只重绘图

数据约定:
    每文件 = 一个时间点的全波长扫描(两列: 波长,吸光度, 逗号分隔, 乱码头自动跳过, 空值→NaN)。
    文件名最后一个横杠后的数字 = 辐照秒数: "xx"→xx 秒; "xx+y"→xx+60y 秒; 无后缀→t=0。
    组别角色按前缀识别: bg/ref/dark/n2/nan3/blank, 其余视为样品(sample)。

输出: runs/run_<时间戳>/ 下 results.csv · fit_summary.json · report.md · run.log
      · figures/ (图a 光谱叠加[标注 ABDA 379/激发 532/荧光团峰] · 图b 动力学拟合
      · 图c k柱状 · 图d Φ森林 · 图e 残差; 均为 svg+png @300dpi)
依赖: Python≥3.9 + numpy scipy matplotlib (pyyaml 可选)
"""
from __future__ import annotations

import argparse, copy, csv, json, logging, re, shutil, subprocess, sys, tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from scipy import stats

VERSION = "1.0.0"
logger = logging.getLogger("phi")
_ZH = [True]                      # 全局: 图表是否用中文(由字体探测决定)


def T(zh: str, en: str) -> str:
    """图表文字: 有中文字体用中文, 否则回退英文。"""
    return zh if _ZH[0] else en


# ═══════════════════════════ 默认配置(全部魔法数字集中于此) ═══════════════════════════
DEFAULT_CFG: dict = {
    "io": {"extensions": [".txt", ".csv"], "recursive": True, "exclude": ["_"],
           "encoding": "gbk", "separator": ",", "ncols": 2},
    "filename": {
        "time_pattern": r"^(?P<stem>.+)-(?P<sec>\d+)(?:\+(?P<min>\d+))?$",
        "group_pattern": r"^(?P<group>.+?)(?:_(?P<rep>\d+))?$",
        "no_time_suffix": "zero",            # zero→t=0 | background→背景文件
        "roles": {"bg": "background", "blank": "blank", "dark": "dark",
                  "ref": "reference", "n2": "n2", "nan3": "quencher",
                  "azide": "quencher"}},
    "wavelength": {"probe": 379.0, "probe_window": 2.0, "excitation": 532.0,
                   "align_tol": 0.51},
    "baseline": {"enable": True, "wl_range": [650.0, 700.0], "method": "median",
                 "warn_above": 0.01},
    "background": {"enable": True, "mode": "matched"},   # matched=按时间最近扣
    "a0": {"dark_max_time": 0.0, "n_early": 2},          # 无暗态→最早2点均值
    "kinetics": {"r_min": 0.30, "r_max": 1.00, "r_max_tol": 0.05, "a_floor": 0.01,
                 "auto_truncate": True, "truncate_frac": 0.70, "curvature_p": 0.05,
                 "dose_normalize": False, "irradiance": {"default": 1.0}},
    "quantum_yield": {"phi_ref": 0.75, "phi_ref_err": 0.075, "iabs_mode": "exact",
                      "a_ex": {}, "a_ex_err": 0.005, "bootstrap": False,
                      "bootstrap_n": 1000, "seed": 42},
    "qc": {"a0_range": [0.05, 1.5], "saturation": 3.0, "flag_negative": True,
           "outlier": {"enable": True, "window": 7, "k_mad": 6.0, "abs_floor": 0.05},
           "dark_k_rel_max": 0.05, "r2_warn": 0.98},
    "controls": {"enable": True, "n2_inhibition_min": 0.70,
                 "quencher_inhibition_min": 0.70},
    "plot": {"dpi": 300, "formats": ["svg", "png"],
             "spectra_overlay": {"select_times": "all", "cmap": "viridis",
                                 "mark_probe": True, "mark_excitation": True,
                                 "mark_fluorophore": "auto",
                                 "fluorophore_search": [540.0, 620.0]}},
    "run": {"log_level": "INFO"},
}


# ═══════════════════════════ 数据结构 ═══════════════════════════
@dataclass
class Scan:
    """单个光谱文件(一个时间点)。excluded 非空 = 被剔除原因。"""
    path: Path
    group: str
    rep: str = ""
    role: str = "sample"
    t: float = 0.0
    wl: Optional[np.ndarray] = None
    a: Optional[np.ndarray] = None
    excluded: str = ""


@dataclass
class FitResult:
    """准一级拟合结果。status ∈ OK/WARN/FAILED。"""
    k: float = float("nan"); k_err: float = float("nan"); r2: float = float("nan")
    n: int = 0; intercept: float = float("nan")
    truncated: bool = False; status: str = "OK"; message: str = ""
    pre_k: float = float("nan"); pre_r2: float = float("nan")   # 截断前的全窗口拟合
    x: Optional[np.ndarray] = None      # 拟合所用横轴(t/s 或 剂量 J/cm²)
    lnR: Optional[np.ndarray] = None    # 拟合所用 ln R


# ═══════════════════════════ 小工具 ═══════════════════════════
def deep_merge(base: dict, extra: dict) -> dict:
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Optional[Path]) -> dict:
    cfg = copy.deepcopy(DEFAULT_CFG)
    if path is None:
        return cfg
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
    except ImportError:
        print(f"[错误] 未安装 pyyaml, 无法读取 {path} (pip install pyyaml)"); sys.exit(2)
    except Exception as err:
        print(f"[错误] 读取配置失败 {path}: {err}"); sys.exit(2)
    deep_merge(cfg, user)
    logger.info("已加载配置: %s", path)
    return cfg


def read_text_robust(path: Path) -> str:
    """兼容 GBK/UTF-8/UTF-16/BOM 的文本读取(数据行均为 ASCII, 头部乱码无害)。"""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:400]:
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def git_hash() -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


class _Collector(logging.Handler):
    """收集 WARNING 及以上日志, 供 report.md 汇总。"""
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(self.format(record))


def setup_logging(logfile: Path, debug: bool) -> _Collector:
    logger.setLevel(logging.DEBUG)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG); fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO); ch.setFormatter(fmt)
    col = _Collector(); col.setFormatter(fmt)
    for h in (fh, ch, col):
        logger.addHandler(h)
    return col


def make_run_dir(root: Path) -> Path:
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / f"run_{ts}"; i = 1
    while d.exists():
        d = root / f"run_{ts}_{i}"; i += 1
    d.mkdir()
    return d


def _jsonable(o):
    """递归转 JSON 安全类型; NaN/Inf → None。"""
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return _jsonable(o.tolist())
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if np.isfinite(v) else None
    if isinstance(o, (np.integer, int, bool, str)) or o is None:
        return o
    return str(o)


def _fmt(v, spec=".6g") -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    return format(v, spec) if np.isfinite(v) else "N/A"


def _fnum(v, default=np.nan) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-.]+", "_", name)


# ═══════════════════════════ F1: 文件名解析与读取 ═══════════════════════════
def parse_filename(stem: str, fn: dict) -> dict:
    """文件名主干 → {group, rep, t, role}。时间 = 最后一个横杠后的数字。"""
    m = re.match(fn.get("time_pattern",
                        r"^(?P<stem>.+)-(?P<sec>\d+)(?:\+(?P<min>\d+))?$"), stem)
    if m:
        t = float(m.group("sec")) + 60.0 * float(m.group("min") or 0)
        head = m.group("stem")
    else:
        t, head = 0.0, stem
    gm = re.match(fn.get("group_pattern",
                         r"^(?P<group>.+?)(?:_(?P<rep>\d+))?$"), head)
    group = gm.group("group") if gm else head
    rep = (gm.group("rep") or "") if gm else ""
    roles = fn.get("roles", {})
    role = roles.get(group) or next(
        (roles[k] for k in sorted(roles, key=len, reverse=True)
         if group.startswith(k)), "sample")
    if m is None and fn.get("no_time_suffix", "zero") == "background":
        role = "background"
    return {"group": group, "rep": rep, "t": t, "role": role}


def read_scan(path: Path, io_cfg: dict):
    """读取单文件 → (波长, 吸光度)。自动跳过标题/乱码/列数不符行; 空值→NaN。"""
    sep, ncols = io_cfg.get("separator", ","), int(io_cfg.get("ncols", 2))
    rows = []
    for lineno, line in enumerate(read_text_robust(path).splitlines(), 1):
        parts = [p.strip().strip('"').strip("'") for p in line.split(sep)]
        if len(parts) != ncols:
            continue
        try:
            w = float(parts[0])
        except ValueError:
            continue                                   # 标题/列名/乱码行
        if parts[1] == "":
            logger.info("%s 第%d行: 吸光度为空 → NaN", path.name, lineno)
            v = float("nan")
        else:
            try:
                v = float(parts[1])
            except ValueError:
                logger.info("%s 第%d行: 吸光度 %r 非数值 → NaN", path.name, lineno, parts[1])
                v = float("nan")
        rows.append((w, v))
    if len(rows) < 10:
        raise ValueError(f"有效数据行不足({len(rows)} 行)")
    wl = np.array([r[0] for r in rows])
    a = np.array([r[1] for r in rows], float)
    if np.any(np.diff(wl) <= 0):
        o = np.argsort(wl, kind="stable"); wl, a = wl[o], a[o]
    return wl, a


def collect_scans(data_dir: Path, cfg: dict) -> list:
    """递归扫描目录, 解析元数据并读取光谱(坏文件剔除并记日志)。"""
    io_cfg, fn_cfg = cfg["io"], cfg["filename"]
    exts = tuple(e.lower() for e in io_cfg.get("extensions", [".txt", ".csv"]))
    excl = tuple(io_cfg.get("exclude", ["_"]))
    it = data_dir.rglob("*") if io_cfg.get("recursive", True) else data_dir.glob("*")
    scans = []
    for path in sorted(p for p in it if p.is_file()):
        if path.suffix.lower() not in exts or path.name.startswith(excl):
            continue
        meta = parse_filename(path.stem, fn_cfg)
        s = Scan(path=path, group=meta["group"], rep=meta["rep"],
                 role=meta["role"], t=meta["t"])
        try:
            s.wl, s.a = read_scan(path, io_cfg)
        except (ValueError, OSError) as err:
            s.excluded = f"读取失败: {err}"
            logger.warning("剔除 %s: %s", path.name, s.excluded)
        scans.append(s)
    logger.info("F1: 共发现 %d 个文件, 成功解析 %d 个, %d 个组",
                len(scans), sum(1 for s in scans if not s.excluded),
                len({s.group for s in scans}))
    return scans


# ═══════════════════════════ F2/QC: 波长对齐 · 去尖峰 · 基线 ═══════════════════════════
def qc_wavelength(scans: list, cfg: dict) -> None:
    """波长轴对齐检查: 以多数签名为参考, 超差文件整组剔除并记日志。"""
    tol = float(cfg["wavelength"].get("align_tol", 0.51))
    sigs: dict = {}
    for s in scans:
        if s.excluded or s.wl is None:
            continue
        key = (s.wl.size, round(float(s.wl[0]), 1), round(float(s.wl[-1]), 1))
        sigs.setdefault(key, []).append(s)
    if not sigs:
        return
    ref_key = max(sigs, key=lambda k: len(sigs[k]))
    for key, ss in sigs.items():
        if key != ref_key:
            for s in ss:
                s.excluded = f"波长轴与多数文件不一致({key[1]:.1f}-{key[2]:.1f} nm/{key[0]} 点)"
                logger.warning("剔除 %s: %s", s.path.name, s.excluded)
    ref = sigs[ref_key][0]
    for s in sigs[ref_key][1:]:
        if float(np.max(np.abs(s.wl - ref.wl))) > tol:
            s.excluded = "波长轴与参考文件点对点不一致"
            logger.warning("剔除 %s: %s", s.path.name, s.excluded)


def despike(a: np.ndarray, wl: np.ndarray, qc_cfg: dict, tag: str) -> np.ndarray:
    """单点异常剔除(如突然接近 0): 滚动中位数 ± k×MAD, 逐点记日志。"""
    oc = qc_cfg.get("outlier", {})
    out = a.astype(float).copy()
    if not oc.get("enable", True):
        return out
    half = int(oc.get("window", 7)) // 2
    k_mad, floor = float(oc.get("k_mad", 6.0)), float(oc.get("abs_floor", 0.05))
    for i in range(out.size):
        if np.isnan(out[i]):
            continue
        seg = out[max(0, i - half): i + half + 1]
        seg = seg[~np.isnan(seg)]
        if seg.size < 3:
            continue
        med = float(np.median(seg))
        d = abs(out[i] - med)
        if d <= floor:                       # 陡峭光谱边缘由 abs_floor+MAD 双重保护
            continue
        mad = float(np.median(np.abs(seg - med)))
        if d > k_mad * mad:
            logger.info("弃用异常数据点 %s: λ=%.2f nm, A=%.4f (邻域中位数 %.4f, 偏离 %.4f → NaN)",
                        tag, float(wl[i]), out[i], med, d)
            out[i] = np.nan
    return out


def correct_baseline(wl, a, base_cfg: dict, tag: str):
    """基线扣除(应对忽略自动调零): 长波无吸收窗 median/mean/linear。"""
    if not base_cfg.get("enable", True):
        return a, 0.0
    lo, hi = base_cfg.get("wl_range", [650.0, 700.0])
    m = (wl >= float(lo)) & (wl <= float(hi)) & np.isfinite(a)
    if int(m.sum()) < 3:
        logger.warning("%s: 基线窗口 %.0f-%.0f nm 有效点不足(%d) → 跳过校正",
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
        logger.warning("%s: 基线不为零(%.4f AU, 疑未执行自动调零) → 已扣除 [%s]",
                       tag, base, method)
    else:
        logger.debug("%s: 基线 %.4f AU 已扣除", tag, base)
    return corr, base


# ═══════════════════════════ M1/M2/M3: 背景 · 探针 · 归一化 ═══════════════════════════
def _interp_valid(wl, a, wl_new):
    m = np.isfinite(a)
    if int(m.sum()) < 2:
        return np.zeros_like(wl_new, dtype=float)
    return np.interp(wl_new, wl[m], a[m])


def subtract_background(scans: list, bg_cfg: dict) -> dict:
    """M1: A_corr = A_sample − A_background (matched=时间最近 / mean=平均谱)。"""
    if not bg_cfg.get("enable", True):
        return {}
    bgs = [s for s in scans if s.role == "background" and not s.excluded]
    if not bgs:
        logger.warning("M1: 未找到背景组→ 跳过背景扣除")
        return {}
    mode = bg_cfg.get("mode", "matched")
    mean_wl = mean_a = None
    if mode != "matched":
        mean_wl = bgs[0].wl
        mean_a = np.mean([_interp_valid(b.wl, b.a, mean_wl) for b in bgs], axis=0)
    for s in scans:
        if s.excluded or s.role == "background":
            continue
        if mode == "matched":
            b = min(bgs, key=lambda bb: abs(bb.t - s.t))
            bg_a, src = _interp_valid(b.wl, b.a, s.wl), b.path.name
        else:
            bg_a, src = _interp_valid(mean_wl, mean_a, s.wl), "背景平均谱"
        s.a = s.a - bg_a
        logger.debug("%s: 已扣除背景 %s", s.path.name, src)
    logger.info("M1: 已对 %d 个文件扣除背景(模式=%s)",
                sum(1 for s in scans if not s.excluded and s.role != "background"), mode)
    return {"mode": mode, "n_bg": len(bgs)}


def extract_probe(wl, a, wl_cfg: dict, tag: str) -> float:
    """M2: A(t) = λ_probe±window 内最大值(抗峰漂移); window≤0 → 严格单点。"""
    lp, win = float(wl_cfg.get("probe", 379.0)), float(wl_cfg.get("probe_window", 2.0))
    if win <= 0:
        i = int(np.argmin(np.abs(wl - lp)))
        if not np.isfinite(a[i]):
            logger.warning("%s: λ=%.1f nm 值缺失 → A(t)=NaN", tag, float(wl[i]))
            return float("nan")
        return float(a[i])
    m = (wl >= lp - win) & (wl <= lp + win) & np.isfinite(a)
    if not m.any():
        logger.warning("%s: 探针窗口 %.0f±%.0f nm 无有效数据 → A(t)=NaN", tag, lp, win)
        return float("nan")
    return float(np.max(a[m]))


def normalize_series(t, A, a0_cfg: dict, tag: str, a_floor: float = 0.0):
    """M3: A(0)=暗态点均值; 无暗态→最早 n_early 点均值+WARNING。A<a_floor 掩码防 -inf。"""
    t, A = np.asarray(t, float), np.asarray(A, float)
    dark = np.isfinite(A) & (t <= float(a0_cfg.get("dark_max_time", 0.0)))
    if dark.any():
        A0 = float(np.mean(A[dark]))
        logger.info("%s: A(0)=%.4f (%d 个暗态点均值, t≤%.0f s)", tag, A0,
                    int(dark.sum()), float(a0_cfg.get("dark_max_time", 0.0)))
    else:
        n_e = max(1, int(a0_cfg.get("n_early", 2)))
        idx = [int(i) for i in np.argsort(t) if np.isfinite(A[i])][:n_e]
        A0 = float(np.mean(A[idx])) if idx else float("nan")
        logger.warning("%s: 无暗态点(t≤%.0f s) → A(0)=%.4f 取最早 %d 点均值(t=%s s)",
                       tag, float(a0_cfg.get("dark_max_time", 0.0)), A0, len(idx),
                       "/".join(f"{t[i]:.0f}" for i in idx))
    if not np.isfinite(A0) or A0 == 0:
        logger.error("%s: A(0)=%s 无效, R 全部置 NaN", tag, A0)
        return A0, np.full(A.shape, np.nan)
    R = A / A0
    for i in np.where(np.isfinite(A) & (A < a_floor))[0]:
        logger.info("%s: t=%.0f s A=%.4f < a_floor=%g → 掩码(防 ln→-inf)",
                    tag, t[i], A[i], a_floor)
        R[i] = np.nan
    return A0, R


# ═══════════════════════════ M4: 准一级拟合 + 自动截断 ═══════════════════════════
def _linfit(x, y, tag, label) -> FitResult:
    lr = stats.linregress(x, y)
    logger.info("%s[%s]: k=%.6e ± %.2e, R²=%.5f, n=%d",
                tag, label, -lr.slope, lr.stderr, lr.rvalue ** 2, x.size)
    return FitResult(k=float(-lr.slope), k_err=float(lr.stderr),
                     r2=float(lr.rvalue ** 2), n=int(x.size),
                     intercept=float(lr.intercept), x=np.asarray(x), lnR=np.asarray(y))


def _quad_p(x, resid) -> float:
    """残差对 x 二次项显著性 p 值(系统性弯曲检验)。"""
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
        return float(2.0 * stats.t.sf(abs(tstat), dof))
    except np.linalg.LinAlgError:
        return 1.0


def fit_kinetics(t, R, cfg: dict, tag: str, x=None) -> FitResult:
    """M4: ln R = −k·x (自然对数)。仅拟合 R∈[r_min, r_max+tol]; 残差弯曲→自动截断。"""
    kin = cfg["kinetics"]
    t, R = np.asarray(t, float), np.asarray(R, float)
    x = np.asarray(t if x is None else x, float)
    r_min = float(kin.get("r_min", 0.30))
    r_max = float(kin.get("r_max", 1.0)) + float(kin.get("r_max_tol", 0.05))
    fin = np.isfinite(R) & np.isfinite(x)
    for i in np.where(fin & ((R < r_min) | (R > r_max)))[0]:
        why = (f"R={R[i]:.3f}<{r_min:.2f}(信号过弱)" if R[i] < r_min
               else f"R={R[i]:.3f}>{r_max:.2f}(超出线性段)")
        logger.info("%s: 剔除 t=%.0f s 数据点(%s)", tag, t[i], why)
    m = fin & (R >= r_min) & (R <= r_max)
    xf, y = x[m], np.log(R[m])                     # ← 自然对数, 严禁 log10
    if xf.size < 3 or float(np.ptp(xf)) == 0:
        return FitResult(status="FAILED", message=f"有效点不足或横轴无跨度(n={xf.size})")
    o = np.argsort(xf, kind="stable"); xf, y = xf[o], y[o]
    res = _linfit(xf, y, tag, "全窗口")
    if bool(kin.get("auto_truncate", True)) and res.n >= 6:
        p = _quad_p(xf, y - (res.intercept - res.k * xf))
        if p < float(kin.get("curvature_p", 0.05)):
            nkeep = max(3, int(round(float(kin.get("truncate_frac", 0.70)) * xf.size)))
            res2 = _linfit(xf[:nkeep], y[:nkeep], tag, "截断后")
            logger.warning("%s: 残差系统性弯曲(二次项 p=%.3g < %.2g) → 自动截断, "
                           "保留最早 %d/%d 点(横轴≤%.3g); 全窗口 k=%.4e → 截断后 k=%.4e",
                           tag, p, float(kin.get("curvature_p", 0.05)), nkeep, xf.size,
                           float(xf[nkeep - 1]), res.k, res2.k)
            res2.truncated, res2.pre_k, res2.pre_r2 = True, res.k, res.r2
            res2.message = f"auto_truncate: p={p:.3g}, 保留 {nkeep}/{xf.size} 点"
            res = res2
    if res.k <= 0:
        res.status, res.message = "WARN", f"k={res.k:.3e} ≤ 0(无有效衰减)"
    elif res.r2 < float(cfg["qc"].get("r2_warn", 0.98)):
        res.status, res.message = "WARN", f"R²={res.r2:.4f} < {float(cfg['qc'].get('r2_warn', 0.98)):.2f}"
    return res


# ═══════════════════════════ M5/M7: Φ_Δ 与误差 ═══════════════════════════
def iabs(a_ex: float, mode: str = "exact") -> float:
    """I_abs = 1−10^(−A_ex) (exact) 或 2.303·A_ex (linear, A_ex<0.1 近似)。"""
    return 2.303 * a_ex if mode == "linear" else 1.0 - 10.0 ** (-a_ex)


def calc_phi(k_s, k_r, aex_s, aex_r, phi_ref, mode="exact") -> float:
    """Φ_sample = Φ_ref × (k_sample/k_ref) × (I_abs,ref / I_abs,sample)。"""
    return float(phi_ref * (k_s / k_r) * (iabs(aex_r, mode) / iabs(aex_s, mode)))


def phi_sigma(phi, k_s, k_s_err, k_r, k_r_err, aex_s, aex_r,
              phi_ref, phi_ref_err, a_ex_err, mode="exact") -> float:
    """误差传递: σ_Φ = Φ·√[(σks/ks)²+(σkr/kr)²+(σΦref/Φref)²+Σ(σI/I)²]。"""
    rel = (k_s_err / k_s) ** 2 + (k_r_err / k_r) ** 2 + (phi_ref_err / phi_ref) ** 2
    for ax in (aex_s, aex_r):
        if mode == "linear":
            rel += (a_ex_err / ax) ** 2
        else:
            rel += (np.log(10.0) * 10.0 ** (-ax) * a_ex_err / iabs(ax)) ** 2
    return float(phi * np.sqrt(rel))


def bootstrap_phi(t_s, R_s, t_r, R_r, aex_s, aex_r, phi_ref, cfg, n=None, seed=None):
    """可选稳健性校验: 成对 bootstrap 重采样重拟合 → (Φ均值, 标准差)。"""
    qy, kin = cfg["quantum_yield"], cfg["kinetics"]
    n = int(n or qy.get("bootstrap_n", 1000))
    rng = np.random.default_rng(seed if seed is not None else qy.get("seed", 42))
    mode = qy.get("iabs_mode", "exact")
    r_min = float(kin.get("r_min", 0.30))
    r_max = float(kin.get("r_max", 1.0)) + float(kin.get("r_max_tol", 0.05))

    def prep(t, R):
        t, R = np.asarray(t, float), np.asarray(R, float)
        m = np.isfinite(R) & (R >= r_min) & (R <= r_max)
        return t[m], np.log(R[m])

    ts, ys = prep(t_s, R_s); tr, yr = prep(t_r, R_r)
    if min(ts.size, tr.size) < 3:
        return float("nan"), float("nan")
    out = []
    for _ in range(n):
        i, j = rng.integers(0, ts.size, ts.size), rng.integers(0, tr.size, tr.size)
        try:
            ks = -stats.linregress(ts[i], ys[i]).slope
            kr = -stats.linregress(tr[j], yr[j]).slope
            if ks > 0 and kr > 0:
                out.append(calc_phi(ks, kr, aex_s, aex_r, phi_ref, mode))
        except Exception:
            continue
    if not out:
        return float("nan"), float("nan")
    arr = np.array(out)
    return float(arr.mean()), (float(arr.std(ddof=1)) if arr.size > 1 else 0.0)


# ═══════════════════════════ F6: 特异性对照判定 ═══════════════════════════
def judge_controls(results: dict, cfg: dict) -> dict:
    if not cfg.get("controls", {}).get("enable", True):
        return {}
    ks = {g: e["pooled"].k for g, e in results.items()
          if e.get("pooled") and e["pooled"].status != "FAILED"}
    s_ks = [k for g, k in ks.items() if results[g]["role"] == "sample"]
    if not s_ks:
        return {}
    k_s = float(np.mean(s_ks))
    out = {}
    for g, e in sorted(results.items()):
        role = e["role"]
        if role not in ("dark", "n2", "quencher") or g not in ks:
            continue
        k = ks[g]
        if role == "dark":
            thr = float(cfg["qc"].get("dark_k_rel_max", 0.05)) * abs(k_s)
            out[g] = ("通过" if abs(k) <= thr else "存疑",
                      f"|k_dark|={abs(k):.2e}(阈值 {thr:.2e})")
        else:
            inh = 1.0 - k / k_s
            thr = float(cfg["controls"].get(
                "n2_inhibition_min" if role == "n2" else "quencher_inhibition_min", 0.70))
            out[g] = ("通过" if inh >= thr else ("存疑" if inh >= 0.5 * thr else "失败"),
                      f"抑制率 {inh:.0%}(阈值 {thr:.0%})")
        logger.info("对照判定 %s: %s(%s)", g, out[g][0], out[g][1])
    return out


# ═══════════════════════════ 流水线: run_core / _fit_all / compute_phi ═══════════════════════════
def run_core(data_dir, cfg: dict, dry: bool = False):
    """端到端: 解析→QC→去尖峰→基线→(dry 则止)→背景→拟合。返回 (scans, results)。"""
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        logger.error("数据目录不存在: %s", data_dir)
        return [], {}
    scans = collect_scans(data_dir, cfg)
    if not scans:
        logger.warning("目录 %s 未找到光谱文件(扩展名 %s)", data_dir,
                       cfg["io"].get("extensions"))
        return scans, {}
    if all(s.excluded for s in scans):
        logger.error("所有文件解析失败, 无有效数据")
        return scans, {}
    qc_wavelength(scans, cfg)
    sat = float(cfg["qc"].get("saturation", 3.0))
    for s in scans:
        if s.excluded:
            continue
        if np.isfinite(s.a).any() and float(np.nanmax(s.a)) > sat:
            s.excluded = f"吸光度饱和(max={float(np.nanmax(s.a)):.2f}>{sat:g})"
            logger.warning("剔除 %s: %s", s.path.name, s.excluded)
            continue
        s.a = despike(s.a, s.wl, cfg["qc"], s.path.name)
        s.a, _ = correct_baseline(s.wl, s.a, cfg["baseline"], s.path.name)
        n_neg = int((s.a < 0).sum())
        if n_neg and cfg["qc"].get("flag_negative", True):
            logger.info("%s: %d 个负吸光度点(基线校正后残留, 标记不剔除, 最小 %.4f)",
                        s.path.name, n_neg, float(np.nanmin(s.a)))
    if dry:
        return scans, {}
    subtract_background(scans, cfg["background"])
    return scans, _fit_all(scans, cfg)


def _fit_all(scans: list, cfg: dict) -> dict:
    """汇总时间序列 → dark 漂移校正 → 逐重复拟合 + 逐组合并拟合。"""
    wl_cfg, kin, qy = cfg["wavelength"], cfg["kinetics"], cfg["quantum_yield"]
    groups: dict = {}
    for s in scans:
        if s.excluded or s.role == "background":
            continue
        e = groups.get(s.group)
        if e is None:
            e = groups[s.group] = {"role": s.role, "reps": {}, "first": s}
        if s.t < e["first"].t:
            e["first"] = s
        r = e["reps"].setdefault(s.rep, {"t": [], "A": []})
        r["t"].append(float(s.t))
        r["A"].append(extract_probe(s.wl, s.a, wl_cfg, s.path.name))
    for g, e in groups.items():
        for rep, r in e["reps"].items():
            r["t"], r["A"] = np.asarray(r["t"], float), np.asarray(r["A"], float)
            o = np.argsort(r["t"], kind="stable")
            r["t"], r["A"] = r["t"][o], r["A"][o]
            dup = r["t"][:-1][np.diff(r["t"]) == 0]
            if dup.size:
                logger.warning("组 %s(重复 %s): 存在重复时间戳 %s", g, rep, dup.tolist())
    for e in [e for e in groups.values() if e["role"] == "dark"]:   # F3 漂移校正
        ts = np.concatenate([r["t"] for r in e["reps"].values()])
        A = np.concatenate([r["A"] for r in e["reps"].values()])
        m = np.isfinite(A)
        if int(m.sum()) >= 4 and float(np.ptp(ts[m])) > 0:
            lr = stats.linregress(ts[m], A[m])
            if lr.pvalue < 0.05 and lr.slope != 0:
                logger.warning("F3: dark 组漂移斜率 %.3e AU/s(p=%.3g) → 各样品组线性校正",
                               lr.slope, lr.pvalue)
                for e2 in groups.values():
                    if e2["role"] in ("dark", "background"):
                        continue
                    for r in e2["reps"].values():
                        r["A"] = r["A"] - lr.slope * (r["t"] - r["t"][0])
    lo, hi = cfg["qc"].get("a0_range", [0.05, 1.5])
    floor = float(kin.get("a_floor", 0.01))
    dose = bool(kin.get("dose_normalize", False))
    results = {}
    for g, e in sorted(groups.items()):
        irr = kin.get("irradiance", {}) or {}
        I_g = float(irr.get(g, irr.get("default", 1.0)))
        entry = {"role": e["role"], "reps": {}, "irradiance": I_g,
                 "a_ex_auto": float("nan")}
        if g not in (qy.get("a_ex") or {}):        # A_ex 备用: 最早光谱 λ_ex 处
            s0 = e["first"]
            iex = int(np.argmin(np.abs(s0.wl - float(wl_cfg.get("excitation", 532.0)))))
            entry["a_ex_auto"] = _fnum(s0.a[iex])
        for rep, r in e["reps"].items():
            tag = f"{g}_{rep}" if rep else g
            A0, R = normalize_series(r["t"], r["A"], cfg["a0"], tag, a_floor=floor)
            r.update({"R": R, "A0": A0})
            if not (np.isfinite(A0) and lo <= A0 <= hi):
                msg = f"A(0)={A0:.3f} 超出合理区间 [{lo:g},{hi:g}]"
                logger.warning("%s: %s → FAILED", tag, msg)
                r["fit"] = FitResult(status="FAILED", message=msg)
                continue
            x = r["t"] * I_g / 1000.0 if dose else r["t"]      # M6 剂量横轴
            r["fit"] = fit_kinetics(r["t"], R, cfg, tag, x=x)
        ok = {rep: r for rep, r in e["reps"].items() if r["fit"].status != "FAILED"}
        if not ok:
            entry["pooled"] = FitResult(status="FAILED", message="所有重复均拟合失败")
        elif len(ok) == 1:
            entry["pooled"] = next(iter(ok.values()))["fit"]
        else:
            tp = np.concatenate([r["t"] for r in ok.values()])
            Rp = np.concatenate([r["R"] for r in ok.values()])
            entry["pooled"] = fit_kinetics(tp, Rp, cfg, g,
                                           x=tp * I_g / 1000.0 if dose else tp)
        entry["A0_pooled"] = (float(np.mean([r["A0"] for r in e["reps"].values()]))
                              if e["reps"] else float("nan"))
        p = entry["pooled"]
        entry["k_per_I"] = _fnum(p.k / I_g) if (p.status != "FAILED" and I_g) else float("nan")
        if p.status != "FAILED" and I_g != 1.0:
            logger.info("%s: 辐照度归一 k/I=%.4e (I=%.3f mW/cm²)", g, p.k / I_g, I_g)

        entry["reps"] = e["reps"] 
        results[g] = entry
    return results


def compute_phi(results: dict, cfg: dict) -> dict:
    """F5: sample↔ref 配对计算 Φ_Δ(多参比均值); 无参比 → NO_REF 不阻塞。"""
    qy = cfg["quantum_yield"]
    mode = qy.get("iabs_mode", "exact")
    aex_cfg = dict(qy.get("a_ex") or {})
    refs = [g for g, e in results.items() if e["role"] == "reference"
            and e.get("pooled") is not None and e["pooled"].status != "FAILED"]
    warned = set()

    def _aex(g, e):
        if g in aex_cfg:
            return float(aex_cfg[g])
        v = _fnum(e.get("a_ex_auto"))
        if np.isfinite(v) and g not in warned:
            warned.add(g)
            logger.warning("组 %s: A_ex=%.4f 取自最早光谱 λ_ex 处(config 未声明, "
                           "可能含探针贡献)", g, v)
        return v

    out = {}
    for g, e in sorted(results.items()):
        if e["role"] != "sample":
            continue
        p = e.get("pooled")
        if p is None or p.status == "FAILED":
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "FAILED", "pairs": []}
            continue
        if not refs:
            logger.info("组 %s: 无参比组 → Φ 输出 N/A (NO_REF)", g)
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "NO_REF", "pairs": []}
            continue
        aex_s, pairs = _aex(g, e), []
        for rg in refs:
            rp, aex_r = results[rg]["pooled"], _aex(rg, results[rg])
            if not (np.isfinite(aex_s) and np.isfinite(aex_r)):
                logger.warning("配对 %s↔%s 缺少有效 A_ex, 跳过", g, rg)
                continue
            if p.k <= 0 or rp.k <= 0:
                logger.warning("配对 %s↔%s k≤0, 跳过", g, rg)
                continue
            phi = calc_phi(p.k, rp.k, aex_s, aex_r, qy["phi_ref"], mode)
            sig = phi_sigma(phi, p.k, p.k_err, rp.k, rp.k_err, aex_s, aex_r,
                            qy["phi_ref"], qy["phi_ref_err"],
                            qy.get("a_ex_err", 0.005), mode)
            pairs.append({"ref": rg, "phi": phi, "phi_err": sig})
            logger.info("Φ_Δ(%s | 参比 %s) = %.4f ± %.4f", g, rg, phi, sig)
        if not pairs:
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "FAILED", "pairs": []}
            continue
        phis = np.array([q["phi"] for q in pairs])
        errs = np.array([q["phi_err"] for q in pairs])
        entry = {"phi": float(phis.mean()),
                 "phi_err": float(np.sqrt((errs ** 2).mean() + phis.var())),
                 "status": "OK", "pairs": pairs}
        if qy.get("bootstrap", False):
            rg = refs[0]
            t_s = np.concatenate([v["t"] for v in e["reps"].values()])
            R_s = np.concatenate([v["R"] for v in e["reps"].values()])
            t_r = np.concatenate([v["t"] for v in results[rg]["reps"].values()])
            R_r = np.concatenate([v["R"] for v in results[rg]["reps"].values()])
            bm, bs = bootstrap_phi(t_s, R_s, t_r, R_r, aex_s, _aex(rg, results[rg]),
                                   qy["phi_ref"], cfg)
            entry["bootstrap"] = {"mean": bm, "std": bs}
            logger.info("Φ_Δ bootstrap(%s) = %.4f ± %.4f", g, bm, bs)
        out[g] = entry
    return out


# ═══════════════════════════ synthetic 数据生成器 ═══════════════════════════
SYN = {"k_true": 5.0e-3, "phi_true": 0.60, "phi_ref": 0.75, "a_ex": 0.15,
       "k_ref": 5.0e-3 * 0.75 / 0.60, "lambda_ex": 532.0}   # k_ref=6.25e-3 → Φ_true 精确成立
SYN_TIMES = [0, 1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 120, 150, 180, 210, 240, 300, 360, 480, 600]


def make_synthetic(out_dir: Path, seed: int = 42) -> dict:
    """生成 synthetic 自检数据集(T1-T4 场景 + 缺陷注入)并写 _truth.json。"""
    rng = np.random.default_rng(seed)
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    wl = np.arange(300.0, 701.0)
    g = lambda mu, sig: np.exp(-0.5 * ((wl - mu) / sig) ** 2)
    probe = 0.634 * g(359.5, 8) + 1.0 * g(379, 7) + 0.941 * g(400, 8)   # ABDA 型三重峰
    sens = 0.5 * g(571, 45) + 0.16 * np.exp(-(wl - 300) / 150) + 0.004  # 荧光团型光敏剂
    sens = sens * (SYN["a_ex"] / sens[int(np.argmin(np.abs(wl - SYN["lambda_ex"])))])
    groups = {"sample_1": (SYN["k_true"], 1.0), "sample_2": (SYN["k_true"], 0.95),
              "ref_1": (SYN["k_ref"], 1.0), "bg_1": (None, 0.0),
              "dark_1": (1e-6, 1.0), "n2_1": (0.1 * SYN["k_true"], 1.0),
              "nan3_1": (0.05 * SYN["k_true"], 1.0), "nonlin_1": ("biexp", 1.0),
              "mis_1": (SYN["k_true"], 1.0)}
    defects = {("sample_1", 150): ("spike", 379.0), ("sample_2", 200): ("missing", 300.0)}
    for grp, (k, amp) in groups.items():
        for t in SYN_TIMES:
            sens_t = sens * np.exp(-1e-4 * t)
            if k is None:
                a = sens_t.copy()
            elif k == "biexp":
                a = amp * probe * (0.25 * np.exp(-0.02 * t) + 0.75 * np.exp(-0.002 * t)) + sens_t
            else:
                a = amp * probe * np.exp(-k * t) + sens_t
            a = a + rng.normal(0, 1, a.shape) * (0.02 * np.abs(a) + 0.001)   # 2% 噪声
            wlx = wl + 5.0 if grp == "mis_1" else wl                          # T4 波长错位
            kind, dwl = defects.get((grp, t), (None, 0.0))
            lines = [f'"{grp}-{t} - RawData"', '"波长","吸光值"']
            for w, v in zip(wlx, a):
                if kind == "missing" and abs(w - dwl) < 0.5:
                    lines.append(f"{w:.2f}, ")
                elif kind == "spike" and abs(w - dwl) < 0.5:
                    lines.append(f"{w:.2f},0.001")
                else:
                    lines.append(f"{w:.2f},{v:.3f}")
            (out_dir / f"{grp}-{t}.txt").write_text("\n".join(lines) + "\n", encoding="gbk")
    (out_dir / "broken.csv").write_text("这不是光谱数据\n\"垃圾\",\"表头\"\n1,2,3,4\n,,\n",
                                        encoding="utf-8")
    truth = dict(SYN); truth["seed"] = seed
    (out_dir / "_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    return truth


# ═══════════════════════════ 绘图 ═══════════════════════════
def _setup_mpl(cfg: dict):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import font_manager
    import matplotlib.pyplot as plt
    for name in ["Microsoft YaHei", "SimHei", "PingFang SC", "Noto Sans CJK SC",
                 "Source Han Sans SC", "WenQuanYi Micro Hei", "Arial Unicode MS"]:
        if name in {f.name for f in font_manager.fontManager.ttflist}:
            matplotlib.rcParams["font.sans-serif"] = [name]
            _ZH[0] = True
            break
    else:
        _ZH[0] = False
        logger.warning("未找到中文字体 → 图表标签改用英文(安装 微软雅黑/SimHei 可恢复中文)")
    matplotlib.rcParams["axes.unicode_minus"] = False
    return plt


def _cmap(plt, name: str):
    import matplotlib
    try:
        return matplotlib.colormaps[name]
    except Exception:
        return plt.get_cmap(name)


def _save(fig, figdir: Path, name: str, cfg: dict):
    for ext in cfg["plot"].get("formats", ["svg", "png"]):
        fig.savefig(figdir / f"{name}.{ext}", dpi=int(cfg["plot"].get("dpi", 300)),
                    bbox_inches="tight")
    fig.clf()
    logger.info("已保存图: figures/%s.(svg|png)", name)


def _xvals(e: dict, cfg: dict) -> np.ndarray:
    """该组全部时间点对应的拟合横轴(t/s 或 剂量 J/cm²)。"""
    t = np.concatenate([v["t"] for v in e["reps"].values()])
    if bool(cfg["kinetics"].get("dose_normalize", False)):
        return t * float(e.get("irradiance", 1.0)) / 1000.0
    return t


def _fig_a_spectra(plt, scans, cfg, figdir):
    ov = cfg.get("plot", {}).get("spectra_overlay", {})
    by_group: dict = {}
    for s in scans:
        if not s.excluded and s.role != "background":
            by_group.setdefault(s.group, []).append(s)
    if not by_group:
        logger.info("图a: 无可用光谱, 跳过")
        return
    wl_cfg = cfg["wavelength"]
    single = len(by_group) == 1
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
        sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap); sm.set_array([])
        fig.colorbar(sm, ax=ax).set_label(T("辐照时间 t (s)", "Irradiation time (s)"))
        ymax = max(float(np.nanmax(s.a)) for s in ss2)
        if ov.get("mark_probe", True):                       # ★ ABDA 吸收峰
            lp, win = float(wl_cfg.get("probe", 379.0)), float(wl_cfg.get("probe_window", 2.0))
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
        ax.set_title((g + " — " if not single else "") +
                     T("紫外-可见吸收光谱随辐照时间的变化(基线校正后)",
                       "UV-vis spectra vs irradiation time"))
        _save(fig, figdir, "fig_a_spectra_overlay" if single else f"fig_a_spectra_{_safe(g)}", cfg)


def _fig_b_fit(plt, g, e, cfg, figdir):
    f = e.get("pooled")
    if f is None or f.status == "FAILED" or f.x is None or len(f.x) == 0:
        return
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
        xbar, sxx = float(xx.mean()), float(np.sum((xx - xbar) ** 2))
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
    ax.set_xlabel(T("累计剂量 (J/cm²)", "Dose (J/cm²)") if dose else T("辐照时间 t (s)", "Time (s)"))
    ax.set_ylabel(T("ln( A(t)/A₀ )", "ln(A/A0)"))
    ax.set_title(f"{g} — " + T("准一级动力学拟合", "pseudo-first-order kinetics"))
    ax.text(0.03, 0.05, f"k = {f.k:.4e} ± {f.k_err:.1e} {unit}\nR² = {f.r2:.4f}, n = {f.n}"
             + (T("（已自动截断）", " (truncated)") if f.truncated else ""),
             transform=ax.transAxes, fontsize=9, va="bottom",
             bbox=dict(facecolor="white", edgecolor="0.7", alpha=0.85))
    ax.legend(loc="upper right", fontsize=9)
    _save(fig, figdir, f"fig_b_kinetics_{_safe(g)}", cfg)


def _fig_c_bars(plt, results, cfg, figdir):
    items = [(g, e["pooled"]) for g, e in sorted(results.items()) if e.get("pooled")]
    if not items:
        return
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
    _save(fig, figdir, "fig_c_k_obs_bars", cfg)


def _fig_d_phi(plt, phi, cfg, figdir):
    items = [(g, p) for g, p in sorted((phi or {}).items())
             if p.get("status") == "OK" and _fnum(p.get("phi"), None) is not None]
    if not items:
        logger.info("图d: 无有效 Φ_Δ(缺参比组或拟合失败) → 跳过")
        return
    fig, ax = plt.subplots(figsize=(6.5, max(2.5, 0.9 * len(items) + 1.5)))
    ax.errorbar([p["phi"] for _, p in items], np.arange(len(items)),
                xerr=[_fnum(p.get("phi_err"), 0.0) for _, p in items],
                fmt="o", color="crimson", capsize=3, lw=1.5)
    ax.set_yticks(np.arange(len(items)))
    ax.set_yticklabels([g for g, _ in items])
    phi_ref = float(cfg["quantum_yield"].get("phi_ref", 0.75))
    ax.axvline(phi_ref, ls="--", color="gray", lw=1)
    ax.text(phi_ref, len(items) - 0.4, f" Φ_ref={phi_ref:g}", color="gray", fontsize=9)
    ax.set_xlabel(T("相对量子产率 Φ_Δ", "Relative quantum yield Φ_Δ"))
    ax.set_title(T("Φ_Δ 森林图(误差含 k、A_ex、Φ_ref 传递)", "Φ_Δ forest plot"))
    _save(fig, figdir, "fig_d_phi_forest", cfg)


def _fig_e_residuals(plt, results, cfg, figdir):
    fits = [(g, e["pooled"]) for g, e in sorted(results.items())
            if e.get("pooled") and e["pooled"].status != "FAILED"
            and e["pooled"].x is not None and len(e["pooled"].x)]
    if not fits:
        return
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
                        "o", mfc="none", mec="crimson", label=T("截断弃用点", "truncated pts"))
        ax.set_title(g, fontsize=10)
        ax.set_ylabel(T("残差", "resid"))
        ax.legend(fontsize=8)
    axes.ravel()[-1].set_xlabel(T("累计剂量 (J/cm²)", "Dose (J/cm²)") if dose
                                else T("辐照时间 t (s)", "Time (s)"))
    fig.tight_layout()
    _save(fig, figdir, "fig_e_residuals", cfg)

# ══════════════ JACS 风格 + 光谱读取工具 (放在 def plot_all 上方) ══════════════
JACS_RC = {
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

def apply_jacs_style():
    """全局切换 JACS 风格: 细线/无上右边框/朝外刻度/Arial/600dpi。"""
    import matplotlib as mpl
    mpl.rcParams.update(JACS_RC)

import re as _re
_NUM_RE = _re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")

def _read_spectrum_file(path):
    """兜底读取: 从原始文件取 (波长, 吸光度) 两列。"""
    txt = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            txt = Path(path).read_text(encoding=enc)
            break
        except Exception:
            continue
    if txt is None:
        return None
    wl, ab = [], []
    for line in txt.splitlines():
        toks = _NUM_RE.findall(line.replace(",", " ").replace(";", " "))
        if len(toks) >= 2:
            x, y = float(toks[0]), float(toks[1])
            if 150 <= x <= 1200:
                wl.append(x); ab.append(y)
    if len(wl) >= 10:
        i = np.argsort(np.asarray(wl))
        return np.asarray(wl)[i], np.asarray(ab)[i]
    return None

def _spectrum_arrays(scan):
    """取 scan 的 (wl, abs); 优先已解析属性, 兜底回读原始文件。"""
    for wa, aa in (("wl", "abs"), ("wavelength", "abs"), ("x", "y"),
                   ("wl", "a"), ("wavelength", "absorbance")):
        w, a = getattr(scan, wa, None), getattr(scan, aa, None)
        if w is None or a is None:
            continue
        try:
            w, a = np.asarray(w, float), np.asarray(a, float)
            if w.size >= 10 and w.size == a.size:
                return w, a
        except Exception:
            pass
    return _read_spectrum_file(scan.path)

def probe_absorbance(wl, ab, lam, win):
    """lam±win 窗口内平均吸光度; 窗口空则取最近点。"""
    m = (wl >= lam - win) & (wl <= lam + win)
    if m.sum() >= 1:
        return float(np.mean(ab[m]))
    return float(ab[int(np.argmin(np.abs(wl - lam)))])

def jacs_stack_figure(entries, lam, figdir, tag):
    """entries: [(t, wl, ab)] 按 t 排序 → JACS 风格堆叠图 (png+pdf)。"""
    import matplotlib.pyplot as plt
    from matplotlib.cm import ScalarMappable
    from matplotlib.colors import Normalize
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
    sm = ScalarMappable(norm=norm, cmap=cmap); sm.set_array([])
    cb = fig.colorbar(sm, ax=ax, pad=0.02)
    cb.set_label("Time (s)", fontsize=7)
    cb.ax.tick_params(labelsize=6, width=0.6, length=2.5)
    cb.outline.set_linewidth(0.6)
    fig.tight_layout(pad=0.4)
    figdir.mkdir(parents=True, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(figdir / f"jacs_stack_{tag}.{ext}")
    plt.close(fig)

def jacs_stack_all(scans, cfg, run_dir):
    """每个组一张 JACS 堆叠图 → run_dir/figures/ (plot_all 末尾调用)。"""
    lam = cfg["wavelength"]["probe"]
    by_g = {}
    for s in scans:
        if getattr(s, "excluded", False) or s.role in ("background", "dark"):
            continue
        by_g.setdefault(s.group, []).append(s)
    figdir = Path(run_dir) / "figures"
    made = 0
    for g in sorted(by_g):
        arrs = []
        for s in by_g[g]:
            a = _spectrum_arrays(s)
            if a is not None:
                arrs.append((float(s.t), a[0], a[1]))
        if len(arrs) < 2:
            continue
        arrs.sort(key=lambda e: e[0])
        jacs_stack_figure(arrs, lam, figdir,
                           _re.sub(r'[\\/:*?"<>|\s]+', "_", g)[:30] or "group")
        made += 1
    if made:
        logger.info("JACS 堆叠图 x%d -> %s", made, figdir)
def plot_all(scans, results, phi, controls, cfg, run_dir: Path):
    apply_jacs_style()
    try:
        plt = _setup_mpl(cfg)
    except ImportError:
        logger.error("未安装 matplotlib → 跳过绘图(pip install matplotlib)")
        return
    figdir = run_dir / "figures"
    figdir.mkdir(exist_ok=True)
    _fig_a_spectra(plt, scans, cfg, figdir)
    for g, e in results.items():
        _fig_b_fit(plt, g, e, cfg, figdir)
    _fig_c_bars(plt, results, cfg, figdir)
    _fig_d_phi(plt, phi, cfg, figdir)
    _fig_e_residuals(plt, results, cfg, figdir)
    jacs_stack_all(scans, cfg, run_dir) 


# ═══════════════════════════ 输出: csv / json / report ═══════════════════════════
def write_results_csv(path, results, phi, controls):
    head = ["组别", "角色", "k_obs(s^-1)", "k_stderr", "R2", "n", "自动截断", "A0",
            "A_ex(自动)", "辐照度(mW/cm^2)", "Phi_Delta", "Phi_err", "状态", "对照判定", "备注"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        for g in sorted(results):
            e = results[g]; fit = e.get("pooled"); p = phi.get(g, {})
            jd = controls.get(g, ("", ""))
            note = ((fit.message if fit else "") +
                    (f" | {jd[1]}" if jd and jd[1] else "")).strip(" |")
            w.writerow([g, e.get("role", ""), _fmt(fit.k if fit else np.nan),
                        _fmt(fit.k_err if fit else np.nan),
                        _fmt(fit.r2 if fit else np.nan, ".5f"),
                        (fit.n if fit else 0), "是" if (fit and fit.truncated) else "否",
                        _fmt(e.get("A0_pooled", np.nan), ".4f"),
                        _fmt(e.get("a_ex_auto", np.nan), ".4f"),
                        _fmt(e.get("irradiance", 1.0), ".4f"),
                        _fmt(p.get("phi", np.nan), ".4f"),
                        _fmt(p.get("phi_err", np.nan), ".4f"),
                        (fit.status if fit else "FAILED"),
                        (jd[0] if jd else ""), note])
    logger.info("已写出 %s", path)


def write_fit_summary(path, scans, results, phi, controls, cfg):
    j = {"version": VERSION, "git_hash": git_hash(),
         "generated_at": datetime.now().isoformat(timespec="seconds"),
         "config": cfg,
         "files": [{"file": s.path.name, "group": s.group, "rep": s.rep,
                    "role": s.role, "t_s": s.t, "excluded": (s.excluded or None)}
                   for s in scans],
         "groups": {}, "phi": phi, "controls": controls}
    for g, e in results.items():
        f = e.get("pooled")
        gj = {"role": e.get("role"), "irradiance": e.get("irradiance"),
              "A0_pooled": e.get("A0_pooled"), "a_ex_auto": e.get("a_ex_auto"),
              "reps": {r: {"t": v["t"], "A": v["A"], "R": v["R"], "A0": v["A0"]}
                       for r, v in e["reps"].items()},
              "fit": (None if f is None else
                      {"k": f.k, "k_err": f.k_err, "r2": f.r2, "n": f.n,
                       "intercept": f.intercept, "truncated": f.truncated,
                       "pre_k": f.pre_k, "pre_r2": f.pre_r2, "status": f.status,
                       "message": f.message, "x": f.x, "lnR": f.lnR})}
        sp = [s for s in scans if not s.excluded and s.role != "background"
              and s.group == g]
        if sp:
            gj["spectra"] = {"wl": sp[0].wl,
                             "scans": [{"t": s.t, "a": s.a}
                                       for s in sorted(sp, key=lambda s: s.t)]}
        j["groups"][g] = gj
    Path(path).write_text(json.dumps(_jsonable(j), ensure_ascii=False, indent=2),
                          encoding="utf-8")
    logger.info("已写出 %s", path)


def write_report(path, data_dir, scans, results, phi, controls, cfg, warnings):
    wl_cfg, kin, qy = cfg["wavelength"], cfg["kinetics"], cfg["quantum_yield"]
    n_ok = sum(1 for s in scans if not s.excluded)
    has_dark = any((not s.excluded) and s.role == "sample"
                   and s.t <= float(cfg["a0"].get("dark_max_time", 0.0)) for s in scans)
    a0_txt = (f"t ≤ {float(cfg['a0'].get('dark_max_time', 0.0)):.0f} s 暗态点均值" if has_dark
              else f"最早 {cfg['a0'].get('n_early', 2)} 个时间点均值（**本批无暗态数据**）")
    L = ["# 单线态氧（¹O₂）相对量子产率 Φ_Δ 计算报告", "",
         f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
         f"- 程序：singlet_o2_phi v{VERSION}（git: {git_hash()}）  ",
         f"- 数据目录：`{data_dir}`（{n_ok}/{len(scans)} 个文件有效）", "",
         "## 1. 数据概览", "", "| 组别 | 角色 | 时间点数 | 时间范围 |", "|---|---|---|---|"]
    for g in sorted(results):
        e = results[g]
        if e["reps"]:
            ts = np.concatenate([r["t"] for r in e["reps"].values()])
            L.append(f"| {g} | {e['role']} | {sum(len(r['t']) for r in e['reps'].values())} "
                     f"| {np.nanmin(ts):.0f}–{np.nanmax(ts):.0f} s |")
    excluded = [s for s in scans if s.excluded]
    if excluded:
        L += ["", "**被剔除的文件：**", "", "| 文件 | 原因 |", "|---|---|"]
        L += [f"| `{s.path.name}` | {s.excluded} |" for s in excluded]
    L += ["", "## 2. 方法学", "",
          "1. **背景扣除**：A_corr(λ,t) = A_sample(λ,t) − A_background(λ,t)。本次运行："
          + ("已按时间匹配扣除。" if any(s.role == "background" and not s.excluded for s in scans)
             else "**未提供背景组，已跳过**（日志有警告）。"),
          f"2. **基线校正**：每条光谱在 {cfg['baseline'].get('wl_range')[0]:.0f}–"
          f"{cfg['baseline'].get('wl_range')[1]:.0f} nm 无吸收窗取 "
          f"{cfg['baseline'].get('method')} 并整条扣除（应对未自动调零）。",
          f"3. **探针信号**：A(t) = A_corr 在 λ_probe = {wl_cfg.get('probe', 379):.0f} nm ± "
          f"{wl_cfg.get('probe_window', 2):.0f} nm 窗口内最大值（ABDA 特征峰，抗峰位漂移）。",
          f"4. **归一化**：R(t) = A(t)/A(0)，A(0) = {a0_txt}。",
          f"5. **动力学拟合**：ln R(t) = −k_obs·t（自然对数），仅拟合 R ∈ "
          f"[{kin.get('r_min'):.2f}, {kin.get('r_max'):.2f}]，scipy.stats.linregress；"
          f"残差系统性弯曲时自动截断至前 {int(round(float(kin.get('truncate_frac', 0.7)) * 100))}% 点。",
          f"6. **量子产率**：Φ = Φ_ref × (k_sample/k_ref) × (I_abs,ref/I_abs,sample)，"
          f"I_abs = 1 − 10^(−A_ex)（iabs_mode={qy.get('iabs_mode')}），λ_ex = "
          f"{wl_cfg.get('excitation', 532):.0f} nm，Φ_ref = {qy.get('phi_ref')}。",
          f"7. **误差传递**：σ_Φ 由 k 标准误、A_ex 不确定度（±{qy.get('a_ex_err')}）与 "
          f"Φ_ref 文献不确定度（±{100 * qy.get('phi_ref_err', 0) / qy.get('phi_ref', 1):.0f}%）合成。",
          "", "## 3. 结果", "",
          "| 组别 | k_obs (s⁻¹) | R² | n | 截断 | A(0) | Φ_Δ | 状态 |",
          "|---|---|---|---|---|---|---|---|"]
    for g in sorted(results):
        e = results[g]; f = e.get("pooled"); p = phi.get(g, {})
        if f and f.status != "FAILED":
            phi_txt = (f"{p['phi']:.4f} ± {p['phi_err']:.4f}" if p.get("status") == "OK"
                       else "N/A（无参比组）" if p.get("status") == "NO_REF" else "N/A")
            L.append(f"| {g} | {f.k:.4e} ± {f.k_err:.1e} | {f.r2:.4f} | {f.n} | "
                     f"{'是' if f.truncated else '否'} | {e.get('A0_pooled', float('nan')):.4f} "
                     f"| {phi_txt} | {f.status} |")
        else:
            L.append(f"| {g} | FAILED | - | - | - | - | - | FAILED |")
    if controls:
        L += ["", "## 4. 特异性对照判定", "", "| 组别 | 判定 | 说明 |", "|---|---|---|"]
        L += [f"| {g} | {jd[0]} | {jd[1]} |" for g, jd in sorted(controls.items())]
    L += ["", "## 5. 图", "", "| 图 | 文件 | 内容 |", "|---|---|---|",
          f"| a | figures/fig_a_spectra_overlay.png | 光谱叠加（按辐照时间着色；标注 ABDA "
          f"{wl_cfg.get('probe', 379):.0f} nm、激发 {wl_cfg.get('excitation', 532):.0f} nm、"
          "荧光团最大吸收） |",
          "| b | figures/fig_b_kinetics_*.png | ln(A/A₀)–t 拟合（95% 置信带、截断区间） |",
          "| c | figures/fig_c_k_obs_bars.png | 各组 k_obs 柱状图（误差棒） |",
          "| d | figures/fig_d_phi_forest.png | Φ_Δ 森林图（含 Φ_ref 参考线） |",
          "| e | figures/fig_e_residuals.png | 拟合残差（判断偏离一级动力学） |",
          "", "## 6. 警告汇总", ""]
    L += ([f"- {w}" for w in warnings] if warnings else ["无警告。"])
    L += ["", "## 7. 备注与建议", ""]
    if any(p.get("status") == "NO_REF" for p in phi.values()):
        L.append("- 本次未找到 `ref` 参比组，Φ_Δ 为 N/A。补充参比组数据并在 config.yaml 指定 "
                 "`quantum_yield.phi_ref` 后重跑即可。")
    if not any(s.role == "background" and not s.excluded for s in scans):
        L.append("- 未提供 `bg` 背景组（只加光敏剂不加探针），本次未做背景扣除；"
                 "若光敏剂在 379 nm 附近有明显吸收，建议补测。")
    if any(e.get("pooled") and e["pooled"].truncated for e in results.values()):
        L.append("- 有组触发自动截断（残差系统性弯曲）：全窗口 k 与截断后 k 均记录于 "
                 "fit_summary.json 与 run.log，请结合残差图 e 判断偏离来源"
                 "（光产物增吸 / 内滤效应 / 探针耗尽等）。")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    logger.info("已写出 %s", path)


# ═══════════════════════════ 控制台速览 / dry-run ═══════════════════════════
def print_console_summary(data_dir, scans, results, phi, controls, run_dir, cfg):
    print("\n" + "=" * 64)
    print("  分析完成 — 关键结果速览")
    print("=" * 64)
    print(f"  数据目录   : {data_dir}  ({sum(1 for s in scans if not s.excluded)}"
          f"/{len(scans)} 个文件有效)")
    for g in sorted(results):
        e = results[g]; f = e.get("pooled")
        if e["reps"]:
            ts = np.concatenate([r["t"] for r in e["reps"].values()])
            print(f"  -- 组 [{g}] ({e.get('role')}, {len(e['reps'])} 条序列, "
                  f"t={np.nanmin(ts):.0f}-{np.nanmax(ts):.0f} s)")
        if f is None or f.status == "FAILED":
            print(f"     拟合失败 : {f.message if f else '无有效数据'}")
            continue
        print(f"     A(0)     : {e.get('A0_pooled', float('nan')):.4f} AU")
        print(f"     k_obs    : {f.k:.4e} ± {f.k_err:.1e} s^-1   (R^2={f.r2:.4f}, n={f.n})")
        if f.truncated:
            print(f"     [!] 已自动截断: 全窗口 k={f.pre_k:.4e} -> 截断后 k={f.k:.4e} "
                  f"(保留前 {int(round(float(cfg['kinetics'].get('truncate_frac', 0.7)) * 100))}%)")
        if f.k > 0:
            print(f"     半衰期   : {np.log(2) / f.k:.0f} s")
        p = phi.get(g, {})
        if p.get("status") == "OK":
            print(f"     Phi_D    : {p['phi']:.4f} ± {p['phi_err']:.4f}")
        elif p.get("status") == "NO_REF":
            print("     Phi_D    : N/A (未找到 ref 参比组 -> 补充 ref 组并在 config 指定 "
                  "phi_ref 后可计算)")
        elif p.get("status") == "FAILED":
            print("     Phi_D    : 计算失败(见日志)")
        if g in controls:
            print(f"     对照判定 : {controls[g][0]} ({controls[g][1]})")
    print("-" * 64)
    print(f"  输出目录   : {run_dir}")
    print("    results.csv / fit_summary.json / report.md / run.log / figures/")
    print("=" * 64 + "\n")


def _print_dry_run(scans):
    print(f"\n{'文件名':<44}{'组别':<30}{'角色':<10}{'t(s)':>7}  状态")
    print("-" * 110)
    for s in sorted(scans, key=lambda s: (s.group, s.t)):
        stat = "OK" if not s.excluded else f"剔除: {s.excluded}"
        print(f"{s.path.name:<44}{s.group:<30}{s.role:<10}{s.t:>7.0f}  {stat}")
    n_ok = sum(1 for s in scans if not s.excluded)
    print("-" * 110)
    print(f"共 {len(scans)} 个文件, 有效 {n_ok} 个, 剔除 {len(scans) - n_ok} 个(原因见 run.log)\n")


# ═══════════════════════════ --plot-only: 从 json 重建 ═══════════════════════════
def results_from_json(j):
    results = {}
    for g, gd in (j.get("groups") or {}).items():
        e = {"role": gd.get("role"), "irradiance": gd.get("irradiance", 1.0),
             "A0_pooled": gd.get("A0_pooled"), "a_ex_auto": gd.get("a_ex_auto"),
             "reps": {r: {k2: np.asarray(v2, float) for k2, v2 in v.items()}
                      for r, v in (gd.get("reps") or {}).items()},
             "pooled": None}
        fd = gd.get("fit")
        if fd:
            e["pooled"] = FitResult(
                k=_fnum(fd.get("k")), k_err=_fnum(fd.get("k_err")), r2=_fnum(fd.get("r2")),
                n=int(fd.get("n") or 0), intercept=_fnum(fd.get("intercept")),
                truncated=bool(fd.get("truncated")), status=fd.get("status", "OK"),
                message=fd.get("message", ""), pre_k=_fnum(fd.get("pre_k")),
                pre_r2=_fnum(fd.get("pre_r2")),
                x=np.asarray(fd.get("x") or [], float),
                lnR=np.asarray(fd.get("lnR") or [], float))
        results[g] = e
    return results


def scans_from_json(j):
    scans = []
    for g, gd in (j.get("groups") or {}).items():
        sp = gd.get("spectra")
        if not sp:
            continue
        wl = np.asarray(sp["wl"], float)
        for sc in sp.get("scans", []):
            scans.append(Scan(path=Path(f"{g}-{sc['t']}.txt"), group=g,
                              role=gd.get("role", "sample"), t=float(sc["t"]),
                              wl=wl, a=np.asarray(sc["a"], float)))
    return scans


# ═══════════════════════════ 内置自检 (T1–T5 + F6) ═══════════════════════════
def _t3_ok(truth) -> bool:
    a0 = truth["a_ex"]
    p0 = calc_phi(0.005, 0.00625, a0, a0, 0.75)
    pr = calc_phi(0.005, 0.00625, a0, 10 * a0, 0.75)   # ref 侧 A_ex×10
    ps = calc_phi(0.005, 0.00625, 10 * a0, a0, 0.75)   # sample 侧 A_ex×10
    r = iabs(10 * a0) / iabs(a0)
    return abs(pr / p0 - r) < 1e-9 and abs(ps / p0 - 1 / r) < 1e-9


def self_test() -> bool:
    tmp = Path(tempfile.mkdtemp(prefix="phi_selftest_"))
    logging.getLogger("phi").setLevel(logging.ERROR)     # 自检时静音流水线日志
    try:
        truth = make_synthetic(tmp, seed=42)
        cfg = copy.deepcopy(DEFAULT_CFG)
        cfg["quantum_yield"]["a_ex"] = {"sample": truth["a_ex"], "ref": truth["a_ex"]}
        cfg["quantum_yield"]["phi_ref"] = truth["phi_ref"]
        scans, res = run_core(tmp, cfg)
        phi = compute_phi(res, cfg)
        jc = judge_controls(res, cfg)
        _, res2 = run_core(tmp, cfg)
        empty = Path(tempfile.mkdtemp(prefix="phi_empty_"))
        try:
            e_scans, e_res = run_core(empty, copy.deepcopy(DEFAULT_CFG))
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        f = res["sample"]["pooled"]; p = phi["sample"]
        checks = [
            ("T1  k 反算偏差 < 5%",
             f.status == "OK" and abs(f.k - truth["k_true"]) / truth["k_true"] < 0.05,
             f"k_hat={f.k:.4e} (真值 {truth['k_true']:.4e})"),
            ("T1  Φ 反算偏差 < 8%",
             p.get("status") == "OK" and _fnum(p.get("phi"), None) is not None
             and abs(p["phi"] - truth["phi_true"]) / truth["phi_true"] < 0.08,
             f"Phi_hat={_fmt(p.get('phi'), '.4f')} (真值 {truth['phi_true']:.2f})"),
            ("T2  非线性数据触发自动截断", res["nonlin"]["pooled"].truncated, "nonlin 组"),
            ("T3  A_ex×10 → Φ 按 I_abs 比例变化", _t3_ok(truth), "ref 侧增大/sample 侧下降"),
            ("T4  波长错位组整组剔除", "mis" not in res, "mis 组不存在"),
            ("T4  坏文件不崩溃且被剔除",
             any("broken" in s.path.name and s.excluded for s in scans), "broken.csv"),
            ("T4  空目录优雅降级", e_res == {} and e_scans == [], "返回空结果"),
            ("T5  两次运行比特一致",
             f"{f.k:.9e}" == f"{res2['sample']['pooled'].k:.9e}", "k 9 位有效数字一致"),
            ("F6  dark/N2/NaN3 对照判定通过",
             all(jc.get(x, ("x",))[0] == "通过" for x in ("dark", "n2", "nan3")),
             "; ".join(f"{x}:{jc.get(x, ('?',))[0]}" for x in ("dark", "n2", "nan3")))]
        print("\n===== 内置自检 (synthetic, seed=42) =====")
        ok = True
        for name, passed, info in checks:
            print(f"  [{'通过' if passed else '失败'}] {name}  <- {info}")
            ok &= bool(passed)
        print(f"===== 自检{'全部通过' if ok else '存在失败项'} =====\n")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        logging.getLogger("phi").setLevel(logging.NOTSET)


# ═══════════════════════════ CLI ═══════════════════════════
def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")     # Windows 控制台遇生僻字符不崩溃
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="¹O₂ 相对量子产率 Φ_Δ 计算工具(用法见文件头)")
    ap.add_argument("data_dir", nargs="?", type=Path, help="数据目录(每文件=一个时间点)")
    ap.add_argument("-c", "--config", type=Path, help="config.yaml(默认自动查找 ./config.yaml)")
    ap.add_argument("-o", "--out-root", type=Path, default=Path("runs"), help="输出根目录")
    ap.add_argument("--dry-run", action="store_true", help="只解析+QC, 不拟合不出图")
    ap.add_argument("--plot-only", type=Path, metavar="RUN_DIR", help="复用已有结果只重绘图")
    ap.add_argument("--bootstrap", action="store_true", help="附加 bootstrap 校验(n=1000)")
    ap.add_argument("--debug", action="store_true", help="控制台输出 DEBUG 级日志")
    ap.add_argument("--make-synthetic", nargs="?", const=Path("data/synthetic_demo"),
                    type=Path, metavar="DIR", help="生成 synthetic 测试数据集")
    ap.add_argument("--self-test", action="store_true", help="运行内置验收自检(T1-T5)")
    args = ap.parse_args(argv)

    if args.self_test:
        sys.exit(0 if self_test() else 1)
    if args.make_synthetic is not None:
        t = make_synthetic(args.make_synthetic)
        print(f"[OK] synthetic 数据已生成 -> {args.make_synthetic}")
        print(f"     k_true={t['k_true']:.4e} s^-1, k_ref={t['k_ref']:.4e}, "
              f"Phi_true={t['phi_true']:.2f}")
        print(f"     试运行: python {Path(sys.argv[0]).name} {args.make_synthetic}")
        return
    if args.plot_only:
        try:
            j = json.loads((args.plot_only / "fit_summary.json").read_text(encoding="utf-8"))
        except Exception as err:
            print(f"[错误] 无法读取 {args.plot_only}/fit_summary.json: {err}"); sys.exit(2)
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        plot_all(scans_from_json(j), results_from_json(j), j.get("phi") or {},
                 j.get("controls") or {}, j.get("config") or DEFAULT_CFG, args.plot_only)
        print(f"[OK] 图已重绘 -> {args.plot_only / 'figures'}")
        return
    if args.data_dir is None:
        ap.error("请提供数据目录(或使用 --self-test / --make-synthetic)")

    cfg = load_config(args.config or (Path("config.yaml") if Path("config.yaml").exists() else None))
    if args.bootstrap:
        cfg["quantum_yield"]["bootstrap"] = True
    run_dir = make_run_dir(args.out_root)
    debug = args.debug or cfg.get("run", {}).get("log_level") == "DEBUG"
    col = setup_logging(run_dir / "run.log", debug)
    logger.info("singlet_o2_phi v%s | git=%s | Python %s", VERSION, git_hash(),
                sys.version.split()[0])
    logger.info("数据目录: %s | 输出: %s", args.data_dir.resolve(), run_dir)

    scans, results = run_core(args.data_dir, cfg, dry=args.dry_run)
    if args.dry_run:
        _print_dry_run(scans)
        logger.info("dry-run 结束(未拟合、未出图); 日志: %s", run_dir / "run.log")
        return
    phi = compute_phi(results, cfg)
    controls = judge_controls(results, cfg)
    write_results_csv(run_dir / "results.csv", results, phi, controls)
    write_fit_summary(run_dir / "fit_summary.json", scans, results, phi, controls, cfg)
    plot_all(scans, results, phi, controls, cfg, run_dir)
    write_report(run_dir / "report.md", args.data_dir, scans, results, phi, controls, cfg,
                 col.records)
    print_console_summary(args.data_dir, scans, results, phi, controls, run_dir, cfg)
    sys.exit(0 if results else 1)


if __name__ == "__main__":
    main()