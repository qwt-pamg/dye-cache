# -*- coding: utf-8 -*-
"""``core.grouping`` 单元测试：母核指纹、自动分组、items 构造（FR-2）。"""
from __future__ import annotations

import numpy as np
import pytest

from conftest import make_scan
from core.grouping import (_dist, _suggest_label, auto_group, core_fingerprint,
                           items_from_scans)

WL = np.arange(300.0, 701.0)
AG = {"core_lo": 450.0, "core_hi": 650.0, "step": 5.0, "d_max": 0.06}


def _gauss(mu: float, sig: float, amp: float = 1.0) -> np.ndarray:
    return amp * np.exp(-0.5 * ((WL - mu) / sig) ** 2)


def _item(name: str, t: float, mu: float, sig: float = 15.0, amp: float = 1.0) -> dict:
    return {"name": name, "t": float(t), "wl": WL, "ab": _gauss(mu, sig, amp)}


# ═══════════════════════════ 母核指纹 ═══════════════════════════
def test_core_fingerprint_peak_normalized_within_region() -> None:
    """指纹在母核区峰值归一（max=1.0），λmax 落回网格点。"""
    lam, fp = core_fingerprint(WL, _gauss(550.0, 25.0), 450.0, 650.0, 5.0)
    assert lam == pytest.approx(550.0)
    assert fp is not None
    assert fp.max() == pytest.approx(1.0)
    assert len(fp) == len(np.arange(450.0, 650.0 + 1e-9, 5.0))
    assert fp.min() >= 0.0


def test_core_fingerprint_returns_none_when_region_too_sparse() -> None:
    """母核区有效点 <10 → ``(None, None)``。"""
    assert core_fingerprint(np.arange(300.0, 400.0), np.ones(100),
                            450.0, 650.0, 5.0) == (None, None)
    sparse = np.linspace(450.0, 650.0, 9)
    assert core_fingerprint(sparse, np.ones(9), 450.0, 650.0, 5.0) == (None, None)
    # 有 10 个点 → 可以指纹化
    ten = np.linspace(450.0, 650.0, 10)
    assert core_fingerprint(ten, np.ones(10), 450.0, 650.0, 5.0)[1] is not None


def test_core_fingerprint_none_when_non_positive_region() -> None:
    """母核区最大值 ≤0（无吸收）→ ``(None, None)``。"""
    assert core_fingerprint(WL, -np.ones(WL.size), 450.0, 650.0, 5.0) == (None, None)
    assert core_fingerprint(WL, np.zeros(WL.size), 450.0, 650.0, 5.0) == (None, None)


def test_core_fingerprint_ignores_nan_points() -> None:
    """NaN 点被排除；剩余有效点仍可指纹化。"""
    ab = _gauss(500.0, 30.0)
    ab[:100] = np.nan                                     # 300–399 nm 全缺
    lam, fp = core_fingerprint(WL, ab, 450.0, 650.0, 5.0)
    assert lam == pytest.approx(500.0) and fp is not None


def test_dist_is_rms_distance() -> None:
    """``_dist`` = 逐点差的 RMS。"""
    a = np.array([0.0, 1.0, 2.0])
    b = np.array([0.0, 0.0, 0.0])
    assert _dist(a, b) == pytest.approx(np.sqrt((0 + 1 + 4) / 3))
    assert _dist(a, a) == 0.0


def test_suggest_label_strips_trailing_index() -> None:
    """标签 = 组内文件名公共前缀（去掉尾部序号）；过短则退回文件名主干。"""
    lab = _suggest_label(["Resorufin-2I-pbs-TEST-1.txt", "Resorufin-2I-pbs-TEST-10.txt"])
    assert lab == "Resorufin-2I-pbs-TEST"
    single = _suggest_label(["ab.txt"])
    assert isinstance(single, str) and single
    assert _suggest_label(["sampleA.txt", "sampleB.txt"]).startswith("sample")


# ═══════════════════════════ 自动分组 ═══════════════════════════
def test_auto_group_identical_spectra_form_one_group() -> None:
    """完全相同的谱形 → 只产生 1 个组，成员按 t 升序。"""
    items = [_item("a-30.txt", 30.0, 550.0), _item("a-10.txt", 10.0, 550.0),
             _item("a-20.txt", 20.0, 550.0)]
    groups, invalid = auto_group(items, AG)
    assert invalid == []
    assert len(groups) == 1
    members = groups[0]["members"]
    assert [m["t"] for m in members] == [10.0, 20.0, 30.0]
    assert [m["name"] for m in members] == ["a-10.txt", "a-20.txt", "a-30.txt"]
    assert groups[0]["lam_max"] == pytest.approx(550.0)
    assert isinstance(groups[0]["label"], str) and groups[0]["label"]


def test_auto_group_separates_two_different_spectra() -> None:
    """谱形明显不同（480 vs 620 nm 母核峰）→ 分为 2 组。"""
    items = [_item("x-0.txt", 0.0, 480.0), _item("x-10.txt", 10.0, 480.0),
             _item("y-0.txt", 0.0, 620.0), _item("y-10.txt", 10.0, 620.0)]
    groups, invalid = auto_group(items, AG)
    assert invalid == []
    assert len(groups) == 2
    assert all(len(g["members"]) == 2 for g in groups)
    lams = sorted(g["lam_max"] for g in groups)
    assert lams[0] == pytest.approx(480.0) and lams[1] == pytest.approx(620.0)
    sets = [set(m["name"][0] for m in g["members"]) for g in groups]
    assert sets[0] != sets[1]                             # 两个族没有被混到一起


def test_auto_group_dmax_threshold_controls_merging() -> None:
    """RMS 阈值可调：d_max 放大后相近的两族合并为一组。"""
    items = [_item("p-0.txt", 0.0, 520.0), _item("q-0.txt", 0.0, 560.0)]
    groups_strict, _ = auto_group(items, AG)
    assert len(groups_strict) == 2
    groups_loose, _ = auto_group(items, {**AG, "d_max": 1.0})
    assert len(groups_loose) == 1


def test_auto_group_puts_truncated_spectrum_into_invalid() -> None:
    """母核区无有效数据的谱 → ``invalid``，其余正常归组。"""
    truncated = {"name": "short-0.txt", "t": 0.0,
                 "wl": np.arange(300.0, 400.0), "ab": np.ones(100)}
    items = [_item("ok-0.txt", 0.0, 550.0), _item("ok-10.txt", 10.0, 550.0),
             truncated]
    groups, invalid = auto_group(items, AG)
    assert invalid == ["short-0.txt"]
    assert len(groups) == 1
    assert [m["name"] for m in groups[0]["members"]] == ["ok-0.txt", "ok-10.txt"]


def test_auto_group_all_invalid_gives_no_groups() -> None:
    """全部谱都残缺 → 0 组，全部进 invalid（不抛异常）。"""
    items = [{"name": f"s{i}.txt", "t": float(i), "wl": np.arange(300.0, 400.0),
              "ab": np.ones(100)} for i in range(3)]
    groups, invalid = auto_group(items, AG)
    assert groups == []
    assert sorted(invalid) == ["s0.txt", "s1.txt", "s2.txt"]


def test_auto_group_empty_input() -> None:
    """空 items → ([], [])。"""
    assert auto_group([], AG) == ([], [])


def test_auto_group_sorts_groups_by_size_desc() -> None:
    """组按成员数降序返回（GUI 展示顺序）。"""
    items = ([_item(f"big-{i}.txt", float(i), 550.0) for i in range(3)]
             + [_item("small-0.txt", 0.0, 620.0)])
    groups, _ = auto_group(items, AG)
    assert [len(g["members"]) for g in groups] == [3, 1]


# ═══════════════════════════ items_from_scans ═══════════════════════════
def test_items_from_scans_skips_background_dark_and_excluded(tmp_path) -> None:
    """``items_from_scans`` 跳过 background/dark 与已剔除文件，并统计读不出的文件。"""
    good = make_scan("s-10.txt", WL, _gauss(550.0, 15.0), t=10.0)
    bg = make_scan("bg-0.txt", WL, _gauss(550.0, 15.0), group="bg", role="background")
    dark = make_scan("dark-0.txt", WL, _gauss(550.0, 15.0), group="dark", role="dark")
    bad_excluded = make_scan("bad-1.txt", WL, _gauss(550.0, 15.0), excluded="读取失败")
    unreadable = make_scan(tmp_path / "missing-1.txt", None, None, t=1.0)

    items, bad = items_from_scans([good, bg, dark, bad_excluded, unreadable])
    assert [it["name"] for it in items] == ["s-10.txt"]
    assert bad == 1
    it = items[0]
    assert it["t"] == 10.0 and it["group"] == "g"
    assert np.asarray(it["wl"]).size == 401
    assert len(np.asarray(it["ab"])) == 401


def test_items_from_scans_real_samples_produces_35_items() -> None:
    """真实样例：35 个文件全部转成 items（无坏文件）。"""
    from conftest import SAMPLES_UVVIS
    from core.io import collect_scans
    from core.config import default_config
    scans = collect_scans(SAMPLES_UVVIS, default_config())
    items, bad = items_from_scans(scans)
    assert len(items) == 35 and bad == 0
    assert {it["group"] for it in items} == {"Resorufin-2I-pbs-TEST", "Resorufin-pbs-t"}
    assert any(it["t"] == 390.0 for it in items)
