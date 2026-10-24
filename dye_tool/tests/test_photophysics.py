# -*- coding: utf-8 -*-
"""V2.6 G 道 task-20：论文风格新图型（FR-45）+ layout panel_label_style 测试。

覆盖（A42.5/A42.6 的测试侧）：

* 三类新图型生成：文件存在 / PNG 像素尺寸 / dpi≈600 / 零 PDF / 返回 Path；
* 关键样式：内向刻度 / 四边框 / 粗体轴标签 / 无网格 / 首末包络填充 /
  黑色误差棒 / 论文式裸粗体面板字母；
* layout.panel_label_style：默认 paren 与旧输出逐字节一致，bare 为论文式；
* legacy JACS fig a–e 渲染函数存在且签名未改；零新配置键、rc 默认未改。
"""
from __future__ import annotations

import copy
import inspect
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ═══════════════════════ 公共工具 ═══════════════════════
def _frames(n: int = 4) -> list:
    x = np.linspace(300.0, 700.0, 101)
    out = []
    for i in range(n):
        y = (np.exp(-0.5 * ((x - 380.0 - i * 2.0) / 40.0) ** 2)
             * (1.0 - 0.12 * i))
        out.append({"x": x, "y": y, "t": float(i * 60)})
    return out


def _decay_data():
    t = np.linspace(0.0, 60.0, 25)
    y = 0.95 * np.exp(-0.048 * t) + 0.05
    return t, y


def _bars_data():
    groups = ["Dye-A", "Dye-B", "Dye-C"]
    series = {
        "DMAR-SY": {"values": [0.40, 0.50, 0.45],
                    "errors": [0.03, 0.04, 0.02]},
        "DMAR-TB": {"values": [0.60, 0.55, 0.70],
                    "errors": [0.05, 0.03, 0.04]},
    }
    return groups, series


def _close(fig) -> None:
    import matplotlib.pyplot as plt
    plt.close(fig)


def _png_info(path: Path) -> tuple:
    from PIL import Image
    with Image.open(path) as im:
        return im.size, im.info.get("dpi")


def _tick_direction_in(ax) -> bool:
    """matplotlib 3.11 没有可读的 tick 参数字典；用 tick 线 marker 判方向：

    x 轴下侧 tick1 的 marker：向内 = 2，向外 = 3（本机 mpl 3.11.2 实测）。
    """
    return int(ax.xaxis.get_major_ticks()[0].tick1line.get_marker()) == 2


def _assert_paper_axes(ax) -> None:
    """论文样式坐标轴断言：四边框 + 内向刻度 + 轴标签粗体 + 无网格。"""
    for name, spine in ax.spines.items():
        assert spine.get_visible(), f"四边框缺失：{name}"
    assert _tick_direction_in(ax), "主刻度必须朝内（direction=in）"
    assert ax.xaxis.label.get_fontweight() == "bold", "x 轴标签必须粗体"
    assert ax.yaxis.label.get_fontweight() == "bold", "y 轴标签必须粗体"
    assert not any(g.get_visible() for g in ax.get_xgridlines()), "不得有 x 网格"
    assert not any(g.get_visible() for g in ax.get_ygridlines()), "不得有 y 网格"


def _assert_png(path: Path, kind_height_px: int) -> None:
    assert isinstance(path, Path) and path.is_file(), path
    assert path.suffix == ".png" and path.stat().st_size > 1000
    (w, h), dpi = _png_info(path)
    assert (w, h) == (1950, kind_height_px), (w, h)
    assert dpi[0] == pytest.approx(600, rel=0.01), dpi


# ═══════════════════════ ① 光谱演化图 ═══════════════════════
def test_spectral_evolution_creates_png_and_paper_style(tmp_path) -> None:
    from core.render.photophysics import (T0_COLOR,
                                          build_spectral_evolution_figure,
                                          plot_spectral_evolution)

    out = plot_spectral_evolution(_frames(), tmp_path / "paper_evolution.png",
                                  panel_label="a")
    _assert_png(out, 1500)                       # 3.25 in x 2.5 in @600 dpi
    assert not list(tmp_path.glob("*.pdf")), "零 PDF 红线"
    fig, meta = build_spectral_evolution_figure(_frames())
    try:
        ax = fig.axes[0]
        _assert_paper_axes(ax)
        assert len(ax.collections) >= 1, "首末曲线包络 fill_between 缺失"
        assert meta["line_colors"][0] == T0_COLOR, "t=0 必须红色高亮"
        assert "h" in meta["arrow_text"] and r"\nu" in meta["arrow_text"]
        texts = [t.get_text() for t in ax.texts]
        assert "a" in texts, f"缺裸粗体面板字母 a：{texts}"
        assert ax.get_legend() is not None, "演化图应给出首/末时间内联图例"
        assert meta["n_frames"] == 4
    finally:
        _close(fig)


# ═══════════════════════ ② 衰减曲线 ═══════════════════════
def test_decay_curve_creates_png_and_inline_legend(tmp_path) -> None:
    from core.render.photophysics import (build_decay_figure,
                                          plot_decay_curve)

    t, y = _decay_data()
    fit = {"k": 0.048, "r2": 0.999}
    out = plot_decay_curve(t, y, fit, tmp_path / "paper_decay.png")
    _assert_png(out, 1500)
    fig, meta = build_decay_figure(t, y, fit)
    try:
        ax = fig.axes[0]
        _assert_paper_axes(ax)
        assert len(ax.lines) >= 2, "需要散点 + 拟合线"
        assert meta["k"] == pytest.approx(0.048, rel=1e-6)
        assert meta["t_half"] == pytest.approx(np.log(2) / 0.048, rel=1e-6)
        legend = ax.get_legend()
        assert legend is not None, "衰减图必须带内联图例"
        labels = [tx.get_text() for tx in legend.get_texts()]
        joined = " ".join(labels)
        assert "k" in joined and "min" in joined, joined
        assert "t" in joined, joined
        assert "a" in [tx.get_text() for tx in ax.texts]
    finally:
        _close(fig)


# ═══════════════════════ ③ 分组柱状图 ═══════════════════════
def test_grouped_bars_creates_png_and_style(tmp_path) -> None:
    from core.render.photophysics import (build_grouped_bars_figure,
                                          plot_grouped_bars)

    groups, series = _bars_data()
    out = plot_grouped_bars(groups, series, tmp_path / "paper_bars.png")
    _assert_png(out, 1680)                       # 3.25 in x 2.8 in @600 dpi
    fig, meta = build_grouped_bars_figure(groups, series)
    try:
        ax = fig.axes[0]
        _assert_paper_axes(ax)
        assert len(meta["bar_containers"]) == 2, "两组柱容器缺失"
        assert len(meta["errorbar_containers"]) == 2, "黑色误差棒容器缺失"
        assert meta["colors"][0] != meta["colors"][1], "必须双色"
        assert [lbl.get_rotation() for lbl in ax.get_xticklabels()] ==             [45.0, 45.0, 45.0], "x 标签必须旋转 45 度"
        assert "a" in [tx.get_text() for tx in ax.texts]
    finally:
        _close(fig)


# ═══════════════════════ 论文式面板字母（Q-E20） ═══════════════════════
def test_panel_letters_are_bare_bold_lowercase_outside() -> None:
    from core.render.photophysics import build_grouped_bars_figure

    groups, series = _bars_data()
    fig, meta = build_grouped_bars_figure(groups, series, panel_label="b")
    try:
        assert meta["panel_label"] == "b"
        assert meta["panel_label_style"] == "bare"
        letters = [t for t in fig.axes[0].texts if t.get_text() == "b"]
        assert letters, "缺论文式裸字母 b"
        letter = letters[0]
        assert letter.get_fontweight() == "bold", "面板字母必须粗体"
        assert letter.get_position()[0] < 0, "面板字母应在左上外侧"
        assert letter.get_position()[1] > 1
    finally:
        _close(fig)


# ═══════════════════════ layout.panel_label_style ═══════════════════════
def _cell_png(path: Path, color) -> Path:
    from PIL import Image
    Image.new("RGB", (160, 120), color).save(path)
    return path


def test_layout_panel_label_style_default_paren_byte_identical(tmp_path) -> None:
    from core.render.layout import LayoutSpec, compose_layout

    imgs = [_cell_png(tmp_path / "a.png", (200, 80, 80)),
            _cell_png(tmp_path / "b.png", (80, 160, 220))]
    spec = LayoutSpec(rows=1, cols=2, dpi=120, panel_w_in=2.0,
                      panel_h_in=1.5, margin_in=0.10, gutter_in=0.05)
    assert LayoutSpec().panel_label_style == "paren", "默认必须兼容旧 (a)"
    default = compose_layout(imgs, spec, {}, tmp_path / "d1",
                             name_stem="same")[0]
    paren = compose_layout(imgs, spec, {}, tmp_path / "d2", name_stem="same",
                           panel_label_style="paren")[0]
    bare = compose_layout(imgs, spec, {}, tmp_path / "d3", name_stem="same",
                          panel_label_style="bare")[0]
    assert default.read_bytes() == paren.read_bytes(), \
        "默认 paren 输出必须与显式 paren 逐字节一致（旧输出不变）"
    assert default.read_bytes() != bare.read_bytes(), "bare 必须产出不同画面"


def test_layout_panel_label_text_and_validation() -> None:
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    from core.render.layout import (PANEL_LABEL_STYLES, LayoutError,
                                    LayoutSpec, apply_panel_label,
                                    panel_label, panel_label_text)

    assert PANEL_LABEL_STYLES == ("paren", "bare")
    assert panel_label_text(0) == "(a)"
    assert panel_label_text(2) == "(c)"
    assert panel_label_text(1, style="bare") == "b"
    assert panel_label(0, style="bare") == "a"
    with pytest.raises(ValueError):
        panel_label_text(0, style="bogus")
    with pytest.raises(LayoutError):
        LayoutSpec(panel_label_style="bogus").validate()
    fig, ax = plt.subplots()
    try:
        text = apply_panel_label(ax, 0, style="bare")
        assert text.get_text() == "a"
        assert text.get_fontweight() == "bold"
        assert text.get_position()[0] < 0
        paren = apply_panel_label(ax, 1, style="paren")
        assert paren.get_text() == "(b)"
    finally:
        plt.close(fig)


# ═══════════════════════ 红线（A42.6） ═══════════════════════
def test_legacy_jacs_render_functions_unchanged() -> None:
    from core.render import plotting_jacs as pj

    expected = {
        "fig_a_overlay": ["scans", "cfg", "figdir", "captions", "save",
                          "groups", "session"],
        "fig_b_fit": ["g", "e", "cfg", "figdir", "captions", "save"],
        "fig_c_bars": ["results", "cfg", "figdir", "captions", "save"],
        "fig_d_phi": ["phi", "cfg", "figdir", "captions", "save"],
        "fig_e_residuals": ["results", "cfg", "figdir", "captions", "save"],
        "save_jacs": ["fig", "out_dir", "name", "cfg", "formats", "dpi"],
        "jacs_stack_all": ["scans", "cfg", "run_dir", "captions", "figdir",
                           "save", "group_dye", "groups"],
    }
    for name, want in expected.items():
        fn = getattr(pj, name)
        got = [p.name for p in inspect.signature(fn).parameters.values()]
        assert got == want, f"legacy 渲染函数 {name} 签名变了：{got} != {want}"


def test_zero_new_config_keys_and_rc_defaults_unchanged() -> None:
    import matplotlib

    from core.config import default_config
    from core.render import plotstyle
    from core.render import photophysics as pp
    from core.render.plotting import JACS_RC, LEGACY_RC

    schema_before = set(plotstyle.JACS_SCHEMA)
    jacs_before = copy.deepcopy(plotstyle.jacs_defaults())
    rc_before = copy.deepcopy(matplotlib.rcParams)
    jacs_rc_before, legacy_rc_before = copy.deepcopy(JACS_RC), copy.deepcopy(LEGACY_RC)
    cfg_keys_before = set(default_config()["plot"]["jacs"])

    figs = []
    try:
        figs.append(pp.build_spectral_evolution_figure(_frames()))
        t, y = _decay_data()
        figs.append(pp.build_decay_figure(t, y, {"k": 0.048}))
        groups, series = _bars_data()
        figs.append(pp.build_grouped_bars_figure(groups, series))
    finally:
        for fig, _meta in figs:
            _close(fig)

    assert set(plotstyle.JACS_SCHEMA) == schema_before, "不得新增 config 键"
    assert plotstyle.jacs_defaults() == jacs_before, "JACS 默认值不得被改"
    assert JACS_RC == jacs_rc_before and LEGACY_RC == legacy_rc_before, \
        "legacy rcParams 常量不得被改"
    assert set(default_config()["plot"]["jacs"]) == cfg_keys_before
    # 构建过程不残留全局 rcParams 改动（局部 rc_context 生效）
    for key in ("font.sans-serif", "mathtext.fontset", "axes.unicode_minus"):
        assert matplotlib.rcParams[key] == rc_before[key], f"全局 rcParams 被改：{key}"


def test_invalid_inputs_raise_chinese_value_errors(tmp_path) -> None:
    from core.render.photophysics import (plot_decay_curve,
                                          plot_grouped_bars,
                                          plot_spectral_evolution)

    with pytest.raises(ValueError) as e1:
        plot_spectral_evolution([], tmp_path / "x.png")
    assert "光谱" in str(e1.value)
    with pytest.raises(ValueError) as e2:
        plot_grouped_bars(["A"], {}, tmp_path / "y.png")
    assert "series" in str(e2.value)
    with pytest.raises(ValueError) as e3:
        plot_decay_curve([0.0, 1.0], [1.0], {"k": 0.1}, tmp_path / "z.png")
    assert "长度" in str(e3.value)
    with pytest.raises(ValueError) as e4:
        plot_decay_curve([0.0, 1.0], [2.0, 1.0], None, tmp_path / "w.png")
    assert "拟合参数" in str(e4.value)
    assert not list(tmp_path.glob("*.png")), "出错时不得留下半成品图"
