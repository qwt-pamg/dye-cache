# -*- coding: utf-8 -*-
"""V2.6 FR-44：光物理表征总表（单模块；FR-41 Stokes 表合并于此，Q-E19）。

本模块承担三件事（只做记录/格式化/导出，不做数值重算）：

1. FR-44 表征总表：14 列（条件/溶剂 · 化合物/组别 · 吸收谱文件 · 发射谱文件 ·
   λabs(nm) · λem(nm) · Δλ(nm) · Δν̃(cm⁻¹) · ε(M⁻¹cm⁻¹) · φ ·
   brightness(M⁻¹cm⁻¹) · k_photo(min⁻¹) · t½(min) · 备注）；
   行 = 化合物 × 条件，缺项留空（不臆造）。
2. FR-41 Stokes 表的唯一实现（原 core/report/stokes_table.py 改为纯转发别名）：
   StokesRow / STOKES_COLUMNS / make_stokes_row / row_from_spectra /
   write_stokes_csv / default_stokes_name / 格式化函数。数值口径复用
   core.analysis.spectra.stokes_shift（本模块只做行构造与写盘）。
3. 导出：CSV（UTF-8-SIG、中文列名、原子写、同名不覆盖、空表不产文件）与
   Excel（复用 core/report/excel_export 的样式/宽度/冻结/筛选单源；空表仍产出
   表头 + 无数据提示行的工作簿，与既有 Excel 导出纪律一致）。

不新增配置键；不 import GUI；不改流水线/Φ_Δ/ε/动力学算法。
"""
from __future__ import annotations

import csv
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from core.paths import resolve_out_root
from core.report.results import atomic_write

log = logging.getLogger("dye_tool.characterization")

#: FR-44 表征总表列名（中文；A42.4 直接断言顺序/齐全）
CHARACTERIZATION_COLUMNS = (
    "条件/溶剂", "化合物/组别", "吸收谱文件", "发射谱文件",
    "λabs(nm)", "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)",
    "ε(M⁻¹cm⁻¹)", "φ", "brightness(M⁻¹cm⁻¹)",
    "k_photo(min⁻¹)", "t½(min)", "备注",
)

#: FR-41 Stokes 对比表列名（Q-E19：定义在本模块，stokes_table 只转发）
STOKES_COLUMNS = ("组别", "吸收谱文件", "发射谱文件",
                 "λabs(nm)", "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)", "录入时间")

#: 录入时间格式（表内 / CSV 同一口径）
TIME_FMT = "%Y-%m-%d %H:%M:%S"

#: Excel 工作表名（A42.5 可直接断言）
SHEET_NAME = "表征总表"


# ═══════════════════════════ 行模型 ═══════════════════════════
@dataclass
class CharacterizationRow:
    """一行表征记录（FR-44；兼容 FR-41 的 StokesRow 语义）。

    数值缺项一律 NaN（缺项留空，不臆造）；group 是 compound 的读写别名，
    让 FR-41 的作图层仍可按 group 访问/编辑组别。
    """

    condition: str = ""
    compound: str = ""
    abs_file: str = ""
    em_file: str = ""
    lam_abs: float = float("nan")
    lam_em: float = float("nan")
    delta_lambda: float = float("nan")
    delta_wavenumber: float = float("nan")
    epsilon: float = float("nan")
    phi: float = float("nan")
    brightness: float = float("nan")
    k_photo: float = float("nan")
    t_half: float = float("nan")
    note: str = ""
    recorded_at: str = ""

    @property
    def group(self) -> str:
        """FR-41 的组别 = FR-44 的化合物/组别（读写兼容）。"""
        return self.compound

    @group.setter
    def group(self, value) -> None:
        self.compound = str(value or "")

    @property
    def ok(self) -> bool:
        """两个 λmax 都有限时才算可计算（NaN 行在表内标不可计算）。"""
        return (math.isfinite(float(self.lam_abs))
                and math.isfinite(float(self.lam_em)))


#: FR-41 的 StokesRow 与表征行是同一个类（Q-E19 单一真相）
StokesRow = CharacterizationRow


# ═══════════════════════════ 取值 / 格式化 ═══════════════════════════
def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def delta_wavenumber(lam_abs, lam_em) -> float:
    """Δν̃ = 1e7/λ_abs − 1e7/λ_em（cm⁻¹)；任一 λ 非有限 / ≤0 → NaN。"""
    la, le = _as_float(lam_abs), _as_float(lam_em)
    if not (math.isfinite(la) and math.isfinite(le)) or la <= 0 or le <= 0:
        return float("nan")
    return 1e7 / la - 1e7 / le


def fmt_nm(value) -> str:
    """波长 / Δλ 文本：1 位小数；NaN → 破折号。"""
    v = _as_float(value)
    return "—" if not math.isfinite(v) else f"{v:.1f}"


def fmt_wavenumber(value) -> str:
    """Δν̃ 文本：取整；NaN → 破折号。"""
    v = _as_float(value)
    return "—" if not math.isfinite(v) else str(int(round(v)))


def _fmt_num(value, sig: int) -> str:
    """表征表通用数值文本：有效数字（NaN → 空串；Excel/CSV 里是空白格）。"""
    v = _as_float(value)
    if not math.isfinite(v):
        return ""
    return f"{v:.{int(sig)}g}"


def _file_name(spec) -> str:
    """谱对象 → 文件名（缺 path 时退 label / 空串）。"""
    path = getattr(spec, "path", None)
    if path:
        try:
            return Path(str(path)).name
        except (TypeError, ValueError):
            pass
    return str(getattr(spec, "label", "") or "")


def group_from_path(path, cfg: Optional[dict] = None) -> str:
    """组别默认解析：复用 parse_filename（与数据页同源）；失败退整段 stem。"""
    from core.inputs.io import parse_filename

    try:
        p = Path(str(path))
    except (TypeError, ValueError):
        return ""
    fn = ((cfg or {}).get("filename") or {}) if isinstance(cfg, dict) else {}
    try:
        group = str(parse_filename(p.stem, fn).get("group") or "").strip()
    except Exception:                                  # noqa: BLE001
        group = ""
    return group or p.stem


def _now_text(now=None) -> str:
    return (now or datetime.now()).strftime(TIME_FMT)


def _pick(fields: dict, *names, default=None):
    """从 kwargs 里按别名顺序取值（None 视为未提供）。"""
    for name in names:
        if name in fields and fields[name] is not None:
            return fields[name]
    return default


def _stamp(now=None) -> str:
    return (now or datetime.now()).strftime("%H%M%S")


# ═══════════════════════════ FR-44 行构造 ═══════════════════════════
def make_characterization_row(**fields) -> CharacterizationRow:
    """由任意字段（别名宽容）构造表征行：缺项留空，不臆造。"""
    lam_abs = _as_float(_pick(fields, "lam_abs", "lambda_abs", "λabs",
                              "lam_abs_nm"))
    lam_em = _as_float(_pick(fields, "lam_em", "lambda_em", "λem",
                             "lam_em_nm"))
    d_lam = _pick(fields, "delta_lambda", "dlambda", "Δλ")
    if d_lam is None and math.isfinite(lam_abs) and math.isfinite(lam_em):
        d_lam = lam_em - lam_abs
    else:
        d_lam = _as_float(d_lam)
    d_nu = _pick(fields, "delta_wavenumber", "dnu", "Δν̃")
    d_nu = (delta_wavenumber(lam_abs, lam_em)
            if d_nu is None else _as_float(d_nu))
    return CharacterizationRow(
        condition=str(_pick(fields, "condition", "条件", "溶剂",
                            "condition_solvent", default="") or "").strip(),
        compound=str(_pick(fields, "compound", "group", "组别", "化合物",
                           "dye", default="") or "").strip(),
        abs_file=str(_pick(fields, "abs_file", "abs_path", "absorption",
                           "吸收谱文件", default="") or ""),
        em_file=str(_pick(fields, "em_file", "em_path", "emission",
                          "发射谱文件", default="") or ""),
        lam_abs=lam_abs,
        lam_em=lam_em,
        delta_lambda=d_lam,
        delta_wavenumber=d_nu,
        epsilon=_as_float(_pick(fields, "epsilon", "eps", "ε")),
        phi=_as_float(_pick(fields, "phi", "φ")),
        brightness=_as_float(_pick(fields, "brightness", "亮度")),
        k_photo=_as_float(_pick(fields, "k_photo", "k", "k_min")),
        t_half=_as_float(_pick(fields, "t_half", "t_half_min", "t½")),
        note=str(_pick(fields, "note", "备注", default="") or ""),
        recorded_at=str(_pick(fields, "recorded_at", "录入时间",
                              default="") or "") or _now_text(),
    )


#: 便捷别名（UI 侧可读性）
make_row = make_characterization_row


# ═══════════════════════════ FR-41 Stokes（唯一实现） ═══════════════════════════
def make_stokes_row(group, abs_file, em_file, lam_abs, lam_em, *,
                    recorded_at: Optional[str] = None) -> CharacterizationRow:
    """由一对 λmax 构造记录行（不抛异常：坏输入 → NaN 行）。

    Δλ = lam_em − lam_abs；Δν̃ = 1e7/lam_abs − 1e7/lam_em；NaN 传播。
    数值口径的唯一来源仍是 core.analysis.spectra.stokes_shift（调用方负责取峰）。
    """
    la, le = _as_float(lam_abs), _as_float(lam_em)
    if math.isfinite(la) and math.isfinite(le):
        d_lambda = le - la
    else:
        d_lambda = float("nan")
    return CharacterizationRow(
        compound=str(group or ""),
        abs_file=str(abs_file or ""),
        em_file=str(em_file or ""),
        lam_abs=la,
        lam_em=le,
        delta_lambda=d_lambda,
        delta_wavenumber=delta_wavenumber(la, le),
        recorded_at=recorded_at or _now_text(),
    )


def row_from_spectra(abs_spec, em_spec, *, group: Optional[str] = None,
                     abs_range: Optional[Sequence[float]] = None,
                     em_range: Optional[Sequence[float]] = None,
                     recorded_at: Optional[str] = None) -> CharacterizationRow:
    """对一对 Spectrum 调 core.analysis.spectra.stokes_shift 后构造记录行。"""
    from core.analysis.spectra import stokes_shift

    la, le, _shift = stokes_shift(getattr(abs_spec, "wl", []),
                                  getattr(abs_spec, "y", []),
                                  getattr(em_spec, "wl", []),
                                  getattr(em_spec, "y", []),
                                  abs_range, em_range)
    g = group if group is not None else group_from_path(
        getattr(abs_spec, "path", ""))
    return make_stokes_row(g, _file_name(abs_spec), _file_name(em_spec),
                           la, le, recorded_at=recorded_at)


# ═══════════════════════════ 文件名 / 路径 ═══════════════════════════
def default_stokes_name(now=None) -> str:
    """FR-41 默认文件名 stokes_<HHMMSS>.csv（now 可注入以便测试）。"""
    return f"stokes_{_stamp(now)}.csv"


def default_stokes_path(cfg: Optional[dict] = None, *,
                        out_root=None, now=None) -> Path:
    """FR-41 默认落盘路径：resolve_out_root(cfg[, out_root]) / stokes_<HHMMSS>.csv。"""
    return resolve_out_root(cfg or {}, out_root) / default_stokes_name(now)


def default_characterization_name(now=None) -> str:
    """FR-44 默认 CSV 文件名 characterization_<HHMMSS>.csv。"""
    return f"characterization_{_stamp(now)}.csv"


def default_characterization_xlsx_name(now=None) -> str:
    """FR-44 默认 Excel 文件名 characterization_<HHMMSS>.xlsx。"""
    return f"characterization_{_stamp(now)}.xlsx"


def default_characterization_path(cfg: Optional[dict] = None, *,
                                  out_root=None, now=None) -> Path:
    """FR-44 默认 CSV 落盘路径（out_root 同 FR-41 单源解析）。"""
    return (resolve_out_root(cfg or {}, out_root)
            / default_characterization_name(now))


def _resolve_target(out, name_fn, suffix: str) -> Path:
    """out 是目录 → 追加默认名；是完整文件路径 → 原样使用。"""
    p = Path(str(out))
    if p.is_dir() or p.suffix.lower() != suffix:
        p = p / name_fn()
    return p


def _unique_path(path: Path) -> Path:
    """同名不覆盖：已存在则追加 _2 / _3 … 后缀，返回真正可写的路径。"""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 10000):
        cand = path.with_name(f"{stem}_{i}{suffix}")
        if not cand.exists():
            return cand
    raise OSError(f"无法为 {path} 找到不覆盖的文件名")


# ═══════════════════════════ CSV 单元格 ═══════════════════════════
def _row_get(row, key: str):
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _csv_cell_num(value, fmt) -> str:
    """CSV 数值列：非有限写空串（Excel 里为空白格）。"""
    v = _as_float(value)
    return "" if not math.isfinite(v) else fmt(v)


def _stokes_cells(row) -> list:
    return [str(_row_get(row, "group") or ""),
            str(_row_get(row, "abs_file") or ""),
            str(_row_get(row, "em_file") or ""),
            _csv_cell_num(_row_get(row, "lam_abs"), lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "lam_em"), lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "delta_lambda"),
                          lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "delta_wavenumber"),
                          lambda v: str(int(round(v)))),
            str(_row_get(row, "recorded_at") or "")]


def _characterization_cells(row) -> list:
    return [str(_row_get(row, "condition") or ""),
            str(_row_get(row, "compound") or ""),
            str(_row_get(row, "abs_file") or ""),
            str(_row_get(row, "em_file") or ""),
            _csv_cell_num(_row_get(row, "lam_abs"), lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "lam_em"), lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "delta_lambda"),
                          lambda v: f"{v:.1f}"),
            _csv_cell_num(_row_get(row, "delta_wavenumber"),
                          lambda v: str(int(round(v)))),
            _csv_cell_num(_row_get(row, "epsilon"), lambda v: f"{v:.6g}"),
            _csv_cell_num(_row_get(row, "phi"), lambda v: f"{v:.4g}"),
            _csv_cell_num(_row_get(row, "brightness"),
                          lambda v: f"{v:.6g}"),
            _csv_cell_num(_row_get(row, "k_photo"), lambda v: f"{v:.6g}"),
            _csv_cell_num(_row_get(row, "t_half"), lambda v: f"{v:.4g}"),
            str(_row_get(row, "note") or "")]


def _write_csv(rows, columns, cells_fn, out, now, name_fn) -> Optional[Path]:
    items = list(rows or [])
    if not items:
        return None
    target = _unique_path(
        _resolve_target(out, lambda: name_fn(now), ".csv"))
    with atomic_write(target, newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(list(columns))
        for row in items:
            w.writerow(cells_fn(row))
    return target


def write_stokes_csv(rows: Sequence, out, *,
                     now: Optional[datetime] = None) -> Optional[Path]:
    """FR-41 对比表 CSV（UTF-8-SIG、中文列名、原子写；空表 → None 不建文件）。"""
    return _write_csv(rows, STOKES_COLUMNS, _stokes_cells, out, now,
                      default_stokes_name)


def write_characterization_csv(rows: Sequence, out, *,
                               now: Optional[datetime] = None
                               ) -> Optional[Path]:
    """FR-44 表征总表 CSV（UTF-8-SIG、14 列中文表头、同名不覆盖、空表 None）。"""
    return _write_csv(rows, CHARACTERIZATION_COLUMNS,
                      _characterization_cells, out, now,
                      default_characterization_name)


# ═══════════════════════════ Excel（复用 excel_export 样式单源） ═══════════════════════════
def _xlsx_row(row) -> list:
    """Excel 数据行：数值保持数值（NaN/Inf → None 空单元格），文本原样。"""
    keys = ("condition", "compound", "abs_file", "em_file", "lam_abs",
            "lam_em", "delta_lambda", "delta_wavenumber", "epsilon", "phi",
            "brightness", "k_photo", "t_half", "note")
    out = []
    for key in keys:
        value = _row_get(row, key)
        if value is None:
            out.append(None)
        elif isinstance(value, bool):
            out.append(str(value))
        elif isinstance(value, (int, float)):
            v = float(value)
            out.append(v if math.isfinite(v) else None)
        else:
            out.append(str(value))
    return out


def write_characterization_xlsx(rows: Sequence, out, *,
                                now: Optional[datetime] = None,
                                cfg: Optional[dict] = None
                                ) -> Optional[Path]:
    """FR-44 表征总表 Excel（复用 excel_export 的样式/宽度/冻结/筛选单源）。

    空表也生成 表头 + 无数据提示行 的工作簿（与既有 Excel 导出纪律一致）；
    openpyxl 缺失 / 写盘失败 → None（IO 层绝不打断 GUI）。
    """
    from core.report import excel_export as ex

    items = list(rows or [])
    try:
        if not ex._ensure_openpyxl():
            log.warning("未安装 openpyxl，无法导出表征总表 Excel")
            return None
        from openpyxl import Workbook
    except Exception as err:                           # noqa: BLE001
        log.warning("openpyxl 不可用：%s", err)
        return None
    target = _unique_path(_resolve_target(
        out, lambda: default_characterization_xlsx_name(now), ".xlsx"))
    excel_cfg = ((cfg or {}).get("excel") or {}) if isinstance(cfg, dict) else {}
    try:
        decimals = int(excel_cfg.get("decimals", 6) or 6)
    except (TypeError, ValueError):
        decimals = 6
    try:
        width_max = float(excel_cfg.get("column_width_max", 60) or 60)
    except (TypeError, ValueError):
        width_max = 60.0
    freeze = bool(excel_cfg.get("freeze_header", True))
    autofilter = bool(excel_cfg.get("autofilter", True))
    matrix = [_xlsx_row(row) for row in items]
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    tmp = target.with_name(f".{target.name}.tmp{os.getpid()}")
    try:
        ex._write_table(ws, list(CHARACTERIZATION_COLUMNS), matrix,
                        decimals=decimals, freeze=freeze,
                        autofilter=autofilter, width_max=width_max,
                        note="本次没有表征记录")
        target.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(tmp))
        os.replace(tmp, target)
    except BaseException as err:                       # noqa: BLE001
        log.warning("写出表征总表 Excel 失败：%s", err)
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return target


#: 别名（UI 侧命名习惯）
write_characterization_excel = write_characterization_xlsx


__all__ = [
    "CHARACTERIZATION_COLUMNS", "STOKES_COLUMNS", "SHEET_NAME", "TIME_FMT",
    "CharacterizationRow", "StokesRow",
    "make_characterization_row", "make_row", "make_stokes_row",
    "row_from_spectra", "default_characterization_name",
    "default_characterization_xlsx_name", "default_characterization_path",
    "default_stokes_name", "default_stokes_path",
    "write_characterization_csv", "write_characterization_xlsx",
    "write_characterization_excel", "write_stokes_csv",
    "group_from_path", "delta_wavenumber", "fmt_nm", "fmt_wavenumber",
]
