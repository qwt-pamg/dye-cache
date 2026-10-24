"""② ε 任务卡（V2.6 FR-39）"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QFileDialog
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

class ExtinctionPanel(PageBase):
    """P6 GUI 化：摩尔消光系数 ε（V2.6 FR-39.1/39.2：浓度表格化）。

    与 CLI ``--extinction`` **共用同一个 core 函数**（``compute_extinction`` +
    ``write_extinction_table``），不复制实现。表格行 = 光谱文件，行序来自
    :func:`core.inputs.io.list_scan_files`（与 ``collect_scans`` **同一枚举/排序**，
    Q-E11 单一顺序源）；浓度逐行填写，提交仍写
    ``cfg["extinction"]["concentrations"]``（与 CLI/配置文件同一条路）。
    """

    TITLE = "摩尔消光系数 ε（P6）"
    SUBTITLE = ("载入光谱序列 → 表格按文件解析顺序逐行填浓度（mol/L）→ 计算 ε。"
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
        self._build()

    def _build(self) -> None:
        from PySide6.QtWidgets import QPlainTextEdit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 计算 ε")
        self.btn_run.setEnabled(False)
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.btn_run)
        bar.addStretch(1)
        lay.addLayout(bar)
        hint = QLabel("浓度按文件解析顺序逐行填写（行序 = 程序读取文件的顺序，"
                      "不会错位）；默认留空，不替用户猜值。双击/F2 编辑，"
                      "Tab/回车换行，可直接从 Excel 粘贴一列。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)
        self.tbl_conc = _ParamTable(editable_col=self.CONC_COL)
        self.tbl = self.tbl_conc              # 通用别名（脚本/测试可用）
        self.tbl_conc.setObjectName("concTable")
        self.tbl_conc.setColumnCount(3)
        self.tbl_conc.setHorizontalHeaderLabels(["序号", "文件名", "浓度 (mol/L)"])
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
        lay.addWidget(self.tbl_conc, 2)
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
                it_val.setToolTip("填该文件的浓度（mol/L，与谱一一对应）")
                self.tbl_conc.setItem(r, 0, it_no)
                self.tbl_conc.setItem(r, 1, it_name)
                self.tbl_conc.setItem(r, self.CONC_COL, it_val)
        finally:
            self._filling = False
        ready = bool(self._files)
        self.tbl_conc.setEnabled(ready)
        self.btn_run.setEnabled(ready and not self._busy)
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
        return len(self._files)

    def _choose_dir(self) -> None:
        start = str((self.cfg.get("gui") or {}).get("last_dir") or app_root())
        d = QFileDialog.getExistingDirectory(self, "选择光谱目录", start)
        if d:
            self.remember_dir(Path(d))
            self.set_data_dir(Path(d), force=True)

    # ── 校验（FR-39.2：空/非数值/负数 → 单元格高亮 + 中文状态提示，不阻塞编辑） ──
    @staticmethod
    def _cell_error(text: str) -> str:
        """浓度单元格校验；返回中文原因（空串=合法）。"""
        raw = str(text or "").strip()
        if not raw:
            return "为空（请填浓度 mol/L）"
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return "不是数值（示例：5e-6）"
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
            self.view.setPlainText(text)
            self.actions.set_output(p if ok else None)
            self.actions.set_result_text(text)
            if ok:
                self.actions.set_state(True)
            else:
                self.actions.set_state(False,
                                       failure_hint=self._failure_hint(result))
        except Exception as err:                       # noqa: BLE001
            log.warning("ε 结果区动作条刷新失败：%s", err)
        self._done(res)                                # 既有钩子（测试/日志复用）

    def _done(self, res) -> None:
        result, p = res
        ok = bool(result.get("ok"))
        self.last_result = dict(result or {})      # FR-42：供光物理表征卡复用 ε
        text = "\n".join(self._format_result_lines(result, p))
        self._last_result_text = text
        self.view.setPlainText(text)
        self.logged.emit("INFO" if ok else "WARNING",
                         f"ε 计算{'完成' if ok else '未完成'} → {p}")
