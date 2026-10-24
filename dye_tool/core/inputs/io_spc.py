# -*- coding: utf-8 -*-
"""SPC ``.spc`` 原生存档的读取适配器（V2.0 β 批 / FR-17）。

为什么单独一个模块
------------------
与 :mod:`core.io_fds` 同理：``core/io.py`` 承载的是**文本**格式的解析与扫描缓存，
而 ``.spc`` 是 OLE2 复合文档（没有"行"的概念）。保持"一个格式一个适配器"，
由 ``io.py`` 的内容嗅探按需调用。

格式要点（实测 38 个真实样例，2026-10-17；完整结论见 ``docs/spc_format_spec.md``）
--------------------------------------------------------------------------------
* 文件头 8 字节魔术字 ``D0 CF 11 E0 A1 B1 1A E1``（OLE2 复合文档）；
* **流在深层存储里**，不是根目录：:

      DataStorage1/DataSetGroup/DataSet1/DataSpectrumStorage/Data/X Data.1
      DataStorage1/DataSetGroup/DataSet1/DataSpectrumStorage/Data/Y Data.1
      DataStorage1/DataSetGroup/DataSet1/DataSpectrumStorage/DataHeader/Header Info
      DataStorage1/DataSetGroup/DataSet1/DataHistoryStorage/DataSetHistory
      NumberofSaved                                        （根目录，u32 数据集个数）

  ⚠ **设计文档 V1.3 附录 B.2 把流写成根目录平铺（``X Data.1``）——实测不成立**，
  所以本模块按**后缀**定位流（``endswith("X Data.1")``），既兼容平铺也兼容深层；
* ``X Data.N`` / ``Y Data.N`` 是**小端 float64**，X 为**降序**（700 → 300 nm），
  **解析后翻转为升序**（设计文档 Q3）；
* ``Header Info`` 的**前三个 LE float64 依次是 起始波长 / 终止波长 / 步长**
  （实测 offset 0/8/16——设计文档写的 4/12/20 是错的，那三处读出来是 denormal 垃圾）；
* 文件名（含时间语义）一律走 ``io.parse_filename``；``.spc`` 内部无辐照时间字段。

与既有格式的判别安全性
----------------------
OLE2 魔术字 + 目录里存在 ``X Data`` 流，是"这是 SPC 存档"的**内容**特征；
两列 CSV / 仪器 FL 文本 / FDS 二进制的头部都不可能是 ``D0 CF 11 E0``，
故交叉误判风险为 0（``tests/test_batch_beta.py`` 的四类判别矩阵钉住）。

设计约束（设计文档 §3 / §5）
-------------------------
* 解析失败一律抛 :class:`SpcError`（``ValueError`` 子类，**中文人话**），
  由调用方转成"剔除原因"——**本模块不吞异常、不打印**；
* 依赖 ``olefile``（纯 Python，PyInstaller 友好）。**缺失时降级**：抛带安装
  指引的 :class:`SpcError`，绝不让整个程序 import 期就崩；
* 不 import matplotlib / PySide6。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger("dye_tool.io.spc")

#: OLE2 复合文档魔术字（8 字节）
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: 数据集流名（``N`` 为数据集序号，从 1 开始）
_DATA_STREAM_RE = re.compile(r"^[XY] Data\.(\d+)$")

#: 数据点数下限（与文本路径一致：有效点不足 10 视为不可用）
MIN_POINTS = 10


class SpcError(ValueError):
    """``.spc`` 解析失败。消息为**中文人话**，可直接作为文件表"剔除原因"。"""


# ═══════════════════════════ 内容嗅探 ═══════════════════════════
def looks_like_ole2(data: bytes) -> bool:
    """是否是 OLE2 复合文档（判别的**第一层**依据，不看扩展名）。"""
    return len(data) >= 8 and data[:8] == OLE2_MAGIC


def looks_like_spc(data: bytes) -> bool:
    """是否像 SPC ``.spc`` 存档。

    判据（设计文档 FR-13.3）：**OLE2 魔术字 + 目录里存在 ``X Data`` 流**。
    只看魔术字不够——OLE2 也被 Word/Excel 用，必须再要求"有 X Data 流"。
    """
    if not looks_like_ole2(data):
        return False
    try:
        ole = _open(data)
    except SpcError as err:
        # A.M7（V2.x 审查）：这里原来统一 `return False`，把**缺 olefile 依赖**
        # 也吞成"不是 spc"——于是安装提示永远不可见（用户只会看到"文件被剔除"）。
        # 依赖缺失是**环境错误**，必须往上抛；内容损坏才是"不是 spc"。
        if "olefile" in str(err):
            raise
        return False
    try:
        return bool(_streams(ole))
    finally:
        _close(ole)


def _import_olefile():
    """延迟 import ``olefile``；缺失时给**可操作**的中文提示。"""
    try:
        import olefile
    except ImportError as err:                          # pragma: no cover
        raise SpcError(
            "缺少 olefile 依赖，无法读取 .spc 文件"
            "（安装：pip install olefile）") from err
    return olefile


def _open(data: bytes):
    """把内存里的字节交给 olefile（避免二次读盘）。"""
    import io as _io
    olefile = _import_olefile()
    try:
        return olefile.OleFileIO(_io.BytesIO(data))
    except Exception as err:                            # noqa: BLE001
        raise SpcError(f"不是有效的 OLE2 复合文档：{type(err).__name__}: {err}") from err


def _close(ole) -> None:
    try:
        ole.close()
    except Exception:                                   # noqa: BLE001
        pass


def _streams(ole) -> list[str]:
    """全部流的点分路径（``olefile.listdir`` 返回"路径分量列表"的列表）。"""
    try:
        raw = ole.listdir(streams=True, storages=False)
    except Exception as err:                            # noqa: BLE001
        raise SpcError(f"OLE2 目录读取失败：{type(err).__name__}: {err}") from err
    return ["/".join(parts) for parts in raw]


def _find_stream(names: list[str], suffix: str) -> str:
    """按**后缀**定位流（兼容深层存储与设计文档假定的根目录平铺两种形态）。"""
    hits = [n for n in names if n == suffix or n.endswith("/" + suffix)]
    return hits[0] if hits else ""


def _data_streams(names: list[str]) -> dict[int, dict]:
    """``{数据集序号: {"x": 流路径, "y": 流路径}}``（按序号升序）。"""
    out: dict[int, dict] = {}
    for n in names:
        m = _DATA_STREAM_RE.search(n.rsplit("/", 1)[-1])
        if not m:
            continue
        idx = int(m.group(1))
        slot = out.setdefault(idx, {})
        slot["x" if n.rsplit("/", 1)[-1].startswith("X") else "y"] = n
    return dict(sorted(out.items()))


# ═══════════════════════════ 小工具 ═══════════════════════════
def _read_f8(ole, name: str) -> np.ndarray:
    """读一个流并按小端 float64 解成数组。"""
    try:
        raw = ole.openstream(name).read()
    except Exception as err:                            # noqa: BLE001
        raise SpcError(f"读取流 {name} 失败：{type(err).__name__}: {err}") from err
    if len(raw) % 8:
        raise SpcError(f"流 {name} 长度 {len(raw)} 不是 8 的整数倍（文件可能被截断）")
    return np.frombuffer(raw, dtype="<f8").astype(float)


def _read_u32(ole, name: str) -> Optional[int]:
    """读一个 4 字节小端 u32 流（缺失/异常返回 None，不抛）。"""
    try:
        raw = ole.openstream(name).read()
    except Exception:                                   # noqa: BLE001
        return None
    if len(raw) < 4:
        return None
    return int(np.frombuffer(raw[:4], dtype="<u4")[0])


def _read_text_stream(ole, name: str) -> str:
    """读一个"短字符串"流并清洗成可显示文本（失败返回空串）。

    实测形态：``\\x0eStorage 005339``（首字节是长度前缀），所以这里剥掉前导
    控制字符再解码；无法解码时退回 latin-1 而不抛。
    """
    if not name:
        return ""
    try:
        raw = ole.openstream(name).read()
    except Exception:                                   # noqa: BLE001
        return ""
    if not raw:
        return ""
    body = raw.lstrip(bytes(range(0x20)))               # 去掉长度前缀等控制字节
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            txt = body.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:                                               # pragma: no cover
        txt = body.decode("latin-1", "replace")
    return " ".join(txt.replace("\x00", " ").split()).strip()


def _header_triple(ole, names: list[str]) -> tuple[Optional[float], ...]:
    """``Header Info`` 的前三个 LE float64：起始波长 / 终止波长 / 步长。"""
    nm = _find_stream(names, "Header Info")
    if not nm:
        return (None, None, None)
    try:
        raw = ole.openstream(nm).read()
    except Exception:                                   # noqa: BLE001
        return (None, None, None)
    if len(raw) < 24:
        return (None, None, None)
    vals = np.frombuffer(raw[:24], dtype="<f8")
    return (float(vals[0]), float(vals[1]), float(vals[2]))


def _ascending(wl: np.ndarray, a: np.ndarray) -> tuple[np.ndarray, np.ndarray, bool]:
    """把 (波长, 吸光度) 统一成**升序**（设计文档 Q3）。

    返回 ``(wl, a, flipped)``。判据沿用文本路径的写法：``diff <= 0`` 即认为
    不是严格升序。这里**不丢弃 NaN**——设计文档附录 B.3 明确"t=0 文件 300–399 nm
    空段是有意设计，如实读 NaN、不补段不剔除"。
    """
    if wl.size < 2:
        return wl, a, False
    finite = np.isfinite(wl)
    if finite.sum() < 2:
        return wl, a, False
    diffs = np.diff(wl[finite])
    if np.all(diffs > 0):
        return wl, a, False
    if np.all(diffs < 0):
        return wl[::-1].copy(), a[::-1].copy(), True
    # 非单调：按波长稳定排序（与文本路径同策略），并记 WARNING 由调用方决定
    log.warning("波长列非单调（降序/升序都不是）→ 按波长升序重排")
    order = np.argsort(wl, kind="stable")
    return wl[order], a[order], False


def _fix_nonfinite(a: np.ndarray, tag: str) -> np.ndarray:
    """±inf/NaN 入口归一为 NaN（S1 判据，与 FDS 分支同一口径）。"""
    bad = ~np.isfinite(a)
    bad &= ~np.isnan(a)                                 # NaN 本来就算无效点
    if bad.any():
        log.warning("%s: %d 个非有限吸光度（±inf）已归一为 NaN", tag, int(bad.sum()))
        return np.where(bad, np.nan, a)
    return a


# ═══════════════════════════ 主解析 ═══════════════════════════
def read_spc_bytes(data: bytes) -> list[dict]:
    """解析 ``.spc`` 字节流 → **每个数据集一条**记录。

    Returns:
        ``[{"wavelength_nm": ndarray, "absorbance": ndarray, "meta": dict}, ...]``
        （按数据集序号升序；单数据集文件长度恒为 1）。

    Raises:
        SpcError: 任何解析失败（中文人话）。
    """
    if not looks_like_ole2(data):
        raise SpcError("不是 OLE2 复合文档（文件头不是 D0 CF 11 E0）")
    ole = _open(data)
    try:
        names = _streams(ole)
        streams = _data_streams(names)
        if not streams:
            raise SpcError("OLE2 里没有 X Data/Y Data 流（不是 SPC 的 .spc 存档）")
        n_saved = _read_u32(ole, "NumberofSaved")
        start, stop, step = _header_triple(ole, names)
        storage_name = _read_text_stream(ole, _find_stream(names, "DataStorageName"))
        out: list[dict] = []
        for idx, slot in streams.items():
            if "x" not in slot or "y" not in slot:
                raise SpcError(f"数据集 {idx} 只有 X 或只有 Y 流（文件不完整）")
            wl = _read_f8(ole, slot["x"])
            a = _read_f8(ole, slot["y"])
            if wl.size != a.size:
                raise SpcError(f"数据集 {idx} 的波长与吸光度点数不一致"
                               f"（{wl.size} vs {a.size}）")
            if wl.size < MIN_POINTS:
                raise SpcError(f"数据集 {idx} 有效点不足({wl.size} 点)")
            wl, a, flipped = _ascending(wl, a)
            a = _fix_nonfinite(a, f"{storage_name or '.spc'} 数据集{idx}")
            raw_name = _read_text_stream(ole, _find_stream(names, "DataSetHistory"))
            meta = {
                "format": "spc",
                "dataset_index": idx,
                "n_datasets": n_saved if n_saved is not None else len(streams),
                "storage_name": storage_name,
                "header_start_nm": start,
                "header_stop_nm": stop,
                "header_step_nm": step,
                "flipped": bool(flipped),
                "n_points": int(wl.size),
            }
            if raw_name:
                meta["history_head"] = raw_name[:120]
            # 结构自检（与 FDS 的"两条算路必须一致"同一思路）：
            # Header Info 给的 起始/步长 必须与实有波长列吻合，否则记 WARNING
            if start is not None and step and wl.size > 1:
                implied = abs(float(wl[-1] - wl[0])) / (wl.size - 1)
                if abs(implied - abs(float(step))) > 1e-6:
                    log.warning("%s: Header Info 步长 %s 与实有波长列（%.6g）不一致，"
                                "以波长列数据为准", storage_name or ".spc",
                                step, implied)
            out.append({"wavelength_nm": wl, "absorbance": a, "meta": meta})
        return out
    finally:
        _close(ole)


def read_spc_file_ex(path: Path | str
                     ) -> tuple[Optional[list[dict]], str]:
    """读一个 ``.spc`` 文件 → ``(记录列表 | None, 中文失败原因)``。

    一次读盘同时拿到数据与原因（照 ``io_fds.read_fds_file_ex`` 的形态）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        return None, f"读取失败: {err}"
    try:
        return read_spc_bytes(data), ""
    except SpcError as err:
        return None, str(err)


def read_spc_file(path: Path | str) -> Optional[tuple[np.ndarray, np.ndarray, dict]]:
    """读一个 ``.spc`` 文件 → ``(wl, a, meta)``；**任何失败返回 ``None``**。

    与 :func:`core.io.read_scan` / ``io_fds.read_fds_file`` 同构，便于在内容嗅探
    链里与既有格式并列。**多数据集文件只返回第一个数据集**——需要全部数据集时用
    :func:`read_spc_bytes`（``collect_scans`` 走的是后者）。
    """
    got, _why = read_spc_file_ex(path)
    if not got:
        return None
    rec = got[0]
    return rec["wavelength_nm"], rec["absorbance"], rec["meta"]


def is_spc_file(path: Path | str) -> bool:
    """只读文件内容判断是否 SPC ``.spc``（**不看扩展名**；读盘失败即 False）。

    真嗅探需要打开 OLE2 目录，所以读整个文件（样例 21–24 KB，代价可忽略）。
    """
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    try:
        return looks_like_spc(data)
    except SpcError as err:
        # 依赖缺失（olefile）与环境问题要让调用方看见，但**不能**打断扫描循环：
        # 记一条 WARNING（全项目只可能刷一次，见 _import_olefile 的缓存）后按 False。
        log.warning("无法判别 %s 是否为 .spc：%s", path, err)
        return False


def describe_failure(path: Path | str) -> str:
    """解析失败的**中文原因**（成功或"根本不是 spc"返回空串）。"""
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        return f"读取失败: {err}"
    if not looks_like_ole2(data):
        return ""
    try:
        read_spc_bytes(data)
    except SpcError as err:
        return str(err)
    return ""
