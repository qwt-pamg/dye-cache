# -*- coding: utf-8 -*-
"""``core.io`` 单元测试：文件名解析、稳健读文本、两列光谱、荧光谱、目录收集。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from conftest import SAMPLES_FL, SAMPLES_UVVIS
from core.io import (Scan, clear_scan_cache, collect_scans, fds_anchor_of,
                     load_spectra_files, parse_filename, parse_time_only,
                     read_emission_file, read_scan, read_spectrum_file,
                     read_text_robust, scan_cache_stats, spectrum_arrays)
from core.io_fds import read_fds_bytes

#: 真实样例：2I 系列 t=0 文件（379±2 nm 全 NaN）
REAL_ZERO = SAMPLES_UVVIS / "Resorufin-2I-pbs-TEST.txt"
REAL_SCAN = SAMPLES_UVVIS / "Resorufin-pbs-t-90+5.txt"
REAL_EM = SAMPLES_FL / "5uM in PBS.TXT"

#: 仓库自带的真实 .FDS 样例（与 tests/test_fds_adapter.py:33 同一个目录；不新建数据文件）
FDS_FIXTURES = Path(__file__).resolve().parent / "data" / "fds"
REAL_FDS = FDS_FIXTURES / "10UM.FDS"


# ═══════════════════════════ 文件名元数据 ═══════════════════════════
def test_parse_time_only_plain_minutes_and_no_suffix(cfg) -> None:
    """``xx`` → xx 秒；``xx+y`` → xx+60y 秒；无时间后缀 → 0。"""
    fn = cfg["filename"]
    assert parse_time_only("sample-30", fn) == 30.0
    assert parse_time_only("Resorufin-pbs-t-90", fn) == 90.0
    assert parse_time_only("Resorufin-pbs-t-90+5", fn) == 390.0
    assert parse_time_only("Resorufin-pbs-t-90+10", fn) == 690.0
    assert parse_time_only("Resorufin-2I-pbs-TEST", fn) == 0.0
    assert parse_time_only("Resorufin-2I-pbs-TEST-600", fn) == 600.0
    # 无分钟位时 + 形式不匹配 → 退化为无后缀（t=0），不得抛异常
    assert parse_time_only("sample-abc", fn) == 0.0


def test_parse_filename_repetition_form(cfg) -> None:
    """``sample_2-30`` → t=30 s、组 ``sample``、重复号 ``2``。"""
    fn = cfg["filename"]
    meta = parse_filename("sample_2-30", fn)
    assert meta == {"group": "sample", "rep": "2", "t": 30.0, "role": "sample"}
    no_time = parse_filename("sample_2", fn)
    assert no_time["group"] == "sample"
    assert no_time["rep"] == "2"
    assert no_time["t"] == 0.0


@pytest.mark.parametrize("stem,role", [
    ("bg-1", "background"),
    ("bg_sample-1", "background"),          # 前缀识别（roles 按长度降序匹配）
    ("blank-1", "blank"),
    ("dark-1", "dark"),
    ("ref-1", "reference"),
    ("ref-60", "reference"),
    ("n2-1", "n2"),
    ("nan3-1", "quencher"),
    ("azide-5", "quencher"),
    ("Resorufin-pbs-t-90", "sample"),
    ("Resorufin-2I-pbs-TEST-1", "sample"),
])
def test_parse_filename_role_detection(cfg, stem, role) -> None:
    """角色前缀识别：bg/blank/dark/ref/n2/nan3/azide，其余为 sample。"""
    assert parse_filename(stem, cfg["filename"])["role"] == role


def test_parse_filename_no_time_suffix_can_mean_background(cfg) -> None:
    """``no_time_suffix="background"`` 时无时间后缀的文件视为背景。"""
    fn = dict(cfg["filename"], no_time_suffix="background")
    assert parse_filename("sample", fn)["role"] == "background"
    assert parse_filename("sample", cfg["filename"])["role"] == "sample"


def test_scan_dataclass_ok_and_tag() -> None:
    """``Scan.ok`` / ``Scan.tag`` 语义。"""
    s = Scan(path=Path("a-1.txt"), group="a", wl=np.arange(300.0, 310.0),
             a=np.zeros(10))
    assert s.ok and s.tag == "a"
    s.rep = "2"
    assert s.tag == "a_2"
    s.excluded = "读取失败"
    assert not s.ok


# ═══════════════════════════ 稳健读取 / 两列光谱 ═══════════════════════════
def test_read_text_robust_handles_gbk_and_utf16(tmp_path) -> None:
    """GBK 与带 BOM 的 UTF-16 均能被正确解码。"""
    gbk = tmp_path / "gbk.txt"
    gbk.write_bytes("温度：25 ℃\n300.00,0.500\n".encode("gbk"))
    txt = read_text_robust(gbk)
    assert "温度" in txt and "300.00" in txt

    u16 = tmp_path / "u16.txt"
    u16.write_bytes("温度\n300.00,0.500\n".encode("utf-16"))
    assert "温度" in read_text_robust(u16)

    utf8 = tmp_path / "utf8.txt"
    utf8.write_bytes("\ufeff温度\n300.00,0.5\n".encode("utf-8"))
    assert read_text_robust(utf8).startswith("温度")


def test_read_scan_skips_garbled_headers_and_maps_empty_to_nan(tmp_path, cfg) -> None:
    """两行带引号的乱码表头被跳过；空吸光度 → NaN。"""
    rows = [f"{300 + i:.2f},{0.100 + 0.001 * i:.3f}" for i in range(20)]
    rows[5] = "305.00, "                                  # 空吸光度
    rows[6] = "306.00,\"\""                               # 仅引号
    body = "\n".join(['"测试-1 - RawData"', '"波长(nm)","吸收值"', *rows]) + "\n"
    p = tmp_path / "scan-1.txt"
    p.write_bytes(body.encode("gbk"))                     # GBK 乱码表头

    wl, a = read_scan(p, cfg["io"])
    assert wl.size == 20
    assert wl[0] == pytest.approx(300.0)
    assert wl[-1] == pytest.approx(319.0)
    assert np.isnan(a[5]) and np.isnan(a[6])
    assert np.isfinite(a[0]) and a[0] == pytest.approx(0.100)


def test_read_scan_real_sample_file_has_401_rows_300_700(cfg) -> None:
    """真实样例：401 行、300→700 nm，无 NaN。"""
    wl, a = read_scan(REAL_SCAN, cfg["io"])
    assert wl.size == 401
    assert wl[0] == pytest.approx(300.0)
    assert wl[-1] == pytest.approx(700.0)
    assert np.allclose(np.diff(wl), 1.0)
    assert not np.isnan(a).any()


def test_read_scan_real_zero_file_probe_window_is_nan(cfg) -> None:
    """t=0 文件：379±2 nm 窗口内全部 NaN（引擎因此走“无暗态点”分支）。"""
    wl, a = read_scan(REAL_ZERO, cfg["io"])
    m = (wl >= 377.0) & (wl <= 381.0)
    assert m.sum() == 5
    assert np.isnan(a[m]).all()
    # 其余区间仍有有效点（该文件并非整体坏）
    assert np.isfinite(a).sum() > 300


def test_read_scan_empty_absorbance_in_real_file(tmp_path, cfg) -> None:
    """真实 -600 文件首行吸光度为空 → NaN。"""
    wl, a = read_scan(SAMPLES_UVVIS / "Resorufin-2I-pbs-TEST-600.txt", cfg["io"])
    assert wl.size == 401
    assert np.isnan(a[0])
    assert np.isfinite(a).sum() >= 390


def test_read_scan_raises_valueerror_when_too_few_rows(tmp_path, cfg) -> None:
    """有效行 <10 → 抛 ValueError（调用方据此记剔除原因）。"""
    p = tmp_path / "tiny-1.txt"
    p.write_text('"tiny - RawData"\n"nm","A"\n' +
                 "\n".join(f"{300 + i},0.1" for i in range(5)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_scan(p, cfg["io"])


def test_read_scan_sorts_descending_wavelength_axis(tmp_path, cfg) -> None:
    """波长轴倒序时自动升序排列。"""
    rows = [f"{400 - i},{0.01 * i:.3f}" for i in range(15)]
    p = tmp_path / "desc-1.txt"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    wl, a = read_scan(p, cfg["io"])
    assert np.all(np.diff(wl) > 0)
    assert wl[0] == pytest.approx(386.0) and wl[-1] == pytest.approx(400.0)


def test_read_spectrum_file_and_spectrum_arrays(tmp_path, cfg) -> None:
    """兜底读取（数字正则）与 ``spectrum_arrays`` 的回读分支。"""
    got = read_spectrum_file(REAL_SCAN)
    assert got is not None and got[0].size == 401
    # 窗口过滤掉范围外的数字
    assert read_spectrum_file(REAL_SCAN, (350.0, 360.0))[0].size == 11
    # Scan 已带数组 → 直接返回
    s = Scan(path=REAL_SCAN, group="g", wl=np.arange(300.0, 310.0), a=np.ones(10))
    arr = spectrum_arrays(s)
    assert arr is not None and arr[0].size == 10
    # Scan 无数组 → 回读文件
    s2 = Scan(path=REAL_SCAN, group="g")
    assert spectrum_arrays(s2)[0].size == 401
    # 文件不存在 → None
    assert spectrum_arrays(Scan(path=tmp_path / "nope.txt", group="g")) is None


# ═══════════════════════════ 荧光发射谱 ═══════════════════════════
def test_read_emission_file_data_points_block_and_meta(cfg) -> None:
    """荧光发射文件：Data Points 段 143 个数值行，706.2→848.2 nm，头部元数据可用。

    注：文件头部 ``Number of points`` 等行属于方法设置段；Data Points 段除表头
    ``nm\\tData`` 外共有 143 个数值行（即 143 点），与设计文档写的“144 点”相差 1
    —— 那是把表头行一并计入所致（已核实）。
    """
    got = read_emission_file(REAL_EM, cfg["io"])
    assert got is not None
    wl, y, meta = got
    assert wl.size == 143
    assert wl[0] == pytest.approx(706.2)
    assert wl[-1] == pytest.approx(848.2)
    assert np.all(np.diff(wl) > 0)
    assert np.isfinite(y).all()
    assert meta["Scan mode"] == "Emission"
    assert meta["EX WL"] == "690.0 nm"
    assert meta["Model"] == "Fluorescence Spectrophotometer"
    assert meta["Sample"].startswith("sample-a")


def test_read_emission_file_without_marker_falls_back(tmp_path, cfg) -> None:
    """无 ``Data Points`` 标记 → 回退到数字正则兜底读取。"""
    p = tmp_path / "plain-1.txt"
    p.write_text('"x - RawData"\n"nm","A"\n' +
                 "\n".join(f"{300 + i},0.1" for i in range(20)) + "\n", encoding="utf-8")
    wl, y, meta = read_emission_file(p, cfg["io"])
    assert wl.size == 20 and meta == {}


def test_read_emission_file_unparsable_returns_none(tmp_path, cfg) -> None:
    """完全无法解析 → None（调用方转 excluded）。"""
    p = tmp_path / "junk.txt"
    p.write_text("Data Points\nnm\tData\n什么都\n没有\n", encoding="utf-8")
    assert read_emission_file(p, cfg["io"]) is None


# ═══════════════════════════ load_spectra_files（FR-4） ═══════════════════════════
def test_load_spectra_files_classifies_abs_and_em(cfg) -> None:
    """``samples/uvvis/*`` → kind=abs；``samples/fluorescence/*`` → kind=em。"""
    p_abs = sorted(SAMPLES_UVVIS.glob("*.txt"))[0]
    abs_sp = load_spectra_files([p_abs], cfg)[0]
    assert abs_sp.kind == "abs" and abs_sp.ok
    assert abs_sp.meta["unit"] == "AU"
    assert abs_sp.wl.size == 401

    em_sp = load_spectra_files([REAL_EM], cfg)[0]
    assert em_sp.kind == "em" and em_sp.ok
    assert em_sp.meta["unit"] == "a.u."
    assert em_sp.label.startswith("sample-a")
    assert em_sp.wl.size == 143

    # 全部荧光文件都能被识别为发射谱
    all_em = load_spectra_files(sorted(SAMPLES_FL.glob("*.TXT")), cfg)
    assert len(all_em) == 6
    assert all(s.kind == "em" and s.ok for s in all_em)


def test_load_spectra_files_marks_unreadable_as_excluded(tmp_path, cfg) -> None:
    """坏文件转 ``excluded`` 而不是抛异常。"""
    bad = tmp_path / "junk.txt"
    bad.write_text("垃圾数据\n不可解析\n", encoding="utf-8")
    sp = load_spectra_files([bad, REAL_EM], cfg)
    assert len(sp) == 2
    assert not sp[0].ok and sp[0].excluded
    assert sp[1].ok


def test_spectrum_lambda_max_method() -> None:
    """``Spectrum.lambda_max`` 方法（带搜索窗）。"""
    from core.io import Spectrum
    wl = np.arange(300.0, 701.0)
    y = 1.0 * np.exp(-0.5 * ((wl - 600.0) / 20.0) ** 2) + \
        2.0 * np.exp(-0.5 * ((wl - 520.0) / 8.0) ** 2)
    sp = Spectrum(path=Path("x-1.txt"), wl=wl, y=y, kind="abs")
    assert sp.lambda_max() == pytest.approx(520.0)
    assert sp.lambda_max(lo=560.0, hi=650.0) == pytest.approx(600.0)
    assert np.isnan(sp.lambda_max(lo=800.0, hi=900.0))
    assert sp.ok                                        # ok 只关心 excluded 与 wl 非空
    sp.excluded = "坏"
    assert not sp.ok


# ═══════════════════════════ collect_scans ═══════════════════════════
def test_collect_scans_real_samples_finds_35_files_in_2_groups(cfg) -> None:
    """``samples/uvvis``：35 个文件、2 个组、全部可读。"""
    scans = collect_scans(SAMPLES_UVVIS, cfg)
    assert len(scans) == 35
    assert {s.group for s in scans} == {"Resorufin-2I-pbs-TEST", "Resorufin-pbs-t"}
    assert all(not s.excluded for s in scans)
    assert all(s.role == "sample" for s in scans)
    assert all(s.wl.size == 401 for s in scans)
    t905 = [s.t for s in scans if s.path.name == "Resorufin-pbs-t-90+5.txt"]
    assert t905 == [390.0]


def test_collect_scans_only_paths_filter_and_progress_cb(cfg) -> None:
    """``only_paths`` 过滤生效（大小写不敏感）；``progress_cb`` 收到 0–100 进度。"""
    target = SAMPLES_UVVIS / "Resorufin-pbs-t-90+5.txt"
    one = collect_scans(SAMPLES_UVVIS, cfg, only_paths=[target])
    assert len(one) == 1 and one[0].path.name == target.name
    assert one[0].t == 390.0

    two = collect_scans(SAMPLES_UVVIS, cfg,
                        only_paths=[str(target).upper(), SAMPLES_UVVIS / "Resorufin-pbs-t-1.txt"])
    assert len(two) == 2

    calls: list[tuple[int, str]] = []
    collect_scans(SAMPLES_UVVIS, cfg, progress_cb=lambda p, s: calls.append((p, s)))
    assert calls and calls[-1][0] == 100
    assert all(0 <= p <= 100 for p, _ in calls)


def test_collect_scans_missing_dir_returns_empty(cfg) -> None:
    """目录不存在 → ``[]`` 且不抛异常。"""
    assert collect_scans(SAMPLES_UVVIS / "no_such_dir", cfg) == []


def test_collect_scans_garbage_files_are_excluded_not_raised(tmp_path, cfg) -> None:
    """全是垃圾文件时：每个 scan 都带 ``excluded``，不抛异常。"""
    (tmp_path / "a-1.txt").write_text("不是光谱\n随便写点\n", encoding="utf-8")
    (tmp_path / "b-2.txt").write_bytes(b"\x00\x01\x02\xff\xfe garbage\n")
    (tmp_path / "c-3.csv").write_text("1,2,3,4\n,,\n", encoding="utf-8")
    scans = collect_scans(tmp_path, cfg)
    assert len(scans) == 3
    assert all(s.excluded for s in scans)
    assert all("读取失败" in s.excluded for s in scans)


def test_collect_scans_skips_excluded_extensions_and_prefix(cfg, tmp_path) -> None:
    """扩展名白名单与 ``exclude`` 前缀（下划线）过滤。"""
    ok = tmp_path / "sample-1.txt"
    ok.write_text("\n".join(f"{300 + i},0.1" for i in range(20)) + "\n", encoding="utf-8")
    (tmp_path / "_truth.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    scans = collect_scans(tmp_path, cfg)
    assert [s.path.name for s in scans] == ["sample-1.txt"]


def test_collect_scans_non_recursive(cfg, tmp_path) -> None:
    """``io.recursive=False`` 时不进入子目录。"""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner-1.txt").write_text("\n".join(f"{300 + i},0.1" for i in range(20)),
                                     encoding="utf-8")
    (tmp_path / "outer-1.txt").write_text("\n".join(f"{300 + i},0.1" for i in range(20)),
                                          encoding="utf-8")
    cfg_nr = {**cfg, "io": {**cfg["io"], "recursive": False}}
    assert [s.path.name for s in collect_scans(tmp_path, cfg_nr)] == ["outer-1.txt"]
    assert len(collect_scans(tmp_path, cfg)) == 2


# ═════════ DS2｜Scan 溯源字段 source/meta（FR-18，本批只写不读） ═════════
# 设计权威：重构设计_兼容性批次.md §1.2（判据 ①②③）
def test_scan_source_meta_are_compare_false() -> None:
    """判据①：除 source/meta 外相同 → ``Scan`` 相等（等价性语义冻结在旧字段上）。"""
    base = Scan(path=Path("a-1.txt"), group="a", rep="1", role="sample", t=1.0)
    other = Scan(path=Path("a-1.txt"), group="a", rep="1", role="sample", t=1.0,
                 source="fds", meta={"format": "fds", "ex_wl_nm": 580.0})
    assert base == other, "source/meta 必须 compare=False（不进任何既有比较逻辑）"
    assert other == base, "__eq__ 必须双向成立"
    # 默认值 = 文本解析的语义
    assert base.source == "txt"
    assert base.meta == {}
    # 机制只针对这两个字段：其余字段照旧参与比较（不是把 __eq__ 关掉）
    assert base != Scan(path=Path("a-1.txt"), group="b", rep="1", role="sample", t=1.0)
    assert base != Scan(path=Path("a-1.txt"), group="a", rep="1", role="sample", t=2.0)
    # field(default_factory=dict)：两个 Scan 绝不共享同一个 meta dict
    base.meta["mine"] = 1
    assert other.meta == {"format": "fds", "ex_wl_nm": 580.0}


def test_scan_source_and_meta_for_fds_fixture(cfg) -> None:
    """判据②：真实 ``.FDS`` 样例 → ``source=="fds"``，``meta`` 即 io_fds 的元信息。"""
    clear_scan_cache()
    scans = collect_scans(FDS_FIXTURES, cfg, only_paths=[REAL_FDS])
    assert len(scans) == 1 and not scans[0].excluded
    s = scans[0]
    assert s.source == "fds"
    # 键集以 io_fds 的返回为准（不在测试里自造键名）：同一条解析路径必须逐键一致
    expected = read_fds_bytes(REAL_FDS.read_bytes(), fds_anchor_of(cfg["io"]))["meta"]
    assert expected, "io_fds 对真实样例必须给出仪器元信息"
    assert s.meta == expected
    assert s.meta["format"] == "fds"
    assert isinstance(s.meta["ex_wl_nm"], float) and s.meta["ex_wl_nm"] > 0.0
    assert isinstance(s.meta["n_points"], int)


def test_scan_source_is_txt_for_text_samples(cfg) -> None:
    """判据②：文本样例 → ``source=="txt"`` 且不造元信息（本批 TXT 只写 source）。"""
    clear_scan_cache()
    s = collect_scans(SAMPLES_UVVIS, cfg, only_paths=[REAL_SCAN])[0]
    assert not s.excluded
    assert s.source == "txt"
    assert s.meta == {}


def test_scan_source_txt_also_covers_csv(tmp_path, cfg) -> None:
    """设计枚举：``"txt"`` 覆盖 .txt/.csv 两条文本解析路径（本批不发 "csv"/"spc"）。"""
    csv = tmp_path / "sample-1.csv"
    csv.write_text("\n".join(f"{400.0 + i},0.1" for i in range(20)) + "\n",
                   encoding="utf-8")
    clear_scan_cache()
    s = collect_scans(tmp_path, cfg)[0]
    assert not s.excluded
    assert s.source == "txt" and s.meta == {}


def _one_scan(cfg: dict, path: Path, *, use_cache: bool) -> Scan:
    """解析单个文件 → 唯一的 ``Scan``（辅助，避免三处重复 only_paths 样板）。"""
    scans = collect_scans(path.parent, cfg, only_paths=[path], use_cache=use_cache)
    assert len(scans) == 1
    return scans[0]


@pytest.mark.parametrize("use_cache", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("sample", [REAL_FDS, REAL_SCAN], ids=["fds", "txt"])
def test_scan_provenance_identical_on_reparse(cfg, sample, use_cache) -> None:
    """判据③：同文件两次解析 → source/meta 一致；warm 组还须证明第二次真的命中缓存。"""
    clear_scan_cache()
    first = _one_scan(cfg, sample, use_cache=use_cache)
    second = _one_scan(cfg, sample, use_cache=use_cache)
    assert not first.excluded and not second.excluded
    assert (first.source, first.meta) == (second.source, second.meta)
    if use_cache:
        assert scan_cache_stats()["hit"] >= 1, "第二次解析必须命中缓存（本组验的是热路径）"
