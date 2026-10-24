# -*- coding: utf-8 -*-
"""染料身份层：注册表、目录归档、跨 run 历史档案（设计文档 V1.1 FR-11）。

设计三原则（对应设计文档 §1.3-10 / §1.3-11）：

1. **唯一命名源**：``会话覆盖 > dyes.yaml 注册表 > auto 启发式``，三条路径都过
   同一个 :func:`sanitize_dye_name`；同一名字驱动输出目录、历史档案、图内标注。
2. **整定是会话层**：手工命名只写 ``dyes.yaml``（勾"记住"时）或留在内存会话里，
   **绝不写回用户原始数据文件**。
3. **绝不阻塞**：``dyes.yaml`` 缺失/损坏 → 全部走 auto 并记日志，分析照跑。

目录结构（``dye.dir_scheme: "dye"``，V1.1 新默认）::

    runs/
    ├── Resorufin/                 ← 染料名（消毒后）
    │   ├── history.csv            ← 跨 run 累积档案
    │   ├── 20261014-222104/       ← 一次 run（内部结构与 V1.0 完全一致）
    │   └── …
    ├── _multi/<时间戳>/           ← 一次 run 含 >1 种染料
    └── run_2026…/                 ← V1.0 旧目录：只读识别，绝不迁移
"""
from __future__ import annotations

import csv
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from core.paths import is_reparse_point

log = logging.getLogger("dye_tool.dyes")

__all__ = [
    "DEFAULT_DYE", "MULTI_DIR", "HISTORY_NAME", "HISTORY_COLUMNS",
    "auto_dye_name", "sanitize_dye_name", "resolve_dye_name",
    "dye_for_group", "fluorophore_label_for",
    "dye_dir_for", "make_run_dir", "registry_path", "load_registry",
    "save_registry", "remember_mapping", "forget_mapping",
    "append_history", "read_history", "list_dyes", "scan_runs",
    "iter_run_dirs",
    "write_run_params", "read_run_params", "write_time_overrides",
    "read_time_overrides", "is_legacy_run_dir",
]

#: 无法解析出染料名时的兜底目录名
DEFAULT_DYE = "unnamed-dye"
#: 一次 run 含多种染料时的容器目录
MULTI_DIR = "_multi"
#: 跨 run 档案文件名
HISTORY_NAME = "history.csv"
#: 档案列（追加制；同 run_id 覆盖去重）
HISTORY_COLUMNS = ("run_id", "dir", "dyes", "groups", "n_files", "n_used",
                   "k", "k_se", "R2", "n", "A0", "Phi", "Phi_se", "status",
                   "notes", "time_edited")

#: Windows 非法字符 + 控制字符
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
#: Windows 保留设备名
_RESERVED = {p.upper() for p in (
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)])}


# ═══════════════════════════ 染料名解析 ═══════════════════════════
def auto_dye_name(group: str, auto_from: str = "group_first_field") -> str:
    """由组名推导染料名（与 R1 的 ``fluorophore_label:"auto"`` **同一函数**）。

    规则：取组名首个 ``-`` 之前的字段——
    ``Resorufin-2I-pbs-TEST`` → ``Resorufin``；无 ``-`` 的 ``Cy5`` → ``Cy5``。
    """
    g = str(group or "").strip()
    if not g:
        return DEFAULT_DYE
    if auto_from == "group_first_field":
        head = g.split("-", 1)[0].strip()
        return head or g
    return g


def sanitize_dye_name(raw: Any, existing: Optional[Iterable[str]] = None,
                      max_len: int = 60) -> str:
    """把任意字符串变成**合法且不冲突**的目录名（A13② 正负例见 docstring 末）。

    * ``<>:"/\\|?*`` 与控制字符 → ``-``；
    * 去掉首尾空格/句点；连续 ``-`` 折叠；
    * 空或全非法 → ``unnamed-dye``；
    * Windows 保留名（CON/PRN/AUX/NUL/COM1-9/LPT1-9）加前缀 ``_``；
    * 超长按 ``max_len`` 截断；
    * 与 ``existing`` 大小写不敏感冲突时追加 ``~2``/``~3``…

    示例::

        sanitize_dye_name('Cy5:PBS')        -> 'Cy5-PBS'
        sanitize_dye_name('***')            -> 'unnamed-dye'
        sanitize_dye_name('CON')            -> '_CON'
        sanitize_dye_name('Cy5', {'cy5'})   -> 'Cy5~2'
    """
    s = str(raw if raw is not None else "")
    s = _BAD_CHARS.sub("-", s)
    s = s.strip().strip(". ")
    s = re.sub(r"-{2,}", "-", s).strip("- ")
    if not s:
        s = DEFAULT_DYE
    if s.upper() in _RESERVED:
        s = "_" + s
    if max_len and len(s) > int(max_len):
        s = s[:int(max_len)].rstrip("- .")
        if not s:
            s = DEFAULT_DYE
    used = {str(x).lower() for x in (existing or ())}
    if s.lower() not in used:
        return s
    i = 2
    while f"{s}~{i}".lower() in used:
        i += 1
    return f"{s}~{i}"


def resolve_dye_name(group: str, dye_cfg: Optional[dict] = None,
                     registry: Optional[dict] = None,
                     session: Optional[dict] = None) -> str:
    """按 ``会话 > 注册表 > auto`` 解析染料名（FR-11.1）。

    Args:
        group: 组名（已去掉时间后缀）。
        dye_cfg: ``cfg["dye"]`` 节（取 ``auto_from`` / ``name_max_len``）。
        registry: :func:`load_registry` 的结果。
        session: 本次会话的 UI 覆盖 ``{组名: 染料名}``。

    Returns:
        消毒后的染料名（永不返回空串）。
    """
    dcfg = dye_cfg or {}
    max_len = int(dcfg.get("name_max_len", 60) or 60)
    auto_from = str(dcfg.get("auto_from", "group_first_field"))
    src = None
    if session and group in session and str(session[group]).strip():
        src = session[group]
    elif registry:
        ent = registry.get(group)
        if isinstance(ent, dict):
            src = ent.get("dye")
        elif isinstance(ent, str):
            src = ent
    if src is None:
        # 自动启发式；名称被截断但仍有意义
        src = auto_dye_name(group, auto_from)
    # D2（评审记录）：这里原有一个 `override = "auto"` 变量与 `if override: return name`
    # 分支，而 fallthrough 也是 `return name` —— 两支同值，纯死代码。已删。
    return sanitize_dye_name(src, max_len=max_len)


def dye_for_group(cfg: Optional[dict], group: str,
                  registry: Optional[dict] = None,
                  session: Optional[dict] = None) -> str:
    """便捷入口：从 ``cfg`` 里取 ``dye`` 节与**会话覆盖**解析组对应的染料名。

    会话覆盖默认放在 ``cfg["_dye_session"]``（私有键，不写进任何 yaml）——GUI 的
    预分析命名面板改的就是它；未改的组落回注册表 / auto。

    ``session`` 显式给出时优先于 cfg 里的那份。这是给**多进程渲染**（P2）用的：
    子进程要按同一份会话命名标注，但**不该靠改 cfg** 来传递——每个渲染单元共用
    同一份 cfg 对象，就地改会互相干扰。
    """
    dcfg = (cfg or {}).get("dye") or {}
    if session is None:
        session = (cfg or {}).get("_dye_session") or {}
    if registry is None:
        registry = load_registry()
    return resolve_dye_name(group, dcfg, registry, session)


def fluorophore_label_for(cfg: Optional[dict], group: str,
                          registry: Optional[dict] = None,
                          session: Optional[dict] = None) -> str:
    """解析图内染料标注文本（§9 R1）：``auto`` → 染料名（三处同源）。

    显式字符串原样返回；**空串表示只画参考线不写字**。
    """
    raw = (((cfg or {}).get("plot") or {}).get("jacs") or {}).get(
        "fluorophore_label", "auto")
    if isinstance(raw, str) and raw.strip().lower() == "auto":
        return dye_for_group(cfg, group, registry, session)
    return "" if raw is None else str(raw)


# ═══════════════════════════ 注册表（dyes.yaml） ═══════════════════════════
def registry_path() -> Path:
    """注册表位置：exe 同级 / 项目根（与 config.yaml、presets.yaml 三文件三职）。"""
    from ..paths import app_root
    return Path(app_root()) / "dyes.yaml"


#: 注册表缓存（O3）：``路径 → (mtime_ns, size, 内容)``。一次出图会按组反复问
#: "这组叫什么名字"（30 组要问 100+ 次），每次都读盘纯属浪费；按 mtime+size
#: 失效，所以"勾了记住"改完文件立刻生效（save_registry 也会主动清缓存）。
_REGISTRY_CACHE: dict = {}


def load_registry(path: Optional[Path | str] = None) -> dict:
    """读 ``dyes.yaml``；缺失或损坏返回空 dict 并记日志（**永不阻塞分析**）。

    O3：结果按 ``(mtime_ns, size)`` 缓存——文件没变就不重复读盘；文件被外部
    编辑器改动（mtime 变）则自动重读，不会拿到过期映射。
    """
    p = Path(path) if path is not None else registry_path()
    try:
        st = p.stat()
        ck = (str(p), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        _REGISTRY_CACHE.pop(str(p), None)
        return {}
    hit = _REGISTRY_CACHE.get(str(p))
    if hit is not None and hit[0] == ck:
        return hit[1]
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as err:                           # noqa: BLE001
        log.warning("染料注册表读取失败 %s: %s（本次全部走自动命名）", p, err)
        return {}
    if not isinstance(data, dict):
        log.warning("染料注册表顶层不是映射，忽略: %s", p)
        _REGISTRY_CACHE[str(p)] = (ck, {})
        return {}
    dyes = data.get("dyes")
    out = dyes if isinstance(dyes, dict) else {}
    _REGISTRY_CACHE[str(p)] = (ck, out)
    return out


def clear_registry_cache() -> None:
    """清空注册表缓存（测试与"保存后立刻生效"用）。"""
    _REGISTRY_CACHE.clear()


def save_registry(registry: dict, path: Optional[Path | str] = None) -> Path:
    """写出注册表（UI 勾"记住"时调用）。"""
    import yaml
    p = Path(path) if path is not None else registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# dye_tool 染料注册表：组名 → 染料名（可手工编辑、可随组共享）\n"
        "# 由 GUI「染料命名」面板写入；删除某项即回到自动命名\n"
        + yaml.safe_dump({"dyes": registry}, allow_unicode=True,
                         sort_keys=True, default_flow_style=False),
        encoding="utf-8")
    _REGISTRY_CACHE.pop(str(p), None)      # O3：写盘即失效，下次读到的就是新内容
    log.info("已保存染料注册表: %s（%d 项）", p, len(registry))
    return p


def remember_mapping(group: str, dye: str,
                     path: Optional[Path | str] = None) -> dict:
    """记住一条「组名 → 染料名」映射并落盘，返回更新后的注册表。"""
    reg = load_registry(path)
    reg[str(group)] = {"dye": str(dye),
                       "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    save_registry(reg, path)
    return reg


def forget_mapping(group: str, path: Optional[Path | str] = None) -> dict:
    """删除一条映射（回到自动命名），返回更新后的注册表。"""
    reg = load_registry(path)
    reg.pop(str(group), None)
    save_registry(reg, path)
    return reg


# ═══════════════════════════ 目录归档 ═══════════════════════════
def is_legacy_run_dir(d: Path) -> bool:
    """是否是 V1.0 的 ``run_<时间戳>`` 旧目录（只读识别，不迁移）。"""
    return d.is_dir() and d.name.startswith("run_") and \
        not (d / HISTORY_NAME).exists()


#: run 目录名的形态：``<时间戳>[_<序号>]``（``make_run_dir`` 冲突时加 ``_2``、``_3``…）
_RUN_ID_RE = re.compile(r"^(\d{8}-\d{6})(?:_(\d+))?$")


def _run_sort_key(name: str) -> tuple:
    """run 目录名 / run_id 的**可比较**排序键（O12）。

    直接比字符串会让 ``...-000000_10`` 排在 ``...-000000_9`` **前面**（"1" < "9"），
    于是同一秒内冲突到两位数之后"最近 run"就指错了。这里把结尾序号拆成整数一起比：
    ``(时间戳, 序号)``，无序号按 0 算；不符合该形态的退化为按原字符串比。
    """
    raw = str(name)
    if raw.startswith("run_"):
        raw = raw[4:]
    m = _RUN_ID_RE.match(raw)
    if not m:
        return (str(name), 0)
    return (m.group(1), int(m.group(2) or 0))


def dye_dir_for(dye: str, run_id: str, scheme: str = "dye",
                out_root: Optional[Path | str] = None) -> Path:
    """给定染料名与 run 时间戳 → 目标 run 目录（**不创建**）。

    * ``scheme="dye"``  → ``<out_root>/<染料>/<run_id>``
    * ``scheme="flat"`` → ``<out_root>/run_<run_id>``（V1.0 行为）
    * 染料名为 ``_multi`` / 空 → ``<out_root>/_multi/<run_id>``
    """
    root = Path(out_root) if out_root is not None else Path("runs")
    name = str(dye or "").strip() or MULTI_DIR
    if scheme == "flat":
        return root / f"run_{run_id}"
    if name == MULTI_DIR:
        return root / MULTI_DIR / run_id
    return root / name / run_id


def _unique(d: Path) -> Path:
    """目录已存在时加 ``_1``/``_2``… 后缀。"""
    if not d.exists():
        return d
    i = 1
    while True:
        cand = d.with_name(f"{d.name}_{i}")
        if not cand.exists():
            return cand
        i += 1


def make_run_dir(out_root: Path | str, dye: Optional[str] = None,
                 scheme: str = "dye", run_id: Optional[str] = None,
                 exist_ok: bool = False) -> Path:
    """创建 run 目录并返回（FR-11.3 归档结构）。

    ``dye=None`` 且 ``scheme="dye"`` → 走 ``_multi`` 容器；``scheme="flat"``
    时完全复现 V1.0 的 ``run_<时间戳>`` 命名（外部脚本兼容出口）。
    """
    rid = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    d = dye_dir_for(dye or MULTI_DIR, rid, scheme, root)
    if exist_ok and d.exists():
        return d
    d = _unique(d)
    d.mkdir(parents=True)
    return d


# ═══════════════════════════ 跨 run 历史档案 ═══════════════════════════
def append_history(dye_dir: Path | str, row: dict) -> Optional[Path]:
    """把一行结果追加到 ``<染料目录>/history.csv``（同 run_id 覆盖去重）。

    ⚠ DS3′ 触发器（设计 §1.3）：本表列自 V1.0 起从未演化，故**不加版本标记行**；将来
    列改名/删除/新增时，必须同一批次完成：版本标记行 + :func:`read_history` 对无标记
    文件的容错分支 + 旧格式回归夹具（旧 exe 的 DictReader 会把注释行当表头 → 全表错位）。

    Returns:
        档案文件路径；写入失败返回 None（记警告，不影响分析）。
    """
    d = Path(dye_dir)
    p = d / HISTORY_NAME
    rid = str(row.get("run_id", ""))
    if not rid:
        log.warning("history 行缺少 run_id，跳过登记")
        return None
    rows: list[dict] = []
    header = list(HISTORY_COLUMNS)
    if p.exists():
        broken: Optional[str] = None
        try:
            with p.open("r", encoding="utf-8-sig", newline="") as fh:
                rd = csv.DictReader(fh)
                raw = list(rd.fieldnames or [])
                if not raw:
                    broken = "文件是空的（无表头）"
                elif "run_id" not in raw:
                    # 表头不像我们的档案（多半被别的程序覆盖或编码坏了）。
                    # ⚠ 必须先判再补列：补列会把 run_id 加进去，之后再判就永远通过
                    # ——这个顺序错了曾让"背份并重建"形同虚设（Q21）。
                    broken = f"表头缺少 run_id 列（实际列：{raw[:4]}…）"
                else:
                    header = raw
                    for k in HISTORY_COLUMNS:
                        if k not in header:
                            header.append(k)
                    rows = [dict(r) for r in rd]
        except Exception as err:                       # noqa: BLE001
            broken = str(err)
        if broken:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bad = p.with_name(f"{p.stem}.bad.{stamp}.csv")
            try:
                p.replace(bad)
                log.warning("history.csv 不可用（已备份为 %s）：%s",
                            bad.name, broken)
            except OSError as err:
                # M10：备份失败时**绝不能再往下走**清空重建——那会把没能备份的旧
                # 档案覆盖成"只剩一行新表"（Q21 修复留下的缺口）。直接放弃登记，
                # 旧文件原样留在磁盘上，用户还有救回的机会。
                log.error("history.csv 不可用且**无法备份**（%s）→ 本次跳过档案登记，"
                          "原文件保持不动：%s", err, p)
                return None
            rows = []
            header = list(HISTORY_COLUMNS)
    rows = [r for r in rows if str(r.get("run_id", "")) != rid]
    clean = {k: ("" if row.get(k) is None else row.get(k))
             for k in header}
    rows.append(clean)
    try:
        d.mkdir(parents=True, exist_ok=True)
        # O5：原子写（临时文件 + os.replace）——history.csv 是"读-改-写"的累积
        # 档案，写一半被杀会把整个染料的历史毁掉。
        from .results import atomic_write
        with atomic_write(p, newline="", encoding="utf-8-sig") as fh:
            wr = csv.DictWriter(fh, fieldnames=header)
            wr.writeheader()
            for r in rows:
                wr.writerow({k: r.get(k, "") for k in header})
    except Exception as err:                           # noqa: BLE001
        log.warning("history.csv 写出失败 %s: %s", p, err)
        return None
    log.info("已登记染料档案: %s（%d 条）", p, len(rows))
    return p


def read_history(dye_dir: Path | str) -> list[dict]:
    """读某染料的档案（缺失/损坏返回空列表）。"""
    p = Path(dye_dir) / HISTORY_NAME
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(r) for r in csv.DictReader(fh)]
    except Exception as err:                           # noqa: BLE001
        log.warning("读取 history.csv 失败 %s: %s", p, err)
        return []


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f else None                     # 过滤 NaN
    except (TypeError, ValueError):
        return None


#: FR-2/3/4 的功能分层目录（染料层叠加在它们下面）
FUNC_SUBDIRS = ("stack", "compare", "spectra")


#: ``runs/`` 下**不是染料**的容器目录（内部库房/汇总目录）。
#: ``_legacy`` 是 P9 整理旧目录后存放空壳的地方（V1.3 新增）——它长得像个染料目录，
#: 但列进"染料库"里会让用户莫名其妙多出一个叫 `_legacy` 的"染料"，故排除。
NON_DYE_DIRS = (MULTI_DIR, "_legacy")


def _child_dirs(parent: Path) -> list[Path]:
    """parent 的一级子目录（跳过 junction/符号链接与不可读项；V25-O1）。

    runs/ 的发现逻辑（④档案与「统计报表」共用）不能跟随 reparse point：否则链接
    目标里的 run 会被重复发现，甚至扫描到 out_root 之外（G6 同一判据）。
    """
    out: list[Path] = []
    try:
        children = sorted(parent.iterdir())
    except OSError:
        return out
    for d in children:
        try:
            if d.is_dir() and not is_reparse_point(d):
                out.append(d)
        except OSError:
            continue
    return out


def _has_any_entry(d: Path) -> bool:
    """目录里是否有任何条目（T10：CLI --dry-run 留下的空 run_<ts>/ 不算 run）。"""
    try:
        return next(iter(d.iterdir()), None) is not None
    except OSError:
        return False


def _dye_entries(root: Path) -> list[tuple[str, Path]]:
    """``[(染料名, 染料目录)]``——同时看 ``runs/<染料>/`` 与 ``runs/<功能>/<染料>/``。"""
    out: list[tuple[str, Path]] = []
    for sub in _child_dirs(root):
        if sub.name in NON_DYE_DIRS or sub.name.startswith("run_"):
            continue
        if sub.name in FUNC_SUBDIRS:
            for d in _child_dirs(sub):
                if not d.name.startswith("run_"):
                    out.append((d.name, d))
            continue
        out.append((sub.name, sub))
    return out


def list_dyes(out_root: Path | str) -> list[dict]:
    """扫描 ``out_root``，汇总每个染料（含 ``_multi`` 内的登记与功能子目录）。

    Returns:
        每项 ``{dye, dir, runs, last_run, last_k, last_phi, has_history}``，
        按最近 run 倒序。旧 ``run_*`` 目录归入"未归档(V1.0)"分组（见
        :func:`scan_runs`）。
    """
    root = Path(out_root)
    out: list[dict] = []
    if not root.is_dir():
        return out
    merged: dict[str, dict] = {}
    for name, sub in _dye_entries(root):
        rows = read_history(sub)
        runs = [d for d in sub.iterdir()
                if d.is_dir() and (d / "results.csv").exists()
                or (d.is_dir() and (d / "figures").is_dir())]
        entry = merged.setdefault(name, {
            "dye": name, "dir": sub, "runs": 0, "last_run": "",
            "last_k": None, "last_phi": None,
            "has_history": (sub / HISTORY_NAME).exists(), "dirs": []})
        entry["dirs"].append(sub)
        entry["runs"] += len(runs)
        entry["has_history"] = entry["has_history"] or \
            (sub / HISTORY_NAME).exists()
        if rows:
            last = rows[-1]
            rid = str(last.get("run_id", ""))
            if _run_sort_key(rid) >= _run_sort_key(str(entry["last_run"])):
                entry["last_run"] = rid
                entry["last_k"] = _num(last.get("k"))
                entry["last_phi"] = _num(last.get("Phi"))
        elif runs:
            newest = max((d.name for d in runs), key=_run_sort_key,
                         default="")
            if _run_sort_key(newest) > _run_sort_key(str(entry["last_run"])):
                entry["last_run"] = newest
    out = list(merged.values())
    # _multi 内出现过的染料也聚合进来（读各行指针）
    multi = root / MULTI_DIR
    if multi.is_dir():
        seen: set[str] = set()
        for rd in sorted(multi.iterdir()):
            if not rd.is_dir():
                continue
            for r in read_history(rd):
                for name in str(r.get("dyes", "")).split("|"):
                    name = name.strip()
                    if name:
                        seen.add(name)
        for name in seen:
            if not any(e["dye"] == name for e in out):
                out.append({"dye": name, "dir": None, "runs": 0,
                            "last_run": "", "last_k": None, "last_phi": None,
                            "has_history": False, "multi": True, "dirs": []})
    out.sort(key=lambda e: (e["last_run"] or "", e["dye"]), reverse=True)
    return out


def scan_runs(out_root: Path | str) -> dict:
    """扫描结果根目录 → ``{"dyes": [...], "legacy": [Path, ...]}``（FR-11.6）。"""
    root = Path(out_root)
    legacy: list[Path] = []
    if root.is_dir():
        # ④档案的 legacy 列表保持 V1.x 行为（空目录也列出，供人工整理）；
        # T10 只改「统计/队列」的 run 判定（iter_run_dirs），不改这里。
        legacy = sorted((d for d in _child_dirs(root) if is_legacy_run_dir(d)),
                        key=lambda d: d.name, reverse=True)
    return {"dyes": list_dyes(root), "legacy": legacy}


def iter_run_dirs(out_root: Path | str, *,
                  dye: Optional[str] = None) -> list[tuple[str, Path]]:
    """列出 out_root 下所有 run 目录 → ``[(染料名, run_dir)]``（V2.5 FR-30.1）。

    与 :func:`list_dyes` / :func:`scan_runs` **同一套发现逻辑**（``_dye_entries`` +
    "子目录里有没有 results.csv"），供 ④档案与「统计报表」共用，避免两套发现口径
    漂移（审查 B3 同类风险）。

    * ``runs/<染料>/<时间戳>/`` 与 ``runs/<功能>/<染料>/<时间戳>/`` 都覆盖；
    * ``runs/_multi/<时间戳>/``（多染料 run）记染料名 ``_multi``；
    * ``runs/`` 根下的旧扁平 ``run_<时间戳>/``（:func:`is_legacy_run_dir`）记 "未归档"；
      空目录（CLI ``--dry-run`` 残留）跳过，不计入 run/异常（T10）；
    * run 判据 = 目录里有 results.csv **或** fit_summary.json（与 list_dyes 的
      runs 计数同口径）；**坏 run（缺 results.csv / 坏 JSON）也要被发现**，
      由统计报表列入"异常"（A30.3），不能静默消失；
    * **不跟随 junction/符号链接**（V25-O1，与 G6 同一判据）：链接目标里的 run
      不会被重复发现，也不会扫出 out_root 之外。
    """
    root = Path(out_root)
    out: list[tuple[str, Path]] = []
    if not root.is_dir():
        return out
    for name, sub in _dye_entries(root):
        for d in _child_dirs(sub):
            try:
                if (d / "results.csv").is_file() \
                        or (d / "fit_summary.json").is_file():
                    out.append((name, d))
            except OSError:
                continue
    for d in _child_dirs(root):
        # T10：CLI --dry-run 留下的空 run_<ts>/ 不是 run（否则统计会把它当坏 run）
        if is_legacy_run_dir(d) and _has_any_entry(d):
            out.append(("未归档", d))
    multi = root / MULTI_DIR
    if multi.is_dir():
        for d in _child_dirs(multi):
            try:
                if (d / "results.csv").is_file() \
                        or (d / "fit_summary.json").is_file():
                    out.append((MULTI_DIR, d))
            except OSError:
                continue
    out.sort(key=lambda x: (str(x[0]), str(x[1])))
    if dye:
        out = [(n, p) for n, p in out if n == dye]
    return out


# ═══════════════════════════ run 参数 / 时间覆盖快照 ═══════════════════════════
def write_run_params(run_dir: Path | str, cfg: dict,
                     extra: Optional[dict] = None) -> Optional[Path]:
    """把该次 run 的**全参数快照**写成 ``run_params.yaml``（复现入口）。"""
    import yaml
    from core import VERSION
    p = Path(run_dir) / "run_params.yaml"
    keys = ("wavelength", "baseline", "background", "a0", "kinetics",
            "quantum_yield", "qc", "controls", "plot", "autogroup", "spectra",
            "io", "filename", "dye")
    body = {k: cfg[k] for k in keys if k in cfg}
    body["_meta"] = {"written_by": VERSION,
                     "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        body["_run"] = extra
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# dye_tool run 参数快照（「载入 run 参数」直接读这里）\n"
                     + yaml.safe_dump(body, allow_unicode=True, sort_keys=False,
                                      default_flow_style=False),
                     encoding="utf-8")
        log.info("已写出 run 参数快照: %s", p)
        return p
    except Exception as err:                           # noqa: BLE001
        log.warning("run 参数快照写出失败: %s", err)
        return None


def read_run_params(run_dir: Path | str) -> dict:
    """读 ``run_params.yaml``（缺失/损坏返回空 dict）。"""
    p = Path(run_dir) / "run_params.yaml"
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            data.pop("_meta", None)
            data.pop("_run", None)
            return data
    except Exception as err:                           # noqa: BLE001
        log.warning("读取 run 参数快照失败 %s: %s", p, err)
    return {}


def write_time_overrides(run_dir: Path | str, overrides: dict) -> Optional[Path]:
    """把时间整定写成 ``time_overrides.yaml``（仅记录**被改过**的项）。"""
    import yaml
    if not overrides:
        return None
    p = Path(run_dir) / "time_overrides.yaml"
    body = {"time_overrides": {str(k): float(v) for k, v in overrides.items()},
            "note": "仅记录被手动修改的文件→时间(s)；未修改的按文件名解析"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# dye_tool 时间整定快照（FR-12；绝不写回原始数据文件）\n"
                     + yaml.safe_dump(body, allow_unicode=True, sort_keys=True,
                                      default_flow_style=False),
                     encoding="utf-8")
        log.info("已写出时间整定快照: %s（%d 项）", p, len(overrides))
        return p
    except Exception as err:                           # noqa: BLE001
        log.warning("时间整定快照写出失败: %s", err)
        return None


def read_time_overrides(run_dir: Path | str) -> dict:
    """读 ``time_overrides.yaml`` → ``{文件名: t}``（缺失/损坏返回空 dict）。"""
    p = Path(run_dir) / "time_overrides.yaml"
    if not p.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        got = data.get("time_overrides") if isinstance(data, dict) else None
        if isinstance(got, dict):
            out: dict[str, float] = {}
            for k, v in got.items():
                f = _num(v)
                if f is not None:
                    out[str(k)] = float(f)
            return out
    except Exception as err:                           # noqa: BLE001
        log.warning("读取时间整定快照失败 %s: %s", p, err)
    return {}


def history_row(run_id: str, run_dir: Path, dye: str,
                groups: Sequence[str], results: dict, phi: dict,
                n_files: int, n_used: int,
                time_edited: int = 0) -> dict:
    """由结果组装一行档案。

    多组时取**k 最大**的那一组代表该染料整体（``best_g``）——docstring 原来写
    "第一个可拟合组"，与实现不符（M9 一并订正）。
    """
    best_g, best_f = "", None
    for g, e in sorted((results or {}).items()):
        f = (e or {}).get("pooled")
        if f is not None and getattr(f, "status", "") != "FAILED" and \
                getattr(f, "k", None) is not None:
            if best_f is None or float(f.k) > float(best_f.k):
                best_g, best_f = g, f
    p = (phi or {}).get(best_g) or {}
    # M9：A(0) 在 FitResult 上**不存在**（它挂在 results[g]["A0_pooled"]），原来
    # `getattr(f, "a0", None)` 恒为 None → history.csv 的 A0 列永远是空的。
    a0 = None
    if best_g:
        a0 = ((results or {}).get(best_g) or {}).get("A0_pooled")
    notes = []
    if best_f is not None and getattr(best_f, "truncated", False):
        notes.append("自动截断")
    if time_edited:
        notes.append(f"手动调整 {time_edited} 个时间点")
    if len(groups) > 1:
        notes.append(f"{len(groups)} 组")
    return {
        "run_id": run_id,
        "dir": str(run_dir),
        "dyes": dye,
        "groups": "|".join(groups),
        "n_files": n_files,
        "n_used": n_used,
        "k": _fmt(best_f.k if best_f is not None else None),
        "k_se": _fmt(getattr(best_f, "k_err", None) if best_f else None),
        "R2": _fmt(getattr(best_f, "r2", None) if best_f else None),
        "n": (int(getattr(best_f, "n", 0)) if best_f else 0),
        "A0": _fmt(a0),
        "Phi": _fmt(p.get("phi")),
        "Phi_se": _fmt(p.get("phi_err")),
        "status": (getattr(best_f, "status", "") if best_f else "FAILED") or "",
        "notes": "; ".join(notes),
        "time_edited": int(time_edited),
    }


def _fmt(v: Any) -> str:
    """数值 → 稳定字符串（None → 空串）。"""
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:
        return ""
    return f"{f:.6g}"
