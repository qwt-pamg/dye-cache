# -*- coding: utf-8 -*-
"""高级出图参数对话框（设计文档 FR-7.3 / FR-8）。

把 ``plot.jacs.*`` 的全部键按 :data:`core.plotstyle.JACS_SCHEMA` 的 ``group``
铺成标签页（键多，每页可滚动）：

* 控件类型由 ``JACS_SCHEMA[key]["type"]`` 决定——``float``/``int`` 用 SpinBox、
  ``bool`` 用 CheckBox、``str``/``color``/列表/区间/映射用 LineEdit 并按各自
  语法解析；**每个控件都有中文 tooltip**（"小白优先"）；
* 「校验」直接调 :func:`core.plotstyle.validate_jacs`，问题按 ERROR/WARNING 分色，
  「恢复默认」用 :func:`jacs_defaults`，「预设」下拉列出内置 + 用户预设；
* 右侧样板图（示意数据）按主要键实时重绘，控件改动经 **200 ms 防抖** 后重绘，
  连续输入不会拖慢界面。

对外只暴露两个查询方法：:meth:`PlotStyleDialog.values` 与
:meth:`PlotStyleDialog.result_cfg_dict`；二者都**不改动**传入的 cfg（构造时即已
深拷贝），调用方可以放心持有原对象。
"""
from __future__ import annotations

import copy
import functools
import html
import json
import logging
import math
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QMenu,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSpinBox, QTabWidget, QToolButton,
                               QVBoxLayout, QWidget)

from core.plotstyle import (JACS_SCHEMA, ConfigIssue, delete_user_preset,
                            jacs_defaults, list_presets, load_preset,
                            save_user_preset, validate_jacs)

log = logging.getLogger("dye_tool.ui.plot_style_dialog")

__all__ = ["PlotStyleDialog"]

#: 预览是可选功能：matplotlib / Qt 后端缺失时整个对话框仍要能打开
try:
    import matplotlib
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    _MPL_ERROR: Optional[str] = None
except Exception as _err:                                  # noqa: BLE001
    matplotlib = None                                      # type: ignore[assignment]
    FigureCanvasQTAgg = None                               # type: ignore[assignment,misc]
    Figure = None                                          # type: ignore[assignment,misc]
    _MPL_ERROR = f"matplotlib 预览不可用（{_err}）；其余参数编辑功能不受影响。"

#: 样板图显示尺寸上限（in @100 dpi）：画布再大也只等比缩小，避免对话框被撑爆
_PREVIEW_MAX_W = 3.4
_PREVIEW_MAX_H = 2.9
_PREVIEW_DPI = 100

#: 文本框解析失败时的红框提示
_BAD_STYLE = "border: 1px solid #c00000; background: #fff5f5;"

#: 标签页顺序（设计文档 FR-7.3）；schema 里新出现但未列出的分组追加在末尾
_GROUP_ORDER = ("版式与导出", "轴与框", "曲线与配色", "标注与趋势箭头",
                "常规作图 (FR-4)", "高级")

#: 类型 → 中文（tooltip 里告诉用户该填什么）
_TYPE_ZH = {
    "float": "小数", "int": "整数", "bool": "开关（true/false）",
    "str": "文本", "color": "颜色或文本",
    "list_float": "数值列表（如 300, 700）",
    "list_str": "文本列表（逗号分隔）",
    "dict": "映射（k=v 或 JSON）",
    "search": "两个数值或 auto",
    "range_or_data": "两个数值或 data",
}

#: 各类型文本框的占位提示（小白照着填即可）
_PLACEHOLDER = {
    "list_float": "两个数值，逗号分隔，如 300, 700",
    "search": "两个数值（如 540, 620）或 auto",
    "range_or_data": "两个数值（如 432, 556）或 data",
    "list_str": "逗号分隔，如 png,svg",
    "dict": "如 a=2.5,c=null；也接受 JSON {\"a\": 2.5}",
    "color": "颜色名/灰度/十六进制，如 crimson、0.5、#c00000",
    "str": "文本",
}


# ═══════════════════════════ 文本解析小工具 ═══════════════════════════
def _scalar_text(val: Any) -> str:
    """标量 → 文本（``None`` → ``null``，布尔 → ``true``/``false``）。"""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return f"{val:g}"
    return str(val)


def _num_text(val: Any) -> str:
    """数值 → 紧凑文本（``300.0`` → ``"300"``）；非数值原样输出。"""
    try:
        return f"{float(val):g}"
    except (TypeError, ValueError):
        return str(val)


def _split_commas(text: str) -> list[str]:
    """按中英文逗号/分号切分并去掉空项（用户常混用全角符号）。"""
    flat = (text.replace("，", ",").replace("；", ",")
            .replace(";", ",").replace("\n", ","))
    return [p.strip() for p in flat.split(",") if p.strip()]


def _parse_numbers(text: str, *, two: bool = True) -> list[float]:
    """解析 ``"540,620"`` / ``"540 620"`` 这类数值列表；失败抛 ``ValueError``。"""
    parts = [p for p in (text.replace("，", ",").replace("；", ",")
                         .replace(";", ",").replace(" ", ",").split(","))
             if p.strip()]
    if not parts:
        raise ValueError("内容不能为空")
    if two and len(parts) != 2:
        raise ValueError(f"应填两个数值（如 540,620），当前识别到 {len(parts)} 个")
    vals: list[float] = []
    for part in parts:
        try:
            vals.append(float(part))
        except ValueError as err:
            raise ValueError(f"「{part}」不是数值") from err
    return vals


def _coerce_scalar(text: str) -> Any:
    """``k=v`` 里的文本值 → 数值/布尔/None/字符串（``fig_heights`` 的 null 走这里）。"""
    s = text.strip()
    low = s.lower()
    if low in ("null", "none", "nil", ""):
        return None
    if low in ("true", "yes", "是"):
        return True
    if low in ("false", "no", "否"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _parse_mapping(text: str) -> dict:
    """解析 ``a=2.5,c=null`` 或 JSON 对象 → dict；失败抛 ``ValueError``。"""
    if not text:
        raise ValueError("内容不能为空")
    if text.lstrip().startswith("{"):
        try:
            obj = json.loads(text)
        except Exception as err:                           # noqa: BLE001
            raise ValueError(f"JSON 解析失败：{err}") from err
        if not isinstance(obj, dict):
            raise ValueError("JSON 顶层必须是对象（以 { 开头）")
        return {str(k): v for k, v in obj.items()}
    out: dict[str, Any] = {}
    for part in _split_commas(text):
        key, sep, val = part.partition("=")
        if not sep:
            key, sep, val = part.partition(":")
        if not sep or not key.strip():
            raise ValueError(f"「{part}」缺少 = 号（应写成 键=值）")
        out[key.strip()] = _coerce_scalar(val)
    if not out:
        raise ValueError("内容不能为空")
    return out


def _parse_text(spec: dict, text: str) -> tuple[bool, Any, str]:
    """按键类型解析文本框 → ``(是否成功, 值, 失败原因)``；**不抛异常**。"""
    t = str(spec.get("type", "str"))
    raw = (text or "").strip()
    try:
        if t == "list_float":
            return True, _parse_numbers(raw, two=True), ""
        if t in ("search", "range_or_data"):
            allowed = ("auto", "data") if t == "search" else ("data",)
            if raw.lower() in allowed:
                return True, raw.lower(), ""
            return True, _parse_numbers(raw, two=True), ""
        if t == "list_str":
            vals = _split_commas(raw)
            if not vals:
                raise ValueError("至少填一项（逗号分隔）")
            return True, vals, ""
        if t == "dict":
            return True, _parse_mapping(raw), ""
        if t in ("str", "color"):
            if not raw:
                raise ValueError("内容不能为空")
            return True, raw, ""
        if t in ("float", "int"):
            return True, (int(float(raw)) if t == "int" else float(raw)), ""
        raise ValueError(f"未知类型 {t}")
    except ValueError as err:
        return False, None, str(err)
    except Exception as err:                               # noqa: BLE001
        return False, None, f"{type(err).__name__}: {err}"


def _format_mapping(obj: Any) -> str:
    """dict → 单行文本（标量值用 ``k=v`` 便于手改，嵌套值用 JSON）。"""
    if not isinstance(obj, dict):
        return str(obj)
    if all(not isinstance(v, (dict, list, tuple)) for v in obj.values()):
        return ", ".join(f"{k}={_scalar_text(v)}" for k, v in obj.items())
    return json.dumps(obj, ensure_ascii=False)


def _format_value(spec: dict, val: Any) -> str:
    """配置值 → 文本框内容（与 :func:`_parse_text` 互逆）。"""
    t = str(spec.get("type", "str"))
    if t == "list_float":
        return (", ".join(_num_text(v) for v in val)
                if isinstance(val, (list, tuple)) else str(val))
    if t in ("search", "range_or_data"):
        if isinstance(val, (list, tuple)):
            return ", ".join(_num_text(v) for v in val)
        return str(val)
    if t == "list_str":
        return (", ".join(str(v) for v in val)
                if isinstance(val, (list, tuple)) else str(val))
    if t == "dict":
        return _format_mapping(val)
    return str(val)


# ═══════════════════════════ 标签 / tooltip 文案 ═══════════════════════════
def _short_help(help_text: str, limit: int = 30) -> str:
    """取 help 的第一句（或前 ``limit`` 字）做标签副标题。"""
    text = help_text.strip().replace("\n", " ")
    for stop in ("。", "；", "，"):
        idx = text.find(stop)
        if 0 < idx <= limit:
            return text[:idx]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row_label(spec: dict) -> str:
    """表单左侧标签：``label`` + 换行一句 help 前半句（不悬停也能看懂）。"""
    label = html.escape(str(spec.get("label") or ""))
    short = _short_help(str(spec.get("help") or ""))
    if short:
        return (f"<b>{label}</b><br>"
                f"<span style='color:#666666'>{html.escape(short)}</span>")
    return f"<b>{label}</b>"


def _tooltip_text(key: str, spec: dict) -> str:
    """控件 tooltip：中文说明 + 键名/类型/默认值/可选值/建议范围。"""
    t = str(spec.get("type", "str"))
    lines = [str(spec.get("help") or "该键暂无额外说明。")]
    default_text = _format_value(spec, spec.get("default"))
    if len(default_text) > 48:
        default_text = default_text[:45] + "…"
    lines.append(f"键名：plot.jacs.{key}")
    lines.append(f"类型：{_TYPE_ZH.get(t, t)}；默认值：{default_text}")
    if spec.get("choices"):
        lines.append("可选值：" + "、".join(str(c) for c in spec["choices"]))
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None or hi is not None:
        lines.append("建议范围："
                     f"{'不限' if lo is None else _num_text(lo)} ~ "
                     f"{'不限' if hi is None else _num_text(hi)}")
    return "\n".join(lines)


def _grouped_keys() -> list[tuple[str, list[str]]]:
    """按 ``JACS_SCHEMA[*]["group"]`` 分组 → ``[(组名, [键...]), ...]``（顺序固定）。"""
    groups: dict[str, list[str]] = {}
    for key, spec in JACS_SCHEMA.items():
        groups.setdefault(str(spec.get("group") or "其它"), []).append(key)
    ordered = [g for g in _GROUP_ORDER if g in groups]
    ordered += [g for g in groups if g not in ordered]
    return [(g, groups[g]) for g in ordered]


def _read_probe(cfg: Any) -> float:
    """探针波长（样板图灰虚线用）；缺失/类型不对时按 379 nm。"""
    wl = cfg.get("wavelength") if isinstance(cfg, dict) else None
    val = wl.get("probe") if isinstance(wl, dict) else None
    if isinstance(val, (int, float)) and not isinstance(val, bool) \
            and 100.0 <= float(val) <= 1200.0:
        return float(val)
    return 379.0


def _cmap_colors(name: str, lo: float, hi: float, n: int) -> list:
    """取 ``n`` 个色带颜色（**委托** ``core.render.plotstyle.cmap_colors``）。

    V2.x 审查 M1：原先本函数自带一套"非法区间回退 0.30/0.95"的规则，而渲染侧
    不校验 → 预览与实际出图色序不同。现在两边共用同一实现（含同样的告警）。
    """
    from core.render.plotstyle import cmap_colors
    return cmap_colors(name, lo, hi, n)


# ═══════════════════════════ 对话框 ═══════════════════════════
class PlotStyleDialog(QDialog):
    """「高级出图参数」对话框：编辑一份 ``plot.jacs.*`` 取值（FR-7.3 / FR-8）。

    用法::

        dlg = PlotStyleDialog(cfg, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            cfg = dlg.result_cfg_dict()      # 新 dict；原 cfg 一个字节都没改
    """

    def __init__(self, cfg: dict, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("高级出图参数（plot.jacs）")
        #: 传入 cfg 的深拷贝——此后所有读写只发生在副本上
        self._cfg: dict = copy.deepcopy(cfg) if isinstance(cfg, dict) else {}
        self._probe_nm: float = _read_probe(self._cfg)
        # A23（审查附录 C）：**单一登记源** = `_style_rows`。
        # 原来另有 `_editors` 字典与它双份登记，两者漂移会让"搜索隐藏了、取值
        # 却仍读旧控件"；现在取值一律从 `_style_rows` 查（`_editor_of`）。
        #: 文本框解析失败时的回退值（保证 values() 永远是合法取值）
        self._last_values: dict = jacs_defaults()
        self._preset_name: Optional[str] = None
        self._preset_labels: dict[str, str] = {}
        self._user_presets: set[str] = set()
        self._builtin_presets: set[str] = set()
        self._fig: Optional[Any] = None
        self._canvas: Optional[Any] = None
        #: 200 ms 防抖：连续输入/连点只触发一次重绘
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(200)
        self._debounce.timeout.connect(self._draw_preview)
        self._build()
        self.set_values(self._initial_values())
        self._refresh_presets()
        self.resize(1180, 760)

    # ══════════════ 构建界面 ══════════════
    def _build(self) -> None:
        """搭界面：左标签页 + 右样板图 + 底部校验区/按钮排。"""
        # 先建状态标签：编辑器的改动回调会写它们
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet("color: #555555;")
        self.lbl_preset = QLabel("预设：未选择")
        self.lbl_preset.setToolTip("最近套用的预设名；手动改动后标记为「已改动」")

        self._style_rows: list = []          # FR-24.5：(键名, 标签文本, QLabel, 编辑器, 分组)
        self._group_forms: dict = {}         # A23：分组名 → QFormLayout（过滤用）
        self._group_tab_index: dict = {}     # A23：分组名 → 页签下标（空组隐藏用）
        self._row_filters: dict = {}         # A23：分组名 → FormRowFilter（150ms 防抖）
        self.tabs = QTabWidget()
        self.tabs.setToolTip("按 JACS_SCHEMA 的 group 分组；每页可滚动查看全部键")
        for group, keys in _grouped_keys():
            idx = self.tabs.addTab(self._make_group_page(keys, group), group)
            self._group_tab_index[str(group)] = idx
        # A23：每组一个 FormRowFilter（与参数面板同一套语义：150ms 防抖 +
        # 标签/控件文字/tooltip 匹配 + 单一登记源），过滤后**空分组页签隐藏**。
        from ui.widgets.filtering import FormRowFilter
        for group, form in self._group_forms.items():
            self._row_filters[group] = FormRowFilter(
                form, [],
                rows_provider=(lambda g=group: [
                    (lab, ed) for _k, _lt, lab, ed, grp in self._style_rows
                    if grp == g]),
                on_change=(lambda shown, q, g=group:
                           self._on_group_filtered(g, shown, q)))

        body = QHBoxLayout()
        left = QVBoxLayout()
        # V2.3 FR-24.5：按键名/中文说明/分组实时过滤；Ctrl+F 聚焦搜索框
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText(
            "搜索键名 / 中文说明 / 分组（空 = 全部显示；Ctrl+F 聚焦）")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.textChanged.connect(self._schedule_filter)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("搜索"))
        search_row.addWidget(self.ed_search, 1)
        left.addLayout(search_row)
        left.addWidget(self.tabs, 1)
        body.addLayout(left, 1)
        body.addWidget(self._make_preview_panel(), 0)
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)

        self.txt_issues = QPlainTextEdit()
        self.txt_issues.setReadOnly(True)
        self.txt_issues.setMaximumHeight(110)
        self.txt_issues.setPlaceholderText(
            "点「校验」检查当前参数：未知键 / 类型 / 范围 / 区间顺序 / 跨键一致性")
        self.txt_issues.setToolTip("validate_jacs() 的中文问题清单；ERROR 红字、WARNING 橙字")

        self.btn_validate = QPushButton("校验")
        self.btn_validate.setToolTip("按 core.plotstyle.validate_jacs 检查当前界面的取值")
        self.btn_validate.clicked.connect(self._on_validate)
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.setToolTip("把所有控件恢复为 JACS_SCHEMA 里登记的默认值")
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_preset = QToolButton()
        self.btn_preset.setText("预设 ▾")
        self.btn_preset.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_preset.setToolTip("套用内置预设或自己保存的用户预设（presets.yaml）")
        self._preset_menu = QMenu(self)
        self.btn_preset.setMenu(self._preset_menu)
        self.btn_save = QPushButton("另存为预设…")
        self.btn_save.setToolTip("把当前所有取值存成用户预设（不能与内置预设重名）")
        self.btn_save.clicked.connect(self._on_save_preset)
        self.btn_delete = QPushButton("删除用户预设…")
        self.btn_delete.setToolTip("删除一个用户预设；内置预设不可删除")
        self.btn_delete.clicked.connect(self._on_delete_preset)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        ok_btn.setText("确定")
        ok_btn.setToolTip("采纳当前取值（用 result_cfg_dict() 取回合并后的配置）")
        cancel_btn.setText("取消")
        cancel_btn.setToolTip("放弃本次改动")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        bottom = QHBoxLayout()
        bottom.addWidget(self.lbl_status, 1)
        bottom.addWidget(self.lbl_preset)
        for btn in (self.btn_validate, self.btn_reset, self.btn_preset,
                    self.btn_save, self.btn_delete):
            bottom.addWidget(btn)
        bottom.addWidget(self.buttons)

        main = QVBoxLayout(self)
        main.addLayout(body, 1)
        main.addWidget(QLabel("校验结果（ERROR 红字 / WARNING 橙字）"))
        main.addWidget(self.txt_issues)
        main.addLayout(bottom)

    def _make_group_page(self, keys: list[str], group: str = "") -> QScrollArea:
        """一个分组 → 可滚动的 ``QFormLayout`` 页（键多，必须能滚）。"""
        page = QWidget()
        form = QFormLayout(page)
        # A23：登记本组 form（过滤 = FormRowFilter 统一语义，见 filtering.py）
        self._group_forms[str(group)] = form
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        for key in keys:
            spec = JACS_SCHEMA[key]
            editor = self._make_editor(key, spec)
            tip = _tooltip_text(key, spec)
            editor.setToolTip(tip)
            label = QLabel(_row_label(spec))
            label.setWordWrap(True)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            label.setToolTip(tip)
            form.addRow(label, editor)
            # FR-24.5：登记行，供搜索过滤（键名 / 中文说明 / 分组）
            self._style_rows.append((key, label.text(), label, editor, str(group)))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    # ══════════════ V2.3 FR-24.5：搜索过滤 ══════════════
    def _focus_search(self) -> None:
        """Ctrl+F：聚焦搜索框并全选。"""
        self.ed_search.setFocus()
        self.ed_search.selectAll()

    def _schedule_filter(self, text: str) -> None:
        """A23：键入防抖 150 ms（与参数面板 FormRowFilter 同一间隔）。"""
        for f in getattr(self, "_row_filters", {}).values():
            f.schedule(text)

    def _filter_rows(self, text: str) -> int:
        """立即按 键名 / 说明 / 分组 过滤（返回可见行数；测试与 Ctrl+F 用）。

        走 :class:`~ui.widgets.filtering.FormRowFilter` 的**同一套匹配语义**
        （标签文字 + 控件文字 + tooltip），不再另写一份 key/label/group 判断。
        """
        shown = 0
        for f in getattr(self, "_row_filters", {}).values():
            shown += f.apply(text)
        return shown

    def _on_group_filtered(self, group: str, shown: int, q: str) -> None:
        """A23：过滤后隐藏**空分组页签**；有查询时切到第一个有命中的页签。"""
        idx = self._group_tab_index.get(group)
        if idx is None:
            return
        self.tabs.setTabVisible(idx, (shown > 0) or (not q))
        if q and shown > 0 and self.tabs.currentIndex() == idx:
            return
        if q and shown > 0 and not self.tabs.isTabVisible(self.tabs.currentIndex()):
            self.tabs.setCurrentIndex(idx)

    def _editor_of(self, key: str) -> Optional[QWidget]:
        """按唯一登记源 `_style_rows` 取某键的编辑器控件。"""
        for k, _lt, _lab, ed, _grp in self._style_rows:
            if k == key:
                return ed
        return None

    def _make_editor(self, key: str, spec: dict) -> QWidget:
        """按键类型造控件，并接上「改动 → 防抖重绘」。"""
        t = str(spec.get("type", "str"))
        widget: QWidget
        if t == "bool":
            widget = QCheckBox("启用")
        elif t == "int":
            spin = QSpinBox()
            lo, hi = spec.get("min"), spec.get("max")
            spin.setRange(int(lo) if lo is not None else -10 ** 6,
                          int(hi) if hi is not None else 10 ** 6)
            spin.setAccelerated(True)
            widget = spin
        elif t == "float":
            dspin = QDoubleSpinBox()
            lo, hi = spec.get("min"), spec.get("max")
            lo_f = float(lo) if lo is not None else -10 ** 6
            hi_f = float(hi) if hi is not None else 10 ** 6
            if hi_f <= lo_f:                               # schema 写反也要能用
                lo_f, hi_f = -10 ** 6, 10 ** 6
            dspin.setRange(lo_f, hi_f)
            dspin.setDecimals(3)                           # 无 min/max 时保留 3 位小数
            dspin.setSingleStep(0.1 if (hi_f - lo_f) <= 10.0 else 1.0)
            dspin.setAccelerated(True)
            widget = dspin
        else:
            edit = QLineEdit()
            edit.setPlaceholderText(_PLACEHOLDER.get(t, "文本"))
            widget = edit
        if isinstance(widget, QCheckBox):
            widget.toggled.connect(functools.partial(self._on_editor_changed, key))
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            widget.valueChanged.connect(functools.partial(self._on_editor_changed, key))
        elif isinstance(widget, QLineEdit):
            widget.editingFinished.connect(functools.partial(self._on_editor_changed, key))
        # （单一登记：_style_rows 已登记，这里不再写第二份）
        return widget

    def _make_preview_panel(self) -> QWidget:
        """右侧样板图：示意谱图 + 刷新按钮 + 说明（matplotlib 缺失时给中文提示）。"""
        panel = QWidget()
        panel.setMinimumWidth(380)
        lay = QVBoxLayout(panel)
        head = QLabel("样板图预览（示意数据，非真实测量）")
        head.setStyleSheet("font-weight: bold;")
        lay.addWidget(head)
        if Figure is None or FigureCanvasQTAgg is None:     # 预览不可用，但不影响编辑
            hint = QLabel(_MPL_ERROR or "matplotlib 不可用，预览已禁用。")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #b26a00;")
            lay.addWidget(hint)
        else:
            self._fig = Figure(figsize=(_PREVIEW_MAX_W, _PREVIEW_MAX_H),
                               dpi=_PREVIEW_DPI)
            self._fig.patch.set_facecolor("white")
            self._canvas = FigureCanvasQTAgg(self._fig)
            lay.addWidget(self._canvas, 0,
                          Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)
        self.btn_preview = QPushButton("刷新预览")
        self.btn_preview.setToolTip("按当前参数立即重绘样板图（改动控件时会自动重绘）")
        self.btn_preview.clicked.connect(self._draw_preview)
        lay.addWidget(self.btn_preview)
        note = QLabel("预览只体现主要键（画布尺寸/字号/线宽/框/刻度方向/色带/标注/箭头），"
                      "其余键请以导出结果为准。")
        note.setWordWrap(True)
        note.setStyleSheet("color: #777777;")
        lay.addWidget(note)
        lay.addStretch(1)
        return panel

    # ══════════════ 读写契约（对外） ══════════════
    def _initial_values(self) -> dict:
        """初始值 = ``JACS_SCHEMA`` 默认值 + 传入 cfg 里已有的 ``plot.jacs`` 值。"""
        vals = jacs_defaults()
        plot = self._cfg.get("plot") if isinstance(self._cfg, dict) else None
        got = plot.get("jacs") if isinstance(plot, dict) else None
        if isinstance(got, dict):
            vals.update({k: v for k, v in got.items() if k in JACS_SCHEMA})
        return vals

    def set_values(self, values: dict) -> None:
        """把 ``values`` 回填到所有控件（未知键忽略；不改动传入的对象）。"""
        src = jacs_defaults()
        if isinstance(values, dict):
            src.update({k: v for k, v in values.items()
                        if k in JACS_SCHEMA and v is not None})
        for key, spec in JACS_SCHEMA.items():
            widget = self._editor_of(key)
            if widget is None:
                continue
            val = src.get(key, spec.get("default"))
            widget.blockSignals(True)
            try:
                self._write_widget(spec, widget, val)
            except Exception as err:                       # noqa: BLE001
                log.debug("回填 %s 失败，改用默认值：%s", key, err)
            finally:
                widget.blockSignals(False)
        self._last_values = copy.deepcopy(src)
        self._draw_preview()                               # 回填后立即重绘，不必防抖

    def _write_widget(self, spec: dict, widget: QWidget, val: Any) -> None:
        """单控件写入（类型不符时抛异常，由 :meth:`set_values` 兜底）。"""
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(val))
        elif isinstance(widget, QSpinBox):
            widget.setValue(int(val))
        elif isinstance(widget, QDoubleSpinBox):
            widget.setValue(float(val))
        elif isinstance(widget, QLineEdit):
            widget.setStyleSheet("")
            widget.setText(_format_value(spec, val))
        else:
            raise TypeError(f"未知控件 {type(widget).__name__}")

    def values(self) -> dict:
        """当前控件取值（只含 ``JACS_SCHEMA`` 里的键，与传入 cfg 无引用关系）。

        文本框解析失败时该键**回退到上次有效值**并在状态栏给中文提示，
        因此返回值永远是合法类型、键数恒等于 ``len(JACS_SCHEMA)``。
        """
        out: dict[str, Any] = jacs_defaults()
        bad: list[str] = []
        for key, spec in JACS_SCHEMA.items():
            widget = self._editor_of(key)
            fallback = self._last_values.get(key, spec.get("default"))
            if widget is None:
                out[key] = copy.deepcopy(fallback)
                continue
            try:
                if isinstance(widget, QCheckBox):
                    out[key] = bool(widget.isChecked())
                elif isinstance(widget, QSpinBox):
                    out[key] = int(widget.value())
                elif isinstance(widget, QDoubleSpinBox):
                    out[key] = float(widget.value())
                elif isinstance(widget, QLineEdit):
                    ok, val, _msg = _parse_text(spec, widget.text())
                    if ok:
                        out[key] = val
                    else:
                        out[key] = copy.deepcopy(fallback)
                        bad.append(key)
                else:
                    out[key] = copy.deepcopy(fallback)
            except Exception as err:                       # noqa: BLE001
                log.debug("读取 %s 失败：%s", key, err)
                out[key] = copy.deepcopy(fallback)
                bad.append(key)
        self._last_values.update({k: copy.deepcopy(v)
                                  for k, v in out.items() if k not in bad})
        if bad:
            names = "、".join(str(JACS_SCHEMA[k].get("label") or k) for k in bad)
            self._set_status(f"⚠ 填写有误的键已暂用上次有效值：{names}")
        return copy.deepcopy(out)

    def result_cfg_dict(self) -> dict:
        """当前取值合并进传入 cfg 的**新 dict**（深拷贝，写 ``plot['jacs']``）。

        ``plot.jacs`` 写的是**全量键**（含默认值），避免上一份配置里的旧值残留；
        传入的 cfg 对象始终未被改动。
        """
        out = copy.deepcopy(self._cfg) if isinstance(self._cfg, dict) else {}
        plot = out.get("plot")
        if not isinstance(plot, dict):
            plot = {}
            out["plot"] = plot
        plot["jacs"] = self.values()
        return out

    # ══════════════ 控件回调 ══════════════
    def _on_editor_changed(self, key: str, *_ignored: Any) -> None:
        """任一控件改动：文本框先试解析（失败只提示），再防抖重绘预览。"""
        spec = JACS_SCHEMA.get(key) or {}
        widget = self._editors.get(key)
        if isinstance(widget, QLineEdit):
            ok, val, msg = _parse_text(spec, widget.text())
            widget.setStyleSheet("" if ok else _BAD_STYLE)
            if ok:
                self._last_values[key] = val
            else:
                label = spec.get("label") or key
                self._set_status(f"⚠「{label}」填写有误：{msg}（暂用上次有效值）")
        if self._preset_name:
            label = self._preset_labels.get(self._preset_name, self._preset_name)
            self.lbl_preset.setText(f"预设：{label}（已改动）")
        self._debounce.start()                             # 200 ms 防抖

    def _set_status(self, msg: str) -> None:
        """底部状态文字（空串给占位，避免布局跳动）。"""
        self.lbl_status.setText(msg or "就绪")

    def _on_reset(self) -> None:
        """恢复默认：把 :func:`jacs_defaults` 回填所有控件。"""
        self.set_values(jacs_defaults())
        self._preset_name = None
        self.lbl_preset.setText("预设：未选择")
        self._set_status("已恢复默认值（JACS_SCHEMA 的 default）")

    def _on_validate(self) -> None:
        """校验当前取值并把问题清单按等级分色显示。"""
        try:
            issues = validate_jacs(self.values())
        except Exception as err:                           # noqa: BLE001
            issues = [ConfigIssue("ERROR", "plot.jacs",
                                  f"校验器异常：{err}", "请把该信息反馈给开发者")]
        self.txt_issues.clear()
        if not issues:
            self.txt_issues.appendHtml(
                "<span style='color:#1a7f37'>✔ 未发现问题，可以放心「确定」。</span>")
            self._set_status("校验通过：0 个问题")
            return
        n_err = sum(1 for i in issues if i.level == "ERROR")
        for it in issues:
            is_err = str(it.level).upper() == "ERROR"
            color = "#c00000" if is_err else "#b26a00"
            tag = "错误" if is_err else "警告"
            text = f"【{tag}】{it.key}：{it.message}"
            if it.suggestion:
                text += f"　建议：{it.suggestion}"
            self.txt_issues.appendHtml(
                f"<span style='color:{color}'>{html.escape(text)}</span>")
        self._set_status(f"校验完成：{n_err} 个错误、{len(issues) - n_err} 个警告")

    # ══════════════ 预设（FR-8） ══════════════
    def _refresh_presets(self) -> None:
        """重建「预设 ▾」菜单：内置在前、用户在后。"""
        self._preset_menu.clear()
        self._preset_labels.clear()
        self._user_presets.clear()
        self._builtin_presets.clear()
        rows: list[tuple] = []
        try:
            rows = list(list_presets())
        except Exception as err:                           # noqa: BLE001
            log.warning("读取预设列表失败：%s", err)
        if not rows:
            self._preset_menu.addAction("（未找到任何预设）").setEnabled(False)
            return
        for name, label, is_builtin in rows:
            name, label = str(name), str(label)
            self._preset_labels[name] = label
            if is_builtin:
                self._builtin_presets.add(name)
            else:
                self._user_presets.add(name)
            action = self._preset_menu.addAction(
                label if is_builtin else f"{label}　★用户预设")
            action.triggered.connect(functools.partial(self._apply_preset, name))

    def _apply_preset(self, name: str, *_ignored: Any) -> None:
        """套用预设：先回默认再叠加预设值（与 ``apply_preset`` 同语义）。"""
        try:
            vals = load_preset(name)
        except Exception as err:                           # noqa: BLE001
            vals = None
            log.warning("读取预设 %s 失败：%s", name, err)
        if not isinstance(vals, dict):
            self._set_status(f"⚠ 预设「{name}」不存在或已损坏，未做任何改动"
                             "（预设计列表已刷新）")
            self._refresh_presets()
            return
        merged = jacs_defaults()
        merged.update(vals)                                # 内置预设只存覆盖值
        self.set_values(merged)
        self._preset_name = name
        label = self._preset_labels.get(name, name)
        self.lbl_preset.setText(f"预设：{label}")
        self._set_status(f"已套用预设「{label}」（未列出的键按默认值）")

    def _on_save_preset(self) -> None:
        """把当前控件值另存为用户预设（拒绝与内置预设重名）。"""
        name, ok = QInputDialog.getText(self, "另存为预设",
                                        "预设名（建议英文/数字/连字符）：")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self._builtin_presets:
            self._set_status(f"⚠「{name}」是内置预设名，请换一个（内置预设不可覆盖）")
            return
        if name in self._user_presets:
            ans = QMessageBox.question(
                self, "另存为预设", f"用户预设「{name}」已存在，覆盖它？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        bad = set('/\\:*?"<>|') & set(name)
        if bad:
            self._set_status("⚠ 预设名不能包含：" + "、".join(sorted(bad)))
            return
        try:
            path = save_user_preset(name, self.values())
        except Exception as err:                           # noqa: BLE001
            QMessageBox.critical(self, "另存为预设", f"保存失败：{err}")
            return
        self._preset_name = name
        self._refresh_presets()
        self.lbl_preset.setText(f"预设：{self._preset_labels.get(name, name)}")
        self._set_status(f"已保存用户预设「{name}」→ {path}")

    def _on_delete_preset(self) -> None:
        """删除一个用户预设（内置预设不可删）。"""
        if not self._user_presets:
            self._set_status("当前没有用户预设可删除（内置预设不可删）")
            return
        names = sorted(self._user_presets)
        labels = [self._preset_labels.get(n, n) for n in names]
        chosen, ok = QInputDialog.getItem(self, "删除用户预设",
                                          "选择要删除的用户预设：", labels, 0, False)
        if not ok or chosen not in labels:
            return
        name = names[labels.index(chosen)]
        ans = QMessageBox.question(
            self, "删除用户预设", f"确定删除用户预设「{name}」？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        try:
            done = delete_user_preset(name)
        except Exception as err:                           # noqa: BLE001
            QMessageBox.critical(self, "删除用户预设", f"删除失败：{err}")
            return
        if self._preset_name == name:
            self._preset_name = None
            self.lbl_preset.setText("预设：未选择")
        self._refresh_presets()
        self._set_status(f"已删除用户预设「{name}」" if done
                         else f"没找到用户预设「{name}」")

    # ══════════════ 样板图预览 ══════════════
    def _preview_inches(self, width_in: float, height_in: float) -> tuple[float, float]:
        """预览显示尺寸：按 ``width_in``/``height_in`` 等比缩放到面板内。"""
        w = max(1.0, min(float(width_in), 20.0))
        h = max(0.8, min(float(height_in), 20.0))
        scale = min(1.0, _PREVIEW_MAX_W / w, _PREVIEW_MAX_H / h)
        return max(1.0, w * scale), max(0.8, h * scale)

    def _new_preview_figure(self, width_in: float, height_in: float) -> Any:
        """换上一张**全新** Figure —— 某个键写坏时不会留下"中毒"的轴对象。"""
        fig = Figure(figsize=(width_in, height_in), dpi=_PREVIEW_DPI)
        fig._original_dpi = fig.dpi                       # 与 FigureCanvasBase 初始化保持一致
        fig.patch.set_facecolor("white")
        fig.set_layout_engine("constrained")
        fig.set_canvas(self._canvas)
        self._canvas.figure = fig
        self._fig = fig
        return fig

    def _preview_error(self, msg: str) -> None:
        """在预览区显示中文提示（不弹窗；本方法自身绝不抛异常）。"""
        log.warning("样板图预览不可用：%s", msg)
        if self._canvas is None:
            return
        try:
            fig = self._new_preview_figure(_PREVIEW_MAX_W, _PREVIEW_MAX_H)
            ax = fig.add_subplot(111)
            ax.set_axis_off()
            ax.text(0.5, 0.5, msg, ha="center", va="center", wrap=True,
                    transform=ax.transAxes, color="crimson", fontsize=8)
            self._canvas.setFixedSize(int(_PREVIEW_MAX_W * _PREVIEW_DPI) + 2,
                                      int(_PREVIEW_MAX_H * _PREVIEW_DPI) + 2)
            self._canvas.draw_idle()
        except Exception as err:                           # noqa: BLE001 —— 兜底中的兜底
            log.debug("预览占位提示绘制失败：%s", err)
        self._set_status("⚠ " + msg)

    def _draw_preview(self, *_ignored: Any) -> None:
        """按当前控件值重绘样板图（3 条高斯谱 + 灰虚线探针 + 趋势箭头 + 标注文字）。

        只读取主要键（画布尺寸 / 字号 / 线宽 / 框 / 刻度 / 色带 / 标注 / 箭头），
        轴样式交给 :func:`core.plotting_jacs.finish_axes`，保证与真实出图一致。
        """
        if self._canvas is None:
            return
        try:
            from core.plotting_jacs import finish_axes, jcfg
        except Exception as err:                           # noqa: BLE001
            self._preview_error(f"无法加载 core.plotting_jacs：{err}")
            return
        try:
            vals = self.values()
            cfg = {"plot": {"jacs": vals}}
            j = jcfg(cfg)                                  # 与真实渲染共用取值逻辑
            w_in = float(j.get("width_in") or 3.25)
            h_in = float(j.get("height_in") or 2.5)
            disp_w, disp_h = self._preview_inches(w_in, h_in)
            if str(j.get("tick_direction")) not in ("in", "out", "inout"):
                log.debug("刻度方向 %r 非法，预览按 in 绘制", j.get("tick_direction"))
                cfg["plot"]["jacs"]["tick_direction"] = "in"
            fig = self._new_preview_figure(disp_w, disp_h)
            ax = fig.add_subplot(111)

            # ── 波长轴与示意谱线（300–700 nm 上的高斯带，浅→深 = 早→晚）──
            xlim = j.get("xlim") or [300.0, 700.0]
            try:
                x0, x1 = float(xlim[0]), float(xlim[1])
            except (TypeError, ValueError, IndexError):
                x0, x1 = 300.0, 700.0
            if not x1 > x0:                                # 区间非法时回退，预览照样能看
                x0, x1 = 300.0, 700.0
            peaks = ((432.0, 34.0, 1.00), (521.0, 44.0, 0.78), (607.0, 50.0, 0.55))
            xs = [x0 + (x1 - x0) * i / 239.0 for i in range(240)]
            colors = _cmap_colors(str(j.get("cmap", "Blues")),
                                  float(j.get("cmap_lo", 0.30)),
                                  float(j.get("cmap_hi", 0.95)), len(peaks))
            lw = float(j.get("curve_lw", 1.3))
            y_top = 0.0
            for i, (mu, sig, amp) in enumerate(peaks):
                ys = [amp * math.exp(-0.5 * ((x - mu) / sig) ** 2) for x in xs]
                y_top = max(y_top, max(ys))
                ax.plot(xs, ys, color=colors[i], lw=lw, label=f"{i * 10} min")
            y_top = max(y_top, 1e-6) * (1.0 + max(0.0, float(
                j.get("ylim_top_headroom", 0.05))))
            try:                                           # 图例位置串写错也不能毁了预览
                ax.legend(fontsize=float(j.get("legend_size", 6.5)),
                          loc=str(j.get("legend_loc", "best")), frameon=False)
            except Exception as err:                       # noqa: BLE001
                log.debug("预览图例绘制失败：%s", err)

            # ── 灰色虚线探针 + 一行水平标注文字 ──
            annot_size = float(j.get("annot_size", 7.5))
            annot_color = str(j.get("annot_color", "0.25"))
            if bool(j.get("mark_probe", True)):
                ax.axvline(self._probe_nm, color="0.45", ls="--",
                           lw=float(j.get("annot_lw", 0.8)), zorder=1)
                ax.text(self._probe_nm + float(j.get("probe_label_dx_nm", 4.8)),
                        y_top * float(j.get("probe_label_y_frac", 0.985)),
                        f"{self._probe_nm:g} nm", fontsize=annot_size,
                        color=annot_color, va="top")
            band_label = str(j.get("fluorophore_label", "auto"))
            if band_label == "auto":
                # G21（第二轮审查）：这里原来硬编码 "Fluorescein" —— 与"无硬编码
                # 染料名"的红线精神冲突（真实渲染路径走 dyes 注册表，见
                # plotting_jacs._fluorophore_label）。预览示意图不该暗示某个具体染料，
                # 换成中性占位；想改文字用 plot.jacs.fluorophore_label 显式指定。
                band_label = "Dye A"
            if band_label and bool(j.get("mark_fluorophore", True)):
                ax.text(520.0 + float(j.get("fluorophore_label_dx_nm", 6.0)),
                        y_top * (1.0 - float(j.get("fluorophore_label_dy_frac", 0.055))),
                        band_label, fontsize=annot_size, color=annot_color,
                        va="bottom")

            # ── 竖直趋势箭头（取代 colorbar）──
            if bool(j.get("trend_arrow", True)):
                try:
                    span = j.get("arrow_x_range")
                    if isinstance(span, (list, tuple)) and len(span) == 2:
                        xa = (float(span[0]) + float(span[1])) / 2.0
                    else:                                  # data / 异常 → 取右侧空档
                        xa = x0 + 0.82 * (x1 - x0)
                    y_lo = y_top * float(j.get("arrow_y_lo_frac", 0.28))
                    y_hi = y_top * float(j.get("arrow_y_hi_frac", 0.82))
                    if y_hi > y_lo:
                        ax.annotate("", xy=(xa, y_hi), xytext=(xa, y_lo),
                                    arrowprops=dict(
                                        arrowstyle=str(j.get("arrow_style", "-|>")),
                                        color=str(j.get("arrow_color", "0.25")),
                                        lw=float(j.get("annot_lw", 0.8))))
                        ax.text(xa + (x1 - x0) * float(j.get("arrow_label_dx_frac", 0.02)),
                                (y_lo + y_hi) / 2.0, str(j.get("arrow_label", "t")),
                                fontsize=annot_size, color=annot_color, va="center")
                except Exception as err:                   # noqa: BLE001
                    log.debug("预览箭头绘制失败：%s", err)

            finish_axes(ax, cfg, xlabel=str(j.get("xlabel", "Wavelength / nm")),
                        ylabel=str(j.get("ylabel", "Absorbance")),
                        xlim=[x0, x1], ylim=[0.0, y_top])
            self._canvas.setFixedSize(int(disp_w * _PREVIEW_DPI) + 2,
                                      int(disp_h * _PREVIEW_DPI) + 2)
            self._canvas.draw_idle()
        except Exception as err:                           # noqa: BLE001
            log.debug("样板图预览绘制失败", exc_info=True)
            self._preview_error(f"绘制预览失败：{err}")
