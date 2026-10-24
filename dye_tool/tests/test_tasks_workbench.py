# -*- coding: utf-8 -*-
"""ε / A@λ / 峰分析三张任务卡的混搭工作台骨架：流程条、检查器分步、结论卡。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _make(qt_app, cls_path: str):
    import importlib
    from core.config import default_config
    mod, cls = cls_path.rsplit(".", 1)
    return getattr(importlib.import_module(mod), cls)(default_config())


@pytest.mark.parametrize("cls_path,n_steps", [
    ("ui.pages.tasks.extinction.ExtinctionPanel", 4),
    ("ui.pages.tasks.wavelength.WavelengthPanel", 3),
    ("ui.pages.tasks.peaks.PeaksPanel", 3),
])
def test_panel_flow_bar_and_inspector_steps(qt_app, tmp_path, cls_path, n_steps):
    from ui.widgets.flow_bar import FlowBar
    p = _make(qt_app, cls_path)
    try:
        bar = p.top_bar_widget()
        assert isinstance(bar, FlowBar) and bar is p.top_bar_widget()   # 每次同一对象
        assert bar.count() == n_steps
        assert p.inspector.count() == n_steps
        assert p.inspector.current() == 0 and bar.current() == 0
        # 没选目录时后面的步不可用：点了回到第 0 步（WavelengthPanel 的「填波长」除外）
        p._go_step(n_steps - 1)
        assert p.inspector.current() == 0
        # 选了目录后每一步都能直接点，检查器换页、流程条高亮跟着走
        (tmp_path / "a.txt").write_text("400\t0.1\n", encoding="utf-8")
        if hasattr(p, "set_data_dir"):
            p.set_data_dir(tmp_path)
        else:
            p.data_dir = tmp_path
        for i in reversed(range(n_steps)):
            p._go_step(i)
            assert p.inspector.current() == i and bar.current() == i
            assert p.inspector.stack.currentIndex() == i
            assert p.inspector.stack.currentWidget() is p.inspector.page(i)
        # 主按钮在检查器里（不在交给分析页的流程条里）
        assert p.inspector.isAncestorOf(p.btn_run)
        assert not bar.isAncestorOf(p.btn_run)
    finally:
        p.deleteLater()


def test_extinction_verdict_flow_and_stale(qt_app, tmp_path):
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO
    p = _make(qt_app, "ui.pages.tasks.extinction.ExtinctionPanel")
    try:
        for n in ("s1.txt", "s2.txt"):
            (tmp_path / n).write_text("400\t0.1\n500\t0.2\n", encoding="utf-8")
        assert p.verdict.mode() == "empty"
        p.set_data_dir(tmp_path)
        if not p._files:
            pytest.skip("样例文件未被枚举（读入规则不同）")
        assert p.inspector.current() == 1                   # 载入后直接到「填浓度」
        assert p.flow_bar.state(0) == STEP_DONE and p.flow_bar.state(1) == STEP_TODO
        for r in range(p.tbl_conc.rowCount()):
            p.tbl_conc.item(r, p.CONC_COL).setText(str(r + 1))
        assert p.flow_bar.state(1) == STEP_DONE
        p._run_key = p._current_key()
        res = {"ok": True, "eps": 52300.0, "eps_unit": "M⁻¹cm⁻¹", "r2": 0.9991,
               "r2_warn": 0.995, "n_points": 2, "n_dropped": 0, "eps_stderr": 410.0,
               "wavelength": 512.0, "wavelength_mode": "lambda_max", "force_origin": True,
               "issues": []}
        p._finish((res, tmp_path / "extinction.csv"))
        assert p.verdict.mode() == "verdict"
        assert p.verdict.value_text() == "52,300"
        assert p.verdict.lbl_error.text() == "L·mol⁻¹·cm⁻¹"
        assert "R² = 0.9991" in p.verdict.headline_text() and "2 个浓度" in p.verdict.headline_text()
        assert any("偏高" in lbl.full_text() for lbl in p.verdict.lbl_lines)  # 选点偏差提示
        assert p.inspector.current() == 3
        assert p.flow_bar.state(2) == STEP_DONE and p.flow_bar.state(3) == STEP_DONE
        # 改了浓度 → 计算与导出标成"需要重算"
        p.tbl_conc.item(0, p.CONC_COL).setText("9")
        assert p.flow_bar.state(2) == STEP_STALE and p.flow_bar.state(3) == STEP_STALE
        p._finish(({"ok": False, "reason": "浓度全相同"}, tmp_path / "extinction.csv"))
        assert p.verdict.mode() == "failed"
    finally:
        p.deleteLater()


def test_wavelength_verdict_mode_and_data_dir_property(qt_app, tmp_path):
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE
    p = _make(qt_app, "ui.pages.tasks.wavelength.WavelengthPanel")
    try:
        assert not p.flow_bar.step_enabled(2)
        p.data_dir = tmp_path                       # 分析页接批次就是直接赋值
        assert p.flow_bar.step_enabled(2) and p.inspector.current() == 1
        assert isinstance(p.data_dir, Path)
        p.cmb_mode.setCurrentIndex(1)
        assert p.interpolate() and not p.sp_tol.isEnabled() and p.mode_text() == "线性插值"
        p._run_key, p._run_mode = p._current_key(), p.mode_text()
        tab = {"ok": True, "wavelengths": [400.0, 450.0], "columns": ["A@400.0", "A@450.0"],
               "rows": [{"file": "a.txt", "group": "g", "A@400.0": 0.1, "A@450.0": float("nan")},
                        {"file": "b.txt", "group": "g", "A@400.0": 0.2, "A@450.0": 0.3}],
               "skipped": [], "issues": [],
               "stats": {"A@400.0": {"n": 2, "mean": 0.15}, "A@450.0": {"n": 1, "mean": 0.3}}}
        p._finish((tab, tmp_path / "a_at_wavelength.csv"))
        assert p.verdict.value_text() == "2 × 2"
        assert p.verdict.headline_text() == "2 个谱 × 2 个波长 · 线性插值"
        assert p.tbl_result.rowCount() == 2 and p.tbl_result.columnCount() == 4
        assert p.tbl_result.item(0, 3).text() == "—"
        assert p.flow_bar.state(2) == STEP_DONE and p.inspector.current() == 2
        p.cmb_mode.setCurrentIndex(0)                # 改取值方式 → 导出过期
        assert p.flow_bar.state(2) == STEP_STALE
    finally:
        p.deleteLater()


def test_peaks_verdict_params_and_stale(qt_app, tmp_path):
    from core.analysis.peaks import DEFAULT_PROMINENCE_REL
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE
    p = _make(qt_app, "ui.pages.tasks.peaks.PeaksPanel")
    try:
        assert p.find_params()["prominence_rel"] == pytest.approx(DEFAULT_PROMINENCE_REL)
        p.set_data_dir(tmp_path)
        assert p.inspector.current() == 1
        p._run_key = p._current_key()
        rows = [("a.txt", 1, "380.0000", "0.5", "0.200000", "8", "8.0"),
                ("a.txt", 2, "512.0000", "1.0", "0.900000", "10", "10.0"),
                ("b.txt", 1, "513.0000", "0.9", "0.800000", "10", "10.0")]
        p._finish((rows, 2))
        assert p.verdict.value_text() == "3"
        assert "512.5 nm" in p.verdict.headline_text()       # 两个文件主峰的中位
        assert p.btn_export.isEnabled() and p.inspector.current() == 2
        assert p.flow_bar.state(1) == STEP_DONE
        out = p.export_csv(tmp_path / "peaks.csv")
        assert out is not None and p.flow_bar.state(2) == STEP_DONE
        p.sp_prom.setValue(10.0)                             # 改判据 → 峰表与导出过期
        assert p.flow_bar.state(1) == STEP_STALE and p.flow_bar.state(2) == STEP_STALE
        p._finish(([], 2))
        assert p.verdict.mode() == "failed" and not p.btn_export.isEnabled()
    finally:
        p.deleteLater()
