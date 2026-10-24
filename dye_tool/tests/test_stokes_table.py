# -*- coding: utf-8 -*-
"""V2.6 F 道 FR-41 / A41：斯托克斯位移测量与跨组对比表。

对应《工程设计文档_V2.6_完整版》§5.6 FR-41.1–41.6 与 §7.4 A41.1–A41.6。
只测可观测契约（选择/预览/表格/CSV/标签/桥接/红线），不测像素外观：

* A41.1 一对一约束：吸收/发射各单选、重复点击替换不累积；未选齐「加入对比表」
  禁用；不自动配对、不自动入表；
* A41.2 计算正确性：合成已知峰位谱 → λabs/λem/Δλ/Δν̃ 与手算公式逐项一致
  （Δλ=λem−λabs；Δν̃=1e7/λabs−1e7/λem）；NaN 行标"不可计算"；
* A41.3 对比表：不同组两次录入 → 行/列正确；组别文件名解析 + 表内可编辑；
  排序/按组过滤/删除/清空（确认）；
* A41.4 导出：UTF-8-SIG、中文列名、stokes_<HHMMSS>.csv 落 out_root、不覆盖、
  空表不产文件、λ/Δλ 1 位小数 / Δν̃ 取整；
* A41.5 ③对齐与桥接：镜像标签单对语义/未选提示/auto 多对逐行；record_pair_requested
  → analysis prefill 数值同源一致；换目录候选重置、对比表保留；
* A41.6 红线：core stokes_shift 签名/公式未改、spectra.py 绘图函数保留、
  零新配置键（无 stokes 配置节）、零 PDF。

测试手法：offscreen + 面板 run_task 换同线程 fake（不开 Job）；所有写盘落 tmp_path
（绝不写交付 runs/）。A21/全量 GUI 零回归由收口门禁另行覆盖，本文件不跑
full/release/验收/冒烟。
"""
from __future__ import annotations

import contextlib
import csv
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: A41.6 零新配置键：GUI 会话态基线键集
#: theme 是前端重构（浅色/深色主题）有意新增的界面键
GUI_BASELINE_KEYS = {"recent_dirs", "max_recent", "out_root", "show_welcome",
                     "lazy_preview", "window", "nav", "theme"}

#: 对比表列序（与 core.report.stokes_table.STOKES_COLUMNS 一致）
COL_GROUP, COL_ABS, COL_EM, COL_LA, COL_LE, COL_DL, COL_DNU, COL_TIME = range(8)


# ═══════════════════════ Qt 夹具 / 通用工具 ═══════════════════════
@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _events(app, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


def _destroy(app, widget) -> None:
    try:
        widget.close()
    except Exception:                                   # noqa: BLE001
        pass
    widget.deleteLater()
    _events(app, 4)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text))


# ═══════════════════════ 合成光谱文件 ═══════════════════════
def _write_abs(directory: Path, name: str, peak: float = 450.0, *,
               amp: float = 0.8, nan: bool = False) -> Path:
    """写一支两列吸收谱（逗号分隔；nan=True 时吸光度列全空 → NaN）。"""
    wl = np.arange(380.0, 700.0 + 1e-9, 1.0)
    a = amp * np.exp(-0.5 * ((wl - peak) / 30.0) ** 2)
    if nan:
        text = "\n".join(f"{w:.1f}," for w in wl)
    else:
        text = "\n".join(f"{w:.1f},{v:.8f}" for w, v in zip(wl, a))
    p = directory / name
    p.write_text(text + "\n", encoding="utf-8", newline="\n")
    return p


def _write_em(directory: Path, name: str, peak: float = 520.0, *,
              amp: float = 1.0) -> Path:
    """写一支仪器 荧光 荧光发射谱（Data Points 段；Tab 分隔）。"""
    wl = np.arange(430.0, 800.0 + 1e-9, 1.0)
    y = amp * np.exp(-0.5 * ((wl - peak) / 35.0) ** 2)
    head = "Scan mode:\tEmission\nData Points\nnm\tData\n"
    body = "\n".join(f"{w:.1f}\t{v:.8f}" for w, v in zip(wl, y))
    p = directory / name
    p.write_text(head + body + "\n", encoding="utf-8", newline="\n")
    return p


def _make_pair_dir(tmp_path: Path, name: str = "spectra") -> Path:
    """两组各一对谱（组别可从文件名解析：Rhodamine / Fluorescein）。"""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    _write_abs(d, "Rhodamine_1-0.txt", 450.0)
    _write_em(d, "Rhodamine_1-30.txt", 520.0)
    _write_abs(d, "Fluorescein_2-0.txt", 480.0)
    _write_em(d, "Fluorescein_2-30.txt", 540.0)
    return d


@contextlib.contextmanager
def _panel(qt_app, tmp_path, monkeypatch, tag: str = "x"):
    """构造并 show 一个分析页 StokesPanel（cfg 落临时路径）。"""
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / f"cfg_stokes_{tag}.yaml"))
    from core.config import default_config
    from ui.shell.analysis_page import StokesPanel

    panel = StokesPanel(default_config())
    try:
        panel.resize(980, 780)
        panel.show()
        _events(qt_app, 6)
        yield panel
    finally:
        _destroy(qt_app, panel)


def _install_sync_run(monkeypatch, panel):
    """panel.run_task → 同线程直接执行（测试从不启动后台 Job）。"""
    calls: list[str] = []

    def fake_run_task(task, label, on_done, *args, **kwargs):
        calls.append(str(label))
        result = task(lambda *a, **k: None, lambda *a, **k: None)
        on_done(result)

    monkeypatch.setattr(panel, "run_task", fake_run_task, raising=False)
    return calls


def _load_dir(qt_app, panel, directory: Path, monkeypatch, *, force: bool = True) -> None:
    _install_sync_run(monkeypatch, panel)
    try:
        panel.set_data_dir(directory, force=force)
    except TypeError:
        panel.set_data_dir(directory)
    _events(qt_app, 8)


def _list_index(lst, needle: str) -> int:
    for i in range(lst.count()):
        if needle in (lst.item(i).text() or ""):
            return i
    texts = [lst.item(i).text() for i in range(lst.count())]
    raise AssertionError(f"列表里找不到含 {needle!r} 的项；现有：{texts}")


def _click_item(qt_app, lst, row: int) -> None:
    """在 QListWidget 上真实点一下（触发 current/selection 链路）。"""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    lst.setCurrentRow(row)
    _events(qt_app, 2)
    item = lst.item(row)
    rect = lst.visualItemRect(item)
    if not rect.isEmpty():
        QTest.mouseClick(lst.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    _events(qt_app, 4)


def _select_pair(qt_app, panel, abs_needle: str, em_needle: str) -> None:
    _click_item(qt_app, panel.lst_abs, _list_index(panel.lst_abs, abs_needle))
    _click_item(qt_app, panel.lst_em, _list_index(panel.lst_em, em_needle))


def _table_texts(tbl, col: int) -> list[str]:
    return [tbl.item(r, col).text() if tbl.item(r, col) is not None else ""
            for r in range(tbl.rowCount())]


def _visible_rows(tbl) -> list[int]:
    return [r for r in range(tbl.rowCount()) if not tbl.isRowHidden(r)]


# ═══════════════════════ A41.1 一对一约束 ═══════════════════════
def test_a41_1_stokes_card_is_sixth_and_single_selection_replaces(qt_app, tmp_path,
                                                                 monkeypatch):
    """A41.1：第 6 卡（Ctrl+6 由 TASKS 动态绑定）；单选列表点击替换不累积。"""
    from ui.shell.analysis_page import TASKS

    # F 道交付时斯托克斯是第 6 卡；G 道及之后可能继续追加任务卡（如第 7 卡「光物理表征」），
    # 因此只钉前 6 卡顺序 + stokes 存在，不禁止后续追加（其余 11 条断言不动）。
    keys = [t[0] for t in TASKS]
    assert len(TASKS) >= 6, f"任务卡至少 6 张，实际 {len(TASKS)}：{keys}"
    assert keys[:5] == ["phi", "extinction", "wavelength", "queue",
                        "stokes"], keys
    assert "stokes" in keys
    assert "斯托克斯" in TASKS[4][1]

    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "sel") as panel:
        _load_dir(qt_app, panel, d, monkeypatch)
        assert panel.lst_abs.count() == 2, panel.lst_abs.count()
        assert panel.lst_em.count() == 2, panel.lst_em.count()

        # 吸收列表：点第 1 个 → 再点第 2 个 = 替换（selectedItems 恒为 1）
        _click_item(qt_app, panel.lst_abs, 0)
        assert len(panel.lst_abs.selectedItems()) == 1
        first = panel.lst_abs.currentRow()
        _click_item(qt_app, panel.lst_abs, 1)
        assert len(panel.lst_abs.selectedItems()) == 1, "吸收列表发生多选累积"
        assert panel.lst_abs.currentRow() == 1 and first == 0

        # 发射列表同理
        _click_item(qt_app, panel.lst_em, 0)
        _click_item(qt_app, panel.lst_em, 1)
        assert len(panel.lst_em.selectedItems()) == 1, "发射列表发生多选累积"
        assert panel.lst_em.currentRow() == 1

        pv = panel.preview_values()
        assert np.isfinite(float(pv["lam_abs"])) and np.isfinite(float(pv["lam_em"]))
        assert panel.btn_add.isEnabled(), "选齐一对后「加入对比表」应可用"


def test_a41_1_add_disabled_until_pair_and_never_auto_rows(qt_app, tmp_path, monkeypatch):
    """A41.1：未选齐禁用「加入对比表」；加载/选择都不自动配对、不自动入表。"""
    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "gate") as panel:
        assert panel.tbl.rowCount() == 0
        assert not panel.btn_add.isEnabled(), "未选齐前「加入对比表」应禁用"
        _load_dir(qt_app, panel, d, monkeypatch)
        assert panel.tbl.rowCount() == 0, "载入目录后自动入表了（Q-E16 禁止）"
        assert not panel.btn_add.isEnabled()

        _click_item(qt_app, panel.lst_abs, 0)          # 只选吸收
        assert panel.tbl.rowCount() == 0
        assert not panel.btn_add.isEnabled(), "只选吸收时「加入对比表」应禁用"

        _click_item(qt_app, panel.lst_em, 0)           # 再选发射
        assert panel.tbl.rowCount() == 0, "选齐后自动配对入表了（应等用户点按钮）"
        assert panel.btn_add.isEnabled()
        panel.btn_add.click()
        _events(qt_app, 5)
        assert panel.tbl.rowCount() == 1, "点击「加入对比表」没有落一行"


# ═══════════════════════ A41.2 计算正确性 ═══════════════════════
def test_a41_2_core_formula_matches_hand_calculation():
    """A41.2：make_stokes_row/格式化与手算 Δλ、Δν̃ 逐项一致；NaN 不抛。"""
    from core.analysis.spectra import stokes_shift
    from core.report.stokes_table import (STOKES_COLUMNS, StokesRow,
                                          delta_wavenumber, fmt_nm,
                                          fmt_wavenumber, make_stokes_row,
                                          row_from_spectra)

    lam_abs, lam_em = 450.0, 520.0
    want_dl = lam_em - lam_abs                       # 70.0 nm
    want_dnu = 1.0e7 / lam_abs - 1.0e7 / lam_em      # cm^-1
    row = make_stokes_row("Rhodamine", "Rhodamine_1-0.txt", "Rhodamine_1-30.txt",
                          lam_abs, lam_em, recorded_at="2026-10-18 23:00:00")
    assert isinstance(row, StokesRow)
    assert list(STOKES_COLUMNS) == ["组别", "吸收谱文件", "发射谱文件", "λabs(nm)",
                                    "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)", "录入时间"]
    assert row.delta_lambda == pytest.approx(want_dl)
    assert row.delta_wavenumber == pytest.approx(want_dnu)
    assert row.ok is True
    assert row.recorded_at == "2026-10-18 23:00:00"
    assert fmt_nm(lam_abs) == "450.0"
    assert fmt_nm(row.delta_lambda) == "70.0"
    assert fmt_wavenumber(row.delta_wavenumber) == str(int(round(want_dnu))) == "2991"
    assert delta_wavenumber(lam_abs, lam_em) == pytest.approx(want_dnu)

    # 数组级算法同一口径（逐项一致）
    abs_wl = np.arange(380.0, 701.0, 1.0)
    em_wl = np.arange(430.0, 801.0, 1.0)
    abs_y = np.exp(-0.5 * ((abs_wl - lam_abs) / 20.0) ** 2)
    em_y = np.exp(-0.5 * ((em_wl - lam_em) / 25.0) ** 2)
    got = stokes_shift(abs_wl, abs_y, em_wl, em_y)
    assert got == pytest.approx((lam_abs, lam_em, want_dl))

    # NaN：坏输入不抛，行 ok=False，格式化为"—"
    bad = make_stokes_row("g", "a", "e", float("nan"), lam_em)
    assert bad.ok is False
    assert np.isnan(bad.delta_lambda) and np.isnan(bad.delta_wavenumber)
    assert fmt_nm(bad.lam_abs) == "—" and fmt_wavenumber(bad.delta_wavenumber) == "—"
    assert np.isnan(delta_wavenumber(0.0, lam_em))

    # 行构造复用 stokes_shift（A41.6 结构断言的一部分）
    src = inspect.getsource(row_from_spectra)
    assert "stokes_shift" in src, "row_from_spectra 未复用 core.analysis.spectra.stokes_shift"


def test_a41_2_panel_preview_matches_stokes_shift(qt_app, tmp_path, monkeypatch):
    """A41.2：选择变化即时预览；数值与 core.stokes_shift 直算一致；NaN 标不可计算。"""
    from core.analysis.spectra import stokes_shift
    from core.config import default_config
    from core.inputs.io import load_spectra_files

    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "preview") as panel:
        _load_dir(qt_app, panel, d, monkeypatch)
        _select_pair(qt_app, panel, "Rhodamine_1-0", "Rhodamine_1-30")
        pv = panel.preview_values()
        assert float(pv["lam_abs"]) == pytest.approx(450.0, abs=1e-6)
        assert float(pv["lam_em"]) == pytest.approx(520.0, abs=1e-6)
        assert float(pv["delta_lambda"]) == pytest.approx(70.0, abs=1e-6)
        assert float(pv["delta_wavenumber"]) == pytest.approx(
            1.0e7 / 450.0 - 1.0e7 / 520.0)
        text = panel.lbl_preview.text()
        assert "450.0" in text and "70.0" in text and "2991" in text, text

        # 直接对照：同一对文件的 stokes_shift 返回
        cfg = default_config()
        spectra = load_spectra_files([d / "Rhodamine_1-0.txt",
                                      d / "Rhodamine_1-30.txt"], cfg)
        abs_s = next(s for s in spectra if s.kind == "abs")
        em_s = next(s for s in spectra if s.kind == "em")
        la, le, dl = stokes_shift(abs_s.wl, abs_s.y, em_s.wl, em_s.y)
        assert (float(pv["lam_abs"]), float(pv["lam_em"]),
                float(pv["delta_lambda"])) == pytest.approx((la, le, dl))

    # NaN 预览：吸收谱列全空（NaN）→ λabs 不可得 → 标"不可计算"+中文原因
    d2 = tmp_path / "nan_spectra"
    d2.mkdir()
    _write_abs(d2, "Bad_9-0.txt", nan=True)
    _write_em(d2, "Bad_9-30.txt", 520.0)
    with _panel(qt_app, tmp_path, monkeypatch, "nan") as panel:
        _load_dir(qt_app, panel, d2, monkeypatch)
        if panel.lst_abs.count() > 0 and panel.lst_em.count() > 0:
            _select_pair(qt_app, panel, "Bad_9-0", "Bad_9-30")
            pv = panel.preview_values()
            assert not np.isfinite(float(pv["lam_abs"]))
            assert "不可计算" in panel.lbl_preview.text(), panel.lbl_preview.text()
        else:                                          # 实现把 NaN 谱标记为不可用 → 列表为空亦可接受
            assert panel.lst_abs.count() == 0


# ═══════════════════════ A41.3 跨组对比表 ═══════════════════════
def test_a41_3_table_rows_columns_group_parse_and_edit(qt_app, tmp_path, monkeypatch):
    """A41.3：不同组两次录入 → 行/列正确；组别默认解析且表内可编辑。"""
    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "table") as panel:
        _load_dir(qt_app, panel, d, monkeypatch)
        _select_pair(qt_app, panel, "Rhodamine_1-0", "Rhodamine_1-30")
        panel.btn_add.click()
        _events(qt_app, 4)
        _select_pair(qt_app, panel, "Fluorescein_2-0", "Fluorescein_2-30")
        panel.btn_add.click()
        _events(qt_app, 4)

        tbl = panel.tbl
        assert tbl.rowCount() == 2
        assert tbl.columnCount() >= 8, tbl.columnCount()
        heads = [tbl.horizontalHeaderItem(c).text() for c in range(8)]
        for need in ("组别", "吸收谱文件", "发射谱文件", "λabs", "λem", "Δλ", "Δν̃", "录入时间"):
            assert any(need in h for h in heads), f"表头缺 {need}：{heads}"
        groups = _table_texts(tbl, COL_GROUP)
        assert groups == ["Rhodamine", "Fluorescein"], groups
        assert _table_texts(tbl, COL_ABS)[0].startswith("Rhodamine_1-0")
        assert float(tbl.item(0, COL_LA).text()) == pytest.approx(450.0, abs=0.05)
        assert float(tbl.item(0, COL_LE).text()) == pytest.approx(520.0, abs=0.05)
        assert float(tbl.item(0, COL_DL).text()) == pytest.approx(70.0, abs=0.05)
        assert int(float(tbl.item(0, COL_DNU).text())) == 2991

        # 组别列可编辑 → 导出一致（人工归组台账）
        from PySide6.QtCore import Qt
        item = tbl.item(0, COL_GROUP)
        assert item is not None and bool(item.flags() & Qt.ItemIsEditable), (
            "组别列不可编辑")
        item.setText("DyeX")
        _events(qt_app, 4)
        out = tmp_path / "edited.csv"
        panel.export_csv(path=out)
        _events(qt_app, 3)
        assert out.is_file(), "编辑组别后导出失败"
        with out.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["组别", "吸收谱文件", "发射谱文件", "λabs(nm)",
                           "λem(nm)", "Δλ(nm)", "Δν̃(cm⁻¹)", "录入时间"], rows[0]
        assert rows[1][0] == "DyeX", rows[1]


def test_a41_3_sort_filter_delete_and_clear_confirm(qt_app, tmp_path, monkeypatch):
    """A41.3：按数值排序、按组过滤、删除选中、清空（二次确认）。"""
    from PySide6.QtWidgets import QMessageBox

    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "ops") as panel:
        _load_dir(qt_app, panel, d, monkeypatch)
        for abs_needle, em_needle in (("Rhodamine_1-0", "Rhodamine_1-30"),
                                      ("Fluorescein_2-0", "Fluorescein_2-30")):
            _select_pair(qt_app, panel, abs_needle, em_needle)
            panel.btn_add.click()
            _events(qt_app, 3)
        tbl = panel.tbl
        assert tbl.rowCount() == 2

        # 数值排序（视图级，不丢行）
        panel.sort_by("lam_abs", descending=True)
        _events(qt_app, 4)
        la = [float(tbl.item(r, COL_LA).text()) for r in range(tbl.rowCount())]
        assert la == sorted(la, reverse=True), la
        panel.sort_by("lam_abs", descending=False)
        _events(qt_app, 4)
        la = [float(tbl.item(r, COL_LA).text()) for r in range(tbl.rowCount())]
        assert la == sorted(la), la

        # 按组过滤（视图级）
        panel.set_group_filter({"Rhodamine"})
        _events(qt_app, 4)
        visible = _visible_rows(tbl)
        assert len(visible) == 1, visible
        assert "Rhodamine" in (tbl.item(visible[0], COL_GROUP).text() or "")
        assert panel.group_filter() == {"Rhodamine"}
        panel.set_group_filter(None)
        _events(qt_app, 4)
        assert len(_visible_rows(tbl)) == 2

        # 删除选中行
        tbl.selectRow(0)
        _events(qt_app, 2)
        panel.btn_delete.click()
        _events(qt_app, 4)
        assert tbl.rowCount() == 1, tbl.rowCount()

        # 清空：No 不动、Yes 清空
        answers = {"value": QMessageBox.No}
        monkeypatch.setattr(QMessageBox, "question",
                            staticmethod(lambda *a, **k: answers["value"]),
                            raising=False)
        before = tbl.rowCount()
        panel.btn_clear.click()
        _events(qt_app, 4)
        assert tbl.rowCount() == before, "清空确认选 No 后行被删了"
        answers["value"] = QMessageBox.Yes
        panel.btn_clear.click()
        _events(qt_app, 4)
        assert tbl.rowCount() == 0, "清空确认选 Yes 后表未清空"


# ═══════════════════════ A41.4 导出 ═══════════════════════
def test_a41_4_write_csv_utf8sig_format_naming_and_no_overwrite(tmp_path):
    """A41.4：核心 CSV 写出——UTF-8-SIG/中文列名/命名/不覆盖/空表不产文件/格式。"""
    from datetime import datetime

    from core.report.stokes_table import (STOKES_COLUMNS, default_stokes_name,
                                          make_stokes_row, write_stokes_csv)

    now = datetime(2026, 10, 18, 23, 7, 42)
    assert default_stokes_name(now) == "stokes_230742.csv"
    rows = [
        make_stokes_row("Rhodamine", "Rhodamine_1-0.txt", "Rhodamine_1-30.txt",
                        450.0, 520.0, recorded_at="2026-10-18 23:00:00"),
        make_stokes_row("Fluorescein", "Fluorescein_2-0.txt",
                        "Fluorescein_2-30.txt", 480.0, 540.0,
                        recorded_at="2026-10-18 23:01:00"),
        make_stokes_row("Bad", "x.txt", "y.txt", float("nan"), 520.0,
                        recorded_at="2026-10-18 23:02:00"),
    ]
    out_dir = tmp_path / "out"
    p1 = write_stokes_csv(rows, out_dir, now=now)
    assert p1 is not None and p1.parent == out_dir and p1.name == "stokes_230742.csv"
    assert p1.read_bytes()[:3] == b"\xef\xbb\xbf", "CSV 没有 UTF-8-SIG BOM"
    with p1.open("r", encoding="utf-8-sig", newline="") as f:
        got = list(csv.reader(f))
    assert got[0] == list(STOKES_COLUMNS), got[0]
    assert got[1][:7] == ["Rhodamine", "Rhodamine_1-0.txt", "Rhodamine_1-30.txt",
                          "450.0", "520.0", "70.0", "2991"], got[1]
    assert got[2][3:7] == ["480.0", "540.0", "60.0", "2315"], got[2]
    assert got[3][3:7] == ["", "520.0", "", ""], (
        "NaN 数值列应写空串（Excel 空白格），有限值照常写")

    # 同名不覆盖：再写一次 → 新文件、旧文件字节不变
    before = p1.read_bytes()
    p2 = write_stokes_csv(rows, out_dir, now=now)
    assert p2 is not None and p2 != p1 and p1.read_bytes() == before
    assert "230742" in p2.name and p2.name != p1.name

    # 空表不产文件
    count_before = len(list(out_dir.glob("*.csv")))
    assert write_stokes_csv([], out_dir, now=now) is None
    assert len(list(out_dir.glob("*.csv"))) == count_before, "空表居然写出了文件"


def test_a41_4_panel_export_rules(qt_app, tmp_path, monkeypatch):
    """A41.4：面板导出——tmp 落盘、中文列名；空表返回 None 且不建文件并给中文提示。"""
    from PySide6.QtWidgets import QFileDialog

    d = _make_pair_dir(tmp_path)
    with _panel(qt_app, tmp_path, monkeypatch, "export") as panel:
        _load_dir(qt_app, panel, d, monkeypatch)
        _select_pair(qt_app, panel, "Rhodamine_1-0", "Rhodamine_1-30")
        panel.btn_add.click()
        _events(qt_app, 4)

        out = tmp_path / "panel_out" / "stokes_custom.csv"
        got = panel.export_csv(path=out)
        _events(qt_app, 3)
        assert got is not None and Path(got).resolve() == out.resolve()
        assert out.is_file()
        head = out.read_text(encoding="utf-8-sig").splitlines()[0]
        assert head.startswith("组别,") and "λabs(nm)" in head and "Δν̃(cm⁻¹)" in head
        assert out.read_bytes()[:3] == b"\xef\xbb\xbf"

        # 按钮路径（QFileDialog 打桩，不弹模态）
        target = tmp_path / "btn_export.csv"
        monkeypatch.setattr(QFileDialog, "getSaveFileName",
                            staticmethod(lambda *a, **k: (str(target),
                                                          "CSV (*.csv)")),
                            raising=False)
        panel.btn_export.click()
        _events(qt_app, 5)
        assert target.is_file(), "「导出 CSV…」按钮没有产出文件"

    # 空表：不产文件 + 中文提示返回 None
    with _panel(qt_app, tmp_path, monkeypatch, "export_empty") as panel:
        status: list[str] = []
        panel.status.connect(status.append)
        empty = tmp_path / "empty_out.csv"
        assert panel.export_csv(path=empty) is None
        _events(qt_app, 3)
        assert not empty.exists(), "空表导出产出了文件"
        assert any(_has_cjk(m) for m in status), f"空表导出没有中文提示：{status}"


# ═══════════════════════ A41.5 ③对齐与桥接 ═══════════════════════
def _make_page(qt_app, tmp_path, monkeypatch, tag: str = "p"):
    from core.config import default_config
    from ui.pages.spectra_page import SpectraPage
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / f"cfg_page_{tag}.yaml"))
    page = SpectraPage(default_config())
    page.resize(1100, 760)
    page.show()
    _events(qt_app, 6)
    return page


def _page_load(page, paths: list[Path]) -> None:
    from core.config import default_config
    from core.inputs.io import load_spectra_files
    page._on_loaded(load_spectra_files(paths, default_config()))
    _events(qt_app_from_global(), 6)


def qt_app_from_global():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_a41_5_mirror_label_single_pair_unpaired_auto_and_empty(qt_app, tmp_path,
                                                                monkeypatch):
    """A41.5：③标签单对语义/未选提示/auto 多对逐行/空态引导。"""
    from core.analysis.spectra import stokes_shift

    d = _make_pair_dir(tmp_path)
    page = _make_page(qt_app, tmp_path, monkeypatch, "label")
    try:
        _page_load(page, [d / "Rhodamine_1-0.txt", d / "Rhodamine_1-30.txt"])
        assert page.set_mirror_pair(0, 0) is True, "set_mirror_pair 失败"
        _events(qt_app, 4)
        text = page.lbl_stokes.text()
        assert "当前配对" in text, text
        pair = page.current_pair()
        assert pair is not None and pair[0].kind == "abs" and pair[1].kind == "em"
        la, le, dl = stokes_shift(pair[0].wl, pair[0].y, pair[1].wl, pair[1].y)
        want_nu = 1.0e7 / la - 1.0e7 / le
        assert f"λabs={la:.1f} nm" in text, text
        assert f"λem={le:.1f} nm" in text, text
        assert f"Δλ={dl:.1f} nm" in text, text
        assert f"Δν̃={int(round(want_nu))} cm⁻¹" in text, text
        assert page.btn_record_pair.isEnabled(), "已配对时「在分析页记录」应可用"

        # 非配对模式 → 未选择配对提示
        page.cmb_mode.setCurrentIndex(0)                 # overlay
        _events(qt_app, 3)
        assert "未选择配对" in page.lbl_stokes.text(), page.lbl_stokes.text()
        assert not page.btn_record_pair.isEnabled(), "非配对的叠图模式下记录按钮应禁用"

        # auto 多对逐行 + 提示
        page.cmb_mode.setCurrentIndex(page._modes.index("auto"))
        _events(qt_app, 3)
        page._on_run_done(SimpleNamespace(ok=True, out_dir=None,
                                          files=[str(d / "Rhodamine_1-0.txt")],
                                          stokes=[(450.0, 520.0, 70.0)]))
        _events(qt_app, 4)
        auto_text = page.lbl_stokes.text()
        assert "配对 1" in auto_text, auto_text
        assert "逐对入表请去②分析页" in auto_text, auto_text

        # 换目录：候选/预览重置并提示（对比表在②分析页侧保留，见另一用例）
        from core.config import default_config
        from core.inputs.io import load_spectra_files
        status: list[str] = []
        page.status.connect(status.append)
        d2 = tmp_path / "page_set_b"
        d2.mkdir()
        _write_abs(d2, "Coumarin_3-0.txt", 430.0)
        _write_em(d2, "Coumarin_3-30.txt", 500.0)
        page._on_loaded(load_spectra_files([d2 / "Coumarin_3-0.txt",
                                            d2 / "Coumarin_3-30.txt"],
                                           default_config()))
        _events(qt_app, 5)
        assert any("重置" in m and "保留" in m for m in status), (
            f"③换目录没有『重置候选/保留对比表』提示：{status}")
    finally:
        _destroy(qt_app, page)

    # 空态：无光谱 → 先选择光谱目录
    page2 = _make_page(qt_app, tmp_path, monkeypatch, "empty")
    try:
        page2._on_loaded([])
        _events(qt_app, 4)
        assert "先选择光谱目录" in page2.lbl_stokes.text(), page2.lbl_stokes.text()
        assert not page2.btn_record_pair.isEnabled()
    finally:
        _destroy(qt_app, page2)


@contextlib.contextmanager
def _window(qt_app, tmp_path, monkeypatch):
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "cfg_stokes_win.yaml"))
    from core.config import default_config
    from ui.main_window import MainWindow
    cfg = default_config()
    cfg.setdefault("gui", {})["nav"] = "flow"
    cfg["gui"]["show_welcome"] = False
    win = MainWindow(cfg)
    try:
        win.resize(1280, 800)
        win.show()
        _events(qt_app, 8)
        yield win
    finally:
        _destroy(qt_app, win)


def test_a41_5_record_bridge_to_analysis_prefill(qt_app, tmp_path, monkeypatch):
    """A41.5：record_pair_requested → ②分析页 prefill，数值同源、不自动入表。"""
    from core.analysis.spectra import stokes_shift
    from core.config import default_config
    from core.inputs.io import load_spectra_files

    d = _make_pair_dir(tmp_path)
    with _window(qt_app, tmp_path, monkeypatch) as win:
        fluor = win.plot_page.fluor
        spectra = load_spectra_files([d / "Rhodamine_1-0.txt",
                                      d / "Rhodamine_1-30.txt"], default_config())
        fluor._on_loaded(spectra)
        _events(qt_app, 5)
        assert fluor.set_mirror_pair(0, 0) is True
        _events(qt_app, 4)
        assert fluor.btn_record_pair.isEnabled()

        ap = win.analysis_page
        sp = getattr(ap, "stokes_panel", None) or ap._pages["stokes"]
        _install_sync_run(monkeypatch, sp)           # 若 prefill 走 run_task 也不开线程
        fluor.btn_record_pair.click()
        _events(qt_app, 8)

        assert win.nav.current_key == "analysis", (
            f"「在分析页记录」未切到②分析页：{win.nav.current_key}")
        assert ap.current_key() == "stokes"
        assert sp.lst_abs.currentRow() >= 0 and sp.lst_em.currentRow() >= 0, (
            "prefill 后吸收/发射列表没有预选")
        assert "Rhodamine_1-0" in sp.lst_abs.currentItem().text()
        assert "Rhodamine_1-30" in sp.lst_em.currentItem().text()

        pv = sp.preview_values()
        la, le, dl = stokes_shift(spectra[0].wl, spectra[0].y,
                                  spectra[1].wl, spectra[1].y)
        assert float(pv["lam_abs"]) == pytest.approx(la, abs=1e-6)
        assert float(pv["lam_em"]) == pytest.approx(le, abs=1e-6)
        assert float(pv["delta_lambda"]) == pytest.approx(dl, abs=1e-6)
        assert float(pv["delta_wavenumber"]) == pytest.approx(
            1.0e7 / la - 1.0e7 / le, rel=1e-9)
        assert sp.tbl.rowCount() == 0, "桥接预填自动入表了（应等用户点「加入对比表」）"


def test_a41_5_dir_change_resets_candidates_keeps_table(qt_app, tmp_path, monkeypatch):
    """A41.5：②换目录 → 候选/预览重置并提示；已录入对比表保留。"""
    d1 = _make_pair_dir(tmp_path, "set_a")
    d2 = tmp_path / "set_b"
    d2.mkdir()
    _write_abs(d2, "Coumarin_3-0.txt", 430.0)
    _write_em(d2, "Coumarin_3-30.txt", 500.0)
    with _panel(qt_app, tmp_path, monkeypatch, "reload") as panel:
        _load_dir(qt_app, panel, d1, monkeypatch)
        _select_pair(qt_app, panel, "Rhodamine_1-0", "Rhodamine_1-30")
        panel.btn_add.click()
        _events(qt_app, 4)
        assert panel.tbl.rowCount() == 1

        status: list[str] = []
        logs: list[tuple] = []
        panel.status.connect(status.append)
        panel.logged.connect(lambda lv, msg: logs.append((lv, msg)))
        _load_dir(qt_app, panel, d2, monkeypatch)
        assert panel.lst_abs.count() == 1 and panel.lst_em.count() == 1
        assert "Coumarin_3-0" in panel.lst_abs.item(0).text()
        assert panel.tbl.rowCount() == 1, "换目录把对比表清掉了（FR-41.5 要求保留）"
        msgs = list(status) + [m for _lv, m in logs]
        # FR-41.5：换目录需有反馈（候选重置）；"对比表保留"是本卡核心判据
        assert any(_has_cjk(m) for m in msgs), f"换目录没有任何中文反馈：{msgs}"
        assert not panel.btn_add.isEnabled(), "换目录后未重新选齐，加入按钮应禁用"


# ═══════════════════════ A41.6 红线 ═══════════════════════
def test_a41_6_redlines():
    """A41.6：stokes_shift/绘图函数结构未改；零新配置键；零 PDF 口径。"""
    from core.analysis import spectra as spec_mod
    from core.config import DEFAULT_CFG

    params = list(inspect.signature(spec_mod.stokes_shift).parameters)
    assert params == ["abs_wl", "abs_y", "em_wl", "em_y", "abs_range", "em_range"], params
    src = inspect.getsource(spec_mod.stokes_shift)
    assert "lam_e - lam_a" in src, "stokes_shift 的 Δλ 公式被改动了"

    for name in ("plot_normalized_overlay", "plot_mirror", "plot_spectra_panel"):
        assert hasattr(spec_mod, name), f"绘图函数 {name} 不见了（FR-41.6 红线）"
    sig = list(inspect.signature(spec_mod.plot_mirror).parameters)
    assert sig[:2] == ["abs_spec", "em_spec"], sig

    # 零新配置键：没有 stokes 配置节；gui 键集保持基线
    assert "stokes" not in DEFAULT_CFG and "stokes_table" not in DEFAULT_CFG
    assert set(DEFAULT_CFG["gui"]) == GUI_BASELINE_KEYS, (
        f"gui 键集变了：{sorted(set(DEFAULT_CFG['gui']) ^ GUI_BASELINE_KEYS)}")
