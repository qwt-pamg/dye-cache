# -*- coding: utf-8 -*-
"""Φ_Δ 页：按组过滤画布时用的图件名集合。"""
from __future__ import annotations


def figure_names(groups: set, scans: list) -> set:
    """这些组对应的图件名集合（命名与 ``plotting_jacs`` 同源，防漂移）。

    A03（V2.x 审查）："单组"只数**可出图的样品组**（不含已剔除/background/dark），
    与图 a 的命名口径一致；否则"1 样品组 + 1 背景组"时图 a 哪个页签都看不见。
    """
    try:
        from ui.prep.names import fig_a_names, stack_tag
        from core.render.plotting import _safe
    except Exception:                                  # noqa: BLE001
        return set()
    draw_groups = sorted({str(s.group) for s in (scans or [])
                          if not s.excluded
                          and str(getattr(s, "role", "")) not in ("background", "dark")})
    single = len(draw_groups) <= 1
    names: set = set()
    for g in groups:
        if single:
            names.update(fig_a_names([g]))
        else:
            names.add(f"fig_a_spectra_{_safe(g)}")
        names.add(f"fig_b_kinetics_{_safe(g)}")
        names.add(f"jacs_stack_{stack_tag(g)}")
    return names
