# -*- coding: utf-8 -*-
"""功能② 单线态氧工作台（V2.9.3 FR-65；旧两组对比路径保留）。

载入后按谱形指纹自动分组（§FR-2.1），允许**手动改组**（把成员移入第 1/第 2 组、
重命名标签）；选定两组后拟合 exp2/exp3、给出 k₁/k₂ 比值，可选按激发波长吸光度
归一（``f_abs < 0.01`` 时禁用并提示）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from core.config import autogroup_cfg
from core.grouping import auto_group, items_from_scans
from core.io import collect_scans
from core.pipeline import run_workbench
from ui.pages.base_page import PageBase
from ui.pages.phi_page import ResultActions
from ui.widgets.export_bar import ExportBar
from ui.widgets.mpl_canvas import CanvasPanel


def _dspin(lo, hi, val, step=1.0, dec=2):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSingleStep(step)
    w.setValue(float(val))
    return w


class ComparePage(PageBase):
    """单线态氧工作台页（N 组 k_obs + Φ_Δ；旧 run_compare 仍保留）。"""

    #: V2.6 FR-40.5：结果区「下一步」→ ③ 图件 / 定稿出图
    next_step_requested = Signal(str)

    TITLE = "② 单线态氧工作台（k_obs 拟合 + Φ_Δ）"
    SUBTITLE = ("载入光谱目录 → 自动分组/手动改组 → 逐组拟合 k_obs → "
                "指定任一组为参比 → 同页出 Φ_Δ 与汇总表。")

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.items: list[dict] = []
        self.groups: list[dict] = []
        self.invalid: list[str] = []
        self.batch = None                 # V2.9.1 FR-63：当前目录 dye_batch.yaml
        self._last_output = None
        self._last_result_text = ""
        self._build()
        self.setAcceptDrops(True)
        # V2.9.3 FR-68：工作台关键控件悬停说明 + 灰按钮动态原因
        from ui.help_texts import bind_disabled_reason
        self.btn_run.setToolTip("跑工作台：逐组拟合 k_obs，并给出 Φ_Δ 与汇总表")
        self.btn_group.setToolTip("按母核区谱形指纹重新自动分组")
        self.chk_norm.setToolTip("按激发波长吸光度归一（仅写进对比链旧路径；工作台 Φ 使用 A_ex）")
        self._run_reason = bind_disabled_reason(
            self.btn_run, lambda: ("" if self.data_dir else "先选择数据目录")
            or ("" if self.groups else "请至少分出 1 个组"))

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        self.btn_dir = QPushButton("选择数据目录…")
        self.btn_group = QPushButton("重新分组")
        self.btn_run = QPushButton("▶ 开始对比")
        self.btn_run.setStyleSheet("font-weight: bold;")
        self.lbl_dir = QLabel("未选择目录")
        self.lbl_dir.setStyleSheet("color: #555;")
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_group.clicked.connect(self._regroup)
        self.btn_run.clicked.connect(self._run)
        for b in (self.btn_group, self.btn_run):
            b.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(self.btn_dir)
        top.addWidget(self.btn_group)
        top.addWidget(self.lbl_dir, 1)
        top.addWidget(self.btn_run)

        # ── 左：分组参数 + 组列表 ──
        ag = autogroup_cfg(self.cfg)
        self.sp_lo = _dspin(200, 800, ag["core_lo"], 5.0, 1)
        self.sp_hi = _dspin(200, 900, ag["core_hi"], 5.0, 1)
        self.sp_step = _dspin(0.5, 50, ag["step"], 0.5, 1)
        self.sp_dmax = _dspin(0.001, 1.0, ag["d_max"], 0.01, 4)
        box_ag = QGroupBox("分组参数（母核区谱形指纹）")
        lag = QVBoxLayout(box_ag)
        for label, w in (("母核区起 (nm)", self.sp_lo), ("母核区止 (nm)", self.sp_hi),
                         ("重采样步长 (nm)", self.sp_step), ("RMS 距离阈值", self.sp_dmax)):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(w, 1)
            lag.addLayout(row)

        self.tbl_groups = QTableWidget(0, 4)
        self.tbl_groups.setHorizontalHeaderLabels(["组标签", "文件数", "母核 λmax", "时间范围"])
        self.tbl_groups.setEditTriggers(QAbstractItemView.DoubleClicked)
        self.tbl_groups.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_groups.verticalHeader().setVisible(False)
        self.tbl_groups.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tbl_groups.itemChanged.connect(self._on_label_changed)
        self.tbl_groups.itemSelectionChanged.connect(self._on_group_selected)
        box_g = QGroupBox("自动分组结果（双击标签可改名）")
        lg = QVBoxLayout(box_g)
        lg.addWidget(self.tbl_groups)

        self.lbl_invalid = QLabel("无法归组：—")
        self.lbl_invalid.setWordWrap(True)
        self.lbl_invalid.setStyleSheet("color: #B8860B;")
        lg.addWidget(self.lbl_invalid)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(box_ag)
        ll.addWidget(box_g, 1)
        scroll = QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(360)
        scroll.setMaximumWidth(460)

        # ── 右：成员表 + 选择两组 + 图件 ──
        self.tbl_members = QTableWidget(0, 4)
        self.tbl_members.setHorizontalHeaderLabels(["文件名", "t (s)", "母核 λmax",
                                                    "探针 379 nm"])
        self.tbl_members.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_members.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_members.verticalHeader().setVisible(False)
        self.tbl_members.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        box_m = QGroupBox("选中组成员（可多选后移入某组）")
        lm = QVBoxLayout(box_m)
        lm.addWidget(self.tbl_members)
        mv = QHBoxLayout()
        self.btn_to1 = QPushButton("移入第 1 组")
        self.btn_to2 = QPushButton("移入第 2 组")
        self.btn_new = QPushButton("拆成新组")
        self.btn_to1.clicked.connect(lambda: self._move_selected(1))
        self.btn_to2.clicked.connect(lambda: self._move_selected(2))
        self.btn_new.clicked.connect(self._split_new_group)
        for b in (self.btn_to1, self.btn_to2, self.btn_new):
            mv.addWidget(b)
        lm.addLayout(mv)

        self.tbl_results = QTableWidget(0, 9)
        self.tbl_results.setHorizontalHeaderLabels(
            ["组标签", "文件数", "时间范围", "k_obs ± σ (s⁻¹)", "R²", "n",
             "截断", "Φ_Δ ± σ", "备注"])
        self.tbl_results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_results.verticalHeader().setVisible(False)
        self.tbl_results.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        box_res = QGroupBox("工作台逐样结果")
        lres = QVBoxLayout(box_res)
        lres.addWidget(self.tbl_results)

        pick = QHBoxLayout()
        self.cmb_a = QComboBox()
        self.cmb_b = QComboBox()
        self.ed_la = QLineEdit()
        self.ed_lb = QLineEdit()
        pick.addWidget(QLabel("第 1 组"))
        pick.addWidget(self.cmb_a)
        pick.addWidget(self.ed_la)
        pick.addWidget(QLabel("第 2 组"))
        pick.addWidget(self.cmb_b)
        pick.addWidget(self.ed_lb)
        self.chk_norm = QCheckBox("按激发波长吸光度归一（f_abs<0.01 时自动禁用）")
        # V2.9.1 FR-62/63：参比侧 + Φ_ref（来源 = 既有 config quantum_yield.phi_ref）
        self.cmb_ref_side = QComboBox()
        self.cmb_ref_side.addItems(["A（第 1 组为参比）", "B（第 2 组为参比）"])
        self.cmb_ref_side.setToolTip("选哪一组是已知 Φ_ref 的参比样；另一组输出 Φ_Δ±σ")
        self.sp_phi_ref = _dspin(0.0001, 10.0,
                                 (self.cfg.get("quantum_yield") or {}).get("phi_ref", 0.75),
                                 0.05, 4)
        self.sp_phi_ref.setToolTip("参比样的 Φ_Δ（既有 config quantum_yield.phi_ref 初值，仅会话态；可记住到 dye_batch.yaml）")
        self.btn_remember = QPushButton("记住参比/Φ_ref…")
        self.btn_remember.setToolTip("把当前参比侧与 Φ_ref 写入本目录 dye_batch.yaml（显式动作，宽进严出）")
        self.btn_remember.clicked.connect(self._remember_prefs)
        pick2 = QHBoxLayout()
        pick2.addWidget(QLabel("参比侧"))
        pick2.addWidget(self.cmb_ref_side)
        pick2.addWidget(QLabel("Φ_ref"))
        pick2.addWidget(self.sp_phi_ref)
        pick2.addWidget(self.btn_remember)
        pick2.addStretch(1)
        # ── V2.9.3 FR-65/66/67：工作台参比与图式/增强 ──
        self.cmb_ref = QComboBox()
        self.cmb_ref.setToolTip("选任一组为参比；未指定则 Φ_Δ 留空（k 仍拟合）")
        self.cmb_style = QComboBox()
        self.cmb_style.addItems(["柱状（默认）", "曲线", "散点"])
        self.chk_err = QCheckBox("误差棒")
        self.chk_err.setChecked(True)
        self.chk_annotate = QCheckBox("关键值标注")
        self.chk_annotate.setChecked(True)
        self.cmb_unit = QComboBox()
        self.cmb_unit.addItems(["s", "min", "h"])
        self.chk_transparent = QCheckBox("透明背景")
        self.chk_ci = QCheckBox("bootstrap 置信带")
        self.chk_fmt = {k: QCheckBox(k) for k in ("png", "svg", "pdf", "jpg")}
        self.chk_fmt["png"].setChecked(True)
        pick3 = QHBoxLayout()
        pick3.addWidget(QLabel("参比"))
        pick3.addWidget(self.cmb_ref)
        pick3.addWidget(QLabel("图式"))
        pick3.addWidget(self.cmb_style)
        pick3.addWidget(self.chk_err)
        pick3.addWidget(QLabel("时间轴"))
        pick3.addWidget(self.cmb_unit)
        pick3.addWidget(self.chk_annotate)
        pick3.addWidget(self.chk_transparent)
        pick3.addWidget(self.chk_ci)
        pick4 = QHBoxLayout()
        pick4.addWidget(QLabel("导出格式"))
        for _k, _cb in self.chk_fmt.items():
            pick4.addWidget(_cb)
        pick4.addStretch(1)
        box_pick = QGroupBox("对比设置")
        lp = QVBoxLayout(box_pick)
        lp.addLayout(pick)
        lp.addLayout(pick2)
        lp.addLayout(pick3)
        lp.addLayout(pick4)
        lp.addWidget(self.chk_norm)

        self.canvas = CanvasPanel()
        box_fig = QGroupBox("对比图与 JACS 堆叠图")
        lfig = QVBoxLayout(box_fig)
        lfig.addWidget(self.canvas)

        self.lbl_ratio = QLabel("k₁/k₂ = —")
        self.lbl_ratio.setStyleSheet("font-size: 14px; font-weight: bold;")
        box_r = QGroupBox("结果")
        lr = QVBoxLayout(box_r)
        lr.addWidget(self.lbl_ratio)

        right = QSplitter(Qt.Vertical)
        right.addWidget(box_m)
        right.addWidget(box_res)
        right.addWidget(box_pick)
        right.addWidget(box_fig)
        right.addWidget(box_r)
        right.setSizes([150, 150, 150, 380, 70])

        body = QSplitter(Qt.Horizontal)
        body.addWidget(scroll)
        body.addWidget(right)
        body.setSizes([400, 980])

        self.export = ExportBar()
        # V2.3 FR-24.2/24.6：导出条与画布的常规反馈走状态栏（不弹模态）
        self.export.message.connect(self.status)
        self.canvas.status_message.connect(self.status)
        lay = QVBoxLayout(self)
        lay.addWidget(self.header())
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        lay.addLayout(top)
        lay.addWidget(body, 1)
        lay.addWidget(self.progress_row())
        # V2.6 FR-40.5：结果区动作条（打开输出文件 / 复制结果 / 下一步 / 重试）
        self.actions = ResultActions(self)
        self.actions.message.connect(self.status)
        self.actions.next_step_requested.connect(self.next_step_requested.emit)
        self.actions.retry_requested.connect(self._run)
        lay.addWidget(self.actions)
        lay.addWidget(self.export)

    # ══════════════ 载入与分组 ══════════════
    def _choose_dir(self) -> None:
        d = self.pick_dir()
        if d:
            self._load_dir(d)

    def dragEnterEvent(self, e) -> None:               # noqa: N802
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:                    # noqa: N802
        urls = [Path(u.toLocalFile()) for u in e.mimeData().urls()]
        dirs = [p for p in urls if p.is_dir()]
        if dirs:
            self._load_dir(dirs[0])
        elif urls:
            self._load_dir(urls[0].parent)
        e.acceptProposedAction()

    def _ag(self) -> dict:
        return {"core_lo": self.sp_lo.value(), "core_hi": self.sp_hi.value(),
                "step": self.sp_step.value(), "d_max": self.sp_dmax.value()}

    def _load_dir(self, d: Path) -> None:
        self.data_dir = Path(d)
        self.lbl_dir.setText(f"数据目录：{self.data_dir}")
        self.remember_dir(self.data_dir)
        cfg = self.cfg

        def task(progress_cb, log_cb):
            scans = collect_scans(self.data_dir, cfg, progress_cb=progress_cb)
            items, bad = items_from_scans(scans)
            from core.inputs.batch_file import load_batch
            batch = load_batch(self.data_dir)
            return scans, items, bad, batch

        self.run_task(task, "载入并分组", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.btn_group, self.btn_run])

    def _on_loaded(self, res) -> None:
        scans, items, bad, batch = res
        self.items = items
        self.batch = batch
        # V2.9.1 FR-63：不改输入 → 用 config 初值；有批次记忆 → 用记忆值
        qy = (self.cfg.get("quantum_yield") or {})
        phi = (batch.phi_ref if batch is not None and batch.phi_ref is not None
               else qy.get("phi_ref", 0.75))
        self.sp_phi_ref.setValue(float(phi))
        if not items:
            self._error("该目录没有可解析的光谱")
            return
        self.btn_group.setEnabled(True)
        self._do_group()

    def _error(self, msg: str) -> None:
        """记 ERROR 日志并弹提示（不抛异常）。"""
        self.logged.emit("ERROR", msg)
        QMessageBox.warning(self, "提示", msg)

    def _regroup(self) -> None:
        if self.items:
            self._do_group()

    def _do_group(self) -> None:
        groups, invalid = auto_group(self.items, self._ag())
        self.groups = groups
        self.invalid = invalid
        self._refresh_groups()
        # V2.9.1 FR-61/63：批次记忆的显式参比若匹配某个组标签 → 预选参比侧
        if self.batch is not None and getattr(self.batch, "reference_group", ""):
            ref = str(self.batch.reference_group)
            i = self.cmb_ref.findData(ref)
            if i >= 0:
                self.cmb_ref.setCurrentIndex(i)
        if not groups:
            self._error("一个组都没分出来，请调整母核区或 RMS 阈值")
            return
        self.btn_run.setEnabled(len(groups) >= 1)
        if len(groups) < 2:
            self.logged.emit("WARNING", "只检测到 1 个组；请调整分组参数或改用目录内多个系列")
        self.logged.emit("INFO", f"自动分组完成：{len(groups)} 组，"
                                 f"{len(invalid)} 个文件母核区残缺无法归组")

    def _refresh_groups(self) -> None:
        self.tbl_groups.blockSignals(True)
        self.tbl_groups.setRowCount(len(self.groups))
        for r, g in enumerate(self.groups):
            ts = [m["t"] for m in g["members"]]
            lm = f"{g['lam_max']:.0f} nm" if g.get("lam_max") is not None else "?"
            vals = [g["label"], str(len(g["members"])), lm,
                    f"{min(ts):.0f}–{max(ts):.0f} s" if ts else "—"]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(str(t))
                if c == 0:
                    it.setFlags(it.flags() | Qt.ItemIsEditable)
                else:
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                self.tbl_groups.setItem(r, c, it)
        self.tbl_groups.blockSignals(False)
        self.lbl_invalid.setText("无法归组：" + (", ".join(self.invalid) if self.invalid else "—"))
        # 下拉框
        names = [f"{i + 1}. {g['label']} ({len(g['members'])})"
                 for i, g in enumerate(self.groups)]
        for cmb, ed in ((self.cmb_a, self.ed_la), (self.cmb_b, self.ed_lb)):
            cmb.blockSignals(True)
            cmb.clear()
            cmb.addItems(names)
            cmb.blockSignals(False)
        if len(self.groups) >= 2:
            self.cmb_a.setCurrentIndex(0)
            self.cmb_b.setCurrentIndex(1)
        self.ed_la.setText(self.groups[0]["label"] if self.groups else "")
        self.ed_lb.setText(self.groups[1]["label"] if len(self.groups) > 1 else "")
        # V2.9.3 FR-65：参比下拉 = 所有组（保留已有选择；批次初值后置）
        prev_ref = self.cmb_ref.currentData() if self.cmb_ref.count() else ""
        self.cmb_ref.blockSignals(True)
        self.cmb_ref.clear()
        self.cmb_ref.addItem("（不指定参比）", "")
        for g in self.groups:
            self.cmb_ref.addItem(str(g.get("label", "")), str(g.get("label", "")))
        if prev_ref:
            i = self.cmb_ref.findData(str(prev_ref))
            if i >= 0:
                self.cmb_ref.setCurrentIndex(i)
        self.cmb_ref.blockSignals(False)

    def _fill_members(self, gi: int) -> None:
        """显示第 gi 组的成员。"""
        self.tbl_members.setRowCount(0)
        if not (0 <= gi < len(self.groups)):
            return
        members = self.groups[gi]["members"]
        self.tbl_members.setRowCount(len(members))
        from core.kinetics import probe_abs
        lam = float(self.cfg["wavelength"]["probe"])
        win = float(self.cfg["wavelength"]["probe_window"])
        for r, m in enumerate(members):
            a = probe_abs(m["wl"], m["ab"], lam, win)
            vals = [m["name"], f"{m['t']:.0f}", "—",
                    f"{a:.4f}" if a is not None else "无数据(不参与拟合)"]
            for c, t in enumerate(vals):
                it = QTableWidgetItem(str(t))
                if c == 3 and a is None:
                    it.setToolTip("该文件在探针波长缺数据：照常归组但不参与拟合（与 legacy 一致）")
                self.tbl_members.setItem(r, c, it)

    def _on_group_selected(self) -> None:
        rows = {i.row() for i in self.tbl_groups.selectedIndexes()}
        if rows:
            self._fill_members(sorted(rows)[0])

    def _on_label_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        r = item.row()
        if 0 <= r < len(self.groups):
            self.groups[r]["label"] = item.text().strip() or f"组{r + 1}"
            self._refresh_groups()

    def _selected_member_names(self) -> list[str]:
        rows = {i.row() for i in self.tbl_members.selectedIndexes()}
        return [self.tbl_members.item(r, 0).text() for r in sorted(rows)
                if self.tbl_members.item(r, 0)]

    def _move_selected(self, target: int) -> None:
        """把成员表中的选中项移入第 1/第 2 组（FR-2.1 手动改组）。"""
        gsel = {i.row() for i in self.tbl_groups.selectedIndexes()}
        if not gsel:
            self.status.emit("请先在左侧选中一个来源组")
            return
        src = sorted(gsel)[0]
        names = set(self._selected_member_names())
        if not names:
            self.status.emit("请先在成员表中选择要移动的文件")
            return
        if not (1 <= target <= len(self.groups)) or (target - 1) == src:
            return
        move = [m for m in self.groups[src]["members"] if m["name"] in names]
        if not move:
            return
        self.groups[src]["members"] = [m for m in self.groups[src]["members"]
                                       if m["name"] not in names]
        self.groups[target - 1]["members"] += move
        self.groups[target - 1]["members"].sort(key=lambda m: m["t"])
        self.groups = [g for g in self.groups if g["members"]]
        self._refresh_groups()
        self.logged.emit("INFO", f"已移动 {len(move)} 个文件到第 {target} 组")

    def _split_new_group(self) -> None:
        """把选中成员拆成一个新组。"""
        gsel = {i.row() for i in self.tbl_groups.selectedIndexes()}
        if not gsel:
            return
        src = sorted(gsel)[0]
        names = set(self._selected_member_names())
        if not names:
            return
        move = [m for m in self.groups[src]["members"] if m["name"] in names]
        if not move:
            return
        self.groups[src]["members"] = [m for m in self.groups[src]["members"]
                                       if m["name"] not in names]
        self.groups.append({"members": sorted(move, key=lambda m: m["t"]),
                            "lam_max": None, "label": f"自定义组{len(self.groups) + 1}"})
        self.groups = [g for g in self.groups if g["members"]]
        self._refresh_groups()
        self.btn_run.setEnabled(len(self.groups) >= 1)

    # ══════════════ 运行对比 ══════════════
    def _pick_groups(self) -> Optional[tuple[dict, dict]]:
        if len(self.groups) < 2:
            self.status.emit("至少需要 2 个组才能对比")
            return None
        ia, ib = self.cmb_a.currentIndex(), self.cmb_b.currentIndex()
        if ia == ib:
            self.status.emit("请选择两个不同的组")
            return None
        ga = dict(self.groups[ia])
        gb = dict(self.groups[ib])
        ga["label"] = self.ed_la.text().strip() or ga["label"]
        gb["label"] = self.ed_lb.text().strip() or gb["label"]
        return ga, gb

    def _remember_prefs(self) -> bool:
        """V2.9.3 FR-65：把工作台参比组+Φ_ref 写入 dye_batch.yaml。"""
        if self.data_dir is None or not self.groups:
            self.status.emit("请先载入并分出至少一组")
            return False
        if self.cmb_ref.count():
            ref_label = str(self.cmb_ref.currentData() or "")
        else:
            ref_label = str(self.groups[0].get("label", "") if self.groups else "")
        from core.inputs.batch_file import load_batch, save_batch
        try:
            batch = self.batch if self.batch is not None else load_batch(self.data_dir)
            batch.reference_group = ref_label
            batch.phi_ref = float(self.sp_phi_ref.value())
            ok, why = save_batch(batch, self.data_dir)
        except Exception as err:                           # noqa: BLE001
            self.logged.emit("WARNING", f"记住参比/Φ_ref 失败：{err}")
            return False
        if ok:
            self.batch = batch
            self.logged.emit("INFO", f"[批次] 已记住参比 {ref_label or '自动/无'} / Φ_ref={self.sp_phi_ref.value():g}")
            self.status.emit(why)
        else:
            self.logged.emit("WARNING", why)
        return bool(ok)

    def _run(self) -> None:
        if not self.groups:
            self.status.emit("请先载入并完成分组")
            return
        items = self.items
        cfg = dict(self.cfg)
        if self.cmb_ref.count():
            ref_label = str(self.cmb_ref.currentData() or "")
        else:
            ref_label = str(self.groups[0].get("label", "") if self.groups else "")
        phi_ref = float(self.sp_phi_ref.value())
        style = {0: "bar", 1: "line", 2: "scatter"}.get(self.cmb_style.currentIndex(), "bar")
        formats = tuple(k for k, cb in self.chk_fmt.items() if cb.isChecked()) or ("png",)
        opts = dict(ref_label=ref_label, phi_ref=phi_ref, style=style,
                    error_bars=self.chk_err.isChecked(),
                    time_unit=self.cmb_unit.currentText(),
                    annotate=self.chk_annotate.isChecked(),
                    transparent=self.chk_transparent.isChecked(),
                    formats=formats, ci_band=self.chk_ci.isChecked(),
                    bootstrap=self.chk_ci.isChecked())

        def task(progress_cb, log_cb):
            return run_workbench(items, self.groups, cfg, progress_cb=progress_cb,
                                 log_cb=log_cb, **opts)

        self.actions.reset()
        self.run_task(task, "单线态氧工作台", self._finish_workbench,
                      busy_widgets=[self.btn_run, self.btn_dir, self.btn_group])

    # ── V2.6 FR-40.5：结果区动作（打开 / 复制 / 下一步 / 重试） ──
    @staticmethod
    def _fmt(v, spec: str = ".4g") -> str:
        try:
            return format(float(v), spec)
        except (TypeError, ValueError):
            return "—"

    def _format_compare_lines(self, res) -> str:
        """把对比结果拼成可复制的纯文本（只复述已有字段，不新增判断）。"""
        lines = ["两组 kobs 对比结果"]
        out_dir = str(getattr(res, "out_dir", "") or "")
        if out_dir:
            lines.append(f"输出目录：{out_dir}")
        ga = getattr(res, "group_a", None) or {}
        gb = getattr(res, "group_b", None) or {}
        la = str(ga.get("label", "第1组")) if isinstance(ga, dict) else "第1组"
        lb = str(gb.get("label", "第2组")) if isinstance(gb, dict) else "第2组"
        ratio = getattr(res, "ratio", None)
        if ratio is None:
            lines.append(f"k1/k2 = —（{la} / {lb}）")
        else:
            lines.append(f"k1/k2 = {self._fmt(ratio)}（{la} / {lb}）")
            corrected = getattr(res, "ratio_corrected", None)
            if corrected is not None:
                lines.append(f"按激发波长吸光归一后：{self._fmt(corrected)}")
            pa, pb = getattr(res, "phi_a", None), getattr(res, "phi_b", None)
            ea, eb = getattr(res, "phi_err_a", None), getattr(res, "phi_err_b", None)
            if pa is not None or pb is not None:
                if pa is not None:
                    lines.append(f"第1组 Φ_Δ = {self._fmt(pa)} ± {self._fmt(ea)}")
                if pb is not None:
                    lines.append(f"第2组 Φ_Δ = {self._fmt(pb)} ± {self._fmt(eb)}")
                if getattr(res, "ref_side", ""):
                    lines.append(f"参比侧 = {res.ref_side}；Φ_ref = {self._fmt(getattr(res, 'phi_ref', None))}")
        for tag, fit in (("第1组", getattr(res, "fit_a", None)),
                         ("第2组", getattr(res, "fit_b", None))):
            if not isinstance(fit, dict) or not fit.get("ok"):
                continue
            lines.append(f"{tag} 拟合：k={self._fmt(fit.get('k'), '.4e')} s⁻¹, "
                         f"R²={self._fmt(fit.get('R2'), '.4f')}")
        if len(lines) <= (2 if out_dir else 1):        # 退化：直接抄结果标签
            lines.append(self.lbl_ratio.text() or "—")
        return "\n".join(lines)

    def _failure_hint(self, res) -> str:
        return ("如何修复：" + str(getattr(res, "message", "") or "对比失败")
                + "；检查是否选到 2 个不同组、每组是否有足够时间点参与拟合、"
                  "探针波长处是否有数据、分组参数是否合理；确认后点「重试」重新对比。")

    def _finish_compare(self, res) -> None:
        """run_task 完成回调：先刷新结果区动作条，再走既有 _on_done_compare。"""
        try:
            ok = bool(getattr(res, "ok", False))
            out_dir = str(getattr(res, "out_dir", "") or "")
            text = self._format_compare_lines(res)
            self._last_output = out_dir or None
            self._last_result_text = text
            self.actions.set_output(self._last_output if ok else None)
            self.actions.set_result_text(text)
            if ok:
                self.actions.set_state(True)
            else:
                self.actions.set_state(False,
                                       failure_hint=self._failure_hint(res))
        except Exception as err:                       # noqa: BLE001 —— 动作条不得废页
            self.logged.emit("WARNING", f"对比结果区动作条刷新失败：{err}")
        self._on_done_compare(res)

    def _format_workbench_lines(self, res) -> str:
        lines = ["单线态氧工作台结果"]
        out = str(getattr(res, "out_dir", "") or "")
        if out:
            lines.append(f"输出目录：{out}")
        for r in getattr(res, "rows", []) or []:
            k = self._fmt(r.get("k_obs"), ".4e")
            ke = self._fmt(r.get("k_err"), ".2g")
            p = self._fmt(r.get("phi_delta"), ".4g")
            pe = self._fmt(r.get("phi_err"), ".2g")
            lines.append(f"{r.get('label', '?')}: k={k} ± {ke} s⁻¹; Φ_Δ={p} ± {pe}"
                         + (f"（{r.get('note')}）" if r.get('note') else ""))
        return "\n".join(lines)

    def _fill_result_table(self, rows) -> None:
        self.tbl_results.setRowCount(len(rows))
        for i, r in enumerate(rows):
            t_range = "—"
            if r.get("t_min") is not None and r.get("t_max") is not None:
                t_range = f"{float(r['t_min']):.0f}–{float(r['t_max']):.0f} s"
            vals = [str(r.get("label", "")), str(r.get("file_count", "—")), t_range,
                    f"{self._fmt(r.get('k_obs'), '.4e')} ± {self._fmt(r.get('k_err'), '.2g')}",
                    self._fmt(r.get("r2"), ".4f"), str(r.get("n", "")),
                    "是" if r.get("truncated") else "—",
                    f"{self._fmt(r.get('phi_delta'), '.4g')} ± {self._fmt(r.get('phi_err'), '.2g')}",
                    str(r.get("note", "") or "—")]
            for c, v in enumerate(vals):
                self.tbl_results.setItem(i, c, QTableWidgetItem(str(v)))

    def _finish_workbench(self, res) -> None:
        ok = bool(getattr(res, "ok", False))
        out = str(getattr(res, "out_dir", "") or "")
        text = self._format_workbench_lines(res)
        self._last_output = out or None
        self._last_result_text = text
        self.actions.set_output(self._last_output if ok else None)
        self.actions.set_result_text(text)
        if ok:
            self.actions.set_state(True)
        else:
            self.actions.set_state(False, failure_hint=("如何修复：" + str(getattr(res, "message", "") or "工作台失败")))
        self._fill_result_table(getattr(res, "rows", []) or [])
        if out:
            self.export.set_run_dir(out)
            imgs = [p for p in Path(out).glob("workbench_*.png")]
            if imgs:
                self.canvas.set_images(imgs)
        if not ok:
            self.logged.emit("ERROR", str(getattr(res, "message", "") or "工作台失败"))
            self.lbl_ratio.setText("工作台失败：" + str(getattr(res, "message", "") or ""))
            return
        n = len(getattr(res, "rows", []) or [])
        nphi = sum(1 for r in res.rows if r.get("phi_delta") is not None)
        self.lbl_ratio.setText(f"工作台完成：{n} 组，Φ_Δ 出值 {nphi} 组"
                               + (f"；参比 {res.ref_label}" if res.ref_label else "；未指定参比"))
        self.status.emit(f"工作台完成 — {out}")
    def _on_done_compare(self, res) -> None:
        if res.out_dir:
            self.export.set_run_dir(res.out_dir)
            self.canvas.set_images(list(Path(res.out_dir).glob("*.png")))
        if not res.ok:
            self.logged.emit("ERROR", res.message or "对比失败")
            self.lbl_ratio.setText("k₁/k₂ = —（" + (res.message or "失败") + "）")
            return
        if res.ratio is None:
            self.lbl_ratio.setText("k₁/k₂ = —（至少一组拟合失败）")
        else:
            txt = (f"k₁/k₂ = {res.ratio:.4g}   "
                   f"（{res.group_a['label']} 的 ¹O₂ 生成速率约为 "
                   f"{res.group_b['label']} 的 {res.ratio:.4g} 倍）")
            if res.ratio_corrected:
                txt += f"\n按激发波长吸光归一后：{res.ratio_corrected:.4g}"
            if getattr(res, "phi_a", None) is not None or getattr(res, "phi_b", None) is not None:
                txt += (f"\nΦ_Δ：第1组 {res.phi_a if res.phi_a is not None else '—'} ± "
                        f"{res.phi_err_a if res.phi_err_a is not None else '—'}；"
                        f"第2组 {res.phi_b if res.phi_b is not None else '—'} ± "
                        f"{res.phi_err_b if res.phi_err_b is not None else '—'}")
            self.lbl_ratio.setText(txt)
        for tag, f in (("第1组", res.fit_a), ("第2组", res.fit_b)):
            if f.get("ok"):
                self.logged.emit("INFO", f"{tag} {f['model']} 拟合成功："
                                         f"k={f['k']:.4e} ± {f['k_err'] or float('nan'):.2g}, "
                                         f"R²={f['R2']:.4f}")
            else:
                self.logged.emit("WARNING", f"{tag} 拟合失败：{f.get('msg')}")
        self.status.emit(f"对比完成 — {res.out_dir}")

    def on_activated(self) -> None:
        if self.data_dir and not self.items:
            self._load_dir(self.data_dir)
