# -*- coding: utf-8 -*-
"""A 道（V2.5 FR-29）：批量目录队列 + D10 输出根单源化。

覆盖：
* A-0 D10：_out_root 四处实现先测后统（相对 → app_root；CLI 边界保留 cwd）；
* A-1/A-2：core/report/summary.py 的队列汇总（每目录一行）、Excel 写表；
* A-3/A-4：单目录 helper 同源、队列卡交互（Ctrl+5/状态/取消边界/互斥）。
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════ A-0 D10：输出根单源化（先测后统） ═══════════════════
def test_d10_resolve_out_root_relative_uses_app_root(monkeypatch, tmp_path):
    """cwd ≠ app_root 时，相对 out_root 统一落 app_root（而不是 cwd）。"""
    from core import paths

    monkeypatch.chdir(tmp_path)
    root = paths.resolve_out_root({}, None)
    assert root == paths.app_root() / "runs"
    assert root != tmp_path / "runs"


def test_d10_resolve_out_root_priority_and_absolute(monkeypatch, tmp_path):
    from core import paths

    monkeypatch.chdir(tmp_path)
    cfg = {"gui": {"out_root": "custom_out"}}
    assert paths.resolve_out_root(cfg) == paths.app_root() / "custom_out"
    abs_dir = tmp_path / "abs_out"
    assert paths.resolve_out_root(cfg, abs_dir) == abs_dir
    assert paths.resolve_out_root(cfg, str(abs_dir)) == abs_dir
    # 显式参数优先于 cfg
    assert paths.resolve_out_root(cfg, "explicit") == paths.app_root() / "explicit"


def test_d10_all_out_root_helpers_agree(monkeypatch, tmp_path):
    """excel / Φ_Δ / 主页 / 设置页四处 _out_root 与 core.paths 单一实现一致。"""
    from core import paths
    from core.report.excel_export import _out_root as excel_out_root
    from ui.pages.phi import PhiPage
    from ui.pages.home import HomePage
    from ui.pages.settings import SettingsPage

    monkeypatch.chdir(tmp_path)
    cfg = {"gui": {"out_root": "runs"}}
    want = paths.app_root() / "runs"
    fake = SimpleNamespace(cfg=cfg)
    assert excel_out_root(cfg) == want
    assert PhiPage._out_root(fake) == want
    assert HomePage._out_root(fake) == want
    assert SettingsPage._out_root(fake) == want


def test_d10_pipeline_make_figure_run_dir_uses_single_source(monkeypatch, tmp_path):
    """pipeline 的相对 out_root 也走同一解析（app_root），不再按 cwd。"""
    from core import paths
    from core.analysis import pipeline

    monkeypatch.chdir(tmp_path)
    cfg = {"gui": {"out_root": "runs"}}
    captured = {}

    def fake_make(base, dye=None, scheme=None):
        captured["base"] = Path(base)
        return tmp_path / "fake_run"

    monkeypatch.setattr("core.report.dyes.make_run_dir", fake_make)
    out = pipeline.make_figure_run_dir(cfg, ["g1"], None, subdir="stack")
    assert out == tmp_path / "fake_run"
    assert captured["base"] == paths.app_root() / "runs" / "stack"


def test_d10_cli_boundary_keeps_cwd_semantics(monkeypatch, tmp_path):
    """CLI 显式 -o 相对路径按调用时 cwd 解析（边界解析），与 GUI 分道。"""
    from core.cli import build_parser, resolve_cli_out_root

    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["data", "-o", "rel_out"])
    assert resolve_cli_out_root(args.out_root) == tmp_path / "rel_out"
    assert resolve_cli_out_root(tmp_path / "abs") == tmp_path / "abs"


# ═══════════════════ A-1/A-2：队列汇总（测试夹具） ═══════════════════
def _fake_rr(run_dir, groups) -> SimpleNamespace:
    """最小 RunResult 替身：results 每项带 pooled 拟合对象，phi 带值。"""
    class _Fit:
        def __init__(self, k, k_err=1e-5, r2=0.99, n=10, status="OK"):
            self.k, self.k_err, self.r2, self.n, self.status = k, k_err, r2, n, status

    results = {g: {"role": "sample", "pooled": _Fit(k)} for g, k in groups.items()}
    phi = {g: {"phi": 0.1, "phi_err": 0.01} for g in groups}
    return SimpleNamespace(run_dir=str(run_dir), results=results, phi=phi,
                           ok=True, message="")


def test_queue_summary_one_row_per_directory(tmp_path):
    from core.report.summary import collect_queue_summary

    rr1 = _fake_rr(tmp_path / "run1", {"G1": 1.2e-3, "G2": 3.4e-4})
    entries = [
        {"data_dir": "D:/a", "status": "ok", "rr": rr1, "duration_s": 1.5},
        {"data_dir": "D:/b", "status": "failed", "error": "无可用文件", "duration_s": 0.2},
        {"data_dir": "D:/c", "status": "cancelled"},
    ]
    rows = collect_queue_summary(entries)
    assert len(rows) == 3
    assert rows[0]["状态"] == "完成" and rows[0]["组数"] == 2
    assert "G1 | G2" == rows[0]["组别"]
    assert "0.0012" in rows[0]["k_obs(s^-1)"] and "0.00034" in rows[0]["k_obs(s^-1)"]
    assert rows[1]["状态"] == "失败" and "无可用文件" in rows[1]["异常摘要"]
    # cancelled 是"未运行"状态，不伪造失败原因
    assert rows[2]["状态"] == "取消" and rows[2]["异常摘要"] == ""


def test_queue_summary_excel_roundtrip_no_overwrite(tmp_path):
    from core.report.excel_export import write_queue_summary_workbook
    from core.report.summary import collect_queue_summary, default_output_path

    rows = collect_queue_summary([
        {"data_dir": "D:/a", "status": "ok",
         "rr": _fake_rr(tmp_path / "run1", {"G1": 1.2e-3})},
        {"data_dir": "D:/b", "status": "failed", "error": "坏目录"},
    ])
    p1 = default_output_path(tmp_path, "queue")
    assert p1.name.startswith("queue_summary_")
    assert write_queue_summary_workbook(rows, p1) == p1
    p2 = default_output_path(tmp_path, "queue")          # 同秒重名 → -2
    assert p2 != p1
    assert write_queue_summary_workbook(rows, p2) == p2
    assert p1.is_file() and p2.is_file()

    from openpyxl import load_workbook
    ws = load_workbook(p1)["队列汇总"]
    assert ws.max_row == 1 + len(rows)                   # 表头 + 目录行
    headers = [c.value for c in ws[1]]
    for col in ("k_obs(s^-1)", "k_stderr", "R2", "n", "Phi_Delta"):
        assert col in headers                            # 与 results.csv 列对齐


def test_queue_summary_empty_makes_no_file(tmp_path):
    from core.report.excel_export import write_queue_summary_workbook

    p = tmp_path / "queue_summary_000000.xlsx"
    assert write_queue_summary_workbook([], p) is None
    assert not p.exists()


# ═══════════════════ A-3/A-4：同源 helper + 队列卡（Qt 离屏） ═══════════════════
def _pump(app, n: int = 20) -> None:
    for _ in range(n):
        app.processEvents()


def _destroy(app, widget) -> None:
    try:
        widget.close()
    except Exception:                                   # noqa: BLE001
        pass
    widget.deleteLater()
    _pump(app, 4)


def test_single_directory_helper_same_source():
    """A29.3 结构断言：手动分析与队列调**同一个** core.pipeline.run_directory。"""
    import inspect

    from ui.pages.phi import PhiPage
    from ui.pages.tasks.queue import BatchQueuePanel

    src_phi = inspect.getsource(PhiPage._run)
    src_queue = inspect.getsource(BatchQueuePanel.start_queue)
    assert "run_directory(" in src_phi
    assert "run_directory(" in src_queue
    assert "load_batch(" not in src_phi               # 批次载入只在 helper 里


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.mark.slow
def test_queue_task_card_and_ctrl5(qt_app, tmp_path, monkeypatch):
    """A29.4：②分析页第 5 张卡「批量队列」+ Ctrl+5 可达（V2.6 F 道起共 6 卡，queue 仍是第 5 卡）。"""
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from PySide6.QtGui import QShortcut

    from core.config import default_config
    from ui.pages.analysis import TASKS, AnalysisPage

    page = AnalysisPage(default_config())
    try:
        assert len(TASKS) == 7 and TASKS[3][0] == "queue"
        assert TASKS[-2][0] == "photophysics"    # V2.6 G 道
        assert TASKS[-1][0] == "peaks"          # V2.7 A 道（Q-E5 已批准）
        assert page.select_task("queue") is True
        assert page.stack.currentWidget() is page.queue_panel
        keys = {sc.key().toString() for sc in page.findChildren(QShortcut)}
        assert "Ctrl+5" in keys
    finally:
        _destroy(qt_app, page)


@pytest.mark.slow
def test_queue_add_remove_clear(qt_app, tmp_path):
    from ui.pages.tasks.queue import BatchQueuePanel

    panel = BatchQueuePanel({"gui": {"out_root": str(tmp_path / "runs")}})
    try:
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        assert panel.add_directories([d1, d2]) == 2
        assert panel.list_dirs.count() == 2
        assert panel.btn_start.isEnabled()
        assert panel.add_directories([d1]) == 0          # 去重
        panel.list_dirs.item(0).setSelected(True)
        panel._remove_selected()
        assert len(panel.dirs) == 1
        panel.clear_dirs()
        assert panel.dirs == [] and not panel.btn_start.isEnabled()
    finally:
        _destroy(qt_app, panel)


@pytest.mark.slow
def test_queue_run_serial_status_and_summary(qt_app, tmp_path, monkeypatch):
    """A29.1/A29.2/A29.4：串行顺序、逐目录状态、汇总表一目录一行。"""
    from ui.pages.tasks.queue import BatchQueuePanel

    panel = BatchQueuePanel({"gui": {"out_root": str(tmp_path / "runs")}})
    try:
        d1, d2 = tmp_path / "d1", tmp_path / "d2"
        d1.mkdir()
        d2.mkdir()
        panel.add_directories([d1, d2])
        calls: list = []

        def fake_run(data_dir, cfg, **kw):
            calls.append(Path(data_dir))
            return _fake_rr(tmp_path / ("run_" + Path(data_dir).name),
                            {"G1": 1.0e-3})

        monkeypatch.setattr("core.analysis.pipeline.run_directory", fake_run)
        panel.start_queue()
        assert panel.job is not None and panel.job.wait(15000)
        _pump(qt_app, 40)
        assert calls == [d1, d2]                          # 串行、顺序一致
        assert panel.list_status.count() == 2
        assert panel._last_summary and Path(panel._last_summary).is_file()
        assert panel.btn_open_file.isEnabled()
        from openpyxl import load_workbook
        ws = load_workbook(panel._last_summary)["队列汇总"]
        assert ws.max_row == 1 + 2                        # 表头 + 目录数
    finally:
        _destroy(qt_app, panel)


@pytest.mark.slow
def test_queue_cancel_at_directory_boundary(qt_app, tmp_path, monkeypatch):
    """A29.4：取消在当前目录完成后生效；未跑目录记「取消」并写汇总。"""
    from ui.pages.tasks.queue import BatchQueuePanel

    panel = BatchQueuePanel({"gui": {"out_root": str(tmp_path / "runs")}})
    try:
        dirs = []
        for name in ("d1", "d2", "d3"):
            d = tmp_path / name
            d.mkdir()
            dirs.append(d)
        panel.add_directories(dirs)

        def fake_run(data_dir, cfg, **kw):
            if Path(data_dir).name == "d1":
                panel.job.cancel()                        # 目录边界取消检查点
            return _fake_rr(tmp_path / ("run_" + Path(data_dir).name),
                            {"G1": 1.0e-3})

        monkeypatch.setattr("core.analysis.pipeline.run_directory", fake_run)
        panel.start_queue()
        assert panel.job is not None and panel.job.wait(15000)
        _pump(qt_app, 40)
        statuses = [e.get("status") for e in panel._last_entries]
        assert statuses == ["ok", "cancelled", "cancelled"]
        assert panel.list_status.count() == 3
        assert panel._last_summary and Path(panel._last_summary).is_file()
    finally:
        _destroy(qt_app, panel)


@pytest.mark.slow
def test_queue_mutex_both_directions(qt_app, tmp_path, monkeypatch):
    """A29.4：队列 ↔ 手动任务双向互斥（禁用/恢复按钮）。"""
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from core.config import default_config
    from ui.pages.analysis import AnalysisPage

    page = AnalysisPage(default_config())
    try:
        page.queue_panel.add_directories([tmp_path])
        page._refresh_queue_mutex()
        assert page.queue_panel.btn_start.isEnabled()
        page.phi_page._busy = True                        # 手动分析忙 → 禁止排队
        page._refresh_queue_mutex()
        assert not page.queue_panel.btn_start.isEnabled()
        page.phi_page._busy = False
        page._refresh_queue_mutex()
        assert page.queue_panel.btn_start.isEnabled()
        page._on_queue_busy(True)                         # 队列忙 → 手动按钮禁用
        assert not page.phi_page.btn_run.isEnabled()
        assert not page.extinction_panel.btn_run.isEnabled()
        assert not page.wavelength_panel.btn_run.isEnabled()
        page._on_queue_busy(False)
        assert page.phi_page.btn_run.isEnabled()
        assert page.extinction_panel.btn_run.isEnabled()
    finally:
        _destroy(qt_app, page)
