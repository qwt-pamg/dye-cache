# -*- coding: utf-8 -*-
"""P6 摩尔消光系数（ε）计算器：Beer–Lambert 浓度系列的**共同波长**拟合。

物理模型
--------
Beer–Lambert：``A = ε · c · l``。``A`` 是吸光度（AU，无量纲），``c`` 是浓度（用户
给定单位），``l`` 是光程（cm）。本模块对 (c, A) 做（可过原点 / 可加权）线性最小
二乘，得斜率 ``slope = dA/dc``，再由 ``ε = slope / l`` 得到消光系数：

* ``conc_unit`` 为 µM（``uM`` / ``µM`` / ``μM`` 三种写法都认）时，
  ``ε[M⁻¹cm⁻¹] = A/(c[µM]·l[cm]) × 10⁶``（µM→M 差 10⁶）；
* 其它单位（``mM`` / ``M`` / ``mg/mL`` …）**不做任何猜测性换算**：ε 按用户单位原样
  给出（``ε = A/(c·l)``），单位记作 ``"<conc_unit>⁻¹cm⁻¹"``，并在 ``issues`` 里明确
  写"未做摩尔换算"。

共同波长（本模块最关键的口径，务必理解）
----------------------------------------
λmax 模式下**不是**"每支谱各自找 λmax、再各自取 A"。那样每个浓度点落在不同波长上，
拟合出来的是"峰高随浓度的变化"，波长却在变——ε 的物理定义要求"同一波长上 A 随浓度
成比例"，故本模块这样做：

1. 取所有可用谱波长范围的**交集**，再与 ``lambda_max_range`` 求交，得"共同窗口"
   （这样选出的波长一定落在每支谱的范围内，不会有谱因为"够不到"而被剔除）；
2. 共同窗口内取所有谱采样波长的**并集**作网格——网格点都是真实测量波长，不引入
   插值外推；
3. 把每支谱线性插值到该网格后**逐点取均值**（nanmean，忽略该点无数据的谱），得到
   "均值谱"；在均值谱上取最大值所在波长作为**唯一**共同 λmax，所有谱都用它取 A。

为什么用均值谱而不是第一支谱：理想 Beer–Lambert 系列里各谱只差一个标量因子，
``argmax(均值谱)`` 与真实峰位**严格同址**（不是近似）；而单支谱（尤其是稀释系列的
第一支，A 最小、信噪比最差）的 argmax 会被噪声推着在峰顶附近跳 ±1 个步长，直接给 A
引入系统偏差。取 N 支平均后噪声降 √N，峰位估计最稳。仅当均值谱无法求值（各谱波长
范围互不重叠等）时才退回第一支可用谱。逐谱 λmax 仍会以
``wavelength_per_spectrum``（与 ``labels`` 下标对齐）与 ``per_point[i]["lambda_max"]``
返回，供人核对峰位漂移。

**已知取舍**：均值谱按 A 加权，所以当峰位真的随浓度漂移（聚集、溶剂化）时，高浓度谱会
把共同峰位往自己那边拉——这种数据本就不存在"唯一正确的波长"，工具给的是"强度加权的共同
峰位"，逐谱 λmax 一并返回供判断；对满足 Beer–Lambert 的系列（各谱同形状）毫无影响。

**已知统计偏差（选择效应，M9，必须知道）**
------------------------------------------
"在均值谱上取 argmax"是一个**数据驱动的选点**：选中的 λ* 天然偏向"平均噪声为正"的位置，
于是各谱在 λ* 处的 A 带**同号正偏移** δ>0，过原点斜率被抬高 ``Σc·δ/Σc² > 0``。
200 组固定种子实测（1% 相对噪声、5 支谱）：ε 均值 **+0.774%**（sd 0.514%，约 21σ）；
同批数据换成"用户固定波长"时偏差 ≈ 0。量级随 ``√N`` 收缩（谱越多越轻）。

* **这是选择效应，不是计算错误**——同批数据的斜率与闭式解逐位一致；
* 对**相对比较**（不同染料的 ε 一起偏高同样幅度）基本无影响，对**绝对定量**要留意；
* **规避办法**：把 ``extinction.wavelength`` 设成一个**外部给定**的波长（例如文献值、
  或先用一支高浓度谱单独定峰），此时 ``wavelength_mode="fixed"``，选点不再看数据。

其它数值口径
------------
* 取 A 用 ``np.interp`` **线性插值**（0.2/0.5/1 nm 步长都适用）；目标波长严格落在该谱
  范围之外 → 该点记 NaN 并从拟合中剔除（``issues`` 有中文说明，``per_point`` 有明细）。
* ``force_origin=True`` → 拟合 ``A = slope·c``，最小二乘解 ``slope = Σ(c·A)/Σ(c²)``，
  返回的 ``intercept`` **恒为 0.0**；``False`` → 带截距的普通最小二乘。
* ``weighted=True`` → 权重 ``w_i = 1/A_i``（低吸收点噪声大），最小化
  ``Σ w_i (A_i − Â_i)²``；等价于 ``np.polyfit(c, A, deg, w=np.sqrt(1/A))``（有测试钉住）。
  加权要求全部 A > 0，否则自动退回不加权并在 ``issues`` 里说明（结果里的 ``weighted``
  字段记录**实际**是否加权）。
* 标准误：``cov = s²·(XᵀWX)⁻¹``，``s² = Σ w·r² / (n − p)``（p = 1 过原点 / 2 带截距）。
  权重整体缩放不影响标准误（可验证），未加权时与 ``np.polyfit``/``linregress`` 同量级。
* ``R² = 1 − SS_res/SS_tot``，``SS_tot`` 用**均值中心化**——带截距时与
  ``scipy.stats.linregress`` 的 ``rvalue²`` 严格一致，与 ``core/kinetics.py`` 的 exp2
  分支同一口径。``SS_tot = 0``（各点 A 完全相同）时返回 NaN 而**不是** kinetics 的
  1.0：这里"R²=1"会掩盖"斜率不可信"，该情形改由 ``slope ≤ 0`` 的告警兜住。
* 相对偏差 ``dev = |A − Â| / |Â|``（以拟合值为基准）。R² 与偏差一律用**未加权**残差，
  使加权/不加权两次运行的 ``r2_warn`` / ``dev_warn_frac`` 阈值可比。

健壮性（硬要求）：光谱数与浓度数不一致、浓度含非正值、光程非正、有效点 < 2、目标波长
全部超范围、浓度全相同 —— 一律返回 ``ok=False`` + 中文 ``reason``，**绝不抛异常**。
本模块无任何 GUI / 绘图依赖（只用 numpy + 标准库 csv），可被 CLI、流水线与单测直接调用。
"""
from __future__ import annotations

import csv
import logging
import warnings
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from ..report.results import atomic_write, fmt, fnum

log = logging.getLogger("dye_tool.extinction")

__all__ = ["compute_extinction", "write_extinction_table"]

#: µM 的 ε 单位（摩尔消光系数的常规写法）
EPS_MOLAR_UNIT = "M⁻¹cm⁻¹"

#: 视为"微摩尔"的单位写法（含 micro sign U+00B5 与希腊字母 mu U+03BC）
_MICRO_UNITS = frozenset({"um", "µm", "μm"})

#: 端点容差（nm）：只为吸收步长累加的浮点误差（0.2 nm 步长约 1e-13）
_TOL_NM = 1e-9

_EMPTY = np.array([], float)


# ═══════════════════════════ 配置读取（宽容，不抛异常） ═══════════════════════════
def _ext_cfg(cfg: dict) -> dict:
    """取 ``cfg["extinction"]`` 子节。

    既接受**完整配置**（``cfg["extinction"]`` 存在时用它，键位权威），也接受直接传入
    的 extinction 子节本身——后者是为了单测/脚本调用方便，不改变语义。
    """
    if not isinstance(cfg, dict):
        return {}
    sub = cfg.get("extinction")
    return sub if isinstance(sub, dict) else cfg


def _flag(v, default: bool) -> bool:
    """宽容解析布尔配置（yaml 可能给 bool/int/字符串）；无法判断 → ``default``。"""
    if v is None:
        return default
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, float, np.integer, np.floating)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1", "on"):
        return True
    if s in ("false", "no", "n", "0", "off"):
        return False
    return default


def _num_list(raw) -> Optional[list[float]]:
    """配置里的浓度序列 → ``list[float]``；不是"数字列表" → ``None``。"""
    if raw is None:
        return None
    if isinstance(raw, np.ndarray):
        raw = raw.tolist()
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return None
    out: list[float] = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            return None
    return out


def _range_of(raw, default: tuple[float, float]) -> tuple[float, float]:
    """``lambda_max_range`` → ``(lo, hi)``；长度/数值/顺序不合法 → ``default``。"""
    try:
        lo, hi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return default
    if not (np.isfinite(lo) and np.isfinite(hi)) or lo >= hi:
        return default
    return lo, hi


# ═══════════════════════════ 单支谱 → 数值数组 ═══════════════════════════
def _label_of(sp, i: int) -> str:
    """光谱显示名：优先 ``label``，其次文件名主干，最后 ``#序号``。"""
    lab = str(getattr(sp, "label", "") or "").strip()
    if lab:
        return lab
    try:
        stem = Path(getattr(sp, "path", "")).stem
    except (TypeError, ValueError):
        stem = ""
    return stem or f"#{i + 1}"


def _arrays_of(sp) -> tuple[np.ndarray, np.ndarray, str]:
    """取一支谱的 ``(波长, 强度)``，**只保留有限点**并按波长升序。

    Returns:
        ``(wl, y, why)``：可用时 ``why == ""``；不可用时返回两个空数组 + 中文原因。

    不抛异常：任何形状/类型问题都转成原因字符串（调用方记入 ``per_point[].note``）。
    """
    if sp is None:
        return _EMPTY, _EMPTY, "光谱对象为空"
    if str(getattr(sp, "kind", "") or "").lower() == "em":
        # 荧光发射谱的纵轴不是吸光度，套 Beer–Lambert 没有意义 → 直接排除
        return _EMPTY, _EMPTY, "荧光发射谱（kind=em）的纵轴不是吸光度 A，不能用于 ε 计算"
    if not bool(getattr(sp, "ok", True)):
        why = str(getattr(sp, "excluded", "") or "已标记剔除")
        return _EMPTY, _EMPTY, f"光谱不可用（{why}）"
    wl_raw, y_raw = getattr(sp, "wl", None), getattr(sp, "y", None)
    if wl_raw is None or y_raw is None:
        return _EMPTY, _EMPTY, "缺少波长或强度数组"
    try:
        wl = np.asarray(wl_raw, float).ravel()
        y = np.asarray(y_raw, float).ravel()
    except (TypeError, ValueError) as err:
        return _EMPTY, _EMPTY, f"波长/强度不是数值数组（{err}）"
    if wl.size != y.size:
        return _EMPTY, _EMPTY, f"波长与强度长度不一致（{wl.size} vs {y.size}）"
    m = np.isfinite(wl) & np.isfinite(y)
    if int(m.sum()) < 2:
        return _EMPTY, _EMPTY, f"有效点不足 2 个（仅 {int(m.sum())} 个）"
    w, v = wl[m], y[m]
    if np.any(np.diff(w) <= 0):
        o = np.argsort(w, kind="stable")
        w, v = w[o], v[o]
    return w, v, ""


def _interp_at(w: np.ndarray, v: np.ndarray, target: float) -> float:
    """线性插值到 ``target``；严格超出 ``w`` 端点或有效点 < 2 → NaN。

    端点本身算"范围内"（``np.interp`` 的端点行为），只有严格超出才记 NaN，容差
    ``_TOL_NM`` 仅为吸收步长累加的浮点误差。
    """
    if w.size < 2:
        return float("nan")
    lo, hi = float(w[0]), float(w[-1])
    if target < lo - _TOL_NM or target > hi + _TOL_NM:
        return float("nan")
    return float(np.interp(float(target), w, v))


def _peak_in(w: np.ndarray, v: np.ndarray, lo: float, hi: float) -> float:
    """``[lo, hi]`` 内最大值所在波长；窗外无有效点 → NaN。

    并列时取**最短波长**（``np.argmax`` 语义），与 ``core.io.Spectrum.lambda_max`` /
    ``core.spectra.lambda_max`` 完全一致。
    """
    m = (w >= lo) & (w <= hi)
    if not m.any():
        return float("nan")
    return float(w[m][int(np.argmax(v[m]))])


def _common_lambda_max(pairs: Sequence[tuple], lo: float, hi: float
                       ) -> tuple[float, list[float], list[float]]:
    """在**均值谱**上确定共同 λmax（见模块 docstring）。

    Args:
        pairs: 可用谱的 ``(wl, y)``（已按波长升序、只含有限点）。
        lo, hi: ``lambda_max_range``。

    Returns:
        ``(共同波长, 逐谱 λmax 列表, 网格波长范围)``；共同波长 NaN = 无法确定。
    """
    per = [_peak_in(w, v, lo, hi) for w, v in pairs]
    if not pairs:
        return float("nan"), per, [float("nan"), float("nan")]
    # ① 共同窗口 = 所有谱波长范围的交集 ∩ 搜索窗；空则退回搜索窗（后面按超范围剔除）
    ov_lo = max(float(w[0]) for w, _ in pairs)
    ov_hi = min(float(w[-1]) for w, _ in pairs)
    win_lo, win_hi = max(lo, ov_lo), min(hi, ov_hi)
    if win_lo >= win_hi:
        win_lo, win_hi = lo, hi
    # ② 网格 = 窗口内所有谱采样波长的并集（都是真实测量波长）
    grid = np.unique(np.concatenate([w[(w >= win_lo) & (w <= win_hi)] for w, _ in pairs]))
    if grid.size < 2:
        return float("nan"), per, [float(win_lo), float(win_hi)]
    # ③ 均值谱 → argmax
    stack = np.vstack([np.interp(grid, w, v) for w, v in pairs])
    with warnings.catch_warnings():
        # 某列全部谱都没数据时 nanmean 会报 All-NaN slice，这里本就要忽略该列
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(all="ignore"):
            mean = np.nanmean(stack, axis=0)
    ok = np.isfinite(mean)
    if not ok.any():
        return float("nan"), per, [float(win_lo), float(win_hi)]
    idx = int(np.argmax(np.where(ok, mean, -np.inf)))
    return float(grid[idx]), per, [float(win_lo), float(win_hi)]


# ═══════════════════════════ 加权最小二乘（闭式解） ═══════════════════════════
def _wls_fit(x: np.ndarray, y: np.ndarray, force_origin: bool,
             weights: Optional[np.ndarray]) -> Optional[dict]:
    """一元加权最小二乘 → ``{slope, intercept, se_slope, se_intercept, r2, fit, resid}``。

    不带权（``weights=None``）时与 ``np.polyfit(x, y, 1)`` / ``scipy.stats.linregress``
    数值一致；带权 ``w`` 时与 ``np.polyfit(x, y, deg, w=np.sqrt(w))`` 一致（两条都有
    测试钉住）。带截距且所有 x 相同 → 法方程奇异，返回 ``None``（调用方转中文 reason）。
    """
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    w = np.ones_like(x) if weights is None else np.asarray(weights, float)
    n = x.size
    if force_origin:
        sxx = float(np.sum(w * x * x))
        if not (sxx > 0):
            return None
        slope = float(np.sum(w * x * y)) / sxx
        intercept, npar = 0.0, 1
        cov = np.array([[1.0 / sxx]])                   # (XᵀWX)⁻¹（只有斜率）
    else:
        sw = float(np.sum(w))
        swx = float(np.sum(w * x))
        swy = float(np.sum(w * y))
        swxx = float(np.sum(w * x * x))
        swxy = float(np.sum(w * x * y))
        det = sw * swxx - swx * swx                     # Cauchy–Schwarz ≥ 0
        if not (det > 0):
            return None
        slope = (sw * swxy - swx * swy) / det
        intercept = (swxx * swy - swx * swxy) / det
        npar = 2
        # (XᵀWX)⁻¹，**按 (斜率, 截距) 顺序**排（参数顺序必须是 fit = intercept + slope·x
        # 的逆序对应：var(slope) = s²·sw/det、var(intercept) = s²·swxx/det。写反了不会
        # 报错，只会把截距的标准误当成斜率的——由 linregress 对照用例钉死）。
        cov = np.array([[sw, -swx], [-swx, swxx]]) / det
    fit = intercept + slope * x
    resid = y - fit
    ss_res = float(np.sum(resid ** 2))                  # 未加权残差（与 r2_warn 可比）
    ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
    dof = n - npar
    s2 = float(np.sum(w * resid ** 2)) / dof if dof > 0 else float("nan")
    se = (np.sqrt(s2 * np.diag(cov)) if np.isfinite(s2)
          else np.full(npar, float("nan")))
    return {"slope": float(slope), "intercept": float(intercept),
            "se_slope": float(se[0]),
            "se_intercept": float(se[1]) if npar > 1 else float("nan"),
            "r2": (1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan"),
            "fit": fit, "resid": resid}


# ═══════════════════════════ 结果骨架 ═══════════════════════════
def _skeleton() -> dict:
    """结果骨架：成功/失败两条路径的键**完全一致**（下游不会 KeyError）。"""
    return {
        "ok": False,
        "reason": "",
        "wavelength": float("nan"),
        "wavelength_mode": "lambda_max",
        "wavelength_per_spectrum": [],      # 与 labels 下标对齐（NaN=不可用/窗外）
        "a": [],                            # 仅用于拟合的有效点（与 concentrations 等长）
        "concentrations": [],
        "slope": float("nan"),              # dA/dc（用户浓度单位）
        "slope_stderr": float("nan"),
        "intercept": float("nan"),          # A 轴截距（过原点时恒为 0.0）
        "intercept_stderr": float("nan"),
        "r2": float("nan"),
        "eps": float("nan"),
        "eps_stderr": float("nan"),
        "eps_raw": float("nan"),            # A/(c·l)：按用户浓度单位原样，未做摩尔换算
        "eps_unit": EPS_MOLAR_UNIT,
        "molar_converted": False,
        "conc_unit": "uM",
        "path_length_cm": 1.0,
        "force_origin": True,
        "weighted": False,                  # **实际**是否加权（可能因 A≤0 退回）
        "r2_warn": 0.995,
        "dev_warn_frac": 0.15,
        "n_points": 0,
        "n_total": 0,
        "n_dropped": 0,
        "labels": [],
        "per_point": [],
        "issues": [],
    }


# ═══════════════════════════ 主入口 ═══════════════════════════
def compute_extinction(spectra: list, cfg: dict) -> dict:
    """由浓度系列光谱反算摩尔消光系数 ε（Beer–Lambert 线性拟合）。

    :param spectra: ``core.io.Spectrum`` 列表（**顺序必须与浓度一一对应**）；也接受
        任何暴露 ``wl`` / ``y`` / ``kind`` / ``label`` / ``ok`` 属性的对象。
    :param cfg: 完整配置 dict（读 ``cfg["extinction"]``）；直接传 extinction 子节亦可。
    :return: 结果 dict，键见 :func:`_skeleton`；不可算时 ``ok=False`` 且 ``reason``
        为中文原因（**不抛异常**）。

    关键键位：

    * ``wavelength`` / ``wavelength_mode``：实际取 A 的波长（nm）与来源
      （``"lambda_max"`` = 均值谱共同峰位 / ``"fixed"`` = 配置指定）；
    * ``a`` / ``concentrations``：**仅用于拟合的有效点**（等长，可直接画 A–c 图或复算
      ε）；被剔除的点在 ``per_point`` 里（``used=False`` + 中文 ``note``）；
    * ``slope`` / ``intercept`` / ``r2`` / ``slope_stderr``：A = slope·c + intercept 的
      最小二乘结果（``slope`` 是换算前的值：单位 = AU / 用户浓度单位）；
    * ``eps`` = ``slope / l``（``conc_unit`` 为 µM 时再 ×10⁶），``eps_raw`` =
      ``slope / l``（原样，未换算），``eps_unit`` 是 ε 的单位字符串；
    * ``per_point[i]``：``{index, label, c, a, lambda_max, fit, residual, dev, over,
      used, note}``（``dev`` = |A−Â|/|Â|，``over`` = dev 超 ``dev_warn_frac``）；
    * ``issues``：中文告警/说明，顺序 = 拟合质量 → 被剔除点 → 口径说明。
    """
    et = _ext_cfg(cfg)
    conc_unit = str(et.get("conc_unit") or "uM").strip() or "uM"
    molar = conc_unit.lower() in _MICRO_UNITS
    eps_unit = EPS_MOLAR_UNIT if molar else f"{conc_unit}⁻¹cm⁻¹"
    r2_warn = fnum(et.get("r2_warn"), 0.995)
    dev_warn = fnum(et.get("dev_warn_frac"), 0.15)
    force_origin = _flag(et.get("force_origin"), True)
    want_weighted = _flag(et.get("weighted"), False)
    # L26（评审记录）：原注释写"path_length_cm 留空要能被校验抓住"，**与代码矛盾**：
    # 下面这一行明确把 None 替换成 1.0，所以留空走的是默认光程、永远不会报错。
    # 哪个对？——留空走默认是对的：YAML 里 `path_length_cm:`（留空）与"根本没写这一行"
    # 在 `safe_load` 之后都是 None，二者无法区分；而"写了一行留空"最自然的语义就是
    # "用默认值"。真正该被抓住的是 **写错的非 None 值**（`'abc'`、0、负数、NaN），
    # 那些仍然走 fnum(·, nan) → 落到下面的校验里报错（有测试钉住）。
    raw_l = et.get("path_length_cm", 1.0)
    l_cm = fnum(1.0 if raw_l is None else raw_l, float("nan"))

    out = _skeleton()
    out.update(conc_unit=conc_unit, molar_converted=molar, eps_unit=eps_unit,
               path_length_cm=l_cm, r2_warn=r2_warn, dev_warn_frac=dev_warn,
               force_origin=force_origin, weighted=want_weighted)

    def fail(reason: str, **extra) -> dict:
        """统一的失败出口：写 ``reason`` + 可选补字段，返回同一 dict。"""
        out["ok"] = False
        out["reason"] = reason
        out.update(extra)
        log.warning("P6 ε 计算未完成：%s", reason)
        return out

    # ── ① 输入校验（全部转成中文 reason，绝不抛异常）──────────────────────
    try:
        specs = list(spectra) if spectra is not None else []
    except TypeError:
        return fail("spectra 不是可迭代的光谱列表（类型无效）")
    out["n_total"] = len(specs)
    if not specs:
        return fail("未提供光谱：spectra 为空，无法计算 ε")
    conc = _num_list(et.get("concentrations"))
    if conc is None:
        return fail("配置 extinction.concentrations 不是数字列表"
                    "（应与光谱顺序一一对应，例如 [1, 2, 5, 10]）")
    if len(conc) != len(specs):
        return fail(f"光谱数（{len(specs)}）与浓度数（{len(conc)}）不一致："
                    "浓度必须与光谱顺序一一对应")
    bad = [(i, v) for i, v in enumerate(conc) if not (np.isfinite(v) and v > 0)]
    if bad:
        i0, v0 = bad[0]
        more = f"（另有 {len(bad) - 1} 个非法值）" if len(bad) > 1 else ""
        return fail(f"浓度必须为正的有限数：第 {i0 + 1} 个浓度 {v0!r} 非法{more}")
    if not np.isfinite(l_cm):
        return fail(f"光程不是数值：path_length_cm={et.get('path_length_cm')!r}")
    if l_cm <= 0:
        return fail(f"光程必须为正：path_length_cm={et.get('path_length_cm')!r}"
                    "（常规比色皿 1.0 cm）")

    # ── ② 逐谱取数组（可用性 + kind 判定）────────────────────────────────
    labels: list[str] = []
    arrays: list[tuple[np.ndarray, np.ndarray, str]] = []
    for i, sp in enumerate(specs):
        labels.append(_label_of(sp, i))
        arrays.append(_arrays_of(sp))
    out["labels"] = labels
    pairs = [(w, v) for (w, v, why) in arrays if not why]

    # ── ③ 确定"所有谱共用"的取 A 波长 ───────────────────────────────────
    lo_rng, hi_rng = _range_of(et.get("lambda_max_range"), (300.0, 800.0))
    raw_w = et.get("wavelength", None)
    if raw_w is None:
        wl_all, per_lmax, win = _common_lambda_max(pairs, lo_rng, hi_rng)
        out["wavelength_mode"] = "lambda_max"
        if not np.isfinite(wl_all):
            # L21（评审记录）：没有可用谱时 `_common_lambda_max` 返回的窗口是
            # (nan, nan)，原文案直接格式化进去 → "共同窗口 nan–nan nm"，
            # 用户看到的是一句残缺的话。现在分两种情形说清楚。
            if not pairs:
                return fail("没有可用光谱：全部谱都不可用（见各谱的 reasons）——"
                            "请确认文件是吸收谱（荧光发射谱的纵轴不是吸光度）")
            if np.isfinite(win[0]) and np.isfinite(win[1]):
                where = f"共同窗口 {win[0]:.0f}–{win[1]:.0f} nm 内有效采样点不足（需 ≥2 个）"
            else:
                where = "可用谱的波长范围没有交集（无法在同一波长上比较）"
            return fail(f"在 {lo_rng:.0f}–{hi_rng:.0f} nm 内找不到共同 λmax："
                        f"可用谱 {len(pairs)} 支、{where}")
        log.info("P6: 共同 λmax = %.4g nm（均值谱法，%d 支谱；窗口 %.0f–%.0f nm）",
                 wl_all, len(pairs), win[0], win[1])
    else:
        wl_all = fnum(raw_w, float("nan"))
        out["wavelength_mode"] = "fixed"
        per_lmax = [_peak_in(w, v, lo_rng, hi_rng) for w, v in pairs]
        if not np.isfinite(wl_all):
            return fail(f"配置 extinction.wavelength 不是数值：{raw_w!r}")
        if wl_all <= 0:
            return fail(f"配置 extinction.wavelength 必须为正波长（nm）：{raw_w!r}")
    out["wavelength"] = float(wl_all)
    # 逐谱 λmax：与 labels 下标对齐（不可用/窗外 → NaN），固定波长模式下同样给出供核对
    per_by_idx = [float("nan")] * len(specs)
    k = 0
    for i, (w, v, why) in enumerate(arrays):
        if not why:                              # per_lmax 只覆盖可用谱
            per_by_idx[i] = float(per_lmax[k]) if k < len(per_lmax) else float("nan")
            k += 1
    out["wavelength_per_spectrum"] = per_by_idx

    # ── ④ 逐点在**同一波长**取 A（线性插值；超范围 → NaN + 剔除）────────
    per_point: list[dict] = []
    used_idx: list[int] = []
    for i, (w, v, why) in enumerate(arrays):
        c = float(conc[i])
        a_i, note = float("nan"), why
        if not why:
            a_i = _interp_at(w, v, wl_all)
            if not np.isfinite(a_i):
                note = (f"λ={wl_all:.4g} nm 超出该谱波长范围"
                        f"（{float(w[0]):.4g}–{float(w[-1]):.4g} nm），已从拟合中剔除")
        per_point.append({"index": i, "label": labels[i], "c": c, "a": a_i,
                          "lambda_max": per_by_idx[i], "fit": float("nan"),
                          "residual": float("nan"), "dev": float("nan"),
                          "over": False, "used": False, "note": note})
        if not note and np.isfinite(a_i):
            per_point[-1]["used"] = True
            used_idx.append(i)
    out["per_point"] = per_point
    if len(used_idx) < 2:
        return fail(f"有效点不足 2 个（共 {len(specs)} 支谱，仅 {len(used_idx)} 个点可用）："
                    "无法拟合 ε，请检查波长设置与各谱的波长范围")
    c_arr = np.array([per_point[i]["c"] for i in used_idx], float)
    a_arr = np.array([per_point[i]["a"] for i in used_idx], float)

    # ── ⑤ 拟合（过原点 / 带截距；可选 1/A 加权）────────────────────────
    notes: list[str] = []
    weights = None
    if want_weighted:
        if np.any(a_arr <= 0):
            notes.append(f"加权拟合要求全部 A > 0：有 {int(np.sum(a_arr <= 0))} 个点 A ≤ 0"
                         f"（最小 {float(np.min(a_arr)):.4g} AU），本次自动退回不加权")
            out["weighted"] = False
        else:
            weights = 1.0 / a_arr
    fit = _wls_fit(c_arr, a_arr, force_origin, weights)
    if fit is None:
        return fail("浓度序列退化（所有浓度相同）：斜率无法确定，请给出一组不同的浓度")
    for k, idx in enumerate(used_idx):
        pp = per_point[idx]
        a_hat = float(fit["fit"][k])
        pp["fit"] = a_hat
        pp["residual"] = float(fit["resid"][k])
        pp["dev"] = (abs(float(a_arr[k]) - a_hat) / abs(a_hat)
                     if a_hat != 0 else float("nan"))
        pp["over"] = bool(np.isfinite(pp["dev"]) and pp["dev"] > dev_warn)

    slope, intercept = float(fit["slope"]), float(fit["intercept"])
    eps_raw = slope / l_cm
    factor = 1.0e6 if molar else 1.0
    out.update(ok=True, reason="",
               a=a_arr.tolist(), concentrations=c_arr.tolist(),
               slope=slope, slope_stderr=float(fit["se_slope"]),
               intercept=intercept, intercept_stderr=float(fit["se_intercept"]),
               r2=float(fit["r2"]),
               eps=eps_raw * factor, eps_stderr=float(fit["se_slope"]) / l_cm * factor,
               eps_raw=eps_raw,
               n_points=len(used_idx), n_dropped=len(specs) - len(used_idx))

    # ── ⑥ 告警（顺序：拟合质量 → 被剔除点 → 口径说明）──────────────────
    quality: list[str] = []
    if np.isfinite(out["r2"]) and out["r2"] < r2_warn:
        quality.append(f"线性性差：R²={out['r2']:.4f} < 阈值 {r2_warn:g}"
                       "（浓度系列偏离 Beer–Lambert：可能超出线性区、存在聚集/"
                       "内滤效应，或浓度配制有误）")
    if slope <= 0:
        quality.append(f"拟合斜率 ≤ 0（slope={slope:.4g}）：A 不随浓度增长，"
                       "请确认取 A 的波长是否落在吸收峰上、浓度顺序是否与光谱顺序一致")
    for pp in per_point:
        if pp["over"]:
            quality.append(f"浓度 {pp['c']:g} {conc_unit} 处的相对偏差 "
                           f"{pp['dev'] * 100:.1f}% > 阈值 {dev_warn * 100:g}%"
                           f"（A={pp['a']:.4f}，拟合 A={pp['fit']:.4f}）："
                           "该点可能偏离线性或配制有误")
    drops: list[str] = []
    for pp in (p for p in per_point if not p["used"]):
        if len(drops) < 5:
            drops.append(f"第 {pp['index'] + 1} 支谱「{pp['label']}」未参与拟合：{pp['note']}")
        elif len(drops) == 5:
            drops.append(f"…另有 {len(specs) - len(used_idx) - 5} 个点未参与拟合"
                         "（明细见 per_point 与 CSV 备注列）")
    if not molar:
        notes.append(f"conc_unit=「{conc_unit}」不是 µM：ε 按该浓度单位原样计算，"
                     f"**未做摩尔换算**（不可与文献 M⁻¹cm⁻¹ 直接比较）；"
                     f"若要 M⁻¹cm⁻¹，请把浓度换算成 µM 后重算，单位应为 {eps_unit}")
    if not force_origin:
        notes.append(f"本次为带截距拟合（force_origin=False）：截距 = {intercept:.4g} AU；"
                     "非零截距常来自基线偏移或散射，会与 ε 的系统偏差混在一起")
    # M9：把"选点带来的系统性正偏"说在明处——只写在 docstring 里用户看不到。
    # 只在 lambda_max 模式（选点看数据）时提示；fixed 模式无此偏差。
    if out.get("wavelength_mode") == "lambda_max":
        notes.append(
            f"波长是**由数据选出来的**（均值谱共同 λmax = {wl_all:.4g} nm）："
            "选点偏向平均噪声为正处，会让 ε **系统性偏高**（实测 1% 噪声、5 支谱约 +0.8%，"
            "谱越多越轻）。做**绝对定量**或要与文献值比时，请把 "
            "`extinction.wavelength` 设成一个外部给定的波长（如文献值或单支高浓度谱定峰），"
            "改用 fixed 模式即可消除该偏差")
    if not any(pp["used"] for pp in per_point) or len(used_idx) != len(specs):
        log.warning("P6: %d/%d 支谱未参与拟合（详见 issues/per_point）",
                    len(specs) - len(used_idx), len(specs))
    out["issues"] = quality + drops + notes
    log.info("P6: λ=%.4g nm（%s）, ε=%.6g %s（R²=%.5f, n=%d, 加权=%s）",
             wl_all, out["wavelength_mode"], out["eps"], eps_unit,
             out["r2"], out["n_points"], "是" if out["weighted"] else "否")
    return out


# ═══════════════════════════ 结果写出 ═══════════════════════════
#: CSV 表头（前两列是溯源信息，其后按"波长 → 浓度 → A → 拟合A → 残差 → 相对偏差"排）
_CSV_HEAD = ["序号", "光谱", "波长(nm)", "浓度", "A", "拟合A", "残差", "相对偏差(%)",
             "参与拟合", "备注"]


def _pct(v) -> str:
    """相对偏差（小数）→ 百分数字符串；非有限 → ``"N/A"``。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "N/A"
    return fmt(f * 100.0, ".4g") if np.isfinite(f) else "N/A"


def write_extinction_table(result: dict, out_path) -> Path:
    """把 :func:`compute_extinction` 的结果写成 UTF-8-SIG CSV（Excel 双击可开）。

    编码与 :func:`core.results.write_results_csv` 保持一致（``utf-8-sig`` +
    ``newline=""`` + 原子写），写入 ``excel`` 友好的 BOM。表结构：

    1. 表头 + 每个输入点一行（``per_point`` 顺序 = 输入光谱顺序，含未参与拟合的点，
       备注列写中文原因）；
    2. ``ε 汇总`` 段：状态、失败原因（若有）、波长/光程/单位/ε/标准误/斜率/截距/R²/
       点数/加权与摩尔换算口径；
    3. ``告警与说明`` 段：``issues`` 全文（无告警写"无"）。

    ``result`` 为 ``ok=False`` 的结果时**同样写出**（含失败原因），不抛异常，
    方便把"为什么没算出来"一并留档。返回写出的 ``Path``。
    """
    path = Path(out_path)
    r = dict(result or {})
    unit = str(r.get("conc_unit") or "uM")
    head = list(_CSV_HEAD)
    head[3] = f"浓度({unit})"
    issues = [str(m) for m in (r.get("issues") or [])]
    rows = r.get("per_point") or []
    with atomic_write(path, newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(head)
        for pp in rows:
            pp = pp or {}
            w.writerow([int(pp.get("index", 0)) + 1, pp.get("label", ""),
                        fmt(r.get("wavelength"), ".6g"), fmt(pp.get("c"), ".6g"),
                        fmt(pp.get("a"), ".6g"), fmt(pp.get("fit"), ".6g"),
                        fmt(pp.get("residual"), ".6g"), _pct(pp.get("dev")),
                        "是" if pp.get("used") else "否", pp.get("note") or ""])
        w.writerow([])
        w.writerow(["ε 汇总"])
        w.writerow(["状态", "成功" if r.get("ok") else "失败"])
        if not r.get("ok"):
            w.writerow(["失败原因", r.get("reason") or "未说明"])
        w.writerow(["波长 λ (nm)", fmt(r.get("wavelength"), ".6g")])
        w.writerow(["波长模式", "共同 λmax（均值谱）" if r.get("wavelength_mode")
                    == "lambda_max" else "固定波长（配置指定）"])
        w.writerow(["光程 l (cm)", fmt(r.get("path_length_cm"), ".6g")])
        w.writerow(["浓度单位", unit])
        w.writerow([f"ε ({r.get('eps_unit')})", fmt(r.get("eps"), ".6g")])
        w.writerow(["ε 标准误", fmt(r.get("eps_stderr"), ".6g")])
        w.writerow(["ε 原样值 A/(c·l)（未换算）", fmt(r.get("eps_raw"), ".6g")])
        w.writerow(["斜率 slope (A / 浓度单位)", fmt(r.get("slope"), ".6g")])
        w.writerow(["斜率标准误", fmt(r.get("slope_stderr"), ".6g")])
        w.writerow(["截距 intercept (AU)", fmt(r.get("intercept"), ".6g")])
        w.writerow(["拟合方式", "过原点（无截距）" if r.get("force_origin")
                    else "带截距"])
        w.writerow(["加权拟合 (1/A)", "是" if r.get("weighted") else "否"])
        w.writerow(["摩尔换算（µM→M ×1e6）",
                    "是" if r.get("molar_converted") else "否（未做摩尔换算）"])
        w.writerow(["R²", fmt(r.get("r2"), ".6f")])
        w.writerow(["参与拟合点数", str(int(r.get("n_points") or 0))])
        w.writerow(["输入光谱数", str(int(r.get("n_total") or 0))])
        w.writerow([])
        w.writerow(["告警与说明"])
        for msg in issues or ["无"]:
            w.writerow(["-", msg])
    log.info("已写出 %s（%d 行数据 + ε 汇总 + %d 条告警）",
             path, len(rows), len(issues))
    return path
