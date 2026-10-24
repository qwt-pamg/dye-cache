# -*- coding: utf-8 -*-
"""光物理表征卡三个子页共用的常量与小工具（表征总表列、数值格式、输出路径）。"""
from __future__ import annotations


import importlib
import math
from pathlib import Path
from PySide6.QtWidgets import QDoubleSpinBox
from core.paths import resolve_out_root


_CHAR_COLUMNS_FALLBACK = (
    "条件/溶剂", "化合物/组别", "吸收谱文件", "发射谱文件",
    "λabs(nm)", "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)",
    "ε(M⁻¹cm⁻¹)", "φ", "brightness(M⁻¹cm⁻¹)",
    "k_photo(min⁻¹)", "t½(min)", "备注",
)
#: 表列 → 行字段键（与 characterization.CharacterizationRow 同键，FR-44 原序）
_CHAR_KEYS = ("condition", "compound", "abs_file", "em_file",
              "lam_abs", "lam_em", "delta_lambda", "delta_wavenumber",
              "epsilon", "phi", "brightness", "k_photo", "t_half", "note")
_CHAR_NUMERIC_KEYS = frozenset(("lam_abs", "lam_em", "delta_lambda",
                                "delta_wavenumber", "epsilon", "phi",
                                "brightness", "k_photo", "t_half"))
_CHAR_FMT = {
    "lam_abs": ".1f", "lam_em": ".1f", "delta_lambda": ".1f",
    "delta_wavenumber": ".0f", "epsilon": ".4g", "phi": ".3g",
    "brightness": ".4g", "k_photo": ".4g", "t_half": ".2f",
}
_BLEACH_UNITS = (("min", "分钟 (min)"), ("s", "秒 (s)"), ("h", "小时 (h)"))


def _as_float(value, default=float("nan")) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _fmt_num(value, spec: str) -> str:
    """数值单元格文本：非有限 → 空串（FR-44「缺项留空」）。"""
    return "" if not _finite(value) else format(float(value), spec)


def _fmt_sig3(value) -> str:
    return "—" if not _finite(value) else f"{float(value):.3g}"


def _load_optional(dotted: str):
    """可选依赖懒导入（G-A 核心模块尚未落地时不阻塞 UI 构建）。"""
    return importlib.import_module(dotted)


def _spin(lo: float, hi: float, val: float, step: float = 1.0,
          dec: int = 2, suffix: str = "") -> QDoubleSpinBox:
    w = QDoubleSpinBox()
    w.setRange(float(lo), float(hi))
    w.setDecimals(int(dec))
    w.setSingleStep(float(step))
    w.setValue(float(val))
    if suffix:
        w.setSuffix(suffix)
    return w


def _empty_char_row() -> dict:
    row = {k: "" for k in _CHAR_KEYS}
    for k in _CHAR_NUMERIC_KEYS:
        row[k] = float("nan")
    return row


def photo_out_path(cfg: dict, kind: str) -> Path:
    """新图型输出路径：out_root/photophysics_<kind>_<HHMMSS>.png（不覆盖）。"""
    import datetime as _dt
    out_root = resolve_out_root(cfg)
    out_root.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%H%M%S")
    p = out_root / f"photophysics_{kind}_{stamp}.png"
    i = 2
    while p.exists():
        p = out_root / f"photophysics_{kind}_{stamp}-{i}.png"
        i += 1
    return p
