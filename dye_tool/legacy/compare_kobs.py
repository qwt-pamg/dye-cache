#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_kobs.py v2.0 — 自动分组版
═══════════════════════════════════════════════════════════════════════════
分组原理 :
  每张谱取"母核区"(默认 450–650 nm, 避开 ADBA 379 nm 探针峰) →
  重采样到固定网格 → 峰值归一化 = 谱形指纹 →
  两两 RMS 距离 ≤ d_max 判为同组 → 自动分出实验组, 每组报告母核 λ_max。

  · 母核区残缺的谱 → 无法归组, 单独列出
  · 探针波长(379 nm)缺数据的谱 → 照常归组, 但不进拟合
  · 引擎函数 core_fingerprint / auto_group 可被其他脚本 import 复用

菜单: [1] 两组 k_obs 对比  [2] JACS 风格堆叠图  [3] 分组诊断  [q] 退出
config.yaml 可选覆盖:
  autogroup: {core_lo: 450, core_hi: 650, step: 5, d_max: 0.06}
"""
from __future__ import annotations

import csv
import os
import re
import sys
import filepick
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy.optimize import curve_fit

import singlet_o2_phi as sp
from run_menu import _input, browse_dir, default_root, open_folder

SCRIPT_DIR = Path(__file__).resolve().parent

if not hasattr(sp, "jacs_stack_figure"):
    print("[错误] singlet_o2_phi.py 缺少第一轮 JACS 补丁块 (jacs_stack_figure 等)。")
    try:
        input("按回车退出...")
    except EOFError:
        pass
    sys.exit(2)

sp.apply_jacs_style()
import matplotlib.pyplot as plt

AG_DEFAULT = {"core_lo": 450.0, "core_hi": 650.0, "step": 5.0, "d_max": 0.06}


# ════════════════════════ 1. 自动分组引擎 ════════════════════════

def core_fingerprint(wl, ab, lo, hi, step):
    """母核区谱形指纹 → (λ_max, 峰值归一化曲线); 区域数据不足 → (None, None)。"""
    wl = np.asarray(wl, float)
    ab = np.asarray(ab, float)
    m = (wl >= lo) & (wl <= hi) & np.isfinite(ab)
    if m.sum() < 10:
        return None, None
    grid = np.arange(lo, hi + 1e-9, step)
    fp = np.interp(grid, wl[m], ab[m])
    imax = int(np.argmax(fp))
    if fp[imax] <= 0:
        return None, None
    return float(grid[imax]), fp / fp[imax]


def _dist(a, b):
    return float(np.sqrt(np.mean((a - b) ** 2)))


def _suggest_label(names):
    """组内文件名公共前缀 → 建议标签 (只作默认值, 不参与分组)。"""
    pre = os.path.commonprefix(names)
    pre = re.sub(r"[^A-Za-z0-9]+[A-Za-z0-9]*$", "", pre)
    return pre if len(pre) >= 3 else (Path(names[0]).stem or "series")


def auto_group(items, ag):
    """items: [{name, t, wl, ab}] → (groups, 无法归组的文件名列表)。
    groups[i] = {members(按t排序), lam_max, label}"""
    fps = [core_fingerprint(it["wl"], it["ab"], ag["core_lo"],
                            ag["core_hi"], ag["step"]) for it in items]
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        if fps[i][1] is None:
            continue
        for j in range(i + 1, n):
            if fps[j][1] is None:
                continue
            if _dist(fps[i][1], fps[j][1]) <= ag["d_max"]:
                parent[find(j)] = find(i)

    clusters = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    groups, invalid = [], []
    for idx in clusters.values():
        if len(idx) == 1 and fps[idx[0]][1] is None:
            invalid.append(items[idx[0]]["name"])
            continue
        members = sorted((items[i] for i in idx), key=lambda m: m["t"])
        lams = [fps[i][0] for i in idx if fps[i][0] is not None]
        groups.append({"members": members,
                       "lam_max": float(np.median(lams)) if lams else None,
                       "label": _suggest_label([m["name"] for m in members])})
    groups.sort(key=lambda g: -len(g["members"]))
    return groups, invalid


# ════════════════════════ 2. 读取与交互 ════════════════════════

def _cfg():
    p = SCRIPT_DIR / "config.yaml"
    return sp.load_config(p if p.exists() else None)


def _scans_from_choice(cfg):
    """可视化选择数据来源 (无 tkinter 时自动退回控制台浏览)。
    → (scan列表, 来源描述) 或 None"""
    if filepick.HAS_TK:
        got = filepick.pick_data(default_root())
        if got is None:
            return None
        kind, val = got
        if kind == "dir":
            return sp.collect_scans(val, cfg), str(val)
        # 单独选文件: 按父目录解析, 只保留被选中的那几个
        want = {os.path.normcase(os.path.abspath(str(p))) for p in val}
        scans = []
        for parent in {p.parent for p in val}:
            for s in sp.collect_scans(parent, cfg):
                if os.path.normcase(os.path.abspath(str(s.path))) in want:
                    scans.append(s)
        return scans, f"{len(val)} 个所选文件"
    d = browse_dir(default_root())            # 无图形环境的老回退
    if d is None:
        return None
    return sp.collect_scans(d, cfg), str(d)


def load_items(cfg):
    """选数据 (可视化窗口) → 全部可用光谱 → (来源描述, items) 或 None。"""
    got = _scans_from_choice(cfg)
    if got is None:
        return None
    scans, src = got
    ok = [s for s in scans
          if not getattr(s, "excluded", False)
          and s.role not in ("background", "dark")]
    if not ok:
        print(f"  [!] {src}: 没有可用光谱文件")
        return None
    items, bad = [], 0
    for s in ok:
        arr = sp._spectrum_arrays(s)
        if arr is None:
            bad += 1
            continue
        items.append({"name": s.path.name, "t": float(s.t),
                      "wl": arr[0], "ab": arr[1]})
    if bad:
        print(f"  [!] {bad} 个文件读不出光谱, 已跳过")
    if not items:
        print("  [!] 没有可解析的光谱")
        return None
    print(f"  已载入 {len(items)} 张光谱  ← {src}")
    return src, items


def _show_groups(groups, invalid, ag):
    print(f"\n── 自动分组 (母核区 {ag['core_lo']:.0f}–{ag['core_hi']:.0f} nm, "
          f"谱形 RMS 距离 ≤ {ag['d_max']}) ──")
    for i, g in enumerate(groups, 1):
        ts = [m["t"] for m in g["members"]]
        lg = f"{g['lam_max']:.0f}" if g["lam_max"] is not None else "?"
        print(f"  [{i}] 母核 λ_max {lg} nm │ {len(g['members'])} 文件 │ "
              f"t={min(ts):.0f}~{max(ts):.0f} s │ {g['label']}")
    if invalid:
        print(f"  (母核区无有效数据、无法归组的 {len(invalid)} 个文件: "
              + ", ".join(invalid) + ")")
    print()


def _parse_pair(s, nmax):
    nums = [int(t) for t in re.split(r"[,\s]+", s.strip()) if t.isdigit()]
    if len(nums) != 2 or len(set(nums)) != 2:
        return None
    if not all(1 <= v <= nmax for v in nums):
        return None
    return nums


def _kw_split(items, ag):
    """回退方案: 按文件名关键词硬拆成两组。"""
    kw = _input("  关键词 (文件名包含它的为第 1 组): ")
    kw = (kw or "").strip().lower()
    if not kw:
        return None
    pa = [it for it in items if kw in it["name"].lower()]
    pb = [it for it in items if kw not in it["name"].lower()]
    if not (pa and pb):
        print("  [!] 关键词没能把文件分成两份")
        return None
    ga, _ = auto_group(pa, ag)
    gb, _ = auto_group(pb, ag)
    if not (ga and gb):
        print("  [!] 拆分后无法成组")
        return None
    return ga[0], gb[0]


def acquire(cfg):
    """选目录 → 自动分组 → 选定两组 → 标签。返回 (gA, gB) 或 None。"""
    got = load_items(cfg)
    if got is None:
        return None
    _d, items = got
    ag = dict(AG_DEFAULT, **(cfg.get("autogroup") or {}))
    groups, invalid = auto_group(items, ag)
    if not groups:
        print("  [!] 一个组都没分出来")
        return None
    _show_groups(groups, invalid, ag)

    gA = gB = None
    if len(groups) == 1:
        raw = _input("  只检测到 1 组。d=再选一个目录作第 2 组 / "
                     "k=文件名关键词拆分 / 回车=取消\n  > ")
        c = (raw or "").strip().lower()
        if c == "d":
            gA = groups[0]
            got2 = load_items(cfg)
            if got2 is None:
                return None
            _d2, items2 = got2
            g2, inv2 = auto_group(items2, ag)
            if not g2:
                print("  [!] 第二个目录没分出组")
                return None
            _show_groups(g2, inv2, ag)
            if len(g2) == 1:
                gB = g2[0]
            else:
                raw2 = _input(f"  第 2 组选哪个? (回车=1 / 1~{len(g2)}): ")
                c2 = (raw2 or "").strip()
                i2 = int(c2) if c2.isdigit() and 1 <= int(c2) <= len(g2) else 1
                gB = g2[i2 - 1]
        elif c == "k":
            pair = _kw_split(items, ag)
            if pair:
                gA, gB = pair
        if gB is None:
            print("  已取消")
            return None
    else:
        while gA is None:
            raw = _input("  选两组对比 (回车=1,2 / 如 2,3 / k=关键词 / q)\n  > ")
            if raw is None:
                return None
            c = raw.strip().lower()
            if c == "q":
                return None
            if c == "k":
                pair = _kw_split(items, ag)
                if pair:
                    gA, gB = pair
                continue
            pair = _parse_pair(c if c else "1,2", len(groups))
            if pair is None:
                print("  [!] 无效输入")
                continue
            gA, gB = groups[pair[0] - 1], groups[pair[1] - 1]

    for tag, g in (("第 1 组", gA), ("第 2 组", gB)):
        raw = _input(f"  {tag}标签 (回车={g['label']}): ")
        if raw and raw.strip():
            g["label"] = raw.strip()
    return gA, gB


def acquire_groups(cfg):
    """选目录 → 自动分组 → (groups, ag) 或 None。"""
    got = load_items(cfg)
    if got is None:
        return None
    _d, items = got
    ag = dict(AG_DEFAULT, **(cfg.get("autogroup") or {}))
    groups, invalid = auto_group(items, ag)
    if not groups:
        print("  [!] 一个组都没分出来")
        return None
    _show_groups(groups, invalid, ag)
    return groups, ag


# ════════════════════════ 3. 探针吸光度与拟合 ════════════════════════

def probe_abs(wl, ab, lam, win):
    """lam±win 内有效数据均值; 无有效数据 → None (如整段空白的残缺谱)。"""
    wl = np.asarray(wl, float)
    ab = np.asarray(ab, float)
    m = (wl >= lam - win) & (wl <= lam + win) & np.isfinite(ab)
    return float(np.mean(ab[m])) if m.sum() else None


def group_kinetics(g, cfg):
    """组 → (t, A@探针波长, 被跳过的文件名)。"""
    lam = cfg["wavelength"]["probe"]
    win = cfg["wavelength"]["probe_window"]
    pts, skipped = [], []
    for m in g["members"]:
        a = probe_abs(m["wl"], m["ab"], lam, win)
        if a is None or not np.isfinite(a):
            skipped.append(m["name"])
            continue
        pts.append((m["t"], a))
    pts.sort(key=lambda p: p[0])
    t = np.array([p[0] for p in pts], float)
    A = np.array([p[1] for p in pts], float)
    if len(set(t.tolist())) < len(t):
        print(f"  [!] [{g['label']}] 时间点重复, 请检查是否混入别的实验")
    return t, A, skipped


def excitation_abs(g, cfg):
    """最早一张谱在激发波长的吸光度 → (A_exc, 被吸收占比 f)。"""
    lam = cfg["wavelength"]["excitation"]
    win = cfg["wavelength"]["probe_window"]
    m0 = g["members"][0]                      # members 已按 t 排序
    a = probe_abs(m0["wl"], m0["ab"], lam, win)
    if a is None:
        return None, 0.0
    return a, (1 - 10 ** (-a) if a > 0 else 0.0)


def _exp2(t, A0, k):
    return A0 * np.exp(-k * t)


def _exp3(t, A0, k, c):
    return A0 * np.exp(-k * t) + c


def fit_series(t, A):
    """A(t) = A0·e^(-kt) (+c); 自动在 2/3 参数模型里选。"""
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
            p, pc = curve_fit(_exp3, t, A, p0=(A00, k0, c0), maxfev=20000,
                              bounds=([1e-9, 1e-9, 0.0], [A0max, kmax, cmax]))
            ss = float(np.sum((A - _exp3(t, *p)) ** 2))
            st = float(np.sum((A - A.mean()) ** 2))
            best = ("exp3", p, np.sqrt(np.diag(pc)),
                    1 - ss / st if st > 0 else 1.0)
        except Exception:
            best = None
    try:
        p2, pc2 = curve_fit(_exp2, t, A, p0=(A00, k0), maxfev=20000,
                            bounds=([1e-9, 1e-9], [A0max, kmax]))
        ss = float(np.sum((A - _exp2(t, *p2)) ** 2))
        st = float(np.sum((A - A.mean()) ** 2))
        r2 = 1 - ss / st if st > 0 else 1.0
        perr2 = np.sqrt(np.diag(pc2))
    except Exception:
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


# ════════════════════════ 4. 出图与输出 ════════════════════════

def _mnum(v, sig=3):
    if v == 0:
        return "0"
    a = abs(v)
    if 1e-3 <= a < 1e4:
        return f"{v:.{sig}g}"
    e = int(np.floor(np.log10(a)))
    return f"{v / 10 ** e:.{sig}g}\\times10^{{{e}}}"


def _equation_mathtext(f):
    k, A0 = _mnum(f["k"]), _mnum(f["A0"])
    if f["model"] == "exp3":
        return f"$A={A0}\\,e^{{-{k}\\,t}}+{_mnum(f['c'])}$"
    return f"$A={A0}\\,e^{{-{k}\\,t}}$"


def _eq_console(f):
    if f["model"] == "exp3":
        return f"A = {f['A0']:.3g}·e^(-{f['k']:.3g}·t) + {f['c']:.3g}"
    return f"A = {f['A0']:.3g}·e^(-{f['k']:.3g}·t)"


def make_compare_figure(s1, f1, s2, f2, ratio, out_dir, lam):
    fig, ax = plt.subplots(figsize=(3.35, 2.7))
    cols = ("#000000", "#C81E1E")
    for s, f, c, m in ((s1, f1, cols[0], "o"), (s2, f2, cols[1], "s")):
        ax.plot(s["t"], s["A"], ls="none", marker=m, ms=3.4,
                mfc="none", mec=c, mew=0.7, label=s["label"])
        if f["ok"]:
            tt = np.linspace(float(s["t"].min()), float(s["t"].max()), 240)
            yy = (_exp3(tt, f["A0"], f["k"], f["c"]) if f["model"] == "exp3"
                  else _exp2(tt, f["A0"], f["k"]))
            ax.plot(tt, yy, color=c, lw=1.0)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel(f"Absorbance at {lam:.0f} nm")
    ax.legend(loc="upper right", handletextpad=0.4)

    y = 0.16
    for s, f, c in ((s1, f1, cols[0]), (s2, f2, cols[1])):
        if f["ok"]:
            r2 = min(f["R2"], 0.9999)
            ax.text(0.03, y, _equation_mathtext(f) + f"   ($R^2={r2:.4f}$)",
                    transform=ax.transAxes, fontsize=6.3, color=c)
        else:
            ax.text(0.03, y, f"{s['label']}: fit failed",
                    transform=ax.transAxes, fontsize=6.3, color=c)
        y -= 0.075
    if ratio is not None:
        ax.text(0.03, 0.012, f"$k_1/k_2={_mnum(ratio)}$",
                transform=ax.transAxes, fontsize=6.3, color="0.15")

    fig.tight_layout(pad=0.4)
    for ext in ("png", "pdf"):
        fig.savefig(out_dir / f"compare_{lam:.0f}nm.{ext}")
    plt.close(fig)


def _write_csv(out, gA, fA, gB, fB, corrA, corrB):
    with open(out / "compare_summary.csv", "w", newline="",
              encoding="utf-8-sig") as fp:
        w = csv.writer(fp)
        w.writerow(["label", "core_lambda_max_nm", "n", "t_min_s", "t_max_s",
                    "model", "A0", "k_obs_s^-1", "k_err", "c", "R2",
                    "A_exc", "f_abs_exc", "k_corr_s^-1"])
        for g, f, cx in ((gA, fA, corrA), (gB, fB, corrB)):
            ts = [m["t"] for m in g["members"]]
            A_exc, fabs = cx if cx else (None, None)
            kcorr = f["k"] / fabs if (fabs and f["ok"]) else None
            w.writerow([g["label"], g["lam_max"], len(ts),
                        f"{min(ts):.0f}", f"{max(ts):.0f}",
                        f["model"] or "FAILED", f["A0"], f["k"], f["k_err"],
                        f["c"], f["R2"], A_exc, fabs, kcorr])


def _safe(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s)[:30] or "series"


def _out_dir(prefix):
    root = SCRIPT_DIR / "runs"
    root.mkdir(exist_ok=True)
    d = root / (prefix + "_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    d.mkdir()
    return d


def _maybe_open(path):
    raw = _input("  打开结果文件夹? (回车=y / n): ")
    if (raw or "").strip().lower() in ("", "y", "yes", "是"):
        open_folder(path)


# ════════════════════════ 5. 流程与菜单 ════════════════════════

def flow_compare():
    cfg = _cfg()
    got = acquire(cfg)
    if got is None:
        print("  已取消")
        return
    gA, gB = got
    lam = cfg["wavelength"]["probe"]

    tA, AA, skA = group_kinetics(gA, cfg)
    tB, AB, skB = group_kinetics(gB, cfg)
    for g, sk in ((gA, skA), (gB, skB)):
        if sk:
            print(f"  [!] [{g['label']}] {len(sk)} 个文件在 {lam:.0f} nm "
                  f"无有效数据, 不参与拟合:")
            for n in sk:
                print(f"      - {n}")
    if len(tA) < 4 or len(tB) < 4:
        print("  [!] 至少一组可用点 <4, 无法对比")
        return

    fA = fit_series(tA, AA)
    fB = fit_series(tB, AB)

    raw = _input("  按激发波长吸光度归一 (更接近相对 Φ_Δ)? y/N: ")
    do_corr = (raw or "").strip().lower() in ("y", "yes", "是")
    corr = {}
    if do_corr:
        corr = {1: excitation_abs(gA, cfg), 2: excitation_abs(gB, cfg)}
        if any(v[1] < 0.01 for v in corr.values()):
            print("  [!] 激发波长吸光度太低或无数据, 无法可靠归一, 已跳过")
            corr, do_corr = {}, False

    ratio = fA["k"] / fB["k"] if (fA["ok"] and fB["ok"]) else None
    ratio_c = ((fA["k"] / corr[1][1]) / (fB["k"] / corr[2][1])
               if (do_corr and ratio) else None)

    out = _out_dir("compare")
    make_compare_figure({"label": gA["label"], "t": tA, "A": AA}, fA,
                        {"label": gB["label"], "t": tB, "A": AB}, fB,
                        ratio, out, lam)
    for i, g in ((1, gA), (2, gB)):
        entries = [(m["t"], m["wl"], m["ab"]) for m in g["members"]]
        sp.jacs_stack_figure(entries, lam, out, f"{i}_{_safe(g['label'])}")
    _write_csv(out, gA, fA, gB, fB, corr.get(1), corr.get(2))

    print(f"\n════════ k_obs 对比 (ADBA @{lam:.0f} nm) ════════")
    for g, f, t in ((gA, fA, tA), (gB, fB, tB)):
        lg = f"{g['lam_max']:.0f}" if g["lam_max"] is not None else "?"
        print(f"[{g['label']}]  n={len(t)}, t={t.min():.0f}~{t.max():.0f} s, "
              f"母核 λ_max={lg} nm")
        if f["ok"]:
            print(f"   {_eq_console(f)}   R²={min(f['R2'], 0.9999):.4f}")
            ke = f" ± {f['k_err']:.2g}" if f["k_err"] else ""
            line = f"   k_obs = {f['k']:.3e}{ke} s^-1"
            if f["msg"]:
                line += f"   (!) {f['msg']}"
            print(line)
        else:
            print(f"   拟合失败: {f['msg']}")
    print("─" * 46)
    if ratio:
        print(f"k_obs({gA['label']}) / k_obs({gB['label']}) = {ratio:.3g}")
        print(f"→ {gA['label']} 的 ¹O₂ 生成速率约为 {gB['label']} 的 {ratio:.3g} 倍")
        if ratio_c:
            print(f"   按 {cfg['wavelength']['excitation']:.0f} nm 吸光归一后: "
                  f"{ratio_c:.3g}  (A_exc: {corr[1][0]:.3g} / {corr[2][0]:.3g})")
    else:
        print("  [!] 至少一组拟合失败, 无比值")
    print(f"\n  输出目录: {out}")
    _maybe_open(out)


def flow_stack():
    cfg = _cfg()
    got = acquire_groups(cfg)
    if got is None:
        return
    groups, ag = got
    raw = _input("  回车=每组一张 / 输入编号只画那一组: ")
    c = (raw or "").strip()
    sel = groups
    if c.isdigit() and 1 <= int(c) <= len(groups):
        sel = [groups[int(c) - 1]]
    out = _out_dir("stack")
    lam = cfg["wavelength"]["probe"]
    for i, g in enumerate(sel, 1):
        entries = [(m["t"], m["wl"], m["ab"]) for m in g["members"]]
        sp.jacs_stack_figure(entries, lam, out, f"{i}_{_safe(g['label'])}")
    print(f"  [OK] {len(sel)} 张堆叠图 -> {out}")
    _maybe_open(out)


def flow_diag():
    cfg = _cfg()
    got = acquire_groups(cfg)
    if got is None:
        return
    groups, ag = got
    lam = cfg["wavelength"]["probe"]
    win = cfg["wavelength"]["probe_window"]
    for i, g in enumerate(groups, 1):
        lg = f"{g['lam_max']:.0f}" if g["lam_max"] is not None else "?"
        print(f"\n  ── 组[{i}] {g['label']}  (母核 λ_max {lg} nm, "
              f"{len(g['members'])} 文件) ──")
        print(f"  {'t(s)':>7}  {'A@%.0f' % lam:>8}  文件")
        for m in g["members"]:
            a = probe_abs(m["wl"], m["ab"], lam, win)
            av = f"{a:.4f}" if (a is not None and np.isfinite(a)) else "--"
            print(f"  {m['t']:>7.0f}  {av:>8}  {m['name']}")
    print("\n  (诊断完毕, 不生成文件)")


def menu():
    print("\n════════════════════════════════════════\n"
          "  两组 k_obs 对比 / 自动分组 / JACS 绘图 v2\n"
          "════════════════════════════════════════\n"
          "  [1] 两组 k_obs 对比 (自动分组; 公式+R²+k 比值)\n"
          "  [2] JACS 风格光谱堆叠图 (自动分组)\n"
          "  [3] 分组诊断 (分组明细 + 探针波长数值表)\n"
          "  [q] 退出\n")
    while True:
        raw = _input("请选择: ")
        if raw is None:
            break
        c = raw.strip().lower()
        if c == "1":
            flow_compare()
        elif c == "2":
            flow_stack()
        elif c == "3":
            flow_diag()
        elif c in ("q", ""):
            break
        else:
            print("  [!] 请输入 1 / 2 / 3 / q")
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
        print("\n[意外错误] 请把以下信息发回:")
        traceback.print_exc()
        try:
            input("\n按回车退出...")
        except EOFError:
            pass