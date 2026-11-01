# -*- coding: utf-8 -*-
"""argument_prover(可执行论证图与推理证明检查核心)测试。

覆盖 13 个动作 + 安全/隐私不变量 + 逻辑缺陷检测器:
  · build_argument 把主张展开成论证图(前提→规则→中间结论→主张)并入库
  · premises/inference_chain/check_validity/find_gaps/fallacy_scan/minimal_proof/
    counterargument/strengthen/explain_logic 的分析形态(支持 argument_id 或现场 claim/text)
  · 缺陷检测: 循环论证 / 跳步 / 偷换概念 / 因果倒置(假 world_model) / 相关非因果 /
    未证明前提 / 价值混入事实(is-ought) / 修辞谬误目录
  · 隐私: 凭据文件硬拦不入库 / 私密论证脱敏 + _private/_force_local 标记
  · handoff 落地动作必经 autopilot 审批队列(假 store, 绝不执行)/ 纯交接无副作用
  · dispatch 白名单只读(build_argument/handoff/audit 不暴露)/ 工具名与 approved_tools 一致
  · deferred 记录不建桩 / status/health canonical / _is_secret_path 双层(本地正则不可绕)
  · 源码静态护栏(无 subprocess/os.system/confirmed=True/执行面方法)+ selftest + py_compile
全程隔离: env 覆盖 HOME 到 tmp; 注入假依赖经 monkeypatch _opt, 不碰真实库/真实副作用。
"""
import os
import sys
import importlib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import argument_prover as ap  # noqa: E402


# --------------------------------------------------------------------------- #
# 隔离: env 把 HOME 指向 tmp(每个测试独立库); 默认断开所有软依赖(纯启发式路径)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(ap._HOME_ENV, str(tmp_path / "ap"))
    # 默认让软依赖全部缺失 → 走纯本地启发式(可预测); 个别测试再注入假依赖。
    monkeypatch.setattr(ap, "_opt", lambda name: None)
    yield


def _mk_opt(mapping):
    """返回一个 _opt 替身: 只认 mapping 里的模块, 其余 → None。"""
    return lambda name: mapping.get(name)


# --------------------------------------------------------------------------- #
# build_argument — 构造论证图并入库
# --------------------------------------------------------------------------- #
def test_build_argument_assembles_graph():
    r = ap.build_argument(claim="该买这只股票", text="因为它市盈率低, 所以该买这只股票")
    assert r["ok"]
    assert r["argument_id"].startswith("arg_")
    assert r["n_premises"] >= 1
    assert r["claim_kind"] in ("fact", "value", "normative", "mixed", "assumption")
    assert "verdict" in r and "validity" in r["verdict"]


def test_build_argument_needs_input():
    r = ap.build_argument()
    assert r["ok"] is False and "error" in r


def test_build_argument_persists_and_status_roundtrip():
    r = ap.build_argument(claim="X 成立", text="因为 A, 所以 X 成立")
    aid = r["argument_id"]
    s = ap.status(aid)
    assert s["ok"] and s["argument_id"] == aid
    assert s["claim"] and s["n_premises"] >= 1


# --------------------------------------------------------------------------- #
# 凭据 / 隐私
# --------------------------------------------------------------------------- #
def test_build_argument_drops_credential_paths():
    r = ap.build_argument(
        claim="应该备份它",
        text="因为 ~/.gmail_creds.json 很关键, 所以应该备份它")
    assert r["ok"]
    row = ap._get_argument(r["argument_id"], ap._db_path())
    import json
    blob = json.dumps(row["graph"], ensure_ascii=False)
    assert ".gmail_creds.json" not in blob          # 凭据路径绝不入库
    assert row["private"] == 1                        # 含凭据 → 标私密


def test_credential_in_claim_also_scrubbed():
    r = ap.build_argument(claim="读取 /Users/x/.localkey", text="因为需要它")
    row = ap._get_argument(r["argument_id"], ap._db_path())
    assert ".localkey" not in (row["claim"] or "")


def test_private_content_not_flagged_when_switch_off_default(monkeypatch):
    """数据层隐私总开关默认关闭(User 2026-07-08 放弃数据层隐私): 即便 privacy_stream 判定
    私密, _classify_private/_text_private/_redact 恒返回"非私密/原文", 不打标记不脱敏,
    敏感数字原样入库(本模块无对外出口, 红线4 由 autopilot 审批闸独立守卫)。"""
    class FakePS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True, "hits": [{"name": "wage"}]}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"privacy_stream": FakePS()}))
    monkeypatch.setattr(ap.config, "get", lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    r = ap.build_argument(claim="该跳槽", text="因为我的工资是 12345 元, 所以该跳槽")
    assert r.get("_private") is None
    assert r.get("_force_local") is None
    row = ap._get_argument(r["argument_id"], ap._db_path())
    import json
    blob = json.dumps(row["graph"], ensure_ascii=False)
    assert "12345" in blob                             # 开关关闭 → 不脱敏, 原文入库


def test_private_content_flagged_and_redacted_when_switch_on(monkeypatch):
    """数据层隐私总开关打开(可逆, 恢复旧行为): 判私密 → 输出带 _private/_force_local,
    敏感数字脱敏后才落库。"""
    class FakePS:
        @staticmethod
        def should_force_local(text, **k):
            return {"private": True, "force_local": True, "hits": [{"name": "wage"}]}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"privacy_stream": FakePS()}))

    def _on(k, d=None):
        if k == "safety.data_layer_privacy_enabled":
            return True
        if k == "safety.allow_cloud_private":
            return False
        return d
    monkeypatch.setattr(ap.config, "get", _on)
    r = ap.build_argument(claim="该跳槽", text="因为我的工资是 12345 元, 所以该跳槽")
    assert r.get("_private") is True
    assert r.get("_force_local") is True
    row = ap._get_argument(r["argument_id"], ap._db_path())
    import json
    blob = json.dumps(row["graph"], ensure_ascii=False)
    assert "12345" not in blob                        # 敏感数字脱敏后才落库


def test_is_secret_path_local_regex_floor_not_bypassed(monkeypatch):
    """MEMORY jarvis-secret-path-dual-layer: 隐私库说 False 也不能绕过本地正则。"""
    class FakeAW:
        @staticmethod
        def is_secret_path(p):
            return False  # 库误判为非机密
    monkeypatch.setattr(ap, "_opt", _mk_opt({"awareness_sensors": FakeAW()}))
    assert ap._is_secret_path("~/.gmail_creds.json") is True    # 本地正则仍兜住
    assert ap._is_secret_path("/x/id_rsa") is True
    assert ap._is_secret_path("/tmp/normal.txt") is False


def test_is_secret_path_library_adds(monkeypatch):
    """隐私库可在本地正则之上**追加**命中(普通路径被库判机密 → True)。"""
    class FakeAW:
        @staticmethod
        def is_secret_path(p):
            return "secret_dir" in p
    monkeypatch.setattr(ap, "_opt", _mk_opt({"awareness_sensors": FakeAW()}))
    assert ap._is_secret_path("/home/secret_dir/x") is True


# --------------------------------------------------------------------------- #
# 逻辑缺陷检测器
# --------------------------------------------------------------------------- #
def test_detect_circular_reasoning():
    r = ap.check_validity(claim="他可信", text="因为他可信, 所以他可信")
    # 循环论证是致命缺陷 → verdict 不能判成立, 且 blocking_flaws 里应含循环
    assert r["verdict"]["validity"] in ("weak", "invalid")
    names = [f["name"] for f in (r.get("blocking_flaws") or [])]
    assert any("循环" in n for n in names)
    full = ap.fallacy_scan(claim="他可信", text="因为他可信, 所以他可信")
    assert any("循环" in f["name"] for f in full["fallacies"])


def test_detect_gap_missing_step():
    r = ap.find_gaps(claim="这文件归到税务类", text="因为文件名里有红色")
    assert r["ok"]
    assert any(f["type"] == "gap" for f in r["gaps"])


def test_detect_unproven_premise():
    r = ap.find_gaps(claim="X", text="因为 Y 一定为真, 所以 X")
    assert any(f["type"] == "unproven_premise" for f in r["unproven_premises"])


def test_detect_value_fact_is_ought():
    r = ap.fallacy_scan(claim="我们应该涨价", text="数据显示成本上升了, 所以我们应该涨价")
    assert any(f["type"] == "value_fact" for f in r["fallacies"])


def test_detect_corr_not_cause():
    r = ap.fallacy_scan(claim="冰淇淋导致溺水",
                        text="数据显示冰淇淋销量和溺水人数同时上升, 冰淇淋导致溺水")
    assert any(f["type"] == "corr_not_cause" for f in r["fallacies"])


def test_detect_causal_reversal_with_world_model(monkeypatch):
    class FakeWM:
        @staticmethod
        def causal_chain(cause, effect, *, depth=4):
            if "效率低" in cause and "加班" in effect:    # 反向更强
                return {"found": True, "chains": [{"confidence": 0.7}]}
            if "加班" in cause and "效率低" in effect:
                return {"found": True, "chains": [{"confidence": 0.2}]}
            return {"found": False, "chains": []}
        @staticmethod
        def explain(target):
            return {"found": False, "because": [], "contradicts": []}
        @staticmethod
        def relationship(a, b):
            return {"direct": []}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"world_model": FakeWM()}))
    r = ap.fallacy_scan(claim="加班导致效率低", text="因为团队最近常加班")
    assert any(f["type"] == "causal_reversal" for f in r["fallacies"])


def test_fallacy_catalogue_slippery_and_dilemma():
    r = ap.fallacy_scan(
        claim="不能批假",
        text="要么所有人都不准请假, 要么公司就乱了; 一旦开了这个口子早晚会失控")
    ids = {f["type"] for f in r["fallacies"]}
    assert "false_dilemma" in ids
    assert "slippery_slope" in ids


def test_fallacy_appeal_to_authority():
    r = ap.fallacy_scan(claim="该这么做", text="因为专家说这么做肯定对, 所以该这么做")
    assert any(f["type"] == "appeal_authority" for f in r["fallacies"])


def test_equivocation_compound_drift():
    r = ap.fallacy_scan(claim="所以应该支持自由市场",
                        text="因为言论自由是基本权利, 所以应该支持自由市场")
    assert any(f["type"] == "equivocation" for f in r["fallacies"])


def test_equivocation_skips_common_noun():
    r = ap.fallacy_scan(claim="这个文件该归财务", text="因为文件名里有发票")
    assert not any(f["type"] == "equivocation" for f in r["fallacies"])


# --------------------------------------------------------------------------- #
# 分析动作形态
# --------------------------------------------------------------------------- #
def test_premises_classifies_and_lists():
    r = ap.premises(claim="该涨价", text="因为成本涨了, 所以该涨价")
    assert r["ok"] and r["premises"]
    assert all("kind" in p and "status" in p for p in r["premises"])


def test_inference_chain_shape():
    r = ap.inference_chain(claim="X", text="因为 A 而且 B, 所以 X")
    assert r["ok"] and r["rules"] and "edges" in r


def test_check_validity_verdict():
    r = ap.check_validity(claim="X", text="因为 A, 所以 X")
    assert r["ok"] and r["verdict"]["validity"] in (
        "valid", "invalid", "strong", "moderate", "weak", "unsupported")


def test_minimal_proof_strips_digression():
    r = ap.minimal_proof(
        claim="该买", text="因为市盈率低, 而且行业增长, 顺便提一句今天天气不错, 所以该买")
    lb = " ".join(x["text"] for x in r["load_bearing_premises"])
    rd = " ".join(x["text"] for x in r["redundant_premises"])
    assert "天气" in rd and "天气" not in lb


def test_counterargument_has_attacks_and_steelman():
    r = ap.counterargument(claim="该上新功能", text="因为竞品都有了, 所以该上新功能")
    assert r["ok"] and "steelman_opposite" in r and "attacks" in r


def test_strengthen_gives_improvements():
    r = ap.strengthen(claim="一定要做", text="因为很重要, 所以一定要做")
    assert r["ok"] and r["improvements"]


def test_explain_logic_full_shape():
    r = ap.explain_logic(claim="该跳槽", text="因为工资低, 所以该跳槽")
    assert r["ok"]
    for k in ("headline", "chain", "verdict", "key_assumptions", "confidence"):
        assert k in r


def test_analysis_actions_accept_stored_id():
    b = ap.build_argument(claim="X", text="因为 A, 所以 X")
    aid = b["argument_id"]
    for fn in (ap.premises, ap.inference_chain, ap.check_validity, ap.find_gaps,
               ap.fallacy_scan, ap.minimal_proof, ap.counterargument,
               ap.strengthen, ap.explain_logic):
        r = fn(argument_id=aid)
        assert r["ok"], fn.__name__


def test_unknown_argument_id_errors():
    r = ap.check_validity(argument_id="arg_doesnotexist")
    assert r["ok"] is False and "error" in r


def test_analysis_needs_some_input():
    r = ap.premises()
    assert r["ok"] is False


# --------------------------------------------------------------------------- #
# handoff — 落地动作必经审批队列, 绝不执行
# --------------------------------------------------------------------------- #
def test_handoff_memo_no_side_effect():
    b = ap.build_argument(claim="X", text="因为 A, 所以 X")
    r = ap.handoff(argument_id=b["argument_id"], note="just a memo")
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

    b = ap.build_argument(claim="X", text="因为 A, 所以 X")
    monkeypatch.setattr(ap, "_opt", _mk_opt({"local_powers": FakeLP(),
                                             "autopilot_store": FakeAps()}))
    r = ap.handoff(argument_id=b["argument_id"], action="curl pay.example")
    assert r["needs_confirm"] is True
    assert r["queued_approval_id"] == 7777
    assert enq["risk"] == "high"
    assert r["red_line"] == "可能触发真实付费"


def test_handoff_failsafe_when_queue_unreachable(monkeypatch):
    # autopilot_store 不可达 → 不执行, needs_confirm=True, 无 queued id
    monkeypatch.setattr(ap, "_opt", _mk_opt({}))
    b = ap.build_argument(claim="X", text="因为 A, 所以 X")
    r = ap.handoff(argument_id=b["argument_id"], action="rm -rf /tmp/x")
    assert r["ok"] and r["needs_confirm"] is True and r["queued_approval_id"] is None


# --------------------------------------------------------------------------- #
# dispatch 白名单 / 工具面 / status / health / deferred
# --------------------------------------------------------------------------- #
def test_dispatch_whitelist_readonly_only():
    for blocked in ("build_argument", "handoff", "audit"):
        assert ap.dispatch(blocked, {}).get("ok") is False
    assert ap.dispatch("status", {}).get("ok") is True
    assert ap.dispatch("check_validity", {"claim": "X", "text": "因为A, 所以X"}).get("ok") is True


def test_tool_names_match_whitelist():
    names = {t["function"]["name"] for t in ap.approved_tools()}
    expected = {"argument_prover_" + v for v in ap._DISPATCH_WHITELIST}
    assert names == expected
    for n in names:
        assert ap.is_tool(n)
    assert ap.is_tool("argument_prover_build_argument") is False


def test_approved_tools_respects_config(monkeypatch):
    monkeypatch.setattr(ap.config, "get",
                        lambda k, d=None: False if k == "argument_prover.enabled" else d)
    assert ap.approved_tools() == []


def test_status_overview_canonical():
    s = ap.status()
    assert s["ok"] and "engines" in s and "deferred" in s
    assert len(s["actions"]) == 13


def test_health_ok():
    h = ap.health()
    assert h["ok"] is True and h["detail"]


def test_audit_credentials_zero_and_guarantees():
    ap.build_argument(claim="X", text="因为 A, 所以 X")
    a = ap.audit()
    assert a["ok"] and a["credential_paths_stored"] == 0
    assert len(a["guarantees"]) >= 5


def test_deferred_modules_recorded_not_stubbed():
    d = ap._deferred_status()
    for m in ("epistemic_core", "problem_framer", "value_reasoner", "metacognition_core"):
        assert d[m]["present"] is False and d[m]["deferred"] is True


# --------------------------------------------------------------------------- #
# 源码静态护栏(回归保护)
# --------------------------------------------------------------------------- #
def test_source_has_no_dangerous_calls():
    import ast
    src = open(ap.__file__, encoding="utf-8").read()
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
                if a.name.split(".")[0] in ap._FORBIDDEN_EXEC_MODULES:
                    bad.append("import " + a.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in ap._FORBIDDEN_EXEC_MODULES:
                bad.append("from " + (node.module or ""))
            self.generic_visit(node)

    _Scan().visit(tree)
    assert bad == [], "源码出现危险调用/执行面 import: " + str(bad)


def test_no_subprocess_import():
    import ast
    src = open(ap.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" not in imported


def test_selftest_passes():
    # selftest 内部自建临时库, 与本测试隔离无关
    assert ap._selftest() == 0


def test_module_compiles():
    import py_compile
    py_compile.compile(ap.__file__, doraise=True)


# --------------------------------------------------------------------------- #
# 对抗审查回归(adversarial review 确认的缺陷, 修复后锁死)
# --------------------------------------------------------------------------- #
def test_grounding_private_node_flagged_and_scrubbed(monkeypatch):
    """接地引入私密节点 → 输出标 _private/_force_local, 入库脱敏成占位(不落明文)。"""
    import json

    class FakeMG:
        @staticmethod
        def query(text, k=8, ntype=""):
            return {"nodes": [{"id": 1, "name": "张三的工资单编号88888888", "private": True}],
                    "edges": [{"src": 1, "dst": 2, "rtype": "supports"}], "_private": True}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"mind_graph": FakeMG()}))
    monkeypatch.setattr(ap.config, "get",
                        lambda k, d=None: False if k == "safety.allow_cloud_private" else d)
    r = ap.build_argument(claim="该加薪", text="因为业绩好, 所以该加薪")
    assert r.get("_private") is True and r.get("_force_local") is True
    row = ap._get_argument(r["argument_id"], ap._db_path())
    blob = json.dumps(row["graph"], ensure_ascii=False)
    assert "张三的工资单编号88888888" not in blob       # 私密节点名绝不落库
    assert row["private"] == 1


def test_grounding_credential_snippet_hard_blocked(monkeypatch):
    """接地引入凭据路径 → 硬拦丢弃, 不进 support, 不落库, 不上云。"""
    import json

    class FakeMG:
        @staticmethod
        def query(text, k=8, ntype=""):
            return {"nodes": [{"id": 1, "name": "/Users/x/.gmail_creds.json"}],
                    "edges": [{"src": 1, "dst": 2, "rtype": "supports"}]}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"mind_graph": FakeMG()}))
    r = ap.build_argument(claim="该备份", text="因为重要, 所以该备份")
    row = ap._get_argument(r["argument_id"], ap._db_path())
    blob = json.dumps(row["graph"], ensure_ascii=False)
    assert ".gmail_creds.json" not in blob               # 凭据绝不入库
    a = ap.audit()
    assert a["credential_paths_stored"] == 0


def test_grounding_resolves_names_not_ids(monkeypatch):
    class FakeMG:
        @staticmethod
        def query(text, k=8, ntype=""):
            return {"nodes": [{"id": 5, "name": "利润率高"}, {"id": 9, "name": "现金流稳"}],
                    "edges": [{"src": 5, "dst": 9, "rtype": "supports"}]}
    monkeypatch.setattr(ap, "_opt", _mk_opt({"mind_graph": FakeMG()}))
    r = ap.premises(claim="该投资", text="因为基本面好, 所以该投资")
    sup = [s for p in r["premises"] for s in p["support"]]
    assert any("利润率高" in s for s in sup)             # 用节点名
    assert not any(s.strip().isdigit() for s in sup)      # 不是裸 id


def test_non_string_input_no_crash():
    # LLM 可能传非字符串; 必须回 {ok:...} 而非抛异常
    assert ap.dispatch("check_validity", {"claim": 123, "text": None}).get("ok") in (True, False)
    assert ap.dispatch("status", {"argument_id": 999}).get("ok") in (True, False)
    assert ap.premises(claim=123, text=None).get("ok") in (True, False)


def test_status_handoff_audit_non_string_id():
    assert ap.status(argument_id=42)["ok"] in (True, False)
    assert ap.audit(argument_id=42)["ok"] is True
    assert ap.handoff(argument_id=42).get("ok") in (True, False)


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

    b = ap.build_argument(claim="X", text="因为 A, 所以 X")
    monkeypatch.setattr(ap, "_opt", _mk_opt({"local_powers": FakeLP(), "autopilot_store": BoomAps()}))
    r = ap.handoff(argument_id=b["argument_id"], action="发邮件给客户")
    assert r["needs_confirm"] is True          # 入队失败 → fail-closed, 绝不静默丢动作
    assert r["queued_approval_id"] is None


def test_regexes_no_redos():
    import time
    # _COND_RX / _REDACT_RX 都已长度有界; 超长输入不应回溯爆炸
    pathological = "如果" + "啊" * 40000 + "那么会出事, 所以要小心"
    t0 = time.time()
    r = ap.check_validity(claim="要小心", text=pathological)
    assert r["ok"] and (time.time() - t0) < 5
    longword = "a" * 60000 + " 所以该做"
    t0 = time.time()
    r2 = ap.build_argument(claim="该做", text=longword)
    assert r2["ok"] and (time.time() - t0) < 5


def test_surrogate_input_no_crash():
    r = ap.build_argument(claim="x\ud800y", text="因为 a\ud83d, 所以 x")
    assert r["ok"]                              # 孤立代理项不应炸 sqlite 写入


def test_output_deterministic_new_terms():
    r = ap.find_gaps(claim="这文件该归到税务发票报销凭证类", text="因为文件名里有红蓝绿字样")
    for g in r["gaps"]:
        if g.get("new_terms"):
            assert g["new_terms"] == sorted(g["new_terms"])   # 已排序 → 跨进程可复现


def test_is_ought_only_fatal_for_normative():
    # 应然结论(应该/必须)+ 全事实前提 → 致命「休谟之叉」
    norm = ap.fallacy_scan(claim="我们应该裁员", text="因为成本高, 所以我们应该裁员")
    assert any(f["name"] == "是→应该的跳跃(休谟之叉)" and f["severity"] == "high"
               for f in norm["fallacies"])
    # 经验性评价结论(更好)→ 不套致命休谟之叉, 只给较弱的「缺价值标准」提示
    val = ap.fallacy_scan(claim="这个方案更好", text="因为它更快, 所以这个方案更好")
    assert not any(f["name"] == "是→应该的跳跃(休谟之叉)" for f in val["fallacies"])


def test_status_engines_only_real_sources():
    s = ap.status()
    # _SOURCE_MODULES 只列真正被调用的来源, 不虚报 memory/semantic/context_brain
    assert set(["mind_graph", "world_model"]).issubset(set(s["engines"].keys()))
    assert "memory" not in s["engines"]
    assert "semantic" not in s["engines"]
