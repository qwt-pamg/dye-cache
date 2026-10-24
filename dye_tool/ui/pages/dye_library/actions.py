# -*- coding: utf-8 -*-
"""染料库按钮背后的动作：浏览 / 打开 / 另存 / 载入参数，以及两件后台活——
拼版导出（V2.4-B，FR-27.3）与多 run 统计报表（V2.5 FR-30）。

后台活都走 :class:`ui.workers.Job`（可取消、忙时禁用入口）；全部动作只读 runs/ 产物，
另存只写用户选的位置。页面类 :class:`~ui.pages.dye_library.page.DyeLibraryPage`
继承本混入类。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QListWidgetItem, QMessageBox

from core.dyes import HISTORY_NAME, read_run_params, read_time_overrides
from core.paths import default_out_root
from .model import latest_run_dir

log = logging.getLogger("dye_tool.ui.dye_library")


class LibraryActions:
    """按钮背后的动作（混入 :class:`DyeLibraryPage`；用到它的控件与 ``_notify``）。"""

    # ══════════════ 浏览 / 打开 / 另存 / 载入参数 ══════════════
    def _browse_root(self) -> None:
        """选择另一个 runs 根目录并刷新。"""
        start = str(self.out_root if self.out_root.is_dir() else default_out_root())
        d = QFileDialog.getExistingDirectory(self, "选择 runs 结果根目录", start)
        if d:
            self.reload(d)

    def _open_dye_folder(self) -> None:
        """在系统文件管理器中打开该染料目录（只浏览，不改动）。"""
        e = self._cur
        d = e.get("dir") if e else None
        if not d:
            self._notify("该染料没有对应的归档目录（先选中左侧一个染料）", "WARNING")
            return
        p = Path(d)
        if not p.is_dir():
            self._notify(f"目录不存在：{p}", "WARNING")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
            self._notify(f"无法自动打开，请手动前往：{p}", "WARNING")

    def _emit_latest_params(self) -> None:
        """读最近一次 run 的参数快照并发出 :attr:`params_loaded` 信号。"""
        run_dir = latest_run_dir(self._cur)
        if run_dir is None:
            self._notify("没有找到该染料的 run 目录", "WARNING")
            return
        try:
            params = read_run_params(run_dir)
            overrides = read_time_overrides(run_dir)
        except Exception as err:                        # noqa: BLE001 —— 快照损坏不能崩页面
            log.warning("读取 run 参数快照失败 %s: %s", run_dir, err)
            params, overrides = {}, {}
        if not params and not overrides:
            self._notify(
                f"没有参数快照：{run_dir}"
                "（多为 V1.0 旧结果；请改选有快照的 run，或重新跑一次分析）",
                "WARNING")
            return
        self.params_loaded.emit(dict(params), dict(overrides))
        self._notify(
            f"已载入最近一次 run 的参数（run 参数 {len(params)} 项、"
            f"时间整定 {len(overrides)} 项）→ 可直接「开始分析」复现")

    def _export_history(self) -> None:
        """把 history.csv 另存到用户指定位置（原档案不动）。"""
        e = self._cur
        d = e.get("dir") if e else None
        src = Path(d) / HISTORY_NAME if d else None
        if src is None or not src.is_file():
            self._notify("该染料没有 history.csv 可导出", "WARNING")
            return
        default_name = f"{(e or {}).get('dye') or src.parent.name}_{HISTORY_NAME}"
        dest, _ = QFileDialog.getSaveFileName(self, "导出 history.csv",
                                              str(src.parent / default_name),
                                              "CSV 文件 (*.csv);;所有文件 (*)")
        if not dest:
            return
        try:
            target = Path(dest)
            if target.resolve() == src.resolve():       # 防呆：别让它覆盖原档案
                self._notify("目标就是原档案本身，已取消（原档案保持不动）", "WARNING")
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
        except Exception as err:                        # noqa: BLE001 —— 写失败只提示
            QMessageBox.critical(self, "导出失败", f"{type(err).__name__}: {err}")
            return
        self._notify(f"history.csv 已导出：{target}")

    def _open_legacy_item(self, item: QListWidgetItem) -> None:
        """双击旧目录 → 在文件管理器中打开（**只浏览，不改名不移动**）。"""
        for p in self._legacy:
            if p.name == item.text() and p.is_dir():
                if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(p))):
                    self._notify(f"无法自动打开，请手动前往：{p}", "WARNING")
                return

    def load_run_params(self, row: int) -> bool:
        """载入**该行 run** 的参数快照（A27；= 行级版的"载入 run 参数"）。"""
        run_dir = self._run_dir_of_row(row)
        if run_dir is None:
            self._notify("该行没有可用的 run 目录", "WARNING")
            return False
        try:
            params = read_run_params(run_dir)
            overrides = read_time_overrides(run_dir)
        except Exception as err:                       # noqa: BLE001 —— 快照坏不崩页
            log.warning("读取 run 参数快照失败 %s: %s", run_dir, err)
            params, overrides = {}, {}
        if not params and not overrides:
            self._notify(f"没有参数快照：{run_dir}", "WARNING")
            return False
        self.params_loaded.emit(dict(params), dict(overrides))
        self._notify(f"已载入该 run 参数：{run_dir.name}"
                     f"（run 参数 {len(params)} 项、时间整定 {len(overrides)} 项）")
        return True

    def _open_path(self, p: Optional[Path]) -> None:
        """在资源管理器中打开目录（离屏测试不可断言，沿用 Q-B5 降级口径）。"""
        if p is None:
            return
        try:
            from PySide6.QtGui import QDesktopServices
            from PySide6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
            self._notify(f"已在文件管理器中打开：{p}")
        except Exception as err:                       # noqa: BLE001
            self._notify(f"无法打开资源管理器：{err}", "WARNING")

    # ══════════════ V2.4-B（FR-27.3）：拼版导出 ══════════════
    def compose_selected(self) -> bool:
        """把勾选的图件拼版导出（对话框选规格 → 后台 Job，可取消）。"""
        if self._compose_job is not None and self._compose_job.isRunning():
            self._notify("已有拼版任务在运行，请等待完成或先取消", "WARNING")
            return False
        imgs = self._selected_figures()
        if not imgs:
            self._notify("请先在「该 run 的图件」里勾选至少一张", "WARNING")
            return False
        from PySide6.QtWidgets import QDialog
        from ui.widgets.layout_dialog import LayoutDialog

        dlg = LayoutDialog(images=imgs, default_dir=self._run_fig_dir
                           or (imgs[0].parent), parent=self)
        try:
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return False
            spec = dlg.spec()
            out_dir = dlg.out_dir()
        finally:
            dlg.deleteLater()
        from ui.workers import Job

        from core.render.layout import compose_layout

        def task(progress_cb, log_cb):
            job = self._compose_job
            return compose_layout(
                imgs, spec, getattr(self, "cfg", None), out_dir,
                cancel_cb=(lambda: bool(job is not None
                                        and job.is_cancel_requested())),
                progress_cb=progress_cb)

        job = Job(task, "拼版导出", self)
        self._compose_job = job

        def _progress(pct, stage):
            self.status.emit(f"拼版导出：{stage}（{pct}%）")

        def _failed(msg, tb):
            self._notify(f"拼版导出失败：{msg}" + (f"\n{tb.splitlines()[-1]}"
                                                   if tb else ""), "ERROR")

        job.progress.connect(_progress)
        job.logged.connect(self.logged.emit)
        job.done.connect(self._on_compose_done)
        job.failed.connect(_failed)
        job.cancelled.connect(lambda: self._notify("拼版导出已取消（未写出文件）"))
        job.finished.connect(self._on_compose_finished)
        self.btn_compose.setEnabled(False)
        self.setBusyCompose(True)
        job.start()
        return True

    def setBusyCompose(self, busy: bool) -> None:      # noqa: N802 —— 与 Qt 命名风格无关的公共钩子
        """拼版进行中禁用入口（Job 结束/失败/取消时恢复）。"""
        self.btn_compose.setEnabled(not busy and bool(self._run_figs))
        self.btn_compose.setText("拼版导出中…" if busy else "拼版导出…")

    def _on_compose_done(self, made) -> None:
        names = "、".join(Path(p).name for p in (made or []))
        self._notify(f"拼版导出完成：{names}" if names else "拼版导出完成（无文件）")

    def _on_compose_finished(self) -> None:
        self._compose_job = None
        self.setBusyCompose(False)

    # ══════════════ V2.5 FR-30：多 run 统计报表（只读、后台 Job） ══════════════
    def _stat_scope(self):
        """当前统计范围 → dye 过滤值（None = 全部 runs）。"""
        if self.cmb_stat_scope.currentData() != "current":
            return None
        cur = self._cur or {}
        return str(cur.get("dye") or "") or None

    def _on_stats_clicked(self) -> bool:
        """按钮入口：选保存位置（默认 runs 根目录的时间戳文件名）→ 后台 Job。"""
        if self._stats_job is not None and self._stats_job.isRunning():
            self._notify("已有统计报表任务在运行，请等待完成或先取消", "WARNING")
            return False
        if self.cmb_stat_scope.currentData() == "current" and self._stat_scope() is None:
            self._notify("请先在左侧选择一个染料，或把范围改为「全部 runs」", "WARNING")
            return False
        from core.report.summary import default_output_path
        default_path = default_output_path(self.out_root, "statistics")
        p, _flt = QFileDialog.getSaveFileName(
            self, "保存统计报表", str(default_path), "Excel 工作簿 (*.xlsx)")
        if not p:
            return False
        return self.run_statistics(p)

    def run_statistics(self, out_path=None) -> bool:
        """扫描 runs → 明细 + 统计两 sheet（V2.5 FR-30.2）。

        全程只读（A30.2：扫描前后 runs/ 逐字节不变）；后台 Job 可取消、忙禁用。
        """
        if self._stats_job is not None and self._stats_job.isRunning():
            self._notify("已有统计报表任务在运行，请等待完成或先取消", "WARNING")
            return False
        if self.cmb_stat_scope.currentData() == "current" and self._stat_scope() is None:
            self._notify("请先在左侧选择一个染料，或把范围改为「全部 runs」", "WARNING")
            return False
        scope = self._stat_scope()
        root = Path(self.out_root)
        if out_path is None:
            from core.report.summary import default_output_path
            out_path = default_output_path(root, "statistics")
        from ui.workers import Job
        from core.report.summary import collect_statistics
        from core.report.excel_export import write_statistics_workbook

        def task(progress_cb, log_cb):
            job = self._stats_job
            if progress_cb:
                progress_cb(10, f"扫描 runs（{root}）")
            info = collect_statistics(root, dye_filter=scope)
            if job is not None and job.is_cancel_requested():
                return {"info": info, "path": ""}
            for e in (info.get("errors") or [])[:20]:
                if log_cb:
                    log_cb("WARNING", f"[统计] 跳过坏 run {e.get('run')}: {e.get('错误')}")
            if progress_cb:
                progress_cb(70, "聚合统计")
            if not info.get("detail"):
                return {"info": info, "path": ""}
            p = write_statistics_workbook(info["detail"], info["stat"], out_path,
                                          getattr(self, "cfg", None))
            return {"info": info, "path": str(p or "")}

        job = Job(task, "统计报表", self)
        self._stats_job = job

        def _progress(pct, stage):
            self.status.emit(f"统计报表：{stage}（{pct}%）")

        def _failed(msg, tb):
            self._notify("统计报表失败：" + str(msg), "ERROR")

        job.progress.connect(_progress)
        job.logged.connect(self.logged.emit)
        job.done.connect(self._on_stats_done)
        job.failed.connect(_failed)
        job.cancelled.connect(lambda: self._notify("统计报表已取消（未写出文件）"))
        job.finished.connect(self._on_stats_finished)
        self._set_busy_stats(True)
        job.start()
        return True

    def _set_busy_stats(self, busy: bool) -> None:
        self.btn_stats.setEnabled(not busy)
        self.cmb_stat_scope.setEnabled(not busy)
        self.btn_stats.setText("统计中…" if busy else "统计报表…")

    def _on_stats_done(self, res) -> None:
        res = res if isinstance(res, dict) else {}
        info = res.get("info") or {}
        path = str(res.get("path") or "")
        if not path:
            self._notify("没有可统计的 run（或写盘失败）——未生成空文件", "WARNING")
            return
        self._last_stats_path = path
        self.btn_open_stats.setEnabled(True)
        self._notify(
            f"统计报表：{path}（明细 {len(info.get('detail') or [])} 行 / "
            f"统计 {len(info.get('stat') or [])} 组 / 跳过坏 run "
            f"{len(info.get('errors') or [])} 个）")

    def _on_stats_finished(self) -> None:
        self._stats_job = None
        self._set_busy_stats(False)

    def _open_stats(self) -> None:
        if not self._last_stats_path:
            return
        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_stats_path))
            self._notify(f"已打开：{self._last_stats_path}")
        except Exception as err:                       # noqa: BLE001
            self._notify(f"无法打开统计报表：{err}", "WARNING")
