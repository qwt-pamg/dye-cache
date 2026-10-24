"""② ε 任务卡（V2.6 FR-39；混搭工作台：流程条 / 结论卡 + 浓度表 / 分步检查器）"""

from __future__ import annotations

import logging
import math

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.paths import app_root
from ui.pages.base import PageBase
from ui.widgets.export_bar import open_folder
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.result_actions import ResultActions
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.verdict import VerdictCard
from ui.pages.tasks.common import _ParamTable, _mark_param_cell, _park_unshown
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

#: 流程条四步；检查器第 i 页、:meth:`ExtinctionPanel._step_summary` 都按这个顺序
STEPS = ("导入", "填浓度", "计算 ε", "导出")
STEP_HINTS = (
    "选光谱目录；数据页载入批次后这里会自动跟上",
    "浓度按文件解析顺序逐行填（行序 = 程序读取文件的顺序，不会错位）；默认留空，不替你猜值",
    "按 Beer–Lambert 线性拟合 A–c，斜率 / 光程即 ε；计算方法与命令行 --extinction 相同",
    "每次计算都会写出 ε 表（含逐点明细与告警）；在这里打开或复制",
)

#: 视为"微摩尔"的浓度单位写法（与 core.analysis.extinction 一致）
_MICRO_UNITS = frozenset({"um", "µm", "μm"})


def _eps_text(v: float) -> str:
    """ε 的大号显示：常见量级用千分位整数，过大/过小用科学计数。"""
    if not math.isfinite(v):
        return "—"
    if 1e3 <= abs(v) < 1e8:
        return f"{v:,.0f}"
    return f"{v:.4g}"


class ExtinctionPanel(PageBase):
    """P6 GUI 化：摩尔消光系数 ε（V2.6 FR-39.1/39.2：浓度表格化）。

    与 CLI ``--extinction`` **共用同一个 core 函数**（``compute_extinction`` +
    ``write_extinction_table``），不复制实现。表格行 = 光谱文件，行序来自
    :func:`core.inputs.io.list_scan_files`（与 ``collect_scans`` **同一枚举/排序**，
    Q-E11 单一顺序源）；浓度逐行填写，提交仍写
    ``cfg["extinction"]["concentrations"]``（与 CLI/配置文件同一条路）。

    布局同 Φ_Δ 页：流程条（交给分析页顶部细条）/ 中栏结论卡 + 浓度表 + 结果区 /
    右栏分步检查器。流程条只决定"现在看哪一步"，不强制顺序。
    """

    TITLE = "摩尔消光系数 ε（P6）"
    SUBTITLE = ("载入光谱序列 → 表格按文件解析顺序逐行填浓度 → 计算 ε。"
                "计算方法与命令行 --extinction 相同。")

    #: 浓度列下标（0=序号、1=文件名、2=浓度）
    CONC_COL = 2

    #: V2.6 FR-40.5：结果区「下一步」→ ③ 图件 / 定稿出图
    next_step_requested = Signal(str)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self._files: list[Path] = []
        self._filling = False
        self._last_output = None
        self._last_result_text = ""
        #: V2.6 FR-42（A42.2）：最近一次 ε 结果（供「光物理表征 → 从 ε 结果填入」）
        self.last_result: dict = {}
        #: 最近一次计算用的（目录, 浓度文本）——与当前表不同 = 结果过期
        self._result_key: Optional[tuple] = None
        self._run_key: Optional[tuple] = None
        self._build()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条 / 中栏结论卡 + 浓度表 + 结果区 · 右栏检查器。"""
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 计算 ε")
        theme.mark(self.btn_run, "primary")
        self.btn_run.setEnabled(False)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_run.clicked.connect(self._run)

        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        center = self._build_center_column()
        inspector = self._build_inspector()
        self._body = three_columns(None, center, inspector)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.flow_bar)
        lay.addWidget(self._body, 1)
        lay.addWidget(self.progress_row())
        self._update_verdict()
        self._go_step(0)
        self._update_flow()
        _park_unshown(self.inspector)

    def top_bar_widget(self) -> QWidget:
        """分析页顶部细条插槽的约定：交出流程条（同一对象，切走时会还回来）。"""
        return self.flow_bar

    def _conc_unit_label(self) -> str:
        """浓度单位（读 ``cfg["extinction"]["conc_unit"]``，µM 写成 µM）。"""
        unit = str((self.cfg.get("extinction") or {}).get("conc_unit") or "uM").strip() or "uM"
        return "µM" if unit.lower() in _MICRO_UNITS else unit

    def _build_center_column(self) -> QWidget:
        """中栏：结论卡 / 浓度表 / 结果（文本 + 动作条）。"""
        self.verdict = VerdictCard()

        self.tbl_conc = _ParamTable(editable_col=self.CONC_COL)
        self.tbl = self.tbl_conc              # 通用别名（脚本/测试可用）
        self.tbl_conc.setObjectName("concTable")
        self.tbl_conc.setColumnCount(3)
        self.tbl_conc.setHorizontalHeaderLabels(
            ["序号", "文件名", f"浓度 ({self._conc_unit_label()})"])
        self.tbl_conc.verticalHeader().setVisible(False)
        hdr = self.tbl_conc.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tbl_conc.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.tbl_conc.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed)
        self.tbl_conc.setEnabled(False)        # FR-39.1：未选目录时表格禁用
        self.tbl_conc.itemChanged.connect(self._on_conc_item_changed)
        box_tbl = QGroupBox("浓度表")
        lt = QVBoxLayout(box_tbl)
        lt.addWidget(self.tbl_conc)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("计算后这里列出 ε、R² 与全部告警")
        # V2.6 FR-40.5：结果区动作条（打开输出文件 / 复制结果 / 下一步 / 重试）
        self.actions = ResultActions(self)
        self.actions.message.connect(self.status)
        self.actions.next_step_requested.connect(self.next_step_requested.emit)
        self.actions.retry_requested.connect(self._run)
        box_res = QGroupBox("结果")
        lr = QVBoxLayout(box_res)
        lr.addWidget(self.view, 1)
        lr.addWidget(self.actions)

        split = QSplitter(Qt.Vertical)
        split.addWidget(box_tbl)
        split.addWidget(box_res)
        split.setChildrenCollapsible(False)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([480, 200])

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(split, 1)
        center.setMinimumWidth(360)
        return center

    def _build_inspector(self) -> QWidget:
        """右栏检查器：只展开当前步能改/能看的东西，其余步折成一行摘要。"""
        # ① 导入
        p_import = QWidget()
        li = QVBoxLayout(p_import)
        li.setContentsMargins(0, 0, 0, 0)
        li.addWidget(self.btn_dir, 0, Qt.AlignLeft)
        self.lbl_dir = QLabel("未选择目录")
        theme.mark(self.lbl_dir, "muted")
        self.lbl_dir.setWordWrap(True)
        li.addWidget(self.lbl_dir)
        li.addStretch(1)
        # ② 填浓度
        p_conc = QWidget()
        lc = QVBoxLayout(p_conc)
        lc.setContentsMargins(0, 0, 0, 0)
        self.lbl_fill = QLabel("")
        self.lbl_fill.setWordWrap(True)
        lc.addWidget(self.lbl_fill)
        hint = QLabel(f"单位 {self._conc_unit_label()}（配置 extinction.conc_unit）。"
                      "在中间的浓度表里填：双击 / F2 编辑，Tab / 回车换行，"
                      "可直接从 Excel 粘贴一列。空、非数值、负数会标红。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lc.addWidget(hint)
        lc.addStretch(1)
        # ③ 计算 ε：口径来自配置（只读展示）
        p_calc = QWidget()
        lk = QVBoxLayout(p_calc)
        lk.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.lbl_wl_mode = QLabel("")
        self.lbl_wl_mode.setWordWrap(True)
        self.lbl_fit_mode = QLabel("")
        self.lbl_path = QLabel("")
        for text, field, tip in (
                ("取 A 的波长", self.lbl_wl_mode,
                 "在哪个波长读各谱的吸光度 A：配置 extinction.wavelength 留空 = 用均值谱的"
                 "共同 λmax；填了 = 固定在该波长"),
                ("拟合", self.lbl_fit_mode,
                 "A–c 直线拟合方式：过原点（Beer–Lambert 理论要求）或带截距；"
                 "可选按 1/A 加权（配置 extinction.force_origin / weighted）"),
                ("光程", self.lbl_path, "比色皿光程 l（配置 extinction.path_length_cm），ε = 斜率 / l")):
            row_lbl = QLabel(text)
            row_lbl.setToolTip(tip)
            field.setToolTip(tip)
            form.addRow(row_lbl, field)
        lk.addLayout(form)
        self.lbl_sel_note = QLabel(
            "波长由数据选出（均值谱 λmax）时，ε 会略偏高（约 +0.8%，谱越多越轻）。"
            "做绝对定量请在配置里固定 extinction.wavelength。")
        self.lbl_sel_note.setWordWrap(True)
        theme.mark(self.lbl_sel_note, "warnHint")
        lk.addWidget(self.lbl_sel_note)
        note = QLabel("这些口径来自配置文件的 extinction 一节，与命令行相同。")
        note.setWordWrap(True)
        theme.mark(note, "hint")
        lk.addWidget(note)
        lk.addStretch(1)
        # ④ 导出
        p_out = QWidget()
        lo = QVBoxLayout(p_out)
        lo.setContentsMargins(0, 0, 0, 0)
        self.lbl_out = QLabel("还没有计算结果")
        theme.mark(self.lbl_out, "muted")
        self.lbl_out.setWordWrap(True)
        lo.addWidget(self.lbl_out)
        self.btn_open_dir = QPushButton("打开所在文件夹")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._open_output_dir)
        lo.addWidget(self.btn_open_dir, 0, Qt.AlignLeft)
        note2 = QLabel("「打开输出文件」「复制结果」在中栏结果区；ε 也会自动带进"
                       "「光物理表征」的亮度计算。")
        note2.setWordWrap(True)
        theme.mark(note2, "hint")
        lo.addWidget(note2)
        lo.addStretch(1)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((p_import, p_conc, p_calc, p_out)):
            ins.set_page(i, w)
        ins.add_action(self.btn_run)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        self._refresh_calc_page()
        return ins

    # ══════════════ 流程条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：流程条高亮、检查器换页、摘要与前后按钮跟着变。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        if idx and not self.flow_bar.step_enabled(idx):
            idx = 0
        self.flow_bar.set_current(idx)
        nxt = idx + 1 < len(STEPS) and self.flow_bar.step_enabled(idx + 1)
        self.inspector.set_current(idx, next_enabled=nxt)
        if idx == 1 and self.tbl_conc.isEnabled():
            self.tbl_conc.setFocus()

    def _fill_counts(self) -> tuple[int, int]:
        """（已填且合法的行数, 填了但有问题的行数）。"""
        good = bad = 0
        for r in range(self.tbl_conc.rowCount()):
            it = self.tbl_conc.item(r, self.CONC_COL)
            text = it.text().strip() if it is not None else ""
            if not text:
                continue
            if self._cell_error(text):
                bad += 1
            else:
                good += 1
        return good, bad

    def _step_summary(self, idx: int) -> str:
        """某一步当前设置的一行摘要（只读已有状态）。"""
        if idx == 0:
            if self.data_dir is None:
                return "还没选光谱目录"
            return f"{self.data_dir.name} · {len(self._files)} 个谱"
        if idx == 1:
            if not self._files:
                return "选目录后逐行填"
            good, bad = self._fill_counts()
            return f"已填 {good}/{len(self._files)}" + (f" · {bad} 行有问题" if bad else "")
        if idx == 2:
            r = self.last_result
            if r.get("ok"):
                return f"{_eps_text(float(r.get('eps')))} · R² {float(r.get('r2')):.4f}"
            return "计算失败" if r else "还没算"
        return Path(self._last_output).name if self._last_output else "计算后自动写出"

    def _current_key(self) -> tuple:
        texts = tuple((self.tbl_conc.item(r, self.CONC_COL).text().strip()
                       if self.tbl_conc.item(r, self.CONC_COL) is not None else "")
                      for r in range(self.tbl_conc.rowCount()))
        return (str(self.data_dir), texts)

    def _update_flow(self) -> None:
        """按目录 / 浓度表 / 结果给每一步打 完成 / 需要重算 / 未做 标记。"""
        loaded = bool(self._files)
        good, bad = self._fill_counts()
        filled = loaded and good == len(self._files) and not bad
        ok = bool(self.last_result.get("ok"))
        stale = ok and self._result_key is not None and self._result_key != self._current_key()
        done = [loaded, filled, ok, ok and self._last_output is not None]
        for i in range(len(STEPS)):
            if i:
                self.flow_bar.set_step_enabled(i, loaded, "先选择光谱目录")
            state = STEP_STALE if (stale and i >= 2 and done[i]) else (
                STEP_DONE if done[i] else STEP_TODO)
            self.flow_bar.set_state(i, state)
        cur = self.flow_bar.current()
        self.inspector.set_next_enabled(
            cur + 1 < len(STEPS) and self.flow_bar.step_enabled(cur + 1))
        if not loaded:
            self.lbl_fill.setText("还没有浓度表：先选择光谱目录")
        else:
            self.lbl_fill.setText(f"已填 {good} / {len(self._files)} 行"
                                  + (f"，{bad} 行有问题（悬停红格看原因）" if bad else ""))
        theme.mark(self.lbl_fill, "warn" if bad else "strong")
        self.inspector.refresh_summaries()

    def _refresh_calc_page(self) -> None:
        """「计算 ε」一页的口径展示（读 cfg 的 extinction 一节）。"""
        et = self.cfg.get("extinction") or {}
        wl = et.get("wavelength")
        fixed = wl is not None and str(wl).strip() != ""
        self.lbl_wl_mode.setText(f"固定 {wl} nm" if fixed else "共同 λmax（由数据选出）")
        self.lbl_sel_note.setVisible(not fixed)
        origin = et.get("force_origin", True)
        weighted = et.get("weighted", False)
        self.lbl_fit_mode.setText(("过原点" if origin is not False else "带截距")
                                  + ("，按 1/A 加权" if weighted else ""))
        self.lbl_path.setText(f"{et.get('path_length_cm') or 1.0} cm")

    # ══════════════ 结论卡 ══════════════
    def _update_verdict(self) -> None:
        """把本次 ε 结果压成一个大数字 + 一句拟合质量（只复述已有字段）。"""
        r = self.last_result
        if not r:
            self.verdict.set_empty(
                "还没有结果",
                "填好浓度表后点「计算 ε」" if self._files else "先选择光谱目录，再逐行填浓度")
            return
        if not r.get("ok"):
            self.verdict.set_failed("这次没有算出 ε", str(r.get("reason") or "详见运行日志"))
            return
        eps = float(r.get("eps"))
        r2 = float(r.get("r2"))
        n = int(r.get("n_points") or 0)
        r2_warn = float(r.get("r2_warn") or 0.995)
        unit = str(r.get("eps_unit") or "")
        unit = "L·mol⁻¹·cm⁻¹" if unit == "M⁻¹cm⁻¹" else unit
        good_fit = math.isfinite(r2) and r2 >= r2_warn
        headline = (f"Beer–Lambert 线性良好：R² = {r2:.4f}，{n} 个浓度" if good_fit
                    else f"线性偏差：R² = {r2:.4f}（低于 {r2_warn:g}），{n} 个浓度")
        lines = []
        err = float(r.get("eps_stderr", float("nan")))
        if math.isfinite(err):
            lines.append(f"标准误 ± {_eps_text(err)} {unit}")
        if int(r.get("n_dropped") or 0):
            lines.append(f"{r.get('n_dropped')} 支谱没参与拟合（原因见下方结果）")
        lambda_max = r.get("wavelength_mode") == "lambda_max"
        if lambda_max:
            lines.append("波长由数据选出，ε 可能略偏高；绝对定量请固定波长")
        groups = [
            {"name": "拟合", "sub": f"R² {r2:.4f}", "pill": "良好" if good_fit else "偏差",
             "kind": "ok" if good_fit else "warn", "color": "accent" if good_fit else "warn"},
            {"name": "选点", "sub": "λmax" if lambda_max else "固定波长",
             "pill": "可能偏高" if lambda_max else "无选点偏差",
             "kind": "warn" if lambda_max else "ok",
             "color": "warn" if lambda_max else "accent"},
        ]
        self.verdict.set_verdict(label=f"ε · λ {float(r.get('wavelength')):g} nm",
                                 value=_eps_text(eps), error=unit,
                                 headline=headline, lines=lines[:3], groups=groups)

    # ── 目录 → 行（FR-39.1：行序 = list_scan_files = collect_scans 解析序） ──
    def set_data_dir(self, data_dir: Path | str, *, force: bool = False) -> int:
        """切换光谱目录：按 ``list_scan_files`` 重建行（**已填值不保留**），返回行数。

        同一目录重复调用不重建（保留用户正在填的值，批次联动时不打扰）；
        ``force=True`` 强制重建（用户重新载入同一目录时用）。
        """
        d = Path(data_dir)
        if not force and d == self.data_dir and self._files:
            return len(self._files)
        had_old = bool(self._files)
        self.data_dir = d
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.cfg)
        except Exception as err:                       # noqa: BLE001 —— 目录/配置问题不崩
            files = []
            self.logged.emit("WARNING", f"枚举光谱目录失败：{err}")
        self._files = list(files)
        self._filling = True
        try:
            self.tbl_conc.setRowCount(len(self._files))
            for r, p in enumerate(self._files):
                it_no = QTableWidgetItem(str(r + 1))
                it_no.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it_no.setTextAlignment(Qt.AlignCenter)
                it_name = QTableWidgetItem(p.name)
                it_name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it_name.setToolTip(str(p))
                it_val = QTableWidgetItem("")          # Q-E12：默认留空，不预填
                it_val.setToolTip(f"填该文件的浓度（{self._conc_unit_label()}，与谱一一对应）")
                self.tbl_conc.setItem(r, 0, it_no)
                self.tbl_conc.setItem(r, 1, it_name)
                self.tbl_conc.setItem(r, self.CONC_COL, it_val)
        finally:
            self._filling = False
        ready = bool(self._files)
        self.tbl_conc.setEnabled(ready)
        self.btn_run.setEnabled(ready and not self._busy)
        self.lbl_dir.setText(f"{d}\n{len(self._files)} 个光谱文件" if ready
                             else f"{d}\n没有可解析的光谱文件")
        if ready:
            self.tbl_conc.clearSelection()
            self.tbl_conc.setCurrentCell(0, self.CONC_COL)
            self.tbl_conc.setFocus()
            msg = (f"已按新目录重置浓度表（{len(self._files)} 行）：请从第 1 行开始填浓度"
                   if had_old else
                   f"浓度表就绪（{len(self._files)} 行）：请从第 1 行开始填浓度")
            self.status.emit(msg)
            state = "已按新目录重置" if had_old else "已载入"
            self.logged.emit("INFO", f"浓度表{state}：{d} —— {len(self._files)} 行")
        else:
            self.status.emit(f"{d} 下没有可解析的光谱文件")
            self.logged.emit("WARNING", f"{d} 下没有找到光谱文件")
        # 换目录后旧结果的键（目录 + 浓度）对不上 → 流程条自动标"需要重算"
        self._update_verdict()
        self._update_flow()
        self._go_step(1 if ready else 0)
        return len(self._files)

    def _choose_dir(self) -> None:
        d = self.pick_dir("选择光谱目录")
        if d:
            self.set_data_dir(d, force=True)

    # ── 校验（FR-39.2：空/非数值/负数 → 单元格高亮 + 中文状态提示，不阻塞编辑） ──
    @staticmethod
    def _cell_error(text: str) -> str:
        """浓度单元格校验；返回中文原因（空串=合法）。"""
        raw = str(text or "").strip()
        if not raw:
            return "为空（请填浓度）"
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return "不是数值（示例：2.5 或 2.5e-1）"
        if val != val or val in (float("inf"), float("-inf")):
            return "不是有限数值"
        if val < 0:
            return "不能为负数"
        return ""

    def _on_conc_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item is None or item.column() != self.CONC_COL:
            return
        reason = self._cell_error(item.text())
        self.tbl_conc.blockSignals(True)
        try:
            _mark_param_cell(item, reason)
        finally:
            self.tbl_conc.blockSignals(False)
        row = item.row() + 1
        if reason:
            self.status.emit(f"第 {row} 行浓度有问题：{reason}")
        else:
            self.status.emit(f"第 {row} 行浓度已记录")
        self._update_flow()

    def conc_errors(self) -> list[tuple[int, str]]:
        """返回 ``[(行号从 0 起, 中文原因)]``；空表返回 ``[(-1, 原因)]``。"""
        if self.tbl_conc.rowCount() <= 0:
            return [(-1, "表格为空：请先选择光谱目录")]
        out: list[tuple[int, str]] = []
        for r in range(self.tbl_conc.rowCount()):
            it = self.tbl_conc.item(r, self.CONC_COL)
            reason = self._cell_error(it.text() if it is not None else "")
            if reason:
                out.append((r, reason))
        return out

    def conc_values(self) -> list[float]:
        """校验通过的浓度列表（顺序=表格行序）；有非法项时 raise ValueError(中文)。"""
        errors = self.conc_errors()
        if errors:
            detail = "；".join(f"第{r + 1}行：{e}" if r >= 0 else e
                              for r, e in errors)
            raise ValueError(f"浓度表校验未通过：{detail}")
        return [float(self.tbl_conc.item(r, self.CONC_COL).text())
                for r in range(self.tbl_conc.rowCount())]

    def _validate_conc_table(self) -> tuple[bool, list, list]:
        """整表校验 + 打/清高亮标记；返回 (是否通过, 错误列表, 数值列表)。"""
        errors: list[tuple[int, str]] = []
        values: list[float] = []
        if self.tbl_conc.rowCount() <= 0:
            errors.append((-1, "表格为空：请先选择光谱目录"))
            return False, errors, values
        for r in range(self.tbl_conc.rowCount()):
            it = self.tbl_conc.item(r, self.CONC_COL)
            reason = self._cell_error(it.text() if it is not None else "")
            if it is not None:
                _mark_param_cell(it, reason)
            if reason:
                errors.append((r, reason))
                values.append(float("nan"))
            else:
                values.append(float(it.text()))
        return (not errors), errors, values

    def _run(self) -> None:
        from core.analysis.extinction import compute_extinction, write_extinction_table
        from core.inputs.io import list_scan_files, load_spectra_files
        if self.data_dir is None:
            self.logged.emit("WARNING", "请先选择光谱目录")
            return
        ok, errors, values = self._validate_conc_table()
        if not ok:
            detail = "；".join(f"第{r + 1}行：{e}" if r >= 0 else e
                              for r, e in errors)
            self.logged.emit("ERROR", f"浓度表校验未通过：{detail}（未启动计算）")
            self.status.emit(f"浓度表校验未通过：{detail}")
            self._go_step(1)
            return
        conc = [float(v) for v in values]
        d = self.data_dir
        # 浓度写进 cfg 的 ``extinction.concentrations``——与 CLI/配置文件**同一条路**，
        # 不另造一套参数（否则 CLI 与 GUI 会慢慢分叉）。
        cfg = dict(self.cfg)
        cfg["extinction"] = dict(cfg.get("extinction") or {}, concentrations=conc)

        def task(progress_cb, log_cb):
            progress_cb(15, "读光谱")
            # 与 CLI --extinction 同一条数据路径：同一枚举 helper → load_spectra_files
            # → compute_extinction（extinction 的数组适配器读 .wl/.y/.ok，见 core 文档）。
            # 顺带修正潜伏缺陷：旧版这里用 collect_scans（Scan 无 .y/.ok）→ 每次
            # 都"没有可用光谱"失败；改后 GUI 与 CLI 数值结果一致（A39.2）。
            files = list_scan_files(d, cfg)
            if len(files) != len(conc):
                raise ValueError(f"光谱 {len(files)} 个，浓度 {len(conc)} 个——"
                                 "两者必须一一对应")
            spectra = load_spectra_files(files, cfg)
            progress_cb(50, "计算 ε")
            res = compute_extinction(spectra, cfg)
            out = app_root() / "runs" / "extinction.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            p = write_extinction_table(res, out)
            return res, p

        self.actions.reset()                       # FR-40.5：新一轮复位动作条
        self._run_key = self._current_key()
        self.run_task(task, "计算 ε", self._finish,
                      busy_widgets=[self.btn_dir, self.btn_run, self.tbl_conc])

    @staticmethod
    def _format_result_lines(result: dict, p) -> list:
        """ε 结果文本（既有 _done 的格式原样抽出，供动作条/复制共用）。"""
        ok = bool(result.get("ok"))
        lines = [f"ε 计算{'成功' if ok else '失败'}：{result.get('reason') or ''}",
                 f"表格：{p}", ""]
        if ok:
            lines += [f"取 A 的波长：{result.get('wavelength'):g} nm"
                      f"（{result.get('wavelength_mode')}）",
                      f"ε = {result.get('eps'):.4g} {result.get('eps_unit')}",
                      f"R² = {result.get('r2'):.6f}　过原点={result.get('force_origin')}"]
        issues = result.get("issues") or []
        if issues:
            lines += ["", "提示："] + [f"  · {i}" for i in issues]
        return lines

    @staticmethod
    def _failure_hint(result: dict) -> str:
        return ("如何修复：" + str(result.get("reason") or "计算未通过")
                + "；检查浓度表是否与光谱一一对应、是否至少有 2 个不同浓度、"
                  "光谱是否覆盖 λmax 附近波长；确认后点「重试」重新计算。")

    def _finish(self, res) -> None:
        """run_task 完成回调：先刷新结果区动作条，再走既有 _done 钩子。"""
        try:
            result, p = res
            ok = bool(result.get("ok"))
            self.last_result = dict(result or {})  # FR-42：ε 结果复用（亮度）
            text = "\n".join(self._format_result_lines(result, p))
            self._last_result_text = text
            self._last_output = p
            self._result_key = self._run_key
            self.view.setPlainText(text)
            self.actions.show_result(ok, output=p, text=text,
                                     failure_hint=self._failure_hint(result))
            self.lbl_out.setText(f"ε 表已写到：\n{p}")
            self.btn_open_dir.setEnabled(p is not None)
        except Exception as err:                       # noqa: BLE001
            log.warning("ε 结果区动作条刷新失败：%s", err)
        self._done(res)                                # 既有钩子（测试/日志复用）
        self._update_verdict()
        self._update_flow()
        if self.last_result.get("ok"):
            self._go_step(len(STEPS) - 1)

    def _done(self, res) -> None:
        result, p = res
        ok = bool(result.get("ok"))
        self.last_result = dict(result or {})      # FR-42：供光物理表征卡复用 ε
        text = "\n".join(self._format_result_lines(result, p))
        self._last_result_text = text
        self.view.setPlainText(text)
        self.logged.emit("INFO" if ok else "WARNING",
                         f"ε 计算{'完成' if ok else '未完成'} → {p}")

    def _open_output_dir(self) -> None:
        if self._last_output is None:
            self.status.emit("还没有输出文件")
            return
        folder = Path(self._last_output).parent
        if not open_folder(folder):
            self.status.emit(f"无法自动打开，请手动前往：{folder}")
