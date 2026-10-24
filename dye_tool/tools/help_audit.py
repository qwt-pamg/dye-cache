# -*- coding: utf-8 -*-
"""V2.9.6：全应用悬停帮助审计（M0/M3 可重复运行；不跑冒烟）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("DYE_TOOL_SMOKE", "1")
    os.environ.setdefault("MPLBACKEND", "Agg")
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    from core.config import default_config
    from ui.help_texts import BANNED_HELP, _is_internal, audit_help
    from ui.window.main_window import MainWindow
    cfg = default_config()
    cfg.setdefault("gui", {}).update({"show_welcome": False, "recent_dirs": [],
                                      "out_root": str(ROOT / "test_temp" / "help_audit_runs")})
    win = MainWindow(cfg)
    win.resize(1600, 900)
    win.show()
    app.processEvents()
    rows = audit_help(win)
    disabled = [r for r in rows if not r["enabled"]]
    missing = [r for r in rows if r["status"] == "missing"]
    generic = [r for r in rows if r["status"] == "generic"]
    banned = [r for r in rows if any(b in (r["static"] + r["reason"]) for b in BANNED_HELP)]
    empty = [r for r in rows if r["enabled"] and not r["static"]]
    internal_disabled = [w for w in win.findChildren(QWidget)
                         if _is_internal(w) and not w.isEnabled()]
    payload = {
        "rows": len(rows), "disabled": len(disabled),
        "internal_disabled": len(internal_disabled),
        "disabled_total": len(disabled) + len(internal_disabled),
        "missing": len(missing), "banned": len(banned), "empty_enabled": len(empty),
        "generic": len(generic),
    }
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_temp" / "help_audit.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    win.close()
    win.deleteLater()
    app.processEvents()
    return 0 if not missing and not banned and not generic and not empty else 1


if __name__ == "__main__":
    raise SystemExit(main())
