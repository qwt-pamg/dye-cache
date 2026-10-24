# -*- coding: utf-8 -*-
"""出图参数键位表、校验器与预设（设计文档 V1.1 FR-7 / FR-8 / FR-10）。

三件事集中在本模块，避免散落：

1. :data:`JACS_SCHEMA` —— ``plot.jacs.*`` 的**唯一键位真相**（类型/范围/默认值/
   中文说明）。``core.config.DEFAULT_CFG["plot"]["jacs"]`` 与
   :data:`core.plotting_jacs.JACS_DEFAULTS` 都由 :func:`jacs_defaults` 生成，
   新增键只改这一处。
2. :func:`validate_jacs` / :func:`validate_config` —— 手改 yaml 打错键名或类型
   时给出**中文问题清单**（含"建议改为"），不再静默忽略（设计文档 §2.3 H10）。
3. :data:`BUILTIN_PRESETS` + 用户预设读写 —— 预设只是 ``plot.jacs.*`` 的一组
   取值，**不引入渲染分支**（设计哲学 §1.3-7）。
"""
from __future__ import annotations

import copy
import difflib
import logging
from pathlib import Path
from typing import Any, NamedTuple, Optional, Sequence

log = logging.getLogger("dye_tool.plotstyle")

__all__ = [
    "ConfigIssue", "JACS_SCHEMA", "REMOVED_KEYS", "filter_formats", "jacs_defaults", "validate_jacs",
    "validate_config", "BUILTIN_PRESETS", "builtin_preset_names",
    "preset_overrides", "apply_preset", "presets_path", "load_user_presets",
    "save_user_preset", "delete_user_preset", "list_presets", "load_preset",
    "schema_help_rows", "KEY_TYPES",
]


# ═══════════════════════════ 问题清单 ═══════════════════════════
class ConfigIssue(NamedTuple):
    """一条配置问题（设计文档 §6 契约：``(level, key, 中文说明, 建议)``）。"""

    level: str          # "ERROR" | "WARNING"
    key: str            # 点分键路径，如 plot.jacs.width_in
    message: str        # 中文说明
    suggestion: str = ""  # 建议动作（可为空）


# ═══════════════════════════ 键位表 ═══════════════════════════
#: 类型标记 → 校验与 GUI 控件选择用
KEY_TYPES = ("float", "int", "bool", "str", "color", "list_float", "list_str",
             "dict", "search", "range_or_data")

_F, _I, _B, _S, _C = "float", "int", "bool", "str", "color"


def _k(t: str, default: Any, label: str, help_: str = "", *, lo=None,
       hi=None, choices=None, group: str = "") -> dict:
    """构造一条键位规格（内部小工具）。"""
    return {"type": t, "default": default, "label": label, "help": help_,
            "min": lo, "max": hi, "choices": choices, "group": group}


#: ``plot.jacs.*`` 全键位表（设计文档附录 B + FR-4 同类项收编）。
#: 顺序即 GUI「高级出图参数」对话框的分组顺序。
JACS_SCHEMA: dict[str, dict] = {}


def _register(group: str, rows: dict) -> None:
    for key, spec in rows.items():
        spec["group"] = group
        JACS_SCHEMA[key] = spec


# —— B.1 版式与导出 ——
_register("版式与导出", {
    "width_in": _k(_F, 3.25, "画布宽度 (in)",
                   "期刊单栏 3.25 in ≈ 82.5 mm；双栏预设 7.0 in。改动会直接改变成品物理尺寸。",
                   lo=1.0, hi=20.0),
    "height_in": _k(_F, 2.5, "默认图高 (in)",
                    "未在「逐图高度」中单独指定的图使用此高度。", lo=0.8, hi=20.0),
    "fig_heights": _k("dict",
                      {"a": 2.5, "b": 2.5, "c": None, "d": None, "e": 3.3,
                       "stack": 2.5, "fr4_overlay": 2.5, "fr4_mirror": 2.6,
                       "fr4_panel": 3.3},
                      "逐图高度 (in)",
                      "按图编号单独设高；填 null 表示按内容自动（柱状图/森林图）。"),
    "bar_row_in": _k(_F, 0.42, "柱状图每行高 (in)",
                     "图 c/d 自动高度 = 每行高 × 组数 + 基础高。", lo=0.1, hi=3.0),
    "bar_base_in": _k(_F, 1.05, "柱状图基础高 (in)",
                      "图 c/d 自动高度的固定部分。", lo=0.2, hi=6.0),
    "dpi": _k(_I, 600, "导出 dpi",
              "投稿要求 ≥ 600；预览档另有更低 dpi（预设 preview-fast 150）。",
              lo=72, hi=2400),
    "formats": _k("list_str", ["png"], "导出格式",
                  "默认只出 png（最快）。需要矢量图时加 svg（文字保持可编辑、"
                  "无需嵌入字体，几乎不增加耗时）；eps/tif 亦可。",
                  choices=["png", "svg", "eps", "tif"]),
    "font": _k(_S, "Arial", "首选字体",
               "缺失时按「回退字体」顺序降级并记警告。"),
    "font_fallback": _k("list_str", ["Helvetica", "DejaVu Sans"], "回退字体",
                        "按顺序尝试；全部缺失则用 DejaVu Sans。"),
    "preview_dpi": _k(_I, 150, "预览档 dpi",
                      "「开始分析」默认只出预览图（快）；此值 ≤ 200 以免又慢又大。",
                      lo=50, hi=200),
    "reuse_rendered": _k(_B, True, "复用已渲染的图",
                         "改参数后「再看一眼」时，未受影响的图直接复用上次的成品"
                         "（数据/参数/dpi 任一变化即失效，绝不会显示旧图）。"),
    "minor_ticks": _k(_B, True, "显示次刻度", "关闭后只保留主刻度。"),
    "minor_len_frac": _k(_F, 0.55, "次刻度长度比",
                         "次刻度长度 = 主刻度 × 此系数。", lo=0.0, hi=2.0),
    "minor_w_frac": _k(_F, 0.75, "次刻度宽度比",
                       "次刻度线宽 = 主刻度 × 此系数。", lo=0.0, hi=2.0),
    "minor_tick_step": _k(_F, 20.0, "次刻度间隔 (nm)",
                          "x 轴次刻度位置间隔；0 = 交给 matplotlib 自动。",
                          lo=0.0, hi=500.0),
})
# —— B.2 轴与框 ——
_register("轴与框", {
    "label_size": _k(_F, 7.5, "轴标签字号 (pt)", "期刊常见 7–9 pt。",
                     lo=3.0, hi=30.0),
    "tick_size": _k(_F, 7.0, "刻度数字字号 (pt)", "期刊常见 6–8 pt。",
                    lo=3.0, hi=30.0),
    "legend_size": _k(_F, 6.5, "图例字号 (pt)", "仅在图例出现时生效。",
                      lo=3.0, hi=30.0),
    "annot_size": _k(_F, 7.5, "标注字号 (pt)",
                     "379、Resorufin、t 等图内短标注的字号。", lo=3.0, hi=30.0),
    "spine_width": _k(_F, 0.9, "坐标轴线宽 (pt)", "期刊常见 0.8–1.0 pt。",
                      lo=0.1, hi=5.0),
    "tick_width": _k(_F, 0.8, "刻度线宽 (pt)", "", lo=0.1, hi=5.0),
    "tick_length": _k(_F, 2.8, "刻度长度 (pt)", "", lo=0.5, hi=20.0),
    "tick_direction": _k(_S, "in", "刻度方向", "投期刊常用 in（朝内）。",
                         choices=["in", "out", "inout"]),
    "box": _k(_B, True, "四边完整框",
              "关闭后只保留左/下两条轴线（回到旧版式）。"),
    "xlim": _k("list_float", [300.0, 700.0], "x 轴范围 (nm)",
               "谱图波长范围；改小可放大局部。"),
    "xtick_step": _k(_F, 100.0, "x 主刻度间隔 (nm)", "", lo=0.0, hi=1000.0),
    "ylim_top_headroom": _k(_F, 0.05, "y 轴顶部余量",
                            "y 轴自 0 起，顶部留出此比例的空隙。", lo=0.0, hi=1.0),
    "xlabel": _k(_S, "Wavelength / nm", "x 轴标题", "改单位或做双语图时用。"),
    "ylabel": _k(_S, "Absorbance", "y 轴标题", "同上。"),
    "ylabel_stack": _k(_S, "Absorbance (a.u.)", "堆叠图 y 轴标题",
                       "堆叠图纵轴为任意单位，标题与普通谱图不同。"),
    "legend_loc": _k(_S, "best", "图例位置",
                     "可填 best/upper right/lower left 等 matplotlib 位置串。"),
    "grid": _k(_B, False, "网格线", "投期刊通常关闭。"),
})

# —— B.3 曲线与配色 ——
_register("曲线与配色", {
    "curve_lw": _k(_F, 1.3, "数据曲线线宽 (pt)", "期刊常见 1.2–1.5 pt。",
                   lo=0.1, hi=6.0),
    "annot_lw": _k(_F, 0.8, "标注细线线宽 (pt)",
                   "参考线（灰虚线等）与误差棒的线宽。", lo=0.1, hi=5.0),
    "cmap": _k(_S, "Blues", "时间序色带",
               "单色顺序色带，浅→深 = 早→晚；换染料/换配色风格时改这里。"),
    "cmap_lo": _k(_F, 0.30, "色带起点", "色带取值下限（0–1）。", lo=0.0, hi=1.0),
    "cmap_hi": _k(_F, 0.95, "色带终点", "色带取值上限（0–1）。", lo=0.0, hi=1.0),
    "fit_color": _k(_C, "crimson", "拟合线颜色", "图 b 的拟合直线。"),
    "band_alpha": _k(_F, 0.15, "置信带透明度", "图 b 的 95% 置信区间填充。",
                     lo=0.0, hi=1.0),
    "trunc_shade_color": _k(_C, "0.5", "截断区阴影色",
                            "自动截断区间的灰色底纹。"),
    "trunc_shade_alpha": _k(_F, 0.16, "截断区透明度", "", lo=0.0, hi=1.0),
    "marker_color": _k(_C, "steelblue", "数据点颜色",
                       "图 b 空心圆与图 e 残差点的颜色。"),
    "marker_edge_w": _k(_F, 0.7, "数据点描边宽 (pt)", "", lo=0.0, hi=5.0),
    "marker_size": _k(_F, 2.9, "数据点大小", "图 b/e 的标记尺寸。",
                      lo=0.5, hi=20.0),
    "second_lw_frac": _k(_F, 0.75, "次级曲线线宽比",
                         "残差连线线宽 = 曲线线宽 × 此系数。", lo=0.1, hi=2.0),
    "zero_lw_frac": _k(_F, 0.8, "零参照线线宽比",
                       "图 e 零线线宽 = 标注线宽 × 此系数。", lo=0.1, hi=2.0),
    "zero_line_color": _k(_C, "0.35", "零参照线颜色", "图 e 的 0 水平线。"),
    "trunc_marker_color": _k(_C, "crimson", "截断点颜色",
                             "被自动截断的数据点（图 e 空心圆）。"),
    "fit_failed_color": _k(_C, "crimson", "拟合失败提示色",
                           "图 c 中拟合失败组的提示文字颜色。"),
    "status_colors": _k("dict", {"OK": "steelblue", "WARN": "darkorange",
                                 "FAILED": "crimson"},
                        "状态配色",
                        "图 c 柱色按拟合状态取色（OK/WARN/FAILED）。"),
    "annot_color": _k(_C, "0.25", "标注默认颜色", "图内短标注的默认前景色。"),
    "panel_label_fmt": _k(_S, "({})", "面板编号格式",
                          "图 e 的面板编号，{} 会被替换成 a/b/c…。"),
})

# —— B.4 标注与趋势箭头 ——
_register("标注与趋势箭头", {
    "mark_probe": _k(_B, True, "标注探针波长",
                     "探针处画灰色细虚线并水平标注波长数字。"),
    "mark_excitation": _k(_B, False, "标注激发波长",
                          "默认不画（写进图注）；开启后灰点划线 + 「532 ex.」样式标注。"),
    "mark_fluorophore": _k(_B, True, "标注染料吸收带",
                           "在染料吸收带峰位画灰点线并水平标注染料名。"),
    "fluorophore_label": _k(_S, "auto", "染料标注文本",
                            "auto = 取组名首字段（换染料体系自动跟随）；"
                            "任意字符串 = 钉死；空串 = 只画参考线不写字。"),
    "fluorophore_search": _k("search", [540.0, 620.0], "染料带搜索区间 (nm)",
                             "[lo, hi] 指定区间；或填 auto = 在 x 轴范围内找全局最大峰、"
                             "排除探针 ± 20 nm（不预设染料带位置）。"),
    "fluorophore_window": _k(_F, 4.0, "取峰窗口 (±nm)",
                             "跨时间序列取该窗口内最大值作为标注高度。",
                             lo=0.5, hi=50.0),
    "probe_label_dx_nm": _k(_F, 4.8, "探针标注横向偏移 (nm)", "", lo=0.0, hi=200.0),
    "probe_label_y_frac": _k(_F, 0.985, "探针标注纵向位置",
                             "相对 y 轴上界的比例。", lo=0.0, hi=1.0),
    "excitation_label_dx_nm": _k(_F, 4.8, "激发标注横向偏移 (nm)", "",
                                 lo=0.0, hi=200.0),
    "excitation_label_y_frac": _k(_F, 0.90, "激发标注纵向位置", "",
                                  lo=0.0, hi=1.0),
    "fluorophore_label_dx_nm": _k(_F, 6.0, "染料标注横向偏移 (nm)", "",
                                  lo=0.0, hi=200.0),
    "fluorophore_label_dy_frac": _k(_F, 0.055, "染料标注纵向偏移",
                                    "相对 y 轴上界的比例。", lo=-1.0, hi=1.0),
    "trend_arrow": _k(_B, True, "趋势箭头",
                      "用竖直箭头表达时间方向（取代 colorbar）。"),
    "trend_sign": _k(_S, "auto", "趋势方向",
                     "auto = 按探针吸光度 ln A 斜率判断（与报告的 k 符号一致）；"
                     "也可强制 up/down。", choices=["auto", "up", "down"]),
    "arrow_x_range": _k("range_or_data", [432.0, 556.0], "箭头避让区间 (nm)",
                        "[lo, hi] 指定候选区间；或填 data = 在当前数据上扫描空闲列。"
                        "指定区间被曲线占满时会自动回退 data（见下一项）。"),
    "arrow_auto_fallback": _k(_B, True, "避让自动回退",
                              "指定区间被曲线占据时自动改用数据扫描并记日志。"),
    "arrow_lane_threshold": _k(_F, 0.06, "空闲列判定阈值 (吸光度)",
                               "data 模式下该列所有谱线的最大吸光度低于此值即视为空闲。",
                               lo=0.0, hi=5.0),
    "arrow_y_lo_frac": _k(_F, 0.28, "箭头下端位置",
                          "在空闲列顶端到 y 上界之间按比例取点。", lo=0.0, hi=1.0),
    "arrow_y_hi_frac": _k(_F, 0.82, "箭头上端位置", "", lo=0.0, hi=1.0),
    "arrow_label": _k(_S, "t", "箭头旁文本", "默认 t（时间）。"),
    "arrow_label_dx_frac": _k(_F, 0.020, "箭头文本横向偏移",
                              "相对 x 轴宽度的比例。", lo=0.0, hi=0.5),
    "arrow_color": _k(_C, "0.25", "箭头颜色", ""),
    "arrow_style": _k(_S, "-|>", "箭头样式",
                      "matplotlib arrowstyle 串，如 -|>、->、fancy。"),
    "arrow_lane_margin_nm": _k(_F, 3.0, "避让判定半宽 (nm)",
                               "判定某列是否空闲时向两侧扩展的波长半宽。",
                               lo=0.1, hi=50.0),
    "stack_arrow_dx_frac": _k(_F, 0.46, "堆叠图箭头横向位置",
                              "相对 x 轴宽度的比例。", lo=0.0, hi=1.0),
    "stack_top_frac": _k(_F, 1.15, "堆叠图顶部余量",
                         "堆叠图 y 上界 = 最高谱线 × 此系数。", lo=1.0, hi=3.0),
    "captions": _k(_B, True, "生成图注",
                   "把说明性文字写入 figures/captions.md。"),
    "show_fit_stats_in_panel": _k(_B, False, "图内显示拟合统计",
                                  "开启后把 k/R² 放回图内（组会投屏预设用）。"),
})

# —— FR-4 常规作图（H3/H5/H9 同类项收编） ——
_register("常规作图 (FR-4)", {
    "fr4_label_dy": _k(_F, 0.03, "λmax 标注纵向偏移",
                       "归一化叠图中峰位数字相对峰值的高度偏移。", lo=0.0, hi=1.0),
    "fr4_label_dx_nm": _k(_F, 4.0, "λ 标注横向偏移 (nm)",
                          "镜像图中 λabs/λem 数字的横向偏移。", lo=0.0, hi=100.0),
    "fr4_marker_ms": _k(_F, 2.6, "λmax 标记大小", "", lo=0.5, hi=20.0),
    "fr4_legend_loc_overlay": _k(_S, "upper right", "叠图图例位置", ""),
    "fr4_legend_loc_mirror": _k(_S, "lower right", "镜像图图例位置", ""),
    "fr4_panel_label_y": _k(_F, 1.0, "镜像图面板标注高度", "", lo=0.0, hi=2.0),
})

# —— B.5 开关与校验 ——
_register("高级", {
    "strict": _k(_B, True, "未知键报警",
                 "关闭后回到旧行为：config.yaml 里打错的键名被静默忽略。"),
    "preview_first": _k(_S, "a", "懒渲染首图",
                        "GUI 预览档先只画这些步骤（默认 a，谱图最直观），其余在后台"
                        "补渲；多步骤用逗号分隔，如「a,stack」。"),
    "stack_min_spectra": _k(_I, 2, "堆叠图最少谱数",
                            "某组有效谱少于此数就不画该组的堆叠图。", lo=2, hi=200),
    "parallel": _k(_B, True, "多进程出图",
                   "整批出图时用多进程并行（图 b/堆叠图按组切分）。Windows 上每个"
                   "子进程要重新 import matplotlib（约 0.7 s），所以只在任务够多、"
                   "估算工时够长时才真正并行；小批量自动走串行，产物完全相同。"),
    "parallel_min_tasks": _k(_I, 8, "并行任务数门槛",
                             "任务数少于它就不并行（子进程启动开销划不来）。",
                             lo=1, hi=200),
    "parallel_min_seconds": _k(_F, 3.0, "并行工时门槛 (s)",
                               "串行估算耗时低于它就不并行。", lo=0.0, hi=600.0),
    "parallel_workers": _k(_I, 0, "并行进程数",
                           "0 = 自动（CPU 核数−1，上限 8，且不超过任务数）。",
                           lo=0, hi=64),
})


def jacs_defaults() -> dict:
    """``plot.jacs.*`` 的默认值深拷贝（唯一来源）。"""
    return {k: copy.deepcopy(v["default"]) for k, v in JACS_SCHEMA.items()}


def schema_help_rows(group: Optional[str] = None) -> list[tuple]:
    """``[(key, label, default, help)]``，供 GUI/文档生成说明表。"""
    return [(k, v["label"], v["default"], v["help"])
            for k, v in JACS_SCHEMA.items()
            if group is None or v["group"] == group]


# ═══════════════════════════ 校验 ═══════════════════════════
#: 已废除的键 → 说明（给旧 config.yaml 一个明确的迁移提示，而不是"拼写错误"）
REMOVED_KEYS: dict[str, str] = {
    "combined_pdf": "V1.2 起不再输出 PDF（实测 PDF 字体子集化占导出耗时一半以上），"
                    "合并 PDF 一并取消",
    "font_cache": "该键是为加速 PDF 字体子集化而加的；不再输出 PDF 后已无意义",
}


def filter_formats(formats, allow_pdf: bool = False) -> list:
    """输出格式过滤：**去掉 pdf 并去重**——V1.2 起全项目唯一的不出-PDF 出口。

    所有保存路径（JACS 的 :func:`save_jacs`、legacy 的 ``_save``、堆叠图、对比图）
    都必须经过这里，一条都不许自己写 ``for ext in ("png", "pdf")``——正是这种
    散落的硬编码让 legacy 路径漏网了两个 PDF 输出点。传入 None/空时安全地
    回到 ``["png"]``。

    O10：同时按大小写不敏感去重（``["png","PNG"]`` 会 savefig 两次并返回重复
    路径），保留首次出现的写法。
    """
    src = list(formats) if formats else ["png"]
    out: list = []
    seen: set = set()
    for f in src:
        key = str(f).strip().lstrip(".").lower()
        if not key or (key == "pdf" and not allow_pdf) or key in seen:
            continue
        seen.add(key)
        out.append(f)
    return out or ["png"]


def _type_ok(value: Any, t: str) -> bool:
    """宽松类型判断（yaml 里 1 与 1.0 都当数值，True/False 单独算布尔）。"""
    if t == _B:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False                        # 布尔不能当数值用
    if t == _I:
        return isinstance(value, int)
    if t == _F:
        return isinstance(value, (int, float))
    if t in (_S, _C):
        return isinstance(value, str)
    if t == "list_float":
        return (isinstance(value, (list, tuple))
                and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                        for v in value))
    if t == "list_str":
        return (isinstance(value, (list, tuple))
                and all(isinstance(v, str) for v in value))
    if t == "dict":
        return isinstance(value, dict)
    if t == "search":
        return (value == "auto" or value == "data"
                or (isinstance(value, (list, tuple)) and len(value) == 2
                    and all(isinstance(v, (int, float))
                            and not isinstance(v, bool) for v in value)))
    if t == "range_or_data":
        return (value == "data"
                or (isinstance(value, (list, tuple)) and len(value) == 2
                    and all(isinstance(v, (int, float))
                            and not isinstance(v, bool) for v in value)))
    return True


_TYPE_ZH = {_F: "数值", _I: "整数", _B: "true/false", _S: "文本", _C: "颜色/文本",
            "list_float": "数值列表", "list_str": "文本列表", "dict": "映射",
            "search": "[下限, 上限] 或 \"auto\"",
            "range_or_data": "[下限, 上限] 或 \"data\""}


def suggest_key(key: str, known: Optional[Sequence[str]] = None) -> str:
    """给一个未知键名提"是不是想写 X？"的建议（无近似则给通用文案）。

    评审记录「重复逻辑 #4」点名"未知键校验 + difflib 建议两套"
    （本模块的 :func:`validate_jacs` 与 ``plotting_jacs.jcfg``）。

    **实测澄清**：两处的**判定**其实不同，不能合并——

    * 本模块会把 ``REMOVED_KEYS`` 里的"已废除键"单独报成另一种文案；
    * ``jcfg`` 只看"在不在 ``JACS_SCHEMA`` 里"。

    但两处的**建议**逐字相同（同 ``cutoff=0.6``、同句式），所以只把建议抽出来共用：
    一处真相，两边调用，判定逻辑各留各的。
    """
    pool = list(known) if known is not None else list(JACS_SCHEMA)
    near = difflib.get_close_matches(str(key), pool, n=1, cutoff=0.6)
    return f"是否想写 `{near[0]}`？" if near else "该键不存在，删掉即可"


#: ``cmap_lo``/``cmap_hi`` 的合法区间回退值（与 JACS_SCHEMA 默认一致）。
CMAP_FALLBACK = (0.30, 0.95)

#: 已就"色带/区间非法"警告过的键（同一进程只吵一次，避免 8 张图刷 8 条）
_CMAP_WARNED: set = set()


def cmap_colors(name, lo, hi, n: int) -> list:
    """取 ``n`` 个色带颜色——**渲染与对话框预览的唯一来源**（V2.x 审查 M1）。

    为什么必须只有一份（M1，静态确认）：原先渲染侧
    ``plotting_jacs._cmap_colors`` 直接读 ``cmap_lo/hi`` 无校验，而对话框侧
    ``plot_style_dialog._cmap_colors`` 在区间非法时**回退 0.30/0.95**。
    用户手改 ``cmap_lo >= cmap_hi`` 时：预览用回退值、实际出图被 matplotlib
    静默 clamp → **预览与实际色序不同**，而这正是"预览"存在的意义。

    本函数把两处收敛成一套语义：

    * 未知色带 → ``Blues`` + WARNING（一次）；
    * 区间非法（非数值 / 不在 [0,1] / ``lo >= hi``）→ :data:`CMAP_FALLBACK`
      + WARNING（一次）；
    * ``n <= 1`` → 取 ``hi`` 端单色（与旧行为一致）。

    Returns:
        长度 ``n`` 的 RGBA 元组列表（可直接喂 matplotlib）。
    """
    import matplotlib

    def _warn(key: str, msg: str) -> None:
        if key in _CMAP_WARNED:
            return
        _CMAP_WARNED.add(key)
        log.warning("%s", msg)

    try:
        cmap = matplotlib.colormaps[str(name)]
    except KeyError:
        _warn(f"name:{name}", f"未知色带 {name!r} → 回退 Blues")
        cmap = matplotlib.colormaps["Blues"]
    try:
        flo, fhi = float(lo), float(hi)
    except (TypeError, ValueError):
        _warn("range:type", f"色带区间不是数值（lo={lo!r}, hi={hi!r}）→ "
                            f"回退 {CMAP_FALLBACK}")
        flo, fhi = CMAP_FALLBACK
    if not (0.0 <= flo < fhi <= 1.0):
        _warn("range", f"色带区间非法（lo={flo}, hi={fhi}，要求 "
                       f"0<=lo<hi<=1）→ 回退 {CMAP_FALLBACK}")
        flo, fhi = CMAP_FALLBACK
    if n <= 1:
        return [cmap(fhi)]
    return [cmap(flo + (fhi - flo) * i / (n - 1)) for i in range(n)]


def validate_jacs(plot_cfg: Optional[dict]) -> list[ConfigIssue]:
    """校验 ``plot.jacs`` 节：未知键 / 类型 / 范围 / 枚举 / 区间顺序。

    Returns:
        :class:`ConfigIssue` 列表（空 = 全部合规）。**不抛异常**。
    """
    issues: list[ConfigIssue] = []
    if plot_cfg is None:
        return issues
    if not isinstance(plot_cfg, dict):
        return [ConfigIssue("ERROR", "plot.jacs", "该节不是映射（键值对）",
                            "应写成 `jacs:` 下面缩进的键值对")]
    known = list(JACS_SCHEMA)
    for key, val in plot_cfg.items():
        if str(key).startswith("_"):
            continue          # M12：下划线私有键（如 `_preset`）不是用户手误，跳过
        if key not in JACS_SCHEMA:
            if key in REMOVED_KEYS:
                issues.append(ConfigIssue(
                    "WARNING", f"plot.jacs.{key}",
                    f"该键已废除、不再有任何效果：{REMOVED_KEYS[key]}",
                    "从 config.yaml 里删掉这一行即可（不影响出图）"))
                continue
            sug = suggest_key(key, known)      # 重复逻辑 #4：建议逻辑与 jcfg 共用
            issues.append(ConfigIssue(
                "WARNING", f"plot.jacs.{key}",
                "未知键（不会被任何代码读取）", sug))
            continue
        spec = JACS_SCHEMA[key]
        if not _type_ok(val, spec["type"]):
            issues.append(ConfigIssue(
                "ERROR", f"plot.jacs.{key}",
                f"类型不对：应为{_TYPE_ZH.get(spec['type'], spec['type'])}，"
                f"当前是 {type(val).__name__}",
                f"建议改为 {spec['default']!r}"))
            continue
        # 枚举
        ch = spec.get("choices")
        if ch:
            vals = val if isinstance(val, (list, tuple)) else [val]
            bad = [v for v in vals if v not in ch]
            if bad:
                issues.append(ConfigIssue(
                    "ERROR", f"plot.jacs.{key}",
                    f"取值 {bad} 不在允许范围 {ch}", f"改为 {ch[0]!r} 等"))
        # 范围
        if spec["type"] in (_F, _I) and isinstance(val, (int, float)):
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and val < lo:
                issues.append(ConfigIssue("WARNING", f"plot.jacs.{key}",
                                          f"{val} 小于建议下限 {lo}",
                                          f"建议 ≥ {lo}"))
            if hi is not None and val > hi:
                issues.append(ConfigIssue("WARNING", f"plot.jacs.{key}",
                                          f"{val} 超过建议上限 {hi}",
                                          f"建议 ≤ {hi}"))
        # 区间顺序
        if spec["type"] in ("list_float", "search") and \
                isinstance(val, (list, tuple)) and len(val) == 2 and \
                isinstance(val[0], (int, float)) and \
                isinstance(val[1], (int, float)) and val[0] >= val[1]:
            issues.append(ConfigIssue("ERROR", f"plot.jacs.{key}",
                                      f"区间顺序反了：{val}",
                                      f"应为 [{val[1]}, {val[0]}]"))
    # 跨键一致性
    lo, hi = plot_cfg.get("cmap_lo"), plot_cfg.get("cmap_hi")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo >= hi:
        issues.append(ConfigIssue("WARNING", "plot.jacs.cmap_lo",
                                  f"色带起点 {lo} 不小于终点 {hi}",
                                  "起点应小于终点"))
    search = plot_cfg.get("fluorophore_search")
    if isinstance(search, (list, tuple)) and len(search) == 2:
        xl = plot_cfg.get("xlim") or [300.0, 700.0]
        if isinstance(xl, (list, tuple)) and len(xl) == 2 and \
                (search[0] < xl[0] or search[1] > xl[1]):
            issues.append(ConfigIssue(
                "WARNING", "plot.jacs.fluorophore_search",
                f"搜索区间 {list(search)} 超出 x 轴范围 {list(xl)}",
                "超出部分的数据不会被画出来，峰可能找不到"))
    return issues


def _safe_section(cfg: dict, name: str,
                  issues: list[ConfigIssue]) -> dict:
    """取子节；**非映射值**记一条 ERROR 并返回 ``{}``（绝不抛）。

    V2.x 审查 S1（动态复现）：历史实现写 ``cfg.get("wavelength") or {}``——
    用户把某节写成标量（``wavelength: 379``）时 `or {}` 拿到的是 379，
    随后的 ``.get()`` 直接 ``AttributeError``，穿透到 ``load_config_ex``（无 try）
    → **GUI/CLI 启动崩溃**。这是"配置写坏了"却表现为"程序坏了"的最坏形态。
    本函数把"不是映射"变成一条可读的 ERROR，校验继续跑完其余节。
    """
    v = cfg.get(name)
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    issues.append(ConfigIssue(
        "ERROR", name,
        f"配置节 {name} 应为「键: 值」映射，实际是 {type(v).__name__}：{v!r}",
        "改成缩进的键值对结构；或整节删掉（用默认值）"))
    return {}


def validate_config(cfg: Optional[dict]) -> list[ConfigIssue]:
    """顶层配置校验（FR-10.1）：``plot.jacs`` 全键 + 既有各节的类型/范围。

    只做**能给出明确建议**的检查，不追求穷举；任何情况下不抛异常。
    """
    issues: list[ConfigIssue] = []
    if not isinstance(cfg, dict):
        return [ConfigIssue("ERROR", "<root>", "配置顶层不是映射", "应为键值对")]
    try:
        return _validate_config_inner(cfg, issues)
    except Exception as err:                            # noqa: BLE001 —— 文档承诺不抛
        # 走到这里说明校验器自身有 bug（不是配置的问题）。**仍然不抛**，
        # 把异常变成一条 ERROR 让用户能反馈——重启崩溃比"校验失败"严重得多。
        from .plotstyle import ConfigIssue as _CI  # 幂等（本模块自身）
        issues.append(_CI("ERROR", "<validate>",
                          f"配置校验自身异常：{type(err).__name__}: {err}",
                          "请连同 config.yaml 一起反馈；本次校验已中止"))
        return issues


def _validate_config_inner(cfg: dict, issues: list[ConfigIssue]) -> list[ConfigIssue]:
    """``validate_config`` 的本体（拆出来只为让外层能整体 try/except）。"""
    plot = cfg.get("plot")
    if isinstance(plot, dict):
        issues += validate_jacs(plot.get("jacs"))
        style = plot.get("style")
        if style not in ("jacs", "legacy"):
            issues.append(ConfigIssue("ERROR", "plot.style",
                                      f"未知风格 {style!r}",
                                      '只能是 "jacs" 或 "legacy"'))
    else:
        issues.append(ConfigIssue("ERROR", "plot", "缺少 plot 节",
                                  "删除该节或写成映射"))
    wl = _safe_section(cfg, "wavelength", issues)
    probe = wl.get("probe")
    if not isinstance(probe, (int, float)) or isinstance(probe, bool):
        issues.append(ConfigIssue("ERROR", "wavelength.probe",
                                  f"探针波长不是数值：{probe!r}",
                                  "建议 379.0（ABDA）；换探针体系时改这里"))
    elif not (150.0 <= float(probe) <= 1200.0):
        issues.append(ConfigIssue("WARNING", "wavelength.probe",
                                  f"探针波长 {probe} 超出常见仪器范围",
                                  "确认是否写错单位（应为 nm）"))
    win = wl.get("probe_window")
    if isinstance(win, (int, float)) and not isinstance(win, bool) and win < 0:
        issues.append(ConfigIssue("ERROR", "wavelength.probe_window",
                                  "窗口不能为负", "建议 2.0"))
    k = _safe_section(cfg, "kinetics", issues)
    rmin, rmax = k.get("r_min"), k.get("r_max")
    if isinstance(rmin, (int, float)) and isinstance(rmax, (int, float)) and \
            rmin >= rmax:
        issues.append(ConfigIssue("ERROR", "kinetics.r_min",
                                  f"拟合下限 {rmin} 不小于上限 {rmax}",
                                  "下限应小于上限（默认 0.30 < 1.00）"))
    frac = k.get("truncate_frac")
    if isinstance(frac, (int, float)) and not (0.1 <= float(frac) <= 1.0):
        issues.append(ConfigIssue("WARNING", "kinetics.truncate_frac",
                                  f"截断比例 {frac} 超出 (0.1, 1.0]",
                                  "建议 0.70"))
    qy = _safe_section(cfg, "quantum_yield", issues)
    pref = qy.get("phi_ref")
    if isinstance(pref, (int, float)) and not (0.0 < float(pref) <= 1.0):
        issues.append(ConfigIssue("WARNING", "quantum_yield.phi_ref",
                                  f"参比量子产率 {pref} 不在 (0, 1]",
                                  "ABDA 文献值 0.75"))
    dye = _safe_section(cfg, "dye", issues)
    scheme = dye.get("dir_scheme")
    if scheme is not None and scheme not in ("dye", "flat"):
        issues.append(ConfigIssue("ERROR", "dye.dir_scheme",
                                  f"未知归档方案 {scheme!r}",
                                  '只能是 "dye" 或 "flat"'))
    issues += _validate_v13_sections(cfg)
    return issues


def _validate_v13_sections(cfg: dict) -> list[ConfigIssue]:
    """V1.3 四节（P5/P6/P7/P8）的类型/范围校验。

    **必须容错**：这四节在旧 ``config.yaml`` 里并不存在（深合并会补默认值），
    即使用户手写也允许缺键。任何异常都不得冒出——校验器本身坏掉比配置坏掉更糟。
    """
    issues: list[ConfigIssue] = []

    def _num(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    # —— P7 任意波长表 ——
    wt = _safe_section(cfg, "wavelength_table", issues)
    wls = wt.get("wavelengths")
    if not isinstance(wls, list) or not wls:
        issues.append(ConfigIssue("WARNING", "wavelength_table.wavelengths",
                                  "波长列表为空，A@λ 表将没有列",
                                  "至少给一个波长，如 [400, 500, 600]"))
    elif not all(_num(x) for x in wls):
        issues.append(ConfigIssue("ERROR", "wavelength_table.wavelengths",
                                  "波长列表里含非数值项",
                                  "应为数字列表，单位为 nm"))
    else:
        bad = [x for x in wls if not (10.0 <= float(x) <= 100000.0)]
        if bad:
            issues.append(ConfigIssue("WARNING", "wavelength_table.wavelengths",
                                      f"这几个波长不像 nm 量级：{bad[:5]}",
                                      "常规紫外可见在 200–900 nm"))
    tol = wt.get("tol_nm")
    if _num(tol) and float(tol) < 0:
        issues.append(ConfigIssue("ERROR", "wavelength_table.tol_nm",
                                  "容差不能为负", "建议 0.5 nm"))

    # —— P5 Excel 导出 ——
    ex = _safe_section(cfg, "excel", issues)
    sh = ex.get("sheets")
    if sh is not None and not isinstance(sh, dict):
        issues.append(ConfigIssue("ERROR", "excel.sheets",
                                  "sheets 应为「表名: 是否生成」的映射",
                                  "例如 summary: true"))
    elif isinstance(sh, dict) and not any(bool(v) for v in sh.values()):
        issues.append(ConfigIssue("WARNING", "excel.sheets",
                                  "所有工作表都被关掉了，导出的工作簿会是空的",
                                  "至少打开 summary"))
    dec = ex.get("decimals")
    if _num(dec) and not (0 <= int(dec) <= 15):
        issues.append(ConfigIssue("WARNING", "excel.decimals",
                                  f"小数位 {dec} 不合理", "建议 6"))

    # —— P6 ε 计算器 ——
    et = cfg.get("extinction") or {}
    lp = et.get("path_length_cm")
    if _num(lp) and float(lp) <= 0:
        issues.append(ConfigIssue("ERROR", "extinction.path_length_cm",
                                  f"光程必须为正：{lp}", "比色皿常规 1.0 cm"))
    conc = et.get("concentrations")
    if conc is not None and not isinstance(conc, list):
        issues.append(ConfigIssue("ERROR", "extinction.concentrations",
                                  "浓度应为列表（与光谱顺序对应）",
                                  "例如 [1, 2, 5, 10]"))
    elif isinstance(conc, list) and conc and not all(_num(x) for x in conc):
        issues.append(ConfigIssue("ERROR", "extinction.concentrations",
                                  "浓度列表里含非数值项", "应为数字列表"))
    rng = et.get("lambda_max_range")
    if isinstance(rng, (list, tuple)) and len(rng) == 2 and \
            all(_num(x) for x in rng) and float(rng[0]) >= float(rng[1]):
        issues.append(ConfigIssue("ERROR", "extinction.lambda_max_range",
                                  "λmax 搜索窗下限不小于上限",
                                  "例如 [300, 800]"))

    # —— P8 QC 仪表盘 ——
    qd = cfg.get("qc_dashboard") or {}
    for key, lo, hi in (("sat_frac_warn", 0.0, 1.0),
                        ("nan_frac_warn", 0.0, 1.0)):
        v = qd.get(key)
        if _num(v) and not (lo <= float(v) <= hi):
            issues.append(ConfigIssue("WARNING", f"qc_dashboard.{key}",
                                      f"占比阈值 {v} 不在 [0, 1]",
                                      "0.05 表示 5%"))
    if _num(qd.get("max_tiles")) and int(qd["max_tiles"]) < 1:
        issues.append(ConfigIssue("ERROR", "qc_dashboard.max_tiles",
                                  "至少要画 1 条谱", "建议 60"))
    return issues


# ═══════════════════════════ 预设（FR-8） ═══════════════════════════
def _scale_heights(factor: float) -> dict:
    """按系数缩放逐图高度（附录 C：预设落地为具体数值）。"""
    base = JACS_SCHEMA["fig_heights"]["default"]
    out: dict = {}
    for key, val in base.items():
        out[key] = None if val is None else round(float(val) * factor, 3)
    return out


#: 内置预设（只读）。每项 = 对 ``plot.jacs.*`` 的覆盖值。
BUILTIN_PRESETS: dict[str, dict] = {
    "jacs-single": {
        "label": "JACS 投稿（单栏，默认）",
        "desc": "82.5 mm 单栏、600 dpi、png——等于 V1.0 的默认外观（不再出 PDF）。",
        "values": {},
    },
    "jacs-double": {
        "label": "JACS 投稿（双栏 7 in）",
        "desc": "双栏宽 178 mm，图高按 1.15 倍放大；字号/线宽保持期刊 pt 不变。",
        "values": {"width_in": 7.0,
                   "height_in": round(2.5 * 1.15, 3),
                   "fig_heights": _scale_heights(1.15)},
    },
    "lab-ppt": {
        "label": "组会投屏（大字号）",
        "desc": "6.5 in 宽、300 dpi、仅 png，字号放大到 11–12 pt，k/R² 直接放回图内。",
        "values": {"width_in": 6.5,
                   "height_in": round(2.5 * 1.3, 3),
                   "fig_heights": _scale_heights(1.3),
                   "dpi": 300, "formats": ["png"],
                   "label_size": 12.0, "tick_size": 11.0,
                   "annot_size": 11.0, "legend_size": 10.0,
                   "curve_lw": 2.0, "spine_width": 1.2,
                   "show_fit_stats_in_panel": True},
    },
    "preview-fast": {
        "label": "快速看图（150 dpi）",
        "desc": "只出低分辨率 png、不写图注——只想快速看一眼数据时用。",
        "values": {"dpi": 150, "formats": ["png"], "captions": False},
    },
}


def builtin_preset_names() -> list[str]:
    """内置预设名（顺序固定）。"""
    return list(BUILTIN_PRESETS)


def preset_overrides(name: str) -> Optional[dict]:
    """取某预设对 ``plot.jacs.*`` 的覆盖值（深拷贝）；未知预设返回 None。"""
    p = BUILTIN_PRESETS.get(name)
    if p is None:
        return None
    return copy.deepcopy(p["values"])


def apply_preset(cfg: dict, name: str) -> dict:
    """把预设套用到 ``cfg['plot']['jacs']``（就地修改并返回 cfg）。

    预设**只写** ``plot.jacs.*``，绝不触碰数值计算键（FR-8.4）。
    """
    ov = preset_overrides(name)
    if ov is None:
        raise KeyError(f"未知预设：{name}")
    j = cfg.setdefault("plot", {}).setdefault("jacs", {})
    # 先回到默认，再套覆盖（避免上一个预设的残留）
    j.update(jacs_defaults())
    j.update(ov)
    # M12：预设名记在**下划线私有键**里。原来写的是 `j["preset"]`，而 `preset`
    # 不在 JACS_SCHEMA 里 → 自家校验会把自家写的键报成"未知键"，日志噪音掩盖
    # 用户真正的手误。私有键（`_` 开头）同时也被 jcfg 的"未知键告警"跳过。
    j["_preset"] = name
    return cfg


# —— 用户预设 / 分析模板（presets.yaml，与 config.yaml 分离；V2.4 FR-26） ——
#: 预设文件格式版本：v2 = ``{schema: 2, plot: {名: …}, analysis: {名: …}}``。
#: 读侧兼容 v1 的**两种实际形态**（历史实现写的是 ``presets:`` 包裹；设计文档
#: §FR-26.1 描述的是"顶层直接是预设名"）——两种都按 plot 预设解释，旧文件零迁移。
PRESETS_SCHEMA_VERSION: int = 2

#: 分析模板的作用域（设计文档 Q-A2b 决议：只这四节；controls/autogroup/background
#: 不纳入——分组与对照语义模板化的风险高于四参数节）。
ANALYSIS_PRESET_SECTIONS: tuple[str, ...] = ("qc", "baseline", "kinetics",
                                             "quantum_yield")

_MISSING = object()


def presets_path() -> Path:
    """用户预设文件位置（exe 同级 / 项目根）。

    ``DYE_TOOL_PRESETS`` 环境变量可覆盖（测试隔离 / 便携安装用；不改变默认位置）。
    """
    import os
    env = os.environ.get("DYE_TOOL_PRESETS")
    if env:
        return Path(env)
    from ..paths import app_root
    return Path(app_root()) / "presets.yaml"


PRESET_SECTIONS: tuple[str, ...] = ("plot", "analysis", "stack", "spectra")


def _read_presets_sections(path: Optional[Path | str] = None) -> tuple[int, dict]:
    """读 presets.yaml -> (schema, {section: {名: 条目}})；V2.8 FR-53 扩 stack/spectra。

    读坏/缺失一律返回 (0, {}) + WARNING（不抛，Q9 契约）；v1 兼容同旧实现。
    """
    p = Path(path) if path is not None else presets_path()
    if not p.exists():
        return 0, {}
    try:
        import yaml
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as err:                           # noqa: BLE001
        log.warning("读取用户预设失败 %s: %s（忽略）", p, err)
        return 0, {}
    if not isinstance(data, dict):
        log.warning("预设文件顶层不是映射，已忽略：%s", p)
        return 0, {}
    schema = data.get("schema")
    if isinstance(schema, int) and schema >= PRESETS_SCHEMA_VERSION:
        sections = {}
        for sec in PRESET_SECTIONS:
            block = data.get(sec)
            if isinstance(block, dict):
                sections[sec] = block
        if "plot" not in sections:
            legacy = data.get("presets")
            sections["plot"] = legacy if isinstance(legacy, dict) else {}
        return schema, sections
    if isinstance(data.get("presets"), dict):
        return 1, {"plot": data["presets"]}
    return 1, {"plot": {k: v for k, v in data.items() if k != "schema"}}


def _read_presets_doc(path: Optional[Path | str] = None
                      ) -> tuple[int, dict, dict]:
    """旧三返回值封装（plot/analysis）；新代码请用 _read_presets_sections。"""
    schema, sections = _read_presets_sections(path)
    return schema, dict(sections.get("plot") or {}), dict(sections.get("analysis") or {})


def _write_presets_sections(sections: dict,
                            path: Optional[Path | str] = None) -> Path:
    """写 v2 分节文件（plot/analysis/stack/spectra 有则写）；原子写、中文注释。"""
    import yaml
    p = Path(path) if path is not None else presets_path()
    doc = {"schema": PRESETS_SCHEMA_VERSION}
    for sec in PRESET_SECTIONS:
        block = sections.get(sec)
        if isinstance(block, dict) and block:
            doc[sec] = {k: block[k] for k in sorted(block)}
        elif sec in ("plot", "analysis"):
            doc[sec] = {}
    text = ("# dye_tool 用户预设（v2：plot=出图 / analysis=分析模板 / "
            "stack=③堆叠图 / spectra=④光谱图显示预设）\n"
            "# 手改安全：未知键在套用时会被忽略并告警；schema 供将来演化分流。\n"
            + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False,
                             default_flow_style=False))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    log.info("已写出预设文件（schema=%d）：%s", PRESETS_SCHEMA_VERSION, p)
    return p


def _write_presets_doc(plot: dict, analysis: dict,
                       path: Optional[Path | str] = None) -> Path:
    """旧三参数封装：保留已有 stack/spectra 节，只替换 plot/analysis。"""
    _schema, sections = _read_presets_sections(path)
    sections["plot"] = dict(plot or {})
    sections["analysis"] = dict(analysis or {})
    return _write_presets_sections(sections, path)

def load_user_presets(path: Optional[Path | str] = None) -> dict:
    """读**用户 plot 预设**节（v1/v2 均可）；返回 ``{名: 条目}``。"""
    _schema, plot, _analysis = _read_presets_doc(path)
    return plot


def _plot_entry_values(entry: Any) -> dict:
    """取一条 plot 预设的差量：兼容 ``{values: {...}}`` 包裹与裸差量。"""
    if not isinstance(entry, dict):
        return {}
    vals = entry.get("values")
    if isinstance(vals, dict):
        return copy.deepcopy(vals)
    return copy.deepcopy(entry)


#: V2.9.3 FR-66：预设文件里允许的会话图式键（不是 config.yaml 键）
FIGURE_PRESET_KEYS = ("figure_style", "figure_error_bars",
                      "figure_transparent", "figure_annotate",
                      "figure_time_unit")


def _plot_entry(values: dict) -> dict:
    """写侧条目：沿用历史 ``{values: {仅 JACS_SCHEMA 键}}`` 形态（旧文件原样往返）。

    ⚠ 与设计文档 §FR-26.1 示例的差异：书里画的是裸差量简写；实际 v1 文件一直是
    ``values:`` 包裹，保留它才能"旧文件读→写→读回"字节语义等价（A26.2）。
    """
    return {"values": {k: v for k, v in (values or {}).items()
                       if k in JACS_SCHEMA or k in FIGURE_PRESET_KEYS}}


def save_user_preset(name: str, values: dict,
                     path: Optional[Path | str] = None) -> Path:
    """写出/覆盖一个用户出图预设（只保留 ``plot.jacs.*`` 键）。"""
    _schema, plot, analysis = _read_presets_doc(path)
    plot[str(name)] = _plot_entry(values)
    return _write_presets_doc(plot, analysis, path)


def delete_user_preset(name: str, path: Optional[Path | str] = None) -> bool:
    """删除用户出图预设；返回是否真的删掉了。内置预设不可删。"""
    _schema, plot, analysis = _read_presets_doc(path)
    if name not in plot:
        return False
    plot.pop(name, None)
    _write_presets_doc(plot, analysis, path)
    return True


def list_presets(path: Optional[Path | str] = None) -> list[tuple]:
    """``[(name, label, is_builtin)]``，内置在前、用户在后。"""
    rows = [(n, BUILTIN_PRESETS[n]["label"], True) for n in builtin_preset_names()]
    for n in sorted(load_user_presets(path)):
        rows.append((n, f"{n}（用户）", False))
    return rows


def load_display_presets(section: str,
                         path: Optional[Path | str] = None) -> dict:
    """读 ③stack / ④spectra 显示预设：{名: 差量 dict}（坏节→{}）。"""
    if section not in ("stack", "spectra"):
        raise ValueError(f"未知预设节 {section!r}（只允许 stack/spectra）")
    _schema, sections = _read_presets_sections(path)
    block = sections.get(section)
    return {k: v for k, v in block.items() if isinstance(v, dict)} if isinstance(block, dict) else {}


def load_display_preset(section: str, name: str,
                        path: Optional[Path | str] = None) -> Optional[dict]:
    vals = load_display_presets(section, path).get(str(name))
    return dict(vals) if isinstance(vals, dict) else None


def save_display_preset(section: str, name: str, values: dict,
                        path: Optional[Path | str] = None) -> Path:
    """保存/覆盖一个 ③④ 显示预设（只存差量 dict；其他节原样保留）。"""
    if section not in ("stack", "spectra"):
        raise ValueError(f"未知预设节 {section!r}（只允许 stack/spectra）")
    _schema, sections = _read_presets_sections(path)
    block = sections.get(section) if isinstance(sections.get(section), dict) else {}
    block = dict(block)
    block[str(name)] = dict(values or {})
    sections[section] = block
    return _write_presets_sections(sections, path)


def delete_display_preset(section: str, name: str,
                          path: Optional[Path | str] = None) -> bool:
    if section not in ("stack", "spectra"):
        raise ValueError(f"未知预设节 {section!r}（只允许 stack/spectra）")
    _schema, sections = _read_presets_sections(path)
    block = sections.get(section)
    if not isinstance(block, dict) or str(name) not in block:
        return False
    block = dict(block)
    block.pop(str(name), None)
    sections[section] = block
    _write_presets_sections(sections, path)
    return True


def list_display_presets(section: str,
                         path: Optional[Path | str] = None) -> list[str]:
    return sorted(load_display_presets(section, path))


def deep_merge_preset(base: dict, delta: dict) -> dict:
    """差量 deep_merge（不改入参；未知键原样保留，由调用方决定忽略）。"""
    import copy as _copy
    out = _copy.deepcopy(base or {})
    for k, v in (delta or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge_preset(out[k], v)
        else:
            out[k] = _copy.deepcopy(v)
    return out

def load_preset(name: str, path: Optional[Path | str] = None) -> Optional[dict]:
    """取出图预设的覆盖值（先内置、后用户）；未知返回 None。"""
    ov = preset_overrides(name)
    if ov is not None:
        return ov
    user = load_user_presets(path).get(name)
    if isinstance(user, dict):
        return _plot_entry_values(user)
    return None


# ═══════════════════════ 分析参数模板（V2.4 FR-26.2/26.3） ═══════════════════════
def _same_value(a: Any, b: Any) -> bool:
    """值相等判定（bool 与 1/0 不混同；数值按值比）。"""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        try:
            return float(a) == float(b)
        except (TypeError, ValueError):
            return False
    try:
        return bool(a == b)
    except Exception:                                  # noqa: BLE001
        return False


def _diff_vs_default(node: Any, default: Any) -> Any:
    """递归求"与默认值不同"的部分；全同返回 None。

    dict：逐键递归；默认里没有的键整体收录；list/标量：值不同才收录。
    """
    if isinstance(node, dict):
        if not isinstance(default, dict):
            return copy.deepcopy(node) if node else None
        out: dict = {}
        for k, v in node.items():
            d = default.get(k, _MISSING)
            if d is _MISSING:
                if v not in ({}, [], None) or _MISSING is d:
                    out[k] = copy.deepcopy(v)
                continue
            sub = _diff_vs_default(v, d)
            if sub is not None:
                out[k] = sub
        return out or None
    if isinstance(node, dict):
        return None
    if _same_value(node, default):
        return None
    return copy.deepcopy(node)


def analysis_preset_diff(cfg: dict,
                         sections: tuple = ANALYSIS_PRESET_SECTIONS) -> dict:
    """把 cfg 的四节压成**差量模板**：只保留与默认值不同的键（A26.5）。

    另存模板时调用。**剔除等于默认值的键**是差量语义的必然推论：否则模板会把
    当前版本的默认值固化，将来 ``DEFAULT_CFG`` 演进后又被旧模板悄悄钉死。
    """
    from ..config import section_defaults

    out: dict = {}
    for sec in sections:
        node = (cfg or {}).get(sec)
        if not isinstance(node, dict):
            continue
        diff = _diff_vs_default(node, section_defaults(sec))
        if diff:
            out[sec] = diff
    return out


def _prune_unknown(diff: dict) -> tuple[dict, list]:
    """模板作用域未知键剔除 + WARNING（FR-26.3①；防手改 presets.yaml 注入死键）。"""
    from ..config import unknown_key_issues

    sections = {k: v for k, v in (diff or {}).items() if k in ANALYSIS_PRESET_SECTIONS}
    issues: list = []
    for k in sorted(set(diff or {}) - set(sections)):
        issues.append(ConfigIssue(
            "WARNING", str(k),
            f"模板节 {k!r} 不在分析模板作用域内（仅 "
            f"{'/'.join(ANALYSIS_PRESET_SECTIONS)}），已忽略",
            "把该键从 presets.yaml 模板里删掉，或确认是否应改到别处"))
    if not sections:
        return {}, issues
    unknown = unknown_key_issues(sections, limit=1000)
    bad = {i.key for i in unknown}
    if not bad:
        return sections, issues
    issues.extend(unknown)

    def _prune(node: dict, prefix: str) -> dict:
        out: dict = {}
        for k, v in node.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if path in bad:
                continue
            if isinstance(v, dict):
                sub = _prune(v, path)
                if sub:
                    out[k] = sub
            else:
                out[k] = v
        return out

    return _prune(sections, ""), issues


def apply_analysis_preset_diff(cfg: dict, diff: dict) -> tuple[dict, list]:
    """把模板差量套进 cfg：未知键剔除 → ``deep_merge``（None 语义沿用配置层规则）。

    Returns:
        ``(新 cfg, issues)``；**不原地修改**传入 cfg（深拷贝）。
    """
    from ..config import deep_merge

    issues: list = []
    pruned, drop_issues = _prune_unknown(diff)
    issues.extend(drop_issues)
    new = copy.deepcopy(cfg or {})
    deep_merge(new, pruned, issues=issues)
    return new, issues


def list_analysis_presets(path: Optional[Path | str] = None) -> list[str]:
    """用户分析模板名（按名排序）。"""
    return sorted(_read_presets_doc(path)[2])


def load_analysis_preset(name: str, path: Optional[Path | str] = None
                         ) -> Optional[dict]:
    """读一个分析模板的差量；不存在返回 None。"""
    analysis = _read_presets_doc(path)[2]
    entry = analysis.get(str(name))
    if not isinstance(entry, dict):
        return None
    vals = entry.get("values")
    return copy.deepcopy(vals if isinstance(vals, dict) else entry)


def save_analysis_preset(name: str, values: dict,
                         path: Optional[Path | str] = None) -> Path:
    """写出/覆盖一个分析模板（只收四节；空名/空差量/越界 → ValueError）。"""
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("模板名不能为空")
    pruned: dict = {}
    for sec in ANALYSIS_PRESET_SECTIONS:
        node = (values or {}).get(sec)
        if isinstance(node, dict) and node:
            pruned[sec] = copy.deepcopy(node)
    if not pruned:
        raise ValueError("模板内容为空：只接受 "
                         f"{'/'.join(ANALYSIS_PRESET_SECTIONS)} 四节的差量")
    _schema, plot, analysis = _read_presets_doc(path)
    analysis[nm] = pruned
    _write_presets_doc(plot, analysis, path)
    log.info("已保存分析模板: %s -> %s", nm, path or presets_path())
    return Path(path) if path is not None else presets_path()


def delete_analysis_preset(name: str, path: Optional[Path | str] = None) -> bool:
    """删除一个分析模板；不存在返回 False。"""
    _schema, plot, analysis = _read_presets_doc(path)
    if name not in analysis:
        return False
    analysis.pop(name, None)
    _write_presets_doc(plot, analysis, path)
    return True


def apply_analysis_preset(cfg: dict, name: str,
                          path: Optional[Path | str] = None
                          ) -> tuple[dict, list, bool]:
    """套用命名模板 → ``(新 cfg, issues, 是否套用)``。

    模板缺失/不可解析 → 一条 WARNING，cfg **原样返回**、``applied=False``（A26.4）。
    """
    diff = load_analysis_preset(name, path)
    if diff is None:
        return cfg, [ConfigIssue(
            "WARNING", f"analysis_preset:{name}",
            f"分析模板不存在或不可解析：{name}（本次按当前参数继续）",
            f"检查 {Path(path) if path is not None else presets_path()} 里的 "
            "analysis 节，或从①数据页另存一份")], False
    new, issues = apply_analysis_preset_diff(cfg, diff)
    return new, issues, True


def apply_batch_analysis_preset(cfg: dict, batch: Any,
                                path: Optional[Path | str] = None
                                ) -> tuple[dict, list, bool]:
    """批次文件里的 ``analysis_preset`` → 套用（GUI/CLI 共用的唯一入口）。"""
    name = str(getattr(batch, "analysis_preset", "") or "").strip()
    if not name or batch is None:
        return cfg, [], False
    return apply_analysis_preset(cfg, name, path)
