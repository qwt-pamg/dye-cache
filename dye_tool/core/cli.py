# -*- coding: utf-8 -*-
"""命令行入口（设计文档 §10 Q4：保留 ``python -m core.cli``）。

与 legacy ``singlet_o2_phi.main`` 的参数解析保持兼容，便于验收 A2
"GUI 结果与 legacy 命令行结果对比"：
``python -m core.cli <数据目录> -o runs`` 与 legacy 用法一一对应。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

from . import VERSION
from .analysis.pipeline import run_core, run_phi_pipeline
from .config import default_config, load_config
from .report.results import (console_summary, make_run_dir, results_from_json,
                             scans_from_json, setup_logging)
from .report.synthetic import make_synthetic, self_test_text


def build_parser() -> argparse.ArgumentParser:
    """构造与 legacy 兼容的参数解析器。"""
    ap = argparse.ArgumentParser(
        prog="dye_tool", description="¹O₂ 相对量子产率 Φ_Δ 计算工具（dye_tool core CLI）")
    ap.add_argument("data_dir", nargs="?", type=Path, help="数据目录(每文件=一个时间点)")
    ap.add_argument("-c", "--config", type=Path, help="config.yaml(默认 ./config.yaml)")
    ap.add_argument("-o", "--out-root", type=Path, default=Path("runs"), help="输出根目录")
    ap.add_argument("--dry-run", action="store_true", help="只解析+QC, 不拟合不出图")
    ap.add_argument("--plot-only", type=Path, metavar="RUN_DIR", help="复用已有结果只重绘图")
    ap.add_argument("--bootstrap", action="store_true", help="附加 bootstrap 校验(n=1000)")
    ap.add_argument("--debug", action="store_true", help="控制台输出 DEBUG 级日志")
    ap.add_argument("--make-synthetic", nargs="?", const=Path("samples/synthetic_demo"),
                    type=Path, metavar="DIR", help="生成 synthetic 测试数据集")
    ap.add_argument("--self-test", action="store_true", help="运行内置验收自检(T1-T5+F6)")
    ap.add_argument("--version", action="version", version=f"dye_tool {VERSION}")
    # ── V1.1 新增（设计文档 FR-6 / FR-11） ──
    ap.add_argument("--export-mode", choices=["final", "preview"], default="final",
                    help="final=600dpi png+图注（默认）；"
                         "preview=低 dpi png 快出图")
    ap.add_argument("--dye", metavar="NAME", default=None,
                    help="强制指定本次 run 的染料名（跳过自动命名，适合自动化）")
    ap.add_argument("--dir-scheme", choices=["dye", "flat"], default=None,
                    help="输出目录方案：dye=runs/<染料>/<时间戳>/（默认）；"
                         "flat=runs/run_<时间戳>/（V1.0 兼容）")
    ap.add_argument("--no-history", action="store_true",
                    help="本次不写染料 history.csv 档案")
    # ── V1.3 新增（P5/P6/P7/P8/P9） ──
    ap.add_argument("--reorg-runs", action="store_true",
                    help="整理旧 runs/ 扁平目录到 runs/<染料>/<时间戳>/；"
                         "默认只出计划(dry-run)，加 --apply 才真正移动")
    ap.add_argument("--apply", action="store_true",
                    help="配合 --reorg-runs：真正执行移动（默认 False=只预览）")
    ap.add_argument("--force", action="store_true",
                    help="配合 --reorg-runs --apply：忽略 config.yaml 里 "
                         "dye.reorg.dry_run=true 这道安全闸（默认尊重它）")
    ap.add_argument("--extinction", nargs="*", type=float, metavar="C",
                    help="计算摩尔消光系数 ε：后面跟与光谱顺序对应的浓度列表")
    ap.add_argument("--a-at-wavelength", metavar="WL[,WL...]",
                    help="导出任意波长吸光度表 A@λ，如 400,450,500")
    # G8（第二轮审查）/ V2.0 FR-16：P8 体检图原先**只有 GUI 按钮**（Φ_Δ 页
    # 导出条上的 btn_dashboard），命令行无入口。这个旗标与 GUI 走**同一对**
    # core 函数（check_scans + render_dashboard），不复制实现。
    ap.add_argument("--dashboard", metavar="DIR", type=Path, nargs="?",
                    const=Path("."),
                    help="出一张 QC 体检仪表盘（P8）到 --out-root；"
                         "缺省 DIR 用当前目录")
    # ── V2.0 δ 新增（FR-14 批次文件） ──
    # 默认 auto：数据目录里有 dye_batch.yaml 就吃，没有就完全按 V1.3 行为
    # （机制约束 5）。给了路径就必须存在，否则明确报错——静默忽略用户显式
    # 指定的文件是所有"我明明写了却没生效"事故的源头。
    ap.add_argument("--batch", nargs="?", const="auto", default="auto",
                    metavar="PATH",
                    help="批次整定文件：缺省 auto=用数据目录里的 dye_batch.yaml"
                         "（没有就忽略）；显式 PATH 则必须存在；"
                         "传 none 完全不读批次文件")
    return ap


def _load_batch_for(args) -> tuple:
    """按 ``--batch`` 读批次整定（FR-14）；返回 ``(BatchFile|None, 说明)``。

    * ``auto``（默认）：数据目录里有 ``dye_batch.yaml`` 才读，没有返回 None；
    * ``none``：完全不读（想复现"纯文件名解析"的 V1.3 行为时用）；
    * 显式路径：必须存在，否则报错返回 ``(None, 错误说明)``——不静默忽略。
    """
    from .inputs.batch_file import BATCH_FILENAME, load_batch_path
    raw = getattr(args, "batch", "auto")
    if raw == "none":
        return None, "（--batch none：不读批次文件）"
    if raw == "auto":
        p = Path(args.data_dir or ".") / BATCH_FILENAME
        if not p.is_file():
            return None, "（无 dye_batch.yaml）"
    else:
        p = Path(raw)
        if not p.is_file():
            return None, f"[错误] 指定的批次文件不存在：{p}"
    # A.M5（V2.x 审查）：显式 `--batch other.yaml` 必须读**这个文件**，
    # 不是它同目录的 dye_batch.yaml（原实现只取 p.parent → 指定文件名被丢弃）。
    # auto 分支算出的 p 就是 `<数据目录>/dye_batch.yaml`，同一条路即可。
    issues: list = []
    b = load_batch_path(p, issues)
    for it in issues:
        print(f"[批次文件 {it.level}] {it.message}")
    # V2.4-A（FR-26.5）：只带 `analysis_preset`（无文件级整定）的批次同样有效——
    # 那是"整个目录用一套分析模板"的常见写法，不能因为 files 为空就整份丢掉。
    if not b.files and not getattr(b, "analysis_preset", ""):
        return None, f"（批次文件无有效条目：{p}）"
    _extra = (f"，分析模板 {b.analysis_preset}" if getattr(b, "analysis_preset", "") else "")
    return b, f"（批次整定 {len(b.files)} 条{_extra}：{p}）"


def _resolve_config_path(explicit=None) -> Optional[Path]:
    """config.yaml 的查找顺序（O13）：``--config`` > 当前目录 > 应用目录。

    冻结版（one-folder exe）里 ``app_root()`` = exe 所在目录，所以"双击 exe 同级
    的 config.yaml"与"在项目根跑 python -m core.cli"两种用法都能找到同一份配置；
    显式 ``--config`` 永远优先。
    """
    if explicit:
        return Path(explicit)
    cwd_cfg = Path("config.yaml")
    if cwd_cfg.exists():
        return cwd_cfg
    try:
        from .paths import config_path as _app_cfg
        p = _app_cfg()
        return p if p.exists() else None
    except Exception:                                  # noqa: BLE001
        return None


def _group_of(path, scans) -> str:
    """按路径找回某个 ``Scan`` 的组名（P7 的 ``dye_of`` 回调用）。

    找不到就返回空串——``dye_for_group(cfg, "")`` 会走"未登记"分支，
    比编一个假组名安全。
    """
    key = str(Path(path)).lower()
    for s in scans:
        if str(Path(s.path)).lower() == key:
            return str(s.group or "")
    return ""


def resolve_cli_out_root(p) -> Path:
    """CLI 边界解析输出根：相对路径按**调用时 cwd**（V2.5 D10 单源化的边界口径）。

    与 ``core.paths.resolve_out_root``（相对按 app_root，GUI/核心默认）分道：
    命令行 ``-o rel`` 的语义是 cwd（legacy 兼容，A2 依赖这一语义）。在参数边界
    解析成绝对路径后，pipeline 内部拿到的永远是绝对路径、不会二次解析。
    """
    q = Path(p) if p is not None else Path("runs")
    return q if q.is_absolute() else Path.cwd() / q


def main(argv=None) -> int:
    """CLI 主入口；返回进程退出码（不直接 sys.exit，便于测试）。"""
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:                                  # noqa: BLE001
        pass
    args = build_parser().parse_args(argv)
    # V2.5 D10：输出根在 CLI 边界固定为绝对路径（显式 -o 相对仍按 cwd）。
    args.out_root = resolve_cli_out_root(args.out_root)

    if args.self_test:
        txt = self_test_text()
        print(txt)
        return 0 if "全部通过" in txt else 1

    if args.make_synthetic is not None:
        t = make_synthetic(args.make_synthetic)
        print(f"[OK] synthetic 数据已生成 -> {args.make_synthetic}")
        print(f"     k_true={t['k_true']:.4e} s^-1, k_ref={t['k_ref']:.4e}, "
              f"Phi_true={t['phi_true']:.2f}")
        print(f"     试运行: python -m core.cli {args.make_synthetic}")
        return 0

    if args.plot_only:
        from . import plotting
        run_dir = Path(args.plot_only)
        try:
            j = json.loads((run_dir / "fit_summary.json").read_text(encoding="utf-8"))
        except Exception as err:                        # noqa: BLE001
            print(f"[错误] 无法读取 {run_dir}/fit_summary.json: {err}")
            return 2
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        plotting.plot_all(scans_from_json(j), results_from_json(j),
                          j.get("phi") or {}, j.get("controls") or {},
                          j.get("config") or default_config(), run_dir)
        print(f"[OK] 图已重绘 -> {run_dir / 'figures'}")
        return 0

    # ``--reorg-runs`` 作用于输出目录而非输入数据，允许不带 data_dir
    if args.data_dir is None and not args.reorg_runs and args.dashboard is None:
        build_parser().error("请提供数据目录(或使用 --self-test / --make-synthetic)")

    # O13：配置查找顺序原来只看 CWD 的 config.yaml，而 GUI 用 app_root()、
    # paths.config_path() 又是第三种口径——冻结版换个工作目录启动就会"读不到
    # 自己的配置"（静默回落到默认值）。统一为：显式 --config > CWD > app_root。
    cfgpath = _resolve_config_path(args.config)
    cfg = load_config(cfgpath)
    if args.bootstrap:
        cfg["quantum_yield"]["bootstrap"] = True
    # O11：``--debug`` 原来只对 ``--dry-run`` 生效（主流程硬编码 debug=False）。
    # 这里在跑主流程前先把根 logger 配好，让 DEBUG 真的打到控制台。
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")

    # ── P9：旧 runs/ 目录染料化整理（默认 dry-run，只看计划） ──
    # 放在 cfg 加载之后：整理要用 dye 节的命名口径判定染料名。
    if args.reorg_runs:
        from .reorg_runs import apply_reorg, describe_plan, plan_reorg
        root = Path(args.out_root or "runs")
        plan = plan_reorg(root, cfg)
        print(f"[P9] {describe_plan(plan)}")
        for s in plan.get("issues", []):
            print(f"      {s}")
        for i, mv in enumerate(plan["moves"], 1):
            flag = "  （撞名→自动改名）" if mv["conflict"] else ""
            print(f"  {i:2d}. {mv['src']}")
            print(f"      → {mv['dst']}{flag}")
            print(f"        染料={mv['dye']}  依据={mv['reason']}")
        if not plan["moves"]:
            return 0
        print("")
        if not args.apply:
            print("[P9] 以上为**计划**（dry-run）。确认无误后加 --apply 真正执行。")
            print("[P9] 整理只移动、不删除；目标已存在时自动加 _1/_2，从不覆盖。")
            return 0
        # M7：真正执行时还受 config.yaml 的 dye.reorg.dry_run 约束（默认 true）
        res = apply_reorg(plan, cfg, force=bool(args.force))
        if res.get("dry_run"):
            print(f"[P9] 未动盘：{res.get('message', '')}")
            return 0
        print(f"[P9] 已移动 {res['moved_n']} 项，失败 {len(res['failed'])} 项，"
              f"空壳归档 {len(res.get('shells', []))} 项")
        if res.get("report"):
            print(f"[P9] 报告: {res['report']}")
        for d in res["failed"]:
            print(f"  [失败] {d['src']} → {d.get('dst')}: {d.get('error')}")
        return 0 if not res["failed"] else 1

    # ── V1.1：染料归档 / 档案 / 导出档位（CLI 默认仍与 V1.0 完全一致） ──
    if args.dir_scheme:
        cfg.setdefault("dye", {})["dir_scheme"] = args.dir_scheme
    if args.no_history:
        cfg.setdefault("dye", {})["history"] = False
    group_dye = None
    if args.dye:
        # 强制整 run 用一个染料名：把预览到的所有组都映射到该名字
        from .dyes import sanitize_dye_name
        from .io import preview_groups
        name = sanitize_dye_name(args.dye)
        group_dye = {g: name for g in preview_groups(args.data_dir, cfg)}
        if not group_dye:
            group_dye = {"__all__": name}
        print(f"[染料] 本次 run 强制归档为: {name}")

    batch, why = _load_batch_for(args)
    if batch is None and why.startswith("[错误]"):
        print(why)
        return 2
    if batch is not None:
        print(why)

    if args.dry_run:
        run_dir = make_run_dir(args.out_root)
        setup_logging(run_dir / "run.log", debug=args.debug)
        scans, _ = run_core(args.data_dir, cfg, dry=True, batch=batch)
        print(f"\n{'文件名':<44}{'组别':<30}{'角色':<10}{'t(s)':>7}  状态")
        print("-" * 110)
        for s in sorted(scans, key=lambda s: (s.group, s.t)):
            stat = "OK" if not s.excluded else f"剔除: {s.excluded}"
            print(f"{s.path.name:<44}{s.group:<30}{s.role:<10}{s.t:>7.0f}  {stat}")
        n_ok = sum(1 for s in scans if not s.excluded)
        print("-" * 110)
        print(f"共 {len(scans)} 个文件, 有效 {n_ok} 个, "
              f"剔除 {len(scans) - n_ok} 个(原因见 run.log)\n")
        print(f"日志: {run_dir / 'run.log'}")
        return 0

    # ── P6：摩尔消光系数 ε（--extinction 后跟与光谱顺序对应的浓度列表）──
    if args.extinction is not None:
        from .extinction import compute_extinction, write_extinction_table
        from .io import load_spectra_files, scan_extensions
        conc = [float(x) for x in args.extinction]
        if not conc:
            print("[错误] --extinction 需要在后面给出浓度列表，例如 "
                  "--extinction 1 2 5 10")
            return 2
        # L1（评审记录）：原来这里**硬编码** (".txt",".csv",".TXT",".fds")，绕过
        # `io.scan_extensions(cfg)`：① 用户自定义 io.extensions 在此失效；
        # ② `.fds` 无视 io.fds.enable=False；③ `.TXT` 在 `.lower()` 之后是死条目。
        exts = set(scan_extensions(cfg.get("io") or {})) | {".dat"}
        files = [p for p in sorted(Path(args.data_dir).rglob("*"))
                 if p.is_file() and p.suffix.lower() in exts
                 and not p.name.startswith("_")]
        if not files:
            print(f"[错误] {args.data_dir} 下没有找到光谱文件"
                  f"（扩展名 {'/'.join(sorted(exts))}）")
            return 2
        if len(files) != len(conc):
            print(f"[错误] 光谱数 {len(files)} 与浓度数 {len(conc)} 不一致 → 无法一一对应")
            print("       光谱：" + ", ".join(p.name for p in files[:8]))
            return 2
        cfg["extinction"]["concentrations"] = conc
        spectra = load_spectra_files(files, cfg)
        res = compute_extinction(spectra, cfg)
        print(f"[P6] 波长 {res.get('wavelength')} nm（{res.get('wavelength_mode')}）"
              f"  光程 {res.get('path_length_cm')} cm")
        if not res.get("ok"):
            print(f"[P6] 无法计算：{res.get('reason')}")
            for s in res.get("issues") or []:
                print(f"      - {s}")
            return 1
        print(f"[P6] ε = {res['eps']:.6g} {res['eps_unit']}"
              f"  ± {res.get('eps_stderr') or float('nan'):.3g}")
        print(f"      A = {res['slope']:.6g}·c" +
              ("" if res.get("force_origin") else f" + {res['intercept']:.6g}") +
              f"   R² = {res['r2']:.6f}   n = {res['n_points']}")
        for s in res.get("issues") or []:
            print(f"      ! {s}")
        _od = Path(args.out_root)
        _od.mkdir(parents=True, exist_ok=True)
        out = write_extinction_table(
            res, _od / str(cfg["extinction"].get("filename") or "extinction.csv"))
        print(f"[P6] 明细: {out}")
        return 0

    # ── P7：任意波长吸光度表 A@λ ──
    if args.a_at_wavelength:
        from .wavelength_table import absorbance_at, write_wavelength_table
        try:
            wls = [float(x) for x in str(args.a_at_wavelength).replace("，", ",")
                   .split(",") if x.strip()]
        except ValueError:
            print("[错误] --a-at-wavelength 需要逗号分隔的数字，例如 400,450,500")
            return 2
        if not wls:
            print("[错误] --a-at-wavelength 没有解析出任何波长")
            return 2
        cfg["wavelength_table"]["wavelengths"] = wls
        scans, _ = run_core(args.data_dir, cfg, dry=True)
        from .dyes import dye_for_group
        res = absorbance_at(scans, cfg,
                            dye_of=lambda p: dye_for_group(cfg, _group_of(p, scans)))
        if not res.get("ok"):
            print(f"[P7] 无法生成：{res.get('reason')}")
            return 1
        _od = Path(args.out_root)
        _od.mkdir(parents=True, exist_ok=True)
        out = write_wavelength_table(
            res, _od / cfg["wavelength_table"]["filename"])
        print(f"[P7] {len(res['rows'])} 行 × {len(res['wavelengths'])} 波长 → {out}")
        for s in (res.get("issues") or [])[:8]:
            print(f"      ! {s}")
        if res.get("skipped"):
            print(f"      跳过 {len(res['skipped'])} 个文件（原因见 CSV/日志）")
        return 0

    # ── G8 / FR-16：P8 QC 体检仪表盘 ──
    if args.dashboard is not None:
        from .qc_dashboard import render_dashboard
        scans, _ = run_core(args.dashboard, cfg, dry=True, batch=batch)
        if not scans:
            print(f"[P8] {args.dashboard} 下没有可用的光谱文件")
            return 2
        _od = Path(args.out_root)
        _od.mkdir(parents=True, exist_ok=True)
        out = render_dashboard(scans, cfg, _od)
        if out is None:
            # render_dashboard 在 qc_dashboard.enable=false 或渲染失败时返回 None
            print("[P8] 未生成体检图：qc_dashboard.enable=false 或渲染失败"
                  "（原因见日志）")
            return 1
        n_bad = sum(1 for s in scans if s.excluded)
        print(f"[P8] 体检图 → {out}（{len(scans)} 条谱，剔除 {n_bad} 条）")
        return 0

    # A.M6（V2.x 审查）：批次文件里的 `dye:` 原来只有 GUI 读（V2.3 F1 修的就是
    # GUI 那条链），CLI 仍只认 `--dye`。这里在没有显式 --dye 时用批次命名兜底：
    # 借一次 dry run 的 scans 把"文件路径 → 组名"映射出来（io 有缓存，代价很小）。
    if group_dye is None and batch is not None and getattr(batch, "files", None):
        try:
            _scans_for_map, _ = run_core(args.data_dir, cfg, dry=True, batch=batch)
            _gd = batch.dye_map(_scans_for_map, args.data_dir)
        except Exception as err:                       # noqa: BLE001 —— 不影响主流程
            print(f"[批次] 染料命名映射失败，按默认命名：{type(err).__name__}: {err}")
            _gd = {}
        if _gd:
            group_dye = _gd
            print(f"[批次] 采用 dye_batch.yaml 里的染料命名：{len(_gd)} 组")
    rr = run_phi_pipeline(args.data_dir, cfg, out_root=args.out_root, batch=batch,
                          export_mode=args.export_mode, group_dye=group_dye,
                          debug=bool(args.debug))
    if rr.run_dir:
        print(console_summary(args.data_dir, rr.scans, rr.results, rr.phi,
                              rr.controls, rr.run_dir, cfg))
    else:
        print(rr.message)
    return 0 if rr.results else 1


if __name__ == "__main__":
    sys.exit(main())
