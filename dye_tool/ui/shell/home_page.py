# -*- coding: utf-8 -*-
"""主页（FR-16 / FR-19）：欢迎 + 最近批次 + 最近结果 + 一键演示 + **命名速查**。

V2.0 的目标是"第一次打开的人不读 README 也能把一组实验做完"，所以主页不是
装饰性的欢迎页，它承担三件事：

1. **给入口**：一键演示（完整任务流的可点击教程，FR-19）、打开数据目录、最近批次/结果直达；
2. **讲约定**：命名速查卡（README §2 的 GUI 版）+ "我的文件该怎么命名"向导；
3. **降门槛**：不需要改任何配置就能跑通（A16 判据之一）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QScrollArea, QTextBrowser,
                               QVBoxLayout, QWidget)
from ui import theme


log = logging.getLogger("dye_tool.ui.home")

#: 命名约定速查（与 ``ui/main_window.NAMING_HELP`` 同源，但这里是"卡片"排版）。
#: FR-19 明确要求"主页内置命名约定速查卡（README §2 的 GUI 版）"。
NAMING_CARD = """<b>组名</b><code>[_重复号]</code><b>-秒数</b><code>[+分钟数]</code> .txt

<table cellpadding="2">
<tr><td><code>Resorufin-pbs-TEST-90.txt</code></td><td>组 Resorufin-pbs-TEST，t = 90 s</td></tr>
<tr><td><code>Resorufin-pbs-t-90+5.txt</code></td><td>t = 90 + 60×5 = 390 s</td></tr>
<tr><td><code>sample_2-30.txt</code></td><td>组 sample 第 2 次平行，t = 30 s</td></tr>
<tr><td><code>Resorufin-pbs-t.txt</code></td><td>无时间后缀 → t = 0（暗态点）</td></tr>
</table>

<b>角色前缀</b>：<code>bg</code>=背景　<code>ref</code>=参比　<code>dark</code>=暗对照
　<code>n2</code>=N₂　<code>nan3</code>/<code>azide</code>=淬灭剂　<code>blank</code>=空白
　其余 = 样品

<b>文件格式</b>：紫外-可见两列「波长,吸光度」；荧光为仪器导出的两列文本；
<b>.fds / .spc 仪器二进制可直接拖进来</b>。

⚠ 程序对原始数据<b>只读</b>，不会修改、移动或重命名任何输入文件。"""


#: 角色前缀（与 ``core/io.parse_time_only`` / 命名约定同源：sample 不加前缀）
_ROLE_PREFIX = {
    "sample": "", "ref": "ref-", "background": "bg-", "blank": "blank-",
    "dark": "dark-", "n2": "n2-", "quencher": "nan3-", "reference": "ref-",
}


def suggest_filename(group: str, role: str, t_s: float) -> str:
    """按命名约定**生成**一个建议文件名（FR-19 向导的规则本体，可单测）。

    规则（README §2 的机器版）：

    * 组名去空格换连字符（文件名里空格麻烦）；
    * 角色：``sample`` 不加前缀，其余加 ``bg-`` / ``ref-`` / ``dark-`` / …
    * 时间：**整数秒**直接写；小数用 ``p`` 代替小数点（``1.5`` → ``1p5``）——
      因为文件名里的 ``.`` 会被当成扩展名分隔符；
    * 分钟用 ``+N`` 后缀表示（``-90+5`` = 390 s），本函数不生成分钟后缀，
      只在提示里说明。

    >>> suggest_filename("Resorufin-pbs", "sample", 90)
    'Resorufin-pbs-90.txt'
    >>> suggest_filename("bg run", "background", 0)
    'bg-bg-run-0.txt'
    """
    g = str(group or "sample").strip().replace(" ", "-") or "sample"
    r = str(role or "sample").strip().lower()
    prefix = _ROLE_PREFIX.get(r, "")
    tv = float(t_s or 0.0)
    if abs(tv - round(tv)) < 1e-9:
        ts = str(int(round(tv)))
    else:
        ts = f"{tv:g}".replace(".", "p")
    return f"{prefix}{g}-{ts}.txt"


class HomePage(QWidget):
    """主页。信号 ``demo_requested`` / ``open_dir_requested`` / ``go(str)``。"""

    demo_requested = Signal()
    open_dir_requested = Signal()
    go = Signal(str)                     # 请求切到某个导航项（如 "data"/"analysis"）
    #: V2.6 FR-40.4：最近批次直达（主窗口据此载入目录并切换批次）
    recent_dir_requested = Signal(str)

    def __init__(self, cfg: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.cfg = cfg or {}
        self._build()
        self.refresh()

    # ══════════════ 构建 ══════════════
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(22, 18, 22, 22)
        lay.setSpacing(14)

        title = QLabel("染料光谱数据分析")
        theme.mark(title, "hero")
        lay.addWidget(title)
        sub = QLabel("一组实验数据 → QC → Φ_Δ → 期刊图件。"
                     "不读文档也能做完：照下面三步走，或点「一键演示」看它自己跑一遍。")
        sub.setWordWrap(True)
        theme.mark(sub, "muted")
        lay.addWidget(sub)

        # ── ① 三步开始 ──
        lay.addWidget(self._steps_box())
        # ── ② 最近批次 / 最近结果 ──
        lay.addWidget(self._recent_box())
        # ── ③ 命名速查 ──
        lay.addWidget(self._naming_box())
        # ── ④ 格式与帮助 ──
        lay.addWidget(self._help_box())
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _steps_box(self) -> QWidget:
        box = QGroupBox("三步开始")
        g = QGridLayout(box)
        g.setColumnStretch(1, 1)
        self.btn_demo = QPushButton("▶ 一键演示（自动跑完整流程）")
        self.btn_demo.setToolTip("生成一组已知真值的合成数据，"
                                 "从数据载入一路跑到出图与档案")
        self.btn_demo.clicked.connect(self.demo_requested.emit)
        self.btn_open = QPushButton("打开我的数据目录…")
        self.btn_open.clicked.connect(self.open_dir_requested.emit)
        self.btn_naming = QPushButton("我的文件该怎么命名？")
        self.btn_naming.clicked.connect(self._show_naming_wizard)
        g.addWidget(QLabel("1"), 0, 0)
        g.addWidget(QLabel("让程序认识你的数据：照命名约定放文件，或点「一键演示」看它跑一遍。"),
                  0, 1)
        g.addWidget(self.btn_open, 1, 0)
        g.addWidget(QLabel("检查批次：① 数据页看文件表、角色/时间是否需要整定、QC 徽标有没有红。"),
                  1, 1)
        g.addWidget(self.btn_demo, 2, 0)
        g.addWidget(QLabel("② 分析页选任务（Φ_Δ / kobs / ε / A@λ）→ 开始 → "
                           "满意后「定稿出图」。"), 2, 1)
        g.addWidget(self.btn_naming, 3, 0, 1, 2)
        return box

    def _recent_box(self) -> QWidget:
        box = QGroupBox("最近")
        lay = QVBoxLayout(box)
        self._recent_dirs_lay = QVBoxLayout()
        self._recent_runs_lay = QVBoxLayout()
        lay.addWidget(QLabel("<b>最近批次目录</b>"))
        lay.addLayout(self._recent_dirs_lay)
        lay.addWidget(QLabel("<b>最近结果</b>"))
        lay.addLayout(self._recent_runs_lay)
        return box

    def _naming_box(self) -> QWidget:
        box = QGroupBox("命名速查")
        lay = QVBoxLayout(box)
        view = QTextBrowser()
        view.setObjectName("namingCard")
        view.setOpenExternalLinks(False)
        view.setHtml(NAMING_CARD)
        view.setMinimumHeight(210)
        theme.mark(view, "plain")
        lay.addWidget(view)
        return box

    def _help_box(self) -> QWidget:
        box = QGroupBox("常用入口")
        row = QHBoxLayout(box)
        for text, key in (("① 数据", "data"), ("② 分析", "analysis"),
                          ("③ 图件", "plot"), ("④ 档案", "archive"),
                          ("⑤ 设置", "settings")):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, k=key: self.go.emit(k))
            row.addWidget(b)
        row.addStretch(1)
        return box

    # ══════════════ 刷新 ══════════════
    def refresh(self) -> None:
        """重建"最近批次 / 最近结果"两块（配置或 runs/ 变化后调用）。"""
        self._clear(self._recent_dirs_lay)
        self._clear(self._recent_runs_lay)
        gui = (self.cfg or {}).get("gui") or {}
        dirs = [d for d in (gui.get("recent_dirs") or []) if Path(d).is_dir()][:5]
        if not dirs:
            self._recent_dirs_lay.addWidget(self._muted("还没有打开过数据目录"))
            # V2.6 FR-40.4：空态可点击引导（切页 + 聚焦①数据页入口）
            b_empty = QPushButton("去①数据页载入数据…")
            b_empty.setToolTip("切到 ① 数据页，选择或拖入数据目录")
            theme.mark(b_empty, "link")
            b_empty.clicked.connect(lambda _=False: self.go.emit("data"))
            self._recent_dirs_lay.addWidget(b_empty)
        for d in dirs:
            b = QPushButton(f"📁 {Path(d).name}    {d}")
            b.setToolTip(d)
            theme.mark(b, "link")
            b.clicked.connect(lambda _=False, p=Path(d): self._recent_dir_clicked(p))
            self._recent_dirs_lay.addWidget(b)
        runs = self._recent_runs(5)
        if not runs:
            self._recent_runs_lay.addWidget(self._muted("还没有结果（跑完第一次分析后这里会有直达链接）"))
        for r in runs:
            b = QPushButton(f"🗂 {r.parent.name} / {r.name}")
            b.setToolTip(str(r))
            theme.mark(b, "link")
            b.clicked.connect(lambda _=False, p=r: self._open_path(p))
            self._recent_runs_lay.addWidget(b)

    def _recent_runs(self, n: int) -> list:
        """``runs/<染料>/<时间戳>/`` 里最新的 n 个（按 mtime 倒序）。

        B3（V2.x 审查）：原来每次 ``refresh()`` 都 ``rglob("run.log")`` 扫整个
        runs/ 树——磁盘 IO 随历史 run 数线性上涨，而主页每次回来都会刷新。
        这里加 **5 秒 TTL 缓存**（足够覆盖"来回切页"，又不会长期看不到新结果）。
        """
        import time as _time
        now = _time.monotonic()
        cache = getattr(self, "_runs_cache", None)
        if cache is not None and now - cache[0] < 5.0:
            return cache[1][:n]
        root = self._out_root()
        if not root.is_dir():
            self._runs_cache = (now, [])
            return []
        found: list = []
        try:
            for d in root.rglob("run.log"):
                found.append(d.parent)
        except OSError:
            return []
        found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        self._runs_cache = (now, found)
        return found[:n]

    def _out_root(self) -> Path:
        from core.paths import resolve_out_root
        return resolve_out_root(self.cfg)

    # ══════════════ 交互 ══════════════
    def _show_naming_wizard(self) -> None:
        """「我的文件该怎么命名」向导（FR-19）：问答式给出建议文件名。"""
        from PySide6.QtWidgets import QDialog, QLineEdit, QComboBox, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("我的文件该怎么命名？")
        dlg.resize(560, 300)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("填写三项，程序告诉你文件该叫什么："))
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("组名"))
        name = QLineEdit("Resorufin-2I-pbs-TEST")
        row1.addWidget(name, 1)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("角色"))
        role = QComboBox()
        role.addItems(["样品 sample", "参比 ref", "背景 bg", "暗对照 dark",
                       "N₂ n2", "淬灭剂 nan3", "空白 blank"])
        row2.addWidget(role, 1)
        lay.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("时间 t (s)"))
        t = QLineEdit("90")
        row3.addWidget(t, 1)
        lay.addLayout(row3)
        out = QLabel("")
        out.setTextInteractionFlags(Qt.TextSelectableByMouse)
        theme.mark(out, "codeBlock")
        lay.addWidget(out)
        hint = QLabel("")
        hint.setWordWrap(True)
        theme.mark(hint, "muted")
        lay.addWidget(hint)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)

        def _update() -> None:
            try:
                tv = float(t.text() or 0)
            except ValueError:
                tv = 0.0
            fname = suggest_filename(name.text(), role.currentText().split()[-1], tv)
            out.setText(fname)
            hint.setText("样品（sample）不加前缀，其余角色一律加前缀（bg-/ref-/dark-…）。"
                         "时间必须是整数秒；分钟请用 +N 后缀（如 -90+5 = 390 s）。"
                         "程序对原始文件只读，改名字是你的自由，只要符合约定。")
            _ = fname
        for w in (name, t):
            w.textChanged.connect(lambda *_: _update())
        role.currentIndexChanged.connect(lambda *_: _update())
        _update()
        dlg.exec()

    def _recent_dir_clicked(self, p: Path) -> None:
        """最近批次直达：主窗口负责切到①数据页 + 载入该目录（与文件菜单同源）。"""
        self.recent_dir_requested.emit(str(p))

    def _open_path(self, p: Path) -> None:
        self.go.emit("archive")
        _ = p

    # ══════════════ 工具 ══════════════
    @staticmethod
    def _muted(text: str) -> QLabel:
        lb = QLabel(text)
        theme.mark(lb, "subtle")
        return lb

    @staticmethod
    def _clear(lay) -> None:
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
