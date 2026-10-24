# -*- coding: utf-8 -*-
"""① 数据页 —— **批次工作区**（FR-14 + FR-16 / UX-2、UX-3）。

职责（一屏做完数据侧的决策，不再"换页重选"）：

1. 选数据目录 → 后台 ``collect_scans`` → 文件表；
2. 角色 / 时间 / 染料整定（复用既有 ``DyeTimePanel``，不重写业务逻辑）；
3. **QC 体检**（P8）：与 ``--dashboard`` 走同一对 core 函数；
4. **批次落盘**（FR-14）：把整定写进数据目录的 ``dye_batch.yaml``；
5. **可疑行徽标**（FR-19）：时间解析不出 / 角色可疑 → 载入即标，不等分析后才发现。

版面（混搭稿，与 Φ_Δ 页同一套部件）：顶部细条放流程条 ``导入 → 整定 → QC 体检``；
左栏批次树（选组即过滤中栏）；中栏结论条 + 文件表（QC 步换成体检图）；右栏分步
检查器，主按钮「去分析 →」。

与"当前批次"的契约（UX-3）：本页是**唯一**的数据源权威。载入成功后发
``batch_changed(list)``，分析页/图件页默认吃这一批（可临时换目录）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QMargins, QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from core.paths import app_root
from ui.pages.base import PageBase
from ui.widgets.batch_tree import BatchTree
from ui.widgets.dye_time_panel import DyeTimePanel
from ui.widgets.file_table import (COL_CHECK, COL_DYE, COL_FILE, COL_GROUP, COL_REASON,
                                   COL_ROLE, COL_STATUS, COL_TIME, ROLE_ZH, FileTable)
from ui.widgets.flow_bar import STEP_DONE, STEP_STALE, STEP_TODO, FlowBar
from ui.widgets.step_inspector import StepInspector, three_columns
from ui.widgets.time_units import format_time_value
from ui.widgets.verdict import VerdictCard
from ui import theme

log = logging.getLogger("dye_tool.ui.data")

#: 流程条三步；检查器第 i 页、:meth:`DataPage._step_summary` 都按这个顺序
STEPS = ("导入", "整定", "QC 体检")
STEP_IMPORT, STEP_TUNE, STEP_QC = range(len(STEPS))
STEP_HINTS = (
    "选数据目录；文件名里的组名和时间会自动识别，识别不对的在下一步改",
    "给每组起染料名、修正识别错的时间；存进 dye_batch.yaml 后随数据一起走",
    "查饱和、无效点和基线漂移，生成一张体检图（与命令行 --dashboard 同一套检查）",
)

#: 顶部细条高度（与分析页的细条一致）
TOP_BAR_H = 44
#: 导入步里列出的最近目录条数
RECENT_MAX = 6

#: QC 体检阈值：(配置键, 标签, 是否按百分比显示, 上限, 小数位)
QC_OPTIONS = (
    ("sat_frac_warn", "饱和点占比警戒", True, 100.0, 1),
    ("nan_frac_warn", "无效点占比警戒", True, 100.0, 1),
    ("drift_warn", "基线漂移警戒 (AU)", False, 5.0, 3),
)


@theme.register_qss
def _qss(p: dict) -> str:
    return f"""
/* ── ① 数据页顶部细条（ui/pages/data.py） ── */
QFrame#dataTopBar {{ background: {p['surface']}; border: none;
                    border-bottom: 1px solid {p['border']}; }}
QFrame#dataTopSep {{ background: {p['border']}; border: none; }}
QLabel#dataQcImage {{ background: {p['surface']}; }}
"""


def suspicious_rows(scans) -> list:
    """找出**解析可疑**的扫描（FR-19：载入即标，不等分析后才发现）。

    判据（每条都能说出"为什么可疑"，而不是拍脑袋）：

    * ``t`` 不是有限数 / 负数 → 时间不可解释；
    * 文件名里**完全没有数字** → 大概率没按约定命名（时间靠猜）；
    * 同组内 ``t`` 重复 → 同一时间点两条谱，分析时会被时间检查挑出来；
    * 角色是 ``sample`` 但组名与角色前缀撞车（如组名以 ``bg`` 开头）。

    返回 ``[(scan, 中文原因)]``，**不修改任何 scan**。
    """
    import math
    import re

    out: list = []
    seen: dict = {}
    for s in scans:
        name = Path(s.path).name
        stem = Path(name).stem
        reasons: list = []
        try:
            t = float(s.t)
            if not math.isfinite(t):
                reasons.append("时间不是有限数")
            elif t < 0:
                reasons.append(f"时间为负（{t:g} s）")
        except (TypeError, ValueError):
            reasons.append("时间无法解析")
        if not re.search(r"\d", stem):
            reasons.append("文件名里没有数字 → 时间多半靠猜")
        key = (s.group, round(float(getattr(s, "t", 0.0)), 6))
        if key in seen:
            reasons.append(f"与 {seen[key]} 同一时间点")
        else:
            seen[key] = name
        if reasons:
            out.append((s, "；".join(reasons)))
    return out


class _ImageView(QScrollArea):
    """QC 体检图：等比缩放到整张放得下（不放大）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.label = QLabel()
        self.label.setObjectName("dataQcImage")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setWordWrap(True)
        self.setWidget(self.label)
        self._pix: Optional[QPixmap] = None
        self.path: Optional[Path] = None
        self.set_placeholder("还没有体检图：在右侧点「生成体检图」")

    def set_placeholder(self, text: str) -> None:
        self._pix, self.path = None, None
        self.label.setPixmap(QPixmap())
        self.label.setText(text)
        theme.mark(self.label, "muted")

    def set_image(self, path: Path) -> bool:
        pix = QPixmap(str(path))
        if pix.isNull():
            self.set_placeholder(f"体检图读不出来：{path}")
            return False
        self._pix, self.path = pix, Path(path)
        self.label.setText("")
        self._refit()
        return True

    def has_image(self) -> bool:
        return self._pix is not None

    def resizeEvent(self, e) -> None:                    # noqa: N802 —— Qt 约定
        super().resizeEvent(e)
        self._refit()

    def _refit(self) -> None:
        if self._pix is None:
            return
        box = self.viewport().size().shrunkBy(QMargins(2, 2, 2, 2))
        pix = self._pix
        if pix.width() > box.width() or pix.height() > box.height():
            pix = pix.scaled(box.expandedTo(QSize(50, 50)), Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        self.label.setPixmap(pix)


class DataPage(PageBase):
    """批次工作区。信号 ``batch_changed(list)`` 通知壳"当前批次变了"。"""

    batch_changed = Signal(object)          # list[Scan]
    batch_saved = Signal(str)               # 保存结果说明
    #: V2.4-A（FR-26.4）：分析模板套用后把新 cfg 冒泡给壳（主窗口统一广播到各页）
    analysis_preset_applied = Signal(object)
    #: 主按钮「去分析 →」（批次已经随 batch_changed 同步过去，这里只请求切页）
    analysis_requested = Signal()

    TITLE = "数据 —— 批次工作区"
    SUBTITLE = ("导入数据目录 → 检查文件表（角色/时间可整定）→ QC 体检 → "
                "整定写入 dye_batch.yaml 随数据一起走。")

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(cfg, parent)
        self.data_dir: Optional[Path] = None
        self.scans: list = []
        self.batch = None
        self._time_overrides: dict = {}
        #: 最近一次 QC 体检：报告 + 图路径；整定改动后标为过期
        self._qc_report: Optional[dict] = None
        self._qc_png: Optional[Path] = None
        self._qc_stale = False
        self._build()
        self.setAcceptDrops(True)          # V2.3 FR-24.3：支持拖入目录/文件
        self.refresh_templates()           # V2.4-A：分析模板下拉
        self._refresh_recent()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        """混搭布局：顶部细条（流程条）/ 左批次树 · 中结论条+文件表 · 右检查器。"""
        self._build_controls()
        self._build_top_bar()
        left = self._build_tree_column()
        center = self._build_center_column()
        inspector = self._build_inspector()
        self.body = three_columns(left, center, inspector)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.top_bar)
        inner = QWidget()
        li = QVBoxLayout(inner)
        li.setContentsMargins(10, 8, 10, 8)
        li.setSpacing(6)
        li.addWidget(self.body, 1)
        li.addWidget(self.progress_row())
        lay.addWidget(inner, 1)
        self.setToolTip("可把数据目录 / 光谱文件直接拖到这里")
        self._go_step(STEP_IMPORT)
        self._update_flow()
        self._update_verdict()

    def _build_controls(self) -> None:
        """检查器各步用到的按钮与输入。"""
        self.btn_dir = QPushButton("选择数据目录…")
        self.btn_reload = QPushButton("重新载入")
        self.btn_reload.setToolTip("清空解析缓存后重新读取当前目录")
        self.btn_reload.setEnabled(False)
        self.chk_recursive = QCheckBox("含子目录")
        self.chk_recursive.setChecked(True)
        self.lbl_dir = QLabel("尚未选择数据目录")
        theme.mark(self.lbl_dir, "muted")
        self.lbl_dir.setWordWrap(True)

        self.btn_save_batch = QPushButton("保存整定到 dye_batch.yaml")
        self.btn_save_batch.setToolTip("把时间 / 染料整定写进数据目录的 dye_batch.yaml，"
                                       "下次载入自动恢复（不改原始数据文件）")
        self.btn_save_batch.setEnabled(False)

        self.btn_qc = QPushButton("▶ 生成体检图")
        self.btn_qc.setToolTip("按下面的警戒线检查每条谱，并在中间显示体检图"
                               "（图存到 runs/qc_dashboard.png）")
        self.btn_qc.setEnabled(False)

        # 主按钮：批次已经随 batch_changed 交给分析页，这里只负责切过去
        self.btn_analyze = QPushButton("去分析 →")
        theme.mark(self.btn_analyze, "primary")
        self.btn_analyze.setToolTip("这一批数据和整定已经同步到「分析」页，点这里切过去")
        self.btn_analyze.setEnabled(False)

        # V2.4-A（FR-26.4）：分析参数模板（**唯一 UI 落点**＝本页；不复制到⑤设置，
        # 遵守单一状态源铁律）。差量模板只覆盖 qc/基线/拟合/Φ 四节。
        self.cmb_template = QComboBox()
        self.cmb_template.setMinimumWidth(120)
        self.cmb_template.setToolTip(
            "把 qc / 基线 / 拟合 / Φ_Δ 四节的参数存成命名模板，换一批数据一键套用。\n"
            "模板只存与默认值不同的键；套用后不会自动重算（按 F5 显式运行）。")
        self.btn_apply_tpl = QPushButton("套用")
        self.btn_save_tpl = QPushButton("另存为模板…")
        self.btn_del_tpl = QPushButton("删除模板")

        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_reload.clicked.connect(lambda: self.load_dir(self.data_dir))
        self.btn_qc.clicked.connect(self._export_qc)
        self.btn_save_batch.clicked.connect(self.save_batch)
        self.btn_analyze.clicked.connect(self._go_analysis)
        self.btn_apply_tpl.clicked.connect(self.apply_template)
        self.btn_save_tpl.clicked.connect(self.save_template)
        self.btn_del_tpl.clicked.connect(self.delete_template)

    def _build_top_bar(self) -> None:
        """顶部细条：「数据」| 流程条 …… 当前目录。"""
        self.flow_bar = FlowBar(STEPS)
        self.flow_bar.step_clicked.connect(self._go_step)
        self.lbl_top = QLabel("")
        theme.mark(self.lbl_top, "subtle")
        bar = QFrame()
        bar.setObjectName("dataTopBar")
        bar.setFixedHeight(TOP_BAR_H)
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(12, 0, 12, 0)
        hb.setSpacing(8)
        hb.addWidget(theme.mark(QLabel("数据"), "muted"))
        sep = QFrame()
        sep.setObjectName("dataTopSep")
        sep.setFixedSize(1, 24)
        hb.addWidget(sep)
        hb.addWidget(self.flow_bar, 1)
        hb.addWidget(self.lbl_top)
        self.top_bar = bar

    def _build_tree_column(self) -> QWidget:
        """左栏：批次树（组 → 文件，只读导航，选组即过滤文件表）。"""
        self.tree = BatchTree()
        self.tree.groups_selected.connect(self._apply_group_filter)
        self.lbl_tree = QLabel("批次")
        theme.mark(self.lbl_tree, "section")
        col = QWidget()
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(self.lbl_tree)
        lay.addWidget(self.tree, 1)
        col.setMinimumWidth(200)
        col.setMaximumWidth(300)
        return col

    def _build_center_column(self) -> QWidget:
        """中栏：结论条 / 文件表或体检图 / 可疑行提示。"""
        self.verdict = VerdictCard()

        self.files = FileTable()
        self.files.time_changed.connect(self._on_file_time_changed)
        self.files.selection_changed.connect(self._on_selection)
        self.files.status_message.connect(self.status)
        # 文件名独占伸缩；备注列可拖宽，默认窄一些，把地方让给文件名
        hdr = self.files.table.horizontalHeader()
        for c in (COL_CHECK, COL_GROUP, COL_DYE, COL_ROLE, COL_TIME, COL_STATUS):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(COL_FILE, QHeaderView.Stretch)
        hdr.setSectionResizeMode(COL_REASON, QHeaderView.Interactive)
        hdr.resizeSection(COL_REASON, 130)
        box_files = QGroupBox("文件表")
        lf = QVBoxLayout(box_files)
        lf.addWidget(self.files)

        self.qc_view = _ImageView()
        box_qc = QGroupBox("QC 体检图")
        lq = QVBoxLayout(box_qc)
        lq.addWidget(self.qc_view)

        self.center_stack = QStackedWidget()
        self.center_stack.addWidget(box_files)
        self.center_stack.addWidget(box_qc)

        # 可疑行提示（FR-19）：文件表下方一行，没问题时隐藏
        self.lbl_suspect = QLabel("")
        self.lbl_suspect.setWordWrap(True)
        theme.mark(self.lbl_suspect, "warn")
        self.lbl_suspect.setVisible(False)

        center = QWidget()
        lay = QVBoxLayout(center)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.verdict)
        lay.addWidget(self.center_stack, 1)
        lay.addWidget(self.lbl_suspect)
        center.setMinimumWidth(380)
        return center

    def _build_inspector(self) -> QWidget:
        """右栏检查器：只展开当前步能改的东西，其余步折成一行摘要。"""
        # ① 导入：目录 + 最近目录 + 分析模板
        p_import = QWidget()
        li = QVBoxLayout(p_import)
        li.setContentsMargins(0, 0, 0, 0)
        li.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(self.btn_dir, 1)
        row.addWidget(self.btn_reload)
        li.addLayout(row)
        li.addWidget(self.chk_recursive)
        li.addWidget(self.lbl_dir)
        hint = QLabel("也可以把数据目录直接拖进窗口。")
        theme.mark(hint, "hint")
        hint.setWordWrap(True)
        li.addWidget(hint)
        li.addSpacing(6)
        li.addWidget(theme.mark(QLabel("最近目录"), "strong"))
        self._recent_lay = QVBoxLayout()
        self._recent_lay.setSpacing(0)
        li.addLayout(self._recent_lay)
        li.addSpacing(6)
        li.addWidget(theme.mark(QLabel("分析模板"), "strong"))
        li.addWidget(self.cmb_template)
        tpl = QHBoxLayout()
        tpl.addWidget(self.btn_apply_tpl)
        tpl.addWidget(self.btn_save_tpl)
        tpl.addWidget(self.btn_del_tpl)
        li.addLayout(tpl)
        li.addStretch(1)
        scroll_i = QScrollArea()
        scroll_i.setWidget(p_import)
        scroll_i.setWidgetResizable(True)
        scroll_i.setFrameShape(QFrame.NoFrame)
        scroll_i.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # ② 整定：染料命名 + 时间工作表（既有面板）+ 落盘
        self.panel = DyeTimePanel()
        self.panel.times_changed.connect(self._on_times_changed)
        self.panel.state_changed.connect(self._refresh_after_panel)
        self.panel.restore_requested.connect(self._refresh_after_panel)
        self.panel.dye_changed.connect(self._on_dye_changed)
        self.panel.cmb_unit.currentTextChanged.connect(self.files.set_time_unit)
        p_tune = QWidget()
        lt = QVBoxLayout(p_tune)
        lt.setContentsMargins(0, 0, 0, 0)
        lt.addWidget(self.panel, 1)
        lt.addWidget(self.btn_save_batch)

        # ③ QC 体检：警戒线（只影响这次体检）+ 生成按钮
        p_qc = QWidget()
        lq = QVBoxLayout(p_qc)
        lq.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        qd = dict((self.cfg or {}).get("qc_dashboard") or {})
        from core.analysis.qc_dashboard import QD_DEFAULTS
        self.qc_spins: dict = {}
        for key, label, pct, top, dec in QC_OPTIONS:
            sp = QDoubleSpinBox()
            sp.setDecimals(dec)
            sp.setRange(0.0, top)
            sp.setSingleStep(0.5 if pct else 0.01)
            if pct:
                sp.setSuffix(" %")
            v = float(qd.get(key, QD_DEFAULTS.get(key, 0.05)))
            sp.setValue(v * 100.0 if pct else v)
            sp.valueChanged.connect(self._on_qc_option_changed)
            self.qc_spins[key] = sp
            form.addRow(label, sp)
        lq.addLayout(form)
        note = QLabel("这里的警戒线只用于这次体检，不改配置文件。")
        theme.mark(note, "hint")
        note.setWordWrap(True)
        lq.addWidget(note)
        lq.addWidget(self.btn_qc)
        self.lbl_qc = QLabel("")
        theme.mark(self.lbl_qc, "muted")
        self.lbl_qc.setWordWrap(True)
        lq.addWidget(self.lbl_qc)
        lq.addStretch(1)

        ins = StepInspector(STEPS, STEP_HINTS)
        for i, w in enumerate((scroll_i, p_tune, p_qc)):
            ins.set_page(i, w)
        ins.add_action(self.btn_analyze)
        ins.step_requested.connect(self._go_step)
        ins.set_summary_provider(self._step_summary)
        self.inspector = ins
        return ins

    # ══════════════ 流程条 ══════════════
    def _go_step(self, idx: int) -> None:
        """切到第 idx 步：检查器换页、中栏在文件表与体检图之间切换。"""
        idx = max(0, min(len(STEPS) - 1, int(idx)))
        if idx and not self.flow_bar.step_enabled(idx):
            idx = STEP_IMPORT
        self.flow_bar.set_current(idx)
        self.center_stack.setCurrentIndex(1 if idx == STEP_QC else 0)
        nxt = idx + 1 < len(STEPS) and self.flow_bar.step_enabled(idx + 1)
        self.inspector.set_current(idx, next_enabled=nxt)

    def _step_summary(self, idx: int) -> str:
        """某一步当前状态的一行摘要（只读已有状态）。"""
        if idx == STEP_IMPORT:
            if self.data_dir is None:
                return "还没选数据目录"
            return f"{self.data_dir.name} · {len(self.scans)} 个谱"
        if idx == STEP_TUNE:
            if not self.scans:
                return "载入后可改组名和时间"
            n_blank = sum(1 for s in self.scans if getattr(s, "time_blank", False))
            n_edit = sum(1 for s in self.scans if getattr(s, "t_edited", False))
            parts = [f"{len(self._groups())} 组"]
            if n_blank:
                parts.append(f"{n_blank} 个时间未定")
            if n_edit:
                parts.append(f"改了 {n_edit} 个时间")
            return " · ".join(parts)
        if self._qc_report is None:
            return "还没体检"
        s = self._qc_report.get("summary") or {}
        text = f"{s.get('n_usable')}/{s.get('n_files')} 可用"
        return text + (" · 需要重新体检" if self._qc_stale else "")

    def _update_flow(self) -> None:
        """按数据 / 整定 / 体检状态给每一步打 完成 / 需要重算 / 未做 标记。"""
        loaded = bool(self.scans)
        tuned = loaded and not any(getattr(s, "time_blank", False) for s in self.scans)
        done = [loaded, tuned, self._qc_report is not None]
        for i in range(len(STEPS)):
            if i:
                self.flow_bar.set_step_enabled(i, loaded, "先选择数据目录")
            if i == STEP_QC and done[i] and self._qc_stale:
                state = STEP_STALE
            else:
                state = STEP_DONE if done[i] else STEP_TODO
            self.flow_bar.set_state(i, state)
        cur = self.flow_bar.current()
        self.inspector.set_next_enabled(
            cur + 1 < len(STEPS) and self.flow_bar.step_enabled(cur + 1))
        self.inspector.refresh_summaries()
        self.btn_analyze.setEnabled(loaded)
        # 同一时刻只有一个主按钮：没数据时是「选择数据目录…」，载入后让位给「去分析 →」
        theme.mark(self.btn_dir, "" if loaded else "primary")
        self.btn_reload.setEnabled(self.data_dir is not None)
        self.btn_qc.setEnabled(loaded)
        self.btn_save_batch.setEnabled(loaded)

    # ══════════════ 结论条 ══════════════
    def _update_verdict(self) -> None:
        """把当前批次压成一个数 + 一句话：几个谱、几组、剔除几个，外加时间与告警。"""
        if not self.scans:
            if self.data_dir is not None:
                self.verdict.set_empty("这个目录里没有读到光谱",
                                       "检查文件格式和命名，或勾选「含子目录」后重新载入")
            else:
                self.verdict.set_empty("还没有载入数据",
                                       "在右侧选择数据目录，或把目录直接拖进窗口")
            return
        groups = self._groups()
        n = len(self.scans)
        n_ex = sum(1 for s in self.scans if s.excluded)
        lines: list = []
        times = [float(s.t) for s in self.scans
                 if not s.excluded and not getattr(s, "time_blank", False)]
        n_blank = sum(1 for s in self.scans if getattr(s, "time_blank", False))
        if times:
            lo, hi = min(times), max(times)
            lines.append(f"时间 {format_time_value(lo, 's')} – {format_time_value(hi, 's')} s"
                         + (f"，{n_blank} 个文件时间未定" if n_blank else ""))
        elif n_blank:
            lines.append(f"{n_blank} 个文件时间未定：在「整定」这一步填上")
        bad = suspicious_rows(self.scans)
        if bad:
            lines.append(f"{len(bad)} 个文件命名或时间可疑，悬停文件表里的黄色行看原因")
        if self._qc_report is not None:
            s = self._qc_report.get("summary") or {}
            qc = f"QC：{s.get('n_usable')}/{s.get('n_files')} 可用"
            if s.get("n_flagged"):
                qc += f"，{s.get('n_flagged')} 个有标记"
            lines.append(qc + ("（整定改过，需要重新体检）" if self._qc_stale else ""))
        elif not bad:
            lines.append("还没做 QC 体检")
        rows = []
        for g in groups:
            members = [s for s in self.scans if s.group == g]
            role = self._group_role(members)
            rows.append({
                "name": self._group_title(g),
                "sub": f"{len(members)} 个",
                "pill": ROLE_ZH.get(role, role),
                "kind": "warn" if role != "sample" else "ok",
                "color": self.tree.group_color(g),
            })
        self.verdict.set_verdict(
            label=f"批次 · {self.data_dir.name}" if self.data_dir else "批次",
            value=str(n), error="个谱",
            headline=f"{n} 个谱 · {len(groups)} 组 · 剔除 {n_ex}",
            lines=lines[:3], groups=rows)

    @staticmethod
    def _group_role(members: list) -> str:
        roles = {str(getattr(s, "role", "sample")) for s in members}
        for r in ("reference", "background", "dark", "blank", "n2", "quencher"):
            if r in roles:
                return r
        return "sample"

    # ══════════════ 批次树 ══════════════
    def _groups(self) -> list:
        return sorted({s.group for s in (self.scans or [])})

    def _group_title(self, group: str) -> str:
        """组标题：染料名 · 组名（两者相同时只显示一个）。"""
        try:
            from core.report.dyes import auto_dye_name
            dye = str(self.panel.dye_map().get(group) or auto_dye_name(group))
        except Exception:                                  # noqa: BLE001
            dye = group
        return group if dye == group else f"{dye} · {group}"

    def _refresh_tree(self) -> None:
        """按当前 scans 重建批次树（保持选中组；重建后会重放一次过滤）。"""
        self.tree.set_scans(self.scans, self._group_title)
        self.lbl_tree.setText(f"批次 {self.data_dir.name}" if self.data_dir else "批次")

    def _apply_group_filter(self, groups) -> None:
        """批次树选中变化：只改文件表的行可见性。"""
        self.files.set_group_filter(groups)

    # ══════════════ 载入 ══════════════
    def _choose_dir(self) -> None:
        d = self.pick_dir()
        if d:
            self.load_dir(d)

    def on_dir_dropped(self, d: Path) -> None:
        self.load_dir(d)

    def load_dir(self, d: Optional[Path]) -> None:
        """载入目录（后台解析）；``None`` = 无操作。"""
        if d is None:
            return
        d = Path(d)
        self.data_dir = d
        self.lbl_dir.setText(f"正在读取 {d} …")
        self.lbl_top.setText(d.name)
        self.lbl_top.setToolTip(str(d))
        self.remember_dir(d)
        self._refresh_recent()
        cfg = dict(self.cfg)
        cfg["io"] = dict((cfg.get("io") or {}),
                         recursive=bool(self.chk_recursive.isChecked()))
        self._qc_cfg = cfg

        def task(progress_cb, log_cb):
            from core.inputs.batch_file import load_batch
            from core.inputs.io import clear_scan_cache, collect_scans
            clear_scan_cache()
            scans = collect_scans(d, cfg,
                                  progress_cb=lambda p, s: progress_cb(p, s))
            issues: list = []
            batch = load_batch(d, issues)
            for it in issues:
                log_cb(it.level, f"[批次文件] {it.message}")
            return scans, batch

        self.run_task(task, "解析数据目录", self._on_loaded,
                      busy_widgets=[self.btn_dir, self.btn_reload])

    def _on_loaded(self, res) -> None:
        scans, batch = res
        self.scans = list(scans)
        self.batch = batch
        self._qc_report, self._qc_png, self._qc_stale = None, None, False
        self.qc_view.set_placeholder("还没有体检图：在右侧点「生成体检图」")
        self.lbl_qc.setText("")
        dye_of = batch.dye_map(self.scans, self.data_dir) if batch else {}
        # V2.9 FR-60.2：载入批次时恢复时间/重复号整定（无文件时零变化）。
        if batch is not None and getattr(batch, "files", None):
            from core.inputs.batch_file import apply_to_scans
            n_applied = apply_to_scans(batch, self.scans, self.data_dir)
            if n_applied:
                self.logged.emit("INFO", f"[批次] 已恢复 {n_applied} 个文件的整定")
        # 面板先应用（“全部留空待填”会改 excluded），文件表随后按最终状态渲染。
        self.panel.set_scans(self.scans, dye_of=dye_of,
                             data_dir=self.data_dir)
        self.files.set_scans(self.scans, dye_of=dye_of)
        n_ok = sum(1 for s in self.scans if not s.excluded)
        n_ex = len(self.scans) - n_ok
        n_batch = len(batch.files) if batch else 0
        self.lbl_dir.setText(
            f"{self.data_dir}\n{len(self.scans)} 个文件，有效 {n_ok}，剔除 {n_ex}；"
            + (f"dye_batch.yaml 里有 {n_batch} 条整定" if n_batch
               else "还没有 dye_batch.yaml"))
        self._refresh_tree()
        self._mark_suspects()
        # V2.4 FR-26.5：dye_batch.yaml 带 analysis_preset → 载入批次时**自动套用一次**
        # （在 batch_changed 之前：下游整定/overrides 与参数一致）。模板缺失 → 降级。
        self._apply_batch_analysis_preset()
        self._update_flow()
        self._update_verdict()
        self.batch_changed.emit(self.scans)
        self.logged.emit("INFO", f"已载入 {len(self.scans)} 个文件（{self.data_dir}）")

    def _refresh_recent(self) -> None:
        """导入步的「最近目录」：配置里的 gui.recent_dirs（只列还存在的目录）。"""
        while self._recent_lay.count():
            w = self._recent_lay.takeAt(0).widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        gui = (self.cfg or {}).get("gui") or {}
        dirs = [Path(d) for d in (gui.get("recent_dirs") or []) if Path(d).is_dir()]
        if not dirs:
            self._recent_lay.addWidget(theme.mark(QLabel("还没有打开过数据目录"), "subtle"))
            return
        for d in dirs[:RECENT_MAX]:
            b = QPushButton(d.name)
            b.setToolTip(str(d))
            theme.mark(b, "link")
            b.clicked.connect(lambda _=False, p=d: self.load_dir(p))
            self._recent_lay.addWidget(b)

    # ══════════════════ V2.4-A（FR-26）：分析参数模板（唯一 UI 落点 = ①数据页） ══════════════════
    def refresh_templates(self) -> None:
        """刷新模板下拉（用户模板按名排序；无模板时禁用三个动作按钮）。"""
        try:
            from core.render import plotstyle
            names = plotstyle.list_analysis_presets()
        except Exception as err:                       # noqa: BLE001 —— 预设坏不得废页
            log.warning("读取分析模板列表失败：%s", err)
            names = []
        cur = self.cmb_template.currentText()
        self.cmb_template.blockSignals(True)
        self.cmb_template.clear()
        self.cmb_template.addItems(names)
        if cur in names:
            self.cmb_template.setCurrentText(cur)
        self.cmb_template.blockSignals(False)
        has = bool(names)
        self.cmb_template.setEnabled(has)
        self.btn_apply_tpl.setEnabled(has)
        self.btn_del_tpl.setEnabled(has)
        self.btn_save_tpl.setEnabled(True)

    def apply_template(self) -> bool:
        """套用当前选中的分析模板（数值节 → FR-25.1 横幅自动出现，不自动重跑）。"""
        name = self.cmb_template.currentText().strip()
        if not name:
            self.status.emit("没有可套用的分析模板")
            return False
        from core.render import plotstyle
        new_cfg, issues, applied = plotstyle.apply_analysis_preset(self.cfg, name)
        for it in issues:
            self.logged.emit(getattr(it, "level", "WARNING"),
                             f"[分析模板] {getattr(it, 'key', '')}: "
                             f"{getattr(it, 'message', it)}")
        if not applied:
            self.status.emit(f"模板「{name}」不存在或不可解析，参数未变")
            return False
        self.cfg = dict(new_cfg)
        self.persist_cfg()                 # 走既有持久化链路（F3 护栏不变）
        self.analysis_preset_applied.emit(self.cfg)
        self.status.emit(f"已套用分析模板「{name}」；如需重算请按 F5")
        self.logged.emit("INFO", f"已套用分析模板「{name}」"
                                 f"（{len(issues)} 条提示）")
        return True

    def save_template(self) -> bool:
        """把**当前 cfg 四节的差量**另存为模板（自动剔除等于默认值的键）。"""
        from PySide6.QtWidgets import QInputDialog
        from core.render import plotstyle

        diff = plotstyle.analysis_preset_diff(self.cfg)
        if not diff:
            self.status.emit("当前参数与默认值完全相同，没有可保存的差量")
            return False
        name, ok = QInputDialog.getText(self, "另存为分析模板",
                                        "模板名（只存 qc/基线/拟合/Φ 四节差量）：")
        if not ok or not str(name or "").strip():
            return False
        try:
            plotstyle.save_analysis_preset(str(name), diff)
        except ValueError as err:
            self.status.emit(f"模板未保存：{err}")
            return False
        self.refresh_templates()
        self.cmb_template.setCurrentText(str(name).strip())
        self.status.emit(f"已保存分析模板「{str(name).strip()}」")
        self.logged.emit("INFO", f"已保存分析模板「{str(name).strip()}」："
                                 f"节 {sorted(diff)}")
        return True

    def delete_template(self) -> bool:
        """删除当前模板（**破坏性确认保留模态**；其余反馈走状态栏）。"""
        from PySide6.QtWidgets import QMessageBox
        from core.render import plotstyle

        name = self.cmb_template.currentText().strip()
        if not name:
            return False
        ans = QMessageBox.question(
            self, "删除分析模板",
            f"确定删除分析模板「{name}」吗？（不影响已保存的 config.yaml）")
        if ans != QMessageBox.Yes:
            return False
        done = plotstyle.delete_analysis_preset(name)
        self.refresh_templates()
        self.status.emit(f"已删除分析模板「{name}」" if done
                         else f"模板「{name}」已不存在")
        self.logged.emit("INFO" if done else "WARNING",
                         f"删除分析模板「{name}」" + ("" if done else "失败"))
        return done

    def _apply_batch_analysis_preset(self) -> None:
        """批次文件的 `analysis_preset` → 套用一次（GUI 链路；CLI 走 pipeline）。"""
        if self.batch is None:
            return
        from core.render import plotstyle
        new_cfg, issues, applied = plotstyle.apply_batch_analysis_preset(
            self.cfg, self.batch)
        name = getattr(self.batch, "analysis_preset", "")
        for it in issues:
            self.logged.emit(getattr(it, "level", "WARNING"),
                             f"[批次模板] {getattr(it, 'key', '')}: "
                             f"{getattr(it, 'message', it)}")
        if not applied:
            if name:
                self.status.emit(f"批次要求的分析模板「{name}」不可用，已按当前参数继续")
            return
        self.cfg = dict(new_cfg)
        self.persist_cfg()
        self.analysis_preset_applied.emit(self.cfg)
        self.status.emit(f"已按批次文件套用分析模板「{name}」")
        self.logged.emit("INFO", f"[批次] 已套用分析模板「{name}」")

    # ── FR-24.9：把本页整定交给分析页（单向同步；不复制控件/状态） ──
    def session_overrides(self) -> dict:
        """当前会话的整定：染料会话覆盖 + 手动时间覆盖（供 ②分析 复用）。"""
        try:
            dye_of = dict(self.panel.dye_map())
        except Exception:                                  # noqa: BLE001
            dye_of = {}
        times = dict(getattr(self, "_time_overrides", None) or {})
        for s in getattr(self, "scans", None) or []:
            if getattr(s, "t_edited", False):
                times.setdefault(s.path.name, float(s.t))
        return {"dye_of": dye_of, "times": times}

    def _go_analysis(self) -> None:
        """主按钮：批次与整定已随 batch_changed 同步，这里只切到「分析」页。"""
        self.analysis_requested.emit()
        goto = getattr(self.window(), "goto", None)
        if not (callable(goto) and goto("analysis")):
            self.status.emit("批次已同步，请在左侧切到「分析」页")

    def _mark_suspects(self) -> None:
        """FR-19：把解析可疑的行**在表里**标出来（工具提示 + 汇总一条）。"""
        bad = suspicious_rows(self.scans)
        if not bad:
            self.lbl_suspect.setText("")
            self.lbl_suspect.setVisible(False)
            return
        for s, why in bad[:60]:
            for r in range(self.files.table.rowCount()):
                it = self.files.table.item(r, 0)
                if it is not None and it.text() == s.path.name:
                    it.setToolTip(f"⚠ 可疑：{why}")
                    it.setForeground(Qt.darkYellow)
                    break
        detail = "；".join(f"{Path(s.path).name}（{w}）" for s, w in bad[:4])
        more = "" if len(bad) <= 4 else f" 等 {len(bad)} 个"
        self.lbl_suspect.setText(f"⚠ 命名/时间可疑{more}：{detail}")
        self.lbl_suspect.setVisible(True)

    # ══════════════ 整定 ══════════════
    def _on_times_changed(self, overrides: dict) -> None:
        """时间工作表改动 → 记下手动时间覆盖（交给分析页；不重算数值）。"""
        self._time_overrides = dict(overrides or {})

    def _on_selection(self, n: int) -> None:
        self.status.emit(f"已选 {n} 个文件")

    def _on_dye_changed(self, _group: str, _dye: str) -> None:
        """染料命名改了 → 文件表染料列、批次树组标题、结论条跟着变。"""
        self.files.refresh_dye_column()
        self._refresh_tree()
        self._update_verdict()

    def _mark_qc_stale(self) -> None:
        if self._qc_report is not None:
            self._qc_stale = True

    # ══════════════ V2.9 FR-60：三态/单位联动 ══════════════
    def _on_file_time_changed(self, _name: str, _t: float) -> None:
        """V29-O2：文件表改时间后同步刷新时间工作表（同一数据对象，仅显示同步）。"""
        self.panel.refresh()
        self._mark_suspects()
        self._mark_qc_stale()
        self._refresh_tree()
        self._update_flow()
        self._update_verdict()

    def _refresh_after_panel(self) -> None:
        """时间工作表三态/单位变化后刷新文件表与下游批次（不重算数值）。"""
        self.files.set_scans(self.scans)
        self._mark_suspects()
        self._mark_qc_stale()
        self._refresh_tree()
        self._update_flow()
        self._update_verdict()
        self.batch_changed.emit(self.scans)

    # ══════════════ 批次落盘（FR-14） ══════════════
    def save_batch(self) -> None:
        """把当前整定写成数据目录里的 ``dye_batch.yaml``（显式动作，严出）。"""
        if not self.scans or self.data_dir is None:
            self.status.emit("请先载入数据目录")
            return
        from core.inputs.batch_file import from_scans, save_batch

        batch = from_scans(self.scans, self.data_dir, base=self.batch)
        # 染料命名：文件表/整定面板里的会话覆盖也一并落盘
        for group, dye in (self.panel.dye_map() or {}).items():
            from core.report.dyes import auto_dye_name
            if not dye or str(dye).strip() == str(auto_dye_name(group)).strip():
                continue
            for s in self.scans:
                if s.group == group:
                    try:
                        rel = Path(s.path).resolve().relative_to(
                            Path(self.data_dir).resolve()).as_posix()
                    except (ValueError, OSError):
                        rel = Path(s.path).name
                    batch.set_entry(rel, dye=str(dye).strip())
        ok, why = save_batch(batch, self.data_dir)
        self.batch = batch
        if ok:
            self.logged.emit("INFO", why)
            self.batch_saved.emit(why)
        else:
            self.logged.emit("WARNING", why)
            self.batch_saved.emit(why)
        self.status.emit(why)

    # ══════════════ QC 体检（P8，与 --dashboard 同源） ══════════════
    def qc_options(self) -> dict:
        """检查器里的警戒线 → ``qc_dashboard`` 节的覆盖值（百分比换回比例）。"""
        out = {}
        for key, _label, pct, _top, _dec in QC_OPTIONS:
            v = float(self.qc_spins[key].value())
            out[key] = v / 100.0 if pct else v
        return out

    def _on_qc_option_changed(self, *_args) -> None:
        self._mark_qc_stale()
        self._update_flow()
        self._update_verdict()

    def _export_qc(self) -> None:
        from core.analysis.qc_dashboard import check_scans, render_dashboard
        out = app_root() / "runs"
        cfg = dict(getattr(self, "_qc_cfg", self.cfg))
        cfg["qc_dashboard"] = {**dict(cfg.get("qc_dashboard") or {}), **self.qc_options()}
        scans = list(self.scans)

        def task(progress_cb, log_cb):
            progress_cb(20, "体检")
            rep = check_scans(scans, cfg)
            out.mkdir(parents=True, exist_ok=True)
            progress_cb(60, "出图")
            p = render_dashboard(scans, cfg, out / "qc_dashboard.png")
            progress_cb(100, "完成")
            return rep, p

        self.run_task(task, "QC 体检", self._on_qc_done, busy_widgets=[self.btn_qc])

    def _on_qc_done(self, res) -> None:
        rep, p = res
        self._qc_report = dict(rep or {})
        self._qc_png = Path(p) if p else None
        self._qc_stale = False
        s = self._qc_report.get("summary") or {}
        if self._qc_png is not None:
            self.qc_view.set_image(self._qc_png)
        else:
            self.qc_view.set_placeholder("体检做完了，但体检图没有画出来（详见运行日志）")
        # core 的结论句后半截是给开发者看的（"——详见 issues 与 files"），这里只留结论
        reason = str(self._qc_report.get("reason") or "").split("——")[0].strip()
        self.lbl_qc.setText(f"{s.get('n_usable')}/{s.get('n_files')} 可用"
                            + (f"：{reason}" if reason else ""))
        self.logged.emit("INFO", f"QC 体检：{s.get('n_usable')}/"
                                 f"{s.get('n_files')} 可用 → {p}")
        self._update_flow()
        self._update_verdict()
        self._go_step(STEP_QC)

    # ══════════════ 生命周期 ══════════════
    def on_activated(self) -> None:
        """页被切到时刷新模板、最近目录与染料列（会话里可能刚改过命名）。"""
        self.refresh_templates()
        self._refresh_recent()
        refresh = getattr(self.files, "refresh_dye_column", None)
        if callable(refresh):
            refresh()
