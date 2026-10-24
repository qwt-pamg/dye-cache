# -*- coding: utf-8 -*-
"""JACS 期刊风格绘图层（``core/plotting_jacs.py``）与风格切换的测试。

重点覆盖：
* 画布**精确单栏尺寸**（3.25 in @600 dpi → PNG 宽恰为 1950 px）；
* 轴样式合规（四边完整框、刻度朝内、7 pt 刻度、无科学计数法偏移）；
* 谱图规范（无 colorbar、无图内标题、无竖排文字、379 灰细虚线 + 水平"379"、
  水平"Resorufin"、532 默认不画）；
* **趋势箭头方向**必须与探针吸光度一致（回归保护：曾因 t=0 截断文件而误判为上升）；
* 图注（captions.md）落盘；
* 风格切换：``style='legacy'`` 时 ``plot_all`` 把绘图参数钉回 legacy（dpi/formats）。
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("matplotlib")

from PIL import Image                                            # noqa: E402

import core.plotting_jacs as J                                   # noqa: E402
from core.config import default_config                           # noqa: E402
from core.plotting import (JACS_RC, LEGACY_RC, apply_jacs_style,   # noqa: E402
                           apply_plot_style, plot_style)
from conftest import SAMPLES_UVVIS, make_scan                    # noqa: E402

WL = np.arange(300.0, 701.0, 1.0)


def _spectrum(amp379=1.0, amp571=0.5, base=0.02):
    """构造一条像样的吸收谱（379 与 571 两个峰 + 谷底）。"""
    a = (base
         + amp379 * np.exp(-0.5 * ((WL - 379.0) / 9.0) ** 2)
         + amp571 * np.exp(-0.5 * ((WL - 571.0) / 26.0) ** 2))
    return a


# ═══════════════════════════ 配置与风格切换 ═══════════════════════════
def test_jcfg_fills_defaults_and_overrides(cfg) -> None:
    """``jcfg`` 用默认值补齐缺项，并让用户配置覆盖默认值。"""
    j = J.jcfg(cfg)
    assert j["width_in"] == 3.25 and j["dpi"] == 600
    assert j["formats"] == ["png"]           # V1.2 起不再输出 PDF
    cfg2 = {"plot": {"jacs": {"width_in": 7.0}}}
    j2 = J.jcfg(cfg2)
    assert j2["width_in"] == 7.0 and j2["dpi"] == 600      # 未覆盖项仍取默认
    assert J.jcfg(None)["width_in"] == 3.25                # 空 cfg 不崩


def test_plot_style_reads_and_falls_back(cfg) -> None:
    """``plot_style`` 认 jacs/legacy，非法值回退 jacs（默认即正确）。"""
    assert plot_style(default_config()) == "jacs"
    cfg["plot"]["style"] = "legacy"
    assert plot_style(cfg) == "legacy"
    cfg["plot"]["style"] = "LEGACY"                        # 大小写不敏感
    assert plot_style(cfg) == "legacy"
    cfg["plot"]["style"] = "no-such-style"
    assert plot_style(cfg) == "jacs"
    assert plot_style(None) == "jacs"


def test_apply_plot_style_sets_matching_rcparams(cfg) -> None:
    """``apply_plot_style`` 按 cfg 切换 rcParams，并返回生效风格名。"""
    import matplotlib
    cfg["plot"]["style"] = "jacs"
    assert apply_plot_style(cfg) == "jacs"
    assert matplotlib.rcParams["xtick.direction"] == "in"
    assert matplotlib.rcParams["axes.spines.top"] is True
    assert float(matplotlib.rcParams["xtick.labelsize"]) == 7.0
    assert int(matplotlib.rcParams["savefig.dpi"]) == 600

    cfg["plot"]["style"] = "legacy"
    assert apply_plot_style(cfg) == "legacy"
    assert matplotlib.rcParams["xtick.direction"] == "out"
    assert matplotlib.rcParams["axes.spines.top"] is False
    apply_jacs_style()                                     # 复位，免影响后续用例
    assert matplotlib.rcParams["xtick.direction"] == "in"


def test_jacs_rc_satisfies_journal_numbers() -> None:
    """JACS_RC 的数值全部落在设计文档给定的区间内。"""
    assert 0.8 <= JACS_RC["axes.linewidth"] <= 1.0
    assert 1.2 <= JACS_RC["lines.linewidth"] <= 1.5
    assert 7.0 <= JACS_RC["axes.labelsize"] <= 9.0
    assert JACS_RC["xtick.labelsize"] == 7.0
    assert JACS_RC["ytick.labelsize"] == 7.0
    assert JACS_RC["xtick.direction"] == "in"
    assert JACS_RC["ytick.direction"] == "in"
    assert JACS_RC["axes.spines.top"] and JACS_RC["axes.spines.right"]
    assert JACS_RC["savefig.dpi"] >= 600
    assert JACS_RC["pdf.fonttype"] == 42                   # PDF 文字可编辑
    # legacy 风格必须保持"仅左下两条轴 + 朝外刻度"，否则 A4.2 回归失真
    assert LEGACY_RC["axes.spines.top"] is False
    assert LEGACY_RC["xtick.direction"] == "out"


def test_ensure_font_returns_sans_and_avoids_cjk(cfg) -> None:
    """``ensure_font`` 选 Arial（或拉丁回退），**不得**退回中文字体。"""
    picked = J.ensure_font(cfg)
    assert picked
    assert picked not in ("Microsoft YaHei", "SimHei")
    import matplotlib
    assert matplotlib.rcParams["font.sans-serif"][0] == picked


# ═══════════════════════════ 画布尺寸与轴样式 ═══════════════════════════
def test_saved_png_is_exactly_single_column_width(cfg, tmp_path) -> None:
    """600 dpi 下 PNG 宽度必须恰为 82.5 mm（1950 px）——不能用 tight 裁剪。"""
    j = J.jcfg(cfg)
    fig, (ax,) = J.new_figure(cfg, height_in=2.5)
    ax.plot(WL, _spectrum(), lw=float(j["curve_lw"]))
    J.finish_axes(ax, cfg, xlabel="Wavelength / nm", ylabel="Absorbance",
                  xlim=j["xlim"], ylim=(0.0, 1.2), xstep=100.0)
    made = J.save_jacs(fig, tmp_path, "t", cfg)
    assert {p.suffix for p in made} == {".png"}       # V1.2 起不再输出 PDF
    im = Image.open(tmp_path / "t.png")
    assert im.size == (round(3.25 * 600), round(2.5 * 600))
    assert (tmp_path / "t.png").stat().st_size > 0


def test_finish_axes_sets_box_ticks_in_and_plain_formatter(cfg) -> None:
    """四边边框可见、刻度朝内（含 top/right）、刻度 7 pt、x 轴无科学计数法偏移。"""
    import matplotlib
    apply_jacs_style()
    fig, (ax,) = J.new_figure(cfg)
    ax.plot(WL, _spectrum())
    J.finish_axes(ax, cfg, xlabel="Wavelength / nm", ylabel="Absorbance",
                  xlim=(300.0, 700.0), ylim=(0.0, 1.2), xstep=100.0)
    assert all(s.get_visible() for s in ax.spines.values())
    for sp in ax.spines.values():
        assert abs(sp.get_linewidth() - J.jcfg(cfg)["spine_width"]) < 1e-9
    # direction='in' → 下轴 tick1line 的 marker 为 TICKUP(2)；'out' 则为 TICKDOWN(3)
    assert ax.xaxis.majorTicks[0].tick1line.get_marker() == matplotlib.markers.TICKUP
    assert ax.yaxis.majorTicks[0].tick1line.get_marker() == matplotlib.markers.TICKRIGHT
    assert float(ax.xaxis.get_ticklabels()[0].get_fontsize()) == 7.0
    assert float(ax.xaxis.label.get_fontsize()) == J.jcfg(cfg)["label_size"]
    # x 轴在 300–700 视窗内的主刻度恰为 100 的整数倍（MultipleLocator 会额外
    # 生成视窗外的 tick 值，但它们不会被绘制，故按视窗过滤后再断言）
    assert [float(t) for t in ax.get_xticks() if 300.0 <= float(t) <= 700.0] == \
        [300.0, 400.0, 500.0, 600.0, 700.0]
    assert tuple(ax.get_xlim()) == (300.0, 700.0)          # 范围 300–700
    assert not ax.xaxis.get_offset_text().get_visible()
    assert ax.xaxis.get_major_formatter().get_useOffset() is False
    assert ax.get_title() == ""                            # 图内无标题
    import matplotlib.pyplot as plt
    plt.close(fig)


# ═══════════════════════════ 图 a：标注与趋势箭头 ═══════════════════════════
def _scans_with_series(probe_down=True, n=6):
    """构造一组时间序列 Scan；``probe_down`` 决定探针吸收随时间升还是降。"""
    scans = []
    for i in range(n):
        f = 1.0 - 0.06 * i if probe_down else 1.0 + 0.06 * i
        scans.append(make_scan(f"g-{i}.txt", WL,
                               _spectrum(amp379=1.0 * f, amp571=0.5 * f),
                               group="g", t=float(10 * i)))
    return scans


def test_fig_a_has_no_colorbar_title_or_rotated_text(cfg, tmp_path) -> None:
    """图 a：无 colorbar、无图内标题、无竖排文字、无 532 竖线，且四边完整。"""
    import matplotlib.pyplot as plt
    apply_jacs_style()
    caps: list = []
    made = J.fig_a_overlay(_scans_with_series(), cfg, tmp_path, caps)
    assert made and {p.suffix for p in made} == {".png"}

    # 重新画一次以便在 close 之前检查结构
    fig, (ax,) = J.new_figure(cfg)
    ax.plot(WL, _spectrum())
    assert len(getattr(fig, "_colorbars", [])) == 0
    assert ax.get_title() == ""
    plt.close(fig)
    assert caps and "t = 0–50 s" in caps[0][1]


def test_fig_a_marks_379_with_gray_thin_dashed_line(cfg, tmp_path) -> None:
    """379 nm 必须是灰色细虚线（ls='--', lw=0.8, color='gray'），且标注全水平。"""
    apply_jacs_style()
    caps: list = []
    J.fig_a_overlay(_scans_with_series(), cfg, tmp_path, caps)
    # 用 spy 抓住画布检查（fig_a_overlay 内部会 close，故重画一次走同样代码路径）
    caught = {}
    orig = J.save_jacs

    def spy(fig, out_dir, name, cfg=None, formats=None, dpi=None):   # noqa: A002
        caught["ax"] = fig.axes[0]
        caught["fig"] = fig
        return orig(fig, out_dir, name, cfg, formats, dpi)
    J.save_jacs = spy
    try:
        J.fig_a_overlay(_scans_with_series(), cfg, tmp_path, [])
    finally:
        J.save_jacs = orig

    ax = caught["ax"]
    vl = [ln for ln in ax.get_lines() if ln.get_xdata() is not None
          and len(ln.get_xdata()) == 2
          and float(ln.get_xdata()[0]) == float(ln.get_xdata()[1]) == 379.0]
    assert vl, "未找到 379 nm 竖线"
    ln = vl[0]
    assert ln.get_color() == "gray"
    assert ln.get_linestyle() == "--"
    assert abs(ln.get_linewidth() - 0.8) < 1e-9
    # 无粉色阴影带（axvspan → 没有 PolyCollection 从 377 铺到 381）
    assert not any(getattr(c, "get_paths", None) and
                   getattr(c, "get_facecolor", None) and
                   len(c.get_paths()) and c.get_paths()[0].vertices.size > 8
                   and c.get_facecolor()[0][3] > 0.02
                   for c in ax.collections)
    # 所有文字水平；存在"379"与染料名两个水平短标注
    texts = [t for t in ax.texts]
    assert texts
    assert all(abs(float(t.get_rotation())) < 1e-9 for t in texts)
    joined = " ".join(t.get_text() for t in texts)
    assert "379" in joined
    # V1.1 §9 R1：染料标注不再硬编码 "Resorufin"，而是 `fluorophore_label`
    # 按 auto（组名首字段）解析；本 fixture 的组名是 "g"，因此标注就是 "g"。
    assert "g" in joined
    assert "Resorufin" not in joined, "代码里不应再出现硬编码染料名（泛用性红线）"
    assert "532" not in joined                             # 532 默认不画（进图注）
    assert "t" in joined                                   # 趋势箭头标签


def test_trend_sign_follows_probe_not_wideband_mean() -> None:
    """回归保护：趋势必须按**探针**吸光度判定。

    构造"探针下降、但宽带平均上升"的数据（正是 t=0 截断文件造成的假象），
    ``_trend_sign`` 必须返回 -1（下降），否则箭头会指反。
    """
    wl = np.arange(300.0, 701.0, 1.0)
    entries = []
    for i in range(5):
        a = _spectrum(amp379=1.0 - 0.08 * i, amp571=0.5)
        a = a + (0.0 if i == 0 else 0.02 * i) * (wl > 400)   # 宽带被抬高
        entries.append((float(10 * i), wl, a))
    assert J._trend_sign(entries, probe=379.0, window=2.0) == -1

    rising = [(float(10 * i), wl, _spectrum(amp379=1.0 + 0.08 * i))
              for i in range(5)]
    assert J._trend_sign(rising, probe=379.0, window=2.0) == 1
    # 探针全 NaN（如本次的 t=0 截断文件）不能崩，也不能颠倒结论
    nan_entries = [(0.0, wl, np.full_like(wl, np.nan))] + rising
    assert J._trend_sign(nan_entries, probe=379.0, window=2.0) == 1


def test_fig_a_respects_mark_excitation_switch(cfg, tmp_path) -> None:
    """``mark_excitation=False``（默认）不画 532；置 True 时画灰点划线 + "532 ex."。"""
    apply_jacs_style()
    caught = {}
    orig = J.save_jacs

    def spy(fig, out_dir, name, cfg=None, formats=None, dpi=None):   # noqa: A002
        caught["ax"] = fig.axes[0]
        return orig(fig, out_dir, name, cfg, formats, dpi)

    def render(c):
        caught.clear()
        J.save_jacs = spy
        try:
            J.fig_a_overlay(_scans_with_series(), c, tmp_path, [])
        finally:
            J.save_jacs = orig
        return caught["ax"]

    ax = render(default_config())
    assert not any(t.get_text().startswith("532") for t in ax.texts)

    cfg2 = default_config()
    cfg2["plot"]["jacs"]["mark_excitation"] = True
    ax2 = render(cfg2)
    assert any("532 ex." in t.get_text() for t in ax2.texts)
    assert any(abs(float(t.get_rotation())) < 1e-9 for t in ax2.texts)


# ═══════════════════════════ 堆叠图与图注 ═══════════════════════════
def test_stack_figure_has_arrow_instead_of_colorbar(cfg, tmp_path) -> None:
    """堆叠图：无 colorbar、有向上的趋势箭头、379 水平标注、无竖排文字。"""
    apply_jacs_style()
    entries = [(float(10 * i), WL, _spectrum(amp379=1.0 - 0.05 * i))
               for i in range(5)]
    caps: list = []
    made = J.jacs_stack_figure(entries, 379.0, tmp_path, cfg=cfg,
                               tag="g", captions=caps)
    assert made and {p.suffix for p in made} == {".png"}
    im = Image.open(tmp_path / "jacs_stack_g.png")
    assert im.size[0] == round(3.25 * 600)
    assert caps and "offset" in caps[0][1] and "379" in caps[0][1]


def test_write_captions_creates_markdown(tmp_path) -> None:
    """``write_captions`` 落盘 Markdown，包含每个图名与其图注。"""
    p = J.write_captions([("fig_x", "第一条图注"), ("fig_y", "第二条图注")], tmp_path)
    assert p is not None and p.name == "captions.md"
    txt = p.read_text(encoding="utf-8")
    assert "fig_x" in txt and "第一条图注" in txt
    assert "fig_y" in txt and "第二条图注" in txt
    assert J.write_captions([], tmp_path) is None          # 空输入不写文件


def test_plot_all_jacs_writes_captions_and_figures(cfg, tmp_path) -> None:
    """总入口：产出 png+pdf、captions.md，且不返回 captions.md 干扰 GUI 预览。"""
    from core.pipeline import run_core
    from core.phi import compute_phi
    apply_jacs_style()
    scans, results = run_core(SAMPLES_UVVIS, cfg)
    phi = compute_phi(results, cfg)
    made = J.plot_all_jacs(scans, results, phi, {}, cfg, tmp_path, want_paths=True)
    names = {p.name for p in made}
    assert "fig_a_spectra_Resorufin-2I-pbs-TEST.png" in names
    assert "fig_c_k_obs_bars.png" in names
    assert not [n for n in names if n.endswith(".pdf")]    # V1.2 起不再出 PDF
    assert "jacs_stack_Resorufin-2I-pbs-TEST.png" in names
    assert not any(n.endswith(".md") for n in names)        # 图注不进 GUI 文件列表
    figdir = tmp_path / "figures"
    assert (figdir / "captions.md").exists()
    assert not list(figdir.glob("*.svg"))                   # JACS 只出 png+pdf
    for p in figdir.glob("*.png"):
        assert Image.open(p).size[0] == round(3.25 * 600)


# ═══════════════════════════ legacy 路径与 A4.2 隔离 ═══════════════════════════
def test_plot_all_legacy_pins_legacy_dpi_and_formats(cfg, tmp_path) -> None:
    """``style='legacy'`` 时把 dpi/formats 钉回 legacy，保证逐像素回归可比。"""
    from core.pipeline import run_core
    apply_jacs_style()                                     # 先故意设成 JACS
    cfg["plot"]["style"] = "legacy"
    cfg["plot"]["dpi"] = 600                               # 用户即便改了也要被钉回 300
    cfg["plot"]["formats"] = ["png", "svg"]
    scans, results = run_core(SAMPLES_UVVIS, cfg)
    from core.plotting import plot_all
    plot_all(scans, results, {}, {}, cfg, tmp_path, want_paths=True)
    figdir = tmp_path / "figures"
    # legacy 路径的 fig_a–e 出 svg+png（cfg 里的 png+svg 被钉回 legacy 值）
    ga = figdir / "fig_a_spectra_Resorufin-2I-pbs-TEST"
    assert ga.with_suffix(".svg").exists() and ga.with_suffix(".png").exists()
    assert not (figdir / "captions.md").exists()           # legacy 无图注
    # V1.2 解冻：legacy 堆叠图沿用同一份 cfg 格式（svg+png），不再硬编码 pdf；
    # PNG 仍按与 legacy 脚本一致的 savefig 参数写出 —— A4.2 像素可比性不受影响
    assert (figdir / "jacs_stack_Resorufin-2I-pbs-TEST.png").exists()
    assert (figdir / "jacs_stack_Resorufin-2I-pbs-TEST.svg").exists()
    assert not list(figdir.glob("*.pdf")), "V1.2 起 legacy 路径也不应产出任何 PDF"
    im = Image.open(ga.with_suffix(".png"))
    assert im.size != (round(3.25 * 600), round(2.5 * 600))      # 不是单栏尺寸

# ══════════════ A4.2（V2.8 迁移）：legacy 绘图代码逐像素回归 ══════════════
@pytest.mark.slow
def test_a4_2_legacy_render_pixel_parity(tmp_path) -> None:
    """A4.2：legacy 对照脚本 vs 当前 core legacy 风格路径 → PNG 逐像素一致、零 PDF。

    V2.8 验收退役时从 `tools/acceptance/checks_a_core._pixel_vs_legacy` 迁移而来；
    判据保持原样：只比 PNG；任一图缺图/尺寸不同/差异像素 ≥1e-6 即失败。
    """
    import contextlib
    import copy
    import inspect
    import io
    import sys
    from pathlib import Path

    import matplotlib
    matplotlib.use("Agg")
    from core.config import default_config
    from core.pipeline import run_phi_pipeline

    root = Path(__file__).resolve().parents[1]
    baseline = root / ".legacy_baseline"
    legacy = root / "legacy"
    if str(legacy) not in sys.path:
        sys.path.insert(0, str(legacy))
    import singlet_o2_phi as L

    # 修正 legacy _fig_b_fit 的 UnboundLocalError（等价于旧验收实现，其余逐字不动）
    buggy = "        xbar, sxx = float(xx.mean()), float(np.sum((xx - xbar) ** 2))"
    fixed = ("        xbar = float(xx.mean())\n"
             "        sxx = float(np.sum((xx - xbar) ** 2))")
    src = inspect.getsource(L._fig_b_fit)
    if buggy in src:
        ns = dict(L.__dict__)
        exec(compile(src.replace(buggy, fixed), "<legacy_fig_b>", "exec"), ns)
        L._fig_b_fit = ns["_fig_b_fit"]

    lg_dir = tmp_path / "fig_legacy"
    lg_dir.mkdir(parents=True, exist_ok=True)
    cfg_legacy = copy.deepcopy(L.DEFAULT_CFG)
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        scans, results = L.run_core(baseline / "data_2i", cfg_legacy)
        phi = L.compute_phi(results, cfg_legacy)
        ctr = L.judge_controls(results, cfg_legacy)
        L.plot_all(scans, results, phi, ctr, cfg_legacy, lg_dir)
    # legacy 对照实现按 V1.0 政策会写 PDF；逐像素只需 PNG，删掉防 A4.5 误伤
    for pdf in sorted(lg_dir.rglob("*.pdf")):
        pdf.unlink()

    cfg_mine = default_config()
    cfg_mine["plot"]["style"] = "legacy"
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        rr = run_phi_pipeline(baseline / "data_2i", cfg_mine,
                              out_root=tmp_path / "mine", write=True)
    assert rr.ok and rr.run_dir, "当前 core legacy 路径未完成"
    mine_dir = Path(rr.run_dir)
    lf, mf = lg_dir / "figures", mine_dir / "figures"
    assert lf.is_dir() and mf.is_dir()

    names = sorted({p.name for p in lf.glob("*.png")} |
                   {p.name for p in mf.glob("*.png")})
    assert names, "两边都没有 PNG"
    rows = []
    worst = 0
    min_frac = 0.0
    for name in names:
        pa, pb = lf / name, mf / name
        assert pa.exists(), f"legacy 缺图：{name}"
        assert pb.exists(), f"当前 core 缺图：{name}"
        ia = np.asarray(Image.open(pa).convert("RGB"), dtype=np.int16)
        ib = np.asarray(Image.open(pb).convert("RGB"), dtype=np.int16)
        assert ia.shape == ib.shape, f"{name}: 尺寸不同 {ia.shape} vs {ib.shape}"
        d = np.abs(ia - ib)
        frac = float((d.max(axis=2) > 12).mean())
        worst = max(worst, int(d.max()))
        min_frac = max(min_frac, frac)
        rows.append(f"{name}: max差={int(d.max())}, 差异像素={frac:.4%}")
        assert frac < 1e-6, f"{name}: 差异像素比例 {frac:.4%} 超限; " + "; ".join(rows)
    assert not list(mf.rglob("*.pdf")), "legacy 路径不应产出 PDF"
    assert worst == 0, f"存在非零像素差（最大 {worst}）: " + "; ".join(rows)


def test_jacs_fig_cd_styles_units_and_transparent(tmp_path):
    from types import SimpleNamespace
    from core.render import plotting_jacs as pj
    fit = SimpleNamespace(status='OK', k=1.2e-3, k_err=2e-5, r2=0.99,
                          n=12, A0=1.0, c=0.0, truncated=False)
    results = {'A': {'pooled': fit}}
    phi = {'A': {'status': 'OK', 'phi': 0.42, 'phi_err': 0.03}}
    cfg = default_config()
    pj.set_figure_session(style=None, error_bars=True, annotate=False,
                          transparent=False, time_unit='s')
    caps: list = []
    made = pj.fig_c_bars(results, cfg, tmp_path, caps)
    assert made and made[0].name == 'fig_c_k_obs_bars.png'
    low = made[0].read_bytes()
    made_d = pj.fig_d_phi(phi, cfg, tmp_path, caps)
    assert made_d and made_d[0].name == 'fig_d_phi_forest.png'
    pj.set_figure_session(style='line', error_bars=False, annotate=True,
                          transparent=True, time_unit='min')
    assert pj.fig_c_bars(results, cfg, tmp_path, caps)[0].is_file()
    assert pj.fig_d_phi(phi, cfg, tmp_path, caps)[0].is_file()
    assert pj.fig_c_bars(results, cfg, tmp_path, caps)[0].read_bytes() != low
    pj.set_figure_session(style=None, error_bars=True, annotate=False,
                          transparent=False, time_unit='s')


def test_fig_e_units_truncated_and_dose(tmp_path):
    from types import SimpleNamespace
    from core.render import plotting_jacs as pj
    t = np.array([0.0, 60.0, 120.0, 180.0])
    f = SimpleNamespace(status='OK', x=np.log(t + 1.0), lnR=np.log(np.exp(-0.002 * t)),
                        truncated=True, intercept=0.0, k=0.002, r2=0.99, n=4)
    e = {'pooled': f, 'reps': {'1': {'R': np.exp(-0.002 * t), 't': t}}}
    cfg = default_config()
    cfg['kinetics']['dose_normalize'] = False
    for unit in ('s', 'min', 'h'):
        pj.set_figure_session(time_unit=unit)
        caps: list = []
        made = pj.fig_e_residuals({'A': e}, cfg, tmp_path / unit, caps)
        assert made and made[0].is_file()
    cfg['kinetics']['dose_normalize'] = True
    pj.set_figure_session(time_unit='min')
    assert pj.fig_e_residuals({'A': e}, cfg, tmp_path / 'dose', [])[0].is_file()
    pj.set_figure_session(time_unit='s')
