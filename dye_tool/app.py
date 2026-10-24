#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dye_tool 图形界面入口（FR-1 ~ FR-5）。

用法：
    python app.py                        # 启动 GUI
    python app.py --self-test            # 内置自检（打包后同样可用）
    python app.py --self-test --report out.txt
    python app.py --version
    python app.py --cli <core.cli 参数>   # 转发给命令行入口，如 --cli samples/uvvis

打包：见 packaging/dye_tool.spec 与 README。
说明：打包为 windowed 程序（无控制台），因此 ``--self-test`` 会把结果
**写入报告文件**（默认 exe 同级 ``selftest_report.txt``）并以退出码表明成败，
便于在无 Python 环境的机器上做验收（设计文档 A5）。
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

# 允许 `python app.py` 在任意工作目录下运行（把项目根加入 sys.path）
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import VERSION                                          # noqa: E402
from core.config import load_config                               # noqa: E402
from core.paths import app_root, config_path                      # noqa: E402
from core.results import setup_logging                            # noqa: E402

log = logging.getLogger("dye_tool.app")

#: ``--help`` 输出（windowed 打包下会落到 exe 同级的 dye_tool_last_run.log）
USAGE = f"""dye_tool {VERSION} —— 染料性能测试数据自动处理与生图工具

用法:
  dye_tool.exe                          启动图形界面（双击即可）
  dye_tool.exe --self-test [--report P] 内置自检（T1–T5 + F6），默认写
                                        exe 同级 selftest_report.txt
  dye_tool.exe --gui-smoke [--report P] GUI 冒烟：离屏建主窗口 + 跑全流程出图，
                                        验证 Qt/matplotlib 模块齐备（打包裁剪用）
  dye_tool.exe --version                打印版本号
  dye_tool.exe --cli <参数...>          转发给命令行入口，例如：
        dye_tool.exe --cli samples/uvvis -o runs
        dye_tool.exe --cli samples/uvvis --dry-run
        dye_tool.exe --cli --plot-only runs/run_20250101-120000
        dye_tool.exe --cli --make-synthetic
        dye_tool.exe --cli --reorg-runs            (整理旧 runs/ 目录，只出计划)
        dye_tool.exe --cli --reorg-runs --apply    (真正执行整理)
  dye_tool.exe --help                   显示本帮助

命令行完整参数请见 README.md 或 `--cli --help`。
出图风格由 config.yaml 的 plot.style 决定：jacs（默认，单栏 82.5 mm、
600 dpi、png）或 legacy（迁移前版式，供回归比对）。
"""


# ═══════════════════════════ 无界面入口 ═══════════════════════════
def _arg_value(argv: list[str], flag: str) -> str | None:
    """取 ``--flag value`` 形式的参数值。"""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def run_headless(argv: list[str]) -> int | None:
    """处理不需要 GUI 的子命令；返回退出码，或 ``None`` 表示应启动 GUI。

    ``--self-test`` 即使在 windowed 打包产物中也必须可用（A5 验收需要），
    因此结果同时写文件 + 打印（有控制台时可见）。
    """
    if "--help" in argv or "-h" in argv:
        # 注意：不能落到 GUI 分支，否则 `dye_tool.exe --help` 会挂住不退出。
        # windowed 打包时 stdout 已指向 dye_tool_last_run.log，帮助即写在那里。
        print(USAGE)
        return 0

    if "--version" in argv or "-V" in argv:
        print(f"dye_tool {VERSION}")
        return 0

    if "--self-test" in argv:
        from core.synthetic import self_test
        setup_logging(None, debug=False)
        checks = self_test()
        lines = [f"dye_tool {VERSION} 内置自检",
                 f"frozen={getattr(sys, 'frozen', False)}  python={sys.version.split()[0]}",
                 ""]
        for name, passed, info in checks:
            lines.append(f"[{'通过' if passed else '失败'}] {name}  <- {info}")
        n_ok = sum(1 for _, p, _ in checks if p)
        ok = n_ok == len(checks)
        lines += ["", f"汇总：{n_ok}/{len(checks)} 项通过 — "
                      f"{'全部通过' if ok else '存在失败项'}"]
        text = "\n".join(lines)
        print(text)
        out = _arg_value(argv, "--report")
        p = Path(out) if out else (app_root() / "selftest_report.txt")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text + "\n", encoding="utf-8")
            print(f"\n自检报告已写出：{p}")
        except Exception as err:                       # noqa: BLE001
            print(f"[警告] 无法写出自检报告 {p}: {err}")
        return 0 if ok else 1

    if "--gui-smoke" in argv:
        return _gui_smoke(argv)

    if "--cli" in argv:
        from core.cli import main as cli_main
        rest = argv[argv.index("--cli") + 1:]
        setup_logging(None, debug=False)
        return int(cli_main(rest))

    return None


# ═══════════════════════════ 冻结产物 GUI 冒烟（P3 的验证门） ═══════════════════════════

def _smoke_tmp_root() -> Path:
    """门自检的临时根目录：程序目录下的 ``.preview_tmp``（可用 ``DYE_TOOL_PREVIEW_ROOT``
    改到别处），不用系统临时目录——维护者要求不往系统盘写临时文件。"""
    root = Path(os.environ.get("DYE_TOOL_PREVIEW_ROOT") or (app_root() / ".preview_tmp"))
    root.mkdir(parents=True, exist_ok=True)
    return root

def _gui_smoke(argv: list[str]) -> int:
    """离屏构建主窗口并跑一条精简流水线，用于**验证打包产物里 Qt/matplotlib 模块齐备**。

    为什么需要它：``--self-test`` 只调 ``core.synthetic.self_test()``（纯数值，
    numpy/scipy 而已），**完全不碰 Qt 与 matplotlib**。于是裁剪打包体积时
    （P3）删掉某个 Qt 插件或 matplotlib 后端，自检照样 9/9 全绿，而用户双击
    就白屏。这个子命令把"GUI 层还能起来 + 能出图"变成**可自动判定的退出码**。

    跑的是真实链路：QApplication → MainWindow → collect_scans → run_phi_pipeline
    → 画布解码 png。产物写到 ``--report`` 指定的文件（默认 exe 同级）。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    lines = [f"dye_tool {VERSION} GUI 冒烟（冻结产物验证）",
             f"frozen={getattr(sys, 'frozen', False)}  python={sys.version.split()[0]}",
             f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM')}", ""]
    ok = True

    def step(name: str, fn):
        nonlocal ok
        try:
            info = fn()
            lines.append(f"[通过] {name}  <- {info}")
            return True
        except Exception as err:                        # noqa: BLE001
            ok = False
            lines.append(f"[失败] {name}  <- {type(err).__name__}: {err}")
            lines.append("       " + traceback.format_exc().replace("\n", "\n       ")[:1500])
            return False

    # ── L16（评审记录）：第①步原来是"恒过 lambda" ──
    # 原写法 `if step(..., lambda: "实例已建立"): app = _qt()` —— 真 QApplication()
    # 在 step **之外**构造，所以插件缺失时异常根本没进报告（进程直接以非 0 退出，
    # 门虽然红、报告却缺了这一项，语义破了）。现在把构造放进被 step 包裹的函数里。
    def _qt():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([sys.argv[0]])
        assert app is not None, "QApplication 构造返回 None"
        return "实例已建立"
    if not step("Qt 平台插件可加载（QApplication）", _qt):
        return _write_smoke(lines, ok, argv)
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()

    # L2（评审记录）：`_window()` 原来 `return f"{w.tabs.count()} 个页签"`——
    # 返回的是**字符串**，于是 `win=[None]` 那套"持引用"机制拿到的根本不是窗口，
    # MainWindow 在步骤②结束就被 GC，后面 5 项检查都在**没有窗口**的状态下跑。
    # 各步自证成立所以不是假通过，但机制是错的；且"延迟才炸"的缺陷（QTimer/
    # 惰性信号）门② 就此测不到。改成真正的持引用。
    win: list = []

    def _window():
        from core.config import default_config
        from ui.window.main_window import MainWindow
        w = MainWindow(default_config())
        w.show()
        for _ in range(10):
            app.processEvents()
        win.append(w)                                  # 持引用，别让 GC 收走
        mode = w.layout_name()
        n = w.page_count()
        # N1（第二轮审查）：原为 `n >= 4` 而文案说"应为 5"——判据与文案不一致，
        # 少一个页签（某页构造失败被吞）门仍会绿。门禁就该钉死期望值。
        # V2.0 ε：默认是 flow 布局（6 个导航项），经典布局仍是 5 个页签。
        want = 6 if mode == "flow" else 5
        assert n == want, f"{mode} 布局页数异常：{n}（应为 {want}）"
        return f"{mode} 布局 / {n} 页"
    step("主窗口可构建（QMainWindow + 全部页）", _window)

    def _matplotlib():
        import matplotlib
        matplotlib.use("Agg", force=True)
        from matplotlib.figure import Figure
        f = Figure(figsize=(2, 1.5))
        ax = f.add_subplot(111)
        ax.plot([0, 1], [1, 0])
        import io as _io
        buf = _io.BytesIO()
        f.savefig(buf, format="png", dpi=72)
        n = buf.tell()
        assert n > 1000, f"png 过小（{n} B）——字体或后端可能缺失"
        from matplotlib import font_manager
        nf = len(font_manager.fontManager.ttflist)
        return f"png {n} B, 可用字体 {nf} 种"
    step("matplotlib 可出图（Agg 后端 + 字体）", _matplotlib)

    def _reader():
        """QImageReader 的解码能力 —— 覆盖 PySide6 的 imageformats 插件。

        分两类断言（P3 裁剪体积时的护栏）：
        * **必需**：``png``（画布要显示 matplotlib 出图）、``svg``（矢量图件预览）；
        * **可选**：其余格式存在与否都不影响本应用，只记录数量。

        为什么要写死必需项：P3 删掉了 tiff/webp/jpeg/icns/tga/wbmp 六个插件，
        如果哪天再删到 png 或 svg 上，这条会立刻变红，而不是等到用户白屏才发现。
        """
        from PySide6.QtGui import QImageReader
        fmts = sorted(bytes(b).decode("ascii", "replace")
                      for b in QImageReader.supportedImageFormats())
        missing = [f for f in ("png", "svg") if f not in fmts]
        assert not missing, f"缺少必需的图片格式 {missing}；实际支持 {fmts}"
        return f"{len(fmts)} 种格式（必需 png/svg 齐备）"
    step("QImageReader 支持 png/svg（imageformats 插件）", _reader)

    def _svg_icon():
        """真加载一张 SVG 图 —— 覆盖 ``qsvgicon``/``qsvg`` 插件与 Qt6Svg.dll。

        ``supportedImageFormats()`` 只反映 QImageReader 的插件，**测不出**
        QIcon 引擎链（那要 QIconEnginePlugin + Qt6Svg.dll 一起在才行）。
        P3 裁剪时最容易误删的就是它（看起来"只是个画图标的"）。
        """
        import tempfile
        from PySide6.QtGui import QIcon, QPainter, QPixmap
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">'
               '<rect width="32" height="32" fill="#C81E1E"/></svg>')
        with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False,
                                         encoding="utf-8", dir=str(_smoke_tmp_root())) as fh:
            fh.write(svg)
            path = fh.name
        try:
            icon = QIcon(path)
            assert not icon.isNull(), "QIcon 无法加载 SVG（Qt6Svg/qsvgicon 缺失？）"
            pm = QPixmap(32, 32)
            pm.fill()
            p = QPainter(pm)
            icon.paint(p, 0, 0, 32, 32)
            p.end()
        finally:
            # G15（第二轮审查）：`delete=False` 的临时 SVG **从来不删** ——
            # 每跑一次门就在 %TEMP% 漏一个 tmpXXXX.svg。用完即删。
            try:
                Path(path).unlink()
            except OSError:
                pass
        return "SVG 图标已渲染"
    step("QIcon 可加载 SVG（Qt6Svg + qsvgicon 插件链）", _svg_icon)

    # ── L3（评审记录）：门的产物必须落在**临时目录**，不能污染交付目录 ──
    # 原先 out_root = exe 同级 runs_gui_smoke：每跑一次门就在交付目录里新建一个
    # 完整 run（results.csv + xlsx + 8 张 png），反复跑持续膨胀。
    # 临时目录用程序目录下的 .preview_tmp（不写系统盘 %TEMP%，结束即删）。
    import tempfile
    smoke_dir = Path(tempfile.mkdtemp(prefix="dye_gui_smoke_", dir=str(_smoke_tmp_root())))

    def _pipeline():
        from core.config import default_config
        from core.io import collect_scans
        from core.pipeline import run_phi_pipeline
        root = app_root()
        data = root / "samples" / "uvvis"
        if not data.is_dir():
            raise RuntimeError(f"随包样例目录缺失：{data}")
        cfg = default_config()
        scans = collect_scans(data, cfg)
        assert scans, "没有扫描到任何文件"
        rr = run_phi_pipeline(data, cfg, out_root=smoke_dir,
                              export_mode="preview")
        assert rr.ok, f"全流程 ok=False：{rr.message}"        # L16：补 ok 断言
        figs = len(getattr(rr, "figures", []) or [])
        assert figs > 0, "预览出图数为 0"
        return f"{len(scans)} 文件 → ok={rr.ok}, {figs} 张图"
    step("全流程（读文件 + QC + 拟合 + 出图）", _pipeline)

    def _batch_flow():
        """V2.0 ε：任务流壳能载入批次、切页、徽标更新（不跑分析，够快）。"""
        w = win[0]
        assert w.layout_name() == "flow", "冻结产物的默认布局应为 flow"
        data = app_root() / "samples" / "uvvis"
        assert data.is_dir(), f"随包样例目录缺失：{data}"
        assert w.goto("data"), "导航到 ① 数据 失败"
        w.data_page.load_dir(data)
        import time as _t
        t0 = _t.time()
        while w.data_page._busy and _t.time() - t0 < 120:
            app.processEvents()
        for _ in range(4):
            app.processEvents()
        n = len(w.data_page.scans or [])
        assert n > 0, "批次工作区没有载入任何文件"
        assert w.goto("analysis"), "导航到 ② 分析 失败"
        assert w.analysis_page.current_key() == "phi", "分析页默认任务应为 Φ_Δ"
        badge = w.nav.badge("data")
        assert str(n) in badge, f"导航徽标没更新：{badge!r}"
        return f"{n} 个文件 / 徽标 {badge!r}"
    step("任务流壳（批次载入 + 导航 + 徽标）", _batch_flow)

    def _canvas():
        """画布能加载并解码真实 png —— 覆盖 UI 层的图片解码路径。"""
        from ui.widgets.mpl_canvas import CanvasPanel
        pngs = sorted(smoke_dir.rglob("*.png"))
        assert pngs, "没有找到出图产物"
        c = CanvasPanel()
        c.set_images(pngs[:3])
        app.processEvents()
        n = c.combo.count()
        assert n > 0, "画布下拉里一张图都没有（解码链可能断了）"   # L16：补断言
        c.deleteLater()
        return f"载入 {n} 张"
    step("图件画布可载入 png（UI 解码链）", _canvas)

    # 收尾：删掉本次的临时 run（失败也不影响结论）
    try:
        import shutil as _sh
        _sh.rmtree(smoke_dir, ignore_errors=True)
    except Exception:                                  # noqa: BLE001
        pass

    return _write_smoke(lines, ok, argv)


def _write_smoke(lines: list[str], ok: bool, argv: list[str]) -> int:
    """写出冒烟报告并返回退出码（GUI 冒烟与自检共用同一套落盘口径）。"""
    n_ok = sum(1 for x in lines if x.startswith("[通过]"))
    n_bad = sum(1 for x in lines if x.startswith("[失败]"))
    lines += ["", f"汇总：{n_ok} 项通过、{n_bad} 项失败 — "
                  f"{'全部通过' if ok else '存在失败项'}"]
    text = "\n".join(lines)
    print(text)
    out = _arg_value(argv, "--report")
    p = Path(out) if out else (app_root() / "gui_smoke_report.txt")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
        print(f"\nGUI 冒烟报告已写出：{p}")
    except Exception as err:                           # noqa: BLE001
        print(f"[警告] 无法写出冒烟报告 {p}: {err}")
    return 0 if ok else 1


# ═══════════════════════════ 全局异常兜底 ═══════════════════════════
def _install_excepthook(app=None) -> None:
    """任何未捕获异常弹窗显示 traceback + 复制按钮（设计文档 §7）。"""
    from PySide6.QtWidgets import QMessageBox
    from ui.pages.base import ErrorDialog

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("未捕获异常: %s", exc)
        try:
            ErrorDialog(f"{exc_type.__name__}: {exc}", detail).exec()
        except Exception:                             # noqa: BLE001
            QMessageBox.critical(None, "严重错误", detail)

    sys.excepthook = hook


def _prepare_frozen_streams() -> None:
    """windowed 打包（``console=False``）下 ``sys.stdout/stderr`` 为 ``None``。

    若不处理，``print()`` 会抛 ``AttributeError``，而 PyInstaller 在 windowed
    模式下还会为未捕获异常弹出**阻塞式**错误对话框，导致进程卡住。
    这里把两个流指向 exe 同级的 ``dye_tool_last_run.log``：既避免崩溃，
    又给用户留下可排查的运行记录（每次运行覆盖）。
    """
    if not getattr(sys, "frozen", False):
        return
    if sys.stdout is not None and sys.stderr is not None:
        return
    import io
    try:
        logf = open(app_root() / "dye_tool_last_run.log", "w",
                    encoding="utf-8", errors="replace", buffering=1)
    except Exception:                                 # noqa: BLE001
        logf = io.StringIO()
    if sys.stdout is None:
        sys.stdout = logf
    if sys.stderr is None:
        sys.stderr = logf


# ═══════════════════════════ GUI ═══════════════════════════
def main(argv=None) -> int:
    """应用入口：优先处理无界面子命令，否则启动 GUI。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_frozen_streams()
    # P2 多进程出图的**必需**前置：打包成 exe 后（Windows spawn）子进程会重新执行
    # 本可执行文件，必须靠 freeze_support 把"子进程启动"接管过来；否则会反复重启
    # 自身（开发环境下它是无害的 no-op）。
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:                                  # noqa: BLE001
        pass

    code = run_headless(argv)
    if code is not None:
        return code

    # Qt 高 DPI 与缩放（须在 QApplication 之前设置）
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    # 无显示环境（CI/远程）下允许离屏运行，便于自动化验收
    if os.environ.get("DYE_TOOL_OFFSCREEN") == "1":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication
    app = QApplication([sys.argv[0]] + argv)
    app.setApplicationName("dye_tool")
    app.setApplicationVersion(VERSION)
    app.setOrganizationName("Photochemistry Dye Lab")
    _install_excepthook(app)

    # 日志：控制台 + 供 UI 使用的根 logger（GUI 内的日志区另有通道）
    setup_logging(None, debug=os.environ.get("DYE_TOOL_DEBUG") == "1")

    # 用户配置：打包后位于 exe 同级目录（可写），开发时为项目根
    cfgp = config_path()
    cfg = load_config(cfgp if cfgp.exists() else None)
    if not cfgp.exists():
        # 首次运行：把 exe 同级的默认配置固化下来，方便用户直接编辑
        try:
            from core.config import save_config
            save_config(cfg, cfgp)
        except Exception as err:                      # noqa: BLE001
            log.warning("首次写出默认配置失败: %s", err)

    from ui.window.main_window import MainWindow
    win = MainWindow(cfg)
    win.show()
    log.info("dye_tool v%s 界面已启动 (frozen=%s)", VERSION,
             getattr(sys, "frozen", False))

    if os.environ.get("DYE_TOOL_SMOKE") == "1":
        # 冒烟模式：构建并显示后自动退出（自动化验收用）
        from PySide6.QtCore import QTimer
        QTimer.singleShot(300, app.quit)
    return app.exec()


if __name__ == "__main__":
    # 最外层兜底：windowed 打包下未捕获异常会弹**阻塞式**对话框，
    # 因此这里一律转成"写日志 + 退出码 3"，确保进程总能自行结束。
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:                             # noqa: BLE001
        _prepare_frozen_streams()
        traceback.print_exc()
        sys.exit(3)
