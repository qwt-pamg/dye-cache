# -*- coding: utf-8 -*-
"""P9：旧 ``runs/`` 目录的染料化整理。

背景
----
V1.0 的产物布局是三种**扁平**形态，V1.1 起统一为 ``runs/<染料>/<run_id>/``：

===================================  ==========================================
V1.0 旧形态                           去向
===================================  ==========================================
``runs/run_<时间戳>/``                 ``runs/<染料>/<时间戳>/``
``runs/stack/<染料>/<时间戳>/``        ``runs/<染料>/<时间戳>/``
``runs/compare/<染料>/<时间戳>/``      ``runs/<染料>/<时间戳>/``
``runs/spectra/_multi/<时间戳>/``      ``runs/_multi/<时间戳>/``（本就是新形态）
===================================  ==========================================

设计原则（都为了"老数据不能丢、不能错位"）
------------------------------------------
1. **两阶段**：:func:`plan_reorg` 只读盘出计划，:func:`apply_reorg` 才动盘。
   ``cfg['dye']['reorg']['dry_run']`` 默认 ``True``——用户先看计划再执行。
2. **只移动、不删除**：整个过程不 ``unlink`` 任何数据文件；旧空壳目录按
   ``keep_legacy`` 移进 ``_legacy/`` 而非删除。
3. **撞名不覆盖**：目标 run 目录已存在时用 :func:`core.dyes._unique` 的
   ``_1/_2`` 规则改名，并写进报告的"冲突"一列。**绝不 merge 覆盖**。
4. **跨盘回退**：``shutil.move`` 在跨盘时会失败，回退为 copy+删源；
   源删不掉（例如被占用）时保留源并在报告里标 FAILED，**不假装成功**。
5. **可解释**：返回的每一条都带 ``reason``（中文人话），失败项带 ``error``。
"""
from __future__ import annotations

import csv
import logging
import re
import shutil
from pathlib import Path
from typing import Optional

from .dyes import (FUNC_SUBDIRS, MULTI_DIR, _unique, resolve_dye_name,
                   sanitize_dye_name)
from ..paths import is_reparse_point

log = logging.getLogger("dye_tool.reorg")

#: ``run_<时间戳>`` 里的时间戳（V1.0 扁平目录）
_LEGACY_STAMP_RE = re.compile(r"^run_(\d{8}-\d{6})$")
#: ``<时间戳>[_<序号>]``（V1.1 run 目录名）
_RUN_ID_RE = re.compile(r"^(\d{8}-\d{6})(?:_(\d+))?$")


# ═══════════════════════════ 小工具 ═══════════════════════════
def _is_run_id(name: str) -> bool:
    """``20261015-141455`` / ``20261015-141455_2`` 形态。"""
    return bool(_RUN_ID_RE.match(str(name)))


def _stamp_of(run_id: str) -> str:
    """从 run 目录名取纯时间戳部分（去掉 ``_2`` 之类的序号）。"""
    m = _RUN_ID_RE.match(str(run_id))
    return m.group(1) if m else str(run_id)


def _is_link(p: Path) -> bool:
    """是否软链接 / junction / 其它**重解析点**（= :func:`core.paths.is_reparse_point`）。

    L11（评审记录）：``is_dir()`` 与 ``rglob`` 会**跟随 junction**。实测
    ``runs/spectra/DyeJ`` 指向 runs/ 之外的目录时会被当成普通染料目录，
    apply 会把 junction **目标**里的真实 run 目录搬进 runs/——只移不删，
    但等于重组了 runs/ 之外的第三方数据，这是整理工具不该做的事。
    这类目录一律跳过并在报告里说明。

    **G6（第二轮审查）实测打脸原实现**：这里原来读
    ``getattr(st, "st_reparse_point", 0)``——而 Python 3.11 的
    ``os.stat_result`` **没有**这个属性（真名是 ``st_reparse_tag``），
    ``getattr`` 的默认值 0 让它**恒为假**，于是 junction 一个都没被跳过：
    注释写着"junction 跳过"，行为是"照走"。判据已统一到
    :func:`core.paths.is_reparse_point`（``st_reparse_tag`` +
    ``FILE_ATTRIBUTE_REPARSE_POINT``，实测 junction 命中）。
    """
    return is_reparse_point(p)


def _read_meta_csv(path: Path) -> dict:
    """读 utf-8-sig CSV 的表头+首行（缺文件/空文件一律返回空 dict）。"""
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            rd = csv.DictReader(f)
            for row in rd:
                return {str(k).strip(): (v or "").strip()
                        for k, v in row.items() if k is not None}
    except (OSError, UnicodeDecodeError, csv.Error) as err:
        log.debug("读 %s 失败: %s", path, err)
    return {}


def _dye_from_run_params(run_dir: Path) -> str:
    """从 ``run_params.yaml`` 里取染料名（缺失/读坏返回空串）。"""
    p = run_dir / "run_params.yaml"
    if not p.is_file():
        return ""
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:                                   # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    # 兼容几种可能的位置：顶层 dye / dyes[0] / results[0].dye
    v = data.get("dye") or data.get("dye_name")
    if isinstance(v, str) and v.strip():
        return v.strip()
    dyes = data.get("dyes")
    if isinstance(dyes, list) and dyes and isinstance(dyes[0], str):
        return dyes[0].strip()
    res = data.get("results")
    if isinstance(res, list) and res and isinstance(res[0], dict):
        v = res[0].get("dye") or res[0].get("dye_name")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _dye_from_results_csv(run_dir: Path, cfg: Optional[dict] = None) -> str:
    """``results.csv`` 首行的"组别"列 → 染料名。

    ``results.csv`` 的列名是中文（见 ``core/results.py``），这里按"组别"取，
    再走 :func:`core.dyes.resolve_dye_name` 的同一套启发式，保证与 GUI 里看到
    的染料名一致（**不另立一套命名规则**）。

    M2（评审记录）：这里原来**硬编码** ``{"enabled": True,
    "auto_from": "group_first_field"}``，把用户真实的 ``cfg["dye"]`` 丢在一边。
    后果是"用户改过 ``auto_from`` 或关掉染料命名"时，P9 归档出的目录名与 GUI
    显示的名字**分歧**——而本函数的 docstring 恰恰承诺"与 GUI 一致"。
    默认配置下两者恰好相同，所以测试全绿；属"自定义配置即错位"类缺陷。
    现在把真实 cfg 透传下去（缺键时 ``resolve_dye_name`` 自己有默认值）。
    """
    row = _read_meta_csv(run_dir / "results.csv")
    group = row.get("组别") or row.get("group") or ""
    if not group:
        return ""
    return str(resolve_dye_name(group, (cfg or {}).get("dye") or {}) or "")


def _guess_dye(run_dir: Path, cfg: Optional[dict]) -> tuple[str, str]:
    """猜一个 run 目录属于哪个染料。

    Returns:
        ``(染料名, 依据)``——依据是中文人话，写进报告供用户核对。
        全部猜不出时返回 ``(MULTI_DIR, "无法判定染料 → 归入 _multi")``。
    """
    name = _dye_from_run_params(run_dir)
    if name:
        return name, "run_params.yaml"
    name = _dye_from_results_csv(run_dir, cfg)
    if name:
        return name, "results.csv 组别列"
    # 兜底：目录名本身像不像染料名（旧的 stack/<染料>/ 已给出答案，此处不适用）
    return MULTI_DIR, "无法判定染料 → 归入 _multi"

# ═══════════════════════════ 计划 ═══════════════════════════
def plan_reorg(out_root: Path | str, cfg: Optional[dict] = None) -> dict:
    """**只读**扫描 ``out_root``，产出"哪些目录要搬到哪"的计划。

    不创建、不移动、不删除任何东西——可以随时安全调用（GUI 里点"预览"）。

    Returns:
        ``{"root", "moves": [...], "skips": [...], "conflicts": [...],
        "issues": [...]}``。``moves`` 每项：
        ``{src, dst, dye, reason, kind, conflict: bool}``。
    """
    root = Path(out_root)
    moves: list[dict] = []
    skips: list[dict] = []
    issues: list[str] = []
    taken: set[str] = set()          # 计划内已被占用的目标（M6）

    def _add(src: Path, dst: Path, dye: str, reason: str, kind: str) -> None:
        """登记一条移动。

        M6（评审记录）：**计划期就要检出"计划内部撞名"**。
        原先只看 ``dst.exists()``（磁盘现状）：当 ``stack/DyeA/<stamp>`` 与
        ``compare/DyeA/<stamp>`` 归入同一染料同一时间戳时，计划期两次判断**都是
        False**、两条都记 ``conflict=False``；到了 apply 阶段第二条才被静默
        ``_unique`` 成 ``…_1`` ——**执行了计划里根本没有的目标**，而报告仍写
        "撞名：否"。数据不丢，但"先看计划再执行"这个承诺被打折。
        现在把"计划已占用的目标"也纳入判断。
        """
        key = str(dst).lower()
        conflict = dst.exists() or key in taken
        if conflict:
            dst = _unique(dst)                          # 只算名字，不创建
            # `_unique` 只看磁盘，可能仍落在计划内已占用的名字上 → 继续往后找
            while str(dst).lower() in taken:
                dst = dst.with_name(f"{dst.name}_1")
        taken.add(str(dst).lower())
        moves.append({"src": src, "dst": dst, "dye": dye,
                      "reason": reason, "kind": kind, "conflict": conflict})

    if not root.is_dir():
        return {"root": root, "moves": [], "skips": [],
                "conflicts": [], "issues": [f"目录不存在：{root}"]}

    # ── ① V1.0 扁平 run 目录：runs/run_<时间戳>/ → runs/<染料>/<时间戳>/ ──
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        m = _LEGACY_STAMP_RE.match(sub.name)
        if not m:
            continue
        dye, why = _guess_dye(sub, cfg)
        dye = sanitize_dye_name(dye) or MULTI_DIR
        _add(sub, root / dye / m.group(1), dye,
             f"{why}；V1.0 扁平 run 目录", "legacy_flat")

    # ── ② 功能子目录：runs/<功能>/<染料>/<run_id>/ → runs/<染料>/<run_id>/ ──
    for func in FUNC_SUBDIRS:
        fdir = root / func
        if not fdir.is_dir():
            continue
        for dye_sub in sorted(fdir.iterdir()):
            if not dye_sub.is_dir() or _is_link(dye_sub):     # L11：junction 跳过
                continue
            if dye_sub.name == MULTI_DIR:
                # spectra/_multi/<时间戳> → runs/_multi/<时间戳>（只差一层）
                for rid in sorted(dye_sub.iterdir()):
                    if not rid.is_dir() or not _is_run_id(rid.name):
                        continue
                    _add(rid, root / MULTI_DIR / rid.name, MULTI_DIR,
                         f"{func}/_multi 提升到 runs/_multi", "func_multi")
                continue
            dye = dye_sub.name
            for rid in sorted(dye_sub.iterdir()):
                if not rid.is_dir():
                    continue
                if not _is_run_id(rid.name):
                    # L5（评审记录）：原来这个分支**不检查 run_id**，于是
                    # `runs/<功能>/<染料>/` 下的**任意**子目录（用户手工放的笔记目录）
                    # 也会被搬走并改名 `<name>_<func>`。只移动不删除、且报告可查，
                    # 所以只算低危；但"不是 run 目录"的东西不该由整理工具来搬。
                    skips.append({
                        "path": rid,
                        "reason": f"{func}/<染料>/ 下的「{rid.name}」不是 run 目录"
                                  f"（名字不像 <时间戳>），已跳过；"
                                  f"若确实要归档请手工移动或改名后重跑"})
                    continue
                _add(rid, root / dye / rid.name, dye,
                     f"{func}/<染料> 提升到 runs/<染料>", f"func_{func}")
    # ── ③ 已经是新形态的 ──
    for sub in sorted(root.iterdir()):
        if not sub.is_dir() or sub.name.startswith("run_"):
            continue
        if sub.name in FUNC_SUBDIRS or sub.name.startswith("_"):
            continue
        if _is_link(sub):                                   # L11
            continue
        for rid in sorted(sub.iterdir()):
            if rid.is_dir() and _is_run_id(rid.name):
                skips.append({"path": rid, "reason": "已是 runs/<染料>/<run_id> 形态"})

    # ── ④ 旧空壳目录（移完就空了）──
    shells = [root / f for f in FUNC_SUBDIRS if (root / f).is_dir()]
    conflicts = [m for m in moves if m["conflict"]]
    if conflicts:
        issues.append(f"{len(conflicts)} 个目标目录已被占用（磁盘已有或计划内重复），"
                      f"将自动加 _1/_2 后缀（**不覆盖**）")
    if not moves:
        issues.append("没有找到需要整理的旧目录（可能已经整理过）")
    log.info("P9 计划：%d 项待移动、%d 项跳过、%d 处冲突",
             len(moves), len(skips), len(conflicts))
    return {"root": root, "moves": moves, "skips": skips,
            "conflicts": conflicts, "issues": issues, "shells": shells}


# ═══════════════════════════ 执行 ═══════════════════════════
def _move_dir(src: Path, dst: Path) -> str:
    """移动目录，返回错误串（成功为空串）。

    P5（评审记录）：原先这里在 ``shutil.move`` 失败后自己写了一套
    ``copytree`` + ``rmtree`` 的跨盘回退。**那是多余的**——``shutil.move``
    内部已经有跨盘回退（先 copy 再删源）；而它真正失败时（复制半途出错）
    ``dst`` 往往已经存在或残缺，我们那层 ``copytree(dirs_exist_ok=False)``
    必然再失败一次。删掉自定义回退，让 ``move`` 的异常直接透传成中文原因。

    L6（评审记录）：若 ``move`` 在复制半途失败，``dst`` 会留下**部分复制的残骸**，
    下次 apply 时 ``_unique`` 会再改名重拷、残骸累积 → 这里清掉它。
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        return f"无法创建目标父目录：{err}"
    if dst.exists():
        return f"目标已存在，拒绝覆盖：{dst}"
    try:
        shutil.move(str(src), str(dst))
        return ""
    except (OSError, shutil.Error) as err:
        # L6：`shutil.move` 在复制半途失败时会留下**部分复制的 dst 残骸**，
        # 下次 apply 时 `_unique` 会再改名重拷、残骸累积。只在源**仍然在**
        # （即确实没搬成）时清掉那个半截目标。
        if src.exists() and dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        return f"移动失败：{err}（若为跨盘/占用，请检查目标磁盘空间与杀软占用后重试）"


def apply_reorg(plan: dict, cfg: Optional[dict] = None,
                force: bool = False) -> dict:
    """按 :func:`plan_reorg` 的结果真正动盘。

    ``cfg['dye']['reorg']`` 相关键：

    * ``dry_run``（默认 **True**）——**真正生效的安全闸**（M7）。为 True 且未传
      ``force=True`` 时本函数**不动任何东西**，只返回 ``{dry_run: True, ...}``。
      原先这个键有定义、有 docstring 承诺，却**没有任何消费者**：用户设
      ``dry_run: true`` 后加 ``--apply`` 照样动盘且无任何提示——而 docstring
      还把安全性归功于它。已接线。
    * ``keep_legacy``（默认 True）——移完后的空壳旧目录移进 ``_legacy/``；
    * ``write_report``（默认 True）——写 ``runs/_reorg_report.md``。

    :param force: 显式覆盖 ``dry_run``（CLI 的 ``--force`` 用它）。

    Returns:
        ``{"done", "failed", "moved_n", "report", "dry_run"}``；
        被 ``dry_run`` 拦下时 ``moved_n=0`` 且带中文 ``message``。
    """
    rcfg = dict(((cfg or {}).get("dye") or {}).get("reorg") or {})
    dry = bool(rcfg.get("dry_run", True)) and not force
    keep_legacy = bool(rcfg.get("keep_legacy", True))
    write_rep = bool(rcfg.get("write_report", True))
    root = Path(plan.get("root") or "runs")
    done: list[dict] = []
    failed: list[dict] = []
    if dry:
        msg = ("配置 dye.reorg.dry_run = true → **只出计划、未动盘**。"
               "确认计划无误后：命令行加 `--apply --force`，"
               "或把 config.yaml 里 dye.reorg.dry_run 设为 false。")
        log.info("P9: %s", msg)
        return {"done": [], "failed": [], "moved_n": 0, "shells": [],
                "report": None, "dry_run": True, "message": msg}

    for mv in plan.get("moves", []):
        src, dst = Path(mv["src"]), Path(mv["dst"])
        if not src.is_dir():
            failed.append({**mv, "error": "源目录已不存在（可能被别的进程移动过）"})
            continue
        # 撞名二次确认（计划生成到执行之间可能有变化）
        if dst.exists():
            dst = _unique(dst)
        err = _move_dir(src, dst)
        if err:
            failed.append({**mv, "dst": dst, "error": err})
            log.warning("P9 移动失败 %s → %s: %s", src, dst, err)
            continue
        done.append({**mv, "dst": dst})
        log.info("P9 已移动 %s → %s", src, dst)

    # 空壳旧目录：移进 _legacy/ 保留（不删）
    shells_moved: list[dict] = []
    if keep_legacy:
        for shell in plan.get("shells", []):
            shell = Path(shell)
            if not shell.is_dir():
                continue
            try:
                remaining = [p for p in shell.rglob("*") if p.is_file()]
            except OSError:
                continue
            if remaining:
                continue                                # 还有东西就不动它
            dst = _unique(root / "_legacy" / shell.name)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(shell), str(dst))
                shells_moved.append({"src": shell, "dst": dst})
            except (OSError, shutil.Error) as err:
                log.debug("空壳目录 %s 未移动: %s", shell, err)

    report = None
    if write_rep:
        try:
            report = write_reorg_report(plan, done, failed, shells_moved, root)
        except OSError as err:
            log.warning("P9 报告写出失败: %s", err)

    log.info("P9 完成：成功 %d、失败 %d、空壳归档 %d",
             len(done), len(failed), len(shells_moved))
    return {"done": done, "failed": failed, "moved_n": len(done),
            "shells": shells_moved, "report": report, "dry_run": False}


# ═══════════════════════════ 报告 ═══════════════════════════
def write_reorg_report(plan: dict, done: list, failed: list,
                       shells: list, root: Path) -> Path:
    """写 ``runs/_reorg_report.md``（中文、可核对、失败项醒目）。"""
    import time
    root = Path(root)
    p = root / "_reorg_report.md"
    rel = lambda x: str(x).replace(str(root) + "\\", "").replace(str(root) + "/", "")
    lines = [
        "# 旧 runs/ 目录整理报告（P9）",
        "",
        f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 扫描根：`{root}`",
        f"- 成功移动：**{len(done)}** 项｜失败：**{len(failed)}** 项｜"
        f"空壳归档：{len(shells)} 项",
        "",
        "> 整理过程**只移动、不删除**数据文件；目标目录已存在时自动加 `_1/_2` 后缀，"
        "**从不覆盖**。原始数据一律保留。",
        "",
    ]
    if plan.get("issues"):
        lines += ["## 提示", ""]
        lines += [f"- {s}" for s in plan["issues"]]
        lines += [""]
    if done:
        lines += ["## 已移动", "",
                  "| 原路径 | 新路径 | 染料 | 判定依据 | 撞名 |",
                  "|---|---|---|---|---|"]
        for d in done:
            lines.append(f"| `{rel(d['src'])}` | `{rel(d['dst'])}` | {d['dye']} | "
                         f"{d['reason']} | {'是' if d.get('conflict') else '否'} |")
        lines.append("")
    if failed:
        lines += ["## ⚠ 失败（需要人工处理）", "",
                  "| 原路径 | 目标 | 原因 |", "|---|---|---|"]
        for d in failed:
            lines.append(f"| `{rel(d['src'])}` | `{rel(d.get('dst', ''))}` | "
                         f"{d.get('error', '')} |")
        lines.append("")
    if shells:
        lines += ["## 空壳旧目录（已移入 _legacy/ 保留）", ""]
        lines += [f"- `{rel(s['src'])}` → `{rel(s['dst'])}`" for s in shells]
        lines.append("")
    if plan.get("skips"):
        lines += ["## 无需整理", ""]
        lines += [f"- `{rel(s['path'])}`：{s['reason']}" for s in plan["skips"][:40]]
        if len(plan["skips"]) > 40:
            lines.append(f"- …另有 {len(plan['skips']) - 40} 项")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    log.info("P9 报告已写出：%s", p)
    return p


def describe_plan(plan: dict) -> str:
    """把计划压成一段中文摘要（CLI/GUI 单行展示用）。"""
    n = len(plan.get("moves", []))
    if not n:
        return "无需整理：没有发现旧扁平目录"
    dyes = sorted({m["dye"] for m in plan["moves"]})
    return (f"将整理 {n} 个旧 run 目录，归入 {len(dyes)} 个染料："
            f"{'、'.join(dyes[:6])}{'…' if len(dyes) > 6 else ''}"
            f"（冲突 {len(plan.get('conflicts', []))} 处，不会覆盖）")
