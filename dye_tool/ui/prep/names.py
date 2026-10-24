"""V2.8 FR-49：图名/组名映射（UI 侧契约；包装 core.render 的既有函数，不重写）。"""
from __future__ import annotations

from core.render.plotting_jacs import _fig_a_names as _core_fig_a_names
from core.render.plotting_jacs import _stack_tag as _core_stack_tag


def fig_a_names(groups):
    """图 a 的样品组名集合（显示层契约，供结果区「下一步」等使用）。"""
    return _core_fig_a_names(list(groups or []))


def stack_tag(group: str) -> str:
    """堆叠图组名 tag（显示层契约）。"""
    return _core_stack_tag(str(group))
