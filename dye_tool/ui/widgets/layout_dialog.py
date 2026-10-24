# -*- coding: utf-8 -*-
"""拼版规格对话框（V2.4 FR-27.3；不塞进 dye_library_page.py——审查 D7/R5 点过体积）。

只负责"选规格"：行列/边距/DPI/标签/格式/输出目录；真正的合成在
:func:`core.render.layout.compose_layout`（后台 Job 执行）。零 PDF 由 core 侧
`_check_formats` 最终把关，本对话框只做前置禁用与提示。
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSpinBox, QVBoxLayout)

from core.render.layout import MAX_CELLS, LayoutError, LayoutSpec

#: 常用规格（按钮式预设；"自定义"只是把两个 spin 设为可编辑）
PRESETS: tuple[tuple[str, int, int], ...] = (
    ("1×2（左右）", 1, 2), ("2×1（上下）", 2, 1),
    ("1×3（一行三图）", 1, 3), ("3×1", 3, 1),
    ("2×2（论文常见）", 2, 2), ("2×3", 2, 3), ("3×2", 3, 2),
    ("3×3", 3, 3), ("3×4", 3, 4), ("4×3", 4, 3),
)


class LayoutDialog(QDialog):
    """拼版规格对话框：``images`` 用来校验"图数 ≤ 格数"并给默认规格。"""

    def __init__(self, images: Sequence[Path], default_dir: Path,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("拼版导出")
        self._images = [Path(p) for p in images]
        self._default_dir = Path(default_dir)
        self._build()
        self._auto_default()
        self._update_ok()

    # ── 构建 ──
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        tip = QLabel(
            f"把选中的 {len(self._images)} 张图拼成一张组合图（面板标签默认 (a)(b)…）。\n"
            "只支持 PNG / SVG（零 PDF）；600 dpi 与 JACS 定稿同档。")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        box = QGroupBox("规格")
        form = QFormLayout(box)
        self.spin_rows = QSpinBox()
        self.spin_rows.setRange(1, MAX_CELLS)
        self.spin_cols = QSpinBox()
        self.spin_cols.setRange(1, MAX_CELLS)
        grid_row = QHBoxLayout()
        grid_row.addWidget(self.spin_rows)
        grid_row.addWidget(QLabel("行 ×"))
        grid_row.addWidget(self.spin_cols)
        grid_row.addWidget(QLabel("列"))
        grid_row.addStretch(1)
        self.cmb_preset = QComboBoxPreset(self)
        form.addRow("常用规格：", self.cmb_preset)
        form.addRow("行列：", grid_row)
        self.spin_dpi = QSpinBox()
        self.spin_dpi.setRange(72, 2400)
        self.spin_dpi.setSingleStep(50)
        self.spin_dpi.setValue(600)
        form.addRow("DPI：", self.spin_dpi)
        self.chk_labels = QCheckBox("显示面板标签 (a)(b)…")
        self.chk_labels.setChecked(True)
        form.addRow("", self.chk_labels)
        self.spin_margin = QDoubleSpinBox()
        self.spin_margin.setRange(0.0, 1.0)
        self.spin_margin.setSingleStep(0.02)
        self.spin_margin.setValue(0.12)
        self.spin_margin.setSuffix(" in")
        self.spin_gutter = QDoubleSpinBox()
        self.spin_gutter.setRange(0.0, 0.5)
        self.spin_gutter.setSingleStep(0.01)
        self.spin_gutter.setValue(0.06)
        self.spin_gutter.setSuffix(" in")
        mg = QHBoxLayout()
        mg.addWidget(self.spin_margin)
        mg.addWidget(QLabel("边距 / 间距"))
        mg.addWidget(self.spin_gutter)
        form.addRow("留白：", mg)
        self.chk_png = QCheckBox("PNG")
        self.chk_png.setChecked(True)
        self.chk_png.setEnabled(False)            # PNG 恒出（预览/Office 都需要）
        self.chk_svg = QCheckBox("SVG（矢量）")
        fmt = QHBoxLayout()
        fmt.addWidget(self.chk_png)
        fmt.addWidget(self.chk_svg)
        fmt.addStretch(1)
        form.addRow("输出格式：", fmt)
        lay.addWidget(box)

        out_box = QGroupBox("输出")
        of = QHBoxLayout(out_box)
        self.edit_dir = QLineEdit(str(self._default_dir))
        self.btn_browse = QPushButton("选择…")
        self.btn_browse.clicked.connect(self._browse)
        of.addWidget(self.edit_dir, 1)
        of.addWidget(self.btn_browse)
        lay.addWidget(out_box)

        self.lbl_warn = QLabel("")
        self.lbl_warn.setStyleSheet("color: #B45309;")
        self.lbl_warn.setWordWrap(True)
        lay.addWidget(self.lbl_warn)

        self.button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.button_box.button(QDialogButtonBox.Ok).setText("开始拼版")
        self.button_box.button(QDialogButtonBox.Cancel).setText("取消")
        self.button_box.accepted.connect(self._on_accept)
        self.button_box.rejected.connect(self.reject)
        lay.addWidget(self.button_box)

        for w in (self.spin_rows, self.spin_cols):
            w.valueChanged.connect(self._update_ok)
        self.cmb_preset.currentIndexChanged.connect(self._on_preset)

    def _auto_default(self) -> None:
        """按图数选一个能装下的最小常规规格（4 张 → 2×2）。"""
        n = max(len(self._images), 1)
        for _label, r, c in PRESETS:
            if r * c >= n:
                self.set_grid(r, c)
                return
        self.set_grid(3, 4)

    # ── 动作/状态 ──
    def set_grid(self, rows: int, cols: int) -> None:
        """设置行列（预设按钮/测试用）；超上限由 spin 自身夹取。"""
        self.spin_rows.setValue(int(rows))
        self.spin_cols.setValue(int(cols))
        self._update_ok()

    def _on_preset(self, idx: int) -> None:
        if idx <= 0:
            return
        _label, r, c = PRESETS[idx - 1]
        self.set_grid(r, c)

    def _update_ok(self) -> None:
        rows, cols = self.spin_rows.value(), self.spin_cols.value()
        cells = rows * cols
        msg = ""
        if cells < len(self._images):
            msg = (f"当前规格 {rows}×{cols} 只有 {cells} 格，"
                   f"装不下选中的 {len(self._images)} 张图。")
        elif cells > MAX_CELLS:
            msg = f"{cells} 格超过上限 {MAX_CELLS}。"
        self.lbl_warn.setText(msg)
        self.button_box.button(QDialogButtonBox.Ok).setEnabled(not msg)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择拼版输出目录",
                                             self.edit_dir.text() or str(self._default_dir))
        if d:
            self.edit_dir.setText(d)

    def _on_accept(self) -> None:
        try:
            self.spec().validate()
        except LayoutError as err:
            self.lbl_warn.setText(str(err))
            return
        self.accept()

    # ── 对外结果 ──
    def spec(self) -> LayoutSpec:
        fmts = ("png", "svg") if self.chk_svg.isChecked() else ("png",)
        return LayoutSpec(rows=self.spin_rows.value(),
                          cols=self.spin_cols.value(),
                          margin_in=float(self.spin_margin.value()),
                          gutter_in=float(self.spin_gutter.value()),
                          panel_labels=self.chk_labels.isChecked(),
                          dpi=int(self.spin_dpi.value()),
                          formats=fmts)

    def out_dir(self) -> Path:
        return Path(self.edit_dir.text().strip() or str(self._default_dir))


class QComboBoxPreset(QComboBox):                       # noqa: N801 —— 内部小控件
    """预设下拉（首项"自定义"）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.addItem("自定义")
        for label, _r, _c in PRESETS:
            self.addItem(label)
