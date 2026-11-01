#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""logic_proof_core.py — 小美的「形式逻辑推理审判器」。

把小美的回答、计划、归类理由、业务判断和行动解释**自动拆成论证骨架**: 前提(premises)、
定义(definitions)、隐含假设(implicit assumptions)、推理步骤(steps)、结论(conclusion)、
适用条件(conditions); 在此之上**审判推理链是否站得住** —— 检查偷换概念、循环论证、
因果倒置、以偏概全、前提不足、结论跳跃、与已知事实冲突等谬误; 给出反例(counterexample)、
反模型(countermodel, 命题层真值赋值)、证明轨迹(proof_trace)、缺口解释(explain_gap)与
最小修补(repair_reasoning)。目标: 让小美在说「为什么」之前, 先证明自己的推理链站得住。

定位: 本模块是**纯只读分析门面 + 离线确定性逻辑内核**, 不自带执行引擎, 不重写任何安全门。
  - 论证拆解 / 谬误扫描 / 有效性判定 / 反模型搜索全部是**纯 stdlib 确定性算法**(命题层真值表
    枚举 + 启发式语言切分), 离线可复现, 不依赖任何 LLM(LLM 缺失也照常工作)。
  - reasoning_core / epistemic_core / problem_framer / metacognition_core **尚未落地** →
    软导入记 deferred(待其上线自动接上, 绝不建桩); world_model / mission_control 已落地 →
    best-effort 只读接入(查「与已知事实冲突」/「与活跃目标对齐」)。

★★★ 安全铁律(与 shared_context / mission_control / simulation_lab 一致, 自进化引擎不得改弱)★★★
  - 纯门面 + 零真实副作用: 本模块自身**绝不** subprocess / os.system / osascript / 删文件 /
    exec / eval / __import__ / 改文件。它只读入一段文本(论证)、产出分析、追加**元数据审计流**。
    它没有任何「落地动作」——不入审批队列、不执行、不发送。
  - 永不自放行: **绝不**对任何引擎传 confirmed=True(本模块根本不调用执行面)。
  - 红线单一真相源: 本模块不复制红线表; 它根本不产生需要红线判定的动作(纯分析)。
  - 隐私: 论证正文可能含私密内容(工资/银行/合同/验证码/凭据)。一律先过
    privacy_stream.should_force_local / awareness_sensors.classify_private 判定; 命中 → 输出带
    `_private`(必要时 `_force_local`), 由 jarvis_brain 切本机模型收尾, 正文不出本机。
    **凭据/密钥(token/sk-/Bearer/private key)等硬敏感串在回显文本里一律脱敏**; 审计流**只记
    hash/计数/判决, 绝不落论证正文**。隐私库不可达 → 一律按私密处理(fail-safe)。
  - LLM 工具面只读且冻结: 只暴露 parse_argument / extract_premises / implicit_assumptions /
    check_validity / fallacy_scan / counterexample / countermodel / proof_trace /
    repair_reasoning / explain_gap / status; audit(隐私自审)走 CLI。无任何写库/落地动作可暴露。

全本机, 纯 stdlib。审计流 ~/.jarvis/logic_proof_core/logic_proof_core.jsonl(只元数据)。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402 — 纯读取, 无副作用


# --------------------------------------------------------------------------- #
# 路径(env 优先 → config → 默认; 函数式以便测试用 env 覆盖)
# --------------------------------------------------------------------------- #
_HOME_ENV = "JARVIS_LOGIC_PROOF_HOME"   # 测试设它指向 tmp; 优先于 config


def _data_dir() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(
        config.get("logic_proof_core.data_dir", "~/.jarvis/logic_proof_core")))


def _audit_path() -> str:
    return os.path.join(_data_dir(), "logic_proof_core.jsonl")


# --------------------------------------------------------------------------- #
# 软导入可选集成模块(importlib, 非 __import__/exec); 缺/出错 → None(降级, 永不抛)
# --------------------------------------------------------------------------- #
def _opt(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# 尚未落地的推理/认知内核 → 记 deferred, 绝不建桩(待其上线 _connect_cores 自动接上)。
_DEFERRED = ("reasoning_core", "epistemic_core", "problem_framer", "metacognition_core")
# 已落地、best-effort 只读接入的语境/事实来源。
_INTEGRATION = ("world_model", "mission_control", "privacy_stream",
                "file_tools", "awareness_sensors")


def _deferred_status() -> dict:
    out = {}
    for m in _DEFERRED:
        out[m] = {"present": _opt(m) is not None, "deferred": True, "fallback": None}
    return out


def _engines_available() -> dict:
    return {m: (_opt(m) is not None) for m in _INTEGRATION}


# --------------------------------------------------------------------------- #
# 时间/输出/审计/hash 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _hash(text: str) -> str:
    """论证正文的稳定指纹(只用于审计去重/计数; 绝不存正文)。"""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _audit(action: str, claim_hash: str = "", **extra) -> None:
    """只记 hash/动作/计数/判决的审计流, **绝不写论证正文/隐私**, 永不抛。"""
    try:
        rec = {"ts": _now(), "action": action, "claim_hash": claim_hash or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        path = _audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 隐私原语(复用 privacy_stream / awareness_sensors 单一姿态; 不可达 → fail-safe)
# --------------------------------------------------------------------------- #
# 凭据/密钥文件硬禁名单(本地兜底; 优先用 awareness_sensors.is_secret_path)。
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".localkey", "config.toml",
})
# 凭据/密钥**值与路径**特征(本地兜底, ALWAYS runs, 绝不被库级判定短路)。
_SECRET_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.localkey|/\.jarvis/config\.toml|"
    r"\.pem$|(^|/)id_rsa|(^|/)\.ssh/|refresh_token|client_secret|"
    r"BEGIN [A-Z ]*PRIVATE KEY|sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}|"
    r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|"
    r"(password|passwd|密码|口令|api[_\s-]?key|secret|token|私钥)\s*[:=：]\s*\S+",
    re.IGNORECASE)


def _is_secret_path(path: str) -> bool:
    """凭据/密钥文件 → True(硬拦, 绝不纳入回显)。判不准当机密(fail-safe)。

    双层(缺一不可): ① awareness_sensors.is_secret_path(库级, 优先); ② 本地黑名单+正则
    (ALWAYS runs, 永不被库级短路 —— 库判 False 仍走本地兜底)。
    """
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "is_secret_path"):
        try:
            if bool(aw.is_secret_path(path)):
                return True
        except Exception:
            return True  # 库级出错 → fail-safe 当机密
    try:
        p = path or ""
        base = os.path.basename(os.path.expanduser(p))
        if base in _SECRET_BASENAMES:
            return True
        return bool(_SECRET_RX.search(p))
    except Exception:
        return True


def _classify_private(text: str = "", app: str = "", window: str = "") -> bool:
    """文本是否私密。复用 awareness_sensors 单一姿态; 不可达且有正文 → fail-safe True。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密内容不占位、返原文
    (本模块无对外出口; 红线4 由 autopilot 审批闸独立守卫, 不靠此分类)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "classify_private"):
        try:
            return bool(aw.classify_private(text, app, window))
        except Exception:
            return True if (text or app or window) else False
    return bool(re.search(r"工资|薪资|银行卡|身份证|密码|底价|佣金|\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b", text or ""))


def _scrub_secrets(text: str) -> str:
    """脱掉硬敏感串(凭据/密钥/token), 保留逻辑结构。回显文本一律先过它。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 返原文, 不脱敏(本模块无对外出口,
    数据层不阉割)。开 → 完整脱敏(可逆)。"""
    s = text or ""
    if not s:
        return ""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return s   # 数据层隐私关: 原文直出; 出口另有 L3 守卫
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "redact"):
        try:
            return aw.redact(s)
        except Exception:
            pass
    return _SECRET_RX.sub("[凭证已脱敏]", s)


def _privacy_assess(text: str) -> dict:
    """判定论证文本的隐私级别。返回 {private, force_local, reason}。

    单一真相源 privacy_stream.should_force_local(按内容形状); 不可达 → 本地兜底
    (awareness_sensors.classify_private + 凭据正则 + allow_cloud_private 开关)。fail-safe 从严。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒非私密/非强制本机, 不占位
    (本模块无对外出口; 红线4 由 autopilot 审批闸独立守卫, 不靠此判定)。
    """
    if not config.get("safety.data_layer_privacy_enabled", False):
        return {"private": False, "force_local": False, "reason": ""}
    txt = text or ""
    ps = _opt("privacy_stream")
    if ps is not None and hasattr(ps, "should_force_local"):
        try:
            v = ps.should_force_local(txt)
            return {"private": bool(v.get("private")),
                    "force_local": bool(v.get("force_local")),
                    "reason": v.get("reason", "")}
        except Exception:
            pass  # 落本地兜底
    try:
        allow_cloud = bool(config.get("safety.allow_cloud_private", False))
    except Exception:
        allow_cloud = False
    hard = bool(_SECRET_RX.search(txt))
    if hard:
        return {"private": True, "force_local": True, "reason": "含凭据/高敏感串, 强制本机"}
    if _classify_private(txt):
        return {"private": True, "force_local": (not allow_cloud),
                "reason": ("私密内容, 未授权上云 → 本机" if not allow_cloud
                           else "私密但已授权上云")}
    return {"private": False, "force_local": False, "reason": ""}


def _flag_private(out: dict, assess: dict) -> dict:
    """给输出标隐私(模型端据此切本机)。out 形态不变, 只加标志。"""
    if assess.get("private"):
        out["_private"] = True
        if assess.get("force_local"):
            out["_force_local"] = True
        out.setdefault("_privacy_reason", assess.get("reason", ""))
    return out


# --------------------------------------------------------------------------- #
# 语言资源(中英双语线索词; 确定性切分论证骨架用)
# --------------------------------------------------------------------------- #
_PREMISE_CUES = ("因为", "由于", "既然", "鉴于", "根据", "基于", "考虑到", "毕竟",
                 "because", "since", "given that", "as", "for")
_CONCL_CUES = ("所以", "因此", "故而", "因而", "由此", "于是", "从而", "可见", "综上",
               "得出", "故", "therefore", "thus", "hence", "consequently",
               "it follows", "so")
# 用于把句中「所以/因此」拆成 (理由, 结论) 的强结论标记。
_CONCL_SPLIT_CUES = ("所以", "因此", "因而", "故而", "由此可见", "由此", "于是", "从而",
                     "可见", "综上所述", "综上", "therefore", "thus", "hence",
                     "consequently")
_ASSUME_CUES = ("假设", "假定", "前提是", "默认", "想必", "理应", "应该", "必然", "显然",
                "众所周知", "毫无疑问", "不言而喻", "obviously", "clearly", "of course",
                "presumably", "needless to say")
_COND_CUES = ("如果", "若", "假如", "倘若", "要是", "只要", "除非", "当", "在",
              "if", "when", "unless", "provided that", "assuming")
_DEF_CUES = ("是指", "定义为", "所谓", "指的是", "means", "is defined as",
             "refers to", "i.e.")
_UNIVERSAL = ("所有", "全部", "每个", "每一", "任何", "凡是", "一切", "总是", "永远",
              "无一例外", "都是", "all", "every", "any", "always", "never",
              "everyone", "no one", "none of")
_PARTICULAR = ("某个", "某些", "有的", "有个", "一次", "一例", "我见过", "我遇到",
               "曾经", "一个例子", "举个例子", "one", "some", "a few", "i saw",
               "i met", "once", "for example")
_NEG_WORDS = ("不", "没", "没有", "未", "非", "无", "别", "勿")
_NEG_EN = (" not ", " no ", " never ", "n't")
_CAUSAL = ("导致", "引起", "使得", "造成", "促成", "因此导致", "所以才",
           "causes", "leads to", "results in", "because of", "due to", "brings about")
_CORRELATION = ("相关", "伴随", "同时出现", "总是一起", "correlat", "associated with",
                "goes with", "at the same time")


# --------------------------------------------------------------------------- #
# 文本切分 / 归一 / 相似度(命题原子对齐的基础)
# --------------------------------------------------------------------------- #
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")
# 归一时剥掉的 CJK 线索/连接/虚词(按子串替换 —— CJK 无词边界)。
# 注意: 按长度降序 —— 多字线索词必须先于单字剔除(否则 '没有' 被 '没' 拆成残留 '有')。
_STRIP_CJK = sorted(
    [t for t in (list(_PREMISE_CUES) + list(_CONCL_CUES) + list(_ASSUME_CUES)
     + list(_COND_CUES) + ["那么", "则", "就", "才", "会", "将", "把", "被",
     "是", "的", "了", "着", "吗", "呢", "啊", "吧", "呀", "一", "个", "这", "那"])
     if re.search(r"[一-鿿]", t)],
    key=len, reverse=True)
# 否定词同理按长度降序(多字先剔)。
_NEG_WORDS_SORTED = sorted(("不", "没", "没有", "未", "非", "无", "别", "勿"),
                           key=len, reverse=True)
_EN_STOP = frozenset(("the", "a", "an", "is", "are", "was", "were", "will", "to", "of",
                      "and", "or", "then", "that", "this", "it", "if", "so", "be",
                      "in", "on", "at", "for", "as", "by", "with", "do", "does",
                      "because", "since", "therefore", "thus", "hence", "obviously",
                      "clearly", "all", "every", "some"))


def _segment(text: str) -> list:
    """切成句子/陈述单元(只按强分隔符; 逗号留在句内, 以免拆散条件句)。"""
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _has_neg(clause: str) -> bool:
    c = clause or ""
    low = " " + c.lower() + " "
    if any(n in low for n in _NEG_EN):
        return True
    return any(n in c for n in _NEG_WORDS)


def _core(clause: str) -> str:
    """归一出子句的内容核(剥线索/连接/否定/虚词/标点); 用于原子对齐。

    CJK 按子串剔除线索词; 英文按词边界剔除停用词(避免 'cat' 被 'a' 误伤); 折叠为单空格。
    """
    s = (clause or "").lower()
    for w in list(_EN_STOP) + ["not", "no", "never"]:
        s = re.sub(r"\b" + re.escape(w) + r"\b", " ", s)
    s = s.replace("n't", " ")
    for tok in _STRIP_CJK:
        s = s.replace(tok, " ")
    for n in _NEG_WORDS_SORTED:
        s = s.replace(n, " ")
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)   # 标点→空格(\w 含 CJK)
    return re.sub(r"\s+", " ", s).strip()


def _content_set(clause: str) -> set:
    """子句的内容 token 集(命题变量 + CJK 字符 bigram + 英文去停用词词)。"""
    toks = set()
    # 命题变量: 独立大写字母(A/B/P/Q…) —— 形式逻辑常用记号, 不可被当英文冠词剔除。
    # 用 ASCII 字母 lookaround(非 \b): \b 在 CJK 与字母间不成立(『非B』里 B 前无边界)。
    for v in re.findall(r"(?<![A-Za-z])[A-Z](?![A-Za-z])", clause or ""):
        toks.add("var:" + v)
    core = _core(clause)
    if not core and not toks:
        return set()
    for w in re.findall(r"[a-z]+", core):
        if w not in _EN_STOP and len(w) > 1:
            toks.add(w)
    cjk = re.findall(r"[一-鿿]", core)
    if len(cjk) == 1:
        toks.add(cjk[0])
    for i in range(len(cjk) - 1):
        toks.add(cjk[i] + cjk[i + 1])
    return toks


def _similar(a: str, b: str) -> float:
    """两子句内容相似度(Jaccard; 内容核完全相等 → 1.0)。用于循环论证的近同义判定。"""
    ca, cb = _core(a), _core(b)
    if ca and ca == cb:
        return 1.0
    sa, sb = _content_set(a), _content_set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _overlap(a: str, b: str) -> float:
    """重叠系数 |A∩B|/min(|A|,|B|)(内容核相等 → 1.0)。用于命题原子对齐:
    『现在下雨了』应与『下雨』视作同一原子, 而 Jaccard 会因长度差被低估。"""
    ca, cb = _core(a), _core(b)
    if ca and ca == cb:
        return 1.0
    sa, sb = _content_set(a), _content_set(b)
    if not sa or not sb:
        return 0.0
    m = min(len(sa), len(sb))
    return (len(sa & sb) / m) if m else 0.0


# --------------------------------------------------------------------------- #
# 线索词匹配(CJK 子串; 英文按词边界, 避免 'so' 命中 'season')
# --------------------------------------------------------------------------- #
def _cue_in(text: str, cue: str) -> bool:
    c = (cue or "").strip()
    if not c:
        return False
    if re.search(r"[一-鿿]", c):
        return c in (text or "")
    return re.search(r"\b" + re.escape(c.lower()) + r"\b", (text or "").lower()) is not None


def _contains_cue(clause: str, cues) -> bool:
    return any(_cue_in(clause, c) for c in cues)


def _starts_with_cue(clause: str, cues) -> bool:
    # 严格前缀匹配(剥前导空白/标点) —— 不可用「cue 出现在前 N 字」, 否则 '现在' 会被 '在' 误判。
    c = (clause or "").lstrip(" \t　,，、:：")
    cl = c.lower()
    for x in cues:
        xs = x.strip()
        if not xs:
            continue
        if re.search(r"[一-鿿]", xs):
            if c.startswith(xs):
                return True
        elif cl.startswith(xs.lower()):
            return True
    return False


def _strip_leading_cue(clause: str, cues) -> str:
    """剥掉子句开头的线索词(让结论回显不带「所以/因此」)。"""
    c = (clause or "").lstrip(" \t　,，、:：")
    for x in sorted((y.strip() for y in cues if y.strip()), key=len, reverse=True):
        if re.search(r"[一-鿿]", x):
            if c.startswith(x):
                return c[len(x):].lstrip(" \t　,，、:：")
        elif c.lower().startswith(x.lower()):
            return c[len(x):].lstrip(" \t　,，、:：")
    return c


def _split_at_conclusion_cue(sentence: str):
    """若句中有结论标记(所以/因此/therefore...)且前后都有内容 → 拆成 (理由, 结论)。"""
    s = sentence or ""
    best_idx, best_cue = None, None
    for cue in _CONCL_SPLIT_CUES:
        if re.search(r"[一-鿿]", cue):
            idx = s.find(cue)
        else:
            m = re.search(r"\b" + re.escape(cue.lower()) + r"\b", s.lower())
            idx = m.start() if m else -1
        if idx > 0 and (best_idx is None or idx < best_idx):
            best_idx, best_cue = idx, cue
    if best_idx is None:
        return None
    before = s[:best_idx].strip(" ，,、；;。.")
    after = s[best_idx + len(best_cue):].strip(" ，,、；;。.")
    if before and after:
        return before, after
    return None


# --------------------------------------------------------------------------- #
# 论证骨架解析(确定性)
# --------------------------------------------------------------------------- #
def _classify_into(clause: str, premises: list, conditions: list,
                   definitions: list, assumptions: list, conclusions: list) -> None:
    if not clause:
        return
    if _contains_cue(clause, _DEF_CUES):
        definitions.append(clause)
        return
    if _starts_with_cue(clause, _CONCL_CUES):
        conclusions.append(_strip_leading_cue(clause, _CONCL_CUES))
        return
    if _starts_with_cue(clause, _COND_CUES):
        conditions.append(clause)
        return
    if _starts_with_cue(clause, _ASSUME_CUES) or _contains_cue(clause, _ASSUME_CUES):
        assumptions.append(clause)
        premises.append(clause)
        return
    premises.append(clause)


def _parse(text: str) -> dict:
    """把一段论证文本拆成骨架。确定性、可复现。

    返回 {premises, conclusion, definitions, conditions, assumptions, steps, clauses}。
    """
    sentences = _segment(text)
    premises, conditions, definitions, assumptions, conclusions = [], [], [], [], []
    for s in sentences:
        split = _split_at_conclusion_cue(s)
        if split:
            before, after = split
            _classify_into(before, premises, conditions, definitions, assumptions, conclusions)
            conclusions.append(after)
            continue
        _classify_into(s, premises, conditions, definitions, assumptions, conclusions)
    # 无显式结论 → 取最后一个前提作结论。
    if not conclusions and premises:
        conclusions.append(premises.pop())
    conclusion = "；".join(conclusions) if conclusions else ""
    steps = _build_steps(premises, conditions, conclusion)
    return {
        "premises": premises,
        "conclusion": conclusion,
        "definitions": definitions,
        "conditions": conditions,
        "assumptions": assumptions,
        "steps": steps,
        "clauses": sentences,
    }


def _build_steps(premises: list, conditions: list, conclusion: str) -> list:
    """构造从前提到结论的推理步骤; 每步标注桥接到结论的支撑强度。"""
    steps = []
    n = 0
    for c in conditions:
        n += 1
        steps.append({"n": n, "from": _scrub_secrets(c), "rule": "condition"})
    for p in premises:
        n += 1
        sim = _similar(p, conclusion) if conclusion else 0.0
        steps.append({"n": n, "from": _scrub_secrets(p), "rule": "premise",
                      "supports_conclusion": round(sim, 3)})
    if conclusion:
        steps.append({"n": n + 1, "rule": "inference",
                      "to": _scrub_secrets(conclusion)})
    return steps


# --------------------------------------------------------------------------- #
# 隐含假设(确定性缺口分析)
# --------------------------------------------------------------------------- #
def _implicit_assumptions_core(parsed: dict) -> list:
    """找出未明说但推理要成立必须为真的桥接假设。"""
    out = []
    premises = parsed["premises"]
    conclusion = parsed["conclusion"]
    if not conclusion:
        return out
    concl_toks = _content_set(conclusion)
    prem_toks = set()
    for p in premises:
        prem_toks |= _content_set(p)
    ungrounded = [t for t in concl_toks if t not in prem_toks]
    if ungrounded and prem_toks:
        out.append({
            "kind": "missing_bridge",
            "text": ("结论引入了前提未提及的内容(" + "、".join(sorted(ungrounded)[:5])
                     + "), 隐含假设: 存在一条把它们连起来的前提。"),
            "tokens": sorted(ungrounded)[:8],
        })
    concl_universal = _contains_cue(conclusion, _UNIVERSAL)
    prem_particular = any(_contains_cue(p, _PARTICULAR) for p in premises)
    if concl_universal and (prem_particular or len(premises) <= 1):
        out.append({
            "kind": "representativeness",
            "text": "结论是全称判断, 但前提只给了个例/单一样本 —— 隐含假设: 这些样本能代表全体。",
        })
    if _contains_cue(conclusion, _CAUSAL) or any(_contains_cue(p, _CAUSAL) for p in premises):
        out.append({
            "kind": "causal_mechanism",
            "text": "推理用到因果关系, 隐含假设: 因果方向正确、不存在第三共因、非巧合相关。",
        })
    if parsed["conditions"]:
        out.append({
            "kind": "condition_satisfied",
            "text": ("推理带适用条件, 隐含假设: 这些条件在当前情形下确实满足。"),
        })
    return out


# --------------------------------------------------------------------------- #
# 命题层模型 + 反模型搜索(确定性真值表枚举)
# --------------------------------------------------------------------------- #
# 条件句模式: (regex, 方向)。方向 'fwd' = 前件→后件; 'rev' = 后件→前件。
_IMPL_PATTERNS = [
    (re.compile(r"(?:如果|若|假如|倘若|要是)(.+?)(?:那么|则|就|，|,)(.+)"), "fwd"),
    (re.compile(r"只要(.+?)(?:就|，|,)(.+)"), "fwd"),
    (re.compile(r"只有(.+?)才(.+)"), "rev"),
    (re.compile(r"(?i)\bif\b(.+?)(?:,|\bthen\b)(.+)"), "fwd"),
    (re.compile(r"(.+?)(?:导致|引起|使得|造成|促成|leads to|causes|results in)(.+)"), "fwd"),
    (re.compile(r"(.+?)(?<![原否规法守准细])则(.+)"), "fwd"),
]


class _Atom:
    """一个命题原子(由若干同义子句对齐而成)。"""

    __slots__ = ("idx", "core", "samples")

    def __init__(self, idx: int, core: str, sample: str):
        self.idx = idx
        self.core = core
        self.samples = [sample]


class _Model:
    """命题模型: 原子集 + 事实(原子→真值) + 蕴含边 + 结论。"""

    def __init__(self):
        self.atoms = []          # list[_Atom]
        self.facts = []          # list[(atom_idx, bool)]
        self.impls = []          # list[(ant_idx, ant_pol, cons_idx, cons_pol)]
        self.conclusion = None   # (atom_idx, bool) | None

    def atom_for(self, clause: str) -> int:
        """把子句对齐到已有原子(重叠系数≥0.66)或新建。"""
        best, best_sim = -1, 0.0
        for a in self.atoms:
            s = _overlap(clause, a.samples[0])
            if s > best_sim:
                best, best_sim = a.idx, s
        if best_sim >= 0.66 and best >= 0:
            self.atoms[best].samples.append(clause)
            return best
        idx = len(self.atoms)
        self.atoms.append(_Atom(idx, _core(clause), clause))
        return idx


def _split_impl(clause: str):
    """若子句是条件/因果句 → 返回 (antecedent, consequent, 方向); 否则 None。"""
    c = clause or ""
    for rx, direction in _IMPL_PATTERNS:
        m = rx.search(c)
        if m and m.group(1).strip() and m.group(2).strip():
            return m.group(1).strip(), m.group(2).strip(), direction
    return None


def _build_prop_model(parsed: dict) -> _Model:
    """从骨架抽命题模型(蕴含 + 事实 + 结论)。"""
    model = _Model()
    impl_sources = list(parsed["conditions"]) + list(parsed["premises"])
    for cl in impl_sources:
        sp = _split_impl(cl)
        if sp is None:
            continue
        ant, cons, direction = sp
        ant_pol = not _has_neg(ant)
        cons_pol = not _has_neg(cons)
        ai = model.atom_for(ant)
        ci = model.atom_for(cons)
        if ai == ci:
            continue
        if direction == "rev":
            model.impls.append((ci, cons_pol, ai, ant_pol))
        else:
            model.impls.append((ai, ant_pol, ci, cons_pol))
    for cl in parsed["premises"]:
        if _split_impl(cl) is not None:
            continue   # 条件/因果前提已作蕴含, 不再作事实
        fi = model.atom_for(cl)
        model.facts.append((fi, not _has_neg(cl)))
    if parsed["conclusion"]:
        cpol = not _has_neg(parsed["conclusion"])
        cidx = model.atom_for(parsed["conclusion"])
        model.conclusion = (cidx, cpol)
    return model


def _eval_model(model: _Model, assignment: list) -> dict:
    """给定真值赋值, 算 premises 是否全真、conclusion 真值。"""
    facts_ok = all(assignment[ai] == val for ai, val in model.facts)
    impls_ok = True
    for ai, apol, ci, cpol in model.impls:
        ant = (assignment[ai] == apol)
        cons = (assignment[ci] == cpol)
        if ant and not cons:        # 前件真而后件假 → 蕴含为假
            impls_ok = False
            break
    premises_true = facts_ok and impls_ok
    concl_true = None
    if model.conclusion is not None:
        ci, cpol = model.conclusion
        concl_true = (assignment[ci] == cpol)
    return {"premises_true": premises_true, "conclusion_true": concl_true}


def _check_validity_core(parsed: dict) -> dict:
    """命题层有效性判定 + 反模型搜索(真值表枚举, 原子≤12)。

    返回 {decidable, valid, reason, countermodel, form, atoms}。
    decidable=False 表示无法抽出足够命题结构, 退回结构启发式。
    """
    model = _build_prop_model(parsed)
    n = len(model.atoms)
    has_struct = bool(model.impls) and model.conclusion is not None
    if not has_struct or n == 0 or n > 12:
        return _structural_verdict(parsed)

    atom_labels = [{"id": a.idx, "core": a.core,
                    "sample": _scrub_secrets(a.samples[0])} for a in model.atoms]
    premise_models = 0
    countermodel = None
    for mask in range(1 << n):
        assignment = [bool((mask >> i) & 1) for i in range(n)]
        ev = _eval_model(model, assignment)
        if not ev["premises_true"]:
            continue
        premise_models += 1
        if ev["conclusion_true"] is False and countermodel is None:
            countermodel = {atom_labels[i]["sample"]: assignment[i] for i in range(n)}

    if premise_models == 0:
        return {"decidable": True, "valid": True, "form": "premises_unsatisfiable",
                "reason": "前提在命题层不可同时为真(自相矛盾) —— 推理空洞有效, 但前提本身有问题。",
                "countermodel": None, "atoms": atom_labels}
    if countermodel is not None:
        return {"decidable": True, "valid": False, "form": _name_invalid_form(model),
                "reason": "存在一个真值赋值使全部前提为真而结论为假 —— 结论不被前提蕴含(无效)。",
                "countermodel": countermodel, "atoms": atom_labels}
    return {"decidable": True, "valid": True, "form": _name_valid_form(model),
            "reason": "在所有使前提为真的赋值下结论都为真 —— 结论被前提蕴含(命题层有效)。",
            "countermodel": None, "atoms": atom_labels}


def _name_valid_form(model: _Model) -> str:
    if len(model.impls) == 1 and len(model.facts) == 1 and model.conclusion is not None:
        ai, apol, ci, cpol = model.impls[0]
        fi, fval = model.facts[0]
        cci, ccpol = model.conclusion
        if fi == ai and cci == ci:
            return "modus_ponens(肯定前件)"
        if fi == ci and cci == ai:
            return "modus_tollens(否定后件)"
    if len(model.impls) >= 2 and not model.facts:
        return "hypothetical_syllogism(假言三段论)"
    return "valid(命题层有效)"


def _name_invalid_form(model: _Model) -> str:
    if len(model.impls) == 1 and len(model.facts) == 1 and model.conclusion is not None:
        ai, apol, ci, cpol = model.impls[0]
        fi, fval = model.facts[0]
        cci, ccpol = model.conclusion
        if fi == ci and cci == ai:
            return "affirming_the_consequent(肯定后件 — 无效)"
        if fi == ai and cci == ci:
            return "denying_the_antecedent(否定前件 — 无效)"
    return "non_sequitur(结论不被蕴含 — 无效)"


def _structural_verdict(parsed: dict) -> dict:
    """抽不出命题结构时的退路: 用前提/结论 token 覆盖度软判。"""
    conclusion = parsed["conclusion"]
    premises = parsed["premises"]
    if not conclusion:
        return {"decidable": False, "valid": None, "form": "no_conclusion",
                "reason": "未识别出结论, 无法判定有效性。", "countermodel": None, "atoms": []}
    if not premises:
        return {"decidable": False, "valid": None, "form": "no_premises",
                "reason": "未识别出任何前提 —— 结论是无支撑断言。", "countermodel": None,
                "atoms": []}
    best = max((_similar(p, conclusion) for p in premises), default=0.0)
    concl_toks = _content_set(conclusion)
    prem_toks = set()
    for p in premises:
        prem_toks |= _content_set(p)
    coverage = (len(concl_toks & prem_toks) / len(concl_toks)) if concl_toks else 0.0
    if coverage < 0.34 and best < 0.34:
        return {"decidable": False, "valid": None, "form": "likely_non_sequitur",
                "reason": ("结论与前提几乎无内容重叠(覆盖率 %.0f%%) —— 疑似结论跳跃/非逻辑后承; "
                           "命题结构不足以做形式判定。" % (coverage * 100)),
                "countermodel": None, "atoms": [], "coverage": round(coverage, 3)}
    return {"decidable": False, "valid": None, "form": "informal",
            "reason": ("无法抽出足够命题结构做形式判定; 前提对结论的内容覆盖率约 %.0f%%, "
                       "属于非形式论证, 谬误扫描仍适用。" % (coverage * 100)),
            "countermodel": None, "atoms": [], "coverage": round(coverage, 3)}


# --------------------------------------------------------------------------- #
# world_model / mission_control best-effort 接入(只读; 缺 → deferred)
# --------------------------------------------------------------------------- #
def _fact_conflicts(text: str, parsed: dict) -> dict:
    """查论证是否与 world_model 已知事实冲突(只读, best-effort)。缺 → deferred。"""
    wm = _opt("world_model")
    if wm is None or not hasattr(wm, "query"):
        return {"ok": False, "deferred": "world_model"}
    claim = parsed.get("conclusion") or text
    try:
        q = wm.query(_scrub_secrets(claim)[:200], k=6)
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    if not isinstance(q, dict) or not q.get("ok"):
        return {"ok": False, "deferred": "world_model"}
    conflicts = []
    for ent in (q.get("entities") or [])[:6]:
        name = ent.get("name") if isinstance(ent, dict) else None
        if not name or not hasattr(wm, "explain"):
            continue
        try:
            ex = wm.explain(name)
        except Exception:
            continue
        if isinstance(ex, dict) and ex.get("ok") and ex.get("contradicts"):
            conflicts.append({"entity": name, "contradicts": ex.get("contradicts")})
    names = [e.get("name") for e in (q.get("entities") or []) if isinstance(e, dict)][:6]
    return {"ok": True, "checked_entities": names,
            "conflicts": conflicts, "conflict_count": len(conflicts)}


def _goal_alignment(text: str) -> dict:
    """查论证/行动是否与活跃目标对齐(mission_control 只读, best-effort)。缺 → deferred。"""
    mc = _opt("mission_control")
    if mc is None or not hasattr(mc, "list_goals"):
        return {"ok": False, "deferred": "mission_control"}
    try:
        goals = mc.list_goals(status="active")
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}
    titles = [g.get("title", "?") for g in (goals or []) if isinstance(g, dict)][:6]
    return {"ok": True, "active_goals": len(goals or []), "goal_titles": titles}


def _connect_cores(metadata: dict) -> dict:
    """把判决**元数据**(非正文)best-effort 喂给已落地的认知内核; 缺 → 记 deferred。

    约定钩子(若内核日后落地且暴露): reasoning_core.record_judgment(payload),
    metacognition_core.note_gap(payload)。绝不传论证正文, 只传 hash/计数/判决。
    """
    notified, deferred = [], []
    hooks = (("reasoning_core", "record_judgment"),
             ("metacognition_core", "note_gap"))
    for mod_name, fn_name in hooks:
        mod = _opt(mod_name)
        if mod is None or not hasattr(mod, fn_name):
            deferred.append(mod_name)
            continue
        try:
            getattr(mod, fn_name)(dict(metadata))
            notified.append(mod_name)
        except Exception:
            deferred.append(mod_name)
    return {"notified": notified, "deferred": deferred}


# --------------------------------------------------------------------------- #
# 谬误扫描(确定性规则; 7 类命名谬误 + 若干线索词谬误)
# --------------------------------------------------------------------------- #
def _scan_fallacies(parsed: dict, text: str, validity: dict,
                    fact_check: dict | None = None) -> list:
    """扫论证里的逻辑谬误。返回 [{code, name, severity, confidence, where, explain}]。"""
    out = []
    premises = parsed["premises"]
    conclusion = parsed["conclusion"]

    # 1. 循环论证: 某前提与结论高度同义(把结论当前提)。
    for p in premises:
        sim = _similar(p, conclusion) if conclusion else 0.0
        if sim >= 0.85 and conclusion:
            out.append(_fal("circular_reasoning", "循环论证", "high", round(sim, 2),
                            _scrub_secrets(p),
                            "该前提与结论几乎同义 —— 用结论本身充当理由, 没提供独立支撑。"))
            break

    # 2. 以偏概全: 全称结论 + 个例/单一前提。
    if conclusion and _contains_cue(conclusion, _UNIVERSAL):
        particular = any(_contains_cue(p, _PARTICULAR) for p in premises)
        if particular or len(premises) <= 1:
            out.append(_fal("hasty_generalization", "以偏概全", "high", 0.75,
                            _scrub_secrets(conclusion),
                            "从个例/单一样本跳到全称结论, 样本不足以支撑「所有/总是」级别的断言。"))

    # 3. 因果倒置 / 相关当因果。
    causal_in_concl = bool(conclusion) and _contains_cue(conclusion, _CAUSAL)
    corr_only = any(_contains_cue(p, _CORRELATION) for p in premises)
    if causal_in_concl or any(_contains_cue(p, _CAUSAL) for p in premises):
        has_mechanism = any(_contains_cue(p, ("机制", "原理", "实验", "因为", "由于"))
                            for p in premises)
        if corr_only and not has_mechanism:
            out.append(_fal("reversed_or_false_causation", "因果倒置/相关误作因果", "med", 0.6,
                            _scrub_secrets(conclusion or text[:40]),
                            "前提只给了相关/同时出现, 却断言因果 —— 可能因果方向反了, 或存在共因。"))

    # 4. 前提不足 / 缺桥接(结论引入前提未覆盖的内容)。
    if conclusion and premises:
        concl_toks = _content_set(conclusion)
        prem_toks = set()
        for p in premises:
            prem_toks |= _content_set(p)
        if concl_toks:
            coverage = len(concl_toks & prem_toks) / len(concl_toks)
            ungrounded = sorted([t for t in concl_toks if t not in prem_toks])[:5]
            if coverage < 0.5 and ungrounded:
                out.append(_fal("insufficient_premises", "前提不足/缺桥接", "med",
                                round(1 - coverage, 2), "、".join(ungrounded),
                                ("结论引入了前提未提供的内容(" + "、".join(ungrounded)
                                 + "), 缺一条把它们连起来的桥接前提。")))

    # 5. 结论跳跃 / 无效形式(由有效性判定指向)。
    form = validity.get("form", "")
    if "non_sequitur" in form or form == "likely_non_sequitur":
        out.append(_fal("non_sequitur", "结论跳跃", "high",
                        0.8 if validity.get("decidable") else 0.55,
                        _scrub_secrets(conclusion or ""),
                        validity.get("reason", "结论不从前提逻辑地得出。")))
    elif form.startswith("affirming_the_consequent"):
        out.append(_fal("affirming_consequent", "肯定后件(无效推理)", "high", 0.9,
                        _scrub_secrets(conclusion or ""),
                        "P→Q 且 Q, 推不出 P —— 这是肯定后件谬误。"))
    elif form.startswith("denying_the_antecedent"):
        out.append(_fal("denying_antecedent", "否定前件(无效推理)", "high", 0.9,
                        _scrub_secrets(conclusion or ""),
                        "P→Q 且 ¬P, 推不出 ¬Q —— 这是否定前件谬误。"))

    # 6. 偷换概念(粗启发式, 低置信)。
    eq = _detect_equivocation(parsed)
    if eq:
        out.append(eq)

    # 7. 与已知事实冲突(world_model best-effort)。
    if fact_check and fact_check.get("ok") and fact_check.get("conflict_count", 0) > 0:
        out.append(_fal("conflicts_known_facts", "与已知事实冲突", "high", 0.7,
                        "; ".join(str(c.get("entity")) for c in fact_check["conflicts"][:3]),
                        "结论/前提与 world_model 已知事实存在矛盾边(contradicts)。"))

    out.extend(_cue_word_fallacies(text))

    # 去重(同 code 保留最高置信)。
    dedup = {}
    for f in out:
        k = f["code"]
        if k not in dedup or f["confidence"] > dedup[k]["confidence"]:
            dedup[k] = f
    return sorted(dedup.values(), key=lambda x: -x["confidence"])


def _fal(code, name, severity, confidence, where, explain) -> dict:
    return {"code": code, "name": name, "severity": severity,
            "confidence": float(confidence), "where": where, "explain": explain}


def _detect_equivocation(parsed: dict):
    """偷换概念粗检: 某内容 token 跨多子句出现, 但所在子句两两差异极大(疑似一词多义)。"""
    clauses = parsed["premises"] + ([parsed["conclusion"]] if parsed["conclusion"] else [])
    if len(clauses) < 2:
        return None
    token_clauses = {}
    for cl in clauses:
        for t in _content_set(cl):
            token_clauses.setdefault(t, []).append(cl)
    for t, cls in token_clauses.items():
        if len(cls) >= 2 and len(t) >= 2:
            sims = []
            for i in range(len(cls)):
                for j in range(i + 1, len(cls)):
                    sims.append(_similar(cls[i], cls[j]))
            if sims and max(sims) < 0.3:
                return _fal("equivocation", "偷换概念(疑似)", "med", 0.45, t,
                            ("关键词「" + t + "」在不同子句的语境差异很大, "
                             "可能前后指代不同含义。"))
    return None


_CUE_FALLACIES = [
    ("appeal_to_authority", "诉诸权威",
     ("专家说", "权威认为", "大家都说", "according to experts")),
    ("appeal_to_emotion", "诉诸情感",
     ("难道你不", "为了孩子", "你忍心", "想想那些", "think of the")),
    ("ad_hominem", "人身攻击",
     ("他这种人", "你懂什么", "没资格", "you're just")),
    ("slippery_slope", "滑坡谬误",
     ("最终会", "必然导致", "一步步走向", "迟早会", "will inevitably lead")),
    ("false_dilemma", "非黑即白",
     ("要么", "只有两种", "either or", "no other choice")),
    ("appeal_to_tradition", "诉诸传统",
     ("一直都是这样", "老祖宗", "自古", "traditionally we")),
]


def _cue_word_fallacies(text: str) -> list:
    out = []
    t = text or ""
    tl = t.lower()
    for code, name, cues in _CUE_FALLACIES:
        for c in cues:
            cc = c.lower()
            if (cc in tl) or (c in t):
                out.append(_fal(code, name, "low", 0.4, c,
                                ("出现「" + c + "」式表达 —— 可能在用 " + name + " 替代论证。")))
                break
    return out


# --------------------------------------------------------------------------- #
# 反例生成(确定性模板 + world_model best-effort)
# --------------------------------------------------------------------------- #
def _counterexamples_core(parsed: dict, fact_check: dict | None) -> list:
    out = []
    conclusion = parsed["conclusion"]
    if not conclusion:
        return out
    if _contains_cue(conclusion, _UNIVERSAL):
        out.append({
            "kind": "universal_counterexample",
            "text": "结论是全称判断, 只需举出**一个反例**即可推翻: 找一个满足主语条件却不满足谓语的个体。",
            "template": "存在一个 X, 使「" + _scrub_secrets(conclusion)[:50] + "」不成立。",
        })
    if _contains_cue(conclusion, _CAUSAL) or any(_contains_cue(p, _CAUSAL)
                                                 for p in parsed["premises"]):
        out.append({
            "kind": "confound_counterexample",
            "text": "因果主张的反例: 找一个有结果却无所述原因的情形, 或一个共因同时驱动两者的例子。",
        })
    if fact_check and fact_check.get("ok") and fact_check.get("conflicts"):
        out.append({
            "kind": "known_fact_counterexample",
            "text": "world_model 中存在与之矛盾的已知事实(见 contradicts), 可直接作反例。",
            "facts": fact_check["conflicts"][:3],
        })
    if not out:
        out.append({
            "kind": "generic",
            "text": "构造反例: 设法让全部前提为真而结论为假的具体情形(见 countermodel 的命题层赋值)。",
        })
    return out


# --------------------------------------------------------------------------- #
# 修补建议 + 缺口解释(确定性)
# --------------------------------------------------------------------------- #
_REPAIR_MAP = {
    "circular_reasoning": "用**独立证据**替换那条把结论当理由的前提; 结论不能出现在自己的支撑里。",
    "hasty_generalization": "把结论从「所有/总是」收窄成「在 X 条件下/部分」, 或补足够多、有代表性的样本前提。",
    "reversed_or_false_causation": "验证因果方向(时间先后)、排除共因、用对照/实验区分相关与因果。",
    "insufficient_premises": "补一条桥接前提, 把结论里悬空的内容连回前提。",
    "non_sequitur": "补中间推理步骤, 显式连接前提与结论的关键词; 否则结论站不住。",
    "affirming_consequent": "改成肯定前件(P→Q 且 P ⊢ Q), 或补充「仅当 P 才 Q」式的双向条件。",
    "denying_antecedent": "改成否定后件(P→Q 且 ¬Q ⊢ ¬P); 否定前件推不出否定后件。",
    "equivocation": "统一关键词定义, 全程同一含义; 若确需两义, 拆成两个不同的词。",
    "conflicts_known_facts": "修正与已知事实冲突的前提/结论, 或给出推翻该已知事实的新证据。",
    "appeal_to_authority": "用证据本身替代「权威背书」; 权威也可能错。",
    "appeal_to_emotion": "剥离情绪修辞, 只留可检验的事实与推理。",
    "ad_hominem": "对事不对人: 评判主张本身, 而非提出者。",
    "slippery_slope": "为每一步因果链补独立证据, 别假设第一步必然滑到最后。",
    "false_dilemma": "列出被忽略的第三/更多选项, 打破「只有两种」的预设。",
    "appeal_to_tradition": "「一直如此」不等于「应当如此」; 用当下的理由重新论证。",
}


def _repair_core(fallacies: list, validity: dict, assumptions: list) -> list:
    out = []
    for f in fallacies:
        fix = _REPAIR_MAP.get(f["code"])
        if fix:
            out.append({"target": f["name"], "code": f["code"], "fix": fix})
    for a in assumptions:
        if a.get("kind") == "missing_bridge":
            out.append({"target": "缺桥接假设", "code": "missing_bridge",
                        "fix": "显式写出隐含的桥接前提: " + a.get("text", "")})
    if validity.get("decidable") and validity.get("valid") is False and not out:
        out.append({"target": "无效形式", "code": validity.get("form", ""),
                    "fix": validity.get("reason", "") + " 按有效形式重排前提与结论。"})
    if not out:
        out.append({"target": "无明显缺陷", "code": "ok",
                    "fix": "未检出谬误/无效形式; 仍建议显式列出隐含假设与适用条件以增强可检验性。"})
    return out


def _explain_gap_core(parsed: dict, validity: dict, fallacies: list,
                      assumptions: list) -> dict:
    """指出推理链最薄弱的一环及原因。"""
    if validity.get("decidable") and validity.get("valid") is False:
        return {"gap": "formal_invalid", "form": validity.get("form"),
                "where": "前提→结论的形式推理",
                "explain": validity.get("reason"),
                "countermodel": validity.get("countermodel")}
    if fallacies:
        top = fallacies[0]
        return {"gap": top["code"], "where": top["where"],
                "explain": top["explain"], "severity": top["severity"],
                "confidence": top["confidence"]}
    for a in assumptions:
        if a.get("kind") == "missing_bridge":
            return {"gap": "missing_bridge", "where": "前提与结论之间",
                    "explain": a.get("text")}
    if validity.get("coverage", 1.0) < 0.34:
        return {"gap": "low_coverage", "where": "前提对结论的内容覆盖",
                "explain": validity.get("reason")}
    return {"gap": "none", "where": "",
            "explain": "未发现明显逻辑缺口; 推理链在可检测范围内成立(注意: 形式有效≠前提为真)。"}


# --------------------------------------------------------------------------- #
# 公开动作(全部纯只读分析; 输出带隐私标志)
# --------------------------------------------------------------------------- #
def _base_meta(text: str, parsed: dict) -> dict:
    return {"claim_hash": _hash(text),
            "n_premises": len(parsed["premises"]),
            "n_conditions": len(parsed["conditions"]),
            "n_assumptions": len(parsed["assumptions"])}


def parse_argument(text: str) -> dict:
    """把一段论证拆成: 前提/定义/隐含假设/推理步骤/结论/适用条件。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    implicit = _implicit_assumptions_core(parsed)
    out = {
        "ok": True,
        "premises": [_scrub_secrets(p) for p in parsed["premises"]],
        "definitions": [_scrub_secrets(d) for d in parsed["definitions"]],
        "conditions": [_scrub_secrets(c) for c in parsed["conditions"]],
        "stated_assumptions": [_scrub_secrets(a) for a in parsed["assumptions"]],
        "implicit_assumptions": implicit,
        "steps": parsed["steps"],
        "conclusion": _scrub_secrets(parsed["conclusion"]),
    }
    _audit("parse_argument", _hash(text), n_premises=len(parsed["premises"]),
           private=1 if assess["private"] else 0)
    return _flag_private(out, assess)


def extract_premises(text: str) -> dict:
    """只抽明示前提(含条件前提)。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    out = {"ok": True,
           "premises": [_scrub_secrets(p) for p in parsed["premises"]],
           "conditions": [_scrub_secrets(c) for c in parsed["conditions"]],
           "conclusion": _scrub_secrets(parsed["conclusion"])}
    _audit("extract_premises", _hash(text), n_premises=len(parsed["premises"]))
    return _flag_private(out, assess)


def implicit_assumptions(text: str) -> dict:
    """挖出未明说、但推理要成立必须为真的隐含假设。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    implicit = _implicit_assumptions_core(parsed)
    pf = _opt("problem_framer")
    pf_note = None
    if pf is not None and hasattr(pf, "implicit_frame"):
        try:
            pf_note = pf.implicit_frame(_scrub_secrets(text)[:200])
        except Exception:
            pf_note = None
    out = {"ok": True, "implicit_assumptions": implicit,
           "stated_assumptions": [_scrub_secrets(a) for a in parsed["assumptions"]]}
    out["problem_framer"] = pf_note if pf_note is not None else {"deferred": True}
    _audit("implicit_assumptions", _hash(text), n=len(implicit))
    return _flag_private(out, assess)


def check_validity(text: str, *, with_facts: bool = True) -> dict:
    """判定结论是否从前提逻辑地得出(命题层真值表; 抽不出结构则退结构启发式)。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    fact_check = (_fact_conflicts(text, parsed) if with_facts
                  else {"ok": False, "deferred": "world_model"})
    out = {"ok": True,
           "decidable": validity["decidable"],
           "valid": validity["valid"],
           "form": validity["form"],
           "reason": validity["reason"],
           "countermodel": validity.get("countermodel"),
           "atoms": validity.get("atoms", []),
           "fact_conflict": fact_check}
    meta = _base_meta(text, parsed)
    meta.update({"valid": validity["valid"], "form": validity["form"]})
    _connect_cores(meta)
    _audit("check_validity", _hash(text), valid=(1 if validity["valid"] else 0),
           decidable=(1 if validity["decidable"] else 0), form=validity["form"])
    return _flag_private(out, assess)


def fallacy_scan(text: str, *, with_facts: bool = True) -> dict:
    """扫论证里的逻辑谬误(7 类命名 + 线索词级)。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    fact_check = _fact_conflicts(text, parsed) if with_facts else None
    fallacies = _scan_fallacies(parsed, text, validity, fact_check)
    out = {"ok": True, "fallacy_count": len(fallacies), "fallacies": fallacies,
           "fact_conflict": fact_check or {"ok": False, "deferred": "world_model"}}
    _audit("fallacy_scan", _hash(text), n_fallacies=len(fallacies),
           codes=",".join(f["code"] for f in fallacies)[:120])
    meta = _base_meta(text, parsed)
    meta["n_fallacies"] = len(fallacies)
    _connect_cores(meta)
    return _flag_private(out, assess)


def counterexample(text: str) -> dict:
    """为论证生成反例候选(全称/因果/与已知事实冲突)。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    fact_check = _fact_conflicts(text, parsed)
    cands = _counterexamples_core(parsed, fact_check)
    out = {"ok": True, "counterexamples": cands, "fact_conflict": fact_check}
    _audit("counterexample", _hash(text), n=len(cands))
    return _flag_private(out, assess)


def countermodel(text: str) -> dict:
    """命题层反模型: 找一个使全部前提为真、结论为假的真值赋值(存在即证无效)。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    if not validity["decidable"]:
        out = {"ok": True, "found": False, "decidable": False,
               "reason": validity["reason"], "atoms": validity.get("atoms", [])}
    elif validity.get("countermodel"):
        out = {"ok": True, "found": True, "decidable": True,
               "countermodel": validity["countermodel"], "form": validity["form"],
               "reason": "该赋值下前提全真、结论假 —— 推理无效。", "atoms": validity["atoms"]}
    else:
        out = {"ok": True, "found": False, "decidable": True,
               "reason": "不存在使前提真而结论假的赋值 —— 命题层有效, 无反模型。",
               "form": validity["form"], "atoms": validity["atoms"]}
    _audit("countermodel", _hash(text), found=(1 if out.get("found") else 0))
    return _flag_private(out, assess)


def proof_trace(text: str) -> dict:
    """渲染从前提到结论的推理轨迹, 逐步标注规则/缺口。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    trace = []
    for c in parsed["conditions"]:
        trace.append({"line": len(trace) + 1, "kind": "condition", "text": _scrub_secrets(c)})
    for p in parsed["premises"]:
        trace.append({"line": len(trace) + 1, "kind": "premise", "text": _scrub_secrets(p)})
    status_ = ("valid" if validity.get("valid") else
               ("invalid" if validity.get("valid") is False else "informal/indeterminate"))
    trace.append({"line": len(trace) + 1, "kind": "conclusion",
                  "text": _scrub_secrets(parsed["conclusion"]),
                  "by": validity.get("form"), "status": status_,
                  "justification": validity.get("reason")})
    out = {"ok": True, "trace": trace, "form": validity.get("form"),
           "valid": validity.get("valid"), "decidable": validity.get("decidable"),
           "countermodel": validity.get("countermodel")}
    _audit("proof_trace", _hash(text), lines=len(trace))
    return _flag_private(out, assess)


def repair_reasoning(text: str, *, with_facts: bool = True) -> dict:
    """针对检出的谬误/无效/缺口, 给最小修补建议。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    fact_check = _fact_conflicts(text, parsed) if with_facts else None
    fallacies = _scan_fallacies(parsed, text, validity, fact_check)
    assumptions = _implicit_assumptions_core(parsed)
    repairs = _repair_core(fallacies, validity, assumptions)
    out = {"ok": True, "repairs": repairs,
           "detected_fallacies": [f["code"] for f in fallacies],
           "valid": validity.get("valid"), "form": validity.get("form")}
    _audit("repair_reasoning", _hash(text), n_repairs=len(repairs))
    return _flag_private(out, assess)


def explain_gap(text: str, *, with_facts: bool = True) -> dict:
    """指出推理链最薄弱的一环及为什么站不住。"""
    if not (text or "").strip():
        return {"ok": False, "error": "空论证文本"}
    parsed = _parse(text)
    assess = _privacy_assess(text)
    validity = _check_validity_core(parsed)
    fact_check = _fact_conflicts(text, parsed) if with_facts else None
    fallacies = _scan_fallacies(parsed, text, validity, fact_check)
    assumptions = _implicit_assumptions_core(parsed)
    gap = _explain_gap_core(parsed, validity, fallacies, assumptions)
    out = {"ok": True, "gap": gap, "conclusion": _scrub_secrets(parsed["conclusion"])}
    _audit("explain_gap", _hash(text), gap=gap.get("gap"))
    return _flag_private(out, assess)


def audit() -> dict:
    """隐私/安全自审(只读, 不外泄正文)。验证: 审计流只存元数据、绝不落论证正文/凭据。"""
    path = _audit_path()
    n_records = 0
    n_private = 0
    leaked_body = 0
    leaked_secret = 0
    tail = []
    allowed_keys = {"ts", "action", "claim_hash", "n_premises", "n_conditions",
                    "n_assumptions", "private", "valid", "decidable", "form", "n",
                    "n_fallacies", "codes", "found", "lines", "gap", "n_repairs"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            n_records += 1
            if rec.get("private"):
                n_private += 1
            for k, v in rec.items():
                if k not in allowed_keys:
                    leaked_body += 1
                if isinstance(v, str) and _SECRET_RX.search(v):
                    leaked_secret += 1
        for line in lines[-15:]:
            try:
                rec = json.loads(line)
                tail.append({"ts": rec.get("ts"), "action": rec.get("action")})
            except Exception:
                continue
    except OSError:
        pass
    out = {
        "ok": True,
        "storage": {"dir": _data_dir(), "audit": path},
        "counts": {"records": n_records, "private_records": n_private},
        "self_check": {
            "credential_strings_in_audit": leaked_secret,   # 期望恒 0
            "unexpected_body_fields": leaked_body,          # 期望恒 0
        },
        "guarantees": [
            "全本机, 零真实副作用(不执行/不删/不发/不入审批)",
            "审计流只记 hash/计数/判决, 绝不落论证正文",
            "凭据/密钥串回显前一律脱敏; 私密内容标 _private/_force_local 切本机",
            "命题层判定/反模型搜索为离线确定性算法, 不依赖任何 LLM",
        ],
        "recent_audit": tail,
    }
    if leaked_secret or leaked_body:
        out["WARN"] = "审计流出现疑似正文/凭据字段(应为 0) —— 需排查。"
    return out


def status() -> dict:
    """总览: 动作清单 + 集成可用性 + deferred 内核 + 存储位置。"""
    return {
        "ok": True,
        "module": "logic_proof_core",
        "role": "形式逻辑推理审判器(纯只读分析门面 + 离线确定性逻辑内核)",
        "actions": ["parse_argument", "extract_premises", "implicit_assumptions",
                    "check_validity", "fallacy_scan", "counterexample", "countermodel",
                    "proof_trace", "repair_reasoning", "explain_gap", "audit", "status"],
        "fallacy_families": ["偷换概念", "循环论证", "因果倒置", "以偏概全", "前提不足",
                             "结论跳跃", "与已知事实冲突",
                             "(+线索词级)诉诸权威/情感/人身攻击/滑坡/非黑即白/诉诸传统"],
        "engines": _engines_available(),
        "deferred": _deferred_status(),
        "storage": {"dir": _data_dir(), "audit": _audit_path()},
        "model_tools": sorted(_DISPATCH_WHITELIST),
        "note": "让小美在说为什么之前先证明推理链站得住; 注意: 形式有效不等于前提为真。",
    }


# --------------------------------------------------------------------------- #
# LLM 工具面(只读且冻结): 分析类全开放, audit(隐私自审)CLI-only。
# --------------------------------------------------------------------------- #
_DISPATCH_WHITELIST = {"parse_argument", "extract_premises", "implicit_assumptions",
                       "check_validity", "fallacy_scan", "counterexample",
                       "countermodel", "proof_trace", "repair_reasoning",
                       "explain_gap", "status"}

_LPC_TOOLS = [
    {"type": "function", "function": {
        "name": "logic_proof_core_parse_argument",
        "description": ("把一段论证(你的回答/计划/归类理由/业务判断/行动解释)拆成前提、定义、"
                        "隐含假设、推理步骤、结论、适用条件。给出理由之前先拆给自己看。只读。"),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要审查的论证原文"}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_check_validity",
        "description": ("判定结论是否从前提逻辑地得出(命题层真值表; 无效会给反模型)。"
                        "下结论前自检推理链是否成立。只读。"),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_fallacy_scan",
        "description": ("扫论证里的逻辑谬误: 偷换概念/循环论证/因果倒置/以偏概全/前提不足/"
                        "结论跳跃/与已知事实冲突等。只读。"),
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_counterexample",
        "description": "为论证(尤其全称/因果主张)生成反例候选, 含 world_model 已知矛盾事实。只读。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_countermodel",
        "description": "命题层反模型: 找使前提全真而结论假的真值赋值(存在即证推理无效)。只读。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_proof_trace",
        "description": "渲染从前提到结论的推理轨迹, 逐行标注规则与缺口/有效性。只读。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_repair_reasoning",
        "description": "针对检出的谬误/无效/缺口给最小修补建议(怎么把推理链补成立)。只读。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_explain_gap",
        "description": "指出推理链最薄弱的一环及为什么站不住(给出理由前先补这里)。只读。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "logic_proof_core_status",
        "description": "看逻辑审判器总览(动作清单 + 集成可用性 + deferred 内核)。只读。",
        "parameters": {"type": "object", "properties": {}}}},
]


def approved_tools() -> list:
    if not config.get("logic_proof_core.enabled", True) or \
            not config.get("logic_proof_core.expose_tools", True):
        return []
    return list(_LPC_TOOLS)


def is_tool(name: str) -> bool:
    return name.startswith("logic_proof_core_") and \
        name.replace("logic_proof_core_", "") in _DISPATCH_WHITELIST


def dispatch(name: str, args: dict | None = None) -> dict:
    """jarvis_brain 工具入口。只放行只读分析动作; audit(隐私自审)走 CLI。"""
    args = dict(args or {})
    short = name.replace("logic_proof_core_", "")
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": ("动作 '" + short + "' 不在 logic_proof_core 模型白名单 "
                          + str(sorted(_DISPATCH_WHITELIST)) + "; audit 请经 CLI。")}
    text = args.get("text", "")
    if short == "parse_argument":
        return parse_argument(text)
    if short == "extract_premises":
        return extract_premises(text)
    if short == "implicit_assumptions":
        return implicit_assumptions(text)
    if short == "check_validity":
        return check_validity(text)
    if short == "fallacy_scan":
        return fallacy_scan(text)
    if short == "counterexample":
        return counterexample(text)
    if short == "countermodel":
        return countermodel(text)
    if short == "proof_trace":
        return proof_trace(text)
    if short == "repair_reasoning":
        return repair_reasoning(text)
    if short == "explain_gap":
        return explain_gap(text)
    if short == "status":
        return status()
    return {"ok": False, "error": "unreachable"}  # pragma: no cover


# --------------------------------------------------------------------------- #
# selftest — AST 静态扫 + 行为验证(纯本地, 绝不真执行/真删/真发)
# --------------------------------------------------------------------------- #
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
      ④ 无 confirmed=True; ⑤ 模型白名单只读(audit 不暴露); ⑥ 行为: 谬误检测确定、
         私密标记、命题反模型正确、凭据脱敏、审计只存元数据。
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
                return  # 跳过自检自身(它合法地内含被禁字面量/清理自造 tmp)
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
    if "audit" in _DISPATCH_WHITELIST:
        fails.append("⑤ 模型白名单泄漏隐私自审动作: audit")

    tmp = tempfile.mkdtemp(prefix="lpc_selftest_")
    old_env = os.environ.get(_HOME_ENV)
    os.environ[_HOME_ENV] = tmp
    try:
        mp = check_validity("如果下雨，地就会湿；现在下雨了；所以地是湿的", with_facts=False)
        if not (mp["ok"] and mp["decidable"] and mp["valid"] is True):
            fails.append("⑥a 肯定前件应判 valid, 实得: " + str(mp.get("form")))
        ac = check_validity("如果下雨，地就会湿；地是湿的；所以下雨了", with_facts=False)
        if not (ac["ok"] and ac["decidable"] and ac["valid"] is False and ac["countermodel"]):
            fails.append("⑥b 肯定后件应判 invalid+反模型, 实得: " + str(ac.get("form")))
        cm = countermodel("如果下雨，地就会湿；地是湿的；所以下雨了")
        if not (cm["ok"] and cm["found"]):
            fails.append("⑥b2 肯定后件 countermodel 应 found=True")
        mt = check_validity("如果下雨，地就会湿；地没有湿；所以没有下雨", with_facts=False)
        if not (mt["ok"] and mt["decidable"] and mt["valid"] is True):
            fails.append("⑥c 否定后件应判 valid, 实得: " + str(mt.get("form")))
        circ = fallacy_scan("因为他值得信任，所以他值得信任", with_facts=False)
        if "circular_reasoning" not in [f["code"] for f in circ["fallacies"]]:
            fails.append("⑥d 循环论证未检出: " + str([f["code"] for f in circ["fallacies"]]))
        hg = fallacy_scan("我见过一只天鹅是白的，所以所有天鹅都是白的", with_facts=False)
        if "hasty_generalization" not in [f["code"] for f in hg["fallacies"]]:
            fails.append("⑥e 以偏概全未检出: " + str([f["code"] for f in hg["fallacies"]]))
        priv = parse_argument("因为他的工资是 25000 元，所以他买得起；银行卡号 6222 0212 3456 7890")
        if not priv.get("_private"):
            fails.append("⑥f 含私密(工资/卡号)未标 _private")
        sec = parse_argument("因为 api_key=sk-ABCDEF0123456789ABCD 有效，所以请求会成功")
        body = json.dumps(sec, ensure_ascii=False)
        if "sk-ABCDEF0123456789ABCD" in body:
            fails.append("⑥g 凭据串未脱敏(出现在输出里)!")
        if not sec.get("_force_local"):
            fails.append("⑥g2 凭据串应触发 _force_local")
        pa = parse_argument("因为今天降温，所以要多穿衣服")
        if not (pa["ok"] and pa["premises"] and pa["conclusion"]):
            fails.append("⑥h parse_argument 骨架不完整")
        if not dispatch("logic_proof_core_check_validity",
                        {"text": "如果A则B；A；所以B"}).get("ok"):
            fails.append("⑤ dispatch 应放行 check_validity")
        if dispatch("logic_proof_core_audit", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 audit(应拒绝)")
        au = audit()
        if not au["ok"]:
            fails.append("⑥j audit 失败")
        elif au["self_check"]["credential_strings_in_audit"] != 0 or \
                au["self_check"]["unexpected_body_fields"] != 0:
            fails.append("⑥j 审计流出现凭据/正文字段(应为 0): " + str(au["self_check"]))
        st = status()
        if "deferred" not in st or "engines" not in st:
            fails.append("⑥k status 缺 deferred/engines 段")
    except Exception as e:
        import traceback
        fails.append("⑥ 行为验证抛异常: " + str(e) + " | " + traceback.format_exc()[-300:])
    finally:
        if old_env is None:
            os.environ.pop(_HOME_ENV, None)
        else:
            os.environ[_HOME_ENV] = old_env
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        for f in fails:
            print("FAIL: " + f)
        return 1
    print("OK logic_proof_core selftest 全过(纯只读分析门面 + 离线确定性逻辑内核):")
    print("  ① 不直接 import 任何执行面模块 ✓")
    print("  ② 源码无执行面方法调用(run_shell/write_file/type_text/click_on...) ✓")
    print("  ③ 无危险 stdlib 调用(system/remove/rmtree/exec...) ✓")
    print("  ④ 源码无 confirmed=True ✓")
    print("  ⑤ 模型白名单只读, audit(隐私自审)不暴露 ✓")
    print("  ⑥ 谬误检测确定 + 命题反模型正确 + 私密标记 + 凭据脱敏 + 审计只存元数据 ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI(argparse; 全 pretty-JSON via _emit)
# --------------------------------------------------------------------------- #
def _main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="logic_proof_core",
                                description="形式逻辑推理审判器")
    sub = p.add_subparsers(dest="cmd", required=True)

    text_actions = (
        ("parse_argument", "把论证拆成前提/定义/隐含假设/推理步骤/结论/适用条件"),
        ("extract_premises", "只抽明示前提(含条件前提)"),
        ("implicit_assumptions", "挖隐含假设(推理要成立必须为真的未明说前提)"),
        ("check_validity", "判定结论是否从前提逻辑地得出(命题层; 无效给反模型)"),
        ("fallacy_scan", "扫逻辑谬误(偷换概念/循环/因果倒置/以偏概全/前提不足/结论跳跃/冲突事实)"),
        ("counterexample", "为论证生成反例候选"),
        ("countermodel", "命题层反模型(使前提真而结论假的真值赋值)"),
        ("proof_trace", "渲染推理轨迹, 逐行标注规则与缺口"),
        ("repair_reasoning", "针对缺陷给最小修补建议"),
        ("explain_gap", "指出推理链最薄弱的一环及原因"),
    )
    for name, helptxt in text_actions:
        sp = sub.add_parser(name, help=helptxt)
        sp.add_argument("text", help="要审查的论证原文")
        sp.add_argument("--no-facts", action="store_true",
                        help="跳过 world_model 已知事实冲突检查(更快/离线)")

    sub.add_parser("audit", help="隐私/安全自审(只读): 审计流只存元数据、凭据计数恒 0")
    sub.add_parser("status", help="总览: 动作清单 + 集成可用性 + deferred 内核")
    sub.add_parser("selftest", help="跑安全不变量 + 逻辑内核自检")

    if not argv:
        _emit(status())
        return 0
    args = p.parse_args(argv)
    wf = not getattr(args, "no_facts", False)

    if args.cmd == "parse_argument":
        _emit(parse_argument(args.text))
    elif args.cmd == "extract_premises":
        _emit(extract_premises(args.text))
    elif args.cmd == "implicit_assumptions":
        _emit(implicit_assumptions(args.text))
    elif args.cmd == "check_validity":
        _emit(check_validity(args.text, with_facts=wf))
    elif args.cmd == "fallacy_scan":
        _emit(fallacy_scan(args.text, with_facts=wf))
    elif args.cmd == "counterexample":
        _emit(counterexample(args.text))
    elif args.cmd == "countermodel":
        _emit(countermodel(args.text))
    elif args.cmd == "proof_trace":
        _emit(proof_trace(args.text))
    elif args.cmd == "repair_reasoning":
        _emit(repair_reasoning(args.text, with_facts=wf))
    elif args.cmd == "explain_gap":
        _emit(explain_gap(args.text, with_facts=wf))
    elif args.cmd == "audit":
        _emit(audit())
    elif args.cmd == "status":
        _emit(status())
    elif args.cmd == "selftest":
        return _selftest()
    else:
        _emit(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
