# -*- coding: utf-8 -*-
"""状态栏：任务终点要报给主窗口的进度条；非流水线任务的完成不当成失败。"""
from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from core.config import default_config


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _wait(app, pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_page_task_reports_end_of_progress(qt_app):
    """任务中途报 50%，结束时必须再报一次终点（以前主窗口进度条会停在 50%）。"""
    from PySide6.QtWidgets import QVBoxLayout
    from ui.pages.base import PageBase
    page = PageBase(default_config())
    QVBoxLayout(page).addWidget(page.progress_row())
    seen = []
    page.progress.connect(lambda pct, stage: seen.append(pct))

    def task(progress_cb, log_cb):
        progress_cb(50, "一半")
        return [1, 2, 3]                       # 像载入清单那样返回 list

    page.run_task(task, "测试任务", lambda _res: None)
    assert _wait(qt_app, lambda: not page._busy and seen and seen[-1] == 100)
    assert seen[0] == 50


def test_run_finished_without_ok_is_not_a_warning(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "cfg.yaml"))
    from ui.window.main_window import MainWindow
    cfg = default_config()
    cfg.setdefault("gui", {}).update({"show_welcome": False, "recent_dirs": []})
    win = MainWindow(cfg)
    logged = []
    monkeypatch.setattr(win, "on_logged", lambda level, msg: logged.append((level, msg)))
    try:
        win._on_page_run_finished([1, 2])                       # 载入清单
        win._on_page_run_finished(("table", "rows"))            # ε / A@λ 结果
        assert logged == []
        win._on_page_run_finished(SimpleNamespace(ok=False, message="缺参比组"))
        win._on_page_run_finished(SimpleNamespace(ok=True, message=""))
        assert logged == [("WARNING", "任务没有得到结果：缺参比组"), ("INFO", "任务完成")]
    finally:
        win.close()
        win.deleteLater()
