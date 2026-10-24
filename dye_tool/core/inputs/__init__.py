# -*- coding: utf-8 -*-
"""``core.inputs`` —— 输入层：文本/CSV 解析与扫描缓存（``io``）、仪器 ``.fds``、仪器 ``.spc``、``dye_batch.yaml`` 批次状态（V2.0 δ 批 FS1 子包化）

成员：:mod:`core.inputs.io`、:mod:`core.inputs.io_fds`、:mod:`core.inputs.io_spc`、:mod:`core.inputs.io_excel`、:mod:`core.inputs.batch_file`

**垫片契约**（V2.0 δ 批 FS1；V2.x 审查 M4/R3 精简后）：
``core.inputs.<模块名>`` 与 ``core.<模块名>`` 都能用，且是**同一个模块对象**
（旧名字由根 :mod:`core` 的 ``_SUBPACKAGE_OF`` + ``sys.modules`` 别名表提供）。

**本文件不再转发符号级名字**（M4：删掉了 ~200 行的 ``_EXPORTS`` 字典）。原因：

* 它把成员模块内部的**私有名**（``np``、``_CACHE_STATS``、``Path``…）泄漏成
  子包公共面，掩盖真实依赖方向——``from core.inputs import np`` 能跑，但没人
  说得清这是"契约"还是"实现细节碰巧同名"；
* 有 300+ 调用点用的是"旧扁平路径"（``from core.io import Scan``），这条路
  由根垫片保证；子包符号转发**只有 2 处**调用点，且都是模块级导入，不需要它。

请写完整模块路径：``from core.inputs.io import ...``。
"""
from __future__ import annotations

import importlib
from typing import Any

#: 本子包的成员模块（也是 ``__all__``：``from core.inputs import *`` 只带出模块）
#: V2.6 FR-35：新增 io_excel（Excel 工作簿导入适配器，io._read_scan_ex 按需调用）。
_MODULES = ("io", "io_fds", "io_spc", "io_excel", "batch_file")


def __getattr__(name: str) -> Any:                       # noqa: D105
    """``core.inputs.<成员模块>`` 惰性导入（子包符号转发已删除，见文件头）。"""
    if name in _MODULES:
        mod = importlib.import_module(f".{name}", __name__)
        globals()[name] = mod
        return mod
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}；子包不再转发符号级名字，"
        f"请用完整模块路径（如 from core.inputs.io import {name}）")


def __dir__() -> list:                                   # noqa: D105
    return sorted(set(list(globals()) + list(_MODULES)))


__all__ = list(_MODULES)
