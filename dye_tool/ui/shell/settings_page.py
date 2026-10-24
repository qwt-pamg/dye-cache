# -*- coding: utf-8 -*-
"""③ 图件页、④ 档案页、⑤ 设置页（FR-16 的其余三段）。

三页都**不重写业务逻辑**，只做"把既有页面/组件摆到任务流该在的位置"：

* :class:`PlotPage` —— 现③堆叠/叠加 + 现④紫外荧光作图合并为"作图台"（Q5 默认决策：
  合并只在 UI 层，渲染路径与导出命名不动）；
* :class:`ArchivePage` —— 现⑤染料库 + "最近结果"直达（U8：结果发现性）；
* :class:`SettingsPage` —— 参数面板（``ParamPanel``）+ 配置校验 + **P9 目录整理 GUI 化**
  （两阶段 dry-run，Q6 默认决策：默认只出计划，勾选后才真正移动）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from ui.pages.base_page import PageBase
from ui.pages.spectra_page import SpectraPage
from ui.pages.stack_page import StackPage
from ui.widgets.param_panel import ParamPanel
from ui import theme

log = logging.getLogger("dye_tool.ui.shell")


class PlotPage(QWidget):
    """作图台：按**数据类型**分「紫外（吸收谱）」与「荧光（发射谱）」两页。

    V2.1 调整（用户实拍反馈）：原先把"堆叠/时间演化"与"紫外/荧光常规"按**页面功能**
    分两个页签，用户面对同一批数据要先想"我该去哪个功能页"。现在按**数据类型**分工：

    * 紫外（吸收）：归一化吸收谱叠图 + ε 计算器 + 堆叠/时间演化（ABDA 动力学）；
    * 荧光（发射）：发射谱归一化叠图 + 吸收-发射镜像图/上下两栏/自动配对。

    业务逻辑仍全部复用既有 ``StackPage`` / ``SpectraPage``（Q5：只在 UI 层重组，
    渲染路径与导出命名不动）；经典布局（``gui.nav=classic``）继续用原生两页。
    """

    logged = Signal(str, str)
    status = Signal(str)
    progress = Signal(int, str)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg or {}
        self._batch_dir: Optional[Path] = None
        self._batch_dirty: bool = False
        self._batch_overrides: dict = {}
        self._batch_recursive: Optional[bool] = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        head = QLabel("作图台")
        theme.mark(head, "title")
        sub = QLabel("按数据类型分工：紫外（吸收谱）与荧光（发射谱）。"
                     "导出文件命名与 V1.3 完全一致（重组只是界面层）。")
        sub.setWordWrap(True)
        theme.mark(sub, "muted")
        lay.addWidget(head)
        lay.addWidget(sub)

        # ── 紫外（吸收）──
        self.uv_overlay = SpectraPage(
            self.cfg, modes=("overlay",), overlay_kinds="abs", show_eps=True,
            title="紫外 · 归一化吸收谱叠图",
            subtitle="载入两列吸收谱（.txt/.csv/.spc）→ 同一坐标系归一化叠图；"
                     "可算 ε = A/(c·l)。时间演化见「堆叠 / 时间演化」子页签。")
        self.uv_stack = StackPage(self.cfg)
        uv_tabs = QTabWidget()
        uv_tabs.addTab(self.uv_overlay, "归一化叠图")
        uv_tabs.addTab(self.uv_stack, "堆叠 / 时间演化")
        self.uv_tabs = uv_tabs

        # ── 荧光（发射）──
        self.fluor = SpectraPage(
            self.cfg, modes=("overlay", "mirror", "stacked", "auto"),
            overlay_kinds="em", show_eps=False,
            title="荧光 · 发射谱作图",
            subtitle="发射谱（仪器 FL 文本 / .fds）归一化叠图；"
                     "镜像图/上下两栏/自动配对需同时载入吸收谱。")

        self.tabs = QTabWidget()
        self.tabs.addTab(uv_tabs, "紫外（吸收）")
        self.tabs.addTab(self.fluor, "荧光（发射）")
        for p in (self.uv_overlay, self.uv_stack, self.fluor):
            for sig in ("logged", "status", "progress"):
                getattr(p, sig).connect(getattr(self, sig))
        lay.addWidget(self.tabs, 1)

    def set_batch(self, scans: list, data_dir: Optional[Path] = None,
                  overrides: Optional[dict] = None,
                  recursive: Optional[bool] = None) -> None:
        """UX-3：记录当前批次（**懒加载**，M3）。

        M3（V2.x 审查）：原实现在数据页刚载入时**立刻**对作图台两个页面各跑一次
        `_load_dir` → 同一目录被解析 2 次以上（0.13 s / `.spc` 目录，196 个 `.fds`
        外推约 0.65 s），而用户此刻还在①数据页，看不到作图台。
        现在只记目录并标脏，等用户**真的切到③图件**（``on_activated``）再载入。
        """
        _ = scans
        self._batch_dir = Path(data_dir) if data_dir else None
        # A19（审查附录 C）：连同①数据页的扫描口径与整定一起记下，
        # 在 on_activated 首次解析时应用（M3 懒加载**不回归**：仍不预先解析）。
        self._batch_overrides = dict(overrides or {})
        self._batch_recursive = recursive
        self._batch_dirty = self._batch_dir is not None

    def on_activated(self) -> None:
        """切到③图件页时才把当前批次灌进紫外侧两页（M3 懒加载）。"""
        if not getattr(self, "_batch_dirty", False) or self._batch_dir is None:
            return
        d = self._batch_dir
        self._batch_dirty = False
        for page in (getattr(self, "uv_overlay", None),
                     getattr(self, "uv_stack", None)):
            if page is None:
                continue
            try:
                # A19：两页都继承 recursive；时间/染料整定只对"吸收时间序列"
                # 有意义（stack），荧光叠加页接受但忽略。
                page._load_dir(d, recursive=self._batch_recursive,
                               overrides=self._batch_overrides)
            except Exception as err:                       # noqa: BLE001
                log.warning("作图页接批次失败：%s", err)
                self._batch_dirty = True                   # 下次再试


class ArchivePage(QWidget):
    """档案：染料库（原⑤）+ 最近结果直达。"""

    params_loaded = Signal(dict, dict)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None, *, dye_page=None):
        super().__init__(parent)
        self.cfg = cfg or {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.dye_page = dye_page
        if dye_page is not None:
            lay.addWidget(dye_page, 1)
        try:
            from ui.widgets.dye_library_page import DyeLibraryPage
            from core.paths import resolve_out_root
            out_root = resolve_out_root(self.cfg)
            if self.dye_page is None:
                self.dye_page = DyeLibraryPage(out_root=out_root)
                lay.addWidget(self.dye_page, 1)
            self.dye_page.params_loaded.connect(self.params_loaded)
        except Exception as err:                           # noqa: BLE001
            lb = QLabel(f"染料库页不可用：{err}")
            lb.setWordWrap(True)
            lay.addWidget(lb)
            log.warning("染料库页构建失败：%s", err)

    def reload(self) -> None:
        if self.dye_page is not None and hasattr(self.dye_page, "reload"):
            self.dye_page.reload()


class SettingsPage(PageBase):
    """设置：参数（ParamPanel）+ 配置校验 + P9 目录整理。"""

    TITLE = "⑤ 设置"
    SUBTITLE = ("参数与出图预设、config.yaml 校验、旧 runs 目录整理（P9）。"
                "目录整理默认只出计划（dry-run），勾选后才会真正移动文件。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.addWidget(self.header())
        tabs = QTabWidget()

        # ── 参数 ──
        w_params = QWidget()
        lp = QVBoxLayout(w_params)
        self.params = ParamPanel()
        self.params.set_cfg(self.cfg)
        lp.addWidget(self.params, 1)
        row = QHBoxLayout()
        # 2026-10-19 GUI 目检：ParamPanel 自带「恢复默认」（FR-40.6，带二次确认），
        # 此处再放一个同名按钮是重复入口 → 移除；本页只保留显式「保存」。
        self.btn_save_params = QPushButton("保存到 config.yaml")
        row.addWidget(self.btn_save_params)
        row.addStretch(1)
        lp.addLayout(row)
        tabs.addTab(w_params, "参数与预设")

        # ── 配置校验 ──
        w_cfg = QWidget()
        lc = QVBoxLayout(w_cfg)
        self.btn_check_cfg = QPushButton("检查 config.yaml")
        self.txt_cfg = QPlainTextEdit()
        self.txt_cfg.setReadOnly(True)
        lc.addWidget(self.btn_check_cfg)
        lc.addWidget(self.txt_cfg, 1)
        tabs.addTab(w_cfg, "配置校验")

        # ── 目录整理（P9） ──
        w_reorg = QWidget()
        lr = QVBoxLayout(w_reorg)
        info = QLabel("把老式 runs/run_<时间戳>/ 扁平目录整理成 runs/<染料>/<时间戳>/。"
                      "**只移动不删除**；先出计划，确认后才执行。")
        info.setWordWrap(True)
        lr.addWidget(info)
        bar = QHBoxLayout()
        self.btn_plan = QPushButton("① 生成计划（dry-run）")
        self.btn_apply = QPushButton("② 执行移动")
        self.btn_apply.setEnabled(False)
        self.chk_force = QCheckBox("忽略配置里的 dry_run=true（--force 语义）")
        bar.addWidget(self.btn_plan)
        bar.addWidget(self.btn_apply)
        bar.addWidget(self.chk_force)
        bar.addStretch(1)
        lr.addLayout(bar)
        self.txt_reorg = QPlainTextEdit()
        self.txt_reorg.setReadOnly(True)
        lr.addWidget(self.txt_reorg, 1)
        tabs.addTab(w_reorg, "目录整理（P9）")

        lay.addWidget(tabs, 1)
        lay.addWidget(self.progress_row())

        self.btn_save_params.clicked.connect(self._save_params)
        self.btn_check_cfg.clicked.connect(self._check_cfg)
        self.btn_plan.clicked.connect(self._plan_reorg)
        self.btn_apply.clicked.connect(self._apply_reorg)
        self.params.changed.connect(self._params_changed)

    # ── 参数 ──
    def _params_changed(self) -> None:
        self.status.emit("参数已改动（保存后写入 config.yaml）")

    def _save_params(self) -> None:
        from core.config import save_config
        try:
            self.cfg = self.params.apply_to(self.cfg)
            save_config(self.cfg, self.config_path())
            self.logged.emit("INFO", "参数已保存到 config.yaml")
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("ERROR", f"保存参数失败：{err}")

    # ── 配置校验 ──
    def _check_cfg(self) -> None:
        from core.config import load_config_ex
        p = self.config_path()
        _cfg, issues = load_config_ex(p if p.exists() else None)
        if not issues:
            self.txt_cfg.setPlainText(f"✅ 配置正常：{p}\n\n未发现未知键、死键或类型问题。")
            return
        lines = [f"配置文件：{p}", f"发现 {len(issues)} 条问题：", ""]
        for it in issues:
            lines.append(f"[{it.level}] {it.key}\n  {it.message}")
            if it.suggestion:
                lines.append(f"  建议：{it.suggestion}")
        self.txt_cfg.setPlainText("\n".join(lines))

    # ── 目录整理（P9） ──
    def _out_root(self) -> Path:
        from core.paths import resolve_out_root
        return resolve_out_root(self.cfg)

    def _plan_reorg(self) -> None:
        from core.report.reorg_runs import describe_plan, plan_reorg
        out_root = self._out_root()

        def task(progress_cb, log_cb):
            progress_cb(20, "扫描 runs/")
            return plan_reorg(out_root, self.cfg)

        def done(plan) -> None:
            self._reorg_plan = plan
            self.txt_reorg.setPlainText(describe_plan(plan))
            n = len(plan.get("moves") or [])
            conflicts = len(plan.get("conflicts") or [])
            self.btn_apply.setEnabled(n > 0)
            self.logged.emit("INFO", f"目录整理计划：{n} 个目录待移动"
                                     f"、{conflicts} 个撞名（已自动改名）"
                                     f"；未改动任何文件")

        self.run_task(task, "生成整理计划", done, busy_widgets=[self.btn_plan])

    def _apply_reorg(self) -> None:
        plan = getattr(self, "_reorg_plan", None)
        if not plan:
            return
        force = bool(self.chk_force.isChecked())
        if not force and QMessageBox.question(
                self, "确认移动",
                "即将按计划移动 run 目录（只移动、不删除）。继续？"
        ) != QMessageBox.Yes:
            return
        from core.report.reorg_runs import apply_reorg
        cfg = self.cfg

        def task(progress_cb, log_cb):
            progress_cb(30, "移动目录")
            return apply_reorg(plan, cfg, force=force)

        def done(res) -> None:
            # apply_reorg 返回 dict：{done, failed, moved_n, report, dry_run, message?}
            moved = int(res.get("moved_n") or 0)
            failed = res.get("failed") or []
            extra = f"（dry_run 拦截：{res.get('message')}）" if res.get("dry_run") else ""
            self.txt_reorg.appendPlainText(
                f"\n完成：移动 {moved} 个，失败 {len(failed)} 个{extra}")
            if res.get("report"):
                self.txt_reorg.appendPlainText(f"报告：{res['report']}")
            self.btn_apply.setEnabled(False)

        self.run_task(task, "执行目录整理", done,
                      busy_widgets=[self.btn_plan, self.btn_apply])
