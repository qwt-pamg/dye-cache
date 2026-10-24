#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_menu.py — singlet_o2_phi.py 的交互式菜单前端

用法:  python run_menu.py     (需与 singlet_o2_phi.py 同目录)
流程:  菜单 → 浏览选目录 → 勾选文件 → 微调参数 → 自动分析 → 可打开结果文件夹
说明:  "选择文件" = 把未选中的文件加入忽略名单(记录于 run.log);
       不复制、不移动、不改动任何原始数据。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

try:
    import singlet_o2_phi as sp
    SCRIPT_DIR = Path(__file__).resolve().parent     # 脚本所在目录
    RUNS_ROOT = SCRIPT_DIR / "runs"                  # 结果一律存这里
except ImportError as e:
    print("[错误] 找不到 singlet_o2_phi.py 或缺少依赖:", e)
    print("      请将本文件与 singlet_o2_phi.py 放同一目录,")
    print("      并已安装 numpy / scipy / matplotlib")
    try:
        input("按回车退出...")
    except EOFError:
        pass
    sys.exit(2)

ROLE_ZH = {"sample": "样品", "reference": "参比", "background": "背景",
           "dark": "暗对照", "n2": "N2", "quencher": "淬灭剂", "blank": "空白"}


# ───────────────────────── 基础工具 ─────────────────────────
def _input(prompt: str):
    """读一行; EOF/Ctrl-C → None(视为退出)。"""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _pause():
    try:
        input("\n按回车继续...")
    except EOFError:
        pass


def _attach_console():
    """给 sp.logger 挂一个控制台处理器(正式分析时会被 setup_logging 接管)。"""
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    sp.logger.addHandler(h)


def open_folder(p: Path):
    try:
        if os.name == "nt":
            os.startfile(str(p))                     # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
    except Exception as err:
        print("  [!] 无法自动打开, 请手动前往:", p, "(", err, ")")


def default_root() -> Path:
    for cand in (SCRIPT_DIR / "data", SCRIPT_DIR / "Data", SCRIPT_DIR / "DATA"):
        if cand.is_dir():
            return cand
    return SCRIPT_DIR


def _count_spec(d: Path) -> int:
    n = 0
    try:
        for p in d.rglob("*"):
            if (p.is_file() and p.suffix.lower() in (".txt", ".csv")
                    and not p.name.startswith("_")):
                n += 1
                if n > 99:                            # 只需知道"有文件"
                    break
    except PermissionError:
        pass
    return n


def parse_ranges(s: str):
    """'1,3,5-8' → [1,3,5,6,7,8]; 无法识别 → None。"""
    out = set()
    for part in s.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.strip().isdigit() and b.strip().isdigit():
                lo, hi = sorted((int(a), int(b)))
                out.update(range(lo, hi + 1))
            else:
                return None
        elif part.isdigit():
            out.add(int(part))
        else:
            return None
    return sorted(out) if out else None


# ───────────────────────── 目录浏览器 ─────────────────────────
def browse_dir(start: Path):
    """逐级浏览选择数据目录; 返回 Path, 取消 → None。"""
    cur = start.resolve()
    while True:
        try:
            subs = sorted(d for d in cur.iterdir()
                          if d.is_dir() and not d.name.startswith((".", "_")))
        except PermissionError:
            print("  [!] 无权限访问该目录")
            subs = []
        print(f"\n── 选择数据目录 (当前: {cur}) ────────")
        print("  [0] ✔ 就用当前目录")
        for i, d in enumerate(subs, 1):
            print(f"  [{i:>2}] {d.name}/  ({_count_spec(d)} 个光谱文件)")
        print("  [u] 返回上级    [r] 手动输入路径    [q] 取消")
        raw = _input("  > ")
        if raw is None:
            return None
        c = raw.strip().lower()
        if c in ("q", ""):
            return None
        if c == "0":
            return cur
        if c == "u":
            cur = cur.parent if cur.parent != cur else cur
            continue
        if c == "r":
            raw2 = _input("  路径: ")
            if raw2 is None:
                return None
            p = Path(raw2.strip()).expanduser()
            if p.is_dir():
                cur = p.resolve()
            else:
                print("  [!] 目录不存在")
            continue
        if c.isdigit() and 1 <= int(c) <= len(subs):
            cur = subs[int(c) - 1].resolve()
            continue
        print("  [!] 无效输入")


# ───────────────────────── 文件勾选 ─────────────────────────
def pick_files(data_dir: Path, cfg: dict):
    """列出目录内文件供勾选。返回 (cfg, 可用总数, 选中数); 取消 → None。

    实现: 未选中的文件名写入 cfg['io']['exclude'], run_core 扫描时跳过。
    """
    scans = sp.collect_scans(data_dir, cfg)
    if not scans:
        print("  [!] 该目录没有找到光谱文件")
        return None
    ok = [s for s in scans if not s.excluded]
    bad = [s for s in scans if s.excluded]
    print(f"\n  共 {len(scans)} 个文件, 可用 {len(ok)} 个"
          + (f" (坏文件 {len(bad)} 个, 将自动跳过, 原因见日志)" if bad else ""))
    print(f"  {'#':>3}  {'文件':<42}{'组别':<28}{'角色':<8}{'t(s)':>7}")
    for i, s in enumerate(scans, 1):
        rel = str(s.path.relative_to(data_dir))[:42]
        role = ROLE_ZH.get(s.role, s.role)
        mark = "" if not s.excluded else "  ← 剔除"
        print(f"  {i:>3}  {rel:<42}{s.group[:26]:<28}{role:<8}{s.t:>7.0f}{mark}")
    while True:
        raw = _input("\n  选择要分析的文件: 回车=全部 | 编号(如 1,3,5-8) | q=取消\n  > ")
        if raw is None:
            return None
        c = raw.strip().lower()
        if c in ("q", "quit"):
            return None
        if c in ("", "a", "all"):
            return cfg, len(ok), len(ok)
        idx = parse_ranges(c)
        if not idx:
            print("  [!] 无法识别, 请输入如 1,3,5-8 (回车=全部)")
            continue
        if idx[0] < 1 or idx[-1] > len(scans):
            print("  [!] 编号超出范围")
            continue
        chosen = [scans[i - 1] for i in idx]
        chosen_ok = [s for s in chosen if not s.excluded]
        if not chosen_ok:
            print("  [!] 选中的都是坏文件, 请重选")
            continue
        ids = {id(s) for s in chosen}
        skip = [s.path.name for s in scans if id(s) not in ids]
        cfg["io"]["exclude"] = tuple(["_"] + skip)     # 未选中 → 忽略名单
        print(f"  [OK] 本次分析 {len(chosen_ok)}/{len(ok)} 个文件 "
              f"(其余 {len(skip)} 个忽略, 已记录)")
        return cfg, len(ok), len(chosen_ok)


# ───────────────────────── 参数微调 ─────────────────────────
def tune_settings(cfg: dict):
    """分析前的关键参数速览与微调; 返回 cfg, 取消 → None。"""
    while True:
        k = cfg["kinetics"]
        q = cfg["quantum_yield"]
        ov = cfg["plot"]["spectra_overlay"]
        sel = ov.get("select_times", "all")
        if isinstance(sel, (list, tuple)):
            sel_txt = ",".join(f"{x:g}" for x in sel)
        elif isinstance(sel, dict):
            sel_txt = "自定义(yaml)"
        else:
            sel_txt = "全部"
        print("\n  ── 关键参数 ─────────────────────────────")
        print(f"  探针窗口 {cfg['wavelength']['probe']:.0f}±"
              f"{cfg['wavelength']['probe_window']:.0f} nm | "
              f"激发 {cfg['wavelength']['excitation']:.0f} nm | "
              f"自动截断 {'开' if k['auto_truncate'] else '关'} | "
              f"叠加图时间点 {sel_txt} | Φ_ref {q['phi_ref']:g}")
        raw = _input("  回车=开始分析 | 1 截断开/关 | 2 叠加图时间点 | "
                     "3 Φ_ref | 0 取消\n  > ")
        if raw is None:
            return None
        c = raw.strip()
        if c == "":
            return cfg
        if c == "1":
            k["auto_truncate"] = not k["auto_truncate"]
        elif c == "2":
            v = _input("    时间点(秒), 逗号分隔, 如 1,60,300,600; 或 all=全部: ")
            if v is None:
                return None
            v = v.strip().lower()
            if v in ("", "all"):
                ov["select_times"] = "all"
            else:
                try:
                    ov["select_times"] = [float(x) for x in
                                          v.replace("，", ",").split(",") if x.strip()]
                except ValueError:
                    print("    [!] 无法识别")
        elif c == "3":
            v = _input(f"    参比物 Φ_Δ 文献值 (当前 {q['phi_ref']:g}): ")
            if v is None:
                return None
            try:
                q["phi_ref"] = float(v)
                print("    (提示: Φ 计算还需数据中含 ref 组, 否则仍显示 N/A)")
            except ValueError:
                print("    [!] 无法识别")
        elif c == "0":
            return None
        else:
            print("  [!] 无效输入")


# ───────────────────────── 正式分析 ─────────────────────────
def do_run(data_dir: Path, cfg: dict, n_total: int, n_sel: int) -> Path:
    run_dir = sp.make_run_dir(RUNS_ROOT)
    col = sp.setup_logging(run_dir / "run.log", False)
    sp.logger.info("交互模式 | 数据目录: %s | 用户选中 %d/%d 个文件",
                   data_dir, n_sel, n_total)
    scans, results = sp.run_core(data_dir, cfg)
    phi = sp.compute_phi(results, cfg)
    controls = sp.judge_controls(results, cfg)
    sp.write_results_csv(run_dir / "results.csv", results, phi, controls)
    sp.write_fit_summary(run_dir / "fit_summary.json", scans, results, phi, controls, cfg)
    sp.plot_all(scans, results, phi, controls, cfg, run_dir)
    sp.write_report(run_dir / "report.md", data_dir, scans, results, phi,
                    controls, cfg, col.records)
    sp.print_console_summary(data_dir, scans, results, phi, controls, run_dir, cfg)
    if not results:
        print("  [!] 没有可拟合的数据, 详情见日志")
    return run_dir


# ───────────────────────── 各菜单流程 ─────────────────────────
def flow_analyze(dry: bool):
    cfg = sp.load_config(SCRIPT_DIR / "config.yaml" if (SCRIPT_DIR / "config.yaml").exists() else None)
    d = browse_dir(default_root())
    if d is None:
        print("  已取消")
        return
    pk = pick_files(d, cfg)
    if pk is None:
        print("  已取消")
        return
    cfg, n_total, n_sel = pk
    if dry:
        run_dir = sp.make_run_dir(RUNS_ROOT)
        sp.setup_logging(run_dir / "run.log", False)
        sp.logger.info("dry-run | 数据: %s | 选中 %d/%d", d, n_sel, n_total)
        scans, _ = sp.run_core(d, cfg, dry=True)
        sp._print_dry_run(scans)
        print(f"  日志: {run_dir / 'run.log'}")
        raw = _input("\n  继续正式分析? (回车=y / n): ")
        if raw is None or raw.strip().lower() not in ("", "y", "yes", "是"):
            return
    cfg2 = tune_settings(cfg)
    if cfg2 is None:
        print("  已取消")
        return
    run_dir = do_run(d, cfg2, n_total, n_sel)
    raw = _input("\n  打开结果文件夹? (回车=y / n): ")
    if raw is not None and raw.strip().lower() in ("", "y", "yes", "是"):
        open_folder(run_dir)


def flow_synthetic():
    raw = _input("  输出目录 (回车=data/synthetic_demo): ")
    if raw is None:
        return
    d = Path(raw.strip()) if raw.strip() else SCRIPT_DIR / "data" / "synthetic_demo"
    t = sp.make_synthetic(d)
    print(f"  [OK] 已生成 -> {d}  (k_true={t['k_true']:.4e} s^-1, "
          f"Phi_true={t['phi_true']:.2f})")
    print("      回到菜单用 [2] 选择该目录, 即可分析验证")


def flow_replot():
    root = RUNS_ROOT
    if not RUNS_ROOT.is_dir():
        print("  [!] 没有 runs 目录")
        return
    runs = sorted(d for d in root.iterdir()
                  if d.is_dir() and (d / "fit_summary.json").exists())
    if not runs:
        print("  [!] runs 下没有可用结果(缺 fit_summary.json)")
        return
    for i, d in enumerate(runs, 1):
        print(f"  [{i:>2}] {d.name}")
    raw = _input("  选择要重绘的运行 (q=取消): ")
    if raw is None:
        return
    c = raw.strip().lower()
    if c in ("q", ""):
        return
    if not (c.isdigit() and 1 <= int(c) <= len(runs)):
        print("  [!] 无效输入")
        return
    d = runs[int(c) - 1]
    try:
        j = json.loads((d / "fit_summary.json").read_text(encoding="utf-8"))
    except Exception as err:
        print("  [!] 读取失败:", err)
        return
    sp.plot_all(sp.scans_from_json(j), sp.results_from_json(j),
                j.get("phi") or {}, j.get("controls") or {},
                j.get("config") or sp.DEFAULT_CFG, d)
    print(f"  [OK] 图已重绘 -> {d / 'figures'}")
    raw = _input("  打开 figures 文件夹? (回车=y / n): ")
    if raw is not None and raw.strip().lower() in ("", "y", "yes", "是"):
        open_folder(d / "figures")


# ───────────────────────── 主菜单 ─────────────────────────
def menu():
    _attach_console()
    banner = (f"\n══════════════════════════════════════════════\n"
              f"   ¹O₂ 相对量子产率 Φ_Δ 计算 — 交互菜单\n"
              f"   (singlet_o2_phi v{sp.VERSION})\n"
              f"══════════════════════════════════════════════\n"
              f"  [1] 运行内置自检 (无需数据, 约 20 秒)\n"
              f"  [2] 选择数据目录并分析\n"
              f"  [3] 仅解析检查 dry-run (不拟合不出图)\n"
              f"  [4] 生成 synthetic 测试数据\n"
              f"  [5] 重新绘图 (从已有 runs 结果)\n"
              f"  [q] 退出\n")
    while True:
        print(banner)
        raw = _input("请选择: ")
        if raw is None:
            break
        c = raw.strip().lower()
        if c == "1":
            sp.self_test()
            _pause()
        elif c == "2":
            flow_analyze(dry=False)
        elif c == "3":
            flow_analyze(dry=True)
        elif c == "4":
            flow_synthetic()
        elif c == "5":
            flow_replot()
        elif c in ("q", ""):
            break
        else:
            print("  [!] 无效输入, 请输入 1-5 或 q")
    print("再见!")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        menu()
    except Exception:
        import traceback
        print("\n[意外错误] 请把以下信息发回, 便于调试:")
        traceback.print_exc()
        try:
            input("\n按回车退出...")
        except EOFError:
            pass