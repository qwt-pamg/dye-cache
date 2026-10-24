# -*- coding: utf-8 -*-
"""dye_tool V2.8 个人版两档检查入口（M1/FR-54.2）。

档位：
    fast（默认）   BOM 检查 + pyflakes + 精选 pytest（金线/核心单测/GUI 冒烟级） 目标 <30 s
    --full          同上静态检查 + 全量 pytest（--parallel 4 默认开，NO_PARALLEL=1 回退）目标 <90 s
    --release       full + PyInstaller 打包（packaging/build.ps1，两道具门已删除）
                    + 打包后一次性启动检查（A21 迁移；不跑两道具）

纪律：任一步非零退出即失败；不写 runs/（门禁报告只打印）；不吞 pytest 退出码。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")
if not Path(PY).is_file():
    PY = sys.executable

STRICT_ENV = {
    "PYTHONIOENCODING": "utf-8",
    "QT_QPA_PLATFORM": "offscreen",
    "QT_QPA_FONTDIR": r"C:\Windows\Fonts",
    "MPLBACKEND": "Agg",
    "DYE_TOOL_SMOKE": "1",
}

#: fast 档精选集（金线 + 核心单测 + GUI 冒烟级；实测 <30 s 目标）
FAST_TESTS = [
    "tests/test_display_prep.py",       # FR-49 显示层契约
    "tests/test_display_presets.py",    # FR-53 ③④ 显示预设
    "tests/test_io.py",                 # 解析适配器
    "tests/test_qc.py",
    "tests/test_kinetics.py",
    "tests/test_peaks.py",
    "tests/test_photophysics.py",
    "tests/test_fluorescence_qy.py",
    "tests/test_extinction.py",
    "tests/test_results.py",
    "tests/test_config.py",
    # 工作台/帮助注册表/悬停质量用例在 --full 跑（V2.9.6R2：fast 需稳定 <30s）
    # V2.9.5 FR-73 预览控制器/真实数据 E2E 体量较大，改在 --full 跑（R3-O1 fast<30s）
]

#: full 并行分片数默认值（NO_PARALLEL=1 回退单进程）
DEFAULT_PARALLEL = 4


def _env() -> dict:
    return {**os.environ, **STRICT_ENV}


def _cmd(*parts: str) -> list[str]:
    return [PY, *parts]


def _p(summary: str) -> None:
    print(f"\n{'=' * 72}\n[{summary}]\n{'=' * 72}", flush=True)


def _capture(cmd: list[str], env: dict, timeout: float) -> dict:
    t0 = time.perf_counter()
    try:
        cp = subprocess.run(cmd, cwd=str(ROOT), env=env, text=True,
                            encoding="utf-8", errors="replace",
                            capture_output=True, timeout=timeout)
        return {"cmd": cmd, "rc": cp.returncode, "out": (cp.stdout or "") + (cp.stderr or ""),
                "wall": time.perf_counter() - t0, "timed_out": False}
    except subprocess.TimeoutExpired as err:
        out = (err.stdout or "") + (err.stderr or "")
        return {"cmd": cmd, "rc": -1, "out": out, "wall": time.perf_counter() - t0,
                "timed_out": True}


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def _run(cmd: list[str], label: str, *, timeout: float = 3600.0,
         tail: int = 12) -> tuple[bool, str]:
    _p(label + " " + " ".join(cmd))
    res = _capture(cmd, _env(), timeout)
    out = str(res["out"])
    if tail and out:
        for ln in out.strip().splitlines()[-tail:]:
            print("    " + ln, flush=True)
    ok = (not res["timed_out"]) and int(res["rc"]) == 0
    detail = "超时" if res["timed_out"] else f"退出码 {res['rc']}"
    print(f"    → {'通过' if ok else '失败'}（{float(res['wall']):.1f}s，{detail}）",
          flush=True)
    return ok, detail


def _bom_check() -> tuple[bool, str]:
    bad = []
    files = sorted((ROOT / "packaging").glob("*.ps1"))
    vi = ROOT / "packaging" / "version_info.txt"
    if vi.is_file():
        files.append(vi)
    for p in files:
        raw = p.read_bytes()
        if not raw.startswith(b"\xef\xbb\xbf"):
            bad.append(p.name)
    return (not bad, "缺 UTF-8 BOM：" + ", ".join(bad) if bad else "BOM 正常")


def _run_bom() -> bool:
    _p("PowerShell/version_info 编码（UTF-8 BOM）")
    ok, detail = _bom_check()
    print("    " + detail)
    print(f"    → {'通过' if ok else '失败'}（0.0s）")
    return ok



def _run_spec_hash() -> bool:
    """打包 spec 内容哈希基线（A20 保留的轻量包装门；不替代 build）。"""
    _p("打包清单 spec 哈希（唯一真相 = packaging/dye_tool.spec.sha256）")
    import hashlib
    spec = ROOT / "packaging" / "dye_tool.spec"
    base = ROOT / "packaging" / "dye_tool.spec.sha256"
    try:
        want = base.read_text(encoding="utf-8").split()[0].upper()
        got = hashlib.sha256(spec.read_bytes()).hexdigest().upper()
    except OSError as err:
        print(f"    → 失败：{err}")
        return False
    ok = bool(want) and got == want
    print(f"    spec={got[:12]}… 基线={want[:12]}…" if want else "    （基线为空）")
    print(f"    → {'通过' if ok else '失败'}（0.0s）")
    return ok


def _run_pyflakes() -> bool:
    return _run(_cmd("-m", "pyflakes", "core", "ui", "tools", "tests", "app.py"),
                "静态检查 pyflakes", timeout=300.0, tail=20)[0]


def _pytest_fast() -> bool:
    return _run(_cmd("-m", "pytest", *FAST_TESTS, "-q", "-p", "no:cacheprovider"),
                "fast pytest（金线 + 核心单测 + GUI 冒烟级）", timeout=600.0, tail=12)[0]


def _parse_counts(out: str) -> dict:
    import re
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped",
                             "xfailed", "xpassed")}
    m = re.search(r"(\d+) passed", out)
    if m:
        counts["passed"] = int(m.group(1))
    for key in ("failed", "error", "skipped", "xfailed", "xpassed"):
        m = re.search(rf"(\d+) {key}", out)
        if m:
            counts[key] = int(m.group(1))
    return counts


def _collect_total() -> tuple[int, str]:
    res = _capture(_cmd("-m", "pytest", "tests", "--collect-only", "-q",
                        "-p", "no:cacheprovider"), _env(), 300.0)
    out = str(res["out"])
    if (res["timed_out"] or int(res["rc"]) != 0):
        return -1, f"collect-only 退出码 {res['rc']}：{_last_line(out)}"
    import re
    m = re.search(r"(\d+) tests? collected", out)
    return (int(m.group(1)) if m else -1, _last_line(out))


def _pytest_full_parallel(n: int) -> bool:
    _p("全量 pytest（{n} 片并行；tools/parallel_pytest.py 底座）".replace("{n}", str(n)))
    import parallel_pytest  # 同目录工具（tools/ 不是包，check.py 运行时把 tools/ 加入 sys.path）
    files = {f"tests/{p.name}": 1 for p in sorted((ROOT / "tests").glob("test_*.py"))}
    if not files:
        print("    → 失败：tests/ 下没有 test_*.py")
        return False
    shards = parallel_pytest.pack_shards(files, n)
    base = ROOT / "test_temp" / "check_parallel"
    shutil.rmtree(base, ignore_errors=True)
    base.mkdir(parents=True, exist_ok=True)
    total, collect_note = _collect_total()
    if total < 0:
        print(f"    → 失败：collect-only 失败/无法解析（{collect_note}）")
        shutil.rmtree(base, ignore_errors=True)
        return False
    runs = parallel_pytest.run_shards(PY, ROOT, shards, _env(), base, 1800.0,
                                      marker=None)
    shutil.rmtree(base, ignore_errors=True)
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped",
                             "xfailed", "xpassed")}
    ok = True
    selected = 0
    for run in runs:
        c = run.counts()
        for k in counts:
            counts[k] += c[k]
        selected += run.selected()
        # V28-M2：rc=5（未收集到用例）不是成功；shard 只接受 rc==0。
        good = (not run.timed_out) and (run.rc == 0)
        print(f"    shard{run.index}: rc={run.rc} "
              f"{_last_line(run.output)}（{run.wall_s:.1f}s）", flush=True)
        if not good:
            ok = False
            for ln in run.output.strip().splitlines()[-15:]:
                print("      " + ln)
    if counts["failed"] or counts["error"]:
        ok = False
        print(f"    （**失败：failed={counts['failed']} error={counts['error']}**）")
    if selected != total:
        ok = False
        print(f"    （**分片选中 {selected} 条 ≠ collect-only {total} 条**）")
    print(f"    合计：{counts['passed']} passed / {counts['failed']} failed / "
          f"{counts['error']} error / {counts['skipped']} skipped / "
          f"{counts['xfailed']} xfailed；selected {selected}/{total}")
    print(f"    → {'通过' if ok else '失败'}" + "（并行）")
    return ok


def _pytest_full_serial() -> bool:
    return _run(_cmd("-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"),
                "全量 pytest（单进程）", timeout=1800.0, tail=10)[0]


def _resolve_parallel(args) -> int:
    if os.environ.get("NO_PARALLEL") == "1":
        return 1
    if args.parallel is not None:
        return max(1, int(args.parallel))
    return DEFAULT_PARALLEL


def _release_launch_check() -> bool:
    _p("打包后一次性启动检查（A21 迁移；不跑两道具）")
    exe = ROOT / "dist" / "dye_tool" / "dye_tool.exe"
    if not exe.is_file():
        print("    → 失败：打包产物不存在")
        return False
    import subprocess as sp
    # DYE_TOOL_SMOKE=1 会让正常启动走 app.py 的内置冒烟后退出（不是白屏），
    # 启动检查必须模拟真实双击：保留 offscreen/字体，去掉冒烟开关。
    env = {k: v for k, v in _env().items() if k != "DYE_TOOL_SMOKE"}
    try:
        p = sp.Popen([str(exe)], cwd=str(exe.parent), env=env,
                     stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    except OSError as err:
        print(f"    → 失败：无法启动 {err}")
        return False
    try:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if p.poll() is not None:
                print(f"    → 失败：exe 提前退出 rc={p.returncode}")
                return False
            time.sleep(0.2)
        print("    → 通过：exe 启动后 12s 内保持运行（已终止）")
        return True
    finally:
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10)
            except sp.TimeoutExpired:
                p.kill()
        # V28R-O2：windowed exe 启动会把 stdout 重定向到 exe 同级的
        # dye_tool_last_run.log；这是启动检查副产物，不应留在交付 dist 里。
        for _log_name in ("dye_tool_last_run.log", "debug.log"):
            try:
                (exe.parent / _log_name).unlink(missing_ok=True)
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="dye_tool V2.8 两档检查")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--fast", action="store_true", help="静态检查 + 精选 pytest（默认）")
    g.add_argument("--full", action="store_true", help="静态检查 + 全量 pytest")
    g.add_argument("--release", action="store_true", help="full + 打包 + 启动检查")
    ap.add_argument("--parallel", type=int, metavar="N", default=None,
                    help=f"full 并行分片数（默认 {DEFAULT_PARALLEL}；NO_PARALLEL=1 回退单进程）")
    args = ap.parse_args(argv)
    tier = "release" if args.release else ("full" if args.full else "fast")
    print(f"dye_tool 检查 V2.8（个人版）：档位 = {tier}  工作区 {ROOT}")
    t0 = time.perf_counter()
    steps: list[tuple[str, bool]] = []
    ok = _run_bom()
    steps.append(("BOM 编码", ok))
    if ok:
        ok = _run_spec_hash()
        steps.append(("spec 哈希", ok))
    if ok:
        ok = _run_pyflakes()
        steps.append(("pyflakes", ok))
    if ok:
        if tier == "fast":
            ok = _pytest_fast()
        else:
            n = _resolve_parallel(args)
            ok = _pytest_full_serial() if n <= 1 else _pytest_full_parallel(n)
        steps.append((f"pytest {tier}", ok))
    if ok and tier == "release":
        ok = _run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(ROOT / "packaging" / "build.ps1")],
                  "打包 exe（PyInstaller；两道具门已删除）", timeout=1800.0, tail=8)[0]
        steps.append(("打包 exe", ok))
        if ok:
            ok = _release_launch_check()
            steps.append(("启动检查", ok))
    dt = time.perf_counter() - t0
    print(f"\n{'=' * 72}\n  汇总：{sum(1 for _n, v in steps if v)}/{len(steps)} 步通过"
          f"  档位 {tier}  总耗时 {dt:.1f}s")
    for name, good in steps:
        print(f"    [{'OK ' if good else 'FAIL'}] {name}")
    return 0 if all(v for _n, v in steps) else 1


if __name__ == "__main__":
    raise SystemExit(main())
