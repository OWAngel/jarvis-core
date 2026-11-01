#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""argument_prover.py — 小美的「可执行论证图与推理证明检查核心」。

把小美从「能给结论」升级成**能证明结论为什么成立**: 把她的任何结论、计划、文件归类理由、
业务建议、行动取舍, 自动展开成一张**论证图** —— 前提(premises) → 推理规则(inference
rules) → 中间结论(intermediates) → 最终主张(final claim) —— 然后逐环检查逻辑缺陷:
循环论证 / 跳步(非逻辑跳跃) / 偷换概念 / 因果倒置 / 相关不等于因果 / 未证明前提 /
价值判断混入事实判断, 并扫一遍经典谬误(诉诸权威·人身攻击·滑坡·非黑即白·以偏概全·
沉没成本·幸存者偏差…)。让小美每次说「所以应该这样」时, 都能把推理链摊开、指出哪一步
靠什么成立、哪里站不住。

定位: 本模块是**纯只读分析门面 + 离线确定性推理检查器**, 不自带执行引擎, 不重写任何安全门。
它把 mind_graph(认知图谱, 给前提找支撑/反对证据) / world_model(因果世界模型, 查因果方向、
相关 vs 因果) 的**只读·脱敏**面, 串成一个「论证构造 + 有效性检验 + 谬误扫描」层。接地从别的
节点捞文本进来时一律先过凭据硬拦 + 脱敏 + 私密判定, 私密则整图标私密(切本机收尾、落库脱敏)。
epistemic_core(信念/辩护) / problem_framer(问题框定) /
value_reasoner(事实 vs 价值) / metacognition_core(置信/不确定) 尚未落地 → 软导入记
deferred(待其上线自动接上, 绝不建桩)。证明本身是纯启发式离线计算, 不调任何 LLM。

★★★ 安全铁律(与 shared_context / simulation_lab / mission_control 一致, 自进化引擎不得改弱)★★★
  - 纯门面 + 零真实副作用: 本模块自身**绝不** subprocess / os.system / osascript / 删文件 /
    exec / __import__。build_argument 只写**自有库**(论证图缓存); handoff 若要落地真动作,
    一律 `autopilot_store.enqueue_approval(...)` 入审批队列(risk=high), 经 autopilot 过
    User 的全部门, 本模块绝不执行。
  - 永不自放行: **绝不**对任何引擎传 confirmed=True。只读地调 mind_graph / world_model 的
    查询/解释面(query / explain / causal_chain / context_for), **绝不**改图谱、绝不写世界模型。
  - 红线单一真相源: 任何要落地的动作分级一律转发 `local_powers._check_red_line` /
    `non_shell_danger`; 本模块绝不复制红线表。
  - 隐私: 命中私密(privacy_stream / file_tools 私密判定 / awareness 私密判定)的论证文本
    **只留结构, 敏感正文一律脱敏后才落库**; 任何含私密内容的输出带 `_private`(必要时
    `_force_local`), 由 jarvis_brain 切本机模型收尾。**凭据/密钥文件(.gmail_creds.json/
    .gcal_creds.json/...)路径与正文一律硬拦, 绝不读取、绝不纳入论证图。** 隐私库不可达 →
    一律按私密处理(fail-safe)。
  - LLM 工具面只读且冻结: 只暴露 premises / inference_chain / check_validity / find_gaps /
    fallacy_scan / minimal_proof / counterargument / strengthen / explain_logic / status;
    build_argument(写库) / handoff(入审批队列) / audit 走 CLI。

全本机, 纯 stdlib sqlite3。自有库 ~/.jarvis/argument_prover/argument_prover.db, 不碰主库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402 — 纯读取, 无副作用


# --------------------------------------------------------------------------- #
# 路径(env 优先 → config → 默认; 函数式以便测试用 env 覆盖、调用时实时取)
# --------------------------------------------------------------------------- #
_HOME_ENV = "JARVIS_ARGUMENT_PROVER_HOME"   # 测试设它指向 tmp; 优先于 config


def _data_dir() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(
        config.get("argument_prover.data_dir", "~/.jarvis/argument_prover")))


def _db_path() -> str:
    return os.path.join(_data_dir(), "argument_prover.db")


def _audit_path() -> str:
    return os.path.join(_data_dir(), "argument_prover.jsonl")


# --------------------------------------------------------------------------- #
# 软导入可选集成模块(importlib, 非 __import__/exec); 缺/出错 → None(降级, 永不抛)
# --------------------------------------------------------------------------- #
def _opt(name):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# 尚未落地的推理/框定/价值/元认知内核 → 记 deferred, 绝不建桩(待其上线 _opt 自动接上)。
_DEFERRED = ("epistemic_core", "problem_framer", "value_reasoner", "metacognition_core")
# 本模块接入(读取)的论证依据来源; 全部只读, 绝不写图谱/世界模型。
# 只列**真正被调用**的来源(status().engines 不虚报); 待接入更多来源时再加。
_SOURCE_MODULES = (
    "mind_graph", "world_model",
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


def _params(kvs) -> dict:
    d = {}
    for kv in kvs or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k] = v
    return d


def _audit(action: str, argument_id: str = "", **extra) -> None:
    """只记 ids/动作/计数/标志的审计流, **绝不写论证正文/隐私**, 永不抛。"""
    try:
        rec = {"ts": _now(), "action": action, "argument_id": argument_id or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        path = _audit_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# 进程内单调序号: itertools.count().__next__ 在 CPython 下原子, 保证同一微秒内连续
# build_argument 的 id 输入也各异 → 彻底消除 id 相撞导致 INSERT OR REPLACE 互相覆盖。
_ARG_SEQ = itertools.count()


def _arg_id(seed: str) -> str:
    stamp = datetime.now().isoformat(timespec="microseconds")
    seq = next(_ARG_SEQ)
    h = hashlib.sha1(
        (seed + stamp + ":" + str(seq)).encode("utf-8", "replace")
    ).hexdigest()[:12]
    return "arg_" + h


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
    """凭据/密钥文件 → True(硬拦, 绝不纳入论证图)。判不准当机密(fail-safe)。

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
    return False


# 本地兜底敏感模式(privacy_stream 不可达时仍能脱敏): 长数字串/邮箱/卡号/工资语境等。
# 各分支长度都**有界**, 杜绝在超长良性输入上回溯爆炸(ReDoS)。
_REDACT_RX = re.compile(
    r"\b\d[\d \-]{5,30}\d\b|[\w.+-]{1,64}@[\w.-]{1,255}\.\w{2,24}|"
    r"(?:\d{4}[ -]?){3}\d{4}|\b1[3-9]\d{9}\b",
    re.IGNORECASE)


def _redact(text: str) -> str:
    """脱敏一段正文: 优先 privacy_stream 的姿态; 再本地兜底抹长数字/邮箱/卡号/手机。
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
    """这段论证正文是否含私密内容(走 privacy_stream 单一真相源; 缺则本地兜底)。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False(本模块无对外出口)。"""
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


def _scrub_for_store(text: str) -> str:
    """落库前清洗(与 shared_context「私密只留元数据」一致):
       凭据 → 硬清; 私密正文 → 整段隐去(只留结构); 普通正文 → 脱敏长数字/邮箱后入库。"""
    t = text or ""
    if _is_secret_path(t):
        return "（凭据/密钥已硬拦, 不入库）"
    if _text_private(t):
        return "（私密内容已隐去, 仅留论证结构）"
    return _redact(t)


def _flag_private(out: dict, any_private: bool) -> dict:
    """含私密 → 打 _private; 未授权上云则加 _force_local, 由 jarvis_brain 切本机收尾。"""
    if any_private:
        out["_private"] = True
        if not config.get("safety.allow_cloud_private", False):
            out["_force_local"] = True
    return out


# --------------------------------------------------------------------------- #
# 语言学资源(中英双语标记; 论证解析与谬误检测的离线启发式)
# --------------------------------------------------------------------------- #
# 前提引导词(其后是前提)。
_PREMISE_MARKERS = (
    "因为", "由于", "既然", "鉴于", "考虑到", "基于", "根据", "假设", "假定",
    "前提是", "毕竟", "因为说", "since", "because", "given that", "given", "as ",
    "assuming", "for ",
)
# 结论引导词(其后是结论/中间结论)。
_CONCLUSION_MARKERS = (
    "所以", "因此", "故", "可见", "由此", "因而", "于是", "这说明", "这意味着",
    "这表明", "综上", "总之", "结论是", "得出", "推出", "可以断定", "足以说明",
    "therefore", "thus", "hence", "so ", "consequently", "it follows", "as a result",
)
# 价值/规范判断标记(应然/评价)。
_VALUE_MARKERS = (
    "应该", "应当", "必须", "得要", "最好", "值得", "不值得", "划算", "不划算",
    "对的", "错的", "好的", "坏的", "公平", "不公平", "合理", "不合理", "重要",
    "该不该", "应不应", "优秀", "糟糕", "更好", "更差", "正确", "错误", "正义",
    "道德", "不道德", "美", "丑", "善", "恶", "should", "ought", "must", "better",
    "worth", "good", "bad", "right", "wrong", "fair", "unfair", "ought to",
)
# 事实/经验判断标记(实然)。
_FACT_MARKERS = (
    "数据显示", "统计", "测量", "调查显示", "事实上", "实际上", "实测", "记录",
    "增长", "下降", "上升", "减少", "占比", "比例", "数量", "金额", "发生", "发现",
    "出现", "data shows", "measured", "statistics", "occurred", "observed", "%",
)
# 因果标记(主张里出现 → 这是一个因果论断)。
_CAUSAL_MARKERS = (
    "导致", "造成", "引起", "使得", "促成", "致使", "因果", "带来", "驱动", "决定了",
    "才会", "所以才", "causes", "caused", "leads to", "results in", "因为.*所以",
)
# 相关/共现标记(只有这些、没机制 → 相关不等于因果嫌疑)。
_CORRELATION_MARKERS = (
    "相关", "同时", "一起", "一致", "同步", "都在", "也在", "伴随", "总是一块",
    "越来越", "正相关", "负相关", "趋势一样", "同涨同跌", "correlat", "associated",
    "along with", "at the same time",
)
# 机制标记(有机制 → 因果更可信, 抵消相关嫌疑)。
_MECHANISM_MARKERS = (
    "机制", "原理", "通过", "经由", "之所以", "其原因在于", "作用于", "传导", "链路",
    "因为它", "由于它", "mechanism", "because it", "via", "through",
)
# 条件句标记(用于检测肯定后件/否定前件)。各组**长度有界**, 防 ReDoS 回溯爆炸。
_COND_RX = re.compile(
    r"如果(.{1,200}?)[,，]?\s*(?:那么|则|就)(.{1,400})|"
    r"若(.{1,200}?)[,，]?\s*(?:则|就)(.{1,400})|"
    r"\bif\b(.{1,200}?)\bthen\b(.{1,400})", re.IGNORECASE)

# 经典谬误目录(文本可检测的修辞型谬误; 结构型谬误见专门检测器)。
# (id, 中文名, en, severity, 正则, 为什么是谬误, 怎么修)
_FALLACY_CATALOGUE = [
    ("appeal_authority", "诉诸权威", "appeal to authority", "med",
     r"专家(说|表示|认为|指出)|权威(说|表示|认为)|名人(说|都)|老板说了算|书上(说|写)|"
     r"教授说|大[V佬](说|讲)|(专家|权威|名人).{0,8}(就是对|肯定对|没错|不会错)",
     "「某权威说」本身不构成证据, 权威也会错; 该看权威给出的论据与证据。",
     "把权威换成它依据的具体证据/数据; 或交叉验证多个独立来源。"),
    ("ad_hominem", "人身攻击", "ad hominem", "med",
     r"你(懂什么|算老几|配吗|又不是)|人品(有问题|不行|差)|既得利益|屁股决定脑袋|"
     r"就你也配|你这种人|他人品",
     "攻击提出者的人品/身份, 不等于驳倒了他的论点; 论点对错与谁说的无关。",
     "把矛头从「谁说的」转回「说了什么」, 直接检验论点本身。"),
    ("appeal_emotion", "诉诸情感", "appeal to emotion", "med",
     r"难道你(忍心|希望|愿意)|为了(孩子|家人|父母|大家)|你不(心疼|可怜)|多可怜|"
     r"于心何忍|将心比心.{0,6}(所以|就该)",
     "激起情绪不等于给出理由; 同情/恐惧不能替代论据。",
     "把情绪诉求换成事实与利弊分析; 情感可作动机, 不可作论据。"),
    ("appeal_popularity", "诉诸大众", "appeal to popularity", "med",
     r"大家都|所有人都|人人都|没有人会(觉得|认为)|主流(都|观点是)|大部分人都|"
     r"这么多人(都|选)|随大流|从众",
     "很多人相信不等于它是对的(从众谬误); 真理不靠投票。",
     "找独立于「多少人信」的证据; 多数曾经也错过。"),
    ("slippery_slope", "滑坡谬误", "slippery slope", "med",
     r"一旦.{0,12}(就会|必然|早晚).{0,12}(最终|崩|失控|完蛋)|接下来(必然|肯定|一定)会|"
     r"长此以往|早晚(会|要)出事|滑向|一发不可收|开了这个口子",
     "把「一小步」当成「必然滑到灾难」, 中间每一步的概率都没论证。",
     "逐步论证每一环的真实概率, 别把可能当必然。"),
    ("false_dilemma", "非黑即白", "false dilemma", "high",
     r"要么.{1,20}要么|不是.{1,12}就是|只有.{1,12}才(能|行|可以)|"
     r"没有(别的|其他)(选择|办法|可能|路)|非.{1,6}即",
     "把连续/多样的选项压成「只有两条路」, 抹掉了中间地带与第三选项。",
     "列出被忽略的中间方案与其它可能, 再比较。"),
    ("hasty_generalization", "以偏概全", "hasty generalization", "med",
     r"我认识的.{0,8}都|有(一次|个).{0,12}(所以|可见|说明)|总是这样|从来都|无一例外|"
     r"所有.{1,8}都(是|会|很|这样)|个个都",
     "从极少数样本/单个例子跳到全称结论, 样本不足以支撑。",
     "扩大样本或限定结论范围(把「所有」改成「部分/在某条件下」)。"),
    ("straw_man", "稻草人", "straw man", "med",
     r"你的意思(就是|不就是)|所以你(是想|就是要)|按你说的就是|你是在说.{0,8}(吧|对吧)|"
     r"你不就是想",
     "把对方观点歪曲成一个更容易打的版本, 再去打它。",
     "先准确复述对方最强版本(steelman), 再反驳。"),
    ("sunk_cost", "沉没成本", "sunk cost fallacy", "med",
     r"都已经(投入|花了|做了|来了)|不能(白费|前功尽弃|半途而废)|坚持(了)?这么久|"
     r"来都来了|不然之前(都|就)白",
     "已花的成本不可回收, 不该成为继续的理由; 决策只看边际未来收益。",
     "忽略已沉没的部分, 只问「从现在起继续 vs 停止」哪个更划算。"),
    ("anecdotal", "轶事证据", "anecdotal evidence", "med",
     r"我(有个|认识个?)(朋友|亲戚|同事|邻居)|我(亲眼|亲身|自己)(见|经历)|身边(就)?有(人|例子)|"
     r"举我自己的例子",
     "个人轶事是单点观察, 易受偶然与记忆偏差影响, 不能代表总体。",
     "用系统数据/受控比较替代「我身边」。"),
    ("survivorship", "幸存者偏差", "survivorship bias", "med",
     r"(成功|赚钱|考上|做到)的(人|都).{0,10}(所以|可见|说明)|那些.{0,8}都(成功|做到|赢)了|"
     r"看看那些(成功|赢家)",
     "只看见「活下来的样本」, 忽略了同样做法却失败/消失的大多数。",
     "把失败/退出的样本也纳入, 看真实成功率。"),
    ("post_hoc", "后此谬误", "post hoc / false cause", "high",
     r"(自从|之后|后来|刚).{0,12}(所以|就是因为|导致|肯定是因为)|前脚.{0,8}后脚|"
     r"一.{0,6}就.{0,6}(肯定|一定)是因为",
     "「B 在 A 之后发生」不等于「A 导致 B」; 先后顺序不是因果。",
     "找因果机制 + 排除共同原因/巧合; 见 因果倒置/相关非因果检测。"),
    ("appeal_nature", "诉诸自然", "appeal to nature", "low",
     r"(天然|自然)的(就是|才|最)(好|对|健康|安全)|不(自然|天然)就(不好|有害)|违背(自然|天性)",
     "「天然/自然」不等于「好」, 「人造」不等于「坏」; 自然界也有毒物。",
     "用实际效果/风险数据评价, 而非「天不天然」。"),
    ("tu_quoque", "你也一样", "tu quoque", "low",
     r"你自己(还不是|也|不也)|你也(一样|没资格|这样)|五十步笑百步|那你怎么不",
     "「你自己也这样」不能反驳对方论点的对错(顶多说他虚伪)。",
     "把对方是否言行一致, 和论点本身的对错分开看。"),
    ("whataboutism", "转移话题", "whataboutism", "low",
     r"那(为什么|怎么)不说|别人(更|还).{0,8}(为什么|怎么不)|你(怎么|为啥)不说.{0,6}(那个|别的)",
     "用「那别的呢」转移焦点, 回避了当前论点, 不构成回应。",
     "先把当前论点处理完, 别的问题另开话题。"),
    ("appeal_ignorance", "诉诸无知", "appeal to ignorance", "med",
     r"没有(证据|人)(能)?证明.{0,10}(所以|就|因此).{0,8}(不|没有|是假|不存在)|"
     r"不能证明.{0,6}就(是|说明|等于)",
     "「没被证明为真」不等于「假」, 反之亦然; 举证责任在主张方。",
     "明确举证责任, 缺证据时给「未知」而非「否定」。"),
    ("loaded_question", "复杂问句", "loaded question", "low",
     r"你(还在|是不是又|什么时候才)(不|停止|改)|你(到底|还要).{0,8}(到什么时候)",
     "问句里预设了未经同意的前提(如「你还在……」预设你一直在)。",
     "先拆出隐藏预设, 单独检验它是否成立。"),
]


# --------------------------------------------------------------------------- #
# 分词 / 重叠度(中英混排; 用于循环论证、偷换概念、跳步等结构检测)
# --------------------------------------------------------------------------- #
_STOPWORDS = frozenset({
    "的", "了", "是", "在", "和", "与", "也", "都", "就", "而", "这", "那", "我",
    "你", "他", "它", "们", "一个", "这个", "那个", "因为", "所以", "但是", "如果",
    "一", "会", "有", "对", "把", "被", "让", "给", "向", "从", "到", "为", "以",
    "及", "或", "等", "之", "其", "于", "且", "并", "却", "则", "故", "可见",
    "a", "an", "the", "is", "are", "to", "of", "and", "or", "in", "on", "it",
    "this", "that", "be", "so", "if", "we", "you", "they", "he", "she",
})


def _terms(text: str) -> set:
    """内容词集合: CJK 抽 2 字 gram + ASCII 词(≥2, 去停用词)。给重叠度/概念检测用。"""
    t = (text or "").lower()
    out = set()
    # ASCII 词
    for w in re.findall(r"[a-z][a-z0-9_]+", t):
        if len(w) >= 2 and w not in _STOPWORDS:
            out.add(w)
    # CJK: 连续汉字串切 2 字 gram(近似词)
    for run in re.findall(r"[一-鿿]+", t):
        if len(run) == 1:
            if run not in _STOPWORDS:
                out.add(run)
            continue
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            # 跳过跨停用词的垃圾 bigram(如「的方」「为他」), 它们会污染 gap/偷换概念判定。
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
        mm = m.lower()
        if mm in t:
            return True
    return False


def _which(text: str, markers) -> list:
    t = (text or "").lower()
    return [m for m in markers if m.lower() in t]


def _split_clauses(text: str) -> list:
    """按中英标点切句/分句(逗号也切, 以便抓住「因为…所以…」的分句)。"""
    if not text:
        return []
    parts = re.split(r"[。!?；;,，\n]", text)
    out = []
    for p in parts:
        p = p.strip()
        if p:
            out.append(p)
    # 若整体没被切开(无强标点), 也按 因为/所以 这类连接词软切一刀
    if len(out) <= 1 and text.strip():
        soft = re.split(r"(?<=。)|(?:所以|因此|因而|于是|故)", text)
        out = [s.strip() for s in soft if s and s.strip()]
        if not out:
            out = [text.strip()]
    return out


# --------------------------------------------------------------------------- #
# 论证图构造(纯启发式离线; 前提→规则→中间结论→最终主张)
# --------------------------------------------------------------------------- #
def _classify_statement(text: str) -> str:
    """事实 / 价值 / 规范 / 混合 / 假设 —— 价值优先借 value_reasoner(deferred), 缺则启发式。"""
    t = text or ""
    vr = _opt("value_reasoner")
    if vr is not None and hasattr(vr, "classify"):
        try:
            r = vr.classify(t)
            kind = (r or {}).get("kind") if isinstance(r, dict) else None
            if kind:
                return str(kind)
        except Exception:
            pass
    has_val = _has_any(t, _VALUE_MARKERS)
    has_fact = _has_any(t, _FACT_MARKERS)
    if _has_any(t, ("假设", "假定", "假如", "若", "assume", "suppose")):
        return "assumption"
    if has_val and has_fact:
        return "mixed"
    if has_val:
        # 「应该/必须」类是规范; 「好/坏/值得」类是价值
        if _has_any(t, ("应该", "应当", "必须", "得要", "该不该", "应不应", "should", "ought", "must")):
            return "normative"
        return "value"
    return "fact"


def _rule_type(premise_texts, conclusion_text) -> tuple:
    """判推理规则类型 + 强度 + 保证(warrant 提示)。"""
    joined = " ".join(premise_texts) + " " + (conclusion_text or "")
    if _has_any(joined, _CAUSAL_MARKERS) or re.search(r"因为.+所以", joined):
        return "causal", 0.55, "因果规则: 由原因推结果(需机制, 防相关当因果/因果倒置)"
    if _COND_RX.search(joined) or _has_any(joined, ("当且仅当", "凡是", "只要", "iff")):
        return "deductive", 0.8, "演绎规则: 形式有效则结论必然(防肯定后件/否定前件)"
    if _has_any(joined, ("类比", "就像", "好比", "如同", "analog", "like ")):
        return "analogical", 0.4, "类比规则: 相似处推相似(类比强度取决于相关相似点)"
    if _has_any(joined, ("专家", "权威", "据说", "书上", "authority")):
        return "authority", 0.35, "诉诸来源: 结论强度不超过来源可靠度"
    if _has_any(joined, ("通常", "大多", "一般", "倾向", "往往", "统计", "概率", "most", "usually")):
        return "inductive", 0.5, "归纳规则: 由样本推一般(强度取决于样本量与代表性)"
    return "inductive", 0.45, "默认按归纳/或然处理: 前提为真只让结论更可能, 非必然"


def _extract_structure(claim: str, text: str, premises_in) -> dict:
    """把(claim, text, 显式 premises)解析成原始论证骨架: premises + 中间结论 + 最终主张。"""
    premises = []
    intermediates = []
    final_claim = str(claim if claim is not None else "").strip()

    # 1) 显式传入的 premises
    for p in (premises_in or []):
        s = str(p if p is not None else "").strip()
        if s:
            premises.append(s)

    # 2) 从 text 抽前提/结论
    clauses = _split_clauses(str(text if text is not None else ""))
    derived_conclusion = ""
    for c in clauses:
        is_concl = False
        s = c
        for m in _CONCLUSION_MARKERS:
            idx = c.lower().find(m.lower())
            if idx != -1:
                is_concl = True
                s = c[idx + len(m):].strip("，,。 ：:").strip()
                break
        if is_concl and s:
            if not final_claim:
                derived_conclusion = s
            else:
                # 已有最终主张 → 这是中间结论(除非与最终主张高度重合)
                if _overlap(s, final_claim) < 0.7:
                    intermediates.append(s)
                else:
                    derived_conclusion = s
            continue
        # 前提(可能带前提引导词, 去掉引导词留干净正文)
        ps = c
        had_pm = False
        for m in _PREMISE_MARKERS:
            idx = c.lower().find(m.lower())
            if idx != -1:
                ps = c[idx + len(m):].strip("，,。 ：:").strip()
                had_pm = True
                break
        if ps:
            # 跟结论高度重合**又没有前提引导词** → 这是结论复述, 不当前提(防误报循环);
            # 但带「因为/由于」等引导词的同义句要保留, 那正是真·循环论证要抓的。
            if _overlap(ps, final_claim) >= 0.85 and not had_pm:
                continue
            premises.append(ps)

    if not final_claim:
        final_claim = derived_conclusion or (premises.pop() if premises else "")

    # 去重(保序)
    seen = set()
    uniq = []
    for p in premises:
        key = p.strip()
        if key and key not in seen:
            seen.add(key)
            uniq.append(p)
    return {"premises": uniq, "intermediates": intermediates, "final_claim": final_claim}


def _ground_premise(text: str) -> dict:
    """给一条前提找支撑/反对证据(只读 mind_graph + world_model; 全 best-effort, 永不抛)。

    接地会从**别的数据节点**捞文本进来 → 是隐私泄漏口。三重防护:
      · 凭据/密钥串 → 硬拦丢弃(绝不进论证图);
      · 普通串 → 脱敏(_redact)后才纳入;
      · 命中私密(图谱标 private / 来源标 _private / 文本判私密)→ 置 out['private']=True,
        交由 _build_graph 把整条前提与整张图标私密, 收尾切本机模型、落库脱敏。
    """
    out = {"grounded": False, "support": [], "contradict": [], "via": [], "private": False}
    t = (text or "").strip()
    if not t:
        return out

    def _clean(item):
        """外部证据 → (可读脱敏短串 或 None, 是否私密)。凭据 → (None, True) 硬拦丢弃。"""
        if isinstance(item, dict):
            s = str(item.get("name") or item.get("entity") or item.get("text")
                    or item.get("snippet") or "")
        else:
            s = str(item if item is not None else "")
        s = s.strip()
        if not s:
            return None, False
        if _is_secret_path(s):          # 凭据/密钥 → 绝不纳入论证图
            return None, True            # 命中即视作私密(并丢弃)
        return _redact(s)[:60], _text_private(s)

    def _add(bucket, raw):
        s, priv = _clean(raw)
        if priv:
            out["private"] = True
        if s:
            out[bucket].append(s)

    mg = _opt("mind_graph")
    if mg is not None and hasattr(mg, "query"):
        try:
            q = mg.query(t, k=4)
            if isinstance(q, dict):
                if q.get("_private") or q.get("_force_local"):
                    out["private"] = True
                id2name = {}
                for n in (q.get("nodes") or []):
                    if isinstance(n, dict):
                        if n.get("private"):
                            out["private"] = True
                        nid = n.get("id")
                        if nid is not None:
                            id2name[str(nid)] = str(n.get("name") or n.get("id") or "")
                if q.get("nodes"):
                    out["grounded"] = True
                    out["via"].append("mind_graph")
                for e in (q.get("edges") or []):
                    if not isinstance(e, dict):
                        continue
                    rt = e.get("rtype")
                    end = e.get("src") if e.get("src") is not None else e.get("dst")
                    name = id2name.get(str(end), str(end if end is not None else ""))
                    if rt == "supports":
                        _add("support", name)
                    elif rt == "contradicts":
                        _add("contradict", name)
        except Exception:
            pass
    wm = _opt("world_model")
    if wm is not None and hasattr(wm, "explain"):
        try:
            ex = wm.explain(t)
            if isinstance(ex, dict):
                if ex.get("_private") or ex.get("_force_local"):
                    out["private"] = True
                if ex.get("found"):
                    out["grounded"] = True
                    if "world_model" not in out["via"]:
                        out["via"].append("world_model")
                for b in (ex.get("because") or [])[:3]:
                    _add("support", b)
                for c in (ex.get("contradicts") or [])[:3]:
                    _add("contradict", c)
        except Exception:
            pass
    # 去重 + 截断
    out["support"] = list(dict.fromkeys([s for s in out["support"] if s]))[:5]
    out["contradict"] = list(dict.fromkeys([s for s in out["contradict"] if s]))[:5]
    return out


def _build_graph(claim: str, text: str = "", premises_in=None, ground: bool = True) -> dict:
    """构造完整论证图: 节点(前提/规则/中间结论/最终主张) + 边 + 私密标记。纯计算, 不落库。"""
    st = _extract_structure(claim, text, premises_in)
    final_claim = st["final_claim"]

    any_private = _text_private(final_claim) or _is_secret_path(final_claim)
    nodes_premises = []
    for i, p in enumerate(st["premises"], 1):
        priv = _text_private(p) or _is_secret_path(p)
        g = _ground_premise(p) if (ground and not priv) else {
            "grounded": False, "support": [], "contradict": [], "via": [], "private": False}
        if g.get("private"):          # 接地引入了私密/凭据内容 → 整条前提按私密处理
            priv = True
        if priv:
            any_private = True
        status = "grounded" if g["grounded"] else "unproven"
        nodes_premises.append({
            "id": "p%d" % i,
            "text": p,
            "kind": _classify_statement(p),
            "status": status,
            "support": g["support"],
            "contradict": g["contradict"],
            "grounded_via": g["via"],
            "private": priv,
        })

    intermediates = []
    for j, c in enumerate(st["intermediates"], 1):
        if _text_private(c) or _is_secret_path(c):
            any_private = True
        intermediates.append({"id": "c%d" % j, "text": c,
                              "kind": _classify_statement(c)})

    # 规则: 简单模型 —— 所有前提 → (首个中间结论或最终主张), 中间结论再 → 最终主张。
    rules = []
    p_ids = [p["id"] for p in nodes_premises]
    p_txts = [p["text"] for p in nodes_premises]
    target_id = "claim"
    if intermediates:
        # 前提 → 第一个中间结论 → … → 最终主张
        rt, strength, warrant = _rule_type(p_txts, intermediates[0]["text"])
        rules.append({"id": "r1", "type": rt, "from": p_ids,
                      "to": intermediates[0]["id"], "strength": strength, "warrant": warrant})
        for k in range(len(intermediates) - 1):
            rt, strength, warrant = _rule_type([intermediates[k]["text"]],
                                               intermediates[k + 1]["text"])
            rules.append({"id": "r%d" % (k + 2), "type": rt,
                          "from": [intermediates[k]["id"]], "to": intermediates[k + 1]["id"],
                          "strength": strength, "warrant": warrant})
        rt, strength, warrant = _rule_type([intermediates[-1]["text"]], final_claim)
        rules.append({"id": "r%d" % (len(intermediates) + 1), "type": rt,
                      "from": [intermediates[-1]["id"]], "to": target_id,
                      "strength": strength, "warrant": warrant})
    else:
        rt, strength, warrant = _rule_type(p_txts, final_claim)
        rules.append({"id": "r1", "type": rt, "from": p_ids, "to": target_id,
                      "strength": strength, "warrant": warrant})

    edges = []
    for r in rules:
        for src in r["from"]:
            edges.append({"src": src, "dst": r["to"], "via": r["id"]})

    graph = {
        "claim": final_claim,
        "claim_kind": _classify_statement(final_claim),
        "premises": nodes_premises,
        "intermediates": intermediates,
        "rules": rules,
        "edges": edges,
        "final": "claim",
        "_any_private": any_private,
    }
    return graph


# --------------------------------------------------------------------------- #
# 逻辑缺陷检测器(每个返回 list[flaw]; flaw = {type,name,severity,where,why,fix,confidence})
# --------------------------------------------------------------------------- #
def _flaw(ftype, name, severity, why, fix, where="", confidence=0.6, **extra) -> dict:
    d = {"type": ftype, "name": name, "severity": severity, "where": where,
         "why": why, "fix": fix, "confidence": round(float(confidence), 2)}
    d.update(extra)
    return d


def _detect_circular(graph: dict) -> list:
    """循环论证 / 乞题: 某前提与它要支撑的结论高度同义(等于拿结论当前提)。"""
    flaws = []
    claim = graph.get("claim", "")
    targets = [(i["id"], i["text"]) for i in graph.get("intermediates", [])]
    targets.append(("claim", claim))
    for p in graph.get("premises", []):
        for tid, ttext in targets:
            ov = _overlap(p["text"], ttext)
            if ov >= 0.72 and p["text"].strip() and ttext.strip():
                flaws.append(_flaw(
                    "circular", "循环论证/乞题", "high",
                    "前提「%s」与结论「%s」几乎是同一句话(重叠 %.0f%%), 等于用结论证明结论, "
                    "没有提供独立支撑。" % (p["text"][:40], ttext[:40], ov * 100),
                    "找一条**独立于结论**的前提(新证据/新事实), 而不是把结论换种说法。",
                    where=p["id"] + "→" + tid, confidence=min(0.95, 0.5 + ov / 2),
                    overlap=ov))
    return flaws


def _detect_gaps(graph: dict) -> list:
    """跳步 / 非逻辑跳跃: 结论引入了前提里完全没有的关键内容词(缺中间环节)。"""
    flaws = []
    prem_terms = set()
    for p in graph.get("premises", []):
        prem_terms |= _terms(p["text"])
    for inter in graph.get("intermediates", []):
        prem_terms |= _terms(inter["text"])  # 中间结论也算已建立的内容
    claim = graph.get("claim", "")
    claim_terms = _terms(claim)
    if not graph.get("premises"):
        flaws.append(_flaw(
            "gap", "无前提的断言", "high",
            "主张「%s」没有任何前提支撑, 是裸断言。" % claim[:40],
            "补上「凭什么这么说」的前提(事实/证据/公认规则)。",
            where="claim", confidence=0.9))
        return flaws
    new_terms = sorted(t for t in claim_terms if t not in prem_terms)  # sorted: 输出确定可复现
    # 只在新内容词占比偏高时判跳步(避免正常措辞差异误报)
    if claim_terms and len(new_terms) / max(1, len(claim_terms)) >= 0.6 and len(new_terms) >= 2:
        flaws.append(_flaw(
            "gap", "推理跳步(缺中间环节)", "med",
            "结论引入了前提里没出现的关键概念(%s), 中间缺一步把前提和结论接上。"
            % "、".join(new_terms[:5]),
            "补一条桥接前提, 显式说明这些新概念是怎么从已有前提推出来的。",
            where="rules", confidence=0.62, new_terms=new_terms[:8]))
    # 肯定后件 / 否定前件(形式无效)
    for r in graph.get("rules", []):
        if r["type"] != "deductive":
            continue
        joined = " ".join(p["text"] for p in graph.get("premises", [])) + " " + claim
        m = _COND_RX.search(joined)
        if m:
            groups = [g for g in m.groups() if g]
            if len(groups) >= 2:
                antecedent, consequent = groups[0], groups[1]
                # 肯定后件: 前提里出现 consequent, 结论是 antecedent
                if _overlap(consequent, " ".join(p["text"] for p in graph["premises"])) > 0.3 \
                        and _overlap(antecedent, claim) > 0.3:
                    flaws.append(_flaw(
                        "gap", "肯定后件(形式无效)", "high",
                        "形如「如果P则Q; 观察到Q; 所以P」—— Q 可能由别的原因造成, 不能反推 P。",
                        "要么补「只有P才Q」(双条件), 要么排除其它能导致 Q 的原因。",
                        where="rules", confidence=0.55))
    return flaws


# 常见泛义名词: 它们在前提/结论里几乎总指同一类东西, 不该被当成偷换概念(防误报)。
_COMMON_NOUNS = frozenset({
    "文件", "公司", "问题", "东西", "时间", "数据", "客户", "工作", "事情", "情况",
    "方面", "方式", "方法", "结果", "原因", "内容", "项目", "产品", "服务", "用户",
    "系统", "团队", "市场", "人员", "成本", "价格", "功能", "需求", "目标",
})
_EQUIV_MARKER_RX = re.compile(
    r"此.{1,4}非彼|偷换概念|偷换|玩文字游戏|两种(意思|含义|概念)|不是一个(意思|概念|东西)")


def _compound(text: str, term: str) -> str:
    """term 在 text 里所属的复合词(向两侧各扩至多 2 个汉字, 到非汉字为止)。粗略分词。"""
    t = text or ""
    idx = t.find(term)
    if idx == -1:
        return ""
    a, b = idx, idx + len(term)
    while a - 1 >= 0 and re.match(r"[一-鿿]", t[a - 1]) and (idx - (a - 1)) <= 2:
        a -= 1
    while b < len(t) and re.match(r"[一-鿿]", t[b]) and (b - (idx + len(term))) <= 2:
        b += 1
    return t[a:b]


def _detect_equivocation(graph: dict) -> list:
    """偷换概念: ①文本里有显式偷换标记; ②同一关键词在前提/结论里组成**不同的复合词**
    (如「言论自由」vs「自由市场」)→ 疑似换了含义。保守判定(避开泛义名词), 标低置信。"""
    flaws = []
    claim = graph.get("claim", "")
    # ① 显式标记(高确定性)
    for p in list(graph.get("premises", [])) + [{"text": claim, "id": "claim"}]:
        if _EQUIV_MARKER_RX.search(p.get("text", "") or ""):
            flaws.append(_flaw(
                "equivocation", "偷换概念(显式)", "med",
                "文本里出现「此X非彼X / 偷换 / 两种意思」一类提示, 同一个词被当作不同含义在用。",
                "把这个词的两种含义分开命名, 各自单独论证, 别在论证里悄悄切换。",
                where=p.get("id", ""), confidence=0.7))
            return flaws
    # ② 复合词漂移(保守: 跳过泛义名词, 两侧复合词须 ≥3 字且不同)
    claim_terms = _terms(claim)
    for p in graph.get("premises", []):
        for term in sorted(claim_terms & _terms(p["text"])):  # sorted: 确定挑选, 不随哈希漂移
            if len(term) < 2 or term in _COMMON_NOUNS:
                continue
            cp, cc = _compound(p["text"], term), _compound(claim, term)
            if cp and cc and cp != cc and len(cp) >= 3 and len(cc) >= 3:
                flaws.append(_flaw(
                    "equivocation", "疑似偷换概念", "med",
                    "关键词「%s」在前提里组成「%s」、在结论里组成「%s」, 可能前后不是同一个概念。"
                    % (term, cp, cc),
                    "确认「%s」在前提和结论里指同一件事; 含义不同则论证不成立。" % term,
                    where=p["id"] + "→claim", confidence=0.4, term=term))
                break  # 每个前提最多报一次, 防刷屏
    return flaws


def _split_cause_effect(text: str):
    """从因果主张里粗抽 (cause, effect)。抓不到 → (None, None)。"""
    t = text or ""
    for m in ("导致", "造成", "引起", "使得", "促成", "致使", "带来", "leads to", "causes", "results in"):
        idx = t.find(m)
        if idx != -1:
            cause = t[:idx].strip("，, 。:：")
            effect = t[idx + len(m):].strip("，, 。:：")
            if cause and effect:
                return cause, effect
    m = re.search(r"因为(.+?)[，,]?\s*所以(.+)", t)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _detect_causal_reversal(graph: dict) -> list:
    """因果倒置: 主张说 A 导致 B, 但世界模型里 B→A 的因果链更强。只读 world_model。"""
    flaws = []
    claim = graph.get("claim", "")
    if not _has_any(claim, _CAUSAL_MARKERS):
        return flaws
    cause, effect = _split_cause_effect(claim)
    if not cause or not effect:
        return flaws
    wm = _opt("world_model")
    if wm is None or not hasattr(wm, "causal_chain"):
        return flaws

    def _best_conf(res):
        if not isinstance(res, dict) or not res.get("found"):
            return 0.0
        best = 0.0
        for ch in (res.get("chains") or []):
            c = ch.get("confidence") if isinstance(ch, dict) else None
            try:
                best = max(best, float(c))
            except (TypeError, ValueError):
                pass
        return best if best else (0.3 if res.get("found") else 0.0)

    try:
        fwd = _best_conf(wm.causal_chain(cause, effect))
        rev = _best_conf(wm.causal_chain(effect, cause))
    except Exception:
        return flaws
    if rev > 0 and rev >= fwd + 0.1:
        flaws.append(_flaw(
            "causal_reversal", "因果倒置", "high",
            "主张说「%s 导致 %s」, 但世界模型里「%s 导致 %s」的因果证据更强(%.2f vs %.2f), "
            "方向可能反了。" % (cause[:24], effect[:24], effect[:24], cause[:24], rev, fwd),
            "重新确认因果方向; 也许是结果被当成了原因, 或两者另有共同原因。",
            where="claim", confidence=min(0.9, 0.5 + (rev - fwd)),
            forward_conf=round(fwd, 2), reverse_conf=round(rev, 2)))
    return flaws


def _detect_corr_not_cause(graph: dict) -> list:
    """相关不等于因果: 因果主张只有共现/相关证据, 缺机制。结合 world_model 的 precedes 边。"""
    flaws = []
    claim = graph.get("claim", "")
    is_causal_claim = _has_any(claim, _CAUSAL_MARKERS)
    # 看支撑前提里有没有"只是相关/共现"的措辞而无机制
    prem_join = " ".join(p["text"] for p in graph.get("premises", []))
    corr = _has_any(prem_join, _CORRELATION_MARKERS) or _has_any(claim, _CORRELATION_MARKERS)
    mech = _has_any(prem_join + " " + claim, _MECHANISM_MARKERS)
    if is_causal_claim and corr and not mech:
        flaws.append(_flaw(
            "corr_not_cause", "相关当成因果", "high",
            "这是个因果主张, 但支撑只有「相关/同时出现」之类的共现证据, 没有说清作用机制 —— "
            "相关不等于因果(可能有第三方共同原因, 或纯属巧合)。",
            "补一条因果机制(A 通过什么过程影响 B), 或做对照/干预实验排除共因。",
            where="rules", confidence=0.7))
        return flaws
    # 即便措辞没露馅, 也查 world_model: 只有 precedes(弱因果候选) 而无 causes 边
    if is_causal_claim and not mech:
        cause, effect = _split_cause_effect(claim)
        wm = _opt("world_model")
        if cause and effect and wm is not None and hasattr(wm, "relationship"):
            try:
                rel = wm.relationship(cause, effect)
                rtypes = set()
                if isinstance(rel, dict):
                    for d in (rel.get("direct") or []):
                        if isinstance(d, dict) and d.get("rtype"):
                            rtypes.add(d["rtype"])
                if "precedes" in rtypes and "causes" not in rtypes:
                    flaws.append(_flaw(
                        "corr_not_cause", "时序≠因果", "med",
                        "世界模型里「%s」只是先于「%s」发生(precedes, 弱因果候选), 并无确证的因果边 "
                        "(causes); 先后顺序不能直接当因果。" % (cause[:20], effect[:20]),
                        "找机制或对照证据把「先后」升级成「因果」, 否则结论降级为相关。",
                        where="claim", confidence=0.6))
            except Exception:
                pass
    return flaws


def _detect_unproven_premises(graph: dict) -> list:
    """未证明前提: 状态 unproven 且无支撑证据的前提(尤其承重的)逐条标出。"""
    flaws = []
    load_bearing_ids = set()
    for r in graph.get("rules", []):
        if r.get("to") == "claim":
            for src in r.get("from", []):
                load_bearing_ids.add(src)
    for p in graph.get("premises", []):
        if p["status"] == "unproven" and not p["support"]:
            lb = p["id"] in load_bearing_ids
            flaws.append(_flaw(
                "unproven_premise", "未证明的前提", "high" if lb else "med",
                "前提「%s」目前是未经证明的断言(图谱/世界模型里查不到支撑)%s。"
                % (p["text"][:40], "; 而且它直接支撑最终主张(承重)" if lb else ""),
                "给这条前提找证据/出处; 查不到就把它降级为「假设」并说明结论依赖此假设。",
                where=p["id"], confidence=0.65, load_bearing=lb))
    return flaws


def _detect_value_fact(graph: dict) -> list:
    """价值判断混入事实判断: ①规范结论却全是事实前提(是→应该跳跃); ②价值句被当事实前提。"""
    flaws = []
    claim_kind = graph.get("claim_kind", "fact")
    premises = graph.get("premises", [])
    kinds = [p["kind"] for p in premises]
    # ① is-ought(休谟之叉): 仅对**应然/规范**结论(应该/必须/should/ought)判致命 —— 经验性的
    #    评价/比较句(value)不套用「是→应该」的致命标签, 只给一条较弱的「缺价值标准」提示,
    #    避免把「这个方案更好」这类可经验论证的评价误判成休谟之叉。
    no_value_premise = not any(k in ("value", "normative", "mixed") for k in kinds)
    if claim_kind == "normative" and premises and no_value_premise:
        flaws.append(_flaw(
            "value_fact", "是→应该的跳跃(休谟之叉)", "high",
            "结论是应然判断「%s」, 但前提全是事实陈述 —— 光靠「是什么」推不出「应该怎样」, "
            "中间缺一条价值前提(你到底看重什么)。" % graph.get("claim", "")[:40],
            "显式补上价值前提(如「我们更看重 X」), 让「应该」有价值依据, 而不是偷偷夹带。",
            where="claim", confidence=0.7))
    elif claim_kind == "value" and premises and no_value_premise:
        flaws.append(_flaw(
            "value_fact", "价值结论缺价值标准", "med",
            "结论是价值评价「%s」, 但前提全是事实 —— 评价需要一条标准(你按什么尺度说它好/值得), "
            "否则「好/值得」缺依据。" % graph.get("claim", "")[:40],
            "补上评价标准(价值前提), 让评价有尺度; 若标准是公认的, 也最好说清。",
            where="claim", confidence=0.55))
    # ② 价值句被当作事实前提陈述
    for p in premises:
        if p["kind"] in ("value", "normative") and p["status"] != "assumption":
            # 它被当成既定前提使用, 但其实是个有争议的价值主张
            flaws.append(_flaw(
                "value_fact", "价值判断被当事实前提", "med",
                "前提「%s」其实是价值判断(有争议、因人而异), 却被当成既定事实在用。"
                % p["text"][:40],
                "把它标成「价值立场」并说明它是否为各方共识; 不是共识就得先单独论证。",
                where=p["id"], confidence=0.5))
    return flaws


def _scan_catalogue(text: str) -> list:
    """扫文本可检测的修辞型谬误目录。"""
    flaws = []
    t = text or ""
    if not t.strip():
        return flaws
    for fid, zh, en, sev, rx, why, fix in _FALLACY_CATALOGUE:
        try:
            m = re.search(rx, t, re.IGNORECASE)
        except re.error:
            m = None
        if m:
            span = m.group(0)
            flaws.append(_flaw(
                fid, zh, sev, why, fix, where="text",
                confidence=0.55, en=en, matched=span[:40]))
    return flaws


def _all_structural_flaws(graph: dict) -> list:
    """跑全部结构型检测器(循环/跳步/偷换/因果倒置/相关非因果/未证明/价值混事实)。"""
    flaws = []
    flaws += _detect_circular(graph)
    flaws += _detect_gaps(graph)
    flaws += _detect_equivocation(graph)
    flaws += _detect_causal_reversal(graph)
    flaws += _detect_corr_not_cause(graph)
    flaws += _detect_unproven_premises(graph)
    flaws += _detect_value_fact(graph)
    return flaws


def _graph_text(graph: dict) -> str:
    """论证图里的全部正文拼一起(给目录谬误扫描用)。"""
    parts = [graph.get("claim", "")]
    parts += [p["text"] for p in graph.get("premises", [])]
    parts += [i["text"] for i in graph.get("intermediates", [])]
    return " ".join([p for p in parts if p])


# --------------------------------------------------------------------------- #
# 有效性判定 / 最小证明 / 反驳 / 强化 / 解释(核心算法)
# --------------------------------------------------------------------------- #
_SEV_WEIGHT = {"high": 1.0, "med": 0.5, "low": 0.2}


def _verdict(graph: dict, flaws: list) -> dict:
    """综合论证图 + 缺陷给一个有效性 verdict。"""
    has_premises = bool(graph.get("premises"))
    fatal = [f for f in flaws if f["severity"] == "high"]
    # 形式有效性: 演绎规则且无致命跳步/循环 → valid; 否则按强度给或然支持
    rule_types = {r["type"] for r in graph.get("rules", [])}
    deductive = "deductive" in rule_types
    if not has_premises:
        label = "unsupported"
        note = "裸断言, 无前提支撑。"
    elif fatal:
        label = "invalid" if deductive else "weak"
        note = "存在致命缺陷(%s), 推理链断裂。" % "、".join(sorted({f["name"] for f in fatal}))
    elif deductive:
        label = "valid"
        note = "演绎结构, 未见致命缺陷; 但「有效」≠「可靠」, 还需前提全为真。"
    else:
        # 或然支持: 看规则强度均值与缺陷扣分
        strengths = [r.get("strength", 0.45) for r in graph.get("rules", [])]
        base = sum(strengths) / len(strengths) if strengths else 0.45
        penalty = sum(_SEV_WEIGHT.get(f["severity"], 0.2) for f in flaws) * 0.15
        score = max(0.0, base - penalty)
        if score >= 0.6:
            label, note = "strong", "或然支持较强, 但非必然。"
        elif score >= 0.35:
            label, note = "moderate", "或然支持中等, 有可改进处。"
        else:
            label, note = "weak", "或然支持弱, 前提撑不起结论。"
    n_unproven = sum(1 for p in graph.get("premises", []) if p["status"] == "unproven")
    sound = (label in ("valid", "strong")) and n_unproven == 0
    return {
        "validity": label,
        "sound": sound,            # 可靠 = 有效/强 且 前提都已证明
        "deductive": deductive,
        "n_flaws": len(flaws),
        "n_fatal": len(fatal),
        "n_unproven_premises": n_unproven,
        "note": note,
    }


_DIGRESSION_RX = re.compile(
    r"顺便|题外话|另外(提|说)|对了[，,]?|扯远|不相干|无关(紧要|的)|插一句|多说一句|跑题")


def _minimal_proof(graph: dict) -> dict:
    """最小证明: 找出真正承重的前提(直接/间接支撑最终主张的), 其余标为冗余。

    冗余判定走**显式离题标记**(顺便/题外话/对了…)—— 保守、低误报; 纯靠语义判相关性不可靠,
    故只摘出明确离题的装饰性前提, 其余按图可达视为承重。
    """
    # 反向可达: 从 claim 沿 edges 反推哪些前提可达
    reachable = set()
    frontier = {"claim"}
    edges = graph.get("edges", [])
    # 多轮反向传播(图很小)
    for _ in range(len(edges) + 2):
        new = set()
        for e in edges:
            if e["dst"] in frontier or e["dst"] in reachable:
                new.add(e["src"])
        if new <= (reachable | frontier):
            break
        reachable |= new
        frontier = new

    def _digression(p):
        return bool(_DIGRESSION_RX.search(p.get("text", "") or ""))

    load_bearing = [p for p in graph.get("premises", [])
                    if p["id"] in reachable and not _digression(p)]
    redundant = [p for p in graph.get("premises", [])
                 if p["id"] not in reachable or _digression(p)]
    skeleton = []
    for r in graph.get("rules", []):
        skeleton.append({"rule": r["id"], "type": r["type"],
                         "from": r["from"], "to": r["to"], "warrant": r["warrant"]})
    return {
        "load_bearing_premises": [{"id": p["id"], "text": p["text"],
                                   "status": p["status"]} for p in load_bearing],
        "redundant_premises": [{"id": p["id"], "text": p["text"]} for p in redundant],
        "proof_skeleton": skeleton,
        "note": "最小证明 = 去掉冗余前提后仍能支撑结论的前提集 + 推理骨架; "
                "承重前提里若有 unproven, 那就是整条论证最脆弱处。",
    }


def _counterargument(graph: dict, flaws: list) -> dict:
    """造最强反驳: 攻击最弱前提 + 攻击推理缺陷 + 摆反对证据 + steelman 对立面。"""
    attacks = []
    # 1) 攻击未证明/承重前提
    mp = _minimal_proof(graph)
    lb_ids = {p["id"] for p in mp["load_bearing_premises"]}
    for p in graph.get("premises", []):
        if p["id"] in lb_ids and (p["status"] == "unproven" or p["contradict"]):
            attacks.append({
                "target": p["id"], "kind": "attack_premise",
                "text": "前提「%s」并不牢靠%s, 它一倒, 结论就失去支撑。" % (
                    p["text"][:40],
                    "(已有反对证据: %s)" % "、".join(p["contradict"][:2]) if p["contradict"]
                    else "(尚无证据)"),
            })
    # 2) 把每个高/中危缺陷转成一击
    for f in flaws:
        if f["severity"] in ("high", "med"):
            attacks.append({"target": f.get("where", ""), "kind": "attack_inference",
                            "text": "%s: %s" % (f["name"], f["why"])})
    # 3) 反对证据(world_model contradicts)
    rebut = []
    for p in graph.get("premises", []):
        rebut += p.get("contradict", [])
    # 4) steelman: 对立主张
    claim = graph.get("claim", "")
    steelman = "对立面最强版本: 即便承认部分前提, 也可主张「并非%s」—— 因为上面这些薄弱点足以" \
               "支持相反结论或「证据不足以下此结论」。" % (claim[:40] or "该结论")
    return {
        "attacks": attacks[:8],
        "rebutting_evidence": list(dict.fromkeys(rebut))[:5],
        "steelman_opposite": steelman,
        "note": "反驳优先打承重且未证明的前提, 其次打推理缺陷; 没有可攻击点时说明论证相当稳固。",
    }


def _strengthen(graph: dict, flaws: list) -> dict:
    """强化建议: 把每个缺陷转成「怎么补」+ 给未证明前提找证据 + 给结论加限定。"""
    actions = []
    for f in flaws:
        actions.append({"addresses": f["name"], "severity": f["severity"], "do": f["fix"],
                        "where": f.get("where", "")})
    # 未证明前提 → 补证据
    for p in graph.get("premises", []):
        if p["status"] == "unproven" and not p["support"]:
            actions.append({"addresses": "未证明前提", "severity": "med",
                            "do": "给前提「%s」补证据/出处, 或降级为显式假设。" % p["text"][:36],
                            "where": p["id"]})
    # 结论过强(全称/必然词)→ 建议加限定
    claim = graph.get("claim", "")
    if _has_any(claim, ("所有", "一定", "必然", "肯定", "永远", "绝对", "everyone", "always", "never")):
        actions.append({"addresses": "结论过强", "severity": "low",
                        "do": "把全称/绝对措辞(所有/一定/必然)改成限定(多数/在某条件下/很可能), "
                              "降低被一个反例推翻的风险。", "where": "claim"})
    # 预防反驳
    actions.append({"addresses": "稳健性", "severity": "low",
                    "do": "主动列出最强反驳并先回应(预防性反驳), 论证更难被推翻。",
                    "where": "argument"})
    # 去重
    seen = set()
    uniq = []
    for a in actions:
        key = (a["addresses"], a["where"], a["do"][:20])
        if key not in seen:
            seen.add(key)
            uniq.append(a)
    return {"improvements": uniq[:10],
            "note": "按 severity 从高到低做; 先把承重的未证明前提补实, 再补桥接前提堵跳步。"}


def _explain_logic(graph: dict, flaws: list, verdict: dict) -> dict:
    """人话讲清推理链为什么成立/不成立: 有序链 + 关键假设 + verdict + 置信。"""
    chain = []
    if graph.get("premises"):
        chain.append("前提: " + "; ".join(
            "[%s|%s] %s" % (p["id"], p["kind"], p["text"][:48]) for p in graph["premises"]))
    for r in graph.get("rules", []):
        froms = "+".join(r["from"])
        chain.append("规则[%s, %s]: 由 %s → %s (%s; 强度%.2f)" % (
            r["id"], r["type"], froms, r["to"], r["warrant"], r.get("strength", 0.0)))
    for inter in graph.get("intermediates", []):
        chain.append("中间结论[%s]: %s" % (inter["id"], inter["text"][:48]))
    chain.append("最终主张[%s|%s]: %s" % (
        "claim", graph.get("claim_kind", ""), graph.get("claim", "")[:60]))

    assumptions = [p["text"] for p in graph.get("premises", [])
                   if p["kind"] == "assumption" or p["status"] == "unproven"]
    # 一句话判词
    label = verdict["validity"]
    if label in ("valid", "strong"):
        headline = "这条推理基本站得住" + ("(且前提都已证明, 可靠)" if verdict["sound"]
                                          else "(形式/或然上成立, 但还有未证明的前提)")
    elif label in ("moderate",):
        headline = "这条推理勉强成立, 有可改进处"
    elif label == "unsupported":
        headline = "这是裸断言, 谈不上证明"
    else:
        headline = "这条推理站不住" + ("(有致命缺陷)" if verdict["n_fatal"] else "(支撑太弱)")
    # 置信: metacognition_core(deferred) 优先, 缺则按 verdict 估
    conf = _meta_confidence(graph, verdict)
    return {
        "headline": headline,
        "chain": chain,
        "verdict": verdict,
        "key_assumptions": assumptions[:6],
        "top_flaws": [{"name": f["name"], "severity": f["severity"], "why": f["why"]}
                      for f in sorted(flaws, key=lambda x: -_SEV_WEIGHT.get(x["severity"], 0))[:4]],
        "confidence": conf,
        "note": "「所以应该这样」要立得住, 需要: ①前提都为真且已证明 ②推理规则有效 "
                "③无循环/跳步/偷换/因果误用 ④价值结论有价值前提。上面逐项给了体检。",
    }


def _meta_confidence(graph: dict, verdict: dict) -> dict:
    """置信/不确定性: 优先 metacognition_core(deferred), 缺则按 verdict 启发式。"""
    mc = _opt("metacognition_core")
    if mc is not None and hasattr(mc, "assess"):
        try:
            r = mc.assess({"graph": graph, "verdict": verdict})
            if isinstance(r, dict) and "confidence" in r:
                return {"value": r.get("confidence"), "via": "metacognition_core",
                        "uncertainty": r.get("uncertainty")}
        except Exception:
            pass
    base = {"valid": 0.85, "strong": 0.75, "moderate": 0.55, "weak": 0.3,
            "invalid": 0.15, "unsupported": 0.1}.get(verdict["validity"], 0.4)
    base -= 0.08 * verdict.get("n_fatal", 0)
    base -= 0.03 * verdict.get("n_unproven_premises", 0)
    base = max(0.05, min(0.95, base))
    return {"value": round(base, 2), "via": "heuristic",
            "uncertainty": "高" if base < 0.4 else ("中" if base < 0.7 else "低")}


# --------------------------------------------------------------------------- #
# DB(纯 stdlib sqlite; 自有库, 不碰主库)
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ap_arguments (
    argument_id TEXT PRIMARY KEY,
    ts          TEXT,
    session_id  TEXT,
    claim       TEXT,
    claim_kind  TEXT,
    graph       TEXT,
    n_premises  INTEGER,
    n_rules     INTEGER,
    validity    TEXT,
    n_flaws     INTEGER,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ap_args_ts ON ap_arguments(ts);
CREATE INDEX IF NOT EXISTS ix_ap_args_session ON ap_arguments(session_id);
CREATE TABLE IF NOT EXISTS ap_handoffs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT,
    argument_id         TEXT,
    to_target           TEXT,
    action              TEXT,
    bundle              TEXT,
    note                TEXT,
    queued_approval_id  TEXT
);
CREATE INDEX IF NOT EXISTS ix_ap_handoffs_arg ON ap_handoffs(argument_id);
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


def _store_graph(graph: dict, claim: str, session_id: str, verdict: dict,
                 n_flaws: int, db_path: str) -> str:
    """落库一个论证图(敏感正文已脱敏); 返回 argument_id。"""
    aid = _arg_id(claim or "arg")
    private = 1 if graph.get("_any_private") else 0
    # 脱敏副本入库(结构留, 敏感正文抹; 接地引入的 support/contradict 也一并脱敏)
    store_graph = json.loads(json.dumps(graph, ensure_ascii=False))
    store_graph["claim"] = _scrub_for_store(store_graph.get("claim", ""))
    for p in store_graph.get("premises", []):
        if p.get("private"):
            # 私密前提 → 只留结构, 正文与接地证据一律不落库(与 shared_context「私密只留元数据」一致)。
            p["text"] = "（私密内容已隐去, 仅留论证结构）"
            p["support"] = []
            p["contradict"] = []
        else:
            p["text"] = _scrub_for_store(p.get("text", ""))
            p["support"] = [_scrub_for_store(s) for s in (p.get("support") or [])]
            p["contradict"] = [_scrub_for_store(s) for s in (p.get("contradict") or [])]
    for i in store_graph.get("intermediates", []):
        i["text"] = _scrub_for_store(i.get("text", ""))
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO ap_arguments "
            "(argument_id, ts, session_id, claim, claim_kind, graph, n_premises, "
            " n_rules, validity, n_flaws, private) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(argument_id) DO UPDATE SET "
            "  ts=excluded.ts, session_id=excluded.session_id, claim=excluded.claim, "
            "  claim_kind=excluded.claim_kind, graph=excluded.graph, "
            "  n_premises=excluded.n_premises, n_rules=excluded.n_rules, "
            "  validity=excluded.validity, n_flaws=excluded.n_flaws, "
            "  private=MAX(private, excluded.private)",
            (aid, _now(), session_id, _safe_text(_scrub_for_store(claim)),
             graph.get("claim_kind", ""),
             _safe_text(json.dumps(store_graph, ensure_ascii=False)),
             len(graph.get("premises", [])), len(graph.get("rules", [])),
             verdict.get("validity", ""), n_flaws, private))
        conn.commit()
    finally:
        conn.close()
    return aid


def _get_argument(argument_id: str, db_path: str) -> dict | None:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT * FROM ap_arguments WHERE argument_id=?",
                         (argument_id,)).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    d = dict(r)
    d["graph"] = _loads(d.get("graph"), {})
    return d


def _load_or_build(argument_id: str, claim: str, text: str, premises_in,
                   db_path: str, ground: bool = True):
    """分析动作的统一取图: 给 argument_id → 读库; 给 claim/text → 现场构造(不落库)。
    返回 (graph, any_private, source) 或 (None, False, error_msg)。"""
    argument_id = "" if argument_id is None else str(argument_id)
    if argument_id:
        row = _get_argument(argument_id, db_path)
        if not row:
            return None, False, "没找到论证 " + argument_id
        g = row.get("graph") or {}
        return g, bool(row.get("private")), "stored"
    if not (claim or text or premises_in):
        return None, False, "请给 argument_id, 或给 claim/text 让我现场构造论证图。"
    g = _build_graph(claim, text, premises_in, ground=ground)
    return g, bool(g.get("_any_private")), "transient"


# --------------------------------------------------------------------------- #
# 1. build_argument — 把主张展开成论证图(写自有库)
# --------------------------------------------------------------------------- #
def build_argument(claim: str = "", text: str = "", premises=None,
                   session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    if not (claim or text or premises):
        return {"ok": False, "error": "给我一个主张(claim), 可选附支撑文本(text)或前提列表(premises)。",
                "hint": "jarvis argument_prover build_argument --claim \"...\" --text \"因为…所以…\""}
    graph = _build_graph(claim, text, premises, ground=True)
    flaws = _all_structural_flaws(graph) + _scan_catalogue(_graph_text(graph))
    verdict = _verdict(graph, flaws)
    aid = _store_graph(graph, graph.get("claim", claim), session_id, verdict, len(flaws), db)
    any_private = bool(graph.get("_any_private"))
    _audit("build_argument", aid, n_premises=len(graph["premises"]),
           n_flaws=len(flaws), validity=verdict["validity"], private=any_private)
    out = {
        "ok": True, "argument_id": aid, "claim": graph["claim"],
        "claim_kind": graph["claim_kind"],
        "n_premises": len(graph["premises"]), "n_rules": len(graph["rules"]),
        "n_intermediates": len(graph["intermediates"]),
        "verdict": verdict,
        "flaws": [{"name": f["name"], "severity": f["severity"], "where": f.get("where", "")}
                  for f in flaws],
        "deferred": _deferred_status(),
        "note": "论证图已构造并入库。用 inference_chain/check_validity/find_gaps/fallacy_scan/"
                "minimal_proof/counterargument/strengthen/explain_logic 接着分析(传 argument_id)。",
    }
    return _flag_private(out, any_private)


# --------------------------------------------------------------------------- #
# 2. premises — 列出(并分类)前提, 含隐含假设
# --------------------------------------------------------------------------- #
def premises(argument_id: str = "", claim: str = "", text: str = "",
             premises_in=None, session_id: str = "default", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    items = [{"id": p["id"], "text": p["text"], "kind": p["kind"], "status": p["status"],
              "support": p["support"], "contradict": p["contradict"], "private": p["private"]}
            for p in g.get("premises", [])]
    # 隐含假设: 结论有新概念但无前提承接 → 提示存在未明说的前提
    hidden = []
    gaps = _detect_gaps(g)
    for f in gaps:
        if f["type"] == "gap" and f.get("new_terms"):
            hidden.append("可能存在隐含前提, 把这些概念接上: " + "、".join(f["new_terms"][:5]))
    out = {"ok": True, "argument_id": argument_id or None, "source": src,
           "claim": g.get("claim", ""), "premises": items,
           "n_unproven": sum(1 for p in items if p["status"] == "unproven"),
           "hidden_assumptions": hidden,
           "note": "kind=事实/价值/规范/假设/混合; status=grounded(图谱/世界模型有支撑)/unproven。"}
    _audit("premises", argument_id, n=len(items))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 3. inference_chain — 摊开推理链(前提→规则→中间结论→最终主张)
# --------------------------------------------------------------------------- #
def inference_chain(argument_id: str = "", claim: str = "", text: str = "",
                    premises_in=None, session_id: str = "default",
                    db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    out = {
        "ok": True, "argument_id": argument_id or None, "source": src,
        "claim": g.get("claim", ""), "claim_kind": g.get("claim_kind", ""),
        "premises": [{"id": p["id"], "text": p["text"], "kind": p["kind"],
                      "status": p["status"]} for p in g.get("premises", [])],
        "rules": g.get("rules", []),
        "intermediates": g.get("intermediates", []),
        "edges": g.get("edges", []),
        "note": "读法: 前提经规则推出中间结论, 再推到最终主张; 规则 strength<1 表示或然(非必然)。",
    }
    _audit("inference_chain", argument_id, n_rules=len(g.get("rules", [])))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 4. check_validity — 检验有效性(结论是否从前提+规则推得出)
# --------------------------------------------------------------------------- #
def check_validity(argument_id: str = "", claim: str = "", text: str = "",
                   premises_in=None, session_id: str = "default",
                   db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    flaws = _all_structural_flaws(g) + _scan_catalogue(_graph_text(g))
    verdict = _verdict(g, flaws)
    out = {
        "ok": True, "argument_id": argument_id or None, "source": src,
        "claim": g.get("claim", ""),
        "verdict": verdict,
        "per_rule": [{"rule": r["id"], "type": r["type"], "strength": r.get("strength"),
                      "warrant": r["warrant"]} for r in g.get("rules", [])],
        "blocking_flaws": [{"name": f["name"], "severity": f["severity"], "why": f["why"]}
                           for f in flaws if f["severity"] == "high"],
        "note": "valid/invalid 看形式; strong/moderate/weak 看或然支持; sound(可靠)=有效且前提全真。"
                " 「有效」只保证『若前提为真则结论真』, 不保证前提真。",
    }
    _audit("check_validity", argument_id, validity=verdict["validity"], n_fatal=verdict["n_fatal"])
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 5. find_gaps — 找跳步/缺中间环节/隐含前提
# --------------------------------------------------------------------------- #
def find_gaps(argument_id: str = "", claim: str = "", text: str = "",
              premises_in=None, session_id: str = "default",
              db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    gaps = _detect_gaps(g)
    unproven = _detect_unproven_premises(g)
    out = {
        "ok": True, "argument_id": argument_id or None, "source": src,
        "claim": g.get("claim", ""),
        "gaps": gaps,
        "unproven_premises": unproven,
        "n_gaps": len(gaps), "n_unproven": len(unproven),
        "note": "gap=前提到结论缺一步(引入新概念/形式无效); unproven=前提本身没被证明。"
                " 把这些缺口补上, 推理链才闭合。",
    }
    _audit("find_gaps", argument_id, n_gaps=len(gaps), n_unproven=len(unproven))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 6. fallacy_scan — 谬误扫描(结构型 + 修辞型目录)
# --------------------------------------------------------------------------- #
def fallacy_scan(argument_id: str = "", claim: str = "", text: str = "",
                 premises_in=None, session_id: str = "default",
                 db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    structural = _all_structural_flaws(g)
    rhetorical = _scan_catalogue(_graph_text(g) + " " + (text or ""))
    allf = structural + rhetorical
    by_sev = {"high": [], "med": [], "low": []}
    for f in allf:
        by_sev.setdefault(f["severity"], []).append(f["name"])
    out = {
        "ok": True, "argument_id": argument_id or None, "source": src,
        "claim": g.get("claim", ""),
        "fallacies": allf,
        "count": len(allf),
        "by_severity": {k: v for k, v in by_sev.items() if v},
        "checks_run": ["循环论证", "推理跳步", "偷换概念", "因果倒置", "相关非因果",
                       "未证明前提", "价值混入事实", "+ %d 条修辞谬误目录" % len(_FALLACY_CATALOGUE)],
        "note": "structural(图结构)+ rhetorical(措辞)双扫; confidence<0.5 的为启发式提示, 需人工复核。",
    }
    _audit("fallacy_scan", argument_id, n=len(allf))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 7. minimal_proof — 最小证明(承重前提 + 推理骨架)
# --------------------------------------------------------------------------- #
def minimal_proof(argument_id: str = "", claim: str = "", text: str = "",
                  premises_in=None, session_id: str = "default",
                  db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    mp = _minimal_proof(g)
    out = {"ok": True, "argument_id": argument_id or None, "source": src,
           "claim": g.get("claim", "")}
    out.update(mp)
    _audit("minimal_proof", argument_id,
           load_bearing=len(mp["load_bearing_premises"]),
           redundant=len(mp["redundant_premises"]))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 8. counterargument — 最强反驳
# --------------------------------------------------------------------------- #
def counterargument(argument_id: str = "", claim: str = "", text: str = "",
                    premises_in=None, session_id: str = "default",
                    db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    flaws = _all_structural_flaws(g) + _scan_catalogue(_graph_text(g))
    ca = _counterargument(g, flaws)
    out = {"ok": True, "argument_id": argument_id or None, "source": src,
           "claim": g.get("claim", "")}
    out.update(ca)
    _audit("counterargument", argument_id, n_attacks=len(ca["attacks"]))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 9. strengthen — 怎么把论证做得更强
# --------------------------------------------------------------------------- #
def strengthen(argument_id: str = "", claim: str = "", text: str = "",
               premises_in=None, session_id: str = "default",
               db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    flaws = _all_structural_flaws(g) + _scan_catalogue(_graph_text(g))
    sg = _strengthen(g, flaws)
    out = {"ok": True, "argument_id": argument_id or None, "source": src,
           "claim": g.get("claim", "")}
    out.update(sg)
    _audit("strengthen", argument_id, n=len(sg["improvements"]))
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 10. explain_logic — 人话讲清推理链为什么成立/不成立(小美的「所以应该这样」证明)
# --------------------------------------------------------------------------- #
def explain_logic(argument_id: str = "", claim: str = "", text: str = "",
                  premises_in=None, session_id: str = "default",
                  db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    g, any_priv, src = _load_or_build(argument_id, claim, text, premises_in, db)
    if g is None:
        return {"ok": False, "error": src}
    flaws = _all_structural_flaws(g) + _scan_catalogue(_graph_text(g))
    verdict = _verdict(g, flaws)
    expl = _explain_logic(g, flaws, verdict)
    out = {"ok": True, "argument_id": argument_id or None, "source": src,
           "claim": g.get("claim", ""), "claim_kind": g.get("claim_kind", "")}
    out.update(expl)
    _audit("explain_logic", argument_id, validity=verdict["validity"])
    return _flag_private(out, any_priv)


# --------------------------------------------------------------------------- #
# 11. handoff — 把证明好的论证打包交接(落地动作必经审批闸; 绝不执行)
# --------------------------------------------------------------------------- #
def handoff(argument_id: str = "", to: str = "", action: str = "", note: str = "",
            db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    argument_id = "" if argument_id is None else str(argument_id)
    action = str(action if action is not None else "")
    row = _get_argument(argument_id, db) if argument_id else None
    if argument_id and not row:
        return {"ok": False, "error": "没找到论证 " + argument_id + "; 先 build_argument。"}
    g = (row or {}).get("graph") or {}
    bundle = {
        "argument_id": argument_id or None,
        "claim": g.get("claim"),
        "claim_kind": g.get("claim_kind"),
        "validity": (row or {}).get("validity"),
        "n_premises": (row or {}).get("n_premises"),
        "n_flaws": (row or {}).get("n_flaws"),
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
                    payload={"argument_id": argument_id, "bundle": bundle, "red_line": red},
                    risk="high",
                    why="argument_prover.handoff 要落地真动作: " + action[:120])
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
            "INSERT INTO ap_handoffs (ts, argument_id, to_target, action, bundle, note, "
            "queued_approval_id) VALUES (?,?,?,?,?,?,?)",
            (_now(), argument_id or "", bundle["to"], action or "(memo)",
             json.dumps(bundle, ensure_ascii=False), note, queued_id))
        conn.commit()
        hid = cur.lastrowid
    finally:
        conn.close()
    _audit("handoff", argument_id, id=hid, has_action=bool(action),
           queued_approval_id=queued_id, red_line=bool(red))
    out = {"ok": True, "id": hid, "kind": "handoff", "argument_id": argument_id or None,
           "to": bundle["to"], "bundle": bundle, "note": note,
           "queued_approval_id": queued_id, "needs_confirm": needs_confirm, "red_line": red,
           "note2": ("纯交接备忘, 无外部副作用。" if not action
                     else "要落地的动作已入 autopilot 审批队列(risk=high), 绝不自动执行; "
                          "须 User 经 `jarvis autopilot approve <id>` 批准。")}
    return _flag_private(out, any_private)


# --------------------------------------------------------------------------- #
# 12. audit — 隐私/安全自审(只读)
# --------------------------------------------------------------------------- #
def audit(argument_id: str = "", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    argument_id = "" if argument_id is None else str(argument_id)
    conn = _conn(db)
    try:
        n_args = conn.execute("SELECT COUNT(*) c FROM ap_arguments").fetchone()["c"]
        n_priv = conn.execute("SELECT COUNT(*) c FROM ap_arguments WHERE private=1").fetchone()["c"]
        n_hand = conn.execute("SELECT COUNT(*) c FROM ap_handoffs").fetchone()["c"]
        # 自验: 任何入库的论证图里都不该出现凭据文件路径(明文)
        leaked = 0
        for r in conn.execute("SELECT graph FROM ap_arguments LIMIT 200").fetchall():
            g = _loads(r["graph"], {})
            blob = json.dumps(g, ensure_ascii=False)
            if _is_secret_path(blob):
                leaked += 1
    finally:
        conn.close()
    tail = []
    try:
        with open(_audit_path(), "r", encoding="utf-8") as f:
            for line in f.readlines()[-20:]:
                rec = _loads(line, None)
                if rec:
                    tail.append({k: rec.get(k) for k in ("ts", "action", "argument_id")})
    except OSError:
        pass
    out = {"ok": True,
           "storage": {"dir": _data_dir(), "db": db},
           "counts": {"arguments": n_args, "private_arguments": n_priv, "handoffs": n_hand},
           "credential_paths_stored": leaked,   # 期望恒 0
           "deferred": _deferred_status(),
           "guarantees": [
               "全本机落盘, 不外发", "凭据/密钥文件路径与正文绝不存(硬拦)",
               "私密论证只留结构, 敏感正文脱敏后才落库", "对外/落地动作经 autopilot 审批队列",
               "从不自传 confirmed=True", "只读调 mind_graph/world_model, 绝不改图谱/世界模型",
               "红线分级复用 local_powers 单一真相源"],
           "recent_audit": tail}
    if leaked > 0:
        out["WARN"] = "检测到疑似凭据路径入库(应为 0)→ 立即排查 _scrub_for_store 隐私门"
    if n_priv > 0:
        out["_private"] = True
    return out


# --------------------------------------------------------------------------- #
# 13. status — 总览 + 各引擎可用性 + deferred
# --------------------------------------------------------------------------- #
def _engines_available() -> dict:
    out = {m: (_opt(m) is not None) for m in _SOURCE_MODULES}
    out["local_powers"] = _opt("local_powers") is not None
    out["autopilot_store"] = _opt("autopilot_store") is not None
    out["privacy_stream"] = _opt("privacy_stream") is not None
    return out


def status(argument_id: str = "", db_path: str | None = None) -> dict:
    db = db_path or _db_path()
    argument_id = "" if argument_id is None else str(argument_id)
    if argument_id:
        row = _get_argument(argument_id, db)
        if not row:
            return {"ok": False, "error": "没找到论证 " + argument_id}
        out = {"ok": True, "argument_id": argument_id, "ts": row["ts"],
               "session_id": row["session_id"], "claim": row["claim"],
               "claim_kind": row["claim_kind"], "n_premises": row["n_premises"],
               "n_rules": row["n_rules"], "validity": row["validity"],
               "n_flaws": row["n_flaws"], "private": bool(row["private"])}
        if row["private"]:
            out["_private"] = True
        return out
    conn = _conn(db)
    try:
        n_args = conn.execute("SELECT COUNT(*) c FROM ap_arguments").fetchone()["c"]
        n_hand = conn.execute("SELECT COUNT(*) c FROM ap_handoffs").fetchone()["c"]
        recent = [{"argument_id": r["argument_id"], "ts": r["ts"], "session_id": r["session_id"],
                   "claim": (r["claim"] or "")[:50], "validity": r["validity"],
                   "n_flaws": r["n_flaws"], "private": bool(r["private"])}
                  for r in conn.execute(
                      "SELECT * FROM ap_arguments ORDER BY ts DESC LIMIT 10").fetchall()]
    finally:
        conn.close()
    return {"ok": True, "db": db,
            "counts": {"arguments": n_args, "handoffs": n_hand},
            "recent": recent, "engines": _engines_available(),
            "deferred": _deferred_status(),
            "actions": ["build_argument", "premises", "inference_chain", "check_validity",
                        "find_gaps", "fallacy_scan", "minimal_proof", "counterargument",
                        "strengthen", "explain_logic", "handoff", "audit", "status"],
            "note": "可执行论证图与推理证明检查核心: build_argument 把主张展开成 前提→规则→中间结论"
                    "→主张 的论证图; 其余动作检验有效性/找漏洞/扫谬误/造反驳/强化/讲清推理链。"
                    "纯只读分析门面, 落地动作经审批队列。"}


def health() -> dict:
    try:
        s = status()
        return {"ok": True, "detail": "论证证明核心就绪: "
                + str(s["counts"]["arguments"]) + " 论证/" + str(s["counts"]["handoffs"]) + " 交接"}
    except Exception as e:
        return {"ok": False, "detail": "argument_prover 异常: " + str(e)}


# --------------------------------------------------------------------------- #
# LLM 工具面(只读且冻结): 仅 premises/inference_chain/check_validity/find_gaps/
# fallacy_scan/minimal_proof/counterargument/strengthen/explain_logic/status。
# build_argument(写库)/handoff(入审批队列)/audit CLI-only, 绝不暴露给模型。
# --------------------------------------------------------------------------- #
_DISPATCH_WHITELIST = {
    "premises", "inference_chain", "check_validity", "find_gaps", "fallacy_scan",
    "minimal_proof", "counterargument", "strengthen", "explain_logic", "status",
}

_AP_PARAMS = {
    "type": "object",
    "properties": {
        "claim": {"type": "string", "description": "要论证/检验的主张或论点"},
        "text": {"type": "string", "description": "支撑该主张的论证文本(如「因为…所以…」)"},
        "argument_id": {"type": "string", "description": "已 build_argument 入库的论证 id(复用)"},
    },
    "required": [],
}


def _tool(name, desc):
    return {"type": "function", "function": {
        "name": "argument_prover_" + name, "description": desc, "parameters": _AP_PARAMS}}


_AP_TOOLS = [
    _tool("check_validity", "检验一个论证是否站得住: 把主张拆成前提→推理链, 判有效/无效/或然强弱, "
                            "列致命缺陷。回答「这个论点成立吗/哪步有问题」。只读。"),
    _tool("find_gaps", "找论证里的跳步/缺中间环节/未证明前提。回答「这推理缺了哪一步」。只读。"),
    _tool("fallacy_scan", "扫描逻辑谬误: 循环论证/偷换概念/因果倒置/相关非因果/价值混入事实/滑坡/"
                          "诉诸权威/以偏概全/沉没成本…。回答「这里面有没有逻辑陷阱」。只读。"),
    _tool("premises", "列出并分类一个主张的前提(事实/价值/规范/假设)+ 隐含假设。只读。"),
    _tool("inference_chain", "摊开完整推理链: 前提→规则→中间结论→最终主张。回答「凭什么得出这个结论」。只读。"),
    _tool("minimal_proof", "给出支撑结论的最小前提集 + 推理骨架, 指出哪些前提是承重的。只读。"),
    _tool("counterargument", "生成最强反驳: 打最弱前提 + 推理缺陷 + 反对证据 + steelman 对立面。只读。"),
    _tool("strengthen", "给出把论证做得更强的具体改法(补证据/堵跳步/加限定/预防反驳)。只读。"),
    _tool("explain_logic", "人话讲清这条推理为什么成立/不成立: 推理链 + 关键假设 + 判词 + 置信。"
                           "小美说「所以应该这样」时用它给出证明。只读。"),
    _tool("status", "看论证证明核心总览(最近论证 + 各依据来源可用性)。只读。"),
]


def approved_tools() -> list:
    if not config.get("argument_prover.enabled", True) or \
            not config.get("argument_prover.expose_tools", True):
        return []
    return list(_AP_TOOLS)


def is_tool(name: str) -> bool:
    return name in ("argument_prover_" + v for v in _DISPATCH_WHITELIST)


def dispatch(name: str, args: dict | None = None) -> dict:
    """jarvis_brain 工具入口。只放行只读分析动作; 写库/落地类绝不暴露给模型。"""
    args = dict(args or {})
    short = name.replace("argument_prover_", "")
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": "动作 '" + short + "' 不在 argument_prover 模型白名单 "
                         + str(sorted(_DISPATCH_WHITELIST))
                         + "; build_argument/handoff/audit 请经 CLI。"}
    a = {"argument_id": args.get("argument_id", ""), "claim": args.get("claim", ""),
         "text": args.get("text", ""), "premises_in": args.get("premises"),
         "session_id": args.get("session_id", "default")}
    fn = {
        "premises": premises, "inference_chain": inference_chain,
        "check_validity": check_validity, "find_gaps": find_gaps,
        "fallacy_scan": fallacy_scan, "minimal_proof": minimal_proof,
        "counterargument": counterargument, "strengthen": strengthen,
        "explain_logic": explain_logic,
    }.get(short)
    if fn is not None:
        return fn(argument_id=a["argument_id"], claim=a["claim"], text=a["text"],
                  premises_in=a["premises_in"], session_id=a["session_id"])
    if short == "status":
        return status(args.get("argument_id", ""))
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
      ④ 无 confirmed=True; ⑤ 模型白名单只读(build_argument/handoff/audit 不暴露);
      ⑥ 行为: 凭据硬拦不入库 + 私密脱敏 + 循环/价值混事实/相关非因果被检出 + dispatch 白名单。
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
    for leaked in ("build_argument", "handoff", "audit"):
        if leaked in _DISPATCH_WHITELIST:
            fails.append("⑤ 模型白名单泄漏写库/落地动作: " + leaked)

    # ⑥ 行为验证: 临时库 + 各类论证, 验隐私门 + 缺陷检测 + dispatch 白名单。
    tmp = tempfile.mkdtemp(prefix="argument_prover_selftest_")
    db = os.path.join(tmp, "ap.db")
    try:
        # 凭据硬拦: 前提里塞凭据路径 → 入库后图里不得出现明文路径
        b1 = build_argument(
            claim="应该备份这个文件",
            text="因为 ~/.gmail_creds.json 很重要, 所以应该备份这个文件",
            session_id="t", db_path=db)
        if not b1.get("ok"):
            fails.append("⑥ build_argument 失败: " + str(b1))
        else:
            row = _get_argument(b1["argument_id"], db)
            blob = json.dumps(row.get("graph", {}), ensure_ascii=False)
            if ".gmail_creds.json" in blob:
                fails.append("⑥ 凭据路径未被硬拦(出现在入库论证图里)!")
        # 循环论证检出
        circ = check_validity(
            claim="他很诚实",
            text="因为他很诚实, 所以他很诚实", db_path=db)
        names = json.dumps(circ, ensure_ascii=False)
        if "循环论证" not in names:
            fails.append("⑥ 循环论证未被检出: " + str(circ.get("verdict")))
        # 价值混入事实(is-ought)检出
        vf = fallacy_scan(
            claim="所以我们应该涨价",
            text="数据显示成本上升了百分之十, 所以我们应该涨价", db_path=db)
        if "是→应该" not in json.dumps(vf, ensure_ascii=False) and \
                "value_fact" not in json.dumps(vf, ensure_ascii=False):
            fails.append("⑥ 价值混入事实(is-ought)未被检出")
        # 相关非因果检出
        cc = fallacy_scan(
            claim="冰淇淋导致溺水",
            text="数据显示冰淇淋销量和溺水人数同时上升, 冰淇淋导致溺水", db_path=db)
        if "corr_not_cause" not in json.dumps(cc, ensure_ascii=False):
            fails.append("⑥ 相关非因果未被检出")
        # explain_logic 形态完整
        ex = explain_logic(claim="该跳槽", text="因为工资低, 所以该跳槽", db_path=db)
        if not (ex.get("ok") and "chain" in ex and "verdict" in ex and "confidence" in ex):
            fails.append("⑥ explain_logic 输出形态不完整: " + str(list(ex.keys())))
        # dispatch 白名单: 拒 build_argument, 放行 check_validity/status
        if dispatch("build_argument", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 build_argument(应拒绝)")
        if dispatch("handoff", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行了 handoff(应拒绝)")
        if not dispatch("status", {}).get("ok"):
            fails.append("⑤ dispatch 应放行 status")
        cv = dispatch("check_validity", {"claim": "x", "text": "因为a, 所以x"})
        if not cv.get("ok"):
            fails.append("⑤ dispatch 应放行 check_validity")
        # handoff 纯交接备忘(无 action) → 不触审批队列、无副作用(只写自有临时库)。
        # 落地动作必经审批队列的门由 test_argument_prover.py 用假 autopilot_store 验证,
        # selftest 不调真 enqueue_approval(避免污染真实审批队列)。
        h = handoff(argument_id=b1.get("argument_id", "") if b1.get("ok") else "",
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
    print("OK argument_prover selftest 全过(纯只读分析门面 + 零真实副作用):")
    print("  ① 不直接 import 任何执行面模块 ✓")
    print("  ② 源码无执行面方法调用(run_shell/write_file/type_text/click_on...) ✓")
    print("  ③ 无危险 stdlib 调用(system/remove/rmtree/exec...) ✓")
    print("  ④ 源码无 confirmed=True ✓")
    print("  ⑤ 模型白名单只读, 写库/落地动作(build_argument/handoff/audit)不暴露给模型 ✓")
    print("  ⑥ 凭据硬拦不入库 + 循环/价值混事实/相关非因果被检出 + dispatch 白名单 ✓")
    return 0


# --------------------------------------------------------------------------- #
# CLI(argparse; 全 pretty-JSON via _emit)
# --------------------------------------------------------------------------- #
def _main(argv: list) -> int:
    p = argparse.ArgumentParser(prog="argument_prover",
                                description="可执行论证图与推理证明检查核心")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser("build_argument", help="把主张展开成论证图并入库")
    pb.add_argument("--claim", default="", help="要论证的主张/结论")
    pb.add_argument("--text", default="", help="支撑文本(如「因为…所以…」)")
    pb.add_argument("--premise", action="append", default=None, help="一条前提(可多次)")
    pb.add_argument("--session", default="default")

    # 同构的只读分析动作: 都接 argument_id 或 claim/text
    for name, helptxt in (
            ("premises", "列出并分类前提(事实/价值/规范/假设)+ 隐含假设"),
            ("inference_chain", "摊开推理链: 前提→规则→中间结论→最终主张"),
            ("check_validity", "检验有效性: 结论是否从前提+规则推得出"),
            ("find_gaps", "找跳步/缺中间环节/未证明前提"),
            ("fallacy_scan", "扫描逻辑谬误(结构型+修辞型)"),
            ("minimal_proof", "最小证明: 承重前提 + 推理骨架"),
            ("counterargument", "生成最强反驳"),
            ("strengthen", "怎么把论证做得更强"),
            ("explain_logic", "人话讲清推理链为什么成立/不成立")):
        sp = sub.add_parser(name, help=helptxt)
        sp.add_argument("--id", dest="argument_id", default="", help="已入库论证 id")
        sp.add_argument("--claim", default="", help="主张(无 id 时现场构造)")
        sp.add_argument("--text", default="", help="论证文本(无 id 时现场构造)")
        sp.add_argument("--premise", action="append", default=None, help="一条前提(可多次)")
        sp.add_argument("--session", default="default")

    ph = sub.add_parser("handoff", help="把证明好的论证打包交接(落地动作必经审批闸)")
    ph.add_argument("--id", dest="argument_id", default="")
    ph.add_argument("--to", default="")
    ph.add_argument("--action", default="", help="若要落地一个真动作: 描述它(必经红线分级+审批队列)")
    ph.add_argument("--note", default="")

    pa = sub.add_parser("audit", help="隐私/安全自审(只读)")
    pa.add_argument("--id", dest="argument_id", default="")

    ps = sub.add_parser("status", help="总览 + 各依据来源可用性")
    ps.add_argument("argument_id", nargs="?", default="")

    sub.add_parser("selftest", help="跑安全不变量自检")

    if not argv:
        _emit(status())
        return 0
    args = p.parse_args(argv)

    if args.cmd == "build_argument":
        _emit(build_argument(args.claim, text=args.text, premises=args.premise,
                             session_id=args.session))
    elif args.cmd in ("premises", "inference_chain", "check_validity", "find_gaps",
                      "fallacy_scan", "minimal_proof", "counterargument", "strengthen",
                      "explain_logic"):
        fn = globals()[args.cmd]
        _emit(fn(argument_id=args.argument_id, claim=args.claim, text=args.text,
                 premises_in=args.premise, session_id=args.session))
    elif args.cmd == "handoff":
        _emit(handoff(argument_id=args.argument_id, to=args.to, action=args.action,
                      note=args.note))
    elif args.cmd == "audit":
        _emit(audit(args.argument_id))
    elif args.cmd == "status":
        _emit(status(args.argument_id))
    elif args.cmd == "selftest":
        return _selftest()
    else:
        _emit(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
