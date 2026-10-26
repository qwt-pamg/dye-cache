# -*- coding: utf-8 -*-
"""V2.8 FR-53：presets.yaml ③stack / ④spectra 显示预设（差量 + deep_merge + 往返）。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from core.config import default_config
from core.render.plotstyle import (deep_merge_preset, delete_display_preset,
                                   load_display_preset, list_display_presets,
                                   save_display_preset, save_user_preset)

pytestmark = pytest.mark.fast
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _destroy(app, widget):
    try:
        widget.close()
    except Exception:                       # noqa: BLE001
        pass
    widget.deleteLater()
    for _ in range(4):
        app.processEvents()


def test_display_presets_core_roundtrip_keeps_other_sections(tmp_path):
    p = tmp_path / "presets.yaml"
    save_user_preset("jacs-x", {"width_in": 7.0}, p)
    assert save_display_preset("stack", "s1", {"jacs": False}, p).is_file()
    save_display_preset("spectra", "v1", {"mode": "叠图"}, p)
    assert load_display_preset("stack", "s1", p) == {"jacs": False}
    assert list_display_presets("spectra", p) == ["v1"]
    # 旧 plot 节没被覆盖
    from core.render.plotstyle import load_preset
    assert load_preset("jacs-x", p)["width_in"] == 7.0
    assert delete_display_preset("stack", "s1", p) is True
    assert load_display_preset("stack", "s1", p) is None
    assert delete_display_preset("stack", "nope", p) is False
    with pytest.raises(ValueError):
        save_display_preset("plot", "bad", {}, p)


def test_display_preset_bad_keys_tolerated_by_pages(qt_app):
    from ui.pages.stack import StackPage
    cfg = default_config()
    page = StackPage(cfg)
    try:
        page.apply_display_preset({"unknown": 1, "cmap": "plasma"})
        assert page.ed_cmap.text() == "plasma"
    finally:
        _destroy(qt_app, page)


def test_stack_preset_save_apply_roundtrip(qt_app, tmp_path):
    from ui.pages.stack import StackPage
    cfg = default_config()
    page = StackPage(cfg)
    try:
        page.chk_jacs.setChecked(False)
        page.chk_overlay.setChecked(True)
        page.chk_despike.setChecked(False)
        page.ed_cmap.setText("plasma")
        page.chk_probe.setChecked(False)
        page.chk_fluor.setChecked(True)
        vals = page.display_preset_values()
        p = tmp_path / "presets.yaml"
        save_display_preset("stack", "mine", vals, p)
        page2 = StackPage(cfg)
        try:
            page2.apply_display_preset(load_display_preset("stack", "mine", p))
            assert page2.display_preset_values() == vals
        finally:
            _destroy(qt_app, page2)
    finally:
        _destroy(qt_app, page)


def test_spectra_preset_save_apply_roundtrip(qt_app, tmp_path):
    from ui.pages.spectra import SpectraPage
    cfg = default_config()
    page = SpectraPage(cfg)
    try:
        page.cmb_mode.setCurrentIndex(page.cmb_mode.count() - 1)
        page.cmb_norm.setCurrentIndex(1)
        page.chk_despike.setChecked(False)
        vals = page.display_preset_values()
        p = tmp_path / "presets.yaml"
        save_display_preset("spectra", "mine", vals, p)
        page2 = SpectraPage(cfg)
        try:
            page2.apply_display_preset(load_display_preset("spectra", "mine", p))
            assert page2.display_preset_values() == vals
        finally:
            _destroy(qt_app, page2)
    finally:
        _destroy(qt_app, page)


def test_deep_merge_preset_is_pure():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    delta = {"a": {"y": 9, "z": 4}, "c": 5}
    out = deep_merge_preset(base, delta)
    assert out == {"a": {"x": 1, "y": 9, "z": 4}, "b": 3, "c": 5}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_preset_ops_do_not_touch_config_yaml(tmp_path, monkeypatch):
    real = ROOT / "config.yaml"
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    p = tmp_path / "presets.yaml"
    save_display_preset("stack", "x", {"jacs": True}, p)
    save_display_preset("spectra", "y", {"norm": "不归一"}, p)
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before
