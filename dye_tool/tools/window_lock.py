# -*- coding: utf-8 -*-
"""跨窗口门禁锁 —— 让同一工作区的两个 DSH 窗口**串行**跑重门禁。

背景（2026-10-18）：用户同时开两个对话窗口处理同一任务——一个审查/测试，
一个执行/修复。两者共享 `test_temp/` 与 `runs/`：pytest 会在会话开始时清理
`--basetemp=test_temp/pytest`，验收开跑前清理 `test_temp/acceptance/`，
`runs/check_report.md` / `runs/acceptance_report.md` 会被覆盖。并发跑重门禁
会互相删目录、覆盖证据 → 所以必须先拿锁（规则见 `../双窗口协作协议.md` §2）。

用法::

    .venv\\Scripts\\python.exe tools\\window_lock.py status
    .venv\\Scripts\\python.exe tools\\window_lock.py acquire --role exec --ttl 30 --note "check --full"
    .venv\\Scripts\\python.exe tools\\window_lock.py release --role exec

锁文件：`test_temp/window.lock.json`（可用环境变量 `DYE_TOOL_WINDOW_LOCK` 覆盖，测试用）。
退出码：0=成功/空闲；1=锁被占或角色不符；2=用法错误；3=读写失败。

设计取舍：
* **TTL 优先于 PID**：两个窗口可能在不同进程/宿主上，跨进程探活不可靠；过期即可接管。
* 锁只用标准库 + 原子创建（`O_CREAT|O_EXCL`），不引入依赖；`--force` 可人工强占（看板留痕）。
* 本工具**只做串行提示**，不会替调用方自动跑门禁，也不改变任何门禁判据。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = ROOT / "test_temp" / "window.lock.json"
ROLES = ("exec", "review", "other")


def _lock_path() -> Path:
    raw = os.environ.get("DYE_TOOL_WINDOW_LOCK", "")
    return Path(raw) if raw else DEFAULT_LOCK


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _describe(rec) -> str:
    if not isinstance(rec, dict):
        return "（锁文件内容损坏，可用 --force 接管）"
    exp = float(rec.get("expires_ts", 0) or 0)
    remain = exp - time.time()
    state = "已过期" if remain <= 0 else f"剩 {max(1, int(remain))} s"
    return (f"role={rec.get('role')} pid={rec.get('pid')} host={rec.get('host')} "
            f"自 {rec.get('started')} 起；{state}；事项={rec.get('note') or '（无）'}")


def _try_create(path: Path, data: dict) -> bool:
    """原子创建锁文件；已存在返回 False。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def cmd_status(args) -> int:
    p = _lock_path()
    rec = _read(p) if p.is_file() else None
    if not rec:
        print(f"[空闲] 没有门禁锁。{p}")
        return 0
    exp = float(rec.get("expires_ts", 0) or 0) if isinstance(rec, dict) else 0
    tag = "[过期]" if exp <= time.time() else "[占用]"
    print(f"{tag} {_describe(rec)}")
    print(f"       锁文件：{p}")
    return 0


def cmd_acquire(args) -> int:
    p = _lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        print(f"[错误] 无法创建锁目录 {p.parent}：{err}")
        return 3
    now = time.time()
    rec = _read(p) if p.is_file() else None
    if rec and isinstance(rec, dict) and float(rec.get("expires_ts", 0) or 0) > now \
            and not args.force:
        print(f"[占用] {_describe(rec)}")
        print(f"        锁文件：{p}")
        print("        → 不要抢跑：先做不需要重门禁的工作，30–60 s 后再试；"
              "确认对方已死可用 --force（看板留痕）。")
        return 1
    if rec:
        if isinstance(rec, dict) and float(rec.get("expires_ts", 0) or 0) <= now:
            reason = "过期自动接管"
        else:
            reason = "--force 强制接管"
        print(f"[提示] 接管旧锁（{reason}）：{_describe(rec)}")
        try:
            p.unlink()
        except OSError as err:
            print(f"[错误] 删除旧锁失败：{err}")
            return 3
    data = {
        "role": args.role,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started": _fmt(now),
        "started_ts": now,
        "expires": _fmt(now + args.ttl * 60),
        "expires_ts": now + args.ttl * 60,
        "note": args.note or "",
    }
    for _ in range(3):
        if _try_create(p, data):
            print(f"[已获取] role={args.role}，有效至 {data['expires']}（{args.ttl} min）；"
                  f"事项={data['note'] or '（无）'}")
            return 0
        other = _read(p)
        if other and isinstance(other, dict) and float(other.get("expires_ts", 0) or 0) > now:
            print(f"[占用] {_describe(other)}")
            return 1
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
    print("[错误] 获取锁失败（并发竞争），请重试。")
    return 3


def cmd_release(args) -> int:
    p = _lock_path()
    rec = _read(p) if p.is_file() else None
    if not rec:
        print(f"[空闲] 没有锁可释放。{p}")
        return 0
    role = rec.get("role") if isinstance(rec, dict) else None
    if role != args.role and not args.force:
        print(f"[拒绝] 锁属于 role={role}，不是 --role {args.role}；确认要接管请加 --force。")
        return 1
    try:
        p.unlink()
    except OSError as err:
        print(f"[错误] 删除锁失败：{err}")
        return 3
    print(f"[已释放] role={role}（{args.role} 释放）；锁文件已删除。")
    return 0


def main(argv=None) -> int:
    # 编码自愈：GBK 控制台下打印中文/符号不能崩（与 check.py / acceptance_report.py 同款）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="跨窗口门禁锁（双窗口协作）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="查看锁状态")
    s.set_defaults(fn=cmd_status)
    a = sub.add_parser("acquire", help="获取锁")
    a.add_argument("--role", choices=ROLES, required=True)
    a.add_argument("--ttl", type=int, default=30, help="有效分钟数，默认 30")
    a.add_argument("--note", default="", help="用途，如 'check --full'")
    a.add_argument("--force", action="store_true", help="强占未过期的锁（慎用，看板留痕）")
    a.set_defaults(fn=cmd_acquire)
    r = sub.add_parser("release", help="释放锁")
    r.add_argument("--role", choices=ROLES, required=True)
    r.add_argument("--force", action="store_true")
    r.set_defaults(fn=cmd_release)
    args = ap.parse_args(argv)
    ttl = getattr(args, "ttl", None)
    if ttl is not None and not (1 <= ttl <= 480):
        ap.error("--ttl 必须在 1–480 分钟之间")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
