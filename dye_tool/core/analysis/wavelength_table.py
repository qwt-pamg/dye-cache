# -*- coding: utf-8 -*-
"""P7 任意波长吸光度表（A@λ）：给定一组波长，批量导出每个文件在这些波长处的吸光度。

场景：论文表格直出——跨组、跨染料把"某几个波长处的吸光度"摆成一张**列对齐**的表。

## 口径（改动前先读这三条，它们是本模块的全部数值契约）

1. **取点**（``interpolate=False``，默认）取**最近的实测点**，但仅当
   ``|λ_实测 − λ_目标| <= tol_nm``（**含边界**，与 :mod:`core.qc` 的容差风格一致）；
   超差该格记 NaN 并告警——**绝不静默取一个远处的点**。
   恰好等距时取**低波长**那个（``np.argmin`` 取首个，坐标已升序）。
2. **插值**（``interpolate=True``）在实测点之间线性插值，数值路径就是
   :func:`numpy.interp`（与 :func:`core.qc.interp_valid` 同源，逐位一致）；
   目标落在数据范围之外**不外推** → 记 NaN 并告警（⚠ ``qc.interp_valid`` 会把端点值
   平推出去、有效点 <2 时还返回 0，那两条语义本模块都不能要，故不复用它）。
   插值模式不使用 ``tol_nm``。
3. **对齐**：所有行共用同一组波长列（同一顺序、同一键名 ``A@400.0``），否则表没法
   逐列比较；重复波长只保留一列并记 WARNING。波长列名由 ``str(float(w))`` 生成，
   既贴合 ``A@400.0`` 的可读形式，又对不同浮点值天然不撞名。

## 边界

* 本模块**不 import matplotlib / PySide6**，也**不 import 任何染料解析的全局状态**
  （``core`` 层不得依赖 UI 配置或染料注册表）：染料名由调用方经 ``dye_of`` 回调注入。
* 任何输入都不得抛异常：坏配置/坏输入 → ``ok=False`` + 中文 ``reason``，
  坏文件 → 逐文件跳过并给出中文原因（见 :func:`absorbance_at` 的 Returns）。
* 写出风格与 :func:`core.results.write_results_csv` 对齐：``utf-8-sig`` + :mod:`csv`
  + :func:`core.results.atomic_write`（Excel 直接打开不乱码；写一半被杀不留半截文件）。
"""
from __future__ import annotations

import csv
import logging
import math
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from ..report.results import atomic_write

log = logging.getLogger("dye_tool.wavelength_table")

#: CSV 里波长列**之前**的元数据列（顺序即列序；行 dict 的键名与之逐字一致）
META_FIELDS = ("file", "group", "dye", "rep", "t", "t_src", "t_edited",
               "role", "n_points")

#: 仅当配置缺 ``tol_nm`` / ``interpolate`` 时才用的回退值。
#: ⚠ 波长列表**没有**回退值：缺了就没有表可出（配置 :mod:`core.config` 是唯一权威）。
DEF_TOL_NM = 0.5
DEF_INTERPOLATE = False

#: 数值单元格格式（6 位有效数字，:func:`core.results.fmt` 的默认口径）
NUM_FMT = ".6g"

#: ``issues`` 一条消息里最多列几个波长列名（避免一行几百字符）
_MAX_LISTED = 8


# ═══════════════════════════ 小工具 ═══════════════════════════
def _num(v, default=None):
    """尽力转 float；失败或非有限 → ``default``。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _attr(obj, name, default=None):
    """取属性（兼容鸭子类型对象；缺属性/属性抛异常都退回 ``default``）。"""
    try:
        return getattr(obj, name, default)
    except Exception:                                  # noqa: BLE001
        return default


def _as_bool(v, default: bool) -> bool:
    """宽松布尔解析（``"false"`` 这种字符串不能被 ``bool()`` 判成 True）。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        t = v.strip().lower()
        if t in ("1", "true", "yes", "on"):
            return True
        if t in ("0", "false", "no", "off", ""):
            return False
        return default
    if isinstance(v, (int, float)):
        return bool(v)
    return default


def _col_name(w: float) -> str:
    """波长 → 列名（``400.0`` → ``A@400.0``）。"""
    return f"A@{float(w)!s}"


def _filter_list(v) -> Optional[set]:
    """``include_groups`` / ``include_dyes`` → 小写字符串集合；空/非法 → ``None``（不过滤）。

    大小写不敏感按**字符串**比较（``"Sample"`` 与 ``"sample"`` 同组）；
    单个字符串按"只含一项"处理（``include_groups: sample`` 这种写法不至于被拆成字符）。
    """
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple, set, frozenset)):
        return None
    out = {str(x).strip().lower() for x in v if str(x).strip()}
    return out or None


def _file_of(s) -> str:
    """``scan.path`` → 文件名（缺失时给 ``"?"``，绝不抛）。"""
    p = _attr(s, "path", None)
    try:
        name = Path(p).name if p is not None else ""
    except TypeError:
        name = str(p)
    return name or "?"


def _value_at(x: np.ndarray, y: np.ndarray, target: float, *,
              tol: float, interpolate: bool) -> tuple:
    """在已排序且全为有限值的 ``(x, y)`` 上取 ``target`` 处的吸光度。

    Returns:
        ``(值, 未命中原因)``；命中时原因为空串，未命中时值为 NaN。
    """
    if interpolate:
        lo, hi = float(x[0]), float(x[-1])
        # L25（评审记录）：越界判定加 ±1e-9 容差，与 `core.extinction` 同一口径。
        # 目标波长**恰好等于端点**时，浮点往返可能让它比 hi 大一丁点，
        # 于是被误判为"超出范围"而记 NaN——同一个端点在两个模块里结论不同。
        eps = 1e-9
        if target < lo - eps or target > hi + eps:
            return float("nan"), f"超出数据范围 {lo:.2f}-{hi:.2f} nm（不外推）"
        return float(np.interp(target, x, y)), ""
    i = int(np.argmin(np.abs(x - target)))          # 等距时取首个 = 低波长
    gap = abs(float(x[i]) - target)
    if gap <= tol:                                  # <=：容差边界算命中（钉住）
        return float(y[i]), ""
    return float("nan"), (f"最近实测点 {float(x[i]):.2f} nm 偏离 {gap:.3f} nm "
                          f"> 容差 {tol:g} nm")


def _scan_row(s, wavelengths: Sequence[float], columns: Sequence[str], *,
              tol: float, interpolate: bool, dye_of, inc_groups, inc_dyes,
              issues: list) -> tuple:
    """单个文件 → ``(行 dict 或 None, 跳过原因, 未命中的 [(列名, 原因), ...])``。

    ``dye_of`` 自身的异常降级记录进 ``issues``（不中断整张表）。
    """
    name = _file_of(s)
    excl = str(_attr(s, "excluded", "") or "")
    if excl:
        return None, f"已被剔除（{excl}）", []
    wl_raw, a_raw = _attr(s, "wl", None), _attr(s, "a", None)
    if wl_raw is None or a_raw is None:
        return None, "没有光谱数据（波长或吸光度为空）", []
    try:
        x = np.asarray(wl_raw, float).ravel()
        y = np.asarray(a_raw, float).ravel()
    except (TypeError, ValueError):
        return None, "波长/吸光度无法转成数值数组", []
    if x.size != y.size:
        return None, f"波长与吸光度长度不一致（{x.size} vs {y.size}）", []
    if x.size == 0:
        return None, "光谱为空（0 个点）", []
    m = np.isfinite(x) & np.isfinite(y)             # 无效点一律不参与取点/插值
    if not m.any():
        return None, "波长与吸光度全是无效值（NaN）", []
    x, y = x[m], y[m]
    o = np.argsort(x, kind="stable")
    x, y = x[o], y[o]

    group = str(_attr(s, "group", "") or "")
    if inc_groups and group.strip().lower() not in inc_groups:
        return None, f"组「{group}」不在 include_groups 里", []
    dye = ""
    if dye_of is not None:
        try:
            v = dye_of(_attr(s, "path", None))
            dye = "" if v is None else str(v)
        except Exception as err:                    # noqa: BLE001 —— 回调坏掉不能拖垮导出
            dye = ""
            msg = f"{name}: 染料回调 dye_of 失败（{type(err).__name__}: {err}）→ dye 列留空"
            issues.append(msg)
            log.warning(msg)
    if inc_dyes and dye.strip().lower() not in inc_dyes:
        return None, f"染料「{dye}」不在 include_dyes 里", []

    t = _num(_attr(s, "t", 0.0), 0.0) or 0.0
    t_src = _num(_attr(s, "t_src", None), None)
    if t_src is None:                               # 未整定 → 原始时间即生效时间
        t_src = t
    ted = _attr(s, "t_edited", None)
    if ted is None:                                 # 鸭子类型对象没有该 property
        ted = abs(t - t_src) > 1e-9
    row = {"file": name, "group": group, "dye": dye,
           "rep": str(_attr(s, "rep", "") or ""),
           "t": float(t), "t_src": float(t_src), "t_edited": bool(ted),
           "role": str(_attr(s, "role", "") or ""), "n_points": int(x.size)}
    missed: list = []
    for w, c in zip(wavelengths, columns):
        v, why = _value_at(x, y, float(w), tol=tol, interpolate=interpolate)
        row[c] = v
        if why:
            missed.append((c, why))
    return row, "", missed


def _fill(res: dict, scans, cfg, dye_of) -> None:
    """把结果写进 ``res``（就地）；所有失败路径都只设 ``ok=False`` + 中文 ``reason``。"""
    wt = (cfg or {}).get("wavelength_table") if isinstance(cfg, dict) else None
    wt = wt if isinstance(wt, dict) else {}
    raw = wt.get("wavelengths")
    # L24（评审记录）：np.ndarray 是**合法的数值序列**，API 直调时不该被拒。
    # yaml 只会给 list，所以这条只影响程序化调用；但"一维数组不是列表"这种拒绝
    # 对使用者毫无帮助——先转成 list 再判。
    if isinstance(raw, np.ndarray):
        raw = raw.tolist()
    if raw is None or isinstance(raw, (str, bytes)) or \
            not isinstance(raw, (list, tuple)):
        res["reason"] = ("配置缺少 wavelength_table.wavelengths"
                         "（应为非空的波长数字列表，单位 nm）")
        return

    # ── 波长列：解析 → 去重 → 定序（**用户的顺序就是列序**）────────────
    issues = res["issues"]
    tol = _num(wt.get("tol_nm"), None)
    if tol is None or tol < 0:
        if wt.get("tol_nm") is not None:
            issues.append(f"tol_nm 配置无效（{wt.get('tol_nm')!r}）→ 回退 {DEF_TOL_NM:g} nm")
        tol = DEF_TOL_NM
    interp = _as_bool(wt.get("interpolate", DEF_INTERPOLATE), DEF_INTERPOLATE)
    wavelengths: list = []
    columns: list = []
    seen: set = set()
    dup: list = []
    bad = 0
    for item in raw:
        w = _num(item, None)
        if w is None:
            bad += 1
            continue
        c = _col_name(w)
        if c in seen:                               # 重复 → 只留一列（见 WARNING）
            dup.append(w)
            continue
        seen.add(c)
        wavelengths.append(w)
        columns.append(c)
    if bad:
        issues.append(f"波长列表里有 {bad} 项不是有效数字，已忽略")
    if dup:
        msg = (f"波长列表里有重复值 {[float(d) for d in dup]}"
               f"（每个只保留一列，共去重 {len(dup)} 个）")
        issues.append(msg)
        log.warning(msg)                            # 用户可见的 WARNING
    if not wavelengths:
        res["reason"] = "波长列表为空（或没有有效数字），没有可导出的列"
        return
    res["wavelengths"] = list(wavelengths)
    res["columns"] = list(columns)

    # ── 文件清单与过滤 ────────────────────────────────────────────
    if scans is None:
        items: list = []
    else:
        try:
            items = list(scans)
        except TypeError:
            items = []
            res["reason"] = "scans 不是可迭代的文件集合"
            return
    if not items:
        res["reason"] = "没有输入文件（scans 为空），无法生成 A@λ 表"
        return
    inc_groups = _filter_list(wt.get("include_groups"))
    inc_dyes = _filter_list(wt.get("include_dyes"))
    if inc_dyes and dye_of is None:
        res["reason"] = ("配置要求按染料过滤（include_dyes 非空），但调用方没有提供 "
                         "dye_of 回调，无法判断染料名")
        return

    # ── 逐文件取点 ───────────────────────────────────────────────
    n_nan = 0
    n_file_nan = 0
    for s in items:
        name = _file_of(s)
        try:
            row, skip, missed = _scan_row(
                s, wavelengths, columns, tol=tol, interpolate=interp,
                dye_of=dye_of, inc_groups=inc_groups, inc_dyes=inc_dyes,
                issues=issues)
        except Exception as err:                    # noqa: BLE001 —— 单个文件绝不拖垮整表
            row, skip, missed = None, f"处理失败（{type(err).__name__}: {err}）", []
        if row is None:
            res["skipped"].append({"file": name, "reason": skip})
            log.warning("A@λ 跳过 %s: %s", name, skip)
            continue
        res["rows"].append(row)
        if missed:
            n_nan += len(missed)
            n_file_nan += 1
            head = "；".join(f"{c}（{why}）" for c, why in missed[:_MAX_LISTED])
            tail = "" if len(missed) <= _MAX_LISTED else f" 等 {len(missed)} 个"
            msg = f"{name}: {len(missed)} 个波长没有可用数据 → {head}{tail}"
            issues.append(msg)
            log.warning(msg)
    if not res["rows"]:
        res["reason"] = (f"{len(items)} 个文件全部被跳过或过滤掉了，"
                         f"没有可导出的行（详见 skipped）")
        return

    # ── 每个波长列的统计（非 NaN 计数 + min/max/mean）──────────────
    stats: dict = {}
    for c in columns:
        v = np.asarray([r[c] for r in res["rows"]], float)
        fin = v[np.isfinite(v)]
        stats[c] = {"n": int(fin.size),
                    "min": float(fin.min()) if fin.size else float("nan"),
                    "max": float(fin.max()) if fin.size else float("nan"),
                    "mean": float(fin.mean()) if fin.size else float("nan")}
    res["stats"] = stats
    if n_nan:
        issues.append(f"共 {n_nan} 个单元格记 NaN（涉及 {n_file_nan} 个文件）："
                      f"对应波长在该文件里既没有容差内的实测点、也没有可插值的范围")
    res["ok"] = True
    res["reason"] = ""
    log.info("A@λ 表: %d 行 × %d 波长列（跳过 %d 个文件，%d 个 NaN 单元格）",
             len(res["rows"]), len(columns), len(res["skipped"]), n_nan)


# ═══════════════════════════ 公开接口 ═══════════════════════════
def absorbance_at(scans: list, cfg: dict, *,
                  dye_of: Optional[Callable] = None) -> dict:
    """把 ``scans`` 在配置波长处的吸光度摊成一张表（每行一个文件）。

    :param scans: :class:`core.io.Scan` 列表（任何鸭子类型对象也行）。
    :param cfg: 完整配置；只读 ``cfg["wavelength_table"]`` 一节
        （``wavelengths`` / ``tol_nm`` / ``interpolate`` / ``include_groups`` /
        ``include_dyes``）。**缺 wavelengths = 没有表可出**（不给隐含默认波长）；
        ``tol_nm``/``interpolate`` 缺失时回退 :data:`DEF_TOL_NM` /
        :data:`DEF_INTERPOLATE`。
    :param dye_of: 可选回调 ``callable(path) -> str``（收到 ``scan.path``），
        由调用方注入染料名解析（``core`` 层不依赖任何染料全局状态）；
        为 ``None`` 时 ``dye`` 列留空字符串。回调抛异常 → 该文件 ``dye`` 留空
        并记一条 issue，不中断导出。

    Returns:
        dict，至少含：

        * ``ok`` / ``reason``：能否出表；失败时 ``reason`` 是中文原因；
        * ``wavelengths``：去重后的波长（= 列序）；
        * ``columns``：列名（``A@400.0``，与 ``wavelengths`` 一一对应）；
        * ``rows``：每行一个文件，键为 :data:`META_FIELDS` + 各波长列
          （``n_points`` = 该谱参与取点的有效点数，NaN 点不计）；
        * ``skipped``：``[{"file", "reason"}]``，被剔除 / 无光谱 / 长度不一致 /
          容差外全空 / 被 include_* 过滤掉的文件的**中文原因**；
        * ``issues``：中文告警（重复波长、每文件缺格原因、NaN 汇总、回调失败…）；
        * ``stats``：``{列名: {"n", "min", "max", "mean"}}``（n = 非 NaN 计数；
          全 NaN 时三个统计量为 NaN）。

    **绝不抛异常**：内部任何意外都会被兜成 ``ok=False`` + ``reason``。
    """
    res: dict = {"ok": False, "reason": "", "wavelengths": [], "columns": [],
                 "rows": [], "skipped": [], "issues": [], "stats": {}}
    try:
        _fill(res, scans, cfg, dye_of)
    except Exception as err:                        # noqa: BLE001 —— GUI 不许被这里打断
        log.exception("A@λ 表计算失败")
        res["ok"] = False
        res["reason"] = f"内部错误：{type(err).__name__}: {err}"
    return res


def write_wavelength_table(result: dict, out_path) -> Path:
    """把 :func:`absorbance_at` 的结果写成 CSV（**只写主表**，stats 由调用方处置）。

    * 列序 = :data:`META_FIELDS` + 用户给的波长顺序（表头形如 ``A@400.0``）；
    * 编码 ``utf-8-sig``、换行交给 :mod:`csv`、同名原子写（与 results.csv 同风格）；
    * 数值 6 位有效数字（:data:`NUM_FMT`）；**非有限值写空串**（Excel 里是空白格，
      数值列可整列直接粘贴/作图；``"N/A"`` 会把整列变成文本）；
    * 布尔（``t_edited``）写 ``是/否``，与 results.csv 的风格一致。

    Returns:
        实际写出的 :class:`pathlib.Path`。

    Raises:
        TypeError: ``result`` 不是 dict（编程错误，早失败好过写出半张错表）；
        OSError: 目标路径不可写（调用方必须知道导出失败）。
    """
    if not isinstance(result, dict):
        raise TypeError("result 必须是 absorbance_at() 返回的 dict")
    path = Path(out_path)
    cols = result.get("columns")
    if not isinstance(cols, (list, tuple)) or not cols:
        cols = [_col_name(w) for w in (result.get("wavelengths") or [])
                if _num(w, None) is not None]
    header = [*META_FIELDS, *[str(c) for c in cols]]
    rows = [r for r in (result.get("rows") or []) if isinstance(r, dict)]
    with atomic_write(path, newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([_cell(r.get(k)) for k in header])
    if not rows:
        log.warning("A@λ 表没有数据行，只写出了表头: %s", path)
    log.info("已写出 %s（%d 行 × %d 波长列）", path, len(rows), len(cols))
    return path


def _cell(v) -> str:
    """单元格文本：布尔 → ``是/否``；非有限数 → 空串；其余转字符串。"""
    if v is None:
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "是" if bool(v) else "否"
    if isinstance(v, (int, float, np.integer, np.floating)):
        f = float(v)
        return format(f, NUM_FMT) if math.isfinite(f) else ""
    return str(v)
