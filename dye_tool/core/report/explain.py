# -*- coding: utf-8 -*-
"""人话层（设计文档 V1.1 FR-9）：把技术字符串翻译成"发生了什么 + 下一步怎么办"。

原则（§1.3-8 小白优先）：

* 每条解释只**复述**已有计算结果，不新增判断逻辑（FR-9.3）；
* 原文技术串保留在括号里，便于检索与对照日志（FR-9.2）；
* 语言为简体中文；写盘的 ``run.log``/``report.md`` 不动，只在界面层翻译（Q12）。
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = ["explain_exclusion", "level_zh", "interpret_fit", "interpret_controls",
           "interpret_phi", "result_notes", "R2_GOOD", "R2_FAIR"]


# ═══════════════════════════ 剔除原因（FR-9.2） ═══════════════════════════
def explain_exclusion(reason: str) -> tuple[str, str]:
    """把剔除原因翻译成 ``(人话, 建议动作)``。

    未知原因不编造：原样返回并给通用建议。
    """
    r = str(reason or "").strip()
    if not r:
        return "", ""
    low = r.lower()

    # —— 读取失败族 ——
    if "读取失败" in r or "读取" in r:
        if "有效数据行不足" in r or "有效行" in r:
            n = re.search(r"\((\d+)\s*行\)", r)
            cnt = n.group(1) if n else "很少"
            return (f"这个文件里能用的数据行太少（{cnt} 行），不足以画出谱图",
                    "确认仪器是否导出完整；也可在 Excel 里打开看看是否被截断")
        if "编码" in r or "decode" in low or "gbk" in low:
            return ("文件编码不是程序预期的 GBK/UTF-8，读出来是乱码",
                    "用记事本「另存为」选 ANSI 或 UTF-8 后重试")
        return (f"文件打不开或内容不是两列（波长, 吸光度）光谱（原文：{r}）",
                "确认这是仪器导出的原始 txt/csv；列数不对可在 Excel 里另存为 csv")

    # —— 波长轴族（QC） ——
    if "波长轴" in r:
        if "点对点" in r:
            return ("这个文件的波长点与其他文件逐个对不上（起始、步长或点数不同）",
                    "多为仪器参数被改过；优先重新导出该文件，或直接取消勾选不使用")
        return (f"这个文件的波长范围/步长与其他文件不一致（原文：{r}）",
                "程序按多数文件的波长轴对齐，这个文件被排除；"
                "若它才是正确的，请检查同组其他文件的导出设置")

    # —— 饱和族 ——
    if "饱和" in r:
        m = re.search(r"max=([\d.]+)", r)
        val = m.group(1) if m else "偏高"
        return (f"吸光度 {val} 超过仪器线性上限（默认 3.0），峰顶被削平",
                "把样品稀释后重测，或改用短光程池；该点已不参与拟合")

    # —— 人为剔除 ——
    if "人为" in r or "手动" in r:
        return ("已在界面上手动取消勾选，本次分析不使用该文件",
                "如需纳入，重新勾选该行即可")

    return (f"该文件未通过检查（原文：{r}）",
            "可在日志区查看完整原因；确认无误后取消勾选即可继续分析")


# ═══════════════════════════ 日志级别（FR-9.4） ═══════════════════════════
_LEVEL_ZH = {"CRITICAL": "严重", "ERROR": "错误", "WARNING": "警告",
             "INFO": "信息", "DEBUG": "调试", "NOTSET": "全部"}


def level_zh(level: str) -> str:
    """日志级别英文 → 界面显示用中文（仅界面；写盘日志保持英文）。"""
    return _LEVEL_ZH.get(str(level).upper(), str(level))


# ═══════════════════════════ 结果一句话解读（FR-9.3） ═══════════════════════════
R2_GOOD = 0.99
R2_FAIR = 0.98


def interpret_fit(f) -> str:
    """拟合质量一句话（只复述 R² / 截断 / 状态，不重新判定）。"""
    if f is None:
        return "无拟合结果"
    status = str(getattr(f, "status", "") or "")
    if status == "FAILED":
        return "拟合失败：有效点太少或数据偏离准一级模型（见日志）"
    r2 = float(getattr(f, "r2", float("nan")))
    n = int(getattr(f, "n", 0) or 0)
    trunc = bool(getattr(f, "truncated", False))
    if r2 >= R2_GOOD:
        quality = "拟合很好"
    elif r2 >= R2_FAIR:
        quality = "拟合尚可"
    else:
        quality = "拟合偏差较大，建议检查数据"
    extra = "；已自动截断后期弯曲点" if trunc else ""
    if status == "WARN" and not trunc:
        extra = "；有告警，详见日志"
    return f"{quality}（R² = {r2:.4f}，n = {n} 点{extra}）"


def interpret_controls(controls: Optional[dict]) -> list[str]:
    """对照组判定结论（复述 ``judge_controls`` 的 ``{组: (判定, 说明)}``）。"""
    out: list[str] = []
    for key, item in sorted((controls or {}).items()):
        if isinstance(item, (tuple, list)) and len(item) >= 1:
            verdict = str(item[0])
            detail = str(item[1]) if len(item) > 1 else ""
            tail = f"（{detail}）" if detail else ""
            out.append(f"{key} 对照判定：{verdict}{tail}")
        elif isinstance(item, dict):
            role = item.get("role") or key
            ok = item.get("ok")
            if ok is None:
                out.append(f"{role}：{item.get('message') or '无判定'}")
            else:
                out.append(f"{role} 对照判定：{'通过' if ok else '未通过'}"
                           + (f"（{item.get('message')}）" if item.get("message") else ""))
    if not out:
        out.append("本次数据未包含 N₂/NaN₃/暗对照等对照组，未做淬灭判定")
    return out


def interpret_phi(phi: Optional[dict]) -> list[str]:
    """Φ_Δ 结果说明（NO_REF / 无暗态点等）。"""
    out: list[str] = []
    for g, p in sorted((phi or {}).items()):
        if not isinstance(p, dict):
            continue
        st = str(p.get("status", "") or "")
        if st == "OK" and p.get("phi") is not None:
            err = p.get("phi_err")
            txt = f"{g}：Φ_Δ = {float(p['phi']):.3f}"
            if isinstance(err, (int, float)):
                txt += f" ± {float(err):.3f}"
            out.append(txt)
        elif st == "NO_REF":
            out.append(f"{g}：无法计算 Φ_Δ —— 本组没找到参比（ref）组，"
                       "补测参比后再跑，或在参数里核对参比判定前缀")
        elif st:
            out.append(f"{g}：Φ_Δ 不可用（{st}）")
    if not out:
        out.append("本次未产出 Φ_Δ（缺参比组或拟合未通过）")
    return out


def result_notes(results: Optional[dict], phi: Optional[dict] = None,
                 controls: Optional[dict] = None) -> list[str]:
    """给结果页汇总栏用的中文短句列表（每条一行）。"""
    lines: list[str] = []
    for g, e in sorted((results or {}).items()):
        f = (e or {}).get("pooled")
        lines.append(f"{g}：{interpret_fit(f)}")
    lines += interpret_phi(phi)
    lines += interpret_controls(controls)
    return lines
