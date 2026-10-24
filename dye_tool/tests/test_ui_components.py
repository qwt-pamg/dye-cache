# -*- coding: utf-8 -*-
"""前端重构后独立出来的组件：过期横幅、组页签、结果动作条、页面基类公共能力、主题。

这些逻辑原来埋在 PhiPage / 各任务卡里，只能整页间接覆盖；拆出来后在这里直接测。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


# ═══════════════════════ StaleBanner ═══════════════════════
def _cfg(**over):
    cfg = {"plot": {"jacs": {"dpi": 600, "width_in": 3.25}},
           "fit": {"window": 2.0}, "excel": {"on": True}, "gui": {"theme": "light"}}
    for k, v in over.items():
        cfg[k] = v
    return cfg


def test_stale_banner_no_snapshot_or_result_never_shows(qt_app):
    from ui.pages.phi.stale import StaleBanner
    b = StaleBanner()
    assert b.sections(_cfg(fit={"window": 9.0})) == []
    b.set_snapshot(_cfg())
    assert b.sections(_cfg(fit={"window": 9.0}), has_result=False) == []


def test_stale_banner_plot_only_change_offers_rerender(qt_app):
    from ui.pages.phi.stale import StaleBanner
    b = StaleBanner()
    b.set_snapshot(_cfg())
    now = _cfg(plot={"jacs": {"dpi": 300, "width_in": 3.25}})
    b.refresh(now)
    assert b.is_shown() and b.kind() == "plot"
    assert b.pending_keys == ["dpi"]
    seen = []
    b.action_requested.connect(seen.append)
    b.btn_action.click()
    assert seen == ["plot"]


def test_stale_banner_numeric_change_requires_rerun_and_ignores_export_and_gui(qt_app):
    from ui.pages.phi.stale import StaleBanner
    b = StaleBanner()
    b.set_snapshot(_cfg())
    b.refresh(_cfg(excel={"on": False}, gui={"theme": "dark"}))
    assert not b.is_shown()                       # 导出分区 / 界面会话态不算过期
    b.refresh(_cfg(fit={"window": 5.0}))
    assert b.is_shown() and b.kind() == "numeric" and "fit" in b.lbl.text()
    b.dismiss()
    assert not b.is_shown() and b.kind() == "numeric"   # 忽略只隐藏，不忘记原因
    b.set_snapshot(_cfg(fit={"window": 5.0}))
    assert not b.is_shown() and b.kind() == ""


# ═══════════════════════ GroupTabs ═══════════════════════
def test_group_tabs_hidden_until_enabled_with_two_groups(qt_app):
    from ui.pages.phi.group_tabs import GroupTabs
    t = GroupTabs()
    got = []
    t.groups_selected.connect(got.append)
    t.rebuild(["a", "b"], str)
    assert t.isHidden() and got == [None]         # 能力未开：隐藏并清过滤
    t.set_feature_enabled(True)
    t.rebuild(["a"], str)
    assert t.isHidden()                           # 单组不显示
    t.rebuild(["a", "b"], lambda g: f"Dye · {g}")
    assert t.labels() == ["全部组（对比）", "Dye · a", "Dye · b"]
    assert got[-1] is None                        # 默认落在「全部组」


def test_group_tabs_keep_selection_by_group_not_index(qt_app):
    from ui.pages.phi.group_tabs import GroupTabs
    t = GroupTabs()
    t.set_feature_enabled(True)
    t.rebuild(["b", "c"], str)
    assert t.select_group("c")
    got = []
    t.groups_selected.connect(got.append)
    t.rebuild(["a", "b", "c"], str)               # 组集合变了，下标会漂
    assert t.tabText(t.currentIndex()) == "c" and got[-1] == {"c"}
    t.set_feature_enabled(False)
    assert t.isHidden() and got[-1] is None and not t.select_group("c")


def test_figure_names_single_vs_multi_draw_groups():
    from ui.pages.phi.group_tabs import figure_names
    s = lambda g, role="sample": SimpleNamespace(group=g, role=role, excluded="")  # noqa: E731
    multi = figure_names({"g1"}, [s("g1"), s("g2")])
    assert any(n.startswith("fig_a_spectra_") for n in multi)
    assert any(n.startswith("fig_b_kinetics_") for n in multi)
    # 1 样品组 + 1 背景组 = 单组命名（A03）
    single = figure_names({"g1"}, [s("g1"), s("bg", "background")])
    assert single != multi and any(n.startswith("jacs_stack_") for n in single)


# ═══════════════════════ ResultActions ═══════════════════════
def test_result_actions_show_result(qt_app, tmp_path):
    from ui.widgets.result_actions import ResultActions
    a = ResultActions()
    a.show_result(True, output=tmp_path, text="k = 1")
    assert a.btn_open_output.isEnabled() and a.btn_copy_result.isEnabled()
    assert a.btn_next_step.isEnabled() and not a.btn_retry.isEnabled()
    a.show_result(False, output=tmp_path, text="", failure_hint="如何修复：x")
    assert not a.btn_open_output.isEnabled()      # 失败不开放输出入口
    assert a.btn_retry.isEnabled() and a.lbl_fix.text() == "如何修复：x"


# ═══════════════════════ PageBase 公共能力 ═══════════════════════
class _Mime:
    def __init__(self, paths):
        from PySide6.QtCore import QUrl
        self._urls = [QUrl.fromLocalFile(str(p)) for p in paths]

    def urls(self):
        return self._urls


class _Drop:
    def __init__(self, paths):
        self._mime = _Mime(paths)
        self.accepted = False

    def mimeData(self):                                  # noqa: N802
        return self._mime

    def acceptProposedAction(self):                      # noqa: N802
        self.accepted = True


def test_pagebase_drop_dir_or_file_parent(qt_app, tmp_path):
    from ui.pages.base import PageBase

    class P(PageBase):
        def __init__(self):
            super().__init__({"gui": {}})
            self.got = []

        def on_dir_dropped(self, d):
            self.got.append(d)

    f = tmp_path / "s-1.txt"
    f.write_text("1,2\n", encoding="utf-8")
    page = P()
    for paths in ([tmp_path], [f]):
        ev = _Drop(paths)
        page.dropEvent(ev)
        assert ev.accepted
    assert page.got == [tmp_path, tmp_path]


def test_pagebase_pick_dir_starts_from_page_dir_and_remembers(qt_app, tmp_path, monkeypatch):
    from ui.pages import base
    seen = {}

    def fake(parent, title, start):
        seen["start"] = start
        return str(tmp_path / "picked")

    monkeypatch.setattr(base.QFileDialog, "getExistingDirectory", staticmethod(fake))
    page = base.PageBase({"gui": {"last_dir": "/elsewhere"}})
    page.data_dir = tmp_path
    got = page.pick_dir()
    assert seen["start"] == str(tmp_path)
    assert got == tmp_path / "picked"
    assert page.cfg["gui"]["recent_dirs"][0] == str(tmp_path / "picked")


# ═══════════════════════ 主题 ═══════════════════════
def test_theme_apply_and_roles(qt_app):
    from PySide6.QtWidgets import QLabel
    from ui import theme
    assert theme.apply("dark") == "dark" and theme.current() == "dark"
    assert theme.hexc("bg") == theme.PALETTES["dark"]["bg"]
    lbl = theme.mark(QLabel("x"), "hint")
    assert lbl.property("role") == "hint"
    theme.set_state(lbl, "invalid", True)
    assert lbl.property("invalid") is True
    assert theme.apply("nonsense") == "light"      # 未知值回落浅色
    assert theme.resolve("system") in ("light", "dark")
    # 两套色板令牌一致，否则切主题会 KeyError
    assert set(theme.PALETTES["light"]) == set(theme.PALETTES["dark"])
    assert "[role=\"hint\"]" in theme.stylesheet("light")
