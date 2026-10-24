# -*- coding: utf-8 -*-
"""V2.6 P1：slow 子集分片并行执行器（tools/check.py --parallel N 的实现底座）。

设计依据：docs/测试提速与非主脚本重写方案.md §3.2 / §5。

* 分片单位是**测试文件**：先跑 pytest --collect-only -m slow -q 得到 slow 文件与
  每个文件的 slow 用例数；再按"文件内 slow 用例数"做 LPT 装箱（大的先放、装到
  当前最轻的片）——**同一文件不拆到两个进程**（模块级 fixture / 会话级缓存不跨
  进程共享，拆开会让"通过集"与单进程 run 产生差异）；
* 每个 shard 是**独立 pytest 进程**：独立 --basetemp=test_temp/pytest_shards/shardN，
  并用 -o addopts= 清掉 pytest.ini 的共享 basetemp，确保命令行 basetemp 是唯一
  来源（pytest 9 不会自动创建 basetemp，本模块负责建目录）；跑完**必删** basetemp；
* 本模块**不做通过判定**（不读 MIN_PASSED、不判红线）：只负责"收集 / 装箱 /
  并发执行 / 解析计数"。通过与否由 check.py 用既有判据（pytest 汇总行、
  rc=5 失败、MIN_PASSED、runs/ 快照）判——避免这里和门禁各说各话；
* **不写 runs/**；输出全在内存返回，失败日志由调用方决定落盘位置；
* 环境变量由调用方传入（与 check.py 的 STRICT_ENV 同一份），本模块不额外假设。

公开入口
========

* collect_slow_files —— collect-only 得到 {文件: slow 用例数} 与总数；
* pack_shards —— LPT 装箱成 N 个文件列表；
* run_shards —— 并发跑 N 个 pytest 进程，返回 ShardRun 列表；
* parse_counts —— 从 pytest 汇总行解析各项计数（供聚合判定用）。
"""
from __future__ import annotations

import concurrent.futures
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: shard 命令行公共参数（与 check.py 单进程档一致；-o addopts= 清掉 pytest.ini
#: 的共享 basetemp，避免"命令行 basetemp + ini basetemp"两份来源）。
COMMON_ARGS = ("-q", "-p", "no:cacheprovider", "-o", "addopts=")

#: 从 pytest 汇总行提取的计数字段（顺序无意义）
COUNT_KEYS = ("passed", "failed", "error", "skipped", "xfailed", "xpassed",
              "deselected")

_COUNT_PATTERNS = {
    "passed": r"(\d+) passed",
    "failed": r"(\d+) failed",
    "error": r"(\d+) errors?",
    "skipped": r"(\d+) skipped",
    "xfailed": r"(\d+) xfailed",
    "xpassed": r"(\d+) xpassed",
    "deselected": r"(\d+) deselected",
}


@dataclass
class ShardRun:
    """一个 shard 进程的执行结果（只记录事实，不做判定）。"""

    index: int
    files: list[str]
    basetemp: Path
    cmd: list[str] = field(default_factory=list)
    rc: "int | None" = None
    wall_s: float = 0.0
    output: str = ""
    timed_out: bool = False

    def counts(self) -> dict[str, int]:
        return parse_counts(self.output)

    def selected(self) -> int:
        """该 shard 实际选中执行的用例数（passed+failed+error+skipped+xfail+xpass）。"""
        c = self.counts()
        return sum(c[k] for k in ("passed", "failed", "error", "skipped",
                                  "xfailed", "xpassed"))


def parse_counts(out: str) -> dict[str, int]:
    """从 pytest 标准汇总行解析计数；没有汇总行时全 0。

    pytest 汇总行形如 "322 passed, 1 skipped in 154.20s" 或
    "1 failed, 2 passed in 3.10s"。只认**最后一条**含 passed/failed/error 的行
    （前面的用例输出里可能也出现这些词）。
    """
    res = {k: 0 for k in COUNT_KEYS}
    last = ""
    for line in (out or "").splitlines():
        if re.search(r"\d+ (passed|failed|error)", line):
            last = line
    if not last:
        return res
    for key, pat in _COUNT_PATTERNS.items():
        m = re.search(pat, last)
        if m:
            res[key] = int(m.group(1))
    return res


def collect_slow_files(py, root, env, timeout: float = 300.0):
    """跑 pytest --collect-only -m slow -q，返回 slow 文件与用例数。

    Returns:
        (files, total, output, error)：
        files = {相对 posix 路径: slow 用例数}（路径相对 root，如
        "tests/test_x.py"）；total = 汇总行里的 slow 收集总数；
        error 非空表示收集失败（调用方应判该步失败，不得静默退化单进程）。
    """
    cmd = [str(py), "-m", "pytest", "tests", *COMMON_ARGS, "-m", "slow",
           "--collect-only"]
    try:
        p = subprocess.run(cmd, cwd=str(root), env=env, text=True,
                           encoding="utf-8", errors="replace",
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {}, 0, "", f"collect-only 超时（{timeout:.0f}s）"
    out = (p.stdout or "") + (p.stderr or "")
    total = 0
    # pytest 9 带 -m 过滤时汇总行是 "323/971 tests collected (648 deselected)"
    # —— 第一个数才是**本次选中的 slow 条数**（旧正则只取到后一个 971）。
    m = re.search(r"(\d+)(?:/(\d+))? tests? collected", out)
    if m:
        total = int(m.group(1))
    files: dict[str, int] = {}
    for line in out.splitlines():
        s = line.strip()
        if "::" not in s or s.startswith(("=", "FAILED", "ERROR")):
            continue
        path = s.split("::", 1)[0].strip()
        if not path.endswith(".py"):
            continue
        files[path] = files.get(path, 0) + 1
    if p.returncode != 0:
        return files, total, out, (
            f"collect-only 失败（rc={p.returncode}，slow 汇总 {total} 条 / "
            f"{len(files)} 个文件）")
    if total <= 0 or not files:
        return files, total, out, (
            f"collect-only 没收到 slow 用例（汇总 {total} 条 / {len(files)} 个文件）——"
            "标记表达式或收集配置可能写错了")
    if sum(files.values()) != total:
        return files, total, out, (
            f"slow 明细计数 {sum(files.values())} 与汇总 {total} 不一致（收集输出异常）")
    return files, total, out, ""


def load_duration_weights(root, filename: str = "slow_file_durations.json") -> dict:
    """读取 test_temp 下由 --durations 记录生成的历史文件时长（秒）；缺失/损坏→{}。

    V2.7 FR-46.3：装箱优先用"文件历史时长"做 LPT 估计（比用例数更均衡）。
    该文件由一次 `pytest -m slow --durations=0` 的日志聚合生成（见 tools/README）。
    """
    import json
    path = Path(root) / "test_temp" / filename
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for key, val in (data or {}).items():
        try:
            out[str(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def pack_shards(files: dict[str, int], n: int,
                weights: "dict | None" = None) -> list[list[str]]:
    """把文件做 LPT 装箱，切成至多 n 片。

    * ``weights`` 给出时按"文件历史时长(秒)"装箱，否则按"文件内 slow 用例数"；
    * 大文件先放、每次放到当前负载最轻的一片（同负载取序号小的，结果**确定**）；
    * 同一文件不拆到两个 shard；文件数少于 n 时只返回非空片；
    * 返回的每个 shard 内文件按字典序排序（命令行可复现）。
    """
    paths = [str(k) for k in (files or {})]
    if not paths:
        return []
    use_w = weights or {}

    def _w(p: str) -> float:
        if p in use_w:
            return float(use_w[p])
        return float(files.get(p, 0) or 0)

    n = max(1, min(int(n), len(paths)))
    bins: list[dict] = [{"files": [], "load": 0.0} for _ in range(n)]
    for path in sorted(paths, key=lambda p: (-_w(p), p)):
        tgt = min(range(n), key=lambda i: (bins[i]["load"], i))
        bins[tgt]["files"].append(path)
        bins[tgt]["load"] += _w(path)
    return [sorted(b["files"]) for b in bins if b["files"]]


def _shard_cmd(py, files: list[str], basetemp: Path,
               junit_xml: "Path | None" = None,
               extra_args: tuple = (), marker: "str | None" = "slow") -> list[str]:
    cmd = [str(py), "-m", "pytest", *files, *COMMON_ARGS, *extra_args]
    if marker:
        cmd += ["-m", marker]
    cmd.append(f"--basetemp={basetemp}")
    if junit_xml is not None:
        cmd.append(f"--junitxml={junit_xml}")
    return cmd


def run_shards(py, root, shards: list[list[str]], env,
               basetemp_root, timeout: float = 1800.0,
               junit_dir=None, extra_args: tuple = (),
               marker: "str | None" = "slow") -> list[ShardRun]:
    """并发跑全部分片；每片独立进程 + 独立 basetemp，跑完删除 basetemp。

    Args:
        py / root: 解释器与工作目录（= 项目根，子进程 cwd）。
        shards: pack_shards 的输出（每片一个文件列表）。
        env: 完整子进程环境（调用方给 {**os.environ, **STRICT_ENV}）。
        basetemp_root: test_temp/pytest_shards 之类；每片用其下 shardN。
        timeout: 单个 shard 的超时秒数。
        junit_dir: 可选；给出时每片额外写 shardN.xml（验证"通过集"用；
            正常门禁不传，避免多余文件）。

    Returns:
        与 shards 同序的 ShardRun 列表（含 rc/输出/墙钟）。
    """
    root = Path(root)
    base_root = Path(basetemp_root)
    base_root.mkdir(parents=True, exist_ok=True)
    jdir = Path(junit_dir) if junit_dir else None
    if jdir is not None:
        jdir.mkdir(parents=True, exist_ok=True)

    def _one(idx: int, files: list[str]) -> ShardRun:
        base = base_root / f"shard{idx + 1}"
        shutil.rmtree(base, ignore_errors=True)
        base.mkdir(parents=True, exist_ok=True)
        junit_xml = None if jdir is None else jdir / f"shard{idx + 1}.xml"
        cmd = _shard_cmd(py, files, base, junit_xml, extra_args, marker)
        t0 = time.perf_counter()
        rc: "int | None" = None
        out = ""
        timed = False
        try:
            p = subprocess.run(cmd, cwd=str(root), env=env, text=True,
                               encoding="utf-8", errors="replace",
                               capture_output=True, timeout=timeout)
            rc = p.returncode
            out = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired:
            timed = True
            out = f"shard{idx + 1} 超时（{timeout:.0f}s）"
        wall = time.perf_counter() - t0
        shutil.rmtree(base, ignore_errors=True)          # ③ 结束清理（成败都清）
        return ShardRun(index=idx + 1, files=list(files), basetemp=base,
                        cmd=cmd, rc=rc, wall_s=wall, output=out,
                        timed_out=timed)

    runs: list[ShardRun] = []
    # subprocess.run 是阻塞调用；用线程池并发拉起 N 个进程即可（无 GIL 竞争点）。
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(shards)) as ex:
        futs = [ex.submit(_one, i, f) for i, f in enumerate(shards)]
        for f in futs:
            runs.append(f.result())
    try:
        base_root.rmdir()                                # 只删空目录
    except OSError:
        pass
    return runs
