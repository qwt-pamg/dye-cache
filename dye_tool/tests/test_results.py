# -*- coding: utf-8 -*-
"""``core.results`` 单元测试：CSV 表头、jsonable/fmt、结果往返、报告、日志与 run 目录。"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import numpy as np
import pytest

from conftest import SAMPLES_UVVIS
from core.pipeline import run_core
from core.results import (CSV_HEAD, LogCapture, console_summary, fmt, fnum,
                          git_hash, jsonable, make_run_dir, results_from_json,
                          safe, scans_from_json, setup_logging,
                          write_fit_summary, write_report, write_results_csv)

#: legacy ``singlet_o2_phi.py`` 第 1211–1212 行的黄金表头（列序必须逐项一致）
LEGACY_HEAD = ["组别", "角色", "k_obs(s^-1)", "k_stderr", "R2", "n", "自动截断", "A0",
               "A_ex(自动)", "辐照度(mW/cm^2)", "Phi_Delta", "Phi_err", "状态",
               "对照判定", "备注"]


# ═══════════════════════════ results.csv ═══════════════════════════
def test_csv_head_matches_legacy_column_order() -> None:
    """``CSV_HEAD`` 与 legacy 表头逐项一致（顺序敏感）。"""
    assert CSV_HEAD == LEGACY_HEAD
    assert len(CSV_HEAD) == 15
    assert CSV_HEAD[0] == "组别" and CSV_HEAD[-1] == "备注"


def test_write_results_csv_header_and_rows(tmp_path, legacy_2i) -> None:
    """写出的 CSV 仅含表头 + 每组一行，且内容与 results/phi/controls 对应。"""
    scans, results, phi, cfg = legacy_2i
    p = write_results_csv(tmp_path / "results.csv", results, phi, {})
    rows = list(csv.reader(p.read_text(encoding="utf-8-sig").splitlines()))
    assert rows[0] == CSV_HEAD
    assert len(rows) == 1 + len(results)
    g = "Resorufin-2I-pbs-TEST"
    row = dict(zip(rows[0], rows[1]))
    assert row["组别"] == g
    assert row["角色"] == "sample"
    assert row["k_obs(s^-1)"] == fmt(results[g]["pooled"].k)
    assert row["R2"] == fmt(results[g]["pooled"].r2, ".5f")
    assert row["n"] == str(results[g]["pooled"].n)
    assert row["自动截断"] == "是"
    assert row["A0"] == fmt(results[g]["A0_pooled"], ".4f")
    assert row["A_ex(自动)"] == fmt(results[g]["a_ex_auto"], ".4f")
    assert row["Phi_Delta"] == "N/A"                      # 无参比 → NO_REF
    assert row["状态"] == "OK"


def test_write_results_csv_handles_failed_and_controls(tmp_path, cfg) -> None:
    """拟合失败组与对照判定（含说明）也能正确落表。"""
    from core.kinetics import FitResult
    results = {
        "bad": {"role": "sample", "reps": {}, "A0_pooled": np.nan,
                "a_ex_auto": np.nan, "irradiance": 1.0,
                "pooled": FitResult(status="FAILED", message="所有重复均拟合失败")},
        "n2": {"role": "n2", "reps": {}, "A0_pooled": 0.9, "a_ex_auto": 0.3,
               "irradiance": 2.0, "pooled": FitResult(k=1e-4, k_err=1e-6, r2=0.99,
                                                      n=8, status="OK")},
    }
    phi = {"bad": {"phi": np.nan, "status": "FAILED"}, "n2": {}}
    controls = {"n2": ("通过", "抑制率 90%(阈值 70%)")}
    p = write_results_csv(tmp_path / "results.csv", results, phi, controls)
    rows = list(csv.reader(p.read_text(encoding="utf-8-sig").splitlines()))
    body = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert body["bad"]["状态"] == "FAILED"
    assert body["bad"]["k_obs(s^-1)"] == "N/A" and body["bad"]["自动截断"] == "否"
    assert "所有重复均拟合失败" in body["bad"]["备注"]
    assert body["n2"]["对照判定"] == "通过"
    assert body["n2"]["辐照度(mW/cm^2)"] == "2.0000"
    assert "抑制率 90%(阈值 70%)" in body["n2"]["备注"]


# ═══════════════════════════ jsonable / fmt / fnum / safe ═══════════════════════════
def test_jsonable_maps_nan_inf_to_none_recursively() -> None:
    """NaN/Inf → None（dict/list/ndarray/嵌套），其余类型转 JSON 安全值。"""
    o = {"a": float("nan"), "b": [float("inf"), -float("inf"), 1.5],
         "c": np.array([1.0, np.nan]), "d": Path("x") / "y.txt", "e": None,
         "f": (3, 2.5), "g": {"h": {"i": float("inf")}},
         "k": np.float64(2.5), "m": np.arange(3.0)}
    j = jsonable(o)
    assert j["a"] is None
    assert j["b"] == [None, None, 1.5]
    assert j["c"] == [1.0, None]
    assert j["d"] == str(Path("x") / "y.txt")
    assert j["e"] is None
    assert j["f"] == [3, 2.5]
    assert j["g"]["h"]["i"] is None
    assert j["k"] == 2.5 and isinstance(j["k"], float)     # np.floating → Python float
    assert j["m"] == [0.0, 1.0, 2.0]                       # ndarray → list
    json.dumps(j)                                          # 必须可直接序列化
    # 递归保结构：dict 键统一转 str
    assert jsonable({1: float("nan")}) == {"1": None}


def test_jsonable_numpy_scalars_are_json_safe() -> None:
    """``np.integer``/``np.bool_`` 必须转成内建标量，否则 ``json.dumps`` 抛 TypeError。

    回归保护：numpy 2.x 下 ``np.int64`` **不是** ``int`` 的子类，若原样返回会导致
    ``json.dumps(jsonable({...}))`` 失败（曾为 core 缺陷，已在
    ``core/results.jsonable`` 中修复）。
    """
    out = jsonable({"n": np.int64(3), "b": np.bool_(True), "i32": np.int32(-7),
                    "u8": np.uint8(255)})
    assert out["n"] == 3 and isinstance(out["n"], int)
    assert not isinstance(out["n"], np.integer)             # 确认已降级为内建 int
    assert out["b"] is True and isinstance(out["b"], bool)
    assert out["i32"] == -7 and isinstance(out["i32"], int)
    assert out["u8"] == 255 and isinstance(out["u8"], int)
    json.dumps(out)                                         # 必须可直接序列化


def test_fmt_and_fnum() -> None:
    """``fmt`` 非有限/非数值 → "N/A"；``fnum`` 失败/非有限 → default。"""
    assert fmt(float("nan")) == "N/A"
    assert fmt(float("inf")) == "N/A"
    assert fmt(-float("inf")) == "N/A"
    assert fmt("not-a-number") == "N/A"
    assert fmt(None) == "N/A"
    assert fmt(0.123456789) == "0.123457"
    assert fmt(1.0, ".4f") == "1.0000"
    assert fmt(9.119423843972e-04) == "0.000911942"

    assert fnum("1.5") == 1.5
    assert fnum(2) == 2.0
    assert fnum("x", -1.0) == -1.0
    assert np.isnan(fnum(None))
    assert fnum(float("nan"), 7.0) == 7.0
    assert fnum(float("inf"), 7.0) == 7.0


def test_safe_and_git_hash() -> None:
    """``safe`` 文件名安全化；``git_hash`` 永远返回字符串。"""
    assert safe("a/b c") == "a_b_c"
    assert safe("Resorufin-2I-pbs-TEST") == "Resorufin-2I-pbs-TEST"
    assert isinstance(git_hash(), str) and git_hash()


# ═══════════════════════════ make_run_dir ═══════════════════════════
def test_make_run_dir_creates_unique_dirs(tmp_path) -> None:
    """连续两次调用生成不同目录（同秒内自动加序号），且都真实存在。"""
    d1 = make_run_dir(tmp_path)
    d2 = make_run_dir(tmp_path)
    assert d1 != d2 and d1.is_dir() and d2.is_dir()
    assert d1.parent == d2.parent == tmp_path
    assert d1.name.startswith("run_") and d2.name.startswith("run_")
    d3 = make_run_dir(tmp_path / "deep" / "nested")        # 自动建父目录
    assert d3.is_dir()


# ═══════════════════════════ 日志 ═══════════════════════════
def test_setup_logging_delivers_records_to_log_cb_and_capture(tmp_path) -> None:
    """``log_cb`` 收到 INFO 及以上；``records`` 只收 WARNING+（两者阈值解耦）。

    回归保护：``LogCapture`` 曾把单个 ``level`` 同时当作 ``records`` 阈值与
    ``emit`` 闸门，于是 ``min_level`` 失效、``log_cb`` 永远收不到 INFO
    （GUI 日志区看不到流水线的 INFO 统计）。现已拆成
    ``records_level``（默认 WARNING）与 ``min_level``（默认 DEBUG）两个字段，
    handler 自身 level 取二者较低值。
    """
    seen: list[tuple[str, str]] = []
    cap = setup_logging(None, debug=False, log_cb=lambda lvl, msg: seen.append((lvl, msg)))
    assert isinstance(cap, LogCapture)
    log = logging.getLogger("dye_tool.unit")
    log.info("普通信息")
    log.warning("警告信息")
    log.error("错误信息")

    # log_cb 收到 INFO 及以上全部记录
    assert [lvl for lvl, _ in seen] == ["INFO", "WARNING", "ERROR"]
    assert any("普通信息" in m for _, m in seen)
    # records 只收 WARNING+（条目带 setup_logging 配置的时间/级别前缀）
    assert len(cap.records) == 2
    assert not any("普通信息" in r for r in cap.records)
    assert any("WARNING]" in r and "警告信息" in r for r in cap.records)
    assert any("ERROR]" in r and "错误信息" in r for r in cap.records)
    # handler 自身 level 必须低于 records 阈值，否则 INFO 进不了 emit()
    assert cap.records_level == logging.WARNING
    assert cap.level == min(cap.records_level, cap.min_level)

    # 文件 handler 写出 run.log（文件侧确实是 DEBUG 级全量）
    cap2 = setup_logging(tmp_path / "run.log", debug=False)
    logging.getLogger("dye_tool.unit").info("写入文件")
    cap2.detach()
    assert "写入文件" in (tmp_path / "run.log").read_text(encoding="utf-8")


def test_setup_logging_log_cb_should_receive_info_per_documented_contract() -> None:
    """按文档约定：``log_cb`` 应收到 INFO 及以上。"""
    seen: list[str] = []
    cap = setup_logging(None, debug=False, log_cb=lambda lvl, msg: seen.append(lvl))
    logging.getLogger("dye_tool.unit2").info("进度信息")
    cap.detach()
    assert "INFO" in seen


def test_log_cb_should_deliver_pipeline_info_messages(cfg) -> None:
    """真实流水线路径：``setup_logging(log_cb=...)`` + ``run_core`` 的 INFO 日志应投递。"""
    seen: list[tuple[str, str]] = []
    cap = setup_logging(None, debug=False,
                        log_cb=lambda lvl, msg: seen.append((lvl, msg)))
    try:
        run_core(SAMPLES_UVVIS, cfg, dry=True)
    finally:
        cap.detach()
    assert any(lvl == "INFO" and "F1: 共发现 35 个文件" in m for lvl, m in seen)


def test_log_capture_decouples_records_threshold_from_log_cb() -> None:
    """``records`` 阈值与 ``log_cb`` 闸门相互独立。

    回归保护："records 只收 WARNING+，同时 log_cb 收 INFO+" 这一约定曾因
    单一 handler level 而不可达；现由 ``records_level`` / ``min_level`` 解耦实现。
    """
    seen: list[str] = []
    cap = LogCapture(level=logging.WARNING, log_cb=lambda lvl, msg: seen.append(lvl))
    lg = logging.getLogger("dye_tool.manual")
    lg.addHandler(cap)
    lg.setLevel(logging.DEBUG)
    lg.info("信息")
    lg.debug("调试")
    lg.warning("警告")
    lg.removeHandler(cap)
    assert seen == ["INFO", "DEBUG", "WARNING"]            # log_cb 收到全部
    assert cap.records == ["警告"]                          # records 只收 WARNING+
    assert cap.records_level == logging.WARNING
    assert cap.min_level == logging.DEBUG


def test_log_capture_detach_removes_handlers() -> None:
    """``detach`` 摘掉根 logger 上所有 handler（防跨用例泄漏）。"""
    lg = logging.getLogger("dye_tool")
    setup_logging(None)
    assert lg.handlers
    cap = setup_logging(None)
    cap.detach()
    assert lg.handlers == []


def test_log_capture_emit_survives_bad_callback() -> None:
    """回调抛异常不得影响日志系统（GUI 回调异常保护）。"""
    def boom(lvl, msg):
        raise RuntimeError("UI 崩了")
    cap = LogCapture(level=logging.WARNING, log_cb=boom)
    lg = logging.getLogger("dye_tool.boom")
    lg.addHandler(cap)
    lg.warning("仍然记录")
    lg.removeHandler(cap)
    assert cap.records == ["仍然记录"]


# ═══════════════════════════ fit_summary.json 往返 ═══════════════════════════
def test_fit_summary_round_trip_preserves_fit_and_rebuilds_scans(tmp_path, legacy_2i) -> None:
    """write_fit_summary → results_from_json / scans_from_json 往返保真。"""
    scans, results, phi, cfg = legacy_2i
    g = "Resorufin-2I-pbs-TEST"
    p = write_fit_summary(tmp_path / "fit_summary.json", scans, results, phi, {}, cfg)
    j = json.loads(p.read_text(encoding="utf-8"))

    assert j["version"] and j["generated_at"]
    assert len(j["files"]) == len(scans)
    assert j["phi"][g]["status"] == "NO_REF"
    assert j["config"]["wavelength"]["probe"] == 379.0

    res2 = results_from_json(j)
    nf, nf2 = results[g]["pooled"], res2[g]["pooled"]
    assert nf2.k == pytest.approx(nf.k, rel=1e-12)
    assert nf2.k_err == pytest.approx(nf.k_err, rel=1e-12)
    assert nf2.r2 == pytest.approx(nf.r2, abs=1e-12)
    assert nf2.n == nf.n
    assert nf2.truncated is True and nf.truncated is True
    assert nf2.pre_k == pytest.approx(nf.pre_k, rel=1e-12)
    assert nf2.status == nf.status and nf2.message == nf.message
    assert res2[g]["role"] == "sample"
    assert res2[g]["A0_pooled"] == pytest.approx(results[g]["A0_pooled"], rel=1e-12)
    assert set(res2[g]["reps"]) == set(results[g]["reps"])
    assert np.allclose(res2[g]["reps"][""]["t"], results[g]["reps"][""]["t"])

    sc2 = scans_from_json(j)
    expect = [s for s in scans if not s.excluded and s.role != "background"]
    assert len(sc2) == len(expect)
    assert {s.group for s in sc2} == {g}
    assert all(s.wl is not None and s.wl.size == 401 for s in sc2)
    assert sorted(s.t for s in sc2) == sorted(s.t for s in expect)
    assert scans_from_json({"groups": {}}) == []


def test_results_from_json_handles_failed_group(tmp_path, legacy_2i) -> None:
    """``fit`` 为 null（拟合失败）的组 → pooled=None，不抛异常。"""
    scans, results, phi, cfg = legacy_2i
    j = {"groups": {"g1": {"role": "sample", "irradiance": 1.0, "fit": None,
                           "reps": {}, "A0_pooled": float("nan")}}}
    res = results_from_json(j)
    assert res["g1"]["pooled"] is None
    assert res["g1"]["role"] == "sample"


# ═══════════════════════════ report.md ═══════════════════════════
def test_write_report_sections_and_no_ref_note(tmp_path, legacy_2i) -> None:
    """report.md 含全部章节标题，且无参比组时给出 Φ N/A 说明。"""
    scans, results, phi, cfg = legacy_2i
    warns = ["22:55:36 [WARNING] M1: 未找到背景组→ 跳过背景扣除",
             "22:55:36 [WARNING] Resorufin-2I-pbs-TEST: 无暗态点(t≤0 s) → A(0)=1.1110"]
    p = write_report(tmp_path / "report.md", "some/dir", scans, results, phi, {},
                     cfg, warns)
    txt = p.read_text(encoding="utf-8")
    for head in ("# 单线态氧（¹O₂）相对量子产率 Φ_Δ 计算报告", "## 1. 数据概览",
                 "## 2. 方法学", "## 3. 结果", "## 5. 图", "## 6. 警告汇总",
                 "## 7. 备注与建议"):
        assert head in txt
    assert "## 4. 特异性对照判定" not in txt              # 无对照 → 不出现该节
    assert "N/A（无参比组）" in txt
    assert "本次未找到 `ref` 参比组，Φ_Δ 为 N/A" in txt
    assert "未提供 `bg` 背景组" in txt
    assert "有组触发自动截断" in txt                       # 该组 truncated=True
    assert warns[0] in txt and warns[1] in txt
    assert "fig_b_kinetics_*.png" in txt
    g = "Resorufin-2I-pbs-TEST"
    assert f"| {g} | " in txt and "9.1194e-04" in txt
    assert "t ≤ 0 s 暗态点均值" in txt                      # 该批存在 t=0 文件


def test_write_report_with_controls_and_no_warnings(tmp_path, cfg) -> None:
    """有对照判定 → 出现第 4 节；无警告 → “无警告。”。"""
    from core.kinetics import FitResult
    scans = []
    results = {"n2": {"role": "n2", "reps": {}, "pooled": FitResult(
        k=1e-4, k_err=1e-6, r2=0.99, n=8, status="OK"), "A0_pooled": 0.9,
        "a_ex_auto": 0.3, "irradiance": 1.0}}
    phi = {"n2": {"phi": np.nan, "status": "FAILED"}}
    controls = {"n2": ("通过", "抑制率 90%(阈值 70%)")}
    p = write_report(tmp_path / "report.md", "d", scans, results, phi, controls, cfg, [])
    txt = p.read_text(encoding="utf-8")
    assert "## 4. 特异性对照判定" in txt
    assert "| n2 | 通过 | 抑制率 90%(阈值 70%) |" in txt
    assert "无警告。" in txt
    assert "（0/0 个文件有效）" in txt


# ═══════════════════════════ console_summary ═══════════════════════════
def test_console_summary_contains_key_metrics(tmp_path, legacy_2i) -> None:
    """控制台速览含各组 k/A(0)/Φ 状态与输出目录。"""
    scans, results, phi, cfg = legacy_2i
    txt = console_summary("some/dir", scans, results, phi, {}, tmp_path, cfg)
    assert "分析完成 — 关键结果速览" in txt
    assert "Resorufin-2I-pbs-TEST" in txt
    assert "k_obs" in txt and "R^2" in txt
    assert "[!] 已自动截断" in txt
    assert "Phi_D    : N/A (未找到 ref 参比组)" in txt
    assert str(tmp_path) in txt
    assert "21/21 个文件有效" in txt


# ═══════════════════════ DS3′：fit_summary 版本标记（FR-18） ═══════════════════════
def test_fit_summary_schema_version_comes_from_config_single_source(tmp_path, legacy_2i) -> None:
    """a. 写出路径落盘的 ``schema_version`` 与 ``core.config.SCHEMA_VERSION`` 同源。"""
    from core.config import SCHEMA_VERSION                     # 单一真相：不硬编码 1
    scans, results, phi, cfg = legacy_2i
    p = write_fit_summary(tmp_path / "fit_summary.json", scans, results, phi, {}, cfg)
    j = json.loads(p.read_text(encoding="utf-8"))
    assert j["schema_version"] == SCHEMA_VERSION


def test_fit_summary_schema_version_is_int_and_version_key_intact(tmp_path, legacy_2i) -> None:
    """b. 该键是**整数**（非 str/float/bool），且既有 ``version`` 键仍在、值不变。"""
    from core import VERSION
    from core.config import SCHEMA_VERSION
    scans, results, phi, cfg = legacy_2i
    p = write_fit_summary(tmp_path / "fit_summary.json", scans, results, phi, {}, cfg)
    raw = p.read_text(encoding="utf-8")
    j = json.loads(raw)
    v = j["schema_version"]
    assert isinstance(v, int) and not isinstance(v, bool)      # 非 str / float / bool
    assert f'"schema_version": {SCHEMA_VERSION}' in raw         # 落盘是整数字面量
    assert "version" in j and j["version"] == VERSION          # 既有键零改动
    # 锁：本批只允许**新增这一个**顶层键（键集与 legacy 版逐项一致 + schema_version）
    assert set(j) == {"version", "schema_version", "git_hash", "generated_at",
                      "config", "files", "groups", "phi", "controls"}


def test_fit_summary_extra_schema_key_does_not_change_round_trip(tmp_path, legacy_2i) -> None:
    """c. 读方按键取值：多出的键被忽略，往返结果与"删掉该键"时逐字段一致。"""
    scans, results, phi, cfg = legacy_2i
    g = "Resorufin-2I-pbs-TEST"
    p = write_fit_summary(tmp_path / "fit_summary.json", scans, results, phi, {}, cfg)
    j = json.loads(p.read_text(encoding="utf-8"))
    without = {k: val for k, val in j.items() if k != "schema_version"}
    assert "schema_version" in j and "schema_version" not in without

    a, b = results_from_json(j)[g]["pooled"], results_from_json(without)[g]["pooled"]
    assert (a.k, a.k_err, a.r2, a.n, a.truncated, a.status,
            a.message) == (b.k, b.k_err, b.r2, b.n, b.truncated, b.status, b.message)
    assert np.array_equal(a.x, b.x) and np.array_equal(a.lnR, b.lnR)
    assert a.k == pytest.approx(results[g]["pooled"].k, rel=1e-12)  # 与加键前同一结果

    sa, sb = scans_from_json(j), scans_from_json(without)
    assert len(sa) == len(sb) > 0
    assert [s.t for s in sa] == [s.t for s in sb]
    assert all(np.allclose(x.wl, y.wl) for x, y in zip(sa, sb))
