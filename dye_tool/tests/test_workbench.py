# -*- coding: utf-8 -*-
"""V2.9.3 FR-66/67：工作台图式/增强（独立模块，不触 legacy 渲染路径）。"""
from __future__ import annotations

import numpy as np
import pytest

from core.config import default_config
from core.render.workbench_plot import normalize_formats, normalize_time_unit, plot_workbench


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _fixture():
    t = np.array([0.0, 30.0, 60.0, 120.0, 180.0])
    A = 1.0 * np.exp(-0.004 * t)
    rows = [
        {"label": "sample-A", "k_obs": 0.004, "phi_delta": 0.42, "phi_err": 0.03,
         "phi_ci_low": 0.38, "phi_ci_high": 0.46},
        {"label": "sample-B", "k_obs": 0.003, "phi_delta": 0.31, "phi_err": None,
         "phi_ci_low": None, "phi_ci_high": None},
    ]
    series = {r["label"]: {"t": t, "A": A, "fit": {"ok": True, "A0": 1.0, "k": 0.004, "c": 0.0}}
              for r in rows}
    return rows, series


def test_normalize_helpers():
    assert normalize_time_unit("MIN") == "min"
    assert normalize_time_unit("bogus") == "s"
    assert normalize_formats("PNG,png;svg,jpeg") == ("png", "svg", "jpg")
    assert normalize_formats(["tiff", "png", "PNG"]) == ("png",)


def test_plot_styles_units_annotate_and_formats(tmp_path):
    rows, series = _fixture()
    files = plot_workbench(rows, series, tmp_path, style="line", error_bars=True,
                           time_unit="min", annotate=True, transparent=True,
                           formats=("png", "svg", "pdf"), ci_band=True)
    names = {p.name for p in files}
    assert "workbench_kinetics.png" in names
    assert "workbench_phi.svg" in names
    assert "workbench_phi.pdf" in names
    assert all(p.stat().st_size > 0 for p in files)


def test_plot_scatter_without_errorbars(tmp_path):
    rows, series = _fixture()
    files = plot_workbench(rows, series, tmp_path, style="scatter",
                           error_bars=False, formats=("png",), annotate=False)
    assert (tmp_path / "workbench_kinetics.png").is_file()
    assert (tmp_path / "workbench_phi.png").is_file()
    assert len(files) == 2



# ═══════════ V2.9.3 FR-66：jacs fig c/d 图式（默认 = 2.7.0 形态） ═══════════
def _jacs_fixture():
    from types import SimpleNamespace
    fit_ok = SimpleNamespace(status='OK', k=1.2e-3, k_err=2e-5, r2=0.99,
                          n=12, A0=1.0, c=0.0, truncated=False)
    fit_bad = SimpleNamespace(status='FAILED', k=float('nan'),
                           k_err=float('nan'), r2=float('nan'), n=0,
                           A0=0.0, c=0.0, truncated=False)
    results = {'A': {'pooled': fit_ok}, 'B': {'pooled': fit_bad}}
    phi = {'A': {'status': 'OK', 'phi': 0.42, 'phi_err': 0.03}}
    return results, phi


def test_filter_formats_pdf_only_when_allowed():
    from core.render.plotstyle import filter_formats
    assert filter_formats(['png', 'pdf', 'PNG']) == ['png']
    assert filter_formats(['png', 'pdf'], allow_pdf=True) == ['png', 'pdf']


def test_param_panel_formats_picker_and_transparent(qt_app):
    from core.config import default_config
    from ui.widgets.param_panel import ParamPanel
    page = ParamPanel()
    try:
        page.ed_formats.setText('svg,png,PNG')
        assert page.ed_formats.text() == 'png,svg'
        cfg = default_config()
        page.apply_to(cfg)
        assert 'pdf' not in (cfg.get('plot', {}).get('jacs', {}) or {}).get('formats', [])
        page.chk_transparent.setChecked(True)
        from core.render import plotting_jacs
        page.apply_to(cfg)
        assert plotting_jacs.get_figure_session()['transparent'] is True
    finally:
        page.deleteLater()



def test_formats_picker_roundtrip_and_pdf(tmp_path):
    from core.render.plotstyle import filter_formats, load_preset, save_user_preset
    from ui.widgets.param_panel import FormatsPicker
    p = FormatsPicker()
    p.setText('PNG,svg,jpg,pdf')
    assert p.text() == 'png,svg,jpg,pdf'
    assert filter_formats(['png', 'pdf']) == ['png']
    assert filter_formats(['png', 'pdf'], allow_pdf=True) == ['png', 'pdf']
    path = tmp_path / 'presets.yaml'
    save_user_preset('fig-test', {'figure_style': 'line',
                                  'figure_error_bars': False,
                                  'figure_transparent': True,
                                  'figure_annotate': True}, path=path)
    vals = load_preset('fig-test', path=path)
    assert vals['figure_style'] == 'line'
    assert vals['figure_transparent'] is True


def test_figure_session_exposes_time_unit_and_options():
    from core.render import plotting_jacs as pj
    got = pj.set_figure_session(style='scatter', time_unit='min',
                                annotate=True, transparent=True)
    assert got['time_unit'] == 'min' and got['style'] == 'scatter'
    assert pj.get_figure_session()['annotate'] is True
    pj.set_figure_session(style=None, time_unit='s', annotate=False,
                          transparent=False, error_bars=True)


def test_param_panel_has_figure_and_time_controls(qt_app):
    from core.config import default_config
    from ui.widgets.param_panel import ParamPanel
    p = ParamPanel()
    try:
        assert p.cmb_fig_style.count() == 4
        assert p.cmb_time_unit.count() == 3
        assert p.chk_fig_err.isChecked() and not p.chk_fig_annot.isChecked()
        p.apply_to(default_config())
    finally:
        p.deleteLater()



def test_phi_page_recompute_phi_without_refit(qt_app, monkeypatch):
    from types import SimpleNamespace
    from ui.pages.phi import PhiPage
    page = PhiPage(default_config())
    try:
        calls = []
        monkeypatch.setattr(page, '_fill_table', lambda rr: calls.append('fill'))
        from core.analysis import phi as phi_mod
        monkeypatch.setattr(phi_mod, 'compute_phi', lambda results, cfg: {'A': {'status': 'OK', 'phi': 0.42, 'phi_err': 0.01}})
        page.sp_phi_ref.setValue(0.42)
        page._last_rr = SimpleNamespace(results={'A': {'role': 'sample'}}, phi={}, scans=[], controls={})
        page._reference_group = 'A'
        page._recompute_phi_only()
        assert page._last_rr.phi['A']['phi'] == 0.42
        assert calls == ['fill']
    finally:
        page.deleteLater()




def test_compare_page_workbench_controls_and_result_table(qt_app, tmp_path):
    from ui.pages.compare import ComparePage

    page = ComparePage(default_config())
    try:
        page.groups = [{"label": "A", "members": [], "lam_max": 570.0},
                       {"label": "B", "members": [], "lam_max": 570.0}]
        page._refresh_groups()
        assert page.cmb_ref.count() == 3
        assert page.cmb_style.count() == 3
        assert page.chk_fmt["png"].isChecked()
        page._fill_result_table([
            {"label": "A", "k_obs": 1e-3, "k_err": 2e-5, "r2": 0.99, "n": 10,
             "t_min": 0, "t_max": 180, "phi_delta": 0.75, "phi_err": 0.0, "note": "参比"},
            {"label": "B", "k_obs": 2e-3, "k_err": 3e-5, "r2": 0.98, "n": 10,
             "t_min": 0, "t_max": 180, "phi_delta": 0.5, "phi_err": 0.02, "note": ""},
        ])
        assert page.tbl_results.rowCount() == 2
        assert page.tbl_results.item(0, 0).text() == "A"
    finally:
        page.deleteLater()

