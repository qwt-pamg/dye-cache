"""V2.8 FR-48：MainWindow 页面胶水/事件 mixin。"""

from __future__ import annotations

import logging

from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction
from PySide6.QtGui import QKeySequence
from PySide6.QtGui import QShortcut
from PySide6.QtWidgets import QMessageBox
from ui.window.parts import SHORTCUT_GROUPS

log = logging.getLogger("dye_tool.ui.window.main_window")

class MainWindowNavMixin:
    def _on_nav(self, key: str) -> None:
        page = self.flow_pages.get(key)
        if page is None:
            return
        # B1：离开 ⑤设置 时把它的面板值写回 cfg（否则关窗只存 Φ_Δ 那份 → 静默丢失）
        prev = getattr(self, "_nav_current", "")
        if prev == "settings" and key != "settings":
            self._sync_settings_page(False)
        if key == "settings":
            self._sync_settings_page(True)
        self._nav_current = key
        self.stack.setCurrentWidget(page)
        # 切页后的刷新失败不能让导航失灵，但要留下记录
        try:
            if callable(getattr(page, "on_activated", None)):
                page.on_activated()
            if key == "archive":
                self.archive_page.reload()
            elif key == "home":
                self.home_page.cfg = self.cfg
                self.home_page.refresh()
        except Exception:                                  # noqa: BLE001
            log.exception("切到「%s」后刷新页面失败", key)
        QTimer.singleShot(0, lambda p=page: self._refresh_help(p))

    # ══════════════ V2.6 FR-40.3：快捷键可发现性 ══════════════
    def _init_nav_shortcuts(self) -> None:
        """Alt+1..6 切页 + 导航项 tooltip 标注（flow 布局）。"""
        from ui.window.nav import NAV_ITEMS
        nav_keys = [key for key, _label, _tip in NAV_ITEMS]
        self._nav_shortcuts: list = []
        for i, key in enumerate(nav_keys, 1):
            sc = QShortcut(QKeySequence(f"Alt+{i}"), self)
            sc.activated.connect(lambda k=key: self.nav.select(k))
            self._nav_shortcuts.append(sc)
        self._apply_nav_shortcut_tips()

    def _apply_nav_shortcut_tips(self) -> None:
        """导航项 tooltip 标注 Alt+1..6（批次徽标刷新后也重贴一次）。"""
        from ui.window.nav import NAV_ITEMS
        hint = "导航快捷键：Alt+1 主页 / Alt+2 ①数据 / Alt+3 ②分析 / " \
               "Alt+4 ③图件 / Alt+5 ④档案 / Alt+6 ⑤设置"
        nav = getattr(self, "nav", None)
        if nav is None:
            return
        try:
            nav.list.setToolTip(hint)
            nav.setToolTip(hint)
        except Exception:                                  # noqa: BLE001
            pass
        for row, (key, _label, tip) in enumerate(NAV_ITEMS):
            item = nav.list.item(row)
            if item is not None:
                item.setToolTip(f"{tip}\n快捷键：Alt+{row + 1}")

    @staticmethod
    def shortcut_groups() -> dict:
        """快捷键分组清单（供测试/帮助对话框读取；数据源 = SHORTCUT_GROUPS）。"""
        return {k: list(v) for k, v in SHORTCUT_GROUPS.items()}

    def shortcut_overview_text(self) -> str:
        """快捷键一览文本：分组清单 + 菜单命令（QAction 自带快捷键）。"""
        lines = ["快捷键一览（分组：全局 / 数据 / 分析 / 作图台 / 画布 / 时间表）", ""]
        for group, rows in SHORTCUT_GROUPS.items():
            lines.append(f"【{group}】")
            for key, desc in rows:
                lines.append(f"  {key:<14}{desc}")
            lines.append("")
        lines.append("【菜单命令（菜单项自带快捷键）】")
        seen: set = set()
        for act in self.findChildren(QAction):
            try:
                seq = act.shortcut().toString()
            except Exception:                              # noqa: BLE001
                seq = ""
            text = act.text() or ""
            if not seq or not text or seq in seen:
                continue
            seen.add(seq)
            lines.append(f"  {seq:<14}{text}")
        return "\n".join(lines)

    def _show_shortcut_overview(self) -> None:
        """帮助 →「快捷键一览…」（模态信息框；内容与 SHORTCUT_GROUPS 同源）。"""
        self._text_dialog("快捷键一览", self.shortcut_overview_text())

    # ══════════════ V2.6 FR-40.6：样式面板「重置全部」接线 ══════════════
    def _wire_style_panel(self, page) -> None:
        """把页面的 StylePanel.reset_requested 接到 live_plot.clear_styles()。"""
        panel = getattr(page, "style_panel", None)
        live = getattr(page, "live", None)
        if panel is None or live is None:
            return
        try:
            panel.reset_requested.connect(
                lambda p=page: self._reset_page_styles(p))
            panel.mark_reset_handler()
        except Exception as err:                           # noqa: BLE001
            log.warning("样式面板重置接线失败：%s", err)

    def _reset_page_styles(self, page) -> None:
        """重置某页全部曲线的会话级样式（复用 live_plot.clear_styles()）。"""
        live = getattr(page, "live", None)
        if live is None:
            return
        live.clear_styles()
        panel = getattr(page, "style_panel", None)
        if panel is not None:
            panel.clear_history()
        sync = getattr(page, "_sync_style_panel", None)
        if callable(sync):
            try:
                sync()
            except Exception as err:                       # noqa: BLE001
                log.warning("重置样式后刷新面板失败：%s", err)
        status = getattr(page, "status", None)
        if status is not None:
            try:
                status.emit("已重置全部曲线的临时样式（颜色/线型/线宽/标记）")
            except Exception:                              # noqa: BLE001
                pass

    # ══════════════ V2.6 FR-40.4/40.5：空态引导 / 最近目录 / 下一步 ══════════════
    def _goto_data_and_focus(self) -> None:
        """②分析页空态按钮 → 切到①数据页并聚焦入口。"""
        self._goto_and_focus_entry("data")

    def _goto_and_focus_entry(self, key: str) -> None:
        """切到某导航项；data 页额外把焦点放到「选择数据目录…」入口。"""
        key = str(key or "data")
        if self.layout_mode == "flow":
            self.nav.select(key)
        if key == "data":
            self._focus_data_entry()

    def _focus_data_entry(self) -> None:
        """把键盘焦点放到①数据页的目录入口（空态引导的可达性）。"""
        entry = getattr(getattr(self, "data_page", None), "btn_dir", None)
        if entry is None:
            return
        try:
            self.activateWindow()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            entry.setFocus()
            QTimer.singleShot(0, entry.setFocus)
        except Exception:                                  # noqa: BLE001
            pass

    def _on_record_pair_requested(self, abs_path: str, em_path: str) -> None:
        """V2.6 FR-41.5：③作图台「在分析页记录」→ ②Stokes 测量卡预填 + 切页。

        预填接口由 F 道测量卡提供（AnalysisPage.prefill_stokes_pair(abs, em)）；
        F 道尚未落地时只记 WARNING 并切页（不阻断③作图台）。
        """
        ap = getattr(self, "analysis_page", None)
        if ap is None:
            self.on_logged("WARNING",
                           "经典页签布局没有②分析页：Stokes 测量卡只在任务流布局提供")
            self.on_status("「在分析页记录」需要任务流布局（重启后切回）")
            return
        prefill = getattr(ap, "prefill_stokes_pair", None)
        if callable(prefill):
            try:
                prefill(str(abs_path), str(em_path))
            except Exception as err:                   # noqa: BLE001 —— 桥接失败不崩
                self.on_logged("WARNING", f"②分析页 Stokes 预填失败：{err}")
        else:
            self.on_logged(
                "WARNING", "②分析页尚未提供 Stokes 测量卡（prefill_stokes_pair 缺失）"
                           "：已切到分析页，请等待 F 道测量卡落地")
        self.nav.select("analysis")
        select = getattr(ap, "select_task", None)
        pages = getattr(ap, "_pages", None)
        # 卡键由 F 道提供（Q-E16：第 6 卡 "stokes"）；F 道尚未挂载面板时跳过。
        card_key = "stokes" if (not isinstance(pages, dict) or "stokes" in pages) else ""
        if callable(select) and card_key:
            try:
                select(card_key)
            except Exception:                          # noqa: BLE001
                pass
        self.on_status("已切到②分析页：确认配对数值后点「加入对比表」")

    def _on_next_step(self, target: str = "plot") -> None:
        """结果区「下一步」→ ③ 图件 / 定稿出图（或 ④ 档案）。"""
        key = "archive" if str(target or "plot") == "archive" else "plot"
        if self.layout_mode == "flow":
            if self.nav.select(key):
                self.on_status("下一步 → ③ 图件预览 / 定稿出图" if key == "plot"
                               else "下一步 → ④ 档案查看历史结果")
                return
            return
        if key == "plot" and getattr(self, "stack_page", None) is not None:
            self.tabs.setCurrentWidget(self.stack_page)
        elif key == "archive" and getattr(self, "dye_page", None) is not None:
            self.tabs.setCurrentWidget(self.dye_page)

    def _recent_dirs_for_menu(self) -> list:
        """文件菜单最近目录：gui.recent_dirs 前 5 条（仅保留仍存在的目录）。"""
        gui = (self.cfg or {}).get("gui") or {}
        out: list = []
        for raw in (gui.get("recent_dirs") or []):
            try:
                p = Path(str(raw))
            except (TypeError, ValueError):
                continue
            if p.is_dir():
                out.append(p)
            if len(out) >= 5:
                break
        return out

    def _refresh_recent_menu(self) -> None:
        """重建「最近打开的数据目录」子菜单（打开菜单时/清空后调用）。"""
        menu = getattr(self, "recent_menu", None)
        if menu is None:
            return
        menu.clear()
        dirs = self._recent_dirs_for_menu()
        for d in dirs:
            act = menu.addAction(f"{d.name} — {d}")
            act.setToolTip(str(d))
            act.triggered.connect(lambda _=False, p=d: self._open_recent_dir(p))
        if dirs:
            menu.addSeparator()
        clear_act = menu.addAction("清空最近")
        clear_act.triggered.connect(self._clear_recent_dirs)
        gui = (self.cfg or {}).get("gui") or {}
        clear_act.setEnabled(bool(gui.get("recent_dirs")))

    def _open_recent_dir(self, path) -> None:
        """文件菜单/主页最近批次直达：切到①数据页并载入该目录（同一批次源）。"""
        try:
            p = Path(str(path))
        except (TypeError, ValueError):
            return
        if not p.is_dir():
            self.on_status(f"目录不存在：{p}")
            return
        if self.layout_mode == "flow":
            self.nav.select("data")
            loader = getattr(self.data_page, "load_dir", None)
            if callable(loader):
                loader(p)
            self._focus_data_entry()
        else:
            page = self._current_page()
            loader = (getattr(page, "_load_dir", None)
                      or getattr(page, "load_dir", None))
            if callable(loader):
                loader(p)
            else:
                self.on_status("当前布局无法载入数据目录")
        self.on_status(f"正在载入最近目录：{p}")

    def _clear_recent_dirs(self) -> None:
        """清空 gui.recent_dirs（会话态；走既有保存链路，不新增配置键）。"""
        self.cfg.setdefault("gui", {})["recent_dirs"] = []
        try:
            self._save_cfg()
        except Exception as err:                           # noqa: BLE001
            log.warning("清空最近目录后保存配置失败：%s", err)
        self._refresh_recent_menu()
        hp = getattr(self, "home_page", None)
        if hp is not None:
            try:
                hp.cfg = self.cfg
                hp.refresh()
            except Exception as err:                       # noqa: BLE001
                log.warning("主页最近区刷新失败：%s", err)
        self.on_status("已清空最近数据目录")

    def _on_analysis_preset_applied(self, new_cfg: dict) -> None:
        """V2.4-A（FR-26.4）：分析模板套用 → 统一广播 cfg（单一状态源）。

        广播后 Φ_Δ 页的 ``on_cfg_changed`` 会刷新 FR-25.1 过期横幅——数值节变了
        就提示"需要重算"，但**不自动重跑**（与显式动作哲学一致）。
        """
        self._apply_cfg_everywhere(new_cfg)
        try:
            self._save_cfg()
        except Exception as err:                       # noqa: BLE001
            log.warning("模板套用后保存配置失败：%s", err)
        self.on_logged("INFO", "分析模板已套用并写入 config.yaml"
                              "（如需重算请按 F5）")

    def _on_batch_changed(self, scans) -> None:
        """数据页载入新批次 → 灌给分析页/作图页，并更新导航徽标（UX-3）。"""
        n = len(scans or [])
        good = sum(1 for s in (scans or []) if not s.excluded)
        badge = f"{n} 个文件" + (f" / {n - good} 剔除" if n - good else "")
        self.nav.set_badge("data", badge)
        self.nav.set_badge("analysis", badge)
        d = getattr(self.data_page, "data_dir", None)
        self.nav.set_context(f"当前批次：{Path(d).name if d else '未选择'}\n{badge}")
        # 徽标刷新会重设导航项 tooltip → 补回 Alt 快捷键标注（FR-40.3）
        self._apply_nav_shortcut_tips()
        # FR-24.9：把①数据页的整定（染料名/时间）随批次单向同步给 ②分析
        overrides = None
        try:
            overrides = self.data_page.session_overrides()
        except Exception as err:                           # noqa: BLE001
            log.warning("读取数据页整定失败：%s", err)
        try:
            self.analysis_page.set_batch(scans, d, overrides=overrides)
        except Exception as err:                           # noqa: BLE001
            log.warning("分析页接批次失败：%s", err)
        try:
            self.plot_page.set_batch(
                scans, d, overrides=overrides,
                recursive=bool(self.data_page.chk_recursive.isChecked()))
        except Exception as err:                           # noqa: BLE001
            log.warning("作图页接批次失败：%s", err)

    def _on_page_run_finished(self, rr) -> None:
        """分析页里的任务跑完 → 更新日志/状态（图件与结果由各页自己展示）。

        A16（V2.x 审查）：原文案恒为"分析完成"，但这里也会收到 ε 计算 / A@λ 表 /
        导出这类**非分析**任务的完成信号 → 日志里出现"分析完成：ok=False"噪声。
        改为中性措辞 + 带上任务类型（能从结果对象判断时）。
        """
        ok = bool(getattr(rr, "ok", False))
        kind = type(rr).__name__ if rr is not None else "任务"
        msg = getattr(rr, "message", "") if rr is not None else ""
        self.on_logged("INFO" if ok else "WARNING",
                       f"{kind} 结束：ok={ok}" + (f"；{msg}" if msg else ""))

    def _nav_hint_if_old_config(self) -> None:
        """旧配置（无 ``gui.nav`` 键）→ 首启一次性提示"可切回经典布局"（Q7）。

        **非模态**：只写状态栏 + 日志。A16 要求主路径零模态阻塞，
        这条提示不该成为"新用户被弹窗拦一下"的理由。

        V2.3 收紧（评审记录 F6 的三个脆弱点）：
        ① 判据从**裸子串** ``"nav:"`` 改成 YAML 解析后的 ``gui.nav`` 真值
           （原写法会把用户注释里出现的 "nav:" 误判成"已有键"而吞掉提示）；
        ② 读失败/写失败的 ``except: pass`` 改成**记日志**（ui 层"失败不静默吞"家规）；
        ③ 写盘走 ``save_config`` 的单点护栏（F3），冒烟模式下自动跳过。
        """
        p = self.config_path()
        if p.exists():
            try:
                import yaml
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                gui = raw.get("gui") if isinstance(raw, dict) else None
                has_key = isinstance(gui, dict) and ("nav" in gui)
            except Exception as err:                   # noqa: BLE001
                log.warning("读取 config.yaml 判断 gui.nav 失败（按已有键处理，"
                            "本次不提示）：%s", err)
                return
            if has_key:
                return
        self.cfg.setdefault("gui", {})["nav"] = "flow"
        try:
            from core.config import save_config
            save_config(self.cfg, p)               # 护栏在 save_config 内（冒烟跳过）
        except Exception as err:                   # noqa: BLE001
            log.warning("首启写出 gui.nav 失败（不影响本次使用）：%s", err)
        self.statusBar().showMessage(
            "已启用任务流布局；想回到旧的平铺页签：「视图」→「经典页签布局」",
            12000)
        self.on_logged("INFO", "已启用任务流布局（gui.nav=flow）；"
                               "「视图」菜单可切回经典页签布局")

    def _on_plot_params_written(self, values: dict) -> None:
        """V2.5 FR-33.3：工作台写入出图参数 → 单一 cfg 广播 + Φ_Δ 过期横幅。

        values 是已写进 cfg 的可映射键（页面负责写 + save_config）；这里只做
        "广播给所有页面/面板"与"让 Φ_Δ 页按 FR-25.1 重算过期分区"两件事。
        """
        try:
            self._apply_cfg_everywhere(self.cfg)
            hook = getattr(self.phi_page, "on_cfg_changed", None)
            if callable(hook):
                hook()                                   # plot 节变更 → "重渲受影响图"
            self.on_status("出图参数已广播；Φ_Δ 页已提示重渲受影响图")
        except Exception as err:                           # noqa: BLE001
            log.warning("出图参数广播失败：%s", err)

    def _apply_cfg_everywhere(self, new_cfg: dict,
                              *, update_panels: bool = True) -> dict:
        """把一份新 cfg 同步到**所有**页面与面板（A20/B1 的单一来源）。

        A20（V2.x 审查）：多处 ``self.cfg = ...`` 只同步了 ``phi_page``；
        Plot/Data/Settings 仍持旧 dict → 同一会话里"设置页改了、分析页没变"。
        这里一次性广播，以后任何"换 cfg"都走这一个入口。
        """
        self.cfg = new_cfg
        for page in list(getattr(self, "pages", [])) + [
                getattr(self, k, None) for k in
                ("data_page", "analysis_page", "plot_page", "archive_page",
                 "settings_page", "home_page")]:
            if page is None:
                continue
            if hasattr(page, "cfg"):
                try:
                    page.cfg = new_cfg
                except Exception:                      # noqa: BLE001
                    pass
        for page in list(getattr(self, "pages", [])) + [
                getattr(self, k, None) for k in ("data_page", "settings_page")]:
            hook = getattr(page, "on_cfg_changed", None)
            if callable(hook):
                try:
                    hook()
                except Exception as err:               # noqa: BLE001
                    log.warning("页面刷新 cfg 失败（%s）：%s",
                                type(page).__name__, err)
        if update_panels:
            for panel_owner in (self.phi_page,
                                getattr(self, "settings_page", None)):
                if panel_owner is None:
                    continue
                panel = getattr(panel_owner, "params", None)
                set_cfg = getattr(panel, "set_cfg", None)
                if callable(set_cfg):
                    try:
                        set_cfg(new_cfg)
                    except Exception as err:           # noqa: BLE001
                        log.warning("参数面板同步失败：%s", err)
        return new_cfg

    def _sync_settings_page(self, entering: bool) -> None:
        """⑤设置 与 Φ_Δ 的参数面板**共享一份 cfg**（B1，V2.x 审查）。

        两个 ParamPanel 必然并存（flow 布局的 ⑤设置 与 ②分析 的 Φ_Δ 页各自要显示
        参数），所以不复制控件、而是把"状态"统一到 ``self.cfg``：

        * **进入** ⑤设置：把面板刷新成当前 cfg（不会覆盖用户刚在别处改的值）；
        * **离开** ⑤设置：把面板取值写回 cfg，并同步给 Φ_Δ 的面板与全部页面
          —— 否则关窗只保存 Φ_Δ 那份，设置页的编辑**静默丢失**。
        """
        sp = getattr(self, "settings_page", None)
        if sp is None:
            return
        panel = getattr(sp, "params", None)
        if panel is None:
            return
        if entering:
            set_cfg = getattr(panel, "set_cfg", None)
            if callable(set_cfg):
                try:
                    set_cfg(self.cfg)
                except Exception as err:               # noqa: BLE001
                    log.warning("设置页面板刷新失败：%s", err)
            return
        apply_to = getattr(panel, "apply_to", None)
        if not callable(apply_to):
            return
        try:
            new_cfg = apply_to(self.cfg)
        except Exception as err:                       # noqa: BLE001
            log.warning("设置页面板取值失败（保留原 cfg）：%s", err)
            return
        self._apply_cfg_everywhere(new_cfg)

    def _apply_run_params(self, params: dict, overrides: dict) -> None:
        """「载入最新 run 参数」：套用历史参数 + 时间整定（FR-11.6 / FR-12.5）。"""
        if not isinstance(params, dict) or not params:
            self.on_status("该 run 没有参数快照")
            return
        merged = dict(self.cfg)
        try:
            from core.config import deep_merge
            deep_merge(merged, params)
        except Exception as err:                           # noqa: BLE001
            QMessageBox.warning(self, "载入失败", f"参数快照无法解析：{err}")
            return
        self._apply_cfg_everywhere(merged)
        n_t = 0
        if overrides:
            self.phi_page._time_overrides = dict(overrides)
            n_t = len(overrides)
            scans = list(getattr(self.phi_page, "scans", []) or [])
            if scans:
                try:
                    from core.io import apply_time_overrides
                    apply_time_overrides(scans, overrides)
                    self.phi_page.files.set_scans(
                        scans, dye_of=self.phi_page.dye_panel.dye_map())
                    self.phi_page.dye_panel.set_scans(
                        scans, dye_of=self.phi_page.dye_panel.dye_map())
                except Exception:                          # noqa: BLE001
                    pass
        self._show_phi()
        self.on_logged("INFO", f"已载入历史 run 参数（时间整定 {n_t} 项）；"
                               "点「开始分析」即可复现该次实验条件")
