# -*- coding: utf-8 -*-
"""显示层预处理（V2.8 FR-49 成层；原 ui/widgets/plot_prep 搬家，行为零变）。

背景（用户 2026-10-17 实拍反馈）：作��台此前直接画原始数据，T600 谱在 ~380 nm
有一个"单点掉到 0"的尖峰，图上非常显眼。同一判据（`qc.despike`：滚动中位数 ±
k×MAD）在 Φ_Δ 流水线里其实会把它删掉，只是作图台从不调用。

`despike` 的语义是**把异常点置 NaN（剔除）**——直接照搬到作图会留下断点。本模块
在"剔除"之后，只对**被剔除的点**做线性插值补上（视觉上平滑），而**原有 NaN**
（如 t=0 文件的 300–399 nm 真实空段）保持断点，不会被凭空补出来。

约定：只影响作图显示与导出的图；**不修改原始数据文件**，也不参与任何数值分析/拟合。
"""
from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger("dye_tool.ui.prep.display")


def despiked_for_plot(a, wl, qc_cfg: dict, tag: str = ""):
    """返回"去尖峰后"的数组：被 despike 判为异常的点 → 线性插值补点。

    Args:
        a: 吸光度数组（或荧光强度）。
        wl: 波长数组（与 ``a`` 等长）。
        qc_cfg: ``cfg["qc"]``（判据参数，与 Φ_Δ 流水线同源）。
        tag: 日志用标签（文件名）。

    Returns:
        新数组（原数组不被修改）；无异常点时与原数组数值相等。
    """
    from core.analysis.qc import despike

    # A24（V2.x 审查）：本函数是"显示层唯一入口"，但完全没有输入校验——
    # 波长与强度长度不一致 / 非有限波长会在 `despike` 的窗口索引处报
    # IndexError 或让整张图静默变空。这里显式拒绝并退回原数组（作图不中断）。
    y = np.asarray(a, float)
    if y.size == 0:
        return y.copy()
    try:
        x = np.asarray(wl, float)
    except (TypeError, ValueError):
        log.warning("作图去尖峰：波长不是数值数组，已跳过（用原始数据作图）")
        return y.copy()
    if x.shape != y.shape:
        log.warning("作图去尖峰：波长(%s)与强度(%s)长度不一致，已跳过",
                    x.shape, y.shape)
        return y.copy()
    if not np.isfinite(x[~np.isnan(x)]).all() and np.isnan(x).all():
        log.warning("作图去尖峰：波长全为 NaN，已跳过")
        return y.copy()
    had_nan = np.isnan(y)
    out = despike(y.copy(), np.asarray(wl, float), qc_cfg or {}, tag)
    dropped = np.isnan(out) & ~had_nan
    if not dropped.any():
        return out
    valid = ~np.isnan(out)
    if valid.sum() < 2:
        return out
    xs = np.flatnonzero(valid)
    out[dropped] = np.interp(np.flatnonzero(dropped), xs, out[valid])
    return out


def normalize_y(y):
    """显示层归一化（最大峰归一；空/全零/无有限值 → 原样返回）。

    由 SpectraPage._norm_y 的显示逻辑抽出（V2.8 FR-49）；不参与数值分析。
    """
    import numpy as np
    arr = np.asarray(y, float)
    if not arr.size:
        return arr
    finite = np.isfinite(arr)
    if not finite.any():
        return arr
    m = float(np.nanmax(arr))
    return arr / m if m else arr
