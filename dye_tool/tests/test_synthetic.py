# -*- coding: utf-8 -*-
"""synthetic 演示数据与内置自检（T1–T5 + F6）的测试。"""
from __future__ import annotations

import json

import numpy as np
import pytest

from core.io import collect_scans, read_scan
from core.pipeline import run_core
from core.synthetic import (SYN, SYN_TIMES, make_synthetic, self_test_ok,
                            self_test_text)


# ═══════════════════════════ make_synthetic ═══════════════════════════
def test_make_synthetic_writes_files_and_truth(tmp_path) -> None:
    """生成 9 组 × 21 时间点 + 坏文件 + ``_truth.json``，真值与 SYN 一致。"""
    truth = make_synthetic(tmp_path)
    assert truth == {**SYN, "seed": 42}
    assert truth["k_true"] == 5.0e-3 and truth["phi_true"] == 0.60
    assert truth["k_ref"] == pytest.approx(5.0e-3 * 0.75 / 0.60)

    t = json.loads((tmp_path / "_truth.json").read_text(encoding="utf-8"))
    assert t == truth
    assert (tmp_path / "broken.csv").exists()

    txts = sorted(tmp_path.glob("*.txt"))
    assert len(txts) == 9 * len(SYN_TIMES) == 189
    for grp in ("sample_1", "sample_2", "ref_1", "bg_1", "dark_1", "n2_1",
                "nan3_1", "nonlin_1", "mis_1"):
        assert len(list(tmp_path.glob(f"{grp}-*.txt"))) == len(SYN_TIMES)
    assert (tmp_path / "sample_1-150.txt").exists()        # 注入尖峰（150 在 SYN_TIMES 内）
    assert (tmp_path / "mis_1-0.txt").exists()             # 波长错位
    # 继承自 legacy 的 no-op：``defects`` 表里那条 ("sample_2", 200) 缺测注入
    # 因 SYN_TIMES 不含 200 s 而从未生效。为保持与 legacy 生成的 synthetic 数据
    # 逐位一致（自检 T1 反算偏差 / T5 比特一致性都依赖它），刻意保留不改。
    # core/synthetic.make_synthetic 的 docstring 已如实说明这一点。
    assert not (tmp_path / "sample_2-200.txt").exists()


@pytest.mark.xfail(strict=True,
                   reason="刻意保留的 legacy 行为：synthetic 的“缺测”注入引用 "
                          "SYN_TIMES 之外的 t=200 s，从未生效；改动会使自检基线数值变化，"
                          "故按设计文档“数值行为不变”保留（docstring 已如实标注）")
def test_make_synthetic_should_inject_missing_value_defect(tmp_path, cfg) -> None:
    """若将来决定让缺测缺陷真正生效，此用例应转为通过（strict=True 会提示 XPASS）。"""
    make_synthetic(tmp_path)
    assert (tmp_path / "sample_2-200.txt").exists()
    _, a = read_scan(tmp_path / "sample_2-200.txt", cfg["io"])
    assert np.isnan(a[0])


def test_make_synthetic_reproducible_with_seed(tmp_path) -> None:
    """相同 seed → 逐字节一致（可复现性，自检 T5 的前提）。"""
    a, b = tmp_path / "a", tmp_path / "b"
    make_synthetic(a, seed=7)
    make_synthetic(b, seed=7)
    f1, f2 = a / "sample_1-30.txt", b / "sample_1-30.txt"
    assert f1.read_bytes() == f2.read_bytes()
    # 不同 seed → 不同（噪声不同）
    c = tmp_path / "c"
    make_synthetic(c, seed=8)
    assert (c / "sample_1-30.txt").read_bytes() != f1.read_bytes()


def test_synthetic_files_are_readable_two_column_gbk(tmp_path, cfg) -> None:
    """生成的文件符合解析约定：GBK、两列、401 点、含引号乱码表头。"""
    make_synthetic(tmp_path)
    raw = read_scan(tmp_path / "sample_1-10.txt", cfg["io"])
    assert raw[0].size == 401
    assert raw[0][0] == 300.0 and raw[0][-1] == 700.0
    assert np.isfinite(raw[1]).all()
    # 注入缺陷：sample_1-150 在 379 nm 处是尖峰 0.001
    wl, a = read_scan(tmp_path / "sample_1-150.txt", cfg["io"])
    assert a[int(np.argmin(np.abs(wl - 379.0)))] == pytest.approx(0.001)
    # ⚠️ 缺陷：sample_2 的“缺测”从未注入（SYN_TIMES 无 200 s）→ 全部为有限值
    for p in sorted(tmp_path.glob("sample_2-*.txt")):
        _, y = read_scan(p, cfg["io"])
        assert np.isfinite(y).all()
    # 波长错位组：整体 +5 nm
    wl_mis, _ = read_scan(tmp_path / "mis_1-0.txt", cfg["io"])
    wl_ref, _ = read_scan(tmp_path / "sample_1-0.txt", cfg["io"])
    assert np.allclose(wl_mis - wl_ref, 5.0)


@pytest.mark.slow
def test_synthetic_pipeline_finds_groups_and_drops_misaligned(tmp_path, cfg) -> None:
    """端到端：190 个文件（含坏文件），波长错位组整组剔除，坏文件被剔除。"""
    make_synthetic(tmp_path)
    scans = collect_scans(tmp_path, cfg)
    assert len(scans) == 190                               # 189 txt + broken.csv
    assert any("broken" in s.path.name and s.excluded for s in scans)

    scans_all, results = run_core(tmp_path, cfg)
    assert "mis" not in results                            # T4：整组剔除
    assert "broken" not in results
    for g in ("sample", "ref", "nonlin", "dark", "n2", "nan3"):
        assert g in results, g
    # 波长错位组的文件（run_core 里的那份 Scan 对象）都带剔除原因
    mis = [s for s in scans_all if s.group == "mis"]
    assert len(mis) == len(SYN_TIMES)
    assert all(s.excluded and "波长轴" in s.excluded for s in mis)
    # nonlin（双指数）必须触发自动截断
    assert results["nonlin"]["pooled"].truncated is True


# ═══════════════════════════ 内置自检 ═══════════════════════════
@pytest.mark.slow
def test_self_test_returns_nine_passing_checks(selftest_checks) -> None:
    """``self_test()`` 返回 9 项检查且全部通过（最强端到端断言）。"""
    assert len(selftest_checks) == 9
    failed = [(n, info) for n, ok, info in selftest_checks if not ok]
    assert failed == [], f"自检失败项: {failed}"
    assert all(isinstance(n, str) and isinstance(info, str)
               for n, _, info in selftest_checks)
    names = " | ".join(n for n, _, _ in selftest_checks)
    for tag in ("T1", "T2", "T3", "T4", "T5", "F6"):
        assert tag in names
    # 关键项的具体说明
    info = {n: i for n, _, i in selftest_checks}
    assert any("k_hat=" in i for n, i in info.items() if n.startswith("T1  k"))
    assert info["T5  两次运行比特一致"] == "k 9 位有效数字一致"
    assert info["F6  dark/N2/NaN3 对照判定通过"] == "dark:通过; n2:通过; nan3:通过"


@pytest.mark.slow
def test_self_test_ok_and_text_helpers() -> None:
    """``self_test_ok`` → True；``self_test_text`` 含通过标记。"""
    assert self_test_ok() is True
    txt = self_test_text()
    assert "内置自检" in txt and "全部通过" in txt
    assert txt.count("[通过]") == 9
    assert "[失败]" not in txt
