# -*- coding: utf-8 -*-
"""simulation_lab(行动仿真沙盒与后果预测器)测试。

重点验证两件事:
  1. 编排正确: simulate/risk_score/impact_predict/shadow_execute/dry_run/approval_packet/...
     把活儿交给底层只读/预演面(swarm.merge._classify_step / world_model / computer_use.plan /
     self_improve.confirmed=False / autopilot_store.enqueue_approval), 参数正确; 强动作只入审批队列。
  2. 护栏不被削弱:
     - 门面自身绝不直接 subprocess/os.system/os.remove/exec/__import__(静态扫描断言)。
     - 绝不对任何引擎自传 confirmed=True(静态扫描 + 行为断言: computer_use 走 plan, self_improve 走 confirmed=False)。
     - 仿真层零真实副作用: executed_for_real 恒 False; 影子执行真实目标不碰(real_target_touched False)。
     - 红线分级转发 swarm.merge._classify_step(单一真相源), 不复制红线表; 红线 → band=critical。
     - 命中隐私(world_model _private / file_tools private=1)的输出带 _private; 隐私库不可达按私密处理。
     - LLM 工具面只读为主且冻结(is_tool/approved_tools = simulate/risk_score/status), 强动作 CLI-only。
     - simulation_lab 已纳入 evolve_daemon._GUARDED_PATHS×2 + self_healer 保护 +
       mission_control 禁执行 import + config._READONLY_KEYS。

用假引擎隔离, 数据目录指向 tmp, 不碰真引擎/真磁盘/真 config/真审批库。
跑: cd brain && python3 -m pytest test_simulation_lab.py -v
"""
import os
import re
import sys
import tokenize

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import simulation_lab as sl

def _read_sibling(name: str) -> str:
    p = os.path.join(_HERE, name)
    if not os.path.exists(p):
        pytest.skip(f"{name} not bundled in standalone jarvis-core")
    return open(p, encoding="utf-8").read()

_SL_PATH = os.path.join(_HERE, "simulation_lab.py")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """数据目录指向 tmp(autouse: 每个测试干净, 永不碰真 ~/.jarvis)。"""
    monkeypatch.setattr(sl, "_data_dir", lambda: str(tmp_path / "sl"))
    yield


# --------------------------------------------------------------------------- #
# 假引擎
# --------------------------------------------------------------------------- #
class FakeMerge:
    """复用真实红线分级语义的极简假体(只读判定)。"""
    def _extract_steps(self, text):
        return [ln[5:].strip() for ln in (text or "").splitlines()
                if ln.strip().upper().startswith("STEP:")]

    def _classify_step(self, action):
        low = action or ""
        if "rm " in low or "rm -rf" in low or "DROP TABLE" in low or "格式化" in low:
            return {"kind": "red_line", "gated": True, "why": "命中红线"}
        if any(h in low for h in ("写入", "修改文件", "部署", "发送", "移动", "覆盖", "删除")):
            return {"kind": "mutating", "gated": True, "why": "疑似副作用"}
        if any(h in low for h in ("只读", "读取", "调研", "查看", "查询")):
            return {"kind": "read_only", "gated": False, "why": ""}
        return {"kind": "mutating", "gated": True, "why": "保守入审批"}


class FakeWM:
    """假 world_model: 只读后果/影响/预测/偏好; private 标志可控。"""
    def __init__(self, private=False):
        self._private = private

    def context_for(self, consumer="", topic="", **kw):
        out = {"ok": True, "consumer": consumer, "topic": topic,
               "entities": [{"name": "下载文件夹"}],
               "impact": {"affects": [{"name": "归档目录"}, {"name": "索引"}],
                          "depends_on": [{"name": "磁盘空间"}]},
               "prediction": {"predictions": [
                   {"predicted": "归档完成", "confidence": 0.8, "kind": "causal"},
                   {"predicted": "可能漏文件", "confidence": 0.3, "kind": "recurrence"}]}}
        if self._private:
            out["_private"] = True
        return out

    def preference_query(self, aspect=""):
        out = {"ok": True, "aspect": aspect, "count": 1,
               "preferences": [{"preference": "喜欢按月归档", "private": self._private}],
               "recent_corrections": []}
        if self._private:
            out["_private"] = True
        return out

    def impact(self, target="", **kw):
        out = {"ok": True, "found": True, "target": target,
               "affects": [{"name": "归档目录"}], "depends_on": [{"name": "磁盘空间"}],
               "goals_touched": [], "constraints_touched": [], "confidence": 0.7}
        if self._private:
            out["_private"] = True
        return out

    def predict(self, target="", **kw):
        return {"ok": True, "found": True, "target": target,
                "predictions": [{"predicted": "x", "confidence": 0.6, "kind": "causal"}]}

    def causal_chain(self, *a, **kw):
        # 真实 world_model.causal_chain 的返回键是 "chains"(非 chain/links)
        return {"ok": True, "chains": [{"path": "下载文件夹 → 归档"}], "count": 1}


class FakeApStore:
    """假 autopilot_store: 记录 enqueue_approval, 断言 risk/payload; 绝不执行任何东西。"""
    def __init__(self):
        self.enqueued = []

    def enqueue_approval(self, action, action_kind, payload, goal_id=None, risk="high", why=""):
        self._next = getattr(self, "_next", 5100) + 1
        self.enqueued.append({"id": self._next, "action": action, "action_kind": action_kind,
                              "payload": payload, "risk": risk, "why": why, "status": "pending"})
        return self._next

    def list_approvals(self, status="pending", limit=50):
        return [dict(e) for e in self.enqueued if e.get("status") == status]

    def add_goal(self, title, detail="", priority=3):
        return 777


class FakeComputerUse:
    """假 computer_use: 记录 do() 调用, 断言只走 plan=True / 绝不 confirmed=True。"""
    def __init__(self):
        self.calls = []

    def do(self, task, confirmed=False, max_steps=6, plan=False):
        self.calls.append({"task": task, "confirmed": confirmed, "plan": plan})
        assert confirmed is not True, "simulation_lab 绝不能给 computer_use 传 confirmed=True"
        assert plan is True, "rehearse/dry_run 必须 plan=True(只看不点)"
        return {"ok": True, "done": False, "task": task, "plan": True,
                "steps": [{"action": "click", "target": "button", "reason": "x", "planned": True}],
                "stopped_reason": "done"}


class FakeSelfImprove:
    """假 self_improve: 记录 improve() 调用, 断言绝不 confirmed=True。"""
    def __init__(self):
        self.calls = []

    def improve(self, task, confirmed=False):
        self.calls.append({"task": task, "confirmed": confirmed})
        assert confirmed is not True, "simulation_lab 绝不能给 self_improve 传 confirmed=True"
        return {"ok": False, "needs_confirm": True, "task": task,
                "message": "改我自己的代码要 User 确认"}


class FakeFileTools:
    """假 file_tools: 隐私判定可控。DB_PATH 默认指向存在的文件(隐私库可达)。"""
    def __init__(self, private_paths=(), db_path=None):
        self.private_paths = set(private_paths)
        # 默认 DB 可达(指向本测试文件这种必定存在的路径); 传 db_path 可模拟 DB 不可达
        self.DB_PATH = db_path if db_path is not None else __file__

    def is_private_path(self, path):
        return path in self.private_paths

    def command_touches_private(self, command):
        return any(p in (command or "") for p in self.private_paths)


def _mk_opt(mapping):
    return lambda name: mapping.get(name)


def _force_data_layer_privacy_on(monkeypatch):
    """总开关 safety.data_layer_privacy_enabled 默认 False(2026-07-08 起数据层隐私放弃,
    见 simulation_lab._path_privacy/_command_touches_private 文档字符串)。测试内强制打开此开关,
    断言老 fail-safe/脱敏行为完整保留(可逆, 未被删除)。"""
    import config
    _orig_get = config.get

    def _on(key, fallback=None):
        return True if key == "safety.data_layer_privacy_enabled" else _orig_get(key, fallback)

    monkeypatch.setattr(config, "get", _on)


@pytest.fixture
def engines(monkeypatch):
    """注入假分级引擎 + 假集成模块(world_model/autopilot_store/computer_use/self_improve/file_tools)。"""
    wm = FakeWM()
    ap = FakeApStore()
    cu = FakeComputerUse()
    si = FakeSelfImprove()
    ft = FakeFileTools()
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    mapping = {"world_model": wm, "autopilot_store": ap, "computer_use": cu,
               "self_improve": si, "file_tools": ft}
    monkeypatch.setattr(sl, "_opt", _mk_opt(mapping))
    return {"wm": wm, "ap": ap, "cu": cu, "si": si, "ft": ft, "mapping": mapping}


# --------------------------------------------------------------------------- #
# 编排正确
# --------------------------------------------------------------------------- #
def test_simulate_classifies_and_forecasts(engines):
    plan = ("整理下载\nSTEP: 查看 ~/Downloads(只读)\n"
            "STEP: 移动 PDF 到归档\nSTEP: rm -rf /tmp/dup")
    r = sl.simulate(plan)
    assert r["ok"] is True
    fc = r["forecast"]
    assert fc["by_kind"] == {"read_only": 1, "mutating": 1, "red_line": 1}
    # 任一红线 → band=critical
    assert fc["risk_band"] == "critical"
    assert r.get("_private") is not True  # FakeWM 默认非私密(非私密时不带 _private 键)
    # 仿真恒不真执行
    sim = sl._load_sim(r["sim_id"])
    assert sim["executed_for_real"] is False
    # 失败点含红线提示
    assert any("红线" in fp for fp in fc["failure_points"])


def test_risk_score_redline_is_critical(engines):
    r = sl.risk_score("STEP: rm -rf /Users/x/业务")
    assert r["ok"] is True
    assert r["risk_band"] == "critical"
    assert r["breakdown"]["red_line"] >= 1


def test_risk_score_readonly_is_low(engines):
    r = sl.risk_score("查看天气")
    assert r["risk_band"] == "low"
    assert r["breakdown"]["red_line"] == 0


def test_simulate_registers_goal_via_goal_engine(engines):
    r = sl.simulate("STEP: 查看现状(只读)", register_goal=True)
    assert r.get("goal_id") == 777  # FakeApStore.add_goal


# --------------------------------------------------------------------------- #
# 护栏: 永不真执行 / 永不自放行 / 强动作只入审批队列
# --------------------------------------------------------------------------- #
def test_approval_packet_enqueues_never_executes(engines):
    sim = sl.simulate("STEP: 写入配置文件")
    r = sl.approval_packet(sim["sim_id"])
    assert r["ok"] is True
    ap = engines["ap"]
    assert len(ap.enqueued) == 1
    enq = ap.enqueued[0]
    # 仿真落地恒 risk≥high
    assert enq["risk"] in ("high", "critical")
    # payload 不含 confirmed=True, 携带 sim_id 供 audit 关联
    assert enq["payload"]["sim_id"] == sim["sim_id"]
    assert enq["payload"].get("confirmed") is not True
    assert r["needs_confirm"] is True
    # sim 记录回填 approval_id, 仍未真执行
    saved = sl._load_sim(sim["sim_id"])
    assert saved["approval_id"] == enq["id"]
    assert saved["executed_for_real"] is False


def test_approval_packet_low_risk_still_at_least_high(engines):
    sim = sl.simulate("STEP: 查看天气(只读)")  # 低风险只读
    r = sl.approval_packet(sim["sim_id"])
    assert r["risk"] in ("high", "critical")  # 落地永不低于 high


def test_shadow_execute_never_touches_real_target(engines, tmp_path):
    target = tmp_path / "real.txt"
    target.write_text("原内容\nA\nB\n", encoding="utf-8")
    newc = tmp_path / "new.txt"
    newc.write_text("原内容\nA\nC\n", encoding="utf-8")
    sim = sl.simulate("STEP: 覆盖 real.txt", params={"file": str(target), "content_file": str(newc)})
    r = sl.shadow_execute(sim["sim_id"])
    assert r["ok"] is True
    assert r["real_target_touched"] is False
    # 真实目标内容**完全没变**
    assert target.read_text(encoding="utf-8") == "原内容\nA\nB\n"
    # 沙盒里产出 diff
    d = r["diffs"][0]
    assert d["sandboxed"] is True
    assert d["added"] >= 1 and d["removed"] >= 1
    # 沙盒目录在隔离数据目录下, 不在真实目标旁
    assert str(tmp_path / "sl") in r["sandbox_dir"]


def test_shadow_execute_private_file_redacted(monkeypatch, tmp_path):
    """数据层隐私总开关默认 False(2026-07-08 起放弃, 小美可读全部数据): 默认→原样展示正文,
    不脱敏。开关打开→老脱敏行为(_private/redacted, 绝不回正文 diff)完整保留(可逆)。"""
    secret = tmp_path / "bank.txt"
    secret.write_text("工资 99999 银行卡 6222", encoding="utf-8")
    ft = FakeFileTools(private_paths={str(secret)})
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    monkeypatch.setattr(sl, "_opt", _mk_opt({"file_tools": ft}))

    # 新默认(开关关闭): 恒原样, 不查 file_tools 私密标记, 不脱敏
    sim = sl.simulate("STEP: 覆盖 bank.txt", params={"file": str(secret)})
    r = sl.shadow_execute(sim["sim_id"])
    d = r["diffs"][0]
    assert d.get("_private") is not True and d.get("redacted") is not True
    assert d.get("sandboxed_orig") is True  # 正常读原文件落沙盒(未被隐私门拦下)
    assert r.get("_private") is not True

    # 开关打开: 老脱敏行为完整保留(可逆)
    _force_data_layer_privacy_on(monkeypatch)
    sim2 = sl.simulate("STEP: 覆盖 bank.txt(开关开)", params={"file": str(secret)})
    r2 = sl.shadow_execute(sim2["sim_id"])
    d2 = r2["diffs"][0]
    assert d2.get("_private") is True and d2.get("redacted") is True
    # 私密文件: 绝不回正文 diff
    assert "diff" not in d2
    assert r2.get("_private") is True


def test_privacy_failsafe_when_file_tools_missing(monkeypatch):
    """file_tools 不可达 → 隐私状态未知。数据层隐私总开关默认 False(2026-07-08 起放弃):
    默认→恒不私密(不查 file_tools, 小美可读全部数据); 开关打开→老 fail-safe(file_tools 不可达
    一律按私密处理)完整保留(可逆)。"""
    monkeypatch.setattr(sl, "_opt", lambda name: None)  # 所有可选模块都缺

    p = sl._path_privacy("/some/path.txt")
    assert p["private"] is False
    assert sl._command_touches_private("cat /some/path.txt") is False

    _force_data_layer_privacy_on(monkeypatch)
    p2 = sl._path_privacy("/some/path.txt")
    assert p2["private"] is True
    assert sl._command_touches_private("cat /some/path.txt") is True


def test_privacy_failsafe_when_index_db_unreachable(monkeypatch):
    """评审 #1(HIGH): file_tools 在但 index.db 不可达。数据层隐私总开关默认 False(2026-07-08
    起放弃): 默认→恒不私密(不探测 DB 可达性); 开关打开→老 fail-safe(不能 fail-open, 一律按
    私密)完整保留(可逆)。"""
    ft = FakeFileTools(db_path="/tmp/__nope_index_does_not_exist__.db")
    monkeypatch.setattr(sl, "_opt", _mk_opt({"file_tools": ft}))

    p = sl._path_privacy("/etc/hosts")  # 非私密路径, 但 DB 不可达
    assert p["private"] is False

    _force_data_layer_privacy_on(monkeypatch)
    p2 = sl._path_privacy("/etc/hosts")
    assert p2["private"] is True
    assert "不可达" in p2["reason"]


def test_secret_credential_file_hard_blocked(monkeypatch, tmp_path):
    """评审 #5(CRITICAL): 密钥/凭据文件内容无关硬拦, 绝不读正文(即便 index.db 没标记)。"""
    creds = tmp_path / ".gmail_creds.json"
    creds.write_text('{"token":"SUPERSECRET"}', encoding="utf-8")
    # file_tools 在且 DB 可达, 但 creds 未被 index.db 标记为 private(private_paths 为空)
    ft = FakeFileTools(private_paths=())
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    monkeypatch.setattr(sl, "_opt", _mk_opt({"file_tools": ft}))
    assert sl._is_secret_path(str(creds)) is True
    assert sl._path_privacy(str(creds))["private"] is True
    sim = sl.simulate("STEP: 覆盖密钥", params={"file": str(creds)})
    r = sl.shadow_execute(sim["sim_id"])
    d = r["diffs"][0]
    assert d.get("_private") is True and d.get("redacted") is True
    assert "diff" not in d  # 绝不回密钥正文
    # 沙盒里也绝不落密钥正文
    assert "SUPERSECRET" not in (d.get("reason", "") + str(d.get("diff", "")))


def test_rehearse_ui_uses_plan_mode(engines):
    r = sl.rehearse_ui("打开 Safari 搜索天气")
    assert r["ok"] is True
    cu = engines["cu"]
    assert len(cu.calls) == 1
    assert cu.calls[0]["plan"] is True
    assert cu.calls[0]["confirmed"] is not True


def test_rehearse_ui_propagates_private(monkeypatch):
    """评审 #2(MED): rehearse_ui 对已标私密的 sim 必须传 _private(切本机模型)。"""
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    monkeypatch.setattr(sl, "_opt", _mk_opt({"world_model": FakeWM(private=True),
                                             "computer_use": FakeComputerUse()}))
    sim = sl.simulate("STEP: 打开私密 App 界面(只读)")
    assert sim.get("_private") is True
    r = sl.rehearse_ui(sim["sim_id"])
    assert r.get("_private") is True


def test_impact_predict_causal_chain_non_empty(engines):
    """评审 #3/#6(MED): impact_predict 读真实键 'chains', 因果链不再恒空。"""
    r = sl.impact_predict("下载文件夹")
    assert r["ok"] is True
    assert r["causal_chain"]  # 非空(读 chains 键)


def test_dry_run_connector_forge_readonly(engines):
    """评审 #4(LOW): 对外步骤 dry_run 经 connector_forge 只读预演(nexus.status), 绝不 send/draft。"""
    class FakeNexus:
        def status(self):
            return {"ok": True, "services": ["gmail", "feishu"]}

    # engines._opt 闭包持有同一 mapping 字典引用, 这里追加即可被 _opt 看到
    engines["mapping"]["nexus"] = FakeNexus()
    sim = sl.simulate("STEP: 发邮件给客户")
    r = sl.dry_run(sim["sim_id"])
    pv = r["previews"][0]
    assert pv["engine"].startswith("connector_forge.nexus")
    assert pv["live"] is False


def test_what_if_change_without_equals_errors(engines):
    """评审 #8(LOW): --change 无 '=' 不再静默走 assume 分支, 报错提示。"""
    sim = sl.simulate("STEP: 查看现状(只读)")
    r = sl.what_if(sim["sim_id"], change="随便改改")
    assert r["ok"] is False
    assert "k=v" in r["error"]


def test_dry_run_self_improve_confirmed_false(engines):
    sim = sl.simulate("STEP: 改代码 重构 brain 模块")
    r = sl.dry_run(sim["sim_id"])
    assert r["ok"] is True
    si = engines["si"]
    assert len(si.calls) == 1
    assert si.calls[0]["confirmed"] is False


def test_dry_run_does_not_execute_mutating(engines):
    """dry_run 对纯副作用步骤只预测, 不碰任何执行引擎。"""
    sim = sl.simulate("STEP: 写入配置文件")
    r = sl.dry_run(sim["sim_id"])
    pv = r["previews"][0]
    assert pv["engine"] == "predict-only"
    assert pv["ok"] is True


# --------------------------------------------------------------------------- #
# 隐私 _private 传播
# --------------------------------------------------------------------------- #
def test_private_world_model_propagates(monkeypatch):
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    monkeypatch.setattr(sl, "_opt", _mk_opt({"world_model": FakeWM(private=True)}))
    r = sl.simulate("STEP: 查看私密项目(只读)")
    assert r.get("_private") is True


# --------------------------------------------------------------------------- #
# 反事实 / 对比 / 回滚 / 审计 — 全只读, 不执行
# --------------------------------------------------------------------------- #
def test_what_if_counterfactual_dry(engines):
    sim = sl.simulate("STEP: 移动文件\nSTEP: 写入索引")
    r = sl.what_if(sim["sim_id"], change="step1=rm -rf /tmp/x")
    assert r["ok"] is True
    assert r["base_sim"] == sim["sim_id"]
    assert r["counterfactual_sim"] != sim["sim_id"]
    # 把一步换成红线 → 风险升高
    assert r["after"]["risk_band"] == "critical"
    cf = sl._load_sim(r["counterfactual_sim"])
    assert cf["executed_for_real"] is False


def test_compare_plans_recommends_lower_risk(engines):
    safe = sl.simulate("STEP: 查看现状(只读)")
    risky = sl.simulate("STEP: rm -rf /tmp/a\nSTEP: 部署到生产")
    r = sl.compare_plans([safe["sim_id"], risky["sim_id"]])
    assert r["ok"] is True
    assert r["recommended"] == safe["sim_id"]


def test_rollback_plan_no_execution(engines):
    sim = sl.simulate("STEP: 写入文件\nSTEP: 发送邮件给客户")
    r = sl.rollback_plan(sim["sim_id"])
    assert r["ok"] is True
    # 对外动作标记不可自动回滚
    assert r["auto_revertible"] is False
    saved = sl._load_sim(sim["sim_id"])
    assert saved["executed_for_real"] is False


def test_audit_invariant_executed_for_real_false(engines):
    sim = sl.simulate("STEP: 写入文件\nSTEP: rm -rf /tmp/x")
    r = sl.audit(sim["sim_id"])
    assert r["ok"] is True
    assert r["invariants_ok"] is True
    assert r["safety_invariants"]["executed_for_real"] is False
    assert r["safety_invariants"]["ever_self_approved"] is False
    assert r["by_kind"]["red_line"] == 1


# --------------------------------------------------------------------------- #
# 引擎缺失优雅降级
# --------------------------------------------------------------------------- #
def test_classify_degrades_conservative_when_engine_missing(monkeypatch):
    monkeypatch.setattr(sl, "_merge_mod", None)
    monkeypatch.setattr(sl, "_opt", lambda name: None)
    r = sl.simulate("做点什么")
    assert r["ok"] is True
    # 引擎缺失 → 保守 gated(mutating), 不会误判只读
    assert r["forecast"]["by_kind"]["read_only"] == 0


# --------------------------------------------------------------------------- #
# LLM 工具面只读且冻结
# --------------------------------------------------------------------------- #
def test_llm_surface_is_frozen_readonly():
    assert sl.is_tool("simulation_lab_simulate")
    assert sl.is_tool("simulation_lab_risk_score")
    assert sl.is_tool("simulation_lab_status")
    # 强动作绝不在 LLM 工具面
    for strong in ("simulation_lab_shadow_execute", "simulation_lab_approval_packet",
                   "simulation_lab_dry_run", "simulation_lab_rehearse_ui"):
        assert not sl.is_tool(strong)
    names = {t["function"]["name"] for t in sl.approved_tools()}
    assert names == {"simulation_lab_simulate", "simulation_lab_risk_score", "simulation_lab_status"}


def test_dispatch_only_readonly(engines):
    r = sl.dispatch("simulation_lab_risk_score", {"target": "查看天气"})
    assert r["ok"] is True
    # 未知/强动作工具不被 dispatch
    bad = sl.dispatch("simulation_lab_shadow_execute", {"sim_id": "x"})
    assert bad["ok"] is False and "unknown" in bad["error"]


# --------------------------------------------------------------------------- #
# 护栏: 纯门面静态扫描(去注释/字符串后扫代码 token 流)
# --------------------------------------------------------------------------- #
def _code_only(path):
    skip = {tokenize.COMMENT, tokenize.STRING, tokenize.NL, tokenize.NEWLINE,
            tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            skip.add(getattr(tokenize, name))
    toks = []
    with open(path, "rb") as f:
        for t in tokenize.tokenize(f.readline):
            if t.type not in skip:
                toks.append(t.string)
    return " ".join(toks)


def test_module_never_executes_directly():
    code = _code_only(_SL_PATH)
    forbidden = [r"\bsubprocess\b", r"\bos\s*\.\s*system\b", r"\bos\s*\.\s*popen\b",
                 r"\bos\s*\.\s*remove\b", r"\bos\s*\.\s*unlink\b",
                 r"\bshutil\s*\.\s*rmtree\b", r"\beval\b", r"\bexec\b",
                 r"\bcompile\b", r"\b__import__\b"]
    hits = [p for p in forbidden if re.search(p, code)]
    assert not hits, f"simulation_lab 代码出现禁忌执行原语: {hits}"


def test_module_never_self_confirms():
    """simulation_lab 绝不对任何引擎自传 confirmed=True(执行授权只能 User approve)。"""
    code = _code_only(_SL_PATH)
    assert not re.search(r"confirmed\s*=\s*True", code), "simulation_lab 出现 confirmed=True 自放行"


def test_module_never_calls_send_or_approve():
    """绝不触对外真发/审批批准面(nexus_send/nexus_approve/appsmith.approve)。"""
    code = _code_only(_SL_PATH)
    for forbidden in ("nexus_send", "nexus_approve", "nexus_reject"):
        assert forbidden not in code, f"simulation_lab 触碰禁用对外发送面: {forbidden}"


def test_module_does_not_touch_credentials():
    code = _code_only(_SL_PATH)
    assert "gmail_creds" not in code and "gcal_creds" not in code


# --------------------------------------------------------------------------- #
# 护栏: 自进化/自愈/规划引擎保护 simulation_lab
# --------------------------------------------------------------------------- #
def test_evolve_daemon_guards_simulation_lab_path():
    src = _read_sibling("evolve_daemon.py")
    assert src.count('"brain/simulation_lab.py"') >= 2, \
        "evolve_daemon._GUARDED_PATHS 未在两个 _guard_diff 函数都覆盖 brain/simulation_lab.py"


def _same_repo(mod) -> bool:
    """模块是否就是本 worktree 的副本(dual-repo 环境下 import 可能解析到规范库副本)。"""
    return os.path.dirname(os.path.abspath(getattr(mod, "__file__", ""))) == _HERE


def test_self_healer_protects_simulation_lab():
    # 源文件级断言(确定性: 验证本 worktree 交付的文件确含注册)
    src = _read_sibling("self_healer.py")
    assert '"simulation_lab.py"' in src, "self_healer._PROTECTED_TARGETS 未覆盖 simulation_lab.py"
    # 运行时功能断言(仅当 import 解析到同一 worktree 副本时, 避免 dual-repo 误判)
    import self_healer as sh
    if _same_repo(sh):
        assert sh._is_protected_target("brain/simulation_lab.py") is True


def test_mission_control_forbids_simulation_lab_exec_import():
    src = _read_sibling("mission_control.py")
    assert '"simulation_lab"' in src, "mission_control._FORBIDDEN_EXEC_MODULES 未覆盖 simulation_lab"
    import mission_control as mc
    if _same_repo(mc):
        assert "simulation_lab" in mc._FORBIDDEN_EXEC_MODULES


def test_config_locks_simulation_lab_tool_keys():
    """simulation_lab.enabled/expose_tools 是 User-only(与 agent_swarm 对称), 模型 config_set 改不动。"""
    src = _read_sibling("config.py")
    assert '"simulation_lab.enabled"' in src and '"simulation_lab.expose_tools"' in src
    import config
    if _same_repo(config):
        assert "simulation_lab.enabled" in config._READONLY_KEYS
        assert "simulation_lab.expose_tools" in config._READONLY_KEYS
        with pytest.raises(PermissionError):
            config.set("simulation_lab.enabled", False)


# =========================================================================== #
# 多情景反事实未来沙盘(scenario sandbox)
# =========================================================================== #
class FakeWMScn:
    """假 world_model(场景版): impact 随 target 变, 撑得起二阶波及 + 低置信预测 + 机制链。"""
    def __init__(self, private=False):
        self._p = private

    def _flag(self, d):
        if self._p:
            d["_private"] = True
        return d

    def causal_chain(self, *a, **k):
        return self._flag({"ok": True, "chains": [
            {"steps": [{"mechanism": "下载→归档", "confidence": 0.7, "via": "causes"}],
             "confidence": 0.7}]})

    def impact(self, target="", **k):
        if target == "归档目录":
            return self._flag({"ok": True, "affects": [{"name": "索引"}], "depends_on": [],
                               "goals_touched": [], "constraints_touched": [], "confidence": 0.6})
        return self._flag({"ok": True,
                           "affects": [{"name": "归档目录"}, {"name": "下载文件夹"}],
                           "depends_on": [{"name": "磁盘空间"}], "goals_touched": ["季度整理"],
                           "constraints_touched": [], "confidence": 0.7})

    def predict(self, target="", **k):
        return self._flag({"ok": True, "predictions": [
            {"predicted": "归档完成", "confidence": 0.8, "kind": "causal"},
            {"predicted": "可能漏文件", "confidence": 0.3, "kind": "recurrence"}]})

    def context_for(self, consumer="", topic="", **k):
        return self._flag({"ok": True, "consumer": consumer, "topic": topic})

    def preference_query(self, aspect=""):
        return self._flag({"ok": True, "count": 1,
                           "preferences": [{"preference": "按月归档"}], "recent_corrections": []})


class FakeMC:
    def list_goals(self, status="active", **k):
        return [{"id": 1, "title": "清理下载文件夹", "priority": 2, "status": "active",
                 "detail": "敏感细节不该带出"}]


class FakeSC:
    """假 shared_context: capture 只回元数据(counts/focus/_private), 绝不回条目正文。"""
    def __init__(self, private=False):
        self._p = private

    def capture(self, utterance="", with_screen=False, hours=48.0, **k):
        # 断言 simulation_lab 永不触发屏幕采集
        assert with_screen is False, "scenario 状态快照必须 with_screen=False(不碰屏幕传感器)"
        out = {"ok": True, "scope_id": "sc1", "n_items": 7,
               "counts": {"todo": 3, "calendar": 2, "file": 2},
               "focus": {"summary": "整理文件", "private": False}}
        if self._p:
            out["_private"] = True
        return out


@pytest.fixture
def scn_engines(monkeypatch):
    """场景沙盘融合输入源: world_model + mission_control + shared_context + autopilot_store。"""
    wm = FakeWMScn()
    mc = FakeMC()
    sc = FakeSC()
    ap = FakeApStore()
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    mapping = {"world_model": wm, "mission_control": mc,
               "shared_context": sc, "autopilot_store": ap}
    monkeypatch.setattr(sl, "_opt", _mk_opt(mapping))
    return {"wm": wm, "mc": mc, "sc": sc, "ap": ap, "mapping": mapping}


_PLAN = ("清理下载\nSTEP: 查看 ~/Downloads(只读)\n"
         "STEP: 移动 PDF 到归档\nSTEP: rm -rf /tmp/dup")


def test_simulate_plan_builds_three_futures(scn_engines):
    r = sl.simulate_plan(_PLAN, goal="清理下载文件夹")
    assert r["ok"] is True and r["scn_id"].startswith("scn_")
    cases = r["scenarios"]["cases"]
    assert set(cases) == {"best", "base", "worst"}
    probs = r["scenarios"]["probabilities"]
    assert abs(sum(probs.values()) - 1.0) < 0.01  # 概率归一
    # 每个未来都带后果链/敏感/失败点/回退/监控
    for key in ("second_order", "failure_triggers", "sensitivity",
                "early_warning", "rollback_paths"):
        assert key in r
    # 硬不变量: 场景记录恒不真执行
    saved = sl._load_sim(r["scn_id"])
    assert saved["executed_for_real"] is False and saved["kind"] == "scenario"


def test_simulate_plan_redline_pushes_worst_critical(scn_engines):
    r = sl.simulate_plan(_PLAN, goal="清理")
    cases = r["scenarios"]["cases"]
    assert cases["worst"]["risk_band"] == "critical"  # 含红线 → worst critical
    # 失败触发点含红线(critical)
    assert any(t["severity"] == "critical" for t in r["failure_triggers"])
    # 敏感变量榜首是红线(摆动最大 ×40)
    assert "红线" in r["sensitivity"]["levers"][0]["variable"]


def test_simulate_plan_records_deferred_not_stubbed(scn_engines):
    """epistemic_core / constraint_planner 缺失 → 记 deferred, 绝不建桩。"""
    r = sl.simulate_plan(_PLAN, goal="清理")
    mods = {d["module"] for d in r["deferred"]}
    assert "epistemic_core" in mods and "constraint_planner" in mods
    # 不确定性退回预测置信代理, 约束退回红线+偏好代理
    assert r["inputs_summary"]["uncertainty_source"] == "预测置信代理"
    assert r["inputs_summary"]["constraints_source"] == "红线+偏好代理"


def test_simulate_plan_fuses_user_goals_and_state(scn_engines):
    r = sl.simulate_plan(_PLAN, goal="清理")
    isum = r["inputs_summary"]
    assert isum["user_goals"] >= 1  # 读到 mission_control 目标
    assert isum["state_counts"] == {"todo": 3, "calendar": 2, "file": 2}  # 只回计数
    assert isum["world_model_available"] is True


def test_state_snapshot_metadata_only(scn_engines):
    """状态快照只回元数据(counts/focus/_private), 绝不带条目正文。"""
    state, deferred = sl._state_snapshot("清理")
    assert state is not None and deferred == []
    assert set(state.keys()) == {"counts", "n_items", "focus", "_private"}
    assert "items" not in state and "sources" not in state


def test_second_order_two_hop(scn_engines):
    r = sl.simulate_plan(_PLAN, goal="清理")
    so = sl.second_order_effects(r["scn_id"])
    assert so["ok"] is True and so["effects"]
    # 一阶"归档目录"的二阶下游含"索引"
    rings = {e["first_order"]: e["second_order"] for e in so["effects"]}
    assert "索引" in rings.get("归档目录", [])


def test_sensitivity_sorted_by_swing(scn_engines):
    r = sl.sensitivity(_PLAN)  # 直接传计划(免先 simulate_plan)
    sw = [l["swing"] for l in r["levers"]]
    assert sw == sorted(sw, reverse=True)


def test_early_warning_tied_to_triggers(scn_engines):
    r = sl.early_warning(_PLAN)
    assert r["ok"] is True and r["early_warning"]
    assert all("tied_to" in m for m in r["early_warning"])


def test_rollback_path_external_not_auto(scn_engines):
    r = sl.rollback_path("STEP: 写入文件\nSTEP: 发送邮件给客户")
    assert r["ok"] is True
    assert r["auto_revertible"] is False  # 对外动作不可自动回滚


def test_scenario_tree_three_branches(scn_engines):
    r = sl.scenario_tree(_PLAN)
    branches = r["tree"]["branches"]
    assert [b["case"] for b in branches] == ["best", "base", "worst"]
    # worst 分支带失败触发子分支
    worst = next(b for b in branches if b["case"] == "worst")
    assert worst["forks"]


def test_counterfactual_lowers_risk_when_redline_removed(scn_engines):
    base = sl.simulate_plan(_PLAN, goal="清理")
    # 把红线步骤(step3)换成只读 → 期望风险下降
    cf = sl.counterfactual(base["scn_id"], change="step3=查看 /tmp/dup(只读)")
    assert cf["ok"] is True
    assert cf["counterfactual_scn"] != base["scn_id"]
    assert cf["after"]["expected_risk"] < cf["before"]["expected_risk"]
    # 反事实记录仍恒不真执行
    saved = sl._load_sim(cf["counterfactual_scn"])
    assert saved["executed_for_real"] is False


def test_counterfactual_requires_change_or_assume(scn_engines):
    base = sl.simulate_plan("STEP: 查看天气(只读)", goal="天气")
    r = sl.counterfactual(base["scn_id"])
    assert r["ok"] is False
    r2 = sl.counterfactual(base["scn_id"], change="乱改")
    assert r2["ok"] is False and "k=v" in r2["error"]


def test_compare_futures_prefers_lower_expected_risk(scn_engines):
    risky = sl.simulate_plan(_PLAN, goal="清理")
    safe = sl.simulate_plan("STEP: 查看天气(只读)", goal="天气")
    r = sl.compare_futures([risky["scn_id"], safe["scn_id"]])
    assert r["ok"] is True
    assert r["recommended"] == safe["scn_id"]


def test_compare_futures_needs_two(scn_engines):
    r = sl.compare_futures(["scn_x"])
    assert r["ok"] is False


def test_explain_outcome_worst_lists_redline_driver(scn_engines):
    base = sl.simulate_plan(_PLAN, goal="清理")
    r = sl.explain_outcome(base["scn_id"], case="worst")
    assert r["ok"] is True and r["case"] == "worst"
    assert any("红线" in d for d in r["drivers"])
    assert r["consequence_chain"]


def test_scenario_private_propagates(monkeypatch):
    """world_model 命中私密 → 场景整包带 _private(下游切本机模型); 切片也传播。"""
    monkeypatch.setattr(sl, "_merge_mod", FakeMerge())
    monkeypatch.setattr(sl, "_opt", _mk_opt({"world_model": FakeWMScn(private=True)}))
    r = sl.simulate_plan("STEP: 查看私密项目(只读)", goal="私密项目")
    assert r.get("_private") is True
    assert sl.second_order_effects(r["scn_id"]).get("_private") is True
    assert sl.sensitivity(r["scn_id"]).get("_private") is True


def test_slice_on_raw_plan_does_not_persist(scn_engines):
    """切片查询直接传计划文本 → 现场组装但不落盘(不污染仿真库)。"""
    before = len(sl._list_sims(limit=200))
    sl.failure_triggers("STEP: 发邮件给客户")
    sl.sensitivity("STEP: 写入文件")
    after = len(sl._list_sims(limit=200))
    assert after == before  # 切片不保存


def test_scenario_audit_invariants(scn_engines):
    base = sl.simulate_plan(_PLAN, goal="清理")
    a = sl.audit(base["scn_id"])
    assert a["ok"] is True
    assert a["invariants_ok"] is True
    assert a["safety_invariants"]["executed_for_real"] is False
    assert a["scenario_audit"]["is_scenario"] is True
    assert set(a["scenario_audit"]["cases"]) == {"best", "base", "worst"}


def test_scenario_status_summary(scn_engines):
    base = sl.simulate_plan(_PLAN, goal="清理")
    st = sl.status(base["scn_id"])
    assert st["ok"] is True
    assert set(st["scenario"]["cases"]) == {"best", "base", "worst"}


def test_scenario_funcs_not_in_llm_surface():
    """新场景接口绝不进 LLM 工具面(强动作 CLI/程序调用; 工具面仍冻结为 3 只读)。"""
    for fn in ("simulation_lab_simulate_plan", "simulation_lab_scenario_tree",
               "simulation_lab_counterfactual", "simulation_lab_compare_futures",
               "simulation_lab_failure_triggers"):
        assert not sl.is_tool(fn)
    names = {t["function"]["name"] for t in sl.approved_tools()}
    assert names == {"simulation_lab_simulate", "simulation_lab_risk_score", "simulation_lab_status"}


def test_scenario_layer_never_self_confirms_static():
    """新增场景层不得引入 confirmed=True / 禁忌执行原语(复用全文件静态扫描)。"""
    code = _code_only(_SL_PATH)
    assert not re.search(r"confirmed\s*=\s*True", code)
    for p in (r"\bsubprocess\b", r"\bexec\b", r"\beval\b", r"\bcompile\b", r"\b__import__\b",
              r"\bos\s*\.\s*remove\b"):
        assert not re.search(p, code), f"场景层引入禁忌原语: {p}"
    assert "gmail_creds" not in code and "gcal_creds" not in code
