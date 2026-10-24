# -*- coding: utf-8 -*-
"""dye_tool.core —— 纯计算层（无任何 GUI 依赖，可独立 pytest）。

V2.0 δ 批（FS1）起 ``core/`` 按**子系统**分子包，本文件同时是**兼容垫片**：

    core/io/       输入层：io（文本/CSV）、io_fds、io_spc、batch_file、io_excel（V2.6，生于子包）
    core/analysis/ 分析层：qc、kinetics、phi、grouping、spectra、extinction、
                   wavelength_table、qc_dashboard、pipeline、fluorescence_qy（V2.6）、
                   photobleach（V2.6）（后两者生于子包）
    core/render/   渲染层：plotting（legacy 冻结）、plotting_jacs、plotstyle、
                   parallel_render、layout（V2.4 FR-27）、photophysics（V2.6）（生于子包）
    core/report/   输出与档案：results、excel_export、dyes、reorg_runs、
                   synthetic、explain、summary（V2.5）、stokes_table、characterization（V2.6）
                   （后三者生于子包）
    core/          根：config（DEFAULT_CFG 唯一真相）、paths、cli

**垫片契约**（500+ 调用点零改动，设计文档 FR-18 / 设计稿 §3 阶段 1）：
``core.X`` 既指"子包"也指"原地那份模块"，两条路径都必须能导入：

* ``core.io``        → 子包 :mod:`core.io`（``core.io.Scan`` 由子包 ``__init__`` 再导出）
* ``core.io_fds``    → 模块 :mod:`core.io.io_fds`（本文件 :func:`__getattr__` 转发）
* ``from core.qc import despike`` → 同上转发，**签名与对象同一份**（不是拷贝）

用 ``__getattr__`` 而不是 import 期一次性全加载：包导入**不触碰磁盘**（iron rule 3）、
多进程 spawn 引入 core 的代价不变，且不会因为"某个子系统 import 坏了"导致整个
core 不可用（失败只在真的用到那个模块时暴露，错误信息仍然指得到具体模块）。
"""
from __future__ import annotations

import importlib
import sys
from typing import Any

__all__ = ["VERSION"]

#: 版本号（**唯一真相**；`packaging/build.ps1` 从这里读并写进 exe 属性）。
#: V1.0→V1.3 = 功能增量与 FDS 适配；**V2.0.0 = 一站式重构**（α 地基 / β `.spc` /
#: γ 量纲感知 QC / δ 批次工作区+子包化 / ε 任务流导航 / ζ 收口六批）；
#: **V2.1.0 = V2.4 能力增量**（分析参数模板 + 多面板拼版导出，设计文档 V2.4 §10 Q-A7）。
#: **V2.2.0 = V2.5 能力增量**（批量队列 + 多 run 统计报表 + 交互工作台/实时参数编辑/生图独立，
#: 设计文档 V2.5 §10 Q-D10：新增 core/report/summary.py 等 = 能力增量）。
#: **V2.3.0 = V2.6 能力增量**（UI 分辨率适配 + 参数表格化 + 人体工学二批 + 斯托克斯位移 +
#: 光物理表征 + Excel 工作簿导入；设计文档 V2.6 §10 Q-E7：新增 core/inputs/io_excel.py 等）。
#: **V2.4.0 = V2.7 能力增量**（峰分析 FR-37/A43：新增 core/analysis/peaks.py + ②第 8 卡；
#: 用户 2026-10-20 批准 V2.6 §10 Q-E5 判据；设计文档 V2.7 §10 Q-F7：A 道实施才升版）。
#: **V2.5.0 = V2.8 质量与结构二批**（验收退役 → pytest 唯一门禁；金线/红线迁移；
#: 测试 22.1k→约 10k 行；check.py 两档；FR-48 装配层拆分；FR-49 显示层；
#: FR-53 ③④显示预设；FR-50 幽灵时间盒证据；无新增产品功能 → 次版本号）。
#: **V2.6.0 = V2.9 观察期首批**（幽灵正式结案；FR-60 时间整定 Origin 化：命名可选加速器、
#: t/dup_index 随 dye_batch.yaml 持久化、时间列 s/min/h、自动识别三态；FR-59 甲 QUICKSTART）。
#: **V2.7.0 = V2.9.1 观察期首批（k_obs 并入 Φ_Δ）**（FR-61 参比显式指定 / FR-62 对比链直通 Φ_Δ
#: 行尾增列 / FR-63 Φ_ref 随 dye_batch.yaml；公式层零改动）。
#: **V2.8.0 = V2.9.3**（FR-65 单线态氧工作台 / FR-66 图式 / FR-67 增强 / FR-68 悬停帮助）。
#: **V2.9.0 = V2.9.4**（FR-69 单工作流 / FR-70 设置区缩放 / FR-71 参数帮助补全）。
#: **V2.9.1 = V2.9.5**（FR-72 布局收敛 / FR-73 作图台实时生图预览）。
#: **V2.9.2 = V2.9.6**（全应用悬停说明/不可用原因修复）。
VERSION = "2.9.2"

#: 旧路径 → 新位置的映射（23 个搬进子包的模块）。留在 core 根的
#: config/paths/cli 不在此表——它们本来就是 ``core.X``。
#:
#: ⚠ **子包名是 inputs 而不是设计稿写的 io**：``core/io/``（包）与 ``core/io.py``
#: （模块）在 Python 里不能共存，而 io 层的本体模块就叫 ``io.py``。试过"包 + 另加载
#: 一份 ``core.io._impl``"：内存里两份 io 模块，`is` 与猴子补丁全错位（既有测试靠
#: ``setattr(core_io, "SCAN_CACHE_MAX", 5)`` 模拟缓存封顶）。改名是唯一不牺牲语义的
#: 做法，代价只是子包名。
_SUBPACKAGE_OF: dict[str, str] = {
    # inputs（输入层）
    "io": "inputs", "io_fds": "inputs", "io_spc": "inputs",
    "batch_file": "inputs",
    # analysis
    "qc": "analysis", "kinetics": "analysis", "phi": "analysis",
    "grouping": "analysis", "spectra": "analysis", "extinction": "analysis",
    "wavelength_table": "analysis", "qc_dashboard": "analysis",
    "pipeline": "analysis",
    # render
    "plotting": "render", "plotting_jacs": "render", "plotstyle": "render",
    "parallel_render": "render",
    # report
    "results": "report", "excel_export": "report", "dyes": "report",
    "reorg_runs": "report", "synthetic": "report", "explain": "report",
}

#: 子包列表（``core.inputs`` 等前缀也走 __getattr__，保证"先 import core 再 core.inputs"可用）
_SUBPACKAGES = ("inputs", "analysis", "render", "report")


def __getattr__(name: str) -> Any:                       # noqa: D105
    """把 ``core.<旧模块名>`` 转发到 ``core/<子包>/<模块>.py``（垫片核心）。

    只在**属性真的被访问**时 import（PEP 562）。找不到就抛标准 ``AttributeError``
    ——`from core import nope` 的报错行为与从前一致，不吞错误。
    """
    sub = _SUBPACKAGE_OF.get(name)
    if sub is not None:
        mod = importlib.import_module(f".{sub}.{name}", __name__)
        globals()[name] = mod                            # 缓存，后续访问不再走这里
        return mod
    if name in _SUBPACKAGES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:                                   # noqa: D105
    """把垫片转发出去的模块名也列进来（`dir(core)` 与从前一样全）。"""
    return sorted(set(list(globals()) + list(_SUBPACKAGE_OF) + list(_SUBPACKAGES)))


def _install_aliases() -> None:
    """把**旧模块名**注册进 ``sys.modules``（垫片的关键一步）。

    光有 :func:`__getattr__` 不够：``import core.qc`` 与
    ``from core.qc import despike`` 走的是**导入系统**（``sys.modules`` 查找 +
    ``importlib`` 机制），**不会**回落到模块的 ``__getattr__``。实测症状就是
    ``ModuleNotFoundError: No module named 'core.qc'``（22 个搬家模块全中）。
    所以这里显式导入四个子包，并把 ``core.<旧名>`` 指向 ``core.<子包>.<旧名>``
    ——同一对象，不是拷贝，所以 ``isinstance`` / 猴子补丁 / ``__eq__`` 全不变。

    为什么要**急加载**（而不是纯惰性）：``sys.modules`` 别名必须在"有人 import
    旧路径"之前就位，而旧路径随时可能出现（500+ 调用点、外部脚本、测试替身）。
    代价是 import core 会连带加载四个子包；这本来就是 V1.3 的行为
    （那时 ``from core.qc import ...`` 也是立刻加载），没有增加额外负担。
    """
    for sub in _SUBPACKAGES:
        pkg = importlib.import_module(f".{sub}", __name__)
        for old, mapped in _SUBPACKAGE_OF.items():
            if mapped != sub:
                continue
            mod = getattr(pkg, old, None)
            if mod is None:
                mod = importlib.import_module(f".{sub}.{old}", __name__)
            # 注：V2.3 删掉了原来的 `old == sub` 守卫——子包改名 `inputs` 之后
            # 映射表里已不存在"旧名 == 子包名"的项（`io → inputs`），该分支
            # 永远不会触发，留着只会让下一个人以为还有同名冲突要处理。
            # 只在"旧名还没被别的东西占着"时注册（不覆盖真实存在的模块）
            sys.modules.setdefault(f"{__name__}.{old}", mod)
            globals().setdefault(old, mod)


_install_aliases()
