# -*- coding: utf-8 -*-
"""V2.8 FR-49：显示层预处理成层（despike/归一/图名映射契约）。"""
from __future__ import annotations

import numpy as np
import pytest

from core.config import default_config
from ui.prep.display import despiked_for_plot, normalize_y
from ui.prep.names import fig_a_names, stack_tag

pytestmark = pytest.mark.fast


def test_despike_interpolates_spike_and_preserves_real_nan():
    wl = np.arange(400.0, 500.0, 1.0)
    y = 1.0 + 0.01 * np.sin(wl)
    y[20] = 0.0                     # 单点掉 0 → 应被 despike 剔除后插值
    y[60] = np.nan                  # 真实空段 → 必须保留 NaN
    cfg = dict(default_config()["qc"])
    out = despiked_for_plot(y, wl, cfg, "probe.txt")
    assert out[60] != out[60] or np.isnan(out[60]), "真实 NaN 被凭空补出"
    assert np.isfinite(out[20]), "尖峰未被插值"
    assert abs(out[20] - (y[19] + y[21]) / 2) <= 0.02
    # 未剔除点基本不变；输入数组未被原地修改
    assert y[20] == 0.0
    assert np.allclose(out[np.arange(len(y)) != 20], y[np.arange(len(y)) != 20],
                      equal_nan=True, atol=1e-12)


def test_despike_bad_inputs_fall_back_without_crash(caplog):
    wl = np.arange(10.0)
    y = np.arange(10.0)
    assert np.array_equal(despiked_for_plot(y, wl[:-1], {}, "bad.txt"), y)
    assert np.array_equal(despiked_for_plot(np.array([]), np.array([]), {}, "e"),
                          np.array([]))
    assert np.array_equal(despiked_for_plot(y, wl, {}, "ok.txt"), y)  # 无尖峰


def test_normalize_y_max_and_degenerate():
    assert np.allclose(normalize_y([1.0, 2.0, 4.0]), [0.25, 0.5, 1.0])
    z = np.zeros(3)
    assert np.array_equal(normalize_y(z), z)
    assert normalize_y(np.array([])).size == 0
    assert np.array_equal(normalize_y([np.nan, 2.0]), np.array([np.nan, 1.0]),
                          equal_nan=True)


def test_display_name_contract_matches_core():
    from core.render.plotting_jacs import _fig_a_names, _stack_tag
    groups = ["Resorufin-2I-pbs-TEST", "Resorufin-pbs-t"]
    assert fig_a_names(groups) == _fig_a_names(groups)
    assert stack_tag("A/B") == _stack_tag("A/B")


def test_ui_pages_use_display_contract():
    """页面不再直接 import core.analysis.qc / 旧 widget 内部实现。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    stack = (root / "ui/pages/stack_page.py").read_text(encoding="utf-8")
    spec = (root / "ui/pages/spectra_page.py").read_text(encoding="utf-8")
    phi = (root / "ui/pages/phi_page.py").read_text(encoding="utf-8")
    assert "from ui.prep.display import despiked_for_plot" in stack
    assert "from ui.prep.display import despiked_for_plot" in spec
    assert "from ui.widgets.plot_prep import" not in stack + spec
    assert "from ui.prep.names import fig_a_names, stack_tag" in phi
    assert "from core.render.plotting_jacs import _fig_a_names" not in phi
