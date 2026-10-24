"""② A@λ 任务卡（V2.6 FR-39.3；混搭工作台：流程条 / 结论卡 + A@λ 表 / 分步检查器）"""

from __future__ import annotations

import logging
import math

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QDoubleSpinBox
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QTableWidget
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
from ui.pages.tasks.common import _ParamTable, _mark_param_cell
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

#: 流程条三步；检查器第 i 页、:meth:`WavelengthPanel._step_summary` 都按这个顺序
STEPS = ("导入", "填波长", "导出")
STEP_HINTS = (
    "选光谱目录；数据页载入批次后这里会自动跟上",
    "逐行填目标波长（nm），再选取值方式；空、非数值、≤0、重复会标红",
    "按「导出 A@λ」生成表格（CSV）；中间是每个谱在各波长处的吸光度",
)

#: 取值方式：（显示文字, interpolate）
MODES = (("就近取点（容差内）", False), ("线性插值", True))

#: 结论卡右侧最多列几个波长
_MAX_WL_ROWS = 4


class WavelengthPanel(PageBase):
    """P7 GUI 化：任意波长吸光度表 A@λ（V2.6 FR-39.3：波长表格化）。

    与 CLI ``--a-at-wavelength`` 共用 core 的 ``wavelength_table`` 模块。
    波长表行 = 目标波长（nm），默认 400/450/500（保留 V1.x 默认）；
    提交时仍写 ``cfg["wavelength_table"]["wavelengths"]``（字段不变），取值方式
    （``interpolate`` / ``tol_nm``）只写进本次运行用的 cfg 副本。

    布局同 Φ_Δ 页：流程条（交给分析页顶部细条）/ 中栏结论卡 + A@λ 结果表 + 结果区 /
    右栏分步检查器（波长表在「填波长」一步里）。
    """

    TITLE = "任意波长表 A@λ（P7）"
    SUBTITLE = ("载入光谱 → 表格填目标波长（nm）→ 导出 A@λ 表。"
                "固定波长取点 / 缺席则按邻近插值，容差判据见日志。")

    #: 默认 3 行波长（V1.x 默认；cfg 里的 wavelengths 是 CLI 默认，不在此预填）
    DEFAULT_WAVELENGTHS = (400.0, 450.0, 500.0)

    #: V2.6 FR-40.5：结果区「下一步」→ ③ 图件 / 定稿出图
    next_step_requested = Signal(str)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self._data_dir: Optional[Path] = None
        self._n_files = 0
        self._filling = False
        self._last_output = None
        self._last_result_text = ""
        #: 最近一次导出的结果（``absorbance_at`` 的 dict）
        self.last_result: dict = {}
        #: 最近一次导出用的（目录, 波长文本, 取值方式）——与当前不同 = 结果过期
        self._result_key: Optional[tuple] = None
        self._run_key: Optional[tuple] = None
        #: 最近一次导出时的取值方式文字（结论卡用；之后改设置不跟着变）
        self._result_mode = ""
        self._run_mode = ""
        self._build()

    # ── 目录：分析页接批次时直接赋值 ``panel.data_dir = …``，这里顺带刷新界面 ──
    @property
    def data_dir(self) -> Optional[Path]:
        return self._data_dir

    @data_dir.setter
    def data_dir(self, value) -> None:
        d = Path(value) if value else None
        changed = d != self._data_dir
        self._data_dir = d
        if not hasattr(self, "flow_bar"):              # 构建前（__init__ 早期）
            return
        self._n_files = 0
        if d is not None:
            from core.inputs.io import list_scan_files
            try:
                self._n_files = len(list_scan_files(d, self.cfg))
            except Exception as err:                   # noqa: BLE001 —— 只为摘要计数
                log.debug("A@λ 枚举光谱目录失败：%s", err)
        self.lbl_dir.setText("未选择目录" if d is None else
                             f"{d}\n{self._n_files} 个光谱文件")
        self._update_verdict()
        self._update_flow()
        if changed and d is not None and self.flow_bar.current() == 0:
            self._go_step(1)

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条 / 中栏结论卡 + A@λ 表 + 结果区 · 右栏检查器。"""
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 导出 A@λ")
        theme.mark(self.btn_run, "primary")
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
        self.set_wavelengths(self.DEFAULT_WAVELENGTHS)
        self._update_verdict()
        self._go_step(0)
        self._update_flow()

    def top_bar_widget(self) -> QWidget:
        """分析页顶部细条插槽的约定：交出流程条（同一对象，切走时会还回来）。"""
        return self.flow_bar

    def _build_center_column(self) -> QWidget:
        """中栏：结论卡 / A@λ 结果表 / 结果（文本 + 动作条）。"""
        self.verdict = VerdictCard()

        self.tbl_result = QTableWidget(0, 0)
        self.tbl_result.setObjectName("wlResultTable")
        self.tbl_result.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_result.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_result.setAlternatingRowColors(True)
        self.tbl_result.verticalHeader().setVisible(False)
        box_tbl = QGroupBox("A@λ 表")
        lt = QVBoxLayout(box_tbl)
        lt.addWidget(self.tbl_result)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("导出后这里列出跳过的文件与缺值原因")
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
        split.setSizes([500, 170])

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(split, 1)
        center.setMinimumWidth(360)
        return center

    def _build_inspector(self) -> QWidget:
        """右栏检查器：导入 / 填波长（波长表 + 取值方式）/ 导出。"""
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
        # ② 填波长
        p_wl = QWidget()
        lw = QVBoxLayout(p_wl)
        lw.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("目标波长 (nm)"))
        row.addStretch(1)
        self.btn_add = QPushButton("+ 添加")
        self.btn_add.setToolTip("在当前行之后插入一个空波长行")
        self.btn_rm = QPushButton("− 删除")
        self.btn_rm.setToolTip("删除选中的波长行（至少保留 1 行）")
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_rm)
        lw.addLayout(row)
        self.tbl_wl = _ParamTable(editable_col=0, allow_expand=True)
        self.tbl = self.tbl_wl                # 通用别名（脚本/测试可用）
        self.tbl_wl.setObjectName("wlTable")
        self.tbl_wl.setColumnCount(1)
        self.tbl_wl.setHorizontalHeaderLabels(["波长 (nm)"])
        self.tbl_wl.verticalHeader().setVisible(False)
        self.tbl_wl.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_wl.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.tbl_wl.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed)
        self.tbl_wl.itemChanged.connect(self._on_wl_item_changed)
        lw.addWidget(self.tbl_wl, 1)
        hint = QLabel("Tab / 回车换行，可直接从 Excel 粘贴一列。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lw.addWidget(hint)
        wt = self.cfg.get("wavelength_table") or {}
        self.cmb_mode = QComboBox()
        for text, interp in MODES:
            self.cmb_mode.addItem(text, interp)
        self.cmb_mode.setCurrentIndex(1 if bool(wt.get("interpolate", False)) else 0)
        self.cmb_mode.setToolTip("就近取点：只取容差内最近的实测点，超差记 NaN；\n"
                                 "线性插值：在实测点之间插值，范围外不外推（记 NaN）")
        self.sp_tol = QDoubleSpinBox()
        self.sp_tol.setRange(0.0, 50.0)
        self.sp_tol.setDecimals(2)
        self.sp_tol.setSingleStep(0.1)
        self.sp_tol.setSuffix(" nm")
        try:
            self.sp_tol.setValue(float(wt.get("tol_nm", 0.5)))
        except (TypeError, ValueError):
            self.sp_tol.setValue(0.5)
        self.sp_tol.setToolTip("就近取点时，实测点与目标波长最多差多少（含边界）")
        form = QFormLayout()
        form.addRow("取值方式", self.cmb_mode)
        form.addRow("容差", self.sp_tol)
        lw.addLayout(form)
        self.cmb_mode.currentIndexChanged.connect(self._on_mode_changed)
        self.sp_tol.valueChanged.connect(lambda *_: self._update_flow())
        self.btn_add.clicked.connect(self._add_row)
        self.btn_rm.clicked.connect(self._del_row)
        # ③ 导出
        p_out = QWidget()
        lo = QVBoxLayout(p_out)
        lo.setContentsMargins(0, 0, 0, 0)
        self.lbl_out = QLabel("还没有导出")
        theme.mark(self.lbl_out, "muted")
        self.lbl_out.setWordWrap(True)
        lo.addWidget(self.lbl_out)
        self.btn_open_dir = QPushButton("打开所在文件夹")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._open_output_dir)
        lo.addWidget(self.btn_open_dir, 0, Qt.AlignLeft)
        note = QLabel("CSV 用 Excel 直接打开不乱码；「复制结果」在中栏结果区。")
        note.setWordWrap(True)
        theme.mark(note, "hint")
        lo.addWidget(note)
        lo.addStretch(1)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((p_import, p_wl, p_out)):
            ins.set_page(i, w)
        ins.add_action(self.btn_run)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        self._on_mode_changed()
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

    def interpolate(self) -> bool:
        """当前取值方式是否为线性插值。"""
        return bool(self.cmb_mode.currentData())

    def mode_text(self) -> str:
        """取值方式的短说明（结论卡 / 摘要用）。"""
        return "线性插值" if self.interpolate() else f"容差 ±{self.sp_tol.value():g} nm"

    def _on_mode_changed(self, *_args) -> None:
        self.sp_tol.setEnabled(not self.interpolate())
        if hasattr(self, "inspector"):
            self._update_flow()

    def _step_summary(self, idx: int) -> str:
        """某一步当前设置的一行摘要（只读已有状态）。"""
        if idx == 0:
            if self.data_dir is None:
                return "还没选光谱目录"
            return f"{self.data_dir.name} · {self._n_files} 个谱"
        if idx == 1:
            n = self.tbl_wl.rowCount()
            bad = len([e for e in self._compute_wl_errors() if e[0] >= 0])
            return f"{n} 个波长 · {self.mode_text()}" + (f" · {bad} 行有问题" if bad else "")
        return Path(self._last_output).name if self._last_output else "还没导出"

    def _current_key(self) -> tuple:
        texts = tuple((self.tbl_wl.item(r, 0).text().strip()
                       if self.tbl_wl.item(r, 0) is not None else "")
                      for r in range(self.tbl_wl.rowCount()))
        return (str(self.data_dir), texts, self.interpolate(),
                None if self.interpolate() else round(self.sp_tol.value(), 4))

    def _update_flow(self) -> None:
        """按目录 / 波长表 / 导出结果给每一步打 完成 / 需要重算 / 未做 标记。"""
        if not hasattr(self, "inspector"):
            return
        loaded = self.data_dir is not None
        wl_ok = not self._compute_wl_errors()
        ok = bool(self.last_result.get("ok"))
        stale = ok and self._result_key is not None and self._result_key != self._current_key()
        done = [loaded, wl_ok, ok and self._last_output is not None]
        for i in range(len(STEPS)):
            state = STEP_STALE if (stale and i == 2 and done[i]) else (
                STEP_DONE if done[i] else STEP_TODO)
            self.flow_bar.set_state(i, state)
        self.flow_bar.set_step_enabled(2, loaded, "先选择光谱目录")
        cur = self.flow_bar.current()
        self.inspector.set_next_enabled(
            cur + 1 < len(STEPS) and self.flow_bar.step_enabled(cur + 1))
        self.inspector.refresh_summaries()

    # ══════════════ 结论卡 ══════════════
    def _update_verdict(self) -> None:
        """把本次 A@λ 表压成"N 个谱 × M 个波长 · 取值方式"（只复述已有字段）。"""
        tab = self.last_result
        if not tab:
            self.verdict.set_empty(
                "还没有结果",
                "填好波长后点「导出 A@λ」" if self.data_dir is not None
                else "先选择光谱目录，再填目标波长")
            return
        if not tab.get("ok"):
            self.verdict.set_failed("这次没有生成 A@λ 表", str(tab.get("reason") or "详见运行日志"))
            return
        rows = tab.get("rows") or []
        cols = list(tab.get("columns") or [])
        wls = list(tab.get("wavelengths") or [])
        mode = self._result_mode or self.mode_text()
        lines = []
        skipped = tab.get("skipped") or []
        if skipped:
            lines.append(f"跳过 {len(skipped)} 个文件（原因见下方结果）")
        n_nan = sum(1 for r in rows for c in cols if not _finite(r.get(c)))
        if n_nan:
            lines.append(f"{n_nan} 个单元格没有可用数据（记 NaN）")
        if self._last_output:
            lines.append(f"已写到 {Path(self._last_output).name}")
        groups = []
        stats = tab.get("stats") or {}
        for w, c in list(zip(wls, cols))[:_MAX_WL_ROWS]:
            st = stats.get(c) or {}
            n = int(st.get("n") or 0)
            mean = st.get("mean")
            groups.append({"name": f"{w:g} nm",
                           "sub": f"均值 {mean:.3f}" if _finite(mean) else "无数据",
                           "pill": f"{n}/{len(rows)}",
                           "kind": "ok" if n == len(rows) else "warn",
                           "color": "accent" if n == len(rows) else "warn"})
        self.verdict.set_verdict(label="谱 × 波长", value=f"{len(rows)} × {len(cols)}",
                                 headline=f"{len(rows)} 个谱 × {len(cols)} 个波长 · {mode}",
                                 lines=lines[:3], groups=groups)

    def _fill_result_table(self, tab: dict) -> None:
        """中栏 A@λ 表：每行一个谱（文件、组 + 各波长 A），NaN 显示成 —。"""
        cols = list(tab.get("columns") or []) if tab.get("ok") else []
        wls = list(tab.get("wavelengths") or [])
        rows = (tab.get("rows") or []) if tab.get("ok") else []
        heads = ["文件", "组"] + [f"A@{w:g} nm" for w in wls[:len(cols)]]
        self.tbl_result.clear()
        self.tbl_result.setColumnCount(len(heads))
        self.tbl_result.setHorizontalHeaderLabels(heads)
        self.tbl_result.setRowCount(len(rows))
        for r, row in enumerate(rows):
            name = QTableWidgetItem(str(row.get("file", "")))
            name.setToolTip(str(row.get("file", "")))
            self.tbl_result.setItem(r, 0, name)
            self.tbl_result.setItem(r, 1, QTableWidgetItem(str(row.get("group", "") or "")))
            for k, c in enumerate(cols):
                v = row.get(c)
                it = QTableWidgetItem(f"{float(v):.4f}" if _finite(v) else "—")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if not _finite(v):
                    it.setToolTip("这个波长在该谱里没有可用数据（容差外 / 超出范围）")
                self.tbl_result.setItem(r, 2 + k, it)
        hdr = self.tbl_result.horizontalHeader()
        if heads:
            hdr.setSectionResizeMode(0, QHeaderView.Stretch)
            for c in range(1, len(heads)):
                hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)

    def _choose_dir(self) -> None:
        d = self.pick_dir("选择光谱目录")
        if d:
            self.data_dir = d

    # ── 行编辑 / 校验（FR-39.3） ──
    @staticmethod
    def _cell_error(text: str) -> str:
        """波长单元格校验；返回中文原因（空串=合法）。"""
        raw = str(text or "").strip()
        if not raw:
            return "为空（请填波长 nm）"
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return "不是数值"
        if val != val or val in (float("inf"), float("-inf")):
            return "不是有限数值"
        if val <= 0:
            return "必须大于 0"
        return ""

    def set_wavelengths(self, values) -> None:
        """整表重填（初始化 / 外部预置 / 测试用）；非法文本原样保留供校验标红。"""
        lines: list[str] = []
        for v in values or []:
            try:
                fv = float(v)
            except (TypeError, ValueError):
                lines.append(str(v))
                continue
            lines.append(str(int(fv)) if fv.is_integer() else str(fv))
        self._filling = True
        try:
            self.tbl_wl.setRowCount(len(lines))
            for r, text in enumerate(lines):
                self.tbl_wl.setItem(r, 0, QTableWidgetItem(text))
        finally:
            self._filling = False
        self._compute_wl_errors()
        if lines:
            self.tbl_wl.clearSelection()
            self.tbl_wl.setCurrentCell(0, 0)
        self._update_flow()

    def _compute_wl_errors(self) -> list[tuple[int, str]]:
        """整表校验 + 打/清高亮（重复行的**每一行**都标红）；返回错误列表。"""
        n = self.tbl_wl.rowCount()
        if n <= 0:
            return [(-1, "表格为空：请至少填一个波长")]
        reasons: dict[int, list[str]] = {r: [] for r in range(n)}
        seen: dict[float, list[int]] = {}
        for r in range(n):
            it = self.tbl_wl.item(r, 0)
            text = it.text() if it is not None else ""
            reason = self._cell_error(text)
            if reason:
                reasons[r].append(reason)
            else:
                seen.setdefault(float(text), []).append(r)
        for rows in seen.values():
            if len(rows) > 1:
                for r in rows:
                    reasons[r].append("与其它行重复")
        self.tbl_wl.blockSignals(True)
        try:
            for r in range(n):
                _mark_param_cell(self.tbl_wl.item(r, 0),
                                 "；".join(reasons[r]))
        finally:
            self.tbl_wl.blockSignals(False)
        return [(r, "；".join(reasons[r])) for r in range(n) if reasons[r]]

    def _on_wl_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item is None or item.column() != 0:
            return
        errors = self._compute_wl_errors()
        reason = self._cell_error(item.text())
        row = item.row() + 1
        if reason:
            self.status.emit(f"第 {row} 行波长有问题：{reason}")
        elif any(r == item.row() for r, _ in errors):
            self.status.emit(f"第 {row} 行波长与其它行重复")
        else:
            self.status.emit(f"第 {row} 行波长已记录")
        self._update_flow()

    def wl_errors(self) -> list[tuple[int, str]]:
        """返回 ``[(行号从 0 起, 中文原因)]``；空表返回 ``[(-1, 原因)]``。"""
        return self._compute_wl_errors()

    def wavelengths(self) -> list[float]:
        """校验通过的波长列表；有非法项时 raise ValueError(中文)。"""
        errors = self.wl_errors()
        if errors:
            detail = "；".join(f"第{r + 1}行：{e}" if r >= 0 else e
                              for r, e in errors)
            raise ValueError(f"波长表校验未通过：{detail}")
        return [float(self.tbl_wl.item(r, 0).text())
                for r in range(self.tbl_wl.rowCount())]

    def _add_row(self) -> None:
        row = max(0, self.tbl_wl.currentRow() + 1)
        self.tbl_wl.insertRow(row)
        self.tbl_wl.setItem(row, 0, QTableWidgetItem(""))
        self.tbl_wl.clearSelection()
        self.tbl_wl.setCurrentCell(row, 0)
        self.tbl_wl.setFocus()
        self._compute_wl_errors()
        self._update_flow()
        self.status.emit(f"已在第 {row + 1} 行添加空波长（请填 nm）")

    def _del_row(self) -> None:
        rows = sorted({i.row() for i in self.tbl_wl.selectedIndexes()},
                      reverse=True)
        if not rows and self.tbl_wl.currentRow() >= 0:
            rows = [self.tbl_wl.currentRow()]
        if not rows:
            self.status.emit("没有选中要删除的波长行")
            return
        removed = 0
        for r in rows:
            if self.tbl_wl.rowCount() <= 1:
                break
            self.tbl_wl.removeRow(r)
            removed += 1
        if self.tbl_wl.rowCount() <= 0:
            self._add_row()
        self._compute_wl_errors()
        self._update_flow()
        self.status.emit(f"已删除 {removed} 行波长")

    def _run(self) -> None:
        from core.analysis.wavelength_table import absorbance_at, write_wavelength_table
        from core.inputs.io import collect_scans
        if self.data_dir is None:
            self.logged.emit("WARNING", "请先选择光谱目录")
            self._go_step(0)
            return
        errors = self.wl_errors()
        if errors:
            detail = "；".join(f"第{r + 1}行：{e}" if r >= 0 else e
                              for r, e in errors)
            self.logged.emit("ERROR", f"波长表校验未通过：{detail}（未启动导出）")
            self.status.emit(f"波长表校验未通过：{detail}")
            self._go_step(1)
            return
        wls = [float(self.tbl_wl.item(r, 0).text())
               for r in range(self.tbl_wl.rowCount())]
        d = self.data_dir
        # 波长写进 cfg 的 ``wavelength_table.wavelengths``——与 CLI/配置文件同一条路；
        # 取值方式只进本次副本（不改用户配置）
        cfg = dict(self.cfg)
        cfg["wavelength_table"] = dict(cfg.get("wavelength_table") or {},
                                       wavelengths=wls,
                                       interpolate=self.interpolate(),
                                       tol_nm=float(self.sp_tol.value()))

        def task(progress_cb, log_cb):
            progress_cb(15, "读光谱")
            scans = collect_scans(d, cfg)
            progress_cb(50, "生成 A@λ 表")
            tab = absorbance_at(scans, cfg)
            out = app_root() / "runs" / "a_at_wavelength.csv"
            out.parent.mkdir(parents=True, exist_ok=True)
            p = write_wavelength_table(tab, out)
            return tab, p

        self.actions.reset()                       # FR-40.5：新一轮复位动作条
        self._run_key = self._current_key()
        self._run_mode = self.mode_text()
        self.run_task(task, "导出 A@λ", self._finish,
                      busy_widgets=[self.btn_dir, self.btn_run, self.tbl_wl])

    @staticmethod
    def _format_result_lines(tab: dict, p) -> list:
        """A@λ 结果文本（既有 _done 的格式原样抽出，供动作条/复制共用）。"""
        ok = bool(tab.get("ok"))
        lines = [f"A@λ 表{'已生成' if ok else '未生成'}：{tab.get('reason') or ''}",
                 f"文件：{p}", f"列：{', '.join(tab.get('columns') or [])}", ""]
        for row in (tab.get("rows") or [])[:40]:
            cells = [str(row.get(c, "")) for c in ("file",) + tuple(
                tab.get("columns") or [])]
            lines.append("  " + "  ".join(cells))
        skipped = tab.get("skipped") or []
        if skipped:
            lines += ["", f"跳过 {len(skipped)} 个："]
            lines += [f"  · {s.get('file')}：{s.get('reason')}" for s in skipped[:10]]
        return lines

    @staticmethod
    def _failure_hint(tab: dict) -> str:
        return ("如何修复：" + str(tab.get("reason") or "导出未完成")
                + "；检查是否已载入光谱目录、波长是否为正数/不重复、表格是否为空；"
                  "确认后点「重试」重新导出。")

    def _finish(self, res) -> None:
        """run_task 完成回调：先刷新结果区动作条，再走既有 _done 钩子。"""
        try:
            tab, p = res
            ok = bool(tab.get("ok"))
            self.last_result = dict(tab or {})
            text = "\n".join(self._format_result_lines(tab, p))
            self._last_result_text = text
            self._last_output = p
            self._result_key = self._run_key
            self._result_mode = self._run_mode
            self.actions.show_result(ok, output=p, text=text,
                                     failure_hint=self._failure_hint(tab))
            self._fill_result_table(tab)
            self.lbl_out.setText(f"A@λ 表已写到：\n{p}")
            self.btn_open_dir.setEnabled(p is not None)
        except Exception as err:                       # noqa: BLE001
            log.warning("A@λ 结果区动作条刷新失败：%s", err)
        self._done(res)                                # 既有钩子（测试/日志复用）
        self._update_verdict()
        self._update_flow()
        if self.last_result.get("ok"):
            self._go_step(len(STEPS) - 1)

    def _done(self, res) -> None:
        tab, p = res
        ok = bool(tab.get("ok"))
        text = "\n".join(self._format_result_lines(tab, p))
        self._last_result_text = text
        # 中栏已有逐格的表；文本区只留告警 / 跳过（全文仍可「复制结果」）
        notes = [f"· {m}" for m in (tab.get("issues") or [])]
        notes += [f"· 跳过 {s.get('file')}：{s.get('reason')}" for s in (tab.get("skipped") or [])]
        self.view.setPlainText("\n".join(notes) if notes else
                               (f"没有告警。表格：{p}" if ok else text))
        self.logged.emit("INFO" if ok else "WARNING", f"A@λ 表 → {p}")

    def _open_output_dir(self) -> None:
        if self._last_output is None:
            self.status.emit("还没有输出文件")
            return
        folder = Path(self._last_output).parent
        if not open_folder(folder):
            self.status.emit(f"无法自动打开，请手动前往：{folder}")


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False
