# -*- coding: utf-8 -*-
"""V2.6 C 道 FR-35 / A35：Excel 工作簿（.xlsx）导入适配器。

对应《工程设计文档_V2.6_完整版》§5.3 FR-35.1–35.5 与 §7.6 A35.1–A35.6：

* A35.1 与 txt 同构等价：同一组谱分别存 txt / xlsx，开关打开后
  collect_scans 的数值/元数据逐项一致，run_phi_pipeline 产物逐字节一致
  （report/run_params 含数据目录，按目录字符串归一后比较）；
* A35.2 表头/列识别：带表头（英文/中文/整格 nm+abs）、无表头、列名不命中
  → 都正确取两列；非数值行跳过、空 y→NaN；
* A35.3 坏表中文原因：加密不可造 → 用坏 ZIP / 假 ZIP / 单列 / 空表 / 点数不足，
  read_excel_file_ex 返回 (None, 中文)、collect_scans 剔除且不抛 traceback、
  失败不入解析缓存；
* A35.4 开关与扫描域：默认关 .xlsx 不被扫描；打开后 scan_extensions 含 .xlsx、
  _io_key 随开关变化（缓存键失效）；
* A35.5 既有格式零影响：默认关时 scan_extensions 与既有 txt/csv/fds/spc 一致，
  删除 io.excel 节（旧 config.yaml）行为等价；
* A35.6 红线：零新增 CLI 旗标、默认配置 DS1 值（enable=False）、交付 config.yaml
  不被重写（SHA 不变）、零 PDF；A1/A2/A15 门禁复跑由收口门禁覆盖。

测试只在 tmp_path 内造文件；需要 openpyxl（缺失则 skip）。
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import zipfile
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_UVVIS = PROJECT_ROOT / "samples" / "uvvis"
CONFIG_YAML = PROJECT_ROOT / "config.yaml"


# ═══════════════════════ 通用工具 ═══════════════════════
def _io_excel():
    import core.inputs.io_excel as m
    return m


def _io():
    import core.inputs.io as m
    return m


def _openpyxl():
    return pytest.importorskip("openpyxl")


def _is_zh(text) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def _write_xlsx(path: Path, rows, *, sheet: str = "Sheet1") -> Path:
    """把 rows（列表的列表）写成单 sheet xlsx（数值/字符串原样）。"""
    Workbook = _openpyxl().Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(list(row))
    wb.save(str(path))
    wb.close()
    return path


def _make_spectrum_rows(n: int = 41, *, base: float = 0.2, wl0: float = 400.0):
    """两列数值行：波长 400..(400+n-1)、吸光度 base*exp 形状（全部数值）。"""
    wl = np.arange(wl0, wl0 + n, 1.0)
    a = base * np.exp(-0.5 * ((wl - (wl0 + n * 0.6)) / 12.0) ** 2)
    # openpyxl 浮点序列化约 16 位有效数字；统一取到 12 位有效数字，
    # 使 xlsx 往返后 double 与 Python 侧逐位相同（A35.2 的严格相等断言需要）。
    a = [float(format(float(v), ".12g")) for v in a]
    return [[float(w), float(v)] for w, v in zip(wl, a)]


def _cfg_excel(enabled: bool):
    from core.config import default_config
    cfg = default_config()
    cfg["io"].setdefault("excel", {})
    cfg["io"]["excel"]["enable"] = bool(enabled)
    cfg["io"]["excel"].setdefault("extensions", [".xlsx"])
    return cfg


def _scan_array(scan):
    return np.asarray(scan.wl, float), np.asarray(scan.a, float)


def _canon(value):
    """results dict → 可比较的规范 JSON 串（numpy/NaN/元组统一）。"""
    import json

    def _conv(v):
        if isinstance(v, dict):
            return {str(k): _conv(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_conv(x) for x in v]
        if isinstance(v, np.ndarray):
            return v.tolist()
        if isinstance(v, (np.floating, float)):
            return None if not np.isfinite(float(v)) else float(v)
        if isinstance(v, (np.integer, int)):
            return int(v)
        return v

    return json.dumps(_conv(value), sort_keys=True, ensure_ascii=False, default=str)


# ═══════════════════════ A35.1 与 txt 同构等价 ═══════════════════════
def _build_equiv_dirs(tmp_path: Path):
    """同一批谱 → 同一数据分别写 txt / xlsx（两边都可被解析，数值逐位相同）。

    openpyxl 写浮点约保留 16 位有效数字，17 位 repr 的 double 会丢最后 1 ulp；
    这里先把数据统一取到 12 位有效数字再双写，保证 txt 与 xlsx 解析出的 double
    逐位一致（A35.1 的同构等价断言需要）。只保留吸光度全有限的谱：全 NaN 列在
    xlsx 里无法被识别为第二个数值列（txt 则仍可保留 NaN 列）。
    """
    from core.inputs.io import collect_scans

    scans = [s for s in collect_scans(SAMPLES_UVVIS, _cfg_excel(False),
                                      use_cache=False)
             if np.isfinite(np.asarray(s.a, float)).all()]
    assert len(scans) >= 5, f"可等价的样本谱不足：{len(scans)}（样本目录 {SAMPLES_UVVIS}）"
    txt_dir = tmp_path / "txt"
    xlsx_dir = tmp_path / "xlsx"
    txt_dir.mkdir()
    xlsx_dir.mkdir()
    for s in scans:
        wl = [format(float(w), ".6g") for w in np.asarray(s.wl, float)]
        a = [format(float(v), ".12g") for v in np.asarray(s.a, float)]
        (txt_dir / (s.path.stem + ".txt")).write_text(
            "\n".join(f"{w},{v}" for w, v in zip(wl, a)) + "\n",
            encoding="utf-8", newline="\n")
        _write_xlsx(xlsx_dir / (s.path.stem + ".xlsx"),
                    [[float(w), float(v)] for w, v in zip(wl, a)])
    return txt_dir, xlsx_dir, len(scans)


def test_a35_1_scan_identity_between_txt_and_xlsx(tmp_path) -> None:
    """A35.1：同一组谱 → xlsx 与 txt 解析出的 wl/a **逐位**一致、元数据一致。"""
    pytest.importorskip("openpyxl")
    from core.inputs.io import collect_scans
    txt_dir, xlsx_dir, n = _build_equiv_dirs(tmp_path)
    scans_txt = collect_scans(txt_dir, _cfg_excel(False), use_cache=False)
    scans_x = collect_scans(xlsx_dir, _cfg_excel(True), use_cache=False)
    assert len(scans_txt) == n and len(scans_x) == n, (
        f"解析条数不一致：txt={len(scans_txt)} xlsx={len(scans_x)} 期望={n}")
    by_stem = {s.path.stem: s for s in scans_x}
    problems = []
    for s_t in scans_txt:
        s_x = by_stem.get(s_t.path.stem)
        if s_x is None:
            problems.append(f"{s_t.path.name}: xlsx 缺少同 stem 文件")
            continue
        wl_t, a_t = _scan_array(s_t)
        wl_x, a_x = _scan_array(s_x)
        if not np.array_equal(wl_t, wl_x):
            problems.append(f"{s_t.path.name}: 波长不一致")
        if not np.array_equal(a_t, a_x, equal_nan=True):
            problems.append(f"{s_t.path.name}: 吸光度不一致")
        if s_x.source != "xlsx" or s_t.source != "txt":
            problems.append(f"{s_t.path.name}: source 异常 {s_x.source}/{s_t.source}")
        if (s_x.group, s_x.rep, s_x.role, float(s_x.t)) != (
                s_t.group, s_t.rep, s_t.role, float(s_t.t)):
            problems.append(f"{s_t.path.name}: 组/重复/角色/时间不一致")
    assert not problems, "A35.1 同构解析不一致：" + "；".join(problems)


def _norm_bytes(blob: bytes, replace: dict) -> bytes:
    text = blob.decode("utf-8", errors="replace")
    for old, new in replace.items():
        text = text.replace(str(old), new)
    return text.encode("utf-8")


#: 两条流水线产物中必然不同、但与数据无关的动态字段（时间戳/数据目录/run 标识）
_DYNAMIC_KEYS = frozenset({"generated_at", "written_at", "since", "data_dir",
                           "run_id"})


def _strip_dynamic(value):
    if isinstance(value, dict):
        return {str(k): _strip_dynamic(v) for k, v in value.items()
                if str(k) not in _DYNAMIC_KEYS}
    if isinstance(value, (list, tuple)):
        return [_strip_dynamic(v) for v in value]
    return value


def _load_canon(path: Path):
    """读 json/yaml → 递归去掉时间戳/数据目录/run_id 后的规范结构。"""
    import json
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    elif suffix in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        return None
    return json.dumps(_canon(_strip_dynamic(data)), sort_keys=True)


def _norm_report(blob: bytes, replace: dict) -> str:
    """report.md 归一：数据目录/run 目录/生成时间行/文件名扩展名 → 占位符。

    文件名扩展名（.txt vs .xlsx）与 Scan.source 是"同一组谱两种存法"的必然
    格式差异（Lead 2026-10-19 裁决），归一化它们不涉及任何数值判据。
    """
    import re
    text = blob.decode("utf-8", errors="replace")
    for old, new in replace.items():
        text = text.replace(str(old), new)
    text = re.sub(r"生成时间：[^\n]*", "生成时间：<TIME>", text)
    # report.md 的「警告汇总」每条也带 HH:MM:SS 时间戳 → 统一归一
    text = re.sub(r"\d{2}:\d{2}:\d{2}", "<TIME>", text)
    text = re.sub(r"\.xlsx\b", "<EXT>", text)
    text = re.sub(r"\.txt\b", "<EXT>", text)
    return text


def _norm_ext(text: str) -> str:
    """JSON/文本里的文件名扩展名归一（只抹掉 .txt/.xlsx 格式差异）。"""
    return text.replace(".xlsx", "").replace(".txt", "")


def test_a35_1_pipeline_products_are_byte_identical(tmp_path) -> None:
    """A35.1：txt 与 xlsx 两组同构谱 → run_phi_pipeline 数据产物逐字节一致。"""
    pytest.importorskip("openpyxl")
    from core.analysis.pipeline import run_phi_pipeline

    txt_dir, xlsx_dir, _n = _build_equiv_dirs(tmp_path)
    cfg_txt = _cfg_excel(True)          # 两边都用同一份 cfg（开关打开）
    cfg_xlsx = _cfg_excel(True)
    out_t = tmp_path / "runs_txt"
    out_x = tmp_path / "runs_xlsx"
    rr_t = run_phi_pipeline(txt_dir, cfg_txt, out_root=out_t, write=True,
                            export_mode="preview", only=["a"])
    rr_x = run_phi_pipeline(xlsx_dir, cfg_xlsx, out_root=out_x, write=True,
                            export_mode="preview", only=["a"])
    assert rr_t.ok and rr_x.ok, (rr_t.message, rr_x.message)
    assert rr_t.run_dir is not None and rr_x.run_dir is not None

    files_t = {p.relative_to(rr_t.run_dir): p
               for p in rr_t.run_dir.rglob("*") if p.is_file()}
    files_x = {p.relative_to(rr_x.run_dir): p
               for p in rr_x.run_dir.rglob("*") if p.is_file()}
    # run.log（耗时/顺序）、summary xlsx（ZIP 时间戳 + run_id 列）不作数据对比对象；
    # 其余同名产物必须一致：results.csv / 图件 PNG 逐字节；
    # fit_summary.json / run_params.yaml 去时间戳/数据目录/文件名扩展名后严格相等；
    # report.md 归一数据目录/run 目录/生成时间/文件名扩展名后相等。
    # 归一化理由（Lead 裁决）：同一组谱存 txt 与 xlsx 时文件名扩展名与 Scan.source
    # 必然不同，这是格式差异；所有**数值字段**仍严格相等，不是放宽数值判据。
    ignore = {Path("run.log")}
    rels = sorted(set(files_t) - ignore)
    assert rels == sorted(set(files_x) - ignore), (
        f"产物文件集合不一致：{sorted(set(files_t) ^ set(files_x))}")
    replace_t = {str(txt_dir): "<DATA_DIR>", str(xlsx_dir): "<DATA_DIR>",
                 str(out_t): "<OUT_ROOT>", str(out_x): "<OUT_ROOT>",
                 str(rr_t.run_dir): "<RUN_DIR>", rr_t.run_dir.name: "<RUN_DIR>"}
    replace_x = {str(xlsx_dir): "<DATA_DIR>", str(txt_dir): "<DATA_DIR>",
                 str(out_x): "<OUT_ROOT>", str(out_t): "<OUT_ROOT>",
                 str(rr_x.run_dir): "<RUN_DIR>", rr_x.run_dir.name: "<RUN_DIR>"}
    problems = []
    for rel in rels:
        name = rel.name
        suffix = rel.suffix.lower()
        bt = files_t[rel].read_bytes()
        bx = files_x[rel].read_bytes()
        if suffix == ".xlsx":
            # summary xlsx：ZIP 时间戳 + dyes 表 run_id 列必然不同；
            # 其数据内容由 fit_summary/results.csv 已严格断言 → 跳过字节对比
            continue
        if name in ("fit_summary.json", "run_params.yaml"):
            if _norm_ext(_load_canon(files_t[rel])) != _norm_ext(
                    _load_canon(files_x[rel])):
                problems.append(f"{rel}（去动态字段/扩展名后数值仍不一致）")
        elif suffix in (".png", ".csv", ".json"):
            if bt != bx:
                problems.append(f"{rel} 字节不一致")
        elif suffix in (".md", ".yaml", ".yml", ".txt"):
            if name == "report.md":
                rt, rx = _norm_report(bt, replace_t), _norm_report(bx, replace_x)
                if rt != rx:
                    import difflib
                    diff = list(difflib.unified_diff(
                        rt.splitlines(), rx.splitlines(), "txt", "xlsx",
                        lineterm="", n=1))[:12]
                    problems.append("report.md（归一后仍不一致）：\\n"
                                    + "\\n".join(diff))
            elif _norm_bytes(bt, replace_t) != _norm_bytes(bx, replace_x):
                problems.append(f"{rel}（归一后仍不一致）")
        elif bt != bx:
            problems.append(f"{rel} 字节不一致")
    assert not problems, "A35.1 流水线产物不一致：" + "；".join(problems)

    # Lead 裁决口径（显式复核）：results.csv 行数/组列一致、数值列逐行严格相等；
    # 文件名列允许 .txt/.xlsx 扩展名差异（本样本 results.csv 无文件名列 → 逐字节同）。
    import csv as _csv

    def _csv_rows(path: Path):
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            return list(_csv.reader(f))

    rows_t = _csv_rows(files_t[Path("results.csv")])
    rows_x = _csv_rows(files_x[Path("results.csv")])
    assert rows_t[0] == rows_x[0], "results.csv 表头不一致"
    assert len(rows_t) == len(rows_x) and len(rows_t) >= 2, (
        f"results.csv 行数不一致：{len(rows_t)} vs {len(rows_x)}")
    for ra, rb in zip(rows_t[1:], rows_x[1:]):
        assert _norm_ext(str(ra)) == _norm_ext(str(rb)), (
            f"results.csv 数值行不一致：{ra} vs {rb}")

    # 失败/成功图目录都在 run_dir 下：预览档只出 a，但至少 1 张图
    figs_t = sorted(files_t)
    assert any(p.suffix.lower() == ".png" for p in figs_t), "流水线没有产出图片"
    assert not list(out_t.rglob("*.pdf")) and not list(out_x.rglob("*.pdf"))


# ═══════════════════════ A35.2 表头/列识别 ═══════════════════════
def test_a35_2_header_detection_three_variants(tmp_path) -> None:
    """A35.2：英文表头/中文表头/无表头三种表都正确取两列。"""
    pytest.importorskip("openpyxl")
    rows = _make_spectrum_rows(31)
    want_wl = np.array([r[0] for r in rows])
    want_a = np.array([r[1] for r in rows])
    cases = {
        "head_en.xlsx": [["Wavelength (nm)", "Absorbance"]] + rows,
        "head_cn.xlsx": [["波长(nm)", "吸光度"]] + rows,
        "head_nm_abs.xlsx": [["nm", "abs"]] + rows,
        "no_head.xlsx": rows,
    }
    read_excel = _io_excel().read_excel_file_ex
    for name, content in cases.items():
        p = _write_xlsx(tmp_path / name, content)
        recs, why = read_excel(p)
        assert recs is not None, f"{name} 解析失败：{why}"
        rec = recs[0]
        assert np.array_equal(np.asarray(rec["wl"], float), want_wl), name
        assert np.array_equal(np.asarray(rec["a"], float), want_a), name
        assert rec.get("source") == "xlsx", rec.get("source")


def test_a35_2_column_fallback_non_numeric_rows_and_nan(tmp_path) -> None:
    """A35.2：表头名不命中 → 取首个数据行前两个数值列；坏行跳过、空 y→NaN。"""
    pytest.importorskip("openpyxl")
    rows = _make_spectrum_rows(21)
    # 第一行非数值 → 当表头跳过；中间插入非数值 / 单列 / 空 y 行
    content = [["列A", "列B"]] + rows[:5] + [
        ["bad-row", "not-a-number"],
        [float(rows[5][0])],                     # 单列 → 跳过
        [float(rows[5][0]), None],               # 空 y → NaN
    ] + rows[5:]
    p = _write_xlsx(tmp_path / "messy.xlsx", content)
    recs, why = _io_excel().read_excel_file_ex(p)
    assert recs is not None, why
    wl = np.asarray(recs[0]["wl"], float)
    a = np.asarray(recs[0]["a"], float)
    assert np.isfinite(wl).all()
    # 非数值 x 行被跳过；单列行与空 y 行保留为 NaN（y 缺失→NaN，符合 FR-35.4）
    assert int(np.isnan(a).sum()) == 2, (
        f"单列/空 y 行应各保留一个 NaN，实际 NaN 数={int(np.isnan(a).sum())}")
    assert wl.size == 23, wl.size            # 21 真实数据行 + 2 个 NaN 行
    finite = np.isfinite(a)
    assert np.array_equal(wl[finite], np.arange(400.0, 421.0)), wl[finite]
    assert np.all(np.diff(wl) >= 0)


def test_a35_2_scan_source_group_time_from_filename(tmp_path) -> None:
    """A35.2：启用后 xlsx 进 collect_scans；Scan.source=xlsx、组/时间按文件名解析。"""
    pytest.importorskip("openpyxl")
    d = tmp_path / "xlsx_dir"
    d.mkdir()
    rows = _make_spectrum_rows(31)
    _write_xlsx(d / "Rhodamine-pbs-TEST-90.xlsx", rows)
    _write_xlsx(d / "Rhodamine-pbs-TEST-180.xlsx", rows)
    scans = _io().collect_scans(d, _cfg_excel(True), use_cache=False)
    assert len(scans) == 2, [s.path.name for s in scans]
    for s in scans:
        assert s.source == "xlsx", s.source
        assert s.group == "Rhodamine-pbs-TEST", s.group
    times = sorted(float(s.t) for s in scans)
    assert times == [90.0, 180.0], times


# ═══════════════════════ A35.3 坏表中文原因 ═══════════════════════
def test_a35_3_bad_workbooks_chinese_reason_without_traceback(tmp_path) -> None:
    """A35.3：坏 ZIP / 假 ZIP / 单列 / 空表 / 点数不足 → (None, 中文)，不抛。"""
    pytest.importorskip("openpyxl")
    mod = _io_excel()
    read_excel = mod.read_excel_file_ex
    cases = []

    bad_zip = tmp_path / "bad_zip.xlsx"
    bad_zip.write_bytes(b"this is not a zip file")
    cases.append(("坏 ZIP", bad_zip))

    fake = tmp_path / "fake.xlsx"
    with zipfile.ZipFile(fake, "w") as zf:
        zf.writestr("hello.txt", "not a workbook")
    cases.append(("假 ZIP（无 xl/workbook.xml）", fake))

    single = tmp_path / "single_col.xlsx"
    _write_xlsx(single, [[float(i)] for i in range(20)])
    cases.append(("单列", single))

    empty = tmp_path / "empty_sheet.xlsx"
    _write_xlsx(empty, [])
    cases.append(("空表", empty))

    few = tmp_path / "few_points.xlsx"
    _write_xlsx(few, _make_spectrum_rows(5))
    cases.append(("点数不足", few))

    failures = []
    for name, path in cases:
        try:
            recs, why = read_excel(path)
        except Exception as err:                      # noqa: BLE001
            failures.append(f"{name}: 抛异常 {type(err).__name__}: {err}")
            continue
        if recs is not None:
            failures.append(f"{name}: 竟解析成功")
        elif not _is_zh(why):
            failures.append(f"{name}: 缺中文原因 {why!r}")
        if mod.is_excel_file(path) and name not in ("单列", "空表", "点数不足"):
            failures.append(f"{name}: is_excel_file 误判 True")
    assert not failures, "坏表处理失败：" + "；".join(failures)


def test_a35_3_bad_tables_not_cached_and_lazy_import(tmp_path, monkeypatch) -> None:
    """A35.3：坏表失败不入缓存；io_excel 模块 import 期不依赖 openpyxl。"""
    pytest.importorskip("openpyxl")
    io_mod = _io()
    d = tmp_path / "mixed"
    d.mkdir()
    _write_xlsx(d / "good.xlsx", _make_spectrum_rows(31))
    bad = d / "bad.xlsx"
    bad.write_bytes(b"PK\x03\x04broken-on-purpose")

    put_keys: list = []
    orig_put = io_mod._cache_put

    def spy_put(key, value):
        put_keys.append(key)
        return orig_put(key, value)

    monkeypatch.setattr(io_mod, "_cache_put", spy_put, raising=False)
    scans = io_mod.collect_scans(d, _cfg_excel(True), use_cache=True)
    good = [s for s in scans if not s.excluded]
    bad_scans = [s for s in scans if s.excluded]
    assert len(good) == 1 and good[0].source == "xlsx", [s.path.name for s in good]
    # 坏 .xlsx（ZIP 魔数但非工作簿）→ is_excel_file=False → 文本回退后剔除；
    # 无论走哪条分支，都必须给中文剔除原因且**不把失败写进解析缓存**。
    assert len(bad_scans) == 1 and _is_zh(bad_scans[0].excluded), (
        f"坏表未按中文原因剔除：{[s.excluded for s in bad_scans]}")
    good_keys = [k for k in put_keys if "good.xlsx" in str(k[0]).lower()]
    bad_keys = [k for k in put_keys if "bad.xlsx" in str(k[0]).lower()]
    assert good_keys, "好表没有写入解析缓存（缓存链路异常）"
    assert not bad_keys, f"坏表被写入解析缓存：{bad_keys}"

    # 结构：openpyxl 只在函数内导入（模块顶层无 import openpyxl）
    src = inspect.getsource(_io_excel())
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            assert "openpyxl" not in module and "openpyxl" not in names, (
                "openpyxl 在模块顶层被导入（应为函数内懒导入）")
    assert "openpyxl" in src.lower()
    assert "def _import_openpyxl" in src


# ═══════════════════════ A35.4 开关与扫描域 ═══════════════════════
def test_a35_4_default_off_xlsx_not_scanned(tmp_path) -> None:
    """A35.4：默认关 → .xlsx 不被扫描（list_scan_files/collect_scans/preview_groups）。"""
    pytest.importorskip("openpyxl")
    d = tmp_path / "only_xlsx"
    d.mkdir()
    _write_xlsx(d / "Rhodamine-0.xlsx", _make_spectrum_rows(31))
    cfg = _cfg_excel(False)
    io_mod = _io()
    assert ".xlsx" not in io_mod.scan_extensions(cfg["io"])
    assert io_mod.list_scan_files(d, cfg) == [], "默认关时 .xlsx 被枚举了"
    assert io_mod.collect_scans(d, cfg, use_cache=False) == []
    assert io_mod.preview_groups(d, cfg) == []


def test_a35_4_enable_extends_scan_domain_and_cache_key() -> None:
    """A35.4：打开后 scan_extensions 含 .xlsx；_io_key 随开关变化。"""
    pytest.importorskip("openpyxl")
    io_mod = _io()
    cfg_off = _cfg_excel(False)
    cfg_on = _cfg_excel(True)
    assert ".xlsx" not in io_mod.scan_extensions(cfg_off["io"])
    assert ".xlsx" in io_mod.scan_extensions(cfg_on["io"])

    key_fn = getattr(io_mod, "_io_key", None) or getattr(io_mod, "io_key", None)
    assert callable(key_fn), "io 模块缺 _io_key/io_key（A35.4 缓存键断言入口）"
    k_off = key_fn(cfg_off["io"])
    k_on = key_fn(cfg_on["io"])
    assert k_off != k_on, f"io_key 没有随 excel.enable 变化：{k_off} vs {k_on}"
    # 旧 cfg（完全没有 io.excel 节）→ 等价默认关
    old_io = dict(cfg_off["io"])
    old_io.pop("excel", None)
    assert key_fn(old_io) == k_off, "缺 io.excel 节时缓存键未等价默认关"


# ═══════════════════════ A35.5 既有格式零影响 ═══════════════════════
def test_a35_5_default_off_existing_formats_unchanged(tmp_path) -> None:
    """A35.5：默认关时既有格式扫描域/结果与旧配置等价（不重写 config.yaml）。"""
    from core.config import DEFAULT_CFG, default_config
    from core.inputs.io import collect_scans, scan_extensions

    default_io = default_config()["io"]
    assert DEFAULT_CFG["io"]["excel"] == {"enable": False,
                                          "extensions": [".xlsx"]}, (
        "DS1 默认值不符：io.excel 必须默认关")
    assert ".xlsx" not in scan_extensions(default_io)
    baseline_exts = {".txt", ".csv", ".fds", ".spc"}
    assert set(scan_extensions(default_io)) == baseline_exts, (
        f"默认扫描域被改变：{scan_extensions(default_io)}")
    # 缺 io.excel 节（旧 config.yaml）与显式 enable=False 完全等价
    old_io = {k: v for k, v in default_io.items() if k != "excel"}
    assert scan_extensions(old_io) == scan_extensions(default_io)

    src = SAMPLES_UVVIS
    if not src.is_dir():                             # pragma: no cover
        pytest.skip("samples/uvvis 不存在")
    a = collect_scans(src, default_config(), use_cache=False)
    cfg_old = default_config()
    cfg_old["io"].pop("excel", None)
    b = collect_scans(src, cfg_old, use_cache=False)
    assert [s.path.name for s in a] == [s.path.name for s in b]
    for sa, sb in zip(a, b):
        assert _canon({"wl": _scan_array(sa)[0], "a": _scan_array(sa)[1]}) == \
               _canon({"wl": _scan_array(sb)[0], "a": _scan_array(sb)[1]})


# ═══════════════════════ A35.6 红线 ═══════════════════════
def test_a35_6_redlines_cli_config_sha_no_pdf(tmp_path) -> None:
    """A35.6：零新增 CLI 旗标；交付 config.yaml 不被重写（SHA 不变）；零 PDF。"""
    from core import cli as cli_mod
    from core.config import KNOWN_PATHS, default_config

    src = inspect.getsource(cli_mod)
    for bad in ("--excel", "--xlsx", "--io-excel"):
        assert bad not in src, f"新增了 CLI 旗标 {bad}（FR-35.5 红线）"
    assert "io_excel" not in src, "CLI 直接引用了 io_excel（应走 io 分发层）"

    # DS1：新键已进 KNOWN_PATHS（从 DEFAULT_CFG 唯一真相展开）
    assert "io.excel.enable" in KNOWN_PATHS
    assert "io.excel.extensions" in KNOWN_PATHS
    assert default_config()["io"]["excel"]["enable"] is False

    # 交付 config.yaml 不被重写：读哈希 → 跑一次只读解析 → 再读哈希
    if CONFIG_YAML.is_file():
        h0 = hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest()
        d = tmp_path / "one_xlsx"
        d.mkdir()
        _write_xlsx(d / "s-0.xlsx", _make_spectrum_rows(31))
        _io().collect_scans(d, _cfg_excel(True), use_cache=False)
        h1 = hashlib.sha256(CONFIG_YAML.read_bytes()).hexdigest()
        assert h1 == h0, "交付 config.yaml 被重写了（SHA 变化）"
    # 零 PDF：xlsx 解析/导出链路不得产生 PDF
    assert not list(tmp_path.rglob("*.pdf"))
