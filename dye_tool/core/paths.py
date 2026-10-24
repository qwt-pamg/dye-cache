# -*- coding: utf-8 -*-
"""路径解析层：区分「只读资源」与「可写应用目录」，兼容 PyInstaller 打包。

问题：PyInstaller one-folder 会把 ``datas`` 放进 ``dist/dye_tool/_internal/``，
而 ``__file__`` 在冻结后指向 ``_internal`` 内部。若把 ``config.yaml`` 与
``runs/`` 也定位到 ``_internal``，用户既找不到也难以写入（可能无权限）。

因此本模块区分两个根：

* :func:`resource_root` —— **只读随包资源**（``samples/``、``README.md``）：
  冻结时 = ``sys._MEIPASS``（即 ``_internal``）；否则 = 项目根。
* :func:`app_root` —— **可写应用目录**（``config.yaml``、``runs/``）：
  冻结时 = exe 所在目录（用户看得见、可写）；否则 = 项目根。
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

__all__ = ["is_frozen", "resource_root", "app_root", "default_out_root",
           "resolve_out_root", "default_samples_dir", "config_path",
           "CONFIG_ENV_VAR", "is_reparse_point", "walk_files"]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """只读随包资源根目录。"""
    if is_frozen():
        # PyInstaller ≥ 6 的 one-folder 会把资源放在 _internal（即 _MEIPASS）
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
        return Path(sys.executable).resolve().parent / "_internal"
    return Path(__file__).resolve().parents[1]


def app_root() -> Path:
    """可写应用目录：配置与输出落在这里。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def default_out_root() -> Path:
    """默认结果输出根目录（``<app_root>/runs``）。"""
    return app_root() / "runs"


def resolve_out_root(cfg, out_root=None) -> Path:
    """输出根目录的**唯一解析**（V2.5 FR-29 / 审查 D10 单源化）。

    优先级：显式 ``out_root`` > ``cfg["gui"]["out_root"]`` > ``"runs"``；
    **相对路径统一按 :func:`app_root`**（可写应用目录）解析，不再按 ``cwd``。

    为什么要单源（D10 实测，见《设计说明》§6.22）：此前 excel_export 按
    ``Path.cwd()``、Φ_Δ/主页/设置页按 app_root、pipeline 又按 cwd——从项目目录
    以外启动 GUI 时，Excel 汇总与 runs 扫描会指向不同目录（"报表里少了本次 run"）。

    CLI 不受影响：``core.cli.resolve_cli_out_root`` 在**参数边界**把显式 ``-o``
    相对路径按调用时 cwd 解析成绝对路径，pipeline 拿到的永远是绝对路径。
    """
    gui = (cfg or {}).get("gui") if isinstance(cfg, dict) else None
    if not isinstance(gui, dict):
        gui = {}
    raw = out_root if out_root is not None else gui.get("out_root", "runs")
    p = Path(str(raw or "runs"))
    return p if p.is_absolute() else app_root() / p


def default_samples_dir() -> Path:
    """随附样例数据目录（``<resource_root>/samples``）。"""
    return resource_root() / "samples"


#: 覆盖配置文件位置的**唯一**环境变量（测试与多实例隔离用）。
#: 存在的理由（V1.3 实测踩到）：GUI 会自动把会话态（最近目录、输出根）写回
#: ``config.yaml``，而测试里 ``cfg["gui"]["out_root"]`` 常指向 pytest 临时目录
#: ——于是**每跑一次测试就把交付配置写脏一次**（`out_root` 变成
#: `…\Temp\pytest-of-…\gui_lazy0`）。这类污染不会让任何用例变红，只会让用户
#: 拿到一份指向临时目录的配置，属于"测试绿、产品脏"。
CONFIG_ENV_VAR = "DYE_TOOL_CONFIG"


def cfg_writes_allowed(path: Path | str | None = None) -> bool:
    """当前进程是否**允许**写 ``config.yaml``（ε 批新增、V2.3 收紧的护栏）。

    为什么需要（实测事故）：手工跑离屏 GUI 冒烟时忘了设 ``DYE_TOOL_CONFIG``，
    于是 ``MainWindow.closeEvent`` 把**交付配置** ``config.yaml`` 改写了
    （`recent_dirs`/`last_dir`/`written_at` 变化 → SHA256 与批次基线不一致），
    违反"每批 config.yaml 前后一致"的收口纪律。

    V2.3 收紧（《代码评审记录.md》第3部分·F3）：护栏原来只在两个调用点检查，
    ``⑤设置保存`` / ``app.py 首启固化`` / ``nav 首启写`` 三处**绕过**它。
    现在判定**下沉到** :func:`core.config.save_config` 一处，并改成**路径感知**：

    * 显式设了 ``DYE_TOOL_CONFIG``（测试/多实例）→ 一律放行；
    * 非冒烟模式（正常交互运行）→ 一律放行；
    * 冒烟模式（``DYE_TOOL_SMOKE=1``）→ **只拦"写交付配置"**，写临时路径放行
      （否则验收/测试里写 tmp 配置会被误拦——这是改成路径感知的原因）。

    Args:
        path: 本次要写的配置文件路径；``None`` = 按"是否要写交付配置"判断。

    Returns:
        ``True`` = 允许写；``False`` = 本次跳过（:func:`save_config` 记 debug 日志）。
    """
    import os as _os
    if _os.environ.get(CONFIG_ENV_VAR):
        return True
    if _os.environ.get("DYE_TOOL_SMOKE") != "1":
        return True
    if path is None:
        return False
    try:
        return Path(path).resolve() != config_path().resolve()
    except OSError:                                    # pragma: no cover
        return False


def config_path() -> Path:
    """用户级配置文件路径。

    优先级：环境变量 :data:`CONFIG_ENV_VAR` > ``<app_root>/config.yaml``。
    打包后 ``app_root()`` = exe 同级的目录，所以默认仍是"双击 exe 旁边那份"。
    """
    override = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return app_root() / "config.yaml"


# ═════════════ 目录遍历：junction / 重解析点（V2.0 α 批 G6） ═════════════
def is_reparse_point(p: Path) -> bool:
    """是否软链接 / junction / 其它 **重解析点**（Windows）。

    G6 实测（2026-10-16，Python 3.11.3）：``os.stat_result`` **没有**
    ``st_reparse_point`` 属性（那是 ``st_reparse_tag``，值是
    ``0xa0000003`` 这种 tag）——所以旧写法
    ``getattr(st, "st_reparse_point", 0)`` **恒为 0**，junction 一个都认不出来，
    ``reorg_runs`` 里"junction 跳过"的注释与文档全是失效的。而
    ``pathlib.Path.is_junction`` 要 Python 3.12+。真正可用的判据有两个，
    这里两个都试（任一亮即算）：

    * ``st_reparse_tag``（3.8+，Windows 上有值）；
    * ``st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT``（0x400）。

    为什么需要它：``Path.rglob`` 与 ``is_dir()`` 都会**跟随** junction
    （探针实测：junction 里的 ``sub/f.txt`` 被 rglob 收进来了）。整理工具
    与目录扫描必须显式跳过，否则会去动 runs/ 之外的第三方数据。
    """
    try:
        if p.is_symlink():
            return True
        st = p.stat(follow_symlinks=False)
    except OSError:
        return False
    if getattr(st, "st_reparse_tag", 0):
        return True
    return bool(getattr(st, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def walk_files(root: Path, *, recursive: bool = True) -> list[Path]:
    """遍历 ``root`` 下的**文件**，**不进入**软链接 / junction。

    用途：``collect_scans`` / ``preview_groups`` 与 ``reorg_runs`` 共用的目录遍历。
    语义与原 ``Path.rglob("*")`` 对齐（返回文件路径、相对形式与原实现同为
    ``root / 相对路径``；隐藏文件与 ``__pycache__`` 之类**照样**会被列出，因为
    扩展名过滤在调用方；只在"不跟随重解析点"这一点上收紧——这正是 G6 的意图》。

    为什么不能用"先 rglob 再过滤"：``rglob`` 会把 junction **内部**的文件也吐出来，
    事后按路径前缀过滤虽然能挡住它们，但走的是"已经读了一堆别人的数据再丢掉"，
    而且软链接环（A→B→A）会直接**无限递归**。这里用 ``os.walk`` 在**下降前**剪枝。

    ``os.walk`` 的默认 ``onerror=None`` 会**静默忽略**无权限的子目录——与原
    ``rglob`` 直接抛 PermissionError 相比更接近"扫描工具该有的行为"，也与
    Windows 上"某些系统目录读不了"的现实相符。
    """
    root = Path(root)
    out: list[Path] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            cur = Path(dirpath)
            dirnames[:] = [d for d in dirnames
                           if not is_reparse_point(cur / d)]
            out += [cur / f for f in filenames]
    else:
        for p in root.glob("*"):
            if p.is_file():
                out.append(p)
    return out
