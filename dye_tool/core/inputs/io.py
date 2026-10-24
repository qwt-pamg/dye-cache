# -*- coding: utf-8 -*-
"""文件解析层：文件名元数据、光谱读取、目录收集。

迁移自 legacy ``singlet_o2_phi`` 的 F1 段（``parse_filename`` / ``read_scan`` /
``collect_scans`` / ``read_text_robust``）与 JACS 补丁段的 ``_read_spectrum_file``
/ ``_spectrum_arrays``，数值与解析行为**逐行保持**，仅做两处工程化改造：

1. ``print``/``sys.exit`` → ``logging`` + 返回值（GUI 不得被 IO 错误打断）；
2. ``collect_scans`` 增加 ``progress_cb``，把长目录扫描的进度回调给 UI。

命名约定（设计文档附录 A）：组名[_重复号]-秒数[+分钟数].txt
"""
from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

import numpy as np

from ..paths import walk_files

log = logging.getLogger("dye_tool.io")

ProgressCb = Optional[Callable[[int, str], None]]

#: ``_read_spectrum_file`` 用的数字抽取正则（legacy 原样）
_NUM_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


# ═══════════════════════════ 数据结构 ═══════════════════════════
@dataclass
class Scan:
    """单个光谱文件（一个时间点）。``excluded`` 非空 = 被剔除原因。

    ``t`` 是**生效时间**（下游全部用它）；``t_src`` 是从文件名解析出的**原始
    时间**，仅在用户做过时间整定（FR-12）时与 ``t`` 不同——工作表要把两者
    并排显示供核对。
    """
    path: Path
    group: str
    rep: str = ""
    role: str = "sample"
    t: float = 0.0
    wl: Optional[np.ndarray] = None
    a: Optional[np.ndarray] = None
    excluded: str = ""
    t_src: Optional[float] = None
    # ── DS2｜溯源字段（FR-18，设计《重构设计_兼容性批次.md》§1.2）──────────
    # 本批**只写不读**：qc/dyes/excel/绘图零改动，消费方留给 γ 批（FR-15 按数据源
    # 路由 QC 判据）。两字段都 compare=False —— dataclass 的 __eq__ 语义就此冻结在
    # 既有字段上，这是"既有等价性/回归断言零改动"的**机制性**保证（原则 5），
    # 不是"记得别比较"的约定。io_key 又在 Scan 之前算出（见 collect_scans），
    # 所以它们结构上也不可能进缓存键。
    # source ∈ {"txt", "fds", "spc", "xlsx"}：默认 "txt" 覆盖 .txt/.csv 文本解析；
    # "fds" = FDS 二进制分支；"spc" = 仪器 OLE2 分支；"xlsx" = FR-35 Excel 分支。
    # meta 只装二进制/Excel 解析器返回的仪器元信息，键集以适配器为准；
    # 文本分支恒为空 dict。
    source: str = field(default="txt", compare=False)
    meta: dict = field(default_factory=dict, compare=False)
    #: V2.9 FR-60.1/60.4：文件名是否含时间后缀（UI 状态列只做提示，不参与数值）。
    time_suffix: bool = field(default=True, compare=False)
    #: V2.9 FR-60.4：「全部留空待填」态中尚未填写时间（不参与下游分析）。
    time_blank: bool = field(default=False, compare=False)
    #: V2.9 FR-60.2：文件名解析出的重复号（``rep`` 的原始值；仅整定后才与 rep 不同）。
    rep_src: Optional[str] = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        """是否可用（未剔除且已读出光谱）。"""
        return not self.excluded and self.wl is not None and self.a is not None

    @property
    def tag(self) -> str:
        """日志用短标签：``组_重复``。"""
        return f"{self.group}_{self.rep}" if self.rep else self.group

    @property
    def t_original(self) -> float:
        """文件名解析出的原始时间（未整定时等于 :attr:`t`）。"""
        return float(self.t if self.t_src is None else self.t_src)

    @property
    def t_edited(self) -> bool:
        """该行的时间是否被手动整定过（FR-12）。"""
        return self.t_src is not None and abs(float(self.t) - float(self.t_src)) > 1e-9

    @property
    def rep_original(self) -> str:
        """文件名解析出的重复号（未整定时等于 :attr:`rep`）。"""
        return str(self.rep if self.rep_src is None else self.rep_src)

    @property
    def rep_edited(self) -> bool:
        """重复号是否被手动整定过（V2.9 FR-60.2 持久化用）。"""
        return self.rep_src is not None and str(self.rep) != str(self.rep_src)


@dataclass
class Spectrum:
    """一支独立光谱（FR-4 用）。``kind`` ∈ {abs, em, unknown}。"""
    path: Path
    wl: np.ndarray
    y: np.ndarray
    kind: str = "unknown"
    label: str = ""
    meta: dict = field(default_factory=dict)
    excluded: str = ""

    @property
    def ok(self) -> bool:
        return not self.excluded and self.wl is not None and self.wl.size > 0

    def lambda_max(self, lo: float | None = None, hi: float | None = None) -> float:
        """返回 ``[lo, hi]`` 内的最大峰位（nm）；无有效点 → NaN。"""
        m = np.isfinite(self.y)
        if lo is not None:
            m &= self.wl >= float(lo)
        if hi is not None:
            m &= self.wl <= float(hi)
        if not m.any():
            return float("nan")
        return float(self.wl[m][int(np.argmax(self.y[m]))])


# ═══════════════════════════ 编码健壮读取 ═══════════════════════════
def read_text_robust(path: Path | str) -> str:
    """兼容 GBK/UTF-8/UTF-16/BOM 的文本读取（数据行均为 ASCII，头部乱码无害）。"""
    raw = Path(path).read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:400]:
        return raw.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


# ═══════════════════════════ 文件名元数据 ═══════════════════════════
def parse_filename(stem: str, fn: dict) -> dict:
    """文件名主干 → ``{group, rep, t, role}``。时间 = 最后一个横杠后的数字。

    ``xx`` → xx 秒；``xx+y`` → xx + 60y 秒；无时间后缀 → t=0。
    角色按前缀识别（bg/blank/dark/ref/n2/nan3/azide），其余为 sample。
    """
    m = re.match(fn.get("time_pattern",
                        r"^(?P<stem>.+)-(?P<sec>\d+)(?:\+(?P<min>\d+))?$"), stem)
    if m:
        t = float(m.group("sec")) + 60.0 * float(m.group("min") or 0)
        head = m.group("stem")
    else:
        t, head = 0.0, stem
    gm = re.match(fn.get("group_pattern",
                         r"^(?P<group>.+?)(?:_(?P<rep>\d+))?$"), head)
    group = gm.group("group") if gm else head
    rep = (gm.group("rep") or "") if gm else ""
    roles = fn.get("roles", {})
    role = roles.get(group) or next(
        (roles[k] for k in sorted(roles, key=len, reverse=True)
         if group.startswith(k)), "sample")
    if m is None and fn.get("no_time_suffix", "zero") == "background":
        role = "background"
    return {"group": group, "rep": rep, "t": t, "role": role}


def has_time_suffix(stem: str, fn: dict) -> bool:
    """文件名是否命中时间后缀规则（V2.9 FR-60.1；不改变 parse_filename 契约）。"""
    return bool(re.match(fn.get("time_pattern",
                                r"^(?P<stem>.+)-(?P<sec>\d+)(?:\+(?P<min>\d+))?$"),
                         stem))


def parse_time_only(stem: str, fn: dict) -> float:
    """只解析辐照时间（A1 用例：``90+5`` → 390 s）。"""
    return float(parse_filename(stem, fn)["t"])


# ═══════════════════════════ 两列光谱读取 ═══════════════════════════
def _ncols_of(io_cfg: Optional[dict]) -> int:
    """``io.ncols`` 的**唯一**读取口（坏值一律回退 2）。

    M13：``ncols:``（yaml 留空）→ ``None`` → ``int(None)`` TypeError 穿透全部兜底；
    ``ncols: 0`` 同样无意义。解析与缓存键**必须**共用本函数，否则会出现"键按 2 算、
    解析按别的算"的不一致。
    """
    raw = (io_cfg or {}).get("ncols", 2)
    try:
        v = int(raw)
    except (TypeError, ValueError):
        log.warning("io.ncols 配置无效（%r）→ 回退 2 列", raw)
        return 2
    return v if v > 0 else 2


# ═══════════════════ 扫描扩展名（V1.3 FR-13.3：FDS 自动纳入） ═══════════════════
def scan_extensions(io_cfg: Optional[dict]) -> tuple[str, ...]:
    """``collect_scans`` / ``preview_groups`` 实际使用的扩展名元组（小写）。

    V1.3 的关键点：**用户不用改任何配置，``.fds`` 就该被识别**。但既有用户的
    ``config.yaml`` 里已经写死了 ``extensions: [.txt, .csv]``（本机那份就是），
    深合并会让用户值**覆盖**掉默认值里的新项——于是"加个默认值"根本不够。
    这里按开关把 ``io.fds.extensions`` **并进**集合（并集而非替换），
    既有扩展名一个不少、新格式按需启用。

    V2.0 β 批：``.spc`` 走**同一套**逻辑（``io.spc.enable`` / ``io.spc.extensions``），
    两个开关各自独立——只想要 FDS 不想要 SPC（或反之）时互不影响。
    V2.6 FR-35：.xlsx 同形，但**默认关**（io.excel.enable 缺省按 False）；只有显式
    打开时 .xlsx 才进入扫描集合，既有目录行为逐位不变。
    """
    io_cfg = io_cfg or {}
    exts = {str(e).lower() for e in io_cfg.get("extensions", [".txt", ".csv"])}
    # (配置节, 默认扩展名, 缺省开关)：excel 与 fds/spc 不同，缺省关。
    for sec, fallback, default_enable in (("fds", [".fds"], True),
                                          ("spc", [".spc"], True),
                                          ("excel", [".xlsx"], False)):
        sub = io_cfg.get(sec) or {}
        if sub.get("enable", default_enable):
            exts |= {str(e).lower() for e in sub.get("extensions", fallback)}
    return tuple(sorted(exts))


def fds_anchor_of(io_cfg: Optional[dict]) -> bytes:
    """``io.fds.serial_anchor`` 的**唯一**读取口（M1）。

    M1 的坑：这个键在 V1.3 首版是**死配置**——定义了、有参数，但没有任何调用方
    传它，用户按 FR-13.5 的键名在 ``config.yaml`` 里设置会被**静默忽略**。

    另一个坑（写在这里免得后人再踩）：默认值是空串 ``""``，若直接把它喂给
    ``buf.find(b"")`` 会**恒返回 0** → ``d0 = 0`` → 全部文件解析报错。
    所以空串必须回退到内置默认锚点。

    第三个坑（G11，第二轮审查）：回退判据必须看**编码结果**，不能看剥文本后的真值。
    非 ASCII 锚点（例如用户手写中文锚）过 ``encode("ascii","ignore")`` 会得到
    ``b""``——若只判 ``txt`` 非空就返回，那个空 bytes 会被直接送进 ``read_fds_bytes``
    → ``find(b"") == 0`` → d0=0 → **所有 FDS 文件被剔除**，而且报错信息完全指不到
    真正原因。所以这里 ``enc or DEFAULT``：编码后为空也算"没给有效锚点"。
    """
    from .io_fds import DEFAULT_SERIAL_ANCHOR
    raw = ((io_cfg or {}).get("fds") or {}).get("serial_anchor", "")
    txt = str(raw or "").strip()
    enc = txt.encode("ascii", "ignore") if txt else b""
    if txt and not enc:
        log.warning("io.fds.serial_anchor 含非 ASCII 字符（%r）→ 过滤后为空，"
                    "回退内置默认锚点 %r", txt, DEFAULT_SERIAL_ANCHOR)
    return enc or DEFAULT_SERIAL_ANCHOR


def is_fds_path(path: Path | str) -> bool:
    """按**内容**判断是不是 FDS 存档（不看扩展名；读盘失败即 False）。

    P2：原先这里逐字复刻了 ``io_fds.is_fds_file``——两份实现会静默漂移。
    现在直接复用那一份（同模块内延迟 import 是本文件既有做法）。
    """
    from .io_fds import is_fds_file
    return is_fds_file(path)


def is_spc_path(path: Path | str) -> bool:
    """按**内容**判断是不是 SPC ``.spc`` 存档（不看扩展名；读盘失败即 False）。

    实现只有一处真相（``io_spc.is_spc_file``），本函数只是 ``io`` 侧的统一入口，
    与 :func:`is_fds_path` 同形——避免"两份会漂移的分支判断"（P2 的教训）。
    """
    from .io_spc import is_spc_file
    return is_spc_file(path)


def is_excel_path(path: Path | str) -> bool:
    """按**后缀 + 内容**判断是不是 Excel 工作簿（FR-35；默认关时不会被调用）。

    实现只有一处真相（io_excel.is_excel_file：.xlsx 后缀 + ZIP 魔数 +
    xl/workbook.xml），本函数只是 io 侧的统一入口，与 is_spc_path 同形。
    """
    from .io_excel import is_excel_file
    return is_excel_file(path)


def read_scan(path: Path | str, io_cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """读取单文件 → (波长, 吸光度)。

    自动跳过标题/乱码/列数不符行（如 ``"波长(nm)","吸光值"`` 与 GBK 乱码表头）；
    吸光度为空串或非数值 → NaN。有效点不足 10 → 抛 ``ValueError``（由调用方记剔除）。

    V1.3：先按**内容嗅探**拦下 ``.FDS`` 二进制存档（``IIHIDTAG`` 魔术字）转给
    :mod:`core.io_fds`。判别走内容而非扩展名——扩展名可以被随手改掉，内容不会。
    V2.0 β：同一位置再拦 ``.spc``（OLE2 + ``X Data`` 流，:mod:`core.io_spc`）；
    **多数据集文件只返回第一个数据集**（``collect_scans`` 走 :func:`_read_scan_ex`
    会逐数据集展开）。

    DS2：函数签名与返回值**逐位不变**（既有调用方零改动）；需要溯源信息的调用方
    走 :func:`_read_scan_ex`——两者是同一条解析路径，不会漂移。
    """
    rec = _read_scan_ex(path, io_cfg)[0]
    return rec["wl"], rec["a"]


def _read_scan_ex(path: Path | str, io_cfg: dict) -> list[dict]:
    """读取单文件 → **记录列表** ``[{"wl", "a", "source", "meta"}, ...]``（DS2 溯源）。

    为什么是列表（V2.0 β）：``.spc`` 一个文件可以内含多个数据集（``NumberofSaved``
    > 1，设计文档 Q2 要求"逐数据集展开为多条 Scan"）。其余格式恒为长度 1 的列表。

    ``source`` ∈ ``{"txt", "fds", "spc", "xlsx"}``：本函数是"这次走了哪条分支"的**唯一判定处**
    （传给 ``collect_scans`` 落 :attr:`Scan.source`，避免调用方重新嗅探一遍而产生
    两份会漂移的分支判断）。``meta`` 在二进制分支是适配器返回的仪器元信息
    （键集以适配器为准），文本分支恒为 ``{}``。
    """
    io_cfg = io_cfg or {}
    path = Path(path)
    excel_cfg = io_cfg.get("excel") or {}
    if excel_cfg.get("enable", False) and is_excel_path(path):
        from .io_excel import read_excel_file_ex
        recs, why = read_excel_file_ex(path)
        if recs is None:
            raise ValueError(why)
        out: list[dict] = []
        for rec in recs:
            wl = np.asarray(rec["wl"], float)
            y = np.asarray(rec["a"], float)
            # ±inf/NaN 入口归一（与 spc/fds 分支同口径）
            bad = ~np.isfinite(y) & ~np.isnan(y)
            if bad.any():
                log.warning("%s: %d 个非有限吸光度（±inf）已归一为 NaN",
                            path.name, int(bad.sum()))
                y = np.where(bad, np.nan, y)
            out.append({"wl": wl, "a": y, "source": "xlsx",
                        "meta": dict(rec.get("meta") or {})})
        return out
    spc_cfg = io_cfg.get("spc") or {}
    if spc_cfg.get("enable", True) and is_spc_path(path):
        from .io_spc import read_spc_file_ex
        recs, why = read_spc_file_ex(path)
        if recs is None:
            raise ValueError(why)
        out: list[dict] = []
        for rec in recs:
            y = np.asarray(rec["absorbance"], float)
            # ±inf/NaN 入口归一（S1 判据：一律先归一到 NaN，下游按无效点处理）
            bad = ~np.isfinite(y) & ~np.isnan(y)
            if bad.any():
                log.warning("%s: %d 个非有限吸光度（±inf）已归一为 NaN",
                            path.name, int(bad.sum()))
                y = np.where(bad, np.nan, y)
            out.append({"wl": np.asarray(rec["wavelength_nm"], float), "a": y,
                        "source": "spc", "meta": dict(rec.get("meta") or {})})
        return out
    fds_cfg = io_cfg.get("fds") or {}
    if fds_cfg.get("enable", True) and is_fds_path(path):
        from .io_fds import FdsError, read_fds_bytes
        try:
            got = read_fds_bytes(path.read_bytes(), fds_anchor_of(io_cfg))
        except FdsError as err:
            raise ValueError(str(err)) from err
        except OSError as err:
            raise ValueError(f"读取失败: {err}") from err
        wl = np.asarray(got["wavelength_nm"], float)
        y = np.asarray(got["intensity"], float)
        # ±inf/NaN 入口归一（S1 修复确立的判据：一律先归一到 NaN，下游按无效点处理）
        bad = ~np.isfinite(y)
        if bad.any():
            log.warning("%s: %d 个非有限强度值（NaN/±inf）已归一为 NaN",
                        path.name, int(bad.sum()))
            y = np.where(bad, np.nan, y)
        # P3：``wl.size < 10`` 的重查已删——io_fds 的 ``n_b < 10`` 已保证
        # （实测不可达分支，留着只会让读者以为这里还可能出问题）。
        # DS2：meta 直接取 io_fds 的返回（`dict()` 拷一份，防调用方就地改到解析器
        # 内部结构）；键集不在这里过滤/改名——设计 §1.2 明确"以 io_fds 返回为准"。
        return [{"wl": wl, "a": y, "source": "fds",
                 "meta": dict(got.get("meta") or {})}]
    sep = io_cfg.get("separator", ",")
    # M13：yaml 里写 `ncols:`（留空）会得到 None → int(None) TypeError 穿透全部兜底；
    # 写 0 也无意义。统一回退到默认 2（与缓存键的口径一致，见 io_key 注释）。
    ncols = _ncols_of(io_cfg)
    rows: list[tuple[float, float]] = []
    for lineno, line in enumerate(read_text_robust(path).splitlines(), 1):
        parts = [p.strip().strip('"').strip("'") for p in line.split(sep)]
        if len(parts) != ncols:
            continue
        try:
            w = float(parts[0])
        except ValueError:
            continue                                   # 标题/列名/乱码行
        if parts[1] == "":
            log.info("%s 第%d行: 吸光度为空 → NaN", path.name, lineno)
            v = float("nan")
        else:
            try:
                v = float(parts[1])
            except ValueError:
                log.info("%s 第%d行: 吸光度 %r 非数值 → NaN", path.name, lineno, parts[1])
                v = float("nan")
        rows.append((w, v))
    if len(rows) < 10:
        raise ValueError(f"有效数据行不足({len(rows)} 行)")
    wl = np.array([r[0] for r in rows], float)
    a = np.array([r[1] for r in rows], float)
    if np.any(np.diff(wl) <= 0):
        o = np.argsort(wl, kind="stable")
        wl, a = wl[o], a[o]
    # DS2：文本分支（.txt/.csv 同一条解析路径）只写 source，不造元信息。
    return [{"wl": wl, "a": a, "source": "txt", "meta": {}}]


#: 本模块自己的 ``read_scan`` 实现——:func:`_read_one` 用它识别"公共接缝是否被替换"。
#: ``read_scan`` 是对外承诺的解析入口，既有的 S3 回归用例（``test_review_fixes.py:194``）
#: 就是替换 ``core.io.read_scan`` 来模拟"文件被 Excel 锁住、读盘抛 PermissionError"。
_READ_SCAN_IMPL = read_scan


def _read_one(path: Path | str, io_cfg: dict) -> list[dict]:
    """``collect_scans`` 的唯一解析入口 → **记录列表**（见 :func:`_read_scan_ex`）。

    正常情况走 :func:`_read_scan_ex`（与 ``read_scan`` 同一条解析路径 + 溯源字段）。
    但 ``read_scan`` 是**公共接缝**：一旦被替换（测试替身 / 外部补丁），就必须尊重
    替换后的实现、只用它的二元组返回值，溯源字段回默认值——否则补丁被静默绕过，
    既有回归断言失效。这不是假想：实现第一版直接调 ``_read_scan_ex`` 时，单跑
    ``test_io`` 43 条全绿，而全量档 ``test_s3_parse_failure_is_not_cached`` 报
    ``assert ([Scan(..., excluded='')] and '读取失败' in '')`` → 1 failed。

    被替换的接缝只返回一对数组，所以**没有多数据集**可言 → 单条记录。
    """
    if read_scan is _READ_SCAN_IMPL:
        return _read_scan_ex(path, io_cfg)
    wl, a = read_scan(path, io_cfg)                     # 尊重被替换的公共接缝
    return [{"wl": wl, "a": a, "source": "txt", "meta": {}}]


def _iter_files(data_dir: Path, io_cfg: dict) -> Iterator[Path]:
    """按 ``io.recursive`` 遍历目录下所有文件（**不进入 junction/软链接**）。

    G6（第二轮审查）：这里原来直接用 ``data_dir.rglob("*")``，而 ``rglob`` 会
    **跟随 junction**——实测 junction 里的文件会被收进结果。数据目录里放一个
    指向别处的 junction（共享数据、旧实验盘的快捷方式）就会把第三方数据当成
    本次实验的样品读进来，且路径还会触发 ``only_paths`` 的 resolve 比对错位。
    遍历改由 :func:`core.paths.walk_files` 做（下降前剪枝，软链接环也不会递归）。

    保持与原实现一致的地方：**返回全部文件**（含隐藏文件、``__pycache__``），
    扩展名/排除名单过滤仍在调用方——本函数只负责"走哪些文件"。
    """
    return iter(walk_files(data_dir, recursive=bool(io_cfg.get("recursive", True))))


def list_scan_files(data_dir: Path | str, cfg: dict,
                    only_paths: Optional[Iterable[Path | str]] = None) -> list[Path]:
    """FR-39.1：列出候选光谱文件（与 :func:`collect_scans` **同一枚举/排序逻辑**）。

    这是 GUI 参数表格的**唯一行序来源**（Q-E11：表格行序必须与解析顺序同源，
    禁止界面层另写一套枚举/排序）：

    * 枚举：:func:`_iter_files`（``walk_files``，不走 junction，尊重 ``io.recursive``）；
    * 排序：``sorted``（与 collect_scans 完全一致）；
    * 过滤：``scan_extensions/cfg["io"]`` 扩展名 + ``io.exclude`` 文件名前缀；
    * ``only_paths`` 给定则再按 resolve 后大小写不敏感过滤（GUI 勾选后重跑用）。

    返回顺序 = 解析顺序（一个文件可能产出多条 Scan 的格式除外，见 collect_scans）。
    """
    data_dir = Path(data_dir)
    io_cfg = cfg["io"]
    exts = scan_extensions(io_cfg)
    excl = tuple(io_cfg.get("exclude", ["_"]))
    keep: Optional[set[str]] = None
    if only_paths is not None:
        keep = {str(Path(p).resolve()).lower() for p in only_paths}
    return [p for p in sorted(_iter_files(data_dir, io_cfg))
            if p.is_file() and p.suffix.lower() in exts
            and not p.name.startswith(excl)
            and (keep is None or str(p.resolve()).lower() in keep)]


#: 解析缓存的值格式代号（V2.0 β：从元组变记录列表时 +1；换值格式必须 +1，
#: 见 collect_scans 内注释）。
_CACHE_GEN = 2


def _io_key(io_cfg: dict) -> tuple:
    """影响解析结果的配置 -> 解析缓存键（collect_scans 的唯一来源）。

    V2.6 C 道把原先写在 collect_scans 里的内联元组抽成模块级函数：一是让
    "开关是否进键"可被测试直接断言（_io_key(on) != _io_key(off)），二是消除
    内联表达式与注释各说一套的漂移风险。键内容：分隔符 / 列数 / FDS 锚点 /
    SPC 开关 / Excel 开关 / 值格式代号。io.fds.enable 仍不进键（分支已由内容
    魔术字唯一决定）；io.spc.enable 与 io.excel.enable 进键是结构性保证——它们
    决定解析时走不走二进制/Excel 分支。
    """
    io_cfg = io_cfg or {}
    return (str(io_cfg.get("separator", ",")), _ncols_of(io_cfg),
            fds_anchor_of(io_cfg)
            if (io_cfg.get("fds") or {}).get("enable", True) else b"",
            bool((io_cfg.get("spc") or {}).get("enable", True)),
            bool((io_cfg.get("excel") or {}).get("enable", False)),
            _CACHE_GEN)


#: 语义同 _io_key 的公开别名（测试/调试可直接用）
io_key = _io_key


def collect_scans(data_dir: Path | str, cfg: dict,
                  progress_cb: ProgressCb = None,
                  only_paths: Optional[Iterable[Path | str]] = None,
                  use_cache: bool = True) -> list[Scan]:
    """递归扫描目录，解析元数据并读取光谱（坏文件剔除并记日志）。

    :param only_paths: 给定则只保留这些文件（GUI 勾选后重跑用），大小写不敏感。
    :param progress_cb: ``(percent, stage)`` 进度回调，线程安全投递给 UI。
    :param use_cache: 命中解析缓存则跳过读盘（FR-6.8）；文件 mtime/size 变了
        自动失效——数值走同一代码路径，只是不跑第二遍。
    """
    data_dir = Path(data_dir)
    io_cfg, fn_cfg = cfg["io"], cfg["filename"]
    if not data_dir.is_dir():
        log.error("数据目录不存在: %s", data_dir)
        return []
    # FR-39.1：枚举/排序/过滤**单点同源**（GUI ε 表格行序直接复用同一 helper）
    files = list_scan_files(data_dir, cfg, only_paths=only_paths)
    scans: list[Scan] = []
    total = max(len(files), 1)
    # O4：原来的键里带 `io.encoding`，但 `read_scan` 走的是自动探测
    # （utf-8-sig → gbk），**根本不读这个配置**——键里带上它只会让"改了没用的
    # 参数"白白产生一批新键（缓存命中率下降）。只保留真正影响解析的两个量。
    #
    # L7（评审记录）：FDS 的**定位锚点**也是真实解析输入，必须进键。
    # 否则用户改了 `io.fds.serial_anchor` 之后，同一批文件（mtime/size 未变）
    # 会**命中旧解析结果**——这正是 V1.2 的 S5（渲染缓存键缺染料命名维度）在
    # FDS 上重演。`io.fds.enable` 不进键：它只决定"要不要走 FDS 分支"，
    # 而走了哪条分支已经由文件内容（魔术字）唯一决定。
    # V2.x 审查 O6：`io.spc.enable` 已显式并进 io_key（结构性保证，与 fds anchor 对称），
    # 下面注释里的"spc 同理不进键"是旧说法，已改正。
    hit = miss = 0
    n_files = len(files)

    # ── ① 先问缓存：命中的文件根本不碰磁盘 ────────────────────────────
    keys: list = [None] * n_files
    loaded: list = [None] * n_files          # 未命中者的并行解析结果
    todo: list[int] = []
    # ⚠ 缓存**值格式**变了就必须换 gen（V2.0 β 实测踩到）：值从
    # `(wl, a, source, meta, err)` 元组变成**记录列表**（多数据集 .spc）。同一进程
    # 里若先跑过旧格式的条目（例如同一 temp 目录里同 mtime/size 的文件），
    # 命中后会拿到旧元组 → `rec["wl"]` 报
    # `TypeError: list indices must be integers or slices, not str`。
    # 把它并进 io_key 是**结构性**保证：换 gen 即全体失效，不依赖"记得清缓存"。
    # V2.3（评审记录 F8）：`io.spc.enable` 也并进键，与 fds anchor 对称。
    # 语义上它只决定"扫描时带不带 .spc 扩展名"，理论上不会造成陈旧命中；
    # 但把"影响解析输入的开关"一律放进键是**结构性**保证（比"记得它对结果无影响"
    # 可靠）——成本是一个 bool。
    # V2.6 C 道：键内容抽到模块级 _io_key()（新增 excel.enable 一个 bool）。
    io_key = _io_key(io_cfg)
    for i, path in enumerate(files):
        ck = None
        if use_cache:
            try:
                st = path.stat()
                ck = (str(path), int(st.st_mtime_ns), int(st.st_size), io_key)
                got = _cache_get(ck)         # O2：命中即标记为最近使用
            except OSError:
                got, ck = None, None
        else:
            got = None
        keys[i] = ck
        if got is not None:
            loaded[i] = got                 # 命中：直接当作"已解析"
        else:
            todo.append(i)

    # ── ② 未命中的文件并行解析（V1.2 P-C）────────────────────────────
    # 读盘 + 文本切分是**纯 I/O 与 numpy**，不共享可变状态，线程池能摊到多核；
    # 300 文件批量时收益最明显。顺序必须与串行完全一致（GUI 表格行序与 A1 的
    # 文件顺序断言都依赖它）→ 用 map 按提交顺序收集，绝不用 as_completed。
    parallel = (bool(io_cfg.get("parallel", True)) and len(todo) >=
                int(io_cfg.get("parallel_min_files", 64)))
    if parallel:
        try:
            from concurrent.futures import ThreadPoolExecutor
        except ImportError:                                # pragma: no cover
            parallel = False
    if parallel:
        workers = int(io_cfg.get("parallel_workers", 0) or 0) or \
            (os.cpu_count() or 4)
        workers = max(1, min(workers, 8, len(todo)))
        paths = [files[i] for i in todo]

        def _load_one(path: Path):
            """线程内只做纯读+解析，异常转成与原串行路径一致的错误串。"""
            try:
                return _read_one(path, io_cfg), ""
            except (ValueError, OSError) as err:
                # 失败项 source 无意义（该 Scan 会被 excluded，且失败不入缓存）→ 取默认值
                return [], f"读取失败: {err}"

        with ThreadPoolExecutor(max_workers=workers) as pool:
            got_list = list(pool.map(_load_one, paths))
        for idx, res in zip(todo, got_list):
            # ⚠ `_load_one` 返回的是 **(记录列表, 错误串)** 二元组（与原实现
            # "四元组 + 错误串"同形）。V2.0 β 第一版在这里漏了拆包，于是
            # `loaded[idx]` 里躺的是二元组 → 组装段报
            # `解析结果形态异常（list … elem_types=['list','str']）`。
            loaded[idx] = res
        log.debug("并行解析 %d/%d 个文件（未命中缓存者，%d 线程）",
                  len(todo), n_files, workers)

    # ── ③ 按原顺序组装 Scan ─────────────────────────────────────────
    # V2.0 β：**一个文件可以产出多条 Scan**（``.spc`` 的 ``NumberofSaved`` > 1 时
    # 逐数据集展开，设计文档 Q2）。其余格式恒为 1 条，顺序与文件名顺序一致。
    for i, path in enumerate(files):
        fn_meta = parse_filename(path.stem, fn_cfg)     # 文件名元信息（≠ 仪器元信息）
        res: object = loaded[i]
        err = ""
        if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], str):
            res, err = res[0], res[1]       # 并行结果（记录列表, 错误串）
        if res is None:                     # 并行关闭（或文件数不足阈值）时的串行兜底
            try:
                res, err = _read_one(path, io_cfg), ""
            except (ValueError, OSError) as exc:
                res, err = [], f"读取失败: {exc}"
        if not res:
            # 解析失败（或空记录）：仍然产出一条 excluded 的 Scan，让用户看见这个文件
            s = Scan(path=path, group=fn_meta["group"], rep=fn_meta["rep"],
                     role=fn_meta["role"], t=fn_meta["t"], t_src=fn_meta["t"],
                     time_suffix=has_time_suffix(path.stem, fn_cfg),
                     rep_src=fn_meta["rep"])
            s.excluded = err or "解析失败"
            log.warning("剔除 %s: %s", path.name, s.excluded)
            scans.append(s)
            if progress_cb and ((i + 1) % 5 == 0 or i + 1 == len(files)):
                progress_cb(int(100 * (i + 1) / total),
                            f"读取文件 {i + 1}/{len(files)}")
            continue
        for k, rec in enumerate(res):
            # 防御（V2.0 β 实测教训）：缓存/接缝是**跨版本可变**的，旧格式的值一旦
            # 漏进来就会以 `rec["wl"]` 的 TypeError 面目出现（指向写入点，而不是
            # 真正的原因）。这里显式判形态并给出可操作的错误。
            if not isinstance(rec, dict) or "wl" not in rec:
                raise ValueError(
                    f"解析结果形态异常（{type(rec).__name__}，期望含 'wl' 的 dict）"
                    f"——多半是旧版本解析缓存残留：清空缓存（clear_scan_cache）"
                    f"或重新载入目录后重试  [file={path.name} "
                    f"res_len={len(res)} elem_types="
                    f"{[type(x).__name__ for x in res][:4]}]")
            s = Scan(path=path, group=fn_meta["group"], rep=fn_meta["rep"],
                     role=fn_meta["role"], t=fn_meta["t"], t_src=fn_meta["t"],
                     time_suffix=has_time_suffix(path.stem, fn_cfg),
                     rep_src=fn_meta["rep"])
            # DS2：溯源字段的**唯一写入点**（设计 §1.2 指的就是这一段）。source/meta
            # 由解析路径带出来，不在这里重新嗅探分支（避免第二份会漂移的判断）。
            s.wl = rec["wl"]
            s.a = rec["a"]
            s.source = rec["source"]
            # 缓存命中的 rec 是缓存里那个 dict 的引用；拷一份，维持 O8 的"缓存不可变"
            # 契约（冷路径上是新 dict，多这一次拷贝只是让两条路径口径一致）。
            s.meta = dict(rec.get("meta") or {})
            if len(res) > 1:
                # 多数据集：与主数据集**同组同时间**，靠 rep 区分（否则两条 Scan
                # 完全等价，下游分组/重复时间检查会认为是"重复时间点"）。
                s.rep = f"{fn_meta['rep']}{k + 1}" if fn_meta["rep"] else str(k + 1)
                s.meta.setdefault("dataset_rep", s.rep)
            scans.append(s)
        # S3：**只在解析成功时才写缓存**。原先无条件写，于是"文件被 Excel 锁住"
        # 这类**暂时性**失败会被缓存粘住：用户关掉 Excel 后 mtime/size 都没变，
        # 重跑永远命中那条失败记录，还以为是数据坏了（只有点「重新载入」才恢复）。
        # 失败项不入缓存 → 下次自然重试；代价只是坏文件每次重读（本来就该读）。
        if use_cache and keys[i] is not None:
            # O8：缓存里存的是**同一批 numpy 数组的引用**，命中方拿到后若就地改
            # （QC 里 a.astype().copy() 之类是安全的，但外部代码未必）会串改缓存。
            # 存之前拷一份，代价纳秒级，换来"缓存不可变"这条简单契约。
            # DS2：source/meta 一并进缓存**值**（键不受影响，见 io_key）。否则冷跑
            # 有 meta、热跑（命中缓存）meta 为空——γ 批按数据源路由 QC 判据时，
            # 同一批文件"第二次看"会静默换判据，正是 S5/L7 那类缓存陈旧病。
            _cache_put(keys[i], [
                {"wl": r["wl"].copy(),
                 "a": None if r["a"] is None else r["a"].copy(),
                 "source": r["source"], "meta": dict(r.get("meta") or {})}
                for r in res])
        if progress_cb and ((i + 1) % 5 == 0 or i + 1 == len(files)):
            progress_cb(int(100 * (i + 1) / total),
                        f"读取文件 {i + 1}/{len(files)}")
    # 命中数：本批里有多少文件是"没读盘"的（缓存命中）。
    # O1：原来在生成器里对每个文件重建一次 set(todo) → O(n²)，1 万文件时约 10⁸ 次
    # 操作。这里预先算成集合，退回 O(n)。
    todo_set = set(todo)
    hit = sum(1 for i in range(n_files)
              if use_cache and keys[i] is not None and i not in todo_set)
    miss = len(todo)
    if use_cache:
        _CACHE_STATS["hit"] += hit
        _CACHE_STATS["miss"] += miss
        if hit:
            log.info("扫描缓存命中 %d/%d（免重复读盘解析）", hit, n_files)
    log.info("F1: 共发现 %d 个文件, 成功解析 %d 个, %d 个组",
             len(scans), sum(1 for s in scans if not s.excluded),
             len({s.group for s in scans}))
    return scans


# ═══════════════ 解析缓存（FR-6.8：改参数"再看一眼"不重复读盘） ═══════════════
#: ``(路径, mtime, size, io 参数指纹)`` → **记录列表**
#: ``[{"wl", "a", "source", "meta"}, ...]``（V2.0 β 起是列表，承载多数据集 ``.spc``）。
#: DS2：溯源字段进的是缓存**值**，不是键——``io_key`` 在 Scan 存在之前就算好了
#: （collect_scans 的 ① 段），新字段结构上无法进键（设计 §1.2 的机制性保证）。
_SCAN_CACHE: "OrderedDict" = OrderedDict()
_CACHE_STATS = {"hit": 0, "miss": 0}

#: 缓存上限（O2）。**只存已解析的谱**，一万文件的量级约 65 MB，长会话（反复
#: 重新载入不同目录）会一直涨。超过上限按"最久未命中"淘汰——用 ``OrderedDict``
#: 保持插入序，命中时把键移到末尾即可，代价 O(1)、不需要额外结构。
SCAN_CACHE_MAX = 20000


def _cache_get(key):
    """读缓存并把命中项标记为"最近使用"（O2 的 LRU 语义）。"""
    if key not in _SCAN_CACHE:
        return None
    _SCAN_CACHE.move_to_end(key)
    return _SCAN_CACHE[key]


def _cache_put(key, value) -> None:
    """写入缓存并维护容量上限（O2）。"""
    _SCAN_CACHE[key] = value
    _SCAN_CACHE.move_to_end(key)
    while len(_SCAN_CACHE) > SCAN_CACHE_MAX:
        _SCAN_CACHE.popitem(last=False)


def clear_scan_cache() -> None:
    """清空解析缓存（GUI「重新载入」按钮 / 强制失效时调用）。"""
    _SCAN_CACHE.clear()
    _CACHE_STATS["hit"] = _CACHE_STATS["miss"] = 0


def scan_cache_stats() -> dict:
    """``{"hit": n, "miss": m, "size": k}``（日志与验收用）。"""
    return {**_CACHE_STATS, "size": len(_SCAN_CACHE)}


def preview_groups(data_dir: Path | str, cfg: dict,
                   only_paths: Optional[Sequence[Path]] = None) -> list[str]:
    """**只解析文件名**返回组名集合（不读文件内容）。

    用途：建 run 目录前就要知道"这批数据属于哪个染料"，而这一步发生在
    读文件之前（FR-11.3）。代价仅一次目录遍历。
    """
    data_dir = Path(data_dir)
    io_cfg, fn_cfg = cfg["io"], cfg["filename"]
    exts = scan_extensions(io_cfg)
    excl = tuple(io_cfg.get("exclude", ["_"]))
    if not data_dir.is_dir():
        return []
    it = _iter_files(data_dir, io_cfg)          # G6：不走 junction（见 _iter_files）
    keep = None
    if only_paths is not None:
        keep = {str(Path(p).resolve()).lower() for p in only_paths}
    out: set[str] = set()
    for p in it:
        if not (p.is_file() and p.suffix.lower() in exts
                and not p.name.startswith(excl)):
            continue
        if keep is not None and str(p.resolve()).lower() not in keep:
            continue
        try:
            out.add(str(parse_filename(p.stem, fn_cfg)["group"]))
        except Exception:                              # noqa: BLE001
            continue
    return sorted(out)


# ═══════════════ 时间整定（FR-12：Origin 式工作表的核心层） ═══════════════
def apply_time_overrides(scans: Sequence[Scan],
                         overrides: Optional[dict] = None) -> list[Scan]:
    """把 ``{文件名: t}`` 覆盖到 ``scan.t``（就地修改并返回同一列表）。

    * 键可用**文件名**（``sample-30.txt``）或**绝对路径字符串**，两种都认；
    * 只改 ``t``，``t_src`` 保留文件名解析原值 → 工作表能并排显示核对；
    * 覆盖表里出现但数据里没有的文件名 → 记 WARNING（不报错）。
    """
    if not overrides:
        return list(scans)
    by_name = {Path(p).name.lower(): float(v) for p, v in overrides.items()}
    by_path = {str(Path(p)).lower(): float(v) for p, v in overrides.items()}
    hit: set[str] = set()
    out: list[Scan] = []
    for s in scans:
        if s.t_src is None:
            s.t_src = float(s.t)
        key_n = s.path.name.lower()
        key_p = str(s.path).lower()
        val = by_name.get(key_n)
        if val is None:
            val = by_path.get(key_p)
        if val is not None:
            s.t = float(val)
            hit.add(key_n)
        out.append(s)
    missing = set(by_name) - hit
    if missing:
        log.warning("时间整定表里有 %d 个文件名未在当前数据中匹配上: %s",
                    len(missing), ", ".join(sorted(missing)[:5]))
    n_edit = sum(1 for s in out if s.t_edited)
    if n_edit:
        log.info("已应用时间整定：%d 个时间点被手动修改（FR-12）", n_edit)
    return out


def validate_times(scans: Sequence[Scan]) -> list:
    """组内时间合法性检查（FR-12.3）：只产生 WARNING，**不阻塞**。

    Returns:
        :class:`core.plotstyle.ConfigIssue` 列表；重复 t / 非单调 / 单点。
    """
    from ..render.plotstyle import ConfigIssue
    issues: list[ConfigIssue] = []
    by_group: dict[str, list[Scan]] = {}
    for s in scans:
        if getattr(s, "excluded", ""):
            continue
        if s.role in ("background", "dark"):
            continue
        by_group.setdefault(s.group, []).append(s)
    for g, ss in sorted(by_group.items()):
        ts = [round(float(s.t), 6) for s in ss]
        if len(ts) < 2:
            issues.append(ConfigIssue(
                "WARNING", f"time.{g}",
                f"组「{g}」只有 {len(ts)} 个有效时间点，无法做动力学拟合",
                "请确认该组文件是否齐全，或用时间工作表修正时间"))
            continue
        # M2（V2.0 F5 未闭环 + V2.x 审查）：多数据集 `.spc` 的**同 t 不同 rep**
        # 是合法数据（一个存档里多个数据集），不是"命名重复"。只有**同一 rep 内**
        # 出现同一 t 才算重复（与 `pipeline._fit_all` 的 per-rep 检查同口径）。
        _by_t: dict = {}
        for s in ss:
            _by_t.setdefault(round(float(s.t), 6), []).append(str(s.rep or ""))
        dup = sorted(t for t, reps in _by_t.items()
                     if len(reps) > len(set(reps)))
        if dup:
            issues.append(ConfigIssue(
                "WARNING", f"time.{g}",
                f"组「{g}」存在重复时间点 {dup}（这些点会全部参与拟合）",
                "若为重复测量可保留；若是命名重复请用时间工作表区分"))
        if ts != sorted(ts):
            issues.append(ConfigIssue(
                "WARNING", f"time.{g}",
                f"组「{g}」的时间非单调（拟合会按时间排序后处理）",
                "检查是否有文件时间记错，可用时间工作表批量平移"))
    return issues


# ═══════════════════════════ 兜底光谱读取（FR-2/FR-4 复用） ═══════════════════════════
def read_spectrum_file(path: Path | str,
                       wl_range: Sequence[float] = (150.0, 1200.0)
                       ) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """兜底读取：用数字正则从原始文件取 (波长, 强度) 两列。

    对 legacy ``_read_spectrum_file`` 的增强：把波长范围提为参数，并按
    ``[lo, hi]`` 过滤（避免 荧光 头部 ``Serial Number`` 等数字混入）。
    """
    txt = None
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            txt = Path(path).read_text(encoding=enc)
            break
        except Exception:                              # noqa: BLE001
            continue
    if txt is None:
        return None
    lo, hi = float(wl_range[0]), float(wl_range[1])
    wl: list[float] = []
    ab: list[float] = []
    for line in txt.splitlines():
        toks = _NUM_RE.findall(line.replace(",", " ").replace(";", " ")
                               .replace("\t", " "))
        if len(toks) >= 2:
            x, y = float(toks[0]), float(toks[1])
            if lo <= x <= hi:
                wl.append(x)
                ab.append(y)
    if len(wl) >= 10:
        i = np.argsort(np.asarray(wl))
        return np.asarray(wl)[i], np.asarray(ab)[i]
    return None


def spectrum_arrays(scan: Scan,
                    wl_range: Sequence[float] = (150.0, 1200.0)
                    ) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """取 ``scan`` 的 (wl, a)；优先已解析属性，兜底回读原始文件。

    兼容 legacy ``_spectrum_arrays`` 的多种属性名，以便 compare 流程对**任意
    Scan 形状**都成立。

    ⚠ 哪些分支真的会走到（评审记录「重复逻辑 #7」点名过这点，这里写清楚免得
    后人以为五对都在用）：本项目的 ``Scan`` 数据类**只有** ``wl`` / ``a`` 两个
    属性，所以对 ``Scan`` 而言**只有最后一对 ``("wl", "a")`` 命中**；前三对
    （``abs`` / ``wavelength`` / ``x`` / ``y`` / ``absorbance``）只在有人传入
    **自称 duck-type 的裸对象**时才可能生效——全项目只有本函数这一个入口，
    而 `grouping` / `plotting` / `plotting_jacs` 传进来的都是 ``Scan``。

    **为什么不删掉那几对**：它是 legacy 的对外兼容契约（换了老对象形状仍能用），
    删掉等于单方面收窄接口，而收益只是省 3 次 ``getattr``（纳秒级）。
    所以选择"保留 + 标注"，不做隐性行为变更。
    """
    for wa, aa in (("wl", "abs"), ("wavelength", "abs"), ("x", "y"),
                   ("wl", "a"), ("wavelength", "absorbance")):
        w, a = getattr(scan, wa, None), getattr(scan, aa, None)
        if w is None or a is None:
            continue
        try:
            w, a = np.asarray(w, float), np.asarray(a, float)
            if w.size >= 10 and w.size == a.size:
                return w, a
        except Exception:                              # noqa: BLE001
            pass
    return read_spectrum_file(getattr(scan, "path", scan), wl_range)


# ═══════════════════════════ 荧光发射谱解析（FR-4，新增） ═══════════════════════════
def _short_label(raw, fallback: str) -> str:
    """仪器元信息里的样品名 → 显示用短标签。

    L9（评审记录）：FDS 的 ``sample`` 字段**可以含括号**（如 ``A (1)``、``A (2)``）。
    原先无条件 ``split("(")[0]`` 会把它们**截成同一个 label**（都变 ``A``），
    于是"多支谱、一个名字"、图注也分不清。改为：只有截断结果**非空**才采用——
    既保住"去仪器后缀"的原本意图，又不会把不同样品合并成同名。
    """
    txt = str(raw or "").strip()
    if not txt:
        return fallback
    head = txt.split("(")[0].strip()
    return head if head else txt


def read_emission_file(path: Path | str, io_cfg: Optional[dict] = None,
                       text: Optional[str] = None
                       ) -> Optional[tuple[np.ndarray, np.ndarray, dict]]:
    """解析荧光发射谱（仪器 荧光 导出的 ``.TXT``）。

    格式：头部 ``Kesample-data 若干行，随后 ``Data Points`` 段以
    ``nm\\tData`` 为表头，之后每行 ``波长\\t强度``。

    :param text: 已读出的文件文本。调用方（如 :func:`load_spectra_files`）
        为了判别格式**已经读过一遍**，传进来可免掉第二次读盘（T2）。
        为 ``None`` 时自行 :func:`read_text_robust`。

    返回 ``(wl, y, meta)``；解析失败返回 ``None``。
    若找不到 ``Data Points`` 标记，回退到 :func:`read_spectrum_file`。
    """
    io_cfg = io_cfg or {}
    marker = str(io_cfg.get("emission_marker", "Data Points"))
    wl_range = io_cfg.get("emission_wl_range", [150.0, 1200.0])
    path = Path(path)
    txt = read_text_robust(path) if text is None else text
    lines = txt.splitlines()
    meta: dict = {}
    start = None
    for i, line in enumerate(lines):
        if line.strip() == marker:                     # "Data Points" 段起
            start = i
        elif start is None and ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'")
    if start is None:
        got = read_spectrum_file(path, wl_range)
        if got is None:
            return None
        return got[0], got[1], meta
    wl_l: list[float] = []
    y_l: list[float] = []
    for line in lines[start + 1:]:
        parts = line.split("\t")
        if len(parts) < 2:
            parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        try:
            w, v = float(parts[0]), float(parts[1])
        except ValueError:
            continue                                   # "nm\tData" 表头
        wl_l.append(w)
        y_l.append(v)
    if len(wl_l) < 10:
        got = read_spectrum_file(path, wl_range)
        if got is None:
            return None
        return got[0], got[1], meta
    wl = np.asarray(wl_l, float)
    y = np.asarray(y_l, float)
    if np.any(np.diff(wl) <= 0):
        o = np.argsort(wl, kind="stable")
        wl, y = wl[o], y[o]
    return wl, y, meta


def load_spectra_files(paths: Iterable[Path | str], cfg: dict) -> list[Spectrum]:
    """设计文档 §6 接口：批量载入独立光谱（FR-4）。

    自动判别吸收/发射：命中 ``Data Points`` 标记或头部含 ``Scan mode: Emission``
    判为 ``em``；两列逗号且落在 300–700 nm 判为 ``abs``。任何单文件失败
    都转成 ``Spectrum.excluded``，绝不向 UI 抛异常。

    V1.3 FR-13：``.FDS`` 二进制存档**优先按内容嗅探**（``IIHIDTAG`` 魔术字）识别，
    走 :mod:`core.io_fds`；实测 196/196 全部是荧光发射谱（荧光 是荧光仪），
    故判为 ``em``。FDS 内**没有**独立的测量类型字段（见 ``docs/fds_format_spec.md``
    §7.3），所以"吸收型 .fds"目前无法自动识别——等拿到吸收样例再按同一适配器模式增补。
    """
    io_cfg = cfg.get("io", {})
    fds_enabled = bool((io_cfg.get("fds") or {}).get("enable", True))
    spc_enabled = bool((io_cfg.get("spc") or {}).get("enable", True))
    out: list[Spectrum] = []
    for p in paths:
        path = Path(p)
        sp = Spectrum(path=path, wl=np.array([]), y=np.array([]),
                      label=path.stem, kind="unknown")
        try:
            is_fds = False
            if fds_enabled:
                from .io_fds import is_fds_file
                # 复用 io_fds 的实现（P2），并传用户配置的锚点（M1）
                is_fds = is_fds_file(path)
            # A.M8（V2.x 审查）：光谱页的选择器用 scan_extensions()（已含 .spc），
            # 但这里原来只嗅 FDS → 选中的 .spc 会被当文本按 utf-16 读，必被剔除。
            # .spc 是 SPC 的**吸光度**存档 → kind="abs"，取第 1 个数据集
            # （光谱页按"一支谱"展示；多数据集请走分析页）。
            is_spc = False
            if spc_enabled and not is_fds:
                from .io_spc import is_spc_file
                is_spc = is_spc_file(path)
            if is_spc:
                from .io_spc import read_spc_file_ex
                got_s, why_s = read_spc_file_ex(path)
                if got_s is None:
                    raise ValueError(why_s or "SPC 存档解析失败（结构不完整）")
                datasets = got_s if isinstance(got_s, list) else [got_s]
                if not datasets:
                    raise ValueError("SPC 存档里没有数据集")
                first = datasets[0]
                w, y = first["wl"], first["a"]
                sp.wl, sp.y = np.asarray(w, float), np.asarray(y, float)
                sp.meta = {"unit": "AU", "n_datasets": len(datasets),
                           **{k: v for k, v in (first.get("meta") or {}).items()}}
                sp.kind = "abs"
                out.append(sp)
                continue
            if is_fds:
                from .io_fds import read_fds_file_ex
                # P4/T2：一次读盘同时拿到数据与中文原因，不再"三读"
                got, why = read_fds_file_ex(path, fds_anchor_of(io_cfg))
                if got is None:
                    raise ValueError(why or "FDS 存档解析失败（结构不完整）")
                w, y, meta = got
                sp.wl, sp.y, sp.meta = np.asarray(w, float), np.asarray(y, float), meta
                # 入口归一：±inf/NaN 一律转 NaN（S1 判据）
                bad = ~np.isfinite(sp.y)
                if bad.any():
                    log.warning("%s: %d 个非有限强度值已归一为 NaN",
                                path.name, int(bad.sum()))
                    sp.y = np.where(bad, np.nan, sp.y)
                sp.kind = "em"
                sp.meta.setdefault("unit", "a.u.")
                sp.label = _short_label(meta.get("sample"), path.stem)
                out.append(sp)
                continue
            raw = read_text_robust(path)
            is_em = (str(io_cfg.get("emission_marker", "Data Points")) in raw
                     or "Emission" in raw)
            if is_em:
                got = read_emission_file(path, io_cfg, text=raw)
            else:
                got = None
                try:
                    w, a = read_scan(path, io_cfg)
                    got = (w, a, {})
                except (ValueError, OSError):
                    w2 = read_spectrum_file(path, [150.0, 1200.0])
                    got = None if w2 is None else (w2[0], w2[1], {})
            if got is None:
                raise ValueError("无法解析为光谱（两列/荧光格式均不匹配）")
            w, y, meta = got
            sp.wl, sp.y, sp.meta = np.asarray(w, float), np.asarray(y, float), meta
            sp.kind = "em" if is_em else "abs"
            # 荧光谱常以仪器原始量纲（非吸光度）；此处仅作展示，不做量纲换算
            if sp.kind == "abs":
                sp.meta.setdefault("unit", "AU")
            else:
                sp.meta.setdefault("unit", "a.u.")
                sp.label = _short_label(meta.get("Sample") or meta.get("File name"),
                                        path.stem)
        except Exception as err:                       # noqa: BLE001 —— 顶层不得抛
            sp.excluded = f"读取失败: {err}"
            log.warning("剔除 %s: %s", path.name, sp.excluded)
        out.append(sp)
    log.info("FR-4: 载入 %d 支光谱（%d 支可用）", len(out),
             sum(1 for s in out if s.ok))
    return out
