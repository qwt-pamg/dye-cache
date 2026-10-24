# -*- coding: utf-8 -*-
"""斯托克斯 / 光物理表征 / 批量队列三张任务卡的混搭工作台：流程条、检查器与结论条。"""
from __future__ import annotations

import math
import os

import pytest


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _check_steps(panel, steps) -> None:
    """顶栏交出的是流程条；检查器步数与流程条一致；_go_step 同时切流程条与检查器页。"""
    from ui.widgets.flow_bar import FlowBar
    bar = panel.top_bar_widget()
    assert isinstance(bar, FlowBar) and bar is panel.top_bar_widget()
    assert bar.count() == panel.inspector.count() == len(steps)
    for i in reversed(range(len(steps))):
        panel._go_step(i)
        assert bar.current() == i
        assert panel.inspector.current() == i
        assert panel.inspector.stack.currentIndex() == i
    bar.step_clicked.emit(len(steps) - 1)            # 点流程条也能切
    assert panel.inspector.stack.currentIndex() == len(steps) - 1


def test_stokes_workbench_steps_and_verdict(qt_app):
    from core.config import default_config
    from ui.pages.tasks.stokes import STEPS, StokesPanel
    p = StokesPanel(default_config())
    try:
        assert STEPS == ("选吸收谱", "选发射谱", "结果与对比")
        _check_steps(p, STEPS)
        assert p.inspector.page(0).isAncestorOf(p.lst_abs)
        assert p.inspector.page(1).isAncestorOf(p.lst_em)
        assert p.verdict.mode() == "empty"
    finally:
        p.deleteLater()


def test_photophysics_workbench_steps_center_and_actions(qt_app, tmp_path):
    from core.config import default_config
    from ui.pages.tasks.photophysics import PhotophysicsPanel
    from ui.pages.tasks.photophysics.panel import STEPS
    p = PhotophysicsPanel(default_config())
    try:
        _check_steps(p, STEPS)
        p._go_step(1)
        assert p.center_stack.currentWidget() is p.tab_bleach
        assert not p.tab_bleach.primary_actions.isHidden()
        assert p.tab_phi.primary_actions.isHidden() and p.tab_char.primary_actions.isHidden()

        csv = tmp_path / "roi.csv"
        csv.write_text("time,intensity\n" + "".join(
            f"{t},{0.2 + 0.8 * math.exp(-0.05 * t):.6f}\n" for t in range(0, 61, 3)),
            encoding="utf-8")
        assert p.tab_bleach.load_bleach_csv(csv) and p.tab_bleach.compute_bleach()["ok"]
        assert p.flow_bar.state(1) == "done" and p.verdict.mode() == "verdict"
        assert p.verdict.headline_text().startswith("半衰期 t½")
        p.tab_bleach.chk_plateau.setChecked(False)    # 拟合后改选项 → 需要重算
        assert p.flow_bar.state(1) == "stale"
        p.tab_bleach.compute_bleach()
        assert p.flow_bar.state(1) == "done"
    finally:
        p.deleteLater()


def test_queue_workbench_steps_and_verdict(qt_app, tmp_path):
    from ui.pages.tasks.queue import STEPS, BatchQueuePanel
    p = BatchQueuePanel({"gui": {"out_root": str(tmp_path / "runs")}})
    try:
        assert STEPS == ("添加目录", "运行", "汇总")
        _check_steps(p, STEPS)
        assert p.btn_start.parent() is not None and p.inspector.isAncestorOf(p.btn_start)
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        p.add_directories([d1, d2])
        assert p.flow_bar.state(0) == "done" and p.tbl_queue.rowCount() == 2
        assert "还没运行" in p.verdict.headline_text()

        p._fill_status([{"data_dir": str(d1), "status": "ok", "duration_s": 1.2},
                        {"data_dir": str(d2), "status": "failed", "error": "没有光谱"}])
        assert p.verdict.headline_text() == "2 个目录 · 1 成功 · 1 失败"
        assert p.flow_bar.state(1) == "done"
        p.add_directories([tmp_path])                 # 跑完又改了队列 → 需要重算
        assert p.flow_bar.state(1) == "stale"
    finally:
        p.deleteLater()
