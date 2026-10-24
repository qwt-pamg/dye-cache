# -*- coding: utf-8 -*-
"""混搭工作台：③ 图件（作图台）/ ④ 档案 / ⑤ 设置 的流程条、检查器与分区导航（离屏）。

数据用 ``tools/make_ui_samples.py`` 现写到 tmp_path，不依赖仓库外的实验数据。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from core.config import default_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


@pytest.fixture
def ui_scans(tmp_path):
    """合成 UV-vis 批次（两组 × 时间序列）→ Scan 列表。"""
    spec = importlib.util.spec_from_file_location("make_ui_samples",
                                                  ROOT / "tools" / "make_ui_samples.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path / "uvvis"
    mod.make_ui_samples(d)
    from core.io import collect_scans
    return d, collect_scans(d, default_config())


# ═══════════════ ③ 作图台：流程条 ⇄ 检查器 ═══════════════
def test_workbench_flow_bar_and_inspector_step_together(qt_app, ui_scans):
    from ui.pages.stack import StackPage
    from ui.pages.workbench import STEP_EXPORT, STEP_LOAD, STEP_STYLE, STEPS
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO
    page = StackPage(default_config())
    try:
        fb, ins = page.flow_bar, page.inspector
        assert fb.count() == ins.count() == len(STEPS) == 3
        assert fb.current() == ins.current() == STEP_LOAD
        assert [fb.state(i) for i in range(3)] == [STEP_TODO] * 3
        # 流程条点哪步，检查器就换到哪步（不是向导：没载入也能点）
        fb.step_clicked.emit(STEP_EXPORT)
        assert fb.current() == ins.current() == STEP_EXPORT
        assert ins.stack.currentIndex() == STEP_EXPORT
        # 检查器摘要行 / 上一步 也走同一个切步入口
        ins.summaries[STEP_STYLE].click()
        assert fb.current() == ins.current() == STEP_STYLE
        ins.btn_prev.click()
        assert fb.current() == STEP_LOAD
        # 主按钮常驻检查器底部；图件始终在中栏
        assert page.btn_run.parent() is not None and ins.isAncestorOf(page.btn_run)
        assert not ins.isAncestorOf(page.view_stack)
        assert page.body.count() == 3

        d, scans = ui_scans
        page.data_dir = d
        page._on_loaded(scans)
        _pump(qt_app)
        assert [fb.state(i) for i in range(3)] == [STEP_DONE, STEP_DONE, STEP_TODO]
        assert page.verdict.mode() == "verdict" and page.verdict.value_text().endswith("条谱")
        assert d.name in ins.summaries[STEP_LOAD].text()

        page._mark_final([d / "fake.png"])
        assert fb.state(STEP_EXPORT) == STEP_DONE
        # 定稿后再改选项 → 导出步标"需要重算"
        page.chk_overlay.setChecked(False)
        _pump(qt_app)
        assert fb.state(STEP_EXPORT) == STEP_STALE
        assert "需要重新生成" in "\n".join(lb.full_text() for lb in page.verdict.lbl_lines)
        # 换数据 → 旧定稿图作废
        page._on_loaded(scans)
        assert fb.state(STEP_EXPORT) == STEP_TODO
    finally:
        page.deleteLater()
        _pump(qt_app)


def test_workbench_style_actions_split_between_steps(qt_app):
    from ui.pages.spectra import SpectraPage
    from ui.pages.workbench import STEP_EXPORT, STEP_STYLE
    page = SpectraPage(default_config())
    try:
        sp, ins = page.style_panel, page.inspector
        style_page, export_page = ins.page(STEP_STYLE), ins.page(STEP_EXPORT)
        for b in (sp.btn_undo, sp.btn_reset_current, sp.btn_reset_all):
            assert style_page.isAncestorOf(b)
        for b in (sp.btn_write, sp.btn_preset, sp.btn_advanced, page.btn_export_view):
            assert export_page.isAncestorOf(b)
        assert style_page.isAncestorOf(page.cmb_display_preset)
        assert "dpi" in page.lbl_export_spec.text()
        # 动作按钮挪了位置，信号仍连在样式面板上
        got = []
        page.style_panel.write_requested.connect(got.append)
        sp.btn_write.click()
        assert got and isinstance(got[0], dict)
    finally:
        page.deleteLater()
        _pump(qt_app)


def test_plot_page_switcher_hosts_each_flow_bar(qt_app):
    from ui.pages.plot import VIEWS, PlotPage
    page = PlotPage(default_config())
    try:
        assert [k for k, _t, _d in VIEWS] == ["uv_overlay", "uv_stack", "fluor"]
        assert page.current_view() == "uv_overlay"
        for key, _t, _d in VIEWS:
            wb = getattr(page, key)
            # 工作台交出的流程条挂在壳页顶部细条里，而不是工作台自己的版面里
            assert page.top_bar.isAncestorOf(wb.top_bar_widget())
            assert not wb.isAncestorOf(wb.top_bar_widget())
            assert page.select_view(key) is True
            assert page.tabs.currentWidget() is wb          # actions._current_page 依赖它
            assert page.top_slot.currentWidget() is wb.top_bar_widget()
            assert page.view_buttons[key].isChecked()
        assert page.select_view("nope") is False
        page.view_buttons["uv_stack"].click()
        assert page.current_view() == "uv_stack"
    finally:
        page.deleteLater()
        _pump(qt_app)


def test_workbench_standalone_keeps_its_own_flow_bar(qt_app):
    """经典布局直接把作图页当页签：流程条留在页内顶部。"""
    from ui.pages.stack import StackPage
    page = StackPage(default_config())
    try:
        assert page.isAncestorOf(page.top_bar_widget())
        assert page.layout().itemAt(0).widget() is page.top_bar_widget()
    finally:
        page.deleteLater()
        _pump(qt_app)


# ═══════════════ ⑤ 设置：分区导航 + 主题 ═══════════════
def test_settings_section_navigation(qt_app):
    from ui.pages.settings import SECTIONS, SettingsPage
    page = SettingsPage(default_config())
    try:
        keys = page.section_keys()
        assert keys == [k for k, _n, _d in SECTIONS] == ["params", "config", "reorg"]
        assert page.current_section() == "params"
        assert page.section_stack.currentIndex() == 0
        assert page.section_stack.currentWidget().isAncestorOf(page.params)
        assert page.select_section("reorg") is True
        assert page.sections.currentRow() == 2
        assert page.section_stack.currentWidget().isAncestorOf(page.btn_plan)
        # 点左栏也会切中栏
        page.sections.setCurrentRow(1)
        assert page.current_section() == "config"
        assert page.section_stack.currentWidget().isAncestorOf(page.btn_check_cfg)
        assert page.select_section("nope") is False and page.current_section() == "config"
        # 界面主题切换在顶部细条里，任何分区都看得见
        assert page.top_bar.isAncestorOf(page.theme_buttons["dark"])
    finally:
        page.deleteLater()
        _pump(qt_app)


def test_settings_theme_switch_standalone(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from ui import theme
    from ui.pages.settings import SettingsPage
    cfg = default_config()
    cfg.setdefault("gui", {})["theme"] = "light"
    page = SettingsPage(cfg)
    try:
        assert page.theme_buttons["light"].isChecked()
        page.theme_buttons["dark"].click()
        assert cfg["gui"]["theme"] == "dark" and theme.current() == "dark"
        assert page.theme_buttons["dark"].isChecked()
    finally:
        theme.apply("light")
        page.deleteLater()
        _pump(qt_app)


def test_settings_theme_switch_goes_through_main_window(qt_app, tmp_path, monkeypatch):
    """在主窗口里切主题 = 走菜单同一路（写 gui.theme、整窗换色），菜单勾选跟着变。"""
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from ui import theme
    from ui.window.main_window import MainWindow
    cfg = default_config()
    cfg.setdefault("gui", {}).update({"show_welcome": False, "recent_dirs": [], "theme": "light"})
    win = MainWindow(cfg)
    try:
        win.show()
        win.goto("settings")
        _pump(qt_app)
        sp = win.settings_page
        got = []
        win.theme_changed.connect(got.append)
        sp.theme_buttons["dark"].click()
        assert win.cfg["gui"]["theme"] == "dark" and theme.current() == "dark"
        assert win.theme_actions["dark"].isChecked()
        win.theme_actions["light"].trigger()             # 菜单改回 → 设置页同步
        assert sp.theme_buttons["light"].isChecked()
        assert got == ["dark", "light"]                  # 两条路都经 MainWindow.set_theme
    finally:
        theme.apply("light")
        win.close()
        win.deleteLater()
        _pump(qt_app, 10)


# ═══════════════ ④ 档案：三栏 + 结论条 + 所选 run 动作 ═══════════════
def _make_dye_runs(root: Path) -> Path:
    from core.dyes import HISTORY_COLUMNS, HISTORY_NAME
    dye_dir = root / "Dye"
    rows = []
    for i, (k, phi) in enumerate(((0.0040, 0.41), (0.0042, 0.44))):
        run = dye_dir / f"2026101{i}-120000"
        (run / "figures").mkdir(parents=True)
        (run / "results.csv").write_text("group,k\n", encoding="utf-8")
        rows.append({"run_id": run.name, "dir": str(run), "dyes": "Dye", "k": k,
                     "k_se": 0.0001, "Phi": phi, "Phi_se": 0.02, "status": "OK"})
    lines = [",".join(HISTORY_COLUMNS)]
    for r in rows:
        lines.append(",".join(str(r.get(c, "")) for c in HISTORY_COLUMNS))
    (dye_dir / HISTORY_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dye_dir


def test_library_three_columns_verdict_and_run_actions(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from ui.pages.dye_library import DyeLibraryPage
    from ui.pages.dye_library.model import figures_of, latest_run_dir
    root = tmp_path / "runs"
    _make_dye_runs(root)
    page = DyeLibraryPage(out_root=root)
    try:
        assert page.body.count() == 3
        assert page.verdict.mode() == "empty"
        page.reload()
        _pump(qt_app)
        assert page.table_dyes.rowCount() == 1
        assert page.verdict.mode() == "verdict"
        assert page.verdict.value_text() == "0.44"
        assert "2 次 run" in page.verdict.headline_text()
        assert page.btn_params.isEnabled() and page.btn_export.isEnabled()
        # 历史表选中一行 → 「所选 run」按钮可用；载入参数走 params_loaded
        assert page.table_hist.currentRow() == 0
        assert page.btn_run_params.isEnabled() and page.btn_run_open.isEnabled()
        assert page.inspector.isAncestorOf(page.btn_compose)
        assert latest_run_dir(page._cur).name == "20261011-120000"
        assert figures_of(None) == ([], "")
        # 旧目录选中 → 结论条回到空态
        page._clear_detail("x")
        assert page.verdict.mode() == "empty"
        assert not page.btn_run_params.isEnabled()
    finally:
        page.close()
        page.deleteLater()
        _pump(qt_app)


def test_archive_page_hosts_library_with_slim_title(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from ui.pages.archive import ArchivePage
    cfg = default_config()
    cfg.setdefault("gui", {})["out_root"] = str(tmp_path / "runs")
    page = ArchivePage(cfg)
    try:
        assert page.dye_page is not None and page.dye_page.lbl_title.text() == "档案"
    finally:
        page.deleteLater()
        _pump(qt_app)
