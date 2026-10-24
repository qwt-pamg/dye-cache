# -*- coding: utf-8 -*-
"""作图台页面的共同部分（③ 堆叠/时间演化页 :mod:`ui.pages.stack`、④ 紫外/荧光页
:mod:`ui.pages.spectra`）。

两页都是"左栏设置 + 右栏工作视图（活画布）/定稿预览 + 样式面板"的结构，下面这些
行为原来在两个文件里各写一份、只差显示预设分区名与导出文件名前缀：

* 首次显示后按真实尺寸校正左右栏宽度；
* 工作视图 ⇄ 定稿预览切换、按当前视图导出；
* 样式面板：播种可映射键、即时预览、撤销、写入出图参数之外的"另存为预设 / 套用预设 /
  高级…"；
* 显示预设（presets.yaml 的 ``stack`` / ``spectra`` 节）的列出、套用、保存。

子类设置 :attr:`PRESET_SECTION` 与 :attr:`PAGE_MARK`；需要在套用预设时同步额外控件
（如堆叠页的色图框）就覆盖 :meth:`_apply_loaded_style`。
"""
from __future__ import annotations

import copy
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QInputDialog

from ui.pages.base import PageBase
from ui.widgets.style_panel import MAPPED_KEYS


class WorkbenchPage(PageBase):
    """作图台页面基类（不单独实例化）。"""

    #: presets.yaml 里的显示预设分区名，也用作工作视图导出文件名的一部分
    PRESET_SECTION = ""
    #: 状态栏文案里的页序号（如「③」）
    PAGE_MARK = ""
    #: 左栏（设置）宽度范围；初值取页宽 30% 后夹在这个范围内
    BODY_LEFT_MIN_W, BODY_LEFT_MAX_W = 360, 470
    #: 「写入出图参数」后向主窗口冒泡（FR-33.3：广播 cfg + Φ_Δ 过期横幅）
    plot_params_written = Signal(dict)

    def _apply_loaded_style(self, vals: dict) -> None:
        """套用预设 / 高级对话框返回后同步页面专有控件（默认无）。"""

    def _body_left_width(self) -> int:
        """左栏初值按页面宽度自适应（360–470，右栏 stretch 1）。"""
        return max(self.BODY_LEFT_MIN_W,
                   min(self.BODY_LEFT_MAX_W, int(round(self.width() * 0.30))))

    def showEvent(self, event) -> None:                # noqa: N802
        """首次显示后按真实布局尺寸校正 splitter 初值（构造期尚不知道窗口尺寸）。"""
        super().showEvent(event)
        if not getattr(self, "_layout_inited", False):
            QTimer.singleShot(0, self._init_layout_sizes)

    def resizeEvent(self, event) -> None:              # noqa: N802
        """页面真正拿到窗口尺寸后再校正一次（一次性；之后用户拖动优先）。"""
        super().resizeEvent(event)
        if not getattr(self, "_layout_inited", False):
            QTimer.singleShot(0, self._init_layout_sizes)

    def _init_layout_sizes(self) -> None:
        """按首次布局出的真实尺寸校正 body / 右栏垂直 splitter（一次性）。"""
        if getattr(self, "_layout_inited", False):
            return
        if not self.isVisible() or self.width() < 500 or self.height() < 250:
            return                  # 未真正显示 / 尺寸未定 → 等下一次 show/resize
        self._layout_inited = True
        try:
            w = self._body_left_width()
            self.body.setSizes([w, max(1, self.width() - w)])
        except Exception:                              # noqa: BLE001 —— 布局未就绪时保持构造初值
            pass
        try:
            h = self.right_split.height()
            if h > 0:
                canvas = max(140, min(int(round(h * 0.65)), h - 90))
                self.right_split.setSizes([canvas, max(90, h - canvas)])
        except Exception:                              # noqa: BLE001
            pass

    def _choose_dir(self) -> None:
        d = self.pick_dir()
        if d:
            self._load_dir(d)

    def _jacs_section(self) -> dict:
        plot = self.cfg.setdefault("plot", {})
        return plot.setdefault("jacs", {})

    def _on_view_changed(self, idx: int) -> None:
        """工作视图 ⇄ 定稿预览（定稿预览**不触发**生成）。"""
        self.view_stack.setCurrentIndex(1 if idx == 1 else 0)
        if idx == 1 and not getattr(self, "_final_figs", None):
            self.status.emit("尚未生图：请先点「生成定稿图」")

    def _working_cmap_range(self) -> tuple:
        """色带区间：默认 0.05/0.95；风格栏「可映射键」即时预览优先（会话级）。"""
        values = getattr(self, "_mapped_preview_values", {}) or {}
        try:
            lo = float(values.get("cmap_lo", 0.05))
            hi = float(values.get("cmap_hi", 0.95))
        except (TypeError, ValueError):
            lo, hi = 0.05, 0.95
        if not (0.0 <= lo < hi <= 1.0):
            lo, hi = 0.05, 0.95
        return lo, hi

    def _export_working_view(self) -> None:
        """按当前工作视图导出 workspace_<页>_*.png/svg（零 PDF、不覆盖 fig_*）。"""
        from datetime import datetime

        from core.paths import resolve_out_root
        from core.render.plotstyle import filter_formats
        out_root = resolve_out_root(self.cfg)
        stem = f"workspace_{self.PRESET_SECTION}_" + datetime.now().strftime("%H%M%S")
        made = []
        for ext in filter_formats(["png", "svg"]):
            p = out_root / f"{stem}.{ext}"
            i = 2
            while p.exists():
                p = out_root / f"{stem}-{i}.{ext}"
                i += 1
            got = self.live.save_figure(p)
            if got:
                made.append(got)
        if made:
            names = "、".join(Path(x).name for x in made)
            self.logged.emit("INFO", f"工作视图已导出：{names} → {out_root}")
            self.status.emit(f"工作视图导出完成：{names}")
        else:
            self.logged.emit("WARNING", "工作视图导出失败（无数据或写盘失败）")

    def _sync_style_panel(self) -> None:
        if hasattr(self, "style_panel"):
            self.style_panel.set_curves(self.live.curve_info())
        # V25-B3：用 cfg 播种可映射键（用户已编辑过则不覆盖）
        self._seed_style_panel()

    def _on_live_style(self, key: str, style: dict) -> None:
        """编辑 → 活画布 artist 属性（不触发渲染器；FR-33.2）。"""
        self.live.set_style(key, **(style or {}))

    def _on_style_rejected(self, msg: str) -> None:
        """非法样式/颜色被拒绝 → 状态栏 + 日志提示（V25-B4：不崩、不改画布）。"""
        self.logged.emit("WARNING", str(msg))
        self.status.emit(str(msg))

    def _on_mapped_preview(self, values: dict) -> None:
        """可映射键即时预览（会话级；不写 cfg，需显式「写入出图参数」）。"""
        self._mapped_preview_values = dict(values or {})
        self._rebuild_working_view()

    def _on_save_plot_preset(self, values: dict) -> None:
        """FR-33.4：另存为 presets.yaml v2 的 plot 预设（V2.4 机制复用）。"""
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "另存为预设", "预设名：")
        name = str(name or "").strip()
        if not ok or not name:
            return
        try:
            from core.render import plotstyle
            plotstyle.save_user_preset(name, dict(values or {}))
            self.logged.emit("INFO", f"出图预设已另存：{name}"
                                     "（presets.yaml v2 plot 节）")
            self._apply_preset_to_working(name)
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("ERROR", f"另存为预设失败：{err}")

    def _apply_preset_to_working(self, name: str) -> None:
        """套用预设到工作视图（只影响显示；不触发渲染器）。

        V2.5 修复（FR-33.4）：改用 plotstyle.load_preset（先内置、后用户
        presets.yaml）。原先用 apply_preset 只认内置预设，用户「另存为预设」写进
        presets.yaml 的名字会 KeyError 被 except 吞掉 → 套用实际失效。
        """
        try:
            from core.render import plotstyle
            loaded = plotstyle.load_preset(name)
            if not isinstance(loaded, dict):
                raise ValueError(f"未知预设：{name}")
            base = dict(plotstyle.jacs_defaults())
            base.update(loaded)
            values = {k: base[k] for k in MAPPED_KEYS if k in base}
            if values:
                self.style_panel.set_mapped_values(values)
                self._mapped_preview_values = dict(values)
            self._apply_loaded_style(loaded)
            self._rebuild_working_view()
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("WARNING", f"套用预设到工作视图失败：{err}")

    def _on_advanced_style(self) -> None:
        """「高级…」→ 复用既有出图风格对话框（完整 JACS_SCHEMA）。"""
        try:
            from ui.widgets.plot_style_dialog import PlotStyleDialog
            dlg = PlotStyleDialog(copy.deepcopy(self.cfg), self)
            try:
                dlg.exec()
                vals = dlg.values() if hasattr(dlg, "values") else {}
            finally:
                dlg.deleteLater()
            if isinstance(vals, dict) and vals:
                picked = {k: vals[k] for k in MAPPED_KEYS if k in vals}
                if picked:
                    self.style_panel.set_mapped_values(picked)
                    self._mapped_preview_values = dict(picked)
                self._apply_loaded_style(vals)
                self._rebuild_working_view()
        except Exception as err:                       # noqa: BLE001
            self.logged.emit("WARNING", f"高级出图对话框不可用：{err}")

    def _refresh_display_presets(self) -> None:
        from core.render.plotstyle import list_display_presets
        self.cmb_display_preset.blockSignals(True)
        self.cmb_display_preset.clear()
        self.cmb_display_preset.addItem("（当前设置）")
        self.cmb_display_preset.addItems(list_display_presets(self.PRESET_SECTION))
        self.cmb_display_preset.blockSignals(False)

    def _on_display_preset_chosen(self, idx: int) -> None:
        if idx <= 0:
            return
        from core.render.plotstyle import load_display_preset
        name = self.cmb_display_preset.itemText(idx)
        vals = load_display_preset(self.PRESET_SECTION, name)
        if vals is not None:
            self.apply_display_preset(vals)
            self.status.emit(f"已套用{self.PAGE_MARK}显示预设：{name}")

    def _save_display_preset_dialog(self) -> None:
        from core.render.plotstyle import save_display_preset
        name, ok = QInputDialog.getText(self, f"保存{self.PAGE_MARK}显示预设", "预设名：")
        if not ok or not name.strip():
            return
        save_display_preset(self.PRESET_SECTION, name.strip(), self.display_preset_values())
        self._refresh_display_presets()
        self.status.emit(f"已保存{self.PAGE_MARK}显示预设：{name.strip()}")
