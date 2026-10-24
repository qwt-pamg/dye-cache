"""V2.8 FR-49：显示层预处理（只影响作图显示/导出，不参与数值分析）。"""
from ui.prep.display import despiked_for_plot, normalize_y
from ui.prep.names import fig_a_names, stack_tag

__all__ = ["despiked_for_plot", "normalize_y", "fig_a_names", "stack_tag"]
