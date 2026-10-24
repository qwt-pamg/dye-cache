# -*- coding: utf-8 -*-
"""V2.9.6：全应用悬停说明/不可用原因（M1–M3）。"""
from __future__ import annotations

import time
import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(app, n=3):
    for _ in range(n):
        app.processEvents()
        time.sleep(0.01)


def test_repeat_hover_shows_same_reason_each_enter(qt_app, monkeypatch):
    from ui.help_texts import bind_disabled_reason
    shown = []
    monkeypatch.setattr("ui.help_texts.QToolTip.showText",
                        lambda pos, text, *a, **k: shown.append(text))
    parent = QWidget()
    btn = QPushButton("计算", parent)
    btn.setEnabled(False)
    flt = bind_disabled_reason(btn, lambda: "先选择光谱目录", parent)
    for _ in range(3):
        flt.eventFilter(btn, QEvent(QEvent.HoverEnter))
        _pump(qt_app, 2)
    hits = [t for t in shown if t == "先选择光谱目录"]
    assert len(hits) >= 3
    _pump(qt_app, 2)
    parent.deleteLater()


def test_enabled_hover_uses_static_help(qt_app, monkeypatch):
    from ui.help_texts import bind_disabled_reason
    shown = []
    monkeypatch.setattr("ui.help_texts.QToolTip.showText",
                        lambda pos, text, *a, **k: shown.append(text))
    parent = QWidget()
    btn = QPushButton("运行", parent)
    btn.setToolTip("真实动作说明")
    flt = bind_disabled_reason(btn, lambda: "不可用原因", parent)
    flt.eventFilter(btn, QEvent(QEvent.HoverEnter))
    _pump(qt_app, 2)
    assert shown and shown[-1] == "真实动作说明"
    parent.deleteLater()


def test_extinction_reason_state_machine(qt_app, tmp_path):
    from core.config import default_config
    from ui.help_texts import resolve_help
    from ui.shell.cards.extinction import ExtinctionPanel
    p = ExtinctionPanel(default_config())
    try:
        btn = p.btn_run
        btn.setEnabled(False)
        assert "光谱目录" in resolve_help(btn)
        p.data_dir = tmp_path
        p._files = []
        assert "可解析" in resolve_help(btn)
        p._files = [tmp_path / "a.txt"]
        assert "计算 ε" in resolve_help(btn) or "消光" in resolve_help(btn)
        p._busy = True
        assert "正在计算" in resolve_help(btn)
    finally:
        p.deleteLater()


def test_install_no_generic_or_empty_on_small_tree(qt_app):
    from ui.help_texts import audit_help, install_app_help
    root = QWidget()
    btn = QPushButton("运行", root)
    btn.setToolTip("旧句：若为灰色，悬停会说明不可用原因。")
    btn2 = QPushButton("后创建", root)
    install_app_help(root)
    btn2.setEnabled(False)
    install_app_help(root)
    assert "若为灰色" not in btn.toolTip()
    assert btn.toolTip() and btn2.toolTip()
    rows = audit_help(root)
    assert not [r for r in rows if r["status"] == "missing"]
    root.deleteLater()
