"""V2.8 FR-48：MainWindow 动作/菜单/生命周期 mixin。"""

from __future__ import annotations

import logging

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtGui import QActionGroup
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QDialog
from PySide6.QtWidgets import QDockWidget
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QMenu
from PySide6.QtWidgets import QMessageBox
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QProgressBar
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QToolButton
from PySide6.QtWidgets import QVBoxLayout
from core.config import default_config
from core.synthetic import make_synthetic
from core.synthetic import self_test
from ui.pages.base import ErrorDialog
from ui.widgets.export_bar import open_folder
from ui.widgets.log_view import LogView
from ui.workers import Job
from ui import theme
from ui.window.parts import ABOUT_TEXT, FAQ_TEXT, MIN_WINDOW_H, MIN_WINDOW_W, NAMING_HELP, PREF_WINDOW_H, PREF_WINDOW_W, PROJECT_ROOT, WINDOW_SCREEN_MARGIN

log = logging.getLogger("dye_tool.ui.window.main_window")

class MainWindowActionsMixin:
    def _build_dock(self) -> None:
        self.log_view = LogView(self, min_level="WARNING")
        self.log_view.setMinimumHeight(120)
        dock = QDockWidget("运行日志（WARNING 及以上）", self)
        dock.setWidget(self.log_view)
        dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.RightDockWidgetArea)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        self.log_dock = dock
        # 日志区默认收起，把高度留给工作区；状态栏「运行日志」按钮展开，
        # 收起期间的警告计数显示在按钮上，ERROR 及以上自动展开
        self.resizeDocks([dock], [200], Qt.Vertical)
        dock.hide()
        self._unseen_warnings = 0
        dock.visibilityChanged.connect(self._on_log_dock_visibility)

    def _build_menu(self) -> None:
        bar = self.menuBar()

        m_file = bar.addMenu("文件(&F)")
        act = QAction("打开数据目录…", self)
        act.setShortcut(QKeySequence("Ctrl+O"))
        act.triggered.connect(self._open_dir)
        m_file.addAction(act)
        act = QAction("打开结果文件夹", self)
        act.triggered.connect(self._open_results)
        m_file.addAction(act)
        m_file.addSeparator()
        # V2.6 FR-40.4：最近打开的数据目录（gui.recent_dirs 前 5 条 + 清空最近）
        self.recent_menu = QMenu("最近打开的数据目录", self)
        self.recent_menu.setToolTipsVisible(True)
        self.recent_menu.aboutToShow.connect(self._refresh_recent_menu)
        self.recent_menu_action = m_file.addMenu(self.recent_menu)
        self._refresh_recent_menu()
        m_file.addSeparator()
        act = QAction("退出", self)
        act.setShortcut(QKeySequence("Ctrl+Q"))
        act.triggered.connect(self.close)
        m_file.addAction(act)

        m_tool = bar.addMenu("工具(&T)")
        act = QAction("生成 synthetic 演示数据…", self)
        act.triggered.connect(self._make_synthetic)
        m_tool.addAction(act)
        act = QAction("一键跑通演示数据", self)
        act.setToolTip("生成演示数据 → 载入 → 直接分析出预览图（新同学 30 秒上手）")
        act.triggered.connect(self._run_demo)
        m_tool.addAction(act)
        act = QAction("恢复默认参数", self)
        act.triggered.connect(self._reset_params)
        m_tool.addAction(act)
        act = QAction("保存当前参数到 config.yaml", self)
        act.setShortcut(QKeySequence("Ctrl+S"))
        act.triggered.connect(self._save_params)
        m_tool.addAction(act)
        m_tool.addSeparator()
        act = QAction("校验当前配置…", self)
        act.setShortcut(QKeySequence("Ctrl+Shift+V"))
        act.setToolTip("检查 config.yaml 的键位/类型/范围，并给出中文修改建议")
        act.triggered.connect(self._validate_cfg)
        m_tool.addAction(act)
        act = QAction("恢复上一份配置", self)
        act.setToolTip("从 config_backup/ 回滚（保留最近 5 份快照）")
        act.triggered.connect(self._restore_cfg_backup)
        m_tool.addAction(act)

        m_view = bar.addMenu("视图(&V)")
        self.act_flow = QAction("任务流布局（默认，重启生效）", self, checkable=True)
        self.act_classic = QAction("经典页签布局（V1.x，重启生效）", self, checkable=True)
        grp = QActionGroup(self)
        grp.addAction(self.act_flow)
        grp.addAction(self.act_classic)
        grp.setExclusive(True)
        (self.act_flow if self.layout_mode == "flow" else self.act_classic).setChecked(True)
        self.act_flow.triggered.connect(lambda _=False: self._switch_layout("flow"))
        self.act_classic.triggered.connect(lambda _=False: self._switch_layout("classic"))
        m_view.addAction(self.act_flow)
        m_view.addAction(self.act_classic)
        m_view.addSeparator()
        act = QAction("主页", self)
        act.triggered.connect(lambda: self.goto("home"))
        m_view.addAction(act)
        act_log = self.log_dock.toggleViewAction()
        act_log.setText("运行日志")
        act_log.setShortcut(QKeySequence("Ctrl+L"))
        m_view.addAction(act_log)
        m_view.addSeparator()
        m_theme = m_view.addMenu("界面主题")
        grp_theme = QActionGroup(self)
        grp_theme.setExclusive(True)
        cur_theme = str((self.cfg.get("gui") or {}).get("theme") or "light")
        self.theme_actions = {}
        for key in theme.THEMES:
            a = QAction(theme.THEME_LABELS[key], self, checkable=True)
            a.setChecked(key == cur_theme)
            a.triggered.connect(lambda _=False, k=key: self._switch_theme(k))
            grp_theme.addAction(a)
            m_theme.addAction(a)
            self.theme_actions[key] = a

        m_help = bar.addMenu("帮助(&H)")
        # V2.6 FR-40.3：帮助 →「快捷键一览…」（分组清单，与实际绑定一致）
        act = QAction("快捷键一览…", self)
        act.setToolTip("按页面分组的快捷键清单（与实际绑定一致）")
        act.triggered.connect(self._show_shortcut_overview)
        m_help.addAction(act)
        m_help.addSeparator()
        act = QAction("运行自检", self)
        act.triggered.connect(self._self_test)
        m_help.addAction(act)
        act = QAction("文件命名约定…", self)
        act.triggered.connect(lambda: self._text_dialog("文件命名约定", NAMING_HELP))
        m_help.addAction(act)
        act = QAction("常见问题…", self)
        act.setToolTip("出图/时间整定/染料归档等高频疑问")
        act.triggered.connect(lambda: self._text_dialog("常见问题（FAQ）", FAQ_TEXT))
        m_help.addAction(act)
        act = QAction("关于 dye_tool…", self)
        act.triggered.connect(lambda: QMessageBox.about(self, "关于", ABOUT_TEXT))
        m_help.addAction(act)

    def _switch_layout(self, mode: str) -> None:
        """写 ``gui.nav`` 并说明"重启生效"。

        **为什么不做热切换**：两种布局的容器不同（QTabWidget vs QStackedWidget），
        热切换要把 8 个页面对象在两种父容器间搬家，信号与尺寸状态容易错乱；
        而"改布局"是低频操作，提示重启是诚实的做法。
        """
        if mode == self.layout_mode:
            return
        self.cfg.setdefault("gui", {})["nav"] = mode
        self._save_cfg()
        name = "任务流布局" if mode == "flow" else "经典页签布局"
        # V2.3 FR-24.4：常规反馈不打断——状态栏 + 日志，不弹模态
        self.on_status(f"布局已保存：下次启动使用「{name}」（当前窗口不变）")
        self.on_logged("INFO", f"布局切换已保存：gui.nav={mode}（重启生效）")

    def _switch_theme(self, mode: str) -> None:
        """切换界面主题：立即整窗生效并写回 ``gui.theme``。"""
        self.cfg.setdefault("gui", {})["theme"] = mode
        theme.apply(mode)
        self._save_cfg()
        self.on_status(f"界面主题：{theme.THEME_LABELS.get(mode, mode)}（画布工具栏图标重启后跟随）")

    def _validate_cfg(self) -> None:
        """当前配置全量校验（FR-10.1）。"""
        from core.plotstyle import validate_config
        self.cfg = self.phi_page.params.config(self.cfg)
        issues = validate_config(self.cfg)
        if not issues:
            self.on_status("配置检查：全部合规")
            self.on_logged("INFO", "配置检查：未发现未知键/死键/类型问题")
            return
        lines = [f"[{i.level}] {i.key}\n    {i.message}"
                 + (f"\n    建议：{i.suggestion}" if i.suggestion else "")
                 for i in issues]
        self._text_dialog(f"配置检查：{len(issues)} 条提示", "\n\n".join(lines))

    def _build_status(self) -> None:
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setMaximumWidth(240)
        self.bar.setVisible(False)
        self.lbl_stage = QLabel("就绪")
        self.btn_log = QToolButton()
        self.btn_log.setText("运行日志")
        self.btn_log.setToolTip("展开 / 收起运行日志（Ctrl+L）")
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(self.log_dock.setVisible)
        if self.layout_mode == "flow":
            # 图标栏放不下"当前批次"摘要 → 状态栏常驻（排在永久区最左，不被临时消息盖住）
            self.lbl_context = QLabel("当前批次 未选择")
            self.lbl_context.setToolTip("当前批次目录：未选择（在 数据 页载入）")
            theme.mark(self.lbl_context, "muted")
            self.statusBar().addPermanentWidget(self.lbl_context)
        self.statusBar().addPermanentWidget(self.lbl_stage)
        self.statusBar().addPermanentWidget(self.bar)
        self.statusBar().addPermanentWidget(self.btn_log)
        self.statusBar().showMessage(
            "就绪 — 主页「打开我的数据目录…」或「一键演示」开始"
            if self.layout_mode == "flow"
            else "就绪 — 请从「① Φ_Δ 分析」页选择数据目录开始")

    def _apply_style(self) -> None:
        """整窗主题（ui/theme.py）：按 ``gui.theme`` 装 Fusion + 色板 + 全局 QSS。"""
        theme.apply((self.cfg.get("gui") or {}).get("theme", "light"))

    # ══════════════ V2.6 FR-38.1 / V2.3 FR-24：窗口几何 / Esc 取消 ══════════════
    @staticmethod
    def _available_geometry():
        """光标所在屏的可用区（多屏优先 cursor screen；无屏幕/离屏环境返回 None）。"""
        try:
            from PySide6.QtGui import QCursor, QGuiApplication
            app = QGuiApplication.instance()
            scr = app.screenAt(QCursor.pos()) if app is not None else None
            if scr is None:
                scr = QGuiApplication.primaryScreen()
            return scr.availableGeometry() if scr is not None else None
        except Exception:                                  # noqa: BLE001 —— 无 GUI 环境
            return None

    @classmethod
    def initial_window_size(cls) -> tuple[int, int]:
        """FR-38.1：首启尺寸 = min(1560, aw-2m) × min(960, ah-2m)，且不小于最小尺寸。

        m = WINDOW_SCREEN_MARGIN（约 40–60px）；屏幕按光标所在屏取（多屏不再固定主屏），
        无屏幕信息时退回期望尺寸。
        """
        g = cls._available_geometry()
        if g is None:
            return PREF_WINDOW_W, PREF_WINDOW_H
        w = min(PREF_WINDOW_W, g.width() - 2 * WINDOW_SCREEN_MARGIN)
        h = min(PREF_WINDOW_H, g.height() - 2 * WINDOW_SCREEN_MARGIN)
        return max(MIN_WINDOW_W, w), max(MIN_WINDOW_H, h)

    def _restore_window_geometry(self) -> None:
        """FR-24.8 / FR-38.1：恢复上次窗口几何；clamp 到主屏可用区（防显示器拔掉后飞出）。

        V2.6：clamp 下限不再写死 800×600，而是不小于最小尺寸 1024×640。
        """
        win = (self.cfg.get("gui") or {}).get("window") or {}
        try:
            w, h = int(win.get("w") or 0), int(win.get("h") or 0)
            x, y = int(win.get("x") or 0), int(win.get("y") or 0)
        except (TypeError, ValueError):
            return
        if w <= 0 or h <= 0:
            return
        w, h = max(MIN_WINDOW_W, min(w, 4000)), max(MIN_WINDOW_H, min(h, 3000))
        try:
            from PySide6.QtGui import QGuiApplication
            scr = QGuiApplication.primaryScreen()
            if scr is not None:
                g = scr.availableGeometry()
                x = max(g.left(), min(x, g.right() - w))
                y = max(g.top(), min(y, g.bottom() - h))
        except Exception:                                  # noqa: BLE001 —— 无屏幕时用原值
            pass
        self.resize(w, h)
        self.move(x, y)

    def _save_window_geometry(self) -> None:
        """FR-24.8：关窗时把几何写回 cfg（随 ``_save_cfg`` 落 config.yaml）。"""
        gui = self.cfg.setdefault("gui", {})
        gui["window"] = {"w": int(self.width()), "h": int(self.height()),
                         "x": int(self.x()), "y": int(self.y())}

    def _cancel_active_jobs(self) -> None:
        """FR-24.1：Esc = 取消当前正在跑的后台任务；无任务时零动作、不弹窗。"""
        pages = list(getattr(self, "pages", []) or [])
        for key in ("data", "analysis", "settings"):
            p = (getattr(self, "flow_pages", None) or {}).get(key)
            if p is not None and p not in pages:
                pages.append(p)
        for p in pages:
            try:
                if getattr(p, "_busy", False):
                    p.cancel_job()
                    self.on_status("已请求取消当前任务")
            except Exception:                              # noqa: BLE001
                pass

    # ══════════════ 共享信号 ══════════════
    def on_logged(self, level: str, msg: str) -> None:
        """把任一页的日志汇总到共享日志区（并弹出 WARNING 以上的 dock）。"""
        self.log_view.append(level, msg)
        if level in ("WARNING", "ERROR", "CRITICAL"):
            self.statusBar().showMessage(f"[{level}] {msg}", 8000)
            if level != "WARNING":
                self.log_dock.show()
            elif not self.log_dock.isVisible():
                self._unseen_warnings += 1
                self._update_log_button()

    def _on_log_dock_visibility(self, visible: bool) -> None:
        if visible:
            self._unseen_warnings = 0
        btn = getattr(self, "btn_log", None)
        if btn is not None and btn.isChecked() != visible:
            btn.setChecked(visible)
        self._update_log_button()

    def _update_log_button(self) -> None:
        btn = getattr(self, "btn_log", None)
        if btn is None:
            return
        n = self._unseen_warnings
        btn.setText(f"运行日志 · {n} 条警告" if n else "运行日志")

    def on_status(self, msg: str) -> None:
        """状态栏消息。"""
        self.statusBar().showMessage(msg, 15000)

    def on_progress(self, pct: int, stage: str) -> None:
        """共享进度条。"""
        running = 0 < pct < 100
        self.bar.setVisible(running or pct == 100)
        self.bar.setValue(pct)
        self.lbl_stage.setText(stage)

    def _refresh_help(self, page=None) -> None:
        """V2.9.4 FR-71：页面/卡激活后重收集悬停帮助（懒加载/动态控件）。"""
        try:
            from ui.help_texts import install_app_help
            install_app_help(page or self)
        except Exception:                                  # noqa: BLE001
            pass

    def _on_tab_changed(self, idx: int) -> None:
        if 0 <= idx < len(self.pages):
            page = self.pages[idx]
            try:
                page.on_activated()
            except Exception:                              # noqa: BLE001
                pass
            try:
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, lambda p=page: self._refresh_help(p))
            except Exception:
                pass
            # 染料库页每次切过去都刷新（run 目录是外部可变的）
            if page is getattr(self, "dye_page", None):
                try:
                    page.reload()
                except Exception:                          # noqa: BLE001
                    pass

    # ══════════════ 菜单动作 ══════════════
    def _show_phi(self) -> None:
        """跳到 Φ_Δ 任务（两种布局各走各的容器）。"""
        if self.layout_mode == "flow":
            self.analysis_page.select_task("phi")
            self.nav.select("analysis")
        else:
            self.tabs.setCurrentWidget(self.phi_page)

    def _current_page(self):
        """当前"业务页"（flow 布局下把壳页映射到真正干活的页面）。"""
        if self.layout_mode == "flow":
            key = self.nav.current_key
            if key == "analysis":
                return self.analysis_page.stack.currentWidget()
            if key == "plot":
                return self.plot_page.tabs.currentWidget()
            if key == "data":
                return self.data_page
            return self.phi_page
        idx = self.tabs.currentIndex()
        return self.pages[idx] if 0 <= idx < len(self.pages) else self.phi_page

    def _open_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "打开数据目录",
                                            str(self.cfg.get("gui", {}).get("last_dir")
                                                or PROJECT_ROOT))
        if not d:
            return
        if self.layout_mode == "flow":
            # UX-3：flow 布局里"打开目录"= 载入批次工作区（数据源只有一个权威）
            self.nav.select("data")
            self.data_page.load_dir(Path(d))
            return
        page = self._current_page()
        if hasattr(page, "_load_dir"):
            page._load_dir(Path(d))

    def _open_results(self) -> None:
        for p in [self.phi_page] + [x for x in self.pages if x is not self.phi_page]:
            if hasattr(p, "export"):
                rd = p.export.run_dir()
                if rd and Path(rd).exists():
                    if open_folder(rd):
                        return
                    self.on_status(f"请手动前往：{rd}")
                    return
        self.on_status("还没有生成任何结果目录")

    def _reset_params(self) -> None:
        self.cfg = default_config()
        self.phi_page.params.reset()
        self.phi_page.cfg = self.cfg
        self._save_cfg()
        self.on_status("参数已恢复默认并写回 config.yaml")
        self.on_logged("INFO", "所有参数已恢复为 DEFAULT_CFG 默认值，并写回 config.yaml")

    def _save_params(self) -> None:
        self.cfg = self.phi_page.params.config(self.cfg)
        self._save_cfg()
        self.on_status(f"参数已写入 {self.config_path()}")
        self.on_logged("INFO", f"参数已保存到 {self.config_path()}")

    def _text_dialog(self, title: str, text: str) -> None:
        d = QDialog(self)
        d.setWindowTitle(title)
        d.resize(640, 520)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        theme.mark(view, "mono")
        btn = QPushButton("关闭")
        btn.clicked.connect(d.accept)
        lay = QVBoxLayout(d)
        lay.addWidget(view, 1)
        lay.addWidget(btn, alignment=Qt.AlignRight)
        d.exec()

    # ══════════════ 自检 / 演示数据 ══════════════
    def _self_test(self) -> None:
        """后台运行 legacy 等价自检，完成后弹窗报告（FR-5）。"""
        if self.job is not None and self.job.isRunning():
            self.on_status("已有任务在运行，请稍候")
            return
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.lbl_stage.setText("自检中…")
        self.on_logged("INFO", "开始运行内置自检（synthetic, seed=42，约需 20–60 秒）")
        self.job = Job(lambda p, l: self._self_test_task(p, l), "内置自检", self)
        self.job.progress.connect(self.on_progress)
        self.job.logged.connect(self.on_logged)
        self.job.failed.connect(self._on_selftest_failed)
        self.job.done.connect(self._on_selftest_done)
        self.job.start()

    @staticmethod
    def _self_test_task(progress_cb, log_cb):
        progress_cb(5, "生成 synthetic 数据并跑流水线")
        checks = self_test()
        progress_cb(95, "汇总结果")
        return checks

    def _on_selftest_done(self, checks) -> None:
        self.bar.setValue(100)
        self.lbl_stage.setText("自检完成")
        n_ok = sum(1 for _, p, _ in checks if p)
        lines = [f"{'[通过]' if p else '[失败]'} {name}   ← {info}"
                 for name, p, info in checks]
        for name, p, info in checks:
            self.on_logged("INFO" if p else "ERROR", f"自检 {name} → {info}")
        ok = n_ok == len(checks)
        box = QMessageBox(self)
        box.setWindowTitle("内置自检")
        box.setIcon(QMessageBox.Information if ok else QMessageBox.Warning)
        box.setText(f"自检{'全部通过' if ok else '存在失败项'}："
                    f"{n_ok}/{len(checks)} 项通过")
        box.setDetailedText("\n".join(lines))
        box.exec()

    def _on_selftest_failed(self, summary: str, detail: str) -> None:
        self.bar.setValue(0)
        self.lbl_stage.setText("自检失败")
        ErrorDialog(f"自检未能完成：{summary}", detail, self).exec()

    def _make_synthetic(self) -> None:
        """生成 synthetic 演示数据（FR-5）。"""
        d = QFileDialog.getExistingDirectory(
            self, "选择 synthetic 数据输出目录",
            str(self.cfg.get("gui", {}).get("last_dir") or PROJECT_ROOT))
        if not d:
            return
        out = Path(d) / "synthetic_demo"
        try:
            truth = make_synthetic(out)
        except Exception as err:                       # noqa: BLE001
            ErrorDialog(f"生成失败：{type(err).__name__}: {err}",
                        __import__("traceback").format_exc(), self).exec()
            return
        self.on_logged("INFO", f"synthetic 数据已生成 → {out}")
        self.on_status(f"演示数据已生成：{out}")
        self.on_logged("INFO",
                       f"k_true = {truth['k_true']:.4e} s⁻¹；"
                       f"k_ref = {truth['k_ref']:.4e}；Φ_true = {truth['phi_true']:.2f}"
                       "（可在 ② 分析页选择该目录验证全流程）")

    # ══════════════ 关闭 ══════════════
    def closeEvent(self, e) -> None:                   # noqa: N802 —— Qt 接口
        """退出前结束后台线程并持久化配置。

        G14（第二轮审查）：这里原来只处理 `self.job`，**漏了 `_warm_job`**
        （启动后 200 ms 起的"预热绘图库"线程）。早关窗口 = 一个仍在跑的 QThread
        被析构 —— 这正是交接说明 §1.5 记载的 `0xC0000409`
        （`QThreadStorage: entry destroyed before end of thread`）幽灵的存活机制之一。
        `--gui-smoke` 门是安全的（它不跑 exec，singleShot 不触发），所以门② 抓不到它。

        现在对两个线程一视同仁：cancel + **有界** wait（不能无限等，否则关不掉窗口）。
        """
        # S2（V2.x 审查）：flow 布局下**遍历全部壳页**（原来手写 data/settings，
        # 漏了 analysis_page —— 它的 ε/A@λ 面板是 PageBase 子类、跑 Job 时不被 cancel）。
        # 有 shutdown 的调，没有的跳过（home/archive/plot 是纯展示壳）。
        extra = list(self.flow_pages.values()) if self.layout_mode == "flow" else []
        seen: set = set()
        self._shutdown_targets = []
        for p in list(self.pages) + extra:
            if p is None or id(p) in seen:
                continue
            seen.add(id(p))
            hook = getattr(p, "shutdown", None)
            if not callable(hook):
                continue
            self._shutdown_targets.append(type(p).__name__)
            try:
                hook()
            except Exception as err:                   # noqa: BLE001
                log.warning("页面 shutdown 失败（%s）：%s", type(p).__name__, err)
        for job in (self.job, getattr(self, "_warm_job", None)):
            if job is None:
                continue
            try:
                if job.isRunning():
                    job.cancel()
                    job.wait(4000)
            except RuntimeError:                       # 底层 C++ 对象已销毁
                pass
        # B1（V2.x 审查）：关窗前先把 ⑤设置 的面板值写回，再合并 Φ_Δ 面板。
        # 原顺序只保存 Φ_Δ 的参数 → 设置页里未保存的编辑静默丢失。
        if self.layout_mode == "flow":
            self._sync_settings_page(False)
        self.cfg = self.phi_page.params.config(self.cfg)
        self._save_window_geometry()          # FR-24.8：几何随配置一起落盘
        self._save_cfg()
        # G15：演示数据目录收尾清掉（生成时不能删——它是被载入的数据目录）
        demo = getattr(self, "_demo_dir", None)
        if demo is not None:
            import shutil as _shutil
            try:
                _shutil.rmtree(Path(demo).parent, ignore_errors=True)
            except OSError:
                pass
        super().closeEvent(e)
