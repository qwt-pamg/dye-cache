"""② 批量队列任务卡（V2.5 FR-29）：添加目录 → 运行 → 汇总。"""

from __future__ import annotations

import logging

import copy
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)
from core.paths import resolve_out_root
from ui.pages.base import PageBase
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.verdict import VerdictCard
from ui import theme

log = logging.getLogger("dye_tool.ui.analysis")

_QUEUE_STATUS_ZH = {"pending": "排队中", "running": "运行中",
                    "ok": "完成", "failed": "失败", "cancelled": "取消"}
#: 状态 → 状态列前景色令牌
_QUEUE_STATUS_TOKEN = {"ok": "success", "failed": "danger", "cancelled": "text_subtle"}

#: 流程条三步；检查器第 i 页、:meth:`BatchQueuePanel._step_summary` 都按这个顺序
STEPS = ("添加目录", "运行", "汇总")
STEP_HINTS = (
    "把要一起跑的数据目录排进队列",
    "按顺序逐个目录跑完整 Φ_Δ 分析",
    "每次运行结束都会写一份汇总表",
)

#: 「目录」列只写目录名（完整路径在悬停提示里）
QUEUE_HEADERS = ["#", "目录", "状态", "用时", "说明"]
COL_NO, COL_DIR, COL_STATUS, COL_TIME, COL_NOTE = range(5)


def _fmt_duration(sec) -> str:
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return "—"
    return f"{v:.1f} s" if v < 120 else f"{v / 60:.1f} min"


class BatchQueuePanel(PageBase):
    """V2.5 FR-29：批量目录队列（多目录串行跑 Φ_Δ，失败继续，产出汇总表）。

    口径（设计文档 §5.1）：

    * 串行逐目录；每目录 = 与 Φ_Δ 页**共用**的 core.pipeline.run_directory；
    * **队列目录不经 DataPage**：整定只来自各目录自己的 dye_batch.yaml
      （+ analysis_preset），卡内已写明；
    * 失败目录 → 汇总行"失败:<原因>"，继续下一个；
    * 取消 = 当前目录完成后停止（run 内不中断，如实标注）。
    """

    TITLE = "批量队列"
    SUBTITLE = ("添加/拖入多个数据目录 → 串行逐目录跑完整 Φ_Δ 链路（各目录自己的 "
                "dye_batch.yaml 与 analysis_preset 自动生效）→ 失败目录继续。"
                "取消在当前目录跑完后生效（run 内不中断）。")
    #: 队列忙状态变化 → AnalysisPage 做互斥（禁用/恢复其它任务卡按钮）
    busy_changed = Signal(bool)

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.dirs: list[Path] = []
        self._last_entries: list = []
        self._last_summary: str = ""
        self._external_busy = False
        #: 正在跑的那一批目录（运行中队列不可改，结论条按它计数）
        self._running_dirs: list[Path] = []
        self._build()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：流程条（挂到分析页顶栏）/ 中栏结论条 + 队列表 + 汇总 · 右栏检查器。"""
        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        center = self._build_center_column()
        inspector = self._build_inspector()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(three_columns(None, center, inspector), 1)
        lay.addWidget(self.progress_row())
        self.btn_add.clicked.connect(self._choose_dirs)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self.clear_dirs)
        self.btn_start.clicked.connect(self.start_queue)
        self.btn_open_file.clicked.connect(self._open_summary)
        self.btn_open_dir.clicked.connect(self._open_summary_dir)
        self.setAcceptDrops(True)
        self._go_step(0)
        self._refresh_dir_list()

    def top_bar_widget(self) -> QWidget:
        """分析页顶部细条插槽的约定：交出流程条（同一对象，切走时会还回来）。"""
        return self.flow_bar

    def _build_center_column(self) -> QWidget:
        """中栏：结论条 / 队列表（一目录一行，带上次运行状态）/ 汇总表路径与打开按钮。"""
        self.verdict = VerdictCard()
        self.tbl_queue = QTableWidget(0, len(QUEUE_HEADERS))
        self.tbl_queue.setObjectName("queueTable")
        self.tbl_queue.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.tbl_queue.verticalHeader().setVisible(False)
        self.tbl_queue.setAlternatingRowColors(True)
        self.tbl_queue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_queue.setToolTip("要排队的目录（可多选后移除；也支持从资源管理器拖入多个目录）")
        hdr = self.tbl_queue.horizontalHeader()
        for c in range(len(QUEUE_HEADERS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_DIR, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_NOTE, QHeaderView.Stretch)
        box_q = QGroupBox("队列")
        lq = QVBoxLayout(box_q)
        lq.addWidget(self.tbl_queue)

        self.lbl_summary = QLabel("汇总表：尚未生成")
        self.lbl_summary.setWordWrap(True)
        theme.mark(self.lbl_summary, "muted")
        self.btn_open_file = QPushButton("打开汇总表")
        self.btn_open_dir = QPushButton("打开所在文件夹")
        self.btn_open_file.setEnabled(False)
        self.btn_open_dir.setEnabled(False)
        box_res = QGroupBox("汇总表（一目录一行）")
        row = QHBoxLayout(box_res)
        row.addWidget(self.lbl_summary, 1)
        row.addWidget(self.btn_open_file)
        row.addWidget(self.btn_open_dir)

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(box_q, 1)
        lay.addWidget(box_res)
        center.setMinimumWidth(380)
        return center

    def _build_inspector(self) -> QWidget:
        """右栏检查器：① 增删目录 ② 运行说明 ③ 汇总说明；主按钮「开始排队」。"""
        def _hint(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            theme.mark(lbl, "hint")
            return lbl

        # ① 添加目录
        self.btn_add = QPushButton("添加目录…")
        self.btn_remove = QPushButton("移除选中")
        self.btn_clear = QPushButton("清空")
        self.lbl_count = QLabel("")
        theme.mark(self.lbl_count, "muted")
        p_add = QWidget()
        la = QVBoxLayout(p_add)
        la.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        for b in (self.btn_add, self.btn_remove, self.btn_clear):
            row.addWidget(b)
        row.addStretch(1)
        la.addLayout(row)
        la.addWidget(self.lbl_count)
        la.addWidget(_hint("也可以把多个数据目录一起拖进来；同一个目录只排一次。"))
        la.addWidget(_hint("队列目录不经「数据」页：染料名、时间等整定只来自各目录自己的 "
                           "dye_batch.yaml，数据页里改的不适用于队列。"))
        la.addStretch(1)
        # ② 运行
        self.lbl_out = QLabel("")
        theme.mark(self.lbl_out, "muted")
        self.lbl_out.setWordWrap(True)
        p_run = QWidget()
        lr = QVBoxLayout(p_run)
        lr.setContentsMargins(0, 0, 0, 0)
        lr.addWidget(_hint("按顺序一个目录一个目录地跑完整 Φ_Δ 分析（预览档图件），"
                           "各目录的 dye_batch.yaml 与分析预设自动生效。"))
        lr.addWidget(_hint("某个目录失败会记下原因，接着跑下一个。"))
        lr.addWidget(_hint("「取消」在当前目录跑完后生效，正在跑的那个不会被打断。"))
        lr.addWidget(self.lbl_out)
        lr.addStretch(1)
        # ③ 汇总
        p_sum = QWidget()
        ls = QVBoxLayout(p_sum)
        ls.setContentsMargins(0, 0, 0, 0)
        ls.addWidget(_hint("每次运行结束写一份汇总 xlsx：每个目录一行，含状态、各组 k_obs 与 Φ_Δ；"
                           "被取消的目录也会记上。"))
        ls.addWidget(_hint("各目录完整的图件与表格在它自己的输出目录里。"))
        ls.addStretch(1)

        self.btn_start = QPushButton("▶ 开始排队")
        theme.mark(self.btn_start, "primary")
        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((p_add, p_run, p_sum)):
            ins.set_page(i, w)
        ins.add_action(self.btn_start)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        return ins

    # ══════════════ 流程条 / 结论条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：流程条高亮、检查器换页（中栏的队列表一直在）。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        self.flow_bar.set_current(idx)
        self.inspector.set_current(idx)

    def _counts(self) -> dict:
        """上次运行各状态的目录数。"""
        out = {"ok": 0, "failed": 0, "cancelled": 0}
        for e in self._last_entries:
            st = str(e.get("status"))
            if st in out:
                out[st] += 1
        return out

    def _ran_dirs(self) -> list:
        return [str(e.get("data_dir", "")) for e in self._last_entries]

    def _queue_changed_since_run(self) -> bool:
        """上次运行之后队列改过（增删目录）→ 运行/汇总两步标「需要重算」。"""
        return bool(self._last_entries) and self._ran_dirs() != [str(d) for d in self.dirs]

    def _step_summary(self, idx: int) -> str:
        """某一步当前状态的一行摘要（只读已有状态）。"""
        if idx == 0:
            return f"{len(self.dirs)} 个目录" if self.dirs else "还没添加目录"
        if idx == 1:
            if self._busy:
                return "运行中…"
            if not self._last_entries:
                return "还没运行"
            c = self._counts()
            return f"{c['ok']} 成功 · {c['failed']} 失败" + (
                f" · {c['cancelled']} 取消" if c["cancelled"] else "")
        return Path(self._last_summary).name if self._last_summary else "还没生成"

    def _update_flow(self) -> None:
        stale = self._queue_changed_since_run()
        ran = bool(self._last_entries) and not self._busy
        done = (bool(self.dirs), ran, ran and bool(self._last_summary))
        for i, ok in enumerate(done):
            state = STEP_STALE if (i and ok and stale) else (STEP_DONE if ok else STEP_TODO)
            self.flow_bar.set_state(i, state)
        self.inspector.refresh_summaries()

    def _update_verdict(self) -> None:
        """结论条：「N 个目录 · M 成功 · K 失败」，右侧列出各目录状态。"""
        n = len(self.dirs)
        if self._busy:
            self.verdict.set_empty(f"正在跑 {len(self._ran_or_queued())} 个目录…",
                                   "「取消」会在当前目录跑完后生效")
            return
        if not self._last_entries:
            if not n:
                self.verdict.set_empty("队列是空的",
                                       "在右侧「添加目录」，也可以把多个数据目录一起拖进来")
            else:
                self.verdict.set_empty(f"已排 {n} 个目录，还没运行",
                                       "按「开始排队」逐个跑完整 Φ_Δ 分析；某个目录失败会接着跑下一个")
            return
        entries = self._last_entries
        c = self._counts()
        headline = f"{len(entries)} 个目录 · {c['ok']} 成功 · {c['failed']} 失败"
        if c["cancelled"]:
            headline += f" · {c['cancelled']} 取消"
        lines = [f"汇总表：{Path(self._last_summary).name}" if self._last_summary
                 else "汇总表没有生成（没有有效目录或写盘失败）"]
        first_fail = next((e for e in entries if e.get("status") == "failed"), None)
        if first_fail is not None:
            lines.append(f"{Path(str(first_fail.get('data_dir', ''))).name} 失败："
                         f"{first_fail.get('error') or '原因见运行日志'}")
        if self._queue_changed_since_run():
            lines.append("运行之后队列改过：结果对应的是上一次的目录")
        groups = [{"name": Path(str(e.get("data_dir", ""))).name,
                   "sub": _fmt_duration(e.get("duration_s")),
                   "pill": _QUEUE_STATUS_ZH.get(str(e.get("status")), str(e.get("status"))),
                   "kind": "ok" if e.get("status") == "ok" else "warn",
                   "color": "success" if e.get("status") == "ok" else "warn"}
                  for e in entries]
        self.verdict.set_verdict(label="批量队列", value=f"{c['ok']}/{len(entries)}",
                                 error="成功", headline=headline, lines=lines[:3],
                                 groups=groups)

    def _ran_or_queued(self) -> list:
        return list(self._running_dirs or self.dirs)

    # ── 拖拽（与 DataPage 同口径：本地目录 URL） ──
    def dragEnterEvent(self, event) -> None:            # noqa: N802 —— Qt 约定
        try:
            if event.mimeData().hasUrls():
                event.acceptProposedAction()
        except Exception:                               # noqa: BLE001
            pass

    def dropEvent(self, event) -> None:                 # noqa: N802 —— Qt 约定
        paths = []
        try:
            paths = [Path(u.toLocalFile()) for u in event.mimeData().urls()
                     if u.isLocalFile() and u.toLocalFile()]
        except Exception:                               # noqa: BLE001
            paths = []
        n = self.add_directories(paths)
        if n:
            self.logged.emit("INFO", f"拖入 {n} 个目录（队列共 {len(self.dirs)} 个）")
        event.acceptProposedAction()

    # ── 目录列表 ──
    def add_directories(self, paths) -> int:
        """加入目录（去重、只接受存在的目录）；返回新增个数（测试/拖拽共用）。"""
        added = 0
        for raw in paths or []:
            try:
                p = Path(raw)
            except (TypeError, ValueError):
                continue
            if p.is_dir() and p not in self.dirs:
                self.dirs.append(p)
                added += 1
        if added:
            self._refresh_dir_list()
        return added

    def _choose_dirs(self) -> None:
        d = self.pick_dir("选择要排队的目录（可重复添加多个）")
        if d:
            self.add_directories([d])

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.tbl_queue.selectionModel().selectedRows()},
                      reverse=True)
        for r in rows:
            if 0 <= r < len(self.dirs):
                self.dirs.pop(r)
        self._refresh_dir_list()

    def clear_dirs(self) -> None:
        self.dirs = []
        self._refresh_dir_list()

    def _refresh_dir_list(self) -> None:
        """重建队列表：每个目录一行，状态取上次运行里同一目录的记录（没跑过 = 待运行）。"""
        last = {str(e.get("data_dir", "")): e for e in self._last_entries}
        self.tbl_queue.setRowCount(len(self.dirs))
        for r, d in enumerate(self.dirs):
            e = last.get(str(d))
            status = str((e or {}).get("status", ""))
            cells = [str(r + 1), d.name,
                     _QUEUE_STATUS_ZH.get(status, status) if e else "待运行",
                     _fmt_duration((e or {}).get("duration_s")) if e else "",
                     str((e or {}).get("error") or "")]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == COL_DIR:
                    it.setToolTip(str(d))
                if c == COL_NOTE and text:
                    it.setToolTip(text)
                if c == COL_STATUS:
                    it.setForeground(theme.color(_QUEUE_STATUS_TOKEN.get(status, "text_muted")))
                self.tbl_queue.setItem(r, c, it)
        self._refresh_start_enabled()
        self._refresh_workbench()

    def _refresh_workbench(self) -> None:
        """目录数、输出位置、流程条与结论条跟着队列/运行状态走。"""
        self.lbl_count.setText(f"队列里有 {len(self.dirs)} 个目录" if self.dirs
                               else "队列是空的")
        self.lbl_out.setText(f"产物写到：{resolve_out_root(self.cfg)}")
        self._update_flow()
        self._update_verdict()

    # ── 互斥（FR-29.4） ──
    def set_external_busy(self, busy: bool) -> None:
        """其它任务（手动 Φ_Δ/对比/ε/A@λ）在跑 → 禁止开始排队。"""
        self._external_busy = bool(busy)
        self._refresh_start_enabled()

    def _refresh_start_enabled(self) -> None:
        idle = not self._busy
        self.btn_start.setEnabled(bool(self.dirs) and idle and not self._external_busy)
        self.btn_clear.setEnabled(bool(self.dirs) and idle)
        self.btn_remove.setEnabled(bool(self.dirs) and idle)
        self.btn_add.setEnabled(idle)

    # ── 运行（串行 / 容错 / 目录边界取消） ──
    def start_queue(self) -> None:
        if self._busy:
            self.status.emit("队列已在运行，请等待完成或取消")
            return
        if self._external_busy:
            self.status.emit("其它分析正在运行，等待其完成后再开始排队")
            return
        if not self.dirs:
            self.logged.emit("WARNING", "队列为空：请先添加数据目录")
            return
        out_root = resolve_out_root(self.cfg)
        dirs = list(self.dirs)
        cfg = self.cfg
        self._last_entries, self._last_summary = [], ""
        self._running_dirs = dirs
        self.lbl_summary.setText("汇总表：运行中…")

        def task(progress_cb, log_cb):
            import time as _time
            from core.analysis.pipeline import run_directory
            from core.report.summary import collect_queue_summary, default_output_path
            from core.report.excel_export import write_queue_summary_workbook
            entries: list[dict] = []
            job = self.job
            total = max(1, len(dirs))
            for i, d in enumerate(dirs, 1):
                if job is not None and job.is_cancel_requested():
                    break                                   # 目录边界取消检查点
                if progress_cb:
                    progress_cb(int((i - 1) * 100 / total),
                                f"第 {i}/{total} 个：{d.name}")
                t0 = _time.perf_counter()
                try:
                    rr = run_directory(d, copy.deepcopy(cfg), out_root=out_root,
                                       write=True, export_mode="preview",
                                       progress_cb=progress_cb, log_cb=log_cb)
                    ok = bool(getattr(rr, "ok", False))
                    status = "ok" if ok else "failed"
                    error = "" if ok else str(getattr(rr, "message", "") or "运行失败")
                except Exception as err:                    # noqa: BLE001 —— 失败继续
                    rr, status = None, "failed"
                    error = f"{type(err).__name__}: {err}"
                    if log_cb:
                        log_cb("ERROR", f"目录失败（继续下一个）：{d} — {error}")
                entries.append({"data_dir": str(d), "status": status, "rr": rr,
                                "error": error,
                                "duration_s": _time.perf_counter() - t0})
            for d in dirs[len(entries):]:                   # 取消后未跑到的目录
                entries.append({"data_dir": str(d), "status": "cancelled",
                                "rr": None, "error": "", "duration_s": None})
            rows = collect_queue_summary(entries)
            summary = ""
            try:
                got = write_queue_summary_workbook(
                    rows, default_output_path(out_root, "queue"))
                summary = str(got or "")
            except Exception as err:                        # noqa: BLE001
                if log_cb:
                    log_cb("WARNING", f"队列汇总表写出失败：{err}")
            self._last_entries = entries
            self._last_summary = summary
            if progress_cb:
                progress_cb(100, "队列完成")
            return entries

        self.run_task(task, "批量队列", self._on_queue_done,
                      busy_widgets=[self.btn_add, self.btn_remove, self.btn_clear,
                                    self.btn_start, self.tbl_queue])
        # F8 静态守卫：不许覆写 PageBase 脚手架（_on_cancelled 等）→ 用信号追加钩子
        if getattr(self, "job", None) is not None:
            self.job.cancelled.connect(self._after_queue_cancelled)
            # V25-O4：失败/异常与线程结束也要释放跨页互斥（否则其它任务卡永久禁用）
            self.job.failed.connect(self._after_queue_failed)
            self.job.finished.connect(self._after_queue_finished)
        if self._busy:
            self.busy_changed.emit(True)
            self._go_step(1)
        self._refresh_dir_list()

    def _after_queue_cancelled(self) -> None:
        """取消后的队列收尾（信号钩子；不覆写 PageBase 脚手架）。"""
        self._fill_status(self._last_entries)
        self.logged.emit("WARNING", "队列已在目录边界取消：完成的目录仍写入汇总表")
        self._after_finished()
        self.busy_changed.emit(False)

    def _after_queue_failed(self, summary: str = "", detail: str = "") -> None:
        """队列任务异常结束：释放互斥 + 界面状态兜底（V25-O4）。"""
        _ = detail
        self._fill_status(self._last_entries)
        self.logged.emit(
            "WARNING", f"队列异常结束：{summary or '未知错误'}（界面已恢复可用）")
        self._after_finished()
        self.busy_changed.emit(False)

    def _after_queue_finished(self) -> None:
        """线程结束的幂等兜底：成功/失败/取消都释放跨页互斥（V25-O4）。

        此前只有 done/cancelled 两条路径发 busy_changed(False)；Job 异常（failed）
        路径只走 PageBase._on_failed → 跨页互斥永远不释放，其它任务卡的运行按钮
        保持禁用。
        """
        self._running_dirs = []
        self.busy_changed.emit(False)
        self._refresh_dir_list()

    def _on_queue_done(self, entries) -> None:
        entries = list(entries or [])
        self._fill_status(entries)
        ok = sum(1 for e in entries if e.get("status") == "ok")
        fail = sum(1 for e in entries if e.get("status") == "failed")
        self.logged.emit("INFO" if fail == 0 else "WARNING",
                         f"队列结束：{len(entries)} 个目录，成功 {ok}，失败 {fail}")
        self._after_finished()
        self.busy_changed.emit(False)

    def _after_finished(self) -> None:
        if self._last_summary:
            self.lbl_summary.setText(f"汇总表：{self._last_summary}")
            self.logged.emit("INFO", f"队列汇总表：{self._last_summary}")
        else:
            self.lbl_summary.setText("汇总表：未生成（无有效目录或写盘失败）")
        has = bool(self._last_summary)
        self.btn_open_file.setEnabled(has)
        self.btn_open_dir.setEnabled(has)
        self._running_dirs = []
        if self._last_entries:
            self._go_step(2)
        self._refresh_dir_list()

    def _fill_status(self, entries) -> None:
        """把本次运行的逐目录记录写回队列表的状态 / 用时 / 说明列。"""
        self._last_entries = list(entries or [])
        self._refresh_dir_list()

    # ── 打开产物（离屏环境不可用 → 只记日志） ──
    def _open_summary(self) -> None:
        if not self._last_summary:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_summary))
        if not ok:
            self.logged.emit("WARNING", f"无法打开汇总表：{self._last_summary}")

    def _open_summary_dir(self) -> None:
        if not self._last_summary:
            return
        from ui.widgets.export_bar import open_folder
        open_folder(Path(self._last_summary).parent)
