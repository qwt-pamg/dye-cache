# -*- coding: utf-8 -*-
"""Φ_Δ 页右侧「出图设置」栏（图式、误差棒、透明背景、时间单位、导出格式）。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel,
                               QScrollArea, QVBoxLayout, QWidget)


class UnifiedSettingsPanel(QWidget):
    """V2.9.4 FR-70：统一页右侧设置区（可滚动/最小宽，兼容 ComparePage 设置项）。"""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._form = QFormLayout()
        self.cmb_fig_style = QComboBox()
        self.cmb_fig_style.addItem('默认（柱状/森林）', None)
        self.cmb_fig_style.addItem('柱状', 'bar')
        self.cmb_fig_style.addItem('曲线', 'line')
        self.cmb_fig_style.addItem('散点', 'scatter')
        self.cmb_fig_style.setToolTip('工作台/定稿 fig c/d 图式；默认与 2.8.0 一致')
        self.chk_err = QCheckBox('误差棒')
        self.chk_err.setChecked(True)
        self.chk_annot = QCheckBox('关键值标注')
        self.chk_transparent = QCheckBox('透明背景')
        self.chk_ci = QCheckBox('Φ_Δ bootstrap 带')
        self.chk_ci.setToolTip('需结果含 bootstrap CI；无 CI 时自动不画')
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(['s', 'min', 'h'])
        self._form.addRow('图式（c/d）', self.cmb_fig_style)
        self._form.addRow('', self.chk_err)
        self._form.addRow('', self.chk_annot)
        self._form.addRow('', self.chk_transparent)
        self._form.addRow('', self.chk_ci)
        self._form.addRow('时间轴单位', self.cmb_unit)
        from ui.widgets.param_panel import FormatsPicker
        self.ed_formats = FormatsPicker()
        self.ed_formats.setToolTip('导出格式（png/svg/jpg/pdf）；与左侧参数面板同步')
        self._form.addRow('导出格式', self.ed_formats)
        self.ed_formats.textChanged.connect(self._apply)
        inner = QWidget()
        inner.setLayout(self._form)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(inner)
        self.setMinimumWidth(320)
        self.setMaximumWidth(440)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel('统一页设置（热更新）'))
        lay.addWidget(scroll, 1)
        for w in (self.cmb_fig_style, self.cmb_unit):
            w.currentIndexChanged.connect(self._apply)
        for w in (self.chk_err, self.chk_annot, self.chk_transparent, self.chk_ci):
            w.toggled.connect(self._apply)

    def _apply(self, *_args) -> None:
        from core.render import plotting_jacs
        plotting_jacs.set_figure_session(
            style=self.cmb_fig_style.currentData(),
            error_bars=self.chk_err.isChecked(),
            annotate=self.chk_annot.isChecked(),
            transparent=self.chk_transparent.isChecked(),
            time_unit=self.cmb_unit.currentText(),
            ci_band=self.chk_ci.isChecked())
        self.changed.emit()

    def values(self) -> dict:
        return {'style': self.cmb_fig_style.currentData(),
                'error_bars': self.chk_err.isChecked(),
                'annotate': self.chk_annot.isChecked(),
                'transparent': self.chk_transparent.isChecked(),
                'time_unit': self.cmb_unit.currentText(),
                'ci_band': self.chk_ci.isChecked()}
