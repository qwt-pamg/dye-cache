# -*- coding: utf-8 -*-
"""功能③ 光谱堆叠/叠加图（FR-3）。

渲染风格由 ``config.yaml`` 的 ``plot.style`` 决定（默认 ``jacs`` 单栏 82.5 mm）：

1. JACS 堆叠图（每组一张）：无 colorbar，改用向上的趋势箭头 + 图注说明时间范围，
   灰色细虚线标 379 nm，标注全部水平；
2. 时间演化叠加图：单色顺序色带（浅→深 = 早→晚）+ 趋势箭头（方向按探针吸光度
   随时间的变化判定），灰细虚线标探针峰并水平标 "379"，峰侧水平标 "Resorufin"；
   532 nm 默认不画（写进 ``figures/captions.md``）；支持只画选定时间点。

``plot.style='legacy'`` 时回退到迁移前版式（viridis + colorbar + 竖排标注），
用于与 legacy 脚本做逐像素回归比对。
"""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox,
                               QHeaderView, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout, QWidget)

from core.io import collect_scans
from core.pipeline import run_stack
from ui.pages.workbench import WorkbenchPage
from ui.widgets.file_table import (COL_CHECK, COL_DYE, COL_FILE, COL_GROUP, COL_REASON,
                                   COL_ROLE, COL_STATUS, COL_TIME, FileTable)
from ui.widgets.live_plot import label_text
from ui.widgets.style_panel import MAPPED_KEYS, StylePanel
from ui import theme

#: 左栏文件表最小高度
PARAM_TABLE_MIN_H = 140


class StackPage(WorkbenchPage):
    """光谱堆叠/叠加图页。"""

    TITLE = "③ 光谱堆叠图 / 时间演化叠加图"
    SUBTITLE = ("每组一张 JACS 堆叠图，另出时间演化叠加图（按辐照时间着色，"
                "标注 ABDA 探针峰、激发波长与荧光团最大吸收）。"
                "工作视图可直接交互；定稿图用「生成定稿图」。")
    PRESET_SECTION = "stack"
    PAGE_MARK = "③"
    LEFT_W = 330

    def __init__(self, cfg: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.scans: list = []
        self._build()
        self.setAcceptDrops(True)

    def _build(self) -> None:
        self.btn_dir = QPushButton("选择数据目录…")
        self.lbl_dir = QLabel("未选择目录")
        theme.mark(self.lbl_dir, "muted")
        self.lbl_dir.setWordWrap(True)
        self.btn_dir.clicked.connect(self._choose_dir)

        # ── 数据选项（「载入」步）──
        self.cmb_group = QComboBox()
        self.cmb_group.addItem("全部组")
        self.chk_jacs = QCheckBox("JACS 堆叠图（每组一张）")
        self.chk_jacs.setChecked(True)
        self.chk_overlay = QCheckBox("时间演化叠加图")
        self.chk_overlay.setChecked(True)
        self.cmb_times = QComboBox()
        self.cmb_times.addItems(["全部时间点", "自定义列表"])
        self.ed_times = QLineEdit()
        self.ed_times.setPlaceholderText("如 1,60,300,600（秒）")
        self.ed_times.setEnabled(False)
        self.cmb_times.currentIndexChanged.connect(
            lambda i: self.ed_times.setEnabled(i == 1))
        # 用户需求：作图前预处理——与 Φ_Δ 流水线同一 despike 判据；
        # 只影响本次作图/导出，不改原始数据文件，也不是数值分析的一部分。
        self.chk_despike = QCheckBox("去尖峰（作图前预处理，与 Φ_Δ 同判据）")
        self.chk_despike.setChecked(True)
        self.chk_despike.setToolTip(
            "作图前按 Φ_Δ 的同一判据（滚动中位数 ± k×MAD）剔除单点异常；\n"
            "只影响本页图件，不修改原始数据文件，也不参与数值分析。\n"
            "取消勾选 = 按原始数据作图（旧行为）。")
        box = QGroupBox("画什么")
        f = QFormLayout(box)
        f.addRow("绘制组", self.cmb_group)
        f.addRow(self.chk_jacs)
        f.addRow(self.chk_overlay)
        f.addRow("叠加图时间点", self.cmb_times)
        f.addRow(self.ed_times)
        f.addRow(self.chk_despike)

        # ── 色带与标注（「样式」步顶部）──
        self.ed_cmap = QLineEdit("viridis")
        self.chk_probe = QCheckBox("标注探针峰")
        self.chk_probe.setChecked(True)
        self.chk_exc = QCheckBox("标注激发波长")
        self.chk_exc.setChecked(True)
        self.chk_fluor = QCheckBox("标注荧光团最大吸收")
        self.chk_fluor.setChecked(True)
        box_marks = QGroupBox("色带与标注")
        fm = QFormLayout(box_marks)
        fm.addRow("色带 (colormap)", self.ed_cmap)
        fm.addRow(self.chk_probe)
        fm.addRow(self.chk_exc)
        fm.addRow(self.chk_fluor)

        p_load = QWidget()
        lp = QVBoxLayout(p_load)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.addWidget(self.btn_dir)
        lp.addWidget(self.lbl_dir)
        hint = QLabel("也可以把数据目录直接拖进来；在①数据页载入的批次切到这里会自动带上。")
        theme.mark(hint, "hint")
        hint.setWordWrap(True)
        lp.addWidget(hint)
        lp.addWidget(box)
        lp.addStretch(1)

        # ── 左栏：文件清单（仅用于确认分组与剔除；时间列可改）──
        self.files = FileTable()
        self.files.table.setMinimumHeight(PARAM_TABLE_MIN_H)
        tbl = self.files.table
        for col in (COL_DYE, COL_ROLE, COL_REASON):      # 窄栏只留关键列；剔除原因在悬停提示里
            tbl.setColumnHidden(col, True)
        tbl.setTextElideMode(Qt.ElideMiddle)
        hdr = tbl.horizontalHeader()
        for col in (COL_CHECK, COL_TIME, COL_STATUS):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_GROUP, QHeaderView.Interactive)   # 组名很长：给定宽、可拖
        hdr.resizeSection(COL_GROUP, 96)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(4)
        lbl = QLabel("文件")
        theme.mark(lbl, "section")
        ll.addWidget(lbl)
        ll.addWidget(self.files, 1)

        self._build_shared()
        self.btn_run.setToolTip(
            "定稿图由分析流水线生成（run figures/，JACS 版式）。\n"
            "载入/切换不再自动生图；工作视图与定稿图分离。")
        self.chk_live.setToolTip("调整出图参数时用最终渲染器低 dpi 预览；不写正式 run")
        # V2.9.5 FR-73：出图选项变化 → 防抖最终渲染器预览
        for _w in (self.chk_jacs, self.chk_overlay, self.chk_despike):
            _w.toggled.connect(lambda *_: self._schedule_preview())
        self.cmb_group.currentIndexChanged.connect(lambda *_: self._schedule_preview())
        self._assemble(left=left, load_page=p_load, style_top=box_marks)

        self._sync_from_cfg()
        self._update_flow()                 # 色带刚按 cfg 刷新：摘要跟着变
        # V25-B3：载入前先用 cfg 播种「可映射键」，避免未编辑就写入默认值
        self._seed_style_panel()
        # V2.5 FR-32：选项变化 → 活画布即时重绘（不触发渲染器）
        self.cmb_group.currentIndexChanged.connect(
            lambda *_: self._rebuild_working_view())
        for w in (self.chk_jacs, self.chk_overlay, self.chk_despike,
                  self.chk_probe, self.chk_exc, self.chk_fluor):
            w.toggled.connect(lambda *_: self._rebuild_working_view())
        self.ed_cmap.editingFinished.connect(self._rebuild_working_view)
        self.cmb_times.currentIndexChanged.connect(
            lambda *_: self._rebuild_working_view())

    # ══════════════ 流程条摘要 / 结论条 ══════════════
    def _has_data(self) -> bool:
        return bool(self._selected_groups())

    def _plot_rows(self) -> list:
        """当前会画的谱（选中组、未剔除、非背景/暗对照）。"""
        groups = set(self._selected_groups())
        return [s for s in (self.scans or []) if not s.excluded
                and s.role not in ("background", "dark") and s.group in groups]

    def _load_summary(self) -> str:
        if not self.data_dir:
            return "还没选数据目录"
        return f"{self.data_dir.name} · {self.cmb_group.currentText()}"

    def _style_summary(self) -> str:
        preset = self.cmb_display_preset.currentText()
        tail = "" if self.cmb_display_preset.currentIndex() <= 0 else f" · 预设 {preset}"
        return f"色带 {self.ed_cmap.text().strip() or '默认'}{tail}"

    def _update_verdict(self) -> None:
        rows = self._plot_rows()
        if not rows:
            if self.scans:
                self.verdict.set_failed("没有可画的样品光谱",
                                        "背景 / 暗对照不计入出图；检查左栏文件的剔除原因")
            else:
                self._verdict_empty()
            return
        groups = sorted({s.group for s in rows})
        ts = [float(s.t) for s in rows]
        kinds = [name for on, name in ((self.chk_jacs.isChecked(), "JACS 堆叠图"),
                                       (self.chk_overlay.isChecked(), "时间演化叠加图")) if on]
        lines = [f"工作视图：{' + '.join(kinds) if kinds else '未勾选图类型'}"
                 + (" · 已去尖峰" if self.chk_despike.isChecked() else "")]
        final = self._verdict_final_line()
        if final:
            lines.append(final)
        self.verdict.set_verdict(
            label=self.data_dir.name if self.data_dir else "当前批次",
            value=f"{len(rows)} 条谱",
            headline=(f"{len(groups)} 组 · {min(ts):g}–{max(ts):g} s · "
                      f"按时间着色（{self.ed_cmap.text().strip() or '默认色带'}）"),
            lines=lines)

    # ══════════════ 配置同步 ══════════════
    def _style_section(self) -> tuple[dict, bool]:
        """返回 ``(当前风格对应的出图参数节, 是否 JACS)``。

        JACS 的色带/标注开关在 ``plot.jacs.*``，legacy 的在
        ``plot.spectra_overlay.*``；写错地方会导致控件"看起来能改、实际无效"。
        """
        plot = (self.cfg or {}).get("plot") or {}
        jacs = str(plot.get("style", "jacs")) != "legacy"
        sec = (plot.get("jacs") or {}) if jacs else (plot.get("spectra_overlay") or {})
        return sec, jacs

    def _sync_from_cfg(self) -> None:
        """按当前 plot.style 把色带/标注开关刷成对应风格的值。"""
        sec, jacs = self._style_section()
        self.ed_cmap.setText(str(sec.get("cmap", "Blues" if jacs else "viridis")))
        self.chk_probe.setChecked(bool(sec.get("mark_probe", True)))
        self.chk_exc.setChecked(bool(sec.get("mark_excitation", not jacs)))
        mf = sec.get("mark_fluorophore", True if jacs else "auto")
        self.chk_fluor.setChecked(bool(mf))

    # ══════════════ V2.8 FR-53：③ 显示预设（presets.yaml stack 节；config.yaml 零新键） ══════════════
    def display_preset_values(self) -> dict:
        """当前 ③堆叠图显示设置（差量 dict；与 presets.yaml stack 节同形）。"""
        return {
            "jacs": bool(self.chk_jacs.isChecked()),
            "overlay": bool(self.chk_overlay.isChecked()),
            "despike": bool(self.chk_despike.isChecked()),
            "cmap": str(self.ed_cmap.text()).strip(),
            "mark_probe": bool(self.chk_probe.isChecked()),
            "mark_excitation": bool(self.chk_exc.isChecked()),
            "mark_fluorophore": bool(self.chk_fluor.isChecked()),
        }

    def apply_display_preset(self, values: dict) -> None:
        """套用 ③ 显示预设（未知键忽略；不写 config.yaml）。"""
        v = dict(values or {})
        for key, widget in (("jacs", self.chk_jacs),
                            ("overlay", self.chk_overlay),
                            ("despike", self.chk_despike),
                            ("mark_probe", self.chk_probe),
                            ("mark_excitation", self.chk_exc),
                            ("mark_fluorophore", self.chk_fluor)):
            if key in v:
                widget.setChecked(bool(v[key]))
        if "cmap" in v and str(v["cmap"]).strip():
            self.ed_cmap.setText(str(v["cmap"]).strip())
        if self.scans:
            self._rebuild_working_view()

    def load_cfg(self) -> dict:
        cfg = super().load_cfg()
        self._sync_from_cfg()
        return cfg

    # ══════════════ 载入 ══════════════

    def on_dir_dropped(self, d: Path) -> None:
        self._load_dir(d)

    def _load_dir(self, d: Optional[Path],
                  recursive: Optional[bool] = None,
                  overrides: Optional[dict] = None) -> None:
        """载入目录。A19（最小修复）：继承 ①数据页的扫描口径与整定。

        * ``recursive``：原来恒用 ``self.cfg["io"]["recursive"]``（默认 True）→
          用户在数据页关掉"含子目录"后，作图台仍递归；
        * ``overrides``：``{dye_of, times}``（:meth:`DataPage.session_overrides`），
          在 ``_on_loaded`` 里应用到 scans / 染料名——作图台与①数据同一口径。
        """
        if not d:
            return
        self.data_dir = Path(d)
        self.lbl_dir.setText(f"数据目录：{self.data_dir.name}")
        self.lbl_dir.setToolTip(str(self.data_dir))
        self.remember_dir(self.data_dir)
        self._load_overrides = dict(overrides or {})
        cfg = dict(self.cfg)
        io = dict(cfg.get("io") or {})
        if recursive is not None:
            io["recursive"] = bool(recursive)
        cfg["io"] = io

        def task(progress_cb, log_cb):
            return collect_scans(self.data_dir, cfg, progress_cb=progress_cb)

        self.run_task(task, "载入文件清单", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.btn_run, self.files])

    def _on_loaded(self, scans: list) -> None:
        # A19：把①数据页的整定应用到本页（时间覆盖 + 染料会话名）
        scans = list(scans)
        ov = getattr(self, "_load_overrides", None) or {}
        times = ov.get("times") or {}
        for s in scans:
            name = getattr(getattr(s, "path", None), "name", "")
            if name in times:
                if getattr(s, "t_src", None) is None:
                    s.t_src = float(s.t)
                s.t = float(times[name])
        self._dye_of = dict(ov.get("dye_of") or {})
        self._load_overrides = {}
        self.scans = scans
        self._final_figs, self._final_stale = [], False      # 新数据：旧定稿图不再对应
        # FR-33.4：重载数据 → per-curve 会话覆写失效（不残留到新一轮数据）
        if hasattr(self, "live"):
            self.live.clear_styles()
        self.files.set_scans(scans)
        groups = sorted({s.group for s in scans if not s.excluded
                         and s.role not in ("background", "dark")})
        self.cmb_group.clear()
        self.cmb_group.addItem("全部组")
        self.cmb_group.addItems(groups)
        self.btn_run.setEnabled(bool(groups))
        if not groups:
            self.logged.emit("ERROR", "没有可用的样品光谱（背景/暗对照不计入出图）")
        self._rebuild_working_view()
        if groups and hasattr(self, "live_preview"):
            self._schedule_preview()

    # ══════════════ V2.5 FR-32/34：工作视图（活画布） ══════════════

    def _working_scans(self) -> list:
        """工作视图数据 = 页面 despiked 数组（"作图前去尖峰"复选框同一口径）。"""
        scans = list(self.scans or [])
        if self.chk_despike.isChecked():
            qc_cfg = dict((self.cfg or {}).get("qc") or {})
            scans = self._despike_scans(scans, qc_cfg)
        return scans

    def _selected_groups(self) -> list:
        sel = self.cmb_group.currentText()
        groups = sorted({s.group for s in (self.scans or []) if not s.excluded
                         and s.role not in ("background", "dark")})
        return groups if sel in ("", "全部组") else [g for g in groups if g == sel]

    def _working_cmap(self) -> str:
        sec, jacs = self._style_section()
        cmap = str(self.ed_cmap.text().strip() or sec.get("cmap")
                   or ("Blues" if jacs else "viridis"))
        from matplotlib import colormaps
        if cmap not in colormaps:
            fallback = "Blues" if jacs else "viridis"
            self.logged.emit("WARNING", f"色带 {cmap!r} 不合法，工作视图暂用 "
                                        f"{fallback}（不会写入 cfg）")
            return fallback
        return cmap

    def _rebuild_working_view(self) -> None:
        """从**页面 despiked 数组**直接绘制工作视图（不调用渲染器）。"""
        if not hasattr(self, "live"):
            return
        scans = self._working_scans()
        if not scans:
            self.live.clear()
            self._after_working_view()
            return
        from core.render.plotstyle import cmap_colors
        groups = self._selected_groups()
        cmap = self._working_cmap()
        panels: list[dict] = []
        probe = float(((self.cfg or {}).get("wavelength") or {}).get("probe", 379.0))
        # 时间演化（叠加）：每个组一条曲线（x=t，y=探针波长处最近点吸光度）
        if self.chk_overlay.isChecked():
            curves: list[dict] = []
            for g in groups:
                pts = []
                for s in scans:
                    if s.excluded or s.role in ("background", "dark") or s.group != g:
                        continue
                    try:
                        wl = np.asarray(s.wl, float)
                        a = np.asarray(s.a, float)
                        if not wl.size or wl.size != a.size:
                            continue
                        j = int(np.abs(wl - probe).argmin())
                        pts.append((float(s.t), float(a[j])))
                    except Exception:                   # noqa: BLE001
                        continue
                if pts:
                    pts.sort()
                    curves.append({"key": f"overlay|{g}", "label": g,
                                   "x": [p[0] for p in pts],
                                   "y": [p[1] for p in pts]})
            if curves:
                panels.append({"title": label_text(
                                   f"时间演化（探针 {probe:g} nm）",
                                   f"Time evolution (probe {probe:g} nm)"),
                               "xlabel": "t / s", "ylabel": "A(t)",
                               "curves": curves})
        # 堆叠：每组一个 panel，每条谱一条曲线（着色按时间顺序）
        if self.chk_jacs.isChecked():
            for g in groups:
                rows = [s for s in scans
                        if not s.excluded and s.role not in ("background", "dark")
                        and s.group == g]
                rows.sort(key=lambda s: float(s.t))
                if not rows:
                    continue
                lo, hi = self._working_cmap_range()
                colors = cmap_colors(cmap, lo, hi, max(1, len(rows)))
                curves = [{"key": f"{g}|{s.path.name}",
                           "label": f"t={float(s.t):g}s",
                           "x": np.asarray(s.wl, float).ravel(),
                           "y": np.asarray(s.a, float).ravel(),
                           "color": colors[i % len(colors)]}
                          for i, s in enumerate(rows)]
                panels.append({"title": label_text(
                                   f"组 {g} ・ {len(rows)} 条谱",
                                   f"Group {g} · {len(rows)} spectra"),
                               "xlabel": label_text("波长 / nm",
                                                    "Wavelength / nm"),
                               "ylabel": "A", "curves": curves})
        self.live.set_panels(
            panels, suptitle=label_text("工作视图",
                                        "Working view"))
        self._sync_style_panel()
        self.cmb_view.setCurrentIndex(0)
        self.view_stack.setCurrentIndex(0)
        self._after_working_view()

    # ── V2.5 FR-33：风格栏桥接（实时=重绘；写入/预设=显式动作） ──

    def _apply_loaded_style(self, vals: dict) -> None:
        if vals.get("cmap"):
            self.ed_cmap.setText(str(vals["cmap"]))

    def _seed_style_panel(self) -> None:
        """用 cfg 的 plot.jacs 播种可映射键（V25-B3）。

        未编辑就点「写入出图参数」不再把 7 个键覆盖成控件默认值。
        """
        if not hasattr(self, "style_panel"):
            return
        jacs = ((self.cfg or {}).get("plot") or {}).get("jacs") or {}
        values = {k: jacs[k] for k in MAPPED_KEYS if k in jacs}
        if not values:
            return
        if self.style_panel.seed_mapped_values(values):
            self._mapped_preview_values = dict(values)

    def _on_write_plot_params(self, values: dict) -> None:
        """FR-33.3：显式写入 cfg + 保存 → 冒泡给主窗口广播/Φ_Δ 横幅。

        V25-B3：写入前校验（cmap_lo<cmap_hi、颜色可解析、数值范围），只写白名单
        键；校验失败 → 状态栏/日志说明，**不写 cfg、不 persist**。
        """
        values = {k: v for k, v in dict(values or {}).items()
                  if k in MAPPED_KEYS}
        ok, msg = StylePanel.validate_values(values)
        if not ok:
            self.logged.emit("ERROR", f"写入出图参数被拒绝：{msg}")
            self.status.emit(f"出图参数未写入：{msg}")
            return
        try:
            self._jacs_section().update(values)
            self.persist_cfg()
            self.plot_params_written.emit(dict(values))
            self.logged.emit(
                "INFO", f"出图参数已写入 cfg（可映射键 {len(values)} 个）"
                        " → Φ_Δ 页提示重渲受影响图")
            self.status.emit("出图参数已写入并广播")
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("ERROR", f"写入出图参数失败：{err}")

    # ══════════════ V2.9.5 FR-73：实时生图预览 ══════════════
    def _schedule_preview(self) -> None:
        try:
            self._preview_cfg = self.params_cfg()
            self._preview_opts = dict(
                only_group=(None if self.cmb_group.currentText() == "全部组"
                            else self.cmb_group.currentText()),
                do_overlay=self.chk_overlay.isChecked(),
                do_jacs=self.chk_jacs.isChecked(),
                do_despike=self.chk_despike.isChecked())
        except Exception:
            return
        self.live_preview.schedule()

    def _render_live_preview(self, out_dir) -> list:
        cfg = copy.deepcopy(getattr(self, "_preview_cfg", self.params_cfg()))
        mapped = dict(getattr(self, "_mapped_preview_values", {}) or {})
        jacs = cfg.setdefault("plot", {}).setdefault("jacs", {})
        jacs.update({k: v for k, v in mapped.items() if k in MAPPED_KEYS})
        opts = dict(getattr(self, "_preview_opts", {}))
        cfg.setdefault("plot", {}).setdefault("jacs", {})["dpi"] = 100
        cfg["plot"]["jacs"]["formats"] = ["png"]
        scans = self.scans or []
        if opts.get("do_despike"):
            scans = self._despike_scans(scans, dict(cfg.get("qc") or {}))
        res = run_stack(scans, cfg, out_dir=out_dir,
                        only_group=opts.get("only_group"),
                        do_overlay=bool(opts.get("do_overlay", True)),
                        do_jacs=bool(opts.get("do_jacs", True)))
        return list(getattr(res, "files", []) or [])

    # ══════════════ 出图 ══════════════
    def _run(self) -> None:
        if not self.scans:
            return
        cfg = self.params_cfg()
        only = self.cmb_group.currentText()
        only = None if only == "全部组" else only
        # G13（第二轮审查）：两个 QCheckBox 的状态**在 UI 线程先取好**再进 task。
        # 原先写在 `task` 体内直接 `self.chk_*.isChecked()`——那是工作线程访问
        # widget，违反 Qt 线程亲和（同 phi_page 的 dye_map 一处）。
        do_overlay = self.chk_overlay.isChecked()
        do_jacs = self.chk_jacs.isChecked()
        do_despike = self.chk_despike.isChecked()
        qc_cfg = dict(cfg.get("qc") or {})

        def task(progress_cb, log_cb):
            scans = self.scans
            if do_despike:
                scans = self._despike_scans(scans, qc_cfg)
            return run_stack(scans, cfg, only_group=only,
                             do_overlay=do_overlay,
                             do_jacs=do_jacs,
                             progress_cb=progress_cb, log_cb=log_cb)

        self.run_task(task, "生成定稿图", self._on_run_done,
                      busy_widgets=self.generation_busy_widgets())
        # V25-B5：任务结束（成功/失败/取消）后恢复依赖关系控件状态
        if getattr(self, "job", None) is not None:
            self.job.finished.connect(self._restore_generation_lock_state)

    def generation_busy_widgets(self) -> list:
        """生图期间要锁定的控件清单（V25-B5；验收/测试可直接断言内容）。"""
        return [self.btn_run, self.btn_dir, self.files,
                self.live, self.cmb_view, self.cmb_group,
                self.chk_jacs, self.chk_overlay, self.chk_despike,
                self.chk_probe, self.chk_exc, self.chk_fluor,
                self.ed_cmap, self.cmb_times,
                self.style_panel, self.chk_readout, self.cmb_annot,
                self.btn_export_view]

    def _restore_generation_lock_state(self) -> None:
        """生图结束（成功/失败/取消）后恢复依赖关系控件的状态（V25-B5）。"""
        try:
            self.ed_times.setEnabled(self.cmb_times.currentIndex() == 1)
            self.btn_run.setEnabled(bool(self._selected_groups()))
        except RuntimeError:                           # 控件已销毁（页面关闭）
            pass

    @staticmethod
    def _despike_scans(scans: list, qc_cfg: dict) -> list:
        """作图前去尖峰（与 Φ_Δ 同判据）：返回**浅拷贝**，不改原 Scan 数组。

        用户需求（2026-10-17）：T600 谱在 ~380 nm 有单点掉到 0 的尖峰——同一判据
        在 Φ_Δ 流水线里能删（despike 实测对这条删 1 点），但作图台此前从不调用，
        于是图上原样出现。这里用 ``ui.prep.display.despiked_for_plot``
        （剔除后**只对剔除点插值补上**，原有 NaN 保持断点）；只影响作图显示/导出图，
        **不修改原始数据文件**，也不参与数值分析。
        """
        from ui.prep.display import despiked_for_plot
        out = []
        for s in scans:
            try:
                n = copy.copy(s)
                n.a = despiked_for_plot(s.a, s.wl, qc_cfg or {}, s.path.name)
                out.append(n)
            except Exception:                              # noqa: BLE001 —— 失败保留原谱
                out.append(s)
        return out

    def params_cfg(self) -> dict:
        """把本页选项写进 cfg 深拷贝后返回。"""
        from core.config import default_config
        cfg = default_config()
        # 继承主窗口/参数面板已有的关键值
        for sec in ("wavelength", "baseline", "background", "a0", "kinetics",
                    "quantum_yield", "qc", "controls", "plot", "autogroup", "io"):
            if sec in self.cfg:
                cfg[sec] = self.cfg[sec]
        # V2.5-C：保留 gui 节（out_root 等）——否则「生成定稿图」会忽略用户
        # 自定义输出根，落回默认 runs/（与工作台导出/分析页不同源）。
        cfg["gui"] = copy.deepcopy(self.cfg.get("gui") or {})
        ov = cfg["plot"]["spectra_overlay"]
        if str(cfg["plot"].get("style", "jacs")) == "jacs":
            # JACS 风格：色带/标注开关写进 plot.jacs.*（否则控件会失效）
            jc = cfg["plot"].setdefault("jacs", {})
            jc["cmap"] = self.ed_cmap.text().strip() or "Blues"
            jc["mark_probe"] = self.chk_probe.isChecked()
            jc["mark_excitation"] = self.chk_exc.isChecked()
            jc["mark_fluorophore"] = self.chk_fluor.isChecked()
        else:
            ov["cmap"] = self.ed_cmap.text().strip() or "viridis"
            ov["mark_probe"] = self.chk_probe.isChecked()
            ov["mark_excitation"] = self.chk_exc.isChecked()
            ov["mark_fluorophore"] = "auto" if self.chk_fluor.isChecked() else False
        if self.cmb_times.currentIndex() == 1:
            vals = []
            for x in self.ed_times.text().replace("，", ",").split(","):
                x = x.strip()
                if x:
                    try:
                        vals.append(float(x))
                    except ValueError:
                        pass
            ov["select_times"] = vals or "all"
        else:
            ov["select_times"] = "all"
        return cfg

    def _on_run_done(self, res) -> None:
        """出图完成的收尾（**不能叫 `_on_done`**：那是 PageBase 的后台任务脚手架，
        签名是 ``(res, on_done)``；V1.3 里本类与 SpectraPage 都用同名方法覆写了它，
        于是任何 `run_task(...)` 都会 TypeError —— ε 批用程序驱动页面时暴露）。
        """
        if res.out_dir:
            self.export.set_run_dir(res.out_dir)
            self._final_figs = list(Path(res.out_dir).glob("figures/*.png"))
            self.canvas.set_images(self._final_figs)
            # 生图是显式动作 → 完成后切到「定稿预览」（不触发生成，只是展示产物）
            self.cmb_view.setCurrentIndex(1)
            self._mark_final(self._final_figs)
        if res.ok:
            self.logged.emit("INFO", f"已生成 {len(res.files)} 个图件文件，"
                                     f"组：{', '.join(res.groups)}")
            self.status.emit(f"出图完成 — {res.out_dir}")
        else:
            self.logged.emit("ERROR", res.message or "出图失败")

    def on_activated(self) -> None:
        if self.data_dir and not self.scans:
            self._load_dir(self.data_dir)
