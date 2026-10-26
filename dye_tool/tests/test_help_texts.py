# -*- coding: utf-8 -*-
"""V2.9.3 FR-68：帮助注册表与灰因 helper。"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from core.config import default_config
from ui.help_texts import (DISABLED_REASONS, PARAM_HELP, TERMS,
                           bind_disabled_reason, help_for_param, help_for_term)


def test_help_registry_covers_core_terms_and_params():
    for key in ("phi_ref", "phi_ref_err", "a_ex", "probe", "time_unit", "style", "formats"):
        assert PARAM_HELP[key].strip()
    for term in ("Φ_Δ", "k_obs", "A_ex", "参比样", "不确定度"):
        assert TERMS[term].strip()
    assert {"no_dir", "no_groups", "no_result"} <= set(DISABLED_REASONS)
    assert help_for_param("phi_ref") == PARAM_HELP["phi_ref"]
    assert help_for_term("nope") == ""


@pytest.fixture
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app





@pytest.mark.parametrize('state,want', [('no_dir', '先选择数据目录'),
                                         ('no_groups', '至少需要 1 个组'),
                                         ('no_result', '结果目录尚未生成')])
def test_disabled_reason_three_states(state, want):
    assert DISABLED_REASONS[state] == want



def test_help_install_does_not_overwrite_specific_tooltip(qt_app):
    from PySide6.QtWidgets import QPushButton, QWidget
    from ui.help_texts import install_app_help
    root = QWidget()
    b = QPushButton('运行', root)
    b.setToolTip('这是专用说明')
    install_app_help(root)
    assert b.toolTip() == '这是专用说明'
    root.deleteLater()



def test_param_help_and_page_reason_registry_coverage():
    from ui.help_texts import PAGE_REASONS, PARAM_HELP
    assert len(PARAM_HELP) >= 30
    assert {'PhiPage', 'ComparePage', 'DataPage', 'StackPage', 'SpectraPage'} <= set(PAGE_REASONS)
    assert PAGE_REASONS['PhiPage']['定稿出图']
    assert PAGE_REASONS['DataPage']['保存批次文件']


def test_disabled_control_gets_page_reason(qt_app):
    from PySide6.QtWidgets import QPushButton, QWidget
    from ui.help_texts import bind_disabled_reason, _page_reason
    root = QWidget()
    b = QPushButton('开始分析', root)
    b.setEnabled(False)
    bind_disabled_reason(b, lambda: _page_reason(b), root, overwrite_tooltip=False)
    assert _page_reason(b)
    root.deleteLater()


def test_form_layout_labels_get_field_tooltip(qt_app):
    from PySide6.QtWidgets import QFormLayout, QLabel, QLineEdit, QWidget
    from ui.help_texts import install_app_help
    root = QWidget()
    form = QFormLayout(root)
    lab = QLabel('参比 φ_ref')
    fld = QLineEdit()
    fld.setToolTip('参比物 Φ_F 文献值')
    form.addRow(lab, fld)
    install_app_help(root)
    assert lab.toolTip() == '参比物 Φ_F 文献值'
    root.deleteLater()



def test_install_app_help_handles_dynamic_controls(qt_app):
    from PySide6.QtWidgets import QPushButton, QWidget
    from ui.help_texts import install_app_help
    root = QWidget()
    install_app_help(root)
    late = QPushButton('后创建', root)
    late.setEnabled(False)
    install_app_help(root)
    assert late.property('dsh_help_reason')
    root.deleteLater()



def test_photophysics_fields_and_labels_have_specific_help(qt_app):
    from ui.help_texts import install_app_help
    from ui.pages.tasks.photophysics import PhotophysicsPanel
    p = PhotophysicsPanel(default_config())
    try:
        install_app_help(p)
        assert '参比' in p.tab_phi.sp_phi_ref.toolTip()
        assert '参比物' in p.tab_phi.sp_i_ref.toolTip() or 'Φ_F' in p.tab_phi.sp_i_ref.toolTip()
        assert '吸光度' in p.tab_phi.sp_a_ref.toolTip()
        assert '折射率' in p.tab_phi.sp_n_ref.toolTip()
        assert p.tab_phi.sp_eps.toolTip() and '消光' in p.tab_phi.sp_eps.toolTip()
        from PySide6.QtWidgets import QLabel
        labels = [w for w in p.findChildren(QLabel) if '参比 φ_ref' in w.text()]
        assert labels and labels[0].toolTip()
    finally:
        p.deleteLater()


def test_install_app_help_fills_generic_tooltips(qt_app):
    from PySide6.QtWidgets import QLineEdit, QPushButton, QTableWidget, QWidget
    from ui.help_texts import install_app_help
    root = QWidget()
    btn = QPushButton("运行", root)
    edit = QLineEdit(root)
    tbl = QTableWidget(0, 2, root)
    tbl.setHorizontalHeaderLabels(["组别", "Φ_Δ"])
    n = install_app_help(root)
    assert n >= 4
    assert btn.toolTip() and edit.toolTip()
    assert tbl.horizontalHeaderItem(0).toolTip()
    root.deleteLater()


def test_bind_disabled_reason_sets_tooltip_and_filter(qt_app):
    parent = QWidget()
    btn = QPushButton("run", parent)
    btn.setEnabled(False)
    flt = bind_disabled_reason(btn, lambda: "先选择数据目录", parent)
    assert btn.toolTip() == "先选择数据目录"
    assert flt is not None
    ev = QEvent(QEvent.HoverEnter)
    assert flt.eventFilter(parent, ev) is False
    btn.setEnabled(True)
    assert flt.eventFilter(parent, ev) is False
    parent.deleteLater()
