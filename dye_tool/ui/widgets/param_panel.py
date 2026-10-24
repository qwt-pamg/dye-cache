# -*- coding: utf-8 -*-
"""参数面板（FR-1.3、FR-5）：常用参数外露 + 高级参数折叠。

* 所有控件初值取自 ``core.config.DEFAULT_CFG``（"默认即正确"）；
* ``apply_to(cfg)`` 把界面值写回 cfg 的**深拷贝**，core 不读全局状态；
* "恢复默认" 重新载入 ``DEFAULT_CFG``。
"""
from __future__ import annotations

import copy
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QToolButton, QVBoxLayout, QWidget)

from core.config import default_config
from ui.widgets.filtering import FormRowFilter, collect_form_rows

log = logging.getLogger("dye_tool.ui.param_panel")


class CollapsibleBox(QWidget):
    """可折叠容器：点击标题展开/收起内部控件（高级参数用）。"""

    def __init__(self, title: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.btn = QToolButton(text=title, checkable=True, checked=False)
        self.btn.setStyleSheet("QToolButton { border: none; font-weight: bold; }")
        self.btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn.setArrowType(Qt.RightArrow)
        self.btn.clicked.connect(self._toggle)
        self.content = QWidget()
        self.content.setVisible(False)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.btn)
        lay.addWidget(self.content)

    def _toggle(self) -> None:
        on = self.btn.isChecked()
        self.btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.content.setVisible(on)

    def body(self) -> QWidget:
        """返回用于放控件的容器。"""
        return self.content


def _dspin(lo: float, hi: float, val: float, step: float = 1.0, dec: int = 3,
           suffix: str = "") -> QDoubleSpinBox:
    """构造统一样式的浮点输入框。"""
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(float(val))
    if suffix:
        w.setSuffix(suffix)
    return w



class FormatsPicker(QWidget):
    """V2.9.3 FR-67.3：导出格式多选（png/svg/jpg/pdf），text()/setText() 兼容旧接口。"""
    textChanged = Signal()

    FORMATS = ("png", "svg", "jpg", "pdf")

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QHBoxLayout
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._boxes = {}
        for ext in self.FORMATS:
            cb = QCheckBox(ext)
            cb.setToolTip("输出格式；pdf 仅 JACS/工作台允许，legacy 仍不出 PDF")
            cb.toggled.connect(self._emit)
            self._boxes[ext] = cb
            lay.addWidget(cb)
        lay.addStretch(1)
        self._boxes["png"].setChecked(True)

    def _emit(self, *_):
        self.textChanged.emit()

    def text(self) -> str:
        return ",".join(k for k, cb in self._boxes.items() if cb.isChecked())

    def setText(self, text) -> None:
        want = {s.strip().lstrip(".").lower()
                for s in str(text or "").replace(";", ",").split(",") if s.strip()}
        if not want:
            want = {"png"}
        for k, cb in self._boxes.items():
            cb.blockSignals(True)
            cb.setChecked(k in want)
            cb.blockSignals(False)
        self.textChanged.emit()


class ParamPanel(QWidget):
    """参数面板。``changed`` 信号在任一控件变化时发出。"""

    changed = Signal()

    #: V2.6 FR-40.6：参数撤销栈上限（与设计文档 Q-E14 一致）
    UNDO_LIMIT = 20
    #: 已改控件的动态属性名（浅色高亮；测试/样式表共用）
    MODIFIED_PROP = "e_modified"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cfg = default_config()
        #: 撤销新值栈：[(控件, 修改前值)]，上限 UNDO_LIMIT
        self._undo_stack: list[tuple[QWidget, object]] = []
        self._tracked: list[QWidget] = []
        self._track_values: dict = {}      # 控件 → 最近一次已知值
        self._base_values: dict = {}       # 控件 → 基线值（set_cfg/reset 时刷新）
        self._syncing = False              # 程序化回填期间不记撤销
        self._modified_hits = 0
        self._build()
        self.reset()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # ── 常用参数 ──
        common = QGroupBox("常用参数")
        f1 = QFormLayout(common)
        f1.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.sp_probe = _dspin(150, 1200, 379.0, 1.0, 1, " nm")
        self.sp_win = _dspin(0, 50, 2.0, 0.5, 1, " nm")
        self.sp_exc = _dspin(150, 1200, 532.0, 1.0, 1, " nm")
        self.sp_phiref = _dspin(0.0, 5.0, 0.75, 0.01, 4)
        self.sp_phireferr = _dspin(0.0, 5.0, 0.075, 0.005, 4)
        self.chk_trunc = QCheckBox("残差弯曲时自动截断")
        self.chk_trunc.setChecked(True)
        self.cmb_times = QComboBox()
        self.cmb_times.addItems(["全部时间点", "自定义列表"])
        self.ed_times = QLineEdit()
        self.ed_times.setPlaceholderText("如 1,60,300,600（秒，逗号分隔）")
        self.ed_times.setEnabled(False)
        self.cmb_times.currentIndexChanged.connect(
            lambda i: self.ed_times.setEnabled(i == 1))
        f1.addRow("探针波长 λ_probe", self.sp_probe)
        f1.addRow("探针窗口 ±", self.sp_win)
        f1.addRow("激发波长 λ_ex", self.sp_exc)
        f1.addRow("参比 Φ_ref", self.sp_phiref)
        f1.addRow("Φ_ref 不确定度", self.sp_phireferr)
        f1.addRow("", self.chk_trunc)
        f1.addRow("叠加图时间点", self.cmb_times)
        f1.addRow("", self.ed_times)
        lay.addWidget(common)

        # ── 高级参数（折叠，带检索：FR-24.5） ──
        adv = CollapsibleBox("高级参数（一般无需修改）")
        body = adv.body()
        vb = QVBoxLayout(body)
        vb.setContentsMargins(0, 0, 0, 0)
        self.ed_adv_search = QLineEdit()
        self.ed_adv_search.setObjectName("advSearch")
        self.ed_adv_search.setPlaceholderText(
            "搜索参数：键名 / 中文说明 / tooltip（如 r_max、箭头、饱和）")
        self.ed_adv_search.setClearButtonEnabled(True)
        self.ed_adv_search.setToolTip("过滤高级参数行（Ctrl+F 聚焦）；空串显示全部")
        vb.addWidget(self.ed_adv_search)
        form_host = QWidget()
        f2 = QFormLayout(form_host)
        f2.setRowWrapPolicy(QFormLayout.WrapLongRows)
        vb.addWidget(form_host)
        self.sp_aexerr = _dspin(0, 1, 0.005, 0.001, 4)
        self.cmb_iabs = QComboBox()
        self.cmb_iabs.addItems(["exact", "linear"])
        self.sp_rmin = _dspin(0, 1, 0.30, 0.05, 3)
        self.sp_rmax = _dspin(0, 2, 1.00, 0.05, 3)
        self.sp_truncfrac = _dspin(0.1, 1.0, 0.70, 0.05, 3)
        self.sp_curvp = _dspin(0.0001, 0.5, 0.05, 0.01, 5)
        self.sp_floor = _dspin(0, 1, 0.01, 0.005, 4)
        self.sp_a0lo = _dspin(0, 10, 0.05, 0.01, 3)
        self.sp_a0hi = _dspin(0, 10, 1.50, 0.05, 3)
        self.sp_nearly = QSpinBox()
        self.sp_nearly.setRange(1, 10)
        self.sp_nearly.setValue(2)
        self.sp_sat = _dspin(0, 100, 3.0, 0.5, 2)
        self.chk_baseline = QCheckBox("启用基线校正")
        self.chk_baseline.setChecked(True)
        self.cmb_basemethod = QComboBox()
        self.cmb_basemethod.addItems(["median", "mean", "linear"])
        self.sp_baselo = _dspin(400, 1000, 650.0, 1.0, 1)
        self.sp_basehi = _dspin(400, 1200, 700.0, 1.0, 1)
        self.chk_bg = QCheckBox("启用背景扣除")
        self.chk_bg.setChecked(True)
        self.cmb_bgmode = QComboBox()
        self.cmb_bgmode.addItems(["matched", "mean"])
        self.sp_aglo = _dspin(200, 800, 450.0, 5.0, 1)
        self.sp_aghi = _dspin(200, 900, 650.0, 5.0, 1)
        self.sp_agstep = _dspin(0.5, 50, 5.0, 0.5, 1)
        self.sp_agdmax = _dspin(0.001, 1.0, 0.06, 0.01, 4)
        self.cmb_style = QComboBox()
        self.cmb_style.addItems(["jacs", "legacy"])
        self.cmb_style.setToolTip(
            "jacs = 期刊投稿风格（单栏 82.5 mm / 600 dpi / png，默认）\n"
            "legacy = 迁移前的老版式（仅用于与旧脚本逐像素比对，一般不用改）")
        # FR-8：出图预设下拉（只写出图参数，绝不碰数值计算键）
        self.cmb_preset = QComboBox()
        self._reload_presets()
        self.cmb_preset.setToolTip(
            "一键套用一组出图参数（尺寸/dpi/字号/线宽…）。\n"
            "选择后仍可手工微调；一改就自动跳到「自定义」。\n"
            "预设**不会**改动探针波长、拟合范围等数值参数。")
        self.btn_style_adv = QPushButton("高级…")
        self.btn_style_adv.setToolTip(
            "打开高级出图参数：附录 B 全部键位（标注摆位、箭头、配色、逐图高度…），\n"
            "带样板图预览与中文校验提示。")
        self.btn_style_adv.setMaximumWidth(70)
        self.btn_style_adv.clicked.connect(self._open_style_dialog)
        self.sp_dpi = QSpinBox()
        self.sp_dpi.setRange(72, 1200)
        self.sp_dpi.setValue(600)
        self.ed_formats = FormatsPicker()
        self.chk_transparent = QCheckBox("透明背景（仅 jacs/工作台）")
        self.chk_transparent.setToolTip("默认关=白底；打开后 savefig facecolor=none，config.yaml 不新增键")
        self.cmb_fig_style = QComboBox()
        self.cmb_fig_style.addItem("图 c/d：默认（柱状/森林）", None)
        self.cmb_fig_style.addItem("图 c/d：柱状", "bar")
        self.cmb_fig_style.addItem("图 c/d：曲线", "line")
        self.cmb_fig_style.addItem("图 c/d：散点", "scatter")
        self.cmb_fig_style.setToolTip("仅 jacs 图 c/d；默认形态与 2.7.0 逐位一致")
        self.chk_fig_err = QCheckBox("图 c/d 误差棒")
        self.chk_fig_err.setChecked(True)
        self.chk_fig_annot = QCheckBox("图 c/d 关键值标注")
        self.chk_fig_annot.setToolTip("默认关（版面干净）；打开后 k_obs/Φ_Δ 标在柱/点旁")
        self.cmb_time_unit = QComboBox()
        self.cmb_time_unit.addItems(["s", "min", "h"])
        self.cmb_time_unit.setToolTip("时间演化图横轴单位；只换算显示，Scan.t 仍为秒")
        self.chk_dose = QCheckBox("按累计剂量归一（J/cm²）")
        f2.addRow("A_ex 不确定度", self.sp_aexerr)
        f2.addRow("I_abs 模式", self.cmb_iabs)
        f2.addRow("R 下界 r_min", self.sp_rmin)
        f2.addRow("R 上界 r_max", self.sp_rmax)
        f2.addRow("截断保留比例", self.sp_truncfrac)
        f2.addRow("弯曲检验 p 阈值", self.sp_curvp)
        f2.addRow("探针 a_floor", self.sp_floor)
        f2.addRow("A(0) 下界", self.sp_a0lo)
        f2.addRow("A(0) 上界", self.sp_a0hi)
        f2.addRow("无暗态取最早点数", self.sp_nearly)
        f2.addRow("饱和阈值", self.sp_sat)
        f2.addRow("", self.chk_baseline)
        f2.addRow("基线方法", self.cmb_basemethod)
        f2.addRow("基线窗口起", self.sp_baselo)
        f2.addRow("基线窗口止", self.sp_basehi)
        f2.addRow("", self.chk_bg)
        f2.addRow("背景模式", self.cmb_bgmode)
        f2.addRow("分组母核起", self.sp_aglo)
        f2.addRow("分组母核止", self.sp_aghi)
        f2.addRow("分组网格步长", self.sp_agstep)
        f2.addRow("分组 RMS 阈值", self.sp_agdmax)
        f2.addRow("", self.chk_dose)
        # ── 出图（FR-7/FR-8：预设 + 高级对话框 + 风格/dpi/格式） ──
        preset_row = QWidget()
        pr = QHBoxLayout(preset_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.addWidget(self.cmb_preset, 1)
        self.btn_save_figstyle = QPushButton("存图式…")
        self.btn_save_figstyle.setToolTip("把当前图 c/d 图式/误差棒/透明/标注/时间轴存成用户预设")
        self.btn_save_figstyle.setMaximumWidth(80)
        self.btn_save_figstyle.clicked.connect(self._save_figure_preset)
        pr.addWidget(self.btn_save_figstyle)
        pr.addWidget(self.btn_style_adv)
        f2.addRow("出图预设", preset_row)
        f2.addRow("绘图风格", self.cmb_style)
        f2.addRow("导出 dpi", self.sp_dpi)
        f2.addRow("导出格式", self.ed_formats)
        f2.addRow("", self.chk_transparent)
        f2.addRow("图式（c/d）", self.cmb_fig_style)
        f2.addRow("", self.chk_fig_err)
        f2.addRow("", self.chk_fig_annot)
        f2.addRow("时间轴单位", self.cmb_time_unit)
        lay.addWidget(adv)
        # FR-24.5：登记高级区所有行 + 防抖过滤（150 ms）；折叠状态**会话内记忆**
        self._adv_box = adv
        self._adv_rows = collect_form_rows(f2)
        self._adv_was_open = False
        self._adv_filter = FormRowFilter(f2, self._adv_rows,
                                         on_change=self._on_adv_filtered)
        self._adv_filter.attach_to_input(self.ed_adv_search)

        # ── 撤销 / 恢复默认（V2.6 FR-40.6） ──
        bar = QHBoxLayout()
        self.btn_undo = QPushButton("撤销上一次修改")
        self.btn_undo.setToolTip(
            "撤销最近一次参数修改（会话内旧值栈，仅回滚界面值；仍走既有保存链路）")
        self.btn_undo.setEnabled(False)
        self.btn_undo.clicked.connect(self.undo_last)
        self.btn_reset = QPushButton("恢复默认")
        self.btn_reset.setToolTip("全部参数恢复到 DEFAULT_CFG（会二次确认）")
        self.btn_reset.clicked.connect(self._on_reset_clicked)
        bar.addWidget(QLabel("改动会写入 config.yaml"))
        bar.addWidget(self.btn_undo)
        bar.addStretch(1)
        bar.addWidget(self.btn_reset)
        lay.addLayout(bar)
        lay.addStretch(1)

        # 统一连接变化信号（V2.6 FR-40.6：改为"记录旧值 → 撤销栈 → 发 changed"）
        self._connect_tracking()
        # FR-24.1：Ctrl+F 聚焦高级参数搜索框（对话框内的 Ctrl+F 见 plot_style_dialog）
        from PySide6.QtGui import QShortcut, QKeySequence
        self._sc_search = QShortcut(QKeySequence("Ctrl+F"), self)
        self._sc_search.activated.connect(self.ed_adv_search.setFocus)
        # 切换风格时把 dpi/格式带成该风格的常用值（用户仍可再手改）
        self.cmb_style.currentTextChanged.connect(self._on_style_changed)
        # FR-8：预设下拉套用；手工改 dpi/格式即跳「自定义」
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_chosen)
        self.sp_dpi.valueChanged.connect(lambda *_: self._mark_custom())
        self.ed_formats.textChanged.connect(lambda *_: self._mark_custom())
        self._apply_help()

    def _apply_help(self) -> None:
        """V2.9.3 FR-68.1：常用/高级参数挂一句话悬停说明（注册表单一来源）。"""
        from ui.help_texts import PARAM_HELP
        pairs = {
            "phi_ref": self.sp_phiref, "phi_ref_err": self.sp_phireferr,
            "probe": self.sp_probe, "probe_window": self.sp_win,
            "excitation": self.sp_exc, "a_ex_err": self.sp_aexerr,
            "iabs_mode": self.cmb_iabs, "r_min": self.sp_rmin,
            "r_max": self.sp_rmax,
        }
        for key, w in pairs.items():
            if key in PARAM_HELP:
                w.setToolTip(PARAM_HELP[key])

    # ══════════════ V2.6 FR-40.6：撤销栈 / 已改高亮 / 恢复默认确认 ══════════════
    def _connect_tracking(self) -> None:
        """登记需要纳入撤销/高亮的控件并连接变化信号（一次性）。"""
        spins = self.findChildren(QDoubleSpinBox) + self.findChildren(QSpinBox)
        checks = self.findChildren(QCheckBox)
        combos = [c for c in self.findChildren(QComboBox)
                  if getattr(self, "cmb_preset", None) is not c]  # 预设=动作非参数
        edits = [w for w in (getattr(self, "ed_times", None),
                             getattr(self, "ed_formats", None)) if w is not None]
        self._tracked = [*spins, *checks, *combos, *edits]
        for w in spins:
            w.valueChanged.connect(lambda *a, w=w: self._track(w))
        for w in checks:
            w.toggled.connect(lambda *a, w=w: self._track(w))
        for w in combos:
            w.currentIndexChanged.connect(lambda *a, w=w: self._track(w))
        for w in edits:
            w.textChanged.connect(lambda *a, w=w: self._track(w))

    @staticmethod
    def _value_of(w) -> object:
        if isinstance(w, (QDoubleSpinBox, QSpinBox)):
            return float(w.value())
        if isinstance(w, QCheckBox):
            return bool(w.isChecked())
        if isinstance(w, QComboBox):
            return int(w.currentIndex())
        if isinstance(w, QLineEdit):
            return str(w.text())
        return None

    def _set_value_of(self, w, value) -> None:
        if isinstance(w, QDoubleSpinBox):
            w.setValue(float(value))
        elif isinstance(w, QSpinBox):
            w.setValue(int(value))
        elif isinstance(w, QCheckBox):
            w.setChecked(bool(value))
        elif isinstance(w, QComboBox):
            w.setCurrentIndex(int(value))
        elif isinstance(w, QLineEdit):
            w.setText(str(value))

    def _capture_baseline(self) -> None:
        """set_cfg/reset 后刷新基线：清撤销栈、清"已改"标记（程序化填值不算改动）。"""
        self._base_values = {w: self._value_of(w) for w in self._tracked}
        self._track_values = dict(self._base_values)
        self._undo_stack = []
        self._syncing = True
        try:
            self._refresh_marks()
        finally:
            self._syncing = False
        self._update_undo_button()

    def _track(self, widget) -> None:
        """用户改动 → 记录旧值（栈上限 UNDO_LIMIT）→ 刷新高亮 → 发 changed。"""
        if self._syncing:
            return
        old = self._track_values.get(widget, self._value_of(widget))
        new = self._value_of(widget)
        if new == old:
            return
        self._undo_stack.append((widget, old))
        if len(self._undo_stack) > self.UNDO_LIMIT:
            del self._undo_stack[0]
        self._track_values[widget] = new
        self._refresh_marks()
        self._update_undo_button()
        self.changed.emit()

    def _refresh_marks(self) -> None:
        """已改键浅色高亮（与基线不同 → property + 浅黄底色；可离屏断言）。"""
        self._modified_hits = 0
        for w in self._tracked:
            base = self._base_values.get(w)
            cur = self._value_of(w)
            modified = bool(base is not None and cur != base)
            if modified:
                self._modified_hits += 1
            if bool(w.property(self.MODIFIED_PROP)) != modified:
                w.setProperty(self.MODIFIED_PROP, modified)
                w.setStyleSheet("background-color: #FFF7E0;" if modified else "")
                try:
                    w.style().unpolish(w)
                    w.style().polish(w)
                except Exception:            # noqa: BLE001 —— 样式刷新失败不影响功能
                    pass

    def _update_undo_button(self) -> None:
        btn = getattr(self, "btn_undo", None)
        if btn is not None:
            btn.setEnabled(bool(self._undo_stack))

    def undo_depth(self) -> int:
        """撤销栈深度（V2.6 FR-40.6：0..UNDO_LIMIT）。"""
        return len(self._undo_stack)

    def modified_count(self) -> int:
        """当前与基线不同的可编辑键数量（"已改键"计数，测试/状态展示可用）。"""
        return int(self._modified_hits)

    def undo_last(self) -> bool:
        """撤销上一次修改：恢复旧值并走既有 changed → 保存链路。"""
        if not self._undo_stack:
            return False
        widget, old = self._undo_stack.pop()
        self._syncing = True
        try:
            self._set_value_of(widget, old)
        finally:
            self._syncing = False
        self._track_values[widget] = self._value_of(widget)
        self._refresh_marks()
        self._update_undo_button()
        self.changed.emit()
        return True

    def _confirm_reset(self) -> bool:
        """恢复默认的二次确认（破坏性操作保留模态；测试 patch 返回值）。"""
        from PySide6.QtWidgets import QMessageBox
        return (QMessageBox.question(
            self, "恢复默认参数",
            "将所有参数恢复为内置默认值（会写回 config.yaml）。\n"
            "当前修改可用「撤销上一次修改」逐步回退；确定恢复默认吗？")
            == QMessageBox.Yes)

    def _on_reset_clicked(self) -> None:
        if not self._confirm_reset():
            return
        self.reset()
        self.changed.emit()      # 用户显式动作 → 走既有保存链路

    # ══════════════ 高级区过滤（FR-24.5） ══════════════
    def _on_adv_filtered(self, shown: int, query: str) -> None:
        """过滤后：有搜索词→展开折叠区（不然过滤了也看不见）；清空→恢复原状态。"""
        box = getattr(self, "_adv_box", None)
        if box is None:
            return
        if query:
            if not box.btn.isChecked():
                self._adv_was_open = False
                box.btn.setChecked(True)
                box.btn.setArrowType(Qt.DownArrow)
                box.content.setVisible(True)
        else:
            if not self._adv_was_open and box.btn.isChecked():
                box.btn.setChecked(False)
                box.btn.setArrowType(Qt.RightArrow)
                box.content.setVisible(False)
            self._adv_was_open = False
        self.changed.emit()          # 视图变化也要让外层知道（统计/刷新用）

    def filter_advanced(self, text: str) -> int:
        """立即按 ``text`` 过滤高级区（测试/程序化用）；返回可见行数。"""
        return self._adv_filter.apply(text)

    def advanced_row_count(self) -> int:
        """高级区总行数（测试用）。"""
        return len(self._adv_rows)

    def advanced_visible_rows(self) -> int:
        """高级区当前可见行数（测试用；直接数控件 isHidden）。"""
        n = 0
        for label_w, field_w in self._adv_rows:
            if field_w is not None and not field_w.isHidden():
                n += 1
        return n

    # ══════════════ 读写 ══════════════
    # ── 出图预设与高级对话框（FR-8 / FR-7.3） ──
    def _reload_presets(self) -> None:
        """填充预设下拉（内置在前，用户在后，末尾「自定义」）。"""
        from core.plotstyle import list_presets
        cur = self.cmb_preset.currentText() if hasattr(self, "cmb_preset") else ""
        self.cmb_preset.blockSignals(True)
        self.cmb_preset.clear()
        try:
            for name, label, _builtin in list_presets():
                self.cmb_preset.addItem(label, name)
        except Exception as err:                           # noqa: BLE001
            # G20（第二轮审查）：原来是 `pass`——用户预设读坏时下拉**静默变空**，
            # 界面看着"没有预设"，日志里也查不到原因（ui/README.md 铁律：
            # 失败不许静默吞）。读预设失败不该拦住 GUI，但必须留痕。
            log.warning("读取出图预设失败（下拉将只显示内置项）：%s", err)
        self.cmb_preset.addItem("自定义…", "__custom__")
        self.cmb_preset.blockSignals(False)
        if cur:
            self.cmb_preset.setCurrentText(cur)

    def _save_figure_preset(self) -> None:
        """V2.9.3 FR-66：把会话图式写入用户预设文件（不写 config.yaml）。"""
        from PySide6.QtWidgets import QInputDialog
        from core.render import plotstyle
        name, ok = QInputDialog.getText(self, "保存图式预设", "预设名：")
        name = str(name or "").strip()
        if not ok or not name:
            return
        try:
            plotstyle.save_user_preset(name, {
                "figure_style": self.cmb_fig_style.currentData(),
                "figure_error_bars": self.chk_fig_err.isChecked(),
                "figure_transparent": self.chk_transparent.isChecked(),
                "figure_annotate": self.chk_fig_annot.isChecked(),
                "figure_time_unit": self.cmb_time_unit.currentText(),
            })
        except Exception as err:                           # noqa: BLE001
            log.warning("保存图式预设失败：%s", err)
            return
        self._reload_presets()

    def _on_preset_chosen(self, _idx: int = 0) -> None:
        """套用预设：只写出图参数，绝不碰数值键（FR-8.4）。"""
        name = self.cmb_preset.currentData()
        if not name or name == "__custom__":
            return
        from core.plotstyle import jacs_defaults, load_preset
        vals = load_preset(name)
        if vals is None:
            return
        # 先回默认再套覆盖：否则上一个预设的残留（如 lab-ppt 的 6.5 in）会留下
        j = self._cfg.setdefault("plot", {}).setdefault("jacs", {})
        j.clear()
        j.update(jacs_defaults())
        j.update(vals)
        style = self._cfg["plot"].get("style", "jacs")
        self.set_cfg(self._cfg)              # 回填控件
        self.cmb_preset.setCurrentText(
            next((self.cmb_preset.itemText(i)
                  for i in range(self.cmb_preset.count())
                  if self.cmb_preset.itemData(i) == name), name))
        self.changed.emit()
        _ = style

    def _mark_custom(self) -> None:
        """手工改动出图参数后，把预设下拉跳到「自定义…」。"""
        idx = self.cmb_preset.findData("__custom__")
        if idx >= 0 and self.cmb_preset.currentIndex() != idx:
            self.cmb_preset.blockSignals(True)
            self.cmb_preset.setCurrentIndex(idx)
            self.cmb_preset.blockSignals(False)

    def _open_style_dialog(self) -> None:
        """高级出图参数对话框（附录 B 全键位）。"""
        try:
            from ui.widgets.plot_style_dialog import PlotStyleDialog
        except Exception as err:                           # noqa: BLE001
            QMessageBox.warning(self, "不可用",
                                f"高级出图对话框加载失败：{err}")
            return
        dlg = PlotStyleDialog(self._cfg, self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            new_cfg = dlg.result_cfg_dict()
        except Exception as err:                           # noqa: BLE001
            QMessageBox.warning(self, "校验失败", f"读取对话框参数失败：{err}")
            return
        self._cfg = new_cfg
        self.set_cfg(self._cfg)
        self._mark_custom()
        self.changed.emit()
        QMessageBox.information(
            self, "已应用",
            "出图参数已更新。\n"
            "点「开始分析」可立刻看到预览效果；点「定稿出图」得到 600 dpi 投稿图件。")

    def _on_style_changed(self, style: str) -> None:
        """绘图风格切换：把 dpi/导出格式带成该风格的默认值。"""
        jacs = str(style) != "legacy"
        self.sp_dpi.setValue(600 if jacs else 300)
        self.ed_formats.setText("png" if jacs else "svg,png")
        self.changed.emit()

    def reset(self) -> None:
        """把所有控件恢复为 ``DEFAULT_CFG`` 的值。"""
        self.set_cfg(default_config())

    def set_cfg(self, cfg: dict) -> None:
        """用 cfg 的值刷新界面（不改动 cfg，也**不触发** ``changed``）。

        O16：这里会程序化设置约 50 个控件，每个 ``setValue`` 都会发 ``changed``
        → 页面把它连到 `_on_params_changed` → **每次刷新都重写一遍 config.yaml**
        （启动、套预设、载入 run 参数各来几十次；Windows 上杀软还会逐次扫描）。
        用 ``QSignalBlocker`` 统一屏蔽（`dye_time_panel._fill_dye` 早有同类抑制），
        函数结束时才放行——用户手动改控件照旧会触发。
        """
        from PySide6.QtCore import QSignalBlocker
        blockers = [QSignalBlocker(w) for w in self.findChildren(QWidget)]
        try:
            self._set_cfg_inner(cfg)
        finally:
            del blockers                      # 出作用域即解除屏蔽

    def _set_cfg_inner(self, cfg: dict) -> None:
        """真正的回填逻辑（由 :meth:`set_cfg` 包在信号屏蔽里调用）。"""
        c = copy.deepcopy(cfg)
        self._cfg = c
        wl = c["wavelength"]
        qy = c["quantum_yield"]
        kin = c["kinetics"]
        qc = c["qc"]
        base = c["baseline"]
        bg = c["background"]
        ag = c["autogroup"]
        self.sp_probe.setValue(float(wl.get("probe", 379.0)))
        self.sp_win.setValue(float(wl.get("probe_window", 2.0)))
        self.sp_exc.setValue(float(wl.get("excitation", 532.0)))
        self.sp_phiref.setValue(float(qy.get("phi_ref", 0.75)))
        self.sp_phireferr.setValue(float(qy.get("phi_ref_err", 0.075)))
        self.chk_trunc.setChecked(bool(kin.get("auto_truncate", True)))
        sel = c["plot"]["spectra_overlay"].get("select_times", "all")
        if isinstance(sel, (list, tuple)) and sel:
            self.cmb_times.setCurrentIndex(1)
            self.ed_times.setText(",".join(f"{float(v):g}" for v in sel))
        else:
            self.cmb_times.setCurrentIndex(0)
            self.ed_times.clear()
        self.sp_aexerr.setValue(float(qy.get("a_ex_err", 0.005)))
        self.cmb_iabs.setCurrentText(str(qy.get("iabs_mode", "exact")))
        self.sp_rmin.setValue(float(kin.get("r_min", 0.30)))
        self.sp_rmax.setValue(float(kin.get("r_max", 1.00)))
        self.sp_truncfrac.setValue(float(kin.get("truncate_frac", 0.70)))
        self.sp_curvp.setValue(float(kin.get("curvature_p", 0.05)))
        self.sp_floor.setValue(float(kin.get("a_floor", 0.01)))
        rng = qc.get("a0_range", [0.05, 1.5])
        self.sp_a0lo.setValue(float(rng[0]))
        self.sp_a0hi.setValue(float(rng[1]))
        self.sp_nearly.setValue(int(c["a0"].get("n_early", 2)))
        self.sp_sat.setValue(float(qc.get("saturation", 3.0)))
        self.chk_baseline.setChecked(bool(base.get("enable", True)))
        self.cmb_basemethod.setCurrentText(str(base.get("method", "median")))
        brng = base.get("wl_range", [650.0, 700.0])
        self.sp_baselo.setValue(float(brng[0]))
        self.sp_basehi.setValue(float(brng[1]))
        self.chk_bg.setChecked(bool(bg.get("enable", True)))
        self.cmb_bgmode.setCurrentText(str(bg.get("mode", "matched")))
        self.sp_aglo.setValue(float(ag.get("core_lo", 450.0)))
        self.sp_aghi.setValue(float(ag.get("core_hi", 650.0)))
        self.sp_agstep.setValue(float(ag.get("step", 5.0)))
        self.sp_agdmax.setValue(float(ag.get("d_max", 0.06)))
        self.chk_dose.setChecked(bool(kin.get("dose_normalize", False)))
        # 绘图风格：dpi/格式两项要跟随**当前风格**读写（jacs 与 legacy 各有一套参数）
        style = str((c.get("plot") or {}).get("style", "jacs"))
        style = style if style in ("jacs", "legacy") else "jacs"
        self.cmb_style.setCurrentText(style)      # 可能触发 _on_style_changed
        pk = ((c.get("plot") or {}).get("jacs") or {}) if style == "jacs" else (c.get("plot") or {})
        self.sp_dpi.setValue(int(pk.get("dpi", 600 if style == "jacs" else 300)))
        self.ed_formats.setText(",".join(
            pk.get("formats", ["png"] if style == "jacs" else ["svg", "png"])))
        self.chk_transparent.setChecked(bool(pk.get("figure_transparent", False)))
        _st = str(pk.get("figure_style") or "")
        _i = self.cmb_fig_style.findData(_st if _st in ("bar", "line", "scatter") else None)
        self.cmb_fig_style.setCurrentIndex(max(0, _i))
        self.chk_fig_err.setChecked(bool(pk.get("figure_error_bars", True)))
        self.chk_fig_annot.setChecked(bool(pk.get("figure_annotate", False)))
        self.cmb_time_unit.setCurrentText(str(pk.get("figure_time_unit", "s"))
                                           if str(pk.get("figure_time_unit", "s")) in ("s", "min", "h") else "s")
        self._capture_baseline()

    def apply_to(self, cfg: dict) -> dict:
        """把界面值写回 ``cfg``（就地修改并返回），数值键名与 DEFAULT_CFG 对齐。"""
        cfg["wavelength"]["probe"] = float(self.sp_probe.value())
        cfg["wavelength"]["probe_window"] = float(self.sp_win.value())
        cfg["wavelength"]["excitation"] = float(self.sp_exc.value())
        qy = cfg["quantum_yield"]
        qy["phi_ref"] = float(self.sp_phiref.value())
        qy["phi_ref_err"] = float(self.sp_phireferr.value())
        qy["a_ex_err"] = float(self.sp_aexerr.value())
        qy["iabs_mode"] = self.cmb_iabs.currentText()
        kin = cfg["kinetics"]
        kin["auto_truncate"] = bool(self.chk_trunc.isChecked())
        kin["r_min"] = float(self.sp_rmin.value())
        kin["r_max"] = float(self.sp_rmax.value())
        kin["truncate_frac"] = float(self.sp_truncfrac.value())
        kin["curvature_p"] = float(self.sp_curvp.value())
        kin["a_floor"] = float(self.sp_floor.value())
        kin["dose_normalize"] = bool(self.chk_dose.isChecked())
        cfg["a0"]["n_early"] = int(self.sp_nearly.value())
        cfg["qc"]["a0_range"] = [float(self.sp_a0lo.value()), float(self.sp_a0hi.value())]
        cfg["qc"]["saturation"] = float(self.sp_sat.value())
        base = cfg["baseline"]
        base["enable"] = bool(self.chk_baseline.isChecked())
        base["method"] = self.cmb_basemethod.currentText()
        base["wl_range"] = [float(self.sp_baselo.value()), float(self.sp_basehi.value())]
        bg = cfg["background"]
        bg["enable"] = bool(self.chk_bg.isChecked())
        bg["mode"] = self.cmb_bgmode.currentText()
        ag = cfg["autogroup"]
        ag["core_lo"] = float(self.sp_aglo.value())
        ag["core_hi"] = float(self.sp_aghi.value())
        ag["step"] = float(self.sp_agstep.value())
        ag["d_max"] = float(self.sp_agdmax.value())
        cfg["plot"]["style"] = self.cmb_style.currentText()
        fmts = [x.strip().lstrip(".") for x in self.ed_formats.text().split(",") if x.strip()]
        if cfg["plot"]["style"] == "jacs":        # JACS 参数写在 plot.jacs 下
            j = cfg["plot"].setdefault("jacs", {})
            # 面板没有直接暴露的键（画布尺寸/箭头/配色/逐图高度…共 80 余项）以
            # 面板内部状态为准——否则「出图预设」与「高级出图参数」的改动会被
            # 传入的 base 覆盖掉（那是 V1.0 的 bug：改了预设看不到效果）。
            j.update(copy.deepcopy((self._cfg.get("plot") or {}).get("jacs") or {}))
            # V2.9.3 FR-66：会话图式键只存预设文件，不写 config.yaml
            for _k in ("figure_style", "figure_error_bars", "figure_transparent",
                       "figure_annotate"):
                j.pop(_k, None)
            j["dpi"] = int(self.sp_dpi.value())
            j["formats"] = fmts or ["png"]
        else:                                     # legacy 参数用顶层键
            cfg["plot"]["dpi"] = int(self.sp_dpi.value())
            cfg["plot"]["formats"] = fmts or ["svg", "png"]
        # V2.9.3 FR-66/67：jacs 图 c/d 图式/误差棒/透明底 = 会话态（不入 config.yaml）
        try:
            from core.render import plotting_jacs
            plotting_jacs.set_figure_session(
                style=self.cmb_fig_style.currentData(),
                error_bars=self.chk_fig_err.isChecked(),
                transparent=self.chk_transparent.isChecked(),
                time_unit=self.cmb_time_unit.currentText(),
                annotate=self.chk_fig_annot.isChecked())
        except Exception as err:                           # noqa: BLE001
            log.warning("设置图 c/d 会话图式失败：%s", err)
        # 叠加图时间点
        if self.cmb_times.currentIndex() == 1:
            vals: list[float] = []
            for x in self.ed_times.text().replace("，", ",").split(","):
                x = x.strip()
                if not x:
                    continue
                try:
                    vals.append(float(x))
                except ValueError:
                    continue
            cfg["plot"]["spectra_overlay"]["select_times"] = vals or "all"
        else:
            cfg["plot"]["spectra_overlay"]["select_times"] = "all"
        return cfg

    def config(self, base: Optional[dict] = None) -> dict:
        """基于 ``base``（默认 DEFAULT_CFG 深拷贝）生成应用了界面值的 cfg。"""
        cfg = copy.deepcopy(base) if base is not None else default_config()
        return self.apply_to(cfg)
