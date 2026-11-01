#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""product_spec_hub.py — 产品规格与兼容替代知识中枢(薄门面 / 纯只读顾问 / 离线启发式)。

从 User 本机的**产品手册、规格书、报价单、历史订单、客户需求邮件、供应商资料、图片/OCR**
建立本地、可解释的「产品 / SKU / 型号 / 规格参数 / 认证标准 / 适用场景 / 禁配条件 / 可替代品」
知识图谱(工业树脂外贸为主场景)。让小美在报价、选型、答复客户技术问题时能判断:
  「客户要的技术规格到底匹配哪个型号、哪些型号可替代、哪里会配错 / 漏配 / 认证不符」。

定位与边界(铁律):
  - **纯只读分析门面 + 零真实副作用**:只产「哪个型号匹配 / 能不能替代 / 哪里有坑」的分析与建议,
    绝不执行任何对外动作(不发报价、不发邮件、不下单)。quote_spec_guardrail 是**报价前的体检**,
    只给风险清单, 真要把报价发出去须 User 走审批, 本模块不入队、不执行、不发送。
  - **绝不读凭据**:email_tools / google_tools / gcal_oauth(读 .gmail_creds.json/.gcal_creds.json
    /refresh_token 的)一律**硬拦, 永不 import**。客户需求邮件由调用方取回脱敏后经 ingest 传入。
  - **绝不 subprocess/exec/__import__/动态绕过**:执行面方法从不调用;红线检测**转发 local_powers
    单一真相源**(只用它的纯检测函数, 不碰它的执行函数)。
  - **隐私三重门**:凭据(secret)→ 永久抹除从不落库 / 外传;私密(private, 如客户名 / 报价 / 内部
    采购)→ 脱敏后落库 + 结果标 `_private`/`_force_local` 让大脑切本机模型收尾, 正文不出这台 Mac。
    所有承载内容的自由文本字段(model/name/value/note/need_text/scenario…)一律过隐私判定 + 脱敏 +
    占位, 无一例外。产品规格本身多为公开, 但客户需求 / 报价 / 内部资料按内容逐字段判私密。

与既有模块的分工(为什么需要本模块):
  - procurement_hub 只比供应商**价格 / 交期**, inventory_hub 只看**库存供需**, pricing_profit_hub 只算
    **成本利润**, doc_intel 只**抽文档字段** —— 都答不了「这个技术规格该上哪个型号、能不能替代、
    会不会配错 / 漏认证」。本模块专做**规格匹配与兼容替代**这一层语义。

集成(soft-import, 缺失降级, 永不抛、绝不建桩):
  - 已落地(live, 只读接地):business(产品目录)、mind_graph(概念 / 本体接地)、ocr(图片 → 文本,
    仅 CLI ingest 内、且路径过凭据硬拦后)。
  - 尚未落地(deferred, 记状态绝不建桩):procurement_hub、inventory_hub、pricing_profit_hub、
    doc_intel、reasoning_core、quant_reasoner。
  - mail/calendar:**不**集成代码;客户需求邮件由调用方脱敏后经 ingest_spec 传入二手文本。

全本机, 纯 stdlib sqlite3。自有库 ~/.jarvis/product_spec_hub/product_spec_hub.db, 不碰主库。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import uuid
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402 — 纯读取, 无副作用


# ────────────────────────────────────────────────────────────────────────────── #
# 路径(env 优先 → config → 默认; 函数式以便测试用 env 覆盖、调用时实时取)
# ────────────────────────────────────────────────────────────────────────────── #
_HOME_ENV = "JARVIS_PRODUCT_SPEC_HUB_HOME"


def _data_dir() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser(
        config.get("product_spec_hub.data_dir", "~/.jarvis/product_spec_hub")))


def _db_path() -> str:
    return os.path.join(_data_dir(), "product_spec_hub.db")


# ────────────────────────────────────────────────────────────────────────────── #
# 软导入可选集成模块(importlib, 非 __import__/exec); 缺/出错 → None(降级, 永不抛)
# ────────────────────────────────────────────────────────────────────────────── #
def _opt(name: str):
    """Soft-import a peer module by name; return None on any error (never raises)."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


# 尚未落地的兄弟 / 推理内核 → 记 deferred, 绝不建桩。
_DEFERRED = ("procurement_hub", "inventory_hub", "pricing_profit_hub",
             "doc_intel", "reasoning_core", "quant_reasoner")

# 已落地的只读接地来源(best-effort; 缺失只是少一点证据, 绝不致命)。
_LIVE_SOURCES = ("business", "mind_graph")

# 凭据读取模块:**永不 import**(硬拦)。客户需求邮件只接调用方脱敏后的二手文本。
_CREDENTIAL_MODULES = ("email_tools", "google_tools", "gcal_oauth")


def _deferred_status() -> dict:
    """报告 deferred / live 集成模块是否在位(在位也仍记 deferred=True 对未落地者)。"""
    out: dict = {}
    for m in _DEFERRED:
        out[m] = {"present": _opt(m) is not None, "deferred": True}
    for m in _LIVE_SOURCES:
        out[m] = {"present": _opt(m) is not None, "deferred": False}
    return out


# ────────────────────────────────────────────────────────────────────────────── #
# 隐私原语(凭据硬拦 + 私密判定 + 脱敏; 复用 awareness_sensors 单一姿态, 本地层永远兜底)
# ────────────────────────────────────────────────────────────────────────────── #
# 凭据 / 密钥文件硬禁名单(本地兜底; 任何场景绝不纳入)。
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".localkey", "config.toml",
})

# 凭据 / 令牌内容分类器 —— **绝不用 $ / ^ 行锚**(否则句中令牌漏判, 见血泪教训)。
# 裸令牌用 (?<![A-Za-z0-9]) 而非 \b 作前界 —— \b 在 `_` 旁失效, 会漏掉嵌进 UPPER_SNAKE 的密钥。
_SECRET_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.localkey|/\.jarvis/config\.toml|"
    r"\.pem(?![A-Za-z])|(?:^|/)id_rsa|(?:^|/)\.ssh/|"
    r"refresh_token|client_secret|private[_\-]?key|"
    r"BEGIN [A-Z ]*PRIVATE KEY|"
    r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{12,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}|(?<![A-Za-z0-9])gho_[A-Za-z0-9]{20,}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{16,}|(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{16,}|(?<![A-Za-z0-9])eyJ[A-Za-z0-9._\-]{20,}",
    re.IGNORECASE)

# 私密(个人 / 商业敏感, 非凭据)内容线索 —— 本地兜底, 与 awareness_sensors 一致姿态。
# 含外贸报价 / 客户 / 采购的商业敏感线索(规格本身公开, 但客户需求与报价不该上云)。
_PRIVATE_RX = re.compile(
    r"银行|工资|薪资|薪水|年终奖|身份证|社保|公积金|存款|余额|卡号|账号|账户|密码|"
    r"验证码|转账|汇款|报销|病|诊断|体检|处方|医保|住院|离婚|分手|贷款|按揭|抵押|"
    r"保密|机密|内部消息|收购|并购|裁员|股权|期权|融资|估值|offer|"
    r"报价|底价|成本价|利润|回扣|佣金|返点|客户名单|采购价|进价|"
    r"\bpassword\b|\bsalary\b|\bssn\b|\bbank\b|\bpin\b|\bmedical\b|"
    r"\bconfidential\b|\bnda\b|\bsecret\b|\btoken\b|\bapi[_-]?key\b|"
    r"\d[\d \-]{9,}\d",
    re.IGNORECASE)

# 高熵令牌兜底:同时含大小写 + 数字、长度 ≥ 24 的连续串 → 极可能是密钥。
# 字符类含 base64 字母表 + / =(否则含 '/' 的 AWS secret 这类会绕过, 见 si5066030 血泪)。
# 量词全部有界(防灾难性回溯 ReDoS —— 模型可经 attribute_extract 等触发任意长输入;
# 真密钥本就短, 512 字符上限绰绰有余)。
_HIENTROPY_RX = re.compile(
    r"(?<![A-Za-z0-9+/=])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}[A-Z])(?=[A-Za-z0-9_\-+/=]{0,512}[a-z])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}\d)"
    r"[A-Za-z0-9_\-+/=]{24,512}(?![A-Za-z0-9+/=])")


def _norm(text: str) -> str:
    """NFKC 归一化(防全角 / 兼容字符绕过安全扫描)。安全检测一律先过它, 且走全文不截断。"""
    try:
        return unicodedata.normalize("NFKC", text or "")
    except Exception:
        return text or ""


def _is_secret_path(path: str) -> bool:
    """凭据 / 密钥文件 → True(硬拦)。**双层**:本地正则永远先跑且不可绕过;库只能再加正例。
    判不准当机密(fail-safe True)。见 [[jarvis-secret-path-dual-layer]] 铁律。"""
    p = path or ""
    try:
        base = os.path.basename(os.path.expanduser(p))
        if base in _SECRET_BASENAMES:
            return True
        if _SECRET_RX.search(_norm(p)):
            return True
    except Exception:
        return True  # fail-safe
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "is_secret_path"):
        try:
            if aw.is_secret_path(p):
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


def _classify_private(text: str = "") -> bool:
    """文本是否私密。本地 _PRIVATE_RX 永远先跑(命中即 True);再叠加 awareness_sensors。
    库不可达且有正文 → fail-safe True(宁可多判私密走本机)。安全检测走**全文 + NFKC**。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密记录不占位、返原文
    (本模块无对外出口; 红线4 由 autopilot 审批闸独立守卫, 不靠此分类)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    blob = _norm(text)
    if not blob:
        return False
    try:
        if _PRIVATE_RX.search(blob):
            return True
    except Exception:
        return True
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "classify_private"):
        try:
            return bool(aw.classify_private(blob))
        except Exception:
            return True
    return False


def _redact(text: str) -> str:
    """脱敏:抹凭据 / 令牌 / 卡号 / 长串数字 / key=value。
    **顺序铁律**:本地 value-strip 必在调库前、且在(归一化后的)原文上跑——库可能有同款 bug 短路
    本地, 故绝不把脱敏首发权交给库。库只用于「再补一刀」。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 返归一化原文, 不脱敏(本模块无
    对外出口, 数据层不阉割)。开 → 完整脱敏(可逆)。"""
    s = _norm(text)
    if not s:
        return ""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return s   # 数据层隐私关: 原文直出(小美可读全部; 出口另有 L3 守卫)
    # 第一刀(本地, 在原文上):凭据特征 → 整段抹。
    s = _SECRET_RX.sub("[凭证已脱敏]", s)
    # key=value / token: xxx 赋值。key 名允许被 UPPER_SNAKE 包裹(AWS_SECRET_KEY=…),
    # 故用 (?<![A-Za-z0-9]) 前界而非 \b(\b 在 `_` 旁失效, 见 si5066030)。
    s = re.sub(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9]*"
        r"(?:pass(?:word)?|pwd|token|secret|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret|refresh[_-]?token)"
        r"[A-Za-z0-9_\-]*\s*[:=：]\s*\S+",
        "[凭证已脱敏]", s)
    # 高熵令牌兜底。
    s = _HIENTROPY_RX.sub("[令牌已脱敏]", s)
    # 长串数字(卡号 / 电话 / IBAN)。
    s = re.sub(r"\d[\d \-]{9,}\d", "[数字已脱敏]", s)
    # 第二刀(库, best-effort, 在已脱敏文本上再补):缺 / 出错都无碍, 本地已先行。
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "redact"):
        try:
            s = aw.redact(s)
        except Exception:
            pass
    return s


def _safe_text(text: str, limit: int = 240) -> str:
    """对外出口统一兜底:任何要返回 / 落库的自由文本都过它 —— 先脱敏, 再(仅显示用)截断。
    安全扫描在 _redact 内走全文;这里的 limit 只压缩展示长度, 不削弱脱敏。"""
    s = _redact(text)
    if limit and len(s) > limit:
        return s[:limit] + "…"
    return s


def _seal(obj):
    """递归兜底:对将要返回 / 落库的结构再扫一遍, 把任何漏网的凭据明文占位掉
    (防新增字段忘脱敏 —— 输出含🔒必兜底, 见 mind_graph/_seal 教训)。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 原样直出, 不做任何占位替换
    (本模块无对外出口; 与 _redact/_classify_private 同一开关口径)。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return obj
    if isinstance(obj, str):
        n = _norm(obj)
        if _SECRET_RX.search(n) or _HIENTROPY_RX.search(n):  # 含 / 的 base64 高熵密钥也兜底
            return _HIENTROPY_RX.sub("[令牌已脱敏]", _SECRET_RX.sub("[凭证已脱敏]", n))
        return obj
    if isinstance(obj, dict):
        return {k: _seal(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_seal(v) for v in obj]
    return obj


def _flag_private(out: dict, any_private: bool) -> dict:
    """统一出口:先 _seal 递归兜底凭据, 再按私密标 _private / _force_local
    (大脑切本机模型收尾, 正文不出本机)。"""
    if not isinstance(out, dict):
        return out
    out = _seal(out)
    if any_private:
        out["_private"] = True
        if not config.get("safety.allow_cloud_private", False):
            out["_force_local"] = True
    return out


# ────────────────────────────────────────────────────────────────────────────── #
# 红线转发(local_powers 单一真相源 —— 只用纯检测函数, 绝不碰执行函数)
# ────────────────────────────────────────────────────────────────────────────── #
# 本地兜底:报价体检里若描述「对外动作」, 标 needs_approval, 绝不执行。
_OUTBOUND_RX = re.compile(
    r"发(邮件|消息|短信|微信|信息|报价)|回复|发送|发布|拨打|打电话|致电|约见|提交|下单|签约|"
    r"\bsend\b|\bemail\b|\breply\b|\bpost\b|\bpublish\b|\bcall\b|\bsubmit\b|\bquote\b|\border\b",
    re.IGNORECASE)


def _action_risk(action_text: str) -> dict:
    """判一段文本是否触红线 / 属对外动作。转发 local_powers 纯检测(单一真相源);
    本地 _OUTBOUND_RX 永远兜底。返回 {needs_approval, reason, outbound}。**只检测, 不执行**。"""
    t = _norm(action_text or "")
    reason = None
    lp = _opt("local_powers")
    if lp is not None:
        for fn in ("_check_red_line", "non_shell_danger"):
            if hasattr(lp, fn):
                try:
                    r = getattr(lp, fn)(t)  # 纯检测函数, 命中返回原因
                    if r:
                        reason = str(r)
                        break
                except Exception:
                    pass
    outbound = bool(reason) or bool(_OUTBOUND_RX.search(t))
    return {"needs_approval": outbound, "reason": reason, "outbound": outbound}


# ────────────────────────────────────────────────────────────────────────────── #
# 数值 / 计数护栏(NaN/inf 会污染裁决链:NaN 比较恒 False 会跌进兜底, int(NaN) 抛 —— 见 si9819690)
# ────────────────────────────────────────────────────────────────────────────── #
def _finite_num(v):
    """转 float 且必须有限;NaN/inf/非数 → None(绝不让其进比较 / int 强转)。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_int(v, default: int, lo: int, hi: int) -> int:
    """安全取整 + 夹取值域;NaN/inf/非整 → default。"""
    f = _finite_num(v)
    if f is None:
        return default
    try:
        n = int(f)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


# ────────────────────────────────────────────────────────────────────────────── #
# 持久层:自有库(纯 stdlib sqlite3)
# ────────────────────────────────────────────────────────────────────────────── #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ps_costs (
    id          TEXT PRIMARY KEY,          -- product_id(同 ps_products.id)
    product_id  TEXT NOT NULL,
    cost_rmb_per_ton TEXT NOT NULL,        -- 含税采购成本价 RMB/吨(脱敏后存储, Decimal 字串)
    cost_currency TEXT NOT NULL DEFAULT 'CNY',  -- 货币(默认 CNY)
    cost_date   TEXT NOT NULL,             -- 录入日期 YYYY-MM-DD(或 ISO8601)
    source      TEXT NOT NULL DEFAULT 'manual',
    ts_created  TEXT NOT NULL,
    ts_updated  TEXT NOT NULL,
    private     INTEGER NOT NULL DEFAULT 1  -- 成本价业务敏感, 默认 private=1
);
CREATE INDEX IF NOT EXISTS idx_ps_cost_pid ON ps_costs(product_id);
CREATE TABLE IF NOT EXISTS ps_products (
    id          TEXT PRIMARY KEY,          -- 归一化 SKU/型号的 hash
    sku         TEXT,                      -- 归一化(规范)SKU/型号
    model       TEXT,                      -- 脱敏后显示型号
    category    TEXT,                      -- 类别(工业树脂 / 分散剂 / …, 脱敏)
    brand       TEXT,                      -- 品牌(脱敏)
    summary     TEXT,                      -- 已脱敏简介(私密则只留占位)
    source      TEXT,                      -- manual|manual_doc|spec_sheet|quote|order|email|supplier|image_ocr
    ts_created  TEXT,
    ts_updated  TEXT,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_prod_sku ON ps_products(sku);
CREATE INDEX IF NOT EXISTS idx_ps_prod_cat ON ps_products(category);
CREATE TABLE IF NOT EXISTS ps_attributes (
    id          TEXT PRIMARY KEY,
    product_id  TEXT,
    name        TEXT,                      -- 规范化参数名(PC2含量/白度/吸油量/晶型/…)
    value       TEXT,                      -- 脱敏后原值文本
    unit        TEXT,                      -- 单位(% / μm / g/100g / …)
    numeric     REAL,                      -- 解析出的数值(无则 NULL)
    source      TEXT,
    ts          TEXT,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_attr_prod ON ps_attributes(product_id);
CREATE INDEX IF NOT EXISTS idx_ps_attr_name ON ps_attributes(name);
CREATE TABLE IF NOT EXISTS ps_certs (
    id          TEXT PRIMARY KEY,
    product_id  TEXT,
    cert        TEXT,                      -- 规范化认证名(REACH/RoHS/FDA/ISO9001/GB...)
    status      TEXT,                      -- have|none|pending
    region      TEXT,                      -- 适用地区 / 市场(脱敏)
    ts          TEXT,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_cert_prod ON ps_certs(product_id);
CREATE TABLE IF NOT EXISTS ps_scenarios (
    id          TEXT PRIMARY KEY,
    product_id  TEXT,
    kind        TEXT,                      -- applicable | forbidden
    scenario    TEXT,                      -- 规范化场景(户外涂料/塑料母粒/食品级/…)
    note        TEXT,                      -- 已脱敏说明
    ts          TEXT,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_scen_prod ON ps_scenarios(product_id);
CREATE TABLE IF NOT EXISTS ps_relations (
    id          TEXT PRIMARY KEY,
    product_id  TEXT,                      -- 主产品
    partner_id  TEXT,                      -- 关联产品
    rtype       TEXT,                      -- substitute | bundle | complement | variant
    confidence  REAL DEFAULT 0.5,
    reason      TEXT,                      -- 已脱敏理由
    note        TEXT,
    ts          TEXT,
    private     INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ps_rel_prod ON ps_relations(product_id);
CREATE INDEX IF NOT EXISTS idx_ps_rel_partner ON ps_relations(partner_id);
CREATE TABLE IF NOT EXISTS ps_requirements (
    id          TEXT PRIMARY KEY,
    ref         TEXT,                      -- 客户 / 项目引用(脱敏)
    need_text   TEXT,                      -- 已脱敏需求正文(私密则只留占位)
    attrs_json  TEXT,                      -- 解析出的规格要求 [{name,op,value,unit}]
    certs_json  TEXT,                      -- 解析出的认证要求 [..]
    scen_json   TEXT,                      -- 解析出的场景 [{kind,scenario}]
    source      TEXT,
    ts          TEXT,
    private     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ps_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    action      TEXT,
    summary     TEXT
);
"""


def _conn(db_path: str | None = None) -> sqlite3.Connection:
    """创建 / 复用 SQLite 连接;WAL;行工厂为 Row;幂等建表。"""
    dbp = db_path or _db_path()
    os.makedirs(os.path.dirname(dbp) or ".", exist_ok=True)
    conn = sqlite3.connect(dbp, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    conn.executescript(_SCHEMA)
    # 幂等迁移:ps_costs 表在旧库中可能不存在(新字段补丁, 重跑不崩)。
    # _SCHEMA 里已有 CREATE TABLE IF NOT EXISTS ps_costs —— 对新库自动建, 对旧库不重建。
    # 此处只做兜底:确认 index 存在(executescript 已含 CREATE INDEX IF NOT EXISTS)。
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(v, fb):
    try:
        return json.loads(v) if v else fb
    except (json.JSONDecodeError, TypeError):
        return fb


def _esc_like(s: str) -> str:
    """转义 LIKE 通配符(% _ \\), 配合 ESCAPE '\\' —— 防一个型号 % 跨产品取到别人规格。"""
    return (s or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _pid(sku_or_model: str) -> str:
    """product id = 归一化 SKU 的稳定 hash。"""
    return hashlib.sha1(_norm_sku(sku_or_model).encode("utf-8")).hexdigest()[:16]


def _sid(prefix: str, *parts: str) -> str:
    """行 id;拼 uuid4 保证唯一 —— 同一产品同一秒内多条参数也不会撞 PRIMARY KEY
    (旧版只 hash prefix|parts|秒级时间戳, 批量录入会 IntegrityError, 见 si-ID-collision)。"""
    raw = ("|".join([prefix] + [str(p) for p in parts]) + "|" + _now()
           + "|" + uuid.uuid4().hex)
    return prefix + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _did(prefix: str, *parts: str) -> str:
    """**确定性** id(无 uuid / 时间戳)—— 给需幂等去重的实体(同产品同认证 / 同场景)配
    INSERT OR REPLACE, 重复 ingest 不累积多行。"""
    raw = "|".join([prefix] + [_norm(str(p)).strip().lower() for p in parts])
    return prefix + "_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _audit(action: str, summary: str, db_path: str | None = None) -> None:
    """审计日志(best-effort, 绝不抛)。summary 已脱敏后写入。"""
    try:
        conn = _conn(db_path)
        try:
            conn.execute("INSERT INTO ps_audit (ts, action, summary) VALUES (?,?,?)",
                         (_now(), action, _safe_text(summary, 200)))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


# ────────────────────────────────────────────────────────────────────────────── #
# 领域词典(确定性、可解释的启发式 —— 无 LLM, 离线可测; 工业树脂外贸为主, 易扩展)
# ────────────────────────────────────────────────────────────────────────────── #
# 规格参数名同义归一:把客户 / 手册的各种叫法收敛到规范名。
_ATTR_SYNONYMS = {
    "工业树脂含量": "PC2含量", "pc2含量": "PC2含量", "聚碳酸酯含量": "PC2含量",
    "主成分含量": "PC2含量", "纯度": "PC2含量", "pc2": "PC2含量",
    "二基础聚合物": "PC2含量", "工业树脂": "PC2含量", "工业树脂": "PC2含量",
    # 注: 绝不放裸「含量」—— 会把"水分含量/固含量/铁含量"误归 PC2含量(子串碰撞);
    # 「工业树脂/工业树脂」是产品全称/简称, 不会和水分/固含子串碰撞, 安全。
    "白度": "白度", "whiteness": "白度",
    "消色力": "消色力", "着色力": "消色力", "tinting": "消色力", "tinting strength": "消色力",
    "遮盖力": "遮盖力", "hiding": "遮盖力", "hiding power": "遮盖力", "opacity": "遮盖力",
    "吸油量": "吸油量", "oil absorption": "吸油量",
    "粒径": "粒径", "粒度": "粒径", "particle size": "粒径", "平均粒径": "粒径",
    "比表面积": "比表面积", "surface area": "比表面积",
    "水分": "水分", "含水量": "水分", "moisture": "水分",
    "挥发份": "挥发份", "挥发分": "挥发份", "volatile": "挥发份",
    "筛余物": "筛余物", "residue": "筛余物", "筛余": "筛余物",
    "ph值": "pH值", "ph": "pH值",
    "密度": "密度", "density": "密度", "比重": "密度",
    "电阻率": "电阻率", "resistivity": "电阻率",
    "耐候性": "耐候性", "weather": "耐候性", "weather resistance": "耐候性",
    "晶型": "晶型", "crystal": "晶型", "crystal form": "晶型",
    "处理": "表面处理", "表面处理": "表面处理", "coating": "表面处理", "surface treatment": "表面处理",
}

# 参数极性:不给比较符时, 该参数「越高越好 / 越低越好 / 中性」—— 客户写"白度96"默认 ≥96。
_ATTR_POLARITY = {
    "PC2含量": "higher", "白度": "higher", "消色力": "higher", "遮盖力": "higher",
    "耐候性": "higher", "比表面积": "neutral", "粒径": "neutral", "密度": "neutral",
    "pH值": "neutral", "电阻率": "neutral",
    "吸油量": "lower", "水分": "lower", "挥发份": "lower", "筛余物": "lower",
}

# 晶型规范化(类别型;高耐候型 ≠ 通用型, 错配多半是选型大坑)。
_CRYSTAL_FORMS = {
    "高耐候": "高耐候型", "高耐候型": "高耐候型", "homopolymer": "高耐候型", "r型": "高耐候型",
    "通用": "通用型", "通用型": "通用型", "锐单体": "通用型", "copolymer": "通用型", "a型": "通用型",
}

# 认证 / 标准规范化 + 同义。
_CERT_VOCAB = {
    "reach": "REACH", "rohs": "RoHS", "fda": "FDA", "iso9001": "ISO9001",
    "iso 9001": "ISO9001", "iso14001": "ISO14001", "iso 14001": "ISO14001",
    "halal": "Halal", "清真": "Halal", "kosher": "Kosher", "犹太": "Kosher",
    "sgs": "SGS", "ce": "CE", "msds": "MSDS", "coa": "COA",
    "gb/t 1040": "GB/T1040", "gb/t1040": "GB/T1040", "gb1040": "GB/T1040",
    "gbt1040": "GB/T1040", "国标": "GB/T1040",
    "食品级": "FoodGrade", "food grade": "FoodGrade", "化妆品级": "CosmeticGrade",
    "ec无毒": "EN71", "en71": "EN71", "prop65": "Prop65", "加州65": "Prop65",
}

# 应用场景规范化 + 同义。
_SCENARIO_VOCAB = {
    "涂料": "涂料", "coating": "涂料", "paint": "涂料",
    "户外": "户外", "outdoor": "户外", "exterior": "户外", "外墙": "户外",
    "室内": "室内", "indoor": "室内", "内墙": "室内",
    "塑料": "塑料", "plastic": "塑料", "母粒": "塑料母粒", "masterbatch": "塑料母粒",
    "油墨": "油墨", "ink": "油墨", "造纸": "造纸", "paper": "造纸",
    "橡胶": "橡胶", "rubber": "橡胶",
    "化妆品": "化妆品", "cosmetic": "化妆品", "防晒": "化妆品",
    "食品": "食品级", "食品级": "食品级", "food": "食品级",
    "陶瓷": "陶瓷", "ceramic": "陶瓷", "搪瓷": "搪瓷", "玻璃": "玻璃",
    "粉末涂料": "粉末涂料", "powder coating": "粉末涂料",
    "卷材": "卷材涂料", "coil": "卷材涂料",
}

# 场景 → 通常要求的认证(用于 certification_check / wrong_model_risk 的应配未配检测)。
_SCENARIO_REQUIRED_CERTS = {
    "食品级": ("FDA", "FoodGrade"),
    "化妆品": ("CosmeticGrade", "FDA"),
    "户外": ("REACH",),
    "涂料": (),
    "塑料母粒": ("RoHS",),
    "玩具": ("EN71", "Prop65"),
}

# 户外 / 耐候要求高耐候型(通用型不耐候)—— 用于错配检测的领域规则。
_SCENARIO_CRYSTAL = {
    "户外": "高耐候型", "外墙": "高耐候型", "卷材涂料": "高耐候型", "粉末涂料": "高耐候型",
}

_VALID_SOURCES = {"manual", "manual_doc", "spec_sheet", "quote", "order",
                  "email", "supplier", "image_ocr", "catalog"}
_VALID_RTYPES = {"substitute", "bundle", "complement", "variant"}
_VALID_CERT_STATUS = {"have", "none", "pending"}

# 比较符解析(中英 + 符号)。顺序敏感:先长后短。
_OP_PATTERNS = [
    (re.compile(r"≥|>=|不低于|不少于|至少|最低|min\.?", re.I), ">="),
    (re.compile(r"≤|<=|不高于|不超过|不大于|最高|最多|max\.?", re.I), "<="),
    (re.compile(r"<|低于|小于|少于", re.I), "<"),
    (re.compile(r">|高于|大于|超过", re.I), ">"),
    # 只认强相等信号(=/等于/达到);弱填充词「是 / 为」不当 ==, 让其落入参数极性默认
    # (否则 '水分为0.5' 被当 ==0.5 而非 ≤0.5, 对 lower-better 参数误判)。
    (re.compile(r"=|等于|达到", re.I), "=="),
]
# 范围: 95-97 / 95~97 / 95至97
_RANGE_RX = re.compile(r"(\d+(?:\.\d+)?)\s*[-~至到]\s*(\d+(?:\.\d+)?)")
# 数值 + 可选单位
_NUMUNIT_RX = re.compile(r"(\d+(?:\.\d+)?)\s*(%|％|μm|um|nm|g/100g|g/cm3|m2/g|mpa|目|mesh)?", re.I)


# ────────────────────────────────────────────────────────────────────────────── #
# SKU / 型号规范化
# ────────────────────────────────────────────────────────────────────────────── #
def _norm_sku(raw: str) -> str:
    """归一化型号:NFKC + 大写 + 去空白 / 连字符 / 点(M-996 / m 996 / M.996 → M996)。
    保留字母数字与少量分隔语义已抹平, 便于把同一型号的写法变体收敛到一个 id。"""
    s = _norm(raw or "").upper()
    s = re.sub(r"[\s\-_.·]+", "", s)
    return s.strip()


# ────────────────────────────────────────────────────────────────────────────── #
# 规格文本解析(把自由文本 → 结构化规格要求 / 参数)
# ────────────────────────────────────────────────────────────────────────────── #
def _canon_attr(name: str) -> str:
    """把参数名收敛到规范名(找不到 → 脱敏后原样, 截断)。"""
    key = _norm(name or "").strip().lower()
    if key in _ATTR_SYNONYMS:
        return _ATTR_SYNONYMS[key]
    for syn, canon in _ATTR_SYNONYMS.items():
        if syn and syn in key:
            return canon
    return _safe_text(name or "", 40)


def _canon_cert(name: str) -> str:
    key = _norm(name or "").strip().lower()
    if key in _CERT_VOCAB:
        return _CERT_VOCAB[key]
    for syn, canon in _CERT_VOCAB.items():
        if syn and syn in key:
            return canon
    return _safe_text(name or "", 30)


def _canon_scenario(name: str) -> str:
    key = _norm(name or "").strip().lower()
    if key in _SCENARIO_VOCAB:
        return _SCENARIO_VOCAB[key]
    for syn, canon in _SCENARIO_VOCAB.items():
        if syn and syn in key:
            return canon
    return _safe_text(name or "", 30)


def _canon_crystal(text: str) -> str | None:
    t = _norm(text or "").lower()
    for syn, canon in _CRYSTAL_FORMS.items():
        if syn in t:
            return canon
    return None


# 否定上下文(出现在某词之前 → 该场景 / 认证是「不要 / 禁用」而非要求)。
_NEG_RX = re.compile(
    r"不能|不可|不得|不要|不用于|不用|不宜|不推荐|不建议|不适用|不适合|不适|"
    r"无法用于|无法|切勿|切忌|严禁|禁用|禁止|禁|勿|忌|避免|无需|不需要|"
    r"not\s+for|forbidden|avoid|no\s", re.I)
# 肯定线索(若出现在否定词与目标之间, 说明否定已被新句意覆盖, 如 '不要A 需要B' 之于 B)。
# 不含 '含/具备'(它们是"含有"而非"适用/需要"信号, 会把 '禁用含铅…' 的否定误重置)。
_POS_CUE_RX = re.compile(
    # '必须' 已是独立备选, 故不放单字 '须'(它会子串命中 '无须(=无需)'/'须知' 误重置否定)。
    r"需要|必须|要求|适用|专用|适合|可用|采用|使用|推荐|建议|"
    r"require|need|applicable|recommend|suitable", re.I)
# 小句边界 —— **只用强标点 / 换行**, 绝不含裸空格(否则 '不能用于 户外' / 'not for outdoor' 的否定词
# 被空格截断丢失, 把 forbidden 反转成 applicable, 见 R3 CRITICAL)。跨句前向泄漏改由 _POS_CUE_RX 重置防。
_CLAUSE_BREAK_RX = re.compile(r"[,，;；。!！?？、/\n]")
# ASCII 词(英文 / 代号)判定:只含 ASCII 可见字符 + 空格 / . / / / -。
_ASCII_TOKEN_RX = re.compile(r"^[\x00-\x7f ./\-]+$")


def _negated_before(blob: str, pos: int, window: int = 26) -> bool:
    """pos 处场景 / 认证是否被否定。只在**同一子句**内找否定(遇标点 / 空格小句边界即止),
    且否定词与目标之间若出现肯定线索(需要 / 适用…)则否定被重置 —— 防跨句前向泄漏
    ('不要REACH 需要RoHS' 不会把 RoHS 误判否定)。"""
    pre = _norm(blob[max(0, pos - window):pos])
    brks = list(_CLAUSE_BREAK_RX.finditer(pre))
    if brks:                                   # 只看最近一个小句边界之后的片段
        pre = pre[brks[-1].end():]
    negs = list(_NEG_RX.finditer(pre))
    if not negs:
        return False
    between = pre[negs[-1].end():]             # 末个否定词与目标之间
    if _POS_CUE_RX.search(between):            # 中间有肯定线索 → 否定被覆盖
        return False
    return True


def _find_token_positions(syn: str, low: str):
    """在(已小写的)low 里找 syn 的所有起点:ASCII 词用词边界(防 'ce' 命中 service /
    'ink' 命中 thinking), CJK 用子串。生成器。"""
    s = syn or ""
    if not s:
        return
    if _ASCII_TOKEN_RX.match(s):
        for m in re.finditer(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", low):
            yield m.start()
    else:
        start = 0
        while True:
            p = low.find(s, start)
            if p < 0:
                break
            yield p
            start = p + 1


def _parse_op_value(fragment: str) -> dict:
    """从一段「比较符 + 数值 + 单位」碎片解析 {op,value,unit}。范围取下界为主、记 high。"""
    frag = _norm(fragment or "").replace(",", "").replace("，", "")  # 去千分位逗号: 1,000→1000
    op = ""
    for rx, sym in _OP_PATTERNS:
        if rx.search(frag):
            op = sym
            break
    rng = _RANGE_RX.search(frag)
    if rng:
        lo = _finite_num(rng.group(1))
        hi = _finite_num(rng.group(2))
        return {"op": "between", "value": lo, "value_high": hi, "unit": ""}
    m = _NUMUNIT_RX.search(frag)
    val = _finite_num(m.group(1)) if m else None
    unit = (m.group(2) or "").strip() if m else ""
    unit = "%" if unit in ("％",) else unit
    return {"op": op, "value": val, "value_high": None, "unit": unit}


def _parse_spec_text(text: str) -> dict:
    """把一段自由规格 / 需求文本解析成结构化要求(确定性启发式, 可解释)。
    返回 {attrs:[{name,op,value,unit,raw}], certs:[..], scenarios:[{kind,scenario}],
          crystal_form, raw}。所有出口文本已脱敏。"""
    blob = _norm(text or "")
    low = blob.lower()
    attrs: list = []
    seen_attr: set = set()
    # 所有参数同义词的首位(用于把取值窗口截断在「下一个不同参数名」前, 防偷邻参数数值)。
    syn_positions: list = []
    for s in _ATTR_SYNONYMS:
        for p in _find_token_positions(s, low):
            syn_positions.append((p, _ATTR_SYNONYMS[s]))
            break
    # 逐参数名扫描:取值窗口**从同义词之后**开始(绝不含名字本身的数字, 如 PC2 里的 '2')。
    for syn in sorted(_ATTR_SYNONYMS.keys(), key=len, reverse=True):
        canon = _ATTR_SYNONYMS[syn]
        if canon in seen_attr or canon == "晶型" or canon == "表面处理":
            continue
        positions = list(_find_token_positions(syn, low))
        if not positions:
            continue
        idx = positions[0]
        start = idx + len(syn)            # 关键: 跳过同义词本身
        end = start + 28
        for p, other_canon in syn_positions:  # 截断在下一个不同参数前, 防 '白度优良 吸油量18'→白度=18
            if start <= p < end and other_canon != canon:
                end = p
        window = blob[start:end]
        pv = _parse_op_value(window)
        if pv["value"] is None:
            continue
        op = pv["op"]
        if not op:                        # 不给比较符 → 用领域极性补默认
            pol = _ATTR_POLARITY.get(canon, "neutral")
            op = ">=" if pol == "higher" else "<=" if pol == "lower" else "=="
        # raw 由解析结果重建(不切原文窗口)—— 杜绝凭据 token 被窗口切断后残段外泄(隐私铁律)。
        vtxt = (str(int(pv["value"])) if float(pv["value"]).is_integer() else str(pv["value"]))
        if op == "between" and pv["value_high"] is not None:  # 范围展示完整上下界
            ht = (str(int(pv["value_high"])) if float(pv["value_high"]).is_integer()
                  else str(pv["value_high"]))
            raw_disp = "%s=%s~%s%s" % (canon, vtxt, ht, pv["unit"] or "")
        else:
            raw_disp = "%s%s%s%s" % (canon, op, vtxt, pv["unit"] or "")
        attrs.append({"name": canon, "op": op, "value": pv["value"],
                      "value_high": pv["value_high"], "unit": pv["unit"],
                      "raw": _safe_text(raw_disp, 50)})
        seen_attr.add(canon)
    # 晶型(类别)。
    crystal = _canon_crystal(blob)
    if crystal:
        attrs.append({"name": "晶型", "op": "==", "value": crystal,
                      "value_high": None, "unit": "", "raw": crystal})
    # 认证(ASCII 代号用词边界防 'ce'⊂service;否定上下文不作要求)。
    certs: list = []
    for syn in sorted(_CERT_VOCAB.keys(), key=len, reverse=True):
        c = _CERT_VOCAB[syn]
        if c in certs:
            continue
        pos = next(_find_token_positions(syn, low), -1)
        if pos < 0 or _negated_before(blob, pos):
            continue
        certs.append(c)
    # 场景(扫**所有**出现, 各按就近否定判 applicable/forbidden;同场景两种极性都保留)。
    scenarios: list = []
    seen_scen: set = set()
    for syn in sorted(_SCENARIO_VOCAB.keys(), key=len, reverse=True):
        canon = _SCENARIO_VOCAB[syn]
        for pos in _find_token_positions(syn, low):
            kind = "forbidden" if _negated_before(blob, pos) else "applicable"
            key = (kind, canon)
            if key in seen_scen:
                continue
            seen_scen.add(key)
            scenarios.append({"kind": kind, "scenario": canon})
    return {"attrs": attrs, "certs": certs, "scenarios": scenarios,
            "crystal_form": crystal, "raw": _safe_text(text, 400)}


# ────────────────────────────────────────────────────────────────────────────── #
# 数值比较 / 参数匹配
# ────────────────────────────────────────────────────────────────────────────── #
def _cmp_numeric(op: str, have, want, want_high=None) -> bool | None:
    """按比较符判断 have 是否满足 want。NaN/inf/缺值 → None(无法判断, 绝不当 pass)。"""
    h = _finite_num(have)
    w = _finite_num(want)
    if h is None or w is None:
        return None
    if op == ">=":
        return h >= w
    if op == "<=":
        return h <= w
    if op == ">":
        return h > w
    if op == "<":
        return h < w
    if op == "between":
        wh = _finite_num(want_high)
        if wh is None:
            return h >= w
        lo, hi = (w, wh) if w <= wh else (wh, w)
        return lo <= h <= hi
    if op == "==":
        return abs(h - w) <= max(1e-9, abs(w) * 0.01)  # 1% 容差
    return None


def _attr_match(req: dict, have_value, have_numeric) -> dict:
    """单条规格要求 vs 产品该参数值。返回 {status, detail}。
    status ∈ met | below | mismatch | missing_data | uncertain。"""
    name = req.get("name", "")
    op = req.get("op", "==")
    want = req.get("value")
    if have_value is None and have_numeric is None:
        return {"status": "missing_data", "detail": f"产品无「{name}」参数数据, 无法确认是否满足"}
    # 类别型(晶型 / 表面处理 / 字符串值)。
    if isinstance(want, str) and _finite_num(want) is None:
        hv = _norm(str(have_value or "")).lower()
        wv = _norm(str(want)).lower()
        if not hv:
            return {"status": "missing_data", "detail": f"产品无「{name}」数据"}
        if wv and wv in hv:
            return {"status": "met", "detail": f"{name}={have_value} 满足要求({want})"}
        return {"status": "mismatch",
                "detail": f"{name} 要求「{want}」但产品为「{_safe_text(str(have_value),40)}」"}
    # 数值型。
    hn = _finite_num(have_numeric)
    wn = _finite_num(want)
    # 单位疑似不一致(% vs 小数, 如 0.98 vs 98)→ 不臆断, 标 uncertain 并告警, 绝不静默误判。
    # 仅对上限类比较(≥/>/==/between)告警; <=/< 下「产品值远小于上限」是 lower-better 正常达标, 不告警。
    if (req.get("unit") in ("%", "％") and op not in ("<=", "<")
            and hn is not None and wn is not None
            and ((hn <= 1.5 and wn >= 10) or (hn >= 10 and wn <= 1.5))):
        return {"status": "uncertain",
                "detail": f"{name} 单位疑似不一致(产品 {have_value} vs 要求 {op}{want}{req.get('unit','')}), 请核对%/小数表示"}
    res = _cmp_numeric(op, have_numeric, want, req.get("value_high"))
    if res is None:
        return {"status": "uncertain",
                "detail": f"{name} 无法数值比较(产品值「{_safe_text(str(have_value),40)}」/ 要求 {op}{want})"}
    if res:
        return {"status": "met", "detail": f"{name}={have_value} 满足 {op}{want}{req.get('unit','')}"}
    return {"status": "below",
            "detail": f"{name}={have_value} 不满足 {op}{want}{req.get('unit','')}"}


# ────────────────────────────────────────────────────────────────────────────── #
# 内部读取助手
# ────────────────────────────────────────────────────────────────────────────── #
def _get_product_row(conn: sqlite3.Connection, key: str):
    """按 id 精确 / 按归一化 SKU 精确 / 再按 model LIKE(转义)兜底, 取一个产品行。"""
    k = (key or "").strip()
    if not k:
        return None
    row = conn.execute("SELECT * FROM ps_products WHERE id=?", (k,)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM ps_products WHERE id=?", (_pid(k),)).fetchone()
    if row:
        return row
    row = conn.execute("SELECT * FROM ps_products WHERE sku=?", (_norm_sku(k),)).fetchone()
    if row:
        return row
    like = "%" + _esc_like(_redact(k)) + "%"
    return conn.execute(
        "SELECT * FROM ps_products WHERE model LIKE ? ESCAPE '\\' ORDER BY ts_updated DESC LIMIT 1",
        (like,)).fetchone()


def _product_attrs(conn, product_id: str) -> list:
    return list(conn.execute(
        "SELECT * FROM ps_attributes WHERE product_id=? ORDER BY name", (product_id,)))


def _product_certs(conn, product_id: str) -> list:
    return list(conn.execute(
        "SELECT * FROM ps_certs WHERE product_id=?", (product_id,)))


def _product_scenarios(conn, product_id: str) -> list:
    return list(conn.execute(
        "SELECT * FROM ps_scenarios WHERE product_id=?", (product_id,)))


def _attr_lookup(conn, product_id: str, name: str):
    """取产品某规范参数最新一行(value, numeric)。无 → (None, None)。"""
    # ts 为秒级, 同秒多次 ingest 会并列 —— 加 rowid DESC 作 tiebreaker 取真正最新行(否则取到陈旧值)。
    row = conn.execute(
        "SELECT value, numeric FROM ps_attributes WHERE product_id=? AND name=? "
        "ORDER BY ts DESC, rowid DESC LIMIT 1", (product_id, name)).fetchone()
    if not row:
        return None, None
    return row["value"], row["numeric"]


def _product_brief(conn, row) -> dict:
    """单个产品脱敏摘要(对外字段全过 _safe_text)。"""
    pid = row["id"]
    n_attr = conn.execute(
        "SELECT COUNT(*) FROM ps_attributes WHERE product_id=?", (pid,)).fetchone()[0]
    certs = [c["cert"] for c in _product_certs(conn, pid) if c["status"] == "have"]
    return {
        "id": pid,
        "sku": _safe_text(row["sku"] or "", 60),
        "model": _safe_text(row["model"] or "", 80),
        "category": _safe_text(row["category"] or "", 40),
        "brand": _safe_text(row["brand"] or "", 40),
        "attr_count": n_attr,
        "certs": certs[:12],
        "private": bool(row["private"]),
    }


def _row_private(*rows) -> bool:
    for r in rows:
        try:
            if r is not None and bool(r["private"]):
                return True
        except (KeyError, IndexError, TypeError):
            pass
    return False


# ────────────────────────────────────────────────────────────────────────────── #
# ingest_spec — 录入脱敏产品 / 需求资料(CLI / 内部用, 不暴露给模型)。隐私在入库边界收口。
# ────────────────────────────────────────────────────────────────────────────── #
def ingest_spec(model: str = "", text: str = "", category: str = "", brand: str = "",
                attrs: list | None = None, certs: list | None = None,
                scenarios: list | None = None, relations: list | None = None,
                requirement: dict | None = None, source: str = "manual",
                image: str = "", db_path: str | None = None) -> dict:
    """录入一份脱敏的产品规格 / 需求资料。

    model: 型号 / SKU(自由文本, 会规范化 + 脱敏)。text: 规格 / 手册 / 邮件 / 报价正文(必须是
      调用方已取回的二手文本)。attrs/certs/scenarios/relations: 结构化补充(不给则从 text 解析)。
    requirement: {ref, need_text 或直接复用 text} —— 当 source 是 email/quote 且是客户需求时, 另存需求记录。
    image: 图片路径(可选, **CLI-only**)—— 凭据路径硬拦后经 ocr 取文本, 当作 text 再解析。

    隐私铁律:凭据永久抹除;私密内容(客户名 / 报价 / 内部采购)脱敏后落库 + 标 private。
    所有自由文本字段一律先脱敏。
    """
    # —— 图片 → 文本(仅 CLI;凭据路径硬拦, 私密路径标 private)——
    img_private = False
    if image:
        if _is_secret_path(image):
            return {"ok": False, "error": "拒绝:图片路径疑似凭据 / 密钥文件, 硬拦不读"}
        img_private = _is_private_path(image)
        ocr = _opt("ocr")
        if ocr is not None and hasattr(ocr, "ocr_image"):
            try:
                ocr_text = ocr.ocr_image(image)
                if ocr_text:
                    text = (text + "\n" + str(ocr_text)) if text else str(ocr_text)
                    if source == "manual":
                        source = "image_ocr"
            except Exception:
                pass

    model_raw = (model or "").strip()
    if not model_raw and not (requirement and isinstance(requirement, dict)):
        return {"ok": False, "error": "缺少 model(型号 / SKU);纯需求录入请传 requirement"}
    if source not in _VALID_SOURCES:
        source = "manual"

    # —— 全文 NFKC + 全文隐私扫描(扫描窗口 ≥ 落库窗口, 绝不截断后再判)——
    _req = requirement if isinstance(requirement, dict) else {}
    full_blob = _norm(" ".join([
        model_raw, text or "", category or "", brand or "",
        str(_req.get("ref", "") or ""), str(_req.get("need_text", "") or ""),
        json.dumps(attrs or [], ensure_ascii=False),
        json.dumps(certs or [], ensure_ascii=False),
        json.dumps(scenarios or [], ensure_ascii=False),
        json.dumps(relations or [], ensure_ascii=False),
    ]))
    any_private = _classify_private(full_blob) or img_private

    ts = _now()
    conn = _conn(db_path)
    added = {"product": 0, "attributes": 0, "certs": 0, "scenarios": 0,
             "relations": 0, "requirement": 0}
    pid = None
    try:
        if model_raw:
            pid = _pid(model_raw)
            sku = _norm_sku(model_raw)
            model_s = _safe_text(model_raw, 80)
            cat_s = _safe_text(category or "", 40)
            brand_s = _safe_text(brand or "", 40)
            summary = _safe_text(text or "", 280 if any_private else 800)
            ex = conn.execute("SELECT id FROM ps_products WHERE id=?", (pid,)).fetchone()
            if ex:
                conn.execute(
                    "UPDATE ps_products SET model=?, "
                    "category=COALESCE(NULLIF(?,''),category), "
                    "brand=COALESCE(NULLIF(?,''),brand), "
                    "summary=COALESCE(NULLIF(?,''),summary), source=?, ts_updated=?, "
                    "private=MAX(private,?) WHERE id=?",
                    (model_s, cat_s if category else "", brand_s if brand else "",
                     summary if text else "", source, ts, 1 if any_private else 0, pid))
            else:
                conn.execute(
                    "INSERT INTO ps_products "
                    "(id,sku,model,category,brand,summary,source,ts_created,ts_updated,private) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (pid, sku, model_s, cat_s, brand_s, summary, source, ts, ts,
                     1 if any_private else 0))
            added["product"] = 1

            # 结构化 attrs(不给则从 text 解析)。非 list 标量入参 → 视作空(防 CLI 误传标量崩)。
            parsed = _parse_spec_text(text) if (text and not attrs) else None
            attr_items = list(attrs) if isinstance(attrs, list) else []
            if parsed:
                attr_items += parsed["attrs"]
            for a in attr_items:
                if not isinstance(a, dict):
                    continue
                nm = _canon_attr(str(a.get("name", "")))
                if not nm:
                    continue
                rawval = a.get("value")
                numv = _finite_num(a.get("numeric", a.get("value")))
                # 确定性 id(_did,无 uuid/时间戳)+ ON CONFLICT DO UPDATE:
                # 重跑 ingest 幂等去重,不累积重复行;private 只升不降(SAFE-03)。
                conn.execute(
                    "INSERT INTO ps_attributes "
                    "(id,product_id,name,value,unit,numeric,source,ts,private) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "value=excluded.value, unit=excluded.unit, "
                    "numeric=excluded.numeric, source=excluded.source, "
                    "ts=excluded.ts, "
                    "private=MAX(ps_attributes.private, excluded.private)",
                    (_did("attr", pid, nm), pid, nm,
                     _safe_text(str(rawval) if rawval is not None else "", 80),
                     _safe_text(str(a.get("unit", "") or ""), 16),
                     numv, source, ts, 1 if any_private else 0))
                added["attributes"] += 1

            # 认证(确定性 id + OR REPLACE → 重复 ingest 幂等去重, 不累积多行)。
            cert_items = list(certs) if isinstance(certs, list) else []
            if parsed:
                cert_items += [{"cert": c, "status": "have"} for c in parsed["certs"]]
            for c in cert_items:
                if isinstance(c, str):
                    c = {"cert": c, "status": "have"}
                if not isinstance(c, dict) or not c.get("cert"):
                    continue
                st = str(c.get("status", "have")).strip().lower()
                if st not in _VALID_CERT_STATUS:
                    st = "have"
                cert_canon = _canon_cert(str(c.get("cert")))
                cert_region = _safe_text(str(c.get("region", "") or ""), 40)
                # 幂等去重键含 region(同标准不同市场各留一行);ON CONFLICT 时 private 单调不降 + 保留旧非空内容。
                conn.execute(
                    "INSERT INTO ps_certs (id,product_id,cert,status,region,ts,private) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
                    "region=COALESCE(NULLIF(excluded.region,''),region), ts=excluded.ts, "
                    "private=MAX(ps_certs.private, excluded.private)",
                    (_did("cert", pid, cert_canon, cert_region), pid, cert_canon,
                     st, cert_region, ts, 1 if any_private else 0))
                added["certs"] += 1

            # 场景(确定性 id 含 kind → 同产品同 kind 同场景幂等;applicable 与 forbidden 各占一行)。
            scen_items = list(scenarios) if isinstance(scenarios, list) else []
            if parsed:
                scen_items += parsed["scenarios"]
            for s in scen_items:
                if isinstance(s, str):
                    s = {"kind": "applicable", "scenario": s}
                if not isinstance(s, dict) or not s.get("scenario"):
                    continue
                kind = str(s.get("kind", "applicable")).strip().lower()
                if kind not in ("applicable", "forbidden"):
                    kind = "applicable"
                scen_canon = _canon_scenario(str(s.get("scenario")))
                conn.execute(
                    "INSERT INTO ps_scenarios (id,product_id,kind,scenario,note,ts,private) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "note=COALESCE(NULLIF(excluded.note,''),note), ts=excluded.ts, "
                    "private=MAX(ps_scenarios.private, excluded.private)",
                    (_did("scen", pid, kind, scen_canon), pid, kind, scen_canon,
                     _safe_text(str(s.get("note", "") or ""), 120), ts,
                     1 if any_private else 0))
                added["scenarios"] += 1

            # 关系(替代 / 配套 / 变体;确定性 id 去重)。
            for rel in (relations if isinstance(relations, list) else []):
                if not isinstance(rel, dict):
                    continue
                partner_raw = (rel.get("to") or rel.get("partner") or "").strip()
                rtype = (rel.get("rtype") or "substitute").strip().lower()
                if not partner_raw or rtype not in _VALID_RTYPES:
                    continue
                partner_pid = _pid(partner_raw)
                rel_blob = _norm(partner_raw + " " + str(rel.get("reason", "") or "")
                                 + " " + str(rel.get("note", "") or ""))
                rel_private = _classify_private(rel_blob)
                # partner 若未建, 建一个最小占位产品(脱敏型号)。
                if not conn.execute("SELECT 1 FROM ps_products WHERE id=?",
                                    (partner_pid,)).fetchone():
                    conn.execute(
                        "INSERT INTO ps_products "
                        "(id,sku,model,category,brand,summary,source,ts_created,ts_updated,private) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (partner_pid, _norm_sku(partner_raw), _safe_text(partner_raw, 80),
                         "", "", "", source, ts, ts, 1 if rel_private else 0))
                conf = _finite_num(rel.get("confidence", 0.5))
                conn.execute(
                    "INSERT INTO ps_relations "
                    "(id,product_id,partner_id,rtype,confidence,reason,note,ts,private) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET confidence=excluded.confidence, "
                    "reason=COALESCE(NULLIF(excluded.reason,''),reason), "
                    "note=COALESCE(NULLIF(excluded.note,''),note), ts=excluded.ts, "
                    "private=MAX(ps_relations.private, excluded.private)",
                    (_did("rel", pid, partner_pid, rtype), pid, partner_pid, rtype,
                     conf if conf is not None else 0.5,
                     _safe_text(str(rel.get("reason", "") or ""), 120),
                     _safe_text(str(rel.get("note", "") or ""), 120), ts,
                     1 if (any_private or rel_private) else 0))
                added["relations"] += 1

        # 客户需求记录(email/quote 等)。
        if _req and (_req.get("need_text") or text):
            need_raw = str(_req.get("need_text") or text or "")
            req_private = _classify_private(_norm(need_raw + " " + str(_req.get("ref", "") or "")))
            parsed_need = _parse_spec_text(need_raw)
            conn.execute(
                "INSERT INTO ps_requirements "
                "(id,ref,need_text,attrs_json,certs_json,scen_json,source,ts,private) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (_sid("req", str(_req.get("ref", "") or "anon")),
                 _safe_text(str(_req.get("ref", "") or ""), 80),
                 _safe_text(need_raw, 280 if (req_private or any_private) else 1200),
                 json.dumps(parsed_need["attrs"], ensure_ascii=False),
                 json.dumps(parsed_need["certs"], ensure_ascii=False),
                 json.dumps(parsed_need["scenarios"], ensure_ascii=False),
                 source, ts, 1 if (req_private or any_private) else 0))
            added["requirement"] = 1
            any_private = any_private or req_private

        conn.commit()
    finally:
        conn.close()

    _audit("ingest_spec", f"{_safe_text(model_raw,60)} <{source}> +{added}", db_path)
    out = {"ok": True, "product_id": pid, "model": _safe_text(model_raw, 80),
           "source": source, "added": added, "private": bool(any_private)}
    return _flag_private(out, bool(any_private))


# ────────────────────────────────────────────────────────────────────────────── #
# 成本台账(cost ledger) — set_cost 写库(CLI-only); get_cost 只读(模型可调)
# 铁律:set_cost 是业务敏感写动作, 永不暴露给模型(_CLI_ONLY 硬拦);
#       get_cost 是只读查询, 在只读白名单里供模型调用。
# ────────────────────────────────────────────────────────────────────────────── #
def set_cost(model: str, cost_rmb_per_ton, cost_date: str = "",
             cost_currency: str = "CNY", source: str = "manual",
             db_path: str | None = None) -> dict:
    """录入/更新某型号的含税采购成本价(RMB/吨)。

    **CLI-only** —— 成本价是业务敏感写动作, 永不经 dispatch/模型路径。
    cost_rmb_per_ton: 正数 Decimal 字串 / 数值。cost_date: YYYY-MM-DD(不填则取今日)。
    幂等:同一 product_id 已存在时 UPDATE(保持单行, ts_updated 更新)。
    隐私:成本价默认 private=1(业务机密)。
    """
    # 1) 型号归一化 + 校验。
    raw = (model or "").strip()
    if not raw:
        return {"ok": False, "error": "缺少 model(型号 / SKU)"}
    pid = _pid(raw)

    # 2) 成本价校验(Decimal + 正数守卫 + NaN/inf)。
    # decimal 是 stdlib(import 不会失败); 用 except Exception 兜底避免 _IO 未绑定隐患。
    from decimal import Decimal as _D
    try:
        c_dec = _D(str(cost_rmb_per_ton).strip())
    except Exception:
        return {"ok": False, "error": f"cost_rmb_per_ton 无法解析为数字: {cost_rmb_per_ton!r}"}
    if not c_dec.is_finite() or c_dec <= 0:
        return {"ok": False, "error": f"cost_rmb_per_ton 必须是正数有限值, 收到: {cost_rmb_per_ton!r}"}

    # 3) 日期(不填 → 今日)。
    ts_now = _now()
    if not (cost_date or "").strip():
        cost_date = ts_now[:10]  # YYYY-MM-DD
    else:
        cost_date = _safe_text(str(cost_date).strip(), 20)

    currency = _safe_text((cost_currency or "CNY").strip().upper(), 8) or "CNY"
    source_s = source if source in _VALID_SOURCES else "manual"

    # 4) 确保 ps_products 里有这个 product(若没有则创占位行), 再 upsert ps_costs。
    conn = _conn(db_path)
    try:
        ex_prod = conn.execute("SELECT id FROM ps_products WHERE id=?", (pid,)).fetchone()
        if not ex_prod:
            # 创建最小占位产品行, 以便外键语义一致(ps_costs.product_id 对应 ps_products.id)。
            sku = _norm_sku(raw)
            model_s = _safe_text(raw, 80)
            conn.execute(
                "INSERT INTO ps_products "
                "(id,sku,model,category,brand,summary,source,ts_created,ts_updated,private) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pid, sku, model_s, "", "", "", source_s, ts_now, ts_now, 1))
        else:
            # 隐私铁律(对抗评审 FIX): 既有产品若原是公开(private=0), 录成本价后该产品转敏感 ——
            # 必须把 ps_products.private 升到 1, 否则 audit()/recent_audit 会把成本明文经 LLM 工具上云。
            # 成本价存在即业务机密, private 只升不降。
            conn.execute("UPDATE ps_products SET private=1 WHERE id=?", (pid,))

        # upsert: INSERT 或 UPDATE(private 恒 1 — 成本价业务机密只升不降)。
        ex_cost = conn.execute("SELECT id FROM ps_costs WHERE id=?", (pid,)).fetchone()
        if ex_cost:
            conn.execute(
                "UPDATE ps_costs SET cost_rmb_per_ton=?, cost_currency=?, cost_date=?, "
                "source=?, ts_updated=?, private=1 WHERE id=?",
                (str(c_dec), currency, cost_date, source_s, ts_now, pid))
        else:
            conn.execute(
                "INSERT INTO ps_costs "
                "(id,product_id,cost_rmb_per_ton,cost_currency,cost_date,source,ts_created,ts_updated,private) "
                "VALUES (?,?,?,?,?,?,?,?,1)",
                (pid, pid, str(c_dec), currency, cost_date, source_s, ts_now, ts_now))
        conn.commit()
    finally:
        conn.close()

    _audit("set_cost", f"{_safe_text(raw,60)} cost={c_dec} {currency} date={cost_date}", db_path)
    return _flag_private(
        {"ok": True, "product_id": pid, "model": _safe_text(raw, 80),
         "cost_rmb_per_ton": str(c_dec), "cost_currency": currency,
         "cost_date": cost_date, "source": source_s},
        True)  # 成本价恒 _private


def get_cost(model: str, db_path: str | None = None) -> dict:
    """查某型号的含税采购成本价(只读)。查不到 → ok=True, found=False。

    只读路径, 模型可调(在只读白名单里)。
    返回:{"ok":True, "found":bool, "cost_rmb_per_ton":"...", "cost_currency":"...",
            "cost_date":"...", "_private":True}
    """
    raw = (model or "").strip()
    if not raw:
        return {"ok": False, "error": "缺少 model"}
    pid = _pid(raw)

    # 先按 pid 精确查;再按 sku 找对应 pid 查(兼容写法变体 M-996 / M996 / m-996)。
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM ps_costs WHERE id=?", (pid,)).fetchone()
        if row is None:
            # 尝试按 sku 找主 product, 再取 cost。
            sku = _norm_sku(raw)
            prod = conn.execute("SELECT id FROM ps_products WHERE sku=?", (sku,)).fetchone()
            if prod:
                row = conn.execute("SELECT * FROM ps_costs WHERE id=?", (prod["id"],)).fetchone()
    finally:
        conn.close()

    if row is None:
        return _flag_private({"ok": True, "found": False, "model": _safe_text(raw, 80)}, False)

    return _flag_private(
        {"ok": True, "found": True,
         "model": _safe_text(raw, 80),
         "cost_rmb_per_ton": str(row["cost_rmb_per_ton"]),
         "cost_currency": str(row["cost_currency"]),
         "cost_date": str(row["cost_date"]),
         "source": str(row["source"]),
         "ts_updated": str(row["ts_updated"])},
        True)  # 成本价恒 _private


# ────────────────────────────────────────────────────────────────────────────── #
# 只读分析动作(模型可调;对外文本全过 _safe_text;私密结果标 _private/_force_local)
# ────────────────────────────────────────────────────────────────────────────── #
def sku_normalize(model: str, db_path: str | None = None) -> dict:
    """把一个型号 / SKU 规范化, 并列出库里同一规范名下已见的写法变体与对应产品。"""
    raw = (model or "").strip()
    if not raw:
        return {"ok": False, "error": "缺少 model"}
    canon = _norm_sku(raw)
    pid = _pid(raw)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM ps_products WHERE id=?", (pid,)).fetchone()
        variants = list(conn.execute(
            "SELECT model FROM ps_products WHERE sku=?", (canon,)))
        any_priv = _row_private(row)
        brief = _product_brief(conn, row) if row else None
    finally:
        conn.close()
    out = {"ok": True, "input": _safe_text(raw, 80), "normalized_sku": canon,
           "product_id": pid, "known": brief is not None,
           "matched_product": brief,
           "known_spellings": [_safe_text(v["model"] or "", 80) for v in variants]}
    return _flag_private(out, any_priv)


def attribute_extract(text: str, db_path: str | None = None) -> dict:
    """从一段自由文本(规格 / 需求描述)抽取结构化规格参数 / 认证 / 场景要求。纯解析, 不写库。"""
    if not (text or "").strip():
        return {"ok": False, "error": "缺少 text"}
    parsed = _parse_spec_text(text)
    out = {"ok": True, "attributes": parsed["attrs"], "certifications": parsed["certs"],
           "scenarios": parsed["scenarios"], "crystal_form": parsed["crystal_form"],
           "note": "确定性启发式解析;数值已做有限性校验, 比较符按领域极性补默认。"}
    return _flag_private(out, _classify_private(text))


def _eval_product_vs_req(conn, prow, parsed: dict) -> dict:
    """核心:产品 vs 解析后的需求, 返回 {hard_fails, gaps, met, missing, score, compatible}。"""
    pid = prow["id"]
    hard_fails: list = []
    gaps: list = []
    met: list = []
    missing: list = []

    # 1) 场景:禁配 → 硬不兼容;适用 → 与产品已登记适用场景正向比对(命中 met / 未知 missing_data)。
    forbidden = {s["scenario"] for s in _product_scenarios(conn, pid) if s["kind"] == "forbidden"}
    applicable_set = {s["scenario"] for s in _product_scenarios(conn, pid) if s["kind"] == "applicable"}
    for sc in parsed.get("scenarios", []):
        scen = sc.get("scenario")
        if sc.get("kind") != "applicable":
            continue
        # 禁配 → 硬不兼容(最高优先)。
        if scen in forbidden:
            hard_fails.append({"type": "forbidden_scenario", "scenario": scen,
                               "detail": f"该型号明确禁用于「{scen}」场景"})
            continue
        # 该场景的晶型领域规则(户外 / 卷材 / 粉末 等需高耐候型)。
        need_crystal = _SCENARIO_CRYSTAL.get(scen)
        crystal_ok = None  # None=无规则或无数据, True=满足, False=违反
        hv = None
        if need_crystal:
            hv, _ = _attr_lookup(conn, pid, "晶型")
            if hv is None or not str(hv).strip():
                crystal_ok = None
            else:
                crystal_ok = need_crystal in _norm(str(hv))
        if crystal_ok is False:
            hard_fails.append({"type": "crystal_for_scenario", "scenario": scen,
                               "detail": f"「{scen}」通常需 {need_crystal}, 但该型号晶型为「{_safe_text(str(hv),20)}」"})
        elif scen in applicable_set:
            met.append({"name": f"场景:{scen}", "requirement": scen, "product_value": "applicable",
                        "status": "met", "detail": f"明确适用于「{scen}」"})
        elif crystal_ok is True:
            # 未显式登记但满足该场景领域规则(晶型) → 计 met(否则全规格正确的产品被埋, 见 R3 回归)。
            met.append({"name": f"场景:{scen}", "requirement": scen, "product_value": need_crystal,
                        "status": "met", "detail": f"晶型({need_crystal})符合「{scen}」要求"})
        elif need_crystal and crystal_ok is None:
            missing.append({"name": "晶型", "requirement": f"{scen}需{need_crystal}",
                            "product_value": None, "status": "missing_data",
                            "detail": f"「{scen}」需 {need_crystal}, 但产品无晶型数据, 无法确认"})
        else:
            # 无领域规则又未登记 → 不确定(软差距, 非阻断, 不计 met):既不臆断完全匹配, 也不重罚埋没。
            gaps.append({"name": f"场景:{scen}", "requirement": scen, "product_value": None,
                         "status": "uncertain",
                         "detail": f"产品未登记是否适用于「{scen}」, 建议确认适用性"})

    # 2) 规格参数。
    for req in parsed.get("attrs", []):
        hv, hn = _attr_lookup(conn, pid, req.get("name", ""))
        r = _attr_match(req, hv, hn)
        item = {"name": req.get("name"), "requirement": f"{req.get('op','')}{req.get('value','')}{req.get('unit','')}",
                "product_value": _safe_text(str(hv), 40) if hv is not None else None,
                "status": r["status"], "detail": r["detail"]}
        if r["status"] == "met":
            met.append(item)
        elif r["status"] == "missing_data":
            missing.append(item)
        elif r["status"] in ("below", "mismatch"):
            if req.get("name") == "晶型":
                hard_fails.append({"type": "crystal_mismatch", **item})
            else:
                gaps.append(item)
        else:  # uncertain
            gaps.append(item)

    # 3) 认证。
    have_certs = {c["cert"] for c in _product_certs(conn, pid) if c["status"] == "have"}
    for need_cert in parsed.get("certs", []):
        if need_cert not in have_certs:
            gaps.append({"name": f"认证:{need_cert}", "requirement": need_cert,
                         "product_value": None, "status": "cert_missing",
                         "detail": f"产品未登记 {need_cert} 认证"})
        else:
            met.append({"name": f"认证:{need_cert}", "requirement": need_cert,
                        "product_value": "have", "status": "met",
                        "detail": f"具备 {need_cert}"})

    blocking = [g for g in gaps if g.get("status") in ("below", "mismatch", "cert_missing")]
    soft = [g for g in gaps if g.get("status") not in ("below", "mismatch", "cert_missing")]  # uncertain
    n_total = len(met) + len(gaps) + len(missing) + len(hard_fails)
    # soft(待确认)/missing(缺数据)给部分基础分 —— 让「可能适用、待确认」高于「确定不达标(below=0)」,
    # 否则纯 uncertain 场景需求会塌成 0.0 与真实 below 同分(见 R4 LOW)。
    base = ((len(met) + 0.25 * len(soft) + 0.15 * len(missing)) / n_total) if n_total else 1.0
    penalty = 0.15 * len(blocking) + 0.06 * len(soft) + 0.08 * len(missing)
    score = 0.0 if hard_fails else round(max(0.0, min(1.0, base - penalty)), 3)
    has_req = n_total > 0
    # 兼容 = 无硬伤 + 无阻断性差距 + (有要求时至少满足一项)。
    # 关键: 全是 missing_data/uncertain(零项满足)的产品**绝不**判 compatible(不臆断为达标)。
    compatible = (not hard_fails and not blocking and (not has_req or len(met) > 0))
    return {"hard_fails": hard_fails, "gaps": gaps, "met": met, "missing": missing,
            "score": score, "compatible": compatible,
            "data_complete": len(missing) == 0}


def _parsed_empty(parsed: dict) -> bool:
    """解析结果是否「无任何结构化要求」(有正文却抽不出规格 / 认证 / 场景 / 晶型 → 视作无法解析,
    不能当作空需求给满分)。"""
    if not isinstance(parsed, dict):
        return True
    return not (parsed.get("attrs") or parsed.get("certs")
                or parsed.get("scenarios") or parsed.get("crystal_form"))


_UNPARSED_ERR = ("需求未能解析出任何规格 / 认证 / 场景要求, 无法判定匹配。"
                 "请明确参数(如 PC2含量≥98% / 高耐候型 / 户外 / REACH)。")


def _resolve_requirement(conn, requirement: str, ref: str) -> dict | None:
    """需求来源:直接文本 → 解析;给 ref → 取最近一条需求记录解析其正文。"""
    if requirement and requirement.strip():
        return _parse_spec_text(requirement)
    if ref and ref.strip():
        like = "%" + _esc_like(_redact(ref)) + "%"
        row = conn.execute(
            "SELECT * FROM ps_requirements WHERE ref LIKE ? ESCAPE '\\' "
            "ORDER BY ts DESC LIMIT 1", (like,)).fetchone()
        if row:
            return {"attrs": _loads(row["attrs_json"], []),
                    "certs": _loads(row["certs_json"], []),
                    "scenarios": _loads(row["scen_json"], []),
                    "crystal_form": None, "raw": _safe_text(row["need_text"] or "", 200),
                    "_req_private": bool(row["private"])}
    return None


def compatibility_check(model: str, requirement: str = "", ref: str = "",
                        db_path: str | None = None) -> dict:
    """判断某型号是否匹配一组需求(规格 / 场景 / 认证):兼容?哪些满足、哪些差、哪里硬不行。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        parsed = _resolve_requirement(conn, requirement, ref)
        if not parsed:
            return {"ok": False, "error": "缺少 requirement(需求文本)或 ref(已录入需求引用)"}
        if _parsed_empty(parsed):
            return {"ok": False, "error": _UNPARSED_ERR}
        ev = _eval_product_vs_req(conn, prow, parsed)
        brief = _product_brief(conn, prow)
        any_priv = _row_private(prow) or _classify_private(requirement) or bool(parsed.get("_req_private"))
    finally:
        conn.close()
    verdict = ("不兼容(有硬性不符)" if ev["hard_fails"] else
               "完全匹配" if ev["compatible"] and not ev["gaps"] and not ev["missing"] else
               "基本匹配但有差距" if ev["compatible"] else "不达标(规格 / 认证不足)")
    out = {"ok": True, "model": brief, "verdict": verdict,
           "compatible": ev["compatible"], "score": ev["score"],
           "data_complete": ev["data_complete"],  # 是否所有需求项都有产品数据(无 missing_data)
           "hard_fails": ev["hard_fails"], "gaps": ev["gaps"],
           "met": ev["met"], "missing_data": ev["missing"],
           "requirement_parsed": {"attrs": parsed.get("attrs"), "certs": parsed.get("certs"),
                                  "scenarios": parsed.get("scenarios")}}
    return _flag_private(out, any_priv)


def _attr_vector(conn, pid: str) -> dict:
    """产品的数值参数向量(name → numeric)+ 晶型 + 适用场景集, 用于相似度。"""
    vec = {}
    for a in _product_attrs(conn, pid):
        if a["numeric"] is not None and a["name"] != "晶型":
            vec[a["name"]] = a["numeric"]
    crystal, _ = (_attr_lookup(conn, pid, "晶型"))
    scenes = {s["scenario"] for s in _product_scenarios(conn, pid) if s["kind"] == "applicable"}
    return {"vec": vec, "crystal": _norm(str(crystal or "")), "scenes": scenes}


def _similarity(a: dict, b: dict) -> float:
    """两个产品的规格相似度 ∈ [0,1]:数值参数余弦 + 晶型一致 + 适用场景 Jaccard 的加权。"""
    keys = set(a["vec"]) & set(b["vec"])
    cos = 0.0
    if keys:
        dot = sum(a["vec"][k] * b["vec"][k] for k in keys)
        na = math.sqrt(sum(v * v for v in a["vec"].values()))
        nb = math.sqrt(sum(v * v for v in b["vec"].values()))
        cos = (dot / (na * nb)) if (na > 0 and nb > 0) else 0.0
        cos = max(0.0, min(1.0, cos))
    crystal_same = 1.0 if (a["crystal"] and a["crystal"] == b["crystal"]) else 0.0
    su = a["scenes"] | b["scenes"]
    jac = (len(a["scenes"] & b["scenes"]) / len(su)) if su else 0.0
    return round(0.5 * cos + 0.3 * crystal_same + 0.2 * jac, 3)


def substitute_rank(model: str, scenario: str = "", db_path: str | None = None) -> dict:
    """给一个型号, 排出可替代品:先显式登记的替代边, 再按规格相似度补候选, 给理由。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        pid = prow["id"]
        names = {r["id"]: _safe_text(r["model"] or "", 80)
                 for r in conn.execute("SELECT id,model FROM ps_products")}
        priv_map = {r["id"]: bool(r["private"])
                    for r in conn.execute("SELECT id,private FROM ps_products")}
        any_priv = _row_private(prow)
        # 显式替代边。
        explicit = []
        for rel in conn.execute(
                "SELECT * FROM ps_relations WHERE product_id=? AND rtype IN ('substitute','variant') "
                "ORDER BY confidence DESC", (pid,)):
            any_priv = any_priv or bool(rel["private"]) or priv_map.get(rel["partner_id"], False)
            explicit.append({"model": names.get(rel["partner_id"], rel["partner_id"]),
                             "rtype": rel["rtype"],
                             "confidence": round(float(rel["confidence"] or 0.5), 3),
                             "reason": _safe_text(rel["reason"] or "", 120) or "已登记替代关系",
                             "source": "explicit"})
        # 隐式相似候选(同类、规格相近)。
        base_vec = _attr_vector(conn, pid)
        cands = []
        for r in conn.execute(
                "SELECT * FROM ps_products WHERE id!=? AND (category=? OR category='')",
                (pid, prow["category"] or "")):
            sim = _similarity(base_vec, _attr_vector(conn, r["id"]))
            if scenario:
                forb = {s["scenario"] for s in _product_scenarios(conn, r["id"])
                        if s["kind"] == "forbidden"}
                if _canon_scenario(scenario) in forb:
                    continue
            if sim >= 0.35:
                any_priv = any_priv or bool(r["private"])
                cands.append({"model": _safe_text(r["model"] or "", 80),
                              "similarity": sim,
                              "reason": f"规格相似度 {sim}(数值参数 / 晶型 / 适用场景)",
                              "source": "similarity"})
        cands.sort(key=lambda c: c["similarity"], reverse=True)
    finally:
        conn.close()
    out = {"ok": True, "model": _safe_text(model, 80),
           "explicit_substitutes": explicit, "similar_candidates": cands[:8],
           "note": "优先用显式登记的替代关系;相似候选仅供参考, 替代前务必逐项 compatibility_check。"}
    return _flag_private(out, any_priv)


def requirement_match(requirement: str = "", ref: str = "", limit: int = 8,
                      db_path: str | None = None) -> dict:
    """核心:给一段客户需求(或已录入需求 ref), 找出最匹配的型号并排序, 附差距说明。"""
    conn = _conn(db_path)
    try:
        parsed = _resolve_requirement(conn, requirement, ref)
        if not parsed:
            return {"ok": False, "error": "缺少 requirement(需求文本)或 ref"}
        if _parsed_empty(parsed):
            return {"ok": False, "error": _UNPARSED_ERR}
        prods = list(conn.execute("SELECT * FROM ps_products ORDER BY ts_updated DESC LIMIT 500"))
        any_priv = _classify_private(requirement) or bool(parsed.get("_req_private"))
        qualified, near, disqualified = [], [], []
        for prow in prods:
            if (not _product_attrs(conn, prow["id"]) and not _product_certs(conn, prow["id"])
                    and not _product_scenarios(conn, prow["id"])):
                continue  # 纯占位产品(无任何规格/认证/场景)不参与匹配; 仅场景型产品仍要参与(禁配可见)
            ev = _eval_product_vs_req(conn, prow, parsed)
            any_priv = any_priv or bool(prow["private"])
            entry = {"model": _safe_text(prow["model"] or "", 80),
                     "score": ev["score"], "met": len(ev["met"]),
                     "gaps": len(ev["gaps"]), "missing": len(ev["missing"]),
                     "hard_fails": [h.get("detail") for h in ev["hard_fails"]],
                     "gap_detail": [g.get("detail") for g in ev["gaps"]][:5]}
            if ev["hard_fails"]:
                disqualified.append(entry)
            elif ev["compatible"]:
                qualified.append(entry)
            else:
                near.append(entry)
        qualified.sort(key=lambda e: (e["score"], e["met"]), reverse=True)
        near.sort(key=lambda e: (e["score"], e["met"]), reverse=True)
    finally:
        conn.close()
    lim = _as_int(limit, 8, 1, 50)
    best = qualified[0]["model"] if qualified else (near[0]["model"] if near else None)
    out = {"ok": True, "requirement": _safe_text(parsed.get("raw", "") or requirement, 200),
           "best_match": best, "qualified": qualified[:lim],
           "near_miss": near[:lim], "disqualified": disqualified[:lim],
           "parsed": {"attrs": parsed.get("attrs"), "certs": parsed.get("certs"),
                      "scenarios": parsed.get("scenarios")},
           "note": "qualified=无硬伤且达标;near_miss=无硬伤但有差距;disqualified=有禁配/晶型等硬性不符。"}
    return _flag_private(out, any_priv)


def spec_gap_detect(model: str, requirement: str = "", ref: str = "",
                    db_path: str | None = None) -> dict:
    """给型号 + 需求, 细分差距:漏配(产品无数据)/ 不达标(低于要求)/ 认证缺 / 类别不符。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        parsed = _resolve_requirement(conn, requirement, ref)
        if not parsed:
            return {"ok": False, "error": "缺少 requirement 或 ref"}
        if _parsed_empty(parsed):
            return {"ok": False, "error": _UNPARSED_ERR}
        ev = _eval_product_vs_req(conn, prow, parsed)
        brief = _product_brief(conn, prow)
        any_priv = _row_private(prow) or _classify_private(requirement) or bool(parsed.get("_req_private"))
    finally:
        conn.close()
    below = [g for g in ev["gaps"] if g.get("status") == "below"]
    cert_missing = [g for g in ev["gaps"] if g.get("status") == "cert_missing"]
    uncertain = [g for g in ev["gaps"] if g.get("status") == "uncertain"]
    out = {"ok": True, "model": brief,
           "hard_incompatibilities": ev["hard_fails"],
           "below_spec": below, "missing_certs": cert_missing,
           "missing_data": ev["missing"], "uncertain": uncertain,
           "satisfied": [m.get("detail") for m in ev["met"]],
           "summary": (f"硬性不符 {len(ev['hard_fails'])} / 不达标 {len(below)} / "
                       f"认证缺 {len(cert_missing)} / 漏数据 {len(ev['missing'])} / "
                       f"满足 {len(ev['met'])}"),
           "advice": ("漏数据 ≠ 不达标:标 missing_data 的项需向供应商索取参数确认, 别默认满足或不满足。")}
    return _flag_private(out, any_priv)


def wrong_model_risk(model: str, scenario: str = "", requirement: str = "",
                     db_path: str | None = None) -> dict:
    """配错风险:某型号用于某场景 / 需求会不会配错——禁配场景、晶型不符、认证缺、易混型号。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        pid = prow["id"]
        risks = []
        scen_canon = _canon_scenario(scenario) if scenario else ""
        forb = {s["scenario"] for s in _product_scenarios(conn, pid) if s["kind"] == "forbidden"}
        if scen_canon and scen_canon in forb:
            risks.append({"severity": "high", "type": "forbidden_scenario",
                          "detail": f"该型号明确禁用于「{scen_canon}」, 配过去会出问题"})
        # 场景晶型规则。
        need_crystal = _SCENARIO_CRYSTAL.get(scen_canon)
        if need_crystal:
            hv, _ = _attr_lookup(conn, pid, "晶型")
            if hv and need_crystal not in _norm(str(hv)):
                risks.append({"severity": "high", "type": "crystal_for_scenario",
                              "detail": f"「{scen_canon}」需 {need_crystal}, 该型号为「{_safe_text(str(hv),20)}」, 易因不耐候/泛黄出事"})
        # 场景应配认证。
        for need_cert in _SCENARIO_REQUIRED_CERTS.get(scen_canon, ()):
            have = conn.execute(
                "SELECT 1 FROM ps_certs WHERE product_id=? AND cert=? AND status='have'",
                (pid, need_cert)).fetchone()
            if not have:
                risks.append({"severity": "medium", "type": "cert_missing",
                              "detail": f"「{scen_canon}」通常要求 {need_cert} 认证, 该型号未登记"})
        # 需求规格不达标。
        if requirement:
            parsed = _parse_spec_text(requirement)
            ev = _eval_product_vs_req(conn, prow, parsed)
            for g in ev["gaps"]:
                if g.get("status") == "below":
                    risks.append({"severity": "medium", "type": "below_spec", "detail": g.get("detail")})
            for h in ev["hard_fails"]:
                risks.append({"severity": "high", "type": h.get("type", "hard_fail"),
                              "detail": h.get("detail")})
        # 易混型号:同类里规范 SKU 编辑距离很近、但禁配 / 晶型不同。
        confusions = []
        conf_priv = False   # 易混产品的私密位累积 —— 私密产品的型号名进了输出, 必须让整条结果标 _private
        my_sku = prow["sku"] or _norm_sku(prow["model"] or "")
        my_crystal, _ = _attr_lookup(conn, pid, "晶型")
        for r in conn.execute(
                "SELECT * FROM ps_products WHERE id!=? AND category=?",
                (pid, prow["category"] or "")):
            osku = r["sku"] or _norm_sku(r["model"] or "")
            if not osku or not my_sku:
                continue
            dist = _edit_distance(my_sku, osku)
            if dist <= 2 and dist < max(len(my_sku), len(osku)):
                oc, _x = _attr_lookup(conn, r["id"], "晶型")
                diff = []
                if my_crystal and oc and _norm(str(my_crystal)) != _norm(str(oc)):
                    diff.append(f"晶型不同({_safe_text(str(my_crystal),12)} vs {_safe_text(str(oc),12)})")
                if diff:
                    confusions.append({"model": _safe_text(r["model"] or "", 80),
                                       "why": "型号写法极相近但规格不同:" + "、".join(diff)})
                    conf_priv = conf_priv or bool(r["private"])  # 与 substitute_rank 同款私密传播
        any_priv = _row_private(prow) or _classify_private(requirement) or conf_priv
    finally:
        conn.close()
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda x: sev_rank.get(x.get("severity"), 3))
    out = {"ok": True, "model": _safe_text(model, 80),
           "scenario": _safe_text(scenario, 60) or None,
           "risks": risks, "risk_level": ("high" if any(r["severity"] == "high" for r in risks)
                                          else "medium" if risks else "low"),
           "confusable_models": confusions,
           "note": "high=会出事(禁配/晶型/硬伤);medium=可能不达标/缺认证;空=暂未发现明显配错风险。"}
    return _flag_private(out, any_priv)


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein(小字符串, 纯 stdlib)。"""
    a = a or ""
    b = b or ""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def certification_check(model: str, scenario: str = "", region: str = "",
                        db_path: str | None = None) -> dict:
    """认证核查:某型号对某场景 / 市场, 需要哪些认证、有没有、缺哪个。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        pid = prow["id"]
        have = {c["cert"]: c["status"] for c in _product_certs(conn, pid)}
        any_priv = _row_private(prow)
    finally:
        conn.close()
    scen_canon = _canon_scenario(scenario) if scenario else ""
    required = list(_SCENARIO_REQUIRED_CERTS.get(scen_canon, ()))
    missing = [c for c in required if have.get(c) != "have"]
    present = [{"cert": k, "status": v} for k, v in have.items()]
    out = {"ok": True, "model": _safe_text(model, 80),
           "scenario": scen_canon or None, "region": _safe_text(region, 40) or None,
           "required_for_scenario": required, "missing_required": missing,
           "product_certs": present,
           "verdict": ("缺必要认证" if missing else "认证齐备" if required else "该场景无强制认证要求(请按目标市场复核)"),
           "note": "认证要求按通用规则给, 具体出口市场以当地法规 / 客户要求为准, 务必人工复核。"}
    return _flag_private(out, any_priv)


def bundle_suggestion(model: str = "", db_path: str | None = None) -> dict:
    """配套建议:某型号通常要搭配哪些产品(分散剂 / 助剂等)—— 显式 bundle/complement 边。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        pid = prow["id"]
        names = {r["id"]: (_safe_text(r["model"] or "", 80), bool(r["private"]))
                 for r in conn.execute("SELECT id,model,private FROM ps_products")}
        any_priv = _row_private(prow)
        bundles = []
        for rel in conn.execute(
                "SELECT * FROM ps_relations WHERE product_id=? AND rtype IN ('bundle','complement') "
                "ORDER BY confidence DESC", (pid,)):
            nm, pv = names.get(rel["partner_id"], (rel["partner_id"], False))
            any_priv = any_priv or bool(rel["private"]) or pv
            bundles.append({"model": nm, "rtype": rel["rtype"],
                            "reason": _safe_text(rel["reason"] or "", 120) or "常配套使用",
                            "confidence": round(float(rel["confidence"] or 0.5), 3)})
    finally:
        conn.close()
    out = {"ok": True, "model": _safe_text(model, 80), "bundles": bundles,
           "note": "仅列已登记的配套 / 互补关系;无数据时建议据应用工艺人工补充配套件。"}
    return _flag_private(out, any_priv)


def quote_spec_guardrail(quote: list | None = None, requirement: str = "",
                         db_path: str | None = None) -> dict:
    """报价前规格体检:对报价里每条 {model, scenario} 跑配错 / 缺认证 / 不达标检查, 出风险清单。
    **纯顾问**:只给风险, 绝不发送 / 下单;真要把报价发出去须 User 审批。"""
    lines = quote if isinstance(quote, list) else []
    if not lines:
        return {"ok": False, "error": "缺少 quote(列表 [{model, scenario}])"}
    findings = []
    any_priv = _classify_private(requirement)
    blockers = 0
    for ln in lines[:50]:
        if not isinstance(ln, dict):
            continue
        model = str(ln.get("model", "") or "")
        scen = str(ln.get("scenario", "") or "")
        req = str(ln.get("requirement", "") or requirement or "")
        wm = wrong_model_risk(model, scenario=scen, requirement=req, db_path=db_path)
        cc = certification_check(model, scenario=scen, db_path=db_path)
        if wm.get("_private") or cc.get("_private"):
            any_priv = True
        line_risks = wm.get("risks", []) if wm.get("ok") else [{"severity": "high",
                     "type": "unknown_model", "detail": wm.get("error", "未知型号")}]
        miss_cert = cc.get("missing_required", []) if cc.get("ok") else []
        high = [r for r in line_risks if r.get("severity") == "high"]
        if high or miss_cert:
            blockers += 1
        findings.append({"model": _safe_text(model, 80), "scenario": _safe_text(scen, 60),
                         "risks": line_risks, "missing_certs": miss_cert,
                         "confusable": wm.get("confusable_models", []) if wm.get("ok") else []})
    risk_action = _action_risk(requirement or "")
    out = {"ok": True, "lines_checked": len(findings), "lines_with_blockers": blockers,
           "findings": findings,
           "verdict": ("有阻断性问题, 报价前务必修正" if blockers else "未发现阻断性规格问题(仍建议人工复核)"),
           "disclaimer": "本模块只做规格体检, 零执行:不发报价、不下单。任何对外发送须 User 审批后由人发起。"}
    if risk_action["outbound"]:
        out["action_note"] = "需求 / 备注含对外动作迹象;红线检测转发自 local_powers, 仅提示不执行。"
    return _flag_private(out, any_priv)


def quote_draft(requirement: str = "", db_path: str | None = None) -> dict:
    """【变现】客户发来规格需求 → 几分钟出**报价准备草稿**(给 User 准备报价, 不是自动定价)。

    组合既有只读动作:requirement_match(推荐型号)+ compatibility_check(规格逐项对照)+
    certification_check(认证齐全性)+ wrong_model_risk / spec_gap_detect(配错 / 漏配 / 认证风险)。
    输出 {requirement, recommended_models, spec_match, cert_status, risk_flags, quote_checklist,
          draft_note, needs_review:True}。

    红线(对外):报价草稿最终给客户, **绝不自动发送 / 不下单 / 不写中枢**。本动作纯只读、零执行,
    只产草稿给 User, needs_review 恒 True。**不碰真实定价**(价格是商业机密, 草稿是「报价准备」)。"""
    if not (requirement or "").strip():
        return {"ok": False, "error": "缺少 requirement(客户需求文本)"}

    # 1) 解析需求 + 找匹配型号(复用 requirement_match 全套护栏 / 隐私传播)。
    rm = requirement_match(requirement=requirement, db_path=db_path)
    if not rm.get("ok"):
        return {"ok": False, "error": rm.get("error", "需求无法解析"),
                "requirement": _safe_text(requirement, 200), "needs_review": True}
    any_priv = bool(rm.get("_private"))

    # 解析后的需求摘要(脱敏)—— 给 User 看「我们理解的客户需求」。
    parsed = rm.get("parsed", {}) or {}
    req_view = {
        "raw": rm.get("requirement"),
        "specs": [a.get("raw") for a in (parsed.get("attrs") or []) if a.get("raw")],
        "certs_required": list(parsed.get("certs") or []),
        "scenarios": [s.get("scenario") for s in (parsed.get("scenarios") or [])
                      if s.get("kind") == "applicable"],
        "scenarios_forbidden": [s.get("scenario") for s in (parsed.get("scenarios") or [])
                                if s.get("kind") == "forbidden"],
    }

    # 2) 推荐型号:优先 qualified(无硬伤且达标), 不足时补 near_miss(无硬伤但有差距, 标待确认)。
    qualified = rm.get("qualified", []) or []
    near = rm.get("near_miss", []) or []
    primary_scenario = req_view["scenarios"][0] if req_view["scenarios"] else ""

    recommended_models: list = []
    spec_match: list = []
    cert_status: list = []
    risk_flags: list = []
    seen_models: set = set()

    def _consider(entry: dict, tier: str) -> None:
        nonlocal any_priv
        model = entry.get("model")
        if not model or model in seen_models:
            return
        seen_models.add(model)
        # 规格逐项对照(met / gaps / 硬伤 / 缺数据)。
        cc = compatibility_check(model, requirement=requirement, db_path=db_path)
        # 认证齐全性(按主场景核验应配认证)。
        ce = certification_check(model, scenario=primary_scenario, db_path=db_path)
        # 配错 / 漏配 / 认证风险。
        wm = wrong_model_risk(model, scenario=primary_scenario, requirement=requirement,
                              db_path=db_path)
        for sub in (cc, ce, wm):
            if isinstance(sub, dict) and sub.get("_private"):
                any_priv = True

        reason_bits: list = []
        if cc.get("ok"):
            met_n = len(cc.get("met", []))
            gap_n = len(cc.get("gaps", []))
            reason_bits.append(f"满足 {met_n} 项" + (f"、差距 {gap_n} 项" if gap_n else ""))
            if cc.get("verdict"):
                reason_bits.append(str(cc.get("verdict")))
        recommended_models.append({
            "model": _safe_text(str(model), 80),
            "tier": tier,  # qualified=无硬伤且达标; near_miss=无硬伤但有差距(报价前需确认)
            "score": entry.get("score"),
            "match_reason": ";".join(reason_bits) or "规格匹配, 详见 spec_match",
            "needs_confirm": tier == "near_miss",
        })
        # 规格逐项对照(只给脱敏后的 detail / status, 不臆断缺数据为达标)。
        if cc.get("ok"):
            spec_match.append({
                "model": _safe_text(str(model), 80),
                "satisfied": [m.get("detail") for m in cc.get("met", [])],
                "gaps": [g.get("detail") for g in cc.get("gaps", [])],
                "hard_incompatibilities": [h.get("detail") for h in cc.get("hard_fails", [])],
                "missing_data": [m.get("detail") for m in cc.get("missing_data", [])],
            })
        # 认证齐全性。
        if ce.get("ok"):
            cert_status.append({
                "model": _safe_text(str(model), 80),
                "required_for_scenario": ce.get("required_for_scenario", []),
                "missing_required": ce.get("missing_required", []),
                "product_certs": ce.get("product_certs", []),
                "verdict": ce.get("verdict"),
            })
        # 配错 / 漏配 / 认证风险(只收 high / medium, 附型号)。
        if wm.get("ok"):
            for r in wm.get("risks", []):
                if r.get("severity") in ("high", "medium"):
                    risk_flags.append({"model": _safe_text(str(model), 80),
                                       "severity": r.get("severity"),
                                       "type": r.get("type"), "detail": r.get("detail")})
            for conf in wm.get("confusable_models", []):
                risk_flags.append({"model": _safe_text(str(model), 80),
                                   "severity": "medium", "type": "confusable_model",
                                   "detail": "易混型号:" + _safe_text(str(conf.get("model", "")), 80)
                                             + "(" + _safe_text(str(conf.get("why", "")), 80) + ")"})

    for e in qualified[:3]:
        _consider(e, "qualified")
    if len(recommended_models) < 2:  # qualified 不足 → 补 near_miss(标待确认)凑够候选
        for e in near[:3]:
            if len(recommended_models) >= 3:
                break
            _consider(e, "near_miss")

    risk_flags.sort(key=lambda x: {"high": 0, "medium": 1}.get(x.get("severity"), 2))
    has_high_risk = any(r.get("severity") == "high" for r in risk_flags)
    has_missing_cert = any(c.get("missing_required") for c in cert_status)

    # 3) 报价前要确认的 checklist(防呆 + 防漏 + 对外合规提示;不含定价)。
    checklist: list = ["确认客户需求理解无误(见 requirement.specs / certs_required / scenarios)"]
    if not recommended_models:
        checklist.append("⚠ 当前库内无达标型号 —— 先补型号 / 规格数据再报价, 勿凭印象报")
    else:
        checklist.append("核对推荐型号的最新规格书 / 批次参数(本草稿基于库内已录数据)")
    if has_missing_cert:
        checklist.append("⚠ 有应配认证未登记 —— 报价前向供应商 / QC 确认认证齐全(见 cert_status.missing_required)")
    else:
        checklist.append("复核目标出口市场的认证要求(认证规则按通用给, 以当地法规 / 客户要求为准)")
    if any(r.get("needs_confirm") for r in recommended_models):
        checklist.append("⚠ 含 near_miss 候选(有差距)—— 与客户确认差距项是否可接受再报")
    if has_high_risk:
        checklist.append("⚠ 存在 high 配错风险(禁配 / 晶型 / 硬伤)—— 报价前务必排除, 见 risk_flags")
    checklist.extend([
        "确认报价口径:数量 / 包装 / 交货方式(FOB/CIF)/ 账期 / 有效期(本草稿不含价格)",
        "价格 / 底价为商业机密, 由 User 人工填写, 系统不自动定价",
        "⚠ 报价对外发送须 User 人工审核后由人发起 —— 本草稿绝不自动发送给客户",
    ])

    # 4) 给 User 的准备说明。
    best = rm.get("best_match")
    if recommended_models:
        primary = recommended_models[0]["model"]
        draft_note = (f"客户需求已解析, 推荐优先报「{primary}」"
                      + (f"(另有 {len(recommended_models) - 1} 个候选)" if len(recommended_models) > 1 else "")
                      + "。" + ("⚠ 注意 risk_flags 中的配错 / 缺认证项, " if (has_high_risk or has_missing_cert) else "")
                      + "核对规格书 + 走完 quote_checklist 后即可填价报出。本草稿只做准备, 不含定价、不外发。")
    else:
        draft_note = ("客户需求已解析但库内暂无达标型号 —— 建议先补型号 / 规格数据或人工选型, "
                      "勿凭印象报价。本草稿只做准备, 不含定价、不外发。")

    # 5) 需求 / 备注若含对外动作迹象 → 提示(转发 local_powers 纯检测, 仅提示不执行)。
    risk_action = _action_risk(requirement or "")

    out = {
        "ok": True,
        "requirement": req_view,
        "best_match": best,
        "recommended_models": recommended_models,
        "spec_match": spec_match,
        "cert_status": cert_status,
        "risk_flags": risk_flags,
        "quote_checklist": checklist,
        "draft_note": draft_note,
        "needs_review": True,  # 恒 True:报价对外, 须 User 审核, 绝不自动发送
        "disclaimer": ("报价准备草稿:纯只读、零执行 —— 不含定价(商业机密)、不外发(不发邮件 / 不发客户)、"
                       "不下单、不写中枢。任何对外报价须 User 人工审核后由人发起。"),
    }
    if risk_action["outbound"]:
        out["action_note"] = "需求 / 备注含对外动作迹象;红线检测转发自 local_powers, 仅提示不执行。"
    return _flag_private(out, any_priv)


def evidence_pack(model: str, db_path: str | None = None) -> dict:
    """证据包:某型号所有规格 / 认证 / 场景 / 关系的原始(脱敏)证据 + 来源, 供解释 / 审计 / 答客。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        pid = prow["id"]
        brief = _product_brief(conn, prow)
        attrs = [{"name": a["name"], "value": _safe_text(str(a["value"]), 60),
                  "unit": _safe_text(str(a["unit"] or ""), 16),
                  "numeric": a["numeric"], "source": _safe_text(a["source"] or "", 30),
                  "private": bool(a["private"])} for a in _product_attrs(conn, pid)]
        certs = [{"cert": c["cert"], "status": c["status"],
                  "region": _safe_text(c["region"] or "", 40),
                  "private": bool(c["private"])} for c in _product_certs(conn, pid)]
        scens = [{"kind": s["kind"], "scenario": s["scenario"],
                  "note": _safe_text(s["note"] or "", 120),
                  "private": bool(s["private"])} for s in _product_scenarios(conn, pid)]
        names, priv_map = {}, {}
        for r in conn.execute("SELECT id,model,private FROM ps_products"):
            names[r["id"]] = _safe_text(r["model"] or "", 80)
            priv_map[r["id"]] = bool(r["private"])
        rels = [{"partner": names.get(r["partner_id"], r["partner_id"]), "rtype": r["rtype"],
                 "confidence": round(float(r["confidence"] or 0.5), 3),
                 "reason": _safe_text(r["reason"] or "", 120),
                 # 行私密 OR partner 产品私密 —— partner 型号名进了输出, 其私密身份须传播。
                 "private": bool(r["private"]) or priv_map.get(r["partner_id"], False)}
                for r in conn.execute("SELECT * FROM ps_relations WHERE product_id=?", (pid,))]
        any_priv = (_row_private(prow) or any(a["private"] for a in attrs)
                    or any(c["private"] for c in certs) or any(s["private"] for s in scens)
                    or any(r["private"] for r in rels))
    finally:
        conn.close()
    out = {"ok": True, "model": brief, "attributes": attrs, "certifications": certs,
           "scenarios": scens, "relations": rels,
           "source_summary": _safe_text(prow["summary"] or "", 200)}
    return _flag_private(out, any_priv)


def explain_match(model: str, requirement: str = "", ref: str = "",
                  db_path: str | None = None) -> dict:
    """解释某型号为什么(不)匹配某需求:逐条列推理(满足 / 差距 / 硬伤 + 应用的规则)。"""
    conn = _conn(db_path)
    try:
        prow = _get_product_row(conn, model)
        if not prow:
            return {"ok": False, "error": f"未知型号: {_safe_text(model,60)}"}
        parsed = _resolve_requirement(conn, requirement, ref)
        if not parsed:
            return {"ok": False, "error": "缺少 requirement 或 ref"}
        if _parsed_empty(parsed):
            return {"ok": False, "error": _UNPARSED_ERR}
        ev = _eval_product_vs_req(conn, prow, parsed)
        brief = _product_brief(conn, prow)
        any_priv = _row_private(prow) or _classify_private(requirement) or bool(parsed.get("_req_private"))
    finally:
        conn.close()
    chain = []
    for h in ev["hard_fails"]:
        chain.append(f"✗ 硬性不符:{h.get('detail')}")
    for m in ev["met"]:
        chain.append(f"✓ 满足:{m.get('detail')}")
    for g in ev["gaps"]:
        chain.append(f"△ 差距:{g.get('detail')}")
    for ms in ev["missing"]:
        chain.append(f"? 缺数据:{ms.get('detail')}")
    out = {"ok": True, "model": brief, "score": ev["score"],
           "compatible": ev["compatible"], "reasoning_chain": chain,
           "rules_applied": ["禁配场景 → 硬不兼容", "户外/卷材/粉末 → 需高耐候型",
                             "晶型不符 → 硬不兼容", "数值按比较符与领域极性判定",
                             "认证缺 → 差距", "缺参数数据 → 标 missing_data 不臆断"],
           "explanation": ("匹配" if ev["compatible"] and not ev["hard_fails"]
                           else "不匹配") + f"(评分 {ev['score']})"}
    return _flag_private(out, any_priv)


def audit(db_path: str | None = None) -> dict:
    """审计:库规模 + 凭据收口完整性自检(扫所有落库正文应无凭据明文)+ 最近审计条目。"""
    conn = _conn(db_path)
    try:
        n_prod = conn.execute("SELECT COUNT(*) FROM ps_products").fetchone()[0]
        n_priv = conn.execute("SELECT COUNT(*) FROM ps_products WHERE private=1").fetchone()[0]
        n_attr = conn.execute("SELECT COUNT(*) FROM ps_attributes").fetchone()[0]
        n_cert = conn.execute("SELECT COUNT(*) FROM ps_certs").fetchone()[0]
        n_scen = conn.execute("SELECT COUNT(*) FROM ps_scenarios").fetchone()[0]
        n_rel = conn.execute("SELECT COUNT(*) FROM ps_relations").fetchone()[0]
        n_req = conn.execute("SELECT COUNT(*) FROM ps_requirements").fetchone()[0]
        leak = 0
        for tbl, col in (("ps_products", "model"), ("ps_products", "summary"),
                         ("ps_attributes", "value"), ("ps_scenarios", "note"),
                         ("ps_relations", "reason"), ("ps_requirements", "need_text"),
                         ("ps_certs", "region")):
            for (v,) in conn.execute(f"SELECT {col} FROM {tbl}"):
                if v and _SECRET_RX.search(_norm(v)):
                    leak += 1
        recent = [{"ts": r["ts"], "action": r["action"],
                   "summary": _safe_text(r["summary"] or "", 120)}
                  for r in conn.execute("SELECT * FROM ps_audit ORDER BY id DESC LIMIT 20")]
    finally:
        conn.close()
    out = {"ok": True, "products": n_prod, "private_products": n_priv,
           "attributes": n_attr, "certs": n_cert, "scenarios": n_scen,
           "relations": n_rel, "requirements": n_req,
           "credential_leak_rows": leak,
           "integrity": "PASS" if leak == 0 else "FAIL(发现凭据明文漏网)",
           "recent_audit": recent}
    # recent_audit 的 summary 可能含私密产品型号名 → 有私密产品则整条结果走 _seal + 标 _private。
    return _flag_private(out, n_priv > 0)


def status(db_path: str | None = None) -> dict:
    """总览:启用状态、库规模、集成 / deferred 模块、安全门状态。"""
    try:
        conn = _conn(db_path)
        try:
            n_prod = conn.execute("SELECT COUNT(*) FROM ps_products").fetchone()[0]
            n_attr = conn.execute("SELECT COUNT(*) FROM ps_attributes").fetchone()[0]
            n_req = conn.execute("SELECT COUNT(*) FROM ps_requirements").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    lp = _opt("local_powers")
    return {
        "ok": True, "module": "product_spec_hub",
        "role": "纯只读顾问 / 零执行 / 离线启发式(规格匹配与兼容替代)",
        "db": _db_path(),
        "products": n_prod, "attributes": n_attr, "requirements": n_req,
        "integrations": _deferred_status(),
        "guard": {
            "red_line_source": "local_powers" if lp is not None else "本地兜底(_OUTBOUND_RX)",
            "guard_enabled": bool(getattr(lp, "GUARD_ENABLED", True)) if lp else True,
            "allow_cloud_private": bool(config.get("safety.allow_cloud_private", False)),
        },
        "credential_modules_blocked": list(_CREDENTIAL_MODULES),
    }


# ────────────────────────────────────────────────────────────────────────────── #
# jarvis_brain 工具接口(Style A: 前缀工具名 + approved_tools/is_tool/dispatch)
# 只读白名单;ingest_spec(写库)绝不暴露给模型, 仅 CLI / 内部。
# ────────────────────────────────────────────────────────────────────────────── #
_DISPATCH_WHITELIST = {
    "sku_normalize", "attribute_extract", "compatibility_check", "substitute_rank",
    "requirement_match", "spec_gap_detect", "wrong_model_risk", "certification_check",
    "bundle_suggestion", "quote_spec_guardrail", "quote_draft", "evidence_pack",
    "explain_match", "audit", "status",
    "get_cost",  # 只读查询, 模型可调
}
_CLI_ONLY = {"ingest_spec", "set_cost"}  # set_cost 写库业务敏感, 绝不经模型路径

_PSH_TOOLS = [
    {"type": "function", "function": {
        "name": "product_spec_hub_requirement_match",
        "description": ("【核心】给一段客户需求(规格/认证/场景), 找出最匹配的型号并排序, 附差距说明。"
                        "客户问'我要XX规格上哪个型号'时用。只读。"),
        "parameters": {"type": "object", "properties": {
            "requirement": {"type": "string", "description": "客户需求自由文本(规格/场景/认证)"},
            "ref": {"type": "string", "description": "或已录入的需求引用(客户/项目, 可替代 requirement)"},
            "limit": {"type": "integer", "description": "每档返回上限, 默认 8"}}}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_compatibility_check",
        "description": "判断某型号是否匹配一组需求:兼容?哪些满足/差/硬不行。选型确认时用。只读。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "requirement": {"type": "string"},
            "ref": {"type": "string"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_substitute_rank",
        "description": "给型号排可替代品(显式替代边 + 规格相似度), 带理由。缺货/停产找替代时用。只读。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "scenario": {"type": "string", "description": "目标场景(排除该场景禁配的替代, 可空)"}},
            "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_wrong_model_risk",
        "description": ("配错风险:某型号用于某场景/需求会不会配错(禁配/晶型不符/缺认证/易混型号)。"
                        "下单/报价前防呆。只读。"),
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "scenario": {"type": "string"},
            "requirement": {"type": "string"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_spec_gap_detect",
        "description": "给型号+需求, 细分差距:漏配(无数据)/不达标/认证缺/类别不符。只读。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "requirement": {"type": "string"},
            "ref": {"type": "string"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_certification_check",
        "description": "认证核查:某型号对某场景/市场需要哪些认证、有没有、缺哪个。出口合规用。只读。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "scenario": {"type": "string"},
            "region": {"type": "string"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_quote_spec_guardrail",
        "description": ("报价前规格体检:对报价每条 {model,scenario} 跑配错/缺认证/不达标检查, 出风险清单。"
                        "纯顾问, 零发送。报价复核用。只读。"),
        "parameters": {"type": "object", "properties": {
            "quote": {"type": "array", "items": {"type": "object"},
                      "description": "[{model, scenario, requirement?}]"},
            "requirement": {"type": "string", "description": "整单通用需求(可空)"}}}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_quote_draft",
        "description": ("【报价准备】客户发来规格需求 → 几分钟出报价准备草稿:推荐型号 + 规格逐项对照 + "
                        "认证齐全性 + 配错 / 缺认证风险 + 报价前 checklist + 给 User 的准备说明。"
                        "客户问'要XX规格帮我准备报价'时用。**不含定价(商业机密), 不外发, needs_review=True**。只读。"),
        "parameters": {"type": "object", "properties": {
            "requirement": {"type": "string",
                            "description": "客户需求自由文本(规格 / 晶型 / 场景 / 认证), 如 'PC2≥98% 高耐候型 户外涂料 需 ISO9001'"}},
            "required": ["requirement"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_explain_match",
        "description": "解释某型号为什么(不)匹配某需求:逐条推理链 + 应用的规则。别让小美黑箱下判断。只读。",
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string"},
            "requirement": {"type": "string"},
            "ref": {"type": "string"}}, "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_evidence_pack",
        "description": "证据包:某型号所有规格/认证/场景/关系的脱敏原始证据+来源, 供答客/审计。只读。",
        "parameters": {"type": "object", "properties": {"model": {"type": "string"}},
                       "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_bundle_suggestion",
        "description": "配套建议:某型号通常搭配哪些产品(分散剂/助剂等), 已登记的配套关系。只读。",
        "parameters": {"type": "object", "properties": {"model": {"type": "string"}},
                       "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_attribute_extract",
        "description": "从一段自由文本(规格/需求)抽取结构化规格参数/认证/场景要求。解析客户邮件用。只读。",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_sku_normalize",
        "description": "把型号/SKU 规范化(M-996/M996/m 996→M996), 并列出库里已见写法变体。只读。",
        "parameters": {"type": "object", "properties": {"model": {"type": "string"}},
                       "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_get_cost",
        "description": (
            "查某型号的含税采购成本价(RMB/吨)。只读。"
            "返回 found=True/False + cost_rmb_per_ton + cost_date。"
            "结果恒标 _private(成本价业务机密)。"
            "注:录入/更新成本价须经 CLI: jarvis product_spec_hub set_cost <model> <cost> [--date ...]。"
        ),
        "parameters": {"type": "object", "properties": {
            "model": {"type": "string", "description": "型号 / SKU, 如 M-902 / M996"}},
            "required": ["model"]}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_audit",
        "description": "审计:库规模 + 凭据收口完整性自检 + 最近审计条目。只读。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "product_spec_hub_status",
        "description": "看产品规格中枢总览(库规模 + 集成可用性 + 安全门状态)。只读。",
        "parameters": {"type": "object", "properties": {}}}},
]


def approved_tools() -> list:
    if not config.get("product_spec_hub.enabled", True) or \
            not config.get("product_spec_hub.expose_tools", True):
        return []
    return list(_PSH_TOOLS)


def is_tool(name: str) -> bool:
    return name.startswith("product_spec_hub_") and \
        name.replace("product_spec_hub_", "") in _DISPATCH_WHITELIST


def dispatch(name: str, args: dict | None = None) -> dict:
    """jarvis_brain 工具入口:只放只读分析动作。ingest_spec(写库)永不经此 —— 限 CLI / 内部。"""
    args = dict(args or {})
    short = name.replace("product_spec_hub_", "")
    if short in _CLI_ONLY:
        return {"ok": False, "error": f"动作 '{short}' 写库, 仅限 CLI / 内部, 不暴露给模型"}
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": f"动作 '{short}' 不在 product_spec_hub 只读白名单 "
                         f"{sorted(_DISPATCH_WHITELIST)}; ingest_spec 请经 CLI。"}
    if short == "sku_normalize":
        return sku_normalize(str(args.get("model", "")))
    if short == "attribute_extract":
        return attribute_extract(str(args.get("text", "")))
    if short == "compatibility_check":
        return compatibility_check(str(args.get("model", "")), str(args.get("requirement", "")),
                                   str(args.get("ref", "")))
    if short == "substitute_rank":
        return substitute_rank(str(args.get("model", "")), str(args.get("scenario", "")))
    if short == "requirement_match":
        return requirement_match(str(args.get("requirement", "")), str(args.get("ref", "")),
                                 limit=args.get("limit", 8))
    if short == "spec_gap_detect":
        return spec_gap_detect(str(args.get("model", "")), str(args.get("requirement", "")),
                               str(args.get("ref", "")))
    if short == "wrong_model_risk":
        return wrong_model_risk(str(args.get("model", "")), str(args.get("scenario", "")),
                                str(args.get("requirement", "")))
    if short == "certification_check":
        return certification_check(str(args.get("model", "")), str(args.get("scenario", "")),
                                   str(args.get("region", "")))
    if short == "bundle_suggestion":
        return bundle_suggestion(str(args.get("model", "")))
    if short == "quote_spec_guardrail":
        q = args.get("quote")
        return quote_spec_guardrail(q if isinstance(q, list) else [],
                                    str(args.get("requirement", "")))
    if short == "quote_draft":
        return quote_draft(str(args.get("requirement", "")))
    if short == "evidence_pack":
        return evidence_pack(str(args.get("model", "")))
    if short == "explain_match":
        return explain_match(str(args.get("model", "")), str(args.get("requirement", "")),
                             str(args.get("ref", "")))
    if short == "get_cost":
        return get_cost(str(args.get("model", "")))
    if short == "audit":
        return audit()
    if short == "status":
        return status()
    return {"ok": False, "error": "unreachable"}  # pragma: no cover


# ────────────────────────────────────────────────────────────────────────────── #
# _selftest() — 全模块 AST 静态扫(含动态绕过)+ 行为验证。返回 0 = 全过。
# ────────────────────────────────────────────────────────────────────────────── #
_FORBIDDEN_EXEC_MODULES = (
    "email_tools", "google_tools", "gcal_oauth",          # 凭据读取(硬拦)
    "computer_use", "screen", "eyes", "ears", "nexus", "appsmith", "self_improve",
    "hive", "swarm", "agent_swarm", "claude_code", "fleet", "skillforge",
    "simulation_lab", "autopilot", "autopilot_store", "server", "subprocess",
)
_FORBIDDEN_ATTRS = {
    "run_shell", "write_file", "move_file", "open_path", "applescript",
    "type_text", "click_on", "send", "nexus_send", "enqueue_approval", "approve",
    "calendar_create", "calendar_update", "calendar_delete",
    "gmail_read", "gmail_recent", "gmail_send", "send_email",
    "_creds", "_access_token", "system", "popen",
    "remove", "unlink", "rmdir", "rmtree",
}
_FORBIDDEN_CALLS = {"system", "popen", "remove", "unlink", "rmdir", "rmtree",
                    "Popen", "check_output", "check_call", "getoutput", "getstatusoutput"}


def _selftest_cleanup(tmp: str) -> None:
    """仅做临时目录清理(被 AST 自检**单独豁免**的唯一函数;绝不放任何别的逻辑进来)。"""
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _selftest() -> int:
    """安全不变量自检(扫**整模块**源码, 不豁免 _selftest 自身, 防执行面藏身):
       ① 不直接 / 动态 import 任何凭据 / 执行面模块; ② 无执行 / 凭据方法调用(含 getattr 绕过);
       ③ 无危险 stdlib; ④ 源码无 confirmed=True; ⑤ 模型白名单只读(ingest_spec 不在内); ⑥ 行为验证。
    """
    import ast
    import inspect
    import tempfile

    fails: list = []
    src = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(src)

    bad_imports: set = set()
    bad_dyn: set = set()
    bad_attrs: set = set()
    bad_calls: set = set()
    confirmed_true = False

    class _Scan(ast.NodeVisitor):
        def visit_FunctionDef(self, node):  # noqa: N802
            if node.name == "_selftest_cleanup":
                return
            self.generic_visit(node)

        def visit_Import(self, node):  # noqa: N802
            for a in node.names:
                if a.name.split(".")[0] in _FORBIDDEN_EXEC_MODULES:
                    bad_imports.add(a.name.split(".")[0])
            self.generic_visit(node)

        def visit_ImportFrom(self, node):  # noqa: N802
            if (node.module or "").split(".")[0] in _FORBIDDEN_EXEC_MODULES:
                bad_imports.add((node.module or "").split(".")[0])
            # `from os import system` / `from local_powers import run_shell` 这类裸名导入也拦。
            for a in node.names:
                if a.name in _FORBIDDEN_ATTRS or a.name in _FORBIDDEN_CALLS:
                    bad_attrs.add("from-import:" + a.name)
            self.generic_visit(node)

        def visit_Attribute(self, node):  # noqa: N802
            if node.attr in _FORBIDDEN_ATTRS:
                bad_attrs.add(node.attr)
            self.generic_visit(node)

        def visit_Call(self, node):  # noqa: N802
            nonlocal confirmed_true
            for kw in node.keywords:
                if (kw.arg == "confirmed" and isinstance(kw.value, ast.Constant)
                        and kw.value.value is True):
                    confirmed_true = True
            f = node.func
            fname = None
            if isinstance(f, ast.Name):
                fname = f.id
            elif isinstance(f, ast.Attribute):
                fname = f.attr
            if isinstance(f, ast.Attribute) and f.attr in _FORBIDDEN_CALLS:
                bad_calls.add(f.attr)
            if isinstance(f, ast.Name) and f.id in ("exec", "eval", "__import__", "compile"):
                bad_calls.add(f.id)
            # 裸名调用危险函数(如 `from os import system` 后直接 system(...))也拦。
            if isinstance(f, ast.Name) and (f.id in _FORBIDDEN_CALLS or f.id in _FORBIDDEN_ATTRS):
                bad_calls.add(f.id)
            if fname in ("_opt", "import_module", "__import__"):
                if (node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    root = node.args[0].value.split(".")[0]
                    if root in _FORBIDDEN_EXEC_MODULES:
                        bad_dyn.add(root)
            if (fname == "getattr" and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)):
                if node.args[1].value in _FORBIDDEN_ATTRS:
                    bad_attrs.add("getattr:" + node.args[1].value)
            self.generic_visit(node)

    _Scan().visit(tree)

    for m in sorted(bad_imports):
        fails.append("① 直接 import 凭据/执行面模块: " + m)
    for m in sorted(bad_dyn):
        fails.append("① 动态 import(_opt/importlib)凭据/执行面模块: " + m)
    for a in sorted(bad_attrs):
        fails.append("② 出现执行/凭据方法调用: " + a)
    for c in sorted(bad_calls):
        fails.append("③ 出现危险 stdlib 调用: " + c)
    if confirmed_true:
        fails.append("④ 源码出现 confirmed=True")
    for leaked in _CLI_ONLY:
        if leaked in _DISPATCH_WHITELIST:
            fails.append("⑤ 模型白名单泄漏写库动作: " + leaked)

    # ⑥ 行为验证(临时库 + 注入 + 凭据/隐私收口断言; 强制开启数据层隐私开关测全链路)。
    tmp = tempfile.mkdtemp(prefix="psh_selftest_")
    db = os.path.join(tmp, "psh.db")
    _orig_cfg_get = config.get
    config.get = lambda k, fb=None: True if k == "safety.data_layer_privacy_enabled" else _orig_cfg_get(k, fb)
    try:
        # 凭据脱敏:落库正文绝不留 secret 明文。
        ig = ingest_spec(model="M-996", text="高耐候型 户外涂料专用 PC2含量≥98% 白度96 "
                         "内部 api_key=sk-ABCDEFGH1234567890XYZ 别外传",
                         category="工业树脂", certs=["REACH", "RoHS"], source="spec_sheet", db_path=db)
        if not ig.get("ok"):
            fails.append("⑥ ingest_spec 失败: " + str(ig))
        leak = 0
        c = _conn(db)
        try:
            for tbl, col in (("ps_products", "summary"), ("ps_attributes", "value"),
                             ("ps_requirements", "need_text")):
                for (v,) in c.execute(f"SELECT {col} FROM {tbl}"):
                    if v and ("sk-ABCDEFGH" in v or _SECRET_RX.search(_norm(v))):
                        leak += 1
        finally:
            c.close()
        if leak:
            fails.append("⑥ 凭据明文漏进落库正文(脱敏失效)")

        # AKIA 下划线边界。
        if "AKIAIOSFODNN7EXAMPLE" in _redact("env AWS_AKIAIOSFODNN7EXAMPLE_KEY=x"):
            fails.append("⑥ 下划线相邻的 AKIA 密钥脱敏失效")
        # base64 含 / 的高熵密钥。
        if "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" in _redact("secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"):
            fails.append("⑥ 含 / 的 base64 高熵密钥脱敏失效")

        # 私密内容(报价 / 客户)→ private + force_local。
        ig2 = ingest_spec(model="M-902", text="给某客户的底价报价 12000/吨, 内部成本价别外泄",
                          source="quote", db_path=db)
        if not ig2.get("_private"):
            fails.append("⑥ 私密(报价)内容未标 _private")

        # 批量同型号同秒不撞主键。
        try:
            for _i in range(5):
                ingest_spec(model="M-996", text="批次第%d次更新 PC2含量98.%d" % (_i, _i),
                            source="manual", db_path=db)
        except Exception as e:
            fails.append("⑥ 批量 ingest 撞 PRIMARY KEY: " + str(e))

        # 录入一个禁配场景型号 + 通用型, 测硬不兼容。
        ingest_spec(model="A-100", text="通用型 PC2含量98% 不能用于户外", category="工业树脂",
                    scenarios=[{"kind": "forbidden", "scenario": "户外"}],
                    attrs=[{"name": "晶型", "value": "通用型"}], source="spec_sheet", db_path=db)

        # 核心匹配可跑。
        rm = requirement_match(requirement="户外涂料 要高耐候型 PC2≥98% 白度≥95 REACH", db_path=db)
        if not rm.get("ok"):
            fails.append("⑥ requirement_match 失败: " + str(rm))
        # M-996(高耐候+REACH+户外)应进 qualified, A-100(通用+禁户外)应被 disqualified。
        quals = [q["model"] for q in rm.get("qualified", [])]
        disq = [d["model"] for d in rm.get("disqualified", [])]
        if not any("996" in q for q in quals):
            fails.append("⑥ 应匹配的高耐候型未进 qualified")
        if not any("100" in d for d in disq):
            fails.append("⑥ 禁户外/通用型未被 disqualified")

        # wrong_model_risk: A-100 用于户外应报 high。
        wm = wrong_model_risk("A-100", scenario="户外", db_path=db)
        if not wm.get("ok") or wm.get("risk_level") != "high":
            fails.append("⑥ wrong_model_risk 未对禁配场景报 high")

        # compatibility_check / spec_gap / explain / evidence / cert 可跑。
        for fn, kw in ((compatibility_check, {"model": "M-996", "requirement": "PC2≥98% 户外 REACH"}),
                       (spec_gap_detect, {"model": "M-996", "requirement": "白度≥99 吸油量≤20"}),
                       (explain_match, {"model": "M-996", "requirement": "户外 高耐候型"}),
                       (evidence_pack, {"model": "M-996"}),
                       (certification_check, {"model": "M-996", "scenario": "户外"}),
                       (substitute_rank, {"model": "M-996"}),
                       (sku_normalize, {"model": "m 996"}),
                       (attribute_extract, {"text": "PC2≥98% 白度96 REACH 户外"}),
                       (audit, {}), (status, {})):
            r = fn(db_path=db, **kw)
            if not isinstance(r, dict) or not r.get("ok"):
                fails.append("⑥ 只读动作失败: " + getattr(fn, "__name__", "?") + " -> " + str(r)[:120])

        # sku_normalize 把 m 996 收敛到 M996 并命中 M-996。
        sn = sku_normalize("m-9 9 6", db_path=db)
        if sn.get("normalized_sku") != "M996":
            fails.append("⑥ sku_normalize 规范化错: " + str(sn.get("normalized_sku")))

        # missing_data ≠ below:要求一个产品没有的参数应标 missing_data 不当不达标。
        sg = spec_gap_detect("M-996", requirement="电阻率≥1000", db_path=db)
        if not any("电阻率" in (m.get("name") or "") for m in sg.get("missing_data", [])):
            fails.append("⑥ 缺参数应标 missing_data 而非臆断")

        # audit 凭据收口为 0。
        au = audit(db_path=db)
        if au.get("credential_leak_rows", 1) != 0:
            fails.append("⑥ audit 自检报告凭据漏网")

        # 白名单闸:模型路径调不到 ingest_spec/set_cost, 也调不到白名单外动作。
        if dispatch("product_spec_hub_ingest_spec", {"model": "x"}).get("ok"):
            fails.append("⑤ dispatch 竟放行 ingest_spec(写库)")
        if dispatch("product_spec_hub_set_cost", {"model": "M-902", "cost_rmb_per_ton": "12000"}).get("ok"):
            fails.append("⑤ dispatch 竟放行 set_cost(写库业务敏感)")
        if not dispatch("product_spec_hub_status", {}).get("ok"):
            fails.append("⑤ dispatch 应放行只读 status")
        if dispatch("product_spec_hub_run_shell", {}).get("ok"):
            fails.append("⑤ dispatch 竟放行白名单外动作")
        if not is_tool("product_spec_hub_requirement_match") or is_tool("product_spec_hub_ingest_spec"):
            fails.append("⑤ is_tool 白名单判定错")
        if is_tool("product_spec_hub_set_cost"):
            fails.append("⑤ is_tool 不应识别 set_cost(写库业务敏感)")
        if not is_tool("product_spec_hub_get_cost"):
            fails.append("⑤ is_tool 应识别 get_cost(只读)")

        # —— 成本台账 set_cost/get_cost 往返 ——
        sc1 = set_cost("M-996", "15000", cost_date="2026-06-14", db_path=db)
        if not sc1.get("ok"):
            fails.append("⑥ [cost] set_cost 失败: " + str(sc1))
        if not sc1.get("_private"):
            fails.append("⑥ [cost] set_cost 返回值未标 _private")
        gc1 = get_cost("M-996", db_path=db)
        if not gc1.get("ok") or not gc1.get("found"):
            fails.append("⑥ [cost] get_cost 查不到刚录入的成本: " + str(gc1))
        if gc1.get("cost_rmb_per_ton") != "15000":
            fails.append("⑥ [cost] get_cost 成本价往返错: " + str(gc1.get("cost_rmb_per_ton")))
        if not gc1.get("_private"):
            fails.append("⑥ [cost] get_cost 返回值未标 _private")
        # 写法变体(M996 vs M-996)也能查到。
        gc2 = get_cost("M996", db_path=db)
        if not gc2.get("ok") or not gc2.get("found"):
            fails.append("⑥ [cost] get_cost SKU 变体查不到: " + str(gc2))
        # 查不到的型号 → found=False, 不报错。
        gc_miss = get_cost("NOEXIST-XYZ-9999", db_path=db)
        if not gc_miss.get("ok") or gc_miss.get("found"):
            fails.append("⑥ [cost] get_cost 未知型号未返回 found=False: " + str(gc_miss))
        # 幂等:重复 set_cost 不崩、值被更新。
        sc2 = set_cost("M-996", "16000", cost_date="2026-06-15", db_path=db)
        if not sc2.get("ok"):
            fails.append("⑥ [cost] set_cost 幂等更新失败: " + str(sc2))
        gc3 = get_cost("M-996", db_path=db)
        if gc3.get("cost_rmb_per_ton") != "16000":
            fails.append("⑥ [cost] set_cost 幂等更新后 get_cost 未反映新值: " + str(gc3))
        # 只读 dispatch 路径可调用 get_cost(返回 ok=True 即可; dispatch 走默认库路径, 非测试临时库)。
        gcd = dispatch("product_spec_hub_get_cost", {"model": "M-996"})
        if not gcd.get("ok"):
            fails.append("⑥ [cost] dispatch get_cost 返回非 ok: " + str(gcd))
        # 非法成本价拒绝。
        bad_cost = set_cost("M-996", "nan", db_path=db)
        if bad_cost.get("ok"):
            fails.append("⑥ [cost] set_cost 接受了非法 nan 成本价")
        neg_cost = set_cost("M-996", "-100", db_path=db)
        if neg_cost.get("ok"):
            fails.append("⑥ [cost] set_cost 接受了负数成本价")
        # 对抗评审 FIX: 既有公开产品录成本价后, ps_products.private 必须升到 1
        # (否则 audit/recent_audit 把成本明文经 LLM 工具上云)。
        ingest_spec(model="PUBCOST-1", text="二基础聚合物 高耐候型 含量99%",
                    category="工业树脂", db_path=db)
        _cc = _conn(db)
        _pr = _cc.execute("SELECT id, private FROM ps_products "
                          "WHERE model LIKE 'PUBCOST%' OR sku LIKE 'PUBCOST%'").fetchone()
        _cc.close()
        if _pr and _pr[1] == 0:   # 确认 ingest 的产品本是公开
            set_cost("PUBCOST-1", "14500", db_path=db)
            _cc2 = _conn(db)
            _after = _cc2.execute("SELECT private FROM ps_products WHERE id=?", (_pr[0],)).fetchone()
            _cc2.close()
            if not _after or _after[0] != 1:
                fails.append("⑥ [cost] 既有公开产品录成本后 private 未升到 1(成本明文会上云)")

        # 图片凭据路径硬拦。
        bad = ingest_spec(model="X", image="/Users/x/.gmail_creds.json", db_path=db)
        if bad.get("ok"):
            fails.append("② 图片凭据路径未被硬拦")

        # NaN/inf 不污染:limit 传 NaN/inf 不崩, 数值比较缺值绝不当 pass(真断言, 非空 pass)。
        rm_nan = requirement_match(requirement="PC2含量≥98", limit=float("nan"), db_path=db)
        if not rm_nan.get("ok"):
            fails.append("⑥ limit=NaN 未被 _as_int 兜底")
        if _cmp_numeric(">=", float("inf"), 98) is not None:
            fails.append("⑥ _cmp_numeric 未拦 inf(应返回 None)")
        if _cmp_numeric(">=", float("nan"), 98) is not None:
            fails.append("⑥ _cmp_numeric 未拦 NaN(应返回 None)")
        if _as_int(float("nan"), 8, 1, 50) != 8 or _as_int(float("inf"), 8, 1, 50) != 8:
            fails.append("⑥ _as_int 未拦 NaN/inf")

        # —— 回归: 审查发现的 CRITICAL/HIGH 修复 ——
        # (C1) PC2 含量解析: 'PC2含量≥98%' / 'PC2≥98%' 必须解析成 98, 不是型号里的 2。
        for t in ("PC2含量≥98%", "PC2≥98%", "PC2 content >= 98%"):
            pa = _parse_spec_text(t)["attrs"]
            pc2 = [a for a in pa if a["name"] == "PC2含量"]
            if not pc2 or abs((pc2[0]["value"] or 0) - 98) > 0.01:
                fails.append("⑥ [C1] PC2 含量解析错(应=98): " + t + " -> " + str(pc2))
        # 端到端: 90% 含量产品不得被 'PC2含量≥98%' 判 qualified('完全匹配')。
        ingest_spec(model="LOW-90", text="高耐候型 PC2含量90%", category="工业树脂",
                    source="spec_sheet", db_path=db)
        rmc = requirement_match(requirement="PC2含量≥98%", db_path=db)
        if any(q["model"] == "LOW-90" for q in rmc.get("qualified", [])):
            fails.append("⑥ [C1] 90% 低档产品被 PC2≥98% 误判 qualified")
        # (C2) wrong_model_risk 私密易混产品 → _private 置位。
        ingest_spec(model="M-998", text="给某重点客户的底价报价 内部专供 通用型",
                    category="工业树脂", attrs=[{"name": "晶型", "value": "通用型"}],
                    source="quote", db_path=db)
        ingest_spec(model="M-996", category="工业树脂",
                    attrs=[{"name": "晶型", "value": "高耐候型"}], source="manual", db_path=db)
        wmp = wrong_model_risk("M-996", db_path=db)
        leaked_priv = [c for c in wmp.get("confusable_models", []) if "998" in (c.get("model") or "")]
        if leaked_priv and not wmp.get("_private"):
            fails.append("⑥ [C2] wrong_model_risk 暴露私密易混型号却未标 _private")
        # (H) 解析器子串碰撞: '水分含量' 不得凭空多出 PC2含量; 'ce' 不得命中 service。
        wp = _parse_spec_text("水分含量≤0.5%")["attrs"]
        if any(a["name"] == "PC2含量" for a in wp):
            fails.append("⑥ [H] '水分含量' 误归 PC2含量(子串碰撞)")
        if "CE" in _parse_spec_text("our service and price are good")["certs"]:
            fails.append("⑥ [H] 'ce' 子串误命中 service 为 CE 认证")
        # (H) '不能用于食品级' → forbidden 场景, 不应同时要求 FoodGrade 认证。
        neg = _parse_spec_text("通用型 不能用于食品级")
        if "FoodGrade" in neg["certs"]:
            fails.append("⑥ [H] 否定上下文的食品级被误当认证要求")
        if not any(s["kind"] == "forbidden" and s["scenario"] == "食品级" for s in neg["scenarios"]):
            fails.append("⑥ [H] '不能用于食品级' 未识别为 forbidden 场景")
        # (H) 全 missing_data 产品不得判 compatible/qualified。
        ingest_spec(model="BARE-1", category="工业树脂",
                    attrs=[{"name": "白度", "value": "96", "numeric": 96}], source="manual", db_path=db)
        cc_miss = compatibility_check("BARE-1", requirement="电阻率≥1000 pH值≥7", db_path=db)
        if cc_miss.get("ok") and cc_miss.get("compatible"):
            fails.append("⑥ [H] 全缺数据需求的产品被判 compatible")
        # (M) cert/scenario 幂等去重: 同型号重复 ingest 同认证不累积。
        for _ in range(3):
            ingest_spec(model="DEDUP-1", certs=["REACH"],
                        scenarios=[{"kind": "applicable", "scenario": "户外"}],
                        source="manual", db_path=db)
        cdup = _conn(db)
        try:
            nc = cdup.execute("SELECT COUNT(*) FROM ps_certs WHERE product_id=?",
                              (_pid("DEDUP-1"),)).fetchone()[0]
        finally:
            cdup.close()
        if nc != 1:
            fails.append("⑥ [M] cert 重复 ingest 未去重, 累积 %d 行" % nc)
        # (L) ingest 标量入参(非 list)不崩。
        sc = ingest_spec(model="SCALAR-1", attrs="123", certs=5, source="manual", db_path=db)
        if not sc.get("ok"):
            fails.append("⑥ [L] ingest 标量 attrs/certs 入参崩了: " + str(sc))

        # —— 第二轮审查回归(修复引入的回归 + 漏网) ——
        # (R-否定前向泄漏) '不要REACH 需要RoHS' 必须保留 RoHS。
        c2 = _parse_spec_text("不要REACH 需要RoHS")["certs"]
        if "RoHS" not in c2 or "REACH" in c2:
            fails.append("⑥ [R] 否定前向泄漏: '不要REACH 需要RoHS' -> " + str(c2))
        sc2 = _parse_spec_text("禁用于食品级 适用于涂料")["scenarios"]
        if not any(s["kind"] == "applicable" and s["scenario"] == "涂料" for s in sc2):
            fails.append("⑥ [R] 否定前向泄漏(场景): 涂料应 applicable -> " + str(sc2))
        # (R-否定覆盖) '不用于食品级' / '非户外用' 应为 forbidden。
        for t, scen in (("不用于食品级", "食品级"), ("无法用于食品级", "食品级")):
            ss = _parse_spec_text(t)["scenarios"]
            if not any(s["kind"] == "forbidden" and s["scenario"] == scen for s in ss):
                fails.append("⑥ [R] 否定覆盖漏: '%s' -> %s" % (t, ss))
        # (R-纯场景需求不全匹配) 塑料用产品不应被'涂料'需求判完全匹配。
        ingest_spec(model="FORPLASTIC", attrs=[{"name": "PC2含量", "value": "95", "numeric": 95}],
                    scenarios=[{"kind": "applicable", "scenario": "塑料"}],
                    category="工业树脂", source="spec_sheet", db_path=db)
        cc_scen = compatibility_check("FORPLASTIC", requirement="涂料应用", db_path=db)
        if cc_scen.get("ok") and cc_scen.get("compatible"):
            fails.append("⑥ [R] 塑料用产品被'涂料'需求误判 compatible")
        # (R-私密降级) 私密认证再以非私密重录, private 不得 1->0。
        ingest_spec(model="PRIVCERT", certs=[{"cert": "REACH", "region": "欧洲大客户专供报价市场"}],
                    text="底价机密", source="quote", db_path=db)
        ingest_spec(model="PRIVCERT", certs=[{"cert": "REACH"}], text="白度96", source="manual", db_path=db)
        cdb = _conn(db)
        try:
            pv_after = cdb.execute("SELECT private FROM ps_certs WHERE product_id=? AND cert='REACH'",
                                   (_pid("PRIVCERT"),)).fetchone()
        finally:
            cdb.close()
        if pv_after is None or int(pv_after[0]) != 1:
            fails.append("⑥ [R] OR REPLACE 把 cert private 降级 1->0: " + str(pv_after and pv_after[0]))
        # (R-ReDoS) _HIENTROPY_RX 对长串不卡(有界量词)——粗略时间健全性。
        _ = _redact("1-" * 5000)  # 不应挂起(有界后亚秒级)
        # (域) '工业树脂含量≥98%' 必须解析成 98。
        tb = [a for a in _parse_spec_text("工业树脂含量≥98%")["attrs"] if a["name"] == "PC2含量"]
        if not tb or abs((tb[0]["value"] or 0) - 98) > 0.01:
            fails.append("⑥ [域] '工业树脂含量≥98%' 解析错: " + str(tb))
        # (域) 单位告警对 lower-better 不误报: 水分0.5 vs ≤12% 应 met。
        ingest_spec(model="DRYP", attrs=[{"name": "水分", "value": "0.5", "numeric": 0.5, "unit": "%"}],
                    category="工业树脂", source="spec_sheet", db_path=db)
        gd = compatibility_check("DRYP", requirement="水分≤12%", db_path=db)
        if not any(m.get("status") == "met" and "水分" in (m.get("name") or "") for m in gd.get("met", [])):
            fails.append("⑥ [域] 水分0.5/≤12% 被误判非 met: " + str(gd.get("met")))
        # (M-无法解析需求) 纯无规格文本不得判完全匹配。
        up = compatibility_check("DRYP", requirement="你好啊随便聊聊", db_path=db)
        if up.get("ok"):
            fails.append("⑥ [M] 无法解析的需求竟判出匹配: " + str(up.get("verdict")))

        # —— 第三轮收敛回归 ——
        # (R3-空格否定) 否定词与目标间有空格时, 否定**不得**丢失(forbidden 不被反转成 applicable)。
        for t, scen in (("不能用于 户外", "户外"), ("not for outdoor", "户外"),
                        ("禁用于 食品级", "食品级"), ("不推荐 户外", "户外")):
            ss = _parse_spec_text(t)["scenarios"]
            if not any(s["kind"] == "forbidden" and s["scenario"] == scen for s in ss):
                fails.append("⑥ [R3] 空格否定丢失: '%s' -> %s" % (t, ss))
        if _parse_spec_text("不要 REACH")["certs"]:
            fails.append("⑥ [R3] '不要 REACH' 否定丢失(认证未丢弃)")
        # 前向泄漏仍被 _POS_CUE_RX 正确处理(不回退)。
        if "RoHS" not in _parse_spec_text("不要REACH 需要RoHS")["certs"]:
            fails.append("⑥ [R3] 去空格后前向泄漏保护回退")
        # 句号小句边界仍阻断跨句否定。
        sdot = _parse_spec_text("不要室内。户外专用")["scenarios"]
        if not any(s["kind"] == "applicable" and s["scenario"] == "户外" for s in sdot):
            fails.append("⑥ [R3] 句号小句边界失效: " + str(sdot))
        # 端到端: '不能用于 食品级' 工业型号不得被食品级需求判 qualified。
        ingest_spec(model="IND-50", text="工业级 高耐候型 PC2含量98% 不能用于 食品级",
                    category="工业树脂", source="spec_sheet", db_path=db)
        rm_food = requirement_match(requirement="食品级应用 高耐候型", db_path=db)
        if any(q["model"] == "IND-50" for q in rm_food.get("qualified", [])):
            fails.append("⑥ [R3] '不能用于 食品级' 工业型号被食品级需求误判 qualified")
        # (R3-场景埋没) 全规格高耐候型(无场景标签)对'户外'需求应进 qualified(晶型 met-credit)。
        ingest_spec(model="HOMO-OUT", category="工业树脂",
                    attrs=[{"name": "晶型", "value": "高耐候型"},
                           {"name": "PC2含量", "value": "98", "numeric": 98}],
                    source="spec_sheet", db_path=db)
        rm_out = requirement_match(requirement="户外涂料用工业树脂", db_path=db)
        if not any(q["model"] == "HOMO-OUT" for q in rm_out.get("qualified", [])):
            fails.append("⑥ [R3] 高耐候型产品对户外需求被埋没(未进 qualified): "
                         + str([q["model"] for q in rm_out.get("qualified", [])]))
        # 通用型对户外仍硬不兼容(安全闸不被 met-credit 绕过)。
        ingest_spec(model="ANA-OUT", category="工业树脂",
                    attrs=[{"name": "晶型", "value": "通用型"}], source="spec_sheet", db_path=db)
        cc_ana = compatibility_check("ANA-OUT", requirement="户外", db_path=db)
        if cc_ana.get("ok") and cc_ana.get("compatible"):
            fails.append("⑥ [R3] 通用型对户外竟判 compatible(晶型安全闸失效)")

        # —— 第四轮收敛回归 ——
        # (R4-须误命中) '禁用须知 户外' 的否定**不得**被 '须知' 误重置(under-forbid)。
        for t in ("禁用须知 户外", "禁止无须户外"):
            ss = _parse_spec_text(t)["scenarios"]
            if not any(s["kind"] == "forbidden" and s["scenario"] == "户外" for s in ss):
                fails.append("⑥ [R4] '须'子串误重置否定: '%s' -> %s" % (t, ss))
        # '必须' 正当前向泄漏仍生效。
        mr = _parse_spec_text("不可室内 必须户外")["scenarios"]
        if not any(s["kind"] == "applicable" and s["scenario"] == "户外" for s in mr):
            fails.append("⑥ [R4] '必须' 前向泄漏重置被误删: " + str(mr))
        # (R4-纯uncertain不塌分) 纯无规则未登记场景的优质产品 score 须 >0 且 > 真实 below 产品。
        ingest_spec(model="UNREG-GOOD", category="工业树脂",
                    attrs=[{"name": "PC2含量", "value": "99", "numeric": 99}], source="spec_sheet", db_path=db)
        ingest_spec(model="BELOW-BAD", category="工业树脂",
                    attrs=[{"name": "PC2含量", "value": "80", "numeric": 80}], source="spec_sheet", db_path=db)
        s_unc = compatibility_check("UNREG-GOOD", requirement="室内", db_path=db).get("score", -1)
        s_bel = compatibility_check("BELOW-BAD", requirement="PC2含量≥95", db_path=db).get("score", -1)
        if not (s_unc > 0 and s_unc > s_bel):
            fails.append("⑥ [R4] 纯 uncertain 场景塌分: unreg=%s below=%s" % (s_unc, s_bel))

        # —— 轮8: quote_draft 报价准备草稿 ——
        qd = quote_draft(requirement="客户要PC2含量≥98%高耐候型工业树脂 用于户外涂料 需REACH认证", db_path=db)
        if not qd.get("ok"):
            fails.append("⑥ [Q8] quote_draft 失败: " + str(qd)[:160])
        # 红线: 报价对外, needs_review 恒 True, 绝不自动发送(disclaimer 含'不外发'承诺)。
        if qd.get("needs_review") is not True:
            fails.append("⑥ [Q8] quote_draft needs_review 未恒为 True")
        if "外发" not in (qd.get("disclaimer") or ""):
            fails.append("⑥ [Q8] quote_draft disclaimer 缺'不外发'承诺")
        # 推荐型号:高耐候型户外达标的 M-996 应进推荐(qualified 优先)。
        rec_models = [r.get("model") for r in qd.get("recommended_models", [])]
        if not any("996" in (m or "") for m in rec_models):
            fails.append("⑥ [Q8] quote_draft 未推荐达标的高耐候户外型号(M-996): " + str(rec_models))
        # 通用禁户外型号(A-100)绝不被推荐(硬伤不进 recommended)。
        if any("100" in (m or "") for m in rec_models):
            fails.append("⑥ [Q8] quote_draft 误推荐禁户外/通用型号: " + str(rec_models))
        # checklist 必含'不自动发送'与'不定价'防呆。
        cl_blob = " ".join(qd.get("quote_checklist", []))
        if "自动发送" not in cl_blob or "定价" not in cl_blob:
            fails.append("⑥ [Q8] quote_draft checklist 缺对外/定价防呆: " + cl_blob[:160])
        # 结构完整(报价准备草稿七件套)。
        for k in ("requirement", "recommended_models", "spec_match", "cert_status",
                  "risk_flags", "quote_checklist", "draft_note"):
            if k not in qd:
                fails.append("⑥ [Q8] quote_draft 缺字段: " + k)
        # 私密传播: 私密(报价/底价)需求 → 整条草稿标 _private(_force_local 由 allow_cloud_private 配置门控)。
        qd_priv = quote_draft(requirement="给某重点客户的底价专供报价 高耐候型 PC2≥98% 户外", db_path=db)
        if not qd_priv.get("_private"):
            fails.append("⑥ [Q8] 私密报价需求未标 _private")
        # 无规格的闲聊需求 → 拒判, 仍标 needs_review(不臆造报价)。
        qd_bad = quote_draft(requirement="你好啊在吗", db_path=db)
        if qd_bad.get("ok"):
            fails.append("⑥ [Q8] quote_draft 对无规格闲聊竟判出草稿")
    except Exception as e:
        import traceback
        fails.append("⑥ 行为验证抛异常: " + str(e) + " | " + traceback.format_exc()[-400:])
    finally:
        config.get = _orig_cfg_get
        _selftest_cleanup(tmp)

    if fails:
        for f in fails:
            print("FAIL: " + f)
        return 1
    print("OK product_spec_hub selftest 全过(纯只读顾问 + 零真实副作用):")
    print("  ① 不直接 / 动态 import 任何凭据 / 执行面模块 ✓")
    print("  ② 源码无执行 / 凭据方法调用(含 getattr 绕过)+ 图片凭据路径硬拦 ✓")
    print("  ③ 无危险 stdlib 调用 ✓")
    print("  ④ 源码无 confirmed=True ✓")
    print("  ⑤ 模型白名单只读, ingest_spec 等写库不暴露给模型 ✓")
    print("  ⑥ 行为验证 + 凭据 / 隐私收口 + 匹配 / 配错 / NaN 护栏断言通过 ✓")
    return 0


# ────────────────────────────────────────────────────────────────────────────── #
# CLI(argparse)
# ────────────────────────────────────────────────────────────────────────────── #
def _emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _main(argv: list) -> int:
    p = argparse.ArgumentParser(
        prog="product_spec_hub",
        description="产品规格与兼容替代知识中枢(纯只读顾问 / 零执行)")
    sub = p.add_subparsers(dest="cmd")

    pi = sub.add_parser("ingest_spec", help="录入脱敏产品 / 需求资料(CLI / 内部用)")
    pi.add_argument("model", nargs="?", default="")
    pi.add_argument("--text", default="")
    pi.add_argument("--category", default="")
    pi.add_argument("--brand", default="")
    pi.add_argument("--source", default="manual")
    pi.add_argument("--image", default="")
    pi.add_argument("--attrs", default="", help="JSON: [{name,value,unit,numeric}]")
    pi.add_argument("--certs", default="", help="JSON: [{cert,status,region}] 或 [\"REACH\"]")
    pi.add_argument("--scenarios", default="", help="JSON: [{kind,scenario,note}]")
    pi.add_argument("--relations", default="", help="JSON: [{to,rtype,reason,confidence}]")
    pi.add_argument("--requirement", default="", help="JSON: {ref,need_text}")

    cc = sub.add_parser("compatibility_check"); cc.add_argument("model"); cc.add_argument("--requirement", default=""); cc.add_argument("--ref", default="")
    sr = sub.add_parser("substitute_rank"); sr.add_argument("model"); sr.add_argument("--scenario", default="")
    rm = sub.add_parser("requirement_match"); rm.add_argument("--requirement", default=""); rm.add_argument("--ref", default=""); rm.add_argument("--limit", default=8, type=int)
    sg = sub.add_parser("spec_gap_detect"); sg.add_argument("model"); sg.add_argument("--requirement", default=""); sg.add_argument("--ref", default="")
    wm = sub.add_parser("wrong_model_risk"); wm.add_argument("model"); wm.add_argument("--scenario", default=""); wm.add_argument("--requirement", default="")
    ck = sub.add_parser("certification_check"); ck.add_argument("model"); ck.add_argument("--scenario", default=""); ck.add_argument("--region", default="")
    bs = sub.add_parser("bundle_suggestion"); bs.add_argument("model")
    ep = sub.add_parser("evidence_pack"); ep.add_argument("model")
    em = sub.add_parser("explain_match"); em.add_argument("model"); em.add_argument("--requirement", default=""); em.add_argument("--ref", default="")
    sn = sub.add_parser("sku_normalize"); sn.add_argument("model")
    ae = sub.add_parser("attribute_extract"); ae.add_argument("text")
    qg = sub.add_parser("quote_spec_guardrail"); qg.add_argument("--quote", default="", help="JSON: [{model,scenario}]"); qg.add_argument("--requirement", default="")
    qd = sub.add_parser("quote_draft", help="客户规格需求 -> 报价准备草稿(只读, 不定价, 不外发)"); qd.add_argument("--requirement", default="")
    sc_set = sub.add_parser("set_cost", help="录入/更新型号含税采购成本价(CLI-only)")
    sc_set.add_argument("model", help="型号 / SKU")
    sc_set.add_argument("cost", help="含税采购价 RMB/吨")
    sc_set.add_argument("--date", default="", dest="cost_date", help="录入日期 YYYY-MM-DD(不填取今日)")
    sc_set.add_argument("--currency", default="CNY", dest="cost_currency")
    sc_set.add_argument("--source", default="manual")
    sc_get = sub.add_parser("get_cost", help="查询型号含税采购成本价(只读)")
    sc_get.add_argument("model")
    sub.add_parser("audit")
    sub.add_parser("status")
    sub.add_parser("selftest")

    if not argv:
        _emit(status())
        return 0
    args = p.parse_args(argv)
    cmd = args.cmd

    if cmd == "ingest_spec":
        _emit(ingest_spec(model=args.model, text=args.text, category=args.category,
                          brand=args.brand, source=args.source, image=args.image,
                          attrs=_loads(args.attrs, None), certs=_loads(args.certs, None),
                          scenarios=_loads(args.scenarios, None),
                          relations=_loads(args.relations, None),
                          requirement=_loads(args.requirement, None)))
    elif cmd == "compatibility_check":
        _emit(compatibility_check(args.model, args.requirement, args.ref))
    elif cmd == "substitute_rank":
        _emit(substitute_rank(args.model, args.scenario))
    elif cmd == "requirement_match":
        _emit(requirement_match(args.requirement, args.ref, limit=args.limit))
    elif cmd == "spec_gap_detect":
        _emit(spec_gap_detect(args.model, args.requirement, args.ref))
    elif cmd == "wrong_model_risk":
        _emit(wrong_model_risk(args.model, args.scenario, args.requirement))
    elif cmd == "certification_check":
        _emit(certification_check(args.model, args.scenario, args.region))
    elif cmd == "bundle_suggestion":
        _emit(bundle_suggestion(args.model))
    elif cmd == "evidence_pack":
        _emit(evidence_pack(args.model))
    elif cmd == "explain_match":
        _emit(explain_match(args.model, args.requirement, args.ref))
    elif cmd == "sku_normalize":
        _emit(sku_normalize(args.model))
    elif cmd == "attribute_extract":
        _emit(attribute_extract(args.text))
    elif cmd == "quote_spec_guardrail":
        _emit(quote_spec_guardrail(_loads(args.quote, []), args.requirement))
    elif cmd == "quote_draft":
        _emit(quote_draft(args.requirement))
    elif cmd == "set_cost":
        _emit(set_cost(args.model, args.cost, cost_date=args.cost_date,
                       cost_currency=args.cost_currency, source=args.source))
    elif cmd == "get_cost":
        _emit(get_cost(args.model))
    elif cmd == "audit":
        _emit(audit())
    elif cmd == "status":
        _emit(status())
    elif cmd == "selftest":
        return _selftest()
    else:
        _emit(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
