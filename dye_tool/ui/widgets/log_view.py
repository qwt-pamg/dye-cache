# -*- coding: utf-8 -*-
"""共享日志组件（FR-1.5、FR-5；V2.6 FR-40.2 检索 / 复制 / 暂停）。

core 通过 log_cb(level, msg) 把记录投递给 Job 信号，最终到这里渲染。
默认只显示 WARNING 及以上（设计文档 FR-1.5），可切换为显示全部 INFO。

V2.6 FR-40.2（只改本组件，页面不改自动生效）：

* 关键字检索：实时过滤 + 命中计数 + 高亮命中（背景色 / 加粗）；
* 「复制选中」「复制全部」（写系统剪贴板）；
* 自动滚动开关：暂停后新日志不抢滚动位置，恢复时跳回尾部；
* 原有级别过滤 / 清空 / 另存行为不变。
"""
from __future__ import annotations

import html
import re
from collections import deque
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QTextEdit, QVBoxLayout, QWidget)

from core.explain import level_zh

#: 级别 → 颜色
LEVEL_COLOR = {"DEBUG": "#888888", "INFO": "#204060", "WARNING": "#B8860B",
               "ERROR": "#C81E1E", "CRITICAL": "#8B0000"}
LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

#: 内部记录上限（O9）。视图本来就有 20000 行上限，但 _records 列表**没有**，
#: 长时间跑（反复分析）会一直涨；这里用 deque(maxlen) 对齐视图上限。
LOG_RECORDS_MAX = 20000

#: 命中高亮样式（toHtml 里可断言 background-color / font-weight）
HIT_STYLE = "background-color:#fff3a0;font-weight:bold"


class LogView(QWidget):
    """带级别过滤、关键字检索、复制、自动滚动、清空、另存的日志视图。"""

    def __init__(self, parent: Optional[QWidget] = None,
                 min_level: str = "WARNING"):
        super().__init__(parent)
        self._records: deque = deque(maxlen=LOG_RECORDS_MAX)
        self._total = 0                # 收到的总条数（含已被滚掉的）
        self._min = LEVEL_ORDER.get(min_level, 30)
        self._keyword = ""             # V2.6 FR-40.2：小写检索词（空 = 不过滤）
        self._hits = 0                 # 当前视图命中条数
        self._auto_scroll = True       # 自动滚动开关（暂停时保留滚动位置）
        self._build()

    def _build(self) -> None:
        # QTextEdit（而非 QPlainTextEdit）：需要富文本高亮命中，且 toHtml() 可断言
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.document().setMaximumBlockCount(20000)   # 防止长时间运行内存膨胀
        self.view.setStyleSheet("font-family: Consolas, monospace; font-size: 12px;")

        self.cmb = QComboBox()
        # FR-9.4：界面显示中文级别名（写盘的 run.log 仍保留英文，便于检索）
        self.cmb.addItems(["仅警告及以上", "全部（含信息/调试）"])
        # V2.6 FR-40.2 修正：下拉初值必须与 min_level 一致——旧实现 min_level=DEBUG
        # 时下拉仍显示"仅警告及以上"，之后 setCurrentIndex(0) 是空操作，级别过滤看似失效。
        self.cmb.blockSignals(True)
        self.cmb.setCurrentIndex(0 if self._min >= LEVEL_ORDER["WARNING"] else 1)
        self.cmb.blockSignals(False)
        self.cmb.currentIndexChanged.connect(self._on_level)
        self.btn_clear = QPushButton("清空")
        self.btn_save = QPushButton("另存日志")
        self.btn_clear.clicked.connect(self.clear)
        self.btn_save.clicked.connect(self.save_as)
        self.btn_save.setToolTip("导出为文本文件（级别名保持英文，便于跨版本检索）")
        self.lbl = QLabel("0 条")
        self.lbl.setToolTip("收到的日志总条数（含被过滤掉的）")

        # V2.6 FR-40.2：关键字检索 + 命中计数
        self.ed_search = QLineEdit()
        self.ed_search.setPlaceholderText("关键字检索（实时过滤 + 高亮命中）")
        self.ed_search.setClearButtonEnabled(True)
        self.ed_search.setToolTip("大小写不敏感；与级别过滤叠加。清空即恢复全部。")
        self.ed_search.textChanged.connect(self._on_search_changed)
        self.lbl_hits = QLabel("")
        self.lbl_hits.setStyleSheet("color: #6b7280; font-size: 11px;")

        # V2.6 FR-40.2：复制选中 / 复制全部
        self.btn_copy_sel = QPushButton("复制选中")
        self.btn_copy_sel.setToolTip("复制日志视图里当前选中的文本")
        self.btn_copy_sel.clicked.connect(lambda: self._copy(self.copy_selected))
        self.btn_copy_all = QPushButton("复制全部")
        self.btn_copy_all.setToolTip("复制当前视图全部可见日志（按当前过滤/检索）")
        self.btn_copy_all.clicked.connect(lambda: self._copy(self.copy_all))

        # V2.6 FR-40.2：自动滚动开关
        self.chk_autoscroll = QCheckBox("自动滚动")
        self.chk_autoscroll.setChecked(True)
        self.chk_autoscroll.setToolTip("暂停后新日志不抢滚动位置；恢复时跳回尾部")
        self.chk_autoscroll.toggled.connect(self._on_autoscroll)

        top = QHBoxLayout()
        top.addWidget(QLabel("日志级别"))
        top.addWidget(self.cmb)
        top.addWidget(QLabel("检索"))
        top.addWidget(self.ed_search, 1)
        top.addWidget(self.lbl_hits)
        top.addWidget(self.chk_autoscroll)
        top.addStretch(1)
        bar = QHBoxLayout()
        bar.addStretch(1)
        bar.addWidget(self.lbl)
        bar.addWidget(self.btn_copy_sel)
        bar.addWidget(self.btn_copy_all)
        bar.addWidget(self.btn_clear)
        bar.addWidget(self.btn_save)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addLayout(bar)
        lay.addWidget(self.view, 1)

    # ── 对外接口 ──
    def append(self, level: str, msg: str) -> None:
        """追加一条日志（按当前级别过滤 + 关键字检索决定是否显示）。"""
        level = (level or "INFO").upper()
        self._records.append((level, msg))         # deque(maxlen) 自动滚掉最旧的
        self._total += 1
        self.lbl.setText(self._count_text())
        if not self._passes(level, msg):
            return
        sb = self.view.verticalScrollBar()
        keep = sb.value()
        self._append_html(self._html(level, msg, self._keyword))
        self._hits += 1
        if self._keyword:
            self.lbl_hits.setText(f"命中 {self._hits} 条")
        if self._auto_scroll:
            self._scroll_to_end()
        else:
            # 暂停自动滚动：新日志不抢滚动位置（保持用户当前查看处）
            sb.setValue(min(keep, sb.maximum()))

    def _passes(self, level: str, msg: str) -> bool:
        if LEVEL_ORDER.get(level, 20) < self._min:
            return False
        kw = self._keyword
        if not kw:
            return True
        hay = f"{msg} {level} {level_zh(level)}".lower()
        return kw in hay

    def _count_text(self) -> str:
        """条数文案：滚动截断时明确写出来，避免用户以为日志丢了。"""
        n = len(self._records)
        if self._total > n:
            return f"{self._total} 条（保留最近 {n} 条）"
        return f"{self._total} 条"

    # ── 渲染 ──
    def _append_html(self, html_text: str) -> None:
        """在文末追加一条富文本（QTextEdit 用 cursor.insertHtml）。"""
        cur = self.view.textCursor()
        cur.movePosition(QTextCursor.End)
        if not self.view.document().isEmpty():
            cur.insertBlock()
        cur.insertHtml(html_text)

    def _scroll_to_end(self) -> None:
        self.view.moveCursor(QTextCursor.End)
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _highlight(text: str, keyword: str) -> str:
        """把命中关键字包成高亮 span（大小写不敏感；先切分再逐段转义）。"""
        raw = str(text)
        kw = str(keyword or "").strip()
        if not kw:
            return html.escape(raw).replace("\n", "<br>")
        try:
            parts = re.split(f"({re.escape(kw)})", raw, flags=re.IGNORECASE)
        except re.error:                              # pragma: no cover —— 兜底
            return html.escape(raw).replace("\n", "<br>")
        out: list[str] = []
        for i, part in enumerate(parts):
            esc = html.escape(part).replace("\n", "<br>")
            if i % 2 == 1:
                esc = f'<span style="{HIT_STYLE}">{esc}</span>'
            out.append(esc)
        return "".join(out)

    @classmethod
    def _html(cls, level: str, msg: str, keyword: str = "") -> str:
        """一条日志的 HTML：中文级别名 + 颜色（FR-9.4）+ 命中高亮（FR-40.2）。"""
        color = LEVEL_COLOR.get(level, "#000000")
        body = cls._highlight(msg, keyword)
        return (f'<span style="color:{color}">'
                f'[{html.escape(level_zh(level))}] {body}</span>')

    def _render(self) -> None:
        """按当前级别过滤 + 检索词整体重绘（仅在过滤/检索变化时调用）。"""
        self.view.clear()
        hits = 0
        for lv, msg in self._records:
            if not self._passes(lv, msg):
                continue
            self._append_html(self._html(lv, msg, self._keyword))
            hits += 1
        self._hits = hits
        self.lbl_hits.setText(f"命中 {hits} 条" if self._keyword else "")
        self.lbl.setText(self._count_text())
        if self._auto_scroll:
            self._scroll_to_end()

    # ── 检索 ──
    def _on_search_changed(self, text: str) -> None:
        self._keyword = str(text or "").strip().lower()
        self._render()

    def search_text(self) -> str:
        """当前检索词（原样；测试/调试用）。"""
        return self.ed_search.text()

    def set_search_text(self, text: str) -> None:
        """程序化设置检索词（等价用户输入；空串恢复全部）。"""
        self.ed_search.setText(str(text or ""))

    def hit_count(self) -> int:
        """当前视图命中条数（无检索词时 = 当前级别过滤后的可见条数）。"""
        return self._hits

    def visible_records(self) -> list:
        """当前视图可见记录 [(level, msg)]（级别过滤 + 检索之后）。"""
        return [(lv, msg) for lv, msg in self._records if self._passes(lv, msg)]

    # ── 复制 ──
    def copy_selected(self) -> int:
        """复制当前选中文本到剪贴板；返回复制字符数（无选中 = 0）。"""
        text = self.view.textCursor().selectedText().replace("\u2029", "\n")
        if not text:
            return 0
        QApplication.clipboard().setText(text)
        return len(text)

    def copy_all(self) -> int:
        """复制当前视图全部可见日志到剪贴板；返回复制字符数。"""
        text = self.view.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
        return len(text)

    def _copy(self, fn) -> None:
        try:
            fn()
        except Exception:                             # noqa: BLE001 —— 剪贴板异常不崩
            pass

    # ── 自动滚动 ──
    def autoscroll_enabled(self) -> bool:
        return bool(self._auto_scroll)

    def set_autoscroll(self, enabled: bool) -> None:
        """程序化开关自动滚动（暂停后不抢滚动条；恢复时跳尾部）。"""
        enabled = bool(enabled)
        if self.chk_autoscroll.isChecked() != enabled:
            self.chk_autoscroll.blockSignals(True)
            try:
                self.chk_autoscroll.setChecked(enabled)
            finally:
                self.chk_autoscroll.blockSignals(False)
        self._auto_scroll = enabled
        if enabled:
            self._scroll_to_end()

    def _on_autoscroll(self, checked: bool) -> None:
        self._auto_scroll = bool(checked)
        if self._auto_scroll:
            self._scroll_to_end()

    # ── 清空 / 另存 / 查询 ──
    def clear(self) -> None:
        """清空视图与内部记录（保留检索词与级别过滤设置）。"""
        self._records.clear()
        self._total = 0
        self.view.clear()
        self._hits = 0
        self.lbl_hits.setText("命中 0 条" if self._keyword else "")
        self.lbl.setText("0 条")

    def records(self) -> list:
        """全部已收到的记录（受 LOG_RECORDS_MAX 上限约束，只保留最近若干条）。"""
        return list(self._records)

    def save_as(self) -> Optional[Path]:
        """另存为文本文件（全部记录，级别名英文；行为与 V1.x 一致）。"""
        path, _ = QFileDialog.getSaveFileName(self, "保存日志", "dye_tool.log",
                                              "日志文件 (*.log *.txt)")
        if not path:
            return None
        p = Path(path)
        p.write_text("\n".join(f"[{lv}] {m}" for lv, m in self._records),
                     encoding="utf-8")
        return p

    # ── 内部 ──
    def _on_level(self, idx: int) -> None:
        self._min = 20 if idx == 1 else 30
        self._render()
