# -*- coding: utf-8 -*-
"""多面板拼版导出（V2.4 FR-27 / 路线 B2，Origin Layout Page 的对标）。

职责：把**已渲染**的 JACS 图件（PNG）按规格（1×2 / 2×2 / 3×3 …，≤12 格）
拼成一张投稿用的组合图，输出 PNG/SVG。

设计边界（设计文档 §5.2 + §4 原则）：

* **唯一新 core 模块**——只读图片、只画布局，不碰任何数值/渲染链路；
* 输出格式一律过 :func:`core.render.plotstyle.filter_formats`（**零 PDF** 唯一出口）；
* 输入图缺失/损坏 → **逐格中文报错清单**，且在全部校验通过前**不写任何文件**
  （不留半成品）；取消亦然（保存前才创建目录）；
* 图件等比嵌入、居中、去坐标轴；面板标签默认 ``(a)`` 小写括号、左上角
  （JACS 惯例）；V2.6 FR-45/Q-E20 起可选 ``panel_label_style="bare"``（论文式
  裸粗体小写 a/b/c、左上外侧）——**默认 paren 的输出逐像素不变**；
* 文件名 ``composite_<rows>x<cols>_<HHMMSS>``，与 ``fig_*`` / ``jacs_stack_*``
  永不冲突；同名已存在则自动加 ``-2`` 后缀，**绝不覆盖**既有图件。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger("dye_tool.layout")

#: 单张拼版最多格数（设计文档 §5.2 FR-27.1；600 dpi 大图的解码/内存上限）
MAX_CELLS = 12

#: 允许的输出格式（零 PDF：设计文档 A4.5 / A27.2）
ALLOWED_FORMATS = ("png", "svg")

#: 面板字母样式（V2.6 FR-45 / Q-E20）：
#:   paren = 既有 JACS 惯例 (a) 在格内左上角（默认，输出逐像素不变）；
#:   bare  = 论文式裸粗体小写 a 置于左上**外侧**（新图型用）。
PANEL_LABEL_STYLES = ("paren", "bare")


class LayoutError(ValueError):
    """拼版失败（中文说明，可直接显示在状态栏/日志）。"""


class LayoutCancelled(RuntimeError):
    """用户在拼版过程中取消（**不产出任何文件**）。"""


@dataclass
class LayoutSpec:
    """拼版规格（面板标签、边距、DPI 与输出格式）。"""

    rows: int = 2
    cols: int = 2
    margin_in: float = 0.12          # 画布四周留白（英寸）
    gutter_in: float = 0.06          # 格间距（英寸）
    panel_w_in: float = 3.2          # 单格宽（英寸）
    panel_h_in: float = 2.4          # 单格高（英寸）
    panel_labels: bool = True        # 默认带 (a)(b)… 标签（Q-A5）
    #: 面板标签样式：paren = (a)（默认，既有拼版输出逐像素不变）；
    #: bare = 论文式裸粗体小写 a 左上外侧（V2.6 FR-45 / Q-E20）
    panel_label_style: str = "paren"
    label_size: float = 11.0
    dpi: int = 600                   # 与 JACS 定稿同档（预览可调低）
    formats: tuple = ("png",)        # png / svg；其它格式一律拒绝（零 PDF）

    @property
    def cells(self) -> int:
        return int(self.rows) * int(self.cols)

    def validate(self) -> "LayoutSpec":
        """规格自检；非法 → :class:`LayoutError`（中文、可操作）。"""
        if self.rows < 1 or self.cols < 1:
            raise LayoutError(f"拼版规格非法：{self.rows}×{self.cols}（行列都必须 ≥1）")
        if self.cells > MAX_CELLS:
            raise LayoutError(
                f"拼版规格 {self.rows}×{self.cols} = {self.cells} 格，"
                f"超过上限 {MAX_CELLS} 格（600 dpi 大图拼太多会占满内存）")
        if self.dpi < 72 or self.dpi > 2400:
            raise LayoutError(f"DPI 非法：{self.dpi}（允许 72–2400）")
        for name, v in (("边距", self.margin_in), ("格间距", self.gutter_in),
                        ("单格宽", self.panel_w_in), ("单格高", self.panel_h_in)):
            if not (0 < float(v) <= 12.0):
                raise LayoutError(f"{name}非法：{v}（允许 (0, 12] 英寸）")
        if self.panel_label_style not in PANEL_LABEL_STYLES:
            raise LayoutError(
                f"面板字母样式非法：{self.panel_label_style!r}（只允许 "
                f"{'/'.join(PANEL_LABEL_STYLES)}：paren=(a) 兼容既有输出，"
                f"bare=论文式裸粗体小写字母）")
        _check_formats(self.formats)
        return self


def _check_formats(formats: Sequence[str]) -> list[str]:
    """校验并规范化输出格式（**零 PDF 唯一出口**，设计文档 A4.5/A27.2）。

    与 :func:`core.render.plotstyle.filter_formats` 同源：先把用户值过一遍那个
    唯一出口；只要**任何一个**请求的格式不被允许（如 pdf），**整单拒绝**——
    绝不"静默只出 png"，那会让用户以为 PDF 已经生成。
    """
    from .plotstyle import filter_formats

    wanted = [str(f).strip().lower().lstrip(".") for f in (formats or ())]
    wanted = [f for f in wanted if f]
    if not wanted:
        raise LayoutError("没有指定输出格式（只支持 png / svg）")
    allowed = tuple(str(f).lower() for f in filter_formats(wanted))
    bad = [f for f in wanted if f not in allowed or f not in ALLOWED_FORMATS]
    if bad:
        raise LayoutError(
            f"不支持的拼版输出格式：{'、'.join(bad)}——只允许 "
            f"{'/'.join(ALLOWED_FORMATS)}（零 PDF 红线：截图见设计文档 A4.5）")
    # 保序去重
    out: list[str] = []
    for f in wanted:
        if f not in out:
            out.append(f)
    return out


def compose_filename(rows: int, cols: int, when: Optional[str] = None) -> str:
    """拼版文件名主干：``composite_<rows>x<cols>_<HHMMSS>``（与既有图件不冲突）。"""
    stamp = when or time.strftime("%H%M%S")
    return f"composite_{int(rows)}x{int(cols)}_{stamp}"


def _panel_letter(index: int) -> str:
    """面板序号 -> 小写字母（0 -> a；上限由 MAX_CELLS 挡住，不会越界）。"""
    return chr(ord("a") + int(index))


def panel_label_text(index: int, *, style: str = "paren") -> str:
    """面板字母文本（V2.6 FR-45 / Q-E20 单一来源）。

    Args:
        index: 面板序号（0 起）。
        style: "paren" -> "(a)"（既有 JACS 惯例，默认）；"bare" -> "a"
            （论文式裸粗体小写字母）。

    Raises:
        ValueError: style 非法（中文说明，列出允许值）。
    """
    if style not in PANEL_LABEL_STYLES:
        raise ValueError(
            f"面板字母样式非法：{style!r}（只允许 {'/'.join(PANEL_LABEL_STYLES)}）")
    letter = _panel_letter(index)
    return letter if style == "bare" else f"({letter})"


#: 兼容别名（新图型/测试可按 panel_label(0, style="bare") 调用）
panel_label = panel_label_text


def apply_panel_label(ax, index, *, style: str = "paren",
                      fontsize: float = 11.0) -> Any:
    """在 ax 上画面板字母，返回 matplotlib 的 Text 对象。

    * style="paren"：格内左上角 "(a)"（与既有拼版输出同一参数，兼容）；
    * style="bare"：**左上外侧**裸粗体小写 "a"（论文式，V2.6 新图型用）。

    index 可传序号（0 -> a）或直接传字母（如 "b"）。
    """
    if style not in PANEL_LABEL_STYLES:
        raise ValueError(
            f"面板字母样式非法：{style!r}（只允许 {'/'.join(PANEL_LABEL_STYLES)}）")
    letter = str(index) if isinstance(index, str) else _panel_letter(index)
    text = letter if style == "bare" else f"({letter})"
    if style == "bare":
        return ax.text(-0.02, 1.02, text, transform=ax.transAxes,
                       ha="left", va="bottom", fontsize=fontsize,
                       fontweight="bold", color="black", clip_on=False)
    return ax.text(0.015, 0.985, text, transform=ax.transAxes,
                   ha="left", va="top", fontsize=fontsize,
                   fontweight="bold", color="black")


def _cell_report(paths: Sequence[Path]) -> tuple[list, list]:
    """逐格检查输入：返回 ``(可读数组列表, 错误清单)``（错误给中文，含格号）。"""
    import numpy as np

    arrays: list = []
    errors: list = []
    for i, p in enumerate(paths):
        q = Path(p)
        if not q.is_file():
            errors.append(f"第 {i + 1} 格：文件不存在 —— {q.name}")
            arrays.append(None)
            continue
        try:
            import matplotlib.image as mpimg
            a = mpimg.imread(str(q))
            if a is None or np.asarray(a).size == 0:
                raise ValueError("图像为空")
            arrays.append(np.asarray(a))
        except Exception as err:                        # noqa: BLE001 —— 逐格收集
            errors.append(f"第 {i + 1} 格：无法解码 —— {q.name}（{err}）")
            arrays.append(None)
    return arrays, errors


def compose_layout(image_paths: Sequence[Path | str],
                   spec: Optional[LayoutSpec] = None,
                   cfg: Optional[dict] = None,
                   out_dir: Optional[Path | str] = None,
                   *,
                   name_stem: Optional[str] = None,
                   cancel_cb: Optional[Callable[[], bool]] = None,
                   progress_cb: Optional[Callable[[int, str], None]] = None,
                   panel_label_style: Optional[str] = None,
                   ) -> list[Path]:
    """把 ``image_paths`` 按 ``spec`` 拼成一张组合图并写盘。

    Args:
        image_paths: 已渲染的图件路径（PNG；顺序 = 面板顺序）。
        spec: 拼版规格；``None`` = 默认 2×2 / 600 dpi / 仅 png。
        cfg: 完整配置（当前不参与几何，保留给将来"按配置定字号"；可为 ``None``）。
        out_dir: 输出目录（默认 = 第一张输入图所在目录）。
        name_stem: 文件名主干（默认 ``composite_<r>x<c>_<HHMMSS>``；测试可注入）。
        cancel_cb: 返回 True 表示用户取消——在**每格绘制前**与保存前检查；
            取消抛 :class:`LayoutCancelled`，不留任何文件。
        progress_cb: (百分比, 阶段名) 回调（后台 Job 用）。
        panel_label_style: 面板字母样式覆盖（V2.6 FR-45/Q-E20）：
            None = 用 spec.panel_label_style（默认 "paren"，输出与 V2.4
            逐像素一致）；"bare" = 论文式裸粗体小写字母。

    Returns:
        实际写出的文件路径列表（每格式一个）。

    Raises:
        LayoutError: 规格非法 / 格式含 PDF / 输入为空 / 图数超过格数 /
            输入缺失或损坏（含**全部**坏格的中文清单，不产半成品）。
        LayoutCancelled: 用户取消。
    """
    sp = (spec or LayoutSpec()).validate()
    if panel_label_style is not None:
        # 显式覆盖（测试/新图型用）；非法值经 validate 抛中文 LayoutError
        sp = replace(sp, panel_label_style=str(panel_label_style)).validate()
    paths = [Path(p) for p in (image_paths or [])]
    if not paths:
        raise LayoutError("没有可拼版的图件（请先在④档案页勾选图件）")
    if len(paths) > sp.cells:
        raise LayoutError(
            f"选了 {len(paths)} 张图，但规格 {sp.rows}×{sp.cols} 只有 {sp.cells} 格："
            "请减少勾选或改用更大规格")
    fmts = _check_formats(sp.formats)

    def _cancelled() -> bool:
        try:
            return bool(cancel_cb and cancel_cb())
        except Exception:                              # noqa: BLE001
            return False

    def _prog(pct: int, stage: str) -> None:
        if progress_cb:
            try:
                progress_cb(int(pct), stage)
            except Exception:                          # noqa: BLE001
                pass

    _prog(5, "检查输入图件")
    arrays, errors = _cell_report(paths)
    if errors:
        raise LayoutError("拼版输入有问题（未写出任何文件）：\n" + "\n".join(errors))

    target_dir = Path(out_dir) if out_dir is not None else paths[0].parent
    stem = name_stem or compose_filename(sp.rows, sp.cols)
    _prog(20, "布局与逐格绘制")
    if _cancelled():
        raise LayoutCancelled("已取消拼版（未写出任何文件）")

    # ── matplotlib 画布（局部 import：启动零成本，与优化 #7 同精神） ──
    import matplotlib
    matplotlib.use("Agg", force=False)
    import matplotlib.pyplot as plt

    fig_w = sp.cols * sp.panel_w_in + (sp.cols - 1) * sp.gutter_in + 2 * sp.margin_in
    fig_h = sp.rows * sp.panel_h_in + (sp.rows - 1) * sp.gutter_in + 2 * sp.margin_in
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=sp.dpi, facecolor="white")
    try:
        gs = fig.add_gridspec(
            sp.rows, sp.cols,
            left=sp.margin_in / fig_w, right=1 - sp.margin_in / fig_w,
            bottom=sp.margin_in / fig_h, top=1 - sp.margin_in / fig_h,
            wspace=sp.gutter_in / sp.panel_w_in,
            hspace=sp.gutter_in / sp.panel_h_in)
        for i, arr in enumerate(arrays):
            if _cancelled():
                raise LayoutCancelled("已取消拼版（未写出任何文件）")
            r, c = divmod(i, sp.cols)
            ax = fig.add_subplot(gs[r, c])
            ax.imshow(arr, interpolation="lanczos")
            ax.set_axis_off()                       # 去坐标轴/刻度（论文拼版惯例）
            if sp.panel_labels:
                letter = chr(ord("a") + i)          # a,b,c…（>26 格已被上限挡住）
                if sp.panel_label_style == "bare":
                    # V2.6 FR-45 / Q-E20：论文式裸粗体小写字母，左上外侧
                    apply_panel_label(ax, i, style="bare",
                                      fontsize=sp.label_size)
                else:
                    # 默认 paren：与 V2.4 输出逐像素一致（A27 回归红线）
                    ax.text(0.015, 0.985, f"({letter})", transform=ax.transAxes,
                            ha="left", va="top", fontsize=sp.label_size,
                            fontweight="bold", color="black")
            _prog(20 + int(60 * (i + 1) / max(len(arrays), 1)),
                  f"绘制第 {i + 1}/{len(arrays)} 格")

        if _cancelled():
            raise LayoutCancelled("已取消拼版（未写出任何文件）")
        _prog(85, "写出拼版文件")
        target_dir.mkdir(parents=True, exist_ok=True)
        made: list[Path] = []
        for ext in fmts:
            p = target_dir / f"{stem}.{ext}"
            n = 2
            while p.exists():                       # 绝不覆盖既有图件
                p = target_dir / f"{stem}-{n}.{ext}"
                n += 1
            fig.savefig(p, dpi=sp.dpi, bbox_inches=None, facecolor="white",
                        edgecolor="none")
            made.append(p)
        _prog(100, "拼版完成")
        log.info("已拼版 %d 张图 → %s（%s，%ddpi）", len(paths),
                 "、".join(m.name for m in made), f"{sp.rows}×{sp.cols}", sp.dpi)
        return made
    finally:
        plt.close(fig)
