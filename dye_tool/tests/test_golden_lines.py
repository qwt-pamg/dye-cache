# -*- coding: utf-8 -*-
"""V2.8 FR-55.1/FR-55.3：数值金线 + 兼容红线迁移（个人版轻量门禁）。

金线期望值生成：test_temp/_gen_golden.py（一次性；已与 legacy A2 基线交叉核对）。
fast 档必须覆盖：A2（金线与 legacy 基线互证）、A4.5（零 PDF 过滤器）、A8.7（两样例组串行，
由 E2E 金线覆盖）、A10.4（core 无硬编码染料）、A15（夹具只读）；A4.2 像素 parity 在 full 档
由 tests/test_plotting_jacs.py 的既有用例承担（保留不动）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from core.config import default_config

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "data" / "golden"
SAMPLE_UVVIS = ROOT / "samples" / "uvvis"
BASELINE = ROOT / ".legacy_baseline"

pytestmark = pytest.mark.golden


def test_golden_e2e_uvvis() -> None:
    """FR-55.1 端到端金线：samples/uvvis 两组串行（A8.7）→ k/σk/R²/拟合窗/Φ 状态冻结值。"""
    from core.pipeline import run_phi_pipeline

    exp = json.loads((GOLDEN / "golden_uvvis.json").read_text(encoding="utf-8"))
    rr = run_phi_pipeline(SAMPLE_UVVIS, default_config(),
                          out_root=ROOT / "test_temp" / "golden_test", write=False)
    assert rr.ok, "流水线失败"
    assert len(rr.scans) == exp["scans"]
    assert set(rr.results) == set(exp["groups"]), \
        f"组集合变化：{set(rr.results)} != {set(exp['groups'])}"
    for g, e in exp["groups"].items():
        f = rr.results[g]["pooled"]
        assert f.status == e["status"], f"{g} status"
        assert int(f.n) == e["n"], f"{g} n"
        assert bool(f.truncated) == e["truncated"], f"{g} truncated"
        # 关键列：k、σk、R²（浮点容差 1e-9 相对值）
        assert f.k == pytest.approx(e["k"], rel=1e-9), f"{g} k"
        assert f.k_err == pytest.approx(e["k_err"], rel=1e-9), f"{g} k_err"
        assert f.r2 == pytest.approx(e["r2"], rel=1e-9), f"{g} r2"
        # 拟合窗（x 首末）也必须冻结
        assert float(f.x[0]) == pytest.approx(e["x_start"], rel=1e-12), f"{g} x_start"
        assert float(f.x[-1]) == pytest.approx(e["x_end"], rel=1e-12), f"{g} x_end"
        if e.get("pre_k") is not None and np.isfinite(e["pre_k"]):
            assert f.pre_k == pytest.approx(e["pre_k"], rel=1e-9), f"{g} pre_k"
        # Φ_Δ 状态（本样例无参比 → NO_REF 属冻结契约）
        ph = rr.phi.get(g) or {}
        assert ph.get("status") == e["phi_status"], f"{g} phi status"
        if e.get("phi_value") is not None:
            assert ph.get("phi") == pytest.approx(e["phi_value"], rel=1e-9), f"{g} phi"


def test_golden_crosschecked_with_legacy_a2() -> None:
    """FR-55.3 / A2：金线与 legacy A2 基线并存互证（同一组 k/R² 偏差在 A2 限内）。"""
    from core.phi import compute_phi
    from core.pipeline import run_core
    from core.results import setup_logging

    exp = json.loads((GOLDEN / "golden_uvvis.json").read_text(encoding="utf-8"))
    legacy = json.loads((BASELINE / "baseline_2i.json").read_text(encoding="utf-8"))
    cap = setup_logging(None, debug=False)
    try:
        scans, results = run_core(BASELINE / "data_2i", default_config())
        compute_phi(results, default_config())
    finally:
        cap.detach()
    assert len(scans) == len(legacy["files"]), "A2 文件数"
    for g, gd in legacy["groups"].items():
        lf = gd["fit"]
        nf = results[g]["pooled"]
        assert abs(nf.k - lf["k"]) / abs(lf["k"]) < 1e-3, f"A2 {g} k 偏差"
        assert abs(nf.r2 - lf["r2"]) < 1e-6, f"A2 {g} R² 偏差"
        assert nf.n == lf["n"] and bool(nf.truncated) == bool(lf["truncated"])
    # 金线冻结值与 A2 基线同组数值一致（一次性交叉核对结果的固化断言）
    g = "Resorufin-2I-pbs-TEST"
    lf = legacy["groups"][g]["fit"]
    e = exp["groups"][g]
    assert abs(e["k"] - lf["k"]) / abs(lf["k"]) < 1e-3
    assert abs(e["r2"] - lf["r2"]) < 1e-6


def test_golden_parse_fds() -> None:
    """FR-55.1 解析金线：FDS 样例 → (波长, 强度) 数组 == 冻结值。"""
    from core.io import load_spectra_files

    d = np.load(GOLDEN / "golden_parse.npz")
    spec = load_spectra_files([ROOT / "tests" / "data" / "fds" / "10UM.FDS"],
                              default_config())[0]
    assert spec.ok and spec.wl.shape == d["fds_wl"].shape
    np.testing.assert_allclose(spec.wl, d["fds_wl"], rtol=1e-12, atol=0)
    np.testing.assert_allclose(spec.y, d["fds_y"], rtol=1e-12, atol=0)


def test_golden_parse_spc() -> None:
    """FR-55.1 解析金线：仪器 .spc 样例 → (波长, 强度) 数组 == 冻结值。"""
    from core.io_spc import read_spc_file_ex

    d = np.load(GOLDEN / "golden_parse.npz")
    recs, why = read_spc_file_ex(ROOT / "tests" / "data" / "spc" /
                                 "Resorufin-2I-pbs-TEST-1.spc")
    assert why == "" and len(recs) == 1, why
    rec = recs[0]
    wl = np.asarray(rec["wavelength_nm"], dtype=float)
    y = np.asarray(rec["absorbance"], dtype=float)
    assert wl.shape == d["spc_wl"].shape
    np.testing.assert_allclose(wl, d["spc_wl"], rtol=1e-12, atol=0)
    np.testing.assert_allclose(y, d["spc_y"], rtol=1e-12, atol=0)


def test_a4_5_zero_pdf_filter() -> None:
    """FR-55.3 / A4.5：filter_formats 过滤 pdf（个人版零 PDF 约束保留）。"""
    from core.plotstyle import filter_formats

    assert "pdf" not in filter_formats(["png", "pdf", "svg"])
    assert set(filter_formats(["png", "svg"])) == {"png", "svg"}


def test_a10_4_no_hardcoded_dye_names_in_core() -> None:
    """FR-55.3 / A10.4：core **运行时代码**不得写死染料名（注释/字符串是文档，跳过）。

    迁移自验收 `_dye_name_in_code()`（tokenize 跳过 COMMENT/STRING 后扫描标识符）。
    """
    import io as _io
    import tokenize

    banned = ("Resorufin", "染料", "ABDA")
    hits = []
    for p in sorted((ROOT / "core").rglob("*.py")):
        code = p.read_text(encoding="utf-8")
        keep: list[str] = []
        try:
            for tok in tokenize.generate_tokens(_io.StringIO(code).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                keep.append(tok.string)
        except Exception:                       # noqa: BLE001 —— 语法异常退回全文
            keep = [code]
        text = " ".join(keep)
        for b in banned:
            if b in text:
                hits.append(f"{p.relative_to(ROOT)}:{b}")
    assert not hits, "core 运行时代码出现染料名硬编码：" + "；".join(hits[:10])


def test_a15_fixtures_read_only() -> None:
    """FR-55.3 / A15：FDS/.spc 夹具解析前后逐字节不变（只读契约）。"""
    from core.io import load_spectra_files
    from core.io_spc import read_spc_file_ex

    files = [ROOT / "tests" / "data" / "fds" / "10UM.FDS",
             ROOT / "tests" / "data" / "spc" / "Resorufin-2I-pbs-TEST-1.spc"]
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    load_spectra_files([files[0]], default_config())
    read_spc_file_ex(files[1])
    after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    assert before == after, "夹具解析改写了源文件"
