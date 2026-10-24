"""共享表格/校验小工具（V2.8 FR-48 搬家）"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem

log = logging.getLogger("dye_tool.ui.analysis")

_INVALID_BG = QColor("#FFE0E0")


def _mark_param_cell(item: QTableWidgetItem, reason: str) -> None:
    """参数表格单元格"非法"标记（底色 + 中文 tooltip；``reason`` 为空则清除）。"""
    if item is None:
        return
    if reason:
        item.setData(Qt.BackgroundRole, _INVALID_BG)
        item.setToolTip(reason)
    else:
        item.setData(Qt.BackgroundRole, None)
        item.setToolTip("")


class _ParamTable(QTableWidget):
    """V2.6 FR-39.2/39.3：ε 浓度表与 A@λ 波长表共用的小交互层。

    * 单元格编辑走 Qt 默认（双击 / F2 / 直接键入）；
    * **Tab / Shift+Tab / 回车**在可编辑列内**纵向**移动（Qt 默认 Tab 横向，
      不符合"逐行填一列"的工作习惯）；
    * **Ctrl+V** 粘贴一列多行：从当前行起逐行填入；``allow_expand`` 时按需
      加行（A@λ 波长表），否则只填到表尾（ε 行数由文件数固定，不虚增行）。
    """

    def __init__(self, parent=None, *, editable_col: int = 0,
                 allow_expand: bool = False):
        super().__init__(parent)
        self._editable_col = int(editable_col)
        self._allow_expand = bool(allow_expand)
        # Tab 交给本类做**纵向**导航（Qt 默认的 tabKeyNavigation 会横移/切焦点）
        self.setTabKeyNavigation(False)
        # 单选：程序化 setCurrentCell 不会叠加旧选区（否则「删除」会一次删多行）
        self.setSelectionMode(QAbstractItemView.SingleSelection)

    def event(self, event):                            # noqa: N802 —— Qt 约定
        """Tab / Shift+Tab 在可编辑列内纵向移动（先于焦点链拦截）。"""
        if (event.type() == QEvent.Type.KeyPress
                and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)):
            backward = (event.key() == Qt.Key_Backtab
                        or bool(event.modifiers() & Qt.ShiftModifier))
            self._move_row(-1 if backward else 1)
            return True
        return super().event(event)

    def keyPressEvent(self, event) -> None:            # noqa: N802 —— Qt 约定
        key = event.key()
        if key in (Qt.Key_Tab, Qt.Key_Backtab):
            backward = (key == Qt.Key_Backtab
                        or bool(event.modifiers() & Qt.ShiftModifier))
            self._move_row(-1 if backward else 1)
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) \
                and self.state() != QAbstractItemView.EditingState:
            self._move_row(1)
            event.accept()
            return
        if event.matches(QKeySequence.Paste) and self._paste_column():
            event.accept()
            return
        super().keyPressEvent(event)

    def _move_row(self, step: int) -> None:
        """在可编辑列内上下移动当前格（到边界停住，不绕回）。"""
        if self.rowCount() <= 0:
            return
        row = self.currentRow()
        row = 0 if row < 0 else max(0, min(self.rowCount() - 1, row + step))
        self.clearSelection()
        self.setCurrentCell(row, self._editable_col)

    def _paste_column(self) -> bool:
        """Ctrl+V：把剪贴板**一列多行**从当前行起填进可编辑列。"""
        from PySide6.QtGui import QGuiApplication
        text = QGuiApplication.clipboard().text()
        if not text or not text.strip():
            return False
        lines = [ln.split("\t")[0].strip() for ln in text.splitlines()
                 if ln.strip()]
        if not lines:
            return False
        start = max(0, self.currentRow())
        if start + len(lines) > self.rowCount():
            if self._allow_expand:
                self.setRowCount(start + len(lines))
            else:
                lines = lines[: max(0, self.rowCount() - start)]
        for i, val in enumerate(lines):
            r = start + i
            if r >= self.rowCount():
                break
            it = self.item(r, self._editable_col)
            if it is None:
                it = QTableWidgetItem("")
                self.setItem(r, self._editable_col, it)
            it.setText(val)
        if lines:
            self.clearSelection()
            self.setCurrentCell(min(start + len(lines), self.rowCount() - 1),
                                self._editable_col)
        return True
