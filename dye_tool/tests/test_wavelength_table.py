# -*- coding: utf-8 -*-
"""``core.wavelength_table`` 单元测试：A@λ 取点 / 插值 / 容差 / 对齐 / 过滤 / 坏输入。

**全部用例都用已知真值**（手算或人工构造），不断言"没崩"：

* 最近点模式：命中实测点 → **逐位等于**该点吸光度（``==``，不是 ``approx``）；
  容差外 → NaN（绝不静默取远点）；
* 插值模式：``wl=[500, 510]`` / ``a=[1.0, 2.0]`` → ``A@505`` **恰好 1.5**，
  且与 ``np.interp`` 逐位一致（守则 §9："插值必须能证明与 np.interp 等价"）；
  范围外不外推 → NaN；
* 容差边界：``|Δλ| == tol`` **取到**（口径是 ``<=``，含边界），略超即 NaN；
* 多文件：三支范围不同的谱 → 逐列断言 NaN 分布 + 统计计数。

⚠ 设计文档用例 1 里"``A@501.4``（tol=0.5）得到 NaN"与该用例自己的规则
（"仅当 ``|wl_actual - wl_target| <= tol_nm``"）**算术上互斥**：501.4 距最近点
501 只有 0.4 nm，``0.4 <= 0.5`` → 必须命中。本文件按**规则**钉住（超差样例改用
501.6 = Δ0.6），并把这条差异交回主线核对。
"""
from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import numpy as np
import pytest

from conftest import make_scan
from core.wavelength_table import (META_FIELDS, absorbance_at,
                                   write_wavelength_table)

#: 标准谱：500…510 nm，间隔 1 nm；吸光度为手写常量（逐位可控）
WL_K = np.arange(500.0, 511.0)
A_K = np.array([0.000, 0.123, 0.234, 0.345, 0.456, 0.567,
                0.678, 0.789, 0.876, 0.932, 1.005])

CFG7 = [400.0, 450.0, 500.0, 550.0, 600.0, 650.0, 700.0]
COLS7 = [f"A@{w!s}" for w in CFG7]


# ═══════════════════════════ 夹具 / 小工具 ═══════════════════════════
def _cfg(wavelengths, **kw) -> dict:
    """最小 ``wavelength_table`` 配置（键位与 core/config.py 一致）。"""
    sec = {"wavelengths": list(wavelengths), "tol_nm": 0.5, "interpolate": False,
           "include_groups": [], "include_dyes": []}
    sec.update(kw)
    return {"wavelength_table": sec}


def _is_zh(s) -> bool:
    """粗判"是中文原因"（至少含一个 CJK 字符）。"""
    return bool(s) and any("\u4e00" <= ch <= "\u9fff" for ch in str(s))


class _Cap(logging.Handler):
    """收集 ``(levelname, message)`` 的临时 handler。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list = []

    def emit(self, record) -> None:                      # noqa: D102
        self.records.append((record.levelname, record.getMessage()))


@contextlib.contextmanager
def _log_records(name: str = "dye_tool.wavelength_table"):
    """抓本模块的日志记录。

    **不用 caplog**：本项目 ``dye_tool`` logger 会被 ``attach_logging``/
    ``setup_logging`` 设成 ``propagate=False``（见 core/results.py），届时 caplog
    挂在 root 上的 handler 一条都收不到——自己挂 handler 才贴近真实运行
    （与 test_v11_features.py 的 ``test_unknown_jacs_key_warns_once`` 同法）。
    """
    lg = logging.getLogger(name)
    h = _Cap()
    lg.addHandler(h)
    try:
        yield h.records
    finally:
        lg.removeHandler(h)


# ═══════════════════════════ 1. 最近点模式 ═══════════════════════════
def test_nearest_point_is_bit_exact() -> None:
    """λ 正好落在实测点上 → 逐位等于该点吸光度（最近点模式的真值基准）。"""
    r = absorbance_at([make_scan("s-0.txt", WL_K, A_K)], _cfg([501.0]))
    assert r["ok"] is True and r["reason"] == ""
    row = r["rows"][0]
    assert row["A@501.0"] == A_K[1]                 # 精确相等，非 approx
    assert row["n_points"] == 11
    assert r["stats"]["A@501.0"] == {"n": 1, "min": A_K[1], "max": A_K[1],
                                     "mean": A_K[1]}


def test_nearest_within_tol_hits_and_outside_is_nan() -> None:
    """tol=0.3：Δ0.2 命中最近点；Δ0.4 > tol → NaN（不取远处的 502）。"""
    r = absorbance_at([make_scan("s-0.txt", WL_K, A_K)],
                      _cfg([501.2, 501.4], tol_nm=0.3))
    row = r["rows"][0]
    assert row["A@501.2"] == A_K[1]
    assert np.isnan(row["A@501.4"])                 # 0.4 > 0.3
    assert row["A@501.4"] != A_K[1]                 # 不是 501 的值
    assert row["A@501.4"] != A_K[2]                 # 更不是"最近的 502"
    assert any("A@501.4" in m for m in r["issues"])
    assert r["stats"]["A@501.4"]["n"] == 0
    assert np.isnan(r["stats"]["A@501.4"]["mean"])


def test_tol_half_of_grid_always_hits_the_nearest_point() -> None:
    """⚠ 口径推论：**1 nm 栅格 + tol=0.5** 时任何目标都在某实测点 0.5 nm 内
    （最坏情况正好是半格 = 0.5，含边界 → 命中），**不存在**超差格。

    设计文档用例 1 的"A@501.4（tol=0.5）→ NaN"与这条推论互斥：501.4 距最近点 501
    只有 0.4 nm。要让"超差"真的发生，必须**栅格 > 2×tol** 或把 tol 调小
    （见 :func:`test_nearest_within_tol_hits_and_outside_is_nan` /
    :func:`test_nearest_out_of_tolerance_is_nan_on_sparse_grid`）。
    """
    r = absorbance_at([make_scan("s-0.txt", WL_K, A_K)],
                      _cfg([501.4, 501.5, 501.6]))
    row = r["rows"][0]
    assert row["A@501.4"] == A_K[1]                 # 距 501 仅 0.4
    assert row["A@501.5"] == A_K[1]                 # 半格 = 0.5 = tol → 含边界，取低波长
    assert row["A@501.6"] == A_K[2]                 # 最近点换成 502（距 0.4），仍是命中
    assert r["issues"] == []                        # 一个超差格都没有


def test_nearest_out_of_tolerance_is_nan_on_sparse_grid() -> None:
    """栅格 2 nm + tol 0.5 → 501.0 距最近点 1.0 nm > tol → NaN（绝不取远点）。"""
    s = make_scan("sparse-0.txt", [500.0, 502.0], [0.11, 0.99])
    r = absorbance_at([s], _cfg([501.0]))
    row = r["rows"][0]
    assert np.isnan(row["A@501.0"])
    assert any("A@501.0" in m and "容差" in m for m in r["issues"])
    assert r["stats"]["A@501.0"]["n"] == 0


def test_equidistant_tie_takes_lower_wavelength() -> None:
    """恰好等距（Δ 都是 1.0 nm）→ 取**低波长**那个（口径钉住）。"""
    r = absorbance_at([make_scan("t-0.txt", [500.0, 502.0], [0.11, 0.99])],
                      _cfg([501.0], tol_nm=1.0))
    assert r["rows"][0]["A@501.0"] == 0.11


def test_invalid_points_do_not_participate_in_picking() -> None:
    """NaN 点不参与取点：只按**有效**点算最近距离，也不会被当成命中。"""
    s = make_scan("p-0.txt", [500.0, 500.2, 501.0], [0.5, np.nan, 0.7])
    r = absorbance_at([s], _cfg([500.1, 500.2]))
    row = r["rows"][0]
    assert row["n_points"] == 2                     # 无效点不计入
    assert row["A@500.1"] == 0.5                    # 最近有效点 500.0，Δ0.1
    assert row["A@500.2"] == 0.5                    # 该点本身无效 → 落到 500.0（Δ0.2 <= tol）


# ═══════════════════════════ 2. 容差边界 ═══════════════════════════
def test_tolerance_boundary_is_inclusive() -> None:
    """口径 = ``<=``：``|Δλ| == tol`` 取到；略超即 NaN；tol=0 只认完全命中。"""
    s = make_scan("b-0.txt", [500.0], [0.7])
    r = absorbance_at([s], _cfg([500.5, 500.5000001], tol_nm=0.5))
    row = r["rows"][0]
    assert 500.5 - 500.0 == 0.5                     # 边界值本身可精确表示
    assert row["A@500.5"] == 0.7                     # Δ == tol → 命中（<=）
    assert np.isnan(row["A@500.5000001"])           # Δ > tol → NaN
    r0 = absorbance_at([s], _cfg([500.0, 500.0000001], tol_nm=0.0))
    assert r0["rows"][0]["A@500.0"] == 0.7          # Δ == 0 == tol → 命中
    assert np.isnan(r0["rows"][0]["A@500.0000001"])


# ═══════════════════════════ 3. 插值模式 ═══════════════════════════
def test_interpolation_is_exact_and_equals_numpy() -> None:
    """``wl=[500,510]``/``a=[1,2]``：A@505 恰好 1.5，且与 ``np.interp`` 逐位一致。"""
    s = make_scan("i-0.txt", [500.0, 510.0], [1.0, 2.0])
    targets = [505.0, 502.5, 500.0, 510.0]
    r = absorbance_at([s], _cfg(targets, interpolate=True))
    assert r["ok"] is True
    row = r["rows"][0]
    assert row["A@505.0"] == 1.5                    # 手算真值，恰好
    assert row["A@502.5"] == 1.25
    assert row["A@500.0"] == 1.0 and row["A@510.0"] == 2.0   # 端点 = 实测值
    for w in targets:                               # 与 np.interp 等价（逐位）
        assert row[f"A@{w!s}"] == float(np.interp(w, [500.0, 510.0], [1.0, 2.0]))


def test_interpolation_does_not_extrapolate() -> None:
    """范围外**不外推** → NaN（qc.interp_valid 会把端点值平推，本模块不能那样）。"""
    s = make_scan("i-0.txt", [500.0, 510.0], [1.0, 2.0])
    r = absorbance_at([s], _cfg([495.0, 515.0, 510.5], interpolate=True))
    row = r["rows"][0]
    assert np.isnan(row["A@495.0"]) and np.isnan(row["A@515.0"])
    assert np.isnan(row["A@510.5"])
    assert r["stats"]["A@495.0"]["n"] == 0 and r["stats"]["A@515.0"]["n"] == 0
    assert any("超出数据范围" in m for m in r["issues"])


def test_interpolation_bridges_invalid_points_like_qc() -> None:
    """中间点无效 → 跨过它插值（与 ``core.qc.interp_valid`` 同一条数值路径）。"""
    s = make_scan("q-0.txt", [500.0, 505.0, 510.0], [0.0, np.nan, 1.0])
    r = absorbance_at([s], _cfg([505.0], interpolate=True))
    row = r["rows"][0]
    assert row["n_points"] == 2
    assert row["A@505.0"] == 0.5
    assert row["A@505.0"] == float(np.interp(505.0, [500.0, 510.0], [0.0, 1.0]))


# ═══════════════════════════ 4. 多文件对齐 ═══════════════════════════
def test_multi_file_alignment_nan_distribution() -> None:
    """三支范围不同的谱：逐列断言 NaN 分布、行键一致、统计计数。"""
    s1 = make_scan("a-0.txt", np.arange(400.0, 701.0), np.full(301, 0.1))
    s2 = make_scan("b-0.txt", np.arange(500.0, 601.0), np.full(101, 0.2))
    s3 = make_scan("c-0.txt", np.arange(300.0, 451.0), np.full(151, 0.3))
    r = absorbance_at([s1, s2, s3], _cfg(CFG7))
    assert r["ok"] is True and r["skipped"] == []
    assert r["wavelengths"] == CFG7 and r["columns"] == COLS7
    assert [row["file"] for row in r["rows"]] == ["a-0.txt", "b-0.txt", "c-0.txt"]
    for row in r["rows"]:                           # 所有行同一组波长（同序）
        assert list(row.keys()) == [*META_FIELDS, *COLS7]
    a_, b_, c_ = r["rows"]
    assert [a_[c] for c in COLS7] == [0.1] * 7      # 全覆盖
    assert [bool(np.isnan(b_[c])) for c in COLS7] == \
        [True, True, False, False, False, True, True]
    assert [bool(np.isnan(c_[c])) for c in COLS7] == \
        [False, False, True, True, True, True, True]
    assert b_["A@500.0"] == 0.2 and b_["A@600.0"] == 0.2      # 不只是 NaN 分布对
    assert c_["A@400.0"] == 0.3 and c_["A@450.0"] == 0.3
    assert [r["stats"][c]["n"] for c in COLS7] == [2, 2, 2, 2, 2, 1, 1]
    assert r["stats"]["A@650.0"]["min"] == r["stats"]["A@650.0"]["max"] == 0.1
    assert r["stats"]["A@400.0"]["mean"] == pytest.approx((0.1 + 0.3) / 2)


def test_row_carries_time_columns_and_fr12_edit_flag() -> None:
    """行元数据齐全；``t_edited`` 如实反映 FR-12 时间整定。"""
    s = make_scan("g-1.txt", WL_K, A_K, group="g", rep="1", role="sample", t=90.0)
    s.t_src = 60.0                                  # 文件名 60 s，用户整定成 90 s
    row = absorbance_at([s], _cfg([501.0]))["rows"][0]
    assert (row["file"], row["group"], row["rep"], row["role"]) == \
        ("g-1.txt", "g", "1", "sample")
    assert (row["t"], row["t_src"], row["t_edited"]) == (90.0, 60.0, True)
    s2 = make_scan("h-1.txt", WL_K, A_K, t=30.0)     # 未整定 → t_src == t
    row2 = absorbance_at([s2], _cfg([501.0]))["rows"][0]
    assert (row2["t"], row2["t_src"], row2["t_edited"]) == (30.0, 30.0, False)


# ═══════════════════════════ 5. 过滤 ═══════════════════════════
def test_include_groups_is_case_insensitive() -> None:
    """``include_groups`` 大小写不敏感；被过滤的文件出现在 ``skipped`` 里。"""
    s1 = make_scan("a-0.txt", WL_K, A_K, group="Sample")
    s2 = make_scan("b-0.txt", WL_K, A_K, group="blank")
    r = absorbance_at([s1, s2], _cfg([501.0], include_groups=["sample"]))
    assert r["ok"] is True
    assert [row["file"] for row in r["rows"]] == ["a-0.txt"]
    assert len(r["skipped"]) == 1
    assert r["skipped"][0]["file"] == "b-0.txt"
    assert "include_groups" in r["skipped"][0]["reason"]
    assert _is_zh(r["skipped"][0]["reason"])
    assert r["rows"][0]["A@501.0"] == A_K[1]


def test_include_dyes_and_dye_column() -> None:
    """``dye_of`` 注入染料名 → ``dye`` 列；``include_dyes`` 大小写不敏感过滤。"""
    s1 = make_scan("a-0.txt", WL_K, A_K)
    s2 = make_scan("b-0.txt", WL_K, A_K)
    names = {"a-0.txt": "DyeA", "b-0.txt": "DyeB"}

    def dye_of(path) -> str:
        return names.get(Path(path).name, "")

    r = absorbance_at([s1, s2], _cfg([501.0], include_dyes=["dyea"]), dye_of=dye_of)
    assert [row["file"] for row in r["rows"]] == ["a-0.txt"]
    assert r["rows"][0]["dye"] == "DyeA"
    assert "include_dyes" in r["skipped"][0]["reason"]
    both = absorbance_at([s1, s2], _cfg([501.0]), dye_of=dye_of)   # 空列表 = 不过滤
    assert [row["dye"] for row in both["rows"]] == ["DyeA", "DyeB"]
    assert both["skipped"] == []
    none_cb = absorbance_at([s1, s2], _cfg([501.0]))               # 不给回调 → 留空
    assert [row["dye"] for row in none_cb["rows"]] == ["", ""]
    assert none_cb["ok"] is True


def test_include_dyes_without_callback_is_uncomputable() -> None:
    """要按染料过滤却没给回调 → 不可算（明确报错，别给一张空表让人猜）。"""
    s = make_scan("a-0.txt", WL_K, A_K)
    r = absorbance_at([s], _cfg([501.0], include_dyes=["DyeA"]))
    assert r["ok"] is False and _is_zh(r["reason"]) and "dye_of" in r["reason"]
    assert r["rows"] == []


def test_broken_dye_callback_does_not_break_export() -> None:
    """回调抛异常 → 该文件 dye 留空 + 一条 issue（绝不中断整张表）。"""
    def boom(_path):
        raise RuntimeError("注册表坏了")

    r = absorbance_at([make_scan("a-0.txt", WL_K, A_K)], _cfg([501.0]), dye_of=boom)
    assert r["ok"] is True and r["rows"][0]["dye"] == ""
    assert any("dye_of" in m for m in r["issues"])


# ═══════════════════════════ 6. 坏输入（绝不抛异常） ═══════════════════════════
def test_bad_inputs_never_raise() -> None:
    """空 scans / 空波长 / 缺配置 → 配置级失败；wl=None / 长度不一致 / 全 NaN /
    空谱 / 已剔除 → 文件级跳过。两种都要 ``ok=False`` + 中文 ``reason`` 且不抛。"""
    good = make_scan("g-0.txt", WL_K, A_K, group="g", t=30.0)
    cfg_cases = [
        ([], _cfg([500.0])),                                   # 空 scans
        (None, _cfg([500.0])),                                 # scans 为 None
        ([good], _cfg([])),                                    # 波长列表空
        ([good], {"wavelength_table": {}}),                    # 缺 wavelengths
        ([good], {}),                                          # 缺整节
        ([good], None),                                        # cfg 为 None
    ]
    for scans, cfg in cfg_cases:
        r = absorbance_at(scans, cfg)               # 不得抛
        assert r["ok"] is False, (scans, cfg)
        assert _is_zh(r["reason"]), r["reason"]
        assert r["rows"] == [] and r["stats"] == {} and r["skipped"] == []
        assert isinstance(r["issues"], list)
    file_cases = [
        [make_scan("n-0.txt", None, None)],                    # wl/a 均为 None
        [make_scan("n-1.txt", WL_K, None)],                    # a 为 None
        [make_scan("m-0.txt", [500.0, 501.0, 502.0], [0.1, 0.2])],   # 长度不一致
        [make_scan("z-0.txt", [500.0, 501.0], [np.nan, np.nan])],    # 全 NaN
        [make_scan("e-0.txt", [], [])],                        # 空谱
        [make_scan("x-0.txt", WL_K, A_K, excluded="吸光度饱和")],     # 已被剔除
    ]
    for scans in file_cases:
        r = absorbance_at(scans, _cfg([500.0]))     # 不得抛
        assert r["ok"] is False, scans
        assert _is_zh(r["reason"]), r["reason"]     # 全部跳完 → 整表不可算
        assert r["rows"] == [] and r["stats"] == {}
        assert len(r["skipped"]) == 1               # 且逐文件给出中文原因
        assert r["skipped"][0]["file"] == scans[0].path.name
        assert _is_zh(r["skipped"][0]["reason"]), r["skipped"][0]


def test_non_numeric_wavelengths_are_dropped_with_issue() -> None:
    """波长列表里的非数值项被忽略（记 issue），其余列照常导出。"""
    r = absorbance_at([make_scan("g-0.txt", WL_K, A_K)], _cfg([501.0, "abc", None]))
    assert r["ok"] is True and r["columns"] == ["A@501.0"]
    assert any("不是有效数字" in m for m in r["issues"])


def test_all_bad_wavelengths_is_uncomputable() -> None:
    """波长列表全是非数值 → 没有列可出。"""
    r = absorbance_at([make_scan("g-0.txt", WL_K, A_K)], _cfg(["x", None]))
    assert r["ok"] is False and _is_zh(r["reason"]) and r["columns"] == []


# ═══════════════════════════ 7. 重复波长 ═══════════════════════════
def test_duplicate_wavelengths_warn_and_keep_one_column() -> None:
    """重复波长（含 ``500`` 与 ``500.0`` 这种同值写法）→ WARNING + 只留一列。"""
    with _log_records() as recs:
        r = absorbance_at([make_scan("g-0.txt", WL_K, A_K)],
                          _cfg([500.0, 500.0, 500, 600.0]))
    assert r["ok"] is True
    assert r["columns"] == ["A@500.0", "A@600.0"]   # 列数正确（去重后 2 列）
    assert r["wavelengths"] == [500.0, 600.0]
    assert list(r["rows"][0].keys()) == [*META_FIELDS, "A@500.0", "A@600.0"]
    assert r["rows"][0]["A@500.0"] == A_K[0]
    assert any("重复" in m for m in r["issues"])
    assert any(level == "WARNING" and "重复" in m for level, m in recs), recs


# ═══════════════════════════ 8. CSV 写出 ═══════════════════════════
def test_write_csv_header_order_nan_blank_and_single_file(tmp_path) -> None:
    """表头 = 元数据列 + **用户给的波长顺序**；NaN → 空串；只写一个文件。"""
    s1 = make_scan("a-0.txt", WL_K, A_K)
    s2 = make_scan("b-0.txt", [699.0, 700.0, 701.0], [0.5, 0.6, 0.7])
    r = absorbance_at([s1, s2], _cfg([700.0, 501.0]))       # 顺序：先 700 后 501
    p = write_wavelength_table(r, tmp_path / "out.csv")
    assert p == tmp_path / "out.csv" and p.exists()
    assert p.read_bytes()[:3] == b"\xef\xbb\xbf"            # utf-8-sig（同 results.csv）
    assert [q.name for q in tmp_path.iterdir()] == ["out.csv"]   # 不额外写 stats 文件
    lines = p.read_text(encoding="utf-8-sig").splitlines()
    head = lines[0].split(",")
    assert head[:9] == list(META_FIELDS)
    assert head[9:] == ["A@700.0", "A@501.0"]
    assert lines[0] == ("file,group,dye,rep,t,t_src,t_edited,role,n_points,"
                        "A@700.0,A@501.0")
    f1, f2 = lines[1].split(","), lines[2].split(",")
    assert f1[0] == "a-0.txt" and f2[0] == "b-0.txt"
    assert f1[9] == "" and f1[10] == format(A_K[1], ".6g")  # 700 超差 → 空白
    assert f2[9] == "0.6" and f2[10] == ""                  # 501 超差 → 空白
    assert f1[6] == "否" and f1[8] == "11"                  # t_edited / n_points


def test_write_csv_uses_result_columns_without_the_key(tmp_path) -> None:
    """手搓的 result（没有 ``columns`` 键）也能写：列名从 wavelengths 推。"""
    r = {"wavelengths": [501.0], "rows": [{"file": "x.txt", "A@501.0": 0.25}]}
    p = write_wavelength_table(r, tmp_path / "hand.csv")
    lines = p.read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].endswith("A@501.0")
    assert lines[1].split(",")[-1] == "0.25"
    assert lines[1].split(",")[1] == ""             # 缺失的元数据列 → 空串


def test_write_rejects_non_dict_result(tmp_path) -> None:
    """``result`` 不是 dict → 立刻报错，别写出半张错表。"""
    with pytest.raises(TypeError):
        write_wavelength_table(None, tmp_path / "x.csv")
