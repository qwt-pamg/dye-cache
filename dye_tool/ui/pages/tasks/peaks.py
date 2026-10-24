"""② 峰分析任务卡（V2.7 FR-37/A43）"""

from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from ui.pages.base import PageBase

log = logging.getLogger("dye_tool.ui.analysis")
from core.paths import resolve_out_root
from ui import theme

class PeaksPanel(PageBase):
    """V2.7 A 道（V2.6 D 道结转）：FR-37 峰分析（②第 8 卡）。

    纯函数在 core.analysis.peaks（Q-E5 判据默认值），本面板只做
    目录 → 后台 Job → 峰表 → 导出 CSV；不写 run 目录、不进流水线。
    默认导出名 peaks_<HHMMSS>.csv 落 out_root，同名自动加 -2 不覆盖；
    无峰 → 空表 + 提示，不产文件（A43.3/A43.5）。
    """

    TITLE = "峰分析（P8）"
    SUBTITLE = ("选光谱目录 → 后台逐谱找峰（突出度≥5%、峰高≥2%、间距≥5 点、≤20 峰、"
                "NaN 分段）→ 峰表 → 导出 CSV。")

    #: 表格列（与 CSV 表头一致，文件列 + 峰序号）
    COLUMNS = ("文件", "峰#", "波长 (nm)", "强度", "突出度", "宽 (pts)", "宽 (nm)")

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self._csv_rows: list = []
        self._last_output = None
        self._build()

    def _build(self) -> None:
        from PySide6.QtWidgets import QPlainTextEdit
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 找峰")
        self.btn_run.setEnabled(False)
        self.btn_export = QPushButton("导出 CSV…")
        self.btn_export.setEnabled(False)
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.btn_run)
        bar.addWidget(self.btn_export)
        bar.addStretch(1)
        lay.addLayout(bar)
        hint = QLabel("判据：突出度 ≥ 动态范围 5%；峰高（相对谱最小值）"
                      "≥ 动态范围 2%；相邻峰 ≥ 5 点；单谱最多 20 峰（按突出度降序截断）；"
                      "NaN 断点分段检测、不跨段合并；全谱范围。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)
        self.tbl = QTableWidget()
        self.tbl.setObjectName("peaksTable")
        self.tbl.setColumnCount(len(self.COLUMNS))
        self.tbl.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        lay.addWidget(self.tbl, 2)
        lay.addWidget(self.progress_row())
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("峰表摘要（无峰时会给出提示）")
        lay.addWidget(self.view, 1)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_run.clicked.connect(self._run)
        self.btn_export.clicked.connect(self._choose_export)

    # ── 目录 / 运行 ──
    def set_data_dir(self, data_dir) -> int:
        d = Path(data_dir)
        self.data_dir = d
        self.btn_run.setEnabled(d.is_dir() and not self._busy)
        self.status.emit(f"光谱目录：{d}")
        return 0 if not d.is_dir() else 1

    def _choose_dir(self) -> None:
        d = self.pick_dir("选择光谱目录")
        if d:
            self.set_data_dir(d)

    def _run(self) -> None:
        from core.analysis.peaks import find_peaks_spectrum, peak_rows
        from core.inputs.io import collect_scans
        if self.data_dir is None:
            self.logged.emit("WARNING", "请先选择光谱目录")
            return
        d = self.data_dir
        cfg = dict(self.cfg)

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            scans = collect_scans(d, cfg)
            total = max(1, len(scans))
            rows: list = []
            n_scan = 0
            for i, sc in enumerate(scans):
                progress_cb(10 + int(80 * (i + 1) / total), f"找峰 {i + 1}/{total}")
                if not sc.ok:
                    continue
                n_scan += 1
                try:
                    peaks = find_peaks_spectrum(sc.wl, sc.a)
                except ValueError as err:
                    log_cb(f"{sc.path.name}：参数/光谱异常——{err}")
                    continue
                if peaks:
                    rows.extend(peak_rows(sc.path.name, peaks))
            return rows, n_scan

        self.run_task(task, "峰分析", self._finish,
                      busy_widgets=[self.btn_dir, self.btn_run, self.btn_export,
                                    self.tbl])

    def _finish(self, res) -> None:
        rows, n_scan = res
        self._csv_rows = list(rows)
        self.tbl.setRowCount(len(self._csv_rows))
        for r, row in enumerate(self._csv_rows):
            for c, val in enumerate(row):
                it = QTableWidgetItem(str(val))
                if c in (0,):
                    it.setToolTip(str(val))
                it.setTextAlignment(Qt.AlignCenter if c in (1, 2, 5) else Qt.AlignLeft)
                self.tbl.setItem(r, c, it)
        self.btn_export.setEnabled(bool(self._csv_rows))
        if self._csv_rows:
            files = len({row[0] for row in self._csv_rows})
            text = f"峰表：{len(self._csv_rows)} 个峰 / {files} 个文件（{n_scan} 条可用光谱）"
            self.view.setPlainText(text)
            self.status.emit(text)
            self.logged.emit("INFO", text)
        else:
            text = ("未检测到峰（无峰/动态范围为 0/全 NaN 等）——空表，导出不可用，"
                    "不产生文件")
            self.view.setPlainText(text)
            self.status.emit(text)
            self.logged.emit("WARNING", text)

    # ── 导出（默认 peaks_<HHMMSS>.csv 到 out_root；不覆盖；无峰不产文件）──
    def default_export_path(self) -> Path:
        import time
        return resolve_out_root(self.cfg) / f"peaks_{time.strftime('%H%M%S')}.csv"

    def export_csv(self, path=None):
        from core.analysis.peaks import write_peaks_csv
        if not self._csv_rows:
            self.status.emit("没有峰可导出（空表不产文件）")
            self.logged.emit("WARNING", "没有峰可导出（空表不产文件）")
            return None
        target = Path(path) if path is not None else self.default_export_path()
        made = write_peaks_csv(self._csv_rows, target)
        self._last_output = made
        if made is not None:
            self.status.emit(f"峰表已导出：{made}")
            self.logged.emit("INFO", f"峰表已导出：{made}（{len(self._csv_rows)} 行）")
        return made

    def _choose_export(self) -> None:
        default = str(self.default_export_path())
        p, _flt = QFileDialog.getSaveFileName(self, "导出峰表 CSV", default,
                                              "CSV (*.csv)")
        if p:
            self.export_csv(Path(p))
