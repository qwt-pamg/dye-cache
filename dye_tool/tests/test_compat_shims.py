# -*- coding: utf-8 -*-
"""兼容垫片契约测试（V2.x 审查 R3/M4：子包 `_EXPORTS` 精简后）。

V2.0 δ 批把 ``core/`` 分子包，靠**两层垫片**保证 500+ 旧调用点零改动：

1. 根 ``core/__init__.py`` 的 ``_SUBPACKAGE_OF`` + ``sys.modules`` 别名：
   ``core.<旧模块名>`` → ``core.<子包>.<旧模块名>``，**同一对象**；
2. 子包 ``__init__.py`` 的 ``_MODULES``：``from core.<子包> import <模块名>`` 可用。

V2.x 审查 M4/R3 删掉了第三层——子包**符号转发**（``_EXPORTS``，约 650 行）。
本文件把"删了之后哪些必须仍然成立"钉住，防止以后有人又把它加回来/误删 _MODULES。
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "core"

#: 与 ``core/__init__.py`` 的 ``_SUBPACKAGE_OF`` 必须逐项一致（这里独立抄一份，
#: 就是为了"两边漂移"时立刻变红——审查 R3 的"防未来重命名漂移"）。
MOVED = {
    "inputs": ("io", "io_fds", "io_spc", "batch_file"),
    "analysis": ("qc", "kinetics", "phi", "grouping", "spectra", "extinction",
                 "wavelength_table", "qc_dashboard", "pipeline"),
    "render": ("plotting", "plotting_jacs", "plotstyle", "parallel_render"),
    "report": ("results", "excel_export", "dyes", "reorg_runs", "synthetic",
               "explain"),
}

#: 生在子包里的新模块（没有旧扁平别名，但必须出现在子包 _MODULES/__all__ 里；
#: V2.4 FR-27 layout / V2.5 FR-30 summary）。`_SUBPACKAGE_OF` 里不应有它们。
born_in_subpackage = {
    "inputs": {"io_excel"},                          # V2.6 C 道
    "analysis": {"fluorescence_qy", "photobleach", "peaks"},  # V2.6 G 道 / V2.7 A 道
    "render": {"layout", "photophysics", "workbench_plot"},  # V2.4/V2.6/V2.9.3
    "report": {"summary", "stokes_table", "characterization"},  # V2.5/V2.6
}


def test_mapping_matches_filesystem_and_core_table() -> None:
    """映射表 ↔ 磁盘 ↔ ``core._SUBPACKAGE_OF`` 三方一致。"""
    import core

    flat = {}
    for pkg, mods in MOVED.items():
        for m in mods:
            flat[m] = pkg
            assert (CORE / pkg / f"{m}.py").is_file(), f"缺少 core/{pkg}/{m}.py"
    assert flat == core._SUBPACKAGE_OF, "core 的映射表与文件系统/本测试漂移"
    # 子包里的实际 .py 文件都在映射表里（防止新增模块忘了登记）
    # V2.4 起子包内可以有**新模块**（出生就在子包里的，不需要旧扁平别名）：
    #   core/render/layout.py（拼版导出，FR-27）、core/report/summary.py（V2.5 统计/队列汇总）
    for pkg, mods in MOVED.items():
        files = {p.stem for p in (CORE / pkg).glob("*.py")} - {"__init__"}
        extra = born_in_subpackage.get(pkg, set())
        assert files == set(mods) | extra, \
            f"core/{pkg} 磁盘文件与映射表不一致：{files}"


@pytest.mark.parametrize("pkg,mods", sorted(MOVED.items()))
def test_old_flat_path_is_identical_object(pkg: str, mods) -> None:
    for m in mods:
        old = importlib.import_module(f"core.{m}")
        new = importlib.import_module(f"core.{pkg}.{m}")
        assert old is new, f"core.{m} 与 core.{pkg}.{m} 不是同一模块对象"


@pytest.mark.parametrize("pkg,mods", sorted(MOVED.items()))
def test_subpackage_module_members_importable(pkg: str, mods) -> None:
    """``from core.<子包> import <模块名>`` 仍可用（含生于子包的新模块，D3）。"""
    for m in list(mods) + sorted(born_in_subpackage.get(pkg, ())):
        got = getattr(importlib.import_module(f"core.{pkg}"), m)
        assert got is importlib.import_module(f"core.{pkg}.{m}")


@pytest.mark.parametrize("pkg", sorted(MOVED))
def test_subpackage_does_not_re_export_symbols(pkg: str) -> None:
    """R3：子包**不再**转发成员模块的符号；报错必须带"完整模块路径"指引。"""
    mod = importlib.import_module(f"core.{pkg}")
    assert not hasattr(mod, "_EXPORTS"), "M4/R3 的 _EXPORTS 又被加回来了"
    extra = {"inputs": "Scan", "analysis": "despike",
             "render": "JACS_SCHEMA", "report": "atomic_write"}[pkg]
    with pytest.raises(AttributeError) as ei:
        _ = getattr(mod, extra)
    assert "完整模块路径" in str(ei.value)


def test_subpackage_all_is_only_module_names() -> None:
    """``from core.<子包> import *`` 只带出模块名（不再泄漏成员模块内部符号）。"""
    for pkg, mods in MOVED.items():
        mod = importlib.import_module(f"core.{pkg}")
        extra = born_in_subpackage.get(pkg, set())
        assert set(mod.__all__) == set(mods) | set(extra)


def test_root_still_exposes_subpackages_and_root_modules() -> None:
    """``core.inputs`` 等前缀与根模块（config/paths/cli）都还能按老写法访问。"""
    import core
    for pkg in MOVED:
        assert hasattr(core, pkg)
    for m in ("config", "paths", "cli"):
        assert importlib.import_module(f"core.{m}").__name__ == f"core.{m}"
