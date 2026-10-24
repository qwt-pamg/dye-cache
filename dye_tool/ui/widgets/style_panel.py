# -*- coding: utf-8 -*-
"""V2.5 FR-33：实时参数编辑风格栏（Origin Plot Details 对标）。

边界（设计文档 §4）：

* **实时 = 重绘，不是重渲**：编辑只改活画布 artist 属性并 draw_idle，
  **绝不触发** run_stack / run_spectra_plots（A33.2 用 AST 结构断言钉住）；
* 防抖 ≤150 ms（QTimer，默认 120 ms）；
* per-curve 覆写**仅会话级**（不写 cfg、不进生图产物）；关闭会话/重载即失效；
* 「写入出图参数 / 另存为预设」是**显式桥接**：只桥接可映射键
  （JACS_SCHEMA + spectra 节），不可映射项明示"仅工作视图"。
"""
from __future__ import annotations

import math
from typing import Optional

from matplotlib.colors import is_color_like, to_hex

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from ui import theme

#: 防抖窗口（ms）——设计文档 FR-33.2 要求 ≤150
DEBOUNCE_MS = 120

#: 线型/标记下拉项（值 = matplotlib 属性）
LINESTYLES = (("-", "实线"), ("--", "虚线"), ("-.", "点划线"), (":", "点线"))
MARKERS = (("", "无"), ("o", "圆"), ("s", "方"), ("^", "三角"), ("D", "菱形"))

#: 曲线表列
CURVE_HEADERS = ["曲线", "颜色", "线型", "线宽(pt)", "标记", "标记大小"]

#: 可映射键（JACS_SCHEMA + spectra 节可桥接的 7 个；FR-33.1）
MAPPED_KEYS = ("cmap_lo", "cmap_hi", "marker_color", "marker_size",
               "marker_edge_w", "legend_size", "legend_loc")

#: 不可映射项清单（per-curve 任意覆写 → 明示"仅工作视图"）
ONLY_WORK_VIEW_NOTE = ("per-curve 覆写（颜色/线型/线宽/标记）**仅工作视图生效**，"
                       "不进生图产物；可映射键用「写入出图参数」桥接。")

#: spectra 可映射键（V25-D1：FR-33.1 要求；点分键 = cfg 内的真实路径）
SPECTRA_MAPPED_KEYS = ("abs_color", "em_color", "spectra_overlay.cmap")

#: 可映射数值键的允许范围（写入前校验；与 JACS_SCHEMA 的 min/max 对齐）
MAPPED_RANGES = {"cmap_lo": (0.0, 1.0), "cmap_hi": (0.0, 1.0),
                 "marker_size": (0.0, 20.0), "marker_edge_w": (0.0, 5.0),
                 "legend_size": (3.0, 30.0)}

#: 可映射颜色键（写入前要求非空且 matplotlib 能解析）
COLOR_MAPPED_KEYS = ("marker_color", "abs_color", "em_color")

#: V2.6 FR-40.6：会话级样式撤销栈上限（与参数面板同口径）
HISTORY_LIMIT = 20

#: 曲线样式字段（与 live_plot.DEFAULT_STYLE 同义；顺序即撤销记录顺序）
STYLE_FIELDS = ("color", "linewidth", "linestyle", "marker", "markersize")

#: 会话级"初始"样式基准：除颜色外都等于 live_plot.DEFAULT_STYLE
_DEFAULT_NON_COLOR = {"linewidth": 1.0, "linestyle": "-", "marker": "",
                      "markersize": 4.0}


class StylePanel(QWidget):
    """曲线级实时编辑 + 可映射键桥接（FR-33.1）。"""

    style_changed = Signal(str, dict)      # (curve_key, {color,linewidth,...})
    edit_rejected = Signal(str)            # 非法输入被拒绝（页面接状态栏/日志）
    mapped_changed = Signal(dict)          # 可映射键即时预览（会话级）
    spectra_changed = Signal(dict)         # spectra 可映射键即时预览（V25-D1）
    write_requested = Signal(dict)         # 「写入出图参数」显式动作
    preset_requested = Signal(dict)        # 「另存为预设」显式动作
    advanced_requested = Signal()          # 「高级…」跳转既有出图对话框
    #: V2.6 FR-40.6：「重置全部」→ 页面据此调用 live_plot.clear_styles()
    reset_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 with_spectra: bool = False):
        super().__init__(parent)
        #: 是否显示 spectra 可映射键区（作图台 ④ 用；stack 页只桥接 JACS）
        self.with_spectra = bool(with_spectra)
        #: V25-B3：用户改过可映射键 → 载入/刷新时不再用 cfg 覆盖
        self.mapped_dirty = False
        #: V25-D1：spectra 键同理
        self.spectra_dirty = False
        self._filling = False
        self._keys: list[str] = []
        self._editors: dict[str, dict] = {}
        self._pending: dict[str, dict] = {}
        #: V2.6 FR-40.6：样式撤销/重置（会话级）
        self._initial_styles: dict[str, dict] = {}   # 每条曲线的初始样式
        self._known_styles: dict[str, dict] = {}     # 当前已知样式（flush 后同步）
        self._style_history: list[tuple[str, dict]] = []   # (key, 修改前样式)
        #: 页面已接线 reset_requested → live_plot.clear_styles()（复位走该信号）
        self._has_reset_handler = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(DEBOUNCE_MS)
        self._timer.timeout.connect(self.flush)
        self._build()

    # ═══════════════════ UI ═══════════════════
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.lbl_note = QLabel(ONLY_WORK_VIEW_NOTE)
        self.lbl_note.setWordWrap(True)
        theme.mark(self.lbl_note, "hint")
        lay.addWidget(self.lbl_note)

        self.tbl = QTableWidget(0, len(CURVE_HEADERS))
        self.tbl.setHorizontalHeaderLabels(CURVE_HEADERS)
        self.tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl.verticalHeader().setVisible(False)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl.setMinimumHeight(110)
        self.tbl.setMaximumHeight(190)
        lay.addWidget(self.tbl)

        box_m = QGroupBox("可映射键（写入出图参数 / 另存为预设才进 cfg/presets）")
        form = QFormLayout(box_m)
        self.sp_lo = QDoubleSpinBox()
        self.sp_lo.setRange(0.0, 1.0)
        self.sp_lo.setSingleStep(0.01)
        self.sp_lo.setDecimals(2)
        self.sp_hi = QDoubleSpinBox()
        self.sp_hi.setRange(0.0, 1.0)
        self.sp_hi.setSingleStep(0.01)
        self.sp_hi.setDecimals(2)
        self.ed_color = QLineEdit()
        self.ed_color.setPlaceholderText("如 #1f4e96")
        self.sp_size = QDoubleSpinBox()
        self.sp_size.setRange(0.0, 20.0)
        self.sp_size.setSingleStep(0.5)
        self.sp_edge = QDoubleSpinBox()
        self.sp_edge.setRange(0.0, 5.0)
        self.sp_edge.setSingleStep(0.1)
        self.sp_legend = QDoubleSpinBox()
        self.sp_legend.setRange(3.0, 30.0)          # 与 JACS_SCHEMA hi=30 对齐
        self.sp_legend.setSingleStep(0.5)
        self.ed_legend_loc = QLineEdit()
        self.ed_legend_loc.setPlaceholderText("如 best / upper right")
        form.addRow("cmap_lo", self.sp_lo)
        form.addRow("cmap_hi", self.sp_hi)
        form.addRow("marker_color", self.ed_color)
        form.addRow("marker_size", self.sp_size)
        form.addRow("marker_edge_w", self.sp_edge)
        form.addRow("legend_size", self.sp_legend)
        form.addRow("legend_loc", self.ed_legend_loc)
        for w in (self.sp_lo, self.sp_hi, self.sp_size, self.sp_edge,
                  self.sp_legend):
            w.valueChanged.connect(lambda *_: self._emit_mapped())
        for w in (self.ed_color, self.ed_legend_loc):
            w.editingFinished.connect(lambda *_: self._emit_mapped())
        lay.addWidget(box_m)
        if self.with_spectra:
            self._build_spectra_box(lay)

        # V2.6 FR-38.3 + Lead 仲裁（A38.2）：动作行做成独立 widget，页面可
        # 调用 take_actions_widget() 取走做 box_style 的**固定页脚**——滚动区只
        # 承载说明/曲线表/可映射键，关键按钮始终可见（不被窗口裁剪）。
        actions = QWidget()
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        self.btn_write = QPushButton("写入出图参数")
        self.btn_write.setToolTip(
            "把可映射键写入 cfg 的 plot 节并保存（白名单/F3 护栏不变）→\n"
            "Φ_Δ 页会提示「重渲受影响图」；不可映射项不会写入")
        self.btn_preset = QPushButton("另存为预设…")
        self.btn_preset.setToolTip(
            "把可映射键另存为 presets.yaml v2 的 plot 预设（V2.4 机制复用）；\n"
            "套用后工作视图同步反映该预设")
        self.btn_advanced = QPushButton("高级…")
        self.btn_advanced.setToolTip("打开既有出图风格对话框（完整 JACS_SCHEMA）")
        self.btn_write.clicked.connect(
            lambda: self.write_requested.emit(self.write_values()))
        self.btn_preset.clicked.connect(
            lambda: self.preset_requested.emit(self.mapped_values()))
        self.btn_advanced.clicked.connect(lambda: self.advanced_requested.emit())
        # V2.6 FR-40.6：撤销上一步 / 重置当前曲线 / 重置全部（会话级，不写 cfg）
        self.btn_undo = QPushButton("撤销上一步")
        self.btn_undo.setToolTip(
            "撤销最近一次曲线样式编辑（颜色/线型/线宽/标记；会话级，不进 cfg）")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self.undo_last)
        self.btn_reset_current = QPushButton("重置当前曲线")
        self.btn_reset_current.setToolTip(
            "把表格中选中曲线的会话级样式恢复到载入时的初始值")
        self.btn_reset_current.clicked.connect(self.reset_current)
        self.btn_reset_all = QPushButton("重置全部")
        self.btn_reset_all.setToolTip(
            "清空全部曲线的会话级样式覆写（复用 live_plot.clear_styles()）")
        self.btn_reset_all.clicked.connect(self.reset_all)
        row.addWidget(self.btn_write)
        row.addWidget(self.btn_preset)
        row.addWidget(self.btn_advanced)
        row.addWidget(self.btn_undo)
        row.addWidget(self.btn_reset_current)
        row.addWidget(self.btn_reset_all)
        row.addStretch(1)
        self.actions_widget = actions
        lay.addWidget(actions)

    # ═══════════════════ V2.6：动作行 → 固定页脚 ═══════════════════
    def take_actions_widget(self) -> QWidget:
        """取走动作行（写入出图参数 / 另存为预设 / 高级）并返回，供页面做固定页脚。

        FR-38.3 要求面板主体可滚动；A38.2 又要求关键按钮始终可见且在窗口内 ——
        页面取走后把本 widget 放在 QScrollArea **之外**（box_style 底部，stretch 0）。
        按钮的信号都连在 StylePanel 的方法上，重新挂载父级不影响连接。
        """
        w = self.actions_widget
        lay = self.layout()
        if lay is not None:
            lay.removeWidget(w)
        w.setParent(None)
        return w

    def _build_spectra_box(self, lay) -> None:
        """spectra 可映射键区（V25-D1；仅作图台 ④ 启用）。"""
        box_s = QGroupBox("spectra 可映射键（写入出图参数才进 cfg）")
        fs = QFormLayout(box_s)
        self.ed_abs_color = QLineEdit()
        self.ed_abs_color.setPlaceholderText("如 #1f4e79")
        self.ed_em_color = QLineEdit()
        self.ed_em_color.setPlaceholderText("如 #C81E1E")
        self.ed_spectra_cmap = QLineEdit()
        self.ed_spectra_cmap.setPlaceholderText("如 viridis")
        fs.addRow("abs_color", self.ed_abs_color)
        fs.addRow("em_color", self.ed_em_color)
        fs.addRow("spectra_overlay.cmap", self.ed_spectra_cmap)
        for w in (self.ed_abs_color, self.ed_em_color, self.ed_spectra_cmap):
            w.editingFinished.connect(lambda *_: self._emit_spectra())
        lay.addWidget(box_s)

    # ═══════════════════ 曲线列表 ═══════════════════
    def set_curves(self, curves: list) -> None:
        """用当前视图曲线刷新表（FR-33.1：曲线列表与视图一致）。"""
        curves = [c for c in (curves or []) if isinstance(c, dict)]
        self._filling = True
        try:
            self._timer.stop()
            self._pending = {}
            self._keys = [str(c.get("key")) for c in curves]
            self._editors = {}
            self.tbl.setRowCount(len(curves))
            for r, c in enumerate(curves):
                key = str(c.get("key"))
                item = QTableWidgetItem(str(c.get("label") or key))
                item.setData(Qt.UserRole, key)
                self.tbl.setItem(r, 0, item)
                ed_color = QLineEdit(self._color_text(c.get("color")))
                ed_color.setMaximumWidth(110)
                ed_color.editingFinished.connect(
                    lambda k=key, w=ed_color: self._on_edit(k, "color", w.text()))
                self.tbl.setCellWidget(r, 1, ed_color)
                cb_line = QComboBox()
                for val, label in LINESTYLES:
                    cb_line.addItem(label, val)
                self._select_data(cb_line, str(c.get("linestyle") or "-"))
                cb_line.currentIndexChanged.connect(
                    lambda _i, k=key, w=cb_line: self._on_edit(
                        k, "linestyle", w.currentData()))
                self.tbl.setCellWidget(r, 2, cb_line)
                sp_lw = QDoubleSpinBox()
                sp_lw.setRange(0.1, 10.0)
                sp_lw.setSingleStep(0.1)
                sp_lw.setDecimals(1)
                sp_lw.setValue(float(c.get("linewidth") or 1.0))
                sp_lw.valueChanged.connect(
                    lambda v, k=key: self._on_edit(k, "linewidth", v))
                self.tbl.setCellWidget(r, 3, sp_lw)
                cb_mark = QComboBox()
                for val, label in MARKERS:
                    cb_mark.addItem(label, val)
                self._select_data(cb_mark, str(c.get("marker") or ""))
                cb_mark.currentIndexChanged.connect(
                    lambda _i, k=key, w=cb_mark: self._on_edit(
                        k, "marker", w.currentData()))
                self.tbl.setCellWidget(r, 4, cb_mark)
                sp_ms = QDoubleSpinBox()
                sp_ms.setRange(0.0, 20.0)
                sp_ms.setSingleStep(0.5)
                sp_ms.setValue(float(c.get("markersize") or 4.0))
                sp_ms.valueChanged.connect(
                    lambda v, k=key: self._on_edit(k, "markersize", v))
                self.tbl.setCellWidget(r, 5, sp_ms)
                self._editors[key] = {"color": ed_color, "linestyle": cb_line,
                                      "linewidth": sp_lw, "marker": cb_mark,
                                      "markersize": sp_ms}
        finally:
            self._filling = False
        # V2.6 FR-40.6：同步"初始/当前"样式（撤销与重置的基准）
        self._sync_styles_from_curves(curves)

    @staticmethod
    def _select_data(cmb: QComboBox, value: str) -> None:
        for i in range(cmb.count()):
            if str(cmb.itemData(i)) == str(value):
                cmb.setCurrentIndex(i)
                return

    def curve_keys(self) -> list:
        return list(self._keys)

    def editor(self, key: str, name: str):
        """测试/高级脚本入口：取某曲线某属性的编辑控件。"""
        return (self._editors.get(key) or {}).get(name)

    # ═══════════════════ 编辑 → 防抖 → 重绘 ═══════════════════
    def _on_edit(self, key: str, name: str, value) -> None:
        if self._filling:
            return
        if name == "color":
            # V25-B4：非法颜色在**编辑时**就拦下并提示，不把 ValueError 带进重绘
            color, err = self._normalize_color(value)
            if err is not None:
                self.edit_rejected.emit(err)
                return
            value = color
            editor = self._editors.get(key, {}).get("color")
            if editor is not None and editor.text() != color:
                editor.setText(color)                    # 回显规整后的 hex
        self._pending.setdefault(key, {})[name] = value
        self._timer.start()                              # 防抖 ≤150 ms

    def flush(self) -> None:
        """立即把待生效的编辑发出去（测试/收尾用；确定性触发）。"""
        self._timer.stop()
        pending, self._pending = self._pending, {}
        for key, style in pending.items():
            old = dict(self._known_styles.get(key)
                       or self._initial_styles.get(key)
                       or self._default_full_style())
            self._style_history.append((key, old))
            if len(self._style_history) > HISTORY_LIMIT:
                del self._style_history[0]
            merged = dict(old)
            merged.update(dict(style))
            self._known_styles[key] = merged
            self.style_changed.emit(key, dict(style))
        self._update_undo_buttons()

    def pending_count(self) -> int:
        return len(self._pending)

    # ═══════════════ V2.6 FR-40.6：样式撤销 / 重置（会话级） ═══════════════
    @staticmethod
    def _default_full_style() -> dict:
        return {"color": "", **dict(_DEFAULT_NON_COLOR)}

    def _sync_styles_from_curves(self, curves: list) -> None:
        """刷新"初始/当前"样式基线（撤销/重置的基准）。

        初始样式 = 曲线"无覆写"时的着色与默认线型。页面重载数据时会先
        live.clear_styles() 再重建 → 本方法据"历史有覆写、当前无覆写"识别
        重载并刷新初始颜色。
        """
        keys: list[str] = []
        for c in curves:
            key = str(c.get("key"))
            keys.append(key)
            full = {"color": self._color_text(c.get("color")),
                    "linewidth": float(c.get("linewidth") or 1.0),
                    "linestyle": str(c.get("linestyle") or "-"),
                    "marker": str(c.get("marker") or ""),
                    "markersize": float(c.get("markersize") or 4.0)}
            pristine = all(full[f] == v for f, v in _DEFAULT_NON_COLOR.items())
            old_known = self._known_styles.get(key)
            old_initial = self._initial_styles.get(key)
            if old_initial is None or old_known == old_initial:
                self._initial_styles[key] = dict(full)
            elif pristine and old_known != full:
                self._initial_styles[key] = dict(full)   # 页面重载过 → 初始更新
            self._known_styles[key] = dict(full)
        stale = [k for k in self._initial_styles if k not in keys]
        for k in stale:
            self._initial_styles.pop(k, None)
            self._known_styles.pop(k, None)
        if stale:
            self._style_history = [(k, s) for k, s in self._style_history
                                   if k in keys]
        self._update_undo_buttons()

    def undo_depth(self) -> int:
        """当前可撤销的样式编辑步数（V2.6 FR-40.6，上限 HISTORY_LIMIT）。"""
        return len(self._style_history)

    def mark_reset_handler(self) -> None:
        """页面已接线 reset_requested（→ live_plot.clear_styles()）时调用。"""
        self._has_reset_handler = True

    def clear_history(self) -> None:
        """清空撤销历史（页面重置全部后调用）。"""
        self._style_history = []
        self._update_undo_buttons()

    def _update_undo_buttons(self) -> None:
        has = bool(self._style_history)
        btn = getattr(self, "btn_undo", None)
        if btn is not None:
            btn.setEnabled(has)
        has_keys = bool(self._keys)
        for name in ("btn_reset_current", "btn_reset_all"):
            b = getattr(self, name, None)
            if b is not None:
                b.setEnabled(has_keys)

    def _current_curve_key(self):
        row = self.tbl.currentRow()
        if 0 <= row < len(self._keys):
            return self._keys[row]
        return self._keys[0] if self._keys else None

    def undo_last(self) -> bool:
        """撤销上一步曲线样式编辑（恢复修改前整条样式）；无历史返回 False。"""
        if not self._style_history:
            return False
        key, old = self._style_history.pop()
        self._known_styles[key] = dict(old)
        self.style_changed.emit(key, dict(old))
        self._update_undo_buttons()
        return True

    def reset_current(self) -> bool:
        """把当前曲线恢复到载入时的初始样式（会话级）。"""
        key = self._current_curve_key()
        if key is None or key not in self._initial_styles:
            return False
        old = dict(self._initial_styles[key])
        self._known_styles[key] = dict(old)
        self._style_history = [(k, s) for k, s in self._style_history
                               if k != key]
        self.style_changed.emit(key, dict(old))
        self._update_undo_buttons()
        return True

    def reset_all(self) -> bool:
        """全部重置：发 reset_requested → 页面调 live_plot.clear_styles()。

        standalone（未接线）时退化为逐曲线恢复初始样式，保证单元测试/脚本可用。
        """
        if not self._keys:
            return False
        self._style_history = []
        self._known_styles = {k: dict(v) for k, v in self._initial_styles.items()}
        self.reset_requested.emit()
        if not self._has_reset_handler:
            for key in list(self._keys):
                st = self._initial_styles.get(key)
                if st:
                    self.style_changed.emit(key, dict(st))
        self._update_undo_buttons()
        return True

    # ═══════════════════ 可映射键（桥接两档） ═══════════════════
    def mapped_values(self) -> dict:
        return {"cmap_lo": float(self.sp_lo.value()),
                "cmap_hi": float(self.sp_hi.value()),
                "marker_color": self.ed_color.text().strip(),
                "marker_size": float(self.sp_size.value()),
                "marker_edge_w": float(self.sp_edge.value()),
                "legend_size": float(self.sp_legend.value()),
                "legend_loc": self.ed_legend_loc.text().strip()}

    def write_values(self) -> dict:
        """「写入出图参数」的完整载荷（JACS 7 键 + spectra 3 键，若启用）。"""
        values = self.mapped_values()
        if self.with_spectra:
            values.update(self.spectra_values())
        return values

    def set_mapped_values(self, values: dict) -> None:
        """程序化设置可映射键（预设/高级对话框套用 = 用户显式动作 → 标脏）。"""
        self._apply_mapped(values)
        self.mapped_dirty = True

    def seed_mapped_values(self, values: dict) -> bool:
        """用 cfg 播种可映射键（V25-B3）；用户编辑过则不覆盖，返回是否生效。"""
        if self.mapped_dirty:
            return False
        self._apply_mapped(values)
        return True

    def _apply_mapped(self, values: dict) -> None:
        values = values if isinstance(values, dict) else {}
        self._filling = True
        try:
            def _f(name: str, default: float) -> float:
                try:
                    return float(values.get(name, default))
                except (TypeError, ValueError):
                    return default
            self.sp_lo.setValue(_f("cmap_lo", 0.30))
            self.sp_hi.setValue(_f("cmap_hi", 0.95))
            self.sp_size.setValue(_f("marker_size", 6.0))
            self.sp_edge.setValue(_f("marker_edge_w", 0.8))
            self.sp_legend.setValue(_f("legend_size", 7.0))
            if values.get("marker_color") is not None:
                self.ed_color.setText(str(values.get("marker_color")))
            if values.get("legend_loc") is not None:
                self.ed_legend_loc.setText(str(values.get("legend_loc")))
        finally:
            self._filling = False

    # ── spectra 可映射键（V25-D1） ──
    def spectra_values(self) -> dict:
        """spectra 可映射键（面板未启用 spectra 区时返回空 dict）。"""
        if not self.with_spectra:
            return {}
        return {"abs_color": self.ed_abs_color.text().strip(),
                "em_color": self.ed_em_color.text().strip(),
                "spectra_overlay.cmap": self.ed_spectra_cmap.text().strip()}

    def set_spectra_values(self, values: dict) -> None:
        """程序化设置 spectra 可映射键（预设/高级套用 = 用户显式动作）。"""
        self._apply_spectra(values)
        self.spectra_dirty = True

    def seed_spectra_values(self, values: dict) -> bool:
        """用 cfg 播种 spectra 可映射键；用户编辑过则不覆盖。"""
        if not self.with_spectra:
            return False
        if self.spectra_dirty:
            return False
        self._apply_spectra(values)
        return True

    def _apply_spectra(self, values: dict) -> None:
        if not self.with_spectra:
            return
        values = values if isinstance(values, dict) else {}
        self._filling = True
        try:
            if values.get("abs_color") is not None:
                self.ed_abs_color.setText(str(values["abs_color"]))
            if values.get("em_color") is not None:
                self.ed_em_color.setText(str(values["em_color"]))
            if values.get("spectra_overlay.cmap") is not None:
                self.ed_spectra_cmap.setText(
                    str(values["spectra_overlay.cmap"]))
        finally:
            self._filling = False

    @staticmethod
    def validate_values(values: dict) -> tuple[bool, str]:
        """写入前校验（V25-B3）：数值范围、cmap_lo<cmap_hi、颜色可解析。"""
        v = dict(values or {})
        for name, (lo, hi) in MAPPED_RANGES.items():
            if name not in v:
                continue
            try:
                x = float(v[name])
            except (TypeError, ValueError):
                return False, f"{name} 不是数字：{v[name]!r}"
            if not math.isfinite(x) or not (lo <= x <= hi):
                return False, f"{name}={x:g} 超出允许范围 {lo:g}–{hi:g}"
        if "cmap_lo" in v and "cmap_hi" in v:
            try:
                lo = float(v["cmap_lo"])
                hi = float(v["cmap_hi"])
            except (TypeError, ValueError):
                return False, "色带区间不是数字"
            if not (0.0 <= lo < hi <= 1.0):
                return False, (f"色带区间要求 0 ≤ cmap_lo < cmap_hi ≤ 1"
                               f"（当前 {lo:g} / {hi:g}）")
        for name in COLOR_MAPPED_KEYS:
            if name not in v:
                continue
            txt = str(v[name] or "").strip()
            if not txt:
                return False, f"{name} 不能为空（请填 #RRGGBB 或颜色名）"
            if not is_color_like(txt):
                return False, f"{name} 颜色无法解析：{txt!r}"
        if "legend_loc" in v:
            txt = str(v["legend_loc"] or "").strip()
            if not txt:
                return False, "legend_loc 不能为空（如 best / upper right）"
        cmap = str(v.get("spectra_overlay.cmap") or "").strip()
        if cmap:
            from matplotlib import colormaps
            if cmap not in colormaps:
                return False, f"spectra_overlay.cmap 不是合法色带：{cmap!r}"
        return True, ""

    @staticmethod
    def _color_text(raw) -> str:
        """颜色 → hex 文本（V25-B4：避免 tuple repr 填进编辑框）。"""
        if raw is None:
            return ""
        try:
            if is_color_like(raw):
                return to_hex(raw)
        except (TypeError, ValueError):
            pass
        return str(raw)

    @staticmethod
    def _normalize_color(raw) -> tuple[str, Optional[str]]:
        """颜色文本 → (规整 hex, 错误说明)；空串 = 未填，error=None。"""
        txt = str(raw or "").strip()
        if not txt:
            return "", None
        if not is_color_like(txt):
            return "", f"颜色无法解析：{txt!r}（已忽略；请用 #RRGGBB 或颜色名）"
        try:
            return to_hex(txt), None
        except (TypeError, ValueError):
            return txt, None

    def _emit_mapped(self) -> None:
        if self._filling:
            return
        self.mapped_dirty = True
        self.mapped_changed.emit(self.mapped_values())

    def _emit_spectra(self) -> None:
        if self._filling:
            return
        self.spectra_dirty = True
        self.spectra_changed.emit(self.spectra_values())
