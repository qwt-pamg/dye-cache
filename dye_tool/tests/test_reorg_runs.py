# -*- coding: utf-8 -*-
"""P9：旧 ``runs/`` 目录染料化整理的回归用例。

覆盖三件最容易出错的事：

1. **计划阶段必须只读**——``plan_reorg`` 跑完盘上文件数/时间戳一字不变；
2. **执行必须保守**——只移动、不删除；撞名时加后缀而**不覆盖**；
3. **可核对**——报告要写清"每个目录凭什么判定成这个染料"。
"""
from __future__ import annotations

import csv
from pathlib import Path

from core.config import default_config
from core.reorg_runs import (apply_reorg, describe_plan, plan_reorg,
                             write_reorg_report)

HEADER = ["组别", "角色", "k_obs(s^-1)", "R2"]


def _legacy_flat(root: Path, stamp: str, group: str = "DyeA-pbs-t") -> Path:
    """造一个 V1.0 扁平 run 目录 ``run_<stamp>/``（含 results.csv + figures/）。"""
    d = root / f"run_{stamp}"
    (d / "figures").mkdir(parents=True)
    with (d / "results.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerow([group, "sample", "0.001", "0.99"])
    (d / "report.md").write_text("# 旧报告\n", encoding="utf-8")
    (d / "figures" / "fig_a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    return d


def _func_run(root: Path, func: str, dye: str, rid: str) -> Path:
    """造一个 ``runs/<功能>/<染料>/<run_id>/`` 目录。"""
    d = root / func / dye / rid
    (d / "figures").mkdir(parents=True)
    (d / "figures" / "stack.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"y" * 16)
    return d


def _snapshot(root: Path) -> dict:
    """路径 → (大小, mtime_ns)，用于证明"计划阶段没有动盘"。"""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.stat()
        out[str(p)] = (st.st_size, st.st_mtime_ns, p.is_dir())
    return out


# ═══════════════════════════ 计划阶段 ═══════════════════════════
def test_plan_is_read_only(tmp_path):
    """``plan_reorg`` 必须是纯读：跑完盘上快照一字不变。"""
    _legacy_flat(tmp_path, "20250101-120000")
    _func_run(tmp_path, "stack", "DyeA", "20250102-120000")
    before = _snapshot(tmp_path)
    plan = plan_reorg(tmp_path, default_config())
    assert plan["moves"], "应当识别出待整理目录"
    assert _snapshot(tmp_path) == before, "计划阶段动了盘！"


def test_plan_classifies_and_explains(tmp_path):
    """三类旧形态都要被识别，且每项都带**中文判定依据**。"""
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    _func_run(tmp_path, "stack", "DyeB", "20250102-120000")
    _func_run(tmp_path, "spectra", "_multi", "20250103-120000")
    plan = plan_reorg(tmp_path, default_config())
    kinds = {m["kind"] for m in plan["moves"]}
    assert kinds == {"legacy_flat", "func_stack", "func_multi"}
    for m in plan["moves"]:
        assert m["reason"], "每项都要有判定依据"
        assert any("\u4e00" <= ch <= "\u9fff" for ch in m["reason"]), \
            f"依据应为中文：{m['reason']!r}"
    # 目标路径形态
    dst_by_kind = {m["kind"]: m["dst"] for m in plan["moves"]}
    assert dst_by_kind["legacy_flat"] == tmp_path / "DyeA" / "20250101-120000"
    assert dst_by_kind["func_stack"] == tmp_path / "DyeB" / "20250102-120000"
    assert dst_by_kind["func_multi"] == tmp_path / "_multi" / "20250103-120000"


def test_plan_detects_collision_without_resolving_on_disk(tmp_path):
    """目标已存在 → 标 ``conflict=True`` 并给新名，但**不创建任何目录**。"""
    _legacy_flat(tmp_path, "20250102-120000", group="DyeA-pbs-t")
    existing = tmp_path / "DyeA" / "20250102-120000"
    existing.mkdir(parents=True)
    (existing / "results.csv").write_text("x\n", encoding="utf-8")

    plan = plan_reorg(tmp_path, default_config())
    mv = next(m for m in plan["moves"] if m["kind"] == "legacy_flat")
    assert mv["conflict"] is True
    assert mv["dst"] != existing, "撞名必须换名，不能指向已存在目录"
    assert mv["dst"].name.startswith("20250102-120000_")
    assert not mv["dst"].exists(), "计划阶段不得创建目录"
    assert plan["conflicts"], "冲突应汇总进 conflicts"


def test_plan_skips_already_organized(tmp_path):
    """已是 ``runs/<染料>/<run_id>/`` 的目录应跳过，不重复搬。"""
    (tmp_path / "DyeA" / "20250101-120000").mkdir(parents=True)
    plan = plan_reorg(tmp_path, default_config())
    assert [m for m in plan["moves"]] == []
    assert plan["skips"], "应记录跳过原因"
    assert "已" in plan["skips"][0]["reason"]


def test_plan_missing_root_is_graceful(tmp_path):
    """目录不存在 → 空计划 + 中文 issue，不抛异常。"""
    plan = plan_reorg(tmp_path / "不存在", default_config())
    assert plan["moves"] == []
    assert plan["issues"] and "不存在" in plan["issues"][0]


def test_describe_plan_is_human_readable(tmp_path):
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    txt = describe_plan(plan_reorg(tmp_path, default_config()))
    assert "整理" in txt and "DyeA" in txt
    empty = describe_plan({"moves": [], "conflicts": []})
    assert "无需整理" in empty


# ═══════════════════════════ 执行阶段 ═══════════════════════════
def test_apply_moves_files_and_content_intact(tmp_path):
    """执行后：文件到位、**内容逐字节不变**、旧目录成空壳。"""
    src = _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    png = (src / "figures" / "fig_a.png").read_bytes()
    csv_bytes = (src / "results.csv").read_bytes()

    cfg = default_config()
    plan = plan_reorg(tmp_path, cfg)
    res = apply_reorg(plan, cfg, force=True)

    assert res["moved_n"] == 1 and not res["failed"]
    dst = tmp_path / "DyeA" / "20250101-120000"
    assert (dst / "figures" / "fig_a.png").read_bytes() == png
    assert (dst / "results.csv").read_bytes() == csv_bytes
    assert not src.exists(), "旧目录（含文件）应已被移走"


def test_apply_never_overwrites_on_collision(tmp_path):
    """撞名时必须**两边数据都在**——绝不覆盖已有 run。"""
    _legacy_flat(tmp_path, "20250102-120000", group="DyeA-pbs-t")
    existing = tmp_path / "DyeA" / "20250102-120000"
    existing.mkdir(parents=True)
    (existing / "results.csv").write_text("原有的\n", encoding="utf-8")

    cfg = default_config()
    res = apply_reorg(plan_reorg(tmp_path, cfg), cfg, force=True)
    assert not res["failed"]
    # 原有文件没被覆盖
    assert (existing / "results.csv").read_text(encoding="utf-8") == "原有的\n"
    # 新数据落在带后缀的目录里
    moved = [d for d in res["done"] if d["conflict"]]
    assert len(moved) == 1
    assert (moved[0]["dst"] / "report.md").exists()


def test_apply_archives_empty_shells(tmp_path):
    """移完的空壳旧目录移进 ``_legacy/`` **保留**（不删除）。"""
    _func_run(tmp_path, "stack", "DyeA", "20250102-120000")
    cfg = default_config()
    res = apply_reorg(plan_reorg(tmp_path, cfg), cfg, force=True)
    assert res["moved_n"] == 1
    assert (tmp_path / "DyeA" / "20250102-120000").is_dir()
    assert not (tmp_path / "stack").exists(), "空壳应被移走"
    assert (tmp_path / "_legacy" / "stack").is_dir(), "空壳应在 _legacy/ 里保留"


def test_shell_kept_when_not_empty(tmp_path):
    """旧目录里还有没搬走的东西时**不许**动它（防误删）。"""
    _func_run(tmp_path, "stack", "DyeA", "20250102-120000")
    (tmp_path / "stack" / "未归类.txt").write_text("手写的\n", encoding="utf-8")
    cfg = default_config()
    apply_reorg(plan_reorg(tmp_path, cfg), cfg, force=True)
    assert (tmp_path / "stack" / "未归类.txt").exists(), "有内容就不该被动"
    assert not (tmp_path / "_legacy" / "stack").exists()


def test_apply_writes_report(tmp_path):
    """报告要写清每项的来源、去向、染料与依据（可人工核对）。"""
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    cfg = default_config()
    res = apply_reorg(plan_reorg(tmp_path, cfg), cfg, force=True)
    rep = res["report"]
    assert rep is not None and rep.exists()
    txt = rep.read_text(encoding="utf-8")
    for needle in ("已移动", "DyeA", "20250101-120000", "results.csv 组别列",
                   "只移动、不删除"):
        assert needle in txt, f"报告缺少 {needle!r}"


def test_apply_is_idempotent(tmp_path):
    """整理两遍：第二遍应报"无需整理"，不再产生移动。"""
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    cfg = default_config()
    apply_reorg(plan_reorg(tmp_path, cfg), cfg, force=True)
    plan2 = plan_reorg(tmp_path, cfg)
    assert plan2["moves"] == []
    res2 = apply_reorg(plan2, cfg, force=True)
    assert res2["moved_n"] == 0 and not res2["failed"]


def test_apply_reports_missing_source_instead_of_pretending(tmp_path):
    """计划生成后源目录被删掉 → 记 FAILED 并给中文原因，**不假装成功**。"""
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    cfg = default_config()
    plan = plan_reorg(tmp_path, cfg)
    import shutil
    shutil.rmtree(plan["moves"][0]["src"])
    res = apply_reorg(plan, cfg, force=True)
    assert res["moved_n"] == 0
    assert len(res["failed"]) == 1
    assert "不存在" in res["failed"][0]["error"]


def test_dry_run_default_is_safe():
    """``dye.reorg.dry_run`` 默认必须是 ``True``（先看计划再动盘）。

    M7（评审记录）：这个键原先**没有消费者**（死配置），用户设了以为生效、
    加 ``--apply`` 照样动盘；而 docstring 却把安全性归功于它。
    现在 ``apply_reorg`` 尊重它——所以下面所有"真执行"的用例都必须显式
    ``force=True``，本用例则专门验证"不 force 时一个字都不动"。
    """
    cfg = default_config()
    assert cfg["dye"]["reorg"]["dry_run"] is True
    assert cfg["dye"]["reorg"]["keep_legacy"] is True
    # 已删除的死键不该回来
    assert "scan_root" not in cfg["dye"]["reorg"]


def test_dry_run_blocks_apply_unless_forced(tmp_path):
    """M7：``dry_run=true`` 且未 force → **完全不动盘**，并给出中文原因。"""
    src = _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    cfg = default_config()                       # dry_run 默认 True
    plan = plan_reorg(tmp_path, cfg)
    res = apply_reorg(plan, cfg)                 # 刻意**不** force
    assert res["dry_run"] is True
    assert res["moved_n"] == 0 and not res["failed"]
    assert "dry_run" in res["message"]
    assert src.is_dir(), "dry_run 下源目录必须原地不动"
    assert not (tmp_path / "DyeA" / "20250101-120000").exists()
    # force=True 才真的动
    res2 = apply_reorg(plan, cfg, force=True)
    assert res2["dry_run"] is False and res2["moved_n"] == 1


def test_plan_detects_duplicate_destination_within_plan(tmp_path):
    """M6：**计划内**两条 move 指向同一 dst 时，计划期就要标 conflict 并换名。

    场景：``stack/DyeA/<stamp>`` 与 ``compare/DyeA/<stamp>`` 归入同染料同时间戳。
    原先两处 ``dst.exists()`` 都是 False → 两条都记 conflict=False；apply 时
    第二条才被静默 ``_unique`` 成 ``…_1``——**执行了计划里没有的目标**，
    而报告仍写"撞名：否"，"先看计划再执行"的承诺被打折。
    """
    stamp = "20250102-120000"
    _func_run(tmp_path, "stack", "DyeA", stamp)
    _func_run(tmp_path, "compare", "DyeA", stamp)
    cfg = default_config()
    plan = plan_reorg(tmp_path, cfg)
    assert len(plan["moves"]) == 2
    dsts = [str(m["dst"]) for m in plan["moves"]]
    assert len(set(dsts)) == 2, f"计划里两条目标重名了：{dsts}"
    conflicts = [m for m in plan["moves"] if m["conflict"]]
    assert len(conflicts) == 1, "计划内的第二条应被标为 conflict"
    # 计划里的目标就是 apply 的目标（不再出现"执行了计划外目标"）
    res = apply_reorg(plan, cfg, force=True)
    assert not res["failed"]
    assert {str(d["dst"]) for d in res["done"]} == set(dsts)
    assert len(list((tmp_path / "DyeA").iterdir())) == 2


# ═══════════════════════════ 染料判定 ═══════════════════════════
def test_dye_from_results_csv_uses_same_naming_as_ui(tmp_path):
    """判定要复用 ``resolve_dye_name`` 的口径，不能另立一套。

    ``DyeA-pbs-t`` 的首字段是 ``DyeA`` → 染料应为 ``DyeA``（与 GUI 里看到的一致）。
    """
    _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    plan = plan_reorg(tmp_path, default_config())
    assert plan["moves"][0]["dye"] == "DyeA"
    assert "组别列" in plan["moves"][0]["reason"]


def test_unresolvable_dye_goes_to_multi(tmp_path):
    """results.csv 缺失且无 run_params → 不能瞎猜，归入 ``_multi``。"""
    d = tmp_path / "run_20250101-120000"
    (d / "figures").mkdir(parents=True)
    (d / "figures" / "x.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    plan = plan_reorg(tmp_path, default_config())
    assert plan["moves"][0]["dye"] == "_multi"
    assert "无法判定" in plan["moves"][0]["reason"]


def test_run_params_yaml_takes_precedence(tmp_path):
    """``run_params.yaml`` 里的染料名优先于 results.csv 猜测。"""
    d = _legacy_flat(tmp_path, "20250101-120000", group="DyeA-pbs-t")
    (d / "run_params.yaml").write_text("dye: ExplicitDye\n", encoding="utf-8")
    plan = plan_reorg(tmp_path, default_config())
    assert plan["moves"][0]["dye"] == "ExplicitDye"
    assert "run_params" in plan["moves"][0]["reason"]


def test_write_reorg_report_lists_failures(tmp_path):
    """报告对失败项要单独醒目列出（不能混在成功里）。"""
    plan = {"root": tmp_path, "moves": [], "skips": [], "conflicts": [],
            "issues": ["演示"], "shells": []}
    failed = [{"src": tmp_path / "run_x", "dst": tmp_path / "A" / "x",
               "dye": "A", "reason": "测试", "kind": "legacy_flat",
               "error": "目标已存在，拒绝覆盖"}]
    p = write_reorg_report(plan, [], failed, [], tmp_path)
    txt = p.read_text(encoding="utf-8")
    assert "失败" in txt and "拒绝覆盖" in txt
