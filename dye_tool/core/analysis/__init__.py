# -*- coding: utf-8 -*-
"""``core.analysis`` —— 分析层：QC、拟合、Φ_Δ、分组、紫外/荧光谱、ε、A@λ、体检面板、流水线编排（V2.0 δ 批 FS1 子包化）

成员：:mod:`core.analysis.qc`、:mod:`core.analysis.kinetics`、:mod:`core.analysis.phi`、:mod:`core.analysis.grouping`、:mod:`core.analysis.spectra`、:mod:`core.analysis.extinction`、:mod:`core.analysis.wavelength_table`、:mod:`core.analysis.qc_dashboard`、:mod:`core.analysis.pipeline`

**垫片契约**（V2.0 δ 批 FS1；V2.x 审查 M4/R3 精简后）：
``core.analysis.<模块名>`` 与 ``core.<模块名>`` 都能用，且是**同一个模块对象**
（旧名字由根 :mod:`core` 的 ``_SUBPACKAGE_OF`` + ``sys.modules`` 别名表提供）。

**本文件不再转发符号级名字**（M4：删掉了 ~200 行的 ``_EXPORTS`` 字典）。原因：

* 它把成员模块内部的**私有名**（``np``、``_CACHE_STATS``、``Path``…）泄漏成
  子包公共面，掩盖真实依赖方向——``from core.inputs import np`` 能跑，但没人
  说得清这是"契约"还是"实现细节碰巧同名"；
* 有 300+ 调用点用的是"旧扁平路径"（``from core.io import Scan``），这条路
  由根垫片保证；子包符号转发**只有 2 处**调用点，且都是模块级导入，不需要它。

请写完整模块路径：``from core.analysis.qc import ...``。
"""
from __future__ import annotations

import importlib
from typing import Any

#: 本子包的成员模块（也是 ``__all__``：``from core.analysis import *`` 只带出模块）
#: V2.6 起 ``fluorescence_qy`` / ``photobleach`` 生于本子包（无旧扁平别名），同样登记；
#: V2.7 A 道 ``peaks``（FR-37 峰分析）生于本子包。
_MODULES = ("qc", "kinetics", "phi", "grouping", "spectra", "extinction",
            "wavelength_table", "qc_dashboard", "pipeline", "fluorescence_qy",
            "photobleach", "peaks")


def __getattr__(name: str) -> Any:                       # noqa: D105
    """``core.analysis.<成员模块>`` 惰性导入（子包符号转发已删除，见文件头）。"""
    if name in _MODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}；子包不再转发符号级名字，"
        f"请用完整模块路径（如 from core.analysis.qc import {name}）")


def __dir__() -> list:                                   # noqa: D105
    return sorted(set(list(globals()) + list(_MODULES)))


__all__ = list(_MODULES)
