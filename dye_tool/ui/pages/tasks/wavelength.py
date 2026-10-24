"""② A@λ 任务卡（V2.6 FR-39.3）"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.paths import app_root
from ui.pages.base import PageBase
from ui.widgets.result_actions import ResultActions
from ui.pages.tasks.common import _ParamTable, _mark_param_cell
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

class WavelengthPanel(PageBase):
    """P7 GUI 化：任意波长吸光度表 A@λ（V2.6 FR-39.3：波长表格化）。

    与 CLI ``--a-at-wavelength`` 共用 core 的 ``wavelength_table`` 模块。
    表格行 = 目标波长（nm），默认 400/450/500（保留 V1.x 默认）；
    提交时仍写 ``cfg["wavelength_table"]["wavelengths"]``（字段不变）。
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
        self.data_dir: Optional[Path] = None
        self._filling = False
        self._last_output = None
        self._last_result_text = ""
        self._build()

    def _build(self) -> None:
        from PySide6.QtWidgets import QPlainTextEdit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 导出 A@λ")
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.btn_run)
        bar.addStretch(1)
        lay.addLayout(bar)
        row = QHBoxLayout()
        row.addWidget(QLabel("目标波长 (nm)："))
        self.btn_add = QPushButton("+ 添加")
        self.btn_add.setToolTip("在当前行之后插入一个空波长行")
        self.btn_rm = QPushButton("− 删除")
        self.btn_rm.setToolTip("删除选中的波长行（至少保留 1 行）")
        row.addWidget(self.btn_add)
        row.addWidget(self.btn_rm)
        row.addStretch(1)
        lay.addLayout(row)
        hint = QLabel("逐行填目标波长（nm）；Tab/回车换行，可直接从 Excel 粘贴一列。"
                      "空/非数值/≤0/重复会在单元格标红并提示。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)
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
        lay.addWidget(self.tbl_wl, 2)
        lay.addWidget(self.progress_row())
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        lay.addWidget(self.view, 1)
        # V2.6 FR-40.5：结果区动作条（打开输出文件 / 复制结果 / 下一步 / 重试）
        self.actions = ResultActions(self)
        self.actions.message.connect(self.status)
        self.actions.next_step_requested.connect(self.next_step_requested.emit)
        self.actions.retry_requested.connect(self._run)
        lay.addWidget(self.actions)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_run.clicked.connect(self._run)
        self.btn_add.clicked.connect(self._add_row)
        self.btn_rm.clicked.connect(self._del_row)
        self.set_wavelengths(self.DEFAULT_WAVELENGTHS)

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
        self.status.emit(f"已删除 {removed} 行波长")

    def _run(self) -> None:
        from core.analysis.wavelength_table import absorbance_at, write_wavelength_table
        from core.inputs.io import collect_scans
        if self.data_dir is None:
            self.logged.emit("WARNING", "请先选择光谱目录")
            return
        errors = self.wl_errors()
        if errors:
            detail = "；".join(f"第{r + 1}行：{e}" if r >= 0 else e
                              for r, e in errors)
            self.logged.emit("ERROR", f"波长表校验未通过：{detail}（未启动导出）")
            self.status.emit(f"波长表校验未通过：{detail}")
            return
        wls = [float(self.tbl_wl.item(r, 0).text())
               for r in range(self.tbl_wl.rowCount())]
        d = self.data_dir
        # 波长写进 cfg 的 ``wavelength_table.wavelengths``——与 CLI/配置文件同一条路
        cfg = dict(self.cfg)
        cfg["wavelength_table"] = dict(cfg.get("wavelength_table") or {},
                                       wavelengths=wls)

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
            text = "\n".join(self._format_result_lines(tab, p))
            self._last_result_text = text
            self._last_output = p
            self.view.setPlainText(text)
            self.actions.show_result(ok, output=p, text=text,
                                     failure_hint=self._failure_hint(tab))
        except Exception as err:                       # noqa: BLE001
            log.warning("A@λ 结果区动作条刷新失败：%s", err)
        self._done(res)                                # 既有钩子（测试/日志复用）

    def _done(self, res) -> None:
        tab, p = res
        ok = bool(tab.get("ok"))
        text = "\n".join(self._format_result_lines(tab, p))
        self._last_result_text = text
        self.view.setPlainText(text)
        self.logged.emit("INFO" if ok else "WARNING", f"A@λ 表 → {p}")
