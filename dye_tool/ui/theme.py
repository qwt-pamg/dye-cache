# -*- coding: utf-8 -*-
"""界面主题：浅色 / 深色两套色板 + 全局 QSS + 控件角色。

用法：

* 启动时 :func:`apply` 一次（``gui.theme``：``light`` | ``dark`` | ``system``），
  设置页切换时再调一次即可整窗生效；
* 控件不再各自 ``setStyleSheet``，而是 :func:`mark` 一个角色（``hint``、``title``、
  ``warn``…），颜色与字号统一由这里的 QSS 决定；
* 表格单元格这类 QSS 管不到的前景色，用 :func:`color` 取当前色板的令牌。

图件（matplotlib 画布）**不跟主题变色**：那是要进论文的产物，始终白底。
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

THEMES = ("light", "dark", "system")
THEME_LABELS = {"light": "浅色", "dark": "深色", "system": "跟随系统"}

PALETTES: dict[str, dict[str, str]] = {
    "light": {
        "bg": "#f1f4f3", "surface": "#ffffff", "surface_alt": "#eef2f1",
        "nav_bg": "#e9eeed", "rail_bg": "#1e2524", "rail_text": "#a9b5b3", "border": "#d5dddb", "border_strong": "#bcc7c4",
        "text": "#15201f", "text_muted": "#5e6b69", "text_subtle": "#87938f",
        "accent": "#0b7a6e", "accent_hover": "#096659", "accent_text": "#ffffff",
        "accent_soft": "#ddf1ee", "series_ref": "#c2410c",
        "warn": "#9a5b00", "warn_bg": "#fbefd6", "warn_border": "#e7b85a",
        "danger": "#c81e1e", "danger_bg": "#fdecec", "success": "#1d7a46",
        "modified_bg": "#fff7e0", "hit_bg": "#fff3a0",
    },
    "dark": {
        "bg": "#0e1312", "surface": "#151b1a", "surface_alt": "#1b2221",
        "nav_bg": "#121817", "rail_bg": "#090c0c", "rail_text": "#8fa09d", "border": "#283433", "border_strong": "#3a4a47",
        "text": "#e1ece9", "text_muted": "#93a6a2", "text_subtle": "#6c7d79",
        "accent": "#3cc3b2", "accent_hover": "#5fd3c4", "accent_text": "#062420",
        "accent_soft": "#173a36", "series_ref": "#fb8a4c",
        "warn": "#f2b35b", "warn_bg": "#3a2c12", "warn_border": "#8a5d17",
        "danger": "#ff6b6b", "danger_bg": "#3d1d1f", "success": "#5fcb8c",
        "modified_bg": "#3b3418", "hit_bg": "#5c4d00",
    },
}

MONO_FONTS = '"Cascadia Mono", Consolas, Menlo, "DejaVu Sans Mono", monospace'

_current = "light"
_applied = False

#: 组件自带的 QSS 片段：``fn(palette) -> str``，拼在全局 QSS 之后
_EXTRA_QSS: list[Callable[[dict[str, str]], str]] = []


def register_qss(fn: Callable[[dict[str, str]], str]) -> Callable[[dict[str, str]], str]:
    """登记组件自己的 QSS（模块导入时调用，可当装饰器）。

    新组件把样式写在自己的模块里，不必改这个文件；主题已装上时立即重装一次。
    """
    if fn not in _EXTRA_QSS:
        _EXTRA_QSS.append(fn)
        app = QApplication.instance()
        if _applied and app is not None:
            app.setStyleSheet(stylesheet(_current))
    return fn


def resolve(mode: Optional[str]) -> str:
    """``light`` / ``dark`` / ``system`` → 实际色板名（未知值按浅色）。"""
    mode = str(mode or "light").lower()
    if mode == "system":
        # Qt.ColorScheme 自 Qt 6.5 起才有；更老的 PySide6 或无 QApplication 时按浅色
        if QGuiApplication.instance() is None:
            return "light"
        try:
            from PySide6.QtCore import Qt
            dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:
            return "light"
        return "dark" if dark else "light"
    return mode if mode in PALETTES else "light"


def current() -> str:
    return _current


def color(token: str) -> QColor:
    """当前色板里的颜色（表格前景色等 QSS 管不到的地方用）。"""
    return QColor(PALETTES[_current][token])


def hexc(token: str) -> str:
    return PALETTES[_current][token]


def mark(widget: QWidget, role: str) -> QWidget:
    """给控件标一个样式角色（见 :func:`stylesheet` 里的 ``[role="…"]``）。"""
    widget.setProperty("role", role)
    _repolish(widget)
    return widget


def set_state(widget: QWidget, name: str, on: bool) -> None:
    """切换动态状态属性（``modified`` / ``invalid``），并让 QSS 立即重算。"""
    if bool(widget.property(name)) == bool(on):
        return
    widget.setProperty(name, bool(on))
    _repolish(widget)


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _palette(p: dict[str, str]) -> QPalette:
    pal = QPalette()
    c = {k: QColor(v) for k, v in p.items()}
    roles = {
        QPalette.Window: c["bg"], QPalette.WindowText: c["text"],
        QPalette.Base: c["surface"], QPalette.AlternateBase: c["surface_alt"],
        QPalette.Text: c["text"], QPalette.Button: c["surface"],
        QPalette.ButtonText: c["text"], QPalette.BrightText: c["danger"],
        QPalette.ToolTipBase: c["surface"], QPalette.ToolTipText: c["text"],
        QPalette.Highlight: c["accent"], QPalette.HighlightedText: c["accent_text"],
        QPalette.Link: c["accent"], QPalette.PlaceholderText: c["text_subtle"],
        QPalette.Mid: c["border"], QPalette.Midlight: c["surface_alt"],
        QPalette.Dark: c["border_strong"], QPalette.Shadow: c["border_strong"],
        QPalette.Light: c["surface"],
    }
    for role, col in roles.items():
        pal.setColor(role, col)
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, c["text_subtle"])
    return pal


def _arrow_files(name: str) -> dict[str, str]:
    """QSS 箭头只能引用文件：按色板把两枚 SVG 小三角写到程序目录的 .preview_tmp/theme
    （与实时预览同一个已忽略的目录；不写系统 %TEMP%），内容不变就不重写。"""
    from core.paths import app_root
    d = app_root() / ".preview_tmp" / "theme"
    d.mkdir(parents=True, exist_ok=True)
    fill = PALETTES[name]["text_muted"]
    shapes = {"up": "0,5 4,0 8,5", "down": "0,0 8,0 4,5"}
    out = {}
    for key, pts in shapes.items():
        f = d / f"arrow_{key}_{name}.svg"
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="8" height="5">'
               f'<polygon points="{pts}" fill="{fill}"/></svg>')
        if not f.is_file() or f.read_text(encoding="utf-8") != svg:
            f.write_text(svg, encoding="utf-8")
        out[key] = f.as_posix()
    return out


def stylesheet(name: str) -> str:
    p = PALETTES[name]
    arrows = _arrow_files(name)
    return f"""
QMainWindow, QDialog {{ background: {p['bg']}; }}
QToolTip {{ background: {p['surface']}; color: {p['text']};
           border: 1px solid {p['border_strong']}; padding: 4px 6px; }}

/* ── 容器 ── */
QGroupBox {{ background: {p['surface']}; border: 1px solid {p['border']};
            border-radius: 6px; margin-top: 10px; padding: 6px 2px 2px 2px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                   color: {p['text_muted']}; font-weight: bold; }}
QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: 6px;
                   background: {p['surface']}; top: -1px; }}
QTabBar::tab {{ padding: 5px 14px; background: transparent; color: {p['text_muted']};
               border: none; border-bottom: 2px solid transparent; margin-right: 2px; }}
QTabBar::tab:selected {{ color: {p['text']}; font-weight: bold;
                        border-bottom: 2px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {p['text']}; }}
QSplitter::handle {{ background: {p['bg']}; }}
QDockWidget {{ color: {p['text_muted']}; }}
QDockWidget::title {{ background: {p['surface_alt']}; padding: 4px 8px;
                     border-top: 1px solid {p['border']}; }}
QStatusBar {{ background: {p['surface_alt']}; color: {p['text_muted']};
             border-top: 1px solid {p['border']}; }}
QMenuBar {{ background: {p['bg']}; }}
QMenu {{ background: {p['surface']}; border: 1px solid {p['border']}; }}
QMenu::item:selected {{ background: {p['accent_soft']}; color: {p['text']}; }}

/* ── 按钮 ── */
QPushButton {{ background: {p['surface']}; color: {p['text']};
              border: 1px solid {p['border_strong']}; border-radius: 5px;
              padding: 3px 10px; }}
QPushButton:hover {{ border-color: {p['accent']}; }}
QPushButton:pressed {{ background: {p['accent_soft']}; }}
QPushButton:disabled {{ color: {p['text_subtle']}; border-color: {p['border']};
                       background: {p['surface_alt']}; }}
QPushButton[role="primary"] {{ background: {p['accent']}; color: {p['accent_text']};
                              border: 1px solid {p['accent']}; font-weight: bold; }}
QPushButton[role="primary"]:hover {{ background: {p['accent_hover']}; }}
QPushButton[role="primary"]:disabled {{ background: {p['surface_alt']};
                                       color: {p['text_subtle']};
                                       border-color: {p['border']}; }}
QPushButton[role="link"] {{ text-align: left; padding: 4px 8px; border: none;
                           background: transparent; color: {p['accent']}; }}
QPushButton[role="link"]:hover {{ background: {p['accent_soft']}; }}
QToolButton {{ border-radius: 4px; padding: 2px; }}
QToolButton:hover {{ background: {p['accent_soft']}; }}

/* ── 输入 ── */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p['surface']}; color: {p['text']};
    border: 1px solid {p['border_strong']}; border-radius: 4px; padding: 2px 4px;
    selection-background-color: {p['accent']}; selection-color: {p['accent_text']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{ border-color: {p['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{
    background: {p['surface_alt']}; color: {p['text_subtle']}; }}
QComboBox QAbstractItemView {{ background: {p['surface']}; color: {p['text']};
                              selection-background-color: {p['accent_soft']};
                              selection-color: {p['text']}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; width: 16px; border: none;
    border-left: 1px solid {p['border']}; background: {p['surface_alt']}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right;
    border-top-right-radius: 4px; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right;
    border-bottom-right-radius: 4px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {p['accent_soft']}; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{arrows['up']}");
    width: 8px; height: 5px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{arrows['down']}");
    width: 8px; height: 5px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
    width: 18px; border: none; }}
QComboBox::down-arrow {{ image: url("{arrows['down']}"); width: 8px; height: 5px; }}
*[modified="true"], *[e_modified="true"] {{ background: {p['modified_bg']}; }}
*[invalid="true"] {{ border: 1px solid {p['danger']}; background: {p['danger_bg']}; }}

/* ── 表格 / 列表 ── */
QTableView, QTableWidget, QTreeView, QListView, QListWidget {{
    background: {p['surface']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: 4px;
    gridline-color: {p['surface_alt']};
    selection-background-color: {p['accent_soft']}; selection-color: {p['text']}; }}
QHeaderView::section {{ background: {p['surface_alt']}; color: {p['text_muted']};
                       border: none; border-right: 1px solid {p['border']};
                       border-bottom: 1px solid {p['border']}; padding: 3px 3px; }}

/* ── 左侧导航 ── */
NavBar {{ background: {p['nav_bg']}; border-right: 1px solid {p['border']}; }}
QLabel#navTitle {{ font-size: 15px; font-weight: bold; color: {p['text']}; }}
QLabel#navContext {{ color: {p['text_subtle']}; font-size: 11px; }}
QListWidget#navList {{ background: transparent; border: none; outline: none; }}
QListWidget#navList::item {{ padding: 7px 10px; margin: 1px 6px; border-radius: 5px;
                            color: {p['text_muted']}; }}
QListWidget#navList::item:hover {{ background: {p['surface_alt']}; color: {p['text']}; }}
QListWidget#navList::item:selected {{ background: {p['accent_soft']}; color: {p['accent']};
                                     font-weight: bold; }}

/* ── ② 分析页任务列表 ── */
QListWidget#taskList {{ background: transparent; border: none; outline: none; }}
QListWidget#taskList::item {{ padding: 7px 8px; margin: 2px 0; border-radius: 6px;
                             border: 1px solid {p['border']}; background: {p['surface']};
                             color: {p['text']}; }}
QListWidget#taskList::item:hover {{ border-color: {p['accent']}; }}
QListWidget#taskList::item:selected {{ background: {p['accent_soft']};
                                      border-color: {p['accent']}; color: {p['text']}; }}

/* ── 文字角色（ui.theme.mark） ── */
QLabel[role="hero"] {{ font-size: 22px; font-weight: bold; color: {p['text']}; }}
QLabel[role="title"] {{ font-size: 16px; font-weight: bold; color: {p['text']}; }}
QLabel[role="section"] {{ font-size: 14px; font-weight: bold; color: {p['text']}; }}
QLabel[role="strong"], QCheckBox[role="strong"] {{ font-weight: bold; }}
QLabel[role="muted"], QCheckBox[role="muted"] {{ color: {p['text_muted']}; }}
QLabel[role="hint"] {{ color: {p['text_subtle']}; font-size: 11px; }}
QLabel[role="subtle"] {{ color: {p['text_subtle']}; }}
QLabel[role="warn"] {{ color: {p['warn']}; }}
QLabel[role="warnHint"] {{ color: {p['warn']}; font-size: 11px; }}
QLabel[role="danger"] {{ color: {p['danger']}; font-weight: bold; }}
QLabel[role="success"] {{ color: {p['success']}; }}
*[role="mono"] {{ font-family: {MONO_FONTS}; font-size: 12px; }}
QLabel[role="codeBlock"] {{ font-family: {MONO_FONTS}; font-size: 13px; padding: 8px;
                           background: {p['surface_alt']}; border-radius: 4px; }}
QFrame[role="banner"] {{ background: {p['warn_bg']}; border: 1px solid {p['warn_border']};
                        border-radius: 6px; }}
QFrame[role="card"] {{ background: {p['surface']}; border: 1px solid {p['border']};
                      border-radius: 8px; }}
*[role="plain"] {{ border: none; background: transparent; }}
""" + "".join(fn(p) for fn in _EXTRA_QSS)


def apply(mode: Optional[str] = None, app: Optional[QApplication] = None) -> str:
    """把主题装到整个应用（Fusion 风格 + 色板 + 全局 QSS），返回实际色板名。"""
    global _current, _applied
    app = app or QApplication.instance()
    _current = resolve(mode)
    if app is None:
        return _current
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    app.setPalette(_palette(PALETTES[_current]))
    app.setStyleSheet(stylesheet(_current))
    _applied = True
    return _current
