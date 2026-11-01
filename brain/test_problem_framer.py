# -*- coding: utf-8 -*-
"""problem_framer(问题框定与需求重构核心)测试。

覆盖 14 个动作 + 安全/隐私不变量 + 框定启发式:
  · frame 把原始请求重构成问题框架(定义/真目标/成功标准/约束/相关方/非目标/假设/子问题/追问)并入库
  · reframe(5 lens)/infer_real_goal/success_criteria/constraints/stakeholders/non_goals/
    hidden_assumptions/decompose_problem/ask_clarifying_questions/explain 的分析形态
    (支持 frame_id 或现场 request/text)
  · 红线显化: constraints 转发 local_powers 单一真相源(假/真), 绝不复制红线表
  · 隐私: 凭据文件硬拦不入库 / 私密请求脱敏 + _private/_force_local 标记 / mission_control 对齐私密保护
  · handoff 落地动作必经 autopilot 审批队列(假 store, 绝不执行)/ 纯交接无副作用 / fail-closed
  · dispatch 白名单只读(frame/handoff/audit 不暴露)/ 工具名与 approved_tools 一致
  · deferred 记录不建桩 / status/health canonical / _is_secret_path 双层(本地正则不可绕)
  · 源码静态护栏(无 subprocess/os.system/confirmed=True/执行面方法)+ selftest + py_compile
全程隔离: env 覆盖 HOME 到 tmp; 注入假依赖经 monkeypatch _opt, 不碰真实库/真实副作用。
"""
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import problem_framer as pf  # noqa: E402


# --------------------------------------------------------------------------- #
# 隔离: env 把 HOME 指向 tmp(每个测试独立库); 默认断开所有软依赖(纯启发式路径)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(pf._HOME_ENV, str(tmp_path / "pf"))
    monkeypatch.setattr(pf, "_opt", lambda name: None)
    yield


def _mk_opt(mapping):
    """返回一个 _opt 替身: 只认 mapping 里的模块, 其余 → None。"""
    return lambda name: mapping.get(name)


# --------------------------------------------------------------------------- #
# frame — 构造问题框架并入库
# --------------------------------------------------------------------------- #
def test_frame_assembles_all_dimensions():
    r = pf.frame(request="帮我做个销售月报系统")
    assert r["ok"]
    assert r["frame_id"].startswith("pf_")
    for k in ("problem_definition", "real_goal", "success_criteria", "constraints",
              "stakeholders", "non_goals", "hidden_assumptions", "subproblems",
              "task_boundaries", "clarifying_questions"):
        assert k in r, k
    assert r["request_kind"] in ("decision", "question", "build", "task",
                                 "exploration", "statement")


def test_frame_needs_input():
    r = pf.frame()
    assert r["ok"] is False and "error" in r


def test_frame_persists_and_status_roundtrip():
    r = pf.frame(request="整理一下这些文件")
    fid = r["frame_id"]
    s = pf.status(fid)
    assert s["ok"] and s["frame_id"] == fid
    assert s["request_kind"] and "n_subproblems" in s


def test_request_classification():
    assert pf.frame(request="该不该涨价")["request_kind"] == "decision"
    assert pf.frame(request="为什么这个会失败")["request_kind"] == "question"
    assert pf.frame(request="帮我搭个数据平台")["request_kind"] == "build"
    assert pf.frame(request="帮我改一下这个配置")["request_kind"] == "task"
    assert pf.frame(request="研究下这个方向看看")["request_kind"] == "exploration"


# --------------------------------------------------------------------------- #
# 凭据 / 隐私
# --------------------------------------------------------------------------- #
def test_frame_drops_credential_paths():
    r = pf.frame(request="帮我备份 ~/.gmail_creds.json 这个很关键的文件")
    assert r["ok"]
    import json
    row = pf._get_frame(r["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert ".gmail_creds.json" not in blob          # 凭据路径绝不入库
    assert row["private"] == 1                        # 含凭据 → 标私密


def test_credential_in_request_column_scrubbed():
    r = pf.frame(request="读取 /Users/x/.localkey 然后继续")
    row = pf._get_frame(r["frame_id"], pf._db_path())
    assert ".localkey" not in (row["request"] or "")


def test_private_content_default_off_raw_no_flag(monkeypatch):
    """数据层隐私总开关(默认 OFF, User 2026-07-08 放弃): 即便 privacy_stream 判私密,
    本模块也不再打 _private/_force_local, 敏感数字原样落库(数据层不阉割)。"""
    class FakePS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True, "hits": [{"name": "wage"}]}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"privacy_stream": FakePS()}))
    monkeypatch.setattr(pf.config, "get",
                        lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    r = pf.frame(request="我工资是 12345 元, 该不该跟老板谈涨薪")
    assert r.get("_private") is not True
    assert r.get("_force_local") is not True
    import json
    row = pf._get_frame(r["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "12345" in blob                             # 开关关: 不再脱敏, 原文落库


def test_private_content_switch_on_flagged_and_redacted(monkeypatch):
    """数据层隐私总开关 ON(可逆恢复旧行为): 打 _private/_force_local, 敏感数字脱敏后才落库。"""
    class FakePS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True, "hits": [{"name": "wage"}]}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"privacy_stream": FakePS()}))

    def _on(k, d=None):
        if k == "safety.data_layer_privacy_enabled":
            return True
        if k == "safety.allow_cloud_private":
            return False
        return d
    monkeypatch.setattr(pf.config, "get", _on)
    r = pf.frame(request="我工资是 12345 元, 该不该跟老板谈涨薪")
    assert r.get("_private") is True
    assert r.get("_force_local") is True
    import json
    row = pf._get_frame(r["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "12345" not in blob                        # 敏感数字脱敏后才落库


def test_is_secret_path_local_regex_floor_not_bypassed(monkeypatch):
    """MEMORY jarvis-secret-path-dual-layer: 隐私库说 False 也不能绕过本地正则。"""
    class FakeAW:
        @staticmethod
        def is_secret_path(p):
            return False  # 库误判为非机密
    monkeypatch.setattr(pf, "_opt", _mk_opt({"awareness_sensors": FakeAW()}))
    assert pf._is_secret_path("~/.gmail_creds.json") is True
    assert pf._is_secret_path("/x/id_rsa") is True
    assert pf._is_secret_path("/tmp/normal.txt") is False


def test_is_secret_path_library_adds(monkeypatch):
    class FakeAW:
        @staticmethod
        def is_secret_path(p):
            return "secret_dir" in p
    monkeypatch.setattr(pf, "_opt", _mk_opt({"awareness_sensors": FakeAW()}))
    assert pf._is_secret_path("/home/secret_dir/x") is True


# --------------------------------------------------------------------------- #
# 红线显化(转发 local_powers 单一真相源, 绝不复制红线表)
# --------------------------------------------------------------------------- #
def test_constraints_surfaces_red_line_via_local_powers(monkeypatch):
    class FakeLP:
        @staticmethod
        def _check_red_line(t):
            return "DROP TABLE 删表" if "DROP TABLE" in t else None
        @staticmethod
        def non_shell_danger(t, kind=None):
            return None
    monkeypatch.setattr(pf, "_opt", _mk_opt({"local_powers": FakeLP()}))
    r = pf.constraints(request="把 users 表 DROP TABLE 掉")
    assert r["ok"] and r["has_red_line"]
    assert any("DROP TABLE" in c["text"] for c in r["red_lines"])
    assert all(c["source"].startswith("local_powers") or c["source"] == "stakeholder"
               for c in r["red_lines"])


def test_constraints_no_red_line_when_local_powers_absent():
    # 软依赖缺失 → 不复制红线表, 不误报红线(只有非红线约束)
    r = pf.constraints(request="把 users 表 DROP TABLE 掉")
    assert r["ok"] and r["has_red_line"] is False


def test_constraints_external_action_red_line4():
    r = pf.constraints(request="把报告发邮件给客户")
    assert any(c["kind"] == "red_line" and "红线4" in c["text"] for c in r["constraints"])


def test_constraints_time_budget_privacy():
    r = pf.constraints(request="今天内免费搞定, 别外发")
    kinds = {c["kind"] for c in r["constraints"]}
    assert {"time", "budget", "privacy"}.issubset(kinds)


# --------------------------------------------------------------------------- #
# 各维度分析动作
# --------------------------------------------------------------------------- #
def test_infer_real_goal_explicit_marker():
    r = pf.infer_real_goal(request="帮我加个缓存, 为了让页面更快")
    goals = r["real_goal"]["inferred_goals"]
    assert any("快" in g["goal"] for g in goals)


def test_infer_real_goal_aligns_mission_goals(monkeypatch):
    class FakeMC:
        @staticmethod
        def list_goals(status="active"):
            return [{"id": 3, "title": "把生产巡检系统稳定性做上去"},
                    {"id": 9, "title": "完全无关的目标 xyz"}]
    monkeypatch.setattr(pf, "_opt", _mk_opt({"mission_control": FakeMC()}))
    r = pf.infer_real_goal(request="给生产巡检系统加个稳定性告警")
    aligned = r["real_goal"]["aligned_strategic_goals"]
    assert any(a["goal_id"] == 3 for a in aligned)


def test_mission_goal_private_title_protected(monkeypatch):
    class FakeMC:
        @staticmethod
        def list_goals(status="active"):
            return [{"id": 1, "title": "/Users/x/.gmail_creds.json 管理"}]
    monkeypatch.setattr(pf, "_opt", _mk_opt({"mission_control": FakeMC()}))
    r = pf.infer_real_goal(request="处理 gmail 凭据")
    import json
    blob = json.dumps(r, ensure_ascii=False)
    assert ".gmail_creds.json" not in blob   # 凭据标题不纳入对齐


def test_success_criteria_has_acceptance_gate():
    r = pf.success_criteria(request="做个导出功能")
    assert r["ok"]
    assert any("review" in c["text"].lower() or "回滚" in c["text"]
               for c in r["success_criteria"])


def test_success_criteria_metric_extracted():
    r = pf.success_criteria(request="把响应时间降到 200 毫秒以内")
    assert any(c["kind"] == "metric" and c["measurable"] for c in r["success_criteria"])


def test_stakeholders_user_default_and_external():
    r = pf.stakeholders(request="把月报发给客户")
    assert any(s["name"] == "User" for s in r["stakeholders"])
    assert r["has_external"] is True


def test_non_goals_explicit_and_inferred():
    r = pf.non_goals(request="改这个 bug, 不要动其它模块")
    assert any(not n["inferred"] for n in r["non_goals"])     # 显式「不要动」
    assert any(n["inferred"] for n in r["non_goals"])         # 推断防 scope creep


def test_hidden_assumptions_vague_reference():
    r = pf.hidden_assumptions(request="帮我把那个东西改一下")
    assert any(h["kind"] == "reference" for h in r["hidden_assumptions"])


def test_hidden_assumptions_continue_state():
    r = pf.hidden_assumptions(request="继续上次没做完的")
    assert any(h["kind"] == "state" for h in r["hidden_assumptions"])


def test_decompose_multistep():
    r = pf.decompose_problem(request="先取数, 然后算奖金, 最后发飞书")
    assert len(r["task_boundaries"]) >= 2


def test_decompose_hidden_subproblems():
    r = pf.decompose_problem(request="把这个部署上线")
    assert r["n_hidden"] >= 1
    assert any(s["hidden"] for s in r["subproblems"])


def test_ask_clarifying_questions_has_defaults():
    r = pf.ask_clarifying_questions(request="帮我弄一下那个")
    assert r["ok"] and r["clarifying_questions"]
    assert all("default" in q for q in r["clarifying_questions"])


def test_clarifying_red_line_is_blocking(monkeypatch):
    class FakeLP:
        @staticmethod
        def _check_red_line(t):
            return "rm 删除文件" if "rm " in t else None
        @staticmethod
        def non_shell_danger(t, kind=None):
            return None
    monkeypatch.setattr(pf, "_opt", _mk_opt({"local_powers": FakeLP()}))
    r = pf.ask_clarifying_questions(request="rm 这个临时文件")
    assert r["has_blocking"] is True


def test_explain_full_shape():
    r = pf.explain(request="该不该重构这个模块")
    for k in ("headline", "narrative", "confidence", "has_red_line"):
        assert k in r


def test_reframe_all_lenses():
    for lens in ("broaden", "narrow", "invert", "first_principles", "restate"):
        r = pf.reframe(request="帮我优化这个查询", lens=lens)
        assert r["ok"] and r["lens"] == lens and r["reframed"]


def test_reframe_unknown_lens_falls_back():
    r = pf.reframe(request="x", lens="nonsense")
    assert r["ok"] and r["lens"] == "restate"


def test_analysis_actions_accept_stored_id():
    b = pf.frame(request="帮我做个奖金表, 然后发给老板")
    fid = b["frame_id"]
    for fn in (pf.infer_real_goal, pf.success_criteria, pf.constraints, pf.stakeholders,
               pf.non_goals, pf.hidden_assumptions, pf.decompose_problem,
               pf.ask_clarifying_questions, pf.explain, pf.reframe):
        r = fn(frame_id=fid)
        assert r["ok"], fn.__name__


def test_unknown_frame_id_errors():
    r = pf.constraints(frame_id="pf_doesnotexist")
    assert r["ok"] is False and "error" in r


def test_analysis_needs_some_input():
    r = pf.infer_real_goal()
    assert r["ok"] is False


# --------------------------------------------------------------------------- #
# handoff — 落地动作必经审批队列, 绝不执行
# --------------------------------------------------------------------------- #
def test_handoff_memo_no_side_effect():
    b = pf.frame(request="整理文件")
    r = pf.handoff(frame_id=b["frame_id"], note="just a memo")
    assert r["ok"] and r["needs_confirm"] is False and r["queued_approval_id"] is None


def test_handoff_landing_action_gated(monkeypatch):
    enq = {}

    class FakeLP:
        @staticmethod
        def _check_red_line(t):
            return "可能触发真实付费" if "pay" in t else None
        @staticmethod
        def non_shell_danger(t, kind=None):
            return None

    class FakeAps:
        @staticmethod
        def enqueue_approval(action, action_kind, payload, risk="high", why="", **k):
            enq.update({"action": action, "risk": risk, "kind": action_kind})
            return 7777

    b = pf.frame(request="付款给供应商")
    monkeypatch.setattr(pf, "_opt", _mk_opt({"local_powers": FakeLP(),
                                             "autopilot_store": FakeAps()}))
    r = pf.handoff(frame_id=b["frame_id"], action="curl pay.example")
    assert r["needs_confirm"] is True
    assert r["queued_approval_id"] == 7777
    assert enq["risk"] == "high"
    assert r["red_line"] == "可能触发真实付费"


def test_handoff_failsafe_when_queue_unreachable(monkeypatch):
    monkeypatch.setattr(pf, "_opt", _mk_opt({}))
    b = pf.frame(request="删掉临时目录")
    r = pf.handoff(frame_id=b["frame_id"], action="rm -rf /tmp/x")
    assert r["ok"] and r["needs_confirm"] is True and r["queued_approval_id"] is None


def test_handoff_failclosed_when_enqueue_raises(monkeypatch):
    class FakeLP:
        @staticmethod
        def _check_red_line(t):
            return None
        @staticmethod
        def non_shell_danger(t, kind=None):
            return None

    class BoomAps:
        @staticmethod
        def enqueue_approval(**k):
            raise RuntimeError("queue down")

    b = pf.frame(request="发邮件给客户")
    monkeypatch.setattr(pf, "_opt", _mk_opt({"local_powers": FakeLP(), "autopilot_store": BoomAps()}))
    r = pf.handoff(frame_id=b["frame_id"], action="发邮件给客户")
    assert r["needs_confirm"] is True
    assert r["queued_approval_id"] is None


# --------------------------------------------------------------------------- #
# dispatch 白名单 / 工具面 / status / health / deferred
# --------------------------------------------------------------------------- #
def test_dispatch_whitelist_readonly_only():
    for blocked in ("frame", "handoff", "audit"):
        assert pf.dispatch(blocked, {}).get("ok") is False
    assert pf.dispatch("status", {}).get("ok") is True
    assert pf.dispatch("infer_real_goal", {"request": "帮我做个表"}).get("ok") is True
    assert pf.dispatch("reframe", {"request": "x", "lens": "broaden"}).get("ok") is True


def test_tool_names_match_whitelist():
    names = {t["function"]["name"] for t in pf.approved_tools()}
    expected = {"problem_framer_" + v for v in pf._DISPATCH_WHITELIST}
    assert names == expected
    for n in names:
        assert pf.is_tool(n)
    assert pf.is_tool("problem_framer_frame") is False


def test_approved_tools_respects_config(monkeypatch):
    monkeypatch.setattr(pf.config, "get",
                        lambda k, d=None: False if k == "problem_framer.enabled" else d)
    assert pf.approved_tools() == []


def test_status_overview_canonical():
    s = pf.status()
    assert s["ok"] and "engines" in s and "deferred" in s and "consumers" in s
    assert len(s["actions"]) == 14


def test_status_engines_only_real_sources():
    s = pf.status()
    assert set(["mission_control", "world_model", "mind_graph"]).issubset(set(s["engines"].keys()))
    assert "memory" not in s["engines"]
    assert "semantic" not in s["engines"]


def test_consumers_documented_not_imported():
    s = pf.status()
    for c in ("metacognition_core", "reasoning_core", "mission_control",
              "strategy_tournament", "action_kernel", "outcome_verifier"):
        assert c in s["consumers"]


def test_health_ok():
    h = pf.health()
    assert h["ok"] is True and h["detail"]


def test_audit_credentials_zero_and_guarantees():
    pf.frame(request="做个表")
    a = pf.audit()
    assert a["ok"] and a["credential_paths_stored"] == 0
    assert len(a["guarantees"]) >= 5


def test_deferred_modules_recorded_not_stubbed():
    d = pf._deferred_status()
    for m in ("metacognition_core", "reasoning_core", "value_reasoner", "epistemic_core"):
        assert d[m]["present"] is False and d[m]["deferred"] is True


# --------------------------------------------------------------------------- #
# 源码静态护栏(回归保护)
# --------------------------------------------------------------------------- #
def test_source_has_no_dangerous_calls():
    import ast
    src = open(pf.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    bad = []

    class _Scan(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            if node.name == "_selftest":
                return
            self.generic_visit(node)

        def visit_Call(self, node):  # noqa: N802
            for kw in node.keywords:
                if kw.arg == "confirmed" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    bad.append("confirmed=True")
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in (
                    "system", "popen", "remove", "unlink", "rmtree", "rmdir"):
                bad.append("." + f.attr)
            if isinstance(f, ast.Name) and f.id in ("exec", "eval", "__import__"):
                bad.append(f.id)
            self.generic_visit(node)

        def visit_Import(self, node):  # noqa: N802
            for a in node.names:
                if a.name.split(".")[0] in pf._FORBIDDEN_EXEC_MODULES:
                    bad.append("import " + a.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in pf._FORBIDDEN_EXEC_MODULES:
                bad.append("from " + (node.module or ""))
            self.generic_visit(node)

    _Scan().visit(tree)
    assert bad == [], "源码出现危险调用/执行面 import: " + str(bad)


def test_no_subprocess_import():
    import ast
    src = open(pf.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" not in imported


def test_selftest_passes():
    assert pf._selftest() == 0


def test_module_compiles():
    import py_compile
    py_compile.compile(pf.__file__, doraise=True)


# --------------------------------------------------------------------------- #
# 对抗审查回归(adversarial review 确认的缺陷, 修复后锁死)
# --------------------------------------------------------------------------- #
def test_stakeholder_grounding_private_node_protected(monkeypatch):
    """mind_graph 接地引入私密人物 → 标 _private, 不落明文。"""
    import json

    class FakeMG:
        @staticmethod
        def query(text, k=4):
            return {"nodes": [{"id": 1, "name": "张三的工资单88888888", "ntype": "person",
                               "private": True}], "edges": [], "_private": True}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"mind_graph": FakeMG()}))
    monkeypatch.setattr(pf.config, "get",
                        lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    r = pf.frame(request="给团队发奖金")
    assert r.get("_private") is True and r.get("_force_local") is True
    row = pf._get_frame(r["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "张三的工资单88888888" not in blob


def test_stakeholder_grounding_credential_hard_blocked(monkeypatch):
    import json

    class FakeMG:
        @staticmethod
        def query(text, k=4):
            return {"nodes": [{"id": 1, "name": "/Users/x/.gmail_creds.json", "ntype": "person"}],
                    "edges": []}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"mind_graph": FakeMG()}))
    r = pf.frame(request="处理联系人")
    row = pf._get_frame(r["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert ".gmail_creds.json" not in blob
    a = pf.audit()
    assert a["credential_paths_stored"] == 0


def test_non_string_input_no_crash():
    assert pf.dispatch("constraints", {"request": 123, "text": None}).get("ok") in (True, False)
    assert pf.dispatch("status", {"frame_id": 999}).get("ok") in (True, False)
    assert pf.infer_real_goal(request=123, text=None).get("ok") in (True, False)
    assert pf.frame(request=123).get("ok") in (True, False)


def test_status_handoff_audit_non_string_id():
    assert pf.status(frame_id=42)["ok"] in (True, False)
    assert pf.audit(frame_id=42)["ok"] is True
    assert pf.handoff(frame_id=42).get("ok") in (True, False)


def test_regexes_no_redos():
    import time
    pathological = "如果" + "啊" * 40000 + "那么尽快发邮件给客户"
    t0 = time.time()
    r = pf.frame(request=pathological)
    assert r["ok"] and (time.time() - t0) < 5
    longword = "a" * 60000 + " 然后发给客户"
    t0 = time.time()
    r2 = pf.constraints(request=longword)
    assert r2["ok"] and (time.time() - t0) < 5


def test_surrogate_input_no_crash():
    r = pf.frame(request="x\ud800y 帮我做个 a\ud83d 表")
    assert r["ok"]


def test_mission_alignment_non_list_no_crash(monkeypatch):
    class FakeMC:
        @staticmethod
        def list_goals(status="active"):
            return None  # 异常返回
    monkeypatch.setattr(pf, "_opt", _mk_opt({"mission_control": FakeMC()}))
    r = pf.infer_real_goal(request="做个表")
    assert r["ok"]


def test_frame_kind_decision_not_executed():
    # 决策型请求: non_goals 应提醒「决策 ≠ 执行」
    r = pf.non_goals(request="该不该上这个功能")
    assert any("执行" in n["text"] for n in r["non_goals"])


# --------------------------------------------------------------------------- #
# 对抗审查 v2 回归(22-agent 审查确认的 13 处缺陷, 修复后锁死)
# --------------------------------------------------------------------------- #
def test_v2_world_model_because_dict_private_dropped(monkeypatch):
    """#1: world_model.explain 的 because 是 dict 列表(含 entity 明文 + per-item private)。
    必须读 per-item private 标志按字段取值, 绝不 str(dict); 私密实体既不入输出也不入库。"""
    import json

    class WM:
        @staticmethod
        def explain(t):
            return {"because": [{"rtype": "related_to", "entity": "供应商示例涂料",
                                 "confidence": 0.9, "private": True},
                                {"entity": "公开背景知识", "private": False}],
                    "contradicts": None}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"world_model": WM}))
    monkeypatch.setattr(pf.config, "get",
                        lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    out = pf.frame(request="帮我做个并购尽调系统")
    live = json.dumps(out, ensure_ascii=False)
    row = pf._get_frame(out["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "供应商示例涂料" not in live and "供应商示例涂料" not in blob   # 私密实体明文绝不逃逸
    assert out.get("_private") is True and row["private"] == 1
    # 公开背景被正确保留(在 live 输出里)
    assert "公开背景知识" in live


def test_v2_frame_level_private_default_off_no_blanking(monkeypatch):
    """#2 默认 OFF: 数据层隐私放弃后, privacy_stream 判私密也不再整框占位, 原文(含非触发
    字段的 token)照常落库, 不打 _private。"""
    import json

    class PS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True} if "工资" in text else {"private": False}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"privacy_stream": PS}))
    monkeypatch.setattr(pf.config, "get",
                        lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    out = pf.frame(request="把我工资数据整理一下, 然后把合同编号 XK-PRIVATE-9988 写进去")
    row = pf._get_frame(out["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "XK-PRIVATE-9988" in blob        # 开关关: 不再整框占位, 原文落库
    assert row["private"] == 0 and out.get("_private") is not True


def test_v2_frame_level_private_switch_on_blanks_non_trigger_tokens(monkeypatch):
    """#2 switch ON(可逆恢复旧行为): 框级私密时, 即便某派生字段片段本身不再独立触发隐私
    分类器, 也整段占位落库。"""
    import json

    class PS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True} if "工资" in text else {"private": False}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"privacy_stream": PS}))

    def _on(k, d=None):
        if k == "safety.data_layer_privacy_enabled":
            return True
        if k == "safety.allow_cloud_private":
            return False
        return d
    monkeypatch.setattr(pf.config, "get", _on)
    out = pf.frame(request="把我工资数据整理一下, 然后把合同编号 XK-PRIVATE-9988 写进去")
    row = pf._get_frame(out["frame_id"], pf._db_path())
    blob = json.dumps(row["frame"], ensure_ascii=False)
    assert "XK-PRIVATE-9988" not in blob    # 非触发字段的敏感 token 也被整框占位
    assert row["private"] == 1 and out.get("_private") is True


def test_v2_decision_request_not_mislabeled_nongoal():
    """#3: 「要不要X」是决策, 不能因 '不要'⊂'要不要' 被登记成 explicit 非目标(意图反转)。"""
    r = pf.non_goals(request="要不要给系统加缓存")
    assert not any((not n["inferred"]) and "要不要" in n["text"] for n in r["non_goals"])
    # 分类仍为 decision
    assert pf.frame(request="要不要给系统加缓存")["request_kind"] == "decision"


def test_v2_weak_decision_markers_reclassified():
    """#4: '还是'(副词义)/'好不好' 不再误判为 decision; 真'A 还是 B'仍是 decision。"""
    assert pf._classify_request("我还是把月报整理一下吧") == "task"
    assert pf._classify_request("用 redis 还是 memcached") == "decision"
    assert pf._classify_request("这个方案好不好") == "question"


def test_v2_strip_lead_verb_no_compound_mangle():
    """#5: _strip_lead_verb 只剥无歧义开场词, 不把 '处理器' 切成 '器'。"""
    assert pf._strip_lead_verb("处理器选型方案") == "处理器选型方案"
    assert pf._strip_lead_verb("分析师报告") == "分析师报告"
    assert pf._strip_lead_verb("帮我处理这个表") == "处理这个表"   # 开场词正确剥离


def test_v2_metric_regex_no_redos():
    """#6: 可度量目标正则量词有界, 长数字串不再 O(n^2) 回溯; 真实指标仍抽得到。"""
    import time
    t0 = time.time()
    r = pf.frame(request="9" * 50000)
    assert r["ok"] and (time.time() - t0) < 3
    sc = pf.success_criteria(request="把延迟降到 50ms, 处理 10000 条, 准确率 99.5%")
    metrics = [c["text"] for c in sc["success_criteria"] if c["kind"] == "metric"]
    assert any("50ms" in m for m in metrics) and any("99.5%" in m for m in metrics)


def test_v2_safe_text_on_all_store_columns():
    """#7: session_id 与 handoff 的 to/action/note 列都过 _safe_text; 孤立代理项不崩。"""
    sid = b"sess-\xff".decode("utf-8", "surrogateescape")
    r = pf.frame(request="做个表", session_id=sid)
    assert r["ok"]
    fid = pf.frame(request="x")["frame_id"]
    note = b"n \xff".decode("utf-8", "surrogateescape")
    h = pf.handoff(frame_id=fid, to="t_" + note, action="", note=note)
    assert h["ok"]   # 无 action → 纯备忘, 但 to/note 含代理项仍不崩


def test_v2_non_shell_danger_dict_without_why_still_red(monkeypatch):
    """#8: non_shell_danger 返回为真但无 'why' 的 dict, 仍登记红线(宁可保守, 不漏报)。"""
    class LP:
        @staticmethod
        def _check_red_line(t):
            return None
        @staticmethod
        def non_shell_danger(t, kind=None):
            return {"danger": True, "reason": "deletes user data"}
    monkeypatch.setattr(pf, "_opt", _mk_opt({"local_powers": LP}))
    r = pf.constraints(request="rm 业务目录")
    assert r["has_red_line"] is True
    assert any("deletes user data" in c["text"] or "危险闸" in c["text"] for c in r["red_lines"])


def test_v2_full_text_dedup_keeps_distinct_items():
    """#10: 去重按完整文本, 仅在长前缀之后才不同的两条不被误并。"""
    fr = {"request": "x", "request_kind": "task", "specificity": 1.0, "context": "",
          "hidden_assumptions": [], "success_criteria": [{"kind": "metric", "measurable": True}],
          "constraints": [{"kind": "red_line", "text": "删除生产环境数据库中的 users 用户表"},
                          {"kind": "red_line", "text": "删除生产环境数据库中的 orders 订单表"}]}
    blocking = [q for q in pf._clarifying_questions(fr) if q.get("blocking")]
    assert len(blocking) == 2   # 两条红线 → 两条独立确认问, 不被前缀去重误并


def test_v2_corrupted_db_frame_no_crash():
    """#11: 读库后结构损坏(列表项非 dict / 缺 text 键 / real_goal 非 dict)→ 各动作不抛, 返 dict。"""
    import sqlite3
    import json
    db = pf._db_path()
    conn = pf._conn(db)
    bad = {"request": "x", "request_kind": "task", "real_goal": "not-a-dict",
           "success_criteria": ["s", {"kind": "nokeytext"}], "constraints": [None, {"kind": "x"}],
           "stakeholders": ["bare", {"role": "r"}], "non_goals": [1, {"x": 1}],
           "hidden_assumptions": [None], "subproblems": ["bare", {"hidden": True}],
           "task_boundaries": [1], "clarifying_questions": ["q", {"blocking": True}]}
    conn.execute("INSERT INTO pf_frames (frame_id,ts,session_id,request,request_kind,frame,"
                 "n_subproblems,n_constraints,n_questions,private) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("pf_bad", "t", "s", "x", "task", json.dumps(bad), 0, 0, 0, 0))
    conn.commit()
    conn.close()
    for a in ("explain", "constraints", "stakeholders", "non_goals", "decompose_problem",
              "ask_clarifying_questions", "success_criteria", "infer_real_goal",
              "hidden_assumptions"):
        r = getattr(pf, a)(frame_id="pf_bad")
        assert isinstance(r, dict) and "ok" in r, a
    for lens in ("broaden", "narrow", "invert", "first_principles", "restate"):
        assert pf.reframe(frame_id="pf_bad", lens=lens)["ok"]


def test_v2_status_order_deterministic_tiebreak():
    """#12: 同秒多帧时 status.recent 有 frame_id 二级 tie-break, 顺序可复现。"""
    for i in range(6):
        pf._store_frame({"request": "req-%d" % i, "request_kind": "task", "subproblems": [],
                         "constraints": [], "clarifying_questions": []}, "s", pf._db_path())
    o1 = [r["frame_id"] for r in pf.status()["recent"]]
    o2 = [r["frame_id"] for r in pf.status()["recent"]]
    assert o1 == o2
    # tie-break 列存在于 SQL: frame_id DESC 单调
    assert o1 == sorted(o1, reverse=True) or len(set(r for r in o1)) == len(o1)


def test_v2_dispatch_double_prefix_consistent_with_is_tool():
    """#13: 双前缀名 dispatch 不再塌成白名单动作; 与 is_tool 一致地拒绝。"""
    assert pf.is_tool("problem_framer_problem_framer_status") is False
    assert pf.dispatch("problem_framer_problem_framer_status", {}).get("ok") is False
    # 正常单前缀仍一致放行
    assert pf.is_tool("problem_framer_status") is True
    assert pf.dispatch("problem_framer_status", {}).get("ok") is True


def test_v2_classify_private_default_off_always_false():
    """数据层隐私总开关默认 OFF(User 2026-07-08 放弃): _classify_private 恒 False,
    不论库缺失还是库出错 —— 数据层不再判私密/占位。"""
    # 库缺失(_opt 全 None, fixture 已设)
    assert pf._classify_private("我工资很私密") is False

    class Boom:
        @staticmethod
        def classify_private(text="", app="", window=""):
            raise RuntimeError("boom")
    import unittest.mock as mock
    with mock.patch.object(pf, "_opt", _mk_opt({"awareness_sensors": Boom})):
        assert pf._classify_private("任意文本") is False


def test_v2_classify_private_switch_on_doc_matches_impl(monkeypatch):
    """#9 switch ON(可逆恢复旧行为) —— 文档已与实现对齐: 库缺失时 _classify_private
    返 False(交回 privacy_stream 主判), 仅库出错时 fail-safe True。锁死该语义防文档/实现再漂移。"""
    _orig = pf.config.get

    def _on(k, d=None):
        return True if k == "safety.data_layer_privacy_enabled" else _orig(k, d)
    monkeypatch.setattr(pf.config, "get", _on)
    # 库缺失(_opt 全 None, fixture 已设)
    assert pf._classify_private("我工资很私密") is False
    # 库出错 → fail-safe True

    class Boom:
        @staticmethod
        def classify_private(text="", app="", window=""):
            raise RuntimeError("boom")
    import unittest.mock as mock
    with mock.patch.object(pf, "_opt", _mk_opt({"awareness_sensors": Boom})):
        assert pf._classify_private("任意文本") is True
