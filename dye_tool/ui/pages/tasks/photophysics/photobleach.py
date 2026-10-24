# -*- coding: utf-8 -*-
"""子页「光漂白」（FR-43）：时间序列谱或两列 CSV → 单指数拟合 k / t½。"""
from __future__ import annotations

import logging

import math
from pathlib import Path
from PySide6.QtWidgets import QCheckBox
from PySide6.QtWidgets import QComboBox
from PySide6.QtWidgets import QFileDialog
from PySide6.QtWidgets import QGroupBox
from PySide6.QtWidgets import QHBoxLayout
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QLineEdit
from PySide6.QtWidgets import QPushButton
from PySide6.QtWidgets import QVBoxLayout
from PySide6.QtWidgets import QWidget
from core.paths import app_root
from ui.widgets.live_plot import LivePlotCanvas

from ui import theme

from .common import (_BLEACH_UNITS, _as_float, _finite, _load_optional, _empty_char_row,
                     photo_out_path)

log = logging.getLogger("dye_tool.ui.analysis")


class PhotobleachTab(QWidget):
    """光漂白子页：载入时间序列 → 单指数拟合 → 出图 / 加入表征总表。"""

    def __init__(self, panel) -> None:
        super().__init__()
        #: 所属任务卡：任务调度、日志/状态信号、配置与外部数据源都经它取
        self.panel = panel
        self._bleach_spectra: list = []
        self._bleach_t: list = []
        self._bleach_y: list = []
        self._bleach_label = ""
        self._last_bleach: dict = {}
        self._bleach_runs: list = []
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        hint = QLabel("单指数衰减 I(t)=I∞+(I0−I∞)·e^(−k·t)；可关平台项退化为 I0·e^(−k·t)。"
                      "时间统一换算为分钟；k/t½/loss%/R² 复用 core.analysis.photobleach。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("输入模式"))
        self.cmb_bleach_src = QComboBox()
        self.cmb_bleach_src.addItem("① 时间序列谱目录", "series")
        self.cmb_bleach_src.addItem("② 两列 CSV（时间,强度）", "csv")
        src_row.addWidget(self.cmb_bleach_src)
        src_row.addStretch(1)
        lay.addLayout(src_row)

        self.box_series = QGroupBox("时间序列谱目录")
        sl = QHBoxLayout(self.box_series)
        self.btn_bleach_dir = QPushButton("选择谱目录…")
        self.lbl_bleach_dir = QLabel("未选择目录")
        self.cmb_bleach_value = QComboBox()
        self.cmb_bleach_value.addItem("峰值吸光度", "peak_abs")
        self.cmb_bleach_value.addItem("积分强度", "integral")
        sl.addWidget(self.btn_bleach_dir)
        sl.addWidget(self.lbl_bleach_dir, 1)
        sl.addWidget(QLabel("取值"))
        sl.addWidget(self.cmb_bleach_value)
        lay.addWidget(self.box_series)

        self.box_csv = QGroupBox("两列 CSV（时间,强度；外部成像 ROI 导出）")
        cl = QHBoxLayout(self.box_csv)
        self.btn_bleach_csv = QPushButton("选择 CSV…")
        self.lbl_bleach_csv = QLabel("未选择文件")
        cl.addWidget(self.btn_bleach_csv)
        cl.addWidget(self.lbl_bleach_csv, 1)
        lay.addWidget(self.box_csv)

        opt = QHBoxLayout()
        self.chk_plateau = QCheckBox("含平台项 I∞")
        self.chk_plateau.setChecked(True)
        self.chk_norm = QCheckBox("归一化对比")
        self.chk_norm.setChecked(True)
        self.cmb_unit = QComboBox()
        for unit, label in _BLEACH_UNITS:
            self.cmb_unit.addItem(label, unit)
        opt.addWidget(self.chk_plateau)
        opt.addWidget(self.chk_norm)
        opt.addWidget(QLabel("输入时间单位"))
        opt.addWidget(self.cmb_unit)
        opt.addStretch(1)
        lay.addLayout(opt)

        ident = QHBoxLayout()
        self.ed_cond_bleach = QLineEdit()
        self.ed_cond_bleach.setPlaceholderText("条件/溶剂（入表用；缺项留空）")
        self.ed_comp_bleach = QLineEdit()
        self.ed_comp_bleach.setPlaceholderText("化合物/组别（默认取目录/文件名）")
        ident.addWidget(QLabel("条件/溶剂"))
        ident.addWidget(self.ed_cond_bleach, 1)
        ident.addWidget(QLabel("化合物/组别"))
        ident.addWidget(self.ed_comp_bleach, 1)
        lay.addLayout(ident)

        preview = QHBoxLayout()
        self.lbl_bleach_k = QLabel("k = —")
        self.lbl_bleach_thalf = QLabel("t½ = —")
        self.lbl_bleach_loss = QLabel("loss = —")
        self.lbl_bleach_r2 = QLabel("R² = —")
        for lb in (self.lbl_bleach_k, self.lbl_bleach_thalf,
                   self.lbl_bleach_loss, self.lbl_bleach_r2):
            theme.mark(lb, "strong")
            preview.addWidget(lb)
        preview.addStretch(1)
        lay.addLayout(preview)
        self.lbl_bleach_msg = QLabel("请先载入时间序列谱目录或两列 CSV")
        self.lbl_bleach_msg.setWordWrap(True)
        theme.mark(self.lbl_bleach_msg, "muted")
        lay.addWidget(self.lbl_bleach_msg)

        row = QHBoxLayout()
        self.btn_fit_bleach = QPushButton("拟合 / 预览")
        self.btn_add_bleach = QPushButton("加入表征表")
        self.btn_add_bleach.setEnabled(False)
        self.btn_export_decay = QPushButton("导出衰减图…")
        self.btn_export_decay.setToolTip(
            "用 core/render/photophysics 的新图型出口（未落地时提示暂不可用）")
        self.btn_export_evolution = QPushButton("导出光谱演化图…")
        self.btn_export_evolution.setToolTip(
            "时间序列谱 → 光谱演化图（时间序蓝系渐变 + 首末包络 + t=0 高亮）")
        row.addWidget(self.btn_fit_bleach)
        row.addWidget(self.btn_add_bleach)
        row.addWidget(self.btn_export_decay)
        row.addWidget(self.btn_export_evolution)
        row.addStretch(1)
        lay.addLayout(row)

        self.bleach_plot = LivePlotCanvas(figsize=(6.0, 3.0), dpi=90)
        lay.addWidget(self.bleach_plot, 3)

        self.btn_bleach_dir.clicked.connect(self._choose_bleach_dir)
        self.btn_bleach_csv.clicked.connect(self._choose_bleach_csv)
        self.btn_fit_bleach.clicked.connect(lambda *_: self.compute_bleach())
        self.btn_add_bleach.clicked.connect(lambda *_: self.add_bleach_to_table())
        self.btn_export_decay.clicked.connect(lambda *_: self.export_decay_figure())
        self.btn_export_evolution.clicked.connect(
            lambda *_: self.export_evolution_figure())
        self.cmb_bleach_src.currentIndexChanged.connect(self._on_bleach_src_changed)
        self.cmb_bleach_value.currentIndexChanged.connect(
            lambda *_: self._reload_bleach_series())
        self._on_bleach_src_changed()

    def _on_bleach_src_changed(self, *_args) -> None:     # noqa: ANN002 —— Qt 槽
        src = self.cmb_bleach_src.currentData()
        self.box_series.setVisible(src == "series")
        self.box_csv.setVisible(src == "csv")

    def _choose_bleach_dir(self) -> None:
        d = self.panel.pick_dir("选择时间序列谱目录")
        if d:
            self.set_bleach_data_dir(d, force=True)

    def set_bleach_data_dir(self, data_dir, *, force: bool = False) -> None:
        d = Path(data_dir)
        if not force and d == self.panel.data_dir and self._bleach_spectra:
            return
        self.panel.data_dir = d
        self.lbl_bleach_dir.setText(str(d))
        from core.inputs.io import list_scan_files
        try:
            files = list_scan_files(d, self.panel.cfg)
        except Exception as err:                       # noqa: BLE001
            files = []
            log.warning("枚举报漂白目录失败：%s", err)
        if not files:
            self._bleach_spectra = []
            self._bleach_t = []
            self._bleach_y = []
            self.panel.status.emit(f"{d} 下没有可解析的光谱文件")
            return
        from core.inputs.io import load_spectra_files

        def task(progress_cb, log_cb):
            progress_cb(10, "读光谱")
            return load_spectra_files(files, self.panel.cfg)

        self.panel.run_task(task, "载入时间序列", self._on_bleach_loaded,
                      busy_widgets=[self.btn_bleach_dir])

    def _on_bleach_loaded(self, spectra) -> None:
        self._bleach_spectra = list(spectra or [])
        fails = [s for s in self._bleach_spectra
                 if not getattr(s, "ok", True)]
        for s in fails[:20]:
            self.panel.logged.emit("WARNING",
                             f"剔除 {Path(str(getattr(s, 'path', ''))).name}："
                             f"{getattr(s, 'excluded', '') or '读取失败'}")
        n = len(self._bleach_spectra) - len(fails)
        self.lbl_bleach_dir.setText(f"{self.panel.data_dir}（可用 {n} 支）")
        self._reload_bleach_series()

    def _reload_bleach_series(self) -> None:
        if not getattr(self, "_bleach_spectra", None):
            return
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001 —— G-A 未落地
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：光漂白拟合暂不可用")
            return
        mode = self.cmb_bleach_value.currentData() or "peak_abs"
        try:
            t, y = ble.series_from_spectra(self._bleach_spectra, mode=mode)
            self._bleach_t = [float(v) for v in t]
            self._bleach_y = [float(v) for v in y]
        except Exception as err:                       # noqa: BLE001
            self._bleach_t = []
            self._bleach_y = []
            self.lbl_bleach_msg.setText(f"时间序列提取失败：{err}")
            self.panel.logged.emit("WARNING", f"光漂白时间序列提取失败：{err}")
            return
        self._bleach_label = (self.panel.data_dir.name if self.panel.data_dir
                              else "时间序列")
        self.lbl_bleach_msg.setText(
            f"已载入 {len(self._bleach_y)} 个时间点（{mode}）→ 点「拟合 / 预览」")

    def _choose_bleach_csv(self) -> None:
        start = str(self.panel.data_dir or app_root())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择两列 CSV（时间,强度）", start, "CSV 文件 (*.csv *.txt);;所有文件 (*)")
        if path:
            self.load_bleach_csv(Path(path))

    def load_bleach_csv(self, path) -> bool:
        """载入成像 ROI 两列 CSV（时间,强度）；失败给中文原因不崩。"""
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：CSV 导入暂不可用")
            return False
        try:
            t, y = ble.read_two_column_csv(Path(str(path)))
        except ValueError as err:
            self.lbl_bleach_msg.setText(f"CSV 读取失败：{err}")
            self.panel.status.emit(f"CSV 读取失败：{err}")
            return False
        except Exception as err:                       # noqa: BLE001
            self.lbl_bleach_msg.setText(f"CSV 读取失败：{type(err).__name__}: {err}")
            return False
        self._bleach_t = [float(v) for v in t]
        self._bleach_y = [float(v) for v in y]
        self._bleach_label = Path(str(path)).name
        self.lbl_bleach_csv.setText(str(path))
        self.lbl_bleach_msg.setText(
            f"已载入 CSV：{len(self._bleach_y)} 个点 → 点「拟合 / 预览」")
        self.panel.status.emit(f"已载入光漂白 CSV：{Path(str(path)).name}")
        return True

    def compute_bleach(self, checked: bool = False):    # noqa: ANN001 —— Qt 槽兼容
        """单指数拟合并刷新 k/t½/loss%/R² + 衰减曲线（复用 photobleach）。"""
        _ = checked
        if not self._bleach_t:
            self.lbl_bleach_msg.setText("请先载入时间序列谱目录或两列 CSV")
            self.btn_add_bleach.setEnabled(False)
            return {"ok": False, "reason": "没有可拟合的时间序列"}
        try:
            ble = _load_optional("core.analysis.photobleach")
        except Exception:                              # noqa: BLE001
            self.lbl_bleach_msg.setText(
                "核心模块 core.analysis.photobleach 尚未落地：拟合暂不可用")
            return {"ok": False, "reason": "core 未落地"}
        unit = self.cmb_unit.currentData() or "min"
        try:
            res = ble.fit_photobleaching(
                self._bleach_t, self._bleach_y,
                plateau=self.chk_plateau.isChecked(), t_unit=unit)
        except Exception as err:                       # noqa: BLE001
            res = {"ok": False, "reason": f"{type(err).__name__}: {err}"}
        self._last_bleach = dict(res or {})
        self._apply_bleach_result(self._last_bleach)
        return self._last_bleach

    def _bleach_t_minutes(self) -> list:
        unit = self.cmb_unit.currentData() or "min"
        try:
            ble = _load_optional("core.analysis.photobleach")
            return [float(v) for v in ble.time_to_minutes(self._bleach_t, unit)]
        except Exception:                              # noqa: BLE001 —— 退化同值
            return [float(v) for v in self._bleach_t]

    def _apply_bleach_result(self, res: dict) -> None:
        ok = bool((res or {}).get("ok"))
        if not ok:
            for lb, txt in ((self.lbl_bleach_k, "k = —"),
                            (self.lbl_bleach_thalf, "t½ = —"),
                            (self.lbl_bleach_loss, "loss = —"),
                            (self.lbl_bleach_r2, "R² = —")):
                lb.setText(txt)
            self.lbl_bleach_msg.setText(
                "拟合失败：" + str((res or {}).get("reason")
                                   or (res or {}).get("error") or "原因未知"))
            self.btn_add_bleach.setEnabled(False)
            self._plot_bleach(res or {})
            return
        k_min = _as_float(res.get("k_min", res.get("k")))
        t_half = _as_float(res.get("t_half_min", res.get("t_half")))
        loss = _as_float(res.get("loss_pct"))
        r2 = _as_float(res.get("r2"))
        self.lbl_bleach_k.setText(f"k = {k_min:.4g} min⁻¹" if _finite(k_min) else "k = —")
        self.lbl_bleach_thalf.setText(f"t½ = {t_half:.3g} min" if _finite(t_half) else "t½ = —")
        self.lbl_bleach_loss.setText(f"loss = {loss:.1f}%" if _finite(loss) else "loss = —")
        self.lbl_bleach_r2.setText(f"R² = {r2:.4f}" if _finite(r2) else "R² = —")
        issues = [str(i) for i in (res.get("issues") or [])]
        msg = "拟合完成：可「加入表征表」"
        if issues:
            msg += "；" + "；".join(issues)
        self.lbl_bleach_msg.setText(msg)
        self.btn_add_bleach.setEnabled(True)
        # 归一化对比：缓存本次成功结果（同 label 覆盖，最多 6 组）
        try:
            run = {"label": self._bleach_label or "序列",
                   "t": list(self._bleach_t_minutes()),
                   "y": [float(v) for v in self._bleach_y],
                   "res": dict(res)}
            self._bleach_runs = [r for r in self._bleach_runs
                                 if r.get("label") != run["label"]]
            self._bleach_runs.append(run)
            if len(self._bleach_runs) > 6:
                self._bleach_runs = self._bleach_runs[-6:]
        except Exception as err:                       # noqa: BLE001
            log.warning("光漂白对比缓存失败：%s", err)
        self._plot_bleach(res)

    def _plot_bleach(self, res: dict) -> None:
        """衰减散点 + 拟合线（归一化对比可选）；仅显示，不改数值路径。"""
        try:
            t_min = self._bleach_t_minutes()
            y = [float(v) for v in self._bleach_y]
            norm = bool(self.chk_norm.isChecked())
            scale = 1.0
            if norm and y:
                scale = max(y) or 1.0
                y = [v / scale for v in y]
            curves = [{"key": "data", "label": "数据",
                       "x": t_min, "y": y, "color": "#1f4e79"}]
            ok = bool((res or {}).get("ok"))
            if ok:
                k = _as_float(res.get("k_min", res.get("k")))
                i0 = _as_float(res.get("i0"))
                i_inf = _as_float(res.get("i_inf"))
                if _finite(k) and _finite(i0):
                    if not _finite(i_inf):
                        i_inf = 0.0
                    y_fit = []
                    for tv in t_min:
                        try:
                            val = i_inf + (i0 - i_inf) * math.exp(-k * tv)
                        except OverflowError:          # pragma: no cover
                            val = i_inf
                        y_fit.append(val / scale if norm else val)
                    curves.append({"key": "fit", "label": "拟合",
                                   "x": t_min, "y": y_fit, "color": "#C81E1E"})
            if norm:
                for j, run in enumerate(self._bleach_runs[:-1], 1):
                    ry = [float(v) for v in run.get("y") or []]
                    m = max(ry) if ry else 0.0
                    if not m:
                        continue
                    curves.append({"key": f"cmp{j}",
                                   "label": f"对比 {run.get('label')}",
                                   "x": [float(v) for v in run.get("t") or []],
                                   "y": [v / m for v in ry],
                                   "color": "#9ca3af"})
            self.bleach_plot.set_panels([{
                "title": f"光漂白：{self._bleach_label or '序列'}",
                "xlabel": "时间 / min",
                "ylabel": "归一化强度" if norm else "强度",
                "curves": curves}])
            self.bleach_plot.set_style("data", linestyle="None", marker="o",
                                       markersize=4.5)
        except Exception as err:                       # noqa: BLE001 —— 绘图不废页
            log.warning("光漂白曲线绘制失败：%s", err)
            self.lbl_bleach_msg.setText(f"曲线绘制失败（数值结果不受影响）：{err}")

    def add_bleach_to_table(self) -> bool:
        """把当前拟合结果落一行（k_photo / t½）；不自动入表。"""
        res = self._last_bleach or {}
        if not res.get("ok"):
            self.panel.status.emit("请先成功拟合再加入表征表")
            return False
        k_min = _as_float(res.get("k_min", res.get("k")))
        t_half = _as_float(res.get("t_half_min", res.get("t_half")))
        r2 = _as_float(res.get("r2"))
        row = _empty_char_row()
        row.update({
            "condition": self.ed_cond_bleach.text().strip(),
            "compound": self.ed_comp_bleach.text().strip() or (self._bleach_label or ""),
            "k_photo": k_min if _finite(k_min) else float("nan"),
            "t_half": t_half if _finite(t_half) else float("nan"),
            "note": (f"R²={r2:.4f}; n={int(_as_float(res.get('n_points'), 0) or 0)}"
                     if _finite(r2) else ""),
        })
        self.panel.add_row(row)
        self.panel.tabs.setCurrentWidget(self.panel.tab_char)
        return True

    def export_decay_figure(self):
        """FR-45 衰减曲线新图型出口（core/render/photophysics.plot_decay_curve）。"""
        if not self._bleach_t:
            self.panel.status.emit("请先载入并拟合光漂白数据，再导出衰减图")
            return None
        try:
            mod = _load_optional("core.render.photophysics")
            fn = getattr(mod, "plot_decay_curve")
        except Exception:                              # noqa: BLE001 —— G-C 未落地
            msg = ("core/render/photophysics 尚未落地：衰减图导出暂不可用"
                   "（数值结果不受影响）")
            self.panel.status.emit(msg)
            self.panel.logged.emit("WARNING", msg)
            return None
        res = dict(self._last_bleach or {})
        plateau = bool(self.chk_plateau.isChecked())
        fit_spec = {
            "model": "exp_plateau" if plateau else "exp",
            "k": _as_float(res.get("k_min", res.get("k"))),
            "t_half": _as_float(res.get("t_half_min", res.get("t_half"))),
            "r2": _as_float(res.get("r2")),
            "I0": _as_float(res.get("i0")),
            "I_inf": _as_float(res.get("i_inf")) if plateau else 0.0,
        } if (res.get("ok") and _finite(res.get("k_min", res.get("k")))) else None
        if fit_spec is None:
            self.panel.status.emit("还没有成功的拟合结果：请先「拟合 / 预览」再导出衰减图")
            return None
        out_path = photo_out_path(self.panel.cfg, "decay")
        try:
            got = fn(self._bleach_t_minutes(),
                     [float(v) for v in self._bleach_y],
                     fit=fit_spec, out_path=out_path, cfg=self.panel.cfg)
        except TypeError:
            try:
                got = fn(self._bleach_t_minutes(),
                         [float(v) for v in self._bleach_y],
                         fit=fit_spec, out_path=out_path)
            except Exception as err:                   # noqa: BLE001
                self.panel.status.emit(f"衰减图导出失败：{err}")
                self.panel.logged.emit("ERROR", f"衰减图导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.panel.status.emit(f"衰减图导出失败：{err}")
            self.panel.logged.emit("ERROR", f"衰减图导出失败：{err}")
            return None
        if got:
            self.panel.status.emit(f"已导出衰减图：{got}")
            self.panel.logged.emit("INFO", f"光物理表征：衰减图已导出 {got}")
        return got

    def export_evolution_figure(self):
        """FR-45 光谱演化图出口（core/render/photophysics.plot_spectral_evolution）。"""
        spectra = [s for s in (self._bleach_spectra or [])
                   if getattr(s, "ok", True)]
        if not spectra:
            self.panel.status.emit("请先在「光漂白」子页载入时间序列谱目录，再导出光谱演化图")
            return None
        try:
            mod = _load_optional("core.render.photophysics")
            fn = getattr(mod, "plot_spectral_evolution")
        except Exception:                              # noqa: BLE001 —— G-C 未落地
            msg = ("core/render/photophysics 尚未落地：光谱演化图导出暂不可用")
            self.panel.status.emit(msg)
            self.panel.logged.emit("WARNING", msg)
            return None
        from core.inputs.io import parse_filename
        fn_cfg = (self.panel.cfg or {}).get("filename") or {}
        frames = []
        for i, s in enumerate(spectra):
            try:
                t_val = float(parse_filename(
                    Path(str(getattr(s, "path", ""))).stem, fn_cfg).get("t") or 0.0)
            except Exception:                          # noqa: BLE001 —— 无时间后缀退序号
                t_val = float(i)
            frames.append({"x": getattr(s, "wl", None),
                           "y": getattr(s, "y", None),
                           "t": t_val,
                           "label": Path(str(getattr(s, "path", ""))).name})
        out_path = photo_out_path(self.panel.cfg, "evolution")
        try:
            got = fn(frames, out_path, cfg=self.panel.cfg)
        except TypeError:
            try:
                got = fn(frames, out_path)
            except Exception as err:                   # noqa: BLE001
                self.panel.status.emit(f"光谱演化图导出失败：{err}")
                self.panel.logged.emit("ERROR", f"光谱演化图导出失败：{err}")
                return None
        except Exception as err:                       # noqa: BLE001
            self.panel.status.emit(f"光谱演化图导出失败：{err}")
            self.panel.logged.emit("ERROR", f"光谱演化图导出失败：{err}")
            return None
        if got:
            self.panel.status.emit(f"已导出光谱演化图：{got}")
            self.panel.logged.emit("INFO", f"光物理表征：光谱演化图已导出 {got}")
        return got
