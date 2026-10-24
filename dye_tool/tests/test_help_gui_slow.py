# -*- coding: utf-8 -*-
"""V2.9.6 M3：真实 GUI 慢用例（Hover 重复/状态/全应用审计/1024 布局）。"""
from __future__ import annotations

import time

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(app, ms=80):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


@pytest.mark.slow
def test_real_mouse_hover_repeat_and_state(qt_app, monkeypatch):
    from core.config import default_config
    from ui.help_texts import install_app_help
    from ui.pages.tasks.extinction import ExtinctionPanel
    shown, hidden = [], []
    monkeypatch.setattr("ui.help_texts.QToolTip.showText",
                        lambda pos, text, *a, **k: shown.append(text))
    monkeypatch.setattr("ui.help_texts.QToolTip.hideText", lambda *a, **k: hidden.append(1))
    panel = ExtinctionPanel(default_config())
    panel.resize(700, 420)
    for _ in range(2):
        install_app_help(panel)
    panel.show()
    panel.activateWindow()
    _pump(qt_app)
    btn = panel.btn_run
    try:
        for _ in range(3):
            QTest.mouseMove(panel, QPoint(4, 4))
            _pump(qt_app, 30)
            QTest.mouseMove(btn, btn.rect().center())
            _pump(qt_app, 60)
        reasons = [t for t in shown if "光谱目录" in t]
        assert len(reasons) >= 3, shown
        assert len(shown) <= 4  # 一次进入不应重复两次（O1）
        panel.data_dir = panel.data_dir or __import__("pathlib").Path("test_temp")
        panel._files = []
        assert "可解析" in __import__("ui.help_texts", fromlist=["resolve_help"]).resolve_help(btn)
    finally:
        panel.close()
        panel.deleteLater()


@pytest.mark.slow
def test_mainwindow_audit_and_1024_phi_layout(qt_app, tmp_path):
    from core.config import default_config
    from ui.help_texts import audit_help
    from ui.window.main_window import MainWindow
    cfg = default_config()
    cfg.setdefault("gui", {}).update({"show_welcome": False, "recent_dirs": [],
                                      "out_root": str(tmp_path / "runs")})
    win = MainWindow(cfg)
    try:
        win.resize(1024, 640)
        win.show()
        _pump(qt_app)
        win.nav.select("analysis")
        _pump(qt_app, 120)
        rows = audit_help(win)
        assert rows
        assert not [r for r in rows if r["status"] in ("missing", "generic")]
        p = win.phi_page
        assert p.width() < 1000
        body = p._body
        vis = [body.widget(i) for i in range(body.count()) if body.widget(i).isVisible()]
        assert len(vis) == 2 and not p.settings_panel.isVisible()
        assert not any(vis[a].geometry().intersects(vis[b].geometry())
                       for a in range(len(vis)) for b in range(a + 1, len(vis)))
        p.btn_settings_toggle.setChecked(True)
        _pump(qt_app, 60)
        vis = [body.widget(i) for i in range(body.count()) if body.widget(i).isVisible()]
        assert p.settings_panel.isVisible()
        assert not any(vis[a].geometry().intersects(vis[b].geometry())
                       for a in range(len(vis)) for b in range(a + 1, len(vis)))
        win.grab().save(str(tmp_path / "analysis_1024_fixed.png"))
    finally:
        win.close()
        win.deleteLater()
        _pump(qt_app, 30)


@pytest.mark.slow
def test_page_and_dialog_hover_help_visible(qt_app, monkeypatch):
    from PySide6.QtWidgets import QDialog, QPushButton
    from core.config import default_config
    from ui.help_texts import GENERIC_STATIC_PATTERNS, install_app_help
    from ui.window.main_window import MainWindow
    shown = []
    monkeypatch.setattr("ui.help_texts.QToolTip.showText",
                        lambda pos, text, *a, **k: shown.append(text))
    cfg = default_config()
    cfg.setdefault("gui", {}).update({"show_welcome": False, "recent_dirs": []})
    win = MainWindow(cfg)
    try:
        win.resize(1366, 768)
        win.show()
        _pump(qt_app)
        hit_pages = 0
        for key in list(win.nav._keys):
            win.nav.select(key)
            _pump(qt_app, 40)
            btn = None
            for b in win.findChildren(QPushButton):
                if b.isVisible() and b.toolTip():
                    btn = b
                    break
            if btn is None:
                continue
            shown.clear()
            QTest.mouseMove(btn, btn.rect().center())
            _pump(qt_app, 80)
            if shown:
                hit_pages += 1
                assert not any(p in shown[-1] for p in GENERIC_STATIC_PATTERNS)
        assert hit_pages >= 4
    finally:
        win.close()
        win.deleteLater()
        _pump(qt_app, 30)
    dlg = QDialog()
    b = QPushButton("对话框执行", dlg)
    install_app_help(dlg)
    dlg.resize(320, 160)
    dlg.show()
    dlg.activateWindow()
    _pump(qt_app, 60)
    shown.clear()
    QTest.mouseMove(b, b.rect().center())
    _pump(qt_app, 80)
    assert shown
    dlg.close()
    dlg.deleteLater()


@pytest.mark.slow
def test_audit_can_detect_generic(monkeypatch):
    from PySide6.QtWidgets import QPushButton, QWidget
    from ui.help_texts import audit_help
    for phrase in ("点击执行本页对应操作。",
                    "执行按钮「某按钮」所标注的操作；结果会更新当前页状态或输出目录。",
                    "「abs_color」是本页分析/出图设置项；修改后需重新计算或出图才生效。",
                    "该控件属于当前页选项组；具体含义由所在行标签决定。",
                    "窗口工具按钮：执行当前工具栏/页签的对应操作。",
                    "「出图预设」：该行参数的标签，字段本身可查看具体含义。"):
        root = QWidget()
        b = QPushButton("某按钮", root)
        b.setToolTip(phrase)
        # 不 install，直接审计原始控件：必须判为 generic
        rows = audit_help(root)
        row = next(r for r in rows if r["widget"] == "QPushButton")
        assert row["status"] == "generic", phrase
        root.deleteLater()
