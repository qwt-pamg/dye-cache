# -*- coding: utf-8 -*-
"""``core.config`` 单元测试：深合并、默认配置隔离、yaml 读写、点号取值。

含 **DS1 全树键校验 + schema 版本化**（V2.0 FR-18 / 设计稿 §1.1）：
三档政策、封顶、round-trip 零告警、老配置静默、假迁移只改内存。
"""
from __future__ import annotations

import core.config as cfg_mod
from core.config import (AG_DEFAULT, DEFAULT_CFG, KNOWN_PATHS, SCHEMA_VERSION,
                         autogroup_cfg, deep_merge, default_config, get,
                         load_config, load_config_ex, save_config,
                         unknown_key_issues)

_REG_KEYS = ("wavelength", "baseline", "background", "a0", "kinetics",
             "quantum_yield", "qc", "controls", "plot", "autogroup", "spectra",
             "io", "filename", "gui", "dye", "excel", "extinction",
             "wavelength_table", "qc_dashboard")


def _write_yaml(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _levels(issues) -> list[str]:
    return [i.level for i in issues]


# ═══════════════════════════ deep_merge ═══════════════════════════
def test_deep_merge_recurses_into_nested_dict() -> None:
    """dict 递归合并：同层合并、缺失键补齐、不覆盖无关键。"""
    base = {"a": {"b": 1, "c": {"d": 2, "e": 3}}, "keep": 9}
    out = deep_merge(base, {"a": {"b": 100, "c": {"d": 200}, "new": 7}})
    assert out is base                                  # 就地修改并返回 base
    assert base["a"]["b"] == 100
    assert base["a"]["c"] == {"d": 200, "e": 3}
    assert base["a"]["new"] == 7
    assert base["keep"] == 9


def test_deep_merge_overrides_list_wholesale() -> None:
    """list 整体覆盖（不是逐元素拼接）——与 legacy 语义一致。"""
    base = {"lst": [1, 2, 3], "t": (1, 2)}
    deep_merge(base, {"lst": [7], "t": "s"})
    assert base["lst"] == [7]
    assert base["t"] == "s"


def test_deep_merge_non_dict_replaces_dict_and_empty_extra_is_noop() -> None:
    """base 侧不是 dict 时整体覆盖；extra 为空/None 时不动 base。"""
    base = {"a": 1}
    deep_merge(base, {"a": {"b": 2}})
    assert base["a"] == {"b": 2}
    assert deep_merge(base, {}) is base
    assert deep_merge(base, None) is base


# ═══════════════════════════ default_config ═══════════════════════════
def test_default_config_is_independent_deep_copy() -> None:
    """修改 default_config() 的结果绝不能影响 DEFAULT_CFG。"""
    cfg = default_config()
    cfg["wavelength"]["probe"] = 999.0
    cfg["io"]["extensions"].append(".xyz")
    cfg["quantum_yield"]["a_ex"]["ghost"] = 1.0
    cfg["new_section"] = {"x": 1}

    assert DEFAULT_CFG["wavelength"]["probe"] == 379.0
    assert ".xyz" not in DEFAULT_CFG["io"]["extensions"]
    assert DEFAULT_CFG["quantum_yield"]["a_ex"] == {}
    assert "new_section" not in DEFAULT_CFG
    assert default_config() == DEFAULT_CFG              # 再次获取仍是纯净默认值


# ═══════════════════════════ load_config ═══════════════════════════
def test_load_config_none_and_missing_path_fall_back_to_defaults(tmp_path) -> None:
    """path=None / 文件不存在 → 返回默认配置且不抛异常。"""
    assert load_config(None) == DEFAULT_CFG
    assert load_config(tmp_path / "no_such_config.yaml") == DEFAULT_CFG


def test_load_config_malformed_yaml_falls_back_without_raising(tmp_path) -> None:
    """YAML 语法错误 → 记 WARNING 后回退默认配置（顶层不得抛）。"""
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [1, 2\nb: }}}\n", encoding="utf-8")
    assert load_config(bad) == DEFAULT_CFG


def test_load_config_non_dict_top_level_falls_back(tmp_path) -> None:
    """顶层是 list / 标量（非映射）→ 回退默认配置。"""
    lst = tmp_path / "list.yaml"
    lst.write_text("- 1\n- 2\n", encoding="utf-8")
    assert load_config(lst) == DEFAULT_CFG
    scl = tmp_path / "scalar.yaml"
    scl.write_text("just a string\n", encoding="utf-8")
    assert load_config(scl) == DEFAULT_CFG


def test_load_config_merges_user_overrides(tmp_path) -> None:
    """用户 yaml 深合并进默认配置：覆盖指定键、保留其余默认值。"""
    p = tmp_path / "user.yaml"
    p.write_text("wavelength:\n  probe: 405.0\nqc:\n  saturation: 2.5\n",
                 encoding="utf-8")
    cfg = load_config(p)
    assert cfg["wavelength"]["probe"] == 405.0
    assert cfg["wavelength"]["probe_window"] == DEFAULT_CFG["wavelength"]["probe_window"]
    assert cfg["qc"]["saturation"] == 2.5
    assert cfg["qc"]["r2_warn"] == DEFAULT_CFG["qc"]["r2_warn"]


def test_save_config_round_trip(tmp_path) -> None:
    """save_config → load_config 往返：用户子集原样恢复，且 DEFAULT_CFG 不被污染。"""
    cfg = default_config()
    cfg["wavelength"]["probe"] = 400.0
    cfg["quantum_yield"]["phi_ref"] = 0.52
    cfg["kinetics"]["truncate_frac"] = 0.6
    p = save_config(cfg, tmp_path / "sub" / "config.yaml")
    assert p.exists()
    assert load_config(p) == cfg
    assert DEFAULT_CFG["wavelength"]["probe"] == 379.0


# ═══════════════════════════ autogroup_cfg / get ═══════════════════════════
def test_autogroup_cfg_default_and_override() -> None:
    """AG_DEFAULT 为底、用户 autogroup 节为覆盖；返回新 dict。"""
    cfg = default_config()
    assert autogroup_cfg(cfg) == AG_DEFAULT
    merged = autogroup_cfg({**cfg, "autogroup": {"d_max": 0.02}})
    assert merged["d_max"] == 0.02
    assert merged["core_lo"] == AG_DEFAULT["core_lo"]
    merged["d_max"] = 999.0
    assert AG_DEFAULT["d_max"] == 0.06                   # 不得改动全局默认


def test_get_dotted_path(cfg) -> None:
    """``"a.b.c"`` 取值：命中返回值、缺失返回 default、中途非 dict 安全返回。"""
    assert get(cfg, "wavelength.probe") == 379.0
    assert get(cfg, "io.extensions") == [".txt", ".csv"]
    assert get(cfg, "wavelength.nope", 5) == 5
    assert get(cfg, "nope.deep.path", "dflt") == "dflt"
    assert get(cfg, "wavelength.probe.too_deep", -1) == -1
    assert get(cfg, "wavelength.probe", None) == 379.0


# ═══════════════ DS1：全树键校验 + schema 版本化（V2.0 FR-18） ═══════════════
def test_known_paths_expand_from_default_cfg() -> None:
    """KNOWN_PATHS 从 DEFAULT_CFG 递归展开，**不另维护键清单**（唯一真相）。"""
    assert KNOWN_PATHS == frozenset(cfg_mod._expand_paths(DEFAULT_CFG))
    # 每一节都至少有一个叶子路径（防止某节被整节漏掉）
    for section in _REG_KEYS:
        assert any(p == section or p.startswith(section + ".") for p in KNOWN_PATHS), \
            f"{section} 节没有出现在 KNOWN_PATHS 里"
    # 已知的具体叶子（回归锚点：V2.0 起 gui.nav 是合法键）
    assert {"gui.nav", "wavelength.probe", "qc.saturation",
            "plot.jacs.width_in"} <= KNOWN_PATHS
    # plot.jacs 叶子数 = JACS_SCHEMA 键数（V2.0 设计文档 R1：实测 103；T1 修锚点 2026-10-16）
    jacs = [p for p in KNOWN_PATHS if p.startswith("plot.jacs.")]
    assert len(jacs) == 103


def test_gui_saved_config_has_zero_unknown_key_warnings(tmp_path) -> None:
    """**本设计最重要的兼容性质**：GUI 保存过的配置重载 → 零新告警。

    GUI 写出的键全部来自 DEFAULT_CFG（save_config 的 keys 白名单），所以
    按 DS1 只校验用户手写 dict 的设计，这批用户不该收到任何新弹窗。
    """
    cfg = default_config()
    cfg["wavelength"]["probe"] = 405.0
    cfg["gui"]["last_dir"] = str(tmp_path)         # GUI 会话态（不进 DEFAULT_CFG）
    cfg["plot"]["jacs"]["_preset"] = "lab-ppt"     # 预设标记（私有键）
    p = save_config(cfg, tmp_path / "sub" / "config.yaml")
    _cfg, issues = load_config_ex(p)
    assert issues == [], f"GUI round-trip 产生了告警：{issues}"


def test_unknown_top_level_key_warns_with_suggestion(tmp_path) -> None:
    """③ 完全未知 → WARNING + difflib 近似键建议（唯一建议来源 suggest_key）。"""
    p = _write_yaml(tmp_path, "typo.yaml", "wavelegth:\n  probe: 400.0\n")
    _cfg, issues = load_config_ex(p)
    assert _levels(issues) == ["WARNING"]
    assert issues[0].key == "wavelegth.probe"
    assert "wavelength.probe" in issues[0].suggestion


def test_dead_keys_get_dedicated_wording(tmp_path) -> None:
    """① 已废除键 → 专用文案（不是"拼写错误"），四字段齐全。"""
    p = _write_yaml(tmp_path, "dead.yaml",
                    "io:\n  encoding: gbk\npaths:\n  scan_root: D:/x\n")
    _cfg, issues = load_config_ex(p)
    keys = [i.key for i in issues]
    assert keys == ["io.encoding", "paths.scan_root"], keys
    assert all(i.level == "WARNING" for i in issues)
    for i in issues:
        assert "已废除" in i.message and i.suggestion
    # 值照常保留进 cfg（宽进：行为零变化）
    cfg = load_config(p)
    assert cfg["io"]["encoding"] == "gbk"


def test_unknown_keys_are_capped_with_summary(tmp_path) -> None:
    """封顶：7 个未知键 → 5 条明细（每条一个键）+ 1 条汇总。"""
    p = _write_yaml(tmp_path, "many.yaml",
                    "".join(f"nokey{i}:\n  sub: 1\n" for i in range(7)))
    _cfg, issues = load_config_ex(p)
    assert len(issues) == 6, [i.key for i in issues]
    assert all(i.level == "WARNING" for i in issues)
    assert [i.key for i in issues[:5]] == [f"nokey{i}.sub" for i in range(5)]
    assert "另有 2 个" in issues[5].message
    # 直接调函数也是同一口径
    assert len(unknown_key_issues({f"k{i}": 1 for i in range(9)})) == 6


def test_plot_jacs_subtree_is_left_to_validate_jacs(tmp_path) -> None:
    """``plot.jacs`` 子树整棵跳过 —— 否则与 validate_jacs 一键双报。"""
    p = _write_yaml(tmp_path, "jacs.yaml",
                    "plot:\n  jacs:\n    totally_bogus: 1\n")
    _cfg, issues = load_config_ex(p)
    keys = [i.key for i in issues]
    assert keys.count("plot.jacs.totally_bogus") == 1, keys
    # 直接调 unknown_key_issues：该子树一条都不报
    assert unknown_key_issues({"plot": {"jacs": {"totally_bogus": 1}}}) == []


def test_private_and_transient_keys_are_skipped(tmp_path) -> None:
    """``_`` 私有键（任意层级）+ 运行时内部键（gui.last_dir）都不报未知键。

    这两类键**不是**用户手写的，报出来就是假告警（原则 2）。
    """
    p = _write_yaml(tmp_path, "priv.yaml",
                    "gui:\n  last_dir: D:/somewhere\n  max_recent: 6\n"
                    "plot:\n  jacs:\n    _preset: lab-ppt\n")
    _cfg, issues = load_config_ex(p)
    assert issues == [], [i.key for i in issues]
    assert unknown_key_issues({"gui": {"last_dir": "x"},
                               "_dye_session": {"a": 1}}) == []
    # 反例：同类但**不是**内部键的拼错键仍要报（否则这条测试等于没测）
    assert [i.key for i in unknown_key_issues({"gui": {"last_dirs": "x"}})] == \
        ["gui.last_dirs"]


def test_old_config_without_schema_is_silent(tmp_path) -> None:
    """④ 老配置没有 ``_meta.schema`` → 静默视为当前版本，**不告警**。

    这批用户不该因为我们加了版本机制而收到新弹窗（设计稿 §1.1 兼容承诺）。
    """
    p = _write_yaml(tmp_path, "old.yaml",
                    "_meta:\n  written_by: 1.0.0\nwavelength:\n  probe: 410.0\n")
    cfg, issues = load_config_ex(p)
    assert issues == [], issues
    assert cfg["wavelength"]["probe"] == 410.0


def test_future_schema_warns_once(tmp_path) -> None:
    """高于本程序认识的 schema 版本 → 一条 WARNING（不阻塞）。"""
    p = _write_yaml(tmp_path, "fut.yaml",
                    f"_meta:\n  schema: {SCHEMA_VERSION + 1}\n")
    _cfg, issues = load_config_ex(p)
    assert _levels(issues) == ["WARNING"]
    # 键名在 δ 批从 `_meta.schema` 改成通用的 `schema_version`（同一套迁移算法
    # 也服务 dye_batch.yaml，两种文件的版本键名不同但语义一致）
    assert issues[0].key == "schema_version"
    # 非整数版本标记 → 也是 WARNING（不抛）
    p2 = _write_yaml(tmp_path, "weird.yaml", "_meta:\n  schema: abc\n")
    _cfg2, issues2 = load_config_ex(p2)
    assert _levels(issues2) == ["WARNING"]


def test_fake_migration_upgrades_in_memory_only(tmp_path) -> None:
    """⑤ 假迁移注册表 → 正确升级，且**原文件字节未变**（原则 6：只在内存迁移）。"""
    p = _write_yaml(tmp_path, "mig.yaml",
                    "_meta:\n  schema: 1\nold_name:\n  probe: 421.0\n")
    before = p.read_bytes()
    real = list(cfg_mod._MIGRATIONS)
    real_ver = cfg_mod.SCHEMA_VERSION

    def _migrate(user: dict) -> None:
        if "old_name" in user:
            user["wavelength"] = {"probe": user.pop("old_name")["probe"]}

    try:
        cfg_mod._MIGRATIONS[:] = [(1, _migrate)]
        cfg_mod.SCHEMA_VERSION = 2
        cfg, issues = load_config_ex(p)
    finally:
        cfg_mod._MIGRATIONS[:] = real
        cfg_mod.SCHEMA_VERSION = real_ver
    assert cfg["wavelength"]["probe"] == 421.0        # 迁移值生效
    assert cfg["wavelength"]["probe_window"] == 2.0   # 其余键仍是默认
    assert p.read_bytes() == before                   # 磁盘文件一个字节没动
    # `old_name` 已被迁移消费 → 不应再报"未知键"
    assert issues == [], issues


def test_default_config_has_zero_unknown_keys_and_validate_issues() -> None:
    """默认配置零 issues 的回归锚点（V2.0 新增全树校验后仍必须成立）。"""
    from core.plotstyle import validate_config
    assert validate_config(default_config()) == []
    assert unknown_key_issues(DEFAULT_CFG) == []

# ═══════════ V28-M2：check.py 并行门禁负例（rc=5 / collect-only 失败不得判绿） ═══════════
def _load_check_module():
    """按路径加载 tools/check.py（tools 不是包；与旧 A20 测试同法）。"""
    import importlib.util
    import sys
    from pathlib import Path

    tools = Path(__file__).resolve().parents[1] / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    p = tools / "check.py"
    spec = importlib.util.spec_from_file_location("_dye_check_v28_gate", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeShardRun:
    index = 1
    timed_out = False
    wall_s = 0.0

    def __init__(self, rc: int, output: str) -> None:
        self.rc = rc
        self.output = output

    def counts(self) -> dict:
        import parallel_pytest
        return parallel_pytest.parse_counts(self.output)

    def selected(self) -> int:
        c = self.counts()
        return sum(c[k] for k in ("passed", "failed", "error", "skipped",
                                  "xfailed", "xpassed"))


def _patch_parallel(monkeypatch, runs: list, tmp_path) -> tuple:
    """把 check.ROOT 指到 tmp（避免嵌套调用删真实 test_temp/check_parallel 破坏并行分片）。"""
    mod = _load_check_module()
    import parallel_pytest
    proj = tmp_path / "proj"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_x.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(mod, "ROOT", proj)
    calls = {"run_shards": 0}

    def fake_run_shards(*_a, **_k):
        calls["run_shards"] += 1
        return runs

    monkeypatch.setattr(parallel_pytest, "run_shards", fake_run_shards)
    return mod, calls


def test_check_parallel_collect_failure_is_red(monkeypatch, tmp_path) -> None:
    """V28-M2：collect-only 失败必须直接判失败，不能继续跑分片。"""
    mod, calls = _patch_parallel(monkeypatch, [], tmp_path)
    monkeypatch.setattr(mod, "_collect_total", lambda: (-1, "伪造收集失败"))
    assert mod._pytest_full_parallel(1) is False
    assert calls["run_shards"] == 0


def test_check_parallel_rc5_is_red(monkeypatch, tmp_path) -> None:
    """V28-M2：shard rc=5（未收集到用例）不得被当作可接受。"""
    mod, calls = _patch_parallel(monkeypatch, [_FakeShardRun(5, "")], tmp_path)
    monkeypatch.setattr(mod, "_collect_total", lambda: (1, "伪总 1"))
    assert mod._pytest_full_parallel(1) is False
    assert calls["run_shards"] == 1


def test_check_parallel_good_is_green(monkeypatch, tmp_path) -> None:
    """正例：rc=0 且 selected 与 collect-only 一致才判绿。"""
    mod, _calls = _patch_parallel(monkeypatch,
                                  [_FakeShardRun(0, "1 passed in 0.01s")],
                                  tmp_path)
    monkeypatch.setattr(mod, "_collect_total", lambda: (1, "伪总 1"))
    assert mod._pytest_full_parallel(1) is True

# ═══════════ V2.9 FR-59 甲：随包 QUICKSTART（M3） ═══════════
def test_quickstart_asset_and_build_copy() -> None:
    from pathlib import Path
    q = Path(__file__).resolve().parents[1] / "packaging" / "QUICKSTART.txt"
    assert q.is_file()
    text = q.read_text(encoding="utf-8")
    for key in ("三步上手", "文件命名", "剔除原因", "--cli", "dye_batch.yaml"):
        assert key in text, key
    assert len(text.splitlines()) <= 60


def test_build_script_copies_quickstart_to_dist_root() -> None:
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / "packaging" / "build.ps1"
    text = p.read_text(encoding="utf-8-sig")
    assert "QUICKSTART.txt" in text
    assert "packaging\\QUICKSTART.txt" in text
