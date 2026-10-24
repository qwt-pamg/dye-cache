# -*- coding: utf-8 -*-
"""Φ_Δ 新版页面的两个独立组件：顶部流程条 FlowBar、画布上方结论条 VerdictCard。"""
from __future__ import annotations

import os

import pytest

STEPS = ["导入", "分组与 QC", "拟合 k_obs", "Φ_Δ", "出图导出"]
GROUPS = [
    {"name": "2I-pbs-TEST", "sub": "5.88e-3 s⁻¹", "pill": "样品", "kind": "ok", "color": "accent"},
    {"name": "pbs-t", "sub": "4.90e-3 s⁻¹", "pill": "参比", "kind": "warn", "color": "series_ref"},
]


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _click(w):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    QTest.mouseClick(w, Qt.LeftButton, pos=w.rect().center())


def _verdict(card):
    card.set_verdict(label="Φ_Δ · Resorufin-2I-pbs-TEST", value="0.90", error="± 0.09",
                     headline="Resorufin-2I 比参比高 20%",
                     lines=["Φ_ref × k_样品 / k_参比 = 0.750 × 1.200",
                            "两组 R² ≥ 0.991 · 参比组 270 s 后 7 点已截断"],
                     groups=GROUPS)


# ═══════════════════════ FlowBar ═══════════════════════
def test_flow_bar_states_and_current(qt_app):
    from ui.widgets.flow_bar import STALE_TIP, STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
    fb = FlowBar(STEPS)
    assert fb.count() == 5 and fb.current() == -1
    assert [fb.state(i) for i in range(5)] == [STEP_TODO] * 5
    for i in range(3):
        fb.set_state(i, STEP_DONE)
    fb.set_current(3)
    assert fb.current() == 3 and fb.state(2) == STEP_DONE
    assert fb._links[0].done and fb._links[2].done and not fb._links[3].done   # 已完成步之后的连线高亮
    fb.set_state(3, STEP_STALE)
    assert fb.state(3) == STEP_STALE and fb._steps[3].toolTip() == STALE_TIP
    fb.set_state(3, STEP_TODO)
    assert fb._steps[3].toolTip() == ""
    fb.set_state(2, STEP_TODO)
    assert not fb._links[2].done
    with pytest.raises(ValueError):
        fb.set_state(0, "bogus")
    with pytest.raises(IndexError):
        fb.set_current(5)
    fb.set_current(-1)
    assert fb.current() == -1


def test_flow_bar_click_emits_but_set_current_and_disabled_do_not(qt_app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from ui.widgets.flow_bar import FlowBar
    fb = FlowBar(STEPS)
    got = []
    fb.step_clicked.connect(got.append)
    fb.set_current(1)
    assert got == []                              # 程序设当前步不算点击
    _click(fb._steps[1])
    _click(fb._steps[1])                          # 已是当前步也照发
    _click(fb._steps[4])
    assert got == [1, 1, 4] and fb.current() == 1  # 点击不自己改当前步

    fb.set_step_enabled(4, False, "先算出 Φ_Δ")
    assert not fb.step_enabled(4) and fb._steps[4].toolTip() == "先算出 Φ_Δ"
    _click(fb._steps[4])
    QTest.keyClick(fb._steps[4], Qt.Key_Return)
    assert got == [1, 1, 4]
    fb.set_step_enabled(4, True)
    assert fb.step_enabled(4) and fb._steps[4].toolTip() == ""


def test_flow_bar_keyboard_activation_and_arrows(qt_app):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from ui.widgets.flow_bar import FlowBar
    fb = FlowBar(STEPS)
    got = []
    fb.step_clicked.connect(got.append)
    assert all(st.focusPolicy() & Qt.TabFocus for st in fb._steps)
    QTest.keyClick(fb._steps[2], Qt.Key_Space)
    QTest.keyClick(fb._steps[0], Qt.Key_Enter)
    assert got == [2, 0]
    fb.set_step_enabled(1, False, "x")
    fb.show()
    fb._steps[0].setFocus(Qt.TabFocusReason)
    qt_app.processEvents()
    QTest.keyClick(fb._steps[0], Qt.Key_Right)    # 跳过不可用的第 2 步
    assert fb._steps[2].hasFocus() and fb._steps[2]._kbd_focus
    fb.close()


# ═══════════════════════ VerdictCard ═══════════════════════
def test_verdict_card_modes(qt_app):
    from ui.widgets.verdict import VerdictCard
    card = VerdictCard()
    assert card.mode() == "empty" and card.value_text() == ""

    _verdict(card)
    assert card.mode() == "verdict" and card.value_text() == "0.90"
    assert card.lbl_error.text() == "± 0.09"
    assert card.headline_text() == "Resorufin-2I 比参比高 20%"
    assert [l.isHidden() for l in card.lbl_lines] == [False, False, True]
    assert not card.box_value.isHidden() and not card.box_groups.isHidden()
    pills = [w for w in card.box_groups.findChildren(type(card.lbl_value)) if w.objectName() == "phiVerdictPill"]
    assert [(p.text(), bool(p.property("warn"))) for p in pills] == [("样品", False), ("参比", True)]

    card.set_failed("拟合失败", "参比组点数不足\n至少需要 4 个时间点")
    assert card.mode() == "failed" and card.value_text() == ""
    assert card.box_value.isHidden() and card.box_groups.isHidden()
    assert card.property("failed") is True and card.lbl_headline.property("failed") is True
    assert [l.isHidden() for l in card.lbl_lines] == [False, False, True]

    card.set_empty("还没有结果")
    assert card.mode() == "empty" and card.value_text() == ""
    assert not card.property("failed") and card.lbl_headline.property("empty") is True
    assert all(l.isHidden() for l in card.lbl_lines)


def test_verdict_card_fits_height_and_elides_at_700px(qt_app):
    from ui.widgets.verdict import MAX_GROUP_ROWS, VerdictCard
    card = VerdictCard()
    _verdict(card)                                # 先填正常数据，再塞超长文字与过多组
    card.set_verdict(label="Φ_Δ · " + "很长的样品名" * 10, value="0.90", error="± 0.09",
                     headline="结论" * 60, lines=["说明" * 80] * 5,
                     groups=GROUPS * 4)
    card.resize(700, 120)
    card.show()
    qt_app.processEvents()
    assert card.height() <= 120 and card.sizeHint().height() <= 120
    head = card.lbl_headline
    assert head.text() != head.full_text() and head.text().endswith("…")
    assert head.toolTip() == head.full_text()                       # 被省略时悬停看全文
    assert sum(not l.isHidden() for l in card.lbl_lines) == 3       # 最多 3 行说明
    names = [w for w in card.box_groups.findChildren(type(card.lbl_value))
             if w.objectName() == "phiVerdictGroup"]
    assert len(names) == MAX_GROUP_ROWS - 1                         # 其余收成「另有 N 组」
    card.close()


def test_verdict_dot_color_resolves_token_hex_and_fallback(qt_app):
    from ui import theme
    from ui.widgets.verdict import _Dot
    theme.apply("light")
    assert _Dot("series_ref")._qcolor() == theme.color("series_ref")
    assert _Dot("#123456")._qcolor().name() == "#123456"
    assert _Dot("no_such_token")._qcolor() == theme.color("text_muted")


# ═══════════════════════ 两套主题下都能建、能画 ═══════════════════════
@pytest.mark.parametrize("mode", ["light", "dark"])
def test_flow_and_verdict_render_in_both_themes(qt_app, mode):
    from ui import theme
    from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, FlowBar
    from ui.widgets.verdict import VerdictCard
    assert theme.apply(mode) == mode
    try:
        assert "QFrame#phiVerdict" in theme.stylesheet(mode)        # 组件 QSS 已登记
        fb = FlowBar(STEPS)
        for i in range(3):
            fb.set_state(i, STEP_DONE)
        fb.set_state(3, STEP_STALE)
        fb.set_current(3)
        fb.set_step_enabled(4, False, "先算出 Φ_Δ")
        card = VerdictCard()
        _verdict(card)
        for w in (fb, card):
            img = w.grab()
            assert not img.isNull() and img.width() > 0
    finally:
        theme.apply("light")
