"""② 峰分析任务卡（V2.7 FR-37/A43；混搭工作台：流程条 / 结论卡 + 峰表 / 分步检查器）"""

from __future__ import annotations

import logging
import statistics

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QDoubleSpinBox
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHeaderView
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QPlainTextEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSpinBox
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QTableWidget
from PySide6.QtWidgets import QTableWidgetItem
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from ui.pages.base import PageBase
from ui.widgets.export_bar import open_folder
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.result_actions import ResultActions
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.verdict import VerdictCard
from ui.pages.tasks.common import _park_unshown

log = logging.getLogger("dye_tool.ui.analysis")
from core.paths import resolve_out_root
from ui import theme

#: 流程条三步；检查器第 i 页、:meth:`PeaksPanel._step_summary` 都按这个顺序
STEPS = ("导入", "找峰参数", "导出")
STEP_HINTS = (
    "选光谱目录；后台逐谱找峰，不写 run 目录、不进流水线",
    "判据按每条谱自己的动态范围（最大 − 最小）算；NaN 断点分段检测，不跨段合并，全谱范围",
    "导出峰表 CSV；默认 peaks_<时分秒>.csv 落输出根目录，同名自动加 -2 不覆盖",
)

#: 把峰按波长归成"峰带"时，相邻峰最多差多少 nm
BAND_GAP_NM = 5.0
#: 结论卡右侧最多列几个峰带
_MAX_BANDS = 4


class PeaksPanel(PageBase):
    """V2.7 A 道（V2.6 D 道结转）：FR-37 峰分析（②第 8 卡）。

    纯函数在 core.analysis.peaks（Q-E5 判据默认值），本面板只做
    目录 → 后台 Job → 峰表 → 导出 CSV；不写 run 目录、不进流水线。
    默认导出名 peaks_<HHMMSS>.csv 落 out_root，同名自动加 -2 不覆盖；
    无峰 → 空表 + 提示，不产文件（A43.3/A43.5）。

    判据可在「找峰参数」一步临时调整（只传给本次 ``find_peaks_spectrum``，
    默认值即 core 的 Q-E5 默认，不新增配置键）。
    """

    TITLE = "峰分析（P8）"
    SUBTITLE = ("选光谱目录 → 后台逐谱找峰（突出度≥5%、峰高≥2%、间距≥5 点、≤20 峰、"
                "NaN 分段）→ 峰表 → 导出 CSV。")

    #: 表格列（与 CSV 表头一致，文件列 + 峰序号）
    COLUMNS = ("文件", "峰#", "波长 (nm)", "强度", "突出度", "宽 (pts)", "宽 (nm)")

    #: V2.6 FR-40.5：结果区「下一步」→ ③ 图件 / 定稿出图
    next_step_requested = Signal(str)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self._csv_rows: list = []
        self._n_scan = 0
        self._last_output = None
        self._summary_text = ""
        #: 最近一次找峰用的（目录, 判据）——与当前不同 = 峰表过期；None = 还没找过
        self._result_key: Optional[tuple] = None
        self._run_key: Optional[tuple] = None
        self._build()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条 / 中栏结论卡 + 峰表 + 结果区 · 右栏检查器。"""
        self.btn_dir = QPushButton("选择光谱目录…")
        self.btn_run = QPushButton("▶ 找峰")
        theme.mark(self.btn_run, "primary")
        self.btn_run.setEnabled(False)
        self.btn_export = QPushButton("导出 CSV…")
        self.btn_export.setEnabled(False)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_run.clicked.connect(self._run)
        self.btn_export.clicked.connect(self._choose_export)

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

    def _build_center_column(self) -> QWidget:
        """中栏：结论卡 / 峰表 / 结果（摘要 + 动作条）。"""
        self.verdict = VerdictCard()

        self.tbl = QTableWidget()
        self.tbl.setObjectName("peaksTable")
        self.tbl.setColumnCount(len(self.COLUMNS))
        self.tbl.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl.setAlternatingRowColors(True)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(self.COLUMNS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        box_tbl = QGroupBox("峰表")
        lt = QVBoxLayout(box_tbl)
        lt.addWidget(self.tbl)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setPlaceholderText("峰表摘要（无峰时会给出提示）")
        self.view.setMaximumHeight(90)
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
        split.setSizes([540, 130])

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(split, 1)
        center.setMinimumWidth(360)
        return center

    def _build_inspector(self) -> QWidget:
        """右栏检查器：导入 / 找峰参数 / 导出。"""
        from core.analysis.peaks import (DEFAULT_HEIGHT_REL, DEFAULT_MAX_PEAKS,
                                         DEFAULT_MIN_DISTANCE_PTS, DEFAULT_PROMINENCE_REL)
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
        # ② 找峰参数（默认 = core 的 Q-E5 默认值）
        p_par = QWidget()
        lp = QVBoxLayout(p_par)
        lp.setContentsMargins(0, 0, 0, 0)
        self.sp_prom = QDoubleSpinBox()
        self.sp_prom.setRange(0.1, 100.0)
        self.sp_prom.setDecimals(1)
        self.sp_prom.setSuffix(" %")
        self.sp_prom.setValue(DEFAULT_PROMINENCE_REL * 100.0)
        self.sp_prom.setToolTip("峰比两侧谷底至少高出动态范围的这个比例")
        self.sp_height = QDoubleSpinBox()
        self.sp_height.setRange(0.1, 100.0)
        self.sp_height.setDecimals(1)
        self.sp_height.setSuffix(" %")
        self.sp_height.setValue(DEFAULT_HEIGHT_REL * 100.0)
        self.sp_height.setToolTip("峰顶相对谱最小值至少高出动态范围的这个比例")
        self.sp_dist = QSpinBox()
        self.sp_dist.setRange(1, 1000)
        self.sp_dist.setSuffix(" 点")
        self.sp_dist.setValue(DEFAULT_MIN_DISTANCE_PTS)
        self.sp_dist.setToolTip("相邻两个峰至少隔多少个数据点")
        self.sp_max = QSpinBox()
        self.sp_max.setRange(1, 500)
        self.sp_max.setSuffix(" 个峰")
        self.sp_max.setValue(DEFAULT_MAX_PEAKS)
        self.sp_max.setToolTip("单条谱最多保留几个峰（按突出度从高到低截断）")
        form = QFormLayout()
        form.addRow("突出度 ≥", self.sp_prom)
        form.addRow("峰高 ≥", self.sp_height)
        form.addRow("峰间距 ≥", self.sp_dist)
        form.addRow("每谱最多", self.sp_max)
        lp.addLayout(form)
        self.btn_defaults = QPushButton("恢复默认判据")
        self.btn_defaults.setToolTip("四个判据改回默认：突出度 5%、峰高 2%、间距 5 点、每谱最多 20 个峰")
        self.btn_defaults.clicked.connect(self._reset_params)
        lp.addWidget(self.btn_defaults, 0, Qt.AlignLeft)
        note = QLabel("只用于这一次找峰，不写进配置。")
        note.setWordWrap(True)
        theme.mark(note, "hint")
        lp.addWidget(note)
        lp.addStretch(1)
        for sp in (self.sp_prom, self.sp_height, self.sp_dist, self.sp_max):
            sp.valueChanged.connect(lambda *_: self._update_flow())
        # ③ 导出
        p_out = QWidget()
        lo = QVBoxLayout(p_out)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.addWidget(self.btn_export, 0, Qt.AlignLeft)
        self.lbl_out = QLabel("")
        theme.mark(self.lbl_out, "muted")
        self.lbl_out.setWordWrap(True)
        lo.addWidget(self.lbl_out)
        self.btn_open_dir = QPushButton("打开所在文件夹")
        self.btn_open_dir.setEnabled(False)
        self.btn_open_dir.clicked.connect(self._open_output_dir)
        lo.addWidget(self.btn_open_dir, 0, Qt.AlignLeft)
        note2 = QLabel("没有峰时不产生文件。")
        note2.setWordWrap(True)
        theme.mark(note2, "hint")
        lo.addWidget(note2)
        lo.addStretch(1)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((p_import, p_par, p_out)):
            ins.set_page(i, w)
        ins.add_action(self.btn_run)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        return ins

    # ══════════════ 判据 ══════════════
    def find_params(self) -> dict:
        """当前判据，键名即 :func:`core.analysis.peaks.find_peaks_spectrum` 的关键字参数。"""
        return {"prominence_rel": round(self.sp_prom.value() / 100.0, 6),
                "height_rel": round(self.sp_height.value() / 100.0, 6),
                "min_distance_pts": int(self.sp_dist.value()),
                "max_peaks": int(self.sp_max.value())}

    def _reset_params(self) -> None:
        from core.analysis.peaks import (DEFAULT_HEIGHT_REL, DEFAULT_MAX_PEAKS,
                                         DEFAULT_MIN_DISTANCE_PTS, DEFAULT_PROMINENCE_REL)
        self.sp_prom.setValue(DEFAULT_PROMINENCE_REL * 100.0)
        self.sp_height.setValue(DEFAULT_HEIGHT_REL * 100.0)
        self.sp_dist.setValue(DEFAULT_MIN_DISTANCE_PTS)
        self.sp_max.setValue(DEFAULT_MAX_PEAKS)

    # ══════════════ 流程条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：流程条高亮、检查器换页、摘要与前后按钮跟着变。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        if idx and not self.flow_bar.step_enabled(idx):
            idx = 0
        self.flow_bar.set_current(idx)
        nxt = idx + 1 < len(STEPS) and self.flow_bar.step_enabled(idx + 1)
        self.inspector.set_current(idx, next_enabled=nxt)

    def _step_summary(self, idx: int) -> str:
        """某一步当前设置的一行摘要（只读已有状态）。"""
        if idx == 0:
            return self.data_dir.name if self.data_dir is not None else "还没选光谱目录"
        if idx == 1:
            p = self.find_params()
            return (f"突出 {p['prominence_rel'] * 100:g}% · 峰高 {p['height_rel'] * 100:g}% · "
                    f"间距 {p['min_distance_pts']} 点")
        return Path(self._last_output).name if self._last_output else "还没导出"

    def _current_key(self) -> tuple:
        return (str(self.data_dir), tuple(sorted(self.find_params().items())))

    def _update_flow(self) -> None:
        """按目录 / 峰表 / 导出给每一步打 完成 / 需要重算 / 未做 标记。"""
        if not hasattr(self, "inspector"):
            return
        loaded = self.data_dir is not None and self.data_dir.is_dir()
        ran = self._result_key is not None
        stale = ran and self._result_key != self._current_key()
        done = [loaded, ran, self._last_output is not None]
        for i in range(len(STEPS)):
            if i:
                self.flow_bar.set_step_enabled(i, loaded, "先选择光谱目录")
            state = STEP_STALE if (stale and i >= 1 and done[i]) else (
                STEP_DONE if done[i] else STEP_TODO)
            self.flow_bar.set_state(i, state)
        cur = self.flow_bar.current()
        self.inspector.set_next_enabled(
            cur + 1 < len(STEPS) and self.flow_bar.step_enabled(cur + 1))
        if self._last_output is not None:
            self.lbl_out.setText(f"峰表已导出：\n{self._last_output}")
        elif self._csv_rows:
            self.lbl_out.setText(f"默认位置：\n{self.default_export_path().parent}")
        else:
            self.lbl_out.setText("先找峰；有峰才能导出")
        self.btn_open_dir.setEnabled(self._last_output is not None)
        self.inspector.refresh_summaries()

    # ══════════════ 结论卡 ══════════════
    @staticmethod
    def _main_peaks(rows: list) -> dict:
        """每个文件突出度最高的那个峰的波长：``{文件: nm}``。"""
        best: dict = {}
        for row in rows:
            name, wl, prom = row[0], float(row[2]), float(row[4])
            if name not in best or prom > best[name][1]:
                best[name] = (wl, prom)
        return {k: v[0] for k, v in best.items()}

    @staticmethod
    def _bands(rows: list) -> list:
        """把所有峰按波长归成峰带（相邻 ≤ :data:`BAND_GAP_NM`）：``[(中位 nm, 文件数)]``，
        按文件数从多到少。"""
        pts = sorted((float(r[2]), r[0]) for r in rows)
        bands, cur = [], []
        for wl, name in pts:
            if cur and wl - cur[-1][0] > BAND_GAP_NM:
                bands.append(cur)
                cur = []
            cur.append((wl, name))
        if cur:
            bands.append(cur)
        out = [(statistics.median(w for w, _ in b), len({n for _, n in b})) for b in bands]
        return sorted(out, key=lambda x: (-x[1], x[0]))

    def _update_verdict(self) -> None:
        """总峰数 + 主峰位置（只复述峰表，不新增判断）。"""
        if self._result_key is None:
            self.verdict.set_empty(
                "还没有结果",
                "点「找峰」开始" if self.data_dir is not None else "先选择光谱目录")
            return
        rows = self._csv_rows
        if not rows:
            self.verdict.set_failed(
                "没有找到峰",
                f"{self._n_scan} 条可用光谱都没有达到判据的峰；可以在「找峰参数」里放宽判据")
            return
        files = len({r[0] for r in rows})
        main = self._main_peaks(rows)
        mains = sorted(main.values())
        bands = self._bands(rows)
        # 主峰 = 离"各谱主峰中位"最近的那个峰带（标题与右侧峰带列表同一个数）
        centre = min(bands, key=lambda b: abs(b[0] - statistics.median(mains)))[0]
        lines = [f"{files} 个文件有峰（共 {self._n_scan} 条可用光谱），平均每谱 "
                 f"{len(rows) / max(1, files):.1f} 个"]
        if len(mains) > 1:
            lines.append(f"各谱主峰在 {mains[0]:.1f}–{mains[-1]:.1f} nm 之间")
        if self._last_output:
            lines.append(f"已导出 {Path(self._last_output).name}")
        groups = []
        for wl, n in bands[:_MAX_BANDS]:
            is_main = wl == centre
            groups.append({"name": f"{wl:.1f} nm", "sub": f"{n} 个谱",
                           "pill": "主峰" if is_main else "",
                           "kind": "ok", "color": "accent" if is_main else "text_muted"})
        self.verdict.set_verdict(label="峰分析 · 总峰数", value=str(len(rows)), error="个峰",
                                 headline=f"主峰在 {centre:.1f} nm",
                                 lines=lines[:3], groups=groups)

    # ── 目录 / 运行 ──
    def set_data_dir(self, data_dir) -> int:
        d = Path(data_dir)
        self.data_dir = d
        self.btn_run.setEnabled(d.is_dir() and not self._busy)
        self.status.emit(f"光谱目录：{d}")
        self.lbl_dir.setText(str(d) if d.is_dir() else f"{d}\n（不是有效目录）")
        self._update_verdict()
        self._update_flow()
        if d.is_dir():
            self._go_step(1)
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
        params = self.find_params()

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
                    peaks = find_peaks_spectrum(sc.wl, sc.a, **params)
                except ValueError as err:
                    log_cb(f"{sc.path.name}：参数/光谱异常——{err}")
                    continue
                if peaks:
                    rows.extend(peak_rows(sc.path.name, peaks))
            return rows, n_scan

        self._run_key = self._current_key()
        self.actions.reset()
        self.run_task(task, "峰分析", self._finish,
                      busy_widgets=[self.btn_dir, self.btn_run, self.btn_export,
                                    self.tbl])

    def _finish(self, res) -> None:
        rows, n_scan = res
        self._csv_rows = list(rows)
        self._n_scan = int(n_scan)
        self._result_key = self._run_key
        self._last_output = None                   # 旧导出对应旧峰表
        self.tbl.setRowCount(len(self._csv_rows))
        for r, row in enumerate(self._csv_rows):
            for c, val in enumerate(row):
                it = QTableWidgetItem(str(val))
                if c in (0,):
                    it.setToolTip(str(val))
                it.setTextAlignment(Qt.AlignCenter if c in (1, 2, 5)
                                    else Qt.AlignLeft | Qt.AlignVCenter)
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
        self._summary_text = text
        self.actions.show_result(bool(self._csv_rows), text=self._table_text(),
                                 failure_hint="" if self._csv_rows else
                                 "如何修复：在「找峰参数」里调低突出度 / 峰高后点「重试」")
        self._update_verdict()
        self._update_flow()
        self._go_step(2 if self._csv_rows else 1)

    def _table_text(self) -> str:
        """「复制结果」的文本：摘要 + 制表符分隔的峰表（粘进 Excel 即成表）。"""
        lines = [self._summary_text, "", "\t".join(self.COLUMNS)]
        lines += ["\t".join(str(v) for v in row) for row in self._csv_rows]
        return "\n".join(lines) if self._csv_rows else self._summary_text

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
            self.actions.show_result(True, output=made, text=self._table_text())
        self._update_verdict()
        self._update_flow()
        return made

    def _choose_export(self) -> None:
        default = str(self.default_export_path())
        p, _flt = QFileDialog.getSaveFileName(self, "导出峰表 CSV", default,
                                              "CSV (*.csv)")
        if p:
            self.export_csv(Path(p))

    def _open_output_dir(self) -> None:
        if self._last_output is None:
            self.status.emit("还没有导出文件")
            return
        folder = Path(self._last_output).parent
        if not open_folder(folder):
            self.status.emit(f"无法自动打开，请手动前往：{folder}")
