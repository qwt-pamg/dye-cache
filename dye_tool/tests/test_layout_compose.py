# -*- coding: utf-8 -*-
"""V2.4-B（FR-27）：多面板拼版导出的测试（先写用例，再实现；A27.1–A27.5 覆盖）。

契约（设计文档 §5.2）：
* ``core/render/layout.py`` 是**唯一新 core 模块**：``LayoutSpec`` + ``compose_layout``；
* 输入是**已渲染的 PNG**，逐格等比嵌入（去坐标轴）；≤12 格；输出 PNG/SVG；
* **零 PDF**：格式一律过 ``plotstyle.filter_formats()``，用户传入 PDF → 拒绝；
* 输入缺失/损坏 → **逐格报错清单**（中文），**不产半成品文件**；
* 输出命名 ``composite_<rows>x<cols>_<HHMMSS>``，绝不覆盖既有图件；
* 后台 Job 可取消（取消 → 无半成品）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════ 公共工具 ═══════════════════════
def _png(path: Path, color=(200, 120, 60), size=(360, 270)) -> Path:
    from PIL import Image
    Image.new("RGB", size, color).save(path)
    return path


def _spec(**kw):
    from core.render.layout import LayoutSpec
    base = dict(rows=2, cols=2, dpi=150, panel_w_in=2.0, panel_h_in=1.5,
                margin_in=0.10, gutter_in=0.05)
    base.update(kw)
    return LayoutSpec(**base)


def _dark_pixels(img, box) -> int:
    """统计一个区域里"明显非白"的像素数（面板标签检测用）。"""
    a = np.asarray(img.convert("L").crop(box), dtype=float)
    return int((a < 120).sum())


# ═══════════════ A27.1 合成正确性 ═══════════════
def test_compose_2x2_png_geometry_and_non_empty_cells(tmp_path) -> None:
    from PIL import Image
    from core.render.layout import compose_layout

    imgs = [_png(tmp_path / f"fig_{i}.png", color=c) for i, c in enumerate(
        [(220, 80, 80), (80, 160, 220), (220, 200, 80), (120, 200, 120)])]
    spec = _spec()
    made = compose_layout(imgs, spec, {}, tmp_path / "out")
    assert len(made) == 1 and made[0].suffix == ".png"
    assert made[0].is_file()
    # 画布 = 各格 + (n-1) 条间距 + 两侧留白（与实现同一口径）
    want_w = int(round((spec.cols * spec.panel_w_in
                        + (spec.cols - 1) * spec.gutter_in
                        + 2 * spec.margin_in) * spec.dpi))
    want_h = int(round((spec.rows * spec.panel_h_in
                        + (spec.rows - 1) * spec.gutter_in
                        + 2 * spec.margin_in) * spec.dpi))
    with Image.open(made[0]) as im:
        assert abs(im.width - want_w) <= 3, (im.width, want_w)
        assert abs(im.height - want_h) <= 3, (im.height, want_h)
        w4, h4 = im.width // 2, im.height // 2
        for box in ((0, 0, w4, h4), (w4, 0, im.width, h4),
                    (0, h4, w4, im.height), (w4, h4, im.width, im.height)):
            a = np.asarray(im.convert("L").crop(box), dtype=float)
            assert a.size > 0 and np.nanstd(a) > 5.0, f"格子空白：{box}"


def test_compose_fewer_images_than_cells_allowed(tmp_path) -> None:
    """3 张图放 2×2：第 4 格留白但**不报错**（论文拼版常见"留一格写说明"）。"""
    from core.render.layout import compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(3)]
    made = compose_layout(imgs, _spec(), {}, tmp_path / "out")
    assert made and made[0].is_file()


def test_compose_more_images_than_cells_rejected(tmp_path) -> None:
    from core.render.layout import LayoutError, compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(5)]
    with pytest.raises(LayoutError) as ei:
        compose_layout(imgs, _spec(), {}, tmp_path / "out")
    assert "5" in str(ei.value) and "2×2" in str(ei.value)
    assert not list((tmp_path / "out").glob("*")) if (tmp_path / "out").exists() else True


def test_compose_svg_output(tmp_path) -> None:
    from core.render.layout import compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(2)]
    made = compose_layout(imgs, _spec(rows=1, cols=2, formats=("png", "svg")),
                          {}, tmp_path / "out")
    exts = sorted(p.suffix for p in made)
    assert exts == [".png", ".svg"]
    svg = next(p for p in made if p.suffix == ".svg")
    assert "<svg" in svg.read_text(encoding="utf-8", errors="replace")[:2000]


# ═══════════════ A27.2 零 PDF ═══════════════
def test_compose_rejects_pdf_and_writes_nothing(tmp_path) -> None:
    from core.render.layout import LayoutError, compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    out = tmp_path / "out"
    with pytest.raises((LayoutError, ValueError)) as ei:
        compose_layout(imgs, _spec(formats=("pdf",)), {}, out)
    assert "pdf" in str(ei.value).lower() or "PDF" in str(ei.value)
    assert not out.exists() or not list(out.glob("*")), "拒绝时不得留下任何产物"


def test_compose_mixed_pdf_format_rejected_entirely(tmp_path) -> None:
    """png+pdf：**整单拒绝**（不静默丢掉 pdf 只出 png）。"""
    from core.render.layout import LayoutError, compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    out = tmp_path / "out"
    with pytest.raises((LayoutError, ValueError)):
        compose_layout(imgs, _spec(formats=("png", "pdf")), {}, out)
    assert not out.exists() or not list(out.glob("*"))


# ═══════════════ A27.3 面板标签 ═══════════════
def test_panel_labels_on_by_default_and_switchable(tmp_path) -> None:
    from PIL import Image
    from core.render.layout import compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    on = compose_layout(imgs, _spec(panel_labels=True), {},
                        tmp_path / "on")[0]
    off = compose_layout(imgs, _spec(panel_labels=False), {},
                         tmp_path / "off")[0]
    assert on.read_bytes() != off.read_bytes(), "标签开关没有影响输出"
    with Image.open(on) as a, Image.open(off) as b:
        # 第一格左上角：带标签的那张必须出现更多深色像素（(a) 文本）
        box = (10, 10, 120, 60)
        assert _dark_pixels(a, box) > _dark_pixels(b, box) + 20


# ═══════════════ A27.4 输出纪律 ═══════════════
def test_missing_and_corrupt_inputs_report_all_cells(tmp_path) -> None:
    from core.render.layout import LayoutError, compose_layout

    good = _png(tmp_path / "good.png")
    missing = tmp_path / "缺失的图.png"
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not a png")
    out = tmp_path / "out"
    with pytest.raises(LayoutError) as ei:
        compose_layout([good, missing, corrupt, good], _spec(), {}, out)
    msg = str(ei.value)
    assert "缺失的图.png" in msg and "corrupt.png" in msg, msg
    assert not out.exists() or not list(out.glob("*")), "失败不得留下半成品"


def test_output_name_and_no_overwrite(tmp_path) -> None:
    from core.render.layout import compose_filename, compose_layout

    stem = compose_filename(2, 2, when="20261018-131500")
    assert stem == "composite_2x2_20261018-131500"
    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    out = tmp_path / "out"
    out.mkdir()
    first = compose_layout(imgs, _spec(), {}, out, name_stem=stem)[0]
    assert first.name == f"{stem}.png"
    keep = first.read_bytes()
    second = compose_layout(imgs, _spec(), {}, out, name_stem=stem)[0]
    assert second.name != first.name, "同名拼版不得覆盖既有文件"
    assert first.read_bytes() == keep, "既有图件被改写了"
    assert not any(p.name.startswith("fig_") for p in out.iterdir())


def test_input_images_are_not_modified(tmp_path) -> None:
    from core.render.layout import compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    before = [p.read_bytes() for p in imgs]
    compose_layout(imgs, _spec(), {}, tmp_path / "out")
    assert [p.read_bytes() for p in imgs] == before


def test_layout_spec_limits() -> None:
    from core.render.layout import LayoutSpec

    LayoutSpec(rows=3, cols=4)          # 12 格：允许
    with pytest.raises(ValueError):
        LayoutSpec(rows=3, cols=5).validate()
    with pytest.raises(ValueError):
        LayoutSpec(rows=0, cols=2).validate()
    with pytest.raises(ValueError):
        LayoutSpec(rows=2, cols=2, dpi=0).validate()


# ═══════════════ A27.5 交互（取消 / 空输入） ═══════════════
def test_cancel_aborts_without_output(tmp_path) -> None:
    from core.render.layout import LayoutCancelled, compose_layout

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    calls: list = []

    def cancel_cb() -> bool:
        calls.append(1)
        return len(calls) >= 2          # 画到一半取消

    out = tmp_path / "out"
    with pytest.raises(LayoutCancelled):
        compose_layout(imgs, _spec(), {}, out, cancel_cb=cancel_cb)
    assert not out.exists() or not list(out.glob("*")), "取消后不得留下半成品"


def test_empty_inputs_rejected(tmp_path) -> None:
    from core.render.layout import LayoutError, compose_layout

    with pytest.raises(LayoutError):
        compose_layout([], _spec(), {}, tmp_path / "out")


# ═══════════════ FR-27.3：规格对话框（离屏） ═══════════════
def test_layout_dialog_defaults_and_spec(qt_app, tmp_path) -> None:
    from core.render.layout import LayoutSpec
    from ui.widgets.layout_dialog import LayoutDialog

    imgs = [_png(tmp_path / f"f{i}.png") for i in range(4)]
    dlg = LayoutDialog(images=imgs, default_dir=tmp_path)
    try:
        spec = dlg.spec()
        assert isinstance(spec, LayoutSpec)
        assert (spec.rows, spec.cols) == (2, 2)
        assert spec.panel_labels is True
        assert spec.dpi >= 600
        # 图多于格 → 确定键禁用
        dlg.set_grid(1, 2)
        assert not dlg.button_box.button(
            dlg.button_box.StandardButton.Ok).isEnabled()
        dlg.set_grid(2, 2)
        assert dlg.button_box.button(
            dlg.button_box.StandardButton.Ok).isEnabled()
    finally:
        dlg.close()
        dlg.deleteLater()
        for _ in range(4):
            qt_app.processEvents()


@pytest.fixture(scope="module")
def qt_app():
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app

# ═══════════════ FR-27.3：④档案页拼版入口（离屏） ═══════════════
def test_library_page_lists_run_figures_and_enables_compose(qt_app, tmp_path,
                                                            monkeypatch) -> None:
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from ui.pages.dye_library import DyeLibraryPage

    run = tmp_path / "runs" / "Dye" / "20261018-120000"
    figdir = run / "figures"
    figdir.mkdir(parents=True)
    for i in range(4):
        _png(figdir / f"fig_{i}.png")
    page = DyeLibraryPage(out_root=tmp_path / "runs")
    try:
        page._fill_run_figures(run)
        assert page.list_figs.count() == 4
        assert page.btn_compose.isEnabled()
        sel = page._selected_figures()
        assert len(sel) == 4 and all(p.is_file() for p in sel)
        # 换成没有图件的 run → 清单清空、按钮禁用
        empty = tmp_path / "runs" / "Dye" / "empty_run"
        empty.mkdir(parents=True)
        page._fill_run_figures(empty)
        assert page.list_figs.count() == 0 and not page.btn_compose.isEnabled()
    finally:
        page.close()
        page.deleteLater()
        for _ in range(4):
            qt_app.processEvents()


def test_library_page_compose_starts_job_and_disables_button(
        qt_app, tmp_path, monkeypatch) -> None:
    """A27.5：拼版走后台 Job、执行中按钮禁用；取消/完成后恢复。"""
    monkeypatch.setenv("DYE_TOOL_CONFIG", str(tmp_path / "c.yaml"))
    from PySide6.QtWidgets import QDialog

    import ui.widgets.layout_dialog as ld
    from ui.pages.dye_library import DyeLibraryPage

    run = tmp_path / "runs" / "Dye" / "20261018-120000"
    (run / "figures").mkdir(parents=True)
    for i in range(4):
        _png(run / "figures" / f"fig_{i}.png")
    outs = tmp_path / "out"
    outs.mkdir()
    monkeypatch.setattr(ld.LayoutDialog, "exec",
                        lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ld.LayoutDialog, "spec",
                        lambda self: _spec(dpi=120))
    monkeypatch.setattr(ld.LayoutDialog, "out_dir", lambda self: outs)
    page = DyeLibraryPage(out_root=tmp_path / "runs")
    try:
        page._fill_run_figures(run)
        assert page.compose_selected() is True
        assert not page.btn_compose.isEnabled(), "执行中按钮必须禁用"
        import time as _time
        deadline = _time.time() + 20
        while _time.time() < deadline:
            job = page._compose_job
            if job is None:
                break
            job.wait(30)                 # 等后台线程真正结束（渲染需 1–2 s）
            qt_app.processEvents()
        assert page._compose_job is None, "Job 结束后未复位"
        assert page.btn_compose.isEnabled()
        assert list(outs.glob("composite_*.png")), "拼版没有产出文件"
    finally:
        job = getattr(page, "_compose_job", None)
        if job is not None and job.isRunning():
            job.cancel()
            job.wait(3000)
        page.close()
        page.deleteLater()
        for _ in range(4):
            qt_app.processEvents()
