# -*- coding: utf-8 -*-
"""子页「量子产率 φ」（FR-42）：相对法荧光量子产率 φ 与亮度 = ε×φ。"""
from __future__ import annotations

import logging

from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView
from PySide6.QtWidgets import QFormLayout
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QListWidget
from PySide6.QtWidgets import QListWidgetItem
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QSplitter
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.report.stokes_table import group_from_path

from core.report.stokes_table import row_from_spectra
from ui import theme

from .common import (_as_float, _finite, _fmt_sig3, _load_optional, _spin,
                     _empty_char_row)

log = logging.getLogger("dye_tool.ui.analysis")


class QuantumYieldTab(QWidget):
    """量子产率 φ 子页：选吸收/发射谱配对 → 相对法 φ → 加入表征总表。"""

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
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        self.btn_phi_dir = QPushButton("选择光谱目录…")
        self.lbl_phi_dir = QLabel("未选择目录")
        theme.mark(self.lbl_phi_dir, "muted")
        bar.addWidget(self.btn_phi_dir)
        bar.addWidget(self.lbl_phi_dir, 1)
        lay.addLayout(bar)
        hint = QLabel("相对法：φ = φ_ref × (I/I_ref) × (A_ref/A) × (n²/n_ref²)；"
                      "I = 发射谱积分强度，A = λex 处吸光度（建议 A ≤ 0.05）。"
                      "数值全部复用 core.analysis.fluorescence_qy，不重写公式。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        split = QSplitter(Qt.Horizontal)
        self.lst_phi_abs = QListWidget()
        self.lst_phi_em = QListWidget()
        for lst, title in ((self.lst_phi_abs, "吸收谱（kind=abs）"),
                           (self.lst_phi_em, "发射谱（kind=em）")):
            lst.setSelectionMode(QAbstractItemView.SingleSelection)
            lst.setAlternatingRowColors(True)
            box = QGroupBox(title)
            vb = QVBoxLayout(box)
            vb.addWidget(lst)
            split.addWidget(box)
        split.setSizes([380, 380])
        lay.addWidget(split, 2)

        box = QGroupBox("测量条件与参比")
        form = QFormLayout(box)
        self.sp_lam_ex = _spin(200.0, 1200.0, 450.0, 1.0, 1, " nm")
        self.sp_phi_ref = _spin(0.0, 5.0, 0.54, 0.01, 4)
        self.sp_i_ref = _spin(0.0, 1.0e12, 1.0, 0.1, 6)
        self.sp_a_ref = _spin(0.0, 10.0, 0.05, 0.01, 4)
        self.sp_n_ref = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_n = _spin(1.0, 2.0, 1.36, 0.01, 3)
        self.sp_eps = _spin(0.0, 1.0e12, 0.0, 1000.0, 3)
        self.btn_eps_from = QPushButton("从 ε 结果填入")
        self.btn_eps_from.setToolTip("从②分析页「摩尔消光系数 ε」卡最近一次成功结果取 ε")
        self.lbl_eps_source = QLabel("ε 来源：手填")
        theme.mark(self.lbl_eps_source, "hint")
        eps_row = QHBoxLayout()
        eps_row.addWidget(self.sp_eps, 1)
        eps_row.addWidget(self.btn_eps_from)
        eps_row.addWidget(self.lbl_eps_source)
        eps_wrap = QWidget()
        eps_wrap.setLayout(eps_row)
        self.ed_cond_phi = QLineEdit()
        self.ed_cond_phi.setPlaceholderText("如 PBS / MeOH（缺项留空）")
        self.ed_comp_phi = QLineEdit()
        self.ed_comp_phi.setPlaceholderText("默认取吸收文件名组别；可手改")
        form.addRow("激发波长 λex", self.sp_lam_ex)
        self.lbl_lam_note = QLabel(
            "λex 只需落在吸收谱范围；发射积分取发射谱自身范围"
            "（仪器 EM 扫描可从 λex 之后开始，V2.6 修正）")
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
        form.addRow("ε (M⁻¹cm⁻¹)", eps_wrap)
        form.addRow("条件/溶剂", self.ed_cond_phi)
        form.addRow("化合物/组别", self.ed_comp_phi)
        lay.addWidget(box)

        preview = QHBoxLayout()
        self.lbl_phi_a = QLabel("A(λex) = —")
        self.lbl_phi_i = QLabel("I = —")
        self.lbl_phi = QLabel("φ = —")
        self.lbl_brightness = QLabel("亮度 = —")
        for lb in (self.lbl_phi_a, self.lbl_phi_i, self.lbl_phi, self.lbl_brightness):
            theme.mark(lb, "strong")
            preview.addWidget(lb)
        preview.addStretch(1)
        lay.addLayout(preview)
        self.lbl_phi_warn = QLabel("")
        self.lbl_phi_warn.setWordWrap(True)
        theme.mark(self.lbl_phi_warn, "warnHint")
        lay.addWidget(self.lbl_phi_warn)
        self.lbl_phi_msg = QLabel("未选择配对：请在左右列表各选一支谱")
        self.lbl_phi_msg.setWordWrap(True)
        theme.mark(self.lbl_phi_msg, "muted")
        lay.addWidget(self.lbl_phi_msg)
        row = QHBoxLayout()
        self.btn_compute_phi = QPushButton("计算 φ")
        self.btn_add_phi = QPushButton("加入表征表")
        self.btn_add_phi.setEnabled(False)
        row.addWidget(self.btn_compute_phi)
        row.addWidget(self.btn_add_phi)
        row.addStretch(1)
        lay.addLayout(row)

        self.btn_phi_dir.clicked.connect(self._choose_phi_dir)
        self.btn_compute_phi.clicked.connect(lambda *_: self.compute_phi())
        self.btn_add_phi.clicked.connect(lambda *_: self.add_phi_to_table())
        self.btn_eps_from.clicked.connect(lambda *_: self._on_eps_from_result())
        for lst in (self.lst_phi_abs, self.lst_phi_em):
            lst.currentRowChanged.connect(lambda *_: self._phi_preview())
        for sp in (self.sp_lam_ex, self.sp_phi_ref, self.sp_i_ref,
                   self.sp_a_ref, self.sp_n_ref, self.sp_n, self.sp_eps):
            sp.valueChanged.connect(lambda *_: self._phi_preview())

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
        """即时刷新 A/I/φ/亮度预览（计算口径全部来自 fluorescence_qy）。"""
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
            qy = _load_optional("core.analysis.fluorescence_qy")
        except Exception:                              # noqa: BLE001 —— G-A 未落地
            self.lbl_phi_msg.setText(
                "核心模块 core.analysis.fluorescence_qy 尚未落地：φ 暂不可算"
                "（候选与参数已就绪，落地后自动可用）")
            self.lbl_phi_warn.setText("")
            return {}
        try:
            res = qy.compute_fluorescence_qy(
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
        eps = float(self.sp_eps.value())
        br = float("nan")
        if eps > 0 and _finite(phi):
            try:
                qy = _load_optional("core.analysis.fluorescence_qy")
                br = float(qy.brightness(eps, phi))
            except Exception:                          # noqa: BLE001 —— 退化为同义乘法
                br = eps * float(phi)
        self.lbl_brightness.setText(
            f"亮度 = {br:.4g} M⁻¹cm⁻¹" if _finite(br) else "亮度 = —（请填 ε）")
        warnings = [str(w) for w in (res.get("warnings") or [])]
        self.lbl_phi_warn.setText("；".join(warnings))
        self.lbl_phi_msg.setText("可「加入表征表」（不自动入表；ε 来源见上方）")
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
        br = float("nan")
        if eps > 0 and _finite(phi):
            try:
                br = float(_load_optional(
                    "core.analysis.fluorescence_qy").brightness(eps, phi))
            except Exception:                          # noqa: BLE001
                br = eps * float(phi)
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
        self.panel.tabs.setCurrentWidget(self.panel.tab_char)
        return True
