#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filepick.py — tkinter 可视化文件/文件夹选择 
  pick_data(initial)  → ("dir", Path) | ("files", [Path, ...]) | None
  pick_dir(initial)   → Path | None
  pick_files(initial) → [Path, ...]
无 tkinter 环境: HAS_TK = False, 各函数返回空, 调用方自行回退。
"""
from __future__ import annotations
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog
    HAS_TK = True
except Exception:
    HAS_TK = False

FILETYPES = [("光谱文件", "*.txt *.csv *.dat"), ("所有文件", "*.*")]


def _root():
    r = tk.Tk()
    r.withdraw()
    try:
        r.attributes("-topmost", True)
    except Exception:
        pass
    return r


def pick_dir(initial=None, title="选择数据文件夹"):
    """弹窗选一个文件夹; 取消 → None"""
    if not HAS_TK:
        return None
    r = _root()
    try:
        p = filedialog.askdirectory(parent=r, title=title,
                                    initialdir=str(initial) if initial else None)
    finally:
        r.destroy()
    return Path(p) if p else None


def pick_files(initial=None, title="选择光谱文件 (可 Ctrl/Shift 多选)"):
    """弹窗多选文件; 取消 → []"""
    if not HAS_TK:
        return []
    r = _root()
    try:
        ps = filedialog.askopenfilenames(
            parent=r, title=title,
            initialdir=str(initial) if initial else None,
            filetypes=list(FILETYPES))
    finally:
        r.destroy()
    return [Path(p) for p in ps]


def pick_data(initial=None):
    """先弹一个小窗选模式 (文件夹 / 文件), 再弹对应系统对话框。
    取消 → None。"""
    if not HAS_TK:
        return None
    r = tk.Tk()
    r.title("选择数据来源")
    r.resizable(False, False)
    try:
        r.attributes("-topmost", True)
    except Exception:
        pass
    choice = {"v": None}

    def _set(v):
        choice["v"] = v
        r.destroy()

    tk.Label(r, text="请选择数据来源:", padx=14, pady=8).pack()
    bar = tk.Frame(r)
    bar.pack(pady=(0, 12))
    tk.Button(bar, text="选择文件夹", width=12,
              command=lambda: _set("dir")).pack(side="left", padx=6)
    tk.Button(bar, text="选择文件", width=12,
              command=lambda: _set("files")).pack(side="left", padx=6)
    tk.Button(bar, text="取消", width=6,
              command=lambda: _set(None)).pack(side="left", padx=6)
    try:
        r.mainloop()
    except Exception:
        return None

    v = choice["v"]
    if v == "dir":
        d = pick_dir(initial)
        return ("dir", d) if d else None
    if v == "files":
        fs = pick_files(initial)
        return ("files", fs) if fs else None
    return None