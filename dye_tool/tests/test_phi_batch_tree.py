# -*- coding: utf-8 -*-
"""Φ_Δ 左栏 BatchTree：结构、选中信号、状态词、主题切换（纯合成数据）。"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SAMPLE, REF = "Dye-A", "Ref-B"


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


def _scan(group, t, *, excluded="", role="sample", rep=""):
    """与 core.io.Scan 同名的、BatchTree 会读到的属性。"""
    return SimpleNamespace(path=Path(f"/data/{group}-{t:g}.txt"), group=group,
                           rep=rep, role=role, t=float(t), excluded=excluded,
                           time_blank=False)


def _scans(n_sample=14, n_ref=5):
    return ([_scan(SAMPLE, 10 * i) for i in range(n_sample)]
            + [_scan(REF, 10 * i) for i in range(n_ref)])


def _title(g):
    return f"染料 · {g}"


def _tree(qt_app, scans=None, **kw):
    from ui.pages.phi.batch_tree import BatchTree
    tree = BatchTree()
    tree.set_scans(_scans() if scans is None else scans, _title, **kw)
    return tree


def _files(tree, group):
    gi = tree._group_items[group]
    return [gi.child(i) for i in range(gi.childCount())]


def _status(tree, group):
    return [it.text(1) for it in _files(tree, group)]


def _pooled(**kw):
    base = dict(status="OK", n=0, truncated=False, x=None, message="")
    base.update(kw)
    return SimpleNamespace(**base)


def test_group_and_file_counts(qt_app):
    from ui.pages.phi.batch_tree import ALL_GROUPS_TITLE
    tree = _tree(qt_app)
    assert tree.groups() == sorted([SAMPLE, REF])
    assert tree.topLevelItemCount() == 3
    assert tree.topLevelItem(0).text(0) == ALL_GROUPS_TITLE
    gi = tree._group_items[SAMPLE]
    assert gi.text(0) == _title(SAMPLE) and gi.text(1) == "14"
    assert tree._group_items[REF].text(1) == "5"
    assert [it.text(0) for it in _files(tree, REF)] == ["0 s", "10 s", "20 s", "30 s", "40 s"]
    assert set(_status(tree, SAMPLE)) == {"可用"}
    # 未选中且 >12 个文件的组默认折叠，其余展开
    assert not gi.isExpanded() and tree._group_items[REF].isExpanded()


def test_groups_selected_none_for_all_and_set_for_group(qt_app):
    tree = _tree(qt_app)
    got = []
    tree.groups_selected.connect(got.append)
    tree.setCurrentItem(tree._group_items[REF])
    assert got[-1] == {REF} and tree.selected_groups() == {REF}
    tree.setCurrentItem(_files(tree, REF)[2])          # 同组换文件不重复发
    assert len(got) == 1 and tree.selected_groups() == {REF}
    tree.setCurrentItem(tree.topLevelItem(0))
    assert got[-1] is None and tree.selected_groups() is None


def test_select_group_emits_and_expands(qt_app):
    tree = _tree(qt_app)
    got = []
    tree.groups_selected.connect(got.append)
    tree.select_group(SAMPLE)
    assert got == [{SAMPLE}] and tree.selected_groups() == {SAMPLE}
    assert tree._group_items[SAMPLE].isExpanded()
    tree.select_group(None)
    assert got[-1] is None


def test_set_scans_keeps_selection_with_same_groups(qt_app):
    tree = _tree(qt_app)
    tree.select_group(REF)
    got = []
    tree.groups_selected.connect(got.append)
    tree.set_scans(_scans(n_ref=6), _title)
    assert tree.selected_groups() == {REF} and got == [{REF}]
    tree.set_scans([_scan("Other", 0)], _title)       # 组集合变了 → 回到全部组
    assert tree.selected_groups() is None and got[-1] is None


def test_excluded_and_qc_status(qt_app):
    scans = _scans(n_sample=3)
    scans[1].excluded = "饱和（A>3）超出量程"
    tree = _tree(qt_app, scans, excluded=[str(scans[2].path)])
    st = _status(tree, SAMPLE)
    assert st[0] == "可用"
    assert st[1].startswith("饱和") and st[1] != "已剔除"
    assert "超出量程" in _files(tree, SAMPLE)[1].toolTip(1)
    assert st[2] == "已剔除"
    # 文件名也认
    tree.set_scans(scans, _title, excluded=[scans[0].path.name])
    assert _status(tree, SAMPLE)[0] == "已剔除"


def test_set_result_marks_truncation_and_reference(qt_app):
    scans = _scans(n_sample=8, n_ref=4)
    tree = _tree(qt_app, scans, reference=REF, excluded=[str(scans[7].path)])
    analyzed = [s for s in scans if s is not scans[7]]
    rr = SimpleNamespace(scans=analyzed, cfg={}, phi={}, results={
        # 截断保留最早 5 点（横轴 0..40）
        SAMPLE: {"role": "sample", "irradiance": 1.0,
                 "pooled": _pooled(n=5, truncated=True, x=np.arange(5) * 10.0)},
        REF: {"role": "reference", "irradiance": 1.0, "pooled": _pooled(n=4)},
    })
    tree.set_result(rr)
    assert _status(tree, SAMPLE) == ["参与拟合"] * 5 + ["截断", "截断", "已剔除"]
    assert _status(tree, REF) == ["参比"] * 4
    from ui import theme
    assert _files(tree, SAMPLE)[5].foreground(1).color() == theme.color("warn")
    # 没有 x 时按时间排名 ≥ n 推算
    rr.results[SAMPLE]["pooled"] = _pooled(n=6, truncated=True)
    tree.set_result(rr)
    assert _status(tree, SAMPLE) == ["参与拟合"] * 6 + ["截断", "已剔除"]
    tree.set_result(None)
    assert _status(tree, SAMPLE)[:2] == ["可用", "可用"]


def test_failed_group_and_group_colors(qt_app):
    from ui import theme
    from ui.pages.phi.batch_tree import GROUP_COLORS
    scans = _scans(n_sample=2, n_ref=2) + [_scan("Dye-C", 0)]
    tree = _tree(qt_app, scans, reference=REF)
    assert tree.group_color(SAMPLE) == theme.hexc("accent")
    assert tree.group_color(REF) == theme.hexc("series_ref")
    assert tree.group_color("Dye-C") == GROUP_COLORS[0]
    rr = SimpleNamespace(scans=scans, cfg={}, results={
        SAMPLE: {"role": "sample", "pooled": _pooled(status="FAILED", message="x")}})
    tree.set_result(rr)
    assert set(_status(tree, SAMPLE)) == {"拟合失败"}
    assert set(_status(tree, REF)) == {"未拟合"}


def test_file_activated_on_double_click(qt_app):
    tree = _tree(qt_app)
    got = []
    tree.file_activated.connect(got.append)
    it = _files(tree, REF)[1]
    tree.itemDoubleClicked.emit(it, 0)
    tree.itemDoubleClicked.emit(tree._group_items[REF], 0)
    assert got == [f"{REF}-10.txt"]


def test_theme_switch_recolors_without_crash(qt_app):
    from ui import theme
    tree = _tree(qt_app, reference=REF)
    tree.show()
    try:
        theme.apply("dark", qt_app)
        qt_app.processEvents()
        assert tree.group_color(REF) == theme.PALETTES["dark"]["series_ref"]
        it = _files(tree, REF)[0]
        assert it.foreground(1).color() == _palette_color("dark", "success")
        theme.apply("light", qt_app)
        qt_app.processEvents()
        assert it.foreground(1).color() == _palette_color("light", "success")
    finally:
        theme.apply("light", qt_app)
        tree.close()


def _palette_color(name, token):
    from PySide6.QtGui import QColor
    from ui import theme
    return QColor(theme.PALETTES[name][token])
