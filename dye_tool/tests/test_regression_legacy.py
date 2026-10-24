# -*- coding: utf-8 -*-
"""A2 回归测试：新 core 流水线 vs legacy 黄金基线（``.legacy_baseline/baseline_*.json``）。

判据（设计文档验收 A2）：
    * 每组 k 相对偏差 < 0.1%；
    * R² 绝对偏差 < 1e-6；
    * k_stderr、n、自动截断标志、A0_pooled、status、pre_k/pre_r2 一致；
    * 逐文件的 group/role/t/剔除原因与 legacy 完全一致。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from conftest import BASELINE_DIR, LEGACY_CASES
from core.config import default_config
from core.phi import compute_phi
from core.pipeline import run_core
from core.results import setup_logging


def _load(tag: str, sub: str, base: str) -> tuple[dict, list, dict, dict]:
    """跑一次新 core，返回 (legacy 基线, 新 scans, 新 results, 新 phi)。"""
    b = json.loads((BASELINE_DIR / base).read_text(encoding="utf-8"))
    cap = setup_logging(None, debug=False)
    try:
        cfg = default_config()
        scans, results = run_core(BASELINE_DIR / sub, cfg)
        phi = compute_phi(results, cfg)
    finally:
        cap.detach()
    return b, scans, results, phi


@pytest.mark.slow
@pytest.mark.parametrize("tag,sub,base", LEGACY_CASES, ids=[c[0] for c in LEGACY_CASES])
def test_a2_legacy_regression(tag, sub, base) -> None:
    """A2：逐文件元数据 + 逐组拟合数值与 legacy 基线逐项比对。"""
    b, scans, results, phi = _load(tag, sub, base)

    # ── 1. 文件级元数据：顺序、组名、角色、时间、剔除原因 ──
    assert len(scans) == len(b["files"]), f"[{tag}] 文件数不一致"
    for lf, ns in zip(b["files"], scans):
        assert ns.path.name == lf["file"], f"[{tag}] 文件顺序/名称不一致"
        assert ns.group == lf["group"], f"[{tag}] {lf['file']} 组名"
        assert ns.rep == lf["rep"], f"[{tag}] {lf['file']} 重复号"
        assert ns.role == lf["role"], f"[{tag}] {lf['file']} 角色"
        assert round(float(ns.t), 6) == round(float(lf["t_s"]), 6), f"[{tag}] {lf['file']} t"
        assert (ns.excluded or "") == (lf["excluded"] or ""), \
            f"[{tag}] {lf['file']} 剔除原因: {ns.excluded!r} != {lf['excluded']!r}"

    # ── 2. 组级拟合数值 ──
    assert set(results) == set(b["groups"]), f"[{tag}] 组集合不一致"
    max_k_rel = 0.0
    max_kerr_rel = 0.0
    max_r2_abs = 0.0
    max_a0_abs = 0.0
    for g, gd in sorted(b["groups"].items()):
        ne, lf = results[g], gd["fit"]
        assert lf is not None, f"[{tag}] 基线缺少 {g} 的拟合结果"
        nf = ne["pooled"]
        assert nf.status == lf["status"], f"[{tag}] {g} status"

        dk = abs(nf.k - lf["k"]) / abs(lf["k"])
        max_k_rel = max(max_k_rel, dk)
        assert dk < 1e-3, f"[{tag}] {g} k 相对偏差 {dk:.3e} 超 0.1%"

        dkerr = abs(nf.k_err - lf["k_err"]) / abs(lf["k_err"])
        max_kerr_rel = max(max_kerr_rel, dkerr)
        assert dkerr < 1e-6, f"[{tag}] {g} k_stderr 相对偏差 {dkerr:.3e}"

        dr2 = abs(nf.r2 - lf["r2"])
        max_r2_abs = max(max_r2_abs, dr2)
        assert dr2 < 1e-6, f"[{tag}] {g} R² 绝对偏差 {dr2:.3e}"

        assert nf.n == lf["n"], f"[{tag}] {g} 拟合点数 n={nf.n} != {lf['n']}"
        assert bool(nf.truncated) == bool(lf["truncated"]), f"[{tag}] {g} 自动截断标志"
        if lf["truncated"]:
            assert nf.pre_k == pytest.approx(lf["pre_k"], rel=1e-9), f"[{tag}] {g} pre_k"
            assert nf.pre_r2 == pytest.approx(lf["pre_r2"], abs=1e-12), f"[{tag}] {g} pre_r2"
            assert nf.message == lf["message"], f"[{tag}] {g} message"
        assert nf.intercept == pytest.approx(lf["intercept"], rel=1e-9), f"[{tag}] {g} 截距"

        da0 = abs(ne["A0_pooled"] - gd["A0_pooled"])
        max_a0_abs = max(max_a0_abs, da0)
        assert da0 < 1e-9, f"[{tag}] {g} A0_pooled 偏差 {da0:.3e}"
        assert ne["role"] == gd["role"], f"[{tag}] {g} 角色"
        if np.isfinite(gd["a_ex_auto"]):
            assert ne["a_ex_auto"] == pytest.approx(gd["a_ex_auto"], abs=1e-12), \
                f"[{tag}] {g} a_ex_auto"

        # 逐重复序列（t/A/R/A0）也要一致
        assert set(ne["reps"]) == set(gd["reps"]), f"[{tag}] {g} 重复号集合"
        for rep, lr in gd["reps"].items():
            nr = ne["reps"][rep]
            assert np.allclose(nr["t"], [v for v in lr["t"]], equal_nan=True)
            assert np.allclose(nr["A"], [np.nan if v is None else v for v in lr["A"]],
                               equal_nan=True, rtol=0, atol=1e-12)
            assert np.allclose(nr["R"], [np.nan if v is None else v for v in lr["R"]],
                               equal_nan=True, rtol=0, atol=1e-12)
            assert nr["A0"] == pytest.approx(lr["A0"], rel=1e-12)

    # ── 3. Φ 状态一致（本批无参比 → NO_REF）──
    assert set(phi) == set(b["phi"]), f"[{tag}] Φ 组集合"
    for g, lp in b["phi"].items():
        assert phi[g]["status"] == lp["status"], f"[{tag}] {g} Φ 状态"

    print(f"\n[A2 {tag}] 文件 {len(scans)} 个 / 组 {len(results)} 个；"
          f"max k 相对偏差 = {max_k_rel:.3e} ({max_k_rel * 100:.3e}%)；"
          f"max k_stderr 相对偏差 = {max_kerr_rel:.3e}；"
          f"max R² 绝对偏差 = {max_r2_abs:.3e}；max A0 绝对偏差 = {max_a0_abs:.3e}")


@pytest.mark.slow
def test_a2_both_cases_share_the_iodide_group_numbers() -> None:
    """交叉校验：2i 与 all 两个数据集里同一组的 k/R² 完全一致（同一批文件）。"""
    out = {}
    for tag, sub, base in LEGACY_CASES:
        _, scans, results, _ = _load(tag, sub, base)
        out[tag] = (results["Resorufin-2I-pbs-TEST"]["pooled"],
                    [s.path.name for s in scans])
    f2i, n2i = out["2i"]
    fall, nall = out["all"]
    assert f2i.k == fall.k and f2i.r2 == fall.r2 and f2i.n == fall.n
    assert n2i == [n for n in nall if n.startswith("Resorufin-2I-pbs-TEST")]


@pytest.mark.slow
def test_a2_matches_shipped_check_regression_script() -> None:
    """与仓库自带 ``.legacy_baseline/check_regression.py`` 的判据同源（脚本可直接跑通）。"""
    import runpy
    import sys
    from pathlib import Path
    root = str(BASELINE_DIR.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    ns = runpy.run_path(str(BASELINE_DIR / "check_regression.py"), run_name="__not_main__")
    assert callable(ns["check"])
    cap = setup_logging(None, debug=False)
    try:
        assert ns["check"]("2i", "data_2i") is True
        assert ns["check"]("all", "data_all") is True
    finally:
        cap.detach()
    # V2.0 δ FS1 起 pipeline 在 core/analysis/ 子包里；断言"被测的那份代码存在"
    # 时**同时认两条路径**——路径本身不是红线的判据，数值才是（上面两条 check 已验）。
    assert (Path(root, "core", "analysis", "pipeline.py").exists()
            or Path(root, "core", "pipeline.py").exists())
