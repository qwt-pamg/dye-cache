# -*- coding: utf-8 -*-
"""子页「表征总表」（FR-44）：行 = 化合物 × 条件，可编辑/排序/过滤/导出。"""
from __future__ import annotations

import logging

from pathlib import Path
from typing import Optional
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from core.paths import resolve_out_root
from core.render import photophysics
from core.report import characterization
from core.report.characterization import CHARACTERIZATION_COLUMNS
from ui.widgets.batch_tree import GROUP_COLORS

from ui import theme

from .common import (_CHAR_KEYS, _CHAR_NUMERIC_KEYS, _CHAR_FMT, _as_float, _finite,
                     _fmt_num, _empty_char_row, photo_out_path)

log = logging.getLogger("dye_tool.ui.analysis")


class CharacterizationTab(QWidget):
    """表征总表这一步（会话级台账）：中栏 = 表 + 导出；检查器 = 筛选与删行。

    本控件自身是中栏内容；:attr:`settings` 是检查器页，:attr:`primary_actions` 是检查器主按钮。
    """

    def __init__(self, panel) -> None:
        super().__init__()
        #: 所属任务卡：任务调度、日志/状态信号、配置与外部数据源都经它取
        self.panel = panel
        self._rows: list = []
        self._view: list = []
        self._filling = False
        self._sort_key: Optional[str] = None
        self._sort_desc = False
        self._filter_condition = ""
        self._filter_compound = ""
        self._last_csv: Optional[Path] = None
        self._last_xlsx: Optional[Path] = None
        self._build()

    def _build(self) -> None:
        self._build_center()
        self._build_settings()
        self.btn_char_import = QPushButton("从斯托克斯表导入")
        theme.mark(self.btn_char_import, "primary")
        self.btn_char_import.setToolTip("把「斯托克斯位移」任务对比表里的行加进来，编辑后统一导出")
        self.primary_actions = self.btn_char_import

        self.btn_char_delete.clicked.connect(lambda *_: self.delete_selected_rows())
        self.btn_char_clear.clicked.connect(lambda *_: self.clear_rows())
        self.btn_char_import.clicked.connect(lambda *_: self.import_stokes_rows())
        self.btn_char_csv.clicked.connect(lambda *_: self.export_csv())
        self.btn_char_xlsx.clicked.connect(lambda *_: self.export_xlsx())
        self.btn_char_figure.clicked.connect(lambda *_: self.export_bar_figure())
        self.cmb_filter_condition.currentIndexChanged.connect(
            lambda *_: self._refresh_char_view())
        self.cmb_filter_compound.currentIndexChanged.connect(
            lambda *_: self._refresh_char_view())

    def _build_center(self) -> None:
        """中栏：表征总表（可编辑、点表头排序）+ 导出行。"""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        cols = list(CHARACTERIZATION_COLUMNS)
        self._columns = cols
        self.tbl = QTableWidget(0, len(cols))
        self.tbl.setObjectName("charTable")
        self.tbl.setHorizontalHeaderLabels([str(c) for c in cols])
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setAlternatingRowColors(True)
        self.tbl.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl.setEditTriggers(QAbstractItemView.DoubleClicked
                                 | QAbstractItemView.EditKeyPressed)
        hdr = self.tbl.horizontalHeader()
        hdr.setSectionsClickable(True)
        hdr.setSortIndicatorShown(False)
        hdr.sectionClicked.connect(self._on_char_header_clicked)
        for c in range(len(cols)):
            hdr.setSectionResizeMode(c, QHeaderView.Interactive)
        self.tbl.setColumnWidth(0, 110)
        self.tbl.setColumnWidth(1, 120)
        self.tbl.setColumnWidth(2, 150)
        self.tbl.setColumnWidth(3, 150)
        self.tbl.itemChanged.connect(self._on_char_item_changed)
        box = QGroupBox("表征总表（行 = 化合物 × 条件；双击单元格可改）")
        lb = QVBoxLayout(box)
        lb.addWidget(self.tbl, 1)
        lay.addWidget(box, 1)

        box_out = QGroupBox("导出")
        lo = QVBoxLayout(box_out)
        row = QHBoxLayout()
        self.btn_char_csv = QPushButton("导出 CSV…")
        self.btn_char_xlsx = QPushButton("导出 Excel…")
        self.btn_char_figure = QPushButton("导出柱状图…")
        self.btn_char_figure.setToolTip("把 φ / 亮度 / ε / k_photo 按化合物画成分组柱状图")
        for b in (self.btn_char_csv, self.btn_char_xlsx, self.btn_char_figure):
            row.addWidget(b)
        row.addStretch(1)
        lo.addLayout(row)
        self.lbl_char_export = QLabel("导出：—")
        self.lbl_char_export.setWordWrap(True)
        theme.mark(self.lbl_char_export, "hint")
        lo.addWidget(self.lbl_char_export)
        lay.addWidget(box_out)

    def _build_settings(self) -> None:
        """检查器页：只看某个条件/化合物 + 删行与清空。"""
        self.settings = QWidget()
        lay = QVBoxLayout(self.settings)
        lay.setContentsMargins(0, 0, 0, 0)
        hint = QLabel("缺的数值留空即可，之后可以在表里补。"
                      "和斯托克斯对比表用的是同一套列，导入后能一起导出。")
        hint.setWordWrap(True)
        theme.mark(hint, "hint")
        lay.addWidget(hint)
        form = QFormLayout()
        self.cmb_filter_condition = QComboBox()
        self.cmb_filter_condition.addItem("全部", "")
        self.cmb_filter_compound = QComboBox()
        self.cmb_filter_compound.addItem("全部", "")
        form.addRow("只看条件", self.cmb_filter_condition)
        form.addRow("只看化合物", self.cmb_filter_compound)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.btn_char_delete = QPushButton("删除选中")
        self.btn_char_clear = QPushButton("清空")
        row.addWidget(self.btn_char_delete)
        row.addWidget(self.btn_char_clear)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def _bar_payload(self):
        """表征总表 → (groups, series)（分组柱状图数据；缺列自动跳过）。"""
        rows = list(self._rows or [])
        if not rows:
            return [], []
        groups: list = []
        for i, r in enumerate(rows):
            name = str(r.get("compound") or "").strip() or f"行{i + 1}"
            cond = str(r.get("condition") or "").strip()
            label = name if not cond else f"{name}\n{cond}"
            groups.append(label)
        series = []
        for label, key in (("φ", "phi"), ("brightness", "brightness"),
                           ("ε", "epsilon"), ("k_photo", "k_photo")):
            vals = [_as_float(r.get(key)) for r in rows]
            if any(_finite(v) for v in vals):
                series.append({"label": label, "values": vals})
        return groups, series

    def rows(self) -> list:
        """会话级表征台账（深拷贝，供测试/导出）。"""
        return [dict(r) for r in self._rows]

    def add_row(self, row: dict) -> None:
        """追加一行（缺项留空；不臆造数值）。"""
        base = _empty_char_row()
        for key in _CHAR_KEYS:
            if key in (row or {}) and (row or {}).get(key) is not None:
                base[key] = (row or {})[key]
        self._rows.append(base)
        self._update_char_filters()
        self._rebuild_char_table()
        self.panel.status.emit(f"已加入表征总表（共 {len(self._rows)} 行）")
        self.panel.logged.emit("INFO", f"表征总表 +1 行（共 {len(self._rows)} 行）")

    def _update_char_filters(self) -> None:
        for cmb, key in ((self.cmb_filter_condition, "condition"),
                         (self.cmb_filter_compound, "compound")):
            cur = cmb.currentData() or ""
            cmb.blockSignals(True)
            try:
                cmb.clear()
                cmb.addItem("全部", "")
                vals = sorted({str(r.get(key) or "").strip()
                               for r in self._rows
                               if str(r.get(key) or "").strip()})
                for v in vals:
                    cmb.addItem(v, v)
                idx = cmb.findData(cur)
                cmb.setCurrentIndex(idx if idx >= 0 else 0)
            finally:
                cmb.blockSignals(False)
        self._filter_condition = self.cmb_filter_condition.currentData() or ""
        self._filter_compound = self.cmb_filter_compound.currentData() or ""

    def _rebuild_char_table(self) -> None:
        """按 过滤 + 排序 重建表格视图（纯视图级：不改行序/编辑值）。"""
        view = [i for i, r in enumerate(self._rows)
                if (not self._filter_condition
                    or str(r.get("condition") or "") == self._filter_condition)
                and (not self._filter_compound
                     or str(r.get("compound") or "") == self._filter_compound)]
        if self._sort_key in _CHAR_KEYS:
            numeric = self._sort_key in _CHAR_NUMERIC_KEYS

            def _keyf(i: int):
                v = self._rows[i].get(self._sort_key)
                if numeric:
                    return (0, float(v)) if _finite(v) else (1, 0.0)
                return (0, str(v or ""))

            view.sort(key=_keyf, reverse=self._sort_desc)
        self._view = view
        self._filling = True
        try:
            self.tbl.setRowCount(len(view))
            for row_i, src in enumerate(view):
                row = self._rows[src]
                for col, key in enumerate(_CHAR_KEYS):
                    if key in _CHAR_NUMERIC_KEYS:
                        text = _fmt_num(row.get(key), _CHAR_FMT.get(key, ".4g"))
                    else:
                        text = str(row.get(key) or "")
                    it = QTableWidgetItem(text)
                    it.setData(Qt.UserRole, (src, key))
                    self.tbl.setItem(row_i, col, it)
        finally:
            self._filling = False
        self.panel.refresh()

    def _refresh_char_view(self) -> None:
        self._filter_condition = self.cmb_filter_condition.currentData() or ""
        self._filter_compound = self.cmb_filter_compound.currentData() or ""
        self._rebuild_char_table()

    def _on_char_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling or item is None:
            return
        info = item.data(Qt.UserRole)
        if not info:
            return
        src, key = info
        if not (0 <= src < len(self._rows)):
            return
        text = (item.text() or "").strip()
        if key in _CHAR_NUMERIC_KEYS:
            if not text:
                val = float("nan")
            else:
                try:
                    val = float(text)
                except ValueError:
                    self.panel.status.emit(
                        f"第 {item.row() + 1} 行「{self.tbl.horizontalHeaderItem(item.column()).text()}"
                        "」不是数值，已恢复原值")
                    self._filling = True
                    try:
                        item.setText(_fmt_num(self._rows[src].get(key),
                                              _CHAR_FMT.get(key, ".4g")))
                    finally:
                        self._filling = False
                    return
            self._rows[src][key] = val
        else:
            self._rows[src][key] = text
        self._update_char_filters()
        self.panel.refresh()

    def _on_char_header_clicked(self, col: int) -> None:
        if not (0 <= col < len(_CHAR_KEYS)):
            return
        key = _CHAR_KEYS[col]
        if self._sort_key == key:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key = key
            self._sort_desc = False
        hdr = self.tbl.horizontalHeader()
        hdr.setSortIndicatorShown(True)
        hdr.setSortIndicator(col, Qt.DescendingOrder if self._sort_desc
                             else Qt.AscendingOrder)
        self._rebuild_char_table()

    def delete_selected_rows(self) -> int:
        """删除表格中选中的行（映射回台账源行）。"""
        rows = sorted({i.row() for i in self.tbl.selectedIndexes()}, reverse=True)
        srcs = sorted({self._view[r] for r in rows
                       if 0 <= r < len(self._view)}, reverse=True)
        for src in srcs:
            if 0 <= src < len(self._rows):
                self._rows.pop(src)
        if srcs:
            self._update_char_filters()
            self._rebuild_char_table()
            self.panel.status.emit(f"已删除 {len(srcs)} 行（剩余 {len(self._rows)} 行）")
        return len(srcs)

    def clear_rows(self) -> int:
        """清空表征总表（破坏性动作，二次确认；返回清掉的行数）。"""
        if not self._rows:
            self.panel.status.emit("表征总表为空")
            return 0
        from PySide6.QtWidgets import QMessageBox
        ans = QMessageBox.question(
            self, "清空表征总表",
            f"将删除全部 {len(self._rows)} 行（仅本次会话台账；导出文件不受影响）。"
            "确定清空吗？")
        if ans != QMessageBox.Yes:
            return 0
        n = len(self._rows)
        self._rows = []
        self._update_char_filters()
        self._rebuild_char_table()
        self.panel.status.emit(f"已清空表征总表（{n} 行）")
        return n

    def import_stokes_rows(self) -> int:
        """把 FR-41 Stokes 对比表行导入表征总表（同一模块，Q-E19）。"""
        provider = self.panel._stokes_provider
        rows = []
        if callable(provider):
            try:
                rows = list(provider() or [])
            except Exception as err:                   # noqa: BLE001
                log.warning("读取 Stokes 行失败：%s", err)
                rows = []
        if not rows:
            self.panel.status.emit("Stokes 对比表为空：没有可导入的行")
            return 0
        added = 0
        for sr in rows:
            row = _empty_char_row()
            row.update({
                "compound": str(getattr(sr, "group", "") or ""),
                "abs_file": str(getattr(sr, "abs_file", "") or ""),
                "em_file": str(getattr(sr, "em_file", "") or ""),
                "lam_abs": _as_float(getattr(sr, "lam_abs", None)),
                "lam_em": _as_float(getattr(sr, "lam_em", None)),
                "delta_lambda": _as_float(getattr(sr, "delta_lambda", None)),
                "delta_wavenumber": _as_float(getattr(sr, "delta_wavenumber", None)),
                "note": "来自 Stokes 对比表",
            })
            for key in _CHAR_KEYS:                     # 只保留已知键
                if key not in row:
                    row[key] = ""
            self._rows.append(row)
            added += 1
        self._update_char_filters()
        self._rebuild_char_table()
        self.panel.status.emit(f"已从 Stokes 表导入 {added} 行（可编辑后统一导出）")
        self.panel.logged.emit("INFO", f"表征总表导入 FR-41 Stokes 行 {added} 条")
        return added

    def _char_row_objects(self) -> list:
        """转成 characterization.CharacterizationRow（导出用）。"""
        out = []
        for r in self._rows:
            fields = {k: r.get(k) for k in _CHAR_KEYS
                      if r.get(k) is not None and r.get(k) != ""}
            out.append(characterization.make_characterization_row(**fields))
        return out

    def _char_default_path(self, suffix: str) -> Path:
        name = characterization.default_characterization_name()
        if suffix != ".csv":
            name = str(name).replace(".csv", suffix)
        return resolve_out_root(self.panel.cfg) / name

    def export_csv(self, path=None):
        """导出表征总表 CSV（UTF-8-SIG / 中文列名 / 空表不产文件）。"""
        if not self._rows:
            self.panel.status.emit("表征总表为空：未导出 CSV（未生成文件）")
            self.panel.logged.emit("WARNING", "表征总表为空，未导出 CSV")
            return None
        if path is None:
            default = self._char_default_path(".csv")
            path, _ = QFileDialog.getSaveFileName(
                self, "导出表征总表 CSV", str(default), "CSV 文件 (*.csv)")
            if not path:
                return None
        try:
            got = characterization.write_characterization_csv(self._char_row_objects(), path)
        except Exception as err:                       # noqa: BLE001
            self.panel.status.emit(f"导出 CSV 失败：{err}")
            self.panel.logged.emit("ERROR", f"表征 CSV 导出失败：{err}")
            return None
        if got is None:
            self.panel.status.emit("空表：未生成 CSV")
            return None
        self._last_csv = Path(str(got))
        self.lbl_char_export.setText(f"导出：{self._last_csv}")
        self.panel.status.emit(f"已导出表征总表 CSV：{self._last_csv}")
        self.panel.logged.emit("INFO", f"表征总表 CSV 已导出：{self._last_csv}")
        return got

    def export_xlsx(self, path=None):
        """导出表征总表 Excel（复用 excel_export 样式单源）。"""
        if not self._rows:
            self.panel.status.emit("表征总表为空：未导出 Excel")
            return None
        if path is None:
            default = self._char_default_path(".xlsx")
            path, _ = QFileDialog.getSaveFileName(
                self, "导出表征总表 Excel", str(default),
                "Excel 工作簿 (*.xlsx)")
            if not path:
                return None
        try:
            got = characterization.write_characterization_xlsx(
                self._char_row_objects(), path, cfg=self.panel.cfg)
        except Exception as err:                       # noqa: BLE001
            self.panel.status.emit(f"导出 Excel 失败：{err}")
            self.panel.logged.emit("ERROR", f"表征 Excel 导出失败：{err}")
            return None
        if got is None:
            self.panel.status.emit("Excel 导出失败（openpyxl 缺失或写盘失败）")
            return None
        self._last_xlsx = Path(str(got))
        self.lbl_char_export.setText(f"导出：{self._last_xlsx}")
        self.panel.status.emit(f"已导出表征总表 Excel：{self._last_xlsx}")
        self.panel.logged.emit("INFO", f"表征总表 Excel 已导出：{self._last_xlsx}")
        return got

    def export_bar_figure(self):
        """FR-45 分组柱状图新图型出口（core/render/photophysics.plot_grouped_bars）。"""
        groups, series = self._bar_payload()
        if not groups or not series:
            self.panel.status.emit("表征总表为空（或没有可画的数值列）：无法导出柱状图")
            return None
        out_path = photo_out_path(self.panel.cfg, "bars")
        try:
            got = photophysics.plot_grouped_bars(groups, series, out_path, cfg=self.panel.cfg)
        except Exception as err:                       # noqa: BLE001
            self.panel.status.emit(f"柱状图导出失败：{err}")
            self.panel.logged.emit("ERROR", f"柱状图导出失败：{err}")
            return None
        if got:
            self.panel.status.emit(f"已导出分组柱状图：{got}")
            self.panel.logged.emit("INFO", f"光物理表征：分组柱状图已导出 {got}")
        return got

    # ── 工作台：摘要 / 结论条 ──
    def summary(self) -> str:
        """检查器里这一步折叠后的一行摘要。"""
        if not self._rows:
            return "还是空的"
        shown = len(self._view)
        return (f"{len(self._rows)} 行" if shown == len(self._rows)
                else f"{len(self._rows)} 行 · 正在看 {shown} 行")

    def fill_verdict(self, card) -> None:
        """结论条：大数 = 行数，一句话 = 化合物 × 条件，说明行 = 各列填了多少。"""
        rows = self._rows
        if not rows:
            card.set_empty("表征总表还是空的",
                           "在「量子产率 φ」或「光漂白」这一步点「加入表征表」，"
                           "或从斯托克斯表导入")
            return
        compounds = list(dict.fromkeys(str(r.get("compound") or "").strip() or "未命名"
                                       for r in rows))
        conds = {str(r.get("condition") or "").strip() for r in rows} - {""}
        n_phi = sum(1 for r in rows if _finite(r.get("phi")))
        n_k = sum(1 for r in rows if _finite(r.get("k_photo")))
        n_stokes = sum(1 for r in rows if _finite(r.get("delta_lambda")))
        lines = [f"有 φ 的 {n_phi} 行 · 有光漂白 k 的 {n_k} 行 · 有 Δλ 的 {n_stokes} 行"]
        if len(self._view) != len(rows):
            lines.append(f"按筛选正在看 {len(self._view)} 行")
        groups = []
        for j, name in enumerate(compounds):
            mine = [r for r in rows
                    if (str(r.get("compound") or "").strip() or "未命名") == name]
            phi = next((r.get("phi") for r in mine if _finite(r.get("phi"))), None)
            groups.append({"name": name,
                           "sub": f"φ {_as_float(phi):.3g}" if phi is not None
                           else f"{len(mine)} 行",
                           "color": GROUP_COLORS[j % len(GROUP_COLORS)]})
        card.set_verdict(label="表征总表", value=str(len(rows)), error="行",
                         headline=f"{len(compounds)} 个化合物 × {max(1, len(conds))} 个条件",
                         lines=lines, groups=groups)
