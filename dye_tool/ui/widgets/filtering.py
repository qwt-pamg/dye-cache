# -*- coding: utf-8 -*-
"""表单行过滤小工具（V2.3 FR-24.5）：给 ``QFormLayout`` 加"按键名/说明实时过滤"。

为什么单独成模块：参数面板的高级折叠区与出图对话框都需要同一套语义
（设计文档 §5.2-FR-24.5："可复用同一小函数"）。这里的约定：

* **过滤 = 隐藏**（``setVisible``），不重建、不改值——与 A 道"过滤 = 隐藏"同一原则；
* 一行的匹配串 = 标签文字 + 字段控件文字 + 字段 tooltip（三者任一命中即显示），
  所以用户输"箭头"能命中 tooltip 里写了"箭头"的键，输键名也能命中；
* 空串 = 全部显示；
* 键入**防抖 150 ms**（设计文档 §9 风险表：100+ 控件逐行 setVisible 不能每键都跑）。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QLabel,
                               QLineEdit, QWidget)

#: 防抖间隔（ms）。设计文档 §9 指定 150 ms。
FILTER_DEBOUNCE_MS = 150


def field_text(widget: Optional[QWidget]) -> str:
    """取一个表单字段控件的"可搜索文字"（标签/按钮文字/当前值/tooltip）。"""
    if widget is None:
        return ""
    parts: list[str] = []
    tip = widget.toolTip() or ""
    if tip:
        parts.append(tip)
    w = widget
    # 复合控件（如"预设 + 高级…"那一行）把子控件文字也收进来
    for child in widget.findChildren(QWidget):
        parts.extend(_one_text(child))
    parts.extend(_one_text(w))
    return " ".join(p for p in parts if p)


def _one_text(w: QWidget) -> list[str]:
    """单个控件的文字（不同类型取值方式不同，都不抛）。"""
    out: list[str] = []
    try:
        if isinstance(w, QLabel):
            out.append(w.text())
        elif isinstance(w, QCheckBox):
            out.append(w.text())
        elif isinstance(w, QLineEdit):
            out.append(w.text())
            out.append(w.placeholderText())
        elif isinstance(w, QComboBox):
            out.extend(w.itemText(i) for i in range(w.count()))
        else:
            tip = w.toolTip()
            if tip:
                out.append(tip)
            # 数值控件：把属性名（objectName）也当线索（如 sp_rmax）
            name = w.objectName()
            if name:
                out.append(name)
    except Exception:                                    # noqa: BLE001
        pass
    return [s for s in out if s]


class FormRowFilter:
    """``QFormLayout`` 行过滤（带防抖）。 attach 后 ``attach_to_input`` 绑定输入框。

    Args:
        form: 目标 ``QFormLayout``。
        rows: ``[(标签控件, 字段控件)]``（构建时登记，避免每次遍历布局）。
        rows_provider: 可选；给了就每次过滤时现取（动态表用），否则用 ``rows``。
        on_change: 过滤后回调（如"有命中就展开折叠区"）。
    """

    def __init__(self, form: QFormLayout, rows: list[tuple],
                 rows_provider: Optional[Callable[[], list[tuple]]] = None,
                 on_change: Optional[Callable[[int, str], None]] = None):
        self.form = form
        self._rows = rows
        self._provider = rows_provider
        self._on_change = on_change
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(FILTER_DEBOUNCE_MS)
        self._timer.timeout.connect(self.apply_pending)
        self._pending_text = ""

    def attach_to_input(self, edit: QLineEdit) -> None:
        """把输入框的 textChanged 接到过滤（防抖）。"""
        edit.textChanged.connect(self.schedule)

    def schedule(self, text: str) -> None:
        """登记最新键入，150 ms 后统一过滤（防抖）。"""
        self._pending_text = text or ""
        self._timer.start()

    # ── 实际过滤（也可在测试里直接调） ──
    def apply(self, text: str) -> int:
        """立即按 ``text`` 过滤；返回**可见行数**。"""
        q = (text or "").strip().lower()
        rows = self._provider() if self._provider is not None else self._rows
        shown = 0
        for label_w, field_w in rows:
            hay = f"{_label_of(label_w)} {field_text(field_w)}".lower()
            ok = (not q) or (q in hay)
            if label_w is not None:
                label_w.setVisible(ok)
            if field_w is not None:
                field_w.setVisible(ok)
            shown += 1 if ok else 0
        if self._on_change is not None:
            self._on_change(shown, q)
        return shown

    def apply_pending(self) -> None:
        self.apply(self._pending_text)


def _label_of(widget: Optional[QWidget]) -> str:
    try:
        return widget.text() if isinstance(widget, QLabel) else ""
    except Exception:                                    # noqa: BLE001
        return ""


def collect_form_rows(form: QFormLayout) -> list[tuple]:
    """从 ``QFormLayout`` 现取 ``[(LabelRole 控件, FieldRole 控件)]``。"""
    rows: list[tuple] = []
    for r in range(form.rowCount()):
        li = form.itemAt(r, QFormLayout.LabelRole)
        fi = form.itemAt(r, QFormLayout.FieldRole)
        rows.append((li.widget() if li is not None else None,
                     fi.widget() if fi is not None else None))
    return rows
