# -*- coding: utf-8 -*-
"""Matplotlib 嵌入画布（FR-1.5、FR-5）：缩放/平移工具栏 + 双击放大预览。

实现策略：core 已经把图写到 run 目录，这里直接**回显 PNG**，保证 GUI 内嵌
预览与导出文件逐像素一致；同时提供 ``show_figure`` 以便将来直接嵌实时图。
"""
from __future__ import annotations

from collections import OrderedDict
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

import matplotlib
matplotlib.use("QtAgg")                                 # 与 PySide6 配套的交互后端
from matplotlib.backends.backend_qtagg import (FigureCanvasQTAgg,
                                               NavigationToolbar2QT)
from matplotlib.figure import Figure
from ui import theme


#: 占位文字用的中文字体候选（按常见 Windows 环境排序）
_ZH_FONTS = ("Microsoft YaHei", "微软雅黑", "SimHei", "黑体",
             "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC")
_ZH_FONT_CACHE: list = []          # 只探测一次（字体扫描有成本）


def _zh_font_family() -> Optional[str]:
    """可用的第一个中文字体名；一个都没有返回 ``None``。

    为什么不能只靠 ``font.sans-serif`` 列表：matplotlib **3.11 实测没有逐字形
    回退**——把中文字体追加进字体链，遇到"尚无图件"这种中文串照样报
    ``missing from font(s) DejaVu Sans`` 并画出豆腐块。而项目约定是
    "Arial 优先、图件全英文"，所以只在**确实含中文的文本对象**上单独换字体。
    """
    if _ZH_FONT_CACHE:
        return _ZH_FONT_CACHE[0] or None
    fam: Optional[str] = None
    try:
        from matplotlib import font_manager
        have = {f.name for f in font_manager.fontManager.ttflist}
        fam = next((n for n in _ZH_FONTS if n in have), None)
        _ZH_FONT_CACHE.append(fam or "")
    except Exception:                                  # noqa: BLE001 —— 探测失败不致命
        _ZH_FONT_CACHE.append("")
    return fam


class MplCanvas(FigureCanvasQTAgg):
    """单张图的画布（Agg/QtAgg 后端）。"""

    def __init__(self, parent: Optional[QWidget] = None, width: float = 8.0,
                 height: float = 5.5, dpi: int = 100):
        self.fig = Figure(figsize=(width, height), dpi=dpi, tight_layout=True)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_axis_off()
        self._set_placeholder("尚无图件")

    def _set_placeholder(self, text: str) -> None:
        """画占位文字；有中文字体就用中文，没有则退回英文（不出豆腐块）。

        空画布跟随界面主题底色；载入图件时再换回白底（图件本身是白底产物）。"""
        self.fig.set_facecolor(theme.hexc("surface"))
        fam = _zh_font_family()
        shown = str(text)
        kwargs = {}
        if any(ord(ch) > 127 for ch in shown):
            if fam:
                kwargs["fontfamily"] = fam
            else:
                shown = shown.encode("ascii", "replace").decode("ascii")
        self._placeholder = self.ax.text(
            0.5, 0.5, shown, ha="center", va="center",
            transform=self.ax.transAxes, color=theme.hexc("text_subtle"), **kwargs)


class ZoomDialog(QDialog):
    """双击放大窗口：把当前 PNG 以更大尺寸显示，可滚动。"""

    def __init__(self, image_path: Path, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(f"放大预览 — {Path(image_path).name}")
        self.resize(1100, 800)
        self.canvas = MplCanvas(self, width=11.0, height=8.0, dpi=110)
        self.canvas.show_image(image_path)
        bar = QHBoxLayout()
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        bar.addStretch(1)
        bar.addWidget(btn)
        lay = QVBoxLayout(self)
        lay.addWidget(NavigationToolbar2QT(self.canvas, self))
        lay.addWidget(self.canvas, 1)
        lay.addLayout(bar)


class CanvasPanel(QWidget):
    """带工具栏、图件切换与双击放大的画布容器。

    信号 ``figure_changed(str)`` 携带当前图件文件名。

    P1 懒渲染支持：``queued(names)`` 先把"已排期但还没画"的图位占进下拉（选中时
    画布显示"渲染中…"占位），``mark_ready(path)`` 在某张图落盘后把它换成真图。

    **索引不变式（S7）**：下拉的每一项都来自唯一的内部名字列表 ``_names``
    （= 真图 ∪ 排队占位，按名排序），``_idx`` / ``step()`` / ``_show()`` 全都只在
    这个列表上取值；"取真图路径"必须经 :meth:`_path_of`（按**名字**查）。
    早期版本的 bug 正是用下拉下标去索引只含真图的 ``_images`` —— 占位与真图混排时
    两个索引空间错位，于是选中占位会显示并返回**错的那张图**、放大打开错文件。
    """

    figure_changed = Signal(str)
    #: V2.3 FR-24.6：状态提示（复制成功/失败等）；页面可接到状态栏
    status_message = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._images: dict[str, Path] = {}         # 图件名 → 真图路径
        self._names: list[str] = []                # 下拉展示的唯一真相（排好序）
        self._queued: dict[str, str] = {}          # 图件名 → 排队说明
        self._queued_steps: dict[str, str] = {}    # 图件名 → 所属步骤
        #: A 道 FR-23.5：只看哪些图名（``None`` = 全部；只过滤下拉可见集）
        self._visible_filter: Optional[Callable[[str], bool]] = None
        self._idx = -1
        self._build()

    def _build(self) -> None:
        self.combo = QComboBox()
        self.combo.setMinimumWidth(280)
        self.combo.currentIndexChanged.connect(self._on_combo)
        self.btn_prev = QPushButton("◀ 上一张")
        self.btn_next = QPushButton("下一张 ▶")
        self.btn_zoom = QPushButton("放大查看")
        self.btn_copy = QPushButton("复制图片")
        self.btn_copy.setToolTip("把当前图复制到剪贴板（Ctrl+Shift+C），可直接贴进 Word/PPT")
        self.btn_prev.clicked.connect(lambda: self.step(-1))
        self.btn_next.clicked.connect(lambda: self.step(1))
        self.btn_zoom.clicked.connect(self.zoom)
        self.btn_copy.clicked.connect(self.copy_current_image)
        # V2.3 FR-24.6：键盘 ←/→ 翻图、Ctrl+Shift+C 复制（不占用表格的 Ctrl+C）
        self.setFocusPolicy(Qt.StrongFocus)
        self._build_context_menu()          # A25.2：右键菜单（复制/另存/定位/放大）
        QShortcut(QKeySequence("Ctrl+Shift+C"), self,
                  activated=self.copy_current_image)
        self.lbl_pending = QLabel("")
        theme.mark(self.lbl_pending, "warn")   # 琥珀色：提示"还在渲"
        self.lbl_pending.setToolTip(
            "预览档先画首图让你尽快看到结果，其余图件在后台继续渲染；\n"
            "渲染完成的图会立刻出现在这个下拉里。")
        top = QHBoxLayout()
        top.addWidget(QLabel("图件"))
        top.addWidget(self.combo, 1)
        top.addWidget(self.lbl_pending)
        top.addWidget(self.btn_prev)
        top.addWidget(self.btn_next)
        top.addWidget(self.btn_zoom)
        top.addWidget(self.btn_copy)

        self.canvas = MplCanvas(self, width=8.5, height=5.5)
        self.toolbar = NavigationToolbar2QT(self.canvas, self)
        hint = QLabel("提示：双击画布可放大；工具栏支持缩放/平移/另存")
        theme.mark(hint, "subtle")
        self.canvas.mpl_connect("button_press_event", self._on_dclick)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addLayout(top)
        lay.addWidget(self.toolbar)
        lay.addWidget(self.canvas, 1)
        lay.addWidget(hint)

    # ── 对外接口 ──
    def set_images(self, paths: Sequence[Path], keep_index: bool = False) -> None:
        """载入一组图件（按文件名排序），并显示第一张。"""
        keep = [Path(p) for p in paths if Path(p).suffix.lower()
                in (".png", ".jpg", ".jpeg", ".svg", ".pdf")]
        self._images = {p.name: p for p in keep}
        # 整批换图 → 旧一轮的排队位与步骤记录都不再适用（新 run 会有新的占位）
        self._queued.clear()
        self._queued_steps.clear()
        old = self.combo.currentText().split("　（", 1)[0]
        self._rebuild_combo()
        if not self._names:
            self._idx = -1
            self.canvas.clear_placeholder("尚无图件")
            return
        idx = 0
        if keep_index and old in self._names:
            idx = self._names.index(old)
        self.combo.setCurrentIndex(idx)
        self._show(idx)

    # ── A25.2（V2.3 §12）：对象级右键菜单 ──
    def _build_context_menu(self) -> None:
        """画布右键菜单：复制图片 / 另存为 / 在资源管理器中显示 / 放大查看。

        动作**复用现有槽**（``copy_current_image`` / ``zoom``），不复制实现；
        设计文档 FR-25.2 的要求就是"右键 = 已有能力的第二入口"。
        """
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos) -> None:              # noqa: ANN001
        from PySide6.QtWidgets import QMenu
        cur = self.current_image()
        menu = QMenu(self)
        act_copy = menu.addAction("复制图片")
        act_copy.setEnabled(cur is not None and Path(cur).exists())
        act_copy.triggered.connect(lambda *_: self.copy_current_image())
        act_save = menu.addAction("另存为…")
        act_save.setEnabled(cur is not None and Path(cur).exists())
        act_save.triggered.connect(lambda *_: self.save_current_as())
        act_reveal = menu.addAction("在资源管理器中显示")
        act_reveal.setEnabled(cur is not None and Path(cur).exists())
        act_reveal.triggered.connect(lambda *_: self.reveal_current())
        menu.addSeparator()
        act_zoom = menu.addAction("放大查看")
        act_zoom.setEnabled(cur is not None)
        act_zoom.triggered.connect(lambda *_: self.zoom())
        menu.exec(self.mapToGlobal(pos))

    def save_current_as(self) -> bool:
        """把当前图另存为（默认同名，可改目录/名字）；成功/失败走状态栏。"""
        cur = self.current_image()
        if cur is None or not Path(cur).exists():
            self.status_message.emit("当前没有可另存的图件")
            return False
        from PySide6.QtWidgets import QFileDialog

        dest, _ = QFileDialog.getSaveFileName(
            self, "另存图件", str(Path(cur).name),
            "PNG 图片 (*.png);;SVG 矢量 (*.svg);;所有文件 (*)")
        if not dest:
            return False
        try:
            import shutil
            shutil.copyfile(cur, dest)
        except Exception as err:                            # noqa: BLE001
            self.status_message.emit(f"另存失败：{err}")
            return False
        self.status_message.emit(f"已另存：{dest}")
        return True

    def reveal_current(self) -> bool:
        """在资源管理器中定位当前图件（Windows：explorer /select）。"""
        cur = self.current_image()
        if cur is None or not Path(cur).exists():
            self.status_message.emit("当前没有可定位的图件")
            return False
        try:
            import subprocess
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", str(Path(cur))])
            else:                                           # pragma: no cover
                subprocess.Popen(["xdg-open", str(Path(cur).parent)])
            self.status_message.emit(f"已定位：{Path(cur).name}")
            return True
        except Exception as err:                            # noqa: BLE001
            self.status_message.emit(f"无法打开资源管理器：{err}")
            return False

    def set_visible_filter(self, predicate: Optional[Callable[[str], bool]] = None
                           ) -> None:
        """A 道 FR-23.5：只过滤**下拉可见集**（``None`` = 全部）。

        ``_images`` / ``_queued`` 保持**全局**——懒渲染的排队占位、"后台渲染中
        N 张"计数都不随过滤变化（所以**禁止**用 ``set_images`` 做过滤：它会
        清空排队占位，见 ``phi_page`` 的补渲链路）。

        当前选中项被过滤掉时落到第一张可见图；可见集为空则显示占位。
        """
        self._visible_filter = predicate
        cur = self.current_name()
        self._rebuild_combo()
        if not self._names:
            self._idx = -1
            self.canvas.clear_placeholder("该组暂无图件")
            return
        if cur and cur in self._names:
            self._select(self._names.index(cur))
            return
        self._select(0)
        self._show(0)

    def queued(self, names: Sequence[str], note: str = "渲染中…",
               steps: Optional[dict] = None) -> None:
        """把"已排期但还没画"的图位加入下拉（不改变当前显示）。

        ``steps`` 可选：``{图件名: 步骤}``。带步骤信息后，渲染某个步骤却一张图都
        没产出时（例如没有可用 Φ_Δ 的图 d），可以用 :meth:`resolve_step` 把这些
        占位一次性收掉。

        占位会**同时**补进下拉列表（排在正确位置），因此本方法可以独立调用，不
        要求先 :meth:`set_images`——界面无论按什么顺序收到"这些图会来"，看到的
        列表都是同一份。
        """
        added = False
        for n in names or []:
            n = str(n)
            if not n or n in self._queued or n in self._images:
                continue          # 已排队 / 已经画出来了 → 绝不能又标成"渲染中"
            self._queued[n] = note
            if steps and n in steps:
                self._queued_steps[n] = str(steps[n])
            added = True
        if added:
            self._rebuild_combo()

    def resolve_step(self, step: str) -> int:
        """某步骤渲染完毕 → 收掉它名下仍未兑现的占位（返回收掉的数量）。"""
        stale = [n for n, s in self._queued_steps.items()
                 if s == str(step) and n in self._queued]
        for n in stale:
            self._queued.pop(n, None)
            self._queued_steps.pop(n, None)
        if stale:
            self._rebuild_combo()      # A05：只 pop 字典不重建 → 下拉留幽灵项
        self._refresh_pending()
        return len(stale)

    def clear_queued(self, keep: Optional[Sequence[str]] = None) -> int:
        """清理排队占位（``keep`` 里的名字保留），返回清理数量。

        渲染结束时必须调用：图 d 这类"可能画不出"的图位不会再有人来把它换成
        真图，留着就会永远显示"渲染中…"。
        """
        alive = {str(n) for n in (keep or ())}
        stale = [n for n in self._queued if n not in alive]
        for n in stale:
            self._queued.pop(n, None)
            self._queued_steps.pop(n, None)
        if stale:
            self._rebuild_combo()      # A05：同上
        self._refresh_pending()
        return len(stale)

    def mark_ready(self, path: Path | str, keep_index: bool = True) -> bool:
        """某张图已落盘：把排队占位换成真图（返回是否真的换了）。"""
        p = Path(path)
        if p.suffix.lower() != ".png" or not p.exists():
            return False
        name = p.name
        cur = self.current_name()
        self._queued.pop(name, None)
        self._queued_steps.pop(name, None)
        if name in self._images:
            self._refresh_pending()
            return False
        self._images[name] = p
        self._rebuild_combo()
        # 保住用户正在看的那一张（按**名字**定位，S7：绝不能用旧下标）
        if keep_index and cur and cur in self._names:
            self._select(self._names.index(cur))
        elif not keep_index and name in self._names:
            self._select(self._names.index(name))
        # A06（V2.x 审查）：如果当前名没变、但画布上显示的是**占位**（刚补渲完成的
        # 正是用户正在看的那张），`_select` 只移动下拉、不会重画 → 画布仍是"渲染中"。
        # 这里在"当前名 == 刚就绪的名字"时强制 `_show` 一次真图。
        if keep_index and cur and cur == name:
            try:
                self._show(self._names.index(cur))
            except Exception:                          # noqa: BLE001 —— 显示失败不致命
                pass
        return True

    def pending_count(self) -> int:
        """还在排队（未出图）的图件数。"""
        return len(self._queued)

    def queued_names(self) -> list[str]:
        """仍在排队（未出图）的图件名。"""
        return sorted(self._queued)

    def current_name(self) -> str:
        """当前选中的图件名（含排队占位；无选中 → 空串）。"""
        if 0 <= self._idx < len(self._names):
            return self._names[self._idx]
        return ""

    def current_image(self) -> Optional[Path]:
        """当前显示的图件**路径**（排队中/无选中 → ``None``）。

        S7：这里必须按名字查真图。原来用下拉下标去索引 ``_images``，占位混排时
        会返回**错的那张图**（实测：选中占位却返回 fig_c.png）。
        """
        return self._images.get(self.current_name())

    def show_image(self, path: Path | str) -> None:
        """直接显示一张图（不动图件列表）。"""
        p = Path(path)
        self._images = {p.name: p}
        self._names = [p.name]
        self._queued.clear()
        self._queued_steps.clear()
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem(p.name)
        self.combo.blockSignals(False)
        self._idx = 0
        self._refresh_pending()
        self.canvas.show_image(p)

    def show_figure(self, fig: Figure) -> None:
        """直接显示一个 matplotlib Figure 对象（实时绘图用）。"""
        self.canvas.show_figure(fig)

    def step(self, delta: int) -> None:
        """在列表中前后翻页。"""
        if not self._names:
            return
        idx = max(0, min(len(self._names) - 1, self._idx + delta))
        self.combo.setCurrentIndex(idx)

    def zoom(self) -> None:
        """打开放大窗口。"""
        cur = self.current_image()
        if cur is None:
            return
        ZoomDialog(cur, self).exec()

    def copy_current_image(self) -> bool:
        """V2.3 FR-24.6：把当前图复制到剪贴板（可直接贴进 Word/PPT）。"""
        cur = self.current_image()
        if cur is None or not Path(cur).exists():
            self.status_message.emit("当前没有可复制的图件")
            return False
        try:
            from PySide6.QtGui import QPixmap
            from PySide6.QtWidgets import QApplication
            pix = QPixmap(str(cur))
            if pix.isNull():
                self.status_message.emit(f"无法解码图件：{Path(cur).name}")
                return False
            QApplication.clipboard().setPixmap(pix)
            self.status_message.emit(
                f"已复制：{Path(cur).name}（{pix.width()}×{pix.height()}）")
            return True
        except Exception as err:                            # noqa: BLE001
            self.status_message.emit(f"复制失败：{err}")
            return False

    def keyPressEvent(self, e) -> None:                     # noqa: N802 —— Qt 接口
        """V2.3 FR-24.6：←/→ 翻图；Home/End 到首/末张。"""
        k = e.key()
        if k == Qt.Key_Left:
            self.step(-1)
            e.accept()
            return
        if k == Qt.Key_Right:
            self.step(1)
            e.accept()
            return
        if k == Qt.Key_Home and self._names:
            self.combo.setCurrentIndex(0)
            e.accept()
            return
        if k == Qt.Key_End and self._names:
            self.combo.setCurrentIndex(len(self._names) - 1)
            e.accept()
            return
        super().keyPressEvent(e)

    # ── 内部 ──
    def _rebuild_combo(self) -> None:
        """按"真图 + 排队占位"重建下拉与 ``_names``，尽量保住当前选中的那张。

        A 道 FR-23.5：``_visible_filter`` 只裁**可见集**——``_images``/``_queued``
        保持全局（被过滤掉的名字仍留在字典里，清掉过滤即恢复）。
        """
        cur = self.current_name()
        names = sorted(set(self._images) | set(self._queued))
        if self._visible_filter is not None:
            names = [n for n in names if self._visible_filter(n)]
        self._names = names
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(self._names)
        self.combo.blockSignals(False)
        if cur and cur in self._names:
            self._idx = self._names.index(cur)
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(self._idx)
            self.combo.blockSignals(False)
        elif self._idx >= len(self._names):
            self._idx = -1
        # A06（V2.x 审查）：重建下拉后把 `_idx` 与**控件实际选中项**对齐。
        # 原来只重填 items：`_idx` 可能仍是 -1（占位从未被显式选中过），
        # 于是 `current_name()` 返回空串——"正在显示占位图"对内部状态不可见，
        # `mark_ready` 的"保名刷新"分支永远走不到，画布就停在"渲染中…"。
        ci = self.combo.currentIndex()
        if 0 <= ci < len(self._names):
            self._idx = ci
        elif self._names and self._idx < 0:
            self._idx = 0
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(0)
            self.combo.blockSignals(False)
        self._refresh_pending()

    def _select(self, idx: int) -> None:
        """程序化选中某一项（不触发重复的 _show 递归）。"""
        if not (0 <= idx < len(self._names)):
            return
        self._idx = idx
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)


    def _refresh_pending(self) -> None:
        """刷新"还有 n 张在渲染"提示与排队图位的下拉后缀。"""
        n = len(self._queued)
        self.lbl_pending.setText(f"· 后台渲染中 {n} 张" if n else "")
        for i in range(self.combo.count()):
            name = self.combo.itemText(i)
            base = name.split("　（", 1)[0]
            if base in self._queued:
                self.combo.setItemText(i, f"{base}　（{self._queued[base]}）")
            elif name != base:
                self.combo.setItemText(i, base)

    def _on_combo(self, idx: int) -> None:
        self._show(idx)

    def _show(self, idx: int) -> None:
        if not (0 <= idx < len(self._names)):
            return
        self._idx = idx
        name = self._names[idx]
        if name in self._queued:
            stem = Path(name).stem
            self.canvas.clear_placeholder(f"{stem}\n\n该图仍在后台渲染，完成后自动出现")
            return
        p = self._images.get(name)
        if p is None:                       # 名字在列表里却没有真图 → 不该发生
            self.canvas.clear_placeholder(f"{name}\n\n图件文件不存在（可能已被移动或删除）")
            return
        self.canvas.show_image(p)
        self.figure_changed.emit(name)

    def _on_dclick(self, event) -> None:
        if event.dblclick:
            self.zoom()


# ── 给 MplCanvas 挂上显示辅助方法（保持单文件紧凑） ──
#: 预览解码尺寸上限（O6）：投稿图是 1950×3600 量级，**每翻一页都全量解码**
#: （mpimg.imread → float32 RGBA ≈ 28 MB）会造成明显卡顿。这里按"最长边不超过
#: 该值"解码（2560 ≈ 130 dpi 的 A4 观感，屏幕上看不出差别），并用一个有上限的
#: LRU 缓存已解码数组，来回翻页不再重复解码；文件一变（mtime/size）自动失效。
_IMAGE_CACHE: "OrderedDict[tuple, object]" = OrderedDict()
_IMAGE_CACHE_MAX = 6
_IMAGE_MAX_PIXELS = 2560


def _decode_image(path: Path):
    """解码 PNG → 数组（带缩放与 LRU 缓存）。失败抛异常，由调用方兜底显示。"""
    p = Path(path)
    try:
        st = p.stat()
        key = (str(p), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        key = (str(p), 0, 0)
    hit = _IMAGE_CACHE.get(key)
    if hit is not None:
        _IMAGE_CACHE.move_to_end(key)
        return hit

    from PySide6.QtGui import QImage, QImageReader
    import numpy as np
    reader = QImageReader(str(p))
    reader.setAutoTransform(True)
    size = reader.size()
    if size.isValid() and max(size.width(), size.height()) > _IMAGE_MAX_PIXELS:
        scaled = size.scaled(_IMAGE_MAX_PIXELS, _IMAGE_MAX_PIXELS,
                             Qt.KeepAspectRatio)
        reader.setScaledSize(scaled)
    img = reader.read()
    if img.isNull():                                  # 极端情况：回退 matplotlib
        import matplotlib.image as mpimg
        arr = mpimg.imread(str(p))
    else:
        img = img.convertToFormat(QImage.Format_RGBA8888)
        w, h = img.width(), img.height()
        buf = bytes(img.constBits())
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, img.bytesPerLine() // 4,
                                                         4)[:, :w, :]
    _IMAGE_CACHE[key] = arr
    _IMAGE_CACHE.move_to_end(key)
    while len(_IMAGE_CACHE) > _IMAGE_CACHE_MAX:
        _IMAGE_CACHE.popitem(last=False)
    return arr


def _clear_image_cache() -> None:
    """清空预览解码缓存（测试用）。"""
    _IMAGE_CACHE.clear()


def _show_image(self: MplCanvas, path: Path | str) -> None:
    """把 PNG 回显到画布（清除坐标轴，保持原始宽高比）。"""
    p = Path(path)
    self.fig.clear()
    self.fig.set_facecolor("white")
    self.ax = self.fig.add_subplot(111)
    try:
        if p.suffix.lower() == ".png":
            self.ax.imshow(_decode_image(p))
        else:
            self.ax.text(0.5, 0.5,
                         f"{p.name}\n（{p.suffix.upper().lstrip('.')} 矢量格式，"
                         f"请用工具栏“保存”或外部查看器打开）",
                         ha="center", va="center", transform=self.ax.transAxes,
                         fontsize=9, color="0.35", wrap=True)
    except Exception as err:                            # noqa: BLE001
        self.ax.text(0.5, 0.5, f"无法预览: {err}", ha="center", va="center",
                     transform=self.ax.transAxes, color="crimson")
    self.ax.set_axis_off()
    self.draw_idle()


def _show_figure(self: MplCanvas, fig: Figure) -> None:
    """把外部 Figure 的坐标轴内容搬到本画布。"""
    self.fig.clear()
    self.fig.set_facecolor("white")
    src = fig.axes[0] if fig.axes else None
    self.ax = self.fig.add_subplot(111)
    if src is not None:
        for ln in src.get_lines():
            self.ax.plot(ln.get_xdata(), ln.get_ydata(), label=ln.get_label())
        self.ax.set_xlabel(src.get_xlabel())
        self.ax.set_ylabel(src.get_ylabel())
        self.ax.set_title(src.get_title())
        if src.get_legend() is not None:
            self.ax.legend(fontsize=7)
    self.draw_idle()


def _clear_placeholder(self: MplCanvas, text: str = "尚无图件") -> None:
    """显示占位文字（字体选择与 ``MplCanvas._set_placeholder`` 同一口径）。"""
    self.fig.clear()
    self.ax = self.fig.add_subplot(111)
    self.ax.set_axis_off()
    self._set_placeholder(text)
    self.draw_idle()


MplCanvas.show_image = _show_image          # type: ignore[attr-defined]
MplCanvas.show_figure = _show_figure        # type: ignore[attr-defined]
MplCanvas.clear_placeholder = _clear_placeholder  # type: ignore[attr-defined]
