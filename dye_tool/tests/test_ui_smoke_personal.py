# -*- coding: utf-8 -*-
"""V2.8 FR-55.2：GUI 冒烟级 pytest（个人版；替代已退役的验收矩阵）。

覆盖四件事：
1. 双布局启动（flow / classic）；
2. 8 卡注册 + Ctrl+N 快捷；
3. 一次完整分析闭环（数据页载入 → ②Φ_Δ 分析 → 结果表/run 目录）；
4. 导出/预设往返（filter_formats + 用户预设 save/load/delete）。
"""
from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "uvvis"


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    app.setCursorFlashTime(0)
    yield app


def _events(app, n: int = 6) -> None:
    for _ in range(n):
        app.processEvents()


def _pump_until(app, cond, seconds: float = 120.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _destroy(app, widget) -> None:
    try:
        widget.close()
    except Exception:                           # noqa: BLE001
        pass
    widget.deleteLater()
    _events(app, 4)


@contextlib.contextmanager
def _window(qt_app, tmp_path, monkeypatch, *, nav: str = "flow"):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / f"cfg_{nav}.yaml"))
    from core.config import default_config
    from ui.window.main_window import MainWindow

    cfg = default_config()
    cfg.setdefault("gui", {})
    cfg["gui"]["nav"] = nav
    cfg["gui"]["show_welcome"] = False
    cfg["gui"]["out_root"] = str(tmp_path / "runs")
    cfg["gui"]["recent_dirs"] = []
    win = MainWindow(cfg)
    try:
        win.resize(1440, 900)
        win.show()
        _events(qt_app, 10)
        yield win
    finally:
        _destroy(qt_app, win)


@pytest.mark.parametrize("nav", ["flow", "classic"])
def test_dual_layout_startup(qt_app, tmp_path, monkeypatch, nav):
    """双布局都能启动到主窗口（个人版冒烟）。"""
    with _window(qt_app, tmp_path, monkeypatch, nav=nav) as win:
        assert win.layout_mode == nav
        assert win.windowTitle().startswith("dye_tool")
        assert len(win.pages) > 0
        if nav == "flow":
            assert win.plot_page is not None
            assert win.nav is not None
            assert win.nav.current_key in ("home", "data", "analysis", "plot",
                                           "archive", "settings")
        else:
            assert win.stack_page is not None and win.spectra_page is not None


def test_task_cards_and_shortcuts(qt_app, tmp_path, monkeypatch):
    """7 张任务卡注册 + Ctrl+1..7 绑定 + 每卡独立页面（V2.9.4 单入口）。"""
    from PySide6.QtGui import QShortcut

    from core.config import default_config
    from ui.pages.analysis import TASKS, AnalysisPage

    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    page = AnalysisPage(default_config())
    try:
        keys = [k for k, *_ in TASKS]
        assert len(keys) == 7 and keys[0] == "phi" and keys[-1] == "peaks"
        assert "compare" not in keys
        assert page.task_picker.keys() == keys
        assert page.current_key() == "phi" and "单线态氧" in page.btn_task.text()
        shortcuts = {sc.key().toString() for sc in page.findChildren(QShortcut)}
        for i in range(1, len(TASKS) + 1):
            assert f"Ctrl+{i}" in shortcuts, f"缺 Ctrl+{i}"
        seen = set()
        for k in keys:
            assert page.select_task(k) is True
            seen.add(id(page.stack.currentWidget()))
        assert len(seen) == len(keys), "任务卡页面重复"
    finally:
        _destroy(qt_app, page)


def test_one_analysis_closed_loop(qt_app, tmp_path, monkeypatch):
    """一次完整分析闭环：①载入 → ②Φ_Δ 分析 → 结果表 + run 目录。"""
    with _window(qt_app, tmp_path, monkeypatch) as win:
        win.goto("data")
        win.data_page.load_dir(SAMPLE)
        assert _pump_until(qt_app, lambda: not win.data_page._busy, 120.0), \
            "数据页解析超时"
        assert len(win.data_page.scans) == 35
        win.goto("analysis")
        _events(qt_app, 8)
        phi = win.analysis_page.phi_page
        assert _pump_until(qt_app, lambda: bool(phi.scans), 30.0), "分析页未收到扫描"
        phi._run()
        assert phi._busy is True
        assert _pump_until(qt_app, lambda: not phi._busy, 180.0), "分析 Job 超时"
        rr = getattr(phi, "_last_rr", None)
        assert rr is not None and bool(rr.ok), "分析结果无效"
        assert phi.table.rowCount() > 0, "结果表为空"
        assert rr.run_dir and Path(rr.run_dir).is_dir(), "run 目录未产出"
        # 结果动作条：成功后出现「打开输出 / 复制结果 / 下一步」，复制文本含各组
        assert phi.actions.btn_next_step.isEnabled() and not phi.actions.isHidden()
        assert phi.actions.btn_copy_result.isEnabled()
        assert all(g in phi._last_result_text for g in rr.results)
        # 流程条：分析后前三步完成、自动停在 Φ_Δ；结论卡有内容；「下一步」切到出图导出
        assert [phi.flow_bar.state(i) for i in range(3)] == ["done"] * 3
        assert phi.flow_bar.current() == 3 and phi.inspector_stack.currentIndex() == 3
        assert phi.verdict.mode() == "verdict" and phi.verdict.value_text()
        phi.actions.btn_next_step.click()
        assert phi.flow_bar.current() == 4 and phi.center_stack.currentIndex() == 1
        # 顶部流程条挂在分析页的细条里（不在 Φ_Δ 页自己的布局里）
        assert phi.top_bar.parent() is not phi


def test_export_and_preset_roundtrip(tmp_path):
    """导出格式过滤 + 用户出图预设 save→load→delete 往返。"""
    from core.config import default_config
    from core.render.plotstyle import (apply_preset, delete_user_preset,
                                      filter_formats, list_presets,
                                      load_preset, save_user_preset)

    assert "pdf" not in filter_formats(["png", "pdf", "svg"])
    presets = tmp_path / "presets.yaml"
    values = {"width_in": 7.0, "dpi": 300}
    made = save_user_preset("smoke-v28", values, presets)
    assert Path(made).is_file()
    got = load_preset("smoke-v28", presets)
    assert got and got["width_in"] == 7.0 and got["dpi"] == 300
    assert "smoke-v28" in {n for n, _l, _b in list_presets(presets)}
    cfg = apply_preset(default_config(), "jacs-double")
    assert cfg["plot"]["jacs"]["width_in"] == 7.0
    assert delete_user_preset("smoke-v28", presets) is True
    assert load_preset("smoke-v28", presets) is None
