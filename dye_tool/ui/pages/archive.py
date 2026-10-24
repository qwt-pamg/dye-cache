# -*- coding: utf-8 -*-
"""④ 档案页：染料库 · 历史 run · 结果直达（布局与动作都在 :mod:`ui.pages.dye_library`）。

本页只是任务流布局下的壳：把同一个 :class:`DyeLibraryPage` 铺满整页（经典布局里它直接当页签），
顶部细条的页名换成「档案」。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

log = logging.getLogger("dye_tool.ui.pages")


class ArchivePage(QWidget):
    """档案：染料库（原⑤）——左栏染料 / 旧目录，中栏历史 run 与图件，右栏操作。"""

    params_loaded = Signal(dict, dict)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None, *, dye_page=None):
        super().__init__(parent)
        self.cfg = cfg or {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.dye_page = dye_page
        if dye_page is not None:
            lay.addWidget(dye_page, 1)
        try:
            from ui.pages.dye_library import DyeLibraryPage
            from core.paths import resolve_out_root
            out_root = resolve_out_root(self.cfg)
            if self.dye_page is None:
                self.dye_page = DyeLibraryPage(out_root=out_root)
                lay.addWidget(self.dye_page, 1)
            self.dye_page.params_loaded.connect(self.params_loaded)
            self.dye_page.lbl_title.setText("档案")
        except Exception as err:                           # noqa: BLE001
            lb = QLabel(f"染料库页不可用：{err}")
            lb.setWordWrap(True)
            lay.addWidget(lb)
            log.warning("染料库页构建失败：%s", err)

    def reload(self) -> None:
        if self.dye_page is not None and hasattr(self.dye_page, "reload"):
            self.dye_page.reload()
