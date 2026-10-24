# -*- coding: utf-8 -*-
"""Φ_Δ 页的后台补渲（P1 懒渲染）：预览档先出首批图，其余在后台逐张补齐。

:class:`FigureBackfill` 持有补渲任务的全部状态（当前任务、已排过的步骤、已占位的
图件）；页面只调 :meth:`~FigureBackfill.start` / :meth:`~FigureBackfill.cancel` /
:meth:`~FigureBackfill.busy` / :meth:`~FigureBackfill.update_progress` /
:meth:`~FigureBackfill.reset`。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.pipeline import rerender_figures
from ui.workers import Job

#: A13（V2.x 审查）：补渲线程在 `wait()` 超时后**脱离页面生命周期**的强引用表。
#: 用户关窗时页面析构 → 子 QThread 连同父对象一起被销毁 → Qt 报
#: "QThread: Destroyed while thread is still running" 并可能崩进程。
#: 这里把超时线程 `setParent(None)` 后存到模块级列表，让它活到自然结束。
_ORPHAN_JOBS: list = []


class _PreviewRenderJob(Job):
    """预览档后台补渲（P1 懒渲染）。

    与普通 ``Job`` 的唯一区别：**每画完一张图就发一次** ``wrote(str)``，让画布
    能"渲染完一张、出现一张"，而不是等整批跑完才刷新。它只跑绘图阶段
    （:func:`core.pipeline.rerender_figures`），不重新读文件、不重算数值，
    因此补渲出的图与"一次跑完"完全一致。
    """

    wrote = Signal(str)
    step_done = Signal(str)

    def run(self) -> None:                              # noqa: D102 —— QThread 约定
        try:
            from core import plotting_jacs
            steps = [s for s in (self._steps or [])
                     if s in plotting_jacs.STEP_ORDER]
            if not steps:
                steps = list(plotting_jacs.STEP_ORDER)
            figdir = self._rr.figures_dir(self._mode)
            before = {p.name for p in figdir.glob("*.png")} if figdir else set()
            ok = True
            for i, s in enumerate(steps):
                if self._cancel:
                    ok = False
                    break
                self._on_progress(int(100 * i / len(steps)),
                                  f"后台补渲 {s}（{i + 1}/{len(steps)}）")
                self._on_log("INFO", f"补渲图 {s} …")
                rerender_figures(self._rr.scans, self._rr.results, self._rr.phi,
                                 self._rr.controls, self._cfg, self._rr.run_dir,
                                 export_mode=self._mode, only=[s],
                                 group_dye=self._dye)
                now = {p.name for p in figdir.glob("*.png")} if figdir else set()
                for nm in sorted(now - before):
                    self.wrote.emit(str(figdir / nm))
                before = now
                # 本步骤渲染完毕：即使一张新图都没有（如图 d 无可用 Φ_Δ）也要报，
                # 好让界面收掉它名下"渲染中"的占位
                if not self._cancel:
                    self.step_done.emit(str(s))
            self._on_progress(100, "补渲完成" if ok else "补渲已取消")
            if self._cancel:
                self.cancelled.emit()          # S6：取消也要有明确终态
                return
            self.done.emit(ok)
        except Exception as err:                        # noqa: BLE001 —— 顶层兜底
            import traceback
            self.failed.emit(f"{type(err).__name__}: {err}", traceback.format_exc())

    def configure(self, rr, cfg: dict, steps, dye: dict,
                  mode: str = "preview") -> None:
        self._rr, self._cfg, self._steps = rr, cfg, list(steps or [])
        self._dye, self._mode = dict(dye or {}), mode


class FigureBackfill(QObject):
    """一次分析之后的后台补渲：排期、逐张回显、收尾与取消。"""

    def __init__(self, page) -> None:
        super().__init__(page)
        self.page = page
        self._fig_job = None
        self._last_fig_job = None               # M5：收尾判断用的"任务身份"
        self._steps_tried: set[str] = set()     # M5：本次 run 已排过的补渲步骤
        self._promised_figs: set[str] = set()   # 本轮"已排期"的图件名（词干）

    def reset(self) -> None:
        """新的一轮分析：补渲步骤与占位重新计。"""
        self._promised_figs = set()
        self._steps_tried = set()

    def busy(self) -> bool:
        """是否还有本页发起的后台任务在跑（主任务或补渲）。"""
        if self.page._busy:
            return True
        return bool(self._fig_job is not None and self._fig_job.isRunning())

    def start(self) -> None:
        """把"还没画的预览图件"排进后台任务；首图已可看，其余画完一张出现一张。"""
        rr = self.page._last_rr
        if rr is None or not rr.run_dir or not rr.ok:
            return
        if self.page._lazy_enabled() is False:
            return                                  # 用户关掉了懒渲染 → 一次已画完
        # A07（V2.x 审查）：`expected_figures` 返回**不带扩展名**的名字，而
        # `_preview_pngs` 返回文件名（带 .png）——原来直接比较导致"已画出的图"
        # 永远不匹配：日志"其余 N 张"偏大、`_promised_figs` 去重也失效。
        # 统一按词干比较。
        have = {p.stem for p in self.page._preview_pngs(rr)}
        try:
            from core.plotting_jacs import expected_figures
            exp = expected_figures(rr.scans, rr.results, rr.phi, rr.cfg or self.page.cfg,
                                   "preview")
        except Exception:                           # noqa: BLE001
            return
        need = [n for n in exp if n not in have]
        steps = self._remaining_steps(have, exp)
        if not need or not steps:
            self.update_progress()
            return
        if self._fig_job is not None and self._fig_job.isRunning():
            return                                  # 已有补渲在跑，不重复排
        # 先占好"渲染中"的图位：否则补渲期间下拉里的图件数量会忽多忽少
        extra = [n for n in need if n not in self._promised_figs]
        if extra:
            from core.plotting_jacs import _step_of
            self.page.canvas.queued([f"{n}.png" for n in extra],
                               steps={f"{n}.png": _step_of(n) for n in extra})
            self._promised_figs |= set(extra)
        self.page.logged.emit("INFO", f"预览档先出 {len(have)} 张；其余 {len(need)} 张"
                                 "转入后台补渲（画完一张出现一张，不影响已出结果）")
        job = _PreviewRenderJob(lambda p, l: None, "后台补渲", self.page)
        job.configure(rr, self.page.params.config(self.page.cfg), steps,
                      self.page.dye_panel.dye_map(), "preview")
        job.wrote.connect(self._on_fig_written)
        job.step_done.connect(self._on_fig_step_done)
        job.progress.connect(self.page._on_progress)
        job.logged.connect(self.page.logged)
        job.failed.connect(self.page._on_failed)
        job.done.connect(self._on_figs_done)
        job.cancelled.connect(self._on_figs_cancelled)
        # QThread.finished 在 done/wrote 全部投递之后才发出 → 用它做"这一轮真的画完了"
        # 的屏障，否则末图的 wrote 可能排在 done 后面（事件队列顺序）而被漏掉
        job.finished.connect(lambda j=job: self._figs_settle(j))
        self._fig_job = job
        self._last_fig_job = job          # M5：done 会清 _fig_job，收尾判断用这个
        self._steps_tried |= set(steps)   # M5：排过就算试过（见 _remaining_steps）
        job.start()

    def _remaining_steps(self, have: set[str], exp=None) -> list[str]:
        """还有哪些**步骤**需要补渲（按 STEP_ORDER 顺序）。

        "剩余"= 该步骤**本次还没排过**，且它名下确实还有图没画出来。

        M5：原来只看"哪一步没有产物文件"，于是**画不出东西的步骤永远算剩余**
        （典型：没有参比组时图 d 一张都不产出）→ 收尾逻辑每轮都再排一个任务，
        每次秒退、无限循环。所以两件事都要判：
        ① ``exp``（预期清单）说明该步骤名下是否还有缺失的图位；
        ② ``_steps_tried`` 说明该步骤是否已经排过（排过就不再重排）。
        """
        from core.plotting_jacs import STEP_ORDER, _step_of
        done = {_step_of(n) for n in have}
        names = list(exp) if exp is not None else list(have)
        active: set = set()
        for s in STEP_ORDER:
            if s in done:
                continue
            want = [n for n in names if _step_of(n) == s]
            if want and not any(n in have for n in want):
                active.add(s)                # 该步骤确实还有没画出来的图
        return [s for s in STEP_ORDER
                if s in active and s not in self._steps_tried]

    def _expected_now(self, rr) -> list:
        """当前数据/参数下的预期图件名清单（失败 → 空表）。"""
        try:
            from core.plotting_jacs import expected_figures
            return expected_figures(rr.scans, rr.results, rr.phi,
                                    rr.cfg or self.page.cfg, "preview")
        except Exception:                           # noqa: BLE001
            return []

    def pending_count(self) -> int:
        """画布上还有多少个"渲染中"的图位（供收尾判断）。"""
        return int(self.page.canvas.pending_count())

    def _figs_settle(self, job=None) -> None:
        """一轮补渲收尾：决定"继续补下一批"还是"清占位并结束"。

        由 ``QThread.finished`` 触发（那时 ``wrote``/``done`` 都已投递完），因此
        不会漏掉末图的信号。**绝不会重复补渲**：只有还存在"没试过且有内容"的
        步骤时才续跑，否则只清占位——图 d 在无可用 Φ_Δ 时正是后者。

        M5：判断"还是不是当前这一轮"**不能只看 ``job is self._fig_job``**——
        ``done`` 信号先于 ``finished`` 投递（实测顺序 ``[done, finished]``），
        ``_on_figs_done`` 已经把 ``_fig_job`` 置 None，于是那个判断恒真、收尾/续渲
        永远提前返回（当前靠 wrote/step_done 信号"碰巧"覆盖清理；一旦补渲中途抛
        异常，占位就永远留在界面上）。现在按**任务身份**判断：仍持有这个任务对象
        即视为本轮（``_last_fig_job`` 不会被 done 清掉）。
        """
        if job is not None and job is not getattr(self, "_last_fig_job", None):
            return                                # 确实换了一轮 → 本次收尾作废
        rr = self.page._last_rr
        if rr is None or not rr.run_dir:
            return
        have = {p.name for p in self.page._preview_pngs(rr)}
        if self._remaining_steps(have, self._expected_now(rr)):
            self.start()                   # 还有步骤没试过 → 补下一批
        else:
            self._figs_settle_done()

    def _figs_settle_done(self) -> None:
        """真正收尾：清掉"到期没人认领"的排队位并刷新计数。"""
        dropped = self.page.canvas.clear_queued()
        n = self.update_progress()
        if dropped:
            self.page.logged.emit("INFO", f"预览档共 {n} 张图件"
                                     f"（{dropped} 个图位本次无内容，已从列表移除）")

    def _on_fig_written(self, path: str) -> None:
        """后台每画完一张就替换掉对应的"渲染中"占位。"""
        self.page.canvas.mark_ready(path)
        self._promised_figs.discard(Path(path).stem)   # A07：promised 存的是词干
        self.update_progress()

    def _on_fig_step_done(self, step: str) -> None:
        """某一步渲染完毕：收掉它名下"不会再有内容"的占位（如图 d 无可用 Φ_Δ）。"""
        if self.page.canvas.resolve_step(step):
            alive = set(self.page.canvas.queued_names())
            self._promised_figs &= alive
        self.update_progress()

    def _on_figs_done(self, ok) -> None:
        self._fig_job = None
        n = self.update_progress()
        if ok:
            self.page.logged.emit("INFO", f"后台补渲结束：预览档现有 {n} 张图件"
                                     "（「定稿出图」出 600 dpi 投稿图件）")

    def _on_figs_cancelled(self) -> None:
        """补渲被取消：收掉占位、把按钮放回来（S6，别让界面卡在"渲染中"）。"""
        self._fig_job = None
        dropped = self.page.canvas.clear_queued()
        self._promised_figs.clear()
        self.update_progress()
        self.page.logged.emit("WARNING", f"后台补渲已取消（{dropped} 个未出图的占位已移除）；"
                                    "已画好的图件不受影响，可随时再点「定稿出图」")

    def cancel(self) -> None:
        """取消进行中的补渲（窗口关闭或用户发起新任务时用）。

        O7：``wait(3000)`` 对"单步出图"可能不够（一张 600 dpi 大图 + 多组时能超过
        3 秒），而**超时后直接丢引用**会留下正在运行的 QThread → Qt 报
        "QThread: Destroyed while thread is still running" 并可能崩进程。
        这里等更久；仍不退就**保留引用**（不置 None），让它在后台自然结束——
        宁可多留一个对象，也不能让线程被 GC 掉。
        """
        j = self._fig_job
        if j is None:
            return
        if j.isRunning():
            j.cancel()
            if not j.wait(15000):
                self.page.logged.emit("WARNING",
                                 "后台补渲超时未停（已脱离页面，让它自然结束；"
                                 "期间请勿继续操作）")
                # A13：**脱离页面**再交给模块级强引用。只保留 self._fig_job 不够——
                # 页面析构时 QThread 作为子对象会被一起销毁，那正是要避免的崩溃面。
                try:
                    j.setParent(None)
                except (RuntimeError, AttributeError):
                    pass
                try:
                    j.finished.connect(
                        lambda jj=j: (_ORPHAN_JOBS.remove(jj)
                                      if jj in _ORPHAN_JOBS else None))
                except (RuntimeError, TypeError):
                    pass
                _ORPHAN_JOBS.append(j)
                self._fig_job = None
                return
        self._fig_job = None

    def update_progress(self) -> int:
        """刷新"已出 n 张 / 共 m 张"提示，并决定「定稿出图」是否可用。"""
        rr = self.page._last_rr
        if rr is None or not rr.run_dir:
            return 0
        have = self.page._preview_pngs(rr)
        try:
            from core.plotting_jacs import expected_figures
            exp = expected_figures(rr.scans, rr.results, rr.phi,
                                   rr.cfg or self.page.cfg, "preview")
        except Exception:                           # noqa: BLE001
            exp = []
        n, m = len(have), max(len(exp), len(have))
        pending = self.page.canvas.pending_count()
        busy = self.busy() or pending > 0
        # 还明确有图在排队就先别放开"定稿"：图集不全也能出定稿（定稿整批重画），
        # 但锁住一会儿能避免用户在补渲中途反复点、把两批 600 dpi 叠在一起
        self.page.btn_final.setEnabled(bool(rr.ok and rr.scans and rr.run_dir and not busy))
        self.page.btn_final.setToolTip(
            ("预览图件仍在后台渲染（%d/%d，排队 %d），完成后可点此出 600 dpi 投稿图件"
             % (n, m, pending)) if busy else
            "用刚才的数据直接重渲 600 dpi 的 png 到 figures/（不再重新读文件）\n"
            "产物与「直接全量跑」完全一致，是投稿用的那一份。")
        if m:
            self.page.status.emit(f"预览图件 {n}/{m}" + ("（后台渲染中…）" if busy else ""))
        return n


def orphan_render_jobs() -> list:
    """仍在跑但已脱离页面的补渲线程（A13；测试/审计用）。"""
    return list(_ORPHAN_JOBS)
