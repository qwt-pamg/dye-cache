# -*- coding: utf-8 -*-
"""synthetic 演示数据生成与内置自检（FR-5）。

迁移自 legacy ``singlet_o2_phi`` 的 ``make_synthetic`` / ``_t3_ok`` / ``self_test``。
差别：``self_test`` 返回结构化检查清单（供 GUI 弹窗），不再 ``print``。
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

import numpy as np

from ..config import default_config
from ..analysis.phi import calc_phi, compute_phi, iabs, judge_controls
from ..analysis.pipeline import run_core

log = logging.getLogger("dye_tool.synthetic")

#: synthetic 真值参数（legacy 原样）
SYN = {"k_true": 5.0e-3, "phi_true": 0.60, "phi_ref": 0.75, "a_ex": 0.15,
       "k_ref": 5.0e-3 * 0.75 / 0.60, "lambda_ex": 532.0}   # k_ref=6.25e-3
SYN_TIMES = [0, 1, 5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 120, 150, 180, 210,
             240, 300, 360, 480, 600]


def make_synthetic(out_dir: Path | str, seed: int = 42) -> dict:
    """生成 synthetic 自检数据集（T1–T4 场景 + 缺陷注入）并写 ``_truth.json``。

    实际注入的缺陷：

    * ``sample_1`` 在 t=150 的 379 nm 处注入**尖峰**（A=0.001，考查 :func:`despike`）；
    * ``mis_1`` 整组波长错位 +5 nm（考查 :func:`qc_wavelength` 整组剔除）；
    * ``broken.csv`` 完全不可解析（考查坏文件不崩溃）；
    * ``nonlin_1`` 双指数衰减（考查自动截断）。

    ⚠ **继承自 legacy 的行为**：legacy 的 ``defects`` 表里还有一条
    ``("sample_2", 200) → ("missing", 300.0)``（缺测注入），但 :data:`SYN_TIMES`
    中并不存在 200 s，因此**该缺陷从未真正注入**（不存在 ``sample_2-200.txt``）。
    为保持与 legacy 生成的 synthetic 数据逐位一致（自检 T1 的反算偏差、
    T5 的两次运行比特一致性都依赖它），此处**刻意保留**该 no-op 而不擅自
    把 200 改成表内时间点。缺测 → NaN 的路径已由真实样例中的 t=0 空值文件
    与 pytest 用例覆盖。
    """
    rng = np.random.default_rng(seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    wl = np.arange(300.0, 701.0)

    def g(mu, sig):
        return np.exp(-0.5 * ((wl - mu) / sig) ** 2)

    probe = 0.634 * g(359.5, 8) + 1.0 * g(379, 7) + 0.941 * g(400, 8)   # ABDA 型三重峰
    sens = 0.5 * g(571, 45) + 0.16 * np.exp(-(wl - 300) / 150) + 0.004  # 荧光团型光敏剂
    sens = sens * (SYN["a_ex"] / sens[int(np.argmin(np.abs(wl - SYN["lambda_ex"])))])
    groups = {"sample_1": (SYN["k_true"], 1.0), "sample_2": (SYN["k_true"], 0.95),
              "ref_1": (SYN["k_ref"], 1.0), "bg_1": (None, 0.0),
              "dark_1": (1e-6, 1.0), "n2_1": (0.1 * SYN["k_true"], 1.0),
              "nan3_1": (0.05 * SYN["k_true"], 1.0), "nonlin_1": ("biexp", 1.0),
              "mis_1": (SYN["k_true"], 1.0)}
    defects = {("sample_1", 150): ("spike", 379.0),
               ("sample_2", 200): ("missing", 300.0)}
    for grp, (k, amp) in groups.items():
        for t in SYN_TIMES:
            sens_t = sens * np.exp(-1e-4 * t)
            if k is None:
                a = sens_t.copy()
            elif k == "biexp":
                a = (amp * probe * (0.25 * np.exp(-0.02 * t)
                                    + 0.75 * np.exp(-0.002 * t)) + sens_t)
            else:
                a = amp * probe * np.exp(-k * t) + sens_t
            a = a + rng.normal(0, 1, a.shape) * (0.02 * np.abs(a) + 0.001)   # 2% 噪声
            wlx = wl + 5.0 if grp == "mis_1" else wl                          # T4 波长错位
            kind, dwl = defects.get((grp, t), (None, 0.0))
            lines = [f'"{grp}-{t} - RawData"', '"波长","吸光值"']
            for w, v in zip(wlx, a):
                if kind == "missing" and abs(w - dwl) < 0.5:
                    lines.append(f"{w:.2f}, ")
                elif kind == "spike" and abs(w - dwl) < 0.5:
                    lines.append(f"{w:.2f},0.001")
                else:
                    lines.append(f"{w:.2f},{v:.3f}")
            (out_dir / f"{grp}-{t}.txt").write_text("\n".join(lines) + "\n",
                                                    encoding="gbk")
    (out_dir / "broken.csv").write_text(
        "这不是光谱数据\n\"垃圾\",\"表头\"\n1,2,3,4\n,,\n", encoding="utf-8")
    truth = dict(SYN)
    truth["seed"] = seed
    (out_dir / "_truth.json").write_text(
        json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("synthetic 数据已生成 -> %s (%d 个文件)", out_dir,
             len(list(out_dir.glob('*.txt'))) + 1)
    return truth


# ═══════════════════════════ 内置自检（T1–T5 + F6） ═══════════════════════════
def _t3_ok(truth: dict) -> bool:
    """T3: A_ex×10 → Φ 按 I_abs 比例变化。"""
    a0 = truth["a_ex"]
    p0 = calc_phi(0.005, 0.00625, a0, a0, 0.75)
    pr = calc_phi(0.005, 0.00625, a0, 10 * a0, 0.75)   # ref 侧 A_ex×10
    ps = calc_phi(0.005, 0.00625, 10 * a0, a0, 0.75)   # sample 侧 A_ex×10
    r = iabs(10 * a0) / iabs(a0)
    return abs(pr / p0 - r) < 1e-9 and abs(ps / p0 - 1 / r) < 1e-9


def self_test(tmp_dir: Optional[Path] = None, seed: int = 42
              ) -> list[tuple[str, bool, str]]:
    """跑 legacy 等价的 T1–T5 + F6 自检，返回 ``[(名称, 是否通过, 说明)]``。

    与 legacy 的差别：不 ``print``、不 ``sys.exit``，结果交给调用方（GUI 弹窗 / CLI 退出码）。
    """
    tmp = Path(tmp_dir) if tmp_dir else Path(tempfile.mkdtemp(prefix="dye_selftest_"))
    own_tmp = tmp_dir is None
    phi_logger = logging.getLogger("dye_tool")
    old_level = phi_logger.level
    phi_logger.setLevel(logging.ERROR)                   # 自检时静音流水线日志
    try:
        truth = make_synthetic(tmp, seed=seed)
        cfg = default_config()
        cfg["quantum_yield"]["a_ex"] = {"sample": truth["a_ex"], "ref": truth["a_ex"]}
        cfg["quantum_yield"]["phi_ref"] = truth["phi_ref"]
        scans, res = run_core(tmp, cfg)
        phi = compute_phi(res, cfg)
        jc = judge_controls(res, cfg)
        _, res2 = run_core(tmp, cfg)
        empty = Path(tempfile.mkdtemp(prefix="dye_empty_"))
        try:
            e_scans, e_res = run_core(empty, default_config())
        finally:
            shutil.rmtree(empty, ignore_errors=True)
        f = res["sample"]["pooled"]
        p = phi["sample"]
        pv = p.get("phi")
        checks = [
            ("T1  k 反算偏差 < 5%",
             f.status == "OK" and abs(f.k - truth["k_true"]) / truth["k_true"] < 0.05,
             f"k_hat={f.k:.4e} (真值 {truth['k_true']:.4e})"),
            ("T1  Φ 反算偏差 < 8%",
             p.get("status") == "OK" and pv is not None and np.isfinite(pv)
             and abs(pv - truth["phi_true"]) / truth["phi_true"] < 0.08,
             f"Phi_hat={pv:.4f} (真值 {truth['phi_true']:.2f})"
             if pv is not None else "Phi_hat=N/A"),
            ("T2  非线性数据触发自动截断", bool(res["nonlin"]["pooled"].truncated),
             "nonlin 组"),
            ("T3  A_ex×10 → Φ 按 I_abs 比例变化", _t3_ok(truth),
             "ref 侧增大/sample 侧下降"),
            ("T4  波长错位组整组剔除", "mis" not in res, "mis 组不存在"),
            ("T4  坏文件不崩溃且被剔除",
             any("broken" in s.path.name and s.excluded for s in scans), "broken.csv"),
            ("T4  空目录优雅降级", e_res == {} and e_scans == [], "返回空结果"),
            ("T5  两次运行比特一致",
             f"{f.k:.9e}" == f"{res2['sample']['pooled'].k:.9e}",
             "k 9 位有效数字一致"),
            ("F6  dark/N2/NaN3 对照判定通过",
             all(jc.get(x, ("x",))[0] == "通过" for x in ("dark", "n2", "nan3")),
             "; ".join(f"{x}:{jc.get(x, ('?',))[0]}" for x in ("dark", "n2", "nan3"))),
        ]
        return checks
    finally:
        phi_logger.setLevel(old_level)
        if own_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def self_test_ok(**kw) -> bool:
    """自检是否全部通过（CLI 退出码 / GUI 弹窗标题用）。"""
    return all(bool(p) for _, p, _ in self_test(**kw))


def self_test_text(**kw) -> str:
    """自检结果的多行文本（CLI 打印）。"""
    checks = self_test(**kw)
    lines = ["===== 内置自检 (synthetic, seed=42) ====="]
    for name, passed, info in checks:
        lines.append(f"  [{'通过' if passed else '失败'}] {name}  <- {info}")
    lines.append(f"===== 自检{'全部通过' if all(c[1] for c in checks) else '存在失败项'} =====")
    return "\n".join(lines)
