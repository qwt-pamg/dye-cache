# -*- coding: utf-8 -*-
"""主页（FR-16 / FR-19）：问候 + 打开数据 / 一键演示 + 最近批次 + **命名速查**。

V2.0 的目标是"第一次打开的人不读 README 也能把一组实验做完"，所以主页不是
装饰性的欢迎页，它承担三件事：

1. **给入口**：打开数据目录（主按钮）、一键演示（完整任务流的可点击教程，FR-19）、
   最近批次一行一个，点一下直达；
2. **讲约定**：命名速查卡（README §2 的 GUI 版）+ "我的文件该怎么命名"向导；
3. **降门槛**：不需要改任何配置就能跑通（A16 判据之一）。

版面刻意安静：不是工作台，没有流程条和检查器；内容居中限宽，宽屏上不铺满。
"""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QSizePolicy, QVBoxLayout, QWidget)
from ui import theme


log = logging.getLogger("dye_tool.ui.home")

#: 内容区最大宽度（宽屏上居中，不铺满）
CONTENT_MAX_W = 1120
#: 最近批次最多列几行
RECENT_DIRS_MAX = 6
#: 数谱时最多看这么多个文件（防止误选巨型目录把主页卡住；超过显示"≥N"）
COUNT_CAP = 5000
#: 找"上次分析"时最多翻看最近这么多次运行
RUNS_SCAN_MAX = 300

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


@theme.register_qss
def _qss(p: dict) -> str:
    return f"""
/* ── 主页（ui/pages/home.py） ── */
QWidget#homeBody {{ background: {p['bg']}; }}
QLabel#homeGreeting {{ font-size: 24px; font-weight: bold; color: {p['text']}; }}
QPushButton#homeOpen {{ font-size: 14px; font-weight: bold; padding: 8px 18px; }}
QPushButton#homeDemo {{ padding: 8px 14px; }}
QFrame#homeRecentRow {{ background: transparent; border: none;
                       border-top: 1px solid {p['border']}; border-radius: 0; }}
QFrame#homeRecentRow:hover {{ background: {p['surface_alt']}; }}
QFrame#homeRecentRow[kbdFocus="true"] {{ background: {p['accent_soft']}; }}
QFrame#homeRecentRow QLabel {{ background: transparent; border: none; }}
QLabel#homeRowDate {{ color: {p['text_muted']}; font-family: {theme.MONO_FONTS}; font-size: 12px; }}
QLabel#homeRowName {{ color: {p['text']}; font-weight: bold; }}
QLabel#homeRowPath {{ color: {p['text_subtle']}; font-size: 11px; }}
QLabel#homeRowCount {{ color: {p['text']}; font-family: {theme.MONO_FONTS}; font-size: 12px; }}
QLabel#homeRowRun {{ color: {p['text_muted']}; font-size: 12px; }}
QLabel#homeRowRun[never="true"] {{ color: {p['text_subtle']}; }}
QLabel#homeRowArrow {{ color: {p['text_subtle']}; font-size: 16px; }}
QLabel#homeNaming {{ color: {p['text']}; }}
"""


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


def greeting(hour: Optional[int] = None) -> str:
    """按钟点问候（主页第一行）。"""
    h = datetime.now().hour if hour is None else int(hour)
    if 5 <= h < 11:
        return "早上好"
    if 11 <= h < 13:
        return "中午好"
    if 13 <= h < 18:
        return "下午好"
    return "晚上好"


def _short_time(ts: float, with_clock: bool = False) -> str:
    """今年内省略年份：``10-23`` / ``10-23 14:05``；往年带年份。"""
    dt = datetime.fromtimestamp(ts)
    fmt = "%m-%d" if dt.year == datetime.now().year else "%Y-%m-%d"
    return dt.strftime(fmt + (" %H:%M" if with_clock else ""))


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def count_spectra(d: Path, cfg: dict) -> tuple[int, float]:
    """目录里的光谱文件数 + 最新文件的修改时间（与数据页载入同一套扩展名/排除规则）。

    只数文件名、不解析内容；超过 :data:`COUNT_CAP` 个就停（返回 ``COUNT_CAP``）。
    出错返回 ``(-1, 目录自身 mtime)``。
    """
    from core.config import default_config
    from core.inputs.io import _iter_files, scan_extensions
    io_cfg = {**default_config()["io"], **dict((cfg or {}).get("io") or {})}
    try:
        exts = scan_extensions(io_cfg)
        excl = tuple(io_cfg.get("exclude", ["_"]))
        n, newest = 0, 0.0
        for p in _iter_files(Path(d), io_cfg):
            if p.suffix.lower() in exts and not p.name.startswith(excl) and p.is_file():
                n += 1
                newest = max(newest, p.stat().st_mtime)
                if n >= COUNT_CAP:
                    break
        return n, newest or Path(d).stat().st_mtime
    except OSError:
        try:
            return -1, Path(d).stat().st_mtime
        except OSError:
            return -1, 0.0


_DATA_DIR_RE = re.compile(r"^\s*data_dir:\s*(.+?)\s*$", re.M)


def run_data_dir(run_dir: Path) -> str:
    """某次运行分析的是哪个数据目录（读 ``run_params.yaml`` 的 ``_run.data_dir``）。"""
    try:
        text = (Path(run_dir) / "run_params.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    m = _DATA_DIR_RE.search(text)
    if not m:
        return ""
    v = m.group(1)
    if len(v) >= 2 and v[0] == v[-1] == "'":
        v = v[1:-1].replace("''", "'")
    elif len(v) >= 2 and v[0] == v[-1] == '"':
        v = v[1:-1].replace('\\\\', '\\')
    return v


class RecentRow(QFrame):
    """最近批次的一行：日期 · 目录名（下面一行完整路径）· 谱数 · 上次分析 · ›。

    整行可点（鼠标或 Tab 到这行后回车 / 空格），点了发 ``clicked``。
    """

    clicked = Signal()

    def __init__(self, path: Path, date: str, n_spectra: int, last_run: str,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("homeRecentRow")
        self.path = Path(path)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_Hover)
        self.setToolTip(f"载入这个目录：{self.path}")

        self.lbl_date = QLabel(date)
        self.lbl_date.setObjectName("homeRowDate")
        self.lbl_date.setFixedWidth(84)
        self.lbl_name = QLabel(self.path.name or str(self.path))
        self.lbl_name.setObjectName("homeRowName")
        self.lbl_path = QLabel(str(self.path.parent))
        self.lbl_path.setObjectName("homeRowPath")
        self.lbl_path.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.lbl_path.setToolTip(str(self.path))
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(self.lbl_name)
        names.addWidget(self.lbl_path)
        if n_spectra < 0:
            count = "—"
        elif n_spectra >= COUNT_CAP:
            count = f"≥{COUNT_CAP} 个谱"
        else:
            count = f"{n_spectra} 个谱"
        self.lbl_count = QLabel(count)
        self.lbl_count.setObjectName("homeRowCount")
        self.lbl_count.setFixedWidth(84)
        self.lbl_count.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.lbl_run = QLabel(f"上次分析 {last_run}" if last_run else "还没分析过")
        self.lbl_run.setObjectName("homeRowRun")
        self.lbl_run.setProperty("never", not last_run)
        self.lbl_run.setFixedWidth(150)
        arrow = QLabel("›")
        arrow.setObjectName("homeRowArrow")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(12)
        lay.addWidget(self.lbl_date)
        lay.addLayout(names, 1)
        lay.addWidget(self.lbl_count)
        lay.addWidget(self.lbl_run)
        lay.addWidget(arrow)
        self.setAccessibleName(f"最近批次 {self.lbl_name.text()}，{date}，{count}，"
                               f"{self.lbl_run.text()}")

    def resizeEvent(self, e) -> None:                    # noqa: N802 —— Qt 约定
        super().resizeEvent(e)
        # 路径太长时中间省略（开头的盘符和结尾的上级目录最有辨识度）
        full = str(self.path.parent)
        self.lbl_path.setText(self.lbl_path.fontMetrics().elidedText(
            full, Qt.ElideMiddle, max(self.lbl_path.width(), 40)))

    def mouseReleaseEvent(self, e) -> None:              # noqa: N802 —— Qt 约定
        if e.button() == Qt.LeftButton and self.rect().contains(e.position().toPoint()):
            self.clicked.emit()
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def keyPressEvent(self, e) -> None:                  # noqa: N802 —— Qt 约定
        if e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e) -> None:                   # noqa: N802 —— Qt 约定
        super().focusInEvent(e)
        theme.set_state(self, "kbdFocus", e.reason() != Qt.MouseFocusReason)

    def focusOutEvent(self, e) -> None:                  # noqa: N802 —— Qt 约定
        super().focusOutEvent(e)
        theme.set_state(self, "kbdFocus", False)


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
        #: 最近批次行（refresh 重建；测试 / 键盘导航用）
        self.recent_rows: list[RecentRow] = []
        #: 目录 → (目录 mtime, 谱数, 最新文件 mtime)；来回切页不重复数文件
        self._count_cache: dict = {}
        #: 运行目录 → 它分析的数据目录（规范化路径）
        self._run_dir_cache: dict = {}
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
        body.setObjectName("homeBody")
        center = QHBoxLayout(body)
        center.setContentsMargins(24, 28, 24, 24)
        column = QWidget()
        column.setMaximumWidth(CONTENT_MAX_W)
        lay = QVBoxLayout(column)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(18)
        center.addStretch(1)
        center.addWidget(column, 100)
        center.addStretch(1)

        lay.addLayout(self._greeting_row())
        cols = QHBoxLayout()
        cols.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._recent_card())
        left.addWidget(self._runs_card())
        left.addStretch(1)
        cols.addLayout(left, 3)
        right = QVBoxLayout()
        right.addWidget(self._naming_card())
        right.addStretch(1)
        cols.addLayout(right, 2)
        lay.addLayout(cols)
        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll)

    def _greeting_row(self) -> QHBoxLayout:
        """问候 + 一句话说明 …… 一键演示（次）/ 打开我的数据目录（主）。"""
        self.lbl_greeting = QLabel(greeting())
        self.lbl_greeting.setObjectName("homeGreeting")
        sub = QLabel("染料光谱数据分析：一组实验数据 → QC → Φ_Δ → 期刊图件。"
                     "手头没有数据，可以先点「一键演示」看它自己跑一遍。")
        sub.setWordWrap(True)
        theme.mark(sub, "muted")
        texts = QVBoxLayout()
        texts.setSpacing(4)
        texts.addWidget(self.lbl_greeting)
        texts.addWidget(sub)

        self.btn_demo = QPushButton("▶ 一键演示")
        self.btn_demo.setObjectName("homeDemo")
        self.btn_demo.setToolTip("生成一组已知真值的合成数据，"
                                 "从数据载入一路跑到出图与档案")
        self.btn_demo.clicked.connect(self.demo_requested.emit)
        self.btn_open = QPushButton("打开我的数据目录…")
        self.btn_open.setObjectName("homeOpen")
        theme.mark(self.btn_open, "primary")
        self.btn_open.setToolTip("选一个放着光谱文件的目录，载入到「数据」页")
        self.btn_open.clicked.connect(self.open_dir_requested.emit)

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addLayout(texts, 1)
        row.addWidget(self.btn_demo, 0, Qt.AlignVCenter)
        row.addWidget(self.btn_open, 0, Qt.AlignVCenter)
        return row

    @staticmethod
    def _card(title: str, hint: str = "") -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        theme.mark(card, "card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)
        head = QHBoxLayout()
        head.addWidget(theme.mark(QLabel(title), "section"))
        head.addStretch(1)
        if hint:
            head.addWidget(theme.mark(QLabel(hint), "hint"))
        lay.addLayout(head)
        return card, lay

    def _recent_card(self) -> QWidget:
        card, lay = self._card("最近批次", "点一行直接载入")
        self._recent_dirs_lay = QVBoxLayout()
        self._recent_dirs_lay.setSpacing(0)
        lay.addLayout(self._recent_dirs_lay)
        return card

    def _runs_card(self) -> QWidget:
        card, lay = self._card("最近结果", "在「档案」页查看")
        self._recent_runs_lay = QVBoxLayout()
        self._recent_runs_lay.setSpacing(0)
        lay.addLayout(self._recent_runs_lay)
        return card

    def _naming_card(self) -> QWidget:
        card, lay = self._card("文件该怎么命名？")
        html = NAMING_CARD.replace("\n\n<table", "<table").replace("\n\n", "<br><br>")
        view = QLabel(html.replace("\n", " "))
        view.setObjectName("homeNaming")
        view.setTextFormat(Qt.RichText)
        view.setWordWrap(True)
        view.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(view)
        self.btn_naming = QPushButton("按我的组名生成文件名…")
        self.btn_naming.setToolTip("填组名、角色和时间，告诉你文件该叫什么（不改动任何数据）")
        self.btn_naming.clicked.connect(self._show_naming_wizard)
        lay.addWidget(self.btn_naming, 0, Qt.AlignLeft)
        return card

    # ══════════════ 刷新 ══════════════
    def refresh(self) -> None:
        """重建"最近批次 / 最近结果"两块（配置或 runs/ 变化后调用）。"""
        self.lbl_greeting.setText(greeting())
        self._clear(self._recent_dirs_lay)
        self._clear(self._recent_runs_lay)
        self.recent_rows = []
        gui = (self.cfg or {}).get("gui") or {}
        dirs = [Path(d) for d in (gui.get("recent_dirs") or [])
                if Path(d).is_dir()][:RECENT_DIRS_MAX]
        if not dirs:
            self._recent_dirs_lay.addWidget(self._muted("还没有打开过数据目录"))
            # V2.6 FR-40.4：空态可点击引导（切页 + 聚焦①数据页入口）
            b_empty = QPushButton("去数据页载入数据…")
            b_empty.setToolTip("切到 数据 页，选择或拖入数据目录")
            theme.mark(b_empty, "link")
            b_empty.clicked.connect(lambda _=False: self.go.emit("data"))
            self._recent_dirs_lay.addWidget(b_empty)
        last_runs = self._last_runs_by_dir() if dirs else {}
        for d in dirs:
            info = self.batch_info(d, last_runs)
            row = RecentRow(d, info["date"], info["n_spectra"], info["last_run"])
            row.clicked.connect(lambda p=d: self._recent_dir_clicked(p))
            self._recent_dirs_lay.addWidget(row)
            self.recent_rows.append(row)
        runs = self._recent_runs(5)
        if not runs:
            self._recent_runs_lay.addWidget(self._muted("还没有结果（跑完第一次分析后这里会有直达链接）"))
        for r in runs:
            b = QPushButton(f"{r.parent.name} / {r.name}")
            b.setToolTip(str(r))
            theme.mark(b, "link")
            b.clicked.connect(lambda _=False, p=r: self._open_path(p))
            self._recent_runs_lay.addWidget(b)

    def batch_info(self, d: Path, last_runs: Optional[dict] = None) -> dict:
        """最近批次一行要显示的东西：``{date, n_spectra, last_run}``（只读磁盘，不解析谱）。"""
        try:
            mtime = Path(d).stat().st_mtime
        except OSError:
            mtime = 0.0
        key = _norm(d)
        hit = self._count_cache.get(key)
        if hit is None or hit[0] != mtime:
            n, newest = count_spectra(Path(d), self.cfg)
            hit = (mtime, n, newest)
            self._count_cache[key] = hit
        _, n, newest = hit
        if last_runs is None:
            last_runs = self._last_runs_by_dir()
        run_ts = last_runs.get(key)
        return {"date": _short_time(newest) if newest else "—",
                "n_spectra": n,
                "last_run": _short_time(run_ts, with_clock=True) if run_ts else ""}

    def _last_runs_by_dir(self) -> dict:
        """规范化数据目录 → 最近一次分析它的运行时间戳（翻看最近 RUNS_SCAN_MAX 次运行）。"""
        out: dict = {}
        for r in self._recent_runs(RUNS_SCAN_MAX):
            key = str(r)
            if key not in self._run_dir_cache:
                src = run_data_dir(r)
                self._run_dir_cache[key] = _norm(src) if src else ""
            src = self._run_dir_cache[key]
            if not src or src in out:
                continue
            try:
                out[src] = r.stat().st_mtime
            except OSError:
                continue
        return out

    def _recent_runs(self, n: int) -> list:
        """``runs/<染料>/<时间戳>/`` 里最新的 n 个（按 mtime 倒序）。

        B3（V2.x 审查）：原来每次 ``refresh()`` 都 ``rglob("run.log")`` 扫整个
        runs/ 树——磁盘 IO 随历史 run 数线性上涨，而主页每次回来都会刷新。
        这里加 **5 秒 TTL 缓存**（足够覆盖"来回切页"，又不会长期看不到新结果）。
        """
        now = time.monotonic()
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
                w.setParent(None)
                w.deleteLater()
