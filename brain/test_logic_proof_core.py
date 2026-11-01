# -*- coding: utf-8 -*-
"""logic_proof_core(形式逻辑推理审判器)测试。

覆盖: 论证拆解骨架 / 命题层有效性(4 类三段论) / 反模型搜索 / 7 类谬误检测 /
隐含假设 / 反例 / 证明轨迹 / 修补 / 缺口解释 / 隐私(私密标记·凭据脱敏·密钥硬拦) /
审计只存元数据 / dispatch 白名单只读 / deferred 内核 / world_model best-effort 冲突 /
安全护栏(AST 静态扫)。

用假引擎隔离, 审计目录指向 tmp(env 覆盖), 不碰真引擎/真磁盘/真 config。
跑: cd brain && python3 -m pytest test_logic_proof_core.py -v
"""
import ast
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config
import logic_proof_core as lpc


def _force_privacy_on(monkeypatch, *, isolate_privacy_stream=False):
    """数据层隐私总开关默认 OFF(2026-07-08 User 放弃); 用此把它翻回 ON,
    断言旧行为(私密标记/凭据脱敏/force_local)仍可逆地保留。

    isolate_privacy_stream=True: 额外让 _opt("privacy_stream") 返回 None, 逼 _privacy_assess
    落到模块自身的本地兜底路径(凭据正则硬判 force_local=True) —— 这样凭据类断言不再依赖
    User 本机 ~/.jarvis/config.toml 里 privacy.allow_cloud_high 的真实取值(该值已被 User
    显式授权为 True, 会让真实 privacy_stream.should_force_local 对高敏感内容也放行上云,
    与本测试想验证的"模块自身兜底"语义无关)。
    """
    _orig = config.get

    def _on(key, fallback=None):
        if key == "safety.data_layer_privacy_enabled":
            return True
        return _orig(key, fallback)

    monkeypatch.setattr(config, "get", _on)

    if isolate_privacy_stream:
        _orig_opt = lpc._opt

        def _opt_no_ps(name):
            if name == "privacy_stream":
                return None
            return _orig_opt(name)

        monkeypatch.setattr(lpc, "_opt", _opt_no_ps)


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """审计目录指向 tmp(autouse: 每个测试干净, 永不碰真 ~/.jarvis)。"""
    monkeypatch.setenv(lpc._HOME_ENV, str(tmp_path / "lpc_home"))
    yield


def _mk_opt(mapping):
    """返回一个 _opt 替身: 只认 mapping 里的模块, 其余 → None。"""
    return lambda name: mapping.get(name)


class FakeWM:
    """假 world_model: 命中 → 返回一条带 contradicts 的实体。"""
    @staticmethod
    def query(text, k=6, etype=""):
        return {"ok": True, "entities": [{"name": "地球是平的"}]}

    @staticmethod
    def explain(target=""):
        return {"ok": True, "contradicts": ["天文观测", "卫星影像"], "evidence": []}


class FakeAwareSecretFalse:
    """假 awareness_sensors: is_secret_path 恒 False —— 用来验证本地兜底不被短路。"""
    @staticmethod
    def is_secret_path(path):
        return False

    @staticmethod
    def classify_private(text="", app="", window=""):
        return False


# --------------------------------------------------------------------------- #
# 基础 / selftest / 编译
# --------------------------------------------------------------------------- #
def test_module_compiles():
    import py_compile
    py_compile.compile(lpc.__file__, doraise=True)


def test_selftest_passes(monkeypatch):
    """_selftest() 内部断言私密标记/凭据脱敏(旧行为); 数据层隐私默认已 OFF,
    故翻回 ON 验证该行为仍可逆保留(而非当作新 bug)。凭据断言额外隔离 privacy_stream,
    见 _force_privacy_on 说明(不依赖 User 本机真实 allow_cloud_high 取值)。"""
    _force_privacy_on(monkeypatch, isolate_privacy_stream=True)
    assert lpc._selftest() == 0


def test_status_lists_actions_and_deferred():
    st = lpc.status()
    assert st["ok"] is True
    for a in ("parse_argument", "check_validity", "fallacy_scan", "countermodel",
              "proof_trace", "repair_reasoning", "explain_gap", "audit", "status"):
        assert a in st["actions"]
    # 四个尚未落地的认知内核必须登记为 deferred
    for core in ("reasoning_core", "epistemic_core", "problem_framer", "metacognition_core"):
        assert st["deferred"][core]["deferred"] is True
    assert "engines" in st


# --------------------------------------------------------------------------- #
# 论证拆解
# --------------------------------------------------------------------------- #
def test_parse_argument_skeleton():
    r = lpc.parse_argument("因为今天降温，所以要多穿衣服")
    assert r["ok"] is True
    assert r["premises"]
    assert "多穿衣服" in r["conclusion"]
    # 结论引入前提没有的内容 → 隐含桥接假设
    kinds = [a["kind"] for a in r["implicit_assumptions"]]
    assert "missing_bridge" in kinds


def test_parse_conditional_kept_intact():
    """条件句不能被逗号拆散(否则蕴含抽不出来)。"""
    r = lpc.parse_argument("如果下雨，地就会湿；现在下雨了；所以地是湿的")
    assert any("如果下雨" in c for c in r["conditions"])


def test_extract_premises():
    r = lpc.extract_premises("因为A并且B，所以C")
    assert r["ok"] is True
    assert r["premises"]
    assert "C" in r["conclusion"]


def test_empty_input_rejected():
    for fn in (lpc.parse_argument, lpc.check_validity, lpc.fallacy_scan,
               lpc.countermodel, lpc.proof_trace, lpc.explain_gap):
        assert fn("")["ok"] is False
        assert fn("   ")["ok"] is False


# --------------------------------------------------------------------------- #
# 命题层有效性 + 反模型(4 类三段论)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,valid,form_key", [
    ("如果下雨，地就会湿；现在下雨了；所以地是湿的", True, "modus_ponens"),
    ("如果下雨，地就会湿；地是湿的；所以下雨了", False, "affirming_the_consequent"),
    ("如果下雨，地就会湿；地没有湿；所以没有下雨", True, "modus_tollens"),
    ("如果下雨，地就会湿；没有下雨；所以地没有湿", False, "denying_the_antecedent"),
    ("如果A则B；A；所以B", True, "modus_ponens"),
    ("如果A则B；B；所以A", False, "affirming_the_consequent"),
])
def test_validity_syllogisms(text, valid, form_key):
    r = lpc.check_validity(text, with_facts=False)
    assert r["ok"] is True
    assert r["decidable"] is True
    assert r["valid"] is valid
    assert form_key in r["form"]


def test_invalid_has_countermodel():
    r = lpc.check_validity("如果下雨，地就会湿；地是湿的；所以下雨了", with_facts=False)
    assert r["valid"] is False
    assert r["countermodel"]                 # 反模型存在
    cm = lpc.countermodel("如果下雨，地就会湿；地是湿的；所以下雨了")
    assert cm["found"] is True and cm["decidable"] is True


def test_valid_has_no_countermodel():
    cm = lpc.countermodel("如果A则B；A；所以B")
    assert cm["found"] is False and cm["decidable"] is True


def test_informal_argument_indeterminate():
    """抽不出命题结构 → decidable=False, 退结构启发式(不瞎判 valid/invalid)。"""
    r = lpc.check_validity("这个方案不错，团队也挺努力的", with_facts=False)
    assert r["decidable"] is False
    assert r["valid"] is None


# --------------------------------------------------------------------------- #
# 谬误扫描(7 类命名 + 线索词)
# --------------------------------------------------------------------------- #
def _codes(r):
    return [f["code"] for f in r["fallacies"]]


def test_fallacy_circular():
    r = lpc.fallacy_scan("因为他值得信任，所以他值得信任", with_facts=False)
    assert "circular_reasoning" in _codes(r)


def test_fallacy_hasty_generalization():
    r = lpc.fallacy_scan("我见过一只天鹅是白的，所以所有天鹅都是白的", with_facts=False)
    assert "hasty_generalization" in _codes(r)


def test_fallacy_affirming_consequent():
    r = lpc.fallacy_scan("如果下雨，地就会湿；地是湿的；所以下雨了", with_facts=False)
    assert "affirming_consequent" in _codes(r)


def test_fallacy_insufficient_premises():
    r = lpc.fallacy_scan("因为价格便宜，所以这个供应商的质量和售后都最好", with_facts=False)
    assert "insufficient_premises" in _codes(r)


def test_fallacy_cue_words():
    r = lpc.fallacy_scan("专家说这个方案最好，所以我们必须用它", with_facts=False)
    assert "appeal_to_authority" in _codes(r)
    r2 = lpc.fallacy_scan("要么听我的，要么项目就完蛋", with_facts=False)
    assert "false_dilemma" in _codes(r2)


def test_fallacy_dedup_and_sorted():
    r = lpc.fallacy_scan("因为他值得信任，所以他值得信任", with_facts=False)
    codes = _codes(r)
    assert len(codes) == len(set(codes))     # 同 code 去重
    confs = [f["confidence"] for f in r["fallacies"]]
    assert confs == sorted(confs, reverse=True)   # 按置信降序


# --------------------------------------------------------------------------- #
# world_model best-effort: 与已知事实冲突
# --------------------------------------------------------------------------- #
def test_conflicts_known_facts_with_fake_wm(monkeypatch):
    monkeypatch.setattr(lpc, "_opt", _mk_opt({"world_model": FakeWM}))
    r = lpc.fallacy_scan("综上，地球是平的", with_facts=True)
    assert "conflicts_known_facts" in _codes(r)
    assert r["fact_conflict"]["ok"] is True
    assert r["fact_conflict"]["conflict_count"] >= 1


def test_fact_check_deferred_when_wm_absent(monkeypatch):
    monkeypatch.setattr(lpc, "_opt", _mk_opt({}))   # 无任何引擎
    r = lpc.check_validity("如果A则B；A；所以B")
    assert r["fact_conflict"].get("deferred") == "world_model"


# --------------------------------------------------------------------------- #
# 反例 / 证明轨迹 / 修补 / 缺口
# --------------------------------------------------------------------------- #
def test_counterexample_universal():
    r = lpc.counterexample("所有供应商都会拖延交期")
    kinds = [c["kind"] for c in r["counterexamples"]]
    assert "universal_counterexample" in kinds


def test_proof_trace_structure():
    r = lpc.proof_trace("如果下雨，地就会湿；下雨了；所以地湿了")
    assert r["ok"] is True
    assert r["trace"][-1]["kind"] == "conclusion"
    assert r["valid"] is True


def test_repair_reasoning_suggests_fix():
    r = lpc.repair_reasoning("我见过一只天鹅是白的，所以所有天鹅都是白的", with_facts=False)
    codes = [x["code"] for x in r["repairs"]]
    assert "hasty_generalization" in codes


def test_explain_gap_points_to_weakest_link():
    r = lpc.explain_gap("如果下雨，地就会湿；地是湿的；所以下雨了", with_facts=False)
    assert r["gap"]["gap"] == "formal_invalid"
    assert r["gap"]["countermodel"]


# --------------------------------------------------------------------------- #
# 隐私: 私密标记 / 凭据脱敏 / 密钥硬拦(双层)
# --------------------------------------------------------------------------- #
def test_private_content_flagged_default_off():
    """数据层隐私总开关默认 OFF(2026-07-08 User 放弃): 私密内容不再占位/标记,
    原样返回(本模块无对外出口; 红线4 由 autopilot 审批闸独立守卫)。"""
    r = lpc.parse_argument("因为他的工资是 25000 元，所以他买得起这套房")
    assert r.get("_private") is None
    assert any("25000" in p for p in r.get("premises", []))  # 原文未占位


def test_private_content_flagged_switch_on(monkeypatch):
    """旧行为可逆保留: 把总开关翻回 ON, 私密内容仍应标 _private。"""
    _force_privacy_on(monkeypatch)
    r = lpc.parse_argument("因为他的工资是 25000 元，所以他买得起这套房")
    assert r.get("_private") is True


def test_credential_not_scrubbed_default_off():
    """默认 OFF: 凭据串不再脱敏, 也不强制本机(数据层不阉割; 出口另有 L3 守卫)。"""
    r = lpc.parse_argument("因为 api_key=sk-ABCDEF0123456789ABCD 有效，所以请求会成功")
    import json
    body = json.dumps(r, ensure_ascii=False)
    assert "sk-ABCDEF0123456789ABCD" in body
    assert r.get("_force_local") is None


def test_credential_scrubbed_and_force_local_switch_on(monkeypatch):
    """旧行为可逆保留: 翻回 ON, 凭据串仍应脱敏 + 强制本机。隔离 privacy_stream(见
    _force_privacy_on 说明) —— 否则本测试断言会受 User 本机真实
    ~/.jarvis/config.toml 的 privacy.allow_cloud_high=True 影响而失真。"""
    _force_privacy_on(monkeypatch, isolate_privacy_stream=True)
    r = lpc.parse_argument("因为 api_key=sk-ABCDEF0123456789ABCD 有效，所以请求会成功")
    import json
    body = json.dumps(r, ensure_ascii=False)
    assert "sk-ABCDEF0123456789ABCD" not in body      # 凭据脱敏
    assert r.get("_force_local") is True              # 强制本机


def test_secret_path_hard_block_dual_layer(monkeypatch):
    """评审铁律: 库级判 False 也不放过 —— 本地正则永远兜底。"""
    monkeypatch.setattr(lpc, "_opt", _mk_opt({"awareness_sensors": FakeAwareSecretFalse}))
    assert lpc._is_secret_path("/Users/x/.gmail_creds.json") is True
    assert lpc._is_secret_path("/Users/x/.gcal_creds.json") is True
    assert lpc._is_secret_path("~/.ssh/id_rsa") is True
    assert lpc._is_secret_path("/tmp/normal.txt") is False


def test_privacy_local_fallback_no_engines_default_off(monkeypatch):
    """数据层隐私总开关默认 OFF: 即便凭据串命中, 引擎缺席也恒非私密/非强制本机
    (本模块无对外出口; 判定短路在开关处, 不再进本地兜底)。"""
    monkeypatch.setattr(lpc, "_opt", _mk_opt({}))
    a = lpc._privacy_assess("token=sk-ABCDEF0123456789ABCD")
    assert a["private"] is False and a["force_local"] is False


def test_privacy_local_fallback_no_engines_switch_on(monkeypatch):
    """旧行为可逆保留: 翻回 ON 后, privacy_stream/awareness 都不可达时,
    凭据串仍按高敏感强制本机(fail-safe)。"""
    _force_privacy_on(monkeypatch)
    monkeypatch.setattr(lpc, "_opt", _mk_opt({}))
    a = lpc._privacy_assess("token=sk-ABCDEF0123456789ABCD")
    assert a["private"] is True and a["force_local"] is True


# --------------------------------------------------------------------------- #
# 审计: 只存元数据, 凭据/正文字段计数恒 0
# --------------------------------------------------------------------------- #
def test_audit_metadata_only():
    lpc.parse_argument("因为 password: hunter2secret123，所以能登录")
    lpc.check_validity("如果A则B；A；所以B", with_facts=False)
    lpc.fallacy_scan("因为他值得信任，所以他值得信任", with_facts=False)
    au = lpc.audit()
    assert au["ok"] is True
    assert au["self_check"]["credential_strings_in_audit"] == 0
    assert au["self_check"]["unexpected_body_fields"] == 0
    assert au["counts"]["records"] >= 3


# --------------------------------------------------------------------------- #
# dispatch 白名单(只读); audit CLI-only
# --------------------------------------------------------------------------- #
def test_dispatch_allows_analysis():
    r = lpc.dispatch("logic_proof_core_check_validity", {"text": "如果A则B；A；所以B"})
    assert r["ok"] is True


def test_dispatch_rejects_audit():
    r = lpc.dispatch("logic_proof_core_audit", {})
    assert r["ok"] is False


def test_dispatch_rejects_unknown():
    r = lpc.dispatch("logic_proof_core_execute", {"text": "x"})
    assert r["ok"] is False


def test_audit_not_in_whitelist():
    assert "audit" not in lpc._DISPATCH_WHITELIST


def test_approved_tools_shape():
    tools = lpc.approved_tools()
    assert isinstance(tools, list) and tools
    names = {t["function"]["name"] for t in tools}
    assert "logic_proof_core_check_validity" in names
    for n in names:
        assert lpc.is_tool(n)


# --------------------------------------------------------------------------- #
# 安全护栏: AST 静态扫(无执行面 import / 无危险调用 / 无 confirmed=True)
# --------------------------------------------------------------------------- #
def test_source_has_no_dangerous_calls():
    """与模块内 _selftest 一致: 跳过 _selftest 自身(它合法地用 tempfile/rmtree 清理自造 tmp)。"""
    src = open(lpc.__file__, encoding="utf-8").read()
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
                if a.name.split(".")[0] in lpc._FORBIDDEN_EXEC_MODULES:
                    bad.append("import " + a.name)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in lpc._FORBIDDEN_EXEC_MODULES:
                bad.append("from " + (node.module or ""))
            self.generic_visit(node)

    _Scan().visit(tree)
    assert bad == [], "源码出现危险调用/执行面 import: " + str(bad)


def test_no_real_side_effects_on_analysis(monkeypatch, tmp_path):
    """分析动作不得创建审计目录之外的任何文件/不得改 cwd。"""
    before = set(os.listdir(tmp_path))
    lpc.parse_argument("因为A，所以B")
    # 仅允许在 lpc_home 下产生审计文件
    after = set(os.listdir(tmp_path))
    assert after - before <= {"lpc_home"}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
