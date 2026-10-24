# -*- coding: utf-8 -*-
"""流水线编排层 —— core 对 UI 暴露的最小接口（设计文档 §6）。

对外仅暴露：

    collect_scans(data_dir, cfg, progress_cb=None) -> list[Scan]
    run_phi_pipeline(data_dir, cfg, progress_cb=None, log_cb=None) -> RunResult
    rerender_figures(scans, results, phi, controls, cfg, run_dir) -> list[Path]
    run_compare(items, group_a, group_b, cfg, normalize_exc=False) -> CompareResult
    auto_group(items, ag_cfg) -> (groups, invalid)
    load_spectra_files(paths, cfg) -> list[Spectrum]

``run_core`` / ``_fit_all`` 逐行迁移自 legacy ``singlet_o2_phi``，
只把 ``print`` 换成 ``logging``、并在阶段边界插入 ``progress_cb`` 回调。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

import numpy as np

from .grouping import auto_group, items_from_scans
from ..inputs.io import (Scan, apply_time_overrides, collect_scans, load_spectra_files,
                 preview_groups, validate_times)
from ..paths import resolve_out_root
from .kinetics import (FitResult, excitation_abs, extract_probe, fit_kinetics,
                       fit_series, group_kinetics, normalize_series,
                       subtract_background)
from .phi import calc_phi, compute_phi, judge_controls, phi_sigma
from .qc import (correct_baseline, despike, flag_negative, flag_saturation,
                 qc_wavelength)

if TYPE_CHECKING:                                       # 只用于类型标注，避免循环导入
    from ..inputs.batch_file import BatchFile

log = logging.getLogger("dye_tool.pipeline")

ProgressCb = Optional[Callable[[int, str], None]]
LogCb = Optional[Callable[[str, str], None]]

__all__ = [
    "RunResult", "CompareResult", "StackResult", "SpectraResult",
    "run_core", "run_phi_pipeline", "rerender_figures", "run_compare",
    "run_stack", "run_spectra_plots",
    "auto_group", "collect_scans", "load_spectra_files", "items_from_scans",
]


# ═══════════════════════════ 结果容器 ═══════════════════════════
@dataclass
class RunResult:
    """FR-1 全流程结果（UI 只依赖本结构与 results/phi/controls 的约定键）。"""
    scans: list[Scan] = field(default_factory=list)
    results: dict = field(default_factory=dict)
    phi: dict = field(default_factory=dict)
    controls: dict = field(default_factory=dict)
    run_dir: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)
    figures: list[Path] = field(default_factory=list)
    cfg: dict = field(default_factory=dict)
    ok: bool = True
    message: str = ""

    def figures_dir(self, export_mode: str = "preview") -> Optional[Path]:
        """本次 run 的图件目录（``figures_preview/`` 或 ``figures/``）。

        与 :func:`core.plotting_jacs.plot_all_jacs` 的选路**同源**，GUI 不必再
        自己拼目录名（拼错就会出现"跑完了却找不到图"）。
        """
        if self.run_dir is None:
            return None
        return Path(self.run_dir) / (
            "figures_preview" if str(export_mode).lower() == "preview" else "figures")


@dataclass
class CompareResult:
    """FR-2 两组 kobs 对比结果。"""
    group_a: dict = field(default_factory=dict)
    group_b: dict = field(default_factory=dict)
    fit_a: dict = field(default_factory=dict)
    fit_b: dict = field(default_factory=dict)
    series_a: dict = field(default_factory=dict)     # {t, A, label, skipped}
    series_b: dict = field(default_factory=dict)
    ratio: Optional[float] = None
    ratio_corrected: Optional[float] = None
    corr: dict = field(default_factory=dict)
    #: V2.9.1 FR-62：对比链直通 Φ_Δ（None = 未请求；旧调用 zero impact）
    phi_a: Optional[float] = None
    phi_b: Optional[float] = None
    phi_err_a: Optional[float] = None
    phi_err_b: Optional[float] = None
    phi_ref: Optional[float] = None
    ref_side: str = ""
    aex_a: Optional[float] = None
    aex_b: Optional[float] = None
    out_dir: Optional[Path] = None
    files: list[Path] = field(default_factory=list)
    ok: bool = True
    message: str = ""
    invalid: list[str] = field(default_factory=list)


# ═══════════════════════════ FR-1 核心流水线 ═══════════════════════════
def run_core(data_dir: Path | str, cfg: dict, dry: bool = False,
             progress_cb: ProgressCb = None,
             only_paths: Optional[Sequence[Path]] = None,
             time_overrides: Optional[dict] = None,
             use_cache: bool = True,
             batch: Optional["BatchFile"] = None
             ) -> tuple[list[Scan], dict]:
    """端到端数值核心：解析→QC→去尖峰→基线→(dry 则止)→背景→拟合。

    返回 ``(scans, results)``。任何单文件问题都转为 ``scan.excluded``，
    目录级问题（不存在/空/全坏）返回空结果并记日志，**不抛异常**。

    ``time_overrides``（FR-12）：``{文件名: t}``，在 QC **之前**套用，
    因此 A(t)、归一化、拟合全部吃整定后的时间；不传 = 完全按文件名解析
    （与 V1.0 逐位一致）。

    ``batch``（V2.0 δ / FR-14）：批次文件里的角色/时间整定，在 QC **之前**套用
    （与 ``time_overrides`` 同一时点、同一理由）。不传 = 完全不碰批次文件，
    行为与 V1.3 一致（机制约束 5）。**优先级**：显式 ``time_overrides`` >
    批次文件 > 文件名解析值——调用方临时改的永远赢，批次是"上次存下来的默认"。
    """
    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        log.error("数据目录不存在: %s", data_dir)
        return [], {}
    _p(2, "扫描目录")
    # collect_scans 自身按"已读/总数"输出 0–100 的**绝对**百分比；直接透传会让
    # 进度条先冲到 100% 再回落到 40%（读取阶段之后的 QC/拟合阶段）。这里把它
    # 压缩映射到本流水线的 2–35% 区间，保证整体进度单调递增。
    def _scan_progress(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(int(2 + (35 - 2) * max(0, min(100, pct)) / 100.0), stage)

    scans = collect_scans(data_dir, cfg,
                          progress_cb=_scan_progress if progress_cb else None,
                          only_paths=only_paths, use_cache=use_cache)
    if not scans:
        log.warning("目录 %s 未找到光谱文件(扩展名 %s)", data_dir,
                    cfg["io"].get("extensions"))
        return scans, {}
    if all(s.excluded for s in scans):
        log.error("所有文件解析失败, 无有效数据")
        return scans, {}
    if batch is not None:
        n = _apply_batch_safe(batch, scans, data_dir)
        if n:
            _p(36, f"批次整定 {n} 个文件")
    if time_overrides:
        apply_time_overrides(scans, time_overrides)
    if batch is not None or time_overrides:
        for issue in validate_times(scans):
            log.warning("[时间检查] %s", issue.message)
    _p(40, "QC 波长对齐")
    qc_wavelength(scans, cfg)
    flag_saturation(scans, cfg)
    _p(50, "去尖峰 / 基线校正")
    for s in scans:
        if s.excluded:
            continue
        s.a = despike(s.a, s.wl, cfg["qc"], s.path.name)
        s.a, _ = correct_baseline(s.wl, s.a, cfg["baseline"], s.path.name)
    flag_negative(scans, cfg)
    if dry:
        return scans, {}
    _p(62, "背景扣除")
    subtract_background(scans, cfg["background"])
    _p(70, "动力学拟合")
    return scans, _fit_all(scans, cfg)


def _apply_batch_safe(batch: "BatchFile", scans: list[Scan],
                      data_dir: Path) -> int:
    """把批次整定套到 scans 上；**任何问题都不许中断分析**（FR-14 机制约束 4）。

    批次文件是可选增强（约束 5）：读坏了、路径对不上、字段非法，都只记日志并
    跳过，绝不把"整份分析"拖垮——用户明明有数据可算。
    """
    try:
        from ..inputs.batch_file import apply_to_scans
        return apply_to_scans(batch, scans, data_dir)
    except Exception as err:                            # noqa: BLE001 —— 刻意的
        log.warning("批次整定套用失败（按无批次文件继续）：%s", err)
        return 0




def _apply_reference_group(results: dict, reference_group: str) -> None:
    """V2.9.1 FR-61：把显式参比组写进结果角色（UI 指定优先于前缀）。

    只改结果字典里的 ``role``（compute_phi/judge_controls 的输入），不改公式；
    指定组不存在时 WARNING 并按原角色继续（不静默错位）。
    """
    ref = str(reference_group or "").strip()
    if not ref:
        return
    if ref not in results:
        log.warning("显式参比组 %r 不在结果里（按原角色继续）", ref)
        return
    for g, e in results.items():
        if g == ref:
            e["role"] = "reference"
        elif e.get("role") == "reference":
            e["role"] = "sample"
    log.info("显式参比组：%s（UI 指定优先于文件名/角色前缀）", ref)


def _fit_all(scans: list[Scan], cfg: dict) -> dict:
    """汇总时间序列 → dark 漂移校正 → 逐重复拟合 + 逐组合并拟合。

    与 legacy ``_fit_all`` 逐行一致；``entry`` 的键（role/reps/irradiance/
    a_ex_auto/A0_pooled/k_per_I/pooled）是 GUI 结果表与 results.csv 的契约。
    """
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
                log.warning("组 %s(重复 %s): 存在重复时间戳 %s", g, rep, dup.tolist())
    for e in [e for e in groups.values() if e["role"] == "dark"]:   # F3 漂移校正
        ts = np.concatenate([r["t"] for r in e["reps"].values()])
        A = np.concatenate([r["A"] for r in e["reps"].values()])
        m = np.isfinite(A)
        if int(m.sum()) >= 4 and float(np.ptp(ts[m])) > 0:
            from scipy import stats
            lr = stats.linregress(ts[m], A[m])
            if lr.pvalue < 0.05 and lr.slope != 0:
                log.warning("F3: dark 组漂移斜率 %.3e AU/s(p=%.3g) → 各样品组线性校正",
                            lr.slope, lr.pvalue)
                for e2 in groups.values():
                    if e2["role"] in ("dark", "background"):
                        continue
                    for r in e2["reps"].values():
                        r["A"] = r["A"] - lr.slope * (r["t"] - r["t"][0])
    lo, hi = cfg["qc"].get("a0_range", [0.05, 1.5])
    floor = float(kin.get("a_floor", 0.01))
    dose = bool(kin.get("dose_normalize", False))
    results: dict = {}
    for g, e in sorted(groups.items()):
        irr = kin.get("irradiance", {}) or {}
        I_g = float(irr.get(g, irr.get("default", 1.0)))
        entry = {"role": e["role"], "reps": {}, "irradiance": I_g,
                 "a_ex_auto": float("nan")}
        if g not in (qy.get("a_ex") or {}):        # A_ex 备用: 最早光谱 λ_ex 处
            s0 = e["first"]
            iex = int(np.argmin(np.abs(s0.wl - float(wl_cfg.get("excitation", 532.0)))))
            entry["a_ex_auto"] = float(s0.a[iex]) if np.isfinite(s0.a[iex]) \
                else float("nan")
        for rep, r in e["reps"].items():
            tag = f"{g}_{rep}" if rep else g
            A0, R = normalize_series(r["t"], r["A"], cfg["a0"], tag, a_floor=floor)
            r.update({"R": R, "A0": A0})
            if not (np.isfinite(A0) and lo <= A0 <= hi):
                msg = f"A(0)={A0:.3f} 超出合理区间 [{lo:g},{hi:g}]"
                log.warning("%s: %s → FAILED", tag, msg)
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
        entry["k_per_I"] = (float(p.k / I_g)
                            if (p.status != "FAILED" and I_g) else float("nan"))
        if p.status != "FAILED" and I_g != 1.0:
            log.info("%s: 辐照度归一 k/I=%.4e (I=%.3f mW/cm²)", g, p.k / I_g, I_g)
        entry["reps"] = e["reps"]
        results[g] = entry
    return results


def resolve_run_target(cfg: dict, groups: Sequence[str],
                       out_root: Path | str,
                       subdir: Optional[str] = None) -> tuple:
    """由参与组名解析出 ``(染料名, dir_scheme, 根目录)``（FR-11.3）。

    * 只解析出**一个**染料名 → 该名字；
    * 多个染料 → ``_multi``（Q17：多染料 run 落 ``_multi/<时间戳>/``）；
    * 解析不出组名 → ``_multi``；
    * ``dye.enabled=false`` 或 ``dir_scheme="flat"`` → 回到 V1.0 的扁平方案。
    """
    from ..report.dyes import MULTI_DIR, dye_for_group
    dcfg = (cfg or {}).get("dye") or {}
    scheme = str(dcfg.get("dir_scheme", "dye") or "dye")
    root = Path(out_root)
    if subdir:
        root = root / subdir
    if not bool(dcfg.get("enabled", True)):
        return MULTI_DIR, "flat", root
    names = sorted({dye_for_group(cfg, g) for g in (groups or []) if g})
    if len(names) == 1:
        return names[0], scheme, root
    return MULTI_DIR, scheme, root


def make_figure_run_dir(cfg: dict, groups: Sequence[str],
                        out_root: Optional[Path | str] = None,
                        subdir: Optional[str] = None) -> Path:
    """FR-2/3/4 共用的输出目录：按 ``dye.dir_scheme`` 归档（FR-11.7）。

    ``subdir`` 保留 V1.0 的功能分层（``stack`` / ``compare`` / ``spectra``），
    染料层叠加在它下面：``runs/stack/<染料>/<时间戳>/``。
    """
    from ..report.dyes import make_run_dir as dye_make
    # V2.5 D10：唯一解析在 core.paths.resolve_out_root（相对 → app_root）。
    root = resolve_out_root(cfg, out_root)
    dye, scheme, base = resolve_run_target(cfg, groups, root, subdir)
    return dye_make(base, dye=dye, scheme=scheme)


def _register_history(cfg: dict, run_dir: Path, run_id: str, dye_name: str,
                      scans: Sequence[Scan], results: dict, phi: dict) -> None:
    """把本次 run 登记进染料档案 ``history.csv``（FR-11.4）。

    单染料 → ``runs/<染料>/history.csv`` 一行；
    多染料（``_multi``）→ 涉及到的每个染料目录各登记一行（Q17）。
    任何失败只记日志，**绝不影响主流程**。
    """
    from ..report.dyes import (MULTI_DIR, append_history, dye_for_group, history_row)
    try:
        dcfg = (cfg or {}).get("dye") or {}
        if not bool(dcfg.get("history", True)) or not dye_name:
            return
        if str(dcfg.get("dir_scheme", "dye")) == "flat":
            return          # 扁平方案没有染料目录，档案无处可放（Q20 兼容出口）
        n_files = len(scans)
        n_used = sum(1 for s in scans if not s.excluded)
        n_time = sum(1 for s in scans if getattr(s, "t_edited", False))
        good = [s for s in scans if not s.excluded and s.role != "background"]
        if dye_name == MULTI_DIR:
            per: dict[str, set] = {}
            for s in good:
                per.setdefault(dye_for_group(cfg, s.group), set()).add(s.group)
            for name, gs in sorted(per.items()):
                row = history_row(run_id, run_dir, name, sorted(gs), results,
                                  phi, n_files, n_used, n_time)
                append_history(run_dir.parent.parent / name, row)
        else:
            groups = sorted({s.group for s in good})
            row = history_row(run_id, run_dir, dye_name, groups, results, phi,
                              n_files, n_used, n_time)
            append_history(run_dir.parent, row)
    except Exception as err:                           # noqa: BLE001
        log.warning("染料档案登记失败（不影响本次结果）: %s", err)


def _maybe_write_excel(rr, cfg: dict, run_dir: Path | None) -> None:
    """P5：把本次 run 汇总成一个多 sheet 工作簿（失败不影响主流程）。

    为什么放在 pipeline 而不是 CLI/GUI：Excel 是"交付格式"，**GUI 与命令行
    两条路都该拿到**，写在编排层只需维护一处。openpyxl 是可选依赖——缺失或
    任何写入失败都只记 WARNING，绝不让"导不出 Excel"把一次成功的分析判成失败。
    """
    if run_dir is None:
        return
    ex = cfg.get("excel") or {}
    if not bool(ex.get("enabled", True)):
        return
    try:
        from ..report.excel_export import export_workbook
        p = export_workbook(rr, cfg)
        if p:
            log.info("P5: 结果工作簿已写出 %s", p)
    except ImportError as err:
        log.warning("P5: Excel 导出跳过（缺少 openpyxl）: %s", err)
    except Exception as err:                           # noqa: BLE001
        log.warning("P5: Excel 导出失败（不影响本次结果）: %s", err)


def run_phi_pipeline(data_dir: Path | str, cfg: dict,
                     progress_cb: ProgressCb = None,
                     log_cb: LogCb = None,
                     only_paths: Optional[Sequence[Path]] = None,
                     out_root: Optional[Path | str] = None,
                     write: bool = True,
                     export_mode: str = "final",
                     time_overrides: Optional[dict] = None,
                     group_dye: Optional[dict] = None,
                     only: Optional[Sequence[str]] = None,
                     debug: bool = False,
                     batch: Optional["BatchFile"] = None,
                     presets_path: Optional[Path | str] = None,
                     reference_group: str = "",
                     phi_ref: Optional[float] = None) -> RunResult:
    """FR-1 全流程：数值核心 + Φ_Δ + 对照判定 + 出图 + 导出 run 目录。

    与 legacy ``run_core`` + ``compute_phi`` + ``judge_controls`` +
    ``plot_all`` + 三个写出函数等价的**单次调用**；每个阶段都通过
    ``progress_cb(percent, stage)`` 报告进度。

    V1.1 增量：

    * ``export_mode``：``"preview"``（默认给 GUI 用，低 dpi png、不写图注）
      或 ``"final"``（默认值，CLI 与既有测试行为不变）；
    * ``time_overrides``（FR-12）：``{文件名: t}``，分析前的时间整定；
    * ``group_dye``（FR-11.5）：``{组名: 染料名}`` 会话命名，决定输出目录与图内标注；
    * 输出目录按 ``dye.dir_scheme`` 归档到 ``runs/<染料>/<时间戳>/``，
      并写出 ``run_params.yaml`` / ``time_overrides.yaml`` 与染料 ``history.csv``。

    V1.2 增量：

    * ``only``（P1 懒渲染）：只出这些图步骤（``{"a","b",...}``），默认 ``None``
      = 全部（**CLI 与既有行为一字不变**）。GUI 预览档传 ``{"a"}`` 让首图秒出，
      其余交给 :func:`rerender_figures` 在后台补齐；legacy 风格会忽略它。
    * ``debug``（O11）：把 run.log 与控制台降到 DEBUG 级。CLI 的 ``--debug``
      原来只对 ``--dry-run`` 生效（主流程硬编码 ``debug=False``），这是修它。

    V2.0 δ（FR-14）增量：

    * ``batch``：``dye_batch.yaml`` 的批次整定（角色/时间）。在 QC **之前**套用，
      优先级低于显式 ``time_overrides``。不传 = 完全不碰批次文件（行为同 V1.3）。
    * ``reference_group``（V2.9.1 FR-61）：显式参比组名；只覆盖结果 role，
      不改公式/数值。空串 = 沿用既有 ref- 前缀/角色自动识别（2.6.0 行为逐位不变）。
    """
    from ..render import plotting
    from ..report.dyes import (make_run_dir as dye_run_dir, write_run_params,
                       write_time_overrides)
    from ..report.results import (LogCapture, attach_logging,
                          write_fit_summary, write_report, write_results_csv)

    data_dir = Path(data_dir)
    cfg = cfg or {}
    # V2.4 FR-26.5：批次文件里的 `analysis_preset` 在这里统一套用——GUI（flow/classic）
    # 与 CLI 都经 `batch=` 走同一条链路，天然双生效；模板缺失/坏 → WARNING 降级不阻塞。
    # 套用后的 cfg 是**深拷贝**（apply_* 保证），调用方传入的 cfg 不被污染。
    if batch is not None:
        try:
            from ..render.plotstyle import apply_batch_analysis_preset
            _pcfg, _pissues, _papplied = apply_batch_analysis_preset(
                cfg, batch, presets_path)
        except Exception as err:                       # noqa: BLE001 —— 模板不得废分析
            log.warning("[批次] 分析模板套用失败（按当前参数继续）：%s", err)
            _papplied = False
        else:
            if _papplied:
                cfg = _pcfg
                log.info("[批次] 已套用分析模板 %r（来自 %s）",
                         getattr(batch, "analysis_preset", ""), batch.path or "dye_batch.yaml")
            _tpl_warnings = [f"[模板] {getattr(_it, 'key', '')}: "
                             f"{getattr(_it, 'message', _it)}" for _it in _pissues]
            for _it in _pissues:
                _lvl = getattr(_it, "level", "WARNING")
                log.log(logging.WARNING if _lvl != "INFO" else logging.INFO,
                        "[模板] %s: %s", getattr(_it, "key", ""),
                        getattr(_it, "message", _it))
    if group_dye:
        cfg = dict(cfg)
        cfg["_dye_session"] = dict(group_dye)
    # V2.9.1 O1 修复：Φ_ref 优先级 = 显式参数（UI 会话）> dye_batch.yaml > config
    _eff_phi = phi_ref
    if _eff_phi is None and batch is not None and getattr(batch, "phi_ref", None) is not None:
        _eff_phi = float(batch.phi_ref)
        log.info("[批次] Φ_ref=%.4g（来自 dye_batch.yaml）", _eff_phi)
    if _eff_phi is not None:
        cfg = dict(cfg)
        qy = dict(cfg.get("quantum_yield") or {})
        qy["phi_ref"] = float(_eff_phi)
        cfg["quantum_yield"] = qy
    # V2.5 D10：唯一解析在 core.paths.resolve_out_root（相对 → app_root）。
    out_root = resolve_out_root(cfg, out_root)

    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    _p(0, "初始化")
    run_dir: Optional[Path] = None
    run_id = ""
    dye_name = ""
    if write:
        # 建目录前先只看文件名确定染料归属（FR-11.3：目录名必须先定）
        try:
            groups = preview_groups(data_dir, cfg, only_paths=only_paths)
            dye_name, scheme, root = resolve_run_target(cfg, groups, out_root)
            run_dir = dye_run_dir(root, dye=dye_name, scheme=scheme)
            run_id = run_dir.name
        except Exception as err:                       # noqa: BLE001 —— O15
            # 建目录失败（只读盘、路径过长、权限不足…）原来会裸 OSError 冒泡，
            # 违背"目录级问题不抛异常"的契约。现在返回可读的失败结果。
            log.error("无法创建输出目录（%s: %s）→ 本次不写出任何文件",
                      type(err).__name__, err)
            return RunResult(run_dir=None, cfg=cfg, ok=False,
                             message=f"无法创建输出目录：{err}")
    # 用增量式装配：不清空外层（GUI）已挂的日志 handler，保证 UI 日志区不断流
    cap: LogCapture = attach_logging(
        (run_dir / "run.log") if write else None, debug=bool(debug),
        log_cb=log_cb)

    rr = RunResult(run_dir=run_dir, cfg=cfg)
    try:
        scans, results = run_core(data_dir, cfg, progress_cb=progress_cb,
                                  only_paths=only_paths,
                                  time_overrides=time_overrides, batch=batch)
        rr.scans, rr.results = scans, results
        if not scans:
            rr.ok, rr.message = False, "未找到可用光谱文件"
            log.error("%s", rr.message)
            return rr
        if not results:
            rr.ok, rr.message = False, "没有可拟合的数据（见日志）"
            log.error("%s", rr.message)
            return rr
        _p(78, "计算 Φ_Δ")
        _apply_reference_group(results, reference_group)
        rr.phi = compute_phi(results, cfg)
        rr.controls = judge_controls(results, cfg)
        if write:
            _p(84, "写出结果表")
            write_results_csv(run_dir / "results.csv", results, rr.phi, rr.controls)
            write_fit_summary(run_dir / "fit_summary.json", scans, results,
                              rr.phi, rr.controls, cfg)
            if bool((cfg.get("dye") or {}).get("params_snapshot", True)):
                write_run_params(run_dir, cfg, extra={
                    "data_dir": str(data_dir), "export_mode": str(export_mode),
                    "dye": dye_name})
            if time_overrides:
                write_time_overrides(run_dir, time_overrides)
        _p(88, "绘制图 a–e")
        figs: list[Path] = []
        try:
            figs = plotting.plot_all(scans, results, rr.phi, rr.controls, cfg,
                                     run_dir or out_root, want_paths=True,
                                     export_mode=export_mode,
                                     group_dye=group_dye, only=only) or []
        except Exception as err:                       # noqa: BLE001 —— 出图失败不废数据
            log.error("绘图阶段失败（数值结果仍有效）: %s", err)
            import traceback
            log.debug("%s", traceback.format_exc())
        rr.figures = figs
        if write:
            _p(96, "写报告")
            write_report(run_dir / "report.md", data_dir, scans, results, rr.phi,
                         rr.controls, cfg, cap.records)
            _register_history(cfg, run_dir, run_id, dye_name, scans, results,
                              rr.phi)
            _maybe_write_excel(rr, cfg, run_dir)
    except Exception as err:                           # noqa: BLE001 —— 顶层兜底
        # S2：这里原来**只有 finally 没有 except**，于是 core 层任何异常（除零、
        # 坏配置…）都会穿透到 GUI/CLI。设计文档 §7 要求"顶层异常捕获并给出可读
        # 信息"，而 core 的契约是"返回 RunResult 说清楚为什么失败，不抛异常"。
        rr.ok = False
        rr.message = f"分析中断：{type(err).__name__}: {err}"
        log.error("%s", rr.message)
        import traceback
        log.debug("%s", traceback.format_exc())
    finally:
        # V2.4-A：模板套用发生在 attach_logging 之前（要用 cfg 跑 run_core），
        # 那几条 WARNING 不在 cap.records 里——显式并进 rr.warnings，A26.4 可见。
        rr.warnings = list(cap.records) + list(locals().get("_tpl_warnings", []))
        cap.detach()
    _p(100, "完成" if rr.ok else "失败")
    return rr


def rerender_figures(scans, results: dict, phi: dict, controls: dict, cfg: dict,
                     run_dir: Path | str, export_mode: str = "preview",
                     only: Optional[Sequence[str]] = None,
                     group_dye: Optional[dict] = None,
                     groups: Optional[Sequence[str]] = None,
                     progress_cb: ProgressCb = None) -> list[Path]:
    """在**已有** run 目录里补渲/重渲图件（GUI「后台补渲」「定稿出图」用，P1）。

    与 :func:`run_phi_pipeline` 的区别：只跑绘图阶段——不重新读文件、不重算
    k 与 Φ_Δ、不重写 csv/json/report/history。数值结果因此与用户看到的那一份
    **完全相同**，这也是"定稿图 = 预览图同源"的保证。

    Args:
        only: 只补渲这些步骤；``None`` = 全部（定稿用）。
        groups: 只补渲这些组（P2 多进程按组切分任务用；``None`` = 全部组）。
        export_mode: ``"preview"`` → ``figures_preview/``；``"final"`` → ``figures/``。

    Returns:
        本次写出的文件路径列表（单张图失败不抛异常，只在日志里报错）。
    """
    from ..render import plotting
    made: list[Path] = []
    try:
        made = plotting.plot_all(scans, results, phi, controls, cfg, Path(run_dir),
                                 want_paths=True, export_mode=export_mode,
                                 only=only, group_dye=group_dye,
                                 groups=groups) or []
    except Exception as err:                           # noqa: BLE001 —— 出图失败不废数据
        log.error("补渲失败（既有结果不受影响）: %s", err)
        import traceback
        log.debug("%s", traceback.format_exc())
    if progress_cb:
        progress_cb(100, "出图完成")
    return made


def _render_figures_parallel_decision(cfg: dict, want, results: dict, scans,
                                      export_mode: str = "preview",
                                      groups=None) -> bool:
    """这批出图会不会走多进程（P2）？—— 给测试/诊断用的**只读**判断。

    真值来源是 :func:`core.plotting_jacs._should_parallel`，这里只是把 core 内部
    判断暴露成 pipeline 的公共名字，避免测试去 import 私有函数。
    """
    try:
        from ..render.plotting_jacs import _should_parallel
        return bool(_should_parallel(dict((cfg or {}).get("plot", {}).get("jacs")
                                          or {}), set(want or ()), results, scans,
                                     export_mode, groups))
    except Exception:                                   # noqa: BLE001
        return False


# ═══════════════════════════ FR-2 对比流程 ═══════════════════════════
def _group_names(group: Optional[dict]) -> list[str]:
    """分组字典 → 其中的**组名**列表（``members[].group``，去重保序）。

    ``auto_group`` 给的 ``label`` 只是"文件名公共前缀"的建议标签（用于展示），
    与 ``Scan.group``（决定染料归属与输出目录）**不是一回事**，所以这里必须取
    成员的 ``group`` 字段。成员没有该字段时退回 ``label``，保证永不返回空。
    """
    g = group or {}
    names: list[str] = []
    for m in g.get("members") or []:
        nm = str((m or {}).get("group") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    if not names and str(g.get("label") or "").strip():
        names.append(str(g["label"]).strip())
    return names


def run_directory(data_dir, cfg: dict, *, only_paths=None, out_root=None,
                  write: bool = True, export_mode: str = "preview",
                  time_overrides=None, group_dye=None, only=None,
                  progress_cb: ProgressCb = None, log_cb: LogCb = None,
                  batch=None, debug: bool = False,
                  reference_group: str = "",
                  phi_ref: Optional[float] = None) -> "RunResult":
    """**单目录完整运行编排**（V2.5 FR-29.2 / A29.3 同源 helper）。

    GUI 的分析页（``ui.pages.phi_page.PhiPage``）与批量队列
    （``ui.shell.analysis_page.BatchQueuePanel``）**共用这一处**，防止"队列一套
    载入逻辑、手动一套"的口径漂移（设计文档 Q-D4 / 风险表"单目录口径漂移"）。

    与 :func:`run_phi_pipeline` 的唯一区别：``batch=None`` 时**从目录自动加载**
    ``dye_batch.yaml``（宽进/只读；问题逐条经 ``log_cb`` 上报），再交给
    ``run_phi_pipeline(batch=)`` 统一套用 ``analysis_preset``（V2.4 FR-26.5）。
    其余参数原样透传——**不复制任何数值/渲染逻辑**（渲染路径零改动）。
    """
    from ..inputs.batch_file import load_batch
    issues: list = []
    if batch is None:
        batch = load_batch(data_dir, issues)
    if log_cb:
        for it in issues:
            log_cb(str(getattr(it, "level", "WARNING")),
                   f"[批次文件] {getattr(it, 'message', it)}")
    return run_phi_pipeline(
        data_dir, cfg, progress_cb=progress_cb, log_cb=log_cb,
        only_paths=only_paths, out_root=out_root, write=write,
        export_mode=export_mode, time_overrides=time_overrides,
        group_dye=group_dye, only=only, debug=debug, batch=batch,
        reference_group=reference_group, phi_ref=phi_ref)


def run_compare(items: Sequence[dict], group_a: dict, group_b: dict, cfg: dict,
                normalize_exc: bool = False,
                out_dir: Optional[Path | str] = None,
                progress_cb: ProgressCb = None,
                log_cb: LogCb = None,
                ref_side: str = "A",
                phi_ref: Optional[float] = None) -> CompareResult:
    """FR-2: 两组 A(t)@探针波长 → exp2/exp3 自动选择 → k 比值 + 图 + CSV。

    对应 legacy ``compare_kobs.flow_compare`` 的非交互部分。
    ``normalize_exc`` 对应 legacy 的"按激发波长吸光度归一"（``f_abs < 0.01`` 时禁用）。
    """
    from ..report.results import attach_logging
    from ..render.plotting import apply_plot_style, make_compare_figure
    from ..render.plotting import jacs_stack_figure as legacy_stack_figure
    from ..render.plotting import plot_style as _plot_style

    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    cap = attach_logging(None, debug=False, log_cb=log_cb)
    res = CompareResult(group_a=group_a, group_b=group_b)
    try:
        if out_dir is None:
            # S4：``group_a/group_b`` 是**分组字典**（``{members, lam_max, label}``），
            # 不是组名。原实现把字典直接当组名喂给 dye_for_group → 注册表非空时
            # TypeError、空注册表时生成 `{'members'- [{'name'…` 这种垃圾目录名。
            # 组名要从成员的 ``group`` 字段取（与 FR-1 的染料归属同一真相）。
            gs = _group_names(group_a) + _group_names(group_b)
            out_dir = make_figure_run_dir(cfg, gs, subdir="compare")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        lam = float(cfg["wavelength"]["probe"])

        _p(10, "提取 A(t)@探针波长")
        tA, AA, skA = group_kinetics(group_a, cfg)
        tB, AB, skB = group_kinetics(group_b, cfg)
        res.series_a = {"t": tA, "A": AA, "label": group_a["label"], "skipped": skA}
        res.series_b = {"t": tB, "A": AB, "label": group_b["label"], "skipped": skB}
        for g, sk in ((group_a, skA), (group_b, skB)):
            if sk:
                log.warning("[%s] %d 个文件在 %.0f nm 无有效数据, 不参与拟合: %s",
                            g["label"], len(sk), lam, ", ".join(sk))
        if len(tA) < 4 or len(tB) < 4:
            res.ok = False
            res.message = "至少一组可用点 <4, 无法对比"
            log.error("%s", res.message)
            return res

        _p(35, "拟合 exp2/exp3")
        fA = fit_series(tA, AA)
        fB = fit_series(tB, AB)
        res.fit_a, res.fit_b = fA, fB

        corr: dict = {}
        if normalize_exc:
            corr = {1: excitation_abs(group_a, cfg), 2: excitation_abs(group_b, cfg)}
            if any(v[1] < 0.01 for v in corr.values()):
                log.warning("激发波长吸光度太低或无数据(f_abs<0.01), 无法可靠归一, 已跳过")
                corr, normalize_exc = {}, False
        res.corr = corr

        res.ratio = (fA["k"] / fB["k"]) if (fA["ok"] and fB["ok"]) else None
        res.ratio_corrected = ((fA["k"] / corr[1][1]) / (fB["k"] / corr[2][1])
                               if (normalize_exc and res.ratio) else None)

        # V2.9.1 FR-62：对比链直通 Φ_Δ（复用 phi.calc_phi/phi_sigma，不复制公式）
        res.ref_side = "B" if str(ref_side).upper() == "B" else "A"
        if phi_ref is not None:
            res.phi_ref = float(phi_ref)
            if res.phi_ref <= 0:
                log.warning("Φ_ref=%r ≤ 0 → Φ 列按 NaN（不阻断对比）", phi_ref)
            elif not (fA["ok"] and fB["ok"]):
                log.warning("至少一组拟合失败 → Φ 列按 NaN")
            else:
                try:
                    aex_a = excitation_abs(group_a, cfg)[0]
                    aex_b = excitation_abs(group_b, cfg)[0]
                    res.aex_a, res.aex_b = aex_a, aex_b
                    qy = cfg.get("quantum_yield") or {}
                    mode = str(qy.get("iabs_mode", "exact"))
                    if aex_a is None or aex_b is None:
                        log.warning("激发波长吸光度缺失 → Φ 列按 NaN")
                    elif res.ref_side == "A":
                        phi = calc_phi(fB["k"], fA["k"], aex_b, aex_a,
                                       res.phi_ref, mode)
                        sig = phi_sigma(phi, fB["k"], fB["k_err"], fA["k"],
                                        fA["k_err"], aex_b, aex_a, res.phi_ref,
                                        float(qy.get("phi_ref_err", 0.05)),
                                        float(qy.get("a_ex_err", 0.005)), mode)
                        res.phi_b, res.phi_err_b = phi, sig
                        res.phi_a, res.phi_err_a = res.phi_ref, 0.0
                    else:
                        phi = calc_phi(fA["k"], fB["k"], aex_a, aex_b,
                                       res.phi_ref, mode)
                        sig = phi_sigma(phi, fA["k"], fA["k_err"], fB["k"],
                                        fB["k_err"], aex_a, aex_b, res.phi_ref,
                                        float(qy.get("phi_ref_err", 0.05)),
                                        float(qy.get("a_ex_err", 0.005)), mode)
                        res.phi_a, res.phi_err_a = phi, sig
                        res.phi_b, res.phi_err_b = res.phi_ref, 0.0
                except Exception as err:                    # noqa: BLE001
                    log.warning("Φ 列计算失败（对比结果仍有效）：%s", err)

        _p(60, "绘制对比图")
        apply_plot_style(cfg)
        jacs = _plot_style(cfg) == "jacs"
        caps: list = []
        if jacs:
            from ..render.plotting_jacs import jacs_stack_figure as stack_figure
        else:
            stack_figure = legacy_stack_figure
        # 注意：必须 extend 而非 append —— 这些函数返回路径列表
        res.files += list(make_compare_figure(
            {"label": group_a["label"], "t": tA, "A": AA}, fA,
            {"label": group_b["label"], "t": tB, "A": AB}, fB,
            res.ratio, out_dir, lam, cfg=cfg, captions=caps) or [])
        for i, g in ((1, group_a), (2, group_b)):
            entries = [(m["t"], m["wl"], m["ab"]) for m in g["members"]]
            if jacs:
                res.files += list(stack_figure(
                    entries, lam, out_dir, cfg=cfg,
                    tag=f"{i}_{_safe(g['label'])}", captions=caps) or [])
            else:
                res.files += list(stack_figure(
                    entries, lam, out_dir, f"{i}_{_safe(g['label'])}") or [])
        if jacs and caps:
            from ..render.plotting_jacs import write_captions
            write_captions(caps, out_dir)
        _p(85, "写出 compare_summary.csv")
        res.files.append(_write_compare_csv(out_dir, group_a, fA, group_b, fB,
                                            corr.get(1), corr.get(2),
                                            phi_a=res.phi_a, phi_b=res.phi_b,
                                            phi_err_a=res.phi_err_a,
                                            phi_err_b=res.phi_err_b,
                                            ref_side=res.ref_side,
                                            phi_ref=res.phi_ref))
        res.out_dir = out_dir
    except Exception as err:                           # noqa: BLE001
        res.ok = False
        res.message = f"对比流程失败: {err}"
        log.error("%s", res.message)
        import traceback
        log.debug("%s", traceback.format_exc())
    finally:
        cap.detach()
    _p(100, "完成")
    return res


def _safe(s: str) -> str:
    """文件名安全化（legacy ``compare_kobs._safe``）。

    ⚠ **这不是** :func:`core.results.safe` 的重复实现，而是**另一份契约**
    （评审记录「重复逻辑 #1」把它当成三套重复之一，实测澄清如下）：

    ==================  ==========================  ==============================
    输入                 ``results.safe``            本函数
    ==================  ==========================  ==============================
    ``""``              ``""``                      ``"series"``
    超长（80 个 a）      原样保留                      截断到 30
    ``nul\\x00byte``     ``"nul_byte"``              原样（NUL 不在字符类里）
    ``中文-组-90+5``     ``"中文-组-90_5"``           ``"中文-组-90+5"``（保留 ``+``）
    ==================  ==========================  ==============================

    11 组样本实测 **4 组不同**。合并成一份会**改变 compare 的输出文件名**
    （产品可见），所以保持独立，并在此写清差异以防再次被误判为重复。
    """
    import re
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:30] or "series"


def _write_compare_csv(out_dir: Path, gA: dict, fA: dict, gB: dict, fB: dict,
                       corrA, corrB, phi_a: Optional[float] = None,
                       phi_b: Optional[float] = None,
                       phi_err_a: Optional[float] = None,
                       phi_err_b: Optional[float] = None,
                       ref_side: str = "",
                       phi_ref: Optional[float] = None) -> Path:
    """``compare_summary.csv``（legacy ``compare_kobs._write_csv``）。

    表头在 legacy 基础上保留 ``core_lambda_max_nm``，并按设计文档 §5 附带
    ``k_ratio`` 列，便于 FR-2 直接引用比值。
    """
    import csv
    p = Path(out_dir) / "compare_summary.csv"
    with open(p, "w", newline="", encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["label", "core_lambda_max_nm", "n", "t_min_s", "t_max_s",
                    "model", "A0", "k_obs_s^-1", "k_err", "c", "R2",
                    "A_exc", "f_abs_exc", "k_corr_s^-1", "k_ratio",
                    "Phi_delta", "Phi_err", "ref_side"])
        ratio = (fA["k"] / fB["k"]) if (fA["ok"] and fB["ok"]) else None
        def _phi_txt(v) -> str:
            try:
                return f"{float(v):.8g}" if v is not None and np.isfinite(float(v)) else ""
            except (TypeError, ValueError):
                return ""

        for g, f, cx, ph, pe in ((gA, fA, corrA, phi_a, phi_err_a),
                                 (gB, fB, corrB, phi_b, phi_err_b)):
            ts = [m["t"] for m in g["members"]]
            A_exc, fabs = cx if cx else (None, None)
            kcorr = f["k"] / fabs if (fabs and f["ok"]) else None
            w.writerow([g["label"], g["lam_max"], len(ts),
                        f"{min(ts):.0f}", f"{max(ts):.0f}",
                        f["model"] or "FAILED", f["A0"], f["k"], f["k_err"],
                        f["c"], f["R2"], A_exc, fabs, kcorr,
                        f"{ratio:.6g}" if ratio else "",
                        _phi_txt(ph), _phi_txt(pe),
                        (ref_side or "") if phi_ref is not None else ""])
    log.info("已写出 %s", p)
    return p


# ═══════════════════════════ V2.9.3 FR-65：单线态氧工作台 ═══════════════════════════
@dataclass
class WorkbenchResult:
    """V2.9.3 FR-65：N 组逐样 k_obs + Φ_Δ 工作台结果。"""
    ok: bool = True
    message: str = ""
    out_dir: Optional[Path] = None
    groups: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    ref_label: str = ""
    phi_ref: Optional[float] = None


def run_workbench(items: Sequence[dict], groups: Sequence[dict], cfg: dict,
                   ref_label: str = "", phi_ref: Optional[float] = None,
                   out_dir: Optional[Path | str] = None,
                   style: str = "bar", error_bars: bool = True,
                   time_unit: str = "s", annotate: bool = False,
                   transparent: bool = False, formats=("png",),
                   ci_band: bool = False, bootstrap: bool = False,
                   progress_cb: ProgressCb = None,
                   log_cb: LogCb = None) -> WorkbenchResult:
    """N 组工作台：逐组拟合 k_obs（复用 fit_series）→ 参比换算 Φ_Δ（复用 calc_phi）。

    这是新增独立入口；``run_compare`` / ``run_phi_pipeline`` 的数值路径不变。
    """
    from ..report.results import attach_logging
    from ..render.workbench_plot import plot_workbench

    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    cap = attach_logging(None, debug=False, log_cb=log_cb)
    res = WorkbenchResult(groups=[dict(g) for g in groups])
    try:
        if not groups:
            res.ok = False
            res.message = "没有可用的组"
            return res
        if out_dir is None:
            gs = []
            for g in groups:
                gs.extend(_group_names(g))
            out_dir = make_figure_run_dir(cfg, gs, subdir="workbench")
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        res.out_dir = out_path
        lam = float(cfg["wavelength"]["probe"])
        qy = cfg.get("quantum_yield") or {}
        mode = str(qy.get("iabs_mode", "exact"))
        eff_phi = float(phi_ref if phi_ref is not None else qy.get("phi_ref", 0.75))
        res.phi_ref = eff_phi
        labels = [str(g.get("label", "")) for g in groups]
        ref_label = str(ref_label or "").strip()
        ref = ref_label if ref_label in labels else ""
        if ref_label and not ref:
            log.warning("工作台指定参比组 %r 不在当前组里，Φ 列留空", ref_label)
        if not ref:
            log.warning("工作台未指定参比组 → Φ_Δ 列留空（不拦 k 拟合）")

        series: dict = {}
        rows: list[dict] = []
        _p(10, "逐组提取 A(t)@探针波长")
        for g in groups:
            label = str(g.get("label", ""))
            t, A, skipped = group_kinetics(g, cfg)
            if skipped:
                log.warning("[%s] %d 个文件在 %.0f nm 无有效数据, 不参与拟合",
                            label, len(skipped), lam)
            fit = fit_series(t, A)
            series[label] = {"t": t, "A": A, "fit": fit}
            rows.append({"label": label, "file_count": int(len(g.get("members") or [])),
                         "truncated": bool(fit.get("truncated", False)),
                         "n": int(len(t)),
                         "t_min": float(t.min()) if len(t) else None,
                         "t_max": float(t.max()) if len(t) else None,
                         "k_obs": (fit.get("k") if fit.get("ok") else None),
                         "k_err": (fit.get("k_err") if fit.get("ok") else None),
                         "r2": (fit.get("R2") if fit.get("ok") else None),
                         "a_ex": None, "phi_delta": None, "phi_err": None,
                         "ref_label": ref, "phi_ref": eff_phi if ref else None,
                         "note": "" if fit.get("ok") else fit.get("msg", "拟合失败"),
                         "phi_ci_low": None, "phi_ci_high": None})
        _p(45, "计算 Φ_Δ")
        aex: dict[str, Optional[float]] = {}
        if ref:
            for g in groups:
                try:
                    aex[str(g.get("label", ""))] = excitation_abs(g, cfg)[0]
                except Exception:                       # noqa: BLE001
                    aex[str(g.get("label", ""))] = None
            ref_fit = series.get(ref, {}).get("fit") or {}
            for r in rows:
                r["a_ex"] = aex.get(r["label"])
                if r["label"] == ref:
                    r["phi_delta"] = eff_phi
                    r["phi_err"] = 0.0
                    r["note"] = "参比"
                    continue
                if not ref_fit.get("ok") or not r.get("k_obs"):
                    r["note"] = r["note"] or "参比拟合失败"
                    continue
                a_s, a_r = aex.get(r["label"]), aex.get(ref)
                if a_s is None or a_r is None:
                    r["note"] = "A_ex 缺失"
                    continue
                try:
                    phi = calc_phi(r["k_obs"], ref_fit["k"], a_s, a_r, eff_phi, mode)
                    sig = phi_sigma(phi, r["k_obs"], r["k_err"], ref_fit["k"],
                                    ref_fit["k_err"], a_s, a_r, eff_phi,
                                    float(qy.get("phi_ref_err", 0.05)),
                                    float(qy.get("a_ex_err", 0.005)), mode)
                    r["phi_delta"], r["phi_err"] = phi, sig
                except Exception as err:               # noqa: BLE001
                    log.warning("工作台 %s Φ 计算失败：%s", r["label"], err)
                if bootstrap and np.isfinite(r.get("phi_delta") or np.nan):
                    try:
                        from .phi import bootstrap_phi
                        bm, bs = bootstrap_phi(
                            series[r["label"]]["t"], series[r["label"]]["A"],
                            series[ref]["t"], series[ref]["A"], a_s, a_r, eff_phi, cfg)
                        r["phi_ci_low"] = bm - 1.96 * bs
                        r["phi_ci_high"] = bm + 1.96 * bs
                    except Exception as err:           # noqa: BLE001
                        log.warning("bootstrap 置信带失败：%s", err)
        _p(70, "写出汇总表")
        p = out_path / "singlet_oxygen_summary.csv"
        import csv as _csv
        with open(p, "w", newline="", encoding="utf-8-sig") as fp:
            w = _csv.writer(fp)
            w.writerow(["label", "n", "t_min_s", "t_max_s", "k_obs", "k_err",
                        "r2", "a_ex", "phi_delta", "phi_err", "ref_label",
                        "phi_ref", "note", "phi_ci_low", "phi_ci_high"])
            def _fmt(v):
                try:
                    return f"{float(v):.8g}" if v is not None and np.isfinite(float(v)) else ""
                except (TypeError, ValueError):
                    return ""
            for r in rows:
                w.writerow([r["label"], r["n"], _fmt(r["t_min"]), _fmt(r["t_max"]),
                            _fmt(r["k_obs"]), _fmt(r["k_err"]), _fmt(r["r2"]),
                            _fmt(r["a_ex"]), _fmt(r["phi_delta"]), _fmt(r["phi_err"]),
                            r["ref_label"], _fmt(r["phi_ref"]), r["note"],
                            _fmt(r["phi_ci_low"]), _fmt(r["phi_ci_high"])])
        res.files.append(p)
        _p(85, "绘制工作台图")
        try:
            res.files += plot_workbench(rows, series, out_path, style=style,
                                        error_bars=error_bars, time_unit=time_unit,
                                        annotate=annotate, transparent=transparent,
                                        formats=formats, ci_band=ci_band)
        except Exception as err:                       # noqa: BLE001
            log.warning("工作台绘图失败（CSV 仍有效）：%s", err)
        res.rows = rows
        res.ref_label = ref
        res.ok = any(r.get("k_obs") is not None for r in rows)
        if not res.ok:
            res.message = "没有一组拟合成功"
    except Exception as err:                           # noqa: BLE001
        res.ok = False
        res.message = f"工作台失败：{type(err).__name__}: {err}"
        log.error("%s", res.message)
    finally:
        cap.detach()
    _p(100, "完成" if res.ok else "失败")
    return res

# ═══════════════════════════ FR-3 光谱堆叠/叠加图 ═══════════════════════════
@dataclass
class StackResult:
    """FR-3 出图结果。"""
    out_dir: Optional[Path] = None
    files: list[Path] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    ok: bool = True
    message: str = ""


def run_stack(scans: Sequence[Scan], cfg: dict,
              out_dir: Optional[Path | str] = None,
              only_group: Optional[str] = None,
              do_overlay: bool = True, do_jacs: bool = True,
              progress_cb: ProgressCb = None,
              log_cb: LogCb = None) -> StackResult:
    """FR-3: 每组一张 JACS 堆叠图 + 时间演化叠加图。

    对应 legacy ``compare_kobs.flow_stack`` 与 ``singlet_o2_phi`` 图 a。
    ``only_group`` 给定则只画该组；``cfg["plot"]["spectra_overlay"]["select_times"]``
    控制叠加图只画哪些时间点（"all" 或秒数列表）。
    渲染风格由 ``cfg["plot"]["style"]`` 决定（jacs=单栏 82.5 mm，legacy=迁移前风格）。
    """
    from ..render.plotting import (_fig_a_spectra, apply_plot_style,
                           jacs_stack_all as legacy_stack_all, plot_style,
                           setup_mpl)
    from ..report.results import attach_logging

    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    cap = attach_logging(None, debug=False, log_cb=log_cb)
    res = StackResult()
    try:
        if out_dir is None:
            groups = sorted({s.group for s in scans if not s.excluded})
            out_dir = make_figure_run_dir(cfg, groups, subdir="stack")
        out_dir = Path(out_dir)
        (out_dir / "figures").mkdir(parents=True, exist_ok=True)
        good = [s for s in scans
                if not s.excluded and s.role not in ("background", "dark")
                and (only_group is None or s.group == only_group)]
        if not good:
            res.ok = False
            res.message = "没有可用的光谱文件"
            log.error("%s", res.message)
            return res
        res.groups = sorted({s.group for s in good})
        jacs = plot_style(cfg) == "jacs"
        apply_plot_style(cfg)
        if jacs:
            from ..render.plotting_jacs import fig_a_overlay, jacs_stack_all
            plt = None
        else:
            plt = setup_mpl(cfg)
            jacs_stack_all = legacy_stack_all
        if do_overlay:
            _p(30, "绘制时间演化叠加图")
            try:
                if jacs:
                    res.files += list(fig_a_overlay(good, cfg,
                                                    out_dir / "figures", []) or [])
                else:
                    res.files += list(_fig_a_spectra(plt, good, cfg,
                                                     out_dir / "figures") or [])
            except Exception as err:                   # noqa: BLE001
                log.error("叠加图绘制失败: %s", err)
        if do_jacs:
            _p(65, "绘制 JACS 堆叠图")
            try:
                res.files += list(jacs_stack_all(good, cfg, out_dir) or [])
            except Exception as err:                   # noqa: BLE001
                log.error("JACS 堆叠图绘制失败: %s", err)
        res.out_dir = out_dir
        if not res.files:
            res.ok = False
            res.message = "未产出任何图件（数据可能不足）"
    except Exception as err:                           # noqa: BLE001
        res.ok = False
        res.message = f"堆叠出图失败: {err}"
        log.error("%s", res.message)
    finally:
        cap.detach()
    _p(100, "完成")
    return res


# ═══════════════════════════ FR-4 常规作图 ═══════════════════════════
@dataclass
class SpectraResult:
    """FR-4 出图结果。"""
    out_dir: Optional[Path] = None
    files: list[Path] = field(default_factory=list)
    stokes: list[tuple] = field(default_factory=list)   # [(λabs, λem, 位移)]
    ok: bool = True
    message: str = ""


def run_spectra_plots(spectra: Sequence, cfg: dict,
                      out_dir: Optional[Path | str] = None,
                      mode: str = "overlay",
                      abs_spec=None, em_spec=None,
                      progress_cb: ProgressCb = None,
                      log_cb: LogCb = None) -> SpectraResult:
    """FR-4: 常规紫外/荧光作图。

    ``mode="overlay"``  → 多文件归一化叠图（自动标注 λmax）
    ``mode="mirror"``   → 吸收+发射镜像归一化叠图（标注斯托克斯位移）
    ``mode="stacked"``  → 吸收/发射上下两栏叠放
    ``mode="auto"``     → 先按溶剂关键词自动配对，再对每对出镜像图
    """
    from . import spectra as spx
    from ..report.results import attach_logging

    def _p(pct: int, stage: str) -> None:
        if progress_cb:
            progress_cb(pct, stage)

    cap = attach_logging(None, debug=False, log_cb=log_cb)
    res = SpectraResult()
    try:
        if out_dir is None:
            # FR-4 的输入是任意谱（无组名）→ 染料未知，落 _multi（FR-11.7）
            out_dir = make_figure_run_dir(cfg, [], subdir="spectra")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        good = [s for s in spectra if s.ok]
        if not good:
            res.ok = False
            res.message = "没有可用的光谱文件"
            log.error("%s", res.message)
            return res
        if mode == "overlay":
            _p(40, "归一化叠图")
            res.files += list(spx.plot_normalized_overlay(good, cfg, out_dir) or [])
        elif mode in ("mirror", "stacked", "auto"):
            pairs: list[tuple] = []
            if abs_spec is not None and em_spec is not None:
                pairs = [(abs_spec, em_spec)]
            else:
                pairs = spx.pair_by_name(good, cfg.get("spectra", {})
                                         .get("auto_pair_keywords", []))
            if not pairs:
                res.ok = False
                res.message = ("未能配对吸收谱与发射谱：请各选一支，"
                               "或确认文件名含相同溶剂关键词")
                log.error("%s", res.message)
                return res
            fn = spx.plot_spectra_panel if mode == "stacked" else spx.plot_mirror
            for i, (a, e) in enumerate(pairs, 1):
                _p(int(30 + 60 * i / max(len(pairs), 1)),
                   f"镜像图 {i}/{len(pairs)}")
                files, info = fn(a, e, cfg, out_dir)
                res.files += list(files)
                res.stokes.append(info)
                log.info("配对 %s ↔ %s: λ_abs=%.1f nm, λ_em=%.1f nm, 斯托克斯位移=%.1f nm",
                         Path(a.path).name, Path(e.path).name, info[0], info[1], info[2])
        else:
            res.ok = False
            res.message = f"未知模式: {mode}"
        res.out_dir = out_dir
    except Exception as err:                           # noqa: BLE001
        res.ok = False
        res.message = f"出图失败: {err}"
        log.error("%s", res.message)
        import traceback
        log.debug("%s", traceback.format_exc())
    finally:
        cap.detach()
    _p(100, "完成")
    return res
