# -*- coding: utf-8 -*-
"""① 数据页 —— **批次工作区**（FR-14 + FR-16 / UX-2、UX-3）。

职责（一屏做完数据侧的决策，不再"换页重选"）：

1. 选数据目录 → 后台 ``collect_scans`` → 文件表；
2. 角色 / 时间 / 染料整定（复用既有 ``DyeTimePanel``，不重写业务逻辑）；
3. **QC 体检**（P8）：与 ``--dashboard`` 走同一对 core 函数；
4. **批次落盘**（FR-14）：把整定写进数据目录的 ``dye_batch.yaml``；
5. **可疑行徽标**（FR-19）：时间解析不出 / 角色可疑 → 载入即标，不等分析后才发现。

与"当前批次"的契约（UX-3）：本页是**唯一**的数据源权威。载入成功后发
``batch_changed(list)``，分析页/图件页默认吃这一批（可临时换目录）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog,
                               QHBoxLayout, QLabel, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from core.paths import app_root
from ui.pages.base import PageBase
from ui.widgets.dye_time_panel import DyeTimePanel
from ui.widgets.file_table import FileTable
from ui import theme

log = logging.getLogger("dye_tool.ui.data")


def suspicious_rows(scans) -> list:
    """找出**解析可疑**的扫描（FR-19：载入即标，不等分析后才发现）。

    判据（每条都能说出"为什么可疑"，而不是拍脑袋）：

    * ``t`` 不是有限数 / 负数 → 时间不可解释；
    * 文件名里**完全没有数字** → 大概率没按约定命名（时间靠猜）；
    * 同组内 ``t`` 重复 → 同一时间点两条谱，分析时会被时间检查挑出来；
    * 角色是 ``sample`` 但组名与角色前缀撞车（如组名以 ``bg`` 开头）。

    返回 ``[(scan, 中文原因)]``，**不修改任何 scan**。
    """
    import math
    import re

    out: list = []
    seen: dict = {}
    for s in scans:
        name = Path(s.path).name
        stem = Path(name).stem
        reasons: list = []
        try:
            t = float(s.t)
            if not math.isfinite(t):
                reasons.append("时间不是有限数")
            elif t < 0:
                reasons.append(f"时间为负（{t:g} s）")
        except (TypeError, ValueError):
            reasons.append("时间无法解析")
        if not re.search(r"\d", stem):
            reasons.append("文件名里没有数字 → 时间多半靠猜")
        key = (s.group, round(float(getattr(s, "t", 0.0)), 6))
        if key in seen:
            reasons.append(f"与 {seen[key]} 同一时间点")
        else:
            seen[key] = name
        if reasons:
            out.append((s, "；".join(reasons)))
    return out


class DataPage(PageBase):
    """批次工作区。信号 ``batch_changed(list)`` 通知壳"当前批次变了"。"""

    batch_changed = Signal(object)          # list[Scan]
    batch_saved = Signal(str)               # 保存结果说明
    #: V2.4-A（FR-26.4）：分析模板套用后把新 cfg 冒泡给壳（主窗口统一广播到各页）
    analysis_preset_applied = Signal(object)

    TITLE = "① 数据 —— 批次工作区"
    SUBTITLE = ("导入数据目录 → 检查文件表（角色/时间可整定）→ QC 体检 → "
                "整定写入 dye_batch.yaml 随数据一起走。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.scans: list = []
        self.batch = None
        self._build()
        self.setAcceptDrops(True)          # V2.3 FR-24.3：支持拖入目录/文件
        self.refresh_templates()           # V2.4-A：分析模板下拉

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        lay.addWidget(self.header())
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")

        # 顶部工具条
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择数据目录…")
        self.btn_reload = QPushButton("重新载入")
        self.btn_qc = QPushButton("QC 体检")
        self.btn_save_batch = QPushButton("保存整定到 dye_batch.yaml")
        self.chk_recursive = QCheckBox("含子目录")
        self.chk_recursive.setChecked(True)
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.btn_reload)
        bar.addWidget(self.btn_qc)
        bar.addWidget(self.chk_recursive)
        bar.addStretch(1)
        bar.addWidget(self.btn_save_batch)
        lay.addLayout(bar)

        # V2.4-A（FR-26.4）：分析参数模板（**唯一 UI 落点**＝本页；不复制到⑤设置，
        # 遵守单一状态源铁律）。差量模板只覆盖 qc/基线/拟合/Φ 四节。
        tpl = QHBoxLayout()
        tpl.addWidget(QLabel("分析模板："))
        self.cmb_template = QComboBox()
        self.cmb_template.setMinimumWidth(200)
        self.cmb_template.setToolTip(
            "把 qc / 基线 / 拟合 / Φ_Δ 四节的参数存成命名模板，换一批数据一键套用。\n"
            "模板只存与默认值不同的键；套用后不会自动重算（按 F5 显式运行）。")
        self.btn_apply_tpl = QPushButton("套用")
        self.btn_save_tpl = QPushButton("另存为模板…")
        self.btn_del_tpl = QPushButton("删除模板")
        tpl.addWidget(self.cmb_template, 1)
        tpl.addWidget(self.btn_apply_tpl)
        tpl.addWidget(self.btn_save_tpl)
        tpl.addWidget(self.btn_del_tpl)
        lay.addLayout(tpl)

        self.lbl_dir = QLabel("尚未选择数据目录")
        theme.mark(self.lbl_dir, "muted")
        self.lbl_dir.setWordWrap(True)
        lay.addWidget(self.lbl_dir)
        lay.addWidget(self.progress_row())

        # 中部：文件表 | 整定面板
        split = QSplitter(Qt.Horizontal)
        self.files = FileTable()
        self.panel = DyeTimePanel()
        split.addWidget(self.files)
        split.addWidget(self.panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        lay.addWidget(split, 1)

        # 底部：可疑行提示
        self.lbl_suspect = QLabel("")
        self.lbl_suspect.setWordWrap(True)
        theme.mark(self.lbl_suspect, "warn")
        lay.addWidget(self.lbl_suspect)

        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_reload.clicked.connect(lambda: self.load_dir(self.data_dir))
        self.btn_qc.clicked.connect(self._export_qc)
        self.btn_save_batch.clicked.connect(self.save_batch)
        self.btn_apply_tpl.clicked.connect(self.apply_template)
        self.btn_save_tpl.clicked.connect(self.save_template)
        self.btn_del_tpl.clicked.connect(self.delete_template)
        self.panel.times_changed.connect(self._on_times_changed)
        self.panel.state_changed.connect(self._refresh_after_panel)
        self.panel.restore_requested.connect(self._refresh_after_panel)
        self.panel.cmb_unit.currentTextChanged.connect(self.files.set_time_unit)
        self.files.time_changed.connect(self._on_file_time_changed)
        self.files.selection_changed.connect(self._on_selection)

    # ── V2.3 FR-24.3：拖入目录/文件 → 载入（与 Φ_Δ/对比/作图页同口径） ──
    def dragEnterEvent(self, e) -> None:               # noqa: N802 —— Qt 接口
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                    # noqa: N802 —— Qt 接口
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        dirs = [p for p in urls if p.is_dir()]
        if dirs:
            self.load_dir(dirs[0])
        elif urls:
            self.load_dir(urls[0].parent)
        e.acceptProposedAction()

    # ══════════════ 载入 ══════════════
    def _choose_dir(self) -> None:
        start = str((self.cfg.get("gui") or {}).get("last_dir") or app_root())
        d = QFileDialog.getExistingDirectory(self, "选择数据目录", start)
        if d:
            self.load_dir(Path(d))

    def load_dir(self, d: Optional[Path]) -> None:
        """载入目录（后台解析）；``None`` = 无操作。"""
        if d is None:
            return
        d = Path(d)
        self.data_dir = d
        self.lbl_dir.setText(f"数据目录：{d}（递归={self.chk_recursive.isChecked()}）")
        self.remember_dir(d)
        cfg = dict(self.cfg)
        cfg["io"] = dict((cfg.get("io") or {}),
                         recursive=bool(self.chk_recursive.isChecked()))
        self._qc_cfg = cfg

        def task(progress_cb, log_cb):
            from core.inputs.batch_file import load_batch
            from core.inputs.io import clear_scan_cache, collect_scans
            clear_scan_cache()
            scans = collect_scans(d, cfg,
                                  progress_cb=lambda p, s: progress_cb(p, s))
            issues: list = []
            batch = load_batch(d, issues)
            for it in issues:
                log_cb(it.level, f"[批次文件] {it.message}")
            return scans, batch

        self.run_task(task, "解析数据目录", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.btn_reload])

    def _on_loaded(self, res) -> None:
        scans, batch = res
        self.scans = list(scans)
        self.batch = batch
        dye_of = batch.dye_map(self.scans, self.data_dir) if batch else {}
        # V2.9 FR-60.2：载入批次时恢复时间/重复号整定（无文件时零变化）。
        if batch is not None and getattr(batch, "files", None):
            from core.inputs.batch_file import apply_to_scans
            n_applied = apply_to_scans(batch, self.scans, self.data_dir)
            if n_applied:
                self.logged.emit("INFO", f"[批次] 已恢复 {n_applied} 个文件的整定")
        # 面板先应用（“全部留空待填”会改 excluded），文件表随后按最终状态渲染。
        self.panel.set_scans(self.scans, dye_of=dye_of,
                             data_dir=self.data_dir)
        self.files.set_scans(self.scans, dye_of=dye_of)
        n_ok = sum(1 for s in self.scans if not s.excluded)
        n_ex = len(self.scans) - n_ok
        n_batch = len(batch.files) if batch else 0
        self.lbl_dir.setText(
            f"数据目录：{self.data_dir}　|　{len(self.scans)} 个文件，"
            f"有效 {n_ok}，剔除 {n_ex}　|　批次文件条目 {n_batch}"
            + ("" if n_batch else "（尚无 dye_batch.yaml）"))
        self._mark_suspects()
        self.btn_qc.setEnabled(bool(self.scans))
        self.btn_save_batch.setEnabled(bool(self.scans))
        # V2.4 FR-26.5：dye_batch.yaml 带 analysis_preset → 载入批次时**自动套用一次**
        # （在 batch_changed 之前：下游整定/overrides 与参数一致）。模板缺失 → 降级。
        self._apply_batch_analysis_preset()
        self.batch_changed.emit(self.scans)
        self.logged.emit("INFO", f"已载入 {len(self.scans)} 个文件（{self.data_dir}）")


    # ══════════════════ V2.4-A（FR-26）：分析参数模板（唯一 UI 落点 = ①数据页） ══════════════════
    def refresh_templates(self) -> None:
        """刷新模板下拉（用户模板按名排序；无模板时禁用三个动作按钮）。"""
        try:
            from core.render import plotstyle
            names = plotstyle.list_analysis_presets()
        except Exception as err:                       # noqa: BLE001 —— 预设坏不得废页
            log.warning("读取分析模板列表失败：%s", err)
            names = []
        cur = self.cmb_template.currentText()
        self.cmb_template.blockSignals(True)
        self.cmb_template.clear()
        self.cmb_template.addItems(names)
        if cur in names:
            self.cmb_template.setCurrentText(cur)
        self.cmb_template.blockSignals(False)
        has = bool(names)
        self.cmb_template.setEnabled(has)
        self.btn_apply_tpl.setEnabled(has)
        self.btn_del_tpl.setEnabled(has)
        self.btn_save_tpl.setEnabled(True)

    def apply_template(self) -> bool:
        """套用当前选中的分析模板（数值节 → FR-25.1 横幅自动出现，不自动重跑）。"""
        name = self.cmb_template.currentText().strip()
        if not name:
            self.status.emit("没有可套用的分析模板")
            return False
        from core.render import plotstyle
        new_cfg, issues, applied = plotstyle.apply_analysis_preset(self.cfg, name)
        for it in issues:
            self.logged.emit(getattr(it, "level", "WARNING"),
                             f"[分析模板] {getattr(it, 'key', '')}: "
                             f"{getattr(it, 'message', it)}")
        if not applied:
            self.status.emit(f"模板「{name}」不存在或不可解析，参数未变")
            return False
        self.cfg = dict(new_cfg)
        self.persist_cfg()                 # 走既有持久化链路（F3 护栏不变）
        self.analysis_preset_applied.emit(self.cfg)
        self.status.emit(f"已套用分析模板「{name}」；如需重算请按 F5")
        self.logged.emit("INFO", f"已套用分析模板「{name}」"
                                 f"（{len(issues)} 条提示）")
        return True

    def save_template(self) -> bool:
        """把**当前 cfg 四节的差量**另存为模板（自动剔除等于默认值的键）。"""
        from PySide6.QtWidgets import QInputDialog
        from core.render import plotstyle

        diff = plotstyle.analysis_preset_diff(self.cfg)
        if not diff:
            self.status.emit("当前参数与默认值完全相同，没有可保存的差量")
            return False
        name, ok = QInputDialog.getText(self, "另存为分析模板",
                                        "模板名（只存 qc/基线/拟合/Φ 四节差量）：")
        if not ok or not str(name or "").strip():
            return False
        try:
            plotstyle.save_analysis_preset(str(name), diff)
        except ValueError as err:
            self.status.emit(f"模板未保存：{err}")
            return False
        self.refresh_templates()
        self.cmb_template.setCurrentText(str(name).strip())
        self.status.emit(f"已保存分析模板「{str(name).strip()}」")
        self.logged.emit("INFO", f"已保存分析模板「{str(name).strip()}」："
                                 f"节 {sorted(diff)}")
        return True

    def delete_template(self) -> bool:
        """删除当前模板（**破坏性确认保留模态**；其余反馈走状态栏）。"""
        from PySide6.QtWidgets import QMessageBox
        from core.render import plotstyle

        name = self.cmb_template.currentText().strip()
        if not name:
            return False
        ans = QMessageBox.question(
            self, "删除分析模板",
            f"确定删除分析模板「{name}」吗？（不影响已保存的 config.yaml）")
        if ans != QMessageBox.Yes:
            return False
        done = plotstyle.delete_analysis_preset(name)
        self.refresh_templates()
        self.status.emit(f"已删除分析模板「{name}」" if done
                         else f"模板「{name}」已不存在")
        self.logged.emit("INFO" if done else "WARNING",
                         f"删除分析模板「{name}」" + ("" if done else "失败"))
        return done

    def _apply_batch_analysis_preset(self) -> None:
        """批次文件的 `analysis_preset` → 套用一次（GUI 链路；CLI 走 pipeline）。"""
        if self.batch is None:
            return
        from core.render import plotstyle
        new_cfg, issues, applied = plotstyle.apply_batch_analysis_preset(
            self.cfg, self.batch)
        name = getattr(self.batch, "analysis_preset", "")
        for it in issues:
            self.logged.emit(getattr(it, "level", "WARNING"),
                             f"[批次模板] {getattr(it, 'key', '')}: "
                             f"{getattr(it, 'message', it)}")
        if not applied:
            if name:
                self.status.emit(f"批次要求的分析模板「{name}」不可用，已按当前参数继续")
            return
        self.cfg = dict(new_cfg)
        self.persist_cfg()
        self.analysis_preset_applied.emit(self.cfg)
        self.status.emit(f"已按批次文件套用分析模板「{name}」")
        self.logged.emit("INFO", f"[批次] 已套用分析模板「{name}」")

    # ── FR-24.9：把本页整定交给分析页（单向同步；不复制控件/状态） ──
    def session_overrides(self) -> dict:
        """当前会话的整定：染料会话覆盖 + 手动时间覆盖（供 ②分析 复用）。"""
        try:
            dye_of = dict(self.panel.dye_map())
        except Exception:                                  # noqa: BLE001
            dye_of = {}
        times = dict(getattr(self, "_time_overrides", None) or {})
        for s in getattr(self, "scans", None) or []:
            if getattr(s, "t_edited", False):
                times.setdefault(s.path.name, float(s.t))
        return {"dye_of": dye_of, "times": times}

    def _mark_suspects(self) -> None:
        """FR-19：把解析可疑的行**在表里**标出来（工具提示 + 汇总一条）。"""
        bad = suspicious_rows(self.scans)
        if not bad:
            self.lbl_suspect.setText("")
            return
        for s, why in bad[:60]:
            for r in range(self.files.table.rowCount()):
                it = self.files.table.item(r, 0)
                if it is not None and it.text() == s.path.name:
                    it.setToolTip(f"⚠ 可疑：{why}")
                    it.setForeground(Qt.darkYellow)
                    break
        detail = "；".join(f"{Path(s.path).name}（{w}）" for s, w in bad[:4])
        more = "" if len(bad) <= 4 else f" 等 {len(bad)} 个"
        self.lbl_suspect.setText(f"⚠ 命名/时间可疑{more}：{detail}")

    # ══════════════ 整定 ══════════════
    def _on_times_changed(self, overrides: dict) -> None:
        """时间工作表改动 → 立刻反映到文件表的时间列（不重算数值）。"""
        self._time_overrides = dict(overrides or {})

    def _on_selection(self, n: int) -> None:
        self.status.emit(f"已选 {n} 个文件")

    # ══════════════ V2.9 FR-60：三态/单位联动 ══════════════
    def _on_file_time_changed(self, _name: str, _t: float) -> None:
        """V29-O2：文件表改时间后同步刷新时间工作表（同一数据对象，仅显示同步）。"""
        self.panel.refresh()
        self._mark_suspects()

    def _refresh_after_panel(self) -> None:
        """时间工作表三态/单位变化后刷新文件表与下游批次（不重算数值）。"""
        self.files.set_scans(self.scans)
        self._mark_suspects()
        self.batch_changed.emit(self.scans)

    # ══════════════ 批次落盘（FR-14） ══════════════
    def save_batch(self) -> None:
        """把当前整定写成数据目录里的 ``dye_batch.yaml``（显式动作，严出）。"""
        if not self.scans or self.data_dir is None:
            self.status.emit("请先载入数据目录")
            return
        from core.inputs.batch_file import from_scans, save_batch

        batch = from_scans(self.scans, self.data_dir, base=self.batch)
        # 染料命名：文件表/整定面板里的会话覆盖也一并落盘
        for group, dye in (self.panel.dye_map() or {}).items():
            from core.report.dyes import auto_dye_name
            if not dye or str(dye).strip() == str(auto_dye_name(group)).strip():
                continue
            for s in self.scans:
                if s.group == group:
                    try:
                        rel = Path(s.path).resolve().relative_to(
                            Path(self.data_dir).resolve()).as_posix()
                    except (ValueError, OSError):
                        rel = Path(s.path).name
                    batch.set_entry(rel, dye=str(dye).strip())
        ok, why = save_batch(batch, self.data_dir)
        self.batch = batch
        if ok:
            self.logged.emit("INFO", why)
            self.batch_saved.emit(why)
        else:
            self.logged.emit("WARNING", why)
            self.batch_saved.emit(why)

    # ══════════════ QC 体检（P8，与 --dashboard 同源） ══════════════
    def _export_qc(self) -> None:
        from core.analysis.qc_dashboard import check_scans, render_dashboard
        out = app_root() / "runs"
        cfg = getattr(self, "_qc_cfg", self.cfg)

        def task(progress_cb, log_cb):
            progress_cb(20, "体检")
            rep = check_scans(self.scans, cfg)
            out.mkdir(parents=True, exist_ok=True)
            p = render_dashboard(self.scans, cfg, out / "qc_dashboard.png")
            return rep, p

        def done(res) -> None:
            rep, p = res
            s = rep.get("summary") or {}
            self.logged.emit("INFO", f"QC 体检：{s.get('n_usable')}/"
                                     f"{s.get('n_files')} 可用 → {p}")

        self.run_task(task, "QC 体检", done, busy_widgets=[self.btn_qc])

    # ══════════════ 生命周期 ══════════════
    def on_activated(self) -> None:
        self.refresh_templates()
        """页被切到时刷新染料列（会话里可能刚改过命名）。"""
        refresh = getattr(self.files, "refresh_dye_column", None)
        if callable(refresh):
            refresh()
