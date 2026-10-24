# -*- coding: utf-8 -*-
"""实验批次文件 ``dye_batch.yaml`` 的读写机制（V2.0 δ 批 / FR-14）。

一次实验 = 一个数据目录的文件集合。V1.x 里"角色判定 / 时间整定 / 染料命名"
只活在 GUI 会话里，同学拷走数据就拿不到；本模块把这三样**落成数据目录内的一个
新文件**（``<数据目录>/dye_batch.yaml``），随数据一起走。

六条机制约束（设计文档 FR-14，逐条对应实现）
------------------------------------------
1. **宽进**：未知键保留在内存、不告警不丢弃；**读入永不改写文件**
   （:func:`load_batch` 只读，不改一个字节）；
2. **严出**：只在显式调用 :func:`save_batch` 时落盘，且走 ``atomic_write``
   （与 ``core/results.py`` 同一机制：写临时文件 + 原子替换，断电不会留半个文件）；
3. **不参与 io_key**：角色/整定是**分析级**输入，解析缓存 ``_SCAN_CACHE`` 的语义
   一字不变（本模块完全不碰 io 缓存）；
4. **只读目录降级**：写不进去时返回 ``False`` + 中文原因，**不落 runs/**、
   **不抛异常**（调用方在状态栏提示，见 :func:`save_batch` 的返回值）；
5. **没有批次文件的目录行为与 V1.3 完全一致**：:func:`load_batch` 在文件不存在时
   返回一个空的 :class:`BatchFile`，所有取值都回落到"文件名解析值"；
6. **版本化用 DS1 同款模式**：``schema_version`` + 模块级 ``_MIGRATIONS``，
   迁移**只在内存做**（复用 :func:`core.config.migrate_schema` 的算法）。

与 ``config.yaml`` 的关系
-------------------------
``dye_batch.yaml`` 是**新文件类型**，与 DS1 的全树键校验（``KNOWN_PATHS`` /
``DEAD_KEYS``）**互不干涉**——设计文档明确禁止拿 config 的校验器去校批次文件
（会把批次里的每个键都报成"未知键"）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..config import ConfigIssue, migrate_schema
from ..report.results import atomic_write

log = logging.getLogger("dye_tool.batch")

#: 批次文件格式版本（与 ``config.SCHEMA_VERSION`` **各管各的**，但机制同款）
BATCH_SCHEMA_VERSION: int = 1

#: 批次文件名（放在数据目录内，随数据一起拷走）
BATCH_FILENAME = "dye_batch.yaml"

#: 角色词表沿用既有 ``ROLE_ZH`` 的键（``filename.roles`` 的**值**集合）
KNOWN_ROLES = ("sample", "background", "blank", "dark", "reference", "n2",
               "quencher")

#: ``t_src`` 的双轨取值（FR-12 沿用）：manual=手工整定 | worksheet=时间工作表
KNOWN_T_SRC = ("manual", "worksheet")

#: DS1 同款迁移表：``[(from_version, callable(user_dict) -> None)]``，**初始为空**
_MIGRATIONS: list[tuple[int, Any]] = []


# ═══════════════════════════ 数据结构 ═══════════════════════════
@dataclass
class BatchEntry:
    """批次里**一个文件**的整定（全部可选；缺省 = 用文件名解析值）。"""

    role: str = ""
    t_override: Optional[float] = None
    t_src: str = ""
    dye: str = ""
    #: V2.9 FR-60.2：重复号整定（文件名 ``_N`` 的 ``N``；空 = 用文件名解析值）。
    dup_index: str = ""
    #: 本次载入时读到的原始 dict（含未知键）——**原样保留**，保存时写回（宽进原则）
    raw: dict = field(default_factory=dict)

    #: 已知字段（写回时先全部从 raw 里摘掉，再按当前值重新加）。
    #: V2.3 修法（评审记录 F8）：原来直接 ``out = dict(self.raw)`` 再"非空才覆盖"，
    #: 于是**清空某个字段**（比如把 dye 置 "")保存时会被 raw 里的旧值**复活**。
    #: 现在先 pop 全部已知键，再按当前值写回——"清空"就是真的清空。
    _KNOWN = ("role", "t_override", "t_src", "dye", "dup_index")

    def as_dict(self) -> dict:
        """写回时的 dict：未知键原样保留，已知键以**当前值**为准（含清空）。"""
        out = dict(self.raw)
        for k in self._KNOWN:
            out.pop(k, None)                   # 先摘掉 raw 里的旧值（防复活）
        if self.role:
            out["role"] = self.role
        if self.t_override is not None:
            out["t_override"] = self.t_override
        if self.t_src:
            out["t_src"] = self.t_src
        if self.dye:
            out["dye"] = self.dye
        if self.dup_index:
            out["dup_index"] = self.dup_index
        return out


@dataclass
class BatchFile:
    """一个目录的批次状态（``files`` 的键 = **相对数据目录的正斜杠路径**）。"""

    schema_version: int = BATCH_SCHEMA_VERSION
    note: str = ""
    #: V2.4 FR-26.5：本批次的分析参数模板名（载入/运行时自动套用一次）。
    #: 属**已知键**（写侧显式落盘、读侧解析），不再是"未知键原样保留"的那一堆。
    analysis_preset: str = ""
    #: V2.9.1 FR-61/63：显式参比组与 Φ_ref（随批次文件记忆；缺省 = 自动/ config 初值）
    reference_group: str = ""
    phi_ref: Optional[float] = None
    files: dict[str, BatchEntry] = field(default_factory=dict)
    #: 顶层未知键（同样宽进保留）
    extra: dict = field(default_factory=dict)
    #: 批次文件实际路径（不存在时为 None）
    path: Optional[Path] = None

    # ── 取值（**全部带文件名解析值的回落**） ──
    def resolve(self, rel: str) -> str:
        """把``rel``解析成 ``files`` 里**实际存在的键**；没有则返回规范化后的原值。

        先精确匹配；未命中再做**大小写不敏感**回退（Windows 路径不区分大小写，
        手编 yaml 的大小写常与磁盘不一致——评审记录 F8）。多个候选取排序第一个
        （确定行为，不静默挑一个随机的）。
        """
        key = _norm_key(rel)
        if key in self.files:
            return key
        low = key.casefold()
        cands = sorted(k for k in self.files if k.casefold() == low)
        return cands[0] if cands else key

    def entry(self, rel: str) -> BatchEntry:
        """取某文件的整定（没有则返回空条目，绝不返回 None）。"""
        return self.files.get(self.resolve(rel)) or BatchEntry()

    def role_of(self, rel: str, default: str = "sample") -> str:
        got = self.entry(rel).role
        return got if got in KNOWN_ROLES else (default or "sample")

    def t_of(self, rel: str, default: Optional[float] = None) -> Optional[float]:
        got = self.entry(rel).t_override
        return default if got is None else float(got)

    def dye_of(self, rel: str, default: str = "") -> str:
        got = self.entry(rel).dye
        return got or default

    def dye_map(self, scans: Optional[Iterable] = None,
                data_dir: Optional[Path | str] = None) -> dict[str, str]:
        """``{组名: 染料名}``（供 ``run_phi_pipeline(group_dye=...)`` 用）。

        批次文件按**文件路径**记染料，而流水线按**组名**要染料，所以需要一次映射。
        给了 ``scans`` + ``data_dir`` 就用 ``Scan.group``（**权威**，来自
        ``io.parse_filename``）；没给则退回"文件名词干"这种近似（只够看，别拿它跑流水线
        ——`Dye-10` 与 `Dye-20` 同组但词干不同，近似会把它们拆开）。

        同一组内多个文件写了不同染料时取**排序第一个**并记 WARNING——不静默挑一个。
        """
        rel_to_group: dict[str, str] = {}
        if scans is not None:
            base = Path(data_dir).resolve() if data_dir else None
            for s in scans:
                try:
                    rel = (str(Path(s.path).resolve().relative_to(base))
                           if base else Path(s.path).name)
                except (ValueError, OSError):
                    rel = Path(s.path).name
                rel_to_group[_norm_key(rel)] = str(getattr(s, "group", "") or "")
        out: dict[str, str] = {}
        for rel in sorted(self.files):
            e = self.files[rel]
            if not e.dye:
                continue
            group = rel_to_group.get(rel) or _group_of_rel(rel)
            if group in out and out[group] != e.dye:
                log.warning("批次文件里同一组 %r 有多个染料名（%r / %r）→ 取前者",
                            group, out[group], e.dye)
                continue
            out.setdefault(group, e.dye)
        return out

    def set_entry(self, rel: str, **kw) -> BatchEntry:
        """写一条整定（就地更新，保留该文件的未知键）。"""
        key = self.resolve(rel)
        cur = self.files.get(key) or BatchEntry()
        for k, v in kw.items():
            if hasattr(cur, k):
                setattr(cur, k, v)
        self.files[key] = cur
        return cur

    def as_dict(self) -> dict:
        """写回时的完整 dict（未知键保留）。"""
        out = dict(self.extra)
        out["schema_version"] = int(self.schema_version)
        if self.note:
            out["note"] = self.note
        if self.analysis_preset:
            out["analysis_preset"] = self.analysis_preset
        if self.reference_group:
            out["reference_group"] = self.reference_group
        if self.phi_ref is not None:
            out["phi_ref"] = float(self.phi_ref)
        out["files"] = {k: self.files[k].as_dict() for k in sorted(self.files)}
        return out


def _norm_key(rel: str | Path) -> str:
    """统一成**正斜杠相对路径**（设计文档 L14 教训：先规范化再当字典键）。

    ⚠ 本函数**不改变大小写**：字典键保持用户/扫描给出的原样。Windows 路径本身
    不区分大小写，手编 yaml 写成 ``dye-10.txt`` 而磁盘是 ``Dye-10.txt`` 时按原样
    查不到——所以 :meth:`BatchFile.entry` 带一层**大小写不敏感回退查找**
    （评审记录 F8 的"至少归一化"选项；这里选**查找侧归一**而不是改存储，
    避免写回时改动用户文件里的键写法）。
    """
    s = str(rel).replace("\\", "/")
    # A.M3（V2.x 审查）：`lstrip("./")` 是**字符集剥离**，不是前缀剥离——
    #   `.hidden.txt` → `hidden.txt`（点开头文件丢点）
    #   `../x`       → `x`        （上级引用被压平）
    # 结果：`.hidden.txt` 与 `hidden.txt` 撞同一个键 → 批次整定**静默错位/覆盖**。
    # 只剥真正的 `./` 前缀（可多次，处理 ``././x``），其余原样保留。
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def _t_src_of(scan) -> float:
    """取"文件名解析出的原始时间"并**归一成 float**。

    ``Scan.t_src`` 是 ``Optional[float]``，老/手工构造的对象可能是 ``None``
    ——直接比较会 `TypeError`（实测踩过：``from_scans`` 在 t_src=None 时报
    "NoneType < float"），而"改了 t 才记进批次"这条语义**不能因此失效**。
    """
    for attr in ("t_src", "t"):
        v = getattr(scan, attr, None)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return float(getattr(scan, "t", 0.0) or 0.0)


def _group_of_rel(rel: str) -> str:
    """从相对路径猜组名（只用于 :meth:`BatchFile.dye_map` 的归并）。

    真正的组名由 ``io.parse_filename`` 决定；这里只需要"同一组的文件归到一起"，
    所以用文件名词干去扩展名即可。
    """
    return Path(rel).stem


def batch_path(data_dir: Path | str) -> Path:
    """数据目录里的批次文件路径。"""
    return Path(data_dir) / BATCH_FILENAME


# ═══════════════════════════ 读（宽进，永不改写文件） ═══════════════════════════
def empty_batch(data_dir: Optional[Path | str] = None) -> BatchFile:
    """空批次（没有批次文件的目录用它——行为与 V1.3 完全一致）。"""
    return BatchFile(path=(batch_path(data_dir) if data_dir else None))


def load_batch_path(path: Path | str,
                    issues: Optional[list] = None) -> BatchFile:
    """按**文件路径**读取批次（A.M5：CLI ``--batch other.yaml`` 必须读那个文件）。

    原实现只取 ``p.parent`` 再找默认文件名 ``dye_batch.yaml`` → 用户显式指定的
    文件名被**静默丢弃**，读到的可能是同目录另一份批次。
    """
    p = Path(path)
    return _load_batch_file(p, issues)


def load_batch(data_dir: Path | str, issues: Optional[list] = None) -> BatchFile:
    """读数据目录里的 ``dye_batch.yaml``；**不存在/读坏都返回空批次**。

    **只读**：本函数不写、不改、不创建任何文件（机制约束 1 + 6）。
    未知键保留在 :attr:`BatchFile.extra` / :attr:`BatchEntry.raw` 里。

    Args:
        data_dir: 数据目录。
        issues: 传了就在里面追加 :class:`core.config.ConfigIssue`（中文说明）。
    """
    return _load_batch_file(batch_path(data_dir), issues, data_dir=data_dir)


def _load_batch_file(p: Path, issues: Optional[list] = None,
                     data_dir: Optional[Path | str] = None) -> BatchFile:
    """``load_batch`` / ``load_batch_path`` 的共同实现（读单个文件）。"""
    out = empty_batch(data_dir if data_dir is not None
                      else (p.parent if p else None))
    if not p.is_file():
        return out
    try:
        import yaml
    except ImportError:                                 # pragma: no cover
        _issue(issues, "WARNING", BATCH_FILENAME, "未安装 pyyaml，已忽略批次文件",
               "pip install pyyaml")
        return out
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as err:                            # noqa: BLE001 —— 读入永不抛
        log.warning("批次文件读取失败 %s: %s（按「无批次文件」处理）", p, err)
        _issue(issues, "ERROR", BATCH_FILENAME,
               f"批次文件语法错误，本次已忽略：{err}",
               f"修好 {BATCH_FILENAME} 或删掉它（不影响原始数据）")
        return out
    if not isinstance(raw, dict):
        _issue(issues, "ERROR", BATCH_FILENAME,
               "批次文件顶层不是键值对结构，本次已忽略", "应写成 `files:` 下的键值对")
        return out

    tmp: list[ConfigIssue] = []
    # DS1 同款版本化：**用本模块自己的迁移表与版本号**（算法共用、表各自持有）
    migrate_schema(raw, tmp, migrations=_MIGRATIONS,
                   current=BATCH_SCHEMA_VERSION)
    for i in tmp:
        _issue(issues, i.level, i.key, i.message, i.suggestion)

    out.schema_version = _as_int(raw.get("schema_version"),
                                 BATCH_SCHEMA_VERSION)
    out.note = str(raw.get("note") or "")
    # V2.4 FR-26.5：`analysis_preset` 升为**已知键**（GUI/CLI 双链路经 pipeline 生效）
    out.analysis_preset = str(raw.get("analysis_preset") or "").strip()
    out.reference_group = str(raw.get("reference_group") or "").strip()
    _phi = raw.get("phi_ref")
    if _phi is not None:
        try:
            out.phi_ref = float(_phi)
        except (TypeError, ValueError):
            _issue(issues, "WARNING", f"{BATCH_FILENAME}:phi_ref",
                   f"Φ_ref={_phi!r} 不是数值", "已回落为 config 初值")
    files = raw.get("files")
    known = {"schema_version", "note", "files", "analysis_preset",
             "reference_group", "phi_ref"}
    out.extra = {k: v for k, v in raw.items() if k not in known}
    if isinstance(files, dict):
        for k, v in files.items():
            if not isinstance(v, dict):
                _issue(issues, "WARNING", f"{BATCH_FILENAME}:{k}",
                       "该文件的条目不是键值对，已忽略", "应为 `role:`/`t_override:` 等")
                continue
            out.files[_norm_key(k)] = _entry_from(v, k, issues)
    out.path = p
    return out


def _entry_from(v: dict, key: str, issues: Optional[list]) -> BatchEntry:
    """把一条 yaml 条目转成 :class:`BatchEntry`（非法值**告警但保留原始键**）。"""
    role = str(v.get("role") or "").strip()
    if role and role not in KNOWN_ROLES:
        _issue(issues, "WARNING", f"{BATCH_FILENAME}:{key}.role",
               f"未知角色 {role!r}", f"可用：{'、'.join(KNOWN_ROLES)}；"
               "本次按 sample 处理，原值保留在文件里")
    t_raw = v.get("t_override")
    t: Optional[float] = None
    if t_raw is not None:
        try:
            t = float(t_raw)
        except (TypeError, ValueError):
            _issue(issues, "WARNING", f"{BATCH_FILENAME}:{key}.t_override",
                   f"时间 {t_raw!r} 不是数值", "已回落为文件名解析值")
    t_src = str(v.get("t_src") or "").strip()
    if t_src and t_src not in KNOWN_T_SRC:
        _issue(issues, "WARNING", f"{BATCH_FILENAME}:{key}.t_src",
               f"未知来源标记 {t_src!r}", f"可用：{'、'.join(KNOWN_T_SRC)}")
    return BatchEntry(role=role if role in KNOWN_ROLES else "",
                      t_override=t, t_src=t_src, dye=str(v.get("dye") or ""),
                      dup_index=str(v.get("dup_index") or "").strip(),
                      raw=dict(v))


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _issue(issues: Optional[list], level: str, key: str, message: str,
           suggestion: str = "") -> None:
    """追加一条问题（``issues=None`` 时只记日志，绝不抛）。"""
    if issues is not None:
        issues.append(ConfigIssue(level, key, message, suggestion))
    if level != "INFO":
        log.warning("%s: %s", key, message)


# ═══════════════════════════ 写（严出，只有显式保存才落盘） ═══════════════════════════
def save_batch(batch: BatchFile, data_dir: Path | str) -> tuple[bool, str]:
    """把批次原子写回数据目录。

    Returns:
        ``(是否成功, 中文说明)``。**失败不抛异常**（机制约束 4）——只读目录、
        无权限、磁盘满都走同一路径：返回 ``(False, 原因)``，由调用方在状态栏提示
        "目录只读，整定仅本次有效"。失败时**不会**写进 ``runs/``，也不留半个文件
        （``atomic_write`` 保证）。

    **严出**：只有这个函数会写盘；格式规范化（``schema_version`` 在前、``files``
    按路径排序）都在这里做。
    """
    d = Path(data_dir)
    p = batch_path(d)
    batch.path = p
    try:
        import yaml
    except ImportError:                                 # pragma: no cover
        return False, "未安装 pyyaml，无法保存批次文件"
    body = ("# dye_tool 实验批次文件（V2.0 FR-14）——随数据目录一起拷走\n"
            "# 只增不改原始数据文件；本文件由 GUI 在「整定提交/显式保存」时写出。\n"
            "# 未知键会被原样保留（宽进严出）；schema_version 供将来字段演化分流。\n")
    try:
        text = body + yaml.safe_dump(batch.as_dict(), allow_unicode=True,
                                     sort_keys=False, default_flow_style=False)
    except Exception as err:                            # noqa: BLE001
        return False, f"批次内容无法序列化：{err}"
    try:
        d.mkdir(parents=True, exist_ok=True)
        with atomic_write(p, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as err:
        return False, f"目录不可写（整定仅本次有效）：{err}"
    except Exception as err:                            # noqa: BLE001
        return False, f"批次文件写入失败：{err}"
    log.info("已保存批次文件: %s（%d 个文件条目）", p, len(batch.files))
    return True, f"已保存 {BATCH_FILENAME}（{len(batch.files)} 条）"


# ═══════════════════════════ 与 Scan 的桥接 ═══════════════════════════
def apply_to_scans(batch: BatchFile, scans: Iterable, data_dir: Path | str) -> int:
    """把批次整定**应用到** ``Scan`` 列表（不改批次对象）。

    只做两件事（角色与时间），染料走 :func:`BatchFile.dye_map` 交给流水线：

    * ``role``：覆盖 ``Scan.role``；
    * ``t_override``：写 ``Scan.t``，并把 ``t_src`` 记为整定来源
      （``Scan.t_original`` 仍是文件名解析值——双轨沿用 FR-12）。

    Returns:
        实际被覆盖的 ``Scan`` 个数（批文件里没有的文件不动）。
    """
    base = Path(data_dir)
    n = 0
    for s in scans:
        try:
            rel = _norm_key(Path(s.path).resolve().relative_to(base.resolve()))
        except (ValueError, OSError):
            rel = _norm_key(Path(s.path).name)          # 目录外的文件按文件名兜底
        key = batch.resolve(rel)
        e = batch.files.get(key)
        if e is None:
            continue
        if e.role:
            s.role = e.role
        if e.t_override is not None:
            # O11（V2.x 审查）：必须**先取**原始值再覆盖 s.t——原实现先写 t，
            # 对 t_src is None 的对象（老/手工构造的 Scan）`_t_src_of(s)` 会回落到
            # 刚写进去的新 t，t_original 就丢了，双轨（FR-12）失效。
            _orig = _t_src_of(s)
            s.t = float(e.t_override)
            s.t_src = _orig
        if e.dup_index:
            s.rep = str(e.dup_index)
        n += 1
    # V2.9.1 FR-61：显式参比组优先于文件名/角色前缀（整组覆盖；旧参比降回 sample）
    if batch.reference_group:
        for s in scans:
            if str(getattr(s, "group", "")) == batch.reference_group:
                s.role = "reference"
            elif s.role == "reference":
                s.role = "sample"
    return n


def from_scans(scans: Iterable, data_dir: Path | str,
               note: str = "",
               base: Optional["BatchFile"] = None) -> BatchFile:
    """按当前 ``Scan`` 状态**生成**一个批次（GUI"保存整定"用）。

    只记录**与文件名解析值不同**的项（``t`` 被改过才写 ``t_override``；
    角色非默认 ``sample`` 才写 ``role``）——这样批次文件里留下的都是"人做过的事"，
    而不是把 500 个文件的默认值全抄一遍。

    ``base``（A.M4，V2.x 审查）：传入**已载入的**批次时做**合并**，而不是重建。
    原实现每次新建 ``BatchEntry(raw={})`` → GUI 保存一次就丢掉：文件里手写的
    ``note``、条目里的未知键、以及**本次没扫描到的文件条目**。这与本模块自己
    宣称的"宽进严出/未知键原样保留"正好相反。
    合并语义：base 的全部条目/顶层未知键/note 保留；本次扫描到的文件按当前
    Scan 状态**重写已知字段**（把角色改回 sample、撤销时间整定都会如实清空），
    未知键不丢；base 里本次未扫描到的条目原样保留（可能是临时拔掉的盘）。
    """
    import copy as _copy

    b = Path(data_dir)
    scan_list = list(scans)
    out = empty_batch(data_dir)
    if base is not None:
        out.schema_version = base.schema_version
        out.extra = _copy.deepcopy(base.extra)
        out.note = base.note
        out.analysis_preset = base.analysis_preset
        out.reference_group = base.reference_group
        out.phi_ref = base.phi_ref
        out.files = _copy.deepcopy(base.files)
    if note:
        out.note = note
    for s in scan_list:
        try:
            rel = _norm_key(Path(s.path).resolve().relative_to(b.resolve()))
        except (ValueError, OSError):
            rel = _norm_key(Path(s.path).name)
        key = out.resolve(rel)
        e = out.files.get(key) or BatchEntry()
        # 已知字段按**当前状态**重写（含"清空"）：role 回到 sample、撤销整定都要落盘
        e.role = "" if str(getattr(s, "role", "sample")) in ("", "sample") \
            else str(s.role)
        if abs(float(s.t) - _t_src_of(s)) > 1e-9:
            e.t_override = float(s.t)
            e.t_src = "manual"
        else:
            e.t_override = None
            e.t_src = ""
        e.dup_index = (str(s.rep) if bool(getattr(s, "rep_edited", False)) else "")
        if e.role or e.t_override is not None or e.dye or e.dup_index or e.raw:
            out.files[key] = e
    # 显式参比组：只从本次扫描到的角色推断（唯一组才写，含糊不猜）
    ref_groups = {str(s.group) for s in scan_list if str(getattr(s, "role", "")) == "reference"}
    # V2.9.1 O2：无 reference 角色 = 用户回到“自动”，Must clear base 的旧显式参比（
    # 否则旧组名会永远粘住，且没有 UI 清空路径）。唯一 reference 组才写，含糊不猜。
    out.reference_group = next(iter(ref_groups)) if len(ref_groups) == 1 else ""
    return out
