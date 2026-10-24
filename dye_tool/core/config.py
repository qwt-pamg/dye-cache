# -*- coding: utf-8 -*-
"""集中配置层。

设计要点（对应设计文档 §1.2 第 3 条）：
    * 所有魔法数字集中在 :data:`DEFAULT_CFG`，GUI 只暴露常用参数子集；
    * 用户级 ``config.yaml`` 与默认配置 **深合并**（dict 递归、其余整体覆盖）；
    * ``core`` 层不读全局状态，cfg 以 dict 贯穿整个调用链。

本模块从 legacy ``singlet_o2_phi.DEFAULT_CFG`` 迁移，数值键名与默认值
**逐项保持一致**，仅在末尾以 ``spectra`` / ``autogroup`` / ``gui`` 三节
做**纯新增**扩展（FR-2、FR-4、FR-5 需要），不影响既有数值行为。
"""
from __future__ import annotations

import copy
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .render.plotstyle import (ConfigIssue, jacs_defaults,  # noqa: F401  (re-export)
                               suggest_key, validate_config)

log = logging.getLogger("dye_tool.config")

# ═══════════════════════════ 默认配置（唯一魔法数字来源） ═══════════════════════════
DEFAULT_CFG: dict = {
    "io": {
        "extensions": [".txt", ".csv"],
        "recursive": True,
        "exclude": ["_"],
        # G5（第二轮审查）：原来这里有个 `"encoding": "gbk"` —— **死键**。
        # 读侧是自动探测编码（read_text_robust，带 GBK/UTF-8/BOM 回退），
        # 全项目**没有任何消费者**读它，但 save_config 会把它写回 config.yaml，
        # 用户看到就以为"这行能控制读码"。已从这个"唯一魔法数字来源"里删除。
        # 用户在 yaml 里自己写的 `io.encoding` 仍会被 deep_merge 保留（不报错），
        # 只是照旧不生效——真要支持指定编码，得在读侧接上，别只留个键。
        "separator": ",",
        "ncols": 2,
        # —— 读盘性能（V1.2）：解析只占 core 约 10%，收益有限，故阈值定得高，
        #    小批量（常见场景）绝不会因线程开销反而变慢 ——
        "parallel": True,
        "parallel_min_files": 64,     # 达到该文件数才启用线程池
        "parallel_workers": 0,        # 0 = 自动（CPU 核数，上限 8）
        # —— 荧光发射谱（荧光）解析用；纯新增 ——
        "emission_marker": "Data Points",
        "emission_wl_range": [150.0, 1200.0],
        # —— V1.3 FR-13：仪器 荧光 / vendor metadata 的 .FDS 原生二进制存档 ——
        # 判别靠**内容**（文件头 8 字节魔术字 IIHIDTAG），扩展名只是"要不要扫它"。
        # enable=True 时 scan_extensions() 会把 extensions 与该列表**取并集**，
        # 这样既有的 config.yaml（写死了 [.txt,.csv]）无需改动即可识别 .fds。
        "fds": {
            "enable": True,
            "extensions": [".fds"],
            # 数据段起点锚点（本批样机的序列号）。换仪器后头部字符串长度可能不同，
            # 解析器在锚点失效时会自动回退到"型号串"路径；这里留空则用内置默认值。
            "serial_anchor": "",
        },
        # —— V2.0 FR-17：SPC / UV-2400PC·UV-2450 的 .spc 原生存档（OLE2）——
        # 判别同样靠**内容**（OLE2 魔术字 + 目录里有 X Data 流），扩展名只决定
        # "要不要扫它"。与 fds 一样做成独立开关：enable=True 时 scan_extensions()
        # 会把 extensions 并进集合，既有 config.yaml（写死 [.txt,.csv]）无需改动。
        "spc": {
            "enable": True,
            "extensions": [".spc"],
        },
        # —— V2.6 FR-35：Excel 工作簿（.xlsx）导入（路线 C1）——
        # 默认**关**：既有用户行为逐位不变（不枚举 .xlsx、不解析）。打开后
        # scan_extensions() 并入 .xlsx，_read_scan_ex 在文本分支前按
        # "后缀 + ZIP 魔数 + xl/workbook.xml" 识别并取首 sheet。
        # 不重写 config.yaml（DS1 默认值机制会自动补键）。
        "excel": {
            "enable": False,
            "extensions": [".xlsx"],
        },
    },
    "filename": {
        "time_pattern": r"^(?P<stem>.+)-(?P<sec>\d+)(?:\+(?P<min>\d+))?$",
        "group_pattern": r"^(?P<group>.+?)(?:_(?P<rep>\d+))?$",
        "no_time_suffix": "zero",            # zero→t=0 | background→背景文件
        "roles": {"bg": "background", "blank": "blank", "dark": "dark",
                  "ref": "reference", "n2": "n2", "nan3": "quencher",
                  "azide": "quencher"},
    },
    "wavelength": {"probe": 379.0, "probe_window": 2.0, "excitation": 532.0,
                   "align_tol": 0.51},
    "baseline": {"enable": True, "wl_range": [650.0, 700.0], "method": "median",
                 "warn_above": 0.01},
    "background": {"enable": True, "mode": "matched"},   # matched=按时间最近扣
    "a0": {"dark_max_time": 0.0, "n_early": 2},          # 无暗态→最早2点均值
    "kinetics": {"r_min": 0.30, "r_max": 1.00, "r_max_tol": 0.05, "a_floor": 0.01,
                 "auto_truncate": True, "truncate_frac": 0.70, "curvature_p": 0.05,
                 "dose_normalize": False, "irradiance": {"default": 1.0}},
    "quantum_yield": {"phi_ref": 0.75, "phi_ref_err": 0.075, "iabs_mode": "exact",
                      "a_ex": {}, "a_ex_err": 0.005, "bootstrap": False,
                      "bootstrap_n": 1000, "seed": 42,
                      # 内滤效应提示阈值（V1.2 E2）：λ_ex 处 A 超过它就在日志里
                      # 提醒"荧光不再正比于浓度/产率"。设 0 = 关闭提示。
                      "ifc_warn_above": 0.10},
    "qc": {"a0_range": [0.05, 1.5], "saturation": 3.0, "flag_negative": True,
           # V2.0 γ 批（FR-15）：**荧光原始强度**的饱和阈值。`.fds`（仪器 荧光）
           # 存的是仪器原始强度（10²–10⁴），拿吸光度阈值 3.0 去判会把**每一条谱
           # 整条标成饱和**（实测 3/3 夹具、100% 点被标）。默认 1e9 ≈ 关闭。
           # 只作用于 source=="fds"；吸光度路径（txt/csv/spc）的判据与默认值**一字不改**。
           # "按 FDS 元信息量程智能判定"留到判据确认后（尾部 PMT 语义未确认，Q3）。
           "saturation_counts": 1e9,
           "outlier": {"enable": True, "window": 7, "k_mad": 6.0, "abs_floor": 0.05},
           "dark_k_rel_max": 0.05, "r2_warn": 0.98},
    "controls": {"enable": True, "n2_inhibition_min": 0.70,
                 "quencher_inhibition_min": 0.70},
    # 绘图层：``style`` 决定出图风格。
    #   jacs   —— JACS 期刊投稿风格（单栏 3.25 in / Arial / 刻度朝内 / 四边完整框 /
    #             ≥600 dpi / png），由 core/plotting_jacs.py 渲染（默认）
    #   legacy —— 迁移前的老风格（8.5×5.5 in / 朝外刻度 / 仅左下轴 / 300 dpi /
    #             svg+png），与 legacy 脚本逐像素一致，供 A4.2 回归比对使用
    #             （该路径**冻结**，其 png+pdf 输出保持不变，仅回归时走到）
    "plot": {
        "style": "jacs",
        # —— legacy 风格参数（保留，勿改：改了 A4.2 的逐像素回归会失真）——
        "dpi": 300,
        "formats": ["svg", "png"],
        # —— JACS 风格参数 ——
        # 键位真相在 core/plotstyle.JACS_SCHEMA（V1.1 FR-7：硬编码全部键化）。
        # 新增键只改 plotstyle，这里自动跟随。
        "jacs": jacs_defaults(),
        "spectra_overlay": {"select_times": "all", "cmap": "viridis",
                            "mark_probe": True, "mark_excitation": True,
                            "mark_fluorophore": "auto",
                            "fluorophore_search": [540.0, 620.0]},
    },
    # ══════════════ 以下三节为本次迁移纯新增（legacy 无对应键） ══════════════
    # FR-2 自动分组（等价 legacy compare_kobs.AG_DEFAULT，键名保持一致）
    "autogroup": {"core_lo": 450.0, "core_hi": 650.0, "step": 5.0, "d_max": 0.06},
    # FR-4 紫外/荧光常规作图
    "spectra": {
        "normalize": "max",                  # max | none
        "abs_color": "#1f4e79",
        "em_color": "#C81E1E",
        "export_formats": ["png", "svg"],
        "emission_wl_range": [300.0, 900.0],
        "auto_pair_keywords": ["pbs", "dcm", "dmso", "etoh", "ch3cn"],
    },
    # FR-5 GUI 会话态（不参与数值计算）
    "gui": {"recent_dirs": [], "max_recent": 8, "out_root": "runs",
            "show_welcome": True,        # FR-9.1 欢迎卡片（可"不再显示"）
            "lazy_preview": True,        # P1 预览懒渲染：首批只画首图，其余后台补渲
            # V2.0 FR-16：导航布局。flow=任务流新导航（默认）| classic=V1.x 平铺五页签
            # （经典模式保留一个版本周期，Q7；该键纳入 DS1 全树校验的 KNOWN_PATHS）。
            # 注意：本键**只有 GUI 读**（ui/main_window.py），CLI/数值层忽略它。
            # V2.3 FR-24.8：窗口几何记忆（0 = 未设置 → 用默认 1560×960）；关窗时写回。
            "window": {"w": 0, "h": 0, "x": 0, "y": 0},
            "nav": "flow",
            # 界面主题：light（默认）| dark | system（跟随系统明暗）。只有 GUI 读（ui/theme.py）。
            "theme": "light"},
    # ══════════════ FR-11 染料命名与档案（V1.1 纯新增） ══════════════
    "dye": {
        "enabled": True,
        "auto_from": "group_first_field",   # 注册表 miss 时的启发式（与 R1 同源）
        "dir_scheme": "dye",                # dye（新默认）| flat（V1.0 兼容）
        "history": True,                    # 写 runs/<染料>/history.csv
        "params_snapshot": True,            # run 目录内写 run_params.yaml
        "name_max_len": 60,
        # —— P9：旧 runs/ 目录染料化整理（dry-run 默认开启，先看再动） ——
        # M7（评审记录）：这里原来还有 `scan_root` 键，**全项目零消费者**
        # （整理对象由 CLI 的 `-o/--out-root` 决定），已删除——死配置比没有配置
        # 更坏：用户设了以为生效。`dry_run` 保留并**真正接线**（见 apply_reorg）。
        "reorg": {
            "dry_run": True,          # true=只出计划不动盘（安全闸，apply_reorg 尊重它）
            "keep_legacy": True,      # 整理后把空壳旧目录移入 _legacy/
            "write_report": True,     # 写 runs/_reorg_report.md
        },
    },
    # ══════════════ P7 任意波长吸光度表（A@λ） ══════════════
    "wavelength_table": {
        # 想查的波长（nm）。默认给一组论文常用值；用户可在 GUI/CLI 覆盖
        "wavelengths": [400.0, 450.0, 500.0, 550.0, 600.0, 650.0, 700.0],
        "tol_nm": 0.5,               # 找最近点的容差；超出记 NaN 并注明
        "interpolate": False,        # false=取最近点，true=线性插值
        "include_groups": [],        # 空=全部组
        "include_dyes": [],          # 空=全部染料
        "filename": "absorbance_at_wavelength.csv",
    },
    # ══════════════ P5 Excel 多 sheet 导出 ══════════════
    "excel": {
        "enabled": True,
        "filename": "dye_tool_结果汇总.xlsx",
        "sheets": {                  # 逐表开关（关掉的表不生成）
            "summary": True,         # 结果总表（每染料/每组一行）
            "qc": True,              # QC 明细（逐文件）
            "kinetics": True,        # 拟合明细（逐点）
            "dyes": True,            # 染料档案 / 历史
            "params": True,          # 参数快照
        },
        "freeze_header": True,       # 冻结首行
        "autofilter": True,          # 首行自动筛选
        "column_width_max": 60,      # 列宽上限（字符）
        "decimals": 6,               # 数值列小数位
    },
    # ══════════════ P6 摩尔消光系数计算器（ε） ══════════════
    "extinction": {
        "wavelength": None,          # None=用每支谱的 λmax；给定值则在该波长取 A
        "lambda_max_range": [300.0, 800.0],   # 自动找 λmax 的搜索窗
        "concentrations": [],        # 与光谱**顺序对应**的浓度（同单位即可）
        "conc_unit": "uM",           # 仅用于标签
        "path_length_cm": 1.0,       # 光程 l（cm）
        "force_origin": True,        # 过原点拟合（Beer–Lambert 理论要求）
        "weighted": False,           # 是否按 1/A 加权（低吸收点噪声大）
        "r2_warn": 0.995,            # 低于此值记 WARNING
        "dev_warn_frac": 0.15,       # 单点与拟合线相对偏差超此比例记 WARNING
        "filename": "extinction.csv",
    },
    # ══════════════ P8 QC 体检仪表盘 ══════════════
    "qc_dashboard": {
        "enable": True,
        "sat_frac_warn": 0.05,       # 饱和点占比超此值 → 该文件标红
        "nan_frac_warn": 0.05,       # 无效点占比阈值
        "drift_warn": 0.05,          # 基线区漂移（AU）阈值
        "wl_mismatch_tol": 0.05,     # 波长轴不一致判定容差（nm）
        "max_tiles": 60,             # 单页最多画几条谱（防卡死）
        "fig_height_in": 6.5,
        "export": "png",
    },
}

#: FR-2 无用户配置时的分组默认值（与 legacy ``AG_DEFAULT`` 一致）
AG_DEFAULT: dict = {"core_lo": 450.0, "core_hi": 650.0, "step": 5.0, "d_max": 0.06}

# ═══════════════════ DS1｜全树键校验 + schema 版本化（V2.0 FR-18） ═══════════════════
#: 配置文件格式版本。**今天 = 1 且迁移表为空**（V1.0→V1.3 从未发生过键改名，
#: 已盘点的历史键只有 `io.encoding` / `paths.scan_root` / plot.jacs 的
#: `combined_pdf`、`font_cache`——前两个进 :data:`DEAD_KEYS`，后两个在
#: ``plotstyle.REMOVED_KEYS``）。机制先行：将来真要改键名，走 :data:`_MIGRATIONS`。
SCHEMA_VERSION: int = 1

#: 已废除的**非 plot.jacs** 顶层键 → 一句"为什么没效果 / 该怎么做"。
#: 用点分**全路径**（与 ConfigIssue.key 口径一致；plotstyle.REMOVED_KEYS 用裸键名，
#: 是因为它只在自己那棵子树里查）。
DEAD_KEYS: dict[str, str] = {
    "io.encoding": "读入编码为自动探测（read_text_robust，UTF-8/GBK/BOM 回退），"
                   "该键从未有过消费者，删掉即可，读码行为不变",
    "paths.scan_root": "M7 已删除：旧 runs 目录整理的对象由 CLI 的 -o/--out-root "
                       "或 dye.reorg 决定，该键设了也不生效",
    # ── V2.x 审查 O1/B4：9 个"有默认值、有注释、但全仓零读取"的历史键 ──
    # 处理方式：从 DEFAULT_CFG 移除（不再出现在新配置里）+ 在这里给"已废除"文案，
    # 于是**旧配置**写这些键时得到的是"已废除、为什么没效果"，而不是"未知键（拼写错误？）"
    # ——版本演进与手误必须分得开（DS1 三档政策第 ① 档）。
    "io.emission_extensions": "荧光发射谱按内容判别（Data Points 标记 / Scan mode），"
                              "扩展名统一由 io.extensions 决定；该键从未被读取",
    "run.log_level": "日志级别由 DYE_TOOL_DEBUG 环境变量与 --debug 决定；该键从未被读取",
    "gui.embed_preview": "预览嵌入策略固定按懒渲染（gui.lazy_preview）实现，该键无消费者",
    "gui.show_dye_column": "文件清单的染料列**始终显示**（无开关）；该键从未实现",
    "gui.show_time_panel": "预分析整定面板**始终存在**（左栏页签）；该键从未实现",
    "gui.log_level_zh": "界面日志级别始终中文化，该键无消费者",
    "spectra.stokes": "斯托克斯位移标注按谱类型自动决定（spectra.py），该键无消费者",
    "spectra.mirror_axis": "镜像/上下两栏版式由作图台页签与函数参数决定，"
                           "不经配置；该键无消费者",
    "dye.confirm_dialog": "归档确认弹窗从未实现（归档流程是显式菜单动作 + dry-run "
                          "预览）；该键只被枚举校验过，无任何行为消费者",
}

#: 私有键（``_`` 开头）一律跳过校验 —— ``plotstyle.validate_jacs`` 自 M12 起
#: 就是这个口径（``_preset`` 不是手误）。这里同源，避免两套规则打架。
_PRIVATE_PREFIX = "_"

#: **运行时内部键**：GUI/引擎会写进 cfg，但**不在** :data:`DEFAULT_CFG` 里，
#: 因此逃不过全树校验——必须显式放行，否则用户每次保存配置都会收到假告警
#: （违反原则 2"只对用户手写的东西告警"）。
#:
#: * ``gui.last_dir``（``base_page.remember_dir``）：GUI 会话态。**刻意不进
#:   DEFAULT_CFG**——进了就会被 save_config 落盘成机器绝对路径，拷给别人就是错目录。
#: * ``plot.jacs._preset``（``plotstyle.apply_preset``）其实是 ``_`` 私有键，
#:   已被上面的前缀规则覆盖；列在这里是为了"内部键"有一份可读的清单。
#:   **但 ``plot.jacs`` 子树整体交给 validate_jacs，见下。**
_TRANSIENT_KEYS: frozenset[str] = frozenset({
    "gui.last_dir",
    "plot.jacs._preset",
})

#: schema 迁移表：``[(from_version, callable(user_dict) -> None)]``，**初始为空**。
#: 只在**内存**迁移（兼容七原则 6：读入永不改写用户文件），按 ``_meta.schema``
#: 从低到高顺序应用。新增迁移时：① 追加一条；② SCHEMA_VERSION += 1；
#: ③ 补一条"假迁移注册表"测试（设计稿 §1.1 测试 ⑤）。
_MIGRATIONS: list[tuple[int, Any]] = []


def _expand_paths(node: Any, prefix: str = "") -> list[str]:
    """递归展开成点分键路径集合（DS1 的 KNOWN_PATHS）。

    唯一真相就是 :data:`DEFAULT_CFG`——**不另维护一份键清单**（那样必然漂移）。
    口径：dict 继续下钻；空 dict 与一切非 dict 值都算**叶子**（用户写
    ``quantum_yield.a_ex.任意键`` 不算未知——a_ex 是可扩展映射）。

    Returns:
        点分路径列表（含嵌套叶子，如 ``plot.jacs.width_in``）。
    """
    out: list[str] = []
    for k, v in (node or {}).items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out += _expand_paths(v, path)
        else:
            out.append(path)
    return out


#: ``config.yaml`` 的合法键路径全集（DS1）。被 `_unknown_key_issues` 与测试引用。
KNOWN_PATHS: frozenset[str] = frozenset(_expand_paths(DEFAULT_CFG))

#: **可扩展映射**：这些路径的**子键由用户自定义**，不算未知键。
#:
#: 为什么需要（《代码评审记录.md》第3部分·F4，文档-代码失配）：``DEFAULT_CFG`` 里
#: ``quantum_yield.a_ex = {}`` 是空 dict → :func:`_expand_paths` 只记叶子
#: ``quantum_yield.a_ex``；而 :func:`_walk_leaves` 对**用户写的非空** a_ex 会继续
#: 下钻出 ``quantum_yield.a_ex.Resorufin`` 之类路径 → 每次启动一条"未知键"WARNING。
#: 而 :func:`_expand_paths` 的 docstring 早就承诺"a_ex 是可扩展映射"——承诺在文档里、
#: 没在代码里。这里落实成白名单（DS1 的第三类政策：可扩展映射放行）。
#:
#: 新增同类节时：① 加到这里；② 在 ``tests/test_config.py`` 补一条"写子键零告警"。
_EXTENSIBLE_PREFIXES: tuple[str, ...] = ("quantum_yield.a_ex",)


def deep_merge(base: dict, extra: dict, *,
               issues: Optional[list] = None, prefix: str = "") -> dict:
    """把 ``extra`` 深合并进 ``base``（就地修改并返回 base）。

    dict 递归合并；其余类型（含 list）整体覆盖 —— 与 legacy 行为一致，
    避免 list 被逐元素拼接导致的重音语义问题。

    **``None`` 一律不覆盖**（M11 + V2.x 审查 A.M1）：
    yaml 里把键/节留空（写个 ``wavelength:`` 或 ``probe_window:``）会得到 ``None``。
    旧实现只在"基值是 dict"时忽略 None，**叶子键仍被 None 覆盖** → ``cfg`` 里出现
    None，``validate_config`` 不查 None，最后在 ``float(None)``（kinetics.py）处
    **分析中途 TypeError**——配置问题表现为运行期崩溃，最难排查。
    现在：任何 None 都＝保留默认值，并记 WARNING；``issues`` 传了还会进问题清单
    （GUI 启动弹窗能看见"这一行留空没生效"，而不是静默）。

    Args:
        base: 目标配置（就地修改）。
        extra: 用户配置。
        issues: 可选，问题清单；给了就追加一条 WARNING（带点分全路径）。
        prefix: 内部递归用的路径前缀（调用方不要传）。
    """
    for k, v in (extra or {}).items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if v is None:
            # 先分辨"默认值本身就是 None"（合法语义，如 `extinction.wavelength: None`
            # = 自动取 λmax、`plot.jacs.fig_heights.c: None` = 自动高度）。
            # 这类 None 必须**静默接受**——否则 GUI 自己保存的配置重载后会报一堆
            # 假告警（实测：首次实现就踩了，`test_gui_saved_config_has_zero_unknown_key_warnings`
            # 当场变红）。用户"把有默认值的键留空"才是要提示的情况（下面那支）。
            if k in base and base[k] is None:
                continue
            msg = f"配置项 `{path}` 值为空(None) → 已忽略，保留默认值"
            log.warning("%s", msg)
            if issues is not None:
                from .plotstyle import ConfigIssue as _CI
                issues.append(_CI("WARNING", path, msg,
                                  "填一个值，或删掉这一行（留空不等于用默认值）"))
            continue
        if isinstance(base.get(k), dict) and not isinstance(v, dict):
            # S1 的第二半（V2.x 审查）：**标量不得顶替配置节**。
            # 例：用户写 `wavelength: 379`（想改探针波长却写成整节标量）。
            # 若直接覆盖，cfg["wavelength"] 就成了 int，后续所有 `.get()` 全炸
            # （S1 动态复现路径）。这里保留默认节 + 明确告警，程序照常启动。
            msg = (f"配置节 `{path}` 应为「键: 值」映射，实际是 "
                   f"{type(v).__name__}：{v!r} → 已忽略，保留默认节")
            log.warning("%s", msg)
            if issues is not None:
                from .plotstyle import ConfigIssue as _CI
                issues.append(_CI("ERROR", path, msg,
                                  "改成缩进的键值对结构（例如 wavelength:\n  probe: "
                                  "379），或整节删掉用默认值"))
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v, issues=issues, prefix=path)
        else:
            base[k] = v
    return base


def default_config() -> dict:
    """返回 :data:`DEFAULT_CFG` 的深拷贝（core 不读全局状态的前提）。"""
    return copy.deepcopy(DEFAULT_CFG)


def unknown_key_issues(user: dict, known=KNOWN_PATHS,
                       limit: int = 5,
                       skip_prefixes: Optional[list] = None
                       ) -> list[ConfigIssue]:
    """DS1：按 :data:`KNOWN_PATHS` 校验**用户手写**的键（G1/DS1，V2.0 FR-18）。

    三档政策（设计稿 §1.1，决策已定）：

    ① 命中 :data:`DEAD_KEYS` → 一句"已废除、为什么没效果"的专用文案
       （**不**用"拼写错误"的口气混淆，否则用户会以为自己打错了字，其实是版本演进）；
    ② 剩下的未知键 → WARNING + ``suggest_key`` 近似建议（difflib，唯一建议来源）；
    ③ 值照常保留进 cfg，行为零变化、永不 ERROR、永不改写文件。

    两条重要的"不误报"保证：

    * **只校验 ``deep_merge`` 之前的原始 user dict**（原则 2）——GUI 保存的配置键
      全部来自 DEFAULT_CFG，所以**零新告警**；
    * ``plot.jacs`` 子树**整棵跳过**：``plotstyle.validate_jacs`` 已在那里给出
      更精确的文案（类型/范围/已废键），重复报一次就是一键双报；
    * **可扩展映射**（:data:`_EXTENSIBLE_PREFIXES`，如 ``quantum_yield.a_ex``）的
      子键放行——用户自定义染料名不是拼写错误（F4）；
    * ``skip_prefixes``：已经由 :func:`deep_merge` 报过"节不是映射"的路径不再
      重复报"未知键"（同一个手误只提示一次）。

    Returns:
        :class:`ConfigIssue` 列表；未知键**超过 ``limit`` 个**时合并成 1 条汇总
        （防启动弹窗刷屏，设计稿 §1.1 第 4 条）。
    """
    skips = tuple(skip_prefixes or ())
    unknown: list[tuple[str, str]] = []
    for path, _ in _walk_leaves(user):
        if path in known or path.startswith("plot.jacs") or \
                path in _TRANSIENT_KEYS:
            continue           # 已知键 / plot.jacs 子树 / 运行时内部键
        if any(path == s or path.startswith(s + ".") for s in skips):
            continue           # 该路径/子树已由 deep_merge 报过"不是映射"，不重复报
        if any(path.startswith(pre + ".") for pre in _EXTENSIBLE_PREFIXES):
            continue           # 可扩展映射的子键（F4：a_ex.<染料> 不算未知）
        if path in DEAD_KEYS:
            unknown.append((path, DEAD_KEYS[path]))
        else:
            unknown.append((path, suggest_key(path, sorted(known))))
    if not unknown:
        return []
    issues: list[ConfigIssue] = []
    for path, hint in unknown[:limit]:
        issues.append(ConfigIssue(
            "WARNING", path,
            ("该键已废除、不再有任何效果：" if path in DEAD_KEYS
             else "未知键（不会被任何代码读取，已原样保留但不生效）"), hint))
    if len(unknown) > limit:
        rest = "、".join(p for p, _ in unknown[limit:])
        issues.append(ConfigIssue(
            "WARNING", "<unknown>",
            f"另有 {len(unknown) - limit} 个未知/已废除键：{rest}",
            "详见日志；删掉这些行即可，不影响其它参数"))
    return issues


def _walk_leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """展开成 ``[(点分路径, 值)]``；``_`` 开头的私有键（**任意层级**）一律跳过。

    实例：``plot.jacs._preset``（``plotstyle.apply_preset`` 写的预设标记）、
    顶层 ``_dye_session``（``pipeline`` 的会话态）。**非** ``_`` 开头但同样属于
    内部态的键（``gui.last_dir``）由调用方按 :data:`_TRANSIENT_KEYS` 放行——
    这里**不能**用"名字里含下划线"来判，那会连 ``last_dir`` 一起跳过。
    """
    out: list[tuple[str, Any]] = []
    for k, v in (node or {}).items():
        if str(k).startswith(_PRIVATE_PREFIX):
            continue           # `_meta` / `_dye_session` / `gui.last_dir` 是内部态
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out += _walk_leaves(v, path)
        else:
            out.append((path, v))
    return out


def migrate_schema(user: dict, issues: list[ConfigIssue],
                   ver: Any = None, *,
                   migrations: Optional[list] = None,
                   current: Optional[int] = None) -> None:
    """按版本标记在**内存**里升级 dict（DS1 / 通用版本化迁移器）。

    这是**通用**算法（V2.0 δ 批起公开）：``config.yaml`` 的 ``_meta.schema`` 与
    ``dye_batch.yaml`` 的 ``schema_version`` 用**同一套模式**（设计文档 FR-14
    机制约束第 6 条说的"DS1 同款"），各自维护自己的迁移表与当前版本号。

    ⚠ 迁移表与当前版本是**显式参数**（默认取本模块的）——第一版写成直接读模块全局，
    于是批次文件注册的假迁移被静默忽略、且拿 config 的版本号去比批次的版本号
    （测试当场抓到：`entry.role` 还是旧值）。**算法共用、表各自持有**才是对的。

    兼容承诺：**老文件没有版本标记 = schema 1 语义**（历史上从未有格式变更）
    → 静默视为当前，**不告警**。

    Args:
        user: 原始 dict（就地修改）。
        issues: 问题清单（就地追加）。
        ver: 显式版本号；``None`` 时依次尝试 ``user["schema_version"]``
            与 ``user["_meta"]["schema"]``。
        migrations: 迁移表；``None`` → 本模块的 :data:`_MIGRATIONS`。
        current: 当前版本；``None`` → 本模块的 :data:`SCHEMA_VERSION`。
    """
    mig = _MIGRATIONS if migrations is None else migrations
    cur_ver = SCHEMA_VERSION if current is None else int(current)
    if ver is None:
        if user.get("schema_version") is not None:
            ver = user.get("schema_version")
        else:
            meta = user.get("_meta")
            ver = meta.get("schema") if isinstance(meta, dict) else None
    if ver is None:
        return                                     # 老文件：静默视为当前
    try:
        cur = int(ver)
    except (TypeError, ValueError):
        issues.append(ConfigIssue(
            "WARNING", "_meta.schema", f"配置版本标记不是整数：{ver!r}",
            f"按当前版本 {SCHEMA_VERSION} 处理；删掉 _meta 即可"))
        return
    if cur > cur_ver:
        issues.append(ConfigIssue(
            "WARNING", "schema_version",
            f"该文件的格式版本为 {cur}，高于本程序认识的 {cur_ver}",
            "可能由更高版本写出；请升级程序，或让对方重新导出"))
        return
    for from_ver, migrate in mig:
        if from_ver == cur:
            migrate(user)
            cur = from_ver + 1
    if cur != cur_ver:
        log.warning("格式版本 %s 无对应迁移（迁移表现有 %d 条）→ 按当前版本处理",
                    ver, len(mig))


def load_config_ex(path: Optional[Path | str] = None
                   ) -> tuple[dict, list[ConfigIssue]]:
    """加载配置并**同时返回问题清单**（FR-9.5 / FR-10.1）。

    与 :func:`load_config` 的区别：这里把"为什么不生效"讲清楚——文件不存在、
    YAML 语法错误（含行号）、顶层不是映射、以及深合并后的键位校验结果。
    读坏时会把原文件备份为 ``config.yaml.bad.<时间戳>``，**绝不静默吞掉**。

    Returns:
        ``(cfg, issues)``；``issues`` 为空表示一切正常。
    """
    cfg = default_config()
    issues: list[ConfigIssue] = []
    if path is None:
        return cfg, issues
    p = Path(path)
    if not p.exists():
        issues.append(ConfigIssue(
            "INFO", "config.yaml", f"配置文件不存在，当前使用内置默认值：{p}",
            "改动任一参数后会自动写出该文件"))
        return cfg, issues
    try:
        import yaml
    except ImportError:
        issues.append(ConfigIssue("WARNING", "config.yaml",
                                  "未安装 pyyaml，已忽略用户配置",
                                  "pip install pyyaml"))
        return cfg, issues
    try:
        text = p.read_text(encoding="utf-8")
        user = yaml.safe_load(text) or {}
    except Exception as err:                       # noqa: BLE001 —— 顶层不得抛
        bad = _backup_bad_config(p, err)
        issues.append(ConfigIssue(
            "ERROR", "config.yaml",
            f"配置文件语法错误，本次已回退默认配置：{err}",
            f"原文件已备份为 {bad.name}；修好后点「恢复上一份配置」或重启"))
        log.warning("读取配置失败 %s: %s（已回退默认）", p, err)
        return cfg, issues
    if not isinstance(user, dict):
        bad = _backup_bad_config(p, "顶层不是映射")
        issues.append(ConfigIssue(
            "ERROR", "config.yaml", "配置顶层不是键值对结构，本次已回退默认配置",
            f"原文件已备份为 {bad.name}"))
        return cfg, issues

    written_by = None
    if isinstance(user.get("_meta"), dict):
        written_by = user["_meta"].get("written_by")
    migrate_schema(user, issues)                   # DS1：先在内存迁移（原则 6）
    user.pop("_meta", None)
    _before = len(issues)
    deep_merge(cfg, user, issues=issues)           # A.M1：留空/标量节 → 忽略 + 进问题清单
    _bad_sections = [i.key for i in issues[_before:] if i.level == "ERROR"]
    if written_by:
        from . import VERSION
        if _ver_tuple(str(written_by)) > _ver_tuple(VERSION):
            issues.append(ConfigIssue(
                "WARNING", "config.yaml",
                f"该配置由更高版本（{written_by}）写出，当前版本 {VERSION} 可能不认识新键",
                "建议升级程序或让对方重新导出配置"))
    issues += unknown_key_issues(                  # DS1：只报用户手写的键（G1）
        user, skip_prefixes=_bad_sections)         # 已报过"不是映射"的节不重复报
    # S1 兜底（V2.x 审查）：`validate_config` 承诺"任何情况下不抛"，但历史实现有
    # `.get()` 直穿非映射节的路径（已在其内部修）；这里再加一道**调用方护栏**——
    # 校验器自身坏掉不该让 GUI/CLI 启动崩溃（config.py 的既有承诺）。
    try:
        issues += validate_config(cfg)
    except Exception as err:                        # noqa: BLE001 —— 顶层不得抛
        log.exception("配置校验器自身异常（已跳过校验，配置照常合并）")
        from .plotstyle import ConfigIssue as _CI
        issues.append(_CI("ERROR", "<validate>",
                          f"配置校验自身异常：{type(err).__name__}: {err}",
                          "请把该报错与 config.yaml 一起反馈；本次已跳过校验"))
    log.info("已加载配置: %s（%d 条提示）", p, len(issues))
    return cfg, issues


def _ver_tuple(v: str) -> tuple:
    """``"1.2.3"`` → ``(1, 2, 3)``（解析失败按 0 处理，不抛）。"""
    out = []
    for part in str(v).split("."):
        try:
            out.append(int("".join(ch for ch in part if ch.isdigit()) or 0))
        except Exception:                              # noqa: BLE001
            out.append(0)
    return tuple(out[:3]) if out else (0,)


def _backup_bad_config(p: Path, why) -> Path:
    """把写坏的配置备份成 ``config.yaml.bad.<时间戳>``（FR-9.5）。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bad = p.with_name(f"{p.name}.bad.{stamp}")
    try:
        shutil.copy2(p, bad)
    except Exception as err:                           # noqa: BLE001
        log.warning("备份损坏配置失败: %s", err)
        return bad
    log.warning("已备份损坏的配置 %s → %s（%s）", p, bad, why)
    return bad


def load_config(path: Optional[Path | str] = None) -> dict:
    """加载配置：``DEFAULT_CFG`` 深合并用户 yaml（既有签名不变）。

    与 legacy 的差别：读失败不再 ``print``/``sys.exit``，而是记 WARNING 并
    回退到默认配置，保证 GUI 顶层永不因配置问题崩溃（设计文档 §6 末段）。
    需要拿到"为什么不生效"时用 :func:`load_config_ex`。
    """
    return load_config_ex(path)[0]


def config_backup(cfg: dict, path: Path | str, keep: int = 5) -> Optional[Path]:
    """保存前把**当前磁盘上的**配置快照到 ``config_backup/``（保留最近 keep 份）。

    与 FR-9.5 的"写坏回退"配套：用户改坏了配置，总能退回上一份能用的。
    """
    p = Path(path)
    if not p.exists():
        return None
    d = p.parent / "config_backup"
    try:
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = d / f"config.{stamp}.yaml"
        shutil.copy2(p, dst)
        old = sorted(d.glob("config.*.yaml"))
        for f in old[:-keep] if len(old) > keep else []:
            try:
                f.unlink()
            except OSError:
                pass
        return dst
    except Exception as err:                           # noqa: BLE001
        log.warning("配置快照失败: %s", err)
        return None


def list_config_backups(path: Path | str) -> list[Path]:
    """按时间倒序列出配置快照（最新的在前）。"""
    p = Path(path)
    d = p.parent / "config_backup"
    if not d.is_dir():
        return []
    return sorted(d.glob("config.*.yaml"), reverse=True)


def restore_config_backup(path: Path | str, which: int = 0) -> Optional[Path]:
    """把第 ``which`` 份快照恢复成正式配置（0 = 最新一份）。"""
    p = Path(path)
    snaps = list_config_backups(p)
    if which >= len(snaps):
        return None
    src = snaps[which]
    if p.exists():
        _backup_bad_config(p, "恢复快照前的当前配置")
    shutil.copy2(src, p)
    log.info("已从快照恢复配置: %s → %s", src, p)
    return p


def save_config(cfg: dict, path: Path | str,
                *, force: bool = False) -> Path:
    """把 cfg 写回 yaml（FR-5 配置持久化）。仅写用户可调子集，避免全量落盘。

    V2.3（《代码评审记录.md》第3部分·F3）：**写盘护栏下沉到这里**。原来
    :func:`core.paths.cfg_writes_allowed` 只在两个 UI 调用点检查，
    ⑤设置保存 / app.py 首启固化 / nav 首启写三处都绕过它 —— 冒烟模式手工跑一次
    就会把交付配置写脏。现在所有写 config 的路径都经过本函数，一处收紧。

    Args:
        cfg: 完整配置 dict。
        path: 目标文件。
        force: 显式覆盖护栏（**仅**用于"这确实是产品预期写入"的场合；
            默认 False。冒烟模式写交付配置会被跳过并记 debug 日志）。

    Returns:
        目标路径（本次被护栏跳过时**不写盘**，仍返回该路径，调用方可照常记录）。
    """
    import yaml
    from . import VERSION

    p = Path(path)
    if not force:
        from .paths import cfg_writes_allowed
        if not cfg_writes_allowed(p):
            log.debug("冒烟/自动化模式：跳过写交付配置 %s（防止污染）", p)
            return p
    p.parent.mkdir(parents=True, exist_ok=True)
    config_backup(cfg, p)                     # FR-9.5：写前留一份快照
    keys = ("wavelength", "baseline", "background", "a0", "kinetics",
            "quantum_yield", "qc", "controls", "plot", "autogroup", "spectra",
            "io", "filename", "gui", "dye",
            # V1.3 新增四节（P5/P6/P7/P8）；旧 config.yaml 缺这些节照常运行
            "excel", "extinction", "wavelength_table", "qc_dashboard")
    user = {k: cfg[k] for k in keys if k in cfg}
    # A.M2（审查附录 A）：`gui.last_dir` 是运行时内部键（`_TRANSIENT_KEYS` 里刻意
    # 注明"不进 DEFAULT_CFG，否则会被落盘成机器绝对路径"），但白名单包含整节 `gui`
    # → 它其实一直被写进 config.yaml。这里显式剔除（`recent_dirs` 保留：那是历史记录）。
    gui = user.get("gui")
    if isinstance(gui, dict):
        user["gui"] = {k: v for k, v in gui.items() if k != "last_dir"}
    user["_meta"] = {"written_by": VERSION,
                     "schema": SCHEMA_VERSION,     # DS1：供未来键改名分流（今天恒为 1）
                     "written_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    p.write_text(
        "# dye_tool 用户配置（与 core.config.DEFAULT_CFG 深合并）\n"
        f"# 由 GUI 参数面板自动写出，可直接手工编辑；_meta.written_by = {VERSION}\n"
        "# 出图参数键位说明见 README 附录 B / core/plotstyle.py\n"
        + yaml.safe_dump(user, allow_unicode=True, sort_keys=False,
                         default_flow_style=False),
        encoding="utf-8")
    log.info("已保存配置: %s", p)
    return p


def autogroup_cfg(cfg: dict) -> dict:
    """取 FR-2 分组参数：AG_DEFAULT 叠加用户 ``autogroup`` 节。"""
    return dict(AG_DEFAULT, **(cfg.get("autogroup") or {}))


def section_defaults(name: str) -> dict:
    """某节的**默认值深拷贝**（FS2：默认值收口的唯一入口）。

    为什么要有它（V2.0 α 审计 + δ 批落地）：``DEFAULT_CFG`` 是唯一真相，但有几个
    模块为了"旧 config.yaml 缺这一节时能降级"各自抄了一份字面量——
    ``qc_dashboard.QD_DEFAULTS``（8 键）与 ``excel_export._FB_*``（6 个数值）。
    抄出来的那份**当下恰好相等**，于是没人发现；一旦默认值改了就会**静默分叉**
    （表里/图里的数与会话里的 cfg 不同）。现在这些兜底一律从这里取，
    真相只有 ``DEFAULT_CFG`` 一处。

    返回深拷贝：调用方拿到的是自己的副本，改它不会污染 ``DEFAULT_CFG``
    （与 :func:`default_config` 同一契约）。节不存在或不是 dict → 返回 ``{}``。
    """
    got = DEFAULT_CFG.get(name)
    return copy.deepcopy(got) if isinstance(got, dict) else {}


def get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """按 ``"a.b.c"`` 路径读配置，缺失返回 default。"""
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
