#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bayes_infer.py — 小美的「贝叶斯概率推断与信息价值核心」。

把小美从「会查、会因果定性」升级成**会在不确定下定量地想**: 把她的信念、证据、
因果机制、时间事件与业务变量, 编译成一张**可更新的概率图模型 / 贝叶斯网络**, 在其上做
精确推断(变量消元)、信念更新(共轭先验)、信息价值(VoI / 互信息)、敏感性分析与
不确定下决策(最大期望效用)。回答的不再是"是/否", 而是"多大概率、为什么这个概率、
还缺哪条证据最值得去查、先验该怎么改、哪个假设最可能"。

定位: 本模块是**纯只读·离线·确定性的概率推断门面**, 不自带执行引擎, 不重写任何安全门。
  · 推断引擎 100% 纯 stdlib(变量消元 + 共轭更新), 无 numpy/scipy 依赖, 结果确定可复现。
  · 接入 world_model(读 predict/causal_chain/impact/explain 的置信度→先验/似然/边)、
    mission_control(读 list_goals/plan_next/status 对齐目标; 落地动作经审批队列, 绝不直接推进)。
  · epistemic_core / reasoning_core / abduction_core / metacognition_core 尚未落地 →
    best-effort 软导入记 deferred(约定接口, 待其上线自动接上, **绝不建桩**)。

★★★ 安全铁律(与 simulation_lab / mission_control / experiment_core / shared_context 一致,
    自进化引擎不得改弱)★★★
  - 纯门面 + 零真实副作用: 本模块自身**绝不** subprocess / os.system / osascript / 删文件 /
    exec / eval / __import__。build_model / observe / update_priors 只写**自有库**(概率模型);
    decision_under_uncertainty 若要落地真动作, 一律 `autopilot_store.enqueue_approval(...)`
    入审批队列(risk=high), 经 autopilot 过 User 全部门, 本模块绝不执行。
  - 永不自放行: **绝不**对任何引擎传 confirmed=True。world_model / mission_control 只读其
    分析面(predict/causal_chain/impact/explain/list_goals/plan_next/status), **绝不**调其
    mutating 动作(extract/link/ingest/sync/advance/checkpoint)。
  - 红线单一真相源: 任何要落地的动作分级一律转发 `local_powers._check_red_line` /
    `non_shell_danger`; 本模块绝不复制红线表。
  - 隐私: 命中私密(file_tools.is_private_path / awareness 私密判定 / privacy_stream)的项
    **只留元数据, 正文/note 一律脱敏不落库**; 任何含私密内容的输出带 `_private`(必要时
    `_force_local`), 由 jarvis_brain 切本机模型收尾。**凭据/密钥文件
    (.gmail_creds.json/.gcal_creds.json/...)路径与正文一律硬拦, 绝不读取、绝不纳入模型。**
    隐私库不可达 → 一律按私密处理(fail-safe)。
  - LLM 工具面只读且冻结: 只暴露 infer / posterior / predict / explain_probability /
    value_of_information / sensitivity / decision_under_uncertainty / compare_hypotheses /
    status; 写库(build_model / observe / update_priors)与 audit 走 CLI。

全本机, 纯 stdlib sqlite3。自有库 ~/.jarvis/bayes_infer/bayes_infer.db, 不碰主库 jarvis.db。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402 — 纯读取, 无副作用


# --------------------------------------------------------------------------- #
# 路径(env 优先 → config → 默认; 函数式以便测试用 env 覆盖、调用时实时取)
# --------------------------------------------------------------------------- #
_HOME_ENV = "JARVIS_BAYES_INFER_HOME"   # 测试设它指向 tmp; 优先于 config


def _data_dir() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(
        config.get("bayes_infer.data_dir", "~/.jarvis/bayes_infer")))


def _db_path() -> str:
    return os.path.join(_data_dir(), "bayes_infer.db")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "bayes_infer.jsonl")


# --------------------------------------------------------------------------- #
# 软导入可选集成模块(importlib, 非 __import__/exec); 缺/出错 → None(降级, 永不抛)
# --------------------------------------------------------------------------- #
def _opt(name):
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None


# 尚未落地的认知内核 → 记 deferred, 绝不建桩。约定接口(待其上线自动接上):
#   epistemic_core.record_belief(payload)        ← 回写后验信念
#   reasoning_core.record_inference(payload)     ← 回写推断结论
#   abduction_core.propose_hypotheses(evidence)  ← 取候选假设(本模块只读消费)
#   metacognition_core.record_confidence(payload)← 回写不确定度/校准信号
_DEFERRED = ("epistemic_core", "reasoning_core", "abduction_core", "metacognition_core")
_AGREED_INTERFACE = {
    "epistemic_core": "record_belief(payload)",
    "reasoning_core": "record_inference(payload)",
    "abduction_core": "propose_hypotheses(evidence)",
    "metacognition_core": "record_confidence(payload)",
}
# 本模块只读接入的存在引擎(分析面)。
_SOURCE_MODULES = ("world_model", "mission_control", "privacy_stream",
                   "file_tools", "awareness_sensors", "autopilot_store", "local_powers")


def _deferred_status() -> dict:
    out = {}
    for m in _DEFERRED:
        out[m] = {"present": _opt(m) is not None, "deferred": True,
                  "interface": _AGREED_INTERFACE.get(m)}
    return out


def _engines_available() -> dict:
    out = {m: (_opt(m) is not None) for m in _SOURCE_MODULES}
    return out


# --------------------------------------------------------------------------- #
# 时间 / 输出 / 审计 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().date().isoformat()


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _params(kvs) -> dict:
    d = {}
    for kv in kvs or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k] = v
    return d


def _audit(action: str, model_id: str = "", **extra) -> None:
    """只记 ids/动作/计数/标志的审计流, **绝不写正文/隐私**, 永不抛。"""
    try:
        rec = {"ts": _now(), "action": action, "model_id": model_id or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        path = _audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _model_id(seed: str) -> str:
    import hashlib
    stamp = datetime.now().isoformat(timespec="microseconds")
    h = hashlib.sha1((seed + "|" + stamp).encode("utf-8")).hexdigest()[:12]
    return "bn_" + h


# --------------------------------------------------------------------------- #
# 隐私原语(复用 awareness_sensors / file_tools / privacy_stream 单一姿态;
#           不可达 → fail-safe 当私密)
# --------------------------------------------------------------------------- #
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".localkey", "config.toml",
})
_SECRET_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.localkey|/\.jarvis/config\.toml|"
    r"\.pem$|(^|/)id_rsa|(^|/)\.ssh/|refresh_token|client_secret|BEGIN [A-Z ]*PRIVATE KEY",
    re.IGNORECASE)


def _is_secret_path(path: str) -> bool:
    """凭据/密钥文件特征 → True(硬拦, 绝不纳入模型)。判不准当机密(fail-safe)。
    ★ 本地 _SECRET_RX 是单一兜底真相源, **始终运行**, 绝不被上游返回 False 短路;
      awareness_sensors 仅作 OR 补充(只能额外命中, 不能否决本地正则)。"""
    p = path or ""
    try:
        base = os.path.basename(os.path.expanduser(p))
        if base in _SECRET_BASENAMES or _SECRET_RX.search(p):
            return True
    except Exception:
        return True   # 判不准 → fail-safe 当机密
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "is_secret_path"):
        try:
            return bool(aw.is_secret_path(p))
        except Exception:
            return True
    return False


def _is_private_path(path: str) -> bool:
    """该文件是否私密(index.db private=1)。复用 file_tools 单一真相源; 不可达 → False。"""
    ft = _opt("file_tools")
    if ft is not None and hasattr(ft, "is_private_path"):
        try:
            return bool(ft.is_private_path(path))
        except Exception:
            return False
    return False


def _classify_private(text: str = "") -> bool:
    """文本是否私密。优先 privacy_stream(单一真相源), 退 awareness_sensors;
    都不可达且有正文 → fail-safe True。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密记录不占位、返原文
    (对外出口/凭证守卫不受此影响, 见 _is_secret_path / _is_private_path / _redact)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    ps = _opt("privacy_stream")
    if ps is not None and hasattr(ps, "should_force_local"):
        try:
            return bool(ps.should_force_local(text).get("private"))
        except Exception:
            pass
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "classify_private"):
        try:
            return bool(aw.classify_private(text, "", ""))
        except Exception:
            return True if text else False
    # 隐私库全不可达 + 有正文 → fail-safe 当私密(绝不把判不准的内容当公开)
    return True if text else False


def _redact(text: str) -> str:
    """脱敏(复用 awareness_sensors.redact; 不可达 → 抹凭据/长数字特征兜底)。"""
    s = text or ""
    if not s:
        return ""
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "redact"):
        try:
            return aw.redact(s)
        except Exception:
            pass
    s = _SECRET_RX.sub("[凭证已脱敏]", s)
    s = re.sub(r"\d[\d \-]{9,}\d", "[数字已脱敏]", s)
    return s


def _looks_private(text: str = "") -> bool:
    """元数据级私密判定: 命中机密/凭据特征 → True; 否则走内容分类器。"""
    t = text or ""
    if not t:
        return False
    if _is_secret_path(t) or _is_private_path(t):
        return True
    try:
        if _SECRET_RX.search(t):   # 正文内嵌凭据特征(如 "见 ~/.gmail_creds.json") → 私密
            return True
    except Exception:
        return True
    return _classify_private(t)


_PRIVATE_NOTE = "[私密备注, 正文仅本机保留, 不落库]"


def _flag_private(out: dict, any_private: bool) -> dict:
    if any_private:
        out["_private"] = True
        if not config.get("safety.allow_cloud_private", False):
            out["_force_local"] = True
    return out


# --------------------------------------------------------------------------- #
# 持久层: 自有库, 两张表(概率模型 / 推断·决策落地备忘)
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bi_models (
    model_id    TEXT PRIMARY KEY,
    ts_created  TEXT,
    ts_updated  TEXT,
    name        TEXT,
    spec        TEXT NOT NULL,     -- JSON: {variables, evidence, meta}
    private     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bi_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    model_id    TEXT,
    kind        TEXT,              -- decision|memo
    detail      TEXT,              -- JSON: 决策包(元数据级)
    queued_approval_id INTEGER
);
"""


def _conn(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# 概率引擎(纯 stdlib, 离散贝叶斯网络, 变量消元精确推断 — 确定可复现)
# --------------------------------------------------------------------------- #
_MAX_VARS = 40
_MAX_STATES = 16
_MAX_PARENTS = 8


def _normalize(vals) -> list:
    """把一组非负权重归一成分布; 全 0 / 非法 → 均匀。"""
    xs = [max(0.0, float(v)) for v in vals]
    s = sum(xs)
    if s <= 0:
        n = len(xs) or 1
        return [1.0 / n] * len(xs)
    return [x / s for x in xs]


def _cpt_key(passign) -> str:
    """父变量取值组合 → 规范 JSON key(父序固定, 与 variables[v]['parents'] 同序)。"""
    return json.dumps(list(passign), ensure_ascii=False)


def _parse_key(raw, parents, model) -> tuple:
    """把用户写的 CPT key 解析成规范父取值元组并校验。
    接受: JSON 列表串 / 逗号分隔串 / 空串或 '*'(根)。"""
    if not parents:
        return tuple()
    parsed = None
    if isinstance(raw, (list, tuple)):
        parsed = list(raw)
    else:
        s = str(raw).strip()
        if s in ("", "*", "[]"):
            parsed = []
        else:
            try:
                j = json.loads(s)
                if isinstance(j, list):
                    parsed = j
            except Exception:
                parsed = None
            if parsed is None:
                parsed = [x.strip() for x in s.split(",")]
    if parsed is None or len(parsed) != len(parents):
        raise ValueError("CPT key 与父变量数不符: " + str(raw)
                         + " (期望 " + str(len(parents)) + " 个父取值)")
    for i, p in enumerate(parents):
        if str(parsed[i]) not in model["variables"][p]["states"]:
            raise ValueError("CPT key 第 " + str(i) + " 个取值 '" + str(parsed[i])
                             + "' 不是父变量 " + p + " 的合法状态")
    return tuple(str(x) for x in parsed)


def _validate_model(model: dict) -> None:
    """结构 + 概率合法性 + 无环 + 规模上限; 不合法直接抛 ValueError。"""
    vs = model.get("variables")
    if not isinstance(vs, dict) or not vs:
        raise ValueError("模型缺少 variables")
    if len(vs) > _MAX_VARS:
        raise ValueError("变量数超过上限 " + str(_MAX_VARS))
    for v, spec in vs.items():
        states = spec.get("states")
        if not isinstance(states, list) or len(states) < 1:
            raise ValueError("变量 " + v + " 的 states 非法")
        if len(states) > _MAX_STATES:
            raise ValueError("变量 " + v + " 状态数超过上限 " + str(_MAX_STATES))
        if len(set(map(str, states))) != len(states):
            raise ValueError("变量 " + v + " 的 states 有重复")
        parents = spec.get("parents", [])
        if not isinstance(parents, list) or len(parents) > _MAX_PARENTS:
            raise ValueError("变量 " + v + " 的 parents 非法或超过上限 " + str(_MAX_PARENTS))
        for p in parents:
            if p not in vs:
                raise ValueError("变量 " + v + " 的父变量 " + str(p) + " 不存在")
    # 无环(DFS)
    color = {v: 0 for v in vs}  # 0 未访问 1 在栈 2 完成

    def _dfs(u):
        color[u] = 1
        for p in vs[u].get("parents", []):
            if color[p] == 1:
                raise ValueError("贝叶斯网络存在环, 经过变量 " + u)
            if color[p] == 0:
                _dfs(p)
        color[u] = 2

    for v in vs:
        if color[v] == 0:
            _dfs(v)
    # CPT 完整 + 归一
    for v, spec in vs.items():
        states = spec["states"]
        parents = spec.get("parents", [])
        cpt = spec.get("cpt", {})
        if not isinstance(cpt, dict):
            raise ValueError("变量 " + v + " 的 cpt 必须是对象")
        parent_state_lists = [vs[p]["states"] for p in parents]
        combos = list(itertools.product(*parent_state_lists)) if parents else [tuple()]
        if len(combos) * len(states) > 100000:
            raise ValueError("变量 " + v + " 的 CPT 规模过大")
        for combo in combos:
            key = _cpt_key(combo)
            dist = cpt.get(key)
            if dist is None:
                raise ValueError("变量 " + v + " 的 CPT 缺少父取值组合 " + key)
            if len(dist) != len(states):
                raise ValueError("变量 " + v + " 在 " + key + " 的分布长度与状态数不符")


def _canon_model(spec: dict) -> dict:
    """把用户 spec 规范化: CPT key → 规范 JSON, 分布 → 归一; 返回校验后的模型。"""
    raw_vs = spec.get("variables") or {}
    model = {"name": spec.get("name", ""), "variables": {},
             "evidence": dict(spec.get("evidence", {})), "meta": dict(spec.get("meta", {}))}
    # 先放进去(_parse_key 要查父 states)。变量名/状态命中凭据特征 → 硬拦(与 observe 一致)。
    for v, s in raw_vs.items():
        states = [str(x) for x in (s.get("states") or [])]
        if _is_secret_path(str(v)) or any(_is_secret_path(st) for st in states):
            raise ValueError("变量名/状态命中凭据·密钥特征, 已硬拦(绝不纳入模型): " + str(v)[:60])
        model["variables"][v] = {
            "states": states,
            "parents": list(s.get("parents", [])),
            "cpt": {}, "kind": s.get("kind", "belief"),
            "note": "", "private": bool(s.get("private", False)),
        }
    # 规范 CPT
    any_private = False
    for v, s in raw_vs.items():
        spec_v = model["variables"][v]
        parents = spec_v["parents"]
        raw_cpt = s.get("cpt", {}) or {}
        for raw_key, dist in raw_cpt.items():
            combo = _parse_key(raw_key, parents, model)
            spec_v["cpt"][_cpt_key(combo)] = _normalize(dist)
        # note 隐私: 私密 → 正文一律不落库(只留占位元数据); 否则截断存。
        note = str(s.get("note", "") or "")
        if note:
            if _looks_private(note):
                spec_v["private"] = True
                spec_v["note"] = _PRIVATE_NOTE
            else:
                spec_v["note"] = note[:240]
        if spec_v["private"]:
            any_private = True
        # hyper(共轭先验追踪, 仅根变量) 透传
        if "hyper" in s and not parents:
            spec_v["hyper"] = s["hyper"]
    # evidence 凭据硬拦 + 私密判定
    clean_ev = {}
    for ev_var, ev_state in (model["evidence"] or {}).items():
        if _is_secret_path(str(ev_var)) or _is_secret_path(str(ev_state)):
            continue  # 凭据特征 → 丢弃, 绝不纳入
        clean_ev[ev_var] = str(ev_state)
    model["evidence"] = clean_ev
    # 写时全量扫一次(含 var 名/状态/evidence), 结果落 meta; 读时只看 meta + spec.private(廉价)。
    model["meta"]["any_private"] = any_private or _scan_private(model)
    _validate_model(model)
    return model


def _scan_private(model: dict) -> bool:
    """全量私密扫描(var 名/状态/note/evidence) —— 只在写时(build/observe)调用一次, 会调隐私库。"""
    for ev_var, ev_state in (model.get("evidence") or {}).items():
        if _looks_private(str(ev_var)) or _looks_private(str(ev_state)):
            return True
    for v, spec in model["variables"].items():
        if spec.get("private") or _looks_private(str(v)):
            return True
        for st in spec.get("states", []):
            if _looks_private(str(st)):
                return True
    return False


def _model_private(model: dict) -> bool:
    """读时判定(廉价, 无隐私库调用): 信任写时算好的 meta.any_private + 各 spec.private。"""
    if bool((model.get("meta") or {}).get("any_private")):
        return True
    return any(spec.get("private") for spec in model["variables"].values())


# ---- 因子代数(变量消元) ---------------------------------------------------- #
def _factor(vars_, table) -> dict:
    return {"vars": list(vars_), "table": dict(table)}


def _cpt_factor(model: dict, var: str) -> dict:
    """P(var | parents) → 因子, 变量序 = [var] + parents。"""
    spec = model["variables"][var]
    states = spec["states"]
    parents = spec["parents"]
    cpt = spec["cpt"]
    vars_ = [var] + list(parents)
    parent_state_lists = [model["variables"][p]["states"] for p in parents]
    table = {}
    combos = list(itertools.product(*parent_state_lists)) if parents else [tuple()]
    for combo in combos:
        dist = _normalize(cpt[_cpt_key(combo)])
        for i, st in enumerate(states):
            table[(st,) + tuple(combo)] = dist[i]
    return _factor(vars_, table)


def _restrict(factor: dict, evidence: dict) -> dict:
    """把因子里出现的证据变量固定为观测值。"""
    ev = {v: s for v, s in evidence.items() if v in factor["vars"]}
    if not ev:
        return factor
    keep = [v for v in factor["vars"] if v not in ev]
    table = {}
    for assign, val in factor["table"].items():
        amap = dict(zip(factor["vars"], assign))
        if all(amap[v] == s for v, s in ev.items()):
            key = tuple(amap[v] for v in keep)
            table[key] = table.get(key, 0.0) + val
    return _factor(keep, table)


def _multiply(f1: dict, f2: dict) -> dict:
    """两个因子逐点相乘(并集变量)。"""
    vars_ = list(f1["vars"]) + [v for v in f2["vars"] if v not in f1["vars"]]
    shared = [v for v in f1["vars"] if v in f2["vars"]]
    table = {}
    f2items = [(dict(zip(f2["vars"], a2)), v2) for a2, v2 in f2["table"].items()]
    for a1, v1 in f1["table"].items():
        m1 = dict(zip(f1["vars"], a1))
        for m2, v2 in f2items:
            if any(m1[s] != m2[s] for s in shared):
                continue
            merged = dict(m1)
            merged.update(m2)
            key = tuple(merged[v] for v in vars_)
            table[key] = table.get(key, 0.0) + v1 * v2
    return _factor(vars_, table)


def _sum_out(factor: dict, var: str) -> dict:
    if var not in factor["vars"]:
        return factor
    keep = [v for v in factor["vars"] if v != var]
    table = {}
    for assign, val in factor["table"].items():
        m = dict(zip(factor["vars"], assign))
        key = tuple(m[v] for v in keep)
        table[key] = table.get(key, 0.0) + val
    return _factor(keep, table)


def _ve(model: dict, query_vars, evidence: dict) -> dict:
    """变量消元 → query_vars 上的(未归一)联合因子, 其总和 = P(evidence)。"""
    factors = []
    for var in model["variables"]:
        factors.append(_restrict(_cpt_factor(model, var), evidence))
    all_vars = list(model["variables"].keys())
    qset = set(query_vars)
    hidden = [v for v in all_vars if v not in qset and v not in evidence]
    # 简单 min-degree 近似: 按出现因子数升序消(只影响效率不影响结果)
    hidden.sort(key=lambda h: sum(1 for f in factors if h in f["vars"]))
    for h in hidden:
        involved = [f for f in factors if h in f["vars"]]
        rest = [f for f in factors if h not in f["vars"]]
        if not involved:
            factors = rest
            continue
        prod = involved[0]
        for f in involved[1:]:
            prod = _multiply(prod, f)
        factors = rest + [_sum_out(prod, h)]
    if not factors:
        return _factor([], {(): 1.0})
    prod = factors[0]
    for f in factors[1:]:
        prod = _multiply(prod, f)
    return prod


def _evidence_prob(model: dict, evidence: dict) -> float:
    """P(evidence): VE 消掉所有非证据变量, 余下常量因子之和。无证据 → 1.0。"""
    if not evidence:
        return 1.0
    joint = _ve(model, [], evidence)
    return sum(joint["table"].values())


def _marginal(model: dict, var: str, evidence: dict):
    """返回 (后验分布 {state: p}, P(evidence))。证据零概率(含矛盾)→ (None, 0.0)。"""
    if var not in model["variables"]:
        raise ValueError("查询变量不存在: " + str(var))
    states = model["variables"][var]["states"]
    if var in evidence:
        # 查询变量本身已被观测 → 后验退化; 但仍须校验整组证据自洽(P(e)>0), 否则矛盾。
        pe = _evidence_prob(model, evidence)
        if pe <= 0:
            return (None, 0.0)
        obs = evidence[var]
        return ({s: (1.0 if s == obs else 0.0) for s in states}, pe)
    joint = _ve(model, [var], evidence)
    f = joint
    for v in list(f["vars"]):
        if v != var:
            f = _sum_out(f, v)
    raw = {s: f["table"].get((s,), 0.0) for s in states}
    z = sum(raw.values())
    if z <= 0:
        return (None, 0.0)
    return ({s: raw[s] / z for s in states}, z)


def _joint_posterior(model: dict, query_vars, evidence: dict):
    """query_vars 上的联合后验 {assignment_tuple: p}; 证据零概率(含矛盾)→ (None, 0.0)。"""
    eff = [q for q in query_vars if q not in evidence]
    if not eff:
        # 全部 query 变量都已被观测 → 仍须校验整组证据自洽。
        pe = _evidence_prob(model, evidence)
        if pe <= 0:
            return (None, 0.0)
        joint = _factor([], {(): 1.0})
    else:
        joint = _ve(model, eff, evidence)
    total = sum(joint["table"].values())
    if total <= 0:
        return (None, 0.0)
    order = list(joint["vars"])
    post = {}
    for assign, val in joint["table"].items():
        m = dict(zip(order, assign))
        # 补上被证据固定的 query 变量
        full = tuple((qv, (m[qv] if qv in m else evidence[qv])) for qv in query_vars)
        post[full] = post.get(full, 0.0) + val / total
    return (post, total)


def _entropy_bits(dist: dict) -> float:
    h = 0.0
    for p in dist.values():
        if p > 0:
            h -= p * math.log(p, 2)
    return h


# --------------------------------------------------------------------------- #
# 模型存取
# --------------------------------------------------------------------------- #
def _save_model(model: dict, db_path: str, model_id: str = "") -> str:
    mid = model_id or _model_id(model.get("name", "bn"))
    priv = _model_private(model)
    conn = _conn(db_path)
    try:
        existing = conn.execute("SELECT ts_created FROM bi_models WHERE model_id=?",
                                (mid,)).fetchone()
        ts_created = existing["ts_created"] if existing else _now()
        conn.execute(
            "INSERT OR REPLACE INTO bi_models (model_id, ts_created, ts_updated, name, spec, private) "
            "VALUES (?,?,?,?,?,?)",
            (mid, ts_created, _now(), model.get("name", ""),
             json.dumps(model, ensure_ascii=False), 1 if priv else 0))
        conn.commit()
    finally:
        conn.close()
    return mid


def _load_model(model_id: str, db_path: str):
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT spec FROM bi_models WHERE model_id=?",
                           (model_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        return json.loads(row["spec"])
    except Exception:
        return None


def _list_models(db_path: str) -> list:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT model_id, name, ts_updated, private FROM bi_models "
            "ORDER BY ts_updated DESC").fetchall()
    finally:
        conn.close()
    return [{"model_id": r["model_id"], "name": r["name"], "updated": r["ts_updated"],
             "private": bool(r["private"])} for r in rows]


def _resolve_model(model_id: str, db_path: str):
    """给定 id → 载入; 空 id → 最近一个。返回 (model, mid) 或 (None, "")。"""
    if model_id:
        m = _load_model(model_id, db_path)
        return (m, model_id) if m else (None, "")
    lst = _list_models(db_path)
    if not lst:
        return (None, "")
    mid = lst[0]["model_id"]
    return (_load_model(mid, db_path), mid)


# --------------------------------------------------------------------------- #
# world_model 只读取信: 从 predict/causal_chain 派生二值变量与边(best-effort)
# --------------------------------------------------------------------------- #
def _seed_from_world_model(topic: str) -> dict:
    """读 world_model.predict(topic) 的预测置信度 → 各派生一个二值变量(发生/不发生),
    先验 = 预测置信; 只读, world_model 不可达 → 空 spec。绝不写回 world_model。"""
    spec = {"name": "wm:" + (topic or "")[:60], "variables": {}, "evidence": {}, "meta": {}}
    wm = _opt("world_model")
    if wm is None or not hasattr(wm, "predict"):
        spec["meta"]["world_model"] = "unavailable"
        return spec
    try:
        pr = wm.predict(topic)
    except Exception as e:
        spec["meta"]["world_model_error"] = str(e)[:160]
        return spec
    if not isinstance(pr, dict) or not pr.get("ok"):
        spec["meta"]["world_model"] = "no_prediction"
        return spec
    any_private = bool(pr.get("_private"))
    n = 0
    for i, p in enumerate(pr.get("predictions", []) or []):
        if n >= 12:
            break
        name = str(p.get("predicted", "") or "").strip()
        if not name or _is_secret_path(name):
            continue
        conf = float(p.get("confidence", 0.5) or 0.5)
        conf = min(0.95, max(0.05, conf))
        priv = bool(p.get("private"))
        vname = "P%d" % (i + 1)
        is_priv = bool(priv or _looks_private(name))
        if is_priv:
            any_private = True
        spec["variables"][vname] = {
            "states": ["发生", "不发生"], "parents": [],
            "cpt": {"[]": [conf, 1.0 - conf]},
            "kind": "mechanism", "private": is_priv,
            "note": (_PRIVATE_NOTE if is_priv else name[:120]),
        }
        n += 1
    spec["meta"]["any_private"] = any_private
    spec["meta"]["source"] = "world_model.predict"
    spec["meta"]["seeded"] = n
    return spec


def _mission_context() -> dict:
    """只读 mission_control 的目标态势(对齐决策), 绝不调 advance/checkpoint。"""
    out = {"available": False, "active_goals": []}
    mc = _opt("mission_control")
    if mc is None:
        return out
    out["available"] = True
    try:
        if hasattr(mc, "list_goals"):
            goals = mc.list_goals(status="active") or []
            out["active_goals"] = [{"goal_id": g.get("id"), "title": str(g.get("title", ""))[:80],
                                    "priority": g.get("priority")} for g in goals[:8]]
    except Exception:
        pass
    return out


def _writeback_deferred(payload: dict) -> dict:
    """best-effort 把后验/推断回写给尚未落地的认知内核; 缺则 deferred(不建桩)。"""
    written, deferred, details = [], [], {}
    plan = {
        "epistemic_core": "record_belief",
        "reasoning_core": "record_inference",
        "metacognition_core": "record_confidence",
    }
    for name, fn in plan.items():
        mod = _opt(name)
        if mod is None or not hasattr(mod, fn):
            deferred.append(name)
            continue
        try:
            getattr(mod, fn)(payload)
            written.append(name + "." + fn)
        except Exception as e:
            deferred.append(name)
            details[name + "_error"] = str(e)[:160]
    return {"written_back": written, "deferred": deferred, "details": details}


# --------------------------------------------------------------------------- #
# 1. build_model — 把信念/证据/机制/事件/业务变量编译成贝叶斯网络(CLI; 写自有库)
# --------------------------------------------------------------------------- #
def build_model(spec: dict | None = None, name: str = "", from_world_model: str = "",
                model_id: str = "", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    try:
        if from_world_model:
            seeded = _seed_from_world_model(from_world_model)
            base = dict(spec or {})
            merged_vars = dict(seeded.get("variables", {}))
            merged_vars.update((base.get("variables") or {}))
            raw = {"name": name or base.get("name") or seeded.get("name", ""),
                   "variables": merged_vars,
                   "evidence": base.get("evidence", {}),
                   "meta": {**seeded.get("meta", {}), **(base.get("meta", {}))}}
        else:
            raw = dict(spec or {})
            if name:
                raw["name"] = name
        if not (raw.get("variables")):
            return {"ok": False, "error": "模型为空: 需要 --spec 提供 variables, 或 --from-world-model 取种子。"}
        model = _canon_model(raw)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": "build_model 失败: " + str(e)}
    mid = _save_model(model, db, model_id=model_id)
    priv = _model_private(model)
    _audit("build_model", mid, n_vars=len(model["variables"]),
           n_evidence=len(model.get("evidence", {})), private=priv,
           from_world_model=bool(from_world_model))
    out = {"ok": True, "model_id": mid, "name": model.get("name", ""),
           "variables": list(model["variables"].keys()),
           "n_vars": len(model["variables"]),
           "evidence": model.get("evidence", {}),
           "note": "已编译为贝叶斯网络并存入自有库(纯本机, 不碰主库/不写回 world_model)。"}
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 2. observe — 记录观测证据(CLI; 写自有库; 凭据硬拦 + 私密脱敏)
# --------------------------------------------------------------------------- #
def observe(model_id: str = "", var: str = "", state: str = "",
            db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if _is_secret_path(str(var)) or _is_secret_path(str(state)):
        return {"ok": False, "error": "观测涉及凭据/密钥特征, 已硬拦(绝不纳入模型)。"}
    if var not in model["variables"]:
        return {"ok": False, "error": "变量不存在: " + str(var)}
    if str(state) not in model["variables"][var]["states"]:
        return {"ok": False, "error": "状态 '" + str(state) + "' 不是变量 " + var
                + " 的合法取值 " + str(model["variables"][var]["states"])}
    model.setdefault("evidence", {})[var] = str(state)
    priv_in = _looks_private(str(var)) or _looks_private(str(state))
    if priv_in:
        model.setdefault("meta", {})["any_private"] = True
    _save_model(model, db, model_id=mid)
    priv = _model_private(model)
    _audit("observe", mid, var=var, private=priv)
    out = {"ok": True, "model_id": mid, "observed": {var: str(state)},
           "evidence": model["evidence"],
           "note": "证据已记入自有库; 下次 infer/posterior 将基于它更新。"}
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 3. infer — 后验边缘 P(query | evidence)(只读; 变量消元精确推断)
# --------------------------------------------------------------------------- #
def infer(model_id: str = "", query: str = "", given: dict | None = None,
          db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not query:
        return {"ok": False, "error": "需要 query 变量名。"}
    if query not in model["variables"]:
        return {"ok": False, "error": "查询变量不存在: " + str(query)}
    evidence = dict(model.get("evidence", {}))
    if given:
        for k, v in given.items():
            if k in model["variables"] and str(v) in model["variables"][k]["states"]:
                evidence[k] = str(v)
    try:
        dist, pe = _marginal(model, query, evidence)
    except Exception as e:
        return {"ok": False, "error": "推断失败: " + str(e)}
    if dist is None:
        return {"ok": False, "error": "给定证据在该模型下概率为 0(矛盾证据), 无法定义后验。"}
    top = max(dist.items(), key=lambda kv: kv[1])
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "query": query, "given": evidence,
           "posterior": {k: round(v, 6) for k, v in dist.items()},
           "map_state": top[0], "map_prob": round(top[1], 6),
           "entropy_bits": round(_entropy_bits(dist), 4),
           "p_evidence": round(pe, 8),
           "method": "variable_elimination(exact)"}
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 4. posterior — 单变量或多变量联合后验(只读)
# --------------------------------------------------------------------------- #
def posterior(model_id: str = "", query=None, given: dict | None = None,
              db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    qvars = query if isinstance(query, list) else ([query] if query else [])
    qvars = [q for q in qvars if q]
    if not qvars:
        return {"ok": False, "error": "需要 query 变量(单个或列表)。"}
    for q in qvars:
        if q not in model["variables"]:
            return {"ok": False, "error": "查询变量不存在: " + str(q)}
    evidence = dict(model.get("evidence", {}))
    if given:
        for k, v in given.items():
            if k in model["variables"] and str(v) in model["variables"][k]["states"]:
                evidence[k] = str(v)
    try:
        if len(qvars) == 1:
            dist, pe = _marginal(model, qvars[0], evidence)
            if dist is None:
                return {"ok": False, "error": "矛盾证据, 后验无定义。"}
            joint = [{"assignment": {qvars[0]: s}, "prob": round(p, 6)}
                     for s, p in sorted(dist.items(), key=lambda kv: -kv[1])]
        else:
            post, pe = _joint_posterior(model, qvars, evidence)
            if post is None:
                return {"ok": False, "error": "矛盾证据, 后验无定义。"}
            joint = [{"assignment": dict(a), "prob": round(p, 6)}
                     for a, p in sorted(post.items(), key=lambda kv: -kv[1])]
    except Exception as e:
        return {"ok": False, "error": "推断失败: " + str(e)}
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "query": qvars, "given": evidence,
           "joint": joint[:64], "n_cells": len(joint),
           "p_evidence": round(pe, 8), "method": "variable_elimination(exact)"}
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 5. predict — 前向预测分布(只读; 接受 what-if 假设证据, 不落库)
# --------------------------------------------------------------------------- #
def predict(model_id: str = "", target: str = "", assume: dict | None = None,
            db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not target or target not in model["variables"]:
        return {"ok": False, "error": "需要合法 target 变量: " + str(target)}
    base_ev = dict(model.get("evidence", {}))
    whatif = dict(assume or {})
    ev = dict(base_ev)
    applied = {}
    for k, v in whatif.items():
        if k in model["variables"] and str(v) in model["variables"][k]["states"]:
            ev[k] = str(v)
            applied[k] = str(v)
    try:
        base_dist, _ = _marginal(model, target, base_ev)
        new_dist, pe = _marginal(model, target, ev)
    except Exception as e:
        return {"ok": False, "error": "预测失败: " + str(e)}
    if new_dist is None:
        return {"ok": False, "error": "假设证据与已知证据矛盾, 概率为 0。"}
    delta = None
    if base_dist is not None:
        delta = {s: round(new_dist.get(s, 0.0) - base_dist.get(s, 0.0), 6)
                 for s in model["variables"][target]["states"]}
    top = max(new_dist.items(), key=lambda kv: kv[1])
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "target": target,
           "assume": applied, "given": base_ev,
           "predictive": {k: round(v, 6) for k, v in new_dist.items()},
           "most_likely": top[0], "prob": round(top[1], 6),
           "delta_vs_no_assumption": delta,
           "note": "前向预测(假设证据仅本次生效, 不落库)。"}
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 6. explain_probability — 为什么是这个概率(主导父变量 + 各证据贡献 + 影响链)(只读)
# --------------------------------------------------------------------------- #
def explain_probability(model_id: str = "", query: str = "", state: str = "",
                        db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not query or query not in model["variables"]:
        return {"ok": False, "error": "需要合法 query 变量: " + str(query)}
    states = model["variables"][query]["states"]
    target_state = state if state in states else None
    evidence = dict(model.get("evidence", {}))
    try:
        post, _ = _marginal(model, query, evidence)
        prior, _ = _marginal(model, query, {})  # 无证据先验
    except Exception as e:
        return {"ok": False, "error": "解释失败: " + str(e)}
    if post is None:
        return {"ok": False, "error": "矛盾证据, 无法解释。"}
    if target_state is None:
        target_state = max(post.items(), key=lambda kv: kv[1])[0]

    # 各证据项的贡献: 逐一移除该证据, 看目标后验如何变化(留一法)
    contributions = []
    for ev_var in list(evidence.keys()):
        reduced = {k: v for k, v in evidence.items() if k != ev_var}
        try:
            d2, _ = _marginal(model, query, reduced)
        except Exception:
            d2 = None
        if d2 is None:
            continue
        shift = post.get(target_state, 0.0) - d2.get(target_state, 0.0)
        contributions.append({
            "evidence": ev_var, "observed": evidence[ev_var],
            "shift_on_target": round(shift, 6),
            "direction": ("提升" if shift > 1e-9 else ("降低" if shift < -1e-9 else "无影响")),
        })
    contributions.sort(key=lambda c: -abs(c["shift_on_target"]))

    # 结构影响链: 目标的祖先(父→...→根), 作为概率的机制来源
    parents = model["variables"][query]["parents"]
    ancestors = _ancestors(model, query)
    prior_p = prior.get(target_state, 0.0) if prior else None
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "query": query, "state": target_state,
           "posterior_prob": round(post.get(target_state, 0.0), 6),
           "prior_prob": (round(prior_p, 6) if prior_p is not None else None),
           "evidence_lift": (round(post.get(target_state, 0.0) - (prior_p or 0.0), 6)
                             if prior_p is not None else None),
           "direct_parents": parents,
           "causal_ancestors": ancestors,
           "evidence_contributions": contributions,
           "dominant_evidence": (contributions[0]["evidence"] if contributions else None),
           "note": ("后验由先验 + 各证据(留一法量化其对该状态的拉动)+ 结构父变量共同决定。"
                    "正贡献=该证据把目标概率往上拉。")}
    return _flag_private(out, priv)


def _ancestors(model: dict, var: str) -> list:
    seen, stack, order = set(), list(model["variables"][var]["parents"]), []
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        order.append(p)
        stack.extend(model["variables"][p]["parents"])
    return order


# --------------------------------------------------------------------------- #
# 7. value_of_information — 信息价值: 先查哪条证据最值得(只读)
#    默认: 关于 target 的互信息(熵减, bits, ≥0); 给决策则算 EVPI(期望完美信息价值)。
# --------------------------------------------------------------------------- #
def value_of_information(model_id: str = "", target: str = "", candidates=None,
                         decision: dict | None = None, db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not target or target not in model["variables"]:
        return {"ok": False, "error": "需要合法 target 变量: " + str(target)}
    evidence = dict(model.get("evidence", {}))
    obs = set(evidence.keys())
    cand = candidates or [v for v in model["variables"]
                          if v != target and v not in obs]
    cand = [c for c in cand if c in model["variables"] and c != target and c not in obs]
    try:
        base, _ = _marginal(model, target, evidence)
    except Exception as e:
        return {"ok": False, "error": "VoI 计算失败: " + str(e)}
    if base is None:
        return {"ok": False, "error": "矛盾证据, 无法计算 VoI。"}
    h0 = _entropy_bits(base)

    decision_mode = bool(decision and decision.get("actions") and decision.get("outcome_var"))
    base_eu = None
    if decision_mode:
        base_eu = _expected_utilities(model, decision, evidence)
        if not base_eu.get("ok"):
            return base_eu
    ranked = []
    for x in cand:
        try:
            px, _ = _marginal(model, x, evidence)
        except Exception:
            continue
        if px is None:
            continue
        exp_h = 0.0
        evpi_post = 0.0
        valid = True
        for xs, pxs in px.items():
            if pxs <= 0:
                continue
            ev2 = dict(evidence)
            ev2[x] = xs
            try:
                ht, _ = _marginal(model, target, ev2)
            except Exception:
                ht = None
            if ht is None:
                continue
            exp_h += pxs * _entropy_bits(ht)
            if decision_mode:
                eu2 = _expected_utilities(model, decision, ev2)
                if eu2.get("ok"):
                    evpi_post += pxs * eu2["best_eu"]
        mi = max(0.0, h0 - exp_h)
        item = {"observe": x, "info_gain_bits": round(mi, 5),
                "expected_posterior_entropy_bits": round(exp_h, 5)}
        if decision_mode and base_eu.get("ok"):
            item["evpi"] = round(max(0.0, evpi_post - base_eu["best_eu"]), 6)
        ranked.append(item)
    sort_key = "evpi" if decision_mode else "info_gain_bits"
    ranked.sort(key=lambda r: -r.get(sort_key, 0.0))
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "target": target,
           "current_entropy_bits": round(h0, 5),
           "metric": ("EVPI(期望完美信息价值, 效用单位)" if decision_mode
                      else "互信息(熵减, bits)"),
           "ranking": ranked,
           "best_to_observe": (ranked[0]["observe"] if ranked else None),
           "note": ("先查排第一的那个变量, 对降低 target 的不确定性(或改善决策)收益最大。"
                    " 信息增益恒 ≥ 0。")}
    if decision_mode:
        out["base_best_eu"] = round(base_eu["best_eu"], 6)
    return _flag_private(out, priv)


# --------------------------------------------------------------------------- #
# 8. sensitivity — 敏感性分析: 哪个先验/CPT 参数最影响目标后验(只读)
# --------------------------------------------------------------------------- #
def sensitivity(model_id: str = "", target: str = "", state: str = "",
                delta: float = 0.1, db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not target or target not in model["variables"]:
        return {"ok": False, "error": "需要合法 target 变量: " + str(target)}
    states = model["variables"][target]["states"]
    tstate = state if state in states else None
    evidence = dict(model.get("evidence", {}))
    try:
        base, _ = _marginal(model, target, evidence)
    except Exception as e:
        return {"ok": False, "error": "敏感性计算失败: " + str(e)}
    if base is None:
        return {"ok": False, "error": "矛盾证据, 无法分析。"}
    if tstate is None:
        tstate = max(base.items(), key=lambda kv: kv[1])[0]
    base_p = base.get(tstate, 0.0)
    try:
        d = float(delta)
    except Exception:
        d = 0.1
    d = min(0.45, max(0.01, abs(d)))

    influences = []
    for v, spec in model["variables"].items():
        for key, dist in spec["cpt"].items():
            if len(dist) < 2:
                continue
            for i in range(len(dist)):
                perturbed = _perturb(dist, i, d)
                cand = _clone_with_cpt(model, v, key, perturbed)
                try:
                    pd, _ = _marginal(cand, target, evidence)
                except Exception:
                    pd = None
                if pd is None:
                    continue
                dp = pd.get(tstate, 0.0) - base_p
                if abs(dp) < 1e-9:
                    continue
                influences.append({
                    "var": v, "cpt_row": key, "state_index": i,
                    "state": spec["states"][i],
                    "param_change": round(d, 4),
                    "target_shift": round(dp, 6),
                    "elasticity": round(dp / d, 6),
                })
    influences.sort(key=lambda x: -abs(x["target_shift"]))
    priv = _model_private(model)
    out = {"ok": True, "model_id": mid, "target": target, "state": tstate,
           "base_prob": round(base_p, 6), "delta": round(d, 4),
           "most_influential": influences[:12],
           "n_params_tested": sum(len(s["cpt"]) for s in model["variables"].values()),
           "note": ("把每个 CPT 参数各加 delta(其余按比例归一)看 target 后验怎么变。"
                    "target_shift 绝对值最大的参数, 是该结论最该被审视/最该去核实的。")}
    return _flag_private(out, priv)


def _perturb(dist: list, idx: int, d: float) -> list:
    """把分布第 idx 项加 d, 其余按原比例缩放, 再归一(保持非负, 和=1)。"""
    n = len(dist)
    cur = _normalize(dist)
    target = min(1.0, cur[idx] + d)
    remain = 1.0 - target
    others = [cur[j] for j in range(n) if j != idx]
    so = sum(others)
    new = [0.0] * n
    new[idx] = target
    for j in range(n):
        if j == idx:
            continue
        new[j] = (cur[j] / so * remain) if so > 0 else (remain / (n - 1) if n > 1 else 0.0)
    return _normalize(new)


def _clone_with_cpt(model: dict, var: str, key: str, new_dist: list) -> dict:
    import copy
    m = copy.deepcopy(model)
    m["variables"][var]["cpt"][key] = list(new_dist)
    return m


# --------------------------------------------------------------------------- #
# 9. decision_under_uncertainty — 不确定下决策(最大期望效用)(只读/顾问)
#    落地动作可选入审批队列(risk=high), 绝不执行、绝不 confirmed=True。
# --------------------------------------------------------------------------- #
def decision_under_uncertainty(model_id: str = "", decision: dict | None = None,
                               enqueue: bool = False, action: str = "",
                               db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not (decision and decision.get("actions") and decision.get("outcome_var")):
        return {"ok": False, "error": "需要 decision = {outcome_var, actions:{动作:{结果状态:效用}}}。"}
    evidence = dict(model.get("evidence", {}))
    eu = _expected_utilities(model, decision, evidence)
    if not eu.get("ok"):
        return eu
    mc = _mission_context()
    priv = _model_private(model)

    queued_id = None
    needs_confirm = False
    red = None
    note2 = "纯顾问输出(期望效用排序), 无任何外部副作用。"
    # 仅当显式要"落地某个真动作"时, 才走红线分级 + 入审批队列(绝不执行)。
    if enqueue and action:
        lp = _opt("local_powers")
        if lp is not None:
            try:
                if hasattr(lp, "_check_red_line"):
                    red = lp._check_red_line(action)
                if not red and hasattr(lp, "non_shell_danger"):
                    red = lp.non_shell_danger(action)
            except Exception:
                red = None
        aps = _opt("autopilot_store")
        bundle = {"model_id": mid, "recommended": eu["best_action"],
                  "expected_utility": eu["best_eu"], "outcome_var": decision["outcome_var"],
                  "posterior": eu["posterior"], "red_line": red}
        if aps is not None and hasattr(aps, "enqueue_approval"):
            try:
                queued_id = aps.enqueue_approval(
                    action=action, action_kind="decision",
                    payload=bundle, risk="high",
                    why="bayes_infer.decision_under_uncertainty 建议落地: " + str(action)[:120])
                needs_confirm = True
            except Exception:
                queued_id = None
                needs_confirm = True
        else:
            needs_confirm = True
        note2 = ("要落地的动作已入 autopilot 审批队列(risk=high), 绝不自动执行; "
                 "须 User 经 `jarvis autopilot approve <id>` 批准。"
                 if queued_id else
                 "autopilot 审批队列不可达 → 仅给建议, 绝不执行, 需人工接力。")
        conn = _conn(db)
        try:
            conn.execute(
                "INSERT INTO bi_links (ts, model_id, kind, detail, queued_approval_id) "
                "VALUES (?,?,?,?,?)",
                (_now(), mid, "decision", json.dumps(bundle, ensure_ascii=False), queued_id))
            conn.commit()
        finally:
            conn.close()
    _audit("decision_under_uncertainty", mid, best=eu["best_action"],
           enqueue=bool(enqueue and action), queued_approval_id=queued_id, private=priv)
    out = {"ok": True, "model_id": mid, "outcome_var": decision["outcome_var"],
           "posterior": eu["posterior"],
           "expected_utility": eu["expected_utility"],
           "recommended_action": eu["best_action"], "best_eu": round(eu["best_eu"], 6),
           "regret_if_wrong": eu.get("regret"),
           "aligned_goals": mc.get("active_goals", []),
           "queued_approval_id": queued_id, "needs_confirm": needs_confirm,
           "red_line": red, "note": note2}
    return _flag_private(out, priv)


def _expected_utilities(model: dict, decision: dict, evidence: dict) -> dict:
    """U(a) = Σ_s P(outcome=s|e)·utility(a,s); 返回每动作 EU + 最优。"""
    ovar = decision.get("outcome_var")
    if ovar not in model["variables"]:
        return {"ok": False, "error": "outcome_var 不存在: " + str(ovar)}
    try:
        post, _ = _marginal(model, ovar, evidence)
    except Exception as e:
        return {"ok": False, "error": "效用计算失败: " + str(e)}
    if post is None:
        return {"ok": False, "error": "矛盾证据, 效用无定义。"}
    actions = decision.get("actions", {})
    eus = {}
    for a, util in actions.items():
        s = 0.0
        for st, p in post.items():
            s += p * float((util or {}).get(st, 0.0))
        eus[a] = s
    if not eus:
        return {"ok": False, "error": "decision.actions 为空。"}
    best = max(eus.items(), key=lambda kv: kv[1])
    worst = min(eus.values())
    return {"ok": True, "posterior": {k: round(v, 6) for k, v in post.items()},
            "expected_utility": {k: round(v, 6) for k, v in eus.items()},
            "best_action": best[0], "best_eu": best[1],
            "regret": round(best[1] - worst, 6)}


# --------------------------------------------------------------------------- #
# 10. update_priors — 共轭先验更新(Beta-Bernoulli / Dirichlet-Categorical)(CLI; 写库)
# --------------------------------------------------------------------------- #
def update_priors(model_id: str = "", var: str = "", counts=None, given: str = "",
                  db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 先 build_model。"}
    if not var or var not in model["variables"]:
        return {"ok": False, "error": "需要合法 var: " + str(var)}
    spec = model["variables"][var]
    states = spec["states"]
    parents = spec["parents"]
    # 解析 counts → 与 states 对齐的列表
    cvec = _counts_vector(counts, states)
    if cvec is None:
        return {"ok": False, "error": "counts 需为 {状态:计数} 或与 states 等长的列表。"}
    # 选 CPT row
    try:
        if parents:
            combo = _parse_key(given, parents, model) if given else None
            if combo is None:
                return {"ok": False, "error": "该变量有父变量, 需用 --given 指定要更新的父取值行。"}
            key = _cpt_key(combo)
        else:
            key = _cpt_key(tuple())
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    prior_dist = _normalize(spec["cpt"].get(key, [1.0] * len(states)))
    # 取/初始化 Dirichlet 超参(Beta = 二态 Dirichlet 特例)
    hyper = spec.get("hyper") if not parents else None
    if isinstance(hyper, dict) and hyper.get("type") in ("beta", "dirichlet"):
        if hyper["type"] == "beta":
            alpha = [float(hyper.get("alpha", 1.0)), float(hyper.get("beta", 1.0))]
            if len(states) != 2:
                alpha = [max(1e-6, prior_dist[i] * len(states)) for i in range(len(states))]
        else:
            alpha = [float(a) for a in hyper.get("alpha", [1.0] * len(states))]
            if len(alpha) != len(states):
                alpha = [max(1e-6, prior_dist[i] * len(states)) for i in range(len(states))]
    else:
        # 用当前先验作弱伪计数(concentration = 状态数), 等价 Dirichlet(prior*K)
        kappa = float(len(states))
        alpha = [max(1e-6, prior_dist[i] * kappa) for i in range(len(states))]
    post_alpha = [alpha[i] + cvec[i] for i in range(len(states))]
    new_dist = _normalize(post_alpha)
    # 写回
    spec["cpt"][key] = new_dist
    if not parents:
        spec["hyper"] = {"type": ("beta" if len(states) == 2 else "dirichlet"),
                         **({"alpha": post_alpha[0], "beta": post_alpha[1]}
                            if len(states) == 2 else {"alpha": post_alpha})}
    _save_model(model, db, model_id=mid)
    priv = _model_private(model)
    _audit("update_priors", mid, var=var, n_obs=int(sum(cvec)), private=priv)
    out = {"ok": True, "model_id": mid, "var": var, "row": key,
           "prior": {states[i]: round(prior_dist[i], 6) for i in range(len(states))},
           "observed_counts": {states[i]: cvec[i] for i in range(len(states))},
           "posterior": {states[i]: round(new_dist[i], 6) for i in range(len(states))},
           "posterior_hyper": post_alpha,
           "method": ("Beta-Bernoulli" if len(states) == 2 else "Dirichlet-Categorical"),
           "note": "共轭更新: 后验 = Dirichlet(α + 计数), 已写回该 CPT 行。"}
    return _flag_private(out, priv)


def _counts_vector(counts, states):
    if counts is None:
        return None
    if isinstance(counts, dict):
        try:
            return [float(counts.get(s, 0)) for s in states]
        except Exception:
            return None
    if isinstance(counts, (list, tuple)):
        if len(counts) != len(states):
            return None
        try:
            return [float(x) for x in counts]
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# 11. compare_hypotheses — 贝叶斯模型比较(后验概率 + 贝叶斯因子)(只读)
#     模式 A: 显式 hypotheses=[{name,prior,likelihood}]; 模式 B: 模型里某假设变量 | evidence。
#     abduction_core 在场时可取候选假设(best-effort, 缺则 deferred)。
# --------------------------------------------------------------------------- #
def compare_hypotheses(model_id: str = "", hypotheses=None, var: str = "",
                       db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    # 模式 A: 显式假设清单
    if hypotheses:
        items = []
        for h in hypotheses:
            name = str(h.get("name", "")) or ("H%d" % (len(items) + 1))
            prior = max(0.0, float(h.get("prior", 1.0)))
            lik = max(0.0, float(h.get("likelihood", h.get("lik", 1.0))))
            items.append({"name": name, "prior": prior, "lik": lik,
                          "joint": prior * lik})
        z = sum(i["joint"] for i in items)
        if z <= 0:
            return {"ok": False, "error": "所有假设联合概率为 0, 无法比较。"}
        for i in items:
            i["posterior"] = i["joint"] / z
        items.sort(key=lambda i: -i["posterior"])
        bf = None
        if len(items) >= 2 and items[1]["lik"] > 0:
            bf = items[0]["lik"] / items[1]["lik"]
        out = {"ok": True, "mode": "explicit",
               "hypotheses": [{"name": i["name"], "prior": round(i["prior"], 6),
                               "likelihood": round(i["lik"], 6),
                               "posterior": round(i["posterior"], 6)} for i in items],
               "best": items[0]["name"], "best_posterior": round(items[0]["posterior"], 6),
               "bayes_factor_top2": (round(bf, 4) if bf is not None else None),
               "abduction": _abduction_status(),
               "note": "后验 ∝ 先验 × 似然; 贝叶斯因子 = 头两名似然比(>1 支持第一名)。"}
        return out
    # 模式 B: 模型里某假设变量的后验
    model, mid = _resolve_model(model_id, db)
    if model is None:
        return {"ok": False, "error": "没有可用模型; 或给 --hypotheses。"}
    if not var or var not in model["variables"]:
        return {"ok": False, "error": "需要合法假设变量 var, 或改用 --hypotheses。"}
    evidence = dict(model.get("evidence", {}))
    try:
        post, pe = _marginal(model, var, evidence)
        prior, _ = _marginal(model, var, {})
    except Exception as e:
        return {"ok": False, "error": "比较失败: " + str(e)}
    if post is None:
        return {"ok": False, "error": "矛盾证据, 无法比较。"}
    ranked = sorted(post.items(), key=lambda kv: -kv[1])
    bf = None
    if len(ranked) >= 2:
        p1, p2 = prior.get(ranked[0][0], 0.0), prior.get(ranked[1][0], 0.0)
        if p1 > 0 and p2 > 0 and ranked[1][1] > 0:
            bf = (ranked[0][1] / ranked[1][1]) / (p1 / p2)  # 后验几率 / 先验几率 = BF
    priv = _model_private(model)
    out = {"ok": True, "mode": "model", "model_id": mid, "var": var,
           "given": evidence,
           "posterior": {k: round(v, 6) for k, v in ranked},
           "prior": {k: round(prior.get(k, 0.0), 6) for k, _ in ranked},
           "best": ranked[0][0], "best_posterior": round(ranked[0][1], 6),
           "bayes_factor_top2": (round(bf, 4) if bf is not None else None),
           "p_evidence": round(pe, 8), "abduction": _abduction_status(),
           "note": "证据下各假设状态的后验; 贝叶斯因子 = 后验几率/先验几率(>1 支持第一名)。"}
    return _flag_private(out, priv)


def _abduction_status() -> dict:
    mod = _opt("abduction_core")
    return {"present": mod is not None, "deferred": mod is None,
            "interface": _AGREED_INTERFACE["abduction_core"]}


# --------------------------------------------------------------------------- #
# 12. audit — 隐私/安全自审(只读; CLI)
# --------------------------------------------------------------------------- #
def audit(db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    conn = _conn(db)
    try:
        n_models = conn.execute("SELECT COUNT(*) c FROM bi_models").fetchone()["c"]
        n_priv = conn.execute("SELECT COUNT(*) c FROM bi_models WHERE private=1").fetchone()["c"]
        n_links = conn.execute("SELECT COUNT(*) c FROM bi_links").fetchone()["c"]
        leaked = 0
        for r in conn.execute("SELECT spec FROM bi_models LIMIT 500").fetchall():
            if _SECRET_RX.search(r["spec"] or ""):
                leaked += 1
    finally:
        conn.close()
    return {"ok": True, "db": db,
            "counts": {"models": n_models, "private_models": n_priv, "decision_links": n_links},
            "credential_specs_stored": leaked,
            "guarantees": [
                "凭据/密钥文件路径与正文硬拦, 绝不纳入模型(build_model/observe 双重过滤)",
                "私密项 note 一律脱敏; 含私密的模型输出带 _private(默认切本机模型)",
                "纯推断门面: 无 subprocess/os.system/exec/删文件; 只写自有库",
                "落地动作经 autopilot 审批队列(risk=high), 从不 confirmed=True、从不执行",
                "world_model/mission_control 只读分析面, 绝不调其 mutating 动作",
            ],
            "note": ("credential_specs_stored 应恒为 0; 非 0 说明有凭据特征落库, 需排查。"
                     if leaked else "凭据零落库, 隐私护栏完好。")}


# --------------------------------------------------------------------------- #
# 13. status — 总览 + 各引擎可用性 + deferred(canonical; registry.doctor 用)
# --------------------------------------------------------------------------- #
_ACTIONS = ["build_model", "observe", "infer", "posterior", "predict",
            "explain_probability", "value_of_information", "sensitivity",
            "decision_under_uncertainty", "update_priors", "compare_hypotheses",
            "audit", "status"]


def status(db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    try:
        models = _list_models(db)
    except Exception:
        models = []
    return {"ok": True, "db": db,
            "counts": {"models": len(models),
                       "private_models": sum(1 for m in models if m["private"])},
            "recent_models": models[:8],
            "engines": _engines_available(),
            "deferred": _deferred_status(),
            "actions": list(_ACTIONS),
            "method": "discrete Bayesian network + 变量消元精确推断 + 共轭先验更新(纯 stdlib)",
            "note": "贝叶斯概率推断与信息价值核心: 在不确定下定量地想(多大概率/为什么/查哪条/怎么更新先验/哪个假设)。"}


def health() -> dict:
    """registry 探针(可选): 库可建 + 引擎自检过 → ok。"""
    try:
        s = status()
        n = s["counts"]["models"]
        return {"ok": True, "detail": "贝叶斯推断核心就绪; 已存模型 %d 个; 引擎: 变量消元(纯本机)。" % n}
    except Exception as e:
        return {"ok": False, "detail": "bayes_infer 不可用: " + str(e)}


# --------------------------------------------------------------------------- #
# LLM 工具面(只读且冻结): 仅 infer / posterior / predict / explain_probability /
# value_of_information / sensitivity / decision_under_uncertainty /
# compare_hypotheses / status。写库(build_model/observe/update_priors)与 audit CLI-only。
# --------------------------------------------------------------------------- #
_DISPATCH_WHITELIST = {"infer", "posterior", "predict", "explain_probability",
                       "value_of_information", "sensitivity",
                       "decision_under_uncertainty", "compare_hypotheses", "status"}

_BI_TOOLS = [
    {"type": "function", "function": {
        "name": "bayes_infer_infer",
        "description": ("贝叶斯推断: 在某概率模型下算 P(查询变量 | 证据) 的后验分布与最可能取值。只读。"),
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "query": {"type": "string", "description": "要推断的变量名"},
            "given": {"type": "object", "description": "临时附加证据 {变量:状态}"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_predict",
        "description": "前向预测: 给一组假设证据(what-if), 算 target 变量的预测分布与相对无假设的变化。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "target": {"type": "string"},
            "assume": {"type": "object", "description": "假设证据 {变量:状态}"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_explain_probability",
        "description": "解释某个概率为何如此: 先验→后验的证据拉动(留一法)、主导证据、因果父变量链。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "query": {"type": "string"}, "state": {"type": "string"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_value_of_information",
        "description": "信息价值: 还缺哪条证据最值得先去查(关于 target 的互信息/熵减; 给决策则算 EVPI)。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "target": {"type": "string"},
            "candidates": {"type": "array", "items": {"type": "string"}}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_decision_under_uncertainty",
        "description": ("不确定下决策: 给动作及其对结果状态的效用, 按后验算各动作期望效用并推荐最优。"
                        "纯顾问, 只读(落地动作须走 CLI 入审批队列)。"),
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"},
            "decision": {"type": "object", "description": "{outcome_var, actions:{动作:{结果状态:效用}}}"}},
            "required": ["decision"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_compare_hypotheses",
        "description": "贝叶斯模型比较: 给竞争假设(先验+似然)或模型里的假设变量, 算后验概率与贝叶斯因子。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "var": {"type": "string"},
            "hypotheses": {"type": "array", "items": {"type": "object"}}}}}},
    {"type": "function", "function": {
        "name": "bayes_infer_sensitivity",
        "description": "敏感性分析: 哪个先验/CPT 参数最影响 target 后验(最该被核实的假设)。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"}, "target": {"type": "string"}, "state": {"type": "string"}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_posterior",
        "description": "后验分布: 单变量或多变量联合后验 P(查询 | 证据)。只读。",
        "parameters": {"type": "object", "properties": {
            "model_id": {"type": "string"},
            "query": {"type": "array", "items": {"type": "string"}}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "bayes_infer_status",
        "description": "看贝叶斯推断核心总览(已存模型 + 各引擎可用性 + deferred)。只读。",
        "parameters": {"type": "object", "properties": {}}}},
]


def approved_tools() -> list:
    if not config.get("bayes_infer.enabled", True) or \
            not config.get("bayes_infer.expose_tools", True):
        return []
    return list(_BI_TOOLS)


def is_tool(name: str) -> bool:
    return name in {t["function"]["name"] for t in _BI_TOOLS}


def dispatch(name: str, args: dict | None = None) -> dict:
    """jarvis_brain 工具入口。只放行只读推断/分析动作; 写库/落地类绝不暴露给模型。"""
    args = dict(args or {})
    short = name.replace("bayes_infer_", "")
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": "动作 '" + short + "' 不在 bayes_infer 模型白名单 "
                         + str(sorted(_DISPATCH_WHITELIST))
                         + "; build_model/observe/update_priors/audit 请经 CLI。"}
    if short == "infer":
        return infer(args.get("model_id", ""), args.get("query", ""), given=args.get("given"))
    if short == "posterior":
        return posterior(args.get("model_id", ""), query=args.get("query"), given=args.get("given"))
    if short == "predict":
        return predict(args.get("model_id", ""), args.get("target", ""), assume=args.get("assume"))
    if short == "explain_probability":
        return explain_probability(args.get("model_id", ""), args.get("query", ""),
                                   state=args.get("state", ""))
    if short == "value_of_information":
        return value_of_information(args.get("model_id", ""), args.get("target", ""),
                                    candidates=args.get("candidates"),
                                    decision=args.get("decision"))
    if short == "sensitivity":
        return sensitivity(args.get("model_id", ""), args.get("target", ""),
                           state=args.get("state", ""))
    if short == "decision_under_uncertainty":
        # 模型白名单永不落地: 强制 enqueue=False(顾问只读)。
        return decision_under_uncertainty(args.get("model_id", ""), decision=args.get("decision"),
                                          enqueue=False, action="")
    if short == "compare_hypotheses":
        return compare_hypotheses(args.get("model_id", ""), hypotheses=args.get("hypotheses"),
                                  var=args.get("var", ""))
    if short == "status":
        return status()
    return {"ok": False, "error": "unreachable"}  # pragma: no cover


# --------------------------------------------------------------------------- #
# selftest — AST 静态扫 + 行为验证(纯本地, 绝不真执行/真删/真发)
# --------------------------------------------------------------------------- #
# 绝不允许 *直接 import* 的执行面(本模块只经 _opt 软导入只读面; 此为回归护栏)。
_FORBIDDEN_EXEC_MODULES = (
    "local_powers", "computer_use", "screen", "nexus", "appsmith", "self_improve",
    "hive", "swarm", "agent_swarm", "claude_code", "fleet", "skillforge",
    "simulation_lab", "autopilot",
)
_FORBIDDEN_ATTRS = {"run_shell", "write_file", "move_file", "applescript",
                    "type_text", "click_on", "nexus_send"}
_FORBIDDEN_CALLS = {"system", "popen", "remove", "unlink", "rmdir", "rmtree",
                    "exec", "eval"}


def _selftest() -> int:
    """返回 0=全过。验证:
      ① 不 import 任何执行面模块; ② 源码无执行面方法调用; ③ 无危险 stdlib 调用;
      ④ 无 confirmed=True; ⑤ 模型白名单只读(build_model/observe/update_priors/audit 不暴露);
      ⑥ 行为: 推断后验数学正确(贝叶斯定理手算对照)、VoI≥0、决策最大 EU、共轭更新方向对、
         凭据硬拦 + 私密脱敏、决策落地入审批闸(从不执行)、dispatch 拒写动作。
    """
    import ast
    import inspect
    import tempfile
    import shutil
    fails = []
    src = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(src)

    bad_imports = set()
    bad_attrs = set()
    bad_calls = set()
    confirmed_true = False

    class _Scan(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            if node.name == "_selftest":
                return  # 跳过自检自身(它合法地内含被禁字面量与临时目录清理)
            self.generic_visit(node)

        def visit_Import(self, node):  # noqa: N802
            for a in node.names:
                if a.name.split(".")[0] in _FORBIDDEN_EXEC_MODULES:
                    bad_imports.add(a.name.split(".")[0])
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in _FORBIDDEN_EXEC_MODULES:
                bad_imports.add((node.module or "").split(".")[0])
            self.generic_visit(node)

        def visit_Attribute(self, node):  # noqa: N802
            if node.attr in _FORBIDDEN_ATTRS:
                bad_attrs.add(node.attr)
            self.generic_visit(node)

        def visit_Call(self, node):  # noqa: N802
            nonlocal confirmed_true
            for kw in node.keywords:
                if kw.arg == "confirmed" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    confirmed_true = True
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr in _FORBIDDEN_CALLS:
                bad_calls.add(f.attr)
            if isinstance(f, ast.Name) and f.id in ("exec", "eval", "__import__", "compile"):
                bad_calls.add(f.id)
            self.generic_visit(node)

    _Scan().visit(tree)

    for m in sorted(bad_imports):
        fails.append("① 直接 import 执行面模块: " + m)
    for a in sorted(bad_attrs):
        fails.append("② 出现执行面方法调用: ." + a)
    for c in sorted(bad_calls):
        fails.append("③ 出现危险 stdlib 调用: " + c)
    if confirmed_true:
        fails.append("④ 源码出现 confirmed=True(本模块绝不允许)")
    for leaked in ("build_model", "observe", "update_priors", "audit"):
        if leaked in _DISPATCH_WHITELIST:
            fails.append("⑤ 模型白名单泄漏写库/自审动作: " + leaked)

    # ⑥ 行为验证: 临时库 + 经典疾病/检测网络(后验可手算对照)。
    tmp = tempfile.mkdtemp(prefix="bi_selftest_")
    db = os.path.join(tmp, "bi.db")
    try:
        # Disease(root) → Test(child)。基率 1%, 灵敏 90%, 假阳 5%。
        spec = {"name": "selftest", "variables": {
            "Disease": {"states": ["pos", "neg"], "parents": [],
                        "cpt": {"[]": [0.01, 0.99]}, "kind": "belief"},
            "Test": {"states": ["pos", "neg"], "parents": ["Disease"],
                     "cpt": {"[\"pos\"]": [0.9, 0.1], "[\"neg\"]": [0.05, 0.95]}},
        }}
        b = build_model(spec=spec, db_path=db)
        if not b.get("ok"):
            fails.append("⑥ build_model 失败: " + str(b))
        mid = b.get("model_id", "")
        # 边缘 P(Test=pos) = .01*.9 + .99*.05 = .0585
        r0 = infer(mid, "Test", db_path=db)
        if not r0.get("ok") or abs(r0["posterior"]["pos"] - 0.0585) > 1e-6:
            fails.append("⑥ 边缘 P(Test=pos) 不对(应 .0585): " + str(r0.get("posterior")))
        # 后验 P(Disease=pos | Test=pos) = .009/.0585 = .15384615...
        r1 = infer(mid, "Disease", given={"Test": "pos"}, db_path=db)
        if not r1.get("ok") or abs(r1["posterior"]["pos"] - 0.15384615) > 1e-5:
            fails.append("⑥ 贝叶斯后验 P(D=pos|T=pos) 不对(应 .15385): " + str(r1.get("posterior")))
        # 后验分布和 ≈ 1
        if r1.get("ok") and abs(sum(r1["posterior"].values()) - 1.0) > 1e-9:
            fails.append("⑥ 后验分布未归一")
        # predict what-if: 假设 Test=pos 应抬升 Disease=pos
        pp = predict(mid, "Disease", assume={"Test": "pos"}, db_path=db)
        if not pp.get("ok") or pp["predictive"]["pos"] <= 0.01:
            fails.append("⑥ what-if 预测未抬升 Disease=pos: " + str(pp.get("predictive")))
        # VoI: 观测 Test 对 Disease 的信息增益 > 0
        voi = value_of_information(mid, "Disease", db_path=db)
        if not voi.get("ok") or not voi["ranking"] or voi["ranking"][0]["info_gain_bits"] <= 0:
            fails.append("⑥ VoI(Test→Disease) 应 > 0: " + str(voi.get("ranking")))
        if voi.get("ok") and any(r["info_gain_bits"] < -1e-9 for r in voi["ranking"]):
            fails.append("⑥ 出现负信息增益(互信息应 ≥ 0)")
        # 决策: U(treat,pos)=0,U(treat,neg)=-1,U(notreat,pos)=-10,U(notreat,neg)=0
        dec = {"outcome_var": "Disease",
               "actions": {"treat": {"pos": 0, "neg": -1}, "notreat": {"pos": -10, "neg": 0}}}
        d0 = decision_under_uncertainty(mid, decision=dec, db_path=db)  # 无证据 → notreat 更优
        if not d0.get("ok") or d0["recommended_action"] != "notreat":
            fails.append("⑥ 无证据时应推荐 notreat: " + str(d0.get("recommended_action")))
        # 观测 Test=pos 后 → treat 更优(决策翻转 → EVPI(Test) > 0)
        observe(mid, "Test", "pos", db_path=db)
        d1 = decision_under_uncertainty(mid, decision=dec, db_path=db)
        if not d1.get("ok") or d1["recommended_action"] != "treat":
            fails.append("⑥ 观测 Test=pos 后应推荐 treat: " + str(d1.get("recommended_action")))
        # 决策落地必须入审批闸 + 绝不执行(注入假 lp/aps)
        _orig_opt = globals()["_opt"]
        enq = []

        def _fake_opt(nm):
            if nm == "local_powers":
                return type("LP", (), {"_check_red_line": staticmethod(lambda t: None),
                                       "non_shell_danger": staticmethod(lambda t, kind=None: None)})()
            if nm == "autopilot_store":
                return type("AP", (), {"enqueue_approval":
                                       staticmethod(lambda **k: (enq.append(k) or 4242))})()
            return _orig_opt(nm)
        globals()["_opt"] = _fake_opt
        try:
            dq = decision_under_uncertainty(mid, decision=dec, enqueue=True,
                                            action="给患者用药", db_path=db)
        finally:
            globals()["_opt"] = _orig_opt
        if not (dq.get("ok") and dq.get("queued_approval_id") == 4242
                and dq.get("needs_confirm") is True):
            fails.append("⑥ 决策落地未入审批队列/未需确认: " + str(
                {"q": dq.get("queued_approval_id"), "nc": dq.get("needs_confirm")}))
        if not (len(enq) == 1 and enq[0].get("risk") == "high"):
            fails.append("⑥ 审批入队 risk 应为 high")
        # 共轭更新方向: 多观测 pos → P(Disease=pos) 上升
        before = infer(mid, "Disease", db_path=db)  # 注意此时已有 Test=pos 证据
        # 用独立模型测先验更新, 避免证据干扰
        b2 = build_model(spec={"name": "u", "variables": {
            "X": {"states": ["pos", "neg"], "parents": [], "cpt": {"[]": [0.2, 0.8]}}}}, db_path=db)
        m2 = b2["model_id"]
        p_before = infer(m2, "X", db_path=db)["posterior"]["pos"]
        up = update_priors(m2, "X", counts={"pos": 30, "neg": 0}, db_path=db)
        p_after = infer(m2, "X", db_path=db)["posterior"]["pos"]
        if not up.get("ok") or not (p_after > p_before):
            fails.append("⑥ 共轭更新方向错(多观测 pos 后 P(pos) 应上升): %s→%s"
                         % (p_before, p_after))
        # 凭据硬拦: observe 涉密路径必拒
        cred = observe(m2, "X", os.path.expanduser("~/.gmail_creds.json"), db_path=db)
        if cred.get("ok"):
            fails.append("⑥ observe 未硬拦凭据状态值!")
        # build_model 含凭据 evidence → 被丢弃
        bsec = build_model(spec={"name": "s", "variables": {
            "Y": {"states": ["a", "b"], "parents": [], "cpt": {"[]": [0.5, 0.5]}}},
            "evidence": {".gmail_creds.json": "a"}}, db_path=db)
        if bsec.get("ok") and ".gmail_creds.json" in str(bsec.get("evidence", {})):
            fails.append("⑥ 凭据 evidence 未被硬拦!")
        # 私密 note → 脱敏 + 标 _private
        bpriv = build_model(spec={"name": "p", "variables": {
            "Z": {"states": ["hi", "lo"], "parents": [], "cpt": {"[]": [0.5, 0.5]},
                  "note": "本月工资 12345 元"}}}, db_path=db)
        rp = infer(bpriv["model_id"], "Z", db_path=db)
        if not rp.get("_private"):
            fails.append("⑥ 含私密 note 的模型输出未带 _private")
        m_loaded = _load_model(bpriv["model_id"], db)
        if "12345" in json.dumps(m_loaded, ensure_ascii=False):
            fails.append("⑥ 私密 note 正文未脱敏(隐私泄漏)!")
        # dispatch 拒写动作, 放行只读
        if dispatch("build_model", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 build_model(应拒绝)")
        if dispatch("observe", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 observe(应拒绝)")
        if not dispatch("status", {}).get("ok"):
            fails.append("⑤ dispatch 应放行 status")
        # compare_hypotheses 显式模式
        ch = compare_hypotheses(hypotheses=[{"name": "H1", "prior": 0.5, "likelihood": 0.9},
                                            {"name": "H2", "prior": 0.5, "likelihood": 0.1}])
        if not ch.get("ok") or ch["best"] != "H1" or abs(ch["best_posterior"] - 0.9) > 1e-6:
            fails.append("⑥ compare_hypotheses 显式模式后验不对: " + str(ch.get("best_posterior")))
        # deferred 内核记录但不建桩
        d = _deferred_status()
        for nm in ("epistemic_core", "reasoning_core", "abduction_core", "metacognition_core"):
            if not (nm in d and d[nm]["deferred"] is True):
                fails.append("⑥ deferred 未正确记录: " + nm)
    except Exception as e:
        import traceback
        fails.append("⑥ 行为验证抛异常: " + str(e) + " | " + traceback.format_exc()[-400:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        for f in fails:
            print("FAIL: " + f)
        return 1
    print("OK bayes_infer selftest 全过(纯只读概率推断门面 + 零真实副作用):")
    print("  ① 不直接 import 任何执行面模块 ✓")
    print("  ② 源码无执行面方法调用(run_shell/write_file/type_text/click_on...) ✓")
    print("  ③ 无危险 stdlib 调用(system/remove/rmtree/exec...) ✓")
    print("  ④ 源码无 confirmed=True ✓")
    print("  ⑤ 模型白名单只读, 写库/自审动作不暴露给模型 ✓")
    print("  ⑥ 贝叶斯后验数学正确(基率题手算对照)+ VoI≥0 + 最大期望效用决策 +")
    print("     共轭更新方向 + 凭据硬拦 + 私密脱敏 + 决策落地入审批闸(从不执行)+ dispatch 拒写 ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI(argparse; 全 pretty-JSON via _emit)
# --------------------------------------------------------------------------- #
def _load_json_arg(s: str, what: str):
    """把 --spec/--decision/... 解析成对象: 既支持内联 JSON, 也支持 @文件路径。"""
    if not s:
        return None
    try:
        if s.startswith("@"):
            path = os.path.abspath(os.path.expanduser(s[1:]))
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return json.loads(s)
    except Exception as e:
        raise ValueError("--" + what + " 不是合法 JSON(或 @文件): " + str(e))


def _main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="bayes_infer",
                                description="贝叶斯概率推断与信息价值核心")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build_model", help="把信念/证据/机制/业务变量编译成贝叶斯网络")
    pb.add_argument("--spec", default="", help="模型 JSON(内联或 @文件)")
    pb.add_argument("--name", default="")
    pb.add_argument("--from-world-model", default="", help="从 world_model.predict(topic) 取种子(只读)")
    pb.add_argument("--model-id", default="", help="指定/覆盖某模型 id")

    po = sub.add_parser("observe", help="记录观测证据(凭据硬拦)")
    po.add_argument("--model-id", default="")
    po.add_argument("var")
    po.add_argument("state")

    pi = sub.add_parser("infer", help="后验边缘 P(query | 证据)")
    pi.add_argument("query")
    pi.add_argument("--model-id", default="")
    pi.add_argument("--given", default="", help="临时附加证据 JSON {变量:状态}")

    pp = sub.add_parser("posterior", help="单/多变量联合后验")
    pp.add_argument("query", nargs="+", help="一个或多个变量名")
    pp.add_argument("--model-id", default="")
    pp.add_argument("--given", default="")

    ppr = sub.add_parser("predict", help="前向预测(what-if 假设证据)")
    ppr.add_argument("target")
    ppr.add_argument("--model-id", default="")
    ppr.add_argument("--assume", default="", help="假设证据 JSON {变量:状态}")

    pe = sub.add_parser("explain_probability", help="为什么是这个概率(证据拉动 + 因果父链)")
    pe.add_argument("query")
    pe.add_argument("--state", default="")
    pe.add_argument("--model-id", default="")

    pv = sub.add_parser("value_of_information", help="先查哪条证据最值得(互信息/EVPI)")
    pv.add_argument("target")
    pv.add_argument("--model-id", default="")
    pv.add_argument("--candidates", default="", help="候选变量, 逗号分隔(默认全部未观测)")
    pv.add_argument("--decision", default="", help="给决策 JSON 则算 EVPI")

    ps = sub.add_parser("sensitivity", help="哪个先验/CPT 参数最影响 target 后验")
    ps.add_argument("target")
    ps.add_argument("--state", default="")
    ps.add_argument("--model-id", default="")
    ps.add_argument("--delta", type=float, default=0.1)

    pd = sub.add_parser("decision_under_uncertainty", help="最大期望效用决策(顾问; 落地经审批闸)")
    pd.add_argument("--model-id", default="")
    pd.add_argument("--decision", default="", help="{outcome_var, actions:{动作:{状态:效用}}} JSON")
    pd.add_argument("--enqueue", action="store_true", help="把推荐动作入 autopilot 审批队列(绝不执行)")
    pd.add_argument("--action", default="", help="要落地的真动作描述(配 --enqueue)")

    pu = sub.add_parser("update_priors", help="共轭先验更新(Beta/Dirichlet)")
    pu.add_argument("var")
    pu.add_argument("--model-id", default="")
    pu.add_argument("--counts", default="", help="{状态:计数} JSON 或逗号分隔计数")
    pu.add_argument("--given", default="", help="有父变量时指定要更新的父取值行")

    pc = sub.add_parser("compare_hypotheses", help="贝叶斯模型比较(后验 + 贝叶斯因子)")
    pc.add_argument("--model-id", default="")
    pc.add_argument("--var", default="", help="模型里的假设变量(模式 B)")
    pc.add_argument("--hypotheses", default="", help="[{name,prior,likelihood}] JSON(模式 A)")

    sub.add_parser("audit", help="隐私/安全自审(只读)")
    sub.add_parser("status", help="总览 + 引擎可用性 + deferred")
    sub.add_parser("selftest", help="自检(AST 静态扫 + 行为验证)")

    args = p.parse_args(argv)
    cmd = args.cmd

    try:
        if cmd == "build_model":
            res = build_model(spec=_load_json_arg(args.spec, "spec"), name=args.name,
                              from_world_model=args.from_world_model, model_id=args.model_id)
        elif cmd == "observe":
            res = observe(args.model_id, args.var, args.state)
        elif cmd == "infer":
            res = infer(args.model_id, args.query, given=_load_json_arg(args.given, "given"))
        elif cmd == "posterior":
            res = posterior(args.model_id, query=list(args.query),
                            given=_load_json_arg(args.given, "given"))
        elif cmd == "predict":
            res = predict(args.model_id, args.target, assume=_load_json_arg(args.assume, "assume"))
        elif cmd == "explain_probability":
            res = explain_probability(args.model_id, args.query, state=args.state)
        elif cmd == "value_of_information":
            cands = [c.strip() for c in args.candidates.split(",") if c.strip()] or None
            res = value_of_information(args.model_id, args.target, candidates=cands,
                                       decision=_load_json_arg(args.decision, "decision"))
        elif cmd == "sensitivity":
            res = sensitivity(args.model_id, args.target, state=args.state, delta=args.delta)
        elif cmd == "decision_under_uncertainty":
            res = decision_under_uncertainty(args.model_id,
                                             decision=_load_json_arg(args.decision, "decision"),
                                             enqueue=args.enqueue, action=args.action)
        elif cmd == "update_priors":
            counts = _load_json_arg(args.counts, "counts") if args.counts.strip().startswith(("{", "[")) \
                else ([x.strip() for x in args.counts.split(",")] if args.counts else None)
            res = update_priors(args.model_id, args.var, counts=counts, given=args.given)
        elif cmd == "compare_hypotheses":
            res = compare_hypotheses(args.model_id, hypotheses=_load_json_arg(args.hypotheses, "hypotheses"),
                                     var=args.var)
        elif cmd == "audit":
            res = audit()
        elif cmd == "status":
            res = status()
        elif cmd == "selftest":
            return _selftest()
        else:  # pragma: no cover
            res = {"ok": False, "error": "未知子命令: " + str(cmd)}
    except ValueError as e:
        res = {"ok": False, "error": str(e)}

    _emit(res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
