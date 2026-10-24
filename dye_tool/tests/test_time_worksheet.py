# -*- coding: utf-8 -*-
"""V2.9 FR-60：时间整定 Origin 化（单位 / 三态 / 持久化 / 无后缀提示）测试。

覆盖 60.1–60.4；GUI 用离屏 Qt，批文件全部落 pytest tmp_path。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from core.config import default_config
from core.inputs.batch_file import (apply_to_scans, empty_batch, from_scans,
                                    load_batch, save_batch)
from core.inputs.io import clear_scan_cache, collect_scans
from ui.widgets.time_units import format_time_value, parse_time_input
from conftest import make_scan

pytestmark = pytest.mark.fast
ROOT = Path(__file__).resolve().parents[1]


def _write_two_col(path: Path, n: int = 20) -> Path:
    rows = [f"{400.0 + i},{1.0 + 0.01 * i}" for i in range(n)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _scan(tmp_path: Path, name: str = "sample-30.txt", t: float = 30.0):
    return make_scan(tmp_path / name, np.arange(400.0, 420.0),
                     np.ones(20), group="sample", t=t)


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ── FR-60.3 单位换算 ──────────────────────────────────────────────
@pytest.mark.parametrize("text,seconds", [
    ("300", 300.0), ("5min", 300.0), (" 1.5h ", 5400.0),
    ("5 MIN", 300.0), ("5分钟", 300.0), ("1.5 小时", 5400.0),
    ("0", 0.0), ("-5s", None), ("abc", None), ("5x", None),
])
def test_parse_time_input_units(text, seconds):
    assert parse_time_input(text) == seconds


def test_format_time_value_units():
    assert format_time_value(300.0, "s") == "300"
    assert format_time_value(300.0, "min") == "5"
    assert format_time_value(3600.0, "h") == "1"


# ── FR-60.1 无时间后缀提示 ────────────────────────────────────────
def test_no_time_suffix_scan_has_t_zero_and_flag(tmp_path):
    _write_two_col(tmp_path / "no_time_suffix.txt")
    cfg = default_config()
    clear_scan_cache()
    scans = [s for s in collect_scans(tmp_path, cfg) if not s.excluded]
    assert scans and scans[0].t == 0.0
    assert scans[0].time_suffix is False


# ── FR-60.2 批文件持久化 ─────────────────────────────────────────
def test_load_batch_missing_does_not_create_file(tmp_path):
    assert load_batch(tmp_path).files == {}
    assert not (tmp_path / "dye_batch.yaml").exists()


def test_time_and_dup_roundtrip_through_batch(tmp_path):
    _write_two_col(tmp_path / "sample_2-30.txt")
    cfg = default_config()
    clear_scan_cache()
    scans = collect_scans(tmp_path, cfg)
    s = next(s for s in scans if not s.excluded)
    assert s.rep == "2" and s.rep_src == "2"
    s.t = 300.0
    s.rep = "9"
    batch = from_scans(scans, tmp_path, base=empty_batch(tmp_path))
    ok, why = save_batch(batch, tmp_path)
    assert ok, why
    clear_scan_cache()
    scans2 = collect_scans(tmp_path, cfg)
    batch2 = load_batch(tmp_path)
    applied = apply_to_scans(batch2, scans2, tmp_path)
    assert applied == 1
    s2 = next(s for s in scans2 if not s.excluded)
    assert s2.t == 300.0 and s2.t_edited
    assert s2.rep == "9" and s2.rep_edited


def test_save_batch_readonly_returns_false_not_raise(tmp_path, monkeypatch):
    import core.inputs.batch_file as bf
    def boom(*_a, **_k):
        raise OSError("read-only")
    monkeypatch.setattr(bf, "atomic_write", boom)
    ok, why = save_batch(empty_batch(tmp_path), tmp_path)
    assert ok is False and "不可写" in why


# ── FR-60.3/60.4 GUI ────────────────────────────────────────────
def test_panel_unit_switch_only_changes_display(qt_app, tmp_path):
    from ui.widgets.dye_time_panel import DyeTimePanel, T_EFF
    scans = [_scan(tmp_path)]
    panel = DyeTimePanel()
    try:
        panel.set_scans(scans, data_dir=tmp_path)
        assert panel.tbl_time.item(0, T_EFF).text() == "30"
        panel.set_time_unit("min")
        assert panel.tbl_time.item(0, T_EFF).text() == "0.5"
        assert scans[0].t == 30.0
        panel.set_time_unit("s")
        assert panel.tbl_time.item(0, T_EFF).text() == "30"
    finally:
        panel.deleteLater()


def test_panel_three_states_blank_and_restore(qt_app, tmp_path):
    from ui.widgets.dye_time_panel import DyeTimePanel, T_EFF, T_NOTE
    scans = [_scan(tmp_path)]
    panel = DyeTimePanel()
    try:
        panel.set_scans(scans, data_dir=tmp_path)
        panel.set_time_mode("blank")
        assert scans[0].time_blank is True
        assert scans[0].ok is False
        assert panel.tbl_time.item(0, T_EFF).text() == ""
        assert panel.tbl_time.item(0, T_NOTE).text() == "时间未定"
        panel.set_time_mode("auto")
        assert scans[0].time_blank is False
        assert scans[0].ok is True
        assert panel.tbl_time.item(0, T_EFF).text() == "30"
        panel.set_time_mode("blank")
        item = panel.tbl_time.item(0, T_EFF)
        item.setText("5min")
        assert scans[0].t == 300.0
        assert scans[0].time_blank is False and scans[0].ok is True
    finally:
        panel.deleteLater()


def test_file_table_no_suffix_hint_and_unit(qt_app, tmp_path):
    from ui.widgets.file_table import COL_STATUS, COL_TIME, FileTable
    s = _scan(tmp_path, "no_time_suffix.txt", t=300.0)
    s.time_suffix = False
    table = FileTable()
    try:
        table.set_scans([s])
        assert "文件名未含时间" in table.table.item(0, COL_STATUS).text()
        table.set_time_unit("min")
        assert table.table.item(0, COL_TIME).text() == "5"
        assert s.t == 300.0
    finally:
        table.deleteLater()


def test_data_page_save_then_reload_restores_time(qt_app, tmp_path):
    from ui.pages.data import DataPage
    _write_two_col(tmp_path / "sample-30.txt")
    cfg = default_config()
    clear_scan_cache()
    scans = collect_scans(tmp_path, cfg)
    page = DataPage(default_config())
    try:
        page.data_dir = tmp_path
        page._on_loaded((scans, empty_batch(tmp_path)))
        s = next(x for x in page.scans if not x.excluded)
        s.t = 300.0
        page.save_batch()
        assert (tmp_path / "dye_batch.yaml").is_file()
        clear_scan_cache()
        scans2 = collect_scans(tmp_path, cfg)
        apply_to_scans(load_batch(tmp_path), scans2, tmp_path)
        s2 = next(x for x in scans2 if not x.excluded)
        assert s2.t == 300.0 and s2.t_edited
    finally:
        page.deleteLater()




# ═══════════════ V2.9.1 FR-61/63：批次参比/Φ_ref 持久化 ═══════════════
def test_batch_reference_group_and_phi_ref_roundtrip(tmp_path):
    _write_two_col(tmp_path / 'A-1.txt')
    _write_two_col(tmp_path / 'B-1.txt')
    cfg = default_config()
    clear_scan_cache()
    scans = collect_scans(tmp_path, cfg)
    batch = empty_batch(tmp_path)
    batch.reference_group = 'A'
    batch.phi_ref = 0.42
    ok, why = save_batch(batch, tmp_path)
    assert ok, why
    batch2 = load_batch(tmp_path)
    assert batch2.reference_group == 'A'
    assert batch2.phi_ref == 0.42
    apply_to_scans(batch2, scans, tmp_path)
    assert next(s for s in scans if s.path.name.startswith('A')).role == 'reference'
    assert next(s for s in scans if s.path.name.startswith('B')).role == 'sample'


def test_batch_without_phi_ref_falls_back_to_none(tmp_path):
    b = load_batch(tmp_path)
    assert b.phi_ref is None and b.reference_group == ''




# ═══════════════ V2.9.1 FR-62/63：对比页 UI 初值/记忆 ═══════════════
def test_compare_page_phi_ref_default_and_controls(qt_app):
    from ui.pages.compare import ComparePage
    page = ComparePage(default_config())
    try:
        assert page.sp_phi_ref.value() == pytest.approx(0.75)
        assert page.cmb_ref_side.count() == 2
        assert page.cmb_ref_side.currentIndex() == 0
    finally:
        page.deleteLater()


def test_compare_page_remember_writes_batch(qt_app, tmp_path):
    from ui.pages.compare import ComparePage
    page = ComparePage(default_config())
    try:
        page.data_dir = tmp_path
        page.groups = [{"label": "A"}, {"label": "B"}]
        page.cmb_a.addItem("A")
        page.cmb_b.addItem("B")
        page.sp_phi_ref.setValue(0.42)
        assert page._remember_prefs() is True
        b = load_batch(tmp_path)
        assert b.reference_group == "A" and b.phi_ref == pytest.approx(0.42)
    finally:
        page.deleteLater()




# ═══════════ V2.9.1 O 修复：跨视图同步 / 清除参比 / PhiPage 记忆 ═══════════
def test_data_page_file_time_change_refreshes_panel(qt_app, tmp_path):
    from ui.pages.data import DataPage
    from ui.widgets.dye_time_panel import T_EFF as PANEL_EFF
    s = _scan(tmp_path, "sample-30.txt", t=30.0)
    page = DataPage(default_config())
    try:
        page.data_dir = tmp_path
        page._on_loaded(([s], empty_batch(tmp_path)))
        assert page.panel.tbl_time.item(0, PANEL_EFF).text() == "30"
        s.t_src = 30.0
        s.t = 300.0
        page.files.time_changed.emit(s.path.name, 300.0)
        assert page.panel.tbl_time.item(0, PANEL_EFF).text() == "300"
        page.panel._restore_times()
        assert s.t == 30.0
    finally:
        page.deleteLater()


def test_from_scans_clears_base_reference_group(tmp_path):
    _write_two_col(tmp_path / "A-1.txt")
    cfg = default_config()
    clear_scan_cache()
    scans = collect_scans(tmp_path, cfg)
    base = empty_batch(tmp_path)
    base.reference_group = "OLD"
    out = from_scans(scans, tmp_path, base=base)
    assert out.reference_group == ""


def test_phi_page_remember_reference_and_phi_ref(qt_app, tmp_path):
    from ui.pages.phi import PhiPage
    page = PhiPage(default_config())
    try:
        page.data_dir = tmp_path
        page.cmb_reference.addItem("A", "A")
        page.cmb_reference.setCurrentIndex(0)
        page.sp_phi_ref.setValue(0.42)
        assert page._remember_reference() is True
        b = load_batch(tmp_path)
        assert b.reference_group == "A" and b.phi_ref == pytest.approx(0.42)
    finally:
        page.deleteLater()


def test_data_page_applies_batch_time_and_dup(qt_app, tmp_path):
    from ui.pages.data import DataPage
    s = _scan(tmp_path, "sample-30.txt", t=30.0)
    batch = empty_batch(tmp_path)
    batch.set_entry("sample-30.txt", t_override=300.0, dup_index="7")
    page = DataPage(default_config())
    try:
        page.data_dir = tmp_path
        page._on_loaded(([s], batch))
        assert s.t == 300.0 and s.t_edited
        assert s.rep == "7"
    finally:
        page.deleteLater()