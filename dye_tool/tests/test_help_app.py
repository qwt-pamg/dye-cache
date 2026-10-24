# -*- coding: utf-8 -*-
"""V2.9.4 FR-71 / V2.9.3 FR-68：全应用灰因 collector（slow 档，仅 full）。"""
from __future__ import annotations

import pytest


@pytest.fixture
def qt_app():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_app_collector_all_disabled_controls_have_reason(qt_app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import (QAbstractButton, QComboBox, QDoubleSpinBox,
                                   QLineEdit, QSpinBox, QWidget)
    from core.config import default_config
    from ui.help_texts import BANNED_HELP, audit_help
    from ui.main_window import MainWindow
    monkeypatch.setenv('DYE_TOOL_CONFIG', str(tmp_path / 'cfg_help.yaml'))
    cfg = default_config()
    cfg.setdefault('gui', {})
    cfg['gui'].update({'show_welcome': False, 'out_root': str(tmp_path / 'runs'),
                       'recent_dirs': []})
    win = MainWindow(cfg)
    try:
        control_types = (QAbstractButton, QLineEdit, QSpinBox,
                         QDoubleSpinBox, QComboBox)
        missing = []
        checked = 0
        for w in win.findChildren(QWidget):
            if isinstance(w, control_types) and not w.isEnabled():
                checked += 1
                if not w.property('dsh_help_reason'):
                    missing.append(type(w).__name__)
        assert checked > 10
        assert not missing, f'缺少灰因的控件: {missing[:5]}'
        rows = audit_help(win)
        assert rows and not [r for r in rows if r["status"] == "missing"]
        banned = [r for r in rows if any(b in (r["static"] + r["reason"]) for b in BANNED_HELP)]
        assert not banned, banned[:3]
        import json
        (tmp_path / "help_audit.json").write_text(
            json.dumps({"rows": len(rows),
                        "disabled": sum(1 for r in rows if not r["enabled"]),
                        "missing": 0, "banned": 0}, ensure_ascii=False),
            encoding="utf-8")
    finally:
        win.close()
        win.deleteLater()
