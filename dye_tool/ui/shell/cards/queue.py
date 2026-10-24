"""② 批量队列任务卡（V2.5 FR-29）"""

from __future__ import annotations

import logging

import copy
from pathlib import Path
from typing import Optional
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from ui.pages.base_page import PageBase

log = logging.getLogger("dye_tool.ui.analysis")
from core.paths import resolve_out_root
from ui import theme

_QUEUE_STATUS_ZH = {"pending": "排队中", "running": "运行中",
                    "ok": "完成", "failed": "失败", "cancelled": "取消"}



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
        self._build()

    # ── UI ──
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.header())
        hint = QLabel("提示：队列目录**不经①数据页**，整定只来自各目录自己的 "
                      "dye_batch.yaml；①数据页的会话整定/染料名不适用于队列。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)
        self.list_dirs = QListWidget()
        self.list_dirs.setObjectName("queueDirList")
        self.list_dirs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list_dirs.setToolTip(
            "要排队的目录（可多选移除；也支持从资源管理器拖入多个目录）")
        lay.addWidget(self.list_dirs, 1)
        bar = QHBoxLayout()
        self.btn_add = QPushButton("添加目录…")
        self.btn_remove = QPushButton("移除选中")
        self.btn_clear = QPushButton("清空")
        self.btn_start = QPushButton("▶ 开始排队")
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_remove)
        bar.addWidget(self.btn_clear)
        bar.addStretch(1)
        bar.addWidget(self.btn_start)
        lay.addLayout(bar)
        lay.addWidget(self.progress_row())
        lay.addWidget(QLabel("每个目录的状态："))
        self.list_status = QListWidget()
        self.list_status.setObjectName("queueStatusList")
        lay.addWidget(self.list_status, 1)
        row = QHBoxLayout()
        self.lbl_summary = QLabel("汇总表：尚未生成")
        self.lbl_summary.setWordWrap(True)
        theme.mark(self.lbl_summary, "muted")
        self.btn_open_file = QPushButton("打开汇总表")
        self.btn_open_dir = QPushButton("打开所在文件夹")
        self.btn_open_file.setEnabled(False)
        self.btn_open_dir.setEnabled(False)
        row.addWidget(self.lbl_summary, 1)
        row.addWidget(self.btn_open_file)
        row.addWidget(self.btn_open_dir)
        lay.addLayout(row)
        self.btn_add.clicked.connect(self._choose_dirs)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self.clear_dirs)
        self.btn_start.clicked.connect(self.start_queue)
        self.btn_open_file.clicked.connect(self._open_summary)
        self.btn_open_dir.clicked.connect(self._open_summary_dir)
        self.setAcceptDrops(True)
        self._refresh_dir_list()

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
        rows = sorted({self.list_dirs.row(it)
                       for it in self.list_dirs.selectedItems()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.dirs):
                self.dirs.pop(r)
        self._refresh_dir_list()

    def clear_dirs(self) -> None:
        self.dirs = []
        self._refresh_dir_list()

    def _refresh_dir_list(self) -> None:
        self.list_dirs.clear()
        for p in self.dirs:
            self.list_dirs.addItem(QListWidgetItem(str(p)))
        self._refresh_start_enabled()

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
        self.list_status.clear()
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
                                    self.btn_start, self.list_dirs])
        # F8 静态守卫：不许覆写 PageBase 脚手架（_on_cancelled 等）→ 用信号追加钩子
        if getattr(self, "job", None) is not None:
            self.job.cancelled.connect(self._after_queue_cancelled)
            # V25-O4：失败/异常与线程结束也要释放跨页互斥（否则其它任务卡永久禁用）
            self.job.failed.connect(self._after_queue_failed)
            self.job.finished.connect(self._after_queue_finished)
        if self._busy:
            self.busy_changed.emit(True)

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
        self.busy_changed.emit(False)

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
        self._refresh_dir_list()

    def _fill_status(self, entries) -> None:
        self.list_status.clear()
        for i, e in enumerate(entries or [], 1):
            status = _QUEUE_STATUS_ZH.get(str(e.get("status")),
                                          str(e.get("status")))
            if status == "失败" and e.get("error"):
                status = f"失败：{e['error']}"
            self.list_status.addItem(f"{i}. [{status}] {e.get('data_dir', '')}")

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
