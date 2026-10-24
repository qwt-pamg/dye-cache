# -*- coding: utf-8 -*-
"""分步检查器 StepInspector 与三栏骨架 three_columns。"""
from __future__ import annotations

import os

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QWidget


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def test_step_inspector_pages_summaries_and_navigation(qt_app):
    from ui.widgets.step_inspector import StepInspector
    ins = StepInspector(["导入", "计算", "导出"], ["选目录", "", "导出表格"])
    pages = [QLabel(f"p{i}") for i in range(3)]
    for i, w in enumerate(pages):
        ins.set_page(i, w)
    ins.set_summary_provider(lambda i: f"s{i}")
    got = []
    ins.step_requested.connect(got.append)

    ins.set_current(1, next_enabled=False)
    assert ins.stack.currentWidget() is pages[1]
    assert ins.lbl_step.text() == "第 2 步 · 计算" and ins.lbl_hint.isHidden()
    assert [b.isHidden() for b in ins.summaries] == [False, True, False]
    assert ins.summaries[0].text() == "导入　s0"
    assert not ins.btn_next.isEnabled() and ins.btn_prev.text() == "← 导入"

    ins.summaries[2].click()
    ins.btn_prev.click()
    assert got == [2, 0]                         # 只发请求，不自己切步
    assert ins.current() == 1

    ins.set_current(2)
    assert ins.btn_next.isHidden() and ins.lbl_hint.text() == "导出表格"


def test_step_actions_only_show_on_their_steps(qt_app):
    from ui.widgets.step_inspector import StepInspector
    ins = StepInspector(["a", "b", "c"])
    every, only_b, b_or_c = QPushButton("all"), QPushButton("b"), QPushButton("bc")
    ins.add_action(every)
    ins.add_action(only_b, steps=1)
    ins.add_action(b_or_c, steps=[1, 2])
    ins.set_current(0)
    assert [w.isHidden() for w in (every, only_b, b_or_c)] == [False, True, True]
    ins.set_current(1)
    assert [w.isHidden() for w in (every, only_b, b_or_c)] == [False, False, False]
    ins.set_current(2)
    assert [w.isHidden() for w in (every, only_b, b_or_c)] == [False, True, False]


def test_single_step_inspector_hides_navigation(qt_app):
    from ui.widgets.step_inspector import StepInspector
    ins = StepInspector(["设置"])
    ins.set_current(0)
    assert ins.lbl_step.text() == "设置"
    assert ins.btn_prev.isHidden() and ins.btn_next.isHidden()
    assert all(b.isHidden() for b in ins.summaries)


def test_three_columns_optional_left(qt_app):
    from ui.widgets.step_inspector import StepInspector, three_columns
    right = StepInspector(["a", "b"])
    center = QWidget()
    body = three_columns(None, center, right)
    assert body.count() == 2 and body.widget(0) is center
    body3 = three_columns(QPushButton("tree"), QWidget(), StepInspector(["a"]))
    assert body3.count() == 3
