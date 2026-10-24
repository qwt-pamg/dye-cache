# -*- coding: utf-8 -*-
"""主页（最近批次行）与数据页（流程条 / 检查器 / 结论条 / 批次树）的混搭版面。

全部离屏 Qt、合成 Scan / pytest tmp_path，不依赖本地数据集。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from core.config import default_config
from core.inputs.batch_file import empty_batch
from conftest import make_scan

pytestmark = pytest.mark.fast


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _scans(tmp_path: Path) -> list:
    wl, a = np.arange(400.0, 420.0), np.ones(20)
    out = [make_scan(tmp_path / f"A-{t}.txt", wl, a, group="A", t=t) for t in (0, 30, 60)]
    out += [make_scan(tmp_path / f"ref-B-{t}.txt", wl, a, group="B", role="reference", t=t)
            for t in (0, 30)]
    return out


@pytest.fixture
def data_page(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "cfg.yaml"))
    from ui.pages.data import DataPage
    cfg = default_config()
    cfg.setdefault("gui", {})["recent_dirs"] = []
    page = DataPage(cfg)
    page.resize(1300, 800)
    yield page
    page.deleteLater()


def _load(page, tmp_path) -> list:
    scans = _scans(tmp_path)
    page.data_dir = tmp_path
    page._on_loaded((scans, empty_batch(tmp_path)))
    return scans


# ═══════════════ 数据页 ═══════════════
def test_data_page_empty_state(data_page):
    from ui.pages.data import STEPS
    p = data_page
    assert p.flow_bar.count() == len(STEPS) == p.inspector.count() == 3
    assert p.flow_bar.current() == 0 and p.inspector.current() == 0
    assert not p.flow_bar.step_enabled(1) and not p.flow_bar.step_enabled(2)
    assert not p.btn_analyze.isEnabled() and not p.btn_qc.isEnabled()
    assert p.verdict.mode() == "empty"
    # 主按钮挂在检查器上；顶部细条里是流程条
    assert p.btn_analyze.parent() is p.inspector
    assert p.flow_bar.parent() is p.top_bar
    # 不可用的步点了不切
    p.flow_bar.step_clicked.emit(2)
    assert p.flow_bar.current() == 0 and p.center_stack.currentIndex() == 0


def test_data_page_loaded_flow_inspector_and_verdict(data_page, tmp_path):
    from ui.widgets.flow_bar import STEP_DONE
    p = data_page
    got = []
    p.batch_changed.connect(got.append)
    scans = _load(p, tmp_path)
    assert got and got[-1] is p.scans and len(p.scans) == len(scans)
    assert p.flow_bar.state(0) == STEP_DONE and p.flow_bar.state(1) == STEP_DONE
    assert p.flow_bar.state(2) == "todo"
    assert p.flow_bar.step_enabled(2) and p.btn_analyze.isEnabled()
    # 结论条：一个数 + "几个谱 · 几组 · 剔除几个"
    assert p.verdict.mode() == "verdict" and p.verdict.value_text() == "5"
    assert p.verdict.headline_text() == "5 个谱 · 2 组 · 剔除 0"
    # 流程条 → 检查器 / 中栏一起切
    p.flow_bar.step_clicked.emit(1)
    assert p.inspector.current() == 1 and p.inspector.page(1).isAncestorOf(p.panel)
    assert p.center_stack.currentIndex() == 0
    p.flow_bar.step_clicked.emit(2)
    assert p.inspector.current() == 2 and p.center_stack.currentIndex() == 1
    assert p.inspector.page(2).isAncestorOf(p.btn_qc)
    # 检查器的摘要行 / 上一步按钮也能切步（经页面统一处理）
    p.inspector.summaries[0].click()
    assert p.flow_bar.current() == 0 and p.center_stack.currentIndex() == 0
    assert p.inspector.summaries[1].text() == "整定　2 组"
    assert p.inspector.summaries[2].text().endswith("还没体检")
    p.inspector.btn_next.click()
    assert p.flow_bar.current() == 1


def test_data_page_tree_filters_file_table(data_page, tmp_path):
    p = data_page
    _load(p, tmp_path)
    assert p.tree.groups() == ["A", "B"]
    p.tree.select_group("B")
    assert p.files.group_filter() == {"B"}
    assert p.files.visible_count() == 2
    p.tree.select_group(None)
    assert p.files.group_filter() is None


def test_data_page_qc_done_then_stale(data_page, tmp_path):
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE
    p = data_page
    _load(p, tmp_path)
    rep = {"summary": {"n_files": 5, "n_usable": 5, "n_flagged": 1},
           "reason": "发现 1 个文件有质量问题——详见 issues 与 files"}
    p._on_qc_done((rep, None))
    assert p.flow_bar.state(2) == STEP_DONE and p.flow_bar.current() == 2
    assert "issues" not in p.lbl_qc.text() and p.lbl_qc.text().startswith("5/5 可用")
    assert any("QC：5/5 可用" in lbl.full_text() for lbl in p.verdict.lbl_lines)
    # 整定改了 → 体检过期（不是清掉）
    p._refresh_after_panel()
    assert p.flow_bar.state(2) == STEP_STALE
    assert "需要重新体检" in p._step_summary(2)
    # 警戒线换成检查器里的值（百分比 → 比例）
    p.qc_spins["sat_frac_warn"].setValue(10.0)
    assert p.qc_options()["sat_frac_warn"] == pytest.approx(0.10)


def test_data_page_go_analysis_and_overrides(data_page, tmp_path):
    p = data_page
    _load(p, tmp_path)
    asked = []
    p.analysis_requested.connect(lambda: asked.append(True))
    p.btn_analyze.click()                     # 没有主窗口：只发信号 + 状态栏提示
    assert asked == [True]
    p.scans[1].t_src, p.scans[1].t = 30.0, 45.0
    assert p.scans[1].t_edited
    ov = p.session_overrides()
    assert ov["times"]["A-30.txt"] == 45.0 and "A" in ov["dye_of"]


def test_data_page_recent_dirs_in_import_step(qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QPushButton
    from ui.pages.data import DataPage
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "cfg.yaml"))
    d = tmp_path / "batch1"
    d.mkdir()
    cfg = default_config()
    cfg.setdefault("gui", {})["recent_dirs"] = [str(d), str(tmp_path / "gone")]
    page = DataPage(cfg)
    try:
        btns = [b for b in page.inspector.page(0).findChildren(QPushButton)
                if b.toolTip() == str(d)]
        assert len(btns) == 1 and btns[0].text() == "batch1"
        loaded = []
        monkeypatch.setattr(page, "load_dir", loaded.append)
        btns[0].click()
        assert loaded == [d]
    finally:
        page.deleteLater()


# ═══════════════ 主页 ═══════════════
def _spectra_dir(root: Path, name: str, n: int) -> Path:
    d = root / name
    d.mkdir()
    for i in range(n):
        (d / f"S-{i * 10}.txt").write_text("400,1\n401,1\n", encoding="utf-8")
    (d / "_skip-1.txt").write_text("x", encoding="utf-8")     # 排除前缀：不算
    (d / "notes.md").write_text("x", encoding="utf-8")        # 非光谱扩展名：不算
    return d


def _fake_run(runs: Path, data_dir: Path) -> Path:
    r = runs / "Dye" / "20261023-140500"
    r.mkdir(parents=True)
    (r / "run.log").write_text("ok", encoding="utf-8")
    (r / "run_params.yaml").write_text(
        f"_run:\n  data_dir: '{data_dir}'\n  export_mode: preview\n", encoding="utf-8")
    return r


def test_home_recent_batch_rows(qt_app, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from ui.pages.home import HomePage
    a = _spectra_dir(tmp_path, "batch_a", 3)
    b = _spectra_dir(tmp_path, "batch_b", 2)
    runs = tmp_path / "runs"
    _fake_run(runs, a)
    cfg = {"gui": {"out_root": str(runs),
                   "recent_dirs": [str(a), str(tmp_path / "missing"), str(b)]}}
    page = HomePage(cfg)
    try:
        rows = page.recent_rows
        assert [r.path for r in rows] == [a, b]               # 不存在的目录不列
        assert rows[0].lbl_name.text() == "batch_a"
        assert rows[0].lbl_count.text() == "3 个谱" and rows[1].lbl_count.text() == "2 个谱"
        assert rows[0].lbl_run.text().startswith("上次分析 ")
        assert rows[1].lbl_run.text() == "还没分析过"
        assert rows[0].lbl_date.text() not in ("", "—")
        got = []
        page.recent_dir_requested.connect(got.append)
        page.resize(1200, 800)
        page.show()
        QTest.mouseClick(rows[1], Qt.LeftButton)
        rows[0].setFocus()
        QTest.keyClick(rows[0], Qt.Key_Return)
        assert got == [str(b), str(a)]
    finally:
        page.deleteLater()


def test_home_empty_state_and_entry_signals(qt_app, tmp_path):
    from PySide6.QtWidgets import QPushButton
    from ui.pages.home import HomePage, greeting
    page = HomePage({"gui": {"out_root": str(tmp_path / "runs"), "recent_dirs": []}})
    try:
        assert page.recent_rows == []
        go, demo, opened = [], [], []
        page.go.connect(go.append)
        page.demo_requested.connect(lambda: demo.append(1))
        page.open_dir_requested.connect(lambda: opened.append(1))
        empty = [b for b in page.findChildren(QPushButton) if b.text() == "去数据页载入数据…"]
        assert len(empty) == 1
        empty[0].click()
        page.btn_demo.click()
        page.btn_open.click()
        assert go == ["data"] and demo == [1] and opened == [1]
        assert page.btn_open.property("role") == "primary"
        assert page.lbl_greeting.text() == greeting()
        assert [greeting(h) for h in (8, 12, 15, 22)] == ["早上好", "中午好", "下午好", "晚上好"]
    finally:
        page.deleteLater()


def test_home_run_data_dir_parses_quoted_paths(tmp_path):
    from ui.pages.home import run_data_dir
    r = tmp_path / "run"
    r.mkdir()
    (r / "run_params.yaml").write_text("_run:\n  data_dir: 'C:\\it''s\\data'\n", encoding="utf-8")
    assert run_data_dir(r) == "C:\\it's\\data"
    assert run_data_dir(tmp_path / "nope") == ""
