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


def test_result_actions_hide_unusable_buttons(qt_app, tmp_path):
    from ui.widgets.result_actions import ResultActions
    a = ResultActions()
    assert a.isHidden()                           # 尚无结果：整条收起，不摆灰按钮
    a.show_result(True, output=tmp_path, text="k = 1")
    assert not a.isHidden()
    assert [b.isHidden() for b in (a.btn_open_output, a.btn_copy_result,
                                   a.btn_next_step, a.btn_retry)] == [False, False, False, True]
    a.show_result(False, failure_hint="如何修复：x")
    assert not a.isHidden() and not a.btn_retry.isHidden()
    assert a.btn_open_output.isHidden() and a.btn_next_step.isHidden()


def test_export_bar_shows_buttons_only_when_usable(qt_app, tmp_path):
    from ui.widgets.export_bar import ExportBar
    bar = ExportBar(on_workbook=lambda: None, on_dashboard=lambda: None)
    assert bar.isHidden()
    bar.set_data_ready(True)                      # 有数据：只出 QC 体检
    assert not bar.isHidden() and not bar.btn_dashboard.isHidden()
    assert bar.btn_open.isHidden() and bar.btn_workbook.isHidden()
    bar.set_run_dir(tmp_path)                     # 有结果目录：其余按钮出现
    assert not bar.btn_open.isHidden() and not bar.btn_workbook.isHidden()
    plain = ExportBar()
    plain.set_run_dir(tmp_path)
    assert plain.btn_workbook.isHidden() and plain.btn_dashboard.isHidden()


def test_progress_row_hidden_until_first_task(qt_app):
    import time
    from PySide6.QtWidgets import QApplication
    from ui.pages.base import PageBase
    page = PageBase({"gui": {}})
    row = page.progress_row()
    assert row.isHidden() and page.btn_cancel.isHidden()
    done = []
    page.run_task(lambda p, l: time.sleep(0.2) or 1, "测试任务", done.append)
    assert not row.isHidden() and not page.btn_cancel.isHidden()
    t0 = time.time()
    while not done and time.time() - t0 < 5:
        QApplication.processEvents()
        time.sleep(0.01)
    QApplication.processEvents()
    assert done == [1]
    assert row.isHidden() and page.btn_cancel.isHidden()   # 成功后整行收起
    page._on_failed("boom", "")
    assert not row.isHidden() and page.lbl_stage.text() == "失败"   # 失败留下来显示原因


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


# ═══════════════════════ 光物理表征卡（拆成三个子页后） ═══════════════════════
def test_photophysics_bleach_csv_flows_into_char_table(qt_app, tmp_path):
    import math
    from core.config import default_config
    from ui.pages.tasks.photophysics import PhotophysicsPanel
    p = PhotophysicsPanel(default_config())
    try:
        assert [p.tabs.tabText(i) for i in range(p.tabs.count())] == ["量子产率 φ", "光漂白", "表征总表"]
        p.set_default_dir(tmp_path)
        assert p.tab_phi.lbl_phi_dir.text() == str(tmp_path)
        assert p.tab_bleach.lbl_bleach_dir.text() == str(tmp_path)

        csv = tmp_path / "roi.csv"
        k = 0.05                                     # min⁻¹
        csv.write_text("time,intensity\n" + "".join(
            f"{t},{0.2 + 0.8 * math.exp(-k * t):.6f}\n" for t in range(0, 61, 3)), encoding="utf-8")
        bleach = p.tab_bleach
        assert bleach.load_bleach_csv(csv)
        res = bleach.compute_bleach()
        assert res.get("ok"), res
        assert bleach.add_bleach_to_table()
        assert p.tabs.currentWidget() is p.tab_char        # 入表后切到总表
        rows = p.rows()
        assert len(rows) == 1 and rows[0]["k_photo"] == pytest.approx(k, rel=0.05)

        out = p.tab_char.export_csv(tmp_path / "char.csv")
        assert out and (tmp_path / "char.csv").is_file()
    finally:
        p.deleteLater()


def test_photophysics_add_without_result_does_not_touch_table(qt_app):
    from core.config import default_config
    from ui.pages.tasks.photophysics import PhotophysicsPanel
    p = PhotophysicsPanel(default_config())
    try:
        assert p.tab_phi.add_phi_to_table() is False
        assert p.tab_bleach.add_bleach_to_table() is False
        assert p.rows() == [] and p.tab_char.export_csv() is None
    finally:
        p.deleteLater()


def test_workbench_pages_share_one_implementation():
    from ui.pages.spectra import SpectraPage
    from ui.pages.stack import StackPage
    from ui.pages.workbench import WorkbenchPage
    assert (StackPage.PRESET_SECTION, StackPage.PAGE_MARK) == ("stack", "③")
    assert (SpectraPage.PRESET_SECTION, SpectraPage.PAGE_MARK) == ("spectra", "④")
    for name in ("_export_working_view", "_apply_preset_to_working", "_on_display_preset_chosen",
                 "_save_display_preset_dialog", "_init_layout_sizes", "_on_advanced_style"):
        assert name not in vars(StackPage) and name not in vars(SpectraPage), name
        assert getattr(StackPage, name) is getattr(WorkbenchPage, name)
