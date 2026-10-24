"""V2.8 FR-48：MainWindow 薄装配类（mixin 见 window_nav/window_actions）。"""

from __future__ import annotations

import logging

import os
from pathlib import Path
from typing import Optional
from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QTabWidget
from PySide6.QtWidgets import QWidget
from core.config import list_config_backups
from core.config import load_config_ex
from core.config import restore_config_backup
from core.config import save_config
from core.paths import config_path as _cfg_path
from core.synthetic import make_synthetic
from core import VERSION
from ui.pages.base_page import ErrorDialog
from ui.pages.compare_page import ComparePage
from ui.pages.phi_page import PhiPage
from ui.pages.spectra_page import SpectraPage
from ui.pages.stack_page import StackPage
from ui.workers import Job
from ui.shell.window_parts import MIN_WINDOW_H, MIN_WINDOW_W
from PySide6.QtWidgets import QMainWindow
from ui.shell.window_actions import MainWindowActionsMixin
from ui.shell.window_nav import MainWindowNavMixin

log = logging.getLogger("dye_tool.ui.main_window")

class MainWindow(QMainWindow, MainWindowNavMixin, MainWindowActionsMixin):
    """主窗口：装配层薄壳（V2.8 FR-48 拆分；行为零变）。"""

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._cfg_issues: list = []
        self.cfg = cfg or self._load_cfg()
        self.job: Optional[Job] = None
        #: 一键演示生成的临时数据目录（G15：本会话内必须存在，关窗时清理）
        self._demo_dir: Optional[Path] = None
        self.setWindowTitle(f"dye_tool {VERSION} — 染料性能测试数据自动处理与生图工具")
        # V2.6 FR-38.1：最小尺寸兜底 + 首启尺寸按**光标所在屏**可用区收边（多屏/小屏不溢出）
        self.setMinimumSize(MIN_WINDOW_W, MIN_WINDOW_H)
        _w0, _h0 = self.initial_window_size()
        self.resize(_w0, _h0)
        self._restore_window_geometry()     # V2.3 FR-24.8：恢复上次窗口几何
        # V2.3 FR-24.1：Esc = 取消当前正在跑的后台任务（无任务时零动作）
        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.activated.connect(self._cancel_active_jobs)
        # 主题要在建页面之前装好：matplotlib 工具栏按建好时的底色挑图标深浅
        self._apply_style()
        self._build_pages()                 # 只构造页面对象，不放进任何容器
        self._build_layout()                # 按 gui.nav 选 flow / classic
        self._build_dock()
        self._build_menu()
        self._build_status()
        # 旧配置（没有 gui.nav 键）→ **非模态**一次性提示（Q7）；必须等
        # log_view / statusBar 建好之后才能发（第一版在 _build_flow 里调，
        # 直接 AttributeError: log_view —— 实测踩过）。
        if self.layout_mode == "flow":
            self._nav_hint_if_old_config()
        QTimer.singleShot(200, self._startup_checks)

    # ══════════════ 启动自检式提示（FR-9.1 / FR-9.5 / FR-6.3） ══════════════
    def _startup_checks(self) -> None:
        """窗口显示后：预热 matplotlib（不阻塞按钮）+ 报告配置问题 + 欢迎卡片。

        ⚠ 冒烟/自动化模式（``DYE_TOOL_SMOKE=1``，见 app.py 与验收 A5.4/A9）下
        **绝不弹任何模态对话框**——弹窗会阻塞事件循环、让进程永远退不出去。
        """
        self._warmup()
        if os.environ.get("DYE_TOOL_SMOKE") == "1":
            return
        self._report_cfg_issues()
        if bool((self.cfg.get("gui") or {}).get("show_welcome", True)):
            self._show_welcome()

    def _warmup(self) -> None:
        """后台预热 matplotlib 与字体缓存（FR-6.3）：首图不再卡在按钮之后。"""
        try:
            from ui.workers import Job as _Job

            def task(progress_cb, log_cb):
                import matplotlib
                matplotlib.use("Agg")
                from matplotlib import font_manager
                n = len(font_manager.fontManager.ttflist)
                return n

            self._warm_job = _Job(task, "预热绘图库", self)
            self._warm_job.logged.connect(self.on_logged)
            self._warm_job.done.connect(
                lambda n: self.on_logged("INFO", f"绘图库预热完成（可用字体 {n} 种）"))
            self._warm_job.failed.connect(
                lambda s, d: log.warning("预热失败: %s", s))
            self._warm_job.start()
        except Exception as err:                           # noqa: BLE001
            log.warning("预热任务未能启动: %s", err)

    def _report_cfg_issues(self) -> None:
        """把配置问题（含写坏的 yaml）用人话讲清楚（FR-9.5）。"""
        issues = list(getattr(self, "_cfg_issues", []) or [])
        errs = [i for i in issues if i.level == "ERROR"]
        for i in issues:
            self.on_logged("ERROR" if i.level == "ERROR" else "WARNING",
                           f"[配置] {i.key}: {i.message}"
                           + (f"　建议：{i.suggestion}" if i.suggestion else ""))
        if not errs:
            return
        lines = [f"· {i.key}：{i.message}" + (f"\n  建议：{i.suggestion}"
                                             if i.suggestion else "")
                 for i in errs]
        box = QMessageBox(self)
        box.setWindowTitle("配置有问题（已回退默认值）")
        box.setIcon(QMessageBox.Warning)
        box.setText("你的 config.yaml 有问题，本次已改用内置默认配置：")
        box.setInformativeText("\n".join(lines[:6]))
        box.setDetailedText("\n\n".join(f"{i.key}\n{i.message}\n{i.suggestion}"
                                        for i in errs))
        btn_restore = box.addButton("恢复上一份配置", QMessageBox.ActionRole)
        box.addButton("知道了", QMessageBox.AcceptRole)
        box.exec()
        if box.clickedButton() is btn_restore:
            self._restore_cfg_backup()

    def _show_welcome(self) -> None:
        """首启欢迎卡片（FR-9.1）：三步引导 + 一键跑通演示数据。"""
        box = QMessageBox(self)
        box.setWindowTitle("欢迎使用 dye_tool")
        box.setIcon(QMessageBox.Information)
        box.setText("三步就能出图：")
        box.setInformativeText(
            "① 按命名约定放好数据文件（「帮助 → 文件命名约定」）\n"
            "② 「① Φ_Δ 分析」页点「选择数据目录…」\n"
            "③ 点「▶ 开始分析」先看预览图，满意后点「定稿出图」\n\n"
            "数据文件名不规范、时间对不上？用左侧「预分析整定」页签\n"
            "（染料命名 + 时间工作表），分析前改就行，不会动你的原始文件。")
        btn_demo = box.addButton("一键跑通演示数据", QMessageBox.ActionRole)
        btn_open = box.addButton("直接选数据目录", QMessageBox.ActionRole)
        btn_close = box.addButton("知道了", QMessageBox.AcceptRole)
        box.setCheckBox(QCheckBox("不再显示这个提示"))
        box.exec()
        if box.checkBox() is not None and box.checkBox().isChecked():
            self.cfg.setdefault("gui", {})["show_welcome"] = False
            self._save_cfg()
        clicked = box.clickedButton()
        if clicked is btn_demo:
            self._run_demo()
        elif clicked is btn_open:
            self._show_phi()
            self._open_dir()
        _ = btn_close

    def _run_demo(self) -> None:
        """一键跑通演示数据：生成 synthetic → 载入 → 直接分析（FR-9.1）。

        G15（第二轮审查）：`dye_demo_*` 目录原先跑完就没人管——它**必须活到本轮
        演示结束**（它是被载入的数据目录，删早了分析会失败），所以不能在生成后立刻删；
        改成记在 `self._demo_dir` 上，由 :meth:`closeEvent` 收尾清掉。
        """
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="dye_demo_")) / "synthetic_demo"
        try:
            truth = make_synthetic(out)
        except Exception as err:                           # noqa: BLE001
            ErrorDialog(f"演示数据生成失败：{err}",
                        __import__("traceback").format_exc(), self).exec()
            return
        self._demo_dir = out                                 # 关窗时清理
        self.on_logged("INFO", f"演示数据已生成 → {out}"
                               f"（k_true={truth['k_true']:.3e}）")
        self._show_phi()
        if self.layout_mode == "flow":
            # 演示走完整任务流（FR-19）：先灌进批次工作区，再切分析页
            self.data_page.load_dir(out)
            self.nav.select("analysis")
        self.phi_page._load_dir(out)

    def _restore_cfg_backup(self) -> None:
        """从 config_backup/ 恢复上一份配置（FR-9.5）。"""
        snaps = list_config_backups(self.config_path())
        if not snaps:
            self.on_status("还没有可用的配置快照")
            return
        if restore_config_backup(self.config_path(), 0) is None:
            QMessageBox.warning(self, "恢复失败", "快照恢复失败，请手工检查 config.yaml。")
            return
        self.cfg, self._cfg_issues = load_config_ex(self.config_path())
        self.phi_page.cfg = self.cfg
        try:
            self.phi_page.params.set_cfg(self.cfg)
        except Exception:                                  # noqa: BLE001
            pass
        self.on_status(f"已恢复配置快照：{snaps[0].name}")
        self.on_logged("INFO",
                       f"已恢复快照：{snaps[0].name}（原配置已备份为 config.yaml.bad.*）")

    # ══════════════ 配置 ══════════════
    @staticmethod
    def config_path() -> Path:
        """用户配置文件路径（打包后 = exe 同级）。"""
        return _cfg_path()

    def _load_cfg(self) -> dict:
        """加载配置；有问题就记下来，窗口显示后弹窗说明（FR-9.5/FR-10）。"""
        p = self.config_path()
        cfg, issues = load_config_ex(p if p.exists() else None)
        self._cfg_issues = list(issues)
        return cfg

    def _save_cfg(self) -> None:
        """把会话 cfg 写回磁盘（护栏在 ``core.config.save_config`` 内一处收紧）。"""
        try:
            save_config(self.cfg, self.config_path())
        except Exception as err:                       # noqa: BLE001
            log.warning("保存配置失败: %s", err)

    # ══════════════ 构建 ══════════════
    def _build_pages(self) -> None:
        """**只构造**功能页对象（两种布局共用同一批实例）。"""
        self.phi_page = PhiPage(self.cfg)
        # V2.6 FR-40.5：Φ_Δ 结果区「下一步」→ ③ 图件 / 定稿出图
        _phi_next = getattr(self.phi_page, "next_step_requested", None)
        if _phi_next is not None:
            _phi_next.connect(self._on_next_step)
        # V2.9.4 FR-69：ComparePage 仅保留类/API 供旧测试；不再作为用户入口
        self.compare_page = ComparePage(self.cfg)
        self.pages = [self.phi_page]
        # ⚠ **不在这里**直连主窗口（V2.3 / 评审记录 F2）：flow 布局下壳页
        # （AnalysisPage / PlotPage）已经把内嵌页的 logged/status/progress 转发到
        # 自己、再由主窗口接壳页——若这里再直连一次，每条日志会到达 on_logged
        # **两次**（flow 模式日志重复行，classic 单行）。直连只在 classic 建。
        self._build_library_page()

    def _build_library_page(self) -> None:
        """第 5 页「染料库」（FR-11.6）；组件不可用时降级为不加载，不阻塞启动。"""
        self.dye_page = None
        try:
            from ui.widgets.dye_library_page import DyeLibraryPage
            from core.paths import resolve_out_root
            out_root = resolve_out_root(self.cfg)
            self.dye_page = DyeLibraryPage(out_root=out_root)
            if hasattr(self.dye_page, "logged"):
                self.dye_page.logged.connect(self.on_logged)
            if hasattr(self.dye_page, "params_loaded"):
                self.dye_page.params_loaded.connect(self._apply_run_params)
            self.pages.append(self.dye_page)
        except Exception as err:                           # noqa: BLE001
            log.warning("染料库页加载失败（不影响其它功能）: %s", err)

    # ══════════════ 布局选择（FR-16 / Q1） ══════════════
    def _build_layout(self) -> None:
        """按 ``gui.nav`` 建 flow（任务流）或 classic（平铺页签）。"""
        nav = str(((self.cfg.get("gui") or {}).get("nav")) or "flow").lower()
        self.layout_mode = "classic" if nav == "classic" else "flow"
        if self.layout_mode == "classic":
            self._build_classic()
        else:
            self._build_flow()

    def layout_name(self) -> str:
        """当前布局名（``--gui-smoke`` 与 A16 用它判断期望的界面形态）。"""
        return self.layout_mode

    def nav_count(self) -> int:
        """flow 布局下的导航项数（classic 返回 0）。"""
        return len(self.flow_pages) if self.layout_mode == "flow" else 0

    def page_count(self) -> int:
        """界面上的"页"数：flow = 导航项数；classic = 页签数。"""
        return self.nav_count() if self.layout_mode == "flow" else self.tabs.count()

    def goto(self, key: str) -> bool:
        """切到 flow 布局的某个导航项（classic 布局下返回 False）。"""
        if self.layout_mode != "flow":
            return False
        return self.nav.select(key)

    def _build_classic(self) -> None:
        """V1.x 平铺五页签（保留一个版本周期）。"""
        # V2.1 FR-22.7：classic 用原生两页（V1.x 的页名与形态一字不变）
        self.stack_page = StackPage(self.cfg)
        self.spectra_page = SpectraPage(self.cfg)
        # V2.6 FR-41.5：「在分析页记录」→ 与 flow 同一桥接（经典布局仅提示）
        _rec = getattr(self.spectra_page, "record_pair_requested", None)
        if _rec is not None:
            try:
                _rec.connect(self._on_record_pair_requested)
            except Exception:                          # noqa: BLE001
                pass
        # V2.6 FR-40.6：样式面板「重置全部」→ live_plot.clear_styles()
        self._wire_style_panel(self.stack_page)
        self._wire_style_panel(self.spectra_page)
        self.pages = [self.phi_page, self.stack_page, self.spectra_page]
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        # F2：classic 没有壳页转发，直连主窗口（每页一次，不重复）
        for page in self.pages[:4]:
            page.logged.connect(self.on_logged)
            page.status.connect(self.on_status)
            page.progress.connect(self.on_progress)
        # V25-B6：classic 布局同样把「写入出图参数」接到主窗口（FR-33.3 广播 +
        # Φ_Δ 过期横幅）。此前只有 flow 布局连接 → classic 下信号断链：cfg 仍写、
        # 日志仍报成功，但广播/横幅不出现。
        for page in (self.stack_page, self.spectra_page):
            _sig = getattr(page, "plot_params_written", None)
            if _sig is not None:
                try:
                    _sig.connect(self._on_plot_params_written)
                except Exception:                      # noqa: BLE001
                    pass
        # A 道 FR-23：经典布局显式关闭组页签（V1.x 外观一字不变）
        self.phi_page.set_group_tabs(False)
        titles = ["① 单线态氧（k_obs → Φ_Δ）", "② 堆叠/叠加图", "③ 紫外/荧光作图"]
        for p, title in zip(self.pages[:3], titles):
            self.tabs.addTab(p, title)
        if self.dye_page is not None:
            self.tabs.addTab(self.dye_page, "⑤ 染料库")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self.tabs)
        self.nav = None
        self.flow_pages = {}

    def _build_flow(self) -> None:
        """V2.0 任务流壳：左侧导航 + 主页 + 五个任务页。"""
        from PySide6.QtWidgets import QStackedWidget

        from ui.shell.analysis_page import AnalysisPage
        from ui.shell.data_page import DataPage
        from ui.shell.home_page import HomePage
        from ui.shell.nav import NavBar
        from ui.shell.settings_page import ArchivePage, PlotPage, SettingsPage

        self.nav = NavBar()
        # V2.6 FR-40.3：Alt+1..6 切页 + 导航项 tooltip 标注快捷键
        self._init_nav_shortcuts()
        self.home_page = HomePage(self.cfg)
        self.data_page = DataPage(self.cfg)
        self.analysis_page = AnalysisPage(self.cfg, phi_page=self.phi_page,
                                          compare_page=self.compare_page)
        # A 道 FR-23：flow 布局启用「按染料组分页」（classic 保持 V1.x 外观）
        self.phi_page.set_group_tabs(True)
        # V2.1 FR-22.7：作图台按数据类型内部创建三页（紫外叠图/堆叠时间演化/荧光）
        self.plot_page = PlotPage(self.cfg)
        # V2.5 FR-33.3：工作台「写入出图参数」→ 广播 cfg + Φ_Δ 过期横幅
        for _pg in (self.plot_page.uv_stack, self.plot_page.uv_overlay,
                    self.plot_page.fluor):
            _sig = getattr(_pg, "plot_params_written", None)
            if _sig is not None:
                try:
                    _sig.connect(self._on_plot_params_written)
                except Exception:                          # noqa: BLE001
                    pass
        # 它们也要进 pages：关闭时统一 shutdown、菜单"打开结果"可遍历
        self.pages.extend([self.plot_page.uv_overlay, self.plot_page.uv_stack,
                           self.plot_page.fluor])
        self.archive_page = ArchivePage(self.cfg, dye_page=self.dye_page)
        self.settings_page = SettingsPage(self.cfg)
        self.flow_pages = {
            "home": self.home_page,
            "data": self.data_page,
            "analysis": self.analysis_page,
            "plot": self.plot_page,
            "archive": self.archive_page,
            "settings": self.settings_page,
        }
        self.stack = QStackedWidget()
        for page in self.flow_pages.values():
            self.stack.addWidget(page)
        # 壳页的信号接到主窗口（与 classic 布局一致）
        for page in (self.data_page, self.settings_page):
            for sig in ("logged", "status", "progress"):
                try:
                    getattr(page, sig).connect(getattr(self, "on_" + sig))
                except Exception:                          # noqa: BLE001
                    pass
        for page in (self.analysis_page, self.plot_page):
            for sig in ("logged", "status", "progress"):
                try:
                    getattr(page, sig).connect(getattr(self, "on_" + sig))
                except Exception:                          # noqa: BLE001
                    pass
        # V2.6 FR-41.5：③作图台「在分析页记录」→ ②Stokes 测量卡预填（仅光谱页）
        for _pg in (self.plot_page.uv_overlay, self.plot_page.fluor):
            _rec = getattr(_pg, "record_pair_requested", None)
            if _rec is not None:
                try:
                    _rec.connect(self._on_record_pair_requested)
                except Exception:                      # noqa: BLE001
                    pass
        # V2.6 FR-40.6：样式面板「重置全部」→ 页面 live_plot.clear_styles()
        for _pg in (self.plot_page.uv_stack, self.plot_page.uv_overlay,
                    self.plot_page.fluor):
            self._wire_style_panel(_pg)
        # 主页 / 数据页 / 分析页之间的协作
        self.home_page.demo_requested.connect(self._run_demo)
        self.home_page.open_dir_requested.connect(self._open_dir)
        # V2.6 FR-40.4：空态/最近批次直达 = 切页 + 聚焦①数据页入口
        self.home_page.go.connect(self._goto_and_focus_entry)
        self.home_page.recent_dir_requested.connect(self._open_recent_dir)
        self.nav.navigated.connect(self._on_nav)
        self.data_page.batch_changed.connect(self._on_batch_changed)
        self.data_page.analysis_preset_applied.connect(
            self._on_analysis_preset_applied)
        self.analysis_page.page_run_finished.connect(self._on_page_run_finished)
        # V2.6 FR-40.4/40.5：分析页空态引导 + 结果区「下一步」
        self.analysis_page.request_data_page.connect(self._goto_data_and_focus)
        self.analysis_page.next_step_requested.connect(self._on_next_step)

        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        h.addWidget(self.nav)
        h.addWidget(self.stack, 1)
        self.setCentralWidget(central)
        # V2.9.3 FR-68：全应用兜底悬停说明（专门说明优先，不覆盖）
        try:
            from ui.help_texts import install_app_help
            install_app_help(self)
            # 打开后再次收集：Dock 标题按钮/菜单栏等由 Qt 延迟创建
            QTimer.singleShot(0, lambda: self._refresh_help(self))
        except Exception:                              # noqa: BLE001
            pass
        self.nav.select("home")

