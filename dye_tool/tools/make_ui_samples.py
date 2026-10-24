# -*- coding: utf-8 -*-
"""生成 ``samples/uvvis`` 合成样例（公开仓库不带实验数据时，供 GUI 冒烟与界面测试用）。

形状与原随包样例一致：两组共 35 个紫外–可见文本文件，
``Resorufin-pbs-t``（样品，18 个时间点）与 ``Resorufin-2I-pbs-TEST``（参比，17 个时间点），
文件名 ``组名-秒数[+分钟数].txt``（含一个 ``90+5`` = 390 s 的样例），GBK 编码、
两列"波长,吸光值"。数值是 ABDA 型三重峰的一级衰减叠加光敏剂底吸收和 2% 噪声，
**不是**实验数据，不能拿来做数值金线。

用法::

    python tools/make_ui_samples.py            # 写到 samples/uvvis（已存在则跳过）
    python tools/make_ui_samples.py --force    # 覆盖重写
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "uvvis"

#: ``组名 → (k_obs / s⁻¹, 文件名里的时间标签)``；两组合计 35 个文件
GROUPS = {
    "Resorufin-pbs-t": (5.0e-3, [0, 1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90,
                                 120, 150, 180, 240, 300, "90+5"]),
    "Resorufin-2I-pbs-TEST": (6.25e-3, [0, 1, 5, 10, 20, 30, 40, 50, 60, 70,
                                        80, 90, 120, 150, 180, 240, 300]),
}


def make_ui_samples(out_dir: Path = OUT, seed: int = 7) -> int:
    rng = np.random.default_rng(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    wl = np.arange(300.0, 701.0)

    def g(mu, sig):
        return np.exp(-0.5 * ((wl - mu) / sig) ** 2)

    probe = 0.634 * g(359.5, 8) + 1.0 * g(379, 7) + 0.941 * g(400, 8)
    sens = 0.5 * g(571, 45) + 0.16 * np.exp(-(wl - 300) / 150) + 0.004
    n = 0
    for grp, (k, times) in GROUPS.items():
        for label in times:
            sec, _, mins = str(label).partition("+")
            t = float(sec) + 60.0 * float(mins or 0)
            a = probe * np.exp(-k * t) + sens * np.exp(-1e-4 * t)
            a = a + rng.normal(0, 1, a.shape) * (0.02 * np.abs(a) + 0.001)
            lines = [f'"{grp}-{label} - RawData"', '"波长","吸光值"']
            lines += [f"{w:.2f},{v:.3f}" for w, v in zip(wl, a)]
            (out_dir / f"{grp}-{label}.txt").write_text("\n".join(lines) + "\n",
                                                    encoding="gbk")
            n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true", help="已存在也覆盖重写")
    args = ap.parse_args(argv)
    if OUT.is_dir() and any(OUT.glob("*.txt")) and not args.force:
        print(f"已存在，跳过：{OUT}（--force 覆盖）")
        return 0
    print(f"已写出 {make_ui_samples()} 个文件 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
