# -*- coding: utf-8 -*-
"""Vendor binary ``.FDS`` header reader (numeric data only; metadata is not exposed).

为什么单独一个模块
------------------
``core/io.py`` 已经承载了两种文本格式的解析与扫描缓存，再塞进一个**二进制**
格式会让那份文件里"文本行切分"的心智模型失效（二进制没有"行"）。这里保持
"一个格式一个适配器"的形态，由 ``io.py`` 的内容嗅探按需调用。

格式要点（完整逆向见 ``docs/fds_format_spec.md``，实测 196 个真实样例）
--------------------------------------------------------------------
* 文件头 8 字节魔术字 ``b"IIHIDTAG"``（196/196 位于 off 0）；
* 头部是变长 NUL 结尾字符串区（只用于定位数据段，不向调用方暴露元信息），
  **不能按固定偏移硬编码**——用序列号串 ``0000-000\\0`` 作锚点算出数值区起点 ``d0``；
* ``d0`` 起两个小端 float64：采样间隔（nm）、起始波长（nm）；
* 紧接 ``d0+16`` 是强度数组：**小端 float64，无缩放、无整型化**；
* 尾部参数区（8 个 float64 = 64 B）紧邻名称块 ``"Reagent 1\\0"`` 之前——
  它是定位数据段长度的锚点；波长列是**隐式**的：``wl[i] = em_start + step * i``；
* 点数有**两条独立算路**（尾部 EM 起止波长反算 / 数据段实有槽位数），
  两者必须相等，否则判为文件损坏——这是最强的结构自检。

与既有格式的判别安全性
----------------------
``IIHIDTAG`` 只可能出现在 vendor metadata marker 的二进制存档里；两列逗号 CSV 与
``Data Points`` 制表符文本的首字节分别是数字/字母。实测在 181 个配套 TXT
与仓库全部文本样例中搜索该魔术字为 **0 命中**，故判错风险为 0。

设计约束（设计文档 §3 / §5）
--------------------------
* **不引入任何第三方依赖**（只用 ``struct``/``bytes``/``numpy``）；
* **不 import matplotlib / PySide6**（core 层无 GUI 依赖）；
* 解析失败一律抛 :class:`FdsError`（``ValueError`` 子类，消息为中文人话），
  由调用方转成"剔除原因"——**本模块不吞异常，也不打印**；
* 失败结果**不得进扫描缓存**（S3 修复确立的原则，由调用方保证）。
"""
from __future__ import annotations

import logging
import struct
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("dye_tool.io.fds")

#: FDS 存档的魔术字（196/196 位于 off 0）
FDS_MAGIC = b"IIHIDTAG"

#: 数据段起点的定位锚点。
#: 头部字符串长度因文件而异，因此锚点可配置（见 :func:`read_fds_bytes` 的
#: ``serial_anchor``），并在失败时回退到"扫描头部找 Model 串"的宽松模式。
DEFAULT_SERIAL_ANCHOR = b"0000-000\x00"

#: 尾部参数区的后置锚点（名称块）
TRAILER_ANCHOR = b"Reagent 1\x00"

#: 尾部参数区长度（8 个小端 float64）
TRAILER_BYTES = 64

#: 头部 ``off=12`` 的 u32 与 ``filesize`` 之差（实测 196/196 成立的结构自检）
TRAILER_SELF_PTR_GAP = 62

#: 不硬编码任何型号串；只用可配置字节锚点定位数据段。
_MODEL_MARKERS = ()

#: 数值合理区间（步长 nm / 起始波长 nm）。区间取得比实测宽，
#: 只用来拦截"明显不是本格式"的垃圾，不做业务判断。
_STEP_LO, _STEP_HI = 1e-3, 20.0
_WL_LO, _WL_HI = 100.0, 1100.0


class FdsError(ValueError):
    """FDS 解析失败。消息为**中文人话**，可直接作为文件表的"剔除原因"。"""


# ═══════════════════════════ 基础工具 ═══════════════════════════
def looks_like_fds(data: bytes) -> bool:
    """内容嗅探：是否像 FDS 存档（判别的**唯一**依据，不看扩展名）。"""
    return len(data) >= 8 and data[:8] == FDS_MAGIC


def _clean_ascii(raw: bytes) -> str:
    """把头部字节串清洗成可显示的文本。

    实测：头部字符串区里可能夹着**非 ASCII 的二进制前缀**（例如魔术字之后
    紧跟的 4 字节高熵值），直接 ``decode`` 会得到 ``'\\x11\\x16'`` 这种
    控制字符"文本"，被当成样品名显示到文件表里就非常费解。这里把控制字符
    整段丢弃，只留下可打印部分。
    """
    out = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in
                  raw.decode("ascii", "replace"))
    return " ".join(out.split()).strip()


def _read_header_strings(data: bytes) -> dict:
    """Return no header metadata: personal/instrument fields are not exposed."""
    return {}


def _read_instrument_strings(data: bytes, d0: int) -> dict:
    """Return no instrument metadata: model/serial/operator fields are not exposed."""
    return {}


def _find_d0(buf: bytes, serial_anchor: bytes) -> int:
    """算出数值参数区起点 ``d0``。

    首选"序列号锚点 + 终止符之后"（196/196 实测稳定）；找不到序列号时回退到
    "型号串之后再跳 20 字节"（ROM 11 + 序列号 9），以便换仪器/换 ROM 后仍可用。
    两条都不成立才报错——报错文案要说清"是头部不认识"，而不是笼统的"解析失败"。
    """
    i = buf.find(serial_anchor, 0, 320)
    if i >= 0:
        return i + len(serial_anchor)
    for marker in _MODEL_MARKERS:
        j = buf.find(marker, 0, 320)
        if j >= 0:
            # 型号串之后固定是 ROM(11B) + 序列号(9B)；锚点失效时按此推算
            cand = j + len(marker) + 20
            if cand + 16 <= len(buf):
                return cand
    raise FdsError('FDS 头部不完整：找不到序列号/型号标记（可能被截断或非本机格式）')


# ═══════════════════════════ 主解析 ═══════════════════════════
def read_fds_bytes(data: bytes,
                   serial_anchor: bytes = DEFAULT_SERIAL_ANCHOR) -> dict:
    """把 FDS 字节串解析成 ``{"wavelength_nm", "intensity", "meta"}``。

    Raises:
        FdsError: 任何结构不自洽/截断/非本格式的情况，消息为中文人话。
    """
    n_bytes = len(data)
    # ── ① 先便宜后昂贵的前置检查（顺序即 §10 的防御式清单）──
    if n_bytes == 0:
        raise FdsError("空文件（0 字节），不是 FDS 存档")
    if n_bytes < 8:
        raise FdsError(f"文件过短（{n_bytes} 字节），不足一个 FDS 存档")
    if not looks_like_fds(data):
        raise FdsError(f'不是 FDS 存档：文件头应为 "IIHIDTAG"，'
                       f'实际是 {data[:8]!r}')
    if n_bytes < 220:
        raise FdsError(f"文件过短（{n_bytes} 字节），FDS 头部尚未完整")

    # ── ② 定位数值参数区 ──
    d0 = _find_d0(data, serial_anchor)
    if d0 + 16 > n_bytes:
        raise FdsError("FDS 文件被截断：数值参数区不完整")

    step = float(struct.unpack_from("<d", data, d0)[0])         # 采样间隔 nm
    start = float(struct.unpack_from("<d", data, d0 + 8)[0])    # 起始波长 nm
    if not (_STEP_LO <= step <= _STEP_HI):
        raise FdsError(f"FDS 参数异常：采样间隔为 {step}，"
                       f"疑似文件损坏或非荧光波长扫描存档")
    if not (_WL_LO <= start <= _WL_HI):
        raise FdsError(f"FDS 参数异常：起始波长为 {start} nm，超出常见仪器范围")

    # ── ③ 定位尾部参数区 / 求点数（两条算路必须互证）──
    data_off = d0 + 16
    r1 = data.find(TRAILER_ANCHOR, data_off)
    if r1 < 0:
        raise FdsError('FDS 文件被截断：找不到尾部标记 "Reagent 1"，'
                       '无法确定数据段长度')
    trailer = r1 - TRAILER_BYTES
    if trailer <= data_off:
        raise FdsError("FDS 结构异常：数据段长度为负（尾部标记位置不合理）")
    tv = struct.unpack_from("<8d", data, trailer)

    n_slot = (trailer - data_off) // 8             # 算路 A：数据段实有槽位
    if not np.isfinite(tv[2]) or not np.isfinite(tv[3]):
        raise FdsError("FDS 结构异常：尾部 EM 波长参数不是有限数")
    n_b = int(round((tv[3] - tv[2]) / step)) + 1   # 算路 B：EM 起止波长反算
    if n_b != n_slot:
        raise FdsError(f"FDS 结构自检失败：尾部参数算得 {n_b} 点，"
                       f"数据段实有 {n_slot} 点，文件可能损坏")
    if n_b < 10:
        raise FdsError(f"FDS 有效数据点不足（{n_b} 点）")
    # 算路 C（L4）：起始波长有**两个独立来源**——数值参数区 d0+8 与尾部 tv[2]。
    # 实测 3 个夹具恒等；波长列是用 tv[2] 生成的，若二者不一致，说明我们对
    # "哪一段才是数据起点"的理解有偏差 → 宁可报错也不要悄悄给出错位的波长轴。
    # 容差取 1e-6 nm（远小于任何真实步长，又容得下 float 往返误差）。
    if abs(start - float(tv[2])) > 1e-6:
        raise FdsError(f"FDS 结构自检失败：数值区起始波长 {start:g} nm 与"
                       f"尾部参数 {float(tv[2]):g} nm 不一致，文件可能损坏")

    # ── ④ 读强度 + 隐式生成波长列 ──
    inten = np.frombuffer(data, dtype="<f8", count=n_b, offset=data_off)
    # frombuffer 给的是只读视图，下游会就地改（归一化/基线），必须拷一份
    inten = np.array(inten, dtype=float, copy=True)
    wave = tv[2] + step * np.arange(n_b, dtype=float)

    # ── ⑤ 元信息（尽力而为；失败不影响数据可用性）──
    meta: dict = {
        "format": "fds",
        "step_nm": step,
        "em_start_nm": float(tv[2]),
        "em_end_nm": float(tv[3]),
        "ex_wl_nm": float(tv[4]),
        "n_points": n_b,
        "unit": "a.u.",
    }
    # 尾部完整性：数据段**不需要**尾部标记，但它的缺失说明"文件可能没拷完"。
    # 按规格书 §10 的建议给一条提示，**不**因此拒绝数据（那样会把好数据挡在门外）。
    if data.rfind(b"vendor metadata marker\x00") < 0:
        meta["tail_incomplete"] = True
        log.warning("FDS 尾部不完整：缺少 'vendor metadata marker' 标记（文件可能未完整拷贝）；"
                    "数据段已完整读出 %d 点，仅归档/GUI 登记信息缺失", n_b)
    # 额外的结构自检（实测 196/196）：头部 off=12 的 u32 是"尾部表自指针"，
    # 恒等于 filesize-62。它**不参与**定位，但不等就说明尺寸/尾部与常见形态不同，
    # 值得记一笔（同样不致命——别的软件版本可能没有这个字段）。
    if n_bytes >= 16:
        self_ptr = struct.unpack_from("<I", data, 12)[0]
        if self_ptr != n_bytes - TRAILER_SELF_PTR_GAP:
            meta["self_ptr_mismatch"] = self_ptr
            log.debug("FDS 头部自指针 %d != filesize-%d（%d）；不影响数据解析",
                      self_ptr, TRAILER_SELF_PTR_GAP, n_bytes)
    # D3（评审记录）：这里原有一个 `try/except FdsError: pass`，但被包住的两个函数
    # 只做 bytes.find / 切片 / ASCII 清洗，**全无 raise**——那个 except 永远进不去，
    # 是"看起来有兜底、其实没有"的虚假安全感。已删。
    # 元信息区的健壮性靠函数自身保证：找不到锚点就返回空 dict（见各自 docstring）。
    meta.update(_read_header_strings(data))
    meta.update(_read_instrument_strings(data, d0))

    # 采集时间/辐照时间若存在 → 供 Scan.t 映射（FR-12 双轨制）
    hms = _parse_run_time(str(meta.get("run_date", "")))
    if hms is not None:
        meta["run_seconds"] = hms

    return {"wavelength_nm": wave, "intensity": inten, "meta": meta}


def _parse_run_time(text: str) -> Optional[float]:
    """``"HH:MM:SS, MM/DD/YYYY"`` → 当日秒数（解析不出返回 None）。

    **注意**：这不是"辐照时间"。仪器记的是**墙上时钟的采集时刻**，
    与"光照了多久"没有直接关系，所以只作为元信息保留，
    **不**用它去覆盖 ``Scan.t``（那会把时间轴彻底搞错）。
    """
    t = str(text).strip().lstrip("'")
    if not t or ":" not in t:
        return None
    clock = t.split(",")[0].strip()
    parts = clock.split(":")
    if len(parts) != 3:
        return None
    try:
        h, m, s = (int(float(x)) for x in parts)
    except (TypeError, ValueError):
        return None
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return float(h * 3600 + m * 60 + s)


# ═══════════════════════════ 对外适配器 ═══════════════════════════
def is_fds_file(path: Path | str) -> bool:
    """只读文件头判断是否 FDS 存档（**不看扩展名**）。

    读盘失败一律返回 ``False``（本函数用于嗅探，不该因坏文件抛异常）。
    """
    try:
        with open(path, "rb") as f:
            return looks_like_fds(f.read(8))
    except OSError:
        return False


def read_fds_file_ex(path: Path | str,
                     serial_anchor: bytes = DEFAULT_SERIAL_ANCHOR
                     ) -> tuple[Optional[tuple[np.ndarray, np.ndarray, dict]], str]:
    """读一个 ``.fds`` 文件 → ``((wl, y, meta) | None, 中文失败原因)``。

    **一次读盘拿到数据与原因**（评审记录 P4/T2）。原先
    :func:`read_fds_file` 把 :class:`FdsError` 吞成 ``None``，调用方为了给
    "剔除原因"列一句中文，只能再调 :func:`describe_failure` —— 那是**第三次**
    读盘 + 完整重解析（8 B 嗅探 → 全文 → 原因）。异常里本来就有原因。

    :param serial_anchor: 数据段定位锚点（空串 = 用内置默认值）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        return None, f"读取失败: {err}"
    try:
        got = read_fds_bytes(data, serial_anchor or DEFAULT_SERIAL_ANCHOR)
    except FdsError as err:
        return None, str(err)
    return (got["wavelength_nm"], got["intensity"], got["meta"]), ""


def read_fds_file(path: Path | str,
                  serial_anchor: bytes = DEFAULT_SERIAL_ANCHOR
                  ) -> Optional[tuple[np.ndarray, np.ndarray, dict]]:
    """读一个 ``.fds`` 文件 → ``(wl, y, meta)``；**任何失败返回 ``None``**。

    签名与 :func:`core.io.read_emission_file` **同构**，便于在内容嗅探链里
    与既有格式并列。与那个函数一样，失败返回 ``None`` 而**不抛异常**——嗅探
    链要能安静地"这个不是我的格式，试下一个"。

    需要**失败原因**（中文人话，用于文件表"剔除原因"列）时用
    :func:`read_fds_file_ex`（顺带少读两遍盘），或 :func:`read_fds_bytes`（抛异常）。
    """
    got, _why = read_fds_file_ex(path, serial_anchor)
    return got


def describe_failure(path: Path | str) -> str:
    """解析失败的**中文原因**（成功返回空串）。

    给"剔除原因"列用：先嗅探确认它确实是 FDS（魔术字对），再解析取原因。
    非 FDS 文件返回空串，调用方据此走别的分支。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        return f"读取失败: {err}"
    if not looks_like_fds(data):
        return ""
    try:
        read_fds_bytes(data)
    except FdsError as err:
        return str(err)
    return ""
