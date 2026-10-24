# -*- coding: utf-8 -*-
"""core 包覆盖率报告（无 pytest-cov 时的替代方案）。

环境里没有安装 ``pytest-cov`` / ``coverage``，因此本脚本用标准库 ``sys.settrace``
（与 ``trace`` 模块同一机制）+ ``trace._find_executable_linenos``（AST 可执行行
统计）自建一个极简行覆盖率计数器：只统计 ``core/*.py`` 中被执行到的行。

用法（工作目录 = 项目根）::

    .venv\\Scripts\\python.exe tests\\coverage_report.py            # 全量用例
    .venv\\Scripts\\python.exe tests\\coverage_report.py --fast     # 只跑 -m "not slow"

输出：每个 core 模块的 可执行行/已覆盖行/覆盖率，以及 core 包总体覆盖率。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = PROJECT_ROOT / "core"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

_executed: dict[str, set[int]] = {}


def _executable_lines(path: Path) -> set[int]:
    """用 AST 统计文件的“可执行行”（stdlib ``trace`` 的同名私有工具）。"""
    src = path.read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src, filename=str(path))
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                       ast.Expr)):
            lines.add(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            lines.add(node.lineno)                     # def/class 行本身可执行
        elif isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            lines.add(node.lineno)
    return {ln for ln in lines if ln > 0}


def _tracer():
    core_prefix = str(CORE_DIR).lower() + "\\"

    def local(frame, event, arg):
        if event == "line":
            fn = frame.f_code.co_filename
            if fn.lower().startswith(core_prefix):
                _executed.setdefault(fn, set()).add(frame.f_lineno)
        return local

    def glob(frame, event, arg):
        fn = frame.f_code.co_filename
        if event == "call" and fn.lower().startswith(core_prefix):
            return local
        return None

    return glob


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="core 包行覆盖率（sys.settrace 版）")
    ap.add_argument("--fast", action="store_true", help="只跑 -m 'not slow'")
    ap.add_argument("--deselect-slowest", action="store_true",
                    help="全量运行但跳过最慢的绘图/CLI 用例")
    args = ap.parse_args(argv)

    import pytest
    argv_pytest = ["tests", "-q", "-p", "no:cacheprovider"]
    if args.fast:
        argv_pytest += ["-m", "not slow"]
    elif args.deselect_slowest:
        argv_pytest += ["-k", "not cli and not a1 and not a4"]

    sys.settrace(_tracer())
    try:
        rc = pytest.main(argv_pytest)
    finally:
        sys.settrace(None)

    # ── 汇总 ──
    rows = []
    tot_exec = tot_hit = 0
    for p in sorted(CORE_DIR.glob("*.py")):
        try:
            exec_lines = _executable_lines(p)
        except SyntaxError:
            continue
        hit = _executed.get(str(p), set()) & exec_lines
        n_exec, n_hit = len(exec_lines), len(hit)
        tot_exec += n_exec
        tot_hit += n_hit
        rows.append((p.name, n_exec, n_hit, (100.0 * n_hit / n_exec) if n_exec else 0.0))

    print("\n" + "=" * 62)
    print("  core 包行覆盖率报告（sys.settrace，语句级）")
    print("=" * 62)
    print(f"  {'模块':<18}{'可执行行':>9}{'已覆盖':>9}{'覆盖率':>10}")
    print("-" * 62)
    for name, n_exec, n_hit, pct in rows:
        print(f"  {name:<18}{n_exec:>9}{n_hit:>9}{pct:>9.1f}%")
    print("-" * 62)
    total_pct = (100.0 * tot_hit / tot_exec) if tot_exec else 0.0
    print(f"  {'TOTAL':<18}{tot_exec:>9}{tot_hit:>9}{total_pct:>9.1f}%")
    print("=" * 62)
    print(f"  目标 ≥70% → {'达标' if total_pct >= 70.0 else '未达标'}")
    print(f"  pytest 退出码: {int(rc)}")
    return 0 if int(rc) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
