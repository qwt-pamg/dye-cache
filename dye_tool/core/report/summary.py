# -*- coding: utf-8 -*-
"""V2.5 FR-29 / FR-30：批量队列汇总 + 多 run 统计报表（**纯数据、只读**）。

设计约束（设计文档 V2.5 §4）：

* 只做"读取 + 聚合"：不重新拟合、不重算数值、**不改 run 产物**（A29.3 / A30.2 红线）；
* run 发现复用 ④档案同源函数 :func:`core.report.dyes.iter_run_dirs`
  （FR-30.1：统计扫描与 ④档案必须同一套发现逻辑，防两套漂移）；
* 写 Excel 在 :mod:`core.report.excel_export`（样式单源，复用 _write_table/_styles），
  本模块**不 import openpyxl**。

公开入口
========

* :func:`collect_queue_summary` —— 队列结果（RunResult 列表）→ **每目录一行**；
* :func:`collect_statistics` —— 扫描 runs → 明细行 + 按 (染料, 组别) 聚合行；
* :func:`default_output_path` —— queue_summary_<HHMMSS>.xlsx / statistics_<HHMMSS>.xlsx
  （同秒重名自动加 -2，绝不覆盖既有文件）。
"""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from .dyes import iter_run_dirs
from .results import fmt

#: 队列汇总默认文件名前缀（设计文档 FR-29.3）
QUEUE_PREFIX = "queue_summary_"
#: 统计报表默认文件名前缀（设计文档 FR-30.2）
STATISTICS_PREFIX = "statistics_"

#: 队列汇总表头：**一个目录一行**（A29.2 判据：行数 = 目录数）。
#: 组级值（多组时）用 " | " 并排；列名尽量与 results.csv 对齐。
QUEUE_HEADERS = ["序号", "目录", "状态", "run 输出目录", "组数",
                 "组别", "角色", "k_obs(s^-1)", "k_stderr", "R2", "n",
                 "Phi_Delta", "异常摘要", "耗时(s)"]

#: 统计**明细**表头（一个 run × 一个组一行）
STAT_DETAIL_HEADERS = ["run", "染料", "组别", "角色", "k_obs(s^-1)", "k_stderr",
                       "R2", "n", "Phi_Delta", "Phi_err", "状态", "run 目录", "异常"]

#: 统计**聚合**表头（按 染料 × 组别）
STAT_HEADERS = ["染料", "组别", "run 数", "k 均值", "k sd", "Phi 均值", "Phi sd",
                "R2 最小", "k 最小 run", "k 最小", "k 最大 run", "k 最大"]

#: 内部状态 → 界面/表格中文（队列行）
_STATUS_ZH = {"ok": "完成", "failed": "失败", "cancelled": "取消",
              "skipped": "跳过", "running": "运行中"}

#: results.csv 的数值列（统计/队列取值用；列名与 CSV_HEAD 一致）
_K_COL = "k_obs(s^-1)"
_PHI_COL = "Phi_Delta"


# ═══════════════════════════ 小工具 ═══════════════════════════
def _f(v) -> Optional[float]:
    """尽力转 float；失败/非有限 → None（Excel 留空）。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _i(v) -> Optional[int]:
    """尽力转 int（计数列 n 用；Excel 里不能显示成 10.000000，审查 O2）。"""
    x = _f(v)
    return None if x is None else int(round(x))


def _txt(v) -> str:
    return "" if v is None else str(v)


def _join(vals, spec: str = ".6g") -> str:
    """把一组可选数值并排成单元格文本；None 显示空串（不写 nan）。"""
    out = []
    for v in vals:
        if v is None:
            out.append("")
        else:
            out.append(fmt(v, spec))
    return " | ".join(out)


def _mean(vals: Sequence[float]) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def _sd(vals: Sequence[float]) -> Optional[float]:
    """样本标准差（n-1）；n<2 时返回 0.0（与"均值±sd"的展示口径一致）。"""
    if not vals:
        return None
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))


#: 进程内已"预订"的输出路径（同秒连续调用、文件尚未落盘时也要避免重名）
_RESERVED_OUT: set = set()


def default_output_path(out_root, kind: str) -> Path:
    """默认输出路径（时间戳命名；**同秒重名自动加 -2，绝不覆盖**）。

    同一进程内连续调用（先取路径、后写盘）也要互不相同：除了检查磁盘上是否
    已存在，还会登记进程内"已预订"集合（A30.5 判据：同名不覆盖）。

    Args:
        out_root: 输出根目录（调用方已用 core.paths.resolve_out_root 解析）。
        kind: "queue" → queue_summary_<HHMMSS>.xlsx；"statistics" → statistics_<HHMMSS>.xlsx。
    """
    prefix = QUEUE_PREFIX if str(kind) == "queue" else STATISTICS_PREFIX
    root = Path(out_root)
    base = prefix + datetime.now().strftime("%H%M%S")
    path = root / (base + ".xlsx")
    i = 2
    while path.exists() or str(path.resolve()) in _RESERVED_OUT:
        path = root / (base + "-" + str(i) + ".xlsx")
        i += 1
    _RESERVED_OUT.add(str(path.resolve()))
    return path


# ═══════════════════════════ 读 run（只读） ═══════════════════════════
def read_results_rows(run_dir) -> tuple[list[dict], str]:
    """读一个 run 的 results.csv → (行, 错误说明)。只读，绝不写。

    判据（A30.3 降级）：文件缺失/无数据行/缺关键列 → 返回 ([], 原因)，
    由调用方把该 run 列入"异常"并跳过。
    """
    path = Path(run_dir) / "results.csv"
    if not path.is_file():
        return [], "缺少 results.csv"
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = [dict(r) for r in csv.DictReader(fh)]
    except (OSError, UnicodeDecodeError) as err:
        return [], f"读取 results.csv 失败：{type(err).__name__}: {err}"
    rows = [r for r in rows if any(_txt(v).strip() for v in r.values())]
    if not rows:
        return [], "results.csv 无数据行"
    missing = [c for c in ("组别", _K_COL) if c not in rows[0]]
    if missing:
        return [], f"results.csv 缺列：{missing}"
    return rows, ""


def read_fit_summary(run_dir) -> tuple[dict, str]:
    """读 fit_summary.json（缺失/损坏 → 空 dict + 原因；用于"坏 run 列入异常"）。"""
    path = Path(run_dir) / "fit_summary.json"
    if not path.is_file():
        return {}, "缺少 fit_summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        return {}, f"fit_summary.json 损坏：{type(err).__name__}: {err}"
    if not isinstance(data, dict):
        return {}, "fit_summary.json 顶层不是对象"
    return data, ""


# ═══════════════════════════ 队列汇总（FR-29.3） ═══════════════════════════
def _queue_group_rows(rr) -> list[dict]:
    """从 RunResult 取每组一行（组别/角色/k/k_err/R2/n/Phi/状态）。"""
    if rr is None:
        return []
    results = getattr(rr, "results", None)
    if not isinstance(results, dict):
        return []
    phi = getattr(rr, "phi", None)
    if not isinstance(phi, dict):
        phi = {}
    out: list[dict] = []
    for g in sorted(results):
        e = results.get(g) or {}
        fit = e.get("pooled")
        p = phi.get(g) or {}
        out.append({
            "group": _txt(g),
            "role": _txt(e.get("role")),
            "k": _f(getattr(fit, "k", None)),
            "k_err": _f(getattr(fit, "k_err", None)),
            "r2": _f(getattr(fit, "r2", None)),
            "n": getattr(fit, "n", None),
            "phi": _f(p.get("phi")),
            "phi_err": _f(p.get("phi_err")),
            "status": _txt(getattr(fit, "status", "")),
        })
    return out


def collect_queue_summary(entries: Sequence[dict]) -> list[dict]:
    """队列结果 → 汇总行（**每个目录一行**；失败目录也有一行并带原因）。

    Args:
        entries: 每项 = dict，键：
            data_dir（源目录）、status（ok/failed/cancelled/skipped）、
            rr（core.pipeline.RunResult 或 None）、error（失败原因）、
            duration_s（该目录耗时，可选）。

    Returns:
        行 dict 列表，键 = :data:`QUEUE_HEADERS`（可直接交给
        :func:`core.report.excel_export.write_queue_summary_workbook`）。
    """
    rows: list[dict] = []
    for i, e in enumerate(list(entries or []), 1):
        e = e if isinstance(e, dict) else {}
        gs = _queue_group_rows(e.get("rr"))
        status_raw = _txt(e.get("status") or "ok").lower()
        status = _STATUS_ZH.get(status_raw, status_raw or "完成")
        err = _txt(e.get("error"))
        if not err and status_raw not in ("ok", "cancelled"):
            err = status
        dur = _f(e.get("duration_s"))
        rows.append({
            "序号": i,
            "目录": _txt(e.get("data_dir")),
            "状态": status,
            "run 输出目录": _txt(getattr(e.get("rr"), "run_dir", "")),
            "组数": len(gs),
            "组别": " | ".join(x["group"] for x in gs),
            "角色": " | ".join(x["role"] for x in gs),
            "k_obs(s^-1)": _join([x["k"] for x in gs]),
            "k_stderr": _join([x["k_err"] for x in gs]),
            "R2": _join([x["r2"] for x in gs], ".5f"),
            "n": " | ".join("" if x["n"] is None else str(x["n"]) for x in gs),
            "Phi_Delta": _join([x["phi"] for x in gs], ".4f"),
            "异常摘要": err,
            "耗时(s)": round(dur, 2) if dur is not None else None,
        })
    return rows


# ═══════════════════════════ 多 run 统计（FR-30） ═══════════════════════════
def collect_statistics(out_root, *, dye_filter: Optional[str] = None) -> dict:
    """扫描 runs → 明细行 + 聚合行 + 异常行（**全程只读**）。

    run 发现复用 :func:`core.report.dyes.iter_run_dirs`（与 ④档案同源）。
    坏 run（缺 results.csv / 坏 JSON）不中断扫描，列入 errors（A30.3）。

    Returns:
        {"detail": [...], "stat": [...], "errors": [...], "runs": 扫描到的 run 数}
    """
    detail: list[dict] = []
    errors: list[dict] = []
    runs = 0
    for dye, run_dir in iter_run_dirs(out_root):
        if dye_filter and dye != dye_filter:
            continue
        runs += 1
        rows, err = read_results_rows(run_dir)
        if not rows:
            errors.append({"run": Path(run_dir).name, "染料": dye,
                           "run 目录": str(run_dir), "错误": err or "无数据"})
            continue
        fit, fit_err = read_fit_summary(run_dir)
        run_note = "" if fit else (fit_err if fit_err != "缺少 fit_summary.json"
                                   else "")
        run_id = Path(run_dir).name
        for r in rows:
            detail.append({
                "run": run_id,
                "染料": dye,
                "组别": _txt(r.get("组别")),
                "角色": _txt(r.get("角色")),
                "k_obs(s^-1)": _f(r.get(_K_COL)),
                "k_stderr": _f(r.get("k_stderr")),
                "R2": _f(r.get("R2")),
                "n": _i(r.get("n")),
                "Phi_Delta": _f(r.get(_PHI_COL)),
                "Phi_err": _f(r.get("Phi_err")),
                "状态": _txt(r.get("状态")),
                "run 目录": str(run_dir),
                "异常": run_note,
            })
    stat = aggregate_statistics(detail)
    return {"detail": detail, "stat": stat, "errors": errors, "runs": runs}


def aggregate_statistics(detail_rows: Sequence[dict]) -> list[dict]:
    """按 (染料, 组别) 聚合：n、k 均值±sd、Phi 均值±sd、R2 min、k 极值 run 指向。

    sd 口径：**样本标准差**（n-1；n=1 → 0.0），与"均值±sd"的展示一致；
    A30.1 的手算对照按同一口径。
    """
    buckets: dict[tuple, list[dict]] = {}
    for r in list(detail_rows or []):
        key = (_txt(r.get("染料")), _txt(r.get("组别")))
        buckets.setdefault(key, []).append(r)
    out: list[dict] = []
    for (dye, group) in sorted(buckets):
        rows = buckets[(dye, group)]
        ks = [(x.get("k_obs(s^-1)"), x) for x in rows
              if x.get("k_obs(s^-1)") is not None]
        phis = [x.get("Phi_Delta") for x in rows if x.get("Phi_Delta") is not None]
        r2s = [x.get("R2") for x in rows if x.get("R2") is not None]
        k_vals = [v for v, _ in ks]
        row = {"染料": dye, "组别": group, "run 数": len(rows)}
        if k_vals:
            km = _mean(k_vals)
            row["k 均值"] = km
            row["k sd"] = _sd(k_vals)
            row["k 最小 run"] = ks[min(range(len(ks)), key=lambda i: ks[i][0])][1].get("run")
            row["k 最小"] = min(k_vals)
            row["k 最大 run"] = ks[max(range(len(ks)), key=lambda i: ks[i][0])][1].get("run")
            row["k 最大"] = max(k_vals)
        else:
            row.update({"k 均值": None, "k sd": None, "k 最小 run": "",
                        "k 最小": None, "k 最大 run": "", "k 最大": None})
        row["Phi 均值"] = _mean(phis)
        row["Phi sd"] = _sd(phis)
        row["R2 最小"] = min(r2s) if r2s else None
        out.append(row)
    return out
