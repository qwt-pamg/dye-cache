# -*- coding: utf-8 -*-
"""pytest 全局夹具与测试公共设施。

职责：

* 把项目根目录加入 ``sys.path``，保证 ``import core`` 在任何工作目录下都成立；
* 提供样例数据、legacy 基线目录等路径常量；
* 提供若干会话级夹具（legacy 回归结果、内置自检结果），避免重复跑重活；
* 每个用例结束后摘掉 ``dye_tool`` logger 上的 handler，防止日志跨用例泄漏。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# ═══════════════════════════ 路径常量 ═══════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SAMPLES = PROJECT_ROOT / "samples"
SAMPLES_UVVIS = SAMPLES / "uvvis"
SAMPLES_FL = SAMPLES / "fluorescence"
SAMPLES_SYNTHETIC = SAMPLES / "synthetic_demo"
BASELINE_DIR = PROJECT_ROOT / ".legacy_baseline"
RUNS_DIR = PROJECT_ROOT / "runs"

# ── 临时文件不许写 C 盘（用户要求）─────────────────────────────────
# `pytest.ini` 的 addopts 把 `--basetemp` 指向 `test_temp/pytest/`，
# 但**pytest 9 不会自动创建 basetemp**：目录不存在时所有用例在 setup 阶段
# 直接 `FileNotFoundError: 系统找不到指定的路径`。（实测过：19 个用例全 ERROR。）
# 所以这里在 conftest 导入时先把它建出来——pytest 解析 addopts 之前必然先导入 conftest。
#
# 背景：pytest 会保留最近几个会话的 tmp 目录，实测一次全量跑在 C 盘留下 95.77 MB。
# 详见 test_temp/README.md。
_TMP_ROOT = PROJECT_ROOT / "test_temp" / "pytest"
try:
    _TMP_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:                                          # pragma: no cover
    pass                                                 # 建不出来就让 pytest 自己报错，别在这里吞掉


def _prune_old_pytest_tmp() -> None:
    """清 `test_temp/pytest/` 里上一次会话留下的东西（**开跑前**执行）。

    为什么必须清（两层，实测）：

    1. pytest 的 `--basetemp` 每次会 `rm_rf` 整个目录再重建。目录非空且删不掉时
       直接抛 `OSError: [WinError 145] 目录不是空的` → **所有用例 setup ERROR**
       （本机 2026-10-17 实测：单跑一个用例也能触发）。
    2. 而它删不掉的原因正是这里的 `*current` **符号链接**：pytest 9 为每个用例的
       `tmp_path` 建一条 `tmp_path_factory` 的 `*current` 链接，`shutil.rmtree` 在
       Windows 上遇到符号链接会失败（`rm_rf` 的 onerror 也删不掉链接本身）。

    所以规矩还是"每次开跑前清上一次"（保留本次，失败时照样能翻），但清理口径要
    **覆盖三种形状**：`pytest-N/` 会话目录、`pytest-current` 链接，以及本次新发现的
    每个用例一条的 `<用例名>current` 链接。链接一律用 `os.unlink`（不是 rmtree）。

    清理失败（句柄被占）就跳过——不能因为它让整个测试会话起不来。
    """
    import os as _os
    import shutil as _shutil
    try:
        entries = [p for p in _TMP_ROOT.iterdir()]
    except OSError:                                      # pragma: no cover
        return
    for p in entries:
        try:
            if p.is_dir() and not p.is_symlink():
                _shutil.rmtree(p, ignore_errors=True)    # 会话目录 / 上次的用例目录
            else:
                _os.unlink(p)                            # 符号链接/junction：只删链接本身
        except OSError:                                  # pragma: no cover
            pass
    # 兜底：还有删不掉的（句柄被占/链接套链接）就让 pytest 自己重建时退让，
    # 但把"没清干净"这件事记下来——否则下一次报 WinError 145 时无从归因。
    left = []
    try:
        left = [p.name for p in _TMP_ROOT.iterdir()]
    except OSError:                                      # pragma: no cover
        pass
    if left:
        import warnings
        warnings.warn(f"test_temp/pytest 未清空（{len(left)} 项：{left[:5]}）；"
                      f"pytest 的 --basetemp 可能报 WinError 145", stacklevel=2)


# ⚠️ **只在主进程里清**（2026-10-17 实测踩到）：`multiprocessing` 的 spawn 子进程会**重新导入 conftest**，
# 于是出图池一启动，子进程就把父进程正在用的 `test_temp/pytest/<本用例>/` 删了
# ——症状是 `plot_all` 的图 a 报 `[Errno 2] ... figures/fig_a_*.png`，而
# `figdir.mkdir(parents=True)` 明明刚成功过（目录在两次 savefig 之间消失）。
# 实测证据：一次用例运行里 conftest 被导入两次（pid 不同），第二次正好在渲染阶段。
# 判据用项目的既有口径 `current_process().name`（铁律 3）——**不要**用
# `getppid()`：在 shell 里跑 pytest 时它返回 shell 的 pid，主进程会被误判成子进程
# 而不清理（下次就报 WinError 145 目录非空）。
def _in_spawn_child() -> bool:
    """当前解释器是不是 spawn 出来的子进程（multiprocessing 口径）。"""
    try:
        import multiprocessing as _mp
        return _mp.current_process().name != "MainProcess"
    except Exception:                                    # noqa: BLE001 pragma: no cover
        return False                                     # 判不出来时按主进程处理（要清）


if not _in_spawn_child():
    _prune_old_pytest_tmp()


#: legacy 黄金基线（A2 判据）：(标签, 数据子目录, 基线 JSON)
LEGACY_CASES = [
    ("2i", "data_2i", "baseline_2i.json"),
    ("all", "data_all", "baseline_all.json"),
]


def _force_agg() -> None:
    """绘图用例必须在无显示环境下工作 → 固定 Agg 后端。"""
    try:
        import matplotlib
        matplotlib.use("Agg")
    except Exception:                                  # noqa: BLE001 —— 未装 mpl 时绘图用例自带跳过
        pass


_force_agg()


# ═══════════════════════════ 夹具 ═══════════════════════════
@pytest.fixture(scope="session", autouse=True)
def _quiet_qt_at_exit():
    """**会话结束前**把 Qt 后台线程与窗口清干净，避免解释器退出时 abort。

    现象与原因（本机实测，间歇复现）：pytest 报完 "332 passed" 之后进程以
    ``0xC0000409``（有符号 = -1073740791）退出，stderr 是
    ``QThreadStorage: entry N destroyed before end of thread`` +
    ``Fatal Python error: Aborted``。这是 Qt 的**清理顺序**问题——某个 QThread
    或窗口活到了解释器退出阶段。带 slow 用例时几乎不出现、跳过 slow 后常出现，
    正是因为"线程有没有跑完"取决于时间。

    修法分两层：

    1. 用例侧显式销毁（``test_review_fixes.py`` 的 ``canvas_factory`` /
       ``page_factory``）——新写涉及 Qt 控件的测试请照做；
    2. 这一层做**兜底**：会话结束时等所有非 daemon 线程结束，并排空 Qt 的待处理
       删除。即使漏了第 1 层，也不会把"测试全过"变成"进程崩溃"。
    """
    yield
    import gc
    import threading
    import time
    # 解释器退出阶段**不要再让 GC 回收 Qt 对象**：widget 之间是父子引用，
    # 循环 GC 的回收顺序不保证，父先死于子是 Qt 会直接 abort
    # （QThreadStorage / "Fatal Python error: Aborted"）。进程马上就要结束，
    # 这里关掉 GC 反而更安全、也更快（省掉退出时的整堆遍历）。
    gc.disable()
    deadline = time.perf_counter() + 10.0
    for t in threading.enumerate():
        if t is threading.main_thread() or t.daemon:
            continue
        t.join(max(0.0, deadline - time.perf_counter()))
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for _ in range(4):                         # 让 deleteLater 真正执行
                app.processEvents()
            app.closeAllWindows()
            app.processEvents()
    except Exception:                                  # noqa: BLE001
        pass
    # V2.7 遗留复测（2026-10-20）：会话末显式关闭 matplotlib 全部 Figure，
    # 减少解释器退出期 Figure/画布析构与 Qt 扩展卸载顺序冲突（幽灵 0xC0000409）。
    # 低风险：测试已全部跑完，关闭图不影响任何判据；真实复现统计见
    # test_temp/c4_ghost_conclusion.json。
    try:
        import matplotlib.pyplot as plt
        plt.close("all")
    except Exception:                                  # noqa: BLE001
        pass


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_config(tmp_path_factory):
    """把 ``config.yaml`` 的读**写**位置重定向到临时目录（**防止测试写脏交付配置**）。

    为什么需要它（V1.3 实测踩到）：GUI 会自动把会话态写回 ``config.yaml``
    （``PageBase.remember_dir()`` → ``persist_cfg()``：最近目录、``gui.last_dir``、
    ``gui.out_root``），而测试里 ``cfg["gui"]["out_root"]`` 常指向 pytest 临时目录。
    结果：**每跑一次测试，交付用的 ``config.yaml`` 就被改一次**——
    ``out_root`` 变成 ``…\\Temp\\pytest-of-…\\gui_lazy0``，
    ``recent_dirs`` 里塞进 pytest 路径。

    这类污染**不会让任何用例变红**（测试读的是内存里的 cfg），只会让用户拿到
    一份指向临时目录的配置——"测试绿、产品脏"。所以这里用
    :data:`core.paths.CONFIG_ENV_VAR` 把落点换到本会话专属的临时文件：

    * 测试**可以**照常读写真配置（行为不变，仍然覆盖"读-改-写"整条链路）；
    * 但改的是副本，交付的 ``config.yaml`` 一个字节都不动。

    ``test_pipeline_fr34.py`` 里断言 ``paths.config_path()`` 形态的用例已同步
    改为显式 ``monkeypatch.delenv``，不受影响。
    """
    from core.paths import CONFIG_ENV_VAR
    import os

    tmp = tmp_path_factory.mktemp("user_config") / "config.yaml"
    real = PROJECT_ROOT / "config.yaml"
    if real.exists():
        tmp.write_bytes(real.read_bytes())      # 从真配置起步，行为与原状一致
    old = os.environ.get(CONFIG_ENV_VAR)
    os.environ[CONFIG_ENV_VAR] = str(tmp)
    try:
        yield tmp
    finally:
        if old is None:
            os.environ.pop(CONFIG_ENV_VAR, None)
        else:
            os.environ[CONFIG_ENV_VAR] = old


@pytest.fixture(autouse=True)
def clean_dye_logger():
    """用例结束后清理 ``dye_tool`` logger 的 handler，避免跨用例日志/文件句柄泄漏。"""
    yield
    lg = logging.getLogger("dye_tool")
    for h in list(lg.handlers):
        try:
            h.close()
        except Exception:                              # noqa: BLE001
            pass
        lg.removeHandler(h)
    lg.setLevel(logging.NOTSET)


@pytest.fixture
def cfg() -> dict:
    """每个用例一份独立的默认配置（深拷贝）。"""
    from core.config import default_config
    return default_config()


@pytest.fixture(scope="session")
def uvvis_scans():
    """``samples/uvvis`` 的 35 个 Scan（会话级，只读用途）。"""
    from core.config import default_config
    from core.io import collect_scans
    from core.results import setup_logging
    cap = setup_logging(None, debug=False)
    try:
        return collect_scans(SAMPLES_UVVIS, default_config())
    finally:
        cap.detach()


@pytest.fixture(scope="session")
def legacy_2i():
    """``.legacy_baseline/data_2i`` 的 ``(scans, results, phi, cfg)``（会话级）。"""
    from core.config import default_config
    from core.phi import compute_phi
    from core.pipeline import run_core
    from core.results import setup_logging
    cap = setup_logging(None, debug=False)
    try:
        cfg = default_config()
        scans, results = run_core(BASELINE_DIR / "data_2i", cfg)
        phi = compute_phi(results, cfg)
    finally:
        cap.detach()
    return scans, results, phi, cfg


@pytest.fixture(scope="session")
def selftest_checks():
    """内置自检结果（会话级：约 3–5 s，全模块共享一次）。"""
    from core.synthetic import self_test
    return self_test()


# ═══════════════════════════ 小工具 ═══════════════════════════
def make_scan(path, wl, a, *, group="g", rep="", role="sample", t=0.0, excluded=""):
    """便捷构造 ``Scan``（测试内减少样板）。"""
    import numpy as np
    from core.io import Scan
    return Scan(path=Path(path), group=group, rep=rep, role=role, t=float(t),
                wl=None if wl is None else np.asarray(wl, float),
                a=None if a is None else np.asarray(a, float),
                excluded=excluded)


def make_fit(k=1.0e-3, status="OK"):
    """最小拟合结果替身：只暴露 ``judge_controls``/``compute_phi`` 需要的属性。"""
    class _Fit:
        def __init__(self) -> None:
            self.k = float(k)
            self.status = status
    return _Fit()

# ═══════════════════════════ 公开仓库的数据集策略 ═══════════════════════════
# 公开仓库不携带实验/仪器测试数据集；相关用例在缺少本地数据时整模块跳过。
_DATA_REQUIRED_MODULES = {
    "test_batch_beta", "test_fds_adapter", "test_golden_lines",
    "test_grouping", "test_io", "test_io_excel", "test_live_preview_e2e",
    "test_peaks", "test_pipeline", "test_plotting_jacs",
    "test_regression_legacy", "test_results", "test_spectra",
    "test_ui_smoke_personal",
}


def pytest_collection_modifyitems(config, items):
    data_dir = Path(__file__).resolve().parent / "data"
    if data_dir.is_dir():
        return
    skip = pytest.mark.skip(reason="public repository excludes experimental test datasets")
    for item in items:
        module = getattr(item, "module", None)
        name = module.__name__.split(".")[-1] if module is not None else ""
        if name in _DATA_REQUIRED_MODULES:
            item.add_marker(skip)
