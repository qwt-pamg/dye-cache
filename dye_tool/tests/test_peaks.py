# -*- coding: utf-8 -*-
"""V2.7 A 道（V2.6 D 道结转）：FR-37 / A43 峰分析（纯函数 + CSV）。

对应验收 A43.1–A43.5 的测试侧；A43.6 红线由验收结构断言覆盖。
"""
from __future__ import annotations

import csv
import os
from pathlib import Path

import numpy as np
import pytest

from core.analysis.peaks import (CSV_HEADERS, Peak, find_peaks_spectrum,
                                 peak_rows, write_peaks_csv)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _gauss(x, mu, sigma=8.0):
    return np.exp(-0.5 * ((x - mu) / sigma) ** 2)


def _three_peak_spectrum():
    """合成三峰：430/480/530，峰高 1.0/0.35/0.7（可判定 prominent 关系）。"""
    wl = np.arange(400.0, 570.0, 1.0)
    y = 0.05 + _gauss(wl, 430.0) + 0.35 * _gauss(wl, 480.0) + 0.70 * _gauss(wl, 530.0)
    return wl, y


# ═══════════════════════ A43.1 合成三峰真值 ═══════════════════════
def test_a43_1_three_peaks_positions_and_relations():
    wl, y = _three_peak_spectrum()
    peaks = find_peaks_spectrum(wl, y)
    assert len(peaks) == 3, [p.wl_nm for p in peaks]
    want = [430.0, 480.0, 530.0]
    got = [p.wl_nm for p in peaks]
    for g, w in zip(got, want):
        assert abs(g - w) <= 1.0, (got, want)       # 峰位误差 ≤1 采样点
    by_wl = {round(p.wl_nm): p for p in peaks}
    # 强度关系：430 > 530 > 480；prominence 同序（480 的峰叠在 430 肩附近）
    assert by_wl[430].intensity > by_wl[530].intensity > by_wl[480].intensity
    assert by_wl[430].prominence_rel > by_wl[530].prominence_rel > by_wl[480].prominence_rel
    assert all(0.0 < p.prominence_rel <= 1.0 for p in peaks)
    assert all(p.width_pts >= 1 and p.width_nm > 0 for p in peaks)
    assert all(isinstance(p, Peak) for p in peaks)


# ═══════════════════════ A43.2 NaN 段分段 ═══════════════════════
def test_a43_2_nan_gap_splits_segments():
    wl = np.arange(400.0, 570.0, 1.0)
    y = 0.05 + _gauss(wl, 430.0) + _gauss(wl, 530.0)
    # 470–490 全 NaN（宽断点：任何插值都会跨过它造出假峰）
    y[(wl >= 470) & (wl <= 490)] = np.nan
    peaks = find_peaks_spectrum(wl, y)
    assert len(peaks) == 2, [p.wl_nm for p in peaks]
    got = [p.wl_nm for p in peaks]
    assert abs(got[0] - 430.0) <= 1.0 and abs(got[1] - 530.0) <= 1.0
    assert not any(468 <= p.wl_nm <= 492 for p in peaks), "断点两侧不得合并/插值出峰"
    # 单点 NaN 也是断点（分段后两侧独立检测；430 峰宽跨断点则可能只剩一侧——
    # 这里断点放在峰间谷底，断言两侧峰都保留且不产生新峰）
    y2 = 0.05 + _gauss(wl, 430.0) + _gauss(wl, 530.0)
    y2[wl == 480.0] = np.nan
    peaks2 = find_peaks_spectrum(wl, y2)
    assert len(peaks2) == 2, [p.wl_nm for p in peaks2]


# ═══════════════════════ A43.3 降级 ═══════════════════════
def test_a43_3_degraded_inputs_return_empty():
    wl = np.arange(400.0, 500.0, 1.0)
    flat = np.full_like(wl, 0.3)
    assert find_peaks_spectrum(wl, flat) == []          # 动态范围 0
    assert find_peaks_spectrum(wl, np.full_like(wl, np.nan)) == []  # 全 NaN
    assert find_peaks_spectrum([], []) == []            # 空谱
    monotonic = 0.3 + 0.001 * (wl - 400.0)              # 单调谱 = 无局部峰
    assert find_peaks_spectrum(wl, monotonic) == []
    assert write_peaks_csv([],  Path("x.csv")) is None  # 空表不产文件


# ═══════════════════════ A43.4 参数边界 ═══════════════════════
def test_a43_4_param_bounds_and_truncation():
    wl, y = _three_peak_spectrum()
    ks = find_peaks_spectrum(wl, y, max_peaks=2)
    assert len(ks) == 2
    full = find_peaks_spectrum(wl, y)
    top2 = sorted(full, key=lambda p: p.prominence_rel, reverse=True)[:2]
    assert [p.wl_nm for p in ks] == sorted(p.wl_nm for p in top2), "max_peaks 应按突出度降序截断"
    for kwargs in ({"prominence_rel": 0.0}, {"prominence_rel": -0.1},
                   {"height_rel": 0.0}, {"height_rel": -0.01},
                   {"min_distance_pts": 0}, {"min_distance_pts": 1.5},
                   {"max_peaks": 0}, {"max_peaks": 2.7}):
        with pytest.raises(ValueError):
            find_peaks_spectrum(wl, y, **kwargs)
    with pytest.raises(ValueError):
        find_peaks_spectrum(wl, y[:-1])                # 长度不一致
    # V27-O2：整数值 float 接受（5.0 视为 5），但 5.5 拒绝
    ok = find_peaks_spectrum(wl, y, min_distance_pts=5.0, max_peaks=20.0)
    assert len(ok) == len(full)
    with pytest.raises(ValueError):
        find_peaks_spectrum(wl, y, min_distance_pts=5.5)


# ═══════════════════════ A43.5 导出 ═══════════════════════
def test_a43_5_csv_write_and_no_overwrite(tmp_path):
    wl, y = _three_peak_spectrum()
    rows = peak_rows("s1.txt", find_peaks_spectrum(wl, y))
    assert len(rows) == 3 and len(rows[0]) == len(CSV_HEADERS)
    p1 = write_peaks_csv(rows, tmp_path / "peaks_120000.csv")
    assert p1 is not None and p1.is_file() and p1.name == "peaks_120000.csv"
    raw = p1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "CSV 必须 UTF-8-SIG（Excel 打开不乱码）"
    with p1.open(encoding="utf-8-sig", newline="") as f:
        got = list(csv.reader(f))
    assert tuple(got[0]) == CSV_HEADERS
    assert len(got) == 4 and got[1][0] == "s1.txt" and got[1][1] == "1"
    # 同名不覆盖 → -2
    p2 = write_peaks_csv(rows, tmp_path / "peaks_120000.csv")
    assert p2 is not None and p2.name == "peaks_120000-2.csv"
    assert p1.read_bytes() == raw
    # 默认名形态：peaks_<HHMMSS>.csv（由 GUI/验收负责生成；此处校验命名正则）
    import re
    assert re.fullmatch(r"peaks_\d{6}(?:-\d+)?\.csv", p2.name)


def test_a43_6_structural_isolation():
    """A43.6 测试侧：峰分析模块不 import 流水线/渲染，不改结果与配置。"""
    src = (PROJECT_ROOT / "core" / "analysis" / "peaks.py").read_text(encoding="utf-8")
    for banned in ("core.pipeline", "core.render", "plot_", "run_phi_pipeline"):
        assert banned not in src, f"peaks.py 不得耦合 {banned}"
    import yaml
    cfg = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert "peaks" not in cfg, "不得新增配置键"

# ═══════════════════════ A43 GUI：后台 Job / 忙禁用 / 取消（V27-O1） ═══════════════════════
def _pump_until(app, cond, seconds: float = 60.0) -> bool:
    import time
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.slow
def test_a43_panel_job_busy_result(qt_app):
    """V27-O1：②峰分析真实后台 Job——忙禁用、完成后峰表/导出可用。"""
    from core.config import default_config
    from ui.shell.analysis_page import PeaksPanel

    cfg = default_config()
    panel = PeaksPanel(cfg)
    try:
        panel.set_data_dir(PROJECT_ROOT / "samples" / "uvvis")
        panel._run()
        assert panel._busy is True, "点击「找峰」后必须进入忙态"
        assert not panel.btn_run.isEnabled(), "忙态下「找峰」应禁用"
        assert not panel.btn_dir.isEnabled(), "忙态下「选择目录」应禁用"
        assert not panel.btn_export.isEnabled(), "忙态下「导出」应禁用"
        assert panel.btn_cancel.isEnabled(), "忙态下「取消」应可用"
        assert _pump_until(qt_app, lambda: not panel._busy, 120.0), "Job 超时未结束"
        assert panel.tbl.rowCount() == len(panel._csv_rows) > 0, "峰表为空"
        assert panel.btn_export.isEnabled(), "完成后「导出」应可用"
        assert panel.btn_run.isEnabled(), "完成后「找峰」应恢复"
        assert not panel.btn_cancel.isEnabled(), "完成后「取消」应禁用"
    finally:
        panel.deleteLater()
        for _ in range(4):
            qt_app.processEvents()


@pytest.mark.slow
def test_a43_panel_job_cancel_restores_ui(qt_app, tmp_path, monkeypatch):
    """V27-O1：②峰分析取消后界面恢复、无结果落表（取消=丢弃结果）。"""
    import time
    from types import SimpleNamespace

    import numpy as np
    from core.config import default_config
    from ui.shell.analysis_page import PeaksPanel

    def slow_collect(_d, _cfg):
        time.sleep(1.0)
        wl = np.arange(400.0, 520.0, 1.0)
        y = 0.05 + np.exp(-0.5 * ((wl - 450.0) / 6.0) ** 2)
        return [SimpleNamespace(ok=True, wl=wl, a=y, path=Path("slow.txt"))]

    monkeypatch.setattr("core.inputs.io.collect_scans", slow_collect)
    d = tmp_path / "spectra"
    d.mkdir()
    panel = PeaksPanel(default_config())
    try:
        panel.set_data_dir(d)
        panel._run()
        assert panel._busy is True
        panel.cancel_job()
        assert _pump_until(qt_app, lambda: not panel._busy, 30.0), "取消后忙态未恢复"
        assert panel.btn_run.isEnabled(), "取消后「找峰」应恢复"
        assert not panel.btn_cancel.isEnabled(), "取消后「取消」应禁用"
        assert panel._csv_rows == [], "取消的任务不得落峰表"
    finally:
        panel.deleteLater()
        for _ in range(4):
            qt_app.processEvents()

