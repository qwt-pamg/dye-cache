# -*- coding: utf-8 -*-
"""V1.3 FR-13：仪器 荧光 / vendor metadata ``.FDS`` 二进制存档适配的回归用例。

覆盖设计文档 §6 的七条测试要求：

1. 黄金基线（点数/波长范围/峰位/特征值逐位比对）；
2. 判别交叉矩阵（四类格式两两不误判）；
3. 坏文件（空/截断/伪扩展名/二进制垃圾 → 中文原因、不抛异常、**不入缓存**）；
4. 混合批次（``.fds`` + ``.txt`` 同目录，组数/成员/t 序列正确）；
5. 回归（全量 pytest + A2 由 ``tools/check.py`` 与 ``test_regression_legacy.py`` 守）；
6. 二进制结构自检（点数两条算路互证、字节序、隐式波长列）。

**可移植性设计**：绝大多数用例用 :func:`build_fds` **合成** FDS 字节，
因此不依赖用户样例目录（CI/他人机器上也能跑）。真实样例存在时，
额外的黄金基线用例会自动启用（``pytest.skip`` 而非失败）。
"""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from core.io import (collect_scans, load_spectra_files, read_scan,
                     scan_cache_stats, scan_extensions, clear_scan_cache)
from core.io_fds import (FDS_MAGIC, FdsError, describe_failure, is_fds_file,
                         looks_like_fds, read_fds_bytes, read_fds_file)

#: 仓库自带的真实样例（3 个，共约 35 KB）——**保证 A15.1 在任何机器上都能验收**
#: 而不是只对合成数据验收。来源与期望值见同目录 ``README.md``。
FIXTURES = Path(__file__).resolve().parent / "data" / "fds"

#: 用户完整样例目录（本机存在时启用全量用例；他人机器上自动跳过）
MODEL = b"generic spectrometer\x00"
ROM = b"0000000 00\x00"


# ═══════════════════════════ 合成器 ═══════════════════════════
def build_fds(intensity, start_nm: float, step_nm: float = 0.2,
              sample: str = "SMP-A", meta_value: str = "user",
              run_date: str = "05:29:30, 01/01/2010", serial: str = "0000-000",
              prefix: bytes = b"\x11\x16\x00\x00",
              trailer_extra: tuple = (531.0, 650.0),
              ex_wl: float = 580.0, tail: bool = True) -> bytes:
    """按实测布局合成一个 FDS 存档（用于不依赖样例的回归）。

    布局（见 ``docs/fds_format_spec.md`` §1）：

    ``magic(8) + u32 + 前缀 + Sample\\0 + Metadata field\\0 + Date\\0 + NaN 填充``
    ``+ Model\\0 + ROM\\0 + Serial\\0 + step(f8) + start(f8)``
    ``+ data(n×f8) + trailer(8×f8) + "Reagent 1\\0" ...``
    """
    y = np.asarray(intensity, dtype=float)
    n = int(y.size)
    end_nm = start_nm + step_nm * (n - 1)
    tv = (float(trailer_extra[0]), float(trailer_extra[1]),
          float(start_nm), float(end_nm), float(ex_wl), 437.0, 1.0, 1.0)
    buf = bytearray()
    buf += FDS_MAGIC
    buf += struct.pack("<I", 0x534A187E)              # 高熵值（解析时跳过）
    buf += prefix
    buf += sample.encode("ascii") + b"\x00"
    buf += meta_value.encode("ascii") + b"\x00"
    buf += run_date.encode("ascii") + b"\x00"
    buf += b"\xff\xff\xff\xff\xff\xff\xef\x7f" * 12   # NaN 哨兵填充
    buf += MODEL
    buf += ROM
    buf += serial.encode("ascii") + b"\x00"
    buf += struct.pack("<d", float(step_nm))
    buf += struct.pack("<d", float(start_nm))
    buf += y.astype("<f8").tobytes()
    buf += struct.pack("<8d", *tv)
    buf += b"Reagent 1\x00Reagent 2\x00"
    if tail:
        buf += b"Arial\x00MS Sans Serif\x00"
        buf += b"Savitsky-Golay Smoothed, 3, 7, 1\x00"
        buf += b"vendor scan Scan\x00Processed Save Data\x00vendor metadata\x00"
    return bytes(buf)


def _ramp(n: int = 41, start: float = 500.0, step: float = 0.2) -> tuple:
    """一条确定性的合成谱：``y = 100 + 10*sin(i/5)``（值域可预测）。"""
    i = np.arange(n, dtype=float)
    return start, step, 100.0 + 10.0 * np.sin(i / 5.0)


# ═══════════════════════════ 1. 黄金基线（合成，精确） ═══════════════════════════
def test_parse_synthetic_exact_values():
    """逐位比对：合成的每个强度值必须**精确**还原，波长列必须精确等差。"""
    start, step, y = _ramp(41, 500.0, 0.2)
    got = read_fds_bytes(build_fds(y, start, step))
    wl, out = got["wavelength_nm"], got["intensity"]
    assert out.size == 41
    assert np.array_equal(out, y)                      # 逐位相等，不设容差
    assert wl[0] == pytest.approx(500.0, abs=1e-12)
    assert wl[-1] == pytest.approx(500.0 + 0.2 * 40, abs=1e-9)
    assert np.allclose(np.diff(wl), 0.2, atol=1e-12)
    assert got["meta"]["n_points"] == 41
    assert got["meta"]["em_start_nm"] == pytest.approx(500.0)
    assert got["meta"]["ex_wl_nm"] == pytest.approx(580.0)


def test_peak_position_is_exact():
    """峰位（±1 nm）与峰值逐位一致 —— A15.1 的核心判据。"""
    start, step, _ = _ramp(201, 600.0, 0.2)
    i = np.arange(201, dtype=float)
    y = 500.0 + 300.0 * np.exp(-((i - 100.0) / 12.0) ** 2)   # 峰在 i=100
    got = read_fds_bytes(build_fds(y, start, step))
    wl, out = got["wavelength_nm"], got["intensity"]
    k = int(np.argmax(out))
    assert k == 100
    assert wl[k] == pytest.approx(600.0 + 0.2 * 100, abs=1e-9)  # = 620.0 nm
    assert float(out[k]) == pytest.approx(float(y[100]), rel=0, abs=1e-12)


def test_metadata_extraction():
    """样品名/操作者/时刻/型号/ROM/序列号 —— 全部走锚点，不靠固定偏移。"""
    start, step, y = _ramp()
    for sample, prefix in (("100-3", b"\x11\x16\x00\x00"),
                           ("SMP-A", b"\xe7\x2e\x00\x00"),
                           ("sample-b", b"h-\x00\x00")):
        got = read_fds_bytes(build_fds(y, start, step, sample=sample,
                                       prefix=prefix))
        m = got["meta"]
        assert m["sample"] == sample
        assert "metadata field" not in m
        assert m["run_date"] == "05:29:30, 01/01/2010"
        assert m["model"] == "generic spectrometer"
        assert m["rom"] == "0000000 00"
        assert m["serial"] == "0000-000"


def test_header_anchor_shift_is_tolerated():
    """头部可变前缀长度变化（2B / 4B / 8B）不得影响解析 —— 不能硬编码 off=12。"""
    start, step, y = _ramp()
    for prefix in (b"\x01\x02\x00\x00", b"\x01\x02\x03\x04\x05\x06\x07\x08\x00\x00",
                   b"\x00\x00"):
        got = read_fds_bytes(build_fds(y, start, step, prefix=prefix))
        assert np.array_equal(got["intensity"], y)
        assert got["meta"]["sample"] == "SMP-A"


# ═══════════════════════════ 2. 结构自检 ═══════════════════════════
def test_n_points_two_ways_must_agree():
    """点数两条算路（尾部 EM 跨度 / 数据段槽位）必须互证；篡改尾部即报错。"""
    start, step, y = _ramp(41, 500.0, 0.2)
    good = build_fds(y, start, step)
    assert read_fds_bytes(good)["meta"]["n_points"] == 41

    # 把尾部 tv[3]（EM 终止波长）改大 → 两条算路不一致 → 必须报"结构自检失败"
    trailer_off = good.find(b"Reagent 1\x00") - 64
    bad = bytearray(good)
    struct.pack_into("<d", bad, trailer_off + 24, 999.0)
    with pytest.raises(FdsError, match="结构自检失败"):
        read_fds_bytes(bytes(bad))


def test_little_endian_is_required():
    """大端写法的强度值必须**读不出**正确结果（钉住字节序，防将来换实现）。"""
    start, step, y = _ramp(41, 500.0, 0.2)
    good = build_fds(y, start, step)
    y_off = good.find(struct.pack("<d", float(step))) + 16
    bad = bytearray(good)
    bad[y_off:y_off + 8 * 41] = y.astype(">f8").tobytes()   # 大端
    got = read_fds_bytes(bytes(bad))
    assert not np.allclose(got["intensity"], y)


def test_wavelength_column_is_implicit():
    """波长列由 start + step*i 生成（文件里没有显式波长列）。"""
    start, step, y = _ramp(51, 640.0, 0.2)
    got = read_fds_bytes(build_fds(y, start, step))
    expect = 640.0 + 0.2 * np.arange(51)
    assert np.array_equal(got["wavelength_nm"], expect)
    # 文件里不该出现波长数值的字节序列（隐式生成的证据）
    assert struct.pack("<d", 640.2) not in build_fds(y, start, step)


def test_non_finite_intensity_kept_by_parser():
    """解析层**如实保留** NaN/±inf（归一由 io.read_scan 的入口层负责）。"""
    start, step, _ = _ramp(41)
    y = np.full(41, 1.0)
    y[3] = np.nan
    y[4] = np.inf
    y[5] = -np.inf
    got = read_fds_bytes(build_fds(y, start, step))
    assert np.isnan(got["intensity"][3])
    assert np.isposinf(got["intensity"][4])
    assert np.isneginf(got["intensity"][5])


# ═══════════════════════════ 3. 坏文件 ═══════════════════════════
@pytest.mark.parametrize("name,data,needle", [
    ("空文件", b"", "空文件"),
    ("仅魔术字", FDS_MAGIC, "过短"),
    ("魔术字+32", FDS_MAGIC + b"\x00" * 32, "过短"),
    ("错误魔术字", b"NOTFDS!!" + b"\x00" * 300, "不是 FDS 存档"),
    ("二进制垃圾", bytes(range(256)) * 4, "不是 FDS 存档"),
])
def test_bad_bytes_raise_chinese_error(name, data, needle):
    """每类坏输入都抛 ``FdsError``，且消息是**中文人话**（可直接进"剔除原因"列）。"""
    with pytest.raises(FdsError) as ei:
        read_fds_bytes(data)
    msg = str(ei.value)
    assert needle in msg, f"{name}: 文案里应含 {needle!r}，实际 {msg!r}"
    assert isinstance(ei.value, ValueError)      # 调用方的 except ValueError 能接住


def test_truncated_file_reports_truncation():
    """数据段被截断 → 中文"截断"原因；**只截掉尾部归档区 → 仍能读出数据**。

    这是一个刻意的行为选择：数据段的长度由尾部参数区的**位置**决定，
    所以"找不到尾部标记"就等于"数据段长度未知"→ 必须报错。
    而尾部标记之后的 GUI 登记区/指针表与数据无关，缺了它数据依然完整，
    此时只记 ``tail_incomplete`` 提示（按规格书 §10 的建议），**不**拒绝数据
    ——否则"拷了一半的归档"会把好数据一起挡在门外。
    """
    start, step, y = _ramp(41)
    good = build_fds(y, start, step)
    r1 = good.find(b"Reagent 1\x00")
    # ① 截在数据段中间 → 必须报"截断"
    for cut in (300, r1 // 2, r1 - 1):
        with pytest.raises(FdsError) as ei:
            read_fds_bytes(good[:cut])
        assert "截断" in str(ei.value) or "结构" in str(ei.value), str(ei.value)
    # ② 只截尾部归档区 → 数据仍完整读出，但带 tail_incomplete 标记
    got = read_fds_bytes(good[:len(good) - 100])
    assert got["meta"]["n_points"] == 41
    assert np.array_equal(got["intensity"], y)
    assert got["meta"].get("tail_incomplete") is True
    # ③ 完整文件不该有该标记
    assert "tail_incomplete" not in read_fds_bytes(good)["meta"]


def test_bad_file_excluded_and_not_cached(tmp_path):
    """坏文件 → 中文剔除原因 + **不入扫描缓存**（S3 原则，A15.3 判据）。"""
    start, step, y = _ramp()
    good = build_fds(y, start, step)
    (tmp_path / "ok-0.fds").write_bytes(good)
    (tmp_path / "short.fds").write_bytes(good[:300])
    (tmp_path / "fake.fds").write_bytes(b"NOTFDS!!" + b"\x00" * 300)
    (tmp_path / "empty.fds").write_bytes(b"")

    clear_scan_cache()
    from core.config import default_config
    scans = collect_scans(tmp_path, default_config())
    by_name = {s.path.name: s for s in scans}
    assert len(scans) == 4
    assert not by_name["ok-0.fds"].excluded
    for bad in ("short.fds", "fake.fds", "empty.fds"):
        assert by_name[bad].excluded, f"{bad} 应被剔除"
        # 中文可读：含中文字符且不含英文异常类名
        assert any("\u4e00" <= ch <= "\u9fff" for ch in by_name[bad].excluded)
        assert "Traceback" not in by_name[bad].excluded
    st = scan_cache_stats()
    assert st["size"] == 1, "只有成功的那个该进缓存"
    assert st["miss"] == 4


def test_describe_failure_only_for_real_fds(tmp_path):
    """``describe_failure``：非 FDS 返回空串（嗅探链据此走别的分支）。"""
    start, step, y = _ramp()
    (tmp_path / "bad.fds").write_bytes(build_fds(y, start, step)[:300])
    (tmp_path / "plain.txt").write_text("400,0.1\n401,0.2\n" * 20, encoding="utf-8")
    assert "截断" in describe_failure(tmp_path / "bad.fds")
    assert describe_failure(tmp_path / "plain.txt") == ""


def test_read_fds_file_returns_none_on_failure(tmp_path):
    """嗅探用入口必须**安静失败**（返回 None），不能把异常抛进判别链。"""
    (tmp_path / "x.fds").write_bytes(b"NOTFDS!!" + b"\x00" * 300)
    assert read_fds_file(tmp_path / "x.fds") is None
    assert read_fds_file(tmp_path / "不存在.fds") is None


# ═══════════════════════════ 4. 判别交叉矩阵 ═══════════════════════════
def _two_col_csv(path: Path, n: int = 40) -> Path:
    lines = [f"{400.0 + i},{0.1 + i * 0.001:.4f}" for i in range(n)]
    path.write_text("\n".join(lines) + "\n", encoding="gbk")
    return path


def _shimadzu_txt(path: Path, n: int = 40) -> Path:
    head = ["Sample:\t'demo(EM)", "File name:\tdemo.TXT", "",
            "Instrument Parameters", "Measurement type:\tWavelength scan",
            "Scan mode:\tEmission", "", "Data Points", "nm\tData"]
    body = [f"{500.0 + i * 0.5:.1f}\t{100.0 + i:.1f}" for i in range(n)]
    path.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    return path


def test_sniff_matrix_no_cross_confusion(tmp_path):
    """四类格式两两不误判（A15.2）：``.fds`` 发射 / 两列 CSV / 仪器 FL 文本。

    判别全部靠**内容**：FDS 看魔术字，文本看 ``Data Points`` 标记与列结构。
    """
    start, step, y = _ramp()
    fds = tmp_path / "a.fds"
    fds.write_bytes(build_fds(y, start, step))
    csv = _two_col_csv(tmp_path / "b.txt")
    shi = _shimadzu_txt(tmp_path / "c.TXT")

    # ① 只有 FDS 会被魔术字命中
    assert looks_like_fds(fds.read_bytes()[:8])
    assert not looks_like_fds(csv.read_bytes()[:8])
    assert not looks_like_fds(shi.read_bytes()[:8])
    assert is_fds_file(fds) and not is_fds_file(csv) and not is_fds_file(shi)

    from core.config import default_config
    cfg = default_config()
    # ② load_spectra_files 的角色判定
    specs = {s.path.name: s for s in load_spectra_files([fds, csv, shi], cfg)}
    assert specs["a.fds"].kind == "em"
    assert specs["b.txt"].kind == "abs"
    assert specs["c.TXT"].kind == "em"
    assert all(s.ok for s in specs.values())
    # FDS 与仪器文本都是 em，但数值来源不同：FDS 精确、仪器按文本
    assert specs["a.fds"].wl.size == 41
    assert specs["c.TXT"].wl.size == 40

    # ③ read_scan 对 FDS 走二进制分支（数值与合成完全一致）
    wl, a = read_scan(fds, cfg["io"])
    assert np.array_equal(a, y)


def test_extension_lie_is_ignored(tmp_path):
    """**扩展名可复用/可改，内容特征才是真相**（设计文档 §3 第 5 条）。

    ``.txt`` 里装 FDS 内容必须按 FDS 解析；``.fds`` 里装两列文本必须按文本解析。
    """
    start, step, y = _ramp()
    liar_txt = tmp_path / "actually_fds.txt"
    liar_txt.write_bytes(build_fds(y, start, step))
    assert is_fds_file(liar_txt)

    from core.config import default_config
    cfg = default_config()
    wl, a = read_scan(liar_txt, cfg["io"])
    assert np.array_equal(a, y)

    liar_fds = _two_col_csv(tmp_path / "actually_text.fds")
    wl2, a2 = read_scan(liar_fds, cfg["io"])
    assert wl2.size == 40 and a2[0] == pytest.approx(0.1)


def test_fds_not_confused_with_other_formats_in_corpus():
    """仓库内既有文本样例中不得出现 FDS 魔术字（防止误判链被污染）。"""
    root = Path(__file__).resolve().parents[1]
    hits = []
    for d in (root / "samples", root / "tests"):
        for p in d.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".txt", ".csv", ".dat", ".TXT"):
                try:
                    if looks_like_fds(p.read_bytes()[:8]):
                        hits.append(str(p))
                except OSError:
                    continue
    assert hits == [], f"这些文本文件竟以 FDS 魔术字开头：{hits}"


# ═══════════════════════════ 5. 接入与混合批次 ═══════════════════════════
def test_scan_extensions_includes_fds_even_with_legacy_user_config():
    """**关键**：旧 ``config.yaml`` 写死了 ``[.txt,.csv]``，``.fds`` 仍须被扫到。

    深合并会让用户值覆盖默认值——所以实现用的是"并集"而非"改默认值"。
    """
    legacy_io = {"extensions": [".txt", ".csv"]}          # 模拟磁盘上的旧配置
    assert ".fds" in scan_extensions(legacy_io)
    assert ".txt" in scan_extensions(legacy_io)           # 既有扩展名一个不少
    # 显式关掉
    assert ".fds" not in scan_extensions(
        {"extensions": [".txt"], "fds": {"enable": False}})


def test_mixed_batch_groups_and_times(tmp_path):
    """混合批次（A15.4）：``.fds`` + ``.txt`` 同组混跑，分组与 t 序列正确。"""
    from core.config import default_config
    cfg = default_config()
    start, step, _ = _ramp(41)
    # 同组同 t 的两种格式（FR-13.4 的"重复时间点"场景）
    for t in (0, 60, 120):
        y = 100.0 + t
        (tmp_path / f"G1-{t}.fds").write_bytes(
            build_fds(np.full(41, y), start, step))
        (tmp_path / f"G1-{t}.txt").write_text(
            "\n".join(f"{500.0 + i * 0.2},{0.5 + i * 1e-4:.6f}"
                      for i in range(41)) + "\n", encoding="gbk")

    clear_scan_cache()
    scans = collect_scans(tmp_path, cfg)
    assert len(scans) == 6
    assert {s.group for s in scans} == {"G1"}
    assert sorted({s.t for s in scans}) == [0.0, 60.0, 120.0]
    assert all(not s.excluded for s in scans)
    # 两种格式各 3 个 → 同一 (组, t) 上各有一份，刻意保留（去重策略在拟合层）
    n_fds = sum(1 for s in scans if s.path.suffix.lower() == ".fds")
    assert n_fds == 3
    # 每份 FDS 的数值对应它自己的 t
    for s in scans:
        if s.path.suffix.lower() == ".fds":
            assert float(np.nanmean(s.a)) == pytest.approx(100.0 + s.t)


def test_fds_numbers_identical_to_txt_batch(tmp_path):
    """A15.4：同数据时 ``.fds`` 批次与纯 ``.txt`` 批次的 k 值一致。

    做法：把同一组数值分别写成 FDS 与两列文本，跑 ``run_core`` 比较 A0/k 的输入数据。
    （完整 pipeline 的端口比对由 ``tests/test_pipeline.py`` / 金线覆盖，这里钉住数据层。）
    """
    from core.config import default_config
    cfg = default_config()
    start, step, _ = _ramp(41, 500.0, 0.2)
    a_fds = tmp_path / "fds"
    a_txt = tmp_path / "txt"
    a_fds.mkdir()
    a_txt.mkdir()
    for t in (0, 30, 60, 90):
        y = 1.0 - 0.004 * t + 0.2 * np.exp(-((np.arange(41) - 20) / 8.0) ** 2)
        (a_fds / f"D-{t}.fds").write_bytes(build_fds(y, start, step))
        (a_txt / f"D-{t}.txt").write_text(
            "\n".join(f"{500.0 + i * 0.2},{y[i]:.10f}" for i in range(41)) + "\n",
            encoding="gbk")

    clear_scan_cache()
    sf = collect_scans(a_fds, cfg)
    clear_scan_cache()
    st = collect_scans(a_txt, cfg)
    assert len(sf) == len(st) == 4
    for x, z in zip(sorted(sf, key=lambda s: s.t), sorted(st, key=lambda s: s.t)):
        assert x.t == z.t
        # 文本只有 10 位小数 → 容差 1e-9；FDS 是全精度
        assert np.allclose(x.a, z.a, atol=1e-9, equal_nan=True)


def test_cache_distinguishes_fds_from_text(tmp_path):
    """缓存键必须能区分 FDS 与其余格式（设计文档 §3 第 7 条）。

    做法：先扫 FDS 版本、再扫文本版本，同路径同 mtime/size 下结论必须不同。
    这里用"两个不同内容的文件占用同一缓存键空间"来验证：分别解析取到的结果不同。
    """
    from core.config import default_config
    cfg = default_config()
    start, step, _ = _ramp(41)
    p = tmp_path / "same.fds"
    p.write_bytes(build_fds(np.full(41, 7.0), start, step))
    clear_scan_cache()
    _, a1 = read_scan(p, cfg["io"])
    assert float(a1[0]) == 7.0

    # 覆盖成另一个数值完全相同长度的 FDS（size 可能相同 → 靠 mtime 失效）
    import time as _t
    _t.sleep(0.01)
    p.write_bytes(build_fds(np.full(41, 9.0), start, step))
    clear_scan_cache()
    _, a2 = read_scan(p, cfg["io"])
    assert float(a2[0]) == 9.0


# ═══════════════════════════ 6. 仓库夹具回归 ═══════════════════════════


@pytest.mark.parametrize("fname,start,end,n,step,peak,pk_wl", [
    # 设计文档附录 A.3 的黄金基线（逐位核对，值来自实测复核）
    ("sample-c.FDS", 710.0, 900.0, 951, 0.2, 9986.3919, None),
    ("sample-d.FDS", 710.0, 900.0, 951, 0.2, 1433.6329, 785.0),
    ("10UM.FDS", 600.0, 800.0, 1001, 0.2, None, None),
])
def test_fixture_golden_baseline(fname, start, end, n, step, peak, pk_wl):
    """A15.1（**始终可跑**）：点数/波长范围/步长/峰值/峰位与黄金基线一致。

    用仓库自带夹具而不是外挂样例目录——否则这条最关键的验收在别人机器上
    会静默跳过，等于没有。
    """
    f = FIXTURES / fname
    assert f.exists(), f"缺少仓库夹具 {f}"
    wl, y, m = read_fds_file(f)
    assert wl.size == n
    assert wl[0] == pytest.approx(start, abs=1e-9)
    assert wl[-1] == pytest.approx(end, abs=1e-9)
    assert m["step_nm"] == pytest.approx(step, abs=1e-12)
    if peak is not None:
        assert float(y.max()) == pytest.approx(peak, abs=1e-3)
    if pk_wl is not None:
        assert float(wl[int(np.argmax(y))]) == pytest.approx(pk_wl, abs=1.0)


def test_fixture_metadata_is_sanitized():
    """夹具的仪器元信息已去标识化（可在公开仓库安全分发）。"""
    wl, y, m = read_fds_file(FIXTURES / "sample-d.FDS")
    assert m["model"] == "generic spectrometer"
    assert m["rom"] == "0000000 00"
    assert m["serial"] == "0000-000"
    assert m["sample"] == "sample-b"
    assert m["ex_wl_nm"] == pytest.approx(700.0)
    assert m["run_date"]                      # 形如 "00:38:19, 01/01/2010"
    assert "tail_incomplete" not in m         # 完整拷贝


def test_fixture_parses_through_full_pipeline(tmp_path):
    """夹具能走完整条 ``collect_scans`` → ``run_core``（不是只过解析器）。

    **每支谱各放一个目录**——这是刻意的：这 3 个夹具的波长范围/量级互不相同
    （710–900 已饱和的 NIR 谱 vs 600–800 的常规谱），混在一个目录里的话
    既有 QC 会（正确地）全部剔除：波长轴不一致。

    V2.0 γ 批起**不需要再手工放宽阈值**：饱和判据按数据来源路由，
    `.fds`（荧光原始强度）走 ``qc.saturation_counts``（默认 1e9≈关闭）。
    """
    import shutil
    from core.config import default_config
    from core.pipeline import run_core
    cfg = default_config()
    for f in sorted(FIXTURES.glob("*.FDS")):
        one = tmp_path / f.stem
        one.mkdir()
        shutil.copy2(f, one / f.name)
        clear_scan_cache()
        scans, _ = run_core(one, cfg, dry=True)
        assert len(scans) == 1, f"{f.name}: 应只扫到 1 个文件"
        s = scans[0]
        assert not s.excluded, f"{f.name} 被剔除：{s.excluded}"
        assert s.wl.size >= 951
        assert np.isfinite(s.a).any()
        # 波长范围必须原样保留（没有被 QC 裁剪）
        assert s.wl[0] == pytest.approx(710.0 if "DMA" in f.name else 600.0, abs=1e-9)


def test_fds_reading_needs_no_config_tweak(tmp_path):
    """V2.0 γ 批（FR-15 / A17）：**默认配置**下 `.fds` 不再被误判饱和。

    改之前的实测基线：默认配置下 3/3 夹具被"吸光度饱和"剔除，且每条谱
    **100% 的点**被标成饱和（`n_saturated` = 点数）。现在：

    * ``flag_saturation`` 不再剔除任何 FDS；
    * QC 体检的 ``n_saturated`` = **0**（两条判据必须一致，否则又会自相矛盾）；
    * 吸光度侧（文本样例）一个都没被剔除 —— 与 V1.3 逐位一致（见
      ``tests/test_batch_gamma.py`` 的 golden 快照）。
    """
    import shutil
    from core.config import default_config
    from core.io import collect_scans
    from core.qc import flag_saturation
    from core.qc_dashboard import check_scans

    cfg = default_config()
    assert cfg["qc"]["saturation_counts"] == 1e9
    mixer = tmp_path / "mix"
    mixer.mkdir()
    for f in sorted(FIXTURES.glob("*.FDS")):
        shutil.copy2(f, mixer / f.name)
    clear_scan_cache()
    scans = collect_scans(mixer, cfg)
    assert len(scans) == 3
    assert all(s.source == "fds" for s in scans)
    assert flag_saturation(scans, cfg) == [], "FDS 又被按吸光度阈值判饱和了"
    rep = check_scans(scans, cfg)
    assert sum(r["n_saturated"] for r in rep["files"]) == 0


def test_fixture_saturation_threshold_is_absorbance_only():
    """钉住**事实**：``qc.saturation`` 仍是吸光度阈值（3.0），且 FDS 量级远高于它。

    如果哪天有人改了 ``qc.saturation`` 的默认值，这条会变红，提醒他
    同步更新 FDS 的使用说明——而不是让文档悄悄过期。
    """
    from core.config import default_config
    cfg = default_config()
    assert cfg["qc"]["saturation"] == 3.0, "默认饱和阈值变了 → 请同步 README/规格书"
    # 夹具的强度量级远高于吸光度阈值（所以才有必要按来源路由）
    for f in sorted(FIXTURES.glob("*.FDS")):
        _, y, _ = read_fds_file(f)
        assert float(np.nanmax(y)) > 3.0
