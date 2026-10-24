# -*- coding: utf-8 -*-
"""界面画布上中文文字用的字体（画布占位、活画布标题/标注、染料库趋势图）。

只给**确实含中文的文本对象**单独换字体，不改全局 rcParams：项目约定图件
"Arial 优先、全英文"，而 matplotlib 没有逐字形回退，把中文字体塞进字体链
也照样画出豆腐块。投稿图件的字体链在 :mod:`core.render.plotting`，与这里无关。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

#: 候选顺序：Windows → macOS → Linux 常见中文字体
CJK_FONT_CANDIDATES = (
    "Microsoft YaHei", "微软雅黑", "SimHei", "黑体",
    "PingFang SC", "Hiragino Sans GB",
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Arial Unicode MS",
)


@lru_cache(maxsize=1)
def cjk_family() -> Optional[str]:
    """系统里第一个可用的中文字体名；一个都没有返回 ``None``（只探测一次）。"""
    try:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
    except Exception:                                  # noqa: BLE001 —— 探测失败不致命
        return None
    return next((n for n in CJK_FONT_CANDIDATES if n in have), None)


@lru_cache(maxsize=1)
def cjk_font_path() -> Optional[str]:
    """:func:`cjk_family` 对应的字体文件路径；找不到返回 ``None``。"""
    fam = cjk_family()
    if fam is None:
        return None
    try:
        from matplotlib import font_manager
        from matplotlib.font_manager import FontProperties
        path = font_manager.findfont(FontProperties(family=fam), fallback_to_default=False)
    except Exception:                                  # noqa: BLE001
        return None
    return str(path) if path and Path(path).is_file() else None
