# -*- coding: utf-8 -*-
"""Φ_Δ 计算层：吸光占比、量子产率、误差传递、bootstrap、对照判定。

迁移自 legacy ``singlet_o2_phi`` 的 M5/M7 与 F6 段，公式**一字未改**。
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

log = logging.getLogger("dye_tool.phi")

#: 优化 #7：同 kinetics —— phi 只在 bootstrap/斜率校正时用 scipy。
_STATS = None


def _stats():
    global _STATS
    if _STATS is None:
        from scipy import stats as _s
        _STATS = _s
    return _STATS


# ═══════════════════════════ M5/M7: Φ_Δ 与误差 ═══════════════════════════
def iabs(a_ex: float, mode: str = "exact") -> float:
    """吸收光子占比 ``I_abs``。

    ``exact``  → ``1 − 10^(−A_ex)``
    ``linear`` → ``2.303·A_ex``（仅 A_ex < 0.1 的近似）
    """
    return 2.303 * a_ex if mode == "linear" else 1.0 - 10.0 ** (-a_ex)


def unusable_aex(a_ex: float, mode: str = "exact") -> str:
    """``A_ex`` 是否**不能**用来算 Φ（返回中文原因；空串 = 可用）。

    ``A_ex = 0`` 在物理上意味着"没有吸收光子"，此时 ``I_abs`` = 0、比值无定义
    ——原实现在这里抛 ``ZeroDivisionError`` 并顺着流水线冒泡，把整批结果全废掉
    （S2）。A_ex < 0（基线校正过头）同样要拦住：``10^(+x)`` 会给出 >1 的"吸收占比"，
    数值上荒谬。判定放在**数值层**，让调用方自己决定怎么跳过。
    """
    if not np.isfinite(a_ex):
        return "A_ex 非有限值"
    if a_ex < 0:
        return f"A_ex={a_ex:.4g} < 0（基线校正过头？）"
    if mode == "linear":
        if a_ex == 0:
            return "A_ex=0 → I_abs=0，Φ 无定义"
        return ""
    if a_ex == 0:
        return "A_ex=0 → I_abs=0，Φ 无定义"
    return ""


def calc_phi(k_s: float, k_r: float, aex_s: float, aex_r: float,
             phi_ref: float, mode: str = "exact") -> float:
    """``Φ_sample = Φ_ref × (k_sample/k_ref) × (I_abs,ref / I_abs,sample)``。

    退化输入（``k_r`` 或任一 ``I_abs`` 为 0）返回 ``NaN`` 而不是抛异常——
    调用方 :func:`compute_phi` 会把它记成一条 WARNING 并跳过该配对。
    """
    r = unusable_aex(aex_s, mode) or unusable_aex(aex_r, mode)
    if r or k_r == 0:
        log.warning("calc_phi 输入退化（%s%s）→ 返回 NaN",
                    r or "", "k_ref=0" if k_r == 0 else "")
        return float("nan")
    return float(phi_ref * (k_s / k_r) * (iabs(aex_r, mode) / iabs(aex_s, mode)))


def phi_sigma(phi: float, k_s: float, k_s_err: float, k_r: float, k_r_err: float,
              aex_s: float, aex_r: float, phi_ref: float, phi_ref_err: float,
              a_ex_err: float, mode: str = "exact") -> float:
    """误差传递：``σ_Φ = Φ·√[(σks/ks)²+(σkr/kr)²+(σΦref/Φref)²+Σ(σI/I)²]``。

    输入退化时（``k`` 或 ``I_abs`` 为 0、``Φ`` 非有限）返回 ``NaN`` 而不是抛异常。
    """
    if not np.isfinite(phi) or k_s == 0 or k_r == 0 or phi_ref == 0:
        return float("nan")
    if unusable_aex(aex_s, mode) or unusable_aex(aex_r, mode):
        return float("nan")
    rel = (k_s_err / k_s) ** 2 + (k_r_err / k_r) ** 2 + (phi_ref_err / phi_ref) ** 2
    for ax in (aex_s, aex_r):
        if mode == "linear":
            rel += (a_ex_err / ax) ** 2
        else:
            rel += (np.log(10.0) * 10.0 ** (-ax) * a_ex_err / iabs(ax)) ** 2
    return float(phi * np.sqrt(rel))


def bootstrap_phi(t_s: np.ndarray, R_s: np.ndarray, t_r: np.ndarray, R_r: np.ndarray,
                  aex_s: float, aex_r: float, phi_ref: float, cfg: dict,
                  n: Optional[int] = None, seed: Optional[int] = None
                  ) -> tuple[float, float]:
    """可选稳健性校验：成对 bootstrap 重采样重拟合 → ``(Φ均值, 标准差)``。"""
    qy, kin = cfg["quantum_yield"], cfg["kinetics"]
    n = int(n or qy.get("bootstrap_n", 1000))
    rng = np.random.default_rng(seed if seed is not None else qy.get("seed", 42))
    mode = qy.get("iabs_mode", "exact")
    r_min = float(kin.get("r_min", 0.30))
    r_max = float(kin.get("r_max", 1.0)) + float(kin.get("r_max_tol", 0.05))

    def prep(t, R):
        t, R = np.asarray(t, float), np.asarray(R, float)
        m = np.isfinite(R) & (R >= r_min) & (R <= r_max)
        return t[m], np.log(R[m])

    ts, ys = prep(t_s, R_s)
    tr, yr = prep(t_r, R_r)
    if min(ts.size, tr.size) < 3:
        return float("nan"), float("nan")
    out: list[float] = []
    for _ in range(n):
        i = rng.integers(0, ts.size, ts.size)
        j = rng.integers(0, tr.size, tr.size)
        try:
            ks = -_stats().linregress(ts[i], ys[i]).slope
            kr = -_stats().linregress(tr[j], yr[j]).slope
            if ks > 0 and kr > 0:
                out.append(calc_phi(ks, kr, aex_s, aex_r, phi_ref, mode))
        except Exception:                              # noqa: BLE001
            continue
    if not out:
        return float("nan"), float("nan")
    arr = np.array(out)
    return float(arr.mean()), (float(arr.std(ddof=1)) if arr.size > 1 else 0.0)


# ═══════════════════════════ F6: 特异性对照判定 ═══════════════════════════
def judge_controls(results: dict, cfg: dict) -> dict:
    """暗对照 / N₂ / 淬灭剂三类的抑制率判定 → ``{组: (判定, 说明)}``。

    ``k_s``（样品组 k 的均值）可能恰好为 0（多组 k 正负抵消、或全部拟合失败只剩
    0）——原实现在 n2/淬灭剂分支直接 ``k/k_s`` 除零并把异常抛穿整批（M8）。
    这里显式判退化：k_s 无效时**只对 n2/淬灭剂**给出"无法判定"，暗对照改用绝对
    阈值判定（不依赖 k_s）。
    """
    if not cfg.get("controls", {}).get("enable", True):
        return {}
    ks = {g: e["pooled"].k for g, e in results.items()
          if e.get("pooled") and e["pooled"].status != "FAILED"}
    s_ks = [k for g, k in ks.items() if results[g]["role"] == "sample"]
    if not s_ks:
        return {}
    k_s = float(np.mean(s_ks))
    k_s_ok = bool(np.isfinite(k_s) and k_s != 0.0)
    out: dict = {}
    for g, e in sorted(results.items()):
        role = e["role"]
        if role not in ("dark", "n2", "quencher") or g not in ks:
            continue
        k = ks[g]
        if role == "dark":
            thr = float(cfg["qc"].get("dark_k_rel_max", 0.05)) * abs(k_s)
            out[g] = ("通过" if abs(k) <= thr else "存疑",
                      f"|k_dark|={abs(k):.2e}(阈值 {thr:.2e})")
        elif not k_s_ok or not np.isfinite(k):
            out[g] = ("无法判定",
                      f"样品组 k 均值无效(k_s={k_s:.3g})，抑制率无定义")
            log.warning("对照判定 %s: 无法判定（k_s=%s 不可用）", g, k_s)
        else:
            inh = 1.0 - k / k_s
            thr = float(cfg["controls"].get(
                "n2_inhibition_min" if role == "n2" else "quencher_inhibition_min", 0.70))
            out[g] = ("通过" if inh >= thr else ("存疑" if inh >= 0.5 * thr else "失败"),
                      f"抑制率 {inh:.0%}(阈值 {thr:.0%})")
        log.info("对照判定 %s: %s(%s)", g, out[g][0], out[g][1])
    return out


# ═══════════════════════════ F5: 批量 Φ_Δ ═══════════════════════════
def compute_phi(results: dict, cfg: dict) -> dict:
    """F5: sample↔ref 配对计算 Φ_Δ（多参比取均值）；无参比 → ``NO_REF`` 不阻塞。"""
    qy = cfg["quantum_yield"]
    mode = qy.get("iabs_mode", "exact")
    aex_cfg = dict(qy.get("a_ex") or {})
    refs = [g for g, e in results.items() if e["role"] == "reference"
            and e.get("pooled") is not None and e["pooled"].status != "FAILED"]
    warned: set = set()

    def _aex(g, e):
        if g in aex_cfg:
            return float(aex_cfg[g])
        v = e.get("a_ex_auto")
        v = float(v) if v is not None and np.isfinite(v) else float("nan")
        if np.isfinite(v) and g not in warned:
            warned.add(g)
            log.warning("组 %s: A_ex=%.4f 取自最早光谱 λ_ex 处(config 未声明, "
                        "可能含探针贡献)", g, v)
        return v

    out: dict = {}
    for g, e in sorted(results.items()):
        if e["role"] != "sample":
            continue
        p = e.get("pooled")
        if p is None or p.status == "FAILED":
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "FAILED", "pairs": []}
            continue
        if not refs:
            log.info("组 %s: 无参比组 → Φ 输出 N/A (NO_REF)", g)
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "NO_REF", "pairs": []}
            continue
        aex_s, pairs = _aex(g, e), []
        why_s = unusable_aex(aex_s, mode)
        for rg in refs:
            rp, aex_r = results[rg]["pooled"], _aex(rg, results[rg])
            if why_s or unusable_aex(aex_r, mode):
                log.warning("配对 %s↔%s 的 A_ex 不可用（%s%s），跳过该配对（不中断整批）",
                            g, rg, f"样品 {why_s}" if why_s else "",
                            f"参比 {unusable_aex(aex_r, mode)}"
                            if unusable_aex(aex_r, mode) else "")
                continue
            if p.k <= 0 or rp.k <= 0:
                log.warning("配对 %s↔%s k≤0, 跳过", g, rg)
                continue
            phi = calc_phi(p.k, rp.k, aex_s, aex_r, qy["phi_ref"], mode)
            sig = phi_sigma(phi, p.k, p.k_err, rp.k, rp.k_err, aex_s, aex_r,
                            qy["phi_ref"], qy["phi_ref_err"],
                            qy.get("a_ex_err", 0.005), mode)
            if not np.isfinite(phi):
                log.warning("配对 %s↔%s Φ 计算失败（退化输入），跳过", g, rg)
                continue
            pairs.append({"ref": rg, "phi": phi, "phi_err": sig})
            log.info("Φ_Δ(%s | 参比 %s) = %.4f ± %.4f", g, rg, phi, sig)
        if not pairs:
            out[g] = {"phi": float("nan"), "phi_err": float("nan"),
                      "status": "FAILED", "pairs": []}
            continue
        phis = np.array([q["phi"] for q in pairs])
        errs = np.array([q["phi_err"] for q in pairs])
        entry = {"phi": float(phis.mean()),
                 "phi_err": float(np.sqrt((errs ** 2).mean() + phis.var())),
                 "status": "OK", "pairs": pairs}
        if qy.get("bootstrap", False):
            rg = refs[0]
            t_s = np.concatenate([v["t"] for v in e["reps"].values()])
            R_s = np.concatenate([v["R"] for v in e["reps"].values()])
            t_r = np.concatenate([v["t"] for v in results[rg]["reps"].values()])
            R_r = np.concatenate([v["R"] for v in results[rg]["reps"].values()])
            bm, bs = bootstrap_phi(t_s, R_s, t_r, R_r, aex_s, _aex(rg, results[rg]),
                                   qy["phi_ref"], cfg)
            entry["bootstrap"] = {"mean": bm, "std": bs}
            log.info("Φ_Δ bootstrap(%s) = %.4f ± %.4f", g, bm, bs)
        out[g] = entry
    return out
