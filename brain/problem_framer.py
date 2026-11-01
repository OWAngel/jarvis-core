#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""problem_framer.py — 小美的「问题框定与需求重构核心」。

把小美从「拿到请求就埋头做」升级成**先把问题想清楚再动手**: 处理任何复杂/模糊任务前,
自动把 User 的原始请求重构成一张**问题框架(problem frame)**——
  · 清晰的问题定义(problem_definition)  · 真实目标(real_goal, 字面诉求背后的「为什么」)
  · 背景假设(hidden_assumptions)        · 成功标准(success_criteria, 尽量可度量)
  · 约束(constraints: 时间/预算/技术/隐私/红线)  · 利益相关方(stakeholders)
  · 非目标(non_goals, 明确不做的)        · 隐藏子问题(hidden subproblems)
  · 可追问点(clarifying_questions, 带默认值, 不阻塞)  · 可执行任务边界(task_boundaries)
让小美每次接到「帮我弄一下那个」「继续上次的」「该不该涨价」时, 都能先摊开:
你真正要的是什么、做成什么样算成功、有哪些约束和红线、涉及谁、什么不做、哪里需要确认。

定位: 本模块是**纯只读分析门面 + 离线确定性框定器**, 不自带执行引擎, 不重写任何安全门。
它把 mission_control(战略目标, 给真实目标找对齐) / world_model(因果世界模型, 给背景假设/
子问题找依据) / mind_graph(认知图谱, 给利益相关方/实体接地)的**只读·脱敏**面, 串成一个
「需求重构 + 目标推断 + 约束/红线显化 + 问题分解」层。接地从别的节点捞文本进来时一律先过
凭据硬拦 + 脱敏 + 私密判定, 私密则整框标私密(切本机收尾、落库脱敏)。
metacognition_core(置信/不确定) / reasoning_core(深层推断) / value_reasoner(事实 vs 价值) /
epistemic_core(信念/辩护)尚未落地 → 软导入记 deferred(待其上线自动接上, 绝不建桩)。
框定本身是纯启发式离线计算, 不调任何 LLM。

接口供下游认知/执行引擎调用(它们 _opt("problem_framer") 取本模块):
  metacognition_core / reasoning_core / mission_control / strategy_tournament /
  action_kernel / outcome_verifier —— 在「动手前先把问题框清楚」这一步复用本模块。

★★★ 安全铁律(与 shared_context / argument_prover / mission_control 一致, 自进化引擎不得改弱)★★★
  - 纯门面 + 零真实副作用: 本模块自身**绝不** subprocess / os.system / osascript / 删文件 /
    exec / __import__。frame 只写**自有库**(问题框架缓存); handoff 若要落地真动作, 一律
    `autopilot_store.enqueue_approval(...)` 入审批队列(risk=high), 经 autopilot 过 User 的
    全部门, 本模块绝不执行。
  - 永不自放行: **绝不**对任何引擎传 confirmed=True。只读地调 mission_control(list_goals /
    status) / world_model(explain / relationship) / mind_graph(query), **绝不**改目标树/世界
    模型/图谱。
  - 红线单一真相源: constraints 显化红线一律转发 `local_powers._check_red_line` /
    `non_shell_danger`; 本模块绝不复制红线表。
  - 隐私: 命中私密(privacy_stream 主判 / file_tools 私密判定 / awareness 私密判定为加成)的请求,
    **整框只留结构, 敏感正文一律脱敏/占位后才落库**(以框级 `_any_private` 信号为准, 不按单字段重判);
    任何含私密内容的输出带 `_private`(必要时 `_force_local`), 由 jarvis_brain 切本机模型收尾。
    **凭据/密钥文件(.gmail_creds.json/.gcal_creds.json/...)路径与正文一律硬拦, 绝不读取、绝不纳入
    问题框架(本地正则永不可绕)。** 隐私库**出错**时按私密处理(fail-safe)。
  - LLM 工具面只读且冻结: 只暴露 reframe / infer_real_goal / success_criteria / constraints /
    stakeholders / non_goals / hidden_assumptions / decompose_problem /
    ask_clarifying_questions / explain / status; frame(写库) / handoff(入审批队列) / audit 走 CLI。

全本机, 纯 stdlib sqlite3。自有库 ~/.jarvis/problem_framer/problem_framer.db, 不碰主库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
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
_HOME_ENV = "JARVIS_PROBLEM_FRAMER_HOME"   # 测试设它指向 tmp; 优先于 config


def _data_dir() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(
        config.get("problem_framer.data_dir", "~/.jarvis/problem_framer")))


def _db_path() -> str:
    return os.path.join(_data_dir(), "problem_framer.db")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "problem_framer.jsonl")


# --------------------------------------------------------------------------- #
# 软导入可选集成模块(importlib, 非 __import__/exec); 缺/出错 → None(降级, 永不抛)
# --------------------------------------------------------------------------- #
def _opt(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# 尚未落地的元认知/推理/价值/信念内核 → 记 deferred, 绝不建桩(待其上线 _opt 自动接上)。
_DEFERRED = ("metacognition_core", "reasoning_core", "value_reasoner", "epistemic_core")
# 本模块接入(读取)的框定依据来源; 全部只读, 绝不写目标树/世界模型/图谱。
# 只列**真正被调用**的来源(status().engines 不虚报)。
_SOURCE_MODULES = (
    "mission_control", "world_model", "mind_graph",
)
# 下游消费者(它们调用本模块的接口; 本模块**绝不** import 它们, 仅作文档登记)。
_CONSUMERS = (
    "metacognition_core", "reasoning_core", "mission_control",
    "strategy_tournament", "action_kernel", "outcome_verifier",
)


def _deferred_status() -> dict:
    out = {}
    for m in _DEFERRED:
        out[m] = {"present": _opt(m) is not None, "deferred": True, "fallback": None}
    return out


# --------------------------------------------------------------------------- #
# 时间/输出/审计 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().date().isoformat()


def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _audit(action: str, frame_id: str = "", **extra) -> None:
    """只记 ids/动作/计数/标志的审计流, **绝不写请求正文/隐私**, 永不抛。"""
    try:
        rec = {"ts": _now(), "action": action, "frame_id": frame_id or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        path = _audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 进程内单调序号: itertools.count().__next__ 在 CPython 下原子, 同一微秒内连续 frame
# 的 id 也各异 → 彻底消除 id 相撞导致 INSERT OR REPLACE 互相覆盖。
_FRAME_SEQ = itertools.count()


def _frame_id(seed: str) -> str:
    stamp = datetime.now().isoformat(timespec="microseconds")
    seq = next(_FRAME_SEQ)
    h = hashlib.sha1(
        (str(seed) + stamp + ":" + str(seq)).encode("utf-8", "replace")
    ).hexdigest()[:12]
    return "pf_" + h


def _loads(v, fb):
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return fb


def _safe_text(s) -> str:
    """落库前消毒: 把孤立代理项等非法码位换成替代符, 防 sqlite UTF-8 编码崩溃。"""
    try:
        return (s if isinstance(s, str) else str(s)).encode("utf-8", "replace").decode("utf-8")
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 隐私原语(复用 privacy_stream / file_tools / awareness 单一姿态; 不可达 → fail-safe)
# --------------------------------------------------------------------------- #
# 凭据/密钥文件硬禁名单(本地兜底; 优先用 awareness_sensors.is_secret_path)。
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".localkey", "config.toml",
})
_SECRET_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.localkey|/\.jarvis/config\.toml|"
    r"\.pem$|(^|/)id_rsa|(^|/)\.ssh/|refresh_token|client_secret|BEGIN [A-Z ]*PRIVATE KEY",
    re.IGNORECASE)


def _is_secret_path(path: str) -> bool:
    """凭据/密钥文件 → True(硬拦, 绝不纳入问题框架)。判不准当机密(fail-safe)。

    双层检测(MEMORY jarvis-secret-path-dual-layer 铁律): **本地正则永远先跑且不可绕过**,
    隐私库只能在其上**追加**命中; 任一层命中即 True; 任一层出错也 fail-safe 当机密。
    绝不让隐私库短路掉本地正则(那是 shared_context 旧版被对抗审查抓出的 critical 缺陷)。
    """
    try:
        p = path or ""
        base = os.path.basename(os.path.expanduser(p))
        if base in _SECRET_BASENAMES:
            return True
        if _SECRET_RX.search(p):
            return True
    except Exception:
        return True
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "is_secret_path"):
        try:
            if aw.is_secret_path(path):
                return True
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


def _classify_private(text: str = "", app: str = "", window: str = "") -> bool:
    """文本是否私密(awareness_sensors 为加成判定; privacy_stream 才是 _text_private 的主真相源)。
    库存在但**出错** → fail-safe True; 库缺失 → 交回上游(privacy_stream 主判), 此处返 False。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密记录不占位、返原文
    (本模块无对外出口; 红线4 由 autopilot 审批闸独立守卫, 不靠此分类)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "classify_private"):
        try:
            return bool(aw.classify_private(text, app, window))
        except Exception:
            return True if (text or app or window) else False
    return False


# 本地兜底敏感模式(privacy_stream 不可达时仍能脱敏): 长数字串/邮箱/卡号/手机号等。
# 各分支长度都**有界**, 杜绝在超长良性输入上回溯爆炸(ReDoS)。
_REDACT_RX = re.compile(
    r"\b\d[\d \-]{5,30}\d\b|[\w.+-]{1,64}@[\w.-]{1,255}\.\w{2,24}|"
    r"(?:\d{4}[ -]?){3}\d{4}|\b1[3-9]\d{9}\b",
    re.IGNORECASE)


def _redact(text: str) -> str:
    """脱敏一段正文: 抹长数字/邮箱/卡号/手机。各分支长度有界, 无 ReDoS。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 返原文, 不脱敏(本模块无对外出口,
    数据层不阉割)。开 → 完整脱敏(可逆)。"""
    t = text or ""
    if not t:
        return t
    if not config.get("safety.data_layer_privacy_enabled", False):
        return t
    try:
        return _REDACT_RX.sub("…", t)
    except Exception:
        return t


def _text_private(text: str) -> bool:
    """这段请求正文是否含私密内容(走 privacy_stream 单一真相源; 缺则本地兜底)。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 不再调用 privacy_stream
    (否则 privacy_stream 会绕过本模块的开关, 短路 _classify_private 已生效的放弃)。"""
    t = text or ""
    if not t:
        return False
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    ps = _opt("privacy_stream")
    if ps is not None and hasattr(ps, "should_force_local"):
        try:
            v = ps.should_force_local(t)
            if isinstance(v, dict) and (v.get("private") or v.get("force_local")):
                return True
        except Exception:
            pass
    return _classify_private(t)


def _scrub_for_store(text: str, frame_private: bool = False) -> str:
    """落库前清洗(与 shared_context「私密只留元数据」一致):
       凭据 → 硬清; 框级私密 或 本字段私密正文 → 整段隐去(只留结构); 普通正文 → 脱敏长数字/邮箱后入库。
       frame_private=True 时整框按私密占位(不再单字段重判, 避免丢掉源级 _private 信号)。"""
    t = text or ""
    if not isinstance(t, str):
        t = str(t)
    if _is_secret_path(t):
        return "（凭据/密钥已硬拦, 不入库）"
    if frame_private or _text_private(t):
        return "（私密内容已隐去, 仅留问题结构）"
    return _redact(t)


def _flag_private(out: dict, any_private: bool) -> dict:
    """含私密 → 打 _private; 未授权上云则加 _force_local, 由 jarvis_brain 切本机收尾。"""
    if any_private:
        out["_private"] = True
        if not config.get("safety.allow_cloud_private", False):
            out["_force_local"] = True
    return out


# --------------------------------------------------------------------------- #
# 语言学资源(中英双语标记; 框定解析的离线启发式)
# --------------------------------------------------------------------------- #
# 目标/意图引导词(其后/其前往往是真实目标)。
_GOAL_MARKERS = (
    "为了", "好让", "以便", "便于", "这样就能", "目的是", "为的是", "用来", "好方便",
    "想要", "希望能", "为了能", "so that", "in order to", "to be able to", "so i can",
)
# 请求/祈使动词(任务型请求的信号)。
_TASK_VERBS = (
    "帮我", "帮忙", "给我", "做个", "做一个", "做下", "写个", "写一个", "改一下", "加个",
    "建个", "搭个", "整理", "查一下", "找一下", "发一下", "算一下", "生成", "弄个", "搞定",
    "处理", "优化", "重构", "实现", "开发", "部署", "配置", "安装", "更新", "修一下",
    "解决", "分析", "总结", "对比", "评估", "设计", "规划", "跑一下", "导出", "make ",
    "build ", "create ", "fix ", "add ", "write ", "implement", "set up", "help me",
)
# 决策型信号(在「做不做/选哪个」之间取舍)。
# 强决策标记(无歧义)。'还是' 用 _is_choice 单独判("A 还是 B" 择一, 排除"我还是/还是先"副词义);
# '好不好/行不行' 归入 _QUESTION_MARKERS(评价型问句, 不是取舍决策)。
_DECISION_MARKERS = (
    "该不该", "要不要", "应不应", "应该不应该", "值不值", "划不划算", "选哪个", "哪个更",
    "哪个好", "哪种好", "怎么选", "如何选择", "做不做", "上不上", "买不买",
    "should i", "which one", "which is better", "or not", "worth it",
)
# 问题型信号(求知/求解释)。
_QUESTION_MARKERS = (
    "吗", "呢", "?", "?", "什么", "为什么", "为何", "怎么", "如何", "是不是", "能不能",
    "可不可以", "好不好", "行不行", "对不对", "多少", "哪里", "哪儿", "怎样", "what", "why",
    "how", "when", "where", "which", "is it", "can i", "does ", "do i",
)
# 模糊/探索型信号(低具体度)。
_VAGUE_MARKERS = (
    "看看", "看下", "研究下", "研究一下", "想想", "搞一下", "弄一下", "随便", "大概", "之类",
    "什么的", "差不多", "了解下", "了解一下", "调研", "捣鼓", "explore", "look into",
    "figure out", "something like", "kind of",
)
# 构建型信号(大项目/系统)。
_BUILD_MARKERS = (
    "系统", "平台", "项目", "应用", "服务", "框架", "引擎", "核心", "模块", "网站",
    "app", "system", "platform", "service", "engine", "pipeline",
)
# 成功/完成标记。
_SUCCESS_MARKERS = (
    "完成", "搞定", "做完", "能用", "可用", "上线", "通过", "达到", "实现", "不再", "确保",
    "保证", "成功", "跑通", "生效", "符合", "满足", "ready", "done", "works", "pass",
    "ship", "live", "complete",
)
# 约束: 时间。
_TIME_MARKERS = (
    "今天", "明天", "今晚", "今早", "本周", "这周", "下周", "本月", "月底", "周五前",
    "周一前", "截止", "deadline", "尽快", "马上", "立刻", "立即", "赶紧", "急", "限时",
    "倒计时", "之前要", "by ", "before ", "asap", "今天内", "下班前",
)
# 约束: 预算/成本。
_BUDGET_MARKERS = (
    "别花钱", "不要花钱", "不花钱", "免费", "预算", "省钱", "不付费", "不要付费", "低成本",
    "便宜", "白嫖", "low cost", "free", "budget", "cheap", "no cost", "no budget",
)
# 约束: 技术/方式。
_TECH_CONSTRAINT_MARKERS = (
    "必须用", "一定要用", "不要用", "不用", "只用", "基于", "必须兼容", "本机", "离线",
    "纯 stdlib", "不依赖", "不引入", "保持", "沿用", "must use", "without ", "offline",
    "local only", "no dependencies", "stdlib only", "兼容",
)
# 约束: 隐私。
_PRIVACY_CONSTRAINT_MARKERS = (
    "私密", "保密", "别外发", "不外发", "不上云", "本机处理", "隐私", "机密", "不要发出去",
    "别发出去", "敏感", "private", "confidential", "don't share", "keep local", "本地处理",
)
# 非目标/排除标记。
_NONGOAL_MARKERS = (
    "不要", "别", "不用", "无需", "不必", "不需要", "暂时不", "先不", "跳过", "不考虑",
    "不动", "保持不变", "除了", "不包括", "不涉及", "don't ", "do not", "no need", "skip ",
    "except ", "leave .* alone", "不改",
)
# 利益相关方标记(可能涉及他人)。
_PERSON_MARKERS = (
    "客户", "同事", "老板", "领导", "团队", "供应商", "合作方", "对方", "用户", "家人",
    "朋友", "员工", "下属", "经理", "总监", "甲方", "乙方", "client", "customer",
    "colleague", "boss", "team", "vendor", "partner", "stakeholder",
)
# 对外动作标记(可能触发红线 4: 影响 User 之外的人)。
_EXTERNAL_ACTION_MARKERS = (
    "发给", "发邮件", "发送给", "发飞书给", "通知", "回复", "对外", "公开", "发布", "提交给",
    "汇报给", "抄送", "群发", "推送给", "send to", "email ", "notify ", "reply to",
    "publish", "post to", "cc ",
)
# 连接/分解标记(多任务/多步骤)。
_CONJ_MARKERS = (
    "然后", "接着", "再", "并且", "并", "以及", "同时", "还要", "还得", "顺便", "之后",
    "最后", "首先", "其次", "第一步", "第二步", "then", "and then", "after that", "also",
    "next ", "finally",
)
# 模糊指代(隐藏假设: 缺前指)。
_VAGUE_REF_MARKERS = (
    "这个", "那个", "这些", "那些", "这张", "那张", "这份", "那份", "上次", "之前那个",
    "刚才", "上面那个", "刚说的", "你懂的", "那边", "那玩意", "that one", "this thing",
    "the previous", "last time", "that file", "it ",
)
# 进行中/续接标记(隐藏假设: 假设有一个在跑的任务)。
_CONTINUE_MARKERS = (
    "继续", "接着做", "接上", "上次那个", "之前的", "未完成的", "剩下的", "再做下去",
    "continue", "resume", "keep going", "carry on",
)


# --------------------------------------------------------------------------- #
# 分词 / 重叠度(中英混排; 用于目标对齐、指代、概念重合判定)
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "也", "都", "就", "而", "这", "那", "我", "你",
    "他", "它", "们", "一个", "这个", "那个", "因为", "所以", "但是", "如果", "一", "会",
    "有", "对", "把", "被", "让", "给", "向", "从", "到", "为", "以", "及", "或", "等",
    "之", "其", "于", "且", "并", "却", "则", "故", "可见", "帮", "下", "个", "请",
    "a", "an", "the", "is", "are", "to", "of", "and", "or", "in", "on", "it", "this",
    "that", "be", "so", "if", "we", "you", "they", "he", "she", "me", "my", "please",
})


def _terms(text: str) -> set:
    """内容词集合: CJK 抽 2 字 gram + ASCII 词(≥2, 去停用词)。给重叠度/对齐用。"""
    t = (text or "").lower()
    out = set()
    for w in re.findall(r"[a-z][a-z0-9_]+", t):
        if len(w) >= 2 and w not in _STOPWORDS:
            out.add(w)
    for run in re.findall(r"[一-鿿]+", t):
        if len(run) == 1:
            if run not in _STOPWORDS:
                out.add(run)
            continue
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g[0] in _STOPWORDS or g[1] in _STOPWORDS or g in _STOPWORDS:
                continue
            out.add(g)
    return out


def _overlap(a: str, b: str) -> float:
    """两段文本内容词的 Jaccard 重叠度 ∈ [0,1]。"""
    ta, tb = _terms(a), _terms(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return round(inter / union, 4) if union else 0.0


def _has_any(text: str, markers) -> bool:
    t = (text or "").lower()
    for m in markers:
        if m.lower() in t:
            return True
    return False


def _which(text: str, markers) -> list:
    """命中了哪些标记(保序、确定; 给可解释输出用)。"""
    t = (text or "").lower()
    return [m for m in markers if m.lower() in t]


def _split_clauses(text: str) -> list:
    """按中英标点 + 连接词切分句(逗号也切, 抓多步骤/多任务)。"""
    if not text:
        return []
    parts = re.split(r"[。!?；;,，\n]|然后|接着|并且|以及|还要|还得", str(text))
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    return out or [str(text).strip()]


# 仅这些**无歧义的请求开场词**才剥离(防把"处理/实现/分析/配置/开发"等动词当成更长名词的
# 前半截误切, 如"处理器选型"→"器选型")。一般动作动词是任务内容, 保留。
_LEAD_OPENERS = (
    "帮我", "帮忙", "帮个忙", "给我", "麻烦你", "麻烦", "请帮我", "请帮", "请你", "请",
    "劳烦", "做个", "做一个", "写个", "写一个", "help me", "please ",
)


def _strip_lead_verb(clause: str) -> str:
    """去掉开头的无歧义请求开场词(帮我/给我/做个…), 留下任务对象正文。
    只剥 _LEAD_OPENERS, 不剥一般动作动词(它们是内容; 剥了会把'处理器'切成'器')。"""
    c = (clause or "").strip()
    for v in sorted(_LEAD_OPENERS, key=len, reverse=True):
        if c.lower().startswith(v.lower()):
            return c[len(v):].strip("，, 。:：的").strip() or c
    return c


# "A 还是 B" 择一结构(真选择), 区别于"我还是/还是先"这类副词义的"还是"。
_CHOICE_ADVERB_PREV = frozenset("我你他她咱就那也总早趁还")


def _is_choice(text: str) -> bool:
    """文本是否含"A 还是 B"择一结构(决策信号)。排除"我还是…/还是先…"的副词义。"""
    t = text or ""
    for m in re.finditer("还是", t):
        i = m.start()
        if i > 0 and t[i - 1] in _CHOICE_ADVERB_PREV:
            continue
        left = t[max(0, i - 12):i].strip("，, 。:：?? ")
        right = t[i + 2:i + 14].strip("，, 。:：?? ")
        if left and right:
            return True
    return False


# --------------------------------------------------------------------------- #
# 框架构造(纯启发式离线; 把请求重构成 problem frame)
# --------------------------------------------------------------------------- #
def _classify_request(request: str, text: str = "") -> str:
    """请求类型: decision / question / build / task / exploration / statement。"""
    t = ((request or "") + " " + (text or "")).strip()
    if _has_any(t, _DECISION_MARKERS) or _is_choice(t):
        return "decision"
    if _has_any(t, _QUESTION_MARKERS):
        return "question"
    if _has_any(t, _BUILD_MARKERS) and _has_any(t, _TASK_VERBS):
        return "build"
    if _has_any(t, _TASK_VERBS):
        return "task"
    if _has_any(t, _VAGUE_MARKERS):
        return "exploration"
    return "statement"


def _specificity(request: str, text: str) -> float:
    """请求具体度 ∈ [0,1]: 内容词越多、模糊指代/探索词越少 → 越高。低 → 更需追问。"""
    blob = (request or "") + " " + (text or "")
    n_terms = len(_terms(blob))
    vague = len(_which(blob, _VAGUE_REF_MARKERS)) + len(_which(blob, _VAGUE_MARKERS))
    base = min(1.0, n_terms / 10.0)
    base -= 0.15 * vague
    return round(max(0.0, min(1.0, base)), 2)


def _problem_definition(request: str, kind: str) -> str:
    """把原始请求重述成一句清晰的问题定义。"""
    core = (request or "").strip()
    if not core:
        return "（请求为空, 无法框定; 先给一句你想解决的事。）"
    lead = {
        "decision": "需要做一个决策: ",
        "question": "需要回答一个问题: ",
        "build": "需要构建一个东西: ",
        "task": "需要完成一个任务: ",
        "exploration": "需要做一次探索/调研: ",
        "statement": "需要厘清的问题: ",
    }.get(kind, "需要厘清的问题: ")
    return lead + core


def _infer_real_goal(request: str, text: str, kind: str) -> tuple:
    """推断字面诉求背后的真实目标。

    显式「为了/以便 Y」→ Y 是真实目标; 否则给候选 + 标不确定(绝不替 User 编死目标)。
    并只读对齐 mission_control 活跃战略目标(脱敏)。返回 (real_goal, any_private)。
    """
    blob = (request or "") + " " + (text or "")
    literal = _strip_lead_verb(request or "") or (request or "").strip()
    inferred = []
    via = []

    # 1) 显式目标引导词后的内容即真实目标
    for m in _GOAL_MARKERS:
        idx = blob.lower().find(m.lower())
        if idx != -1:
            tail = blob[idx + len(m):].strip("，, 。:：")
            tail = _split_clauses(tail)
            if tail and tail[0]:
                inferred.append({"goal": _redact(tail[0])[:80], "basis": "显式目标词「%s」" % m})
                via.append("explicit_marker")
                break

    # 2) 按请求类型给「真实目标」的启发候选(保守、标注是推断)
    if not inferred:
        templ = {
            "decision": "在不确定下做出可回滚、风险可控的选择(而非纠结本身)",
            "question": "拿到能据以行动的答案 + 该答案的可信度",
            "build": "得到一个能用、User 能 review/回滚的产物, 真正解决底层需求",
            "task": "把这件事办成且办对, 同时不破坏其它东西(surgical)",
            "exploration": "降低对该领域的不确定性, 收敛到下一步该做什么",
            "statement": "把问题想清楚, 再决定要不要/怎么动手",
        }.get(kind, "把底层需求真正解决掉")
        inferred.append({"goal": templ, "basis": "按请求类型启发推断(需 User 确认)"})
        via.append("type_heuristic")

    # 3) 只读对齐 mission_control 活跃战略目标(脱敏 + 私密保护)
    aligned, mc_private = _align_mission_goals(blob)

    real = {
        "literal_ask": _redact(literal)[:120],
        "inferred_goals": inferred,
        "aligned_strategic_goals": aligned,
        "via": list(dict.fromkeys(via + (["mission_control"] if aligned else []))),
        "confidence": "低" if via == ["type_heuristic"] else "中",
        "note": "真实目标是推断, 非事实; 字面诉求往往是手段, 别把手段当目的。不确定就 ask_clarifying_questions。",
    }
    return real, bool(mc_private)


def _align_mission_goals(blob: str) -> tuple:
    """只读取 mission_control 活跃目标, 找与请求重叠度高的(脱敏)。返回 (aligned, any_private)。"""
    aligned = []
    any_private = False
    mc = _opt("mission_control")
    if mc is None or not hasattr(mc, "list_goals"):
        return aligned, any_private
    try:
        goals = mc.list_goals(status="active")
    except Exception:
        return aligned, any_private
    if not isinstance(goals, list):
        return aligned, any_private
    scored = []
    for g in goals:
        if not isinstance(g, dict):
            continue
        title = str(g.get("title") or "")
        if _is_secret_path(title):       # 凭据 → 整个目标丢弃, 不纳入
            any_private = True
            continue
        if _text_private(title):
            any_private = True
            continue
        ov = _overlap(blob, title)
        if ov >= 0.12:
            scored.append((ov, g.get("id"), title))
    for ov, gid, title in sorted(scored, key=lambda x: (-x[0], str(x[1])))[:3]:
        aligned.append({"goal_id": gid, "title": _redact(title)[:60], "overlap": ov})
    return aligned, any_private


def _success_criteria(request: str, text: str, kind: str) -> list:
    """派生成功/验收标准(尽量可度量), 区分 outcome / acceptance / metric。"""
    blob = (request or "") + " " + (text or "")
    crit = []
    # 1) 请求类型模板(outcome)
    templ = {
        "decision": "给出明确推荐选项 + 理由 + 主要风险 + 是否可回滚",
        "question": "给出有依据的答案, 并标注不确定性与信息来源",
        "build": "产物存在、能跑通、User 能 review; 关键路径有测试/可验证",
        "task": "目标对象达到期望状态, 且未破坏无关部分(surgical)",
        "exploration": "产出发现清单 + 收敛到 1-3 个可执行下一步",
        "statement": "问题被清晰定义, 利弊摆清, 可据以决定下一步",
    }.get(kind, "问题被解决且可验证")
    crit.append({"text": templ, "kind": "outcome", "measurable": False})

    # 2) 显式成功词周边的句子(往往就是 User 自带的验收线)
    for c in _split_clauses(blob):
        if _has_any(c, _SUCCESS_MARKERS):
            crit.append({"text": _redact(c)[:80], "kind": "acceptance",
                         "measurable": bool(re.search(r"\d", c))})

    # 3) 可度量目标: 数字 + 单位(时间/数量/金额/性能)
    _unit = (r"%|％|个|条|秒|毫秒|微秒|分钟|小时|天|周|月|年|元|块|万|亿|次|行|页|倍|人|"
             r"度|ms|kb|mb|gb|tb|qps|rps")
    # 量词有界 + (?<!\d) 锚定数字串首, 杜绝长数字串 O(n^2) 回溯(与 _REDACT_RX 同一加固纪律)
    for mnum in re.findall(r"(?<!\d)\d{1,15}(?:\.\d{1,6})?\s*(?:" + _unit + r")",
                           blob, re.IGNORECASE)[:5]:
        crit.append({"text": "达到量化目标: " + mnum, "kind": "metric", "measurable": True})

    # 4) 永远附上 User 验收闸(符合 AGGRESSIVE AUTONOMOUS: 可 review/可回滚即可放手做)
    crit.append({"text": "User 能 review diff / 看 log / 一键回滚(可逆即可自主推进)",
                 "kind": "acceptance", "measurable": False})

    # 去重(按完整文本, 不用截断前缀 —— 防边界后才不同的条目被误并)
    seen, uniq = set(), []
    for c in crit:
        if c["text"] not in seen:
            seen.add(c["text"])
            uniq.append(c)
    return uniq


def _red_line_scan(text: str) -> list:
    """显化红线: **转发 local_powers 单一真相源**(只读分级, 绝不执行/绝不复制红线表)。"""
    hits = []
    lp = _opt("local_powers")
    if lp is None:
        return hits
    t = str(text or "")
    try:
        if hasattr(lp, "_check_red_line"):
            why = lp._check_red_line(t)
            if why:
                hits.append({"why": str(why), "via": "local_powers._check_red_line"})
    except Exception:
        pass
    try:
        if hasattr(lp, "non_shell_danger"):
            d = lp.non_shell_danger(t)
            if d:
                # dict 为真即视为命中(红线宁可保守): 取多个可能键, 都没有也登记
                if isinstance(d, dict):
                    why = d.get("why") or d.get("reason") or d.get("detail") \
                        or "触发 non_shell_danger 危险闸(未给原因)"
                else:
                    why = str(d)
                hits.append({"why": str(why), "via": "local_powers.non_shell_danger"})
    except Exception:
        pass
    return hits


def _constraints(request: str, text: str, stakeholders: list) -> list:
    """显化约束: 时间/预算/技术/隐私/红线/利益相关方。"""
    blob = (request or "") + " " + (text or "")
    out = []
    for kind, markers, sev in (
        ("time", _TIME_MARKERS, "med"),
        ("budget", _BUDGET_MARKERS, "med"),
        ("tech", _TECH_CONSTRAINT_MARKERS, "low"),
        ("privacy", _PRIVACY_CONSTRAINT_MARKERS, "high"),
    ):
        hit = _which(blob, markers)
        if hit:
            out.append({"kind": kind, "text": "命中%s约束: %s" % (kind, "、".join(hit[:4])),
                        "severity": sev, "source": "request"})
    # 红线(单一真相源 local_powers)
    for rl in _red_line_scan(blob):
        out.append({"kind": "red_line", "text": "触及红线: " + rl["why"] + "(需 User 显式确认, 绝不自动执行)",
                    "severity": "high", "source": rl["via"]})
    # 红线 4: 涉及 User 之外的人 / 对外动作
    if any(s.get("external") for s in (stakeholders or [])) or _has_any(blob, _EXTERNAL_ACTION_MARKERS):
        out.append({"kind": "red_line", "text": "涉及对外/影响 User 之外的人(红线4): 对外动作需 User 确认",
                    "severity": "high", "source": "stakeholder"})
    return out


def _stakeholders(request: str, text: str) -> tuple:
    """识别利益相关方(User 默认是委托人+决策人); 标外部人 → 红线4。

    mind_graph 接地(只读)会从别处捞实体名 → 同 argument_prover 三重防护:
    凭据硬拦 / 脱敏 / 私密则标 private。返回 (stakeholders, any_private)。
    """
    blob = (request or "") + " " + (text or "")
    out = [{"name": "User", "role": "principal+decider", "external": False}]
    any_private = False
    persons = _which(blob, _PERSON_MARKERS)
    external_action = _has_any(blob, _EXTERNAL_ACTION_MARKERS)
    for p in dict.fromkeys(persons):
        out.append({"name": _redact(p), "role": "affected", "external": True})
    if external_action and len(out) == 1:
        # 有对外动作但没点名具体人 → 仍登记一个外部接收方占位
        out.append({"name": "(对外接收方)", "role": "recipient", "external": True})

    # mind_graph 只读接地(best-effort): 给请求里的实体补利益相关方
    mg = _opt("mind_graph")
    if mg is not None and hasattr(mg, "query"):
        try:
            q = mg.query(blob, k=4)
            if isinstance(q, dict):
                if q.get("_private") or q.get("_force_local"):
                    any_private = True
                for n in (q.get("nodes") or [])[:6]:
                    if not isinstance(n, dict):
                        continue
                    if n.get("private"):
                        any_private = True
                        continue
                    name = str(n.get("name") or "")
                    if not name or _is_secret_path(name):
                        if _is_secret_path(name):
                            any_private = True
                        continue
                    if _text_private(name):
                        any_private = True
                        continue
                    ntype = str(n.get("ntype") or n.get("type") or "")
                    if ntype in ("person", "org", "contact", "人物", "组织"):
                        clean = _redact(name)[:40]
                        if clean and clean not in [s["name"] for s in out]:
                            out.append({"name": clean, "role": "related",
                                        "external": ntype not in ("self",), "via": "mind_graph"})
        except Exception:
            pass
    return out, any_private


def _non_goals(request: str, text: str, kind: str) -> list:
    """显式排除(不要/别/除了…) + 保守推断的非目标(标 inferred)。"""
    blob = (request or "") + " " + (text or "")
    out = []
    for c in _split_clauses(blob):
        # 决策问句("要不要X")不是排除项: "不要"⊂"要不要"; 跳过决策/择一语境 clause 防意图被反转
        if _has_any(c, _DECISION_MARKERS) or _is_choice(c):
            continue
        if _has_any(c, _NONGOAL_MARKERS):
            out.append({"text": _redact(c)[:80], "inferred": False})
    # 推断的常见非目标(保守, 标 inferred=True; 防 scope creep, 呼应 Karpathy surgical)
    inferred = ["顺手重构/改动无关代码(只做被要求的)", "扩大到请求未提及的范围"]
    if kind in ("task", "build"):
        inferred.append("引入未经同意的新依赖/新框架")
    if kind == "decision":
        inferred.append("替 User 把决定执行掉(决策 ≠ 执行)")
    for t in inferred:
        out.append({"text": t, "inferred": True})
    # 去重(按完整文本, 不用截断前缀)
    seen, uniq = set(), []
    for o in out:
        if o["text"] not in seen:
            seen.add(o["text"])
            uniq.append(o)
    return uniq


def _hidden_assumptions(request: str, text: str, kind: str) -> tuple:
    """背景假设: 模糊指代(缺前指)/ 续接假设 / 假定的访问·状态·定义 / 世界假设(只读 world_model)。"""
    blob = (request or "") + " " + (text or "")
    out = []
    # 1) 模糊指代 → 假设双方都知道它指什么
    refs = list(dict.fromkeys(_which(blob, _VAGUE_REF_MARKERS)))[:5]
    for r in refs:
        out.append({"text": "假设你我都清楚「%s」具体指什么(缺明确前指)" % r.strip(),
                    "kind": "reference"})
    # 2) 续接假设
    if _has_any(blob, _CONTINUE_MARKERS):
        out.append({"text": "假设存在一个进行中/上次未完成的任务可供「继续」(需定位它)",
                    "kind": "state"})
    # 3) 访问/工具/权限假设
    if kind in ("task", "build"):
        out.append({"text": "假设所需的数据/文件/权限/工具均已就位且可访问", "kind": "access"})
    # 4) 定义假设: 关键名词是否有公认定义
    if _has_any(blob, _BUILD_MARKERS):
        out.append({"text": "假设关键术语(如系统/平台/模块的边界)双方理解一致", "kind": "definition"})

    # 5) 世界假设(只读 world_model, best-effort, 脱敏)
    any_private = False
    wm = _opt("world_model")
    if wm is not None and hasattr(wm, "explain"):
        try:
            ex = wm.explain((request or "")[:120])
            if isinstance(ex, dict):
                if ex.get("_private") or ex.get("_force_local"):
                    any_private = True
                for b in (ex.get("because") or [])[:2]:
                    # world_model.explain 的 because 是 dict 列表(含 entity 明文 + per-item private)。
                    # 必须读 per-item private 标志并按字段取值, 绝不 str(dict)(否则私密实体明文逃逸,
                    # 且 _text_private 在整段 stringified-dict 上判不出中文人名/实体)。
                    if isinstance(b, dict):
                        if b.get("private"):
                            any_private = True
                            continue            # 私密实体直接丢弃, 不入输出/库
                        s = str(b.get("entity") or b.get("name") or b.get("text") or "")
                    elif isinstance(b, str):
                        s = b
                    else:
                        continue                 # 其它类型跳过(不 str())
                    if not s:
                        continue
                    if _is_secret_path(s):
                        any_private = True
                        continue
                    if _text_private(s):
                        any_private = True
                        continue
                    out.append({"text": "世界模型背景: " + _redact(s)[:60], "kind": "world",
                                "via": "world_model"})
        except Exception:
            pass
    return out, any_private


def _decompose(request: str, text: str, kind: str) -> dict:
    """拆成子问题 + 可执行任务边界(显式连接词分步 + 推断隐藏前置子问题)。"""
    blob = (request or "") + " " + (text or "")
    subs = []
    # 1) 显式多步(连接词切)
    clauses = [c for c in _split_clauses(blob) if c]
    if len(clauses) > 1 and (_has_any(blob, _CONJ_MARKERS) or len(clauses) >= 3):
        for i, c in enumerate(clauses[:8], 1):
            subs.append({"id": "s%d" % i, "text": _redact(_strip_lead_verb(c))[:70],
                         "hidden": False, "depends_on": (["s%d" % (i - 1)] if i > 1 else [])})
    else:
        subs.append({"id": "s1", "text": _redact(_strip_lead_verb(request or ""))[:70] or "(主任务)",
                     "hidden": False, "depends_on": []})

    # 2) 推断隐藏前置子问题(保守模板; 标 hidden=True)
    hidden = []
    if _has_any(blob, ("发", "提交", "汇报", "报告", "send", "submit", "报")):
        hidden.append("准备/校对要发出的内容(发之前)")
    if _has_any(blob, ("算", "统计", "计算", "汇总", "报表", "calculate", "compute")):
        hidden.append("先拿到/核对源数据(算之前)")
    if _has_any(blob, ("部署", "上线", "deploy", "ship", "release")):
        hidden.append("先本地验证 + 备份/可回滚预案(上线之前)")
    if kind == "build":
        hidden.append("确认需求边界 + 选型 + 最小可用切片(动手之前)")
    base_n = len(subs)
    for j, h in enumerate(hidden, 1):
        subs.append({"id": "h%d" % j, "text": h, "hidden": True,
                     "depends_on": [], "note": "隐藏前置, 易被忽略"})

    # 3) 可执行任务边界: 每个子问题 → 是否 actionable(有动词/对象)
    boundaries = []
    for s in subs:
        actionable = bool(_terms(s["text"])) and not s["text"].startswith("(")
        boundaries.append({"id": "t" + s["id"], "text": s["text"],
                           "actionable": actionable, "hidden": s["hidden"]})

    return {
        "subproblems": subs,
        "task_boundaries": boundaries,
        "n_explicit": base_n,
        "n_hidden": len(hidden),
        "note": "h* 是推断的隐藏前置子问题(最易漏); 任务边界 actionable=False 的需先细化再动手。",
    }


def _clarifying_questions(frame: dict) -> list:
    """可追问点(带默认值, 默认不阻塞 —— 呼应 User「能回滚就先做」)。

    只在**会实质改变做法**时追问: 模糊指代 / 缺成功标准 / 时间敏感却无 deadline /
    具体度过低 / 触红线需确认。每条都给一个「不答就这么办」的默认, 让小美能自主推进。
    """
    qs = []
    request = frame.get("request", "")
    kind = frame.get("request_kind", "")
    spec = frame.get("specificity", 1.0)

    # 1) 模糊指代缺前指
    for ha in frame.get("hidden_assumptions", []):
        if ha.get("kind") == "reference":
            qs.append({"q": ha["text"].replace("假设你我都清楚", "请确认") + " 具体指哪个?",
                       "why": "指代不明会让我做错对象", "default": "用当前共享语境里最近、最相关的那个",
                       "blocking": False})
    # 2) 续接但定位不到
    if any(ha.get("kind") == "state" for ha in frame.get("hidden_assumptions", [])):
        qs.append({"q": "「继续/上次的」指的是哪一个进行中的任务?",
                   "why": "续接需要先定位到具体任务", "default": "取最近一次未完成的相关任务",
                   "blocking": False})
    # 3) 成功标准全是模板(没有 User 自带的可度量验收)
    if not any(c.get("kind") in ("metric", "acceptance") and c.get("measurable")
               for c in frame.get("success_criteria", [])):
        qs.append({"q": "做成什么样算 done?(有没有可量化/可验收的线)",
                   "why": "没有验收线容易过度或不足", "default": "做到能用 + User 可 review/回滚为止",
                   "blocking": False})
    # 4) 时间敏感却没给 deadline
    blob = request + " " + frame.get("context", "")
    if _has_any(blob, ("急", "尽快", "马上", "asap")) and not _has_any(blob, _TIME_MARKERS[:12]):
        qs.append({"q": "「尽快」是指什么时候前要?",
                   "why": "急但无明确 deadline 难排期", "default": "按今天内可交付的最小切片先给",
                   "blocking": False})
    # 5) 具体度过低 / 探索型
    if spec < 0.35 or kind == "exploration":
        qs.append({"q": "这件事最想先解决的一个点是什么?(范围能不能先收窄)",
                   "why": "请求较宽泛, 收窄能更快出价值", "default": "先挑信息量最大、最可逆的一小步做",
                   "blocking": False})
    # 6) 触红线 → 这个必须确认(唯一可能 blocking 的)
    for c in frame.get("constraints", []):
        if c.get("kind") == "red_line":
            qs.append({"q": "确认要执行触及红线的动作吗?(" + c["text"][:40] + ")",
                       "why": "红线动作不可逆/影响他人/花钱", "default": "默认不执行, 等 User 明确批准",
                       "blocking": True})

    # 去重(按完整问题文本, 不用截断前缀 —— 两条仅在前缀后不同的红线确认问会被误并)
    seen, uniq = set(), []
    for q in qs:
        if q["q"] not in seen:
            seen.add(q["q"])
            uniq.append(q)
    return uniq


def _build_frame(request: str = "", text: str = "", ground: bool = True) -> dict:
    """构造完整问题框架: 所有维度 + 私密标记。纯计算, 不落库。"""
    request = str(request if request is not None else "")
    text = str(text if text is not None else "")
    kind = _classify_request(request, text)

    any_private = _text_private(request) or _is_secret_path(request) \
        or _text_private(text) or _is_secret_path(text)

    real_goal, mc_priv = _infer_real_goal(request, text, kind)
    stakeholders, sh_priv = (_stakeholders(request, text) if ground else ([{"name": "User",
                             "role": "principal+decider", "external": False}], False))
    hidden, ha_priv = (_hidden_assumptions(request, text, kind) if ground
                       else ([], False))
    constraints = _constraints(request, text, stakeholders)
    decomp = _decompose(request, text, kind)

    any_private = any_private or mc_priv or sh_priv or ha_priv

    frame = {
        "request": request,
        "context": text,
        "request_kind": kind,
        "specificity": _specificity(request, text),
        "problem_definition": _problem_definition(request, kind),
        "real_goal": real_goal,
        "success_criteria": _success_criteria(request, text, kind),
        "constraints": constraints,
        "stakeholders": stakeholders,
        "non_goals": _non_goals(request, text, kind),
        "hidden_assumptions": hidden,
        "subproblems": decomp["subproblems"],
        "task_boundaries": decomp["task_boundaries"],
        "_decomp_meta": {"n_explicit": decomp["n_explicit"], "n_hidden": decomp["n_hidden"]},
        "_any_private": any_private,
    }
    frame["clarifying_questions"] = _clarifying_questions(frame)
    return frame


# --------------------------------------------------------------------------- #
# DB(纯 stdlib sqlite; 自有库, 不碰主库)
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS pf_frames (
    frame_id     TEXT PRIMARY KEY,
    ts           TEXT,
    session_id   TEXT,
    request      TEXT,
    request_kind TEXT,
    frame        TEXT,
    n_subproblems INTEGER,
    n_constraints INTEGER,
    n_questions  INTEGER,
    private      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_pf_frames_ts ON pf_frames(ts);
CREATE INDEX IF NOT EXISTS ix_pf_frames_session ON pf_frames(session_id);
CREATE TABLE IF NOT EXISTS pf_handoffs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT,
    frame_id            TEXT,
    to_target           TEXT,
    action              TEXT,
    bundle              TEXT,
    note                TEXT,
    queued_approval_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_pf_handoffs_frame ON pf_handoffs(frame_id);
"""


def _conn(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _scrub_frame_for_store(frame: dict) -> dict:
    """落库前深拷贝并脱敏(结构留, 敏感正文抹; 框级私密则所有正文只留占位)。"""
    sf = json.loads(json.dumps(frame, ensure_ascii=False))
    fp = bool(sf.get("_any_private"))   # 框级私密 → 所有派生正文整段隐去(不按单字段重判)

    def _sc(x):
        return _scrub_for_store(x, fp)

    sf["request"] = _sc(sf.get("request", ""))
    sf["context"] = _sc(sf.get("context", ""))
    sf["problem_definition"] = _sc(sf.get("problem_definition", ""))
    rg = sf.get("real_goal", {})
    if isinstance(rg, dict):
        rg["literal_ask"] = _sc(rg.get("literal_ask", ""))
        for ig in rg.get("inferred_goals", []):
            if isinstance(ig, dict):
                ig["goal"] = _sc(ig.get("goal", ""))
        for ag in rg.get("aligned_strategic_goals", []):
            if isinstance(ag, dict):
                ag["title"] = _sc(ag.get("title", ""))
    for key in ("success_criteria", "constraints", "non_goals", "hidden_assumptions",
                "subproblems", "task_boundaries"):
        for item in sf.get(key, []):
            if isinstance(item, dict) and "text" in item:
                item["text"] = _sc(item.get("text", ""))
    for s in sf.get("stakeholders", []):
        if isinstance(s, dict):
            s["name"] = _sc(s.get("name", ""))
    for q in sf.get("clarifying_questions", []):
        if isinstance(q, dict):
            q["q"] = _sc(q.get("q", ""))
            q["default"] = _sc(q.get("default", ""))
    return sf


def _store_frame(frame: dict, session_id: str, db_path: str) -> str:
    """落库一个问题框架(敏感正文已脱敏); 返回 frame_id。"""
    fid = _frame_id(frame.get("request", "frame"))
    fp = bool(frame.get("_any_private"))
    private = 1 if fp else 0
    store_frame = _scrub_frame_for_store(frame)
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO pf_frames "
            "(frame_id, ts, session_id, request, request_kind, frame, n_subproblems, "
            " n_constraints, n_questions, private) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(frame_id) DO UPDATE SET "
            "ts=excluded.ts, session_id=excluded.session_id, request=excluded.request, "
            "request_kind=excluded.request_kind, frame=excluded.frame, "
            "n_subproblems=excluded.n_subproblems, n_constraints=excluded.n_constraints, "
            "n_questions=excluded.n_questions, "
            "private=MAX(private, excluded.private)",
            (fid, _now(), _safe_text(session_id),
             _safe_text(_scrub_for_store(frame.get("request", ""), fp)),
             frame.get("request_kind", ""),
             _safe_text(json.dumps(store_frame, ensure_ascii=False)),
             len(frame.get("subproblems", [])), len(frame.get("constraints", [])),
             len(frame.get("clarifying_questions", [])), private))
        conn.commit()
    finally:
        conn.close()
    return fid


def _get_frame(frame_id: str, db_path: str) -> dict | None:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT * FROM pf_frames WHERE frame_id=?", (frame_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    d["frame"] = _loads(d.get("frame"), {})
    return d


_FRAME_LIST_FIELDS = ("success_criteria", "constraints", "non_goals", "hidden_assumptions",
                      "subproblems", "task_boundaries", "stakeholders", "clarifying_questions")


def _normalize_frame(f: dict) -> dict:
    """读库后结构净化(防 DB 里 frame JSON 虽合法但结构损坏: 列表项非 dict / real_goal 非 dict),
    让所有下游分析动作复用同一道防御, 不必逐函数加 isinstance 守卫。"""
    if not isinstance(f, dict):
        return {}
    if not isinstance(f.get("real_goal"), dict):
        f["real_goal"] = {}
    for k in _FRAME_LIST_FIELDS:
        v = f.get(k)
        if isinstance(v, list):
            f[k] = [x for x in v if isinstance(x, dict)]
        elif k in f:
            f[k] = []
    return f


def _load_or_build(frame_id: str, request: str, text: str, db_path: str,
                   ground: bool = True):
    """分析动作的统一取框: 给 frame_id → 读库(脱敏副本); 给 request/text → 现场构造(不落库)。
    返回 (frame, any_private, source) 或 (None, False, error_msg)。"""
    frame_id = "" if frame_id is None else str(frame_id)
    if frame_id:
        row = _get_frame(frame_id, db_path)
        if not row:
            return None, False, "没找到问题框架 " + frame_id
        f = _normalize_frame(row.get("frame") or {})
        return f, bool(row.get("private")), "stored"
    if not (request or text):
        return None, False, "请给 frame_id, 或给 request(原始请求)让我现场框定。"
    f = _build_frame(request, text, ground=ground)
    return f, bool(f.get("_any_private")), "transient"


# --------------------------------------------------------------------------- #
# 1. frame — 把原始请求重构成完整问题框架(写自有库)
# --------------------------------------------------------------------------- #
def frame(request: str = "", text: str = "", session_id: str = "default",
          db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    if not (request or text):
        return {"ok": False,
                "error": "给我一句原始请求(request), 我把它重构成清晰的问题框架。",
                "hint": "jarvis problem_framer frame --request \"帮我弄一下那个奖金表\""}
    f = _build_frame(request, text, ground=True)
    fid = _store_frame(f, session_id, db)
    any_private = bool(f.get("_any_private"))
    _audit("frame", fid, kind=f["request_kind"], n_subproblems=len(f["subproblems"]),
           n_constraints=len(f["constraints"]), n_questions=len(f["clarifying_questions"]),
           private=any_private)
    out = {
        "ok": True, "frame_id": fid,
        "request_kind": f["request_kind"], "specificity": f["specificity"],
        "problem_definition": f["problem_definition"],
        "real_goal": f["real_goal"],
        "success_criteria": f["success_criteria"],
        "constraints": f["constraints"],
        "stakeholders": f["stakeholders"],
        "non_goals": f["non_goals"],
        "hidden_assumptions": f["hidden_assumptions"],
        "subproblems": f["subproblems"],
        "task_boundaries": f["task_boundaries"],
        "clarifying_questions": f["clarifying_questions"],
        "deferred": _deferred_status(),
        "note": "问题框架已构造并入库。用 infer_real_goal/success_criteria/constraints/stakeholders/"
                "non_goals/hidden_assumptions/decompose_problem/ask_clarifying_questions/reframe/"
                "explain 接着细看(传 frame_id)。动手前先看 constraints 里有没有红线、"
                "clarifying_questions 里有没有 blocking 项。",
    }
    return _flag_private(out, any_private)


# --------------------------------------------------------------------------- #
# 2. reframe — 换个角度重新框定(broaden/narrow/invert/first_principles/restate)
# --------------------------------------------------------------------------- #
_LENSES = ("broaden", "narrow", "invert", "first_principles", "restate")


def reframe(frame_id: str = "", request: str = "", text: str = "", lens: str = "restate",
            new_info: str = "", session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    lens = str(lens or "restate").lower()
    if lens not in _LENSES:
        lens = "restate"
    merged_req = (request or "")
    if new_info:
        text = (text + " " + new_info).strip()
    f, any_priv, src = _load_or_build(frame_id, merged_req, text, db)
    if f is None:
        return {"ok": False, "error": src, "lenses": list(_LENSES)}
    rg = f.get("real_goal", {})
    inferred = rg.get("inferred_goals", []) if isinstance(rg, dict) else []
    if lens == "broaden":
        reframed = "放大视角: 真正要解决的是 —— " + (
            inferred[0].get("goal", "") if inferred else f.get("problem_definition", ""))
        hint = "别盯着字面手段, 盯真实目标; 或许有更直接的解法。"
    elif lens == "narrow":
        actionable = [b for b in f.get("task_boundaries", []) if b.get("actionable")]
        smallest = actionable[0].get("text", "") if actionable else f.get("problem_definition", "")
        reframed = "收窄视角: 先只解决最小可行的一步 —— " + smallest
        hint = "先做最可逆、信息量最大的一小步, 再扩。"
    elif lens == "invert":
        avoid = [n.get("text", "") for n in f.get("non_goals", [])][:3]
        risks = [c.get("text", "") for c in f.get("constraints", []) if c.get("kind") == "red_line"][:2]
        reframed = "反转视角(怎样会失败): 要避免 —— " + "; ".join(avoid + risks) \
            if (avoid or risks) else "反转视角: 先想清楚什么算「搞砸了」, 再倒推怎么不搞砸。"
        hint = "从失败反推: 列出最糟结果, 确保每一步都在远离它。"
    elif lens == "first_principles":
        hard = [c["text"] for c in f.get("constraints", [])
                if c.get("severity") == "high"][:3]
        goal = inferred[0]["goal"] if inferred else f.get("problem_definition", "")
        reframed = "第一性原理(剥掉假设): 只保留硬约束 %s, 目标 %s; 其余假设先全部存疑。" % (
            ("、".join(hard) or "(无硬约束)"), goal)
        hint = "把 hidden_assumptions 全部摘出当假设质疑, 只从硬约束 + 真实目标重新推。"
    else:  # restate
        reframed = "重述: " + f.get("problem_definition", "")
        hint = "用一句无歧义的话讲清问题是什么。"
    out = {"ok": True, "frame_id": frame_id or None, "source": src, "lens": lens,
           "reframed": reframed, "hint": hint,
           "original_definition": f.get("problem_definition", ""),
           "available_lenses": list(_LENSES),
           "note": "reframe 不落库(只读视角变换); 满意了再用 frame 固化。"}
    _audit("reframe", frame_id, lens=lens)
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 3. infer_real_goal — 推断字面诉求背后的真实目标
# --------------------------------------------------------------------------- #
def infer_real_goal(frame_id: str = "", request: str = "", text: str = "",
                    session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "request_kind": f.get("request_kind", ""), "real_goal": f.get("real_goal", {}),
           "note": "字面诉求常是手段; 先问「User 要这个是为了达成什么」。confidence=低 时务必 ask_clarifying_questions。"}
    _audit("infer_real_goal", frame_id)
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 4. success_criteria — 派生成功/验收标准
# --------------------------------------------------------------------------- #
def success_criteria(frame_id: str = "", request: str = "", text: str = "",
                     session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    crit = f.get("success_criteria", [])
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "success_criteria": crit,
           "n_measurable": sum(1 for c in crit if c.get("measurable")),
           "note": "outcome=做成啥; acceptance=怎么验收; metric=可量化目标。"
                   "没有可度量项时, 先和 User 对一条验收线(见 ask_clarifying_questions)。"}
    _audit("success_criteria", frame_id, n=len(crit))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 5. constraints — 显化约束(时间/预算/技术/隐私/红线/利益相关方)
# --------------------------------------------------------------------------- #
def constraints(frame_id: str = "", request: str = "", text: str = "",
                session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    cons = f.get("constraints", [])
    red = [c for c in cons if c.get("kind") == "red_line"]
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "constraints": cons,
           "red_lines": red, "has_red_line": bool(red),
           "by_kind": _group_by(cons, "kind"),
           "note": "red_line 一律来自 local_powers 单一真相源(只读分级, 绝不自动执行); "
                   "命中红线的动作必须 User 显式确认。"}
    _audit("constraints", frame_id, n=len(cons), red=len(red))
    return _flag_private(out, any_priv)


def _group_by(items: list, key: str) -> dict:
    out = {}
    for it in items:
        k = it.get(key, "?") if isinstance(it, dict) else "?"
        out.setdefault(k, 0)
        out[k] += 1
    return out


# --------------------------------------------------------------------------- #
# 6. stakeholders — 利益相关方(谁受影响/谁拍板)
# --------------------------------------------------------------------------- #
def stakeholders(frame_id: str = "", request: str = "", text: str = "",
                 session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    sh = f.get("stakeholders", [])
    ext = [s for s in sh if s.get("external")]
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "stakeholders": sh, "external": ext, "has_external": bool(ext),
           "note": "User 默认是委托人+决策人; 出现 external=True 即涉及他人 → 触红线4, 对外动作需 User 确认。"}
    _audit("stakeholders", frame_id, n=len(sh), external=len(ext))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 7. non_goals — 明确不做的
# --------------------------------------------------------------------------- #
def non_goals(frame_id: str = "", request: str = "", text: str = "",
              session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    ng = f.get("non_goals", [])
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "non_goals": ng,
           "n_explicit": sum(1 for n in ng if not n.get("inferred")),
           "note": "inferred=True 的是我保守推断的非目标(防 scope creep, 呼应 surgical changes), 可让 User 否掉。"}
    _audit("non_goals", frame_id, n=len(ng))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 8. hidden_assumptions — 背景假设
# --------------------------------------------------------------------------- #
def hidden_assumptions(frame_id: str = "", request: str = "", text: str = "",
                       session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    ha = f.get("hidden_assumptions", [])
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "hidden_assumptions": ha, "by_kind": _group_by(ha, "kind"),
           "note": "reference=指代不明; state=假设有进行中任务; access=假设权限/数据就位; "
                   "definition=术语理解一致; world=世界模型背景。这些假设一旦不成立, 整个做法可能跑偏。"}
    _audit("hidden_assumptions", frame_id, n=len(ha))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 9. decompose_problem — 拆子问题 + 可执行任务边界
# --------------------------------------------------------------------------- #
def decompose_problem(frame_id: str = "", request: str = "", text: str = "",
                      session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    subs = f.get("subproblems", [])
    bounds = f.get("task_boundaries", [])
    hidden = [s for s in subs if s.get("hidden")]
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "subproblems": subs, "task_boundaries": bounds,
           "hidden_subproblems": hidden, "n_hidden": len(hidden),
           "note": "hidden=True 是推断的隐藏前置子问题(最易漏); task_boundaries 里 actionable=False 的需先细化。"}
    _audit("decompose_problem", frame_id, n_sub=len(subs), n_hidden=len(hidden))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 10. ask_clarifying_questions — 可追问点(带默认值, 默认不阻塞)
# --------------------------------------------------------------------------- #
def ask_clarifying_questions(frame_id: str = "", request: str = "", text: str = "",
                             session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    qs = f.get("clarifying_questions", [])
    blocking = [q for q in qs if q.get("blocking")]
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "clarifying_questions": qs,
           "blocking": blocking, "has_blocking": bool(blocking),
           "note": "每条都带 default(不答就这么办)→ 小美能自主推进, 不必每步问。"
                   "只有 blocking=True(通常是红线)才真停下来等 User。呼应「能回滚就先做」。"}
    _audit("ask_clarifying_questions", frame_id, n=len(qs), blocking=len(blocking))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 11. handoff — 把框定好的问题打包交接(落地动作必经审批闸; 绝不执行)
# --------------------------------------------------------------------------- #
def handoff(frame_id: str = "", to: str = "", action: str = "", note: str = "",
            db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    frame_id = "" if frame_id is None else str(frame_id)
    action = str(action if action is not None else "")
    row = _get_frame(frame_id, db) if frame_id else None
    if frame_id and not row:
        return {"ok": False, "error": "没找到问题框架 " + frame_id + "; 先 frame。"}
    f = (row or {}).get("frame") or {}
    bundle = {
        "frame_id": frame_id or None,
        "request_kind": f.get("request_kind"),
        "problem_definition": f.get("problem_definition"),
        "real_goal": (f.get("real_goal") or {}).get("inferred_goals")
        if isinstance(f.get("real_goal"), dict) else None,
        "n_constraints": (row or {}).get("n_constraints"),
        "n_subproblems": (row or {}).get("n_subproblems"),
        "n_questions": (row or {}).get("n_questions"),
        "to": to or "next-agent",
    }
    any_private = bool((row or {}).get("private"))
    queued_id = None
    needs_confirm = False
    red = None

    # 仅当 handoff 明确要"落地一个真动作"时, 才走红线分级 + 入审批队列(绝不执行)。
    if action:
        lp = _opt("local_powers")
        if lp is not None:
            try:
                red = lp._check_red_line(action)
                if not red and hasattr(lp, "non_shell_danger"):
                    red = lp.non_shell_danger(action)
            except Exception:
                red = None
        aps = _opt("autopilot_store")
        if aps is not None and hasattr(aps, "enqueue_approval"):
            try:
                queued_id = aps.enqueue_approval(
                    action=action, action_kind="handoff",
                    payload={"frame_id": frame_id, "bundle": bundle, "red_line": red},
                    risk="high",
                    why="problem_framer.handoff 要落地真动作: " + action[:120])
                needs_confirm = True
            except Exception:
                # 入队失败也视作队列不可达 → fail-closed: 需人工确认, 绝不静默丢动作。
                queued_id = None
                needs_confirm = True
        else:
            needs_confirm = True  # 审批队列不可达 → 不执行, 只回需人工接力(fail-safe)

    conn = _conn(db)
    try:
        cur = conn.execute(
            "INSERT INTO pf_handoffs (ts, frame_id, to_target, action, bundle, note, "
            "queued_approval_id) VALUES (?,?,?,?,?,?,?)",
            (_now(), _safe_text(frame_id or ""), _safe_text(bundle["to"]),
             _safe_text(action) or "(memo)",
             _safe_text(json.dumps(bundle, ensure_ascii=False)), _safe_text(note), queued_id))
        conn.commit()
        hid = cur.lastrowid
    finally:
        conn.close()
    _audit("handoff", frame_id, id=hid, has_action=bool(action),
           queued_approval_id=queued_id, red_line=bool(red))
    out = {"ok": True, "id": hid, "kind": "handoff", "frame_id": frame_id or None,
           "to": bundle["to"], "bundle": bundle, "note": note,
           "queued_approval_id": queued_id, "needs_confirm": needs_confirm, "red_line": red,
           "note2": ("纯交接备忘, 无外部副作用。" if not action
                     else "要落地的动作已入 autopilot 审批队列(risk=high), 绝不自动执行; "
                          "须 User 经 `jarvis autopilot approve <id>` 批准。")}
    return _flag_private(out, any_private)


# --------------------------------------------------------------------------- #
# 12. explain — 人话讲清这个问题框架(小美「我先把问题想清楚了」)
# --------------------------------------------------------------------------- #
def explain(frame_id: str = "", request: str = "", text: str = "",
            session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    f, any_priv, src = _load_or_build(frame_id, request, text, db)
    if f is None:
        return {"ok": False, "error": src}
    rg = f.get("real_goal", {})
    inferred = rg.get("inferred_goals", []) if isinstance(rg, dict) else []
    red = [c for c in f.get("constraints", []) if c.get("kind") == "red_line"]
    blocking = [q for q in f.get("clarifying_questions", []) if q.get("blocking")]
    # 全用 .get(默认) 取值, 容忍读库后结构损坏的项(配合 _normalize_frame 的字典过滤双保险)
    lines = [
        "① 你说的是: " + f.get("problem_definition", ""),
        "② 你真正想要的(推断): " + (inferred[0].get("goal", "") if inferred else "(待确认)"),
        "③ 做成这样算成功: " + "; ".join(c.get("text", "") for c in f.get("success_criteria", [])[:2]),
        "④ 约束/红线: " + ("; ".join(c.get("text", "") for c in f.get("constraints", [])[:3]) or "(暂无)"),
        "⑤ 涉及: " + "、".join(s.get("name", "") for s in f.get("stakeholders", [])[:4]),
        "⑥ 不做: " + ("; ".join(n.get("text", "") for n in f.get("non_goals", [])[:2]) or "(暂无)"),
        "⑦ 隐藏子问题: " + ("; ".join(s.get("text", "") for s in f.get("subproblems", []) if s.get("hidden"))[:120] or "(暂无)"),
        "⑧ 动手前可追问: " + ("; ".join(q.get("q", "") for q in f.get("clarifying_questions", [])[:2]) or "(够清楚, 可直接做)"),
    ]
    headline = "问题已框清, 可以动手" if not blocking else "有 %d 个红线/阻塞点需先确认" % len(blocking)
    conf = _frame_confidence(f)
    out = {"ok": True, "frame_id": frame_id or None, "source": src,
           "headline": headline, "narrative": lines,
           "has_red_line": bool(red), "n_blocking_questions": len(blocking),
           "confidence": conf,
           "note": "动手前先看 ④红线 + ⑧追问里有没有 blocking; 没有就按 default 自主推进(能回滚即可)。"}
    _audit("explain", frame_id)
    return _flag_private(out, any_priv)


def _frame_confidence(frame: dict) -> dict:
    """框定置信: 优先 metacognition_core(deferred), 缺则按具体度/红线/追问数启发式。"""
    mc = _opt("metacognition_core")
    if mc is not None and hasattr(mc, "assess"):
        try:
            r = mc.assess({"frame": frame})
            if isinstance(r, dict) and "confidence" in r:
                return {"value": r.get("confidence"), "via": "metacognition_core",
                        "uncertainty": r.get("uncertainty")}
        except Exception:
            pass
    base = 0.4 + 0.5 * float(frame.get("specificity", 0.5))
    base -= 0.08 * sum(1 for q in frame.get("clarifying_questions", []) if not q.get("blocking"))
    base -= 0.2 * sum(1 for q in frame.get("clarifying_questions", []) if q.get("blocking"))
    base = max(0.05, min(0.95, base))
    return {"value": round(base, 2), "via": "heuristic",
            "uncertainty": "高" if base < 0.4 else ("中" if base < 0.7 else "低")}


# --------------------------------------------------------------------------- #
# 13. audit — 隐私/安全自审(只读)
# --------------------------------------------------------------------------- #
def audit(frame_id: str = "", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    frame_id = "" if frame_id is None else str(frame_id)
    conn = _conn(db)
    try:
        n_frames = conn.execute("SELECT COUNT(*) c FROM pf_frames").fetchone()["c"]
        n_priv = conn.execute("SELECT COUNT(*) c FROM pf_frames WHERE private=1").fetchone()["c"]
        n_hand = conn.execute("SELECT COUNT(*) c FROM pf_handoffs").fetchone()["c"]
        # 自验: 任何入库的框架里都不该出现凭据文件路径(明文)
        leaked = 0
        for r in conn.execute("SELECT frame FROM pf_frames LIMIT 200").fetchall():
            g = _loads(r["frame"], {})
            blob = json.dumps(g, ensure_ascii=False)
            if _is_secret_path(blob):
                leaked += 1
    finally:
        conn.close()
    tail = []
    try:
        with open(_audit_path(), "r", encoding="utf-8") as fh:
            for line in fh.readlines()[-20:]:
                rec = _loads(line, None)
                if rec:
                    tail.append({k: rec.get(k) for k in ("ts", "action", "frame_id")})
    except OSError:
        pass
    out = {"ok": True,
           "storage": {"dir": _data_dir(), "db": db},
           "counts": {"frames": n_frames, "private_frames": n_priv, "handoffs": n_hand},
           "credential_paths_stored": leaked,   # 期望恒 0
           "deferred": _deferred_status(),
           "consumers": list(_CONSUMERS),
           "guarantees": [
               "全本机落盘, 不外发", "凭据/密钥文件路径与正文绝不存(硬拦)",
               "私密请求只留结构, 敏感正文脱敏后才落库", "对外/落地动作经 autopilot 审批队列",
               "从不自传 confirmed=True", "只读调 mission_control/world_model/mind_graph, 绝不改",
               "红线分级复用 local_powers 单一真相源, 绝不复制红线表"],
           "recent_audit": tail}
    if leaked > 0:
        out["WARN"] = "检测到疑似凭据路径入库(应为 0)→ 立即排查 _scrub_for_store 隐私门"
    if n_priv > 0:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 14. status — 总览 + 各引擎可用性 + deferred + consumers
# --------------------------------------------------------------------------- #
def _engines_available() -> dict:
    out = {m: (_opt(m) is not None) for m in _SOURCE_MODULES}
    out["local_powers"] = _opt("local_powers") is not None
    out["autopilot_store"] = _opt("autopilot_store") is not None
    out["privacy_stream"] = _opt("privacy_stream") is not None
    return out


def status(frame_id: str = "", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    frame_id = "" if frame_id is None else str(frame_id)
    if frame_id:
        row = _get_frame(frame_id, db)
        if not row:
            return {"ok": False, "error": "没找到问题框架 " + frame_id}
        out = {"ok": True, "frame_id": frame_id, "ts": row["ts"],
               "session_id": row["session_id"], "request_kind": row["request_kind"],
               "request": row["request"], "n_subproblems": row["n_subproblems"],
               "n_constraints": row["n_constraints"], "n_questions": row["n_questions"],
               "private": bool(row["private"])}
        if row["private"]:
            out["_private"] = True
        return out
    conn = _conn(db)
    try:
        n_frames = conn.execute("SELECT COUNT(*) c FROM pf_frames").fetchone()["c"]
        n_hand = conn.execute("SELECT COUNT(*) c FROM pf_handoffs").fetchone()["c"]
        recent = [{"frame_id": r["frame_id"], "ts": r["ts"], "session_id": r["session_id"],
                   "request": (r["request"] or "")[:50], "request_kind": r["request_kind"],
                   "n_questions": r["n_questions"], "private": bool(r["private"])}
                  for r in conn.execute(
                      "SELECT * FROM pf_frames ORDER BY ts DESC, frame_id DESC LIMIT 10").fetchall()]
    finally:
        conn.close()
    return {"ok": True, "db": db,
            "counts": {"frames": n_frames, "handoffs": n_hand},
            "recent": recent, "engines": _engines_available(),
            "deferred": _deferred_status(), "consumers": list(_CONSUMERS),
            "actions": ["frame", "reframe", "infer_real_goal", "success_criteria",
                        "constraints", "stakeholders", "non_goals", "hidden_assumptions",
                        "decompose_problem", "ask_clarifying_questions", "handoff",
                        "explain", "audit", "status"],
            "note": "问题框定与需求重构核心: frame 把原始请求重构成 问题定义/真实目标/成功标准/约束/"
                    "利益相关方/非目标/隐藏假设/子问题/可追问点/任务边界; 其余动作细看各维度。"
                    "纯只读分析门面, 落地动作经审批队列。处理复杂/模糊任务前先框一遍。"}


def health() -> dict:
    try:
        s = status()
        return {"ok": True, "detail": "问题框定核心就绪: "
                + str(s["counts"]["frames"]) + " 框架/" + str(s["counts"]["handoffs"]) + " 交接"}
    except Exception as e:
        return {"ok": False, "detail": "problem_framer 异常: " + str(e)}


# --------------------------------------------------------------------------- #
# LLM 工具面(只读且冻结): 仅 reframe/infer_real_goal/success_criteria/constraints/
# stakeholders/non_goals/hidden_assumptions/decompose_problem/ask_clarifying_questions/
# explain/status。frame(写库)/handoff(入审批队列)/audit CLI-only, 绝不暴露给模型。
# --------------------------------------------------------------------------- #
_DISPATCH_WHITELIST = {
    "reframe", "infer_real_goal", "success_criteria", "constraints", "stakeholders",
    "non_goals", "hidden_assumptions", "decompose_problem", "ask_clarifying_questions",
    "explain", "status",
}

_PF_PARAMS = {
    "type": "object",
    "properties": {
        "request": {"type": "string", "description": "User 的原始请求/任务描述(模糊也行)"},
        "text": {"type": "string", "description": "补充上下文(可选)"},
        "frame_id": {"type": "string", "description": "已 frame 入库的问题框架 id(复用)"},
        "lens": {"type": "string",
                 "description": "reframe 的角度: broaden/narrow/invert/first_principles/restate"},
    },
    "required": [],
}


def _tool(name, desc):
    return {"type": "function", "function": {
        "name": "problem_framer_" + name, "description": desc, "parameters": _PF_PARAMS}}


_PF_TOOLS = [
    _tool("infer_real_goal", "推断字面诉求背后的真实目标: User 要这个是为了达成什么。"
                             "回答「他到底想要什么」。处理模糊请求前先用它。只读。"),
    _tool("success_criteria", "派生「做成什么样算成功/done」的验收标准(尽量可度量)。只读。"),
    _tool("constraints", "显化约束: 时间/预算/技术/隐私 + 红线(转发 local_powers)。"
                         "回答「有什么限制、会不会碰红线」。只读。"),
    _tool("stakeholders", "识别利益相关方: 谁受影响/谁拍板; 涉及他人即触红线4。只读。"),
    _tool("non_goals", "列出明确不做的(防 scope creep, 呼应 surgical changes)。只读。"),
    _tool("hidden_assumptions", "挖出背景假设: 指代不明/假设有进行中任务/假设权限就位/术语理解一致。只读。"),
    _tool("decompose_problem", "拆成子问题 + 可执行任务边界, 并标出最易漏的隐藏前置子问题。只读。"),
    _tool("ask_clarifying_questions", "给出动手前值得追问的点(每条带默认值, 默认不阻塞)。"
                                      "回答「该问 User 什么、不答就怎么办」。只读。"),
    _tool("reframe", "换角度重新框定: broaden(看真目标)/narrow(收窄)/invert(怎样会失败)/"
                     "first_principles(剥假设)/restate(重述)。只读。"),
    _tool("explain", "人话讲清整个问题框架(你说的是/真想要的是/成功长啥样/红线/涉及谁/不做啥/"
                     "隐藏子问题/可追问)。小美「我先把问题想清楚了」。只读。"),
    _tool("status", "看问题框定核心总览(最近框架 + 各依据来源可用性)。只读。"),
]


def approved_tools() -> list:
    if not config.get("problem_framer.enabled", True) or \
            not config.get("problem_framer.expose_tools", True):
        return []
    return list(_PF_TOOLS)


def is_tool(name: str) -> bool:
    return name in ("problem_framer_" + v for v in _DISPATCH_WHITELIST)


def dispatch(name: str, args: dict | None = None) -> dict:
    """jarvis_brain 工具入口。只放行只读分析动作; 写库/落地类绝不暴露给模型。"""
    args = dict(args or {})
    # 只剥一次词首前缀(不用 str.replace 全替换, 否则双前缀 'problem_framer_problem_framer_status'
    # 会塌成 'status' 而被放行, 与 is_tool 不一致)。
    _pfx = "problem_framer_"
    short = name[len(_pfx):] if name.startswith(_pfx) else name
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": "动作 '" + short + "' 不在 problem_framer 模型白名单 "
                         + str(sorted(_DISPATCH_WHITELIST))
                         + "; frame/handoff/audit 请经 CLI。"}
    a = {"frame_id": args.get("frame_id", ""), "request": args.get("request", ""),
         "text": args.get("text", ""), "session_id": args.get("session_id", "default")}
    if short == "reframe":
        return reframe(frame_id=a["frame_id"], request=a["request"], text=a["text"],
                       lens=args.get("lens", "restate"), new_info=args.get("new_info", ""),
                       session_id=a["session_id"])
    if short == "status":
        return status(args.get("frame_id", ""))
    fn = {
        "infer_real_goal": infer_real_goal, "success_criteria": success_criteria,
        "constraints": constraints, "stakeholders": stakeholders, "non_goals": non_goals,
        "hidden_assumptions": hidden_assumptions, "decompose_problem": decompose_problem,
        "ask_clarifying_questions": ask_clarifying_questions, "explain": explain,
    }.get(short)
    if fn is not None:
        return fn(frame_id=a["frame_id"], request=a["request"], text=a["text"],
                  session_id=a["session_id"])
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
      ④ 无 confirmed=True; ⑤ 模型白名单只读(frame/handoff/audit 不暴露);
      ⑥ 行为: 凭据硬拦不入库 + 私密脱敏 + 红线显化 + 模糊请求追问 + 分解 + dispatch 白名单。
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
                return  # 跳过自检自身(它合法地内含被禁字面量做检查)
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
    for leaked in ("frame", "handoff", "audit"):
        if leaked in _DISPATCH_WHITELIST:
            fails.append("⑤ 模型白名单泄漏写库/落地动作: " + leaked)

    # ⑥ 行为验证: 临时库 + 各类请求, 验隐私门 + 框定 + dispatch 白名单。
    tmp = tempfile.mkdtemp(prefix="problem_framer_selftest_")
    db = os.path.join(tmp, "pf.db")
    try:
        # 凭据硬拦: 请求里塞凭据路径 → 入库后框架里不得出现明文路径
        b1 = frame(request="帮我备份 ~/.gmail_creds.json 这个文件", session_id="t", db_path=db)
        if not b1.get("ok"):
            fails.append("⑥ frame 失败: " + str(b1))
        else:
            row = _get_frame(b1["frame_id"], db)
            blob = json.dumps(row.get("frame", {}), ensure_ascii=False)
            if ".gmail_creds.json" in blob:
                fails.append("⑥ 凭据路径未被硬拦(出现在入库框架里)!")
            if row.get("private") != 1:
                fails.append("⑥ 含凭据的框架未标私密")
        # 红线显化: 请求含 DROP TABLE → constraints 应有 red_line(经 local_powers)
        cc = constraints(request="把数据库 DROP TABLE users 清掉", db_path=db)
        if not cc.get("ok"):
            fails.append("⑥ constraints 失败: " + str(cc))
        # 注: 红线检出依赖 local_powers 可达; 可达时必须检出, 不可达则跳过(不误判失败)
        if _opt("local_powers") is not None and not cc.get("has_red_line"):
            fails.append("⑥ DROP TABLE 未被显化为红线约束(local_powers 可达却没检出)")
        # 模糊请求 → ask_clarifying_questions 给出 ≥1 问
        aq = ask_clarifying_questions(request="帮我弄一下那个东西", db_path=db)
        if not (aq.get("ok") and aq.get("clarifying_questions")):
            fails.append("⑥ 模糊请求未产出可追问点")
        # 分解多步请求 → ≥2 任务边界
        dp = decompose_problem(request="先取数, 然后算奖金, 最后发飞书给 User", db_path=db)
        if not (dp.get("ok") and len(dp.get("task_boundaries", [])) >= 2):
            fails.append("⑥ 多步请求未拆出 ≥2 任务边界")
        # 对外动作 → stakeholders 标 external + 红线4
        sh = stakeholders(request="把月报发邮件给客户张总", db_path=db)
        if not (sh.get("ok") and sh.get("has_external")):
            fails.append("⑥ 对外请求未识别出外部利益相关方")
        # explain 形态完整
        ex = explain(request="该不该给这个功能加缓存", db_path=db)
        if not (ex.get("ok") and "narrative" in ex and "confidence" in ex):
            fails.append("⑥ explain 输出形态不完整: " + str(list(ex.keys())))
        # dispatch 白名单: 拒 frame/handoff/audit, 放行只读
        if dispatch("frame", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 frame(应拒绝)")
        if dispatch("handoff", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 handoff(应拒绝)")
        if dispatch("audit", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 audit(应拒绝)")
        if not dispatch("status", {}).get("ok"):
            fails.append("⑤ dispatch 应放行 status")
        if not dispatch("constraints", {"request": "今天内做完"}).get("ok"):
            fails.append("⑤ dispatch 应放行 constraints")
        # handoff 纯交接备忘(无 action) → 不触审批队列、无副作用(只写自有临时库)。
        # 落地动作必经审批队列的门由 test_problem_framer.py 用假 autopilot_store 验证,
        # selftest 不调真 enqueue_approval(避免污染真实审批队列)。
        h = handoff(frame_id=b1.get("frame_id", "") if b1.get("ok") else "",
                    note="selftest memo", db_path=db)
        if not h.get("ok"):
            fails.append("⑥ handoff(memo) 失败: " + str(h))
        elif h.get("needs_confirm") or h.get("queued_approval_id") is not None:
            fails.append("⑥ 无 action 的 handoff 不该触发审批/确认")
    except Exception as e:
        import traceback
        fails.append("⑥ 行为验证抛异常: " + str(e) + " | " + traceback.format_exc()[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fails:
        for f in fails:
            print("FAIL: " + f)
        return 1
    print("OK problem_framer selftest 全过(纯只读分析门面 + 零真实副作用):")
    print("  ① 不直接 import 任何执行面模块 ✓")
    print("  ② 源码无执行面方法调用(run_shell/write_file/type_text/click_on...) ✓")
    print("  ③ 无危险 stdlib 调用(system/remove/rmtree/exec...) ✓")
    print("  ④ 源码无 confirmed=True ✓")
    print("  ⑤ 模型白名单只读, 写库/落地动作(frame/handoff/audit)不暴露给模型 ✓")
    print("  ⑥ 凭据硬拦不入库 + 红线显化 + 模糊追问 + 分解 + 对外识别 + dispatch 白名单 ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI(argparse; 全 pretty-JSON via _emit)
# --------------------------------------------------------------------------- #
def _main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="problem_framer",
                                description="问题框定与需求重构核心")
    sub = p.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("frame", help="把原始请求重构成完整问题框架并入库")
    pf.add_argument("--request", default="", help="User 的原始请求(模糊也行)")
    pf.add_argument("--text", default="", help="补充上下文(可选)")
    pf.add_argument("--session", default="default")

    pr = sub.add_parser("reframe", help="换角度重新框定(broaden/narrow/invert/first_principles/restate)")
    pr.add_argument("--id", dest="frame_id", default="", help="已入库框架 id")
    pr.add_argument("--request", default="", help="原始请求(无 id 时现场构造)")
    pr.add_argument("--text", default="", help="补充上下文")
    pr.add_argument("--lens", default="restate", help="角度: broaden/narrow/invert/first_principles/restate")
    pr.add_argument("--new-info", dest="new_info", default="", help="新增信息(并入再框)")
    pr.add_argument("--session", default="default")

    # 同构的只读分析动作: 都接 frame_id 或 request/text
    for name, helptxt in (
            ("infer_real_goal", "推断字面诉求背后的真实目标"),
            ("success_criteria", "派生成功/验收标准(尽量可度量)"),
            ("constraints", "显化约束: 时间/预算/技术/隐私/红线"),
            ("stakeholders", "识别利益相关方(谁受影响/谁拍板; 涉及他人触红线4)"),
            ("non_goals", "明确不做的(防 scope creep)"),
            ("hidden_assumptions", "挖背景假设(指代/状态/权限/术语/世界)"),
            ("decompose_problem", "拆子问题 + 可执行任务边界 + 隐藏前置"),
            ("ask_clarifying_questions", "动手前可追问点(带默认值, 默认不阻塞)"),
            ("explain", "人话讲清整个问题框架")):
        sp = sub.add_parser(name, help=helptxt)
        sp.add_argument("--id", dest="frame_id", default="", help="已入库框架 id")
        sp.add_argument("--request", default="", help="原始请求(无 id 时现场构造)")
        sp.add_argument("--text", default="", help="补充上下文(无 id 时现场构造)")
        sp.add_argument("--session", default="default")

    ph = sub.add_parser("handoff", help="把框定好的问题打包交接(落地动作必经审批闸)")
    ph.add_argument("--id", dest="frame_id", default="")
    ph.add_argument("--to", default="")
    ph.add_argument("--action", default="", help="若要落地一个真动作: 描述它(必经红线分级+审批队列)")
    ph.add_argument("--note", default="")

    pa = sub.add_parser("audit", help="隐私/安全自审(只读)")
    pa.add_argument("--id", dest="frame_id", default="")

    ps = sub.add_parser("status", help="总览 + 各依据来源可用性")
    ps.add_argument("frame_id", nargs="?", default="")

    sub.add_parser("selftest", help="跑安全不变量自检")

    if not argv:
        _emit(status())
        return 0
    args = p.parse_args(argv)

    if args.cmd == "frame":
        _emit(frame(args.request, text=args.text, session_id=args.session))
    elif args.cmd == "reframe":
        _emit(reframe(frame_id=args.frame_id, request=args.request, text=args.text,
                      lens=args.lens, new_info=args.new_info, session_id=args.session))
    elif args.cmd in ("infer_real_goal", "success_criteria", "constraints", "stakeholders",
                      "non_goals", "hidden_assumptions", "decompose_problem",
                      "ask_clarifying_questions", "explain"):
        fn = globals()[args.cmd]
        _emit(fn(frame_id=args.frame_id, request=args.request, text=args.text,
                 session_id=args.session))
    elif args.cmd == "handoff":
        _emit(handoff(frame_id=args.frame_id, to=args.to, action=args.action, note=args.note))
    elif args.cmd == "audit":
        _emit(audit(args.frame_id))
    elif args.cmd == "status":
        _emit(status(args.frame_id))
    elif args.cmd == "selftest":
        return _selftest()
    else:
        _emit(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
