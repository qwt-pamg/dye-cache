# -*- coding: utf-8 -*-
"""V2.9.3 FR-68：全应用悬停帮助注册表 + 灰按钮原因 helper。"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QSpinBox, QTableWidget,
                               QToolButton, QToolTip, QWidget)

#: 可编辑参数：键 → 一句话（用途 + 单位/范围 + 值落点）
PARAM_HELP: dict[str, str] = {
    "phi_ref": "参比样 Φ_Δ 的已知值；与 k 标准误、A_ex 不确定度一起按误差传递合成 σ_Φ。",
    "phi_ref_err": "参比样 Φ_Δ 的文献不确定度（默认 0.05）；进入 σ_Φ 的误差传递。",
    "a_ex": "激发波长处吸光度 A_ex；I_abs=1−10^(−A_ex)，未声明时取最早谱自动值。",
    "a_ex_err": "A_ex 的测量不确定度（默认 0.005）；进入 σ_Φ 的误差传递。",
    "iabs_mode": "I_abs 计算模式：exact=1−10^(−A)（默认）；linear=2.303A（仅 A<0.1 近似）。",
    "r_min": "拟合窗口下限（R=A/A0）；默认 0.30，低于此点不参与准一级拟合。",
    "r_max": "拟合窗口上限；默认 1.00，加 r_max_tol 容差。",
    "truncate_frac": "自动截断时保留的时间比例（默认 0.70）。",
    "curvature_p": "残差弯曲检验 p 阈值；默认 0.05，低于则自动截断。",
    "a_floor": "探针信号下限；低于此值不计入拟合。",
    "a0_range": "A(0) 初值搜索范围（下限/上限）。",
    "n_early": "无暗态时取最早几张谱估计 A(0)。",
    "saturation": "饱和判定阈值（默认 3.0）。",
    "baseline_enable": "启用/关闭基线校正。",
    "baseline_method": "基线方法：median/mean/linear。",
    "baseline_wl_range": "基线窗口起止波长（nm）。",
    "background_enable": "启用/关闭背景扣除。",
    "background_mode": "背景模式：matched/mean。",
    "autogroup_core_lo": "母核区起始波长（nm）；谱形指纹分组用。",
    "autogroup_core_hi": "母核区终止波长（nm）。",
    "autogroup_step": "分组重采样步长（nm）。",
    "autogroup_d_max": "分组 RMS 距离阈值；越小分组越严。",
    "dpi": "导出 dpi；jacs 默认 600，legacy 默认 300。",
    "plot_style": "绘图风格：jacs（默认）/legacy（仅像素回归）。",
    "status_colors": "图 c 的状态颜色（OK/WARN/FAILED）。",
    "marker_size": "数据点大小（pt）。",
    "marker_edge_w": "数据点描边宽度。",
    "curve_lw": "曲线线宽。",
    "band_alpha": "拟合/置信带透明度。",
    "annot_size": "图内标注字号（pt）。",
    "captions": "是否写出 figures/captions.md 图注。",
    "figure_style": "jacs 图 c/d 会话图式：柱状/曲线/散点；默认沿用原形态。",
    "figure_error_bars": "图 c/d 是否画误差棒；无 σ 时自动省略。",
    "figure_transparent": "透明背景（仅 jacs/工作台）；默认关=白底。",
    "figure_annotate": "图 c/d 关键值标注；定稿图默认关。",
    "figure_time_unit": "时间演化图横轴显示单位 s/min/h；内部仍为秒。",
    "I_ref": "参比物的激发光强/吸收因子；用于 Φ_F 相对法的参比项。",
    "A_ref": "参比物在激发波长的吸光度；与 I_ref 一起归一。",
    "n_ref": "参比物所在溶剂的折射率；相对法 Φ_F 的折射率修正项。",
    "n": "样品所在溶剂的折射率；相对法 Φ_F 的折射率修正项。",
    "epsilon": "摩尔消光系数 ε（M⁻¹cm⁻¹）；由浓度序列 A=εcl 求得。",
    "compound": "化合物/样品名；只用于表格与报告标注。",
    "condition": "实验条件标签（溶剂/气氛/温度等）；用于分组对照。",
    "probe": "探针波长 λ（nm）；ABDA 水相默认 379 nm，读取该波长处吸光度做动力学。",
    "probe_window": "探针波长取点窗口（nm）；窗口内取峰/最近点，避免单点噪声。",
    "excitation": "激发波长 λ_ex（nm）；用于取 A_ex 与内滤提示。",
    "r_min": "拟合窗口下限（R=A/A0）；默认 0.30，低于此点不参与准一级拟合。",
    "r_max": "拟合窗口上限；默认 1.00，加 r_max_tol 容差。",
    "time_unit": "时间显示单位（s/min/h）；仅显示换算，Scan.t 内部始终为秒。",
    "style": "图式：柱状/曲线/散点；默认沿用当前形态。",
    "formats": "导出格式多选（png/svg/pdf/jpg）；大小写不敏感去重。",
    "transparent": "透明背景开关（默认关=白底）；仅作用于 jacs/工作台 savefig。",
    "bootstrap": "Φ_Δ bootstrap 置信带（默认关）；复用 bootstrap_phi 重采样，不改公式。",
}

#: 术语 → 它在计算里的用途
TERMS: dict[str, str] = {
    "Φ_Δ": "单线态氧量子产率：Φ_样=Φ_参比×(k_样/k_参比)×(I_abs,参比/I_abs,样)。",
    "k_obs": "准一级表观速率常数（s⁻¹）：ln R = −k_obs·t 的斜率绝对值。",
    "R²": "拟合优度；越接近 1 越好，但不单独作为物理正确性的判据。",
    "A_ex": "激发波长处吸光度；决定 I_abs 与内滤效应提示。",
    "I_abs": "吸收光子占比 1−10^(−A_ex)；相对法公式的分母项。",
    "参比样": "已知 Φ_ref 的标准品（如 Rose Bengal/亚甲基蓝）；其 k 用于换算样品 Φ。",
    "探针窗口": "探针波长附近的取点范围；窗口越宽越抗噪，但可能混入肩峰。",
    "截断": "拟合窗口自动截掉偏离准一级的高/低转化率点。",
    "不确定度": "σ；由 k 标准误、A_ex 误差与 Φ_ref 不确定度按误差传递合成。",
    "bootstrap": "成对重采样重拟合得到的 Φ 分布；用于稳健性校验/置信带。",
}

#: 旧模板句（V2.9.6 起必须清零；仅用于安装时识别并替换）
BANNED_HELP = (
    "若为灰色，悬停会说明不可用原因",
    "可编辑参数：修改后进入本次分析/出图设置；悬停可查专用说明",
    "请先完成本页前置操作或选择数据",
    "先完成本页前置操作或选择数据",
    "原因未登记",
)
#: 未登记原因的显式文案（只用于审计/日志兜底，不在常驻状态使用）
UNREGISTERED_REASON = "当前不可用：原因未登记（请报告）"

#: 灰按钮原因模板：状态 → 文案（reason_fn 可动态返回）
DISABLED_REASONS: dict[str, str] = {
    "no_dir": "先选择数据目录",
    "no_groups": "至少需要 1 个组",
    "no_result": "结果目录尚未生成",
    "busy": "已有任务在运行，请等待完成",
    "no_data": "没有可用的光谱数据",
    "no_selection": "请先选择要操作的行",
    "no_undo": "没有可撤销的修改",
    "generic": "当前不可用：请先完成前置步骤或选择数据",
}


def help_for_param(key: str) -> str:
    return PARAM_HELP.get(str(key), "")


def help_for_term(term: str) -> str:
    return TERMS.get(str(term), "")


class DisabledReasonFilter(QObject):
    """V2.9.6：每次悬停都解析并显示（不靠 last-reason 去重）；离开即重置。

    同时处理启用控件的静态说明与禁用控件的真实原因；禁用件自身收不到
    hover 时，由父级过滤器代收。
    """

    def __init__(self, widget: QWidget, reason_fn: Callable[[], str],
                 parent=None, static_fn: Optional[Callable[[], str]] = None):
        super().__init__(parent or widget)
        self._widget = widget
        self._reason_fn = reason_fn
        self._static_fn = static_fn
        self._last = ""
        self._shown_at = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1)
        self._timer.timeout.connect(self._show)
        self._event_from_parent = False

    def _resolve(self) -> str:
        try:
            if not self._widget.isEnabled():
                return str(self._reason_fn() or "")
            if self._static_fn is not None:
                return str(self._static_fn() or "")
            return str(self._widget.toolTip() or "")
        except RuntimeError:
            return ""

    def _cursor_over_target(self) -> bool:
        try:
            # 被藏起来的控件（如检查器里当前步的摘要、第一步的「←」）几何仍是默认值，
            # 光看几何会把它们的说明误弹出来
            if self._widget.isHidden():
                return False
            par = self._widget.parentWidget()
            if par is not None:
                pos = par.mapFromGlobal(QCursor.pos())
                if not self._widget.geometry().contains(pos):
                    return False
        except RuntimeError:
            return False
        return True

    def _show(self) -> None:
        if self._event_from_parent and not self._cursor_over_target():
            return
        text = self._resolve().strip()
        if not text:
            return
        try:
            pos = self._widget.mapToGlobal(self._widget.rect().center())
        except RuntimeError:
            return
        QToolTip.showText(pos, text, self._widget)
        self._shown_at = 0.0

    def eventFilter(self, obj, event) -> bool:          # noqa: N802
        try:
            if event.type() in (QEvent.HoverEnter, QEvent.Enter):
                self._last = ""
                self._event_from_parent = obj is not self._widget
                # Enter 与 HoverEnter 常成对到达：单次启动的 timer 合并为一次显示
                self._timer.start()
                return False
            if event.type() == QEvent.HoverMove:
                # 同一控件内移动不重复弹；只有离开后重新进入才再次显示
                return False
            if event.type() in (QEvent.HoverLeave, QEvent.Leave):
                self._last = ""
                self._timer.stop()
                QToolTip.hideText()
        except RuntimeError:
            return False
        return False


def _clean_help(text: str) -> str:
    """去掉旧模板句；V2.9.6 起任何保留句都必须有真实信息。"""
    t = str(text or "").strip()
    if not t:
        return ""
    if any(bad in t for bad in BANNED_HELP):
        return ""
    return t


def _field_label(w) -> str:
    """QFormLayout 字段对应的行标签文本。"""
    try:
        par = w.parentWidget()
        lay = par.layout() if par is not None else None
        if isinstance(lay, QFormLayout):
            for row in range(lay.rowCount()):
                fld = lay.itemAt(row, QFormLayout.FieldRole)
                lab = lay.itemAt(row, QFormLayout.LabelRole)
                if fld is not None and fld.widget() is w and lab is not None:
                    lw = lab.widget()
                    if isinstance(lw, QLabel):
                        return str(lw.text() or "").strip("：: ")
    except Exception:
        pass
    return ""


def _param_key(w) -> str:
    for cand in (w.objectName(), _field_label(w)):
        key = str(cand or "").strip()
        if key in PARAM_HELP:
            return key
    return ""


def _auto_help(w) -> str:
    """V2.9.6：legacy 接口保留但不再生成“兜底通用句”（审计 generic 必须为 0）。"""
    return ""


#: V2.9.6：常见按钮的真实动作（不写“点击执行本页对应操作”）
BUTTON_HELP = {
    "取消": "取消当前页正在运行的任务；没有任务时按钮不可用。",
    "选择光谱目录…": "选择包含光谱文件的目录，并把文件载入本页清单/浓度表。",
    "选择数据目录…": "选择包含原始光谱的目录，载入后本页才可分析。",
    "选择光谱文件…": "选择一个或多个光谱文件加入本页。",
    "选择谱目录…": "选择光谱目录并载入本页。",
    "选择 CSV…": "选择浓度或参数 CSV 文件载入本页表格。",
    "◀ 上一张": "查看上一张图件；到第一张时不可用。",
    "下一张 ▶": "查看下一张图件；到最后一张时不可用。",
    "放大查看": "在独立窗口中放大当前图件，便于检查细节。",
    "导出结果到…": "把当前结果表导出到指定目录。",
    "导出 CSV…": "把当前表格/结果导出为 CSV。",
    "导出 Excel…": "把当前结果导出为 Excel 工作簿。",
    "打开结果文件夹": "在文件管理器中打开当前结果目录。",
    "打开输出文件": "打开本次运行生成的结果文件。",
    "打开统计报表": "打开多 run 统计报表；需先生成统计结果。",
    "打开汇总表": "打开队列/汇总结果表；需先生成队列汇总。",
    "打开所在文件夹": "在文件管理器中定位当前结果文件。",
    "复制结果": "把当前结果表文本复制到剪贴板。",
    "清空": "清空本页当前的结果/文本显示；不删除磁盘文件。",
    "删除选中": "删除表格中选中的行/项。",
    "移除选中": "从当前列表移除选中的项目，不删除源文件。",
    "加入表征表": "把当前光物理结果加入表征汇总表。",
    "加入对比表": "把当前样品结果加入对比表。",
    "重新载入": "按当前目录重新读取光谱并刷新本页。",
    "撤销 (Ctrl+Z)": "撤销本页最近一次可撤销的修改。",
    "① 生成计划（dry-run）": "生成配置移动计划并在表格中预览，不写入任何文件。",
    "② 执行移动": "执行已生成的配置移动计划，把变更写入目标配置。",
    "检查 config.yaml": "校验 config.yaml 的键名、类型和可写路径，并显示错误。",
    "保存到 config.yaml": "把当前设置写回 config.yaml（先备份旧文件）。",
    "▶ 找峰": "对当前谱执行峰检测并列出峰位/强度。",
    "从 Stokes 表导入": "从已保存的 Stokes 表导入配对/位移数据。",
    "拟合 / 预览": "按当前参数执行拟合并在画布中预览。",
    "计算 φ": "按当前光物理参数计算荧光量子产率 φ。",
    "QC 体检": "对已载入数据执行质控检查并生成报告。",
    "套用": "套用当前选中的模板/预设。",
    "保存整定到 dye_batch.yaml": "把时间/染料整定保存到 dye_batch.yaml。",
    "▶ 开始排队": "把当前批次加入后台处理队列，按顺序执行。",
    "▶ 导出 A@λ": "导出指定波长处的吸光度 A(λ) 序列。",
    "添加目录…": "把选中目录加入当前队列/列表。",
    "…": "展开更多选项；省略号表示会打开后续对话框或菜单。",
    "另存为模板…": "把当前参数保存为可复用的分析模板。",
    "删除模板": "删除选中的用户模板；需先在模板列表中选择。",
    "打开我的数据目录…": "打开默认数据目录，便于查看或放入光谱文件。",
    "文件该怎么命名？": "打开文件命名说明卡片，不改动任何数据。",
    "① 数据": "切换到①数据页，管理批次、文件与 QC。",
    "② 分析": "切换到②分析页，运行 Φ_Δ 与单线态氧计算。",
    "③ 图件": "切换到③图件页，查看/导出生图结果。",
    "④ 档案": "切换到④档案页，浏览历史 run 与统计。",
    "⑤ 设置": "切换到⑤设置页，修改应用配置。",
    "高级参数（一般无需修改）": "展开或收起高级参数区；一般保持默认即可。",
}

CHECKBOX_HELP = {
    "残差弯曲时自动截断": "勾选后，拟合残差呈系统弯曲时自动截短窗口并取更早的线性段。",
    "启用基线校正": "勾选后按当前基线方法/窗口对每条谱做基线校正。",
    "启用背景扣除": "勾选后按背景模式扣除背景谱；未匹配背景时跳过并记录日志。",
    "按累计剂量归一（J/cm²）": "勾选后按累计光剂量归一横轴，用于不同功率/时间序列比较。",
    "图 c/d 误差棒": "勾选后在图 c/d 画误差棒；无不确定度时自动省略。",
    "忽略配置里的 dry_run=true（--force 语义）": "勾选后忽略配置中的 dry_run=true，真正执行移动（有风险）。",
    "JACS 堆叠图（每组一张）": "勾选后每组生成一张 JACS 版式堆叠图。",
    "时间演化叠加图": "勾选后生成按时间着色的时间演化叠加图。",
    "标注探针峰": "勾选后在图上标注 ABDA 探针峰位置。",
    "标注激发波长": "勾选后在图上标注激发波长 λ_ex。",
    "标注荧光团最大吸收": "勾选后在图上标注荧光团最大吸收峰。",
    "含平台项 I∞": "勾选后光漂白拟合加入平台项 I∞。",
    "归一化对比": "勾选后把对比曲线按峰值/初始值归一后再比较。",
    "误差棒": "勾选后在当前图显示误差棒。",
    "关键值标注": "勾选后在图上标注关键数值。",
    "透明背景": "勾选后导出透明背景图（白底关闭）。",
    "含子目录": "勾选后递归扫描所选目录的子目录。",
}

LABEL_HELP = {
    "叠加图时间点": "时间演化叠加图要画的时间点：全部，或逗号分隔的秒数列表。",
    "作图模式": "作图模式：叠图/镜像/上下堆叠/自动配对，决定出图版式。",
    "归一化方式": "曲线归一化方式：最大峰归一或不归一，只影响显示。",
    "绘制组": "选择本次要绘制的组；全部组或单组。",
    "基线方法": "基线校正方法：median/mean/linear，按谱形选择。",
    "基线窗口起": "基线拟合窗口起始波长（nm）。",
    "基线窗口止": "基线拟合窗口终止波长（nm）。",
    "背景模式": "背景扣除模式：matched 配对扣除或 mean 平均扣除。",
    "分组母核起": "自动分组的母核区起始波长（nm）。",
    "分组母核止": "自动分组的母核区终止波长（nm）。",
    "分组网格步长": "自动分组重采样步长（nm）；越小越细但更慢。",
    "分组 RMS 阈值": "自动分组 RMS 距离阈值；越小分组越严格。",
    "截断保留比例": "自动截断时保留的时间比例（0–1，默认 0.70）。",
    "弯曲检验 p 阈值": "残差弯曲检验 p 阈值；低于该值触发自动截断。",
    "探针 a_floor": "探针信号下限；低于此值的点不参与拟合。",
    "A(0) 下界": "A(0) 初值搜索范围的下界。",
    "A(0) 上界": "A(0) 初值搜索范围的上界。",
    "无暗态取最早点数": "没有暗态点时，用最早几张谱估计 A(0) 的点数。",
    "饱和阈值": "饱和判定阈值；超过后标记饱和并提示。",
    "光程 l (cm)": "比色皿光程（cm），用于 A=εcl 换算。",
    "浓度 c (mol/L)": "该行溶液的浓度（mol/L），用于计算 ε。",
    "吸光度 A": "该行在激发波长处的吸光度 A，用于 ε 计算。",
    "色带 (colormap)": "图件色带名称（matplotlib colormap），如 viridis/Blues。",
    "导出 dpi": "导出图件分辨率（dpi）；越高越清晰、文件越大。",
    "时间轴单位": "时间轴显示单位 s/min/h；内部计算仍用秒。",
    "吸收谱（镜像用）": "镜像图使用的吸收谱文件。",
    "发射谱（镜像用）": "镜像图使用的发射谱文件。",
    "cmap_lo": "色带下限（0–1）；越小低值越暗。",
    "cmap_hi": "色带上限（0–1）；越大高值越亮。",
    "marker_color": "数据点颜色（matplotlib 颜色名/十六进制）。",
    "marker_size": "数据点大小（pt）。",
    "legend_size": "图例字号（pt）。",
    "legend_loc": "图例位置（best/upper right 等）。",
    "abs_color": "吸收谱曲线颜色（matplotlib 颜色名/十六进制）。",
    "em_color": "发射谱曲线颜色（matplotlib 颜色名/十六进制）。",
    "spectra_overlay.cmap": "紫外/荧光叠图色带名称（matplotlib colormap）。",
    "出图预设": "选择已保存的出图样式预设；套用后作用于本页图式，不改原始数据。",
    "导出格式": "勾选导出图件格式（png/svg/pdf/jpg，至少一种；pdf 仅 jacs）。",
}

def _default_help(w) -> str:
    """V2.9.6：为每类可交互控件给具体动作/参数含义（无通用兜底句）。"""
    if isinstance(w, QCheckBox):
        txt = str(w.text() or "").strip()
        if txt in CHECKBOX_HELP:
            return CHECKBOX_HELP[txt]
        if txt:
            return f"勾选“{txt}”启用该选项；取消勾选关闭该行为。"
        return "勾选或取消该复选框以启用/关闭对应选项。"
    if isinstance(w, QAbstractButton):
        txt = str(w.text() or w.objectName() or "").strip()
        obj = str(w.objectName() or "")
        if txt in STATIC_BY_TEXT:
            return STATIC_BY_TEXT[txt]
        if txt in BUTTON_HELP:
            return BUTTON_HELP[txt]
        if obj in ("ScrollLeftButton", "ScrollRightButton"):
            return "标签页滚动按钮：左右滚动以显示被隐藏的页签。"
        if obj in ("qt_toolbar_ext_button", "qt_menubar_ext_button"):
            return "工具栏/菜单栏溢出按钮：展开当前放不下的工具项。"
        if obj.startswith("qt_dockwidget_"):
            return "停靠窗口标题按钮：浮动/关闭对应面板。"
        if isinstance(w, QToolButton) and w.parentWidget() is not None and \
                isinstance(w.parentWidget(), QLineEdit):
            return "输入框清除按钮：清空当前输入内容。"
        if obj == "qt_tableview_cornerbutton":
            return "表格左上角按钮：切换整表/当前行选择。"
        if txt:
            return f"执行按钮「{txt}」所标注的操作；结果会更新当前页状态或输出目录。"
        return "窗口工具按钮：执行当前工具栏/页签的对应操作。"
    if isinstance(w, (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox)):
        label = _field_label(w)
        key = _param_key(w)
        if key:
            return PARAM_HELP[key]
        if label in LABEL_HELP:
            return LABEL_HELP[label]
        if label:
            return f"「{label}」是本页分析/出图设置项；修改后需重新计算或出图才生效。"
        obj = str(w.objectName() or "")
        if obj in LABEL_HELP:
            return LABEL_HELP[obj]
        # 自定义卡片/画布内的无标签控件：按所在容器给可执行说明
        anc = " ".join(type(a).__name__ for a in _ancestors(w))
        if "CanvasPanel" in anc:
            return "画布工具栏控件：切换图件/画布显示或交互工具。"
        if "LogView" in anc:
            return "日志过滤控件：选择要显示的日志等级或过滤条件。"
        if "StokesPanel" in anc:
            return "配对选择控件：选择一对吸收/发射谱后加入对比表。"
        if isinstance(w, QComboBox):
            return "下拉选择框：从当前页可用选项中选取一个值。"
        return "文本/颜色输入框：编辑当前页的文本或颜色值。"
    if isinstance(w, QPlainTextEdit):
        return "本页文本区：查看/编辑当前页记录、日志或配置预览，不参与数值计算。"
    return ""


#: V2.9.3 FR-68：逐页灰因（按控件文本/对象名；值可为 str 或 fn(widget)->str）
PAGE_REASONS: dict[str, dict] = {
    "PhiPage": {
        "开始分析": "先选择数据目录并载入可解析的光谱",
        "定稿出图": "先完成一次分析，才有可重渲的结果",
        "重新载入": "先选择数据目录",
        "取消": "当前没有正在运行的任务",
    },
    "ComparePage": {
        "▶ 开始对比": "先选择数据目录并完成自动分组（至少 1 组）",
        "开始对比": "先选择数据目录并完成自动分组（至少 1 组）",
        "重新分组": "先选择数据目录并载入光谱",
        "记住参比/Φ_ref…": "先完成分组并选出参比组",
    },
    "DataPage": {
        "导出 QC 报告": "先选择并载入数据目录",
        "保存批次文件": "先载入数据目录并完成时间/染料整定",
        "套用模板": "先选择分析模板并载入数据",
        "删除模板": "先选择一个用户模板",
    },
    "StackPage": {
        "开始分析": "先选择数据目录并勾选光谱文件",
        "生成定稿图": "先完成一次堆叠/叠加图预览",
    },
    "SpectraPage": {
        "开始分析": "先选择数据目录并勾选荧光光谱",
        "生成定稿图": "先完成一次荧光图预览",
        "记录配对": "先在图中完成一对吸收/发射光谱选择",
    },
    "SettingsPage": {"应用": "当前没有改动，或配置校验未通过"},
    "DyeLibraryPage": {
        "合成": "先选择至少一个染料组分",
        "打开": "先在列表中选中一个染料",
        "参数": "先在列表中选中一个染料",
        "导出": "先在列表中选中要导出的染料",
        "载入所选 run 参数": "先在历史表里选一次有结果目录的 run",
    },
    "ExportBar": {
        "打开目录": "结果目录尚未生成",
        "复制结果": "当前没有可复制的结果文本",
        "导出工作簿": "当前没有可导出的结果工作簿",
        "生成看板": "当前没有可生成看板的结果",
    },
    "ExtinctionCard": {"开始分析": "先选择数据目录并载入浓度序列"},
    "PeaksCard": {"开始分析": "先选择数据目录并载入光谱", "导出峰表": "当前没有峰分析结果"},
    "QueueCard": {"打开文件": "队列尚未生成汇总文件", "打开目录": "队列尚未生成输出目录"},
    "StokesCard": {"加入配对": "先选择一对吸收/发射光谱"},
    "PhotophysicsCard": {"加入 Φ_F 表": "先选择数据目录并载入光谱", "加入漂白表": "先完成一次光漂白拟合"},
    "__default__": {"__default__": "当前不可用：此操作需要先载入本页数据或完成一次计算"},
}
STATIC_BY_TEXT = {
    "▶ 计算 ε": "按浓度表计算摩尔消光系数 ε（M⁻¹cm⁻¹）；需要至少一行有效浓度。",
    "计算 ε": "按浓度表计算摩尔消光系数 ε（M⁻¹cm⁻¹）；需要至少一行有效浓度。",
    "选择目录…": "选择包含光谱文件的目录，并载入到当前页的浓度表/文件清单。",
    "添加文件…": "把选中的光谱文件加入当前页浓度表。",
    "▶ 开始分析": "按当前参数对已载入数据执行分析计算。",
    "开始分析": "按当前参数对已载入数据执行分析计算。",
    "▶ 生成定稿图": "用当前数据与出图参数调用最终渲染器生成定稿图。",
    "生成定稿图": "用当前数据与出图参数调用最终渲染器生成定稿图。",
    "▶ 开始对比": "对已载入的分组结果执行组间对比。",
    "开始对比": "对已载入的分组结果执行组间对比。",
    "导出峰表": "把当前峰分析结果导出为表格。",
    "加入配对": "把当前选中的吸收/发射谱加入配对列表。",
    "加入 Φ_F 表": "把当前光物理结果加入 Φ_F 汇总表。",
    "加入漂白表": "把当前光漂白拟合结果加入汇总表。",
}

REASON_BY_TEXT = {
    "② 执行移动": "当前没有待执行的配置移动计划；请先生成计划（dry-run）",
    "① 生成计划（dry-run）": "当前没有待生成的配置移动计划；请先选择配置项",
    "撤销上一次修改": "当前没有可撤销的配置修改",
    "载入最新 run 参数": "当前没有可载入的历史 run；请先完成一次分析或选择档案",
    "取消": "当前没有正在运行的任务，取消不可用",
    "重新载入": "先选择数据目录，再重新载入光谱",
    "打开输出文件": "尚未生成结果，暂时没有可打开的输出文件",
    "打开目录": "尚未生成结果目录，暂时无法打开",
    "打开输出目录": "尚未生成结果目录，暂时无法打开",
    "复制结果": "当前没有可复制的结果文本",
    "导出工作簿": "当前没有可导出的结果工作簿",
    "生成看板": "当前没有可生成看板的结果",
    "生成定稿图": "先完成一次分析/预览，再生成定稿图",
    "删除模板": "请先在列表中选择一个用户模板",
    "保存批次文件": "先载入数据目录并完成时间/染料整定",
    "应用": "当前没有待应用的改动，或配置未通过校验",
    "加入配对": "先选择一对吸收/发射光谱",
    "加入 Φ_F 表": "先选择数据目录并载入光谱",
    "加入漂白表": "先完成一次光漂白拟合",
}


def _busy_of(obj) -> bool:
    try:
        if bool(getattr(obj, "_busy", False)):
            return True
        job = getattr(obj, "job", None)
        return bool(job is not None and job.isRunning())
    except Exception:
        return False


def _page_reason(widget) -> str:
    """V2.9.6：从控件文本 + 所在页面真实状态给出禁用原因（不写自指模板）。"""
    w = widget
    text = ""
    try:
        text = str(w.text() or w.objectName() or "").strip()
    except Exception:
        pass
    # 1) 明确动作句（与页面状态无关的第一优先级）
    if text in REASON_BY_TEXT:
        return REASON_BY_TEXT[text]
    try:
        if type(w).__name__ == "QToolButton" and w.objectName() in (
                "ScrollLeftButton", "ScrollRightButton"):
            return "页签滚动按钮：当前页签数量无需滚动，按钮不可用"
        if isinstance(w, QLineEdit) and w.parentWidget() is not None and \
                type(w.parentWidget()).__name__.endswith("SpinBox"):
            return "数值输入框跟随所属参数控件：当前参数控件被禁用，无法编辑"
        if isinstance(w, QLineEdit) and not text and any(
                type(a).__name__ == "ParamPanel" for a in _ancestors(w)):
            return "参数面板输入框：当前参数被禁用或尚未载入数据，暂时不能编辑"
    except Exception:
        pass

    # 2) 静态表（类名 + 按钮文本）；命中才用，不无条件套 class 默认
    for anc in _ancestors(w):
        cls = type(anc).__name__
        m = PAGE_REASONS.get(cls)
        if isinstance(m, dict):
            v = m.get(text)
            if callable(v):
                try:
                    v = v(w)
                except Exception:
                    v = ""
            if isinstance(v, str) and v:
                return v

    # 3) 页面/卡片真实状态
    for anc in _ancestors(w):
        cls = type(anc).__name__
        try:
            if cls == "ExtinctionPanel":
                if getattr(anc, "data_dir", None) is None:
                    return "尚未选择光谱目录：请先选择目录并载入浓度表"
                if not getattr(anc, "_files", None):
                    return "目录中没有可解析的光谱文件，无法计算 ε"
                if _busy_of(anc):
                    return "正在计算 ε，请等待当前任务完成"
                return "当前 ε 结果尚未就绪：请填好浓度表后点击「计算 ε」"
            if _busy_of(anc):
                return "当前页正在运行任务，请等待完成后再操作"
            dd = getattr(anc, "data_dir", None)
            has_dir_attr = hasattr(anc, "data_dir")
            scans = getattr(anc, "scans", None)
            spectra = getattr(anc, "spectra", None)
            has_data_attr = scans is not None or spectra is not None
            if has_dir_attr and dd is None:
                return "尚未选择数据目录：请先选择目录并载入光谱"
            if has_data_attr and not (scans or spectra):
                return "尚未载入可解析的光谱：请先选择目录或文件"
            files = getattr(anc, "_files", None)
            if files is not None and not files:
                return "目录中没有可解析的光谱文件"
            results = getattr(anc, "results", None)
            figs = getattr(anc, "_final_figs", None)
            if results is not None and not results:
                return "尚未生成结果：先完成一次计算/分析"
            if figs is not None and not figs:
                return "尚未生成定稿图：先完成一次出图"
        except Exception:
            continue

    # 4) 按动作类型给出可执行说明
    if any(k in text for k in ("打开", "查看")):
        return "尚无可打开的结果：请先完成本页计算/出图"
    if any(k in text for k in ("导出", "保存", "复制")):
        return "尚无可导出的内容：请先完成本页计算/出图"
    if any(k in text for k in ("添加", "删除", "移除", "选中", "上移", "下移")):
        return "请先在对应表格/列表中选择要操作的行"
    if any(k in text for k in ("重算", "重新", "刷新")):
        return "请先载入数据或完成一次计算"
    if any(k in text for k in ("应用", "确认", "确定")):
        return "当前没有待确认的改动，或校验未通过"
    return "当前不可用：此操作需要先载入本页数据或完成一次计算"


def _ancestors(w):
    out = []
    for _ in range(12):
        if w is None:
            break
        out.append(w)
        try:
            w = w.parentWidget()
        except RuntimeError:
            break
    return out

_HELP_TYPES = (QAbstractButton, QLineEdit, QSpinBox, QDoubleSpinBox,
               QComboBox, QCheckBox, QPlainTextEdit, QToolButton)


def _interactive(w) -> bool:
    return isinstance(w, _HELP_TYPES)


#: 审计质量判据：这些句子视为“通用兜底”，必须为 0
GENERIC_REASON_PATTERNS = (
    "当前不可用：此操作需要先载入本页数据或完成一次计算",
    "当前不可用：请先完成前置步骤或选择数据",
)

GENERIC_STATIC_PATTERNS = (
    "点击执行本页对应操作",
    "参数：修改后用于本页本次分析/出图设置",
    "此参数用于本页本次分析/出图设置",
    "文本区：显示/编辑本页文本内容",
    "本页参数标签",
    "此按钮执行当前页的对应操作",
    "当前按钮执行本页的业务动作",
    "执行本页的该业务动作",
    "本页设置控件",
    "执行按钮「",
    "是本页分析/出图设置项",
    "该控件属于当前页选项组",
    "窗口工具按钮",
    "该行参数的标签，字段本身可查看",
)


def _is_internal(w) -> bool:
    """Qt 内部子控件（SpinBox 的行编辑/表格角按钮）不单独审计。"""
    try:
        obj = str(w.objectName() or "")
        if isinstance(w, QLineEdit) and w.parentWidget() is not None:
            if type(w.parentWidget()).__name__.endswith("SpinBox"):
                return True
        if obj in ("qt_tableview_cornerbutton", "qt_spinbox_lineedit"):
            return True
        if isinstance(w, QToolButton) and w.parentWidget() is not None:
            if isinstance(w.parentWidget(), QLineEdit):
                return True
    except RuntimeError:
        return True
    return False


def _static_help(w) -> str:
    t = _clean_help(w.toolTip())
    if t:
        return t
    return _clean_help(_default_help(w))


def _ensure_filter(w) -> None:
    if getattr(w, "_dsh_help_filter", None) is not None:
        return
    flt = DisabledReasonFilter(
        w, lambda w=w: _page_reason(w), w,
        static_fn=lambda w=w: _static_help(w))
    w._dsh_help_filter = flt
    par = w.parentWidget()
    try:
        w.setAttribute(Qt.WA_Hover, True)
        if par is not None:
            par.setAttribute(Qt.WA_Hover, True)
    except RuntimeError:
        pass
    w.installEventFilter(flt)
    if par is not None:
        par.installEventFilter(flt)


def install_app_help(root) -> int:
    """V2.9.6：为全部可悬停控件建 static/reason 帮助，并挂重复可显示的过滤器。

    - 保留已有专用 tooltip；旧模板句被识别并替换为真实动作句。
    - 不再写“若为灰色/悬停可查”这类自指通用句。
    - 幂等重跑：动态控件可再次注册；已有 filter 不重复安装。
    """
    n = 0
    widgets = [root] + list(root.findChildren(QWidget))
    # QFormLayout 行标签：复制字段的真实说明（光物理六字段等）
    for fl in root.findChildren(QFormLayout):
        for row in range(fl.rowCount()):
            lab = fl.itemAt(row, QFormLayout.LabelRole)
            fld = fl.itemAt(row, QFormLayout.FieldRole)
            lw = lab.widget() if lab is not None else None
            fw = fld.widget() if fld is not None else None
            if isinstance(lw, QLabel) and fw is not None:
                _lt = str(lw.text() or "").strip("：: ")
                t = LABEL_HELP.get(_lt) or _static_help(fw)
                if t and _clean_help(lw.toolTip()) != t:
                    lw.setToolTip(t)
                    n += 1
                if not _clean_help(lw.toolTip()):
                    lw.setToolTip(t or f"「{lw.text().strip('：: ')}」：该行参数的标签，字段本身可查看具体含义。")
                    n += 1
                try:
                    lw.setProperty("dsh_help_static", _static_help(fw))
                    lw.setProperty("dsh_help_reason", _page_reason(lw))
                except RuntimeError:
                    pass
                _ensure_filter(lw)
    for w in widgets:
        try:
            if _is_internal(w):
                try:
                    w.setProperty("dsh_help_reason", _page_reason(w))
                except RuntimeError:
                    pass
                continue
            if isinstance(w, QTableWidget):
                for c in range(w.columnCount()):
                    h = w.horizontalHeaderItem(c)
                    if h is None:
                        continue
                    txt = str(h.text() or "").strip()
                    h.setToolTip(f"列「{txt}」：表格列，显示/编辑对应字段；空值或灰色表示当前未选择。")
                    n += 1
                continue
            if isinstance(w, QLabel):
                # 仅表单标签纳入；装饰性标签不进审计
                if _field_label(w) or _clean_help(w.toolTip()):
                    if not _clean_help(w.toolTip()):
                        w.setToolTip(f"「{w.text().strip('：: ')}」：该行参数的标签，字段本身可查看具体含义。")
                        n += 1
                    _ensure_filter(w)
                continue
            if not _interactive(w):
                continue
            static = _static_help(w)
            if static and _clean_help(w.toolTip()) != static:
                w.setToolTip(static)
                n += 1
            try:
                w.setProperty("dsh_help_static", static)
                w.setProperty("dsh_help_reason", _page_reason(w))
            except RuntimeError:
                continue
            _ensure_filter(w)
            n += 1
        except RuntimeError:
            continue
    return n


def resolve_help(widget) -> str:
    """审计/测试用：返回当前应显示的真实帮助（禁用=原因；启用=静态说明）。"""
    try:
        if not widget.isEnabled():
            return _page_reason(widget)
        return _static_help(widget)
    except RuntimeError:
        return ""


def audit_help(root) -> list[dict]:
    """全应用帮助清单；status 区分 static(具体)/reason(禁用真实原因)/generic/missing。"""
    rows = []
    for w in [root] + list(root.findChildren(QWidget)):
        try:
            if _is_internal(w):
                continue
            interactive = _interactive(w)
            is_label = isinstance(w, QLabel) and bool(_field_label(w) or _clean_help(w.toolTip()))
            if not (interactive or is_label):
                continue
            static = _clean_help(w.property("dsh_help_static") or "") or _static_help(w)
            reason = _page_reason(w) if not w.isEnabled() else ""
            generic = bool(static and any(p in static for p in GENERIC_STATIC_PATTERNS))
            if not w.isEnabled():
                generic_reason = any(p in (reason or "") for p in GENERIC_REASON_PATTERNS)
                status = ("generic" if generic_reason else "reason") if reason else "missing"
                generic = generic or generic_reason
            elif generic:
                status = "generic"
            elif static:
                status = "static"
            else:
                status = "excluded"
            rows.append({
                "widget": type(w).__name__,
                "text": (str(getattr(w, "text", lambda: "")() or "")[:40]
                         if isinstance(w, QAbstractButton) else ""),
                "enabled": bool(w.isEnabled()),
                "status": status,
                "generic": generic,
                "static": static,
                "reason": reason,
            })
        except RuntimeError:
            continue
    return rows

def bind_disabled_reason(widget: QWidget, reason_fn: Callable[[], str],
                         parent: Optional[QWidget] = None,
                         overwrite_tooltip: bool = True) -> DisabledReasonFilter:
    """给一个可能被禁用的控件挂上动态原因（安装到自身 + 父级，覆盖 Qt 禁用件）。"""
    flt = DisabledReasonFilter(widget, reason_fn, parent or widget,
                               static_fn=lambda: _static_help(widget))
    widget.installEventFilter(flt)
    par = parent or widget.parentWidget()
    if par is not None:
        par.installEventFilter(flt)
    if overwrite_tooltip and not _clean_help(widget.toolTip()):
        widget.setToolTip(str(reason_fn() or ""))
    return flt
