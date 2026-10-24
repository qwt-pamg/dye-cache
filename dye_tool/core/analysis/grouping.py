# -*- coding: utf-8 -*-
"""谱形指纹自动分组（FR-2）。

逐行迁移自 legacy ``compare_kobs.py`` 的自动分组引擎：

    每张谱取"母核区"(默认 450–650 nm, 避开 ABDA 379 nm 探针峰) →
    重采样到固定网格 → 峰值归一化 = 谱形指纹 →
    两两 RMS 距离 ≤ d_max 判为同组（并查集）→ 每组报告母核 λ_max。

    母核区残缺的谱 → 无法归组, 单独列出（``invalid``）。
    ``core_fingerprint`` / ``auto_group`` 保持 legacy 签名，便于回归对比。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger("dye_tool.grouping")


# ════════════════════════ 1. 自动分组引擎 ════════════════════════
def core_fingerprint(wl: np.ndarray, ab: np.ndarray, lo: float, hi: float,
                     step: float) -> tuple[float | None, np.ndarray | None]:
    """母核区谱形指纹 → ``(λ_max, 峰值归一化曲线)``；区域数据不足 → ``(None, None)``。"""
    wl = np.asarray(wl, float)
    ab = np.asarray(ab, float)
    m = (wl >= lo) & (wl <= hi) & np.isfinite(ab)
    if m.sum() < 10:
        return None, None
    grid = np.arange(lo, hi + 1e-9, step)
    fp = np.interp(grid, wl[m], ab[m])
    imax = int(np.argmax(fp))
    if fp[imax] <= 0:
        return None, None
    return float(grid[imax]), fp / fp[imax]


def _dist(a: np.ndarray, b: np.ndarray) -> float:
    """两条指纹的 RMS 距离。"""
    return float(np.sqrt(np.mean((a - b) ** 2)))


#: T3 广播分组时每个分块的**字节预算**（默认 64 MB）。
#: 峰值内存 ≈ ``_PAIR_CHUNK_BYTES``，与谱数量无关——这样 5000 个文件的目录也不会
#: 因为 ``(k, k, m)`` 三维中间数组而 OOM。
_PAIR_CHUNK_BYTES = 64 * 1024 * 1024


def _pair_flags(fps: list, d_max: float) -> "np.ndarray | None":
    """两两"距离 ≤ d_max"的布尔矩阵（只看**双方指纹都有效**的项）。

    T3（评审记录）：原实现是 ``for i / for j`` 的**双重 Python 循环**，
    每对调一次 :func:`_dist` → O(N²) 次 numpy 小调用。实测本机
    **600 项要 1454 ms**（179700 对，约 8.2 µs/对），而 2000 项的目录会到十几秒——
    "选个目录等半天"里的主要部分。这里的做法：

    * 指纹来自 :func:`core_fingerprint` 的**固定网格**，所以长度必然一致 →
      堆成一个 ``(n, m)`` 矩阵；
    * 用广播一次算出全部两两 RMS（``sqrt(mean((x_i - x_j)², axis=1))``）——
      **公式逐字相同**，只是把 179700 次小调用换成 1 次大运算；
    * 返回 ``(n, n)`` 布尔矩阵，调用方按**原来的 i<j 顺序**合并，顺序语义不变。

    不同长度（调用方自己塞进来的怪指纹）→ 返回 ``None``，调用方回退原循环。
    这是纯加速：不满足前提时**行为与从前完全一致**。
    """
    idx = [i for i, fp in enumerate(fps) if fp[1] is not None]
    if len(idx) < 2:
        return np.zeros((len(fps), len(fps)), dtype=bool)
    lens = {int(np.asarray(fps[i][1]).size) for i in idx}
    if len(lens) != 1:
        return None                                  # 网格不一致 → 交给原循环
    m = np.vstack([np.asarray(fps[i][1], float) for i in idx])   # (k, m)
    out = np.zeros((len(fps), len(fps)), dtype=bool)
    # 分块：整块广播会分配 (k, k, m) 的三维中间数组——1200 项时那是 11 GB。
    # 按行分块后峰值只有 (chunk, k, m)，与总项数**无关**（只与 chunk 有关）。
    chunk = max(1, int(_PAIR_CHUNK_BYTES // max(8 * m.shape[1] * len(idx), 8)))
    for s in range(0, len(idx), chunk):
        block = idx[s:s + chunk]
        sub = m[s:s + chunk]                                     # (c, m)
        diff = sub[:, None, :] - m[None, :, :]                   # (c, k, m)
        d = np.sqrt(np.mean(diff * diff, axis=2))                # (c, k)
        near = d <= float(d_max)
        for a, i in enumerate(block):
            out[i, np.asarray(idx, dtype=np.intp)[near[a]]] = True
    return out


def _suggest_label(names: Sequence[str]) -> str:
    """组内文件名公共前缀 → 建议标签（只作默认值，不参与分组）。"""
    pre = os.path.commonprefix(list(names))
    pre = re.sub(r"[^A-Za-z0-9]+[A-Za-z0-9]*$", "", pre)
    return pre if len(pre) >= 3 else (Path(names[0]).stem or "series")


def auto_group(items: Sequence[dict], ag: dict) -> tuple[list[dict], list[str]]:
    """``items: [{name, t, wl, ab}]`` → ``(groups, 无法归组的文件名列表)``。

    ``groups[i] = {"members"(按 t 排序), "lam_max", "label"}``；
    与 legacy 一致：单元素且无指纹的簇进 ``invalid``。
    """
    fps = [core_fingerprint(it["wl"], it["ab"], ag["core_lo"],
                            ag["core_hi"], ag["step"]) for it in items]
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    # T3：优先用广播算出的"两两邻近"矩阵；拿不到（指纹网格不一致）才回退双重循环。
    flags = _pair_flags(fps, ag["d_max"])
    if flags is not None:
        # 合并顺序与原来**完全一致**：仍然只走 i<j，且 i 从小到大。
        # （顺序会影响并查集的 parent 链形状，但**不影响最终分组**；保持一致是为了
        #  让"同一次运行两次调用得到逐位相同的 parent 数组"，便于回归比对。）
        ii, jj = np.nonzero(np.triu(flags, k=1))
        for i, j in zip(ii.tolist(), jj.tolist()):
            parent[find(j)] = find(i)
    else:
        log.debug("指纹网格长度不一致 → 回退 O(N²) 循环分组")
        for i in range(n):
            if fps[i][1] is None:
                continue
            for j in range(i + 1, n):
                if fps[j][1] is None:
                    continue
                if _dist(fps[i][1], fps[j][1]) <= ag["d_max"]:
                    parent[find(j)] = find(i)

    clusters: dict = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    groups, invalid = [], []
    for idx in clusters.values():
        if len(idx) == 1 and fps[idx[0]][1] is None:
            invalid.append(items[idx[0]]["name"])
            continue
        members = sorted((items[i] for i in idx), key=lambda m: m["t"])
        lams = [fps[i][0] for i in idx if fps[i][0] is not None]
        groups.append({"members": members,
                       "lam_max": float(np.median(lams)) if lams else None,
                       "label": _suggest_label([m["name"] for m in members])})
    groups.sort(key=lambda g: -len(g["members"]))
    log.info("自动分组: %d 组 / %d 个文件无法归组（母核区 %.0f–%.0f nm, RMS ≤ %.3g）",
             len(groups), len(invalid), ag["core_lo"], ag["core_hi"], ag["d_max"])
    for g in groups:
        lm = f"{g['lam_max']:.0f}" if g["lam_max"] is not None else "?"
        log.info("  组[%s] %d 文件, 母核 λmax=%s nm", g["label"], len(g["members"]), lm)
    if invalid:
        log.warning("母核区无有效数据、无法归组: %s", ", ".join(invalid))
    return groups, invalid


def items_from_scans(scans: Sequence, wl_range: Sequence[float] = (150.0, 1200.0)
                     ) -> tuple[list[dict], int]:
    """``Scan`` 列表 → 分组引擎所需的 items；返回 ``(items, 读不出光谱的数量)``。

    与 legacy ``compare_kobs.load_items`` 的过滤规则一致：
    跳过 ``excluded`` 与 ``background``/``dark`` 角色。
    """
    from ..inputs.io import spectrum_arrays
    ok = [s for s in scans
          if not getattr(s, "excluded", "") and s.role not in ("background", "dark")]
    items, bad = [], 0
    for s in ok:
        arr = spectrum_arrays(s, wl_range)
        if arr is None:
            bad += 1
            continue
        items.append({"name": s.path.name, "t": float(s.t),
                      "wl": arr[0], "ab": arr[1], "path": str(s.path),
                      "group": s.group, "rep": s.rep})
    if bad:
        log.warning("%d 个文件读不出光谱, 已跳过", bad)
    return items, bad
