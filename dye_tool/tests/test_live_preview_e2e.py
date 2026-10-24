# -*- coding: utf-8 -*-
"""V2.9.5 FR-73：真实数据四页预览端到端（slow；full 档）。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from conftest import SAMPLES_UVVIS
from core.config import default_config
from core.io import collect_scans, load_spectra_files


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _wait_until(app, pred, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.slow
def test_phi_layout_three_sizes_screenshots(qt_app):
    from ui.pages.phi_page import PhiPage
    page = PhiPage(default_config())
    try:
        assert 260 <= page.settings_panel.minimumWidth() <= 280
        from PySide6.QtWidgets import QScrollArea, QSplitter
        assert page.settings_panel.findChild(QScrollArea) is not None
        splitters = page.findChildren(QSplitter)
        body = next(s for s in splitters if s.count() == 3)
        assert body is page._body
        out = Path('test_temp') / 'v294_sizes'
        out.mkdir(parents=True, exist_ok=True)
        for w, h in ((1024, 640), (1366, 768), (1638, 1001)):
            page.setFixedSize(w, h)
            page.show()
            qt_app.processEvents()
            tbl = page.files.table
            hdr = tbl.horizontalHeader()
            last = tbl.columnCount() - 1
            need = tbl.fontMetrics().horizontalAdvance(tbl.horizontalHeaderItem(last).text()) + 8
            assert hdr.sectionSize(last) >= need, (w, hdr.sectionSize(last), need)
            assert not tbl.horizontalScrollBar().isVisible()
            assert page.minimumSizeHint().width() <= w, (w, page.minimumSizeHint().width())
            vis = [body.widget(i) for i in range(body.count()) if body.widget(i).isVisible()]
            assert not any(vis[a].geometry().intersects(vis[b].geometry())
                           for a in range(len(vis)) for b in range(a + 1, len(vis))), (w, [v.geometry().getRect() for v in vis])
            page.grab().save(str(out / f'phi_{w}x{h}.png'))
            page.setMinimumSize(0, 0)
            page.setMaximumSize(16777215, 16777215)
    finally:
        page.deleteLater()


@pytest.mark.slow
def test_phi_live_preview_real_data(qt_app, tmp_path):
    from core.pipeline import run_phi_pipeline
    from ui.pages.phi_page import PhiPage

    cfg = default_config()
    cfg.setdefault("gui", {})["out_root"] = str(tmp_path / "runs")
    rr = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path / "runs", write=False)
    assert rr.ok and rr.results
    page = PhiPage(cfg)
    try:
        page._last_rr = rr
        page._preview_cfg = page.params.config(cfg)
        before = set((tmp_path / "runs").rglob("fig_*.png")) if (tmp_path / "runs").exists() else set()
        start = time.time()
        page.live_preview.schedule()
        assert _wait_until(qt_app, lambda: bool(page.canvas._images), 25)
        assert page.canvas._images
        assert time.time() - start < 20
        # 预览只写临时目录：正式 runs 的图件集合不应变化
        after = set((tmp_path / "runs").rglob("fig_*.png")) if (tmp_path / "runs").exists() else set()
        assert after == before
    finally:
        page.deleteLater()


@pytest.mark.slow
def test_stack_live_preview_real_data(qt_app, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import ui.pages.stack_page as stack_mod
    from ui.pages.stack_page import StackPage
    cfg = default_config()
    cfg.setdefault("gui", {})["out_root"] = str(tmp_path / "runs")
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    page = StackPage(cfg)
    try:
        page.scans = scans
        start = time.time()
        page._schedule_preview()
        assert _wait_until(qt_app, lambda: bool(page.canvas._images), 30)
        assert page.canvas._images
        assert time.time() - start < 25
        # 页面级关闭：关闭后改参数不再渲染
        page.chk_live.setChecked(False)
        assert page.live_preview.is_enabled() is False
        page._schedule_preview()
        _wait_until(qt_app, lambda: False, 0.4)
        # 页面级防抖：连续三次改参只触发一次最终渲染
        calls = []
        def fake(out):
            calls.append(out)
            p = Path(out) / "p.png"
            p.write_bytes(b"x")
            return [p]
        page.chk_live.setChecked(True)
        page.live_preview._render_fn = fake
        for i in range(3):
            page._mapped_preview_values = {"marker_size": 4.0 + i}
            page._schedule_preview()
        assert _wait_until(qt_app, lambda: len(calls) >= 1, 5)
        assert len(calls) == 1
        # 样式映射键进入预览 cfg（不再是旧值）
        page._mapped_preview_values = {"marker_size": 7.0}
        captured = {}
        monkeypatch.setattr(stack_mod, "run_stack",
                            lambda scans, cfg, **kw: captured.update(cfg) or SimpleNamespace(files=[]))
        assert page._render_live_preview(tmp_path / "preview_probe") == []
        assert captured["plot"]["jacs"]["marker_size"] == 7.0
    finally:
        page.deleteLater()


@pytest.mark.slow
def test_spectra_live_preview_real_data(qt_app, tmp_path):
    from ui.pages.spectra_page import SpectraPage
    cfg = default_config()
    cfg.setdefault("gui", {})["out_root"] = str(tmp_path / "runs")
    paths = sorted(SAMPLES_UVVIS.glob("*.txt"))
    spectra = load_spectra_files(paths, cfg)
    page = SpectraPage(cfg)
    try:
        page.spectra = spectra
        page._schedule_preview()
        assert _wait_until(qt_app, lambda: bool(page.canvas._images), 30)
        assert page.canvas._images
    finally:
        page.deleteLater()
