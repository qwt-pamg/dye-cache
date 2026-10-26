# -*- coding: utf-8 -*-
"""V2.9.5 FR-73：实时生图预览控制器（防抖/临时目录/关闭/stale）。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QWidget


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait(app, ms=1500):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def test_debounce_single_render_and_temp_only(qt_app, tmp_path):
    from ui.widgets.live_render_preview import LiveRenderPreview
    parent = QWidget()
    calls = []

    def render(out):
        calls.append(Path(out))
        p = Path(out) / "preview.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        return [p]

    ctl = LiveRenderPreview(parent, render, debounce_ms=80)
    got = []
    ctl.updated.connect(lambda files: got.extend(files))
    for _ in range(3):
        ctl.schedule()
    _wait(qt_app, 1200)
    assert len(calls) == 1
    assert got and got[0].name == "preview.png"
    assert Path(calls[0]).name.startswith("dye_preview_")
    assert Path(calls[0]).parent.name == ".preview_tmp"
    assert tmp_path not in Path(calls[0]).parents
    parent.deleteLater()


def test_disabled_does_not_render(qt_app):
    from ui.widgets.live_render_preview import LiveRenderPreview
    parent = QWidget()
    calls = []
    ctl = LiveRenderPreview(parent, lambda out: calls.append(out) or [], debounce_ms=50)
    ctl.set_enabled(False)
    ctl.schedule()
    _wait(qt_app, 300)
    assert calls == []
    parent.deleteLater()



def test_enable_toggle_and_shutdown(qt_app):
    from ui.widgets.live_render_preview import LiveRenderPreview
    parent = QWidget()
    ctl = LiveRenderPreview(parent, lambda out: [], debounce_ms=40)
    states = []
    ctl.state.connect(states.append)
    ctl.set_enabled(False)
    ctl.schedule()
    _wait(qt_app, 150)
    assert '已关闭' in states[-1]
    ctl.set_enabled(True)
    ctl.shutdown()
    parent.deleteLater()


def test_render_failure_sets_failed_state(qt_app):
    from ui.widgets.live_render_preview import LiveRenderPreview
    parent = QWidget()
    def boom(out):
        raise RuntimeError('boom')
    ctl = LiveRenderPreview(parent, boom, debounce_ms=40)
    states, errs = [], []
    ctl.state.connect(states.append)
    ctl.failed.connect(errs.append)
    ctl.schedule()
    _wait(qt_app, 600)
    assert any('失败' in s for s in states)
    assert errs and 'boom' in errs[0]
    parent.deleteLater()


def test_stale_result_is_discarded(qt_app):
    from ui.widgets.live_render_preview import LiveRenderPreview
    parent = QWidget()
    calls = []
    def render(out):
        calls.append(1)
        time.sleep(0.12)
        p = Path(out) / 'x.png'
        p.write_bytes(b'x')
        return [p]
    ctl = LiveRenderPreview(parent, render, debounce_ms=20)
    states = []
    ctl.state.connect(states.append)
    ctl.schedule()
    time.sleep(0.05)
    ctl.schedule()
    _wait(qt_app, 1200)
    assert len(calls) >= 1
    assert any('丢弃' in s or '更新' in s for s in states)
    parent.deleteLater()


def test_pages_have_live_preview_switch(qt_app):
    from core.config import default_config
    from ui.pages.phi import PhiPage
    from ui.pages.spectra import SpectraPage
    from ui.pages.stack import StackPage
    pages = [PhiPage(default_config()), SpectraPage(default_config()),
             StackPage(default_config())]
    try:
        for p in pages:
            assert hasattr(p, "chk_live") and p.chk_live.isChecked()
            assert hasattr(p, "live_preview")
            assert hasattr(p, "lbl_live")
    finally:
        for p in pages:
            p.deleteLater()
