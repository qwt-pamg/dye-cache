# -*- coding: utf-8 -*-
"""「结果已过期」横幅（A25.1）：参数改了但结果没重算时出现，非模态。

横幅自己记住**上次分析用的配置快照**，按"变的是哪些分区"决定文案与动作：

* 只有出图参数（``plot``）变了 → 「重渲受影响图」，数值不重算；
* 数值相关分区变了 → 「重算分析」。

页面只需要在分析/重渲成功后 :meth:`StaleBanner.set_snapshot`，参数变化时
:meth:`StaleBanner.refresh`，并处理 ``action_requested(kind)``。
"""
from __future__ import annotations

import copy
import json
from typing import Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget

from ui import theme

#: 只影响**出图**的分区（变化 → 只需重渲，不重算数值）。
PLOT_LIKE = ("plot",)
#: 只影响**导出**、或对本页图件**无影响**的分区（变化 → 不提示过期）。
#: A10（V2.x 审查）：`spectra` 是作图台（FR-4）的参数，Φ_Δ 的图 a 用的是
#: `plot.jacs.spectra_overlay`，与它无关 → 算进来会误报"仅出图参数变了"。
EXPORT_ONLY = ("excel", "qc_dashboard", "spectra")


def _sections(cfg: dict) -> dict:
    """把 cfg 拆成"分区 → 序列化内容"（排除纯 UI 会话态，避免误报过期）。"""
    out: dict = {}
    for k, v in (cfg or {}).items():
        if str(k).startswith("_") or k == "gui":
            continue
        try:
            out[str(k)] = json.dumps(v, sort_keys=True, default=str)
        except Exception:                              # noqa: BLE001
            out[str(k)] = repr(v)
    return out


def _flatten(node, prefix: str = "") -> dict:
    """把嵌套字典展平成 ``{点分键: repr(值)}``（M5 归因用）。"""
    out: dict = {}
    for k, v in (node or {}).items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict):
            out.update(_flatten(v, path))
        else:
            out[path] = repr(v)
    return out


class StaleBanner(QFrame):
    """过期横幅 + 配置快照比对。"""

    #: ``"plot"`` → 只重渲受影响图；``"numeric"`` → 重算分析
    action_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("staleBanner")
        theme.mark(self, "banner")
        self.setVisible(False)
        self._snapshot: Optional[dict] = None
        self._kind = ""                    # "" | "plot" | "numeric"
        self._keys: list = []              # plot.jacs 内变过的叶子键（M5 细分用）
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        self.lbl = QLabel("")
        self.lbl.setWordWrap(True)
        self.btn_action = QPushButton("…")
        self.btn_hide = QPushButton("忽略")
        self.btn_hide.setToolTip("只隐藏提示；下次改参数会再次出现")
        lay.addWidget(self.lbl, 1)
        lay.addWidget(self.btn_action)
        lay.addWidget(self.btn_hide)
        self.btn_action.clicked.connect(lambda: self.action_requested.emit(self._kind))
        self.btn_hide.clicked.connect(self.dismiss)

    # ── 快照与比对 ──
    def set_snapshot(self, cfg: dict) -> None:
        """记下本次分析/重渲用的配置；之后参数再变才算"过期"。"""
        self._snapshot = copy.deepcopy(cfg)
        self.clear()

    def sections(self, cfg: dict, has_result: bool = True) -> list:
        """相对快照变过的分区名（无快照/无结果 → 空）。"""
        if not self._snapshot or not has_result:
            return []
        snap, now = _sections(self._snapshot), _sections(cfg)
        changed = [k for k in sorted(set(snap) | set(now)) if snap.get(k) != now.get(k)]
        return [k for k in changed if k not in EXPORT_ONLY]

    def changed_keys(self, cfg: dict, has_result: bool = True) -> list:
        """``plot.jacs`` 内相对快照变过的**叶子键**。"""
        if not self._snapshot or not has_result:
            return []
        try:
            old = _flatten(((self._snapshot.get("plot") or {}).get("jacs")) or {})
            now = _flatten(((cfg.get("plot") or {}).get("jacs")) or {})
        except Exception:                              # noqa: BLE001
            return []
        return [k for k in sorted(set(old) | set(now)) if old.get(k) != now.get(k)]

    @property
    def pending_keys(self) -> list:
        """横幅出现时记下的变化键（重渲时按它判定受影响的图）。"""
        return list(self._keys)

    # ── 显示 ──
    def refresh(self, cfg: dict, has_result: bool = True) -> None:
        """按"变的是哪些分区"决定横幅文案与动作（A25.1 分层重算）。"""
        secs = self.sections(cfg, has_result)
        if not secs:
            self.clear()
            return
        self._keys = self.changed_keys(cfg, has_result) if "plot" in secs else []
        if all(s in PLOT_LIKE for s in secs):
            self._kind = "plot"
            self.lbl.setText("⚠ 结果已过期：仅出图参数变了（数值未变）——可只重渲受影响图，"
                             "不必重算。")
            self.btn_action.setText("重渲受影响图")
        else:
            self._kind = "numeric"
            numeric = [s for s in secs if s not in PLOT_LIKE]
            self.lbl.setText("⚠ 结果已过期：数值相关参数变了（"
                             + "、".join(numeric[:4]) + ("…" if len(numeric) > 4 else "")
                             + "）——需要重算分析。")
            self.btn_action.setText("重算分析")
        self.setVisible(True)

    def clear(self) -> None:
        self._kind = ""
        self.setVisible(False)

    def dismiss(self) -> None:
        """用户点「忽略」：只隐藏（不改快照，下次再改参数仍会提示）。"""
        self.setVisible(False)

    def kind(self) -> str:
        return self._kind

    def is_shown(self) -> bool:
        return not self.isHidden()
