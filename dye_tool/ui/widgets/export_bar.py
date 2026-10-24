# -*- coding: utf-8 -*-
"""共享导出组件（FR-5）：打开结果文件夹、复制结果、导出路径提示。

"打开结果文件夹" 同时支持 Windows / macOS / Linux（legacy ``run_menu.open_folder``
的跨平台写法）。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMessageBox,
                               QPushButton, QWidget)

log = logging.getLogger("dye_tool.ui.export")


def open_folder(path: Path | str) -> bool:
    """在系统文件管理器中打开目录；失败返回 False（不抛异常）。"""
    p = Path(path)
    try:
        if not p.exists():
            return False
        if os.name == "nt":
            os.startfile(str(p))                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception as err:                           # noqa: BLE001
        log.warning("无法打开目录 %s: %s", p, err)
        return False


class ExportBar(QWidget):
    """结果导出条：打开文件夹 / 复制结果到… / 工作簿（P5）/ QC 体检（P8）。

    ``set_run_dir(path)`` 由页面在流水线结束后调用。

    ``on_workbook`` / ``on_dashboard`` 是**可选回调**：页面的数据（scans / cfg）
    各不相同，导出条不该去猜，由页面把"点下去干什么"注入进来。回调为 None 时
    对应按钮不显示——所以既有页面一个字不改也照常工作。

    ``message(str)``：常规反馈（已导出/无法打开）走状态栏，不弹模态（FR-24.2）。
    """

    message = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None, *,
                 on_workbook=None, on_dashboard=None):
        super().__init__(parent)
        self._run_dir: Optional[Path] = None
        self._on_workbook = on_workbook
        self._on_dashboard = on_dashboard
        self._build()

    def _build(self) -> None:
        self.lbl = QLabel("结果目录：—")
        # 允许选中文本，便于用户复制路径
        self.lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl.setStyleSheet("color: #333;")
        self.btn_open = QPushButton("打开结果文件夹")
        self.btn_copy = QPushButton("导出结果到…")
        self.btn_open.clicked.connect(self.open_run_dir)
        self.btn_copy.clicked.connect(self.copy_run_dir)
        self.btn_open.setEnabled(False)
        self.btn_copy.setEnabled(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.lbl, 1)
        # P5：Excel 多 sheet 工作簿（点一下就在结果目录里生成/刷新）
        self.btn_workbook = QPushButton("导出 Excel 汇总")
        self.btn_workbook.setToolTip(
            "把本次结果汇总成一个多 sheet 工作簿（结果/QC/拟合/染料档案/参数）\n"
            "写到结果目录下，文件名见 config.yaml 的 excel.filename")
        self.btn_workbook.clicked.connect(self._emit_workbook)
        self.btn_workbook.setEnabled(False)
        if self._on_workbook is None:
            self.btn_workbook.hide()
        # P8：QC 体检仪表盘（跑正式分析前后都能看）
        self.btn_dashboard = QPushButton("QC 体检图")
        self.btn_dashboard.setToolTip(
            "一页看清数据质量：饱和段、无效点（NaN/±inf）、基线漂移、波长轴错位")
        self.btn_dashboard.clicked.connect(self._emit_dashboard)
        self.btn_dashboard.setEnabled(False)
        if self._on_dashboard is None:
            self.btn_dashboard.hide()
        lay.addWidget(self.btn_dashboard)
        lay.addWidget(self.btn_workbook)
        lay.addWidget(self.btn_copy)
        lay.addWidget(self.btn_open)

    # ── 对外接口 ──
    def set_run_dir(self, path: Optional[Path | str]) -> None:
        """登记当前结果目录并启用按钮。"""
        self._run_dir = Path(path) if path else None
        ok = bool(self._run_dir and self._run_dir.exists())
        self.btn_open.setEnabled(ok)
        self.btn_copy.setEnabled(ok)
        # 工作簿要写进结果目录，所以也依赖它；QC 体检只要内存里的 scans，不依赖
        self.btn_workbook.setEnabled(ok and self._on_workbook is not None)
        self.lbl.setText(f"结果目录：{self._run_dir if ok else '—'}")

    def set_data_ready(self, ready: bool) -> None:
        """页面载入了数据后调用：让 QC 体检按钮可用（它不需要结果目录）。"""
        if self._on_dashboard is not None:
            self.btn_dashboard.setEnabled(bool(ready))

    def _emit_workbook(self) -> None:
        if self._on_workbook is not None:
            self._on_workbook()

    def _emit_dashboard(self) -> None:
        if self._on_dashboard is not None:
            self._on_dashboard()

    def run_dir(self) -> Optional[Path]:
        """当前结果目录。"""
        return self._run_dir

    def open_run_dir(self) -> None:
        """在文件管理器中打开结果目录。"""
        if not self._run_dir:
            return
        if not open_folder(self._run_dir):
            self.message.emit(f"无法自动打开，请手动前往：{self._run_dir}")

    def copy_run_dir(self) -> None:
        """把整个 run 目录复制到用户选定位置（不改动原结果）。"""
        if not self._run_dir:
            return
        dest = QFileDialog.getExistingDirectory(self, "选择导出目标目录")
        if not dest:
            return
        target = Path(dest) / self._run_dir.name
        try:
            if target.exists():
                QMessageBox.warning(self, "已存在", f"目标已存在：\n{target}")
                return
            shutil.copytree(self._run_dir, target)
        except Exception as err:                       # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(err))
            return
        self.message.emit(f"已导出到：{target}")
