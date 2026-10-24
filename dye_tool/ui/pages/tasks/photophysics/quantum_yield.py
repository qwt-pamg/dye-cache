# -*- coding: utf-8 -*-
"""子页「量子产率 φ」（FR-42）：相对法荧光量子产率 φ 与亮度 = ε×φ。"""
from __future__ import annotations

import logging

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)
from core.analysis import fluorescence_qy
from core.report.stokes_table import group_from_path

from core.report.stokes_table import row_from_spectra
from ui import theme

from .common import _as_float, _finite, _fmt_sig3, _spin, _empty_char_row

log = logging.getLogger("dye_tool.ui.analysis")


class QuantumYieldTab(QWidget):
    """量子产率 φ 这一步：中栏 = 吸收/发射谱列表 + 读数；检查器 = 测量条件与参比。

    本控件自身是中栏内容；:attr:`settings` 是检查器页，:attr:`primary_actions` 是检查器主按钮。
    """

    def __init__(self, panel) -> None:
        super().__init__()
        #: 所属任务卡：任务调度、日志/状态信号、配置与外部数据源都经它取
        self.panel = panel
        self._phi_spectra: list = []
        self._phi_abs: list = []
        self._phi_em: list = []
        self._last_phi: dict = {}
        self._eps_source = "手填"
        self._build()

    def _build(self) -> None:
        self._build_center()
        self._build_settings()
        self.btn_compute_phi = QPushButton("重新计算")
        self.btn_compute_phi.setToolTip("选择和参数一改就会自动重算；这里可以手动再算一次")
        self.btn_add_phi = QPushButton("加入表征表")
        theme.mark(self.btn_add_phi, "primary")
        self.btn_add_phi.setEnabled(False)
        self.primary_actions = QWidget()
        row = QHBoxLayout(self.primary_actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.btn_add_phi, 1)
        row.addWidget(self.btn_compute_phi)

        self.btn_phi_dir.clicked.connect(self._choose_phi_dir)
        self.btn_compute_phi.clicked.connect(lambda *_: self.compute_phi())
        self.btn_add_phi.clicked.connect(lambda *_: self.add_phi_to_table())
        self.btn_eps_from.clicked.connect(lambda *_: self._on_eps_from_result())
        for lst in (self.lst_phi_abs, self.lst_phi_em):
            lst.currentRowChanged.connect(lambda *_: self._phi_preview())
        for sp in (self.sp_lam_ex, self.sp_phi_ref, self.sp_i_ref,
                   self.sp_a_ref, self.sp_n_ref, self.sp_n, self.sp_eps):
            sp.valueChanged.connect(lambda *_: self._phi_preview())
        self.ed_comp_phi.textChanged.connect(lambda *_: self.panel.refresh())

    def _build_center(self) -> None:
        """中栏：左右两张谱列表（各单选一支）+ 读数行与提示。"""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        split = QSplitter(Qt.Horizontal)
        self.lst_phi_abs = QListWidget()
        self.lst_phi_em = QListWidget()
        for lst, title in ((self.lst_phi_abs, "吸收谱"), (self.lst_phi_em, "发射谱")):
            lst.setSelectionMode(QAbstractItemView.SingleSelection)
            lst.setAlternatingRowColors(True)
            box = QGroupBox(title)
            vb = QVBoxLayout(box)
            vb.addWidget(lst)
            split.addWidget(box)
        split.setSizes([380, 380])
        lay.addWidget(split, 1)

        box = QGroupBox("读数")
        lr = QVBoxLayout(box)
        preview = QHBoxLayout()
        self.lbl_phi_a = QLabel("A(λex) = —")
        self.lbl_phi_i = QLabel("I = —")
        self.lbl_phi = QLabel("φ = —")
        self.lbl_brightness = QLabel("亮度 = —")
        for lb in (self.lbl_phi_a, self.lbl_phi_i, self.lbl_phi, self.lbl_brightness):
            theme.mark(lb, "strong")
            preview.addWidget(lb)
        preview.addStretch(1)
        lr.addLayout(preview)
        self.lbl_phi_warn = QLabel("")
        self.lbl_phi_warn.setWordWrap(True)
        theme.mark(self.lbl_phi_warn, "warnHint")
        lr.addWidget(self.lbl_phi_warn)
        self.lbl_phi_msg = QLabel("未选择配对：请在左右列表各选一支谱")
        self.lbl_phi_msg.setWordWrap(True)
        theme.mark(self.lbl_phi_msg, "muted")
        lr.addWidget(self.lbl_phi_msg)
        lay.addWidget(box)

    def _build_settings(self) -> None:
        """检查器页：光谱目录 + 测量条件与参比 + 入表用的条件/化合物。"""
        self.settings = QWidget()
        lay = QVBoxLayout(self.settings)
        lay.setContentsMargins(0, 0, 0, 0)
        self.btn_phi_dir = QPushButton("选择光谱目录…")
        self.lbl_phi_dir = QLabel("未选择目录")
        theme.mark(self.lbl_phi_dir, "muted")
        self.lbl_phi_dir.setWordWrap(True)
        lay.addWidget(self.btn_phi_dir, 0, Qt.AlignLeft)
        lay.addWidget(self.lbl_phi_dir)
        hint = QLabel("相对法：φ = φ_ref × (I/I_ref) × (A_ref/A) × (n²/n_ref²)。"
                      "I 是发射谱积分强度，A 是 λex 处的吸光度（建议 A ≤ 0.05）。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        form = QFormLayout()
        self.sp_lam_ex = _spin(200.0, 1200.0, 450.0, 1.0, 1, " nm")
        self.sp_phi_ref = _spin(0.0, 5.0, 0.54, 0.01, 4)
        self.sp_i_ref = _spin(0.0, 1.0e12, 1.0, 0.1, 6)
        self.sp_a_ref = _spin(0.0, 10.0, 0.05, 0.01, 4)
        self.sp_n_ref = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_n = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_eps = _spin(0.0, 1.0e12, 0.0, 1000.0, 3)
        self.btn_eps_from = QPushButton("从 ε 结果填入")
        self.btn_eps_from.setToolTip("取「摩尔消光系数 ε」任务最近一次成功的结果")
        self.lbl_eps_source = QLabel("ε 来源：手填")
        theme.mark(self.lbl_eps_source, "hint")
        self.lbl_eps_source.setWordWrap(True)
        # 量程很大的数值框按量程撑宽，窄栏里放不下 → 统一给个小的最小宽度
        for sp in (self.sp_lam_ex, self.sp_phi_ref, self.sp_i_ref, self.sp_a_ref,
                   self.sp_n_ref, self.sp_n, self.sp_eps):
            sp.setMinimumWidth(80)
        eps_row = QHBoxLayout()
        eps_row.setContentsMargins(0, 0, 0, 0)
        eps_row.addWidget(self.btn_eps_from)
        eps_row.addWidget(self.lbl_eps_source, 1)
        eps_wrap = QWidget()
        eps_wrap.setLayout(eps_row)
        self.ed_cond_phi = QLineEdit()
        self.ed_cond_phi.setPlaceholderText("如 PBS / MeOH（缺项留空）")
        self.ed_comp_phi = QLineEdit()
        self.ed_comp_phi.setPlaceholderText("默认取吸收文件名组别；可手改")
        form.addRow("激发波长 λex", self.sp_lam_ex)
        self.lbl_lam_note = QLabel("λex 只需落在吸收谱范围内；发射积分取发射谱自己的范围")
        theme.mark(self.lbl_lam_note, "hint")
        self.lbl_lam_note.setWordWrap(True)
        form.addRow("", self.lbl_lam_note)
        from ui.help_texts import PARAM_HELP
        for _key, _w in (("excitation", self.sp_lam_ex),
                         ("phi_ref", self.sp_phi_ref), ("I_ref", self.sp_i_ref),
                         ("A_ref", self.sp_a_ref), ("n_ref", self.sp_n_ref),
                         ("epsilon", self.sp_eps), ("n", self.sp_n),
                         ("condition", self.ed_cond_phi),
                         ("compound", self.ed_comp_phi)):
            if _key in PARAM_HELP:
                _w.setToolTip(PARAM_HELP[_key])
        eps_wrap.setToolTip(PARAM_HELP["epsilon"])
        form.addRow("参比 φ_ref", self.sp_phi_ref)
        form.addRow("参比积分 I_ref", self.sp_i_ref)
        form.addRow("参比 A_ref", self.sp_a_ref)
        form.addRow("参比折射率 n_ref", self.sp_n_ref)
        form.addRow("样品折射率 n", self.sp_n)
        form.addRow("ε (M⁻¹cm⁻¹)", self.sp_eps)
        form.addRow("", eps_wrap)
        form.addRow("条件/溶剂", self.ed_cond_phi)
        form.addRow("化合物/组别", self.ed_comp_phi)
        lay.addLayout(form)
        lay.addStretch(1)

    def _choose_phi_dir(self) -> None:
        d = self.panel.pick_dir("选择光谱目录（φ）")
        if d:
            self.set_phi_data_dir(d, force=True)

    def set_phi_data_dir(self, data_dir, *, force: bool = False) -> None:
        """选择光谱目录 → run_task 载入吸收/发射候选（忙禁用、可取消）。"""
        d = Path(data_dir)
        if not force and d == self.panel.data_dir and self._phi_spectra:
            return
        self.panel.data_dir = d
        self.lbl_phi_dir.setText(str(d))
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.panel.cfg)
        except Exception as err:                       # noqa: BLE001
            files = []
            log.warning("枚举 φ 候选目录失败：%s", err)
        if not files:
            self._phi_spectra = []
            self._phi_abs = []
            self._phi_em = []
            self._populate_phi_lists()
            self.panel.status.emit(f"{d} 下没有可解析的光谱文件")
            self.panel.logged.emit("WARNING", f"{d} 下没有找到光谱文件")
            return
        from core.inputs.io import load_spectra_files

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            spectra = load_spectra_files(files, self.panel.cfg)
            progress_cb(90, "载入完成")
            return spectra

        self.panel.run_task(task, "载入光谱（φ）", self._on_phi_loaded,
                      busy_widgets=[self.btn_phi_dir, self.lst_phi_abs,
                                    self.lst_phi_em])

    def _on_phi_loaded(self, spectra) -> None:
        self._phi_spectra = list(spectra or [])
        fails = [s for s in self._phi_spectra
                 if not getattr(s, "ok", True)]
        for s in fails[:20]:
            name = Path(str(getattr(s, "path", ""))).name
            self.panel.logged.emit("WARNING",
                             f"剔除 {name}：{getattr(s, 'excluded', '') or '读取失败'}")
        self._populate_phi_lists()
        n_abs, n_em = len(self._phi_abs), len(self._phi_em)
        self.lbl_phi_dir.setText(f"{self.panel.data_dir}（吸收 {n_abs} / 发射 {n_em}"
                                 + (f" / 失败 {len(fails)}" if fails else "") + "）")
        self.panel.status.emit(f"φ 候选已载入：吸收 {n_abs} 支 / 发射 {n_em} 支")
        self.panel.logged.emit("INFO", f"φ 候选载入：吸收 {n_abs} 支，发射 {n_em} 支，"
                                 f"剔除 {len(fails)} 支")

    def _populate_phi_lists(self) -> None:
        self._phi_abs = [s for s in self._phi_spectra
                         if getattr(s, "ok", True)
                         and getattr(s, "kind", "") == "abs"]
        self._phi_em = [s for s in self._phi_spectra
                        if getattr(s, "ok", True)
                        and getattr(s, "kind", "") == "em"]
        for lst, items in ((self.lst_phi_abs, self._phi_abs),
                           (self.lst_phi_em, self._phi_em)):
            lst.blockSignals(True)
            try:
                lst.clear()
                lst.setCurrentRow(-1)
                for i, s in enumerate(items):
                    group = group_from_path(getattr(s, "path", ""), self.panel.cfg)
                    wl = getattr(s, "wl", None)
                    n_pts = 0 if wl is None else len(wl)
                    it = QListWidgetItem(
                        f"{group} / {Path(str(s.path)).name} / {n_pts} 点")
                    it.setData(Qt.UserRole, i)
                    it.setToolTip(str(getattr(s, "path", "")))
                    lst.addItem(it)
                if items:
                    lst.setCurrentRow(0)
            finally:
                lst.blockSignals(False)
        self._phi_preview()

    def _phi_selected(self):
        a = self._phi_abs[self.lst_phi_abs.currentRow()] \
            if 0 <= self.lst_phi_abs.currentRow() < len(self._phi_abs) else None
        e = self._phi_em[self.lst_phi_em.currentRow()] \
            if 0 <= self.lst_phi_em.currentRow() < len(self._phi_em) else None
        return a, e

    def _phi_preview(self) -> dict:
        """即时刷新 A/I/φ/亮度预览（计算口径全部来自 fluorescence_qy），再刷新结论条。"""
        try:
            return self._compute_preview()
        finally:
            self.panel.refresh()

    def _compute_preview(self) -> dict:
        self._last_phi = {}
        a, e = self._phi_selected()
        self.btn_add_phi.setEnabled(False)
        if a is None or e is None:
            for lb, txt in ((self.lbl_phi_a, "A(λex) = —"), (self.lbl_phi_i, "I = —"),
                            (self.lbl_phi, "φ = —"),
                            (self.lbl_brightness, "亮度 = —")):
                lb.setText(txt)
            self.lbl_phi_warn.setText("")
            self.lbl_phi_msg.setText("未选择配对：请在左右列表各选一支吸收谱与发射谱")
            return {}
        try:
            res = fluorescence_qy.compute_fluorescence_qy(
                a, e, lam_ex=float(self.sp_lam_ex.value()),
                i_ref=float(self.sp_i_ref.value()),
                a_ref=float(self.sp_a_ref.value()),
                phi_ref=float(self.sp_phi_ref.value()),
                n_ref=float(self.sp_n_ref.value()),
                n=float(self.sp_n.value()))
        except Exception as err:                       # noqa: BLE001
            res = {"ok": False, "error": f"{type(err).__name__}: {err}"}
        self._last_phi = dict(res or {})
        self._apply_phi_result(self._last_phi)
        return self._last_phi

    def _apply_phi_result(self, res: dict) -> None:
        ok = bool((res or {}).get("ok"))
        if not ok:
            for lb, txt in ((self.lbl_phi_a, "A(λex) = —"), (self.lbl_phi_i, "I = —"),
                            (self.lbl_phi, "φ = —"),
                            (self.lbl_brightness, "亮度 = —")):
                lb.setText(txt)
            self.lbl_phi_warn.setText("")
            msg = (res or {}).get("error") or (res or {}).get("reason") or "无法计算"
            self.lbl_phi_msg.setText(f"不可计算：{msg}")
            self.btn_add_phi.setEnabled(False)
            return
        a_ex, i_em = res.get("a_ex"), res.get("i_em")
        phi = res.get("phi")
        self.lbl_phi_a.setText(f"A(λex) = {_as_float(a_ex):.4f}"
                               if _finite(a_ex) else "A(λex) = —")
        self.lbl_phi_i.setText(f"I = {_as_float(i_em):.6g}"
                               if _finite(i_em) else "I = —")
        display = res.get("phi_display") or _fmt_sig3(phi)
        self.lbl_phi.setText(f"φ = {display}")
        br = self.brightness(phi)
        self.lbl_brightness.setText(
            f"亮度 = {br:.4g} M⁻¹cm⁻¹" if _finite(br) else "亮度 = —（请填 ε）")
        warnings = [str(w) for w in (res.get("warnings") or [])]
        self.lbl_phi_warn.setText("；".join(warnings))
        self.lbl_phi_msg.setText("可以「加入表征表」了（不会自动入表）")
        self.btn_add_phi.setEnabled(True)

    def _on_eps_from_result(self) -> None:
        res = {}
        provider = self.panel._eps_provider
        if callable(provider):
            try:
                res = dict(provider() or {})
            except Exception as err:                    # noqa: BLE001
                log.warning("读取既有 ε 结果失败：%s", err)
                res = {}
        eps = _as_float(res.get("eps"))
        if _finite(eps) and eps > 0:
            self.sp_eps.setValue(eps)
            self._eps_source = "既有 ε 结果"
            unit = str(res.get("eps_unit") or "M⁻¹cm⁻¹")
            self.lbl_eps_source.setText(f"ε 来源：既有 ε 结果（{eps:.4g} {unit}）")
            self.panel.status.emit("已从「摩尔消光系数 ε」结果填入 ε")
            self._phi_preview()
            return
        self.panel.status.emit("没有可用的 ε 结果：请先在「摩尔消光系数 ε」卡成功计算一次，或手填 ε")
        self.panel.logged.emit("WARNING", "光物理表征：没有可用的既有 ε 结果")

    def compute_phi(self, checked: bool = False):       # noqa: ANN001 —— Qt 槽兼容
        """显式计算并刷新 φ/亮度预览；返回结果 dict（失败 ok=False + 中文原因）。"""
        _ = checked
        return dict(self._phi_preview() or {})

    def add_phi_to_table(self) -> bool:
        """把当前 φ 结果落一行到表征总表（显式动作；不自动入表）。"""
        res = self._last_phi or {}
        if not res.get("ok"):
            self.panel.status.emit("请先成功计算 φ 再加入表征表")
            return False
        a, e = self._phi_selected()
        if a is None or e is None:
            return False
        try:
            sr = row_from_spectra(a, e)
            lam_a, lam_e = sr.lam_abs, sr.lam_em
            d_lam, d_nu = sr.delta_lambda, sr.delta_wavenumber
            group = sr.group
        except Exception as err:                       # noqa: BLE001 —— 退化不阻断
            log.warning("φ 入表取峰失败：%s", err)
            lam_a = lam_e = d_lam = d_nu = float("nan")
            group = group_from_path(getattr(a, "path", ""), self.panel.cfg)
        eps = float(self.sp_eps.value())
        phi = res.get("phi")
        br = self.brightness(phi)
        row = _empty_char_row()
        row.update({
            "condition": self.ed_cond_phi.text().strip(),
            "compound": self.ed_comp_phi.text().strip() or group,
            "abs_file": Path(str(getattr(a, "path", ""))).name,
            "em_file": Path(str(getattr(e, "path", ""))).name,
            "lam_abs": lam_a, "lam_em": lam_e,
            "delta_lambda": d_lam, "delta_wavenumber": d_nu,
            "epsilon": eps if eps > 0 else float("nan"),
            "phi": float(phi) if _finite(phi) else float("nan"),
            "brightness": br,
            "note": "；".join([str(w) for w in (res.get("warnings") or [])]),
        })
        self.panel.add_row(row)
        self.panel.show_char_table()
        return True

    # ── 工作台：亮度 / 摘要 / 结论条 ──
    def brightness(self, phi) -> float:
        """亮度 = ε × φ；ε 没填（≤ 0）时为 NaN。"""
        eps = float(self.sp_eps.value())
        return float(fluorescence_qy.brightness(eps, phi)) if eps > 0 else float("nan")

    def has_result(self) -> bool:
        return bool(self._last_phi.get("ok"))

    def summary(self) -> str:
        """检查器里这一步折叠后的一行摘要。"""
        if not self._phi_spectra:
            return "还没选光谱目录"
        if self.has_result():
            phi = self._last_phi.get("phi")
            text = f"φ = {self._last_phi.get('phi_display') or _fmt_sig3(phi)}"
            br = self.brightness(phi)
            return text + (f" · 亮度 {br:.3g}" if _finite(br) else "")
        a, e = self._phi_selected()
        return "还没选齐一对谱" if a is None or e is None else "这一对算不出 φ"

    def fill_verdict(self, card) -> None:
        """结论条：大数 = φ，一句话 = 亮度，说明行 = A、I 与提醒。"""
        a, e = self._phi_selected()
        if a is None or e is None:
            card.set_empty("还没选好一对谱",
                           "在中间各选一支吸收谱和发射谱，φ 即时算出" if self._phi_spectra
                           else "先在右侧选光谱目录，再各选一支吸收谱和发射谱")
            return
        res = self._last_phi
        if not res.get("ok"):
            card.set_failed("这一对算不出 φ",
                            str(res.get("error") or res.get("reason") or "无法计算"))
            return
        phi = res.get("phi")
        br = self.brightness(phi)
        name = self.ed_comp_phi.text().strip() or group_from_path(
            getattr(a, "path", ""), self.panel.cfg)
        headline = (f"亮度 ε×φ = {br:.4g} M⁻¹cm⁻¹" if _finite(br)
                    else "φ 已算出；填上 ε 就能得到亮度")
        lines = [f"A(λex) = {_as_float(res.get('a_ex')):.4f} · "
                 f"I = {_as_float(res.get('i_em')):.4g}",
                 f"{Path(str(a.path)).name} × {Path(str(e.path)).name}"]
        lines += [str(w) for w in (res.get("warnings") or [])]
        card.set_verdict(label=f"荧光量子产率 φ · {name}",
                         value=str(res.get("phi_display") or _fmt_sig3(phi)),
                         headline=headline, lines=lines[:3])
