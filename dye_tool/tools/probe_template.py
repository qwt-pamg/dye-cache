# -*- coding: utf-8 -*-
"""临时探针模板 —— 复制这个文件去排查问题，用完删掉。

**为什么需要模板**：本项目这一轮排查里，手写探针踩过三个坑，每个都浪费了十几分钟：

1. **没写 ``if __name__ == "__main__":``** → 一旦探针里触发多进程（出图并行、或
   只是 import 了会起进程池的代码），Windows 的 spawn 会**重新执行整个探针文件**，
   递归爆炸、进程挂死。本模板的守卫会先检查这一点并拒绝运行。
2. **用 ``python -c "..."`` 跑多进程** → 同上，``-c`` 没有 ``__main__`` 文件可守卫，
   必挂。
3. **忘了设环境变量** → ``DYE_TOOL_SMOKE=1``（不设则启动模态弹窗阻塞事件循环）、
   ``QT_QPA_PLATFORM=offscreen``、``QT_QPA_FONTDIR``。模板里统一设好。

用法::

    copy tools\\probe_template.py tools\\_probe_xxx.py
    # 改下面的 probe()，然后：
    .venv\\Scripts\\python.exe tools\\_probe_xxx.py
    # 用完删掉（tools/ 只保留正式工具：check.py / parallel_pytest.py / bundle_audit.py /
    # probe_template.py / window_lock.py）
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# ── 环境：必须在 import Qt / matplotlib / core 之前设好 ──
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
os.environ.setdefault("DYE_TOOL_SMOKE", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def log(msg: str) -> None:
    """带时间戳的即时输出（探针卡住时靠它定位）。"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def probe() -> None:
    """← 把要查的事情写在这里。"""
    from core.config import default_config
    cfg = default_config()
    log(f"配置读到了：probe={cfg['wavelength']['probe']}")
    log("把探针逻辑写在这里；要跑真实数据就用 samples/uvvis")


def main() -> None:
    # 编码自愈：Windows 默认控制台是 GBK，直接打印非 ASCII 会抛
    # UnicodeEncodeError（而且往往在结论出来之后才抛，把成功变成失败）。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                   # noqa: BLE001
        pass
    log(f"探针开始（Python {sys.version.split()[0]}，工作区 {ROOT.name}）")
    t0 = time.perf_counter()
    probe()
    log(f"探针结束，用时 {time.perf_counter() - t0:.2f}s")


if __name__ == "__main__":
    main()
else:                                                   # pragma: no cover
    # 被 import / 被 spawn 重新执行时走到这里：说明有人把它当模块用了。
    # 直接报错退出，比"递归起进程把机器拖死"友好得多。
    raise SystemExit(
        "probe_template.py 只能作为脚本直接运行（python tools\\_probe_xxx.py）；"
        "被 import 往往意味着 spawn 递归 —— 请检查是否忘了 __main__ 守卫。")
