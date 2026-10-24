# -*- coding: utf-8 -*-
"""⑤ 设置页：参数面板 + 配置校验 + 旧 runs 目录整理（默认只出计划，勾选后才移动）。"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from ui.pages.base import PageBase
from ui.widgets.param_panel import ParamPanel

log = logging.getLogger("dye_tool.ui.pages")


class SettingsPage(PageBase):
    """设置：参数（ParamPanel）+ 配置校验 + P9 目录整理。"""

    TITLE = "⑤ 设置"
    SUBTITLE = ("参数与出图预设、config.yaml 校验、旧 runs 目录整理。"
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
                      "只移动不删除；先出计划，确认后才执行。")
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
