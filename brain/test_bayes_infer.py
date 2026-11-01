# -*- coding: utf-8 -*-
"""test_bayes_infer.py — 贝叶斯概率推断与信息价值核心的回归测试。

覆盖: 模型编译/校验、变量消元精确推断(贝叶斯定理手算对照)、后验/预测、信息价值(互信息/EVPI)、
敏感性、最大期望效用决策(含落地入审批闸)、共轭先验更新、假设比较、隐私护栏(凭据硬拦/私密不落库)、
deferred 内核记录不建桩、LLM 白名单只读、AST 安全静态护栏。

运行: cd brain && python3 -m pytest test_bayes_infer.py -v
"""
import os
import sys
import importlib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import bayes_infer as bi  # noqa: E402
import config  # noqa: E402 — 用于翻转 safety.data_layer_privacy_enabled 双态覆盖


def _force_privacy_switch_on(monkeypatch):
    """把 config.get("safety.data_layer_privacy_enabled", ...) 打成 True(其余 key 照旧透传),
    用于测试数据层隐私总开关打开时的旧行为(可逆, 见 bayes_infer._classify_private 文档)。"""
    _orig = config.get

    def _on(key, fallback=None):
        return True if key == "safety.data_layer_privacy_enabled" else _orig(key, fallback)

    monkeypatch.setattr(config, "get", _on)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个测试一套独立 tmp 库, 绝不碰真实库/真实副作用。"""
    monkeypatch.setenv(bi._HOME_ENV, str(tmp_path / "bi"))
    yield


# --------------------------------------------------------------------------- #
# 测试夹具: 经典网络
# --------------------------------------------------------------------------- #
def _disease_net():
    """疾病/检测网络。基率 1%, 灵敏 90%, 假阳 5%。后验可手算。"""
    spec = {"name": "disease", "variables": {
        "Disease": {"states": ["pos", "neg"], "parents": [],
                    "cpt": {"[]": [0.01, 0.99]}},
        "Test": {"states": ["pos", "neg"], "parents": ["Disease"],
                 "cpt": {"[\"pos\"]": [0.9, 0.1], "[\"neg\"]": [0.05, 0.95]}},
    }}
    return bi.build_model(spec=spec)


def _rain_net():
    spec = {"name": "rain", "variables": {
        "Rain": {"states": ["yes", "no"], "parents": [], "cpt": {"[]": [0.2, 0.8]}},
        "Wet": {"states": ["yes", "no"], "parents": ["Rain"],
                "cpt": {"[\"yes\"]": [0.9, 0.1], "[\"no\"]": [0.2, 0.8]}},
    }}
    return bi.build_model(spec=spec)


def _mk_opt(mapping):
    """返回一个 _opt 替身: 只认 mapping 里的模块, 其余 → None。"""
    return lambda name: mapping.get(name)


# --------------------------------------------------------------------------- #
# build_model / 校验
# --------------------------------------------------------------------------- #
def test_build_model_ok():
    r = _disease_net()
    assert r["ok"] and r["model_id"].startswith("bn_")
    assert set(r["variables"]) == {"Disease", "Test"}
    assert r["n_vars"] == 2


def test_build_model_empty_rejected():
    r = bi.build_model(spec={"variables": {}})
    assert r["ok"] is False


def test_build_model_rejects_cycle():
    spec = {"variables": {
        "A": {"states": ["t", "f"], "parents": ["B"], "cpt": {"[\"t\"]": [.5, .5], "[\"f\"]": [.5, .5]}},
        "B": {"states": ["t", "f"], "parents": ["A"], "cpt": {"[\"t\"]": [.5, .5], "[\"f\"]": [.5, .5]}},
    }}
    r = bi.build_model(spec=spec)
    assert r["ok"] is False and "环" in r["error"]


def test_build_model_rejects_missing_cpt_row():
    spec = {"variables": {
        "D": {"states": ["pos", "neg"], "parents": [], "cpt": {"[]": [.01, .99]}},
        "T": {"states": ["pos", "neg"], "parents": ["D"], "cpt": {"[\"pos\"]": [.9, .1]}},  # 缺 neg 行
    }}
    r = bi.build_model(spec=spec)
    assert r["ok"] is False and "缺少父取值组合" in r["error"]


def test_cpt_distribution_normalized():
    # 未归一的分布应被归一
    spec = {"variables": {"X": {"states": ["a", "b"], "parents": [], "cpt": {"[]": [3, 1]}}}}
    r = bi.build_model(spec=spec)
    m = bi._load_model(r["model_id"], bi._db_path())
    assert abs(sum(m["variables"]["X"]["cpt"]["[]"]) - 1.0) < 1e-12
    assert abs(m["variables"]["X"]["cpt"]["[]"][0] - 0.75) < 1e-12


# --------------------------------------------------------------------------- #
# 推断正确性(贝叶斯定理手算对照)
# --------------------------------------------------------------------------- #
def test_infer_marginal_exact():
    mid = _disease_net()["model_id"]
    r = bi.infer(mid, "Test")
    assert r["ok"]
    assert abs(r["posterior"]["pos"] - 0.0585) < 1e-9   # .01*.9 + .99*.05
    assert abs(sum(r["posterior"].values()) - 1.0) < 1e-12


def test_infer_posterior_base_rate_exact():
    mid = _disease_net()["model_id"]
    r = bi.infer(mid, "Disease", given={"Test": "pos"})
    assert r["ok"]
    # .01*.9 / .0585 = .15384615...
    assert abs(r["posterior"]["pos"] - 0.15384615) < 1e-6
    assert r["map_state"] == "neg"           # 即便检测阳性, 因基率极低仍更可能阴性
    assert 0.0 <= r["posterior"]["pos"] <= 1.0


def test_infer_rain_exact():
    mid = _rain_net()["model_id"]
    r = bi.infer(mid, "Rain", given={"Wet": "yes"})
    assert abs(r["posterior"]["yes"] - 0.52941176) < 1e-6   # .18/.34


def test_infer_unknown_var_rejected():
    mid = _disease_net()["model_id"]
    assert bi.infer(mid, "Nope")["ok"] is False


def test_infer_contradictory_evidence():
    # 给一个状态不存在的证据应被忽略(不报错); 真矛盾由零概率分支处理
    mid = _disease_net()["model_id"]
    r = bi.infer(mid, "Disease", given={"Test": "不存在状态"})
    assert r["ok"]   # 非法状态被忽略 → 退化为无证据边缘


# --------------------------------------------------------------------------- #
# posterior 联合
# --------------------------------------------------------------------------- #
def test_posterior_joint_sums_to_one():
    mid = _disease_net()["model_id"]
    r = bi.posterior(mid, query=["Disease", "Test"])
    assert r["ok"]
    total = sum(c["prob"] for c in r["joint"])
    assert abs(total - 1.0) < 1e-9
    assert r["n_cells"] == 4


# --------------------------------------------------------------------------- #
# predict (what-if)
# --------------------------------------------------------------------------- #
def test_predict_whatif_lifts_probability():
    mid = _disease_net()["model_id"]
    r = bi.predict(mid, "Disease", assume={"Test": "pos"})
    assert r["ok"]
    assert r["predictive"]["pos"] > 0.01            # 抬升 vs 先验 1%
    assert r["delta_vs_no_assumption"]["pos"] > 0


# --------------------------------------------------------------------------- #
# explain_probability
# --------------------------------------------------------------------------- #
def test_explain_probability_evidence_contribution():
    mid = _disease_net()["model_id"]
    bi.observe(mid, "Test", "pos")
    r = bi.explain_probability(mid, "Disease", state="pos")
    assert r["ok"]
    assert r["dominant_evidence"] == "Test"
    assert r["evidence_lift"] > 0                    # 阳性检测把 pos 概率抬升
    assert "Test" not in r["causal_ancestors"]       # Test 是 Disease 的子, 非祖先
    assert r["direct_parents"] == []                 # Disease 是根


# --------------------------------------------------------------------------- #
# value_of_information
# --------------------------------------------------------------------------- #
def test_voi_mutual_information_nonnegative():
    mid = _disease_net()["model_id"]
    r = bi.value_of_information(mid, "Disease")
    assert r["ok"] and r["ranking"]
    assert r["best_to_observe"] == "Test"
    for item in r["ranking"]:
        assert item["info_gain_bits"] >= -1e-12       # 互信息恒 ≥ 0


def test_voi_evpi_with_decision():
    mid = _disease_net()["model_id"]
    dec = {"outcome_var": "Disease",
           "actions": {"treat": {"pos": 0, "neg": -1}, "notreat": {"pos": -10, "neg": 0}}}
    r = bi.value_of_information(mid, "Disease", decision=dec)
    assert r["ok"]
    assert "EVPI" in r["metric"]
    # 观测 Test 能改善决策 → EVPI(Test) > 0
    test_item = next(i for i in r["ranking"] if i["observe"] == "Test")
    assert test_item["evpi"] >= 0


# --------------------------------------------------------------------------- #
# sensitivity
# --------------------------------------------------------------------------- #
def test_sensitivity_ranks_influential_params():
    mid = _disease_net()["model_id"]
    bi.observe(mid, "Test", "pos")
    r = bi.sensitivity(mid, "Disease", state="pos")
    assert r["ok"] and r["most_influential"]
    # 至少一个参数对后验有非零影响
    assert abs(r["most_influential"][0]["target_shift"]) > 0


# --------------------------------------------------------------------------- #
# decision_under_uncertainty (含落地审批闸)
# --------------------------------------------------------------------------- #
def test_decision_max_expected_utility():
    mid = _disease_net()["model_id"]
    dec = {"outcome_var": "Disease",
           "actions": {"treat": {"pos": 0, "neg": -1}, "notreat": {"pos": -10, "neg": 0}}}
    r0 = bi.decision_under_uncertainty(mid, decision=dec)
    assert r0["ok"] and r0["recommended_action"] == "notreat"   # 基率低 → 不治
    bi.observe(mid, "Test", "pos")
    r1 = bi.decision_under_uncertainty(mid, decision=dec)
    assert r1["recommended_action"] == "treat"                  # 阳性后 → 治


def test_decision_advisory_no_side_effect():
    mid = _disease_net()["model_id"]
    dec = {"outcome_var": "Disease", "actions": {"a": {"pos": 1}, "b": {"pos": 0}}}
    r = bi.decision_under_uncertainty(mid, decision=dec)   # 不 enqueue
    assert r["ok"] and r["queued_approval_id"] is None
    assert r["needs_confirm"] is False


def test_decision_landing_gated_to_approval_queue(monkeypatch):
    enq = []
    redline_seen = {}

    class FakeLP:
        @staticmethod
        def _check_red_line(t):
            redline_seen["text"] = t
            return None

        @staticmethod
        def non_shell_danger(t, kind=None):
            return None

    class FakeAps:
        @staticmethod
        def enqueue_approval(action, action_kind, payload, risk="high", why="", **k):
            enq.append({"action": action, "kind": action_kind, "risk": risk})
            return 4242

    mid = _disease_net()["model_id"]
    monkeypatch.setattr(bi, "_opt", _mk_opt({"local_powers": FakeLP(),
                                             "autopilot_store": FakeAps()}))
    dec = {"outcome_var": "Disease", "actions": {"treat": {"pos": 1}, "notreat": {"pos": 0}}}
    r = bi.decision_under_uncertainty(mid, decision=dec, enqueue=True, action="给患者用药")
    assert r["ok"]
    assert r["queued_approval_id"] == 4242        # 入审批队列回链
    assert r["needs_confirm"] is True             # 绝不自动执行
    assert len(enq) == 1 and enq[0]["risk"] == "high"
    assert redline_seen.get("text") == "给患者用药"   # 走了红线单一真相源


# --------------------------------------------------------------------------- #
# update_priors (共轭)
# --------------------------------------------------------------------------- #
def test_update_priors_directional():
    spec = {"variables": {"X": {"states": ["pos", "neg"], "parents": [], "cpt": {"[]": [0.2, 0.8]}}}}
    mid = bi.build_model(spec=spec)["model_id"]
    before = bi.infer(mid, "X")["posterior"]["pos"]
    up = bi.update_priors(mid, "X", counts={"pos": 30, "neg": 0})
    assert up["ok"] and up["method"] == "Beta-Bernoulli"
    after = bi.infer(mid, "X")["posterior"]["pos"]
    assert after > before                          # 多观测 pos → P(pos) 上升
    # 再观测 neg → 回落
    bi.update_priors(mid, "X", counts={"pos": 0, "neg": 50})
    assert bi.infer(mid, "X")["posterior"]["pos"] < after


def test_update_priors_dirichlet_multistate():
    spec = {"variables": {"C": {"states": ["a", "b", "c"], "parents": [],
                                "cpt": {"[]": [0.33, 0.33, 0.34]}}}}
    mid = bi.build_model(spec=spec)["model_id"]
    up = bi.update_priors(mid, "C", counts={"a": 100, "b": 0, "c": 0})
    assert up["ok"] and up["method"] == "Dirichlet-Categorical"
    assert bi.infer(mid, "C")["posterior"]["a"] > 0.5


def test_update_priors_child_requires_given():
    mid = _disease_net()["model_id"]
    r = bi.update_priors(mid, "Test", counts={"pos": 5, "neg": 5})
    assert r["ok"] is False and "父变量" in r["error"]


# --------------------------------------------------------------------------- #
# compare_hypotheses
# --------------------------------------------------------------------------- #
def test_compare_hypotheses_explicit():
    r = bi.compare_hypotheses(hypotheses=[{"name": "H1", "prior": 0.5, "likelihood": 0.9},
                                          {"name": "H2", "prior": 0.5, "likelihood": 0.1}])
    assert r["ok"] and r["best"] == "H1"
    assert abs(r["best_posterior"] - 0.9) < 1e-9
    assert abs(r["bayes_factor_top2"] - 9.0) < 1e-9


def test_compare_hypotheses_model_mode():
    mid = _disease_net()["model_id"]
    bi.observe(mid, "Test", "pos")
    r = bi.compare_hypotheses(mid, var="Disease")
    assert r["ok"] and r["mode"] == "model"
    assert set(r["posterior"].keys()) == {"pos", "neg"}


# --------------------------------------------------------------------------- #
# 隐私护栏
# --------------------------------------------------------------------------- #
def test_observe_blocks_credential_value():
    mid = _disease_net()["model_id"]
    r = bi.observe(mid, "Test", os.path.expanduser("~/.gmail_creds.json"))
    assert r["ok"] is False and "凭据" in r["error"]


def test_build_model_drops_credential_evidence():
    spec = {"variables": {"Y": {"states": ["a", "b"], "parents": [], "cpt": {"[]": [.5, .5]}}},
            "evidence": {".gmail_creds.json": "a"}}
    r = bi.build_model(spec=spec)
    assert r["ok"]
    assert ".gmail_creds.json" not in str(r["evidence"])


def test_private_note_body_not_stored_and_flagged(monkeypatch):
    spec = {"variables": {"Z": {"states": ["hi", "lo"], "parents": [], "cpt": {"[]": [.5, .5]},
                                "note": "本月工资 12345 元"}}}
    import json as _json

    # 新默认(数据层隐私总开关关闭, User 2026-07-08 放弃): 恒当非私密, 正文原样落库不占位
    mid = bi.build_model(spec=spec)["model_id"]
    m = bi._load_model(mid, bi._db_path())
    assert "12345" in _json.dumps(m, ensure_ascii=False)
    r = bi.infer(mid, "Z")
    assert not r.get("_private")

    # 开关打开(可逆): 完整恢复旧行为 —— 正文绝不落库 + 输出带 _private(默认切本机)
    _force_privacy_switch_on(monkeypatch)
    mid2 = bi.build_model(spec=spec)["model_id"]
    m2 = bi._load_model(mid2, bi._db_path())
    assert "12345" not in _json.dumps(m2, ensure_ascii=False)
    r2 = bi.infer(mid2, "Z")
    assert r2.get("_private") is True


def test_audit_zero_credentials():
    _disease_net()
    r = bi.audit()
    assert r["ok"] and r["credential_specs_stored"] == 0
    assert any("凭据" in g for g in r["guarantees"])


# --- 对抗审查回归(adversarial review 确认的 5 处凭据/隐私加固) --------------- #
def test_is_secret_path_regex_always_runs():
    """awareness 不命中也绝不短路本地 _SECRET_RX(id_rsa/PEM/client_secret 必拦)。"""
    for p in ["/home/u/.ssh/id_rsa", "-----BEGIN RSA PRIVATE KEY-----",
              "client_secret=abc", "deploy.pem", "refresh_token=x"]:
        assert bi._is_secret_path(p) is True, p


def test_build_model_rejects_credential_var_name():
    r = bi.build_model(spec={"variables": {
        os.path.expanduser("~/.gmail_creds.json"): {"states": ["a", "b"], "parents": [],
                                                    "cpt": {"[]": [.5, .5]}}}})
    assert r["ok"] is False and "凭据" in r["error"]


def test_build_model_rejects_credential_state():
    r = bi.build_model(spec={"variables": {
        "K": {"states": ["id_rsa", "ok"], "parents": [], "cpt": {"[]": [.5, .5]}}}})
    assert r["ok"] is False and "凭据" in r["error"]


def test_note_with_embedded_credential_not_stored():
    import json as _json
    r = bi.build_model(spec={"variables": {"V": {"states": ["a", "b"], "parents": [],
                                                 "cpt": {"[]": [.5, .5]},
                                                 "note": "密钥见 ~/.gmail_creds.json 文件"}}})
    m = bi._load_model(r["model_id"], bi._db_path())
    assert "gmail_creds" not in _json.dumps(m, ensure_ascii=False)
    assert bi.infer(r["model_id"], "V").get("_private") is True


def test_classify_private_failsafe_when_libs_absent(monkeypatch):
    monkeypatch.setattr(bi, "_opt", _mk_opt({}))      # 隐私库全不可达

    # 新默认(数据层隐私总开关关闭): fail-safe 语义已放弃 —— 恒 False, 与库是否可达无关
    assert bi._classify_private("某段普通正文") is False
    assert bi._looks_private("本月工资 99999") is False
    assert bi._is_secret_path("id_rsa") is True          # 凭据硬拦不受开关影响, 本地正则仍工作

    # 开关打开(可逆): 完整恢复旧 fail-safe 行为 —— 库不可达 + 有正文 → 当私密
    _force_privacy_switch_on(monkeypatch)
    assert bi._classify_private("某段普通正文") is True   # fail-safe 当私密
    assert bi._looks_private("本月工资 99999") is True
    assert bi._is_secret_path("id_rsa") is True          # 本地正则仍工作


def test_contradictory_evidence_with_observed_query_rejected():
    # 确定性网 B=copy(A); 观测 A=t,B=f 不可能。查询 A(本身被观测)也须 ok=False。
    det = {"variables": {
        "A": {"states": ["t", "f"], "parents": [], "cpt": {"[]": [.5, .5]}},
        "B": {"states": ["t", "f"], "parents": ["A"],
              "cpt": {"[\"t\"]": [1.0, 0.0], "[\"f\"]": [0.0, 1.0]}}}}
    mid = bi.build_model(spec=det)["model_id"]
    bad = bi.infer(mid, "A", given={"A": "t", "B": "f"})
    assert bad["ok"] is False                            # 矛盾证据 → 拒绝, 不返回 p_evidence=1.0
    good = bi.infer(mid, "A", given={"A": "t", "B": "t"})
    assert good["ok"] and good["posterior"]["t"] == 1.0  # 自洽的已观测查询仍正常


# --------------------------------------------------------------------------- #
# deferred 内核(记录不建桩)
# --------------------------------------------------------------------------- #
def test_deferred_modules_recorded_not_stubbed(monkeypatch):
    monkeypatch.setattr(bi, "_opt", _mk_opt({}))   # 全部不可用
    d = bi._deferred_status()
    for m in ("epistemic_core", "reasoning_core", "abduction_core", "metacognition_core"):
        assert d[m]["present"] is False and d[m]["deferred"] is True
        assert d[m]["interface"]


def test_deferred_cores_have_no_stub_files():
    # 「记录不建桩」铁律: bayes_infer 绝不为 deferred 认知内核创建**空壳 stub** .py
    # (空壳会让 _opt 返回 no-op 模块, 悄悄吞掉回写 → 静默数据丢失)。
    # 但设计明确欢迎「待其上线自动接上」—— 某内核独立**转正为真实模块**后合法存在
    # (如 epistemic_core 已在 73ea5dc 落地为 1369 行的信念账本, 有自己完整 API;
    # 它未实现 bayes_infer 约定的 record_belief, 故 writeback 仍 best-effort deferred, 安全)。
    # 判据 = 文件不存在(仍 deferred) 或 是 substantial 真实模块(非空壳), 只禁空壳 stub。
    _STUB_MAX_LINES = 40   # 空壳 stub 一般寥寥数行; 真实内核动辄数百~上千行
    for m in ("epistemic_core", "reasoning_core", "abduction_core", "metacognition_core"):
        p = os.path.join(_HERE, m + ".py")
        if not os.path.exists(p):
            continue   # 仍 deferred, 无 stub —— 符合约定
        with open(p, encoding="utf-8") as f:
            n = sum(1 for _ in f)
        assert n > _STUB_MAX_LINES, \
            f"绝不建桩: {m}.py 存在但仅 {n} 行, 疑似空壳 stub(真实内核应 substantial)"


# --------------------------------------------------------------------------- #
# LLM 工具面: 只读白名单
# --------------------------------------------------------------------------- #
def test_dispatch_whitelist_readonly_only():
    mid = _disease_net()["model_id"]
    for blocked in ("build_model", "observe", "update_priors", "audit"):
        assert bi.dispatch(blocked, {}).get("ok") is False
    assert bi.dispatch("status", {}).get("ok") is True
    assert bi.dispatch("infer", {"model_id": mid, "query": "Disease"}).get("ok") is True


def test_dispatch_decision_never_enqueues(monkeypatch):
    # 经 dispatch 的决策永不落地(强制 enqueue=False)
    called = {"enq": 0}

    class FakeAps:
        @staticmethod
        def enqueue_approval(**k):
            called["enq"] += 1
            return 1

    mid = _disease_net()["model_id"]
    monkeypatch.setattr(bi, "_opt", _mk_opt({"autopilot_store": FakeAps()}))
    dec = {"outcome_var": "Disease", "actions": {"a": {"pos": 1}, "b": {"pos": 0}}}
    r = bi.dispatch("decision_under_uncertainty", {"model_id": mid, "decision": dec})
    assert r["ok"] and r["queued_approval_id"] is None
    assert called["enq"] == 0


def test_tool_names_match_approved():
    names = [t["function"]["name"] for t in bi.approved_tools()]
    for n in names:
        assert bi.is_tool(n)
    assert bi.is_tool("bayes_infer_infer")
    assert not bi.is_tool("bayes_infer_build_model")   # 写库动作不暴露


# --------------------------------------------------------------------------- #
# status / health
# --------------------------------------------------------------------------- #
def test_status_and_health():
    _disease_net()
    s = bi.status()
    assert s["ok"] and "engines" in s and "deferred" in s
    assert s["counts"]["models"] >= 1
    assert set(s["actions"]) >= {"build_model", "observe", "infer", "posterior", "predict",
                                 "explain_probability", "value_of_information", "sensitivity",
                                 "decision_under_uncertainty", "update_priors",
                                 "compare_hypotheses", "audit", "status"}
    h = bi.health()
    assert h["ok"] is True and h["detail"]


# --------------------------------------------------------------------------- #
# 安全静态护栏(AST) + selftest
# --------------------------------------------------------------------------- #
def test_source_has_no_dangerous_calls():
    """静态护栏: 真正的危险调用形态(排除 docstring/注释里的提及词)。
    与模块内 _selftest 一致, 跳过 _selftest 自身函数体 —— 它是测试夹具, 合法地用
    tempfile.mkdtemp + shutil.rmtree 清理它**自己造的**临时目录。"""
    src = open(bi.__file__, encoding="utf-8").read()
    import ast
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
                if a.name.split(".")[0] in bi._FORBIDDEN_EXEC_MODULES:
                    bad.append("import " + a.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in bi._FORBIDDEN_EXEC_MODULES:
                bad.append("from " + (node.module or ""))
            self.generic_visit(node)

    _Scan().visit(tree)
    assert bad == [], "源码出现危险调用/执行面 import: " + str(bad)


def test_no_subprocess_import():
    src = open(bi.__file__, encoding="utf-8").read()
    import ast
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" not in imported


def test_selftest_passes(monkeypatch):
    # 新默认(数据层隐私总开关关闭): _selftest 内建的隐私脱敏/_private 断言不再成立(预期中,
    # 见模块文档"User 2026-06-24 放弃"), 但自检本身必须能跑通不崩溃, 且失败原因必须
    # 只限于隐私脱敏这一项 —— 若冒出别的失败项, 说明藏了真实回归, 让它炸出来。
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = bi._selftest()
    out = buf.getvalue()
    if rc != 0:
        stray = [ln for ln in out.splitlines()
                 if ln.startswith("FAIL:") and "私密" not in ln and "_private" not in ln
                 and "脱敏" not in ln]
        assert not stray, "开关关闭下 _selftest 出现非隐私类失败(疑似真实回归): " + str(stray)

    # 开关打开(可逆): 完整恢复旧行为, selftest 必须全过
    _force_privacy_switch_on(monkeypatch)
    assert bi._selftest() == 0


def test_module_compiles():
    import py_compile
    py_compile.compile(bi.__file__, doraise=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
