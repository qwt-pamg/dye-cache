# -*- coding: utf-8 -*-
"""⑤ 设置页：界面主题 + 参数面板 + 配置校验 + 旧 runs 目录整理（默认只出计划，勾选后才移动）。

布局：顶部细条（页名 + 界面主题三段切换，任何分区下都看得见）/ 左栏分区导航 /
中栏当前分区的表单。分区见 :data:`SECTIONS`。
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QHBoxLayout, QLabel,
                               QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QScrollArea,
                               QSplitter, QStackedWidget, QVBoxLayout, QWidget)

from ui.pages.base import PageBase
from ui.widgets.param_panel import ParamPanel
from ui import theme

log = logging.getLogger("dye_tool.ui.pages")

#: 分区：键 → (导航文字, 分区说明)；顺序即左栏顺序
SECTIONS = (
    ("params", "参数与出图预设",
     "分析参数与出图预设。改完点「保存到 config.yaml」才写盘；离开本页时会同步给分析页。"),
    ("config", "config.yaml 校验",
     "检查 config.yaml 里的未知键、死键与类型问题，并给出修改建议（只读，不改文件）。"),
    ("reorg", "目录整理",
     "把老式 runs/run_<时间戳>/ 扁平目录整理成 runs/<染料>/<时间戳>/。"
     "只移动不删除；先出计划（dry-run），确认后才执行。"),
)
#: 顶部细条高度（与分析页 / 图件页一致）；参数表单最大宽度
TOP_ROW_H, PARAMS_MAX_W = 44, 820


@theme.register_qss
def _settings_qss(p: dict[str, str]) -> str:
    return f"""
/* ── ⑤ 设置页（ui/pages/settings.py） ── */
QFrame#settingsTopBar {{ background: {p['surface']}; border: none;
                        border-bottom: 1px solid {p['border']}; }}
QListWidget#settingsNav {{ background: transparent; border: none; outline: none; }}
QListWidget#settingsNav::item {{ padding: 8px 10px; margin: 1px 0; border-radius: 6px;
                                color: {p['text']}; }}
QListWidget#settingsNav::item:hover {{ background: {p['surface_alt']}; }}
QListWidget#settingsNav::item:selected {{ background: {p['accent_soft']}; color: {p['accent']}; }}
QPushButton#themeSeg {{ background: {p['surface']}; color: {p['text_muted']};
                       border: 1px solid {p['border']}; border-radius: 0;
                       padding: 4px 12px; margin: 0; }}
QPushButton#themeSeg:hover {{ color: {p['accent']}; }}
QPushButton#themeSeg:checked {{ background: {p['accent_soft']}; color: {p['accent']};
                               border-color: {p['accent']}; font-weight: bold; }}
QPushButton#themeSeg[segPos="first"] {{ border-top-left-radius: 6px;
                                       border-bottom-left-radius: 6px; }}
QPushButton#themeSeg[segPos="last"] {{ border-top-right-radius: 6px;
                                      border-bottom-right-radius: 6px; }}
"""


class SettingsPage(PageBase):
    """设置：界面主题 + 参数（ParamPanel）+ 配置校验 + P9 目录整理。"""

    TITLE = "⑤ 设置"
    SUBTITLE = ("界面主题、参数与出图预设、config.yaml 校验、旧 runs 目录整理。"
                "目录整理默认只出计划（dry-run），勾选后才会真正移动文件。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._build_top_row())

        self.section_stack = QStackedWidget()
        for key, name, desc in SECTIONS:
            self.section_stack.addWidget(self._section_page(name, desc, self._build_section(key)))
        self.sections = QListWidget()
        self.sections.setObjectName("settingsNav")
        for key, name, desc in SECTIONS:
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, key)
            it.setToolTip(desc)
            self.sections.addItem(it)
        self.sections.currentRowChanged.connect(self.section_stack.setCurrentIndex)
        nav = QWidget()
        ln = QVBoxLayout(nav)
        ln.setContentsMargins(0, 0, 0, 0)
        ln.addWidget(self.sections, 1)
        nav.setMinimumWidth(180)
        nav.setMaximumWidth(240)

        body = QSplitter(Qt.Horizontal)
        body.addWidget(nav)
        body.addWidget(self.section_stack)
        body.setChildrenCollapsible(False)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([200, 1100])
        self.body = body
        wrap = QWidget()
        lw = QVBoxLayout(wrap)
        lw.setContentsMargins(10, 8, 10, 8)
        lw.addWidget(body, 1)
        lw.addWidget(self.progress_row())
        lay.addWidget(wrap, 1)
        self.select_section(SECTIONS[0][0])

        self.btn_save_params.clicked.connect(self._save_params)
        self.btn_check_cfg.clicked.connect(self._check_cfg)
        self.btn_plan.clicked.connect(self._plan_reorg)
        self.btn_apply.clicked.connect(self._apply_reorg)
        self.params.changed.connect(self._params_changed)

    def _build_top_row(self) -> QFrame:
        """顶部细条：页名 + 界面主题（浅色 / 深色 / 跟随系统），切换即整窗生效。"""
        bar = QFrame()
        bar.setObjectName("settingsTopBar")
        bar.setFixedHeight(TOP_ROW_H)
        self.top_bar = bar
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(8)
        row.addWidget(theme.mark(QLabel("设置"), "section"))
        row.addStretch(1)
        row.addWidget(theme.mark(QLabel("界面主题"), "muted"))
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.theme_group = QButtonGroup(self)
        self.theme_group.setExclusive(True)
        self.theme_buttons: dict[str, QPushButton] = {}
        for i, key in enumerate(theme.THEMES):
            b = QPushButton(theme.THEME_LABELS[key])
            b.setObjectName("themeSeg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(f"界面主题：{theme.THEME_LABELS[key]}（立即生效，写入 gui.theme）")
            b.setProperty("segPos", "first" if i == 0 else
                          ("last" if i == len(theme.THEMES) - 1 else "mid"))
            b.clicked.connect(lambda _=False, k=key: self.set_theme(k))
            self.theme_group.addButton(b)
            self.theme_buttons[key] = b
            seg.addWidget(b)
        row.addLayout(seg)
        self._sync_theme_buttons()
        return bar

    @staticmethod
    def _section_page(name: str, desc: str, body: QWidget) -> QWidget:
        """分区外壳：标题 + 一句说明 + 内容。"""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(12, 4, 4, 0)
        lay.setSpacing(6)
        lay.addWidget(theme.mark(QLabel(name), "title"))
        lbl = QLabel(desc)
        theme.mark(lbl, "muted")
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        lay.addWidget(body, 1)
        return page

    def _build_section(self, key: str) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        if key == "params":
            self.params = ParamPanel()
            self.params.set_cfg(self.cfg)
            self.params.setMaximumWidth(PARAMS_MAX_W)       # 宽屏下表单不铺满整行
            sc = QScrollArea()
            sc.setWidget(self.params)
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setAlignment(Qt.AlignLeft | Qt.AlignTop)
            lay.addWidget(sc, 1)
            row = QHBoxLayout()
            # 2026-10-19 GUI 目检：ParamPanel 自带「恢复默认」（FR-40.6，带二次确认），
            # 此处再放一个同名按钮是重复入口 → 移除；本页只保留显式「保存」。
            self.btn_save_params = QPushButton("保存到 config.yaml")
            theme.mark(self.btn_save_params, "primary")
            row.addWidget(self.btn_save_params)
            row.addStretch(1)
            lay.addLayout(row)
        elif key == "config":
            row = QHBoxLayout()
            self.btn_check_cfg = QPushButton("检查 config.yaml")
            theme.mark(self.btn_check_cfg, "primary")
            row.addWidget(self.btn_check_cfg)
            row.addStretch(1)
            lay.addLayout(row)
            self.txt_cfg = QPlainTextEdit()
            self.txt_cfg.setReadOnly(True)
            self.txt_cfg.setPlaceholderText("点「检查 config.yaml」后在这里列出问题与建议")
            lay.addWidget(self.txt_cfg, 1)
        else:
            bar = QHBoxLayout()
            self.btn_plan = QPushButton("① 生成计划（dry-run）")
            theme.mark(self.btn_plan, "primary")
            self.btn_apply = QPushButton("② 执行移动")
            self.btn_apply.setEnabled(False)
            self.chk_force = QCheckBox("忽略配置里的 dry_run=true（--force 语义）")
            bar.addWidget(self.btn_plan)
            bar.addWidget(self.btn_apply)
            bar.addWidget(self.chk_force)
            bar.addStretch(1)
            lay.addLayout(bar)
            self.txt_reorg = QPlainTextEdit()
            self.txt_reorg.setReadOnly(True)
            self.txt_reorg.setPlaceholderText("先生成计划：这里列出将要移动的目录，未改动任何文件")
            lay.addWidget(self.txt_reorg, 1)
        return w

    # ── 分区导航 ──
    def section_keys(self) -> list[str]:
        return [key for key, _n, _d in SECTIONS]

    def current_section(self) -> str:
        row = self.sections.currentRow()
        return SECTIONS[row][0] if 0 <= row < len(SECTIONS) else ""

    def select_section(self, key: str) -> bool:
        """切到某个分区（左栏与中栏一起变）；未知键返回 False。"""
        keys = self.section_keys()
        if key not in keys:
            return False
        self.sections.setCurrentRow(keys.index(key))
        return True

    # ── 界面主题 ──
    def _theme_mode(self) -> str:
        """当前 ``gui.theme``（优先读主窗口那份 cfg：菜单改的也是它）。"""
        cfg = getattr(self.window(), "cfg", None)
        cfg = cfg if isinstance(cfg, dict) else self.cfg
        mode = str((cfg.get("gui") or {}).get("theme") or "light")
        return mode if mode in theme.THEMES else "light"

    def _sync_theme_buttons(self) -> None:
        self.theme_buttons[self._theme_mode()].setChecked(True)

    def set_theme(self, mode: str) -> None:
        """切界面主题：在主窗口里走 ``MainWindow.set_theme``（与菜单同一路）；
        单独使用本页时直接应用并存盘。"""
        if mode not in theme.THEMES:
            return
        win = self.window()
        if win is not self and hasattr(win, "theme_changed"):
            win.set_theme(mode)       # 主窗口发 theme_changed → sync_theme 勾上按钮
            return
        self.cfg.setdefault("gui", {})["theme"] = mode
        theme.apply(mode)
        self.persist_cfg()
        self.status.emit(f"界面主题：{theme.THEME_LABELS[mode]}")
        self.sync_theme(mode)

    def sync_theme(self, mode: str) -> None:
        """主题已经切好（菜单或本页）：三段切换跟着勾上。"""
        btn = self.theme_buttons.get(mode)
        if btn is not None:
            btn.setChecked(True)

    def showEvent(self, event) -> None:                # noqa: N802 —— Qt 约定
        """每次显示都按当前 ``gui.theme`` 刷新三段切换；首次显示时接上菜单的主题项。"""
        super().showEvent(event)
        self._sync_theme_buttons()

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

    def _plan_reorg(self) -> None:
        from core.report.reorg_runs import describe_plan, plan_reorg
        out_root = self.out_root()

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
