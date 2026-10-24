# -*- coding: utf-8 -*-
"""出图多进程并行（P2）。

**为什么必须多进程**：matplotlib 的 Agg 后端把 rcParams 与字体缓存放在**模块级全局**
状态里，两个线程同时画会互相踩（`plot.style` / dpi / Arial 设置全是全局的），所以
并行只能靠**独立进程**。Windows 上进程是 spawn（无 fork），每个子进程都要重新 import
matplotlib + numpy —— 实测**约 0.7 s/进程**，这正是本模块必须"先估工作量、再决定是否
并行"的原因：小批量并行会比串行更慢。

**正确性**：子进程仍然调用 :func:`core.pipeline.rerender_figures`（同一条渲染代码
路径），因此产物与串行逐字节一致——这一点由实测（子进程 vs 父进程 PNG 二进制相同）
与验收 A8.6 共同守住。父进程负责：

* **任务切分**：先把每个步骤切成任务；图 b 与堆叠图是"每组一张"，所以按**组**再切，
  子进程只画它那一组（:func:`core.plotting_jacs.fig_a_overlay` 等的 ``groups`` 参数）；
* **跳过已渲**：命中父进程渲染复用缓存的任务根本不派发（FR-6.7 语义不变）。

图注（``captions.md``）由**父进程**统一补写（``plotting_jacs._write_captions_text``）：
并行时每个子进程只画一步，各自的图注列表天然只是"半份"，让它们写会互相覆盖成
残缺版本。子进程只负责把 PNG 落到磁盘并把路径回传给父进程。
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import os
import time
from typing import Optional, Sequence

log = logging.getLogger("dye_tool.plotting.parallel")

__all__ = ["render_units", "render_units_parallel", "estimate_seconds",
           "pick_workers"]

#: 单个 spawn 子进程的固定开销（import matplotlib+numpy+core），实测约 1.2 s。
#: 用于"值不值得并行"的估算。
SPAWN_COST_S = 1.2

#: 每个"任务"的固定开销下界（打开图、savefig 的公共部分），实测约 0.25 s。
UNIT_MIN_S = 0.25

#: 实测的并行加速（16 核 Windows，本机）——**这些数字是本模块设计的依据**：
#:   * 10 组（32 张）：预览 9.1 → 6.8 s（**1.34×**），定稿 12.6 → 7.7 s（**1.63×**）；
#:   * 30 组（92 张）：定稿 34.8 → 16.0 s（**2.17×**）。
#: 进程数超过 4–6 之后不再变快（每个 spawn 子进程的固定成本随进程数叠加，且任务
#: 粒度只有 0.2–0.4 s）；收益上限还受 Amdahl 限制——图 a/c/d/e 是单任务、切不开。
PARALLEL_SPEEDUP_CAP = 2.2


# ═══════════════════════════ 任务切分与估算 ═══════════════════════════
def render_units(steps: Sequence[str],
                 results: Optional[dict] = None,
                 scans=None) -> list[tuple[str, ...]]:
    """把"要画的步骤"切成并行任务（每个任务是 ``(步骤, 组名)``）。

    ``(step, None)`` = 该步骤一张图（图 a/c/d/e：一次画完所有组）；
    ``(step, 组名)`` = 该步骤的这一组（图 b / 堆叠图：**每组一张**，可独立并行）。

    组的来源是 ``results`` 的键（图 b 与堆叠图都只对能拟合/有足够谱的组出图），
    并按**谱数**从多到少排序：大组先派发，尾部负载更均衡。

    ⚠ 图 a **不按组切分**（M14）：它的文件名有"单组特例"——只画一组时叫
    ``fig_a_spectra_overlay``；按组切分会让两组各自渲染成同一个文件名而互相覆盖。
    """
    #: 只有这两步是"每组一张"、可以按组切分（图 a 的特例名不允许切，见 docstring）
    per_group = {"b", "stack"}
    groups: list[str] = []
    if results:
        # 组名 → 有效谱数（用于排序；拿不到就按 0 排）
        n_by_g: dict = {}
        for s in scans or []:
            if getattr(s, "excluded", "") or s.role in ("background", "dark"):
                continue
            n_by_g[s.group] = n_by_g.get(s.group, 0) + 1
        groups = sorted(results, key=lambda g: (-n_by_g.get(g, 0), str(g)))

    units: list[tuple[str, ...]] = []
    for step in steps:
        if step in per_group and groups:
            units += [(step, g) for g in groups]
        else:
            units.append((step, None))
    return units


def estimate_seconds(units: Sequence[Sequence[str]], export_mode: str = "final"
                     ) -> float:
    """粗估这些任务**串行**要多久（只用于"要不要并行"的门槛判断）。

    成本模型：图 b 与堆叠图单张便宜（约 1 个 Δ 的活），图 a/c/e 要处理所有组、
    是"重"图；定稿档（600 dpi）整体比预览档慢约 1.3×。
    """
    heavy = {"a", "c", "e", "d"}
    per = 0.35 if str(export_mode).lower() == "preview" else 0.5
    total = 0.0
    for u in units:
        step = str(u[0])
        total += (0.9 * per) if step in heavy else (0.45 * per)
    return max(total, UNIT_MIN_S * len(units))


def pick_workers(n_tasks: int, want: int = 0) -> int:
    """进程数：``want<=0`` 时取 ``min(cpu-1, 4, 任务数)``。

    上限故意压在 **4**：16 核实测超过 4 个进程反而变慢（每个 spawn 子进程的固定
    成本随进程数线性叠加，而可并行的任务粒度只有 0.2–0.4 s）。
    """
    cpu = os.cpu_count() or 2
    auto = max(1, min(cpu - 1, 4, int(n_tasks)))
    return max(1, min(int(want) if want and want > 0 else auto, int(n_tasks)))


# ═══════════════════════════ 子进程入口 ═══════════════════════════
def _render_unit_worker(args: tuple) -> tuple:
    """**子进程**：渲染一个任务，返回 ``(任务, 文件路径, 错误, 耗时, pid)``。

    必须是模块级函数（spawn 要求可 pickle）。子进程里第一件事就是固定 Agg 后端：
    子进程不建窗口，绝不碰 Qt。

    每个任务自带整份数据（``scans`` 约 0.6 MB / 100 文件）。这不是最省 IPC 的
    写法，但**实测比"用 initializer 灌一次"更快**：后者让每个子进程在启动阶段
    各吃一份大参数，进程数一多就线性变慢（16 核实测 w=8 时 23.6 s vs 串行 13.0 s，
    而本写法同条件是 7.1 s vs 8.1 s）。先测再选，别凭直觉优化。
    """
    import matplotlib
    matplotlib.use("Agg")

    (scans, results, phi, controls, cfg, run_dir, export_mode, unit,
     group_dye) = args
    step, group = str(unit[0]), unit[1]
    groups = [group] if group else None
    t0 = time.perf_counter()
    try:
        from ..analysis.pipeline import rerender_figures
        made = rerender_figures(scans, results, phi, controls, cfg, run_dir,
                                export_mode=export_mode, only=[step],
                                group_dye=group_dye, groups=groups)
        return (tuple(unit), [str(p) for p in made], [], time.perf_counter() - t0,
                os.getpid())
    except Exception as err:                            # noqa: BLE001 —— 失败不废整批
        return (tuple(unit), [], [f"{type(err).__name__}: {err}"],
                time.perf_counter() - t0, os.getpid())


def render_units_parallel(units: Sequence[Sequence[str]], scans, results: dict,
                          phi: dict, controls: dict, cfg: dict, run_dir,
                          export_mode: str = "final",
                          group_dye: Optional[dict] = None,
                          workers: int = 0,
                          chunksize: int = 1) -> tuple[dict, list[str]]:
    """并行渲染给定任务。

    Returns:
        ``(任务→文件路径列表, 错误消息列表)``；单任务失败只记错误、不影响其余
        （与串行路径"单张图失败不中断"的既有约定一致）。
    """
    run_dir = str(run_dir)
    head = (scans, results, phi, controls, cfg, run_dir, export_mode)
    dye = dict(group_dye or {})
    tasks = [(*head, u, dye) for u in units]
    n = pick_workers(len(tasks), workers)
    ctx = mp.get_context("spawn")
    out: dict = {}
    errs: list[str] = []
    t0 = time.perf_counter()
    log.info("并行出图：%d 个任务 / %d 个进程（spawn）…", len(tasks), n)
    with ctx.Pool(processes=n) as pool:
        for unit, paths, err, dt, _pid in pool.imap_unordered(
                _render_unit_worker, tasks, chunksize=max(1, int(chunksize))):
            out[tuple(unit)] = list(paths)
            for e in err or []:
                errs.append(f"{'|'.join(str(x) for x in unit)}: {e}")
    log.info("并行出图完成：%d 个任务，用时 %.2fs%s", len(tasks),
             time.perf_counter() - t0,
             f"，{len(errs)} 个任务报错" if errs else "")
    return out, errs
