# -*- coding: utf-8 -*-
"""结果写出与日志层：日志捕获、run 目录、results.csv / fit_summary.json / report.md。

迁移自 legacy ``singlet_o2_phi`` 的输出段。**文件格式与 legacy 完全一致**
（设计文档 FR-1 第 5 条明确要求），仅追加：日志可经 ``log_cb`` 实时投递给 UI。
"""
from __future__ import annotations

import csv
import json
import logging
from functools import lru_cache
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

# ⚠ 这里**不能用** ``from ... import VERSION``（三级点）：δ 批 FS1 迁移时踩到
# "attempted relative import beyond top-level package" —— 那个写法对"模块以什么名字
# 被加载"极度敏感（`core.report.results` 可以，被当成非包路径加载就不行）。
# 绝对导入只有一个真相，且不受包结构再变的影响（垫片保证 `core` 一定可导入）。
from core import VERSION
from ..config import SCHEMA_VERSION
from ..inputs.io import Scan
from ..analysis.kinetics import FitResult

log = logging.getLogger("dye_tool.results")

#: 所有 core 模块共享的根 logger；setup_logging 把 handler 挂在它上面
ROOT_LOGGER = "dye_tool"

LogCb = Optional[Callable[[str, str], None]]


# ═══════════════════════════ 小工具 ═══════════════════════════
def jsonable(o):
    """递归转 JSON 安全类型；NaN/Inf → None（legacy ``_jsonable``）。

    相对 legacy 的加固：``np.integer``/``np.bool_`` 显式转成内建 ``int``/``bool``
    （numpy 2.x 下 ``np.int64`` **不是** ``int`` 的子类，直接返回会令
    ``json.dumps`` 抛 ``TypeError: Object of type int64 is not JSON serializable``）。
    """
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, np.ndarray):
        return jsonable(o.tolist())
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        v = float(o)
        return v if np.isfinite(v) else None
    if isinstance(o, (bool, int, str)) or o is None:
        return o
    if isinstance(o, Path):
        return str(o)
    return str(o)


def fmt(v, spec: str = ".6g") -> str:
    """数值格式化：非有限值 → ``"N/A"``（legacy ``_fmt``）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    return format(v, spec) if np.isfinite(v) else "N/A"


def fnum(v, default: float = float("nan")) -> float:
    """尽力转 float；失败或非有限 → default（legacy ``_fnum``）。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


def safe(name: str) -> str:
    """文件名安全化（legacy ``_safe``）。"""
    return re.sub(r"[^\w\-.]+", "_", name)


@lru_cache(maxsize=1)
def git_hash() -> str:
    """尽力取 git 短哈希；不可用返回 ``"unknown"``。

    D12（审查附录 D）：本函数在一次 run 里被调用 2 次（results/report 各一次），
    每次都会起一个 ``git`` 子进程；本项目**不是 git 仓库** → 等于每次 run 白起
    两个进程（Windows 上 ~几十 ms/个）。加 ``lru_cache``：进程内只尝试一次。
    """

    import subprocess
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:                                  # noqa: BLE001
        return "unknown"


def make_run_dir(root: Path | str) -> Path:
    """在 ``root`` 下建 ``run_<时间戳>`` 目录（已存在则加序号）。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    d = root / f"run_{ts}"
    i = 1
    while d.exists():
        d = root / f"run_{ts}_{i}"
        i += 1
    d.mkdir()
    return d


# ═══════════════════════════ 日志：文件 + 控制台 + 捕获 + 回调 ═══════════════════════════
#: 统一格式（legacy 原样）
_LOG_FMT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")


class LogCapture(logging.Handler):
    """收集 WARNING 及以上日志（供 report.md 汇总），并可选转发给 UI 回调。

    级别语义（两件事分开，避免"handler level 先把低级别记录滤掉"的陷阱）：
      * ``records``    —— 只收 ``level``（默认 WARNING）及以上，与 legacy 一致；
      * ``log_cb``     —— 收 ``min_level``（默认 DEBUG）及以上，全部投递给 UI，
        由 UI 侧自行按需过滤显示。

    因此 handler 自身的 ``level`` 取二者的较低值，否则 ``log_cb`` 永远收不到
    INFO/DEBUG（handler 的 level 在 ``emit()`` 之前就已过滤）。

    ``detach()`` 的行为分两种模式：
      * 由 :func:`attach_logging` 创建 —— 只摘掉本次追加的 handler（可嵌套）；
      * 由 :func:`setup_logging` 创建 —— 摘掉根 logger 上全部 handler（legacy 语义）。
    """

    def __init__(self, level: int = logging.WARNING, log_cb: LogCb = None,
                 min_level: int = logging.DEBUG):
        self.records_level = int(level)
        self.min_level = int(min_level)
        effective = min(self.records_level,
                        self.min_level if log_cb is not None else self.records_level)
        super().__init__(level=effective)
        self.records: list[str] = []
        self.log_cb = log_cb
        self.owned: Optional[list[logging.Handler]] = None   # 本次追加的 handler
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:                              # noqa: BLE001
            return
        if record.levelno >= self.records_level:
            self.records.append(msg)
        if self.log_cb is not None and record.levelno >= self.min_level:
            try:
                self.log_cb(record.levelname, msg)     # 线程安全由 UI 侧保证
            except Exception:                          # noqa: BLE001
                pass

    def detach(self) -> None:
        """摘掉本捕获器（以及本次追加的）handler，避免跨测试/跨运行泄漏。"""
        lg = logging.getLogger(ROOT_LOGGER)
        targets = self.owned if self.owned is not None else list(lg.handlers)
        for h in targets:
            try:
                h.close()
            except Exception:                          # noqa: BLE001
                pass
            lg.removeHandler(h)


def _root_logger() -> logging.Logger:
    """取到 ``dye_tool`` 根 logger 并确保其级别已放开。"""
    lg = logging.getLogger(ROOT_LOGGER)
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg


def setup_logging(logfile: Optional[Path], debug: bool = False,
                  log_cb: LogCb = None) -> LogCapture:
    """**重置式**装配根 logger（legacy 语义，供 CLI 单次运行使用）。

    先清空既有 handler，再挂：可选文件 handler + 控制台 handler + 捕获/回调
    handler。返回的 :class:`LogCapture` 的 ``records`` 汇总 WARNING 及以上；
    INFO 及以上会实时经 ``log_cb(level, msg)`` 投递给 UI。

    ⚠ 嵌套调用会清掉外层 handler。流水线内部请改用 :func:`attach_logging`。
    """
    lg = _root_logger()
    for h in list(lg.handlers):
        lg.removeHandler(h)
    cap = LogCapture(level=logging.WARNING, log_cb=log_cb)
    cap.setFormatter(_LOG_FMT)
    if logfile is not None:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_LOG_FMT)
        lg.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(_LOG_FMT)
    lg.addHandler(ch)
    lg.addHandler(cap)
    return cap


def attach_logging(logfile: Optional[Path] = None, debug: bool = False,
                   log_cb: LogCb = None) -> LogCapture:
    """**增量式**装配日志：只追加 handler，绝不清空既有 handler。

    与 :func:`setup_logging` 的差别是支持嵌套——GUI 外层若已配置日志区，
    流水线内部再挂一个捕获器不会把外层处理器清掉（否则外层再也收不到日志）。
    ``detach()`` 只摘掉本次追加的 handler。
    """
    lg = _root_logger()
    owned: list[logging.Handler] = []
    if not lg.handlers:                      # 首次装配才补控制台输出（CLI 场景）
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG if debug else logging.INFO)
        ch.setFormatter(_LOG_FMT)
        lg.addHandler(ch)
        owned.append(ch)
    if logfile is not None:
        Path(logfile).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(logfile, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(_LOG_FMT)
        lg.addHandler(fh)
        owned.append(fh)
    cap = LogCapture(level=logging.WARNING, log_cb=log_cb)
    cap.setFormatter(_LOG_FMT)
    lg.addHandler(cap)
    owned.append(cap)
    cap.owned = owned
    return cap


# ═══════════════════════════ results.csv ═══════════════════════════
CSV_HEAD = ["组别", "角色", "k_obs(s^-1)", "k_stderr", "R2", "n", "自动截断", "A0",
            "A_ex(自动)", "辐照度(mW/cm^2)", "Phi_Delta", "Phi_err", "状态",
            "对照判定", "备注"]


@contextmanager
def atomic_write(path: Path | str, mode: str = "w", encoding: str = "utf-8",
                 newline: Optional[str] = None):
    """**原子写**：先写同目录临时文件，成功后再 ``os.replace`` 覆盖目标（O5）。

    为什么值得做：``history.csv`` / ``fit_summary.json`` / ``results.csv`` 都是
    "读-改-写"或"一次写很多行"的文件。进程在写一半时被杀（用户点取消、任务管理器
    结束、断电），原实现会留下**半截文件**：history.csv 直接变成不可读，整个染料的
    历史档案就废了。临时文件 + 同目录 rename 在 Windows 上也是原子的
    （``os.replace`` 覆盖已存在文件不会失败）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    fh = open(tmp, mode, encoding=encoding, newline=newline)
    try:
        with fh:
            yield fh
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_results_csv(path: Path | str, results: dict, phi: dict, controls: dict) -> Path:
    """写出 ``results.csv``（表头与列序与 legacy 完全一致；原子写）。"""
    path = Path(path)
    with atomic_write(path, newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEAD)
        for g in sorted(results):
            e = results[g]
            fit = e.get("pooled")
            p = phi.get(g, {})
            jd = controls.get(g, ("", ""))
            note = ((fit.message if fit else "")
                    + (f" | {jd[1]}" if jd and jd[1] else "")).strip(" |")
            w.writerow([g, e.get("role", ""), fmt(fit.k if fit else np.nan),
                        fmt(fit.k_err if fit else np.nan),
                        fmt(fit.r2 if fit else np.nan, ".5f"),
                        (fit.n if fit else 0),
                        "是" if (fit and fit.truncated) else "否",
                        fmt(e.get("A0_pooled", np.nan), ".4f"),
                        fmt(e.get("a_ex_auto", np.nan), ".4f"),
                        fmt(e.get("irradiance", 1.0), ".4f"),
                        fmt(p.get("phi", np.nan), ".4f"),
                        fmt(p.get("phi_err", np.nan), ".4f"),
                        (fit.status if fit else "FAILED"),
                        (jd[0] if jd else ""), note])
    log.info("已写出 %s", path)
    return path


# ═══════════════════════════ fit_summary.json ═══════════════════════════
def write_fit_summary(path: Path | str, scans: Sequence[Scan], results: dict,
                      phi: dict, controls: dict, cfg: dict) -> Path:
    """写出 ``fit_summary.json``（键结构与 legacy 一致，可被 --plot-only 复用）。"""
    path = Path(path)
    j = {"version": VERSION, "schema_version": SCHEMA_VERSION, "git_hash": git_hash(),
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
    with atomic_write(path) as f:
        f.write(json.dumps(jsonable(j), ensure_ascii=False, indent=2))
    log.info("已写出 %s", path)
    return path


def results_from_json(j: dict) -> dict:
    """从 ``fit_summary.json`` 重建 results（legacy ``results_from_json``）。"""
    results: dict = {}
    for g, gd in (j.get("groups") or {}).items():
        e = {"role": gd.get("role"), "irradiance": gd.get("irradiance", 1.0),
             "A0_pooled": gd.get("A0_pooled"), "a_ex_auto": gd.get("a_ex_auto"),
             "reps": {r: {k2: np.asarray(v2, float) for k2, v2 in v.items()}
                      for r, v in (gd.get("reps") or {}).items()},
             "pooled": None}
        fd = gd.get("fit")
        if fd:
            e["pooled"] = FitResult(
                k=fnum(fd.get("k")), k_err=fnum(fd.get("k_err")), r2=fnum(fd.get("r2")),
                n=int(fd.get("n") or 0), intercept=fnum(fd.get("intercept")),
                truncated=bool(fd.get("truncated")), status=fd.get("status", "OK"),
                message=fd.get("message", ""), pre_k=fnum(fd.get("pre_k")),
                pre_r2=fnum(fd.get("pre_r2")),
                x=np.asarray(fd.get("x") or [], float),
                lnR=np.asarray(fd.get("lnR") or [], float))
        results[g] = e
    return results


def scans_from_json(j: dict) -> list[Scan]:
    """从 ``fit_summary.json`` 重建 scans（legacy ``scans_from_json``）。"""
    scans: list[Scan] = []
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


# ═══════════════════════════ report.md ═══════════════════════════
def write_report(path: Path | str, data_dir, scans: Sequence[Scan], results: dict,
                 phi: dict, controls: dict, cfg: dict,
                 warnings: Optional[Sequence[str]] = None) -> Path:
    """写出 ``report.md``（章节与 legacy 一致：概览/方法学/结果/对照/图/警告/建议）。"""
    path = Path(path)
    wl_cfg, kin, qy = cfg["wavelength"], cfg["kinetics"], cfg["quantum_yield"]
    n_ok = sum(1 for s in scans if not s.excluded)
    has_dark = any((not s.excluded) and s.role == "sample"
                   and s.t <= float(cfg["a0"].get("dark_max_time", 0.0)) for s in scans)
    a0_txt = (f"t ≤ {float(cfg['a0'].get('dark_max_time', 0.0)):.0f} s 暗态点均值" if has_dark
              else f"最早 {cfg['a0'].get('n_early', 2)} 个时间点均值（**本批无暗态数据**）")
    L = ["# 单线态氧（¹O₂）相对量子产率 Φ_Δ 计算报告", "",
         f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ",
         f"- 程序：dye_tool v{VERSION}（git: {git_hash()}）  ",
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
          + ("已按时间匹配扣除。" if any(s.role == "background" and not s.excluded
                                          for s in scans)
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
        e = results[g]
        f = e.get("pooled")
        p = phi.get(g, {})
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
    # M15：图 a 的文件名有两种（多组 → fig_a_spectra_<组>.png；单组 → 
    # fig_a_spectra_overlay.png），报告里写死单组那个名字会指向不存在的文件。
    _groups = sorted({str(g) for g in (results or {})}) or \
        sorted({str(getattr(s, "group", "")) for s in scans})
    fig_a_name = ("figures/fig_a_spectra_overlay.png" if len(_groups) == 1
                  else "figures/fig_a_spectra_<组名>.png")
    L += ["", "## 5. 图", "", "| 图 | 文件 | 内容 |", "|---|---|---|",
          f"| a | {fig_a_name} | 光谱叠加（按辐照时间着色；标注 ABDA "
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
    # O10（V2.x 审查）：图章节原来**恒定**写 `figures/…`，而预览档的图实际在
    # `figures_preview/` → 只跑预览的 run，report.md 里所有图链接都指向不存在的目录。
    # 按**磁盘现状**选目录（final 有 figures/，preview 有 figures_preview/）。
    _rd = Path(path).parent
    if (_rd / "figures").is_dir():
        _figdir = "figures"
    elif (_rd / "figures_preview").is_dir():
        _figdir = "figures_preview"
    else:
        _figdir = "figures"
    if _figdir != "figures":
        L = [ln.replace("figures/", f"{_figdir}/") for ln in L]
    with atomic_write(path) as f:
        f.write("\n".join(L) + "\n")
    log.info("已写出 %s", path)
    return path


# ═══════════════════════════ 控制台速览（CLI 用） ═══════════════════════════
def console_summary(data_dir, scans: Sequence[Scan], results: dict, phi: dict,
                    controls: dict, run_dir, cfg: dict) -> str:
    """生成控制台速览文本（legacy ``print_console_summary`` 的返回值版本）。"""
    out = ["", "=" * 64, "  分析完成 — 关键结果速览", "=" * 64,
           f"  数据目录   : {data_dir}  "
           f"({sum(1 for s in scans if not s.excluded)}/{len(scans)} 个文件有效)"]
    for g in sorted(results):
        e = results[g]
        f = e.get("pooled")
        if e["reps"]:
            ts = np.concatenate([r["t"] for r in e["reps"].values()])
            out.append(f"  -- 组 [{g}] ({e.get('role')}, {len(e['reps'])} 条序列, "
                       f"t={np.nanmin(ts):.0f}-{np.nanmax(ts):.0f} s)")
        if f is None or f.status == "FAILED":
            out.append(f"     拟合失败 : {f.message if f else '无有效数据'}")
            continue
        out.append(f"     A(0)     : {e.get('A0_pooled', float('nan')):.4f} AU")
        out.append(f"     k_obs    : {f.k:.4e} ± {f.k_err:.1e} s^-1   "
                   f"(R^2={f.r2:.4f}, n={f.n})")
        if f.truncated:
            out.append(f"     [!] 已自动截断: 全窗口 k={f.pre_k:.4e} -> "
                       f"截断后 k={f.k:.4e} "
                       f"(保留前 {int(round(float(cfg['kinetics'].get('truncate_frac', 0.7)) * 100))}%)")
        if f.k > 0:
            out.append(f"     半衰期   : {np.log(2) / f.k:.0f} s")
        p = phi.get(g, {})
        if p.get("status") == "OK":
            out.append(f"     Phi_D    : {p['phi']:.4f} ± {p['phi_err']:.4f}")
        elif p.get("status") == "NO_REF":
            out.append("     Phi_D    : N/A (未找到 ref 参比组)")
        elif p.get("status") == "FAILED":
            out.append("     Phi_D    : 计算失败(见日志)")
        if g in controls:
            out.append(f"     对照判定 : {controls[g][0]} ({controls[g][1]})")
    out += ["-" * 64, f"  输出目录   : {run_dir}",
            "    results.csv / fit_summary.json / report.md / run.log / figures/",
            "=" * 64, ""]
    return "\n".join(out)
