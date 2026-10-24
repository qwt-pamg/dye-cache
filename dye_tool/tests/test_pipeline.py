# -*- coding: utf-8 -*-
"""``core.pipeline`` 端到端测试：FR-1 全流程（A1）、FR-2 两组对比（A3）、参比组出图（A4）。"""
from __future__ import annotations

import csv
import json
import shutil

import numpy as np
import pytest

from conftest import BASELINE_DIR, SAMPLES_UVVIS
from core.config import autogroup_cfg
from core.grouping import auto_group, items_from_scans
from core.io import collect_scans
from core.pipeline import run_compare, run_core, run_phi_pipeline
from core.results import CSV_HEAD

#: legacy 黄金值（21 文件碘代系列，见 .legacy_baseline/baseline_2i.json）
GOLD_2I = {"k": 9.119423843972e-04, "r2": 0.9944157646251929, "n": 14, "A0": 1.111}


# ═══════════════════════════ A1: 全流程 ═══════════════════════════
@pytest.mark.slow
def test_a1_run_phi_pipeline_on_samples_uvvis(tmp_path, cfg) -> None:
    """A1：samples/uvvis 全流程跑通，产出全部工件，且图 b 确实被画出。"""
    progress: list[tuple[int, str]] = []
    rr = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path,
                          progress_cb=lambda p, s: progress.append((p, s)))

    # —— 流程状态 ——
    assert rr.ok is True
    assert rr.message == ""
    assert set(rr.results) == {"Resorufin-2I-pbs-TEST", "Resorufin-pbs-t"}
    assert len(rr.scans) == 35

    # —— 组拟合（对照 legacy 黄金值）——
    fit = rr.results["Resorufin-2I-pbs-TEST"]["pooled"]
    assert fit.status != "FAILED"
    assert fit.status == "OK"
    assert abs(fit.k - GOLD_2I["k"]) / GOLD_2I["k"] < 1e-3
    assert abs(fit.r2 - GOLD_2I["r2"]) < 1e-6
    assert fit.n == GOLD_2I["n"]
    assert fit.truncated is True
    assert rr.results["Resorufin-2I-pbs-TEST"]["A0_pooled"] == pytest.approx(
        GOLD_2I["A0"], abs=1e-9)
    # Φ：本批无 ref 参比组 → NO_REF（不阻塞流程）
    assert rr.phi["Resorufin-2I-pbs-TEST"]["status"] == "NO_REF"

    # —— 无暗态点告警确实产生 ——
    assert any("无暗态点" in w for w in rr.warnings)
    assert any("最早 2 点均值" in w for w in rr.warnings)

    # —— 文件名时间解析：90+5 → 390 s；90+10 → 690 s ——
    t905 = [s.t for s in rr.scans if s.path.name == "Resorufin-pbs-t-90+5.txt"]
    assert t905 == [390.0]
    t9010 = [s.t for s in rr.scans if s.path.name == "Resorufin-pbs-t-90+10.txt"]
    assert t9010 == [690.0]
    assert max(s.t for s in rr.scans) == 690.0
    assert min(s.t for s in rr.scans) == 0.0

    # —— 进度回调完整（⚠️ 百分比不单调，见下方 xfail 用例）——
    assert progress and max(p for p, _ in progress) == 100
    stages = {stage for _, stage in progress}
    assert {"初始化", "扫描目录", "计算 Φ_Δ", "写报告", "完成"} <= stages
    assert any(s.startswith("绘制图") for s in stages)

    # —— run 目录工件 ——
    run_dir = rr.run_dir
    assert run_dir is not None and run_dir.is_dir()
    for name in ("results.csv", "fit_summary.json", "report.md", "run.log"):
        f = run_dir / name
        assert f.exists() and f.stat().st_size > 0, name
    figs = run_dir / "figures"
    assert figs.is_dir() and any(figs.iterdir())

    # —— 图集必须包含图 b（legacy 在图 b 崩溃的回归点）——
    names = [p.name for p in rr.figures]
    assert any(n.startswith("fig_b_kinetics_") and n.endswith(".png") for n in names)
    assert any(n.startswith("fig_a_spectra") for n in names)
    assert "fig_c_k_obs_bars.png" in names
    assert "fig_e_residuals.png" in names
    # 无参比组 → 图 d 跳过（只记日志，不报错）
    assert not any(n.startswith("fig_d_phi_forest") for n in names)
    for p in rr.figures:
        assert p.exists() and p.stat().st_size > 0

    # —— results.csv 表头与行数 ——
    rows = list(csv.reader((run_dir / "results.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    assert rows[0] == CSV_HEAD
    assert len(rows) == 1 + len(rr.results)
    body = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert body["Resorufin-2I-pbs-TEST"]["自动截断"] == "是"
    assert body["Resorufin-pbs-t"]["自动截断"] == "否"
    assert body["Resorufin-pbs-t"]["k_obs(s^-1)"].startswith("0.000635")

    # —— fit_summary.json 可被 --plot-only 复用 ——
    j = json.loads((run_dir / "fit_summary.json").read_text(encoding="utf-8"))
    assert len(j["files"]) == 35
    assert j["phi"]["Resorufin-pbs-t"]["status"] == "NO_REF"
    assert set(j["groups"]) == {"Resorufin-2I-pbs-TEST", "Resorufin-pbs-t"}

    # —— report.md 章节与 N/A 说明 ——
    rep = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## 1. 数据概览" in rep and "## 3. 结果" in rep
    assert "N/A（无参比组）" in rep
    assert "fig_b_kinetics_*.png" in rep
    assert "无暗态点" in rep                                # 警告汇总


@pytest.mark.slow
def test_a1_pipeline_respects_only_paths(tmp_path, cfg) -> None:
    """``only_paths`` 只跑勾选的文件（GUI 重跑路径）。"""
    keep = [SAMPLES_UVVIS / "Resorufin-pbs-t-1.txt",
            SAMPLES_UVVIS / "Resorufin-pbs-t-10.txt",
            SAMPLES_UVVIS / "Resorufin-pbs-t-20.txt",
            SAMPLES_UVVIS / "Resorufin-pbs-t-30.txt",
            SAMPLES_UVVIS / "Resorufin-pbs-t-40.txt"]
    rr = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path, only_paths=keep)
    assert len(rr.scans) == 5
    assert set(rr.results) == {"Resorufin-pbs-t"}
    assert rr.run_dir is not None and (rr.run_dir / "results.csv").exists()


# ═══════════════════════════ A3: 两组对比（FR-2） ═══════════════════════════
@pytest.mark.slow
def test_a3_auto_group_and_run_compare(tmp_path, cfg) -> None:
    """A3：自动分组得 2 组（对应两个文件名族）→ 对比图/CSV 齐全，比值 = k_a/k_b。"""
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, bad = items_from_scans(scans)
    assert bad == 0 and len(items) == 35

    groups, invalid = auto_group(items, autogroup_cfg(cfg))
    assert invalid == []
    assert len(groups) == 2
    fams = set()
    for g in groups:
        names = [m["name"] for m in g["members"]]
        if all(n.startswith("Resorufin-2I-pbs-TEST") for n in names):
            fams.add("2I")
        elif all(n.startswith("Resorufin-pbs-t") for n in names):
            fams.add("t")
    assert fams == {"2I", "t"}                             # 两组 = 两个文件名族
    assert sorted(len(g["members"]) for g in groups) == [14, 21]

    out_dir = tmp_path / "compare"
    cr = run_compare(items, groups[0], groups[1], cfg, out_dir=out_dir)
    assert cr.ok is True and cr.message == ""
    assert cr.out_dir == out_dir

    names = sorted(p.name for p in out_dir.iterdir())
    assert "compare_379nm.png" in names
    assert "compare_summary.csv" in names
    assert len([n for n in names if n.startswith("jacs_stack_") and n.endswith(".png")]) == 2
    assert len([n for n in names if n.startswith("jacs_stack_") and n.endswith(".png")]) == 2
    for n in names:
        assert (out_dir / n).stat().st_size > 0

    # 比值 = k_a / k_b（且与两组拟合的 k 一致）
    assert cr.ratio == pytest.approx(cr.fit_a["k"] / cr.fit_b["k"], rel=1e-12)
    assert cr.fit_a["ok"] and cr.fit_b["ok"]
    assert cr.fit_a["model"] in ("exp2", "exp3")

    # compare_summary.csv：两行数据 + k_ratio 列自洽
    rows = list(csv.reader((out_dir / "compare_summary.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    assert rows[0][0] == "label" and "k_ratio" in rows[0]
    # V2.9.1 FR-62：行尾新增 Φ 列，旧列顺序/内容不变
    assert rows[0][-3:] == ["Phi_delta", "Phi_err", "ref_side"]
    assert rows[0][1] == "core_lambda_max_nm"
    assert len(rows) == 3
    body = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert float(body[rows[1][0]]["k_ratio"]) == pytest.approx(cr.ratio, rel=1e-5)
    for r in rows[1:]:
        assert float(dict(zip(rows[0], r))["k_obs_s^-1"]) > 0
        assert dict(zip(rows[0], r))["core_lambda_max_nm"] == "570.0"


@pytest.mark.slow
def test_a3_run_compare_requires_enough_points(tmp_path, cfg) -> None:
    """任一组可用点 <4 → ok=False 且给出说明（不抛异常）。"""
    from core.grouping import auto_group as ag
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, _ = items_from_scans(scans)
    tiny = [it for it in items if it["name"] in
            ("Resorufin-pbs-t-1.txt", "Resorufin-pbs-t-10.txt")]
    groups, _ = ag(items, autogroup_cfg(cfg))
    res = run_compare(items, groups[0], {"members": tiny, "lam_max": 570.0,
                                         "label": "tiny"}, cfg,
                      out_dir=tmp_path / "cmp_fail")
    assert res.ok is False
    assert "无法对比" in res.message


# ═══════════════════════════ A4: 有参比组 → 图 a–e 齐全 ═══════════════════════════
@pytest.mark.slow
def test_a4_pipeline_with_reference_group_produces_figures_a_to_e(tmp_path, cfg) -> None:
    """A4：把 2I 族改名成 ``ref-*`` 造出参比组 → Φ 可算，图 a–e 全部产出。"""
    dst = tmp_path / "data"
    dst.mkdir()
    for p in sorted(SAMPLES_UVVIS.iterdir()):
        if p.name.startswith("Resorufin-2I-pbs-TEST"):
            suffix = p.stem.replace("Resorufin-2I-pbs-TEST", "")
            shutil.copy2(p, dst / f"ref{suffix}.txt")
        else:
            shutil.copy2(p, dst / p.name)

    rr = run_phi_pipeline(dst, cfg, out_root=tmp_path / "out")
    assert rr.ok is True
    assert set(rr.results) == {"ref", "Resorufin-pbs-t"}
    assert rr.results["ref"]["role"] == "reference"
    assert rr.results["ref"]["pooled"].status != "FAILED"

    phi = rr.phi["Resorufin-pbs-t"]
    assert phi["status"] == "OK"
    assert np.isfinite(phi["phi"]) and phi["phi"] > 0
    assert np.isfinite(phi["phi_err"]) and phi["phi_err"] > 0
    assert phi["pairs"] and phi["pairs"][0]["ref"] == "ref"
    # 参比组自身不参与 Φ 输出
    assert "ref" not in rr.phi

    names = [p.name for p in rr.figures]
    for tag in ("fig_a_spectra", "fig_b_kinetics_", "fig_c_k_obs_bars",
                "fig_d_phi_forest", "fig_e_residuals"):
        assert any(n.startswith(tag) and n.endswith(".png") for n in names), tag
    # JACS 堆叠图（每组一张）
    assert len([n for n in names if n.startswith("jacs_stack_") and n.endswith(".png")]) == 2

    rows = list(csv.reader((rr.run_dir / "results.csv").read_text(
        encoding="utf-8-sig").splitlines()))
    body = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert body["Resorufin-pbs-t"]["Phi_Delta"] != "N/A"
    assert float(body["Resorufin-pbs-t"]["Phi_Delta"]) == pytest.approx(
        phi["phi"], abs=1e-4)
    rep = (rr.run_dir / "report.md").read_text(encoding="utf-8")
    assert "| 组别 | 判定 | 说明 |" not in rep               # 无对照文件


# ═══════════════════════════ run_core 边界 ═══════════════════════════
def test_run_core_dry_stops_before_fitting(tmp_path, cfg) -> None:
    """``dry=True``：只解析+QC，不做背景扣除与拟合。"""
    scans, results = run_core(BASELINE_DIR / "data_2i", cfg, dry=True)
    assert len(scans) == 21
    assert results == {}
    assert all(s.a is not None for s in scans if not s.excluded)


def test_progress_percentages_should_be_monotonic(cfg) -> None:
    """进度回调的百分比应当单调不减（GUI 进度条契约）。

    回归保护：``run_core`` 曾把外层 ``progress_cb`` 原样传给 ``collect_scans``，
    而后者按"已读文件数/总数"输出 0–100 的**绝对**百分比 → 进度在扫描阶段冲到
    100% 后又回落到 40%（读取之后的 QC/拟合阶段）。现已在 ``run_core`` 中把扫描
    阶段压缩映射到 2–35% 区间。
    """
    pcts: list[int] = []
    run_core(SAMPLES_UVVIS, cfg, dry=True,
             progress_cb=lambda p, s: pcts.append(p))
    assert pcts, "未收到任何进度回调"
    assert pcts == sorted(pcts), f"进度百分比回退: {pcts}"
    assert pcts[0] >= 0 and pcts[-1] <= 100
    assert max(pcts) <= 100


def test_run_core_missing_dir_and_empty_dir(tmp_path, cfg) -> None:
    """目录不存在 / 空目录 → ([], {}) 且不抛异常。"""
    assert run_core(tmp_path / "nope", cfg) == ([], {})
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_core(empty, cfg) == ([], {})


def test_run_core_all_unreadable_returns_empty_results(tmp_path, cfg) -> None:
    """目录里全是坏文件 → scans 均带 excluded，results 为空。"""
    (tmp_path / "a-1.txt").write_text("垃圾\n", encoding="utf-8")
    scans, results = run_core(tmp_path, cfg)
    assert len(scans) == 1 and scans[0].excluded
    assert results == {}




# ═══════════════ V2.9.1 FR-61：显式参比组（不依赖 ref- 前缀） ═══════════════
@pytest.mark.slow
def test_fr61_explicit_reference_group_without_prefix(tmp_path, cfg) -> None:
    """无 ref- 前缀时，UI 指定参比组 → 样品组 Φ_Δ 出值；不指定时 NO_REF。"""
    base = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path / 'base', write=False)
    assert base.phi['Resorufin-pbs-t']['status'] == 'NO_REF'
    rr = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path / 'explicit',
                          write=False,
                          reference_group='Resorufin-2I-pbs-TEST')
    assert rr.results['Resorufin-2I-pbs-TEST']['role'] == 'reference'
    phi = rr.phi['Resorufin-pbs-t']
    assert phi['status'] == 'OK' and np.isfinite(phi['phi']) and phi['phi'] > 0


# ═══════════════ V2.9.1 FR-62：对比链直通 Φ_Δ + 行尾列 ═══════════════
@pytest.mark.slow
def test_fr62_compare_phi_uses_calc_phi_and_ratio(tmp_path, cfg) -> None:
    """Φ 列 == calc_phi 期望值；A 参比时 Φ_B == Φ_ref / ratio_corrected。"""
    from core.analysis.kinetics import excitation_abs
    from core.analysis.phi import calc_phi
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, _ = items_from_scans(scans)
    groups, _ = auto_group(items, autogroup_cfg(cfg))
    ga, gb = groups[0], groups[1]
    aex_a = excitation_abs(ga, cfg)[0]
    aex_b = excitation_abs(gb, cfg)[0]
    out = tmp_path / 'phi_cols'
    cr = run_compare(items, ga, gb, cfg, normalize_exc=True, out_dir=out,
                     ref_side='A', phi_ref=0.75)
    assert cr.ok and cr.ratio_corrected
    expect_b = calc_phi(cr.fit_b['k'], cr.fit_a['k'], aex_b, aex_a, 0.75)
    assert cr.phi_a == pytest.approx(0.75)
    assert cr.phi_b == pytest.approx(expect_b, rel=1e-12)
    assert cr.phi_b == pytest.approx(0.75 / cr.ratio_corrected, rel=1e-9)
    rows = list(csv.reader((out / 'compare_summary.csv').read_text(
        encoding='utf-8-sig').splitlines()))
    body = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert body[ga['label']]['ref_side'] == 'A'
    assert float(body[gb['label']]['Phi_delta']) == pytest.approx(cr.phi_b, rel=1e-6)
    # B 为参比：Φ_A == Φ_ref × ratio_corrected
    cr2 = run_compare(items, ga, gb, cfg, normalize_exc=True,
                      out_dir=tmp_path / 'phi_cols_b', ref_side='B', phi_ref=0.75)
    assert cr2.phi_b == pytest.approx(0.75)
    assert cr2.phi_a == pytest.approx(0.75 * cr2.ratio_corrected, rel=1e-9)


@pytest.mark.slow


# ═══════════════ V2.9.1 FR-63：Φ_Δ pipeline 读取 batch.phi_ref ═══════════════
@pytest.mark.slow


# ═══════════════ V2.9.3 FR-65：N 组工作台 ═══════════════
@pytest.mark.slow
def test_fr65_workbench_three_groups_phi_and_csv(tmp_path, cfg) -> None:
    """N=3 组：逐样 k_obs 与 fit_series 一致；Φ_Δ == calc_phi；汇总表逐样一行。"""
    from core.analysis.kinetics import excitation_abs, fit_series, group_kinetics
    from core.analysis.phi import calc_phi
    from core.pipeline import run_workbench
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, _ = items_from_scans(scans)
    groups, _ = auto_group(items, autogroup_cfg(cfg))
    g1, g2 = groups[0], groups[1]
    # 把 g2 拆成两组 → N=3
    half = len(g2['members']) // 2
    g2a = {'label': 'split-A', 'lam_max': g2.get('lam_max'),
           'members': g2['members'][:half]}
    g2b = {'label': 'split-B', 'lam_max': g2.get('lam_max'),
           'members': g2['members'][half:]}
    groups3 = [g1, g2a, g2b]
    out = tmp_path / 'wb'
    wr = run_workbench(items, groups3, cfg, ref_label=g1['label'], phi_ref=0.75,
                       out_dir=out)
    assert wr.ok and len(wr.rows) == 3
    for row, g in zip(wr.rows, groups3):
        t, A, _ = group_kinetics(g, cfg)
        f = fit_series(t, A)
        assert row['k_obs'] == pytest.approx(f['k'], rel=1e-12)
        if row['label'] == g1['label']:
            assert row['phi_delta'] == pytest.approx(0.75)
        else:
            a_s = excitation_abs(g, cfg)[0]
            a_r = excitation_abs(g1, cfg)[0]
            expect = calc_phi(f['k'], fit_series(*group_kinetics(g1, cfg)[:2])['k'],
                              a_s, a_r, 0.75)
            assert row['phi_delta'] == pytest.approx(expect, rel=1e-12)
    csv_path = out / 'singlet_oxygen_summary.csv'
    assert csv_path.is_file()
    header = csv_path.read_text(encoding='utf-8-sig').splitlines()[0].split(',')
    assert header[:4] == ['label', 'n', 't_min_s', 't_max_s']
    assert {'k_obs', 'phi_delta', 'ref_label', 'phi_ref'} <= set(header)
    assert (out / 'workbench_kinetics.png').is_file()
    assert (out / 'workbench_phi.png').is_file()


@pytest.mark.slow
def test_fr65_workbench_no_reference_leaves_phi_blank(tmp_path, cfg) -> None:
    """未指定参比 → k 仍拟合，Φ 列空 + WARNING（不抛）。"""
    from core.pipeline import run_workbench
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, _ = items_from_scans(scans)
    groups, _ = auto_group(items, autogroup_cfg(cfg))
    wr = run_workbench(items, groups, cfg, ref_label='', out_dir=tmp_path / 'wb_noref')
    assert wr.ok
    assert all(r['k_obs'] is not None for r in wr.rows)
    assert all(r['phi_delta'] is None for r in wr.rows)
    assert all(r['ref_label'] == '' for r in wr.rows)


def test_fr63_pipeline_reads_batch_phi_ref(tmp_path, cfg) -> None:
    """dye_batch.yaml 的 phi_ref 进入 Φ_Δ 计算（显式参数 > batch > config）。"""
    from core.inputs.batch_file import empty_batch
    b = empty_batch(SAMPLES_UVVIS)
    b.phi_ref = 0.42
    rr = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path / 'batch_phi',
                          write=False, batch=b, reference_group='Resorufin-2I-pbs-TEST')
    phi = rr.phi['Resorufin-pbs-t']
    assert phi['status'] == 'OK'
    assert phi['pairs'][0]['ref'] == 'Resorufin-2I-pbs-TEST'
    # 与显式 phi_ref 覆盖同口径：batch 0.42 参与 formula
    rr2 = run_phi_pipeline(SAMPLES_UVVIS, cfg, out_root=tmp_path / 'explicit_phi',
                           write=False, phi_ref=0.42,
                           reference_group='Resorufin-2I-pbs-TEST')
    assert rr2.phi['Resorufin-pbs-t']['phi'] == pytest.approx(phi['phi'], rel=1e-12)


def test_fr62_compare_phi_degenerate_ref_does_not_raise(tmp_path, cfg) -> None:
    """Φ_ref<=0：走 NaN 口径 + WARNING，不抛异常；旧列仍正常。"""
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    items, _ = items_from_scans(scans)
    groups, _ = auto_group(items, autogroup_cfg(cfg))
    cr = run_compare(items, groups[0], groups[1], cfg, out_dir=tmp_path / 'phi_bad',
                     phi_ref=0.0)
    assert cr.ok is True and cr.ratio is not None
    assert cr.phi_a is None and cr.phi_b is None

def test_run_core_recovers_legacy_golden_k(cfg) -> None:
    """21 文件碘代系列：k/R²/n/A0 与 legacy 基线一致（1e-3 相对 / 1e-6 绝对）。"""
    scans, results = run_core(BASELINE_DIR / "data_2i", cfg)
    assert len(scans) == 21
    g = "Resorufin-2I-pbs-TEST"
    fit = results[g]["pooled"]
    assert abs(fit.k - GOLD_2I["k"]) / GOLD_2I["k"] < 1e-3
    assert abs(fit.r2 - GOLD_2I["r2"]) < 1e-6
    assert fit.n == GOLD_2I["n"]
    assert fit.truncated is True
    assert results[g]["role"] == "sample"
    assert results[g]["a_ex_auto"] == pytest.approx(0.319, abs=1e-9)
