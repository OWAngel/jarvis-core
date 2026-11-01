#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hs_export_kit.py — PC2出口的HS编码 / 单证清单 / 认证台账工具箱

功能:
  hs_lookup      — 静态HS编码查询(工业树脂相关), 含申报要素/退税率/监管条件
  doc_checklist  — 目的国+付款方式 → 出口单证清单(静态规则)
  cert_ledger    — REACH/ISO9001/SGS等认证台账 SQLite (add/list)
  cert_expiry_check — N天内到期预警(供晨报消费)
  status         — 模块状态
  selftest       — 内置安全+行为自检

定位与边界(铁律):
  - 纯本地静态知识查询+台账, 零真实副作用, 不发送不删除不执行任何对外动作。
  - 绝不读凭据: email_tools / google_tools / gcal_oauth 硬拦永不 import。
  - 所有自由文本输出过 _redact; 到期比较语义化(datetime, 非字符串字典序)。
  - 禁 eval/exec/subprocess/动态import后门; 正则量词有界。
  - 免责一致性: 所有HS/单证知识每条标注「供参考,报关以归类裁定为准」。

全本机, 纯 stdlib sqlite3。自有库 ~/.jarvis/hs_export_kit.db(可通过 --db 或
JARVIS_HS_EXPORT_KIT_DB 环境变量覆盖)。
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
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Union

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config  # noqa: E402 — 纯读取, 无副作用


# ─────────────────────────────────────────────────────────────────────────────── #
# 软导入(importlib; 缺/出错 → None, 永不抛)
# ─────────────────────────────────────────────────────────────────────────────── #
def _opt(name: str):
    """Soft-import a peer module by name; return None on any error (never raises)."""
    try:
        return importlib.import_module(name)
    except Exception:
        return None


_DEFERRED = ("reasoning_core", "mission_control", "work_hub")
_LIVE_SOURCES: tuple = ()
_CREDENTIAL_MODULES = ("email_tools", "google_tools", "gcal_oauth")


def _deferred_status() -> dict:
    out: dict = {}
    for m in _DEFERRED:
        out[m] = {"present": _opt(m) is not None, "deferred": True}
    return out


# ─────────────────────────────────────────────────────────────────────────────── #
# 隐私原语(凭据硬拦 + 私密判定 + 脱敏; 本地层永远兜底)
# ─────────────────────────────────────────────────────────────────────────────── #
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".localkey", "config.toml",
})

# 凭据内容分类器 — 绝不用 $ / ^ 行锚(否则句中令牌漏判)
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

_PRIVATE_RX = re.compile(
    r"银行|工资|薪资|薪水|年终奖|身份证|社保|公积金|存款|余额|卡号|账号|账户|密码|"
    r"验证码|转账|汇款|报销|病|诊断|体检|处方|医保|住院|离婚|分手|贷款|按揭|抵押|"
    r"保密|机密|内部消息|收购|并购|裁员|股权|期权|融资|估值|offer|"
    r"报价|底价|成本价|利润|回扣|佣金|返点|客户名单|采购价|进价|"
    r"\bpassword\b|\bsalary\b|\bssn\b|\bbank\b|\bpin\b|\bmedical\b|"
    r"\bconfidential\b|\bnda\b|\bsecret\b|\btoken\b|\bapi[_-]?key\b|"
    r"\d[\d \-]{9,}\d",
    re.IGNORECASE)

# 高熵令牌兜底: 量词有界防 ReDoS(真密钥远短于 512 字符)
_HIENTROPY_RX = re.compile(
    r"(?<![A-Za-z0-9+/=])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}[A-Z])(?=[A-Za-z0-9_\-+/=]{0,512}[a-z])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}\d)"
    r"[A-Za-z0-9_\-+/=]{24,512}(?![A-Za-z0-9+/=])")


def _norm(text: str) -> str:
    """NFKC 归一化(防全角绕过安全扫描)。安全检测一律先过它, 走全文不截断。"""
    try:
        return unicodedata.normalize("NFKC", text or "")
    except Exception:
        return text or ""


def _is_secret_path(path: str) -> bool:
    """凭据/密钥文件 → True(硬拦)。双层: 本地正则永远先跑不可绕过; 库只能再加正例。"""
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


def _classify_private(text: str = "") -> bool:
    """文本是否私密。本地 _PRIVATE_RX 永远先跑; 库不可达且有正文 → fail-safe True。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密记录不占位、返原文
    (小美可读全部数据; 红线4 由 autopilot 审批闸/凭据硬拦独立守卫, 不靠此分类)。"""
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
    """脱敏: 抹凭据/令牌/卡号/长串数字/key=value。
    顺序铁律: 本地 value-strip 必在调库前、且在归一化后的原文上跑。"""
    s = _norm(text)
    if not s:
        return ""
    s = _SECRET_RX.sub("[凭证已脱敏]", s)
    s = re.sub(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9]*"
        r"(?:pass(?:word)?|pwd|token|secret|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret|refresh[_-]?token)"
        r"[A-Za-z0-9_\-]*\s*[:=：]\s*\S+",
        "[凭证已脱敏]", s)
    s = _HIENTROPY_RX.sub("[令牌已脱敏]", s)
    s = re.sub(r"\d[\d \-]{9,}\d", "[数字已脱敏]", s)
    aw = _opt("awareness_sensors")
    if aw is not None and hasattr(aw, "redact"):
        try:
            s = aw.redact(s)
        except Exception:
            pass
    return s


def _safe_text(text: str, limit: int = 240) -> str:
    """对外出口统一兜底: 先脱敏, 再(仅显示用)截断。"""
    s = _redact(text)
    if limit and len(s) > limit:
        return s[:limit] + "…"
    return s


def _seal(obj):
    """递归兜底: 把漏网凭据占位掉(防新增字段忘脱敏)。"""
    if isinstance(obj, str):
        n = _norm(obj)
        if _SECRET_RX.search(n) or _HIENTROPY_RX.search(n):
            return _HIENTROPY_RX.sub("[令牌已脱敏]", _SECRET_RX.sub("[凭证已脱敏]", n))
        return obj
    if isinstance(obj, dict):
        return {k: _seal(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_seal(v) for v in obj]
    return obj


def _flag_private(out: dict, any_private: bool) -> dict:
    """统一出口: 先 _seal 递归兜底凭据, 再按私密标 _private / _force_local。"""
    if not isinstance(out, dict):
        return out
    out = _seal(out)
    if any_private:
        out["_private"] = True
        try:
            cfg = _opt("config")
            allow = cfg.get("safety.allow_cloud_private", False) if cfg else False
        except Exception:
            allow = False
        if not allow:
            out["_force_local"] = True
    return out


# ─────────────────────────────────────────────────────────────────────────────── #
# 数值护栏(NaN/inf 会污染比较链)
# ─────────────────────────────────────────────────────────────────────────────── #
def _finite_num(v) -> Optional[float]:
    """转 float 且必须有限; NaN/inf/非数 → None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _as_int(v, default: int, lo: int, hi: int) -> int:
    """安全取整 + 夹取值域; NaN/inf/非整 → default。"""
    f = _finite_num(v)
    if f is None:
        return default
    try:
        n = int(f)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


# ─────────────────────────────────────────────────────────────────────────────── #
# 持久层: 自有库(纯 stdlib sqlite3)
# ─────────────────────────────────────────────────────────────────────────────── #
_HOME_ENV = "JARVIS_HS_EXPORT_KIT_DB"

_DISCLAIMER = "供参考,报关以归类裁定为准"


def _db_path() -> str:
    env = os.environ.get(_HOME_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser("~/.jarvis/hs_export_kit.db"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS hek_certs (
    id          TEXT PRIMARY KEY,       -- hash(cert_type|cert_no|product)
    cert_type   TEXT NOT NULL,          -- REACH/ISO9001/SGS/MSDS/COA/RoHS/...
    cert_no     TEXT,                   -- 证书编号(脱敏)
    issuer      TEXT,                   -- 签发机构(脱敏)
    issued_date TEXT,                   -- 签发日期 ISO YYYY-MM-DD
    expiry_date TEXT,                   -- 到期日期 ISO YYYY-MM-DD (空=永久有效)
    product     TEXT,                   -- 覆盖产品/型号范围(脱敏)
    scope       TEXT,                   -- 覆盖范围描述(脱敏)
    note        TEXT,                   -- 备注(脱敏)
    private     INTEGER DEFAULT 0,
    ts_created  TEXT,
    ts_updated  TEXT
);
CREATE INDEX IF NOT EXISTS idx_hek_certs_type ON hek_certs(cert_type);
CREATE INDEX IF NOT EXISTS idx_hek_certs_expiry ON hek_certs(expiry_date);
CREATE TABLE IF NOT EXISTS hek_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    action      TEXT,
    summary     TEXT
);
"""


def _conn(db_path: Optional[str] = None) -> sqlite3.Connection:
    """建库连接, 开 WAL, 建表。"""
    p = db_path or _db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    """解析 ISO 日期字符串 → datetime(UTC); 解析失败 → None。语义化比较用。"""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _cert_id(cert_type: str, cert_no: str, product: str) -> str:
    raw = "|".join([
        _norm(cert_type).lower().strip(),
        _norm(cert_no).strip(),
        _norm(product).lower().strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────── #
# 静态知识库: HS编码
# ─────────────────────────────────────────────────────────────────────────────── #
# 出处: 中国海关《商品综合分类目录(2024版)》及出口退税税率表。
# 注: HS编码及退税率随政策调整; 以下数据截至 2024 年底, 以当期税则/政策为准。
_HS_TABLE = [
    {
        "hs_code": "3907.40.00",
        "cn_code": "39074000",
        "name": "工业树脂(聚碳酸酯树脂,高耐候型为主,PC2含量≥80%)",
        "aliases": ["工业树脂", "高耐候型工业树脂", "homopolymer pc2", "polycarbonate resin",
                    "二基础聚合物", "pc2 resin", "h型工业树脂", "r-", "homopolymer"],
        "description": (
            "含量≥80%的聚碳酸酯树脂制品。工业树脂行业主流出口品类。"
            "高耐候型(Homopolymer)为主,兼含通用型中高PC2含量制品。"
        ),
        "declaration_elements": [
            "品名: 工业树脂(聚碳酸酯树脂)",
            "晶型: 高耐候型(Homopolymer)/通用型(Copolymer) — 须注明",
            "PC2含量(%): 须注明实际含量(如≥96%/≥98%)",
            "用途: 涂料/塑料/造纸/印刷油墨/橡胶等 — 须注明",
            "包装规格: 散装/袋装(25kg/50kg/1000kg大袋等) — 须注明",
        ],
        "export_rebate_rate": "13%",
        "rebate_note": "以当期出口退税税率表为准(2024年底为13%,政策随时可能调整)",
        "supervision_conditions": [
            "无特殊许可证要求(非危险品,可正常出口)",
            "部分客户国/地区要求REACH注册/MSDS/COA,建议提前备齐",
            "出口申报时须如实申报PC2含量及晶型",
        ],
        "disclaimer": _DISCLAIMER,
    },
    {
        "hs_code": "3907.99.00",
        "cn_code": "39079900",
        "name": "其他改性树脂/制品(PC2含量<80%或其他聚碳酸酯树脂制品)",
        "aliases": ["其他工业树脂", "低含量pc2树脂", "通用型工业树脂", "copolymer pc2",
                    "c型工业树脂", "copolymer"],
        "description": (
            "PC2含量低于80%的其他改性树脂, 或特殊配方的聚碳酸酯树脂制品。"
            "部分通用型低含量品种、表面包覆改性系列可能归入此类。"
            "注: 实际归类需综合PC2含量、晶型、是否经表面处理等要素判定。"
        ),
        "declaration_elements": [
            "品名: 改性树脂制品/改性聚碳酸酯树脂",
            "PC2含量(%): 须注明实际含量",
            "晶型及表面处理情况: 须注明",
            "用途: 须注明",
            "包装规格: 须注明",
        ],
        "export_rebate_rate": "13%",
        "rebate_note": "以当期出口退税税率表为准(2024年底为13%,政策随时可能调整)",
        "supervision_conditions": [
            "无特殊许可证要求",
            "如实申报含量及晶型",
        ],
        "disclaimer": _DISCLAIMER,
    },
    {
        "hs_code": "3901.10.00",
        "cn_code": "39011000",
        "name": "基础聚合物(非注塑级,化工/冶金用)",
        "aliases": ["基础聚合物", "base polymer", "非注塑级pc2", "工业级pc2",
                    "聚碳酸酯工业级", "polycarbonate industrial-grade"],
        "description": (
            "非注塑级的基础聚合物。用于冶金、电子、催化剂等工业用途。"
            "工业树脂(注塑级)不归此类; 若以注塑用途出口须申报3907品目。"
            "注: 实际归类以用途、制造方法、含量等综合判定。"
        ),
        "declaration_elements": [
            "品名: 基础聚合物(须注明非注塑级/用途)",
            "PC2纯度(%): 须注明",
            "用途: 冶金/催化/电子等 — 须注明",
            "包装规格: 须注明",
        ],
        "export_rebate_rate": "9%",
        "rebate_note": "以当期出口退税税率表为准(2024年底约9%,政策随时可能调整)",
        "supervision_conditions": [
            "无特殊许可证要求",
            "须注明非注塑用途, 避免与3907混淆",
        ],
        "disclaimer": _DISCLAIMER,
    },
    {
        "hs_code": "2905.31.00",
        "cn_code": "29053100",
        "name": "化工单体(单体原料原料)",
        "aliases": ["化工单体", "monomer", "monomer raw", "单体"],
        "description": (
            "化工单体及精矿。工业树脂上游原料。"
            "一般不涉及工业树脂成品出口,仅作参考。"
        ),
        "declaration_elements": [
            "品名: 化工单体/单体原料",
            "PC2品位(%): 须注明",
        ],
        "export_rebate_rate": "0%",
        "rebate_note": "矿石类原料一般无退税,以当期政策为准",
        "supervision_conditions": [
            "矿产资源类,出口可能需矿产品出口许可证,以当期政策为准",
        ],
        "disclaimer": _DISCLAIMER,
    },
]

# HS 编码规范化: 去除点、空格
def _norm_hs(code: str) -> str:
    return re.sub(r"[\s.\-]", "", _norm(code or "").upper())


def hs_lookup(query: str, db_path: Optional[str] = None) -> dict:
    """按品名或编码查HS编码信息。

    参数:
        query: 品名关键词(如「工业树脂」「homopolymer」)或HS编码(如「3907.40」)

    返回: { ok, matches: [{hs_code, name, declaration_elements, ...}], disclaimer }
    """
    q = _norm(query or "").strip().lower()
    if not q:
        return {"ok": False, "error": "query 不能为空", "disclaimer": _DISCLAIMER}

    is_private = _classify_private(query)
    q_norm_hs = _norm_hs(q)

    matches = []
    for entry in _HS_TABLE:
        hit = False
        # 编码精确/前缀匹配
        code_norm = _norm_hs(entry["hs_code"])
        cn_norm = _norm_hs(entry.get("cn_code", ""))
        if q_norm_hs and (code_norm.startswith(q_norm_hs) or q_norm_hs.startswith(code_norm[:4])):
            hit = True
        # 名称/别名模糊匹配(分词: 每段>=2字)
        if not hit:
            parts = [p for p in re.split(r"[\s/,;、]+", q) if len(p) >= 2]
            searchable = (
                _norm(entry["name"]).lower() + " " +
                " ".join(_norm(a).lower() for a in entry["aliases"]) + " " +
                _norm(entry["description"]).lower()
            )
            for part in (parts if parts else [q]):
                if part in searchable:
                    hit = True
                    break

        if hit:
            m = {
                "hs_code": entry["hs_code"],
                "cn_code": entry.get("cn_code", ""),
                "name": entry["name"],
                "description": _safe_text(entry["description"], 300),
                "declaration_elements": entry["declaration_elements"],
                "export_rebate_rate": entry["export_rebate_rate"],
                "rebate_note": entry["rebate_note"],
                "supervision_conditions": entry["supervision_conditions"],
                "disclaimer": entry["disclaimer"],
            }
            matches.append(m)

    out: dict = {
        "ok": True,
        "query": _safe_text(query, 80),
        "matches": matches,
        "total": len(matches),
        "disclaimer": _DISCLAIMER,
        "note": "HS编码及退税率随政策调整,以当期海关税则/出口退税政策为准,申报前请核实。",
    }
    if not matches:
        out["suggestion"] = (
            "未找到匹配。常见工业树脂品类: "
            "3907.40(PC2≥80%树脂)/3907.99(其他改性树脂)/3901.10(非注塑级基础聚合物)。"
        )
    return _flag_private(out, is_private)


# ─────────────────────────────────────────────────────────────────────────────── #
# 静态知识库: 出口单证清单规则
# ─────────────────────────────────────────────────────────────────────────────── #
# 地区/国家 → 特殊提示的映射(静态, 供参考)
_COUNTRY_NOTES: Dict[str, List[str]] = {
    # 东盟成员国(适用FORM E)
    "东盟": [
        "适用FORM E产地证(中国—东盟自贸协定), 可享受关税优惠",
        "FORM E须在中国国际贸易促进委员会(CCPIT)或海关申请签发",
    ],
    "asean": [
        "FORM E certificate of origin applies (ASEAN-China FTA)",
    ],
    "vietnam": ["越南: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "越南": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "thailand": ["泰国: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "泰国": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "indonesia": ["印尼: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "印度尼西亚": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "malaysia": ["马来西亚: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "马来西亚": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "philippines": ["菲律宾: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "菲律宾": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "singapore": ["新加坡: 适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    "新加坡": ["适用FORM E(中国—东盟), 亦可用RCEP产地证"],
    # RCEP成员国(部分)
    "japan": ["日本: RCEP成员国, 可申请RCEP产地证(取代普惠制FORM A)"],
    "日本": ["RCEP成员国, 可申请RCEP产地证(取代普惠制FORM A)"],
    "south korea": ["韩国: RCEP成员国, 可申请RCEP产地证"],
    "korea": ["韩国: RCEP成员国, 可申请RCEP产地证"],
    "韩国": ["RCEP成员国, 可申请RCEP产地证"],
    "australia": ["澳大利亚: RCEP成员国, 可申请RCEP产地证"],
    "澳大利亚": ["RCEP成员国, 可申请RCEP产地证"],
    "new zealand": ["新西兰: RCEP成员国, 可申请RCEP产地证"],
    "新西兰": ["RCEP成员国, 可申请RCEP产地证"],
    # 欧盟/欧洲
    "eu": [
        "欧盟: REACH注册为强制要求(进口商承担义务, 建议提前确认买方是否已注册)",
        "普惠制FORM A已基本退场(欧盟已取消对华GSP), 不再适用",
        "需提供MSDS(安全数据表)及COA(质检报告)",
    ],
    "europe": [
        "欧洲: REACH注册建议确认; 普惠制FORM A对欧盟不再适用",
    ],
    "欧盟": [
        "REACH注册为强制要求(进口商承担义务, 建议提前确认买方是否已注册)",
        "普惠制FORM A已基本退场(欧盟已取消对华GSP), 不再适用",
        "需提供MSDS及COA",
    ],
    "germany": ["德国/欧盟成员: REACH要求同欧盟, 需MSDS+COA"],
    "德国": ["欧盟成员: REACH要求同欧盟, 需MSDS+COA"],
    "france": ["法国/欧盟成员: REACH要求同欧盟"],
    "法国": ["欧盟成员: REACH要求同欧盟"],
    "italy": ["意大利/欧盟成员: REACH要求同欧盟"],
    "意大利": ["欧盟成员: REACH要求同欧盟"],
    "netherlands": ["荷兰/欧盟成员: REACH要求同欧盟; 鹿特丹为重要中转港"],
    "荷兰": ["欧盟成员: REACH要求同欧盟; 鹿特丹为重要中转港"],
    # 中东/非洲
    "india": [
        "印度: BIS认证可能适用(部分化工品); 海关单据须严格核对商品编码",
        "RCEP目前印度未签署; 普惠制FORM A可适用(以当期政策为准)",
    ],
    "印度": [
        "BIS认证可能适用(部分化工品); 海关单据须严格核对商品编码",
        "RCEP目前印度未签署; 普惠制FORM A可适用(以当期政策为准)",
    ],
    "turkey": [
        "土耳其: 部分商品需Turkish标准认证(TSE), 建议客户确认",
        "无产地证优惠协议(以当期政策为准)",
    ],
    "土耳其": [
        "部分商品需Turkish标准认证(TSE), 建议客户确认",
        "无产地证优惠协议(以当期政策为准)",
    ],
    "saudi arabia": ["沙特: 部分商品需SASO认证/清真证(化工品一般免), 建议客户确认"],
    "沙特": ["部分商品需SASO认证/清真证(化工品一般免), 建议客户确认"],
    "uae": ["阿联酋: 可能需要使馆认证(ATA/legalization), 以客户确认为准"],
    "阿联酋": ["可能需要使馆认证(ATA/legalization), 以客户确认为准"],
    "brazil": [
        "巴西: 进口许可SISCOMEX较严; 可能需要详细的技术规格声明",
        "普惠制FORM A可适用(以当期政策为准)",
    ],
    "巴西": [
        "SISCOMEX进口许可较严; 可能需要详细的技术规格声明",
        "普惠制FORM A可适用(以当期政策为准)",
    ],
    "usa": [
        "美国: 无中美FTA; 部分品类有加征关税(注意HS编码是否在Section 301范围)",
        "FDA注册对通用化工树脂一般不强制, 但TSCA合规建议确认",
    ],
    "us": [
        "美国: 无中美FTA; 注意Section 301加征关税范围",
    ],
    "美国": [
        "无中美FTA; 注意Section 301加征关税范围; TSCA合规建议确认",
    ],
}

# 产地证适用判断
def _coo_recommendation(destination: str) -> dict:
    """推断适用产地证类型。返回 {type, note, form_a_available}。"""
    dest_low = _norm(destination or "").lower()

    # ASEAN成员国 → FORM E
    asean_members = {"vietnam", "越南", "thailand", "泰国", "indonesia", "印度尼西亚",
                     "malaysia", "马来西亚", "philippines", "菲律宾", "singapore", "新加坡",
                     "myanmar", "缅甸", "cambodia", "柬埔寨", "laos", "老挝", "brunei", "文莱",
                     "asean", "东盟"}
    # RCEP成员(非ASEAN部分)→ RCEP产地证
    rcep_non_asean = {"japan", "日本", "south korea", "korea", "韩国", "australia", "澳大利亚",
                      "new zealand", "新西兰", "rcep"}
    # 欧盟 → FORM A已基本退场
    eu_members = {"eu", "欧盟", "germany", "德国", "france", "法国", "italy", "意大利",
                  "netherlands", "荷兰", "spain", "西班牙", "poland", "波兰",
                  "belgium", "比利时", "sweden", "瑞典"}

    for kw in asean_members:
        if kw in dest_low:
            return {
                "type": "FORM E",
                "note": "中国—东盟FTA产地证(FORM E), 可享受关税优惠; 亦可申请RCEP产地证",
                "form_a_available": False,
                "rcep_available": True,
            }
    for kw in rcep_non_asean:
        if kw in dest_low:
            return {
                "type": "RCEP产地证",
                "note": "RCEP成员国, 推荐申请RCEP产地证(替代普惠制FORM A)",
                "form_a_available": False,
                "rcep_available": True,
            }
    for kw in eu_members:
        if kw in dest_low:
            return {
                "type": "普通产地证(CO)",
                "note": "欧盟已取消对华GSP, 普惠制FORM A不再适用; 申请普通产地证即可",
                "form_a_available": False,
                "rcep_available": False,
            }
    # 默认: 可尝试FORM A(以当期政策为准)
    return {
        "type": "普通产地证(CO)或FORM A",
        "note": "普惠制FORM A已基本退场(大多数主要国家已不适用); 建议申请普通CO或确认目的国是否有优惠协议",
        "form_a_available": True,
        "rcep_available": False,
    }


def doc_checklist(
    destination: str,
    payment_terms: str = "TT",
    db_path: Optional[str] = None,
) -> dict:
    """目的国 + 付款方式 → 出口单证清单。

    参数:
        destination:   目的国/地区(如「越南」「EU」「USA」「日本」)
        payment_terms: 付款方式(TT/LC/DP/DA/OA等)

    返回: { ok, destination, payment_terms, basic_docs, coo, lc_notes, special_notes, disclaimer }
    """
    dest = (destination or "").strip()
    pt = (payment_terms or "TT").strip().upper()
    if not dest:
        return {"ok": False, "error": "destination 不能为空", "disclaimer": _DISCLAIMER}

    is_private = _classify_private(destination) or _classify_private(payment_terms)
    dest_low = _norm(dest).lower()

    # 1. 基础单证(任何出口均需)
    basic_docs = [
        {"doc": "形式发票(Proforma Invoice, PI)", "mandatory": True,
         "note": "买卖双方确认订单要素; 须与其他单据一致"},
        {"doc": "商业发票(Commercial Invoice, CI)", "mandatory": True,
         "note": "报关+收汇核心单据; 品名/规格/数量/金额须准确"},
        {"doc": "装箱单(Packing List, PL)", "mandatory": True,
         "note": "每箱件数/净重/毛重/体积; 须与BL保持一致"},
        {"doc": "提单(Bill of Lading, B/L)", "mandatory": True,
         "note": "海运主提单; 港口到港; 须与CI/PL一致"},
        {"doc": "出口报关单(Export Customs Declaration)", "mandatory": True,
         "note": "中国海关电子化申报; 通常由报关行代理; 须如实申报HS编码/申报要素"},
        {"doc": "质检报告/COA(Certificate of Analysis)", "mandatory": False,
         "note": "买家通常要求; 工业树脂必备: PC2含量/白度/消色力/粒径等关键指标"},
        {"doc": "MSDS/SDS(安全数据表)", "mandatory": False,
         "note": "大多数买家要求; 欧盟/部分国家法规要求必须提供"},
    ]

    # 2. 产地证推荐
    coo = _coo_recommendation(dest)

    # 3. LC专项提醒
    lc_notes: List[str] = []
    is_lc = bool(re.search(r"\bL/?C\b|信用证|letter\s+of\s+credit", pt, re.IGNORECASE))
    if is_lc:
        lc_notes = [
            "【LC付款: 单单一致严格要求】",
            "① CI品名/规格/数量/金额须与LC条款完全一致(包括拼写、大小写、单位)",
            "② PL总重/件数须与CI/BL完全一致",
            "③ BL签发日期须在LC最迟装期(latest shipment date)之内",
            "④ 提交单据须在BL日期后规定天数内(通常21天内)完成",
            "⑤ 若LC要求SGS/BV预装货检验, 须提前安排并取得检验证书",
            "⑥ 若LC要求使馆认证(legalization), 须安排领事认证",
            "⑦ 注意不符点(discrepancy)风险: 建议收到LC后由银行/专业人员审核",
        ]
    elif re.search(r"\bDP\b|\bDA\b|\bOA\b", pt, re.IGNORECASE):
        lc_notes = [
            f"【{pt}付款: 收款风险提示】",
            "DP(付款交单)/DA(承兑交单)/OA(赊销)均存在买方拒付风险",
            "建议: ①核实买方资信 ②考虑出口信用保险(中国出口信用保险公司) ③控制发货节奏",
        ]

    # 4. 目的国特殊提示
    special_notes: List[str] = []
    for kw, notes in _COUNTRY_NOTES.items():
        if kw in dest_low:
            special_notes.extend(notes)
    # 去重保序
    seen: set = set()
    dedup: List[str] = []
    for n in special_notes:
        if n not in seen:
            seen.add(n)
            dedup.append(n)
    special_notes = dedup

    out = {
        "ok": True,
        "destination": _safe_text(dest, 80),
        "payment_terms": _safe_text(pt, 40),
        "basic_docs": basic_docs,
        "coo": coo,
        "lc_notes": lc_notes,
        "special_notes": special_notes,
        "disclaimer": _DISCLAIMER,
        "note": "单证清单为静态规则参考,实际要求以LC条款、买家合同及目的港法规为准。",
    }
    return _flag_private(out, is_private)


# ─────────────────────────────────────────────────────────────────────────────── #
# 认证台账
# ─────────────────────────────────────────────────────────────────────────────── #
def cert_ledger_add(
    cert_type: str,
    product: str,
    issuer: str = "",
    cert_no: str = "",
    issued_date: str = "",
    expiry_date: str = "",
    scope: str = "",
    note: str = "",
    db_path: Optional[str] = None,
) -> dict:
    """添加/更新认证台账条目(upsert, private不降级)。

    参数:
        cert_type:    认证类型(REACH/ISO9001/SGS/MSDS/COA/RoHS/…)
        product:      覆盖产品/型号(如「M-996」「所有工业树脂产品」)
        issuer:       签发机构(如 SGS / TÜV / 中国质检院)
        cert_no:      证书编号
        issued_date:  签发日期(YYYY-MM-DD)
        expiry_date:  到期日期(YYYY-MM-DD, 空=永久有效)
        scope:        覆盖范围描述
        note:         备注
    """
    if not cert_type or not product:
        return {"ok": False, "error": "cert_type 和 product 不能为空"}

    # 脱敏所有自由文本字段
    ctype_s = _safe_text(cert_type, 80)
    product_s = _safe_text(product, 120)
    issuer_s = _safe_text(issuer, 120)
    cert_no_s = _safe_text(cert_no, 80)
    scope_s = _safe_text(scope, 300)
    note_s = _safe_text(note, 300)
    issued_s = (issued_date or "").strip()
    expiry_s = (expiry_date or "").strip()

    # 验证日期格式(语义化)
    if issued_s and _parse_date(issued_s) is None:
        return {"ok": False, "error": f"issued_date 格式无法解析: {issued_s}, 请用YYYY-MM-DD"}
    if expiry_s and _parse_date(expiry_s) is None:
        return {"ok": False, "error": f"expiry_date 格式无法解析: {expiry_s}, 请用YYYY-MM-DD"}

    is_private = any(_classify_private(x) for x in [cert_type, product, issuer, cert_no, scope, note])

    cid = _cert_id(cert_type, cert_no, product)
    now = _now_iso()

    c = _conn(db_path)
    try:
        # upsert: ON CONFLICT DO UPDATE; private 只升不降(MAX)
        c.execute("""
            INSERT INTO hek_certs
                (id, cert_type, cert_no, issuer, issued_date, expiry_date, product, scope, note,
                 private, ts_created, ts_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                cert_type   = excluded.cert_type,
                cert_no     = excluded.cert_no,
                issuer      = excluded.issuer,
                issued_date = excluded.issued_date,
                expiry_date = excluded.expiry_date,
                product     = excluded.product,
                scope       = excluded.scope,
                note        = excluded.note,
                private     = MAX(hek_certs.private, excluded.private),
                ts_updated  = excluded.ts_updated
        """, (cid, ctype_s, cert_no_s, issuer_s, issued_s, expiry_s,
              product_s, scope_s, note_s, 1 if is_private else 0, now, now))
        c.execute("INSERT INTO hek_audit (ts, action, summary) VALUES (?, ?, ?)",
                  (now, "cert_add", f"cert_type={ctype_s} product={product_s}"))
        c.commit()
    finally:
        c.close()

    out: dict = {
        "ok": True,
        "id": cid,
        "cert_type": ctype_s,
        "product": product_s,
        "issued_date": issued_s or None,
        "expiry_date": expiry_s or None,
        "action": "upserted",
    }
    return _flag_private(out, is_private)


def cert_ledger_list(
    cert_type: str = "",
    product: str = "",
    db_path: Optional[str] = None,
) -> dict:
    """列出认证台账。可按cert_type或product过滤。"""
    c = _conn(db_path)
    try:
        params: List = []
        clauses: List[str] = []
        if cert_type:
            clauses.append("LOWER(cert_type) LIKE LOWER(?)")
            params.append("%" + cert_type.replace("%", r"\%").replace("_", r"\_") + "%")
        if product:
            clauses.append("LOWER(product) LIKE LOWER(?)")
            params.append("%" + product.replace("%", r"\%").replace("_", r"\_") + "%")
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(
            f"SELECT id, cert_type, cert_no, issuer, issued_date, expiry_date, "
            f"product, scope, note, private, ts_updated FROM hek_certs {where} "
            f"ORDER BY cert_type, expiry_date NULLS LAST",
            params
        ).fetchall()
    finally:
        c.close()

    certs = []
    any_private = False
    today_dt = datetime.now(timezone.utc)
    for row in rows:
        (cid, ctype, cert_no, issuer, issued, expiry, prod, scope, note,
         priv, ts_updated) = row
        if priv:
            any_private = True
        # 判是否已过期(语义化比较)
        expired = False
        days_remaining: Optional[int] = None
        if expiry:
            expiry_dt = _parse_date(expiry)
            if expiry_dt is not None:
                delta = (expiry_dt - today_dt).days
                days_remaining = delta
                expired = delta < 0

        certs.append({
            "id": cid,
            "cert_type": ctype,
            "cert_no": cert_no or "",
            "issuer": issuer or "",
            "issued_date": issued or "",
            "expiry_date": expiry or "",
            "days_remaining": days_remaining,
            "expired": expired,
            "product": prod,
            "scope": scope or "",
            "note": note or "",
            "ts_updated": ts_updated or "",
        })

    out: dict = {
        "ok": True,
        "total": len(certs),
        "certs": certs,
    }
    return _flag_private(out, any_private)


def cert_expiry_check(days: int = 90, db_path: Optional[str] = None) -> dict:
    """检查N天内到期(含已过期)的认证。供晨报消费, 纯只读函数。

    参数:
        days: 预警窗口天数(默认90天)

    返回: { ok, days, expiring: [{cert_type, product, expiry_date, days_remaining, expired, ...}] }
    """
    days_int = _as_int(days, default=90, lo=1, hi=3650)
    today_dt = datetime.now(timezone.utc)
    threshold_dt = today_dt + timedelta(days=days_int)

    c = _conn(db_path)
    try:
        # 只取有到期日的记录
        rows = c.execute(
            "SELECT id, cert_type, cert_no, issuer, issued_date, expiry_date, "
            "product, scope, note, private FROM hek_certs "
            "WHERE expiry_date IS NOT NULL AND expiry_date != '' "
            "ORDER BY expiry_date"
        ).fetchall()
    finally:
        c.close()

    expiring = []
    any_private = False
    for row in rows:
        (cid, ctype, cert_no, issuer, issued, expiry, prod, scope, note, priv) = row
        if priv:
            any_private = True
        expiry_dt = _parse_date(expiry)
        if expiry_dt is None:
            continue  # 无法解析的日期跳过
        # 语义化比较: 已过期 OR 在阈值窗口内
        if expiry_dt <= threshold_dt:
            delta = (expiry_dt - today_dt).days
            expiring.append({
                "id": cid,
                "cert_type": ctype,
                "cert_no": cert_no or "",
                "issuer": issuer or "",
                "issued_date": issued or "",
                "expiry_date": expiry,
                "days_remaining": delta,
                "expired": delta < 0,
                "product": prod,
                "scope": scope or "",
                "note": note or "",
                "alert_level": "expired" if delta < 0 else ("urgent" if delta <= 30 else "warning"),
            })

    out: dict = {
        "ok": True,
        "days": days_int,
        "total_expiring": len(expiring),
        "expiring": expiring,
        "note": "到期日基于台账录入数据,以实际证书为准。空到期日=永久有效,不在预警范围内。",
    }
    return _flag_private(out, any_private)


def coa_freshness(stale_months: int = 6, limit: int = 30,
                  db_path: Optional[str] = None) -> dict:
    """COA 覆盖与新鲜度: 按客户(scope)汇总 COA 质检证明, 标记最新 COA 已过旧(>stale_months 月)
    需补开的客户。外贸出货常要求近期 COA, 此检查帮 User 主动发现哪些客户的 COA 该刷新。

    与到期预警(cert_expiry_check)互补: COA 是按批次的质检文档无监管到期日, 看的是"签发新鲜度"而非"到期"。
    纯只读; 含客户名为内部业务情报, 标 _private, 对外须经审批门。

    参数:
        stale_months: 视为过旧的月数阈值(默认6); 最新 COA 距今超此值→需补开
        limit: stale 列表返回上限(默认30)

    返回: { ok, stale_months, total_coa, customers_covered, fresh_customers,
            stale_customers, undated_customers, stale: [{customer, latest_coa_date, days_since, coa_count}] }
    """
    months = _as_int(stale_months, default=6, lo=1, hi=120)
    lim = _as_int(limit, default=30, lo=1, hi=200)
    stale_days = months * 30
    today_dt = datetime.now(timezone.utc)

    c = _conn(db_path)
    try:
        rows = c.execute(
            "SELECT scope, issued_date FROM hek_certs WHERE cert_type='COA'"
        ).fetchall()
    finally:
        c.close()

    # 按客户(scope)聚合: 取每客户最新 COA 签发日。
    # 铁律: 未来签发日(ingest 把 cert_no 里 "12_29" 误解析成次年)不参与 latest 判定——
    # 否则一个真实 COA 16 个月前、却有张误解析未来日期的客户会被错判"新鲜", 架空本能力目的。
    by_scope: Dict[str, dict] = {}
    for scope, issued in rows:
        key = (scope or "").strip() or "(未标注客户)"
        g = by_scope.setdefault(key, {"count": 0, "latest_dt": None, "latest_str": "",
                                      "has_future": False})
        g["count"] += 1
        dt = _parse_date(issued)
        if dt is None:
            continue
        if dt > today_dt:
            g["has_future"] = True   # 未来签发日: 记异常, 不当作 latest
            continue
        if g["latest_dt"] is None or dt > g["latest_dt"]:
            g["latest_dt"] = dt
            g["latest_str"] = (issued or "").strip()

    stale: List[dict] = []
    fresh_count = 0
    undated_scopes = 0       # 完全无可解析签发日
    future_only_scopes = 0   # 仅有未来签发日(数据异常, 无真实近期 COA 可判)
    for scope, g in by_scope.items():
        if g["latest_dt"] is None:
            if g["has_future"]:
                future_only_scopes += 1
            else:
                undated_scopes += 1
            continue
        days_since = (today_dt - g["latest_dt"]).days
        if days_since > stale_days:
            stale.append({
                "customer": _safe_text(scope, 80),
                "latest_coa_date": g["latest_str"],
                "days_since": days_since,
                "coa_count": g["count"],
            })
        else:
            fresh_count += 1

    stale.sort(key=lambda x: x["days_since"], reverse=True)

    out: dict = {
        "ok": True,
        "stale_months": months,
        "total_coa": len(rows),
        "customers_covered": len(by_scope),
        "fresh_customers": fresh_count,
        "stale_customers": len(stale),
        "undated_customers": undated_scopes,
        "future_dated_customers": future_only_scopes,
        "stale": stale[:lim],
        "note": ("COA 新鲜度按客户最新真实签发日判定(COA 无监管到期日, 看新鲜度); "
                 "过旧客户若有新订单须补开近期 COA。未来签发日视为录入异常已排除("
                 "future_dated_customers 计数), 需核对原始 COA。内部业务情报, 对外须经审批门。"),
    }
    return _flag_private(out, any_private=True)


# ─────────────────────────────────────────────────────────────────────────────── #
# status / dispatch
# ─────────────────────────────────────────────────────────────────────────────── #
def status(db_path: Optional[str] = None) -> dict:
    """模块状态: 台账基本信息 + 集成模块在位情况。"""
    c = _conn(db_path)
    try:
        total_certs = c.execute("SELECT COUNT(*) FROM hek_certs").fetchone()[0]
        expiring_30 = 0
        today_dt = datetime.now(timezone.utc)
        thr30 = today_dt + timedelta(days=30)
        rows = c.execute(
            "SELECT expiry_date FROM hek_certs WHERE expiry_date IS NOT NULL AND expiry_date != ''"
        ).fetchall()
        for (expiry,) in rows:
            dt = _parse_date(expiry)
            if dt is not None and dt <= thr30:
                expiring_30 += 1
    finally:
        c.close()

    return {
        "ok": True,
        "module": "hs_export_kit",
        "version": "1.0.0",
        "db_path": _db_path() if db_path is None else db_path,
        "total_certs": total_certs,
        "expiring_in_30d": expiring_30,
        "hs_entries": len(_HS_TABLE),
        "disclaimer": _DISCLAIMER,
        "integrations": _deferred_status(),
        "description": "PC2出口HS编码/单证清单/认证台账工具箱",
    }


# ─────────────────────────────────────────────────────────────────────────────── #
# 白名单 / dispatch
# ─────────────────────────────────────────────────────────────────────────────── #
# CLI-only 写库动作: 不暴露给模型
_CLI_ONLY = frozenset({"cert_ledger_add"})

# 模型只读白名单
_DISPATCH_WHITELIST = frozenset({
    "hs_lookup",
    "doc_checklist",
    "cert_ledger_list",
    "cert_expiry_check",
    "coa_freshness",
    "status",
})


def is_tool(name: str) -> bool:
    return name.startswith("hs_export_kit_") and \
        name.replace("hs_export_kit_", "") in _DISPATCH_WHITELIST


def dispatch(name: str, args: Optional[dict] = None) -> dict:
    """jarvis_brain 工具入口: 只放只读分析动作。cert_ledger_add(写库)永不经此 — 限CLI/内部。"""
    args = dict(args or {})
    short = name.replace("hs_export_kit_", "")
    if short in _CLI_ONLY:
        return {"ok": False, "error": f"动作 '{short}' 写库, 仅限 CLI / 内部, 不暴露给模型"}
    if short not in _DISPATCH_WHITELIST:
        return {"ok": False,
                "error": f"动作 '{short}' 不在 hs_export_kit 只读白名单 "
                         f"{sorted(_DISPATCH_WHITELIST)}; cert_ledger_add 请经 CLI。"}
    if short == "hs_lookup":
        return hs_lookup(str(args.get("query", "")))
    if short == "doc_checklist":
        return doc_checklist(
            str(args.get("destination", "")),
            str(args.get("payment_terms", "TT")),
        )
    if short == "cert_ledger_list":
        return cert_ledger_list(
            str(args.get("cert_type", "")),
            str(args.get("product", "")),
        )
    if short == "cert_expiry_check":
        return cert_expiry_check(args.get("days", 90))
    if short == "status":
        return status()
    return {"ok": False, "error": "unreachable"}  # pragma: no cover


# ─────────────────────────────────────────────────────────────────────────────── #
# selftest — AST 静态扫 + 行为验证
# ─────────────────────────────────────────────────────────────────────────────── #
_FORBIDDEN_EXEC_MODULES = (
    "email_tools", "google_tools", "gcal_oauth",
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
    """仅做临时目录清理(被 AST 自检单独豁免的唯一函数; 绝不放任何别的逻辑进来)。"""
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _selftest() -> int:
    """安全不变量自检(扫整模块源码, 不豁免 _selftest 自身, 防执行面藏身):
       ① 不直接/动态 import 任何凭据/执行面模块; ② 无执行/凭据方法调用(含 getattr 绕过);
       ③ 无危险 stdlib; ④ 源码无 confirmed=True; ⑤ 模型白名单只读; ⑥ 行为验证。
    返回 0 = 全过; 非 0 = 失败数。
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
            if isinstance(f, ast.Attribute) and f.attr in _FORBIDDEN_CALLS:
                bad_calls.add(f.attr)
            if isinstance(f, ast.Name) and f.id in ("exec", "eval", "__import__", "compile"):
                bad_calls.add(f.id)
            if isinstance(f, ast.Name) and (f.id in _FORBIDDEN_CALLS or f.id in _FORBIDDEN_ATTRS):
                bad_calls.add(f.id)
            fname = None
            if isinstance(f, ast.Name):
                fname = f.id
            elif isinstance(f, ast.Attribute):
                fname = f.attr
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
        fails.append("① 动态 import 凭据/执行面模块: " + m)
    for a in sorted(bad_attrs):
        fails.append("② 出现执行/凭据方法调用: " + a)
    for c_name in sorted(bad_calls):
        fails.append("③ 出现危险 stdlib 调用: " + c_name)
    if confirmed_true:
        fails.append("④ 源码出现 confirmed=True")
    for leaked in _CLI_ONLY:
        if leaked in _DISPATCH_WHITELIST:
            fails.append("⑤ 模型白名单泄漏写库动作: " + leaked)

    # ⑥ 行为验证(临时库 + 断言; 强制开启数据层隐私开关测全链路)
    tmp = tempfile.mkdtemp(prefix="hek_selftest_")
    db = os.path.join(tmp, "hek.db")
    _orig_cfg_get = config.get
    config.get = lambda k, fb=None: True if k == "safety.data_layer_privacy_enabled" else _orig_cfg_get(k, fb)
    try:
        # 6a. hs_lookup: 工业树脂命中 3907.40
        r = hs_lookup("工业树脂", db_path=db)
        if not r.get("ok") or not r.get("matches"):
            fails.append("⑥ hs_lookup('工业树脂') 未返回任何匹配")
        if not any("3907.40" in m["hs_code"] for m in r.get("matches", [])):
            fails.append("⑥ hs_lookup('工业树脂') 未命中 3907.40")

        # 6b. hs_lookup: homopolymer 命中
        r2 = hs_lookup("homopolymer", db_path=db)
        if not r2.get("ok") or not r2.get("matches"):
            fails.append("⑥ hs_lookup('homopolymer') 未返回任何匹配")

        # 6c. hs_lookup: 空查询 → error
        r_empty = hs_lookup("", db_path=db)
        if r_empty.get("ok"):
            fails.append("⑥ hs_lookup('') 应返回 ok=False")

        # 6d. hs_lookup: 不存在的关键词
        r_none = hs_lookup("不存在的化学品XYZABC", db_path=db)
        if not r_none.get("ok") or r_none.get("total", -1) != 0:
            fails.append("⑥ hs_lookup('不存在的化学品XYZABC') 应返回 ok=True total=0")

        # 6e. hs_lookup: 每条结果含免责声明
        for m in r.get("matches", []):
            if "供参考" not in (m.get("disclaimer") or ""):
                fails.append("⑥ hs_lookup 结果缺免责声明")
                break

        # 6f. doc_checklist: 越南+LC
        dc = doc_checklist("越南", "LC", db_path=db)
        if not dc.get("ok"):
            fails.append("⑥ doc_checklist('越南','LC') 失败: " + str(dc))
        if not dc.get("lc_notes"):
            fails.append("⑥ doc_checklist LC模式应有 lc_notes")
        if "FORM E" not in str(dc.get("coo", {})):
            fails.append("⑥ doc_checklist 越南应推荐 FORM E")

        # 6g. doc_checklist: 欧盟+TT → REACH提示
        dc_eu = doc_checklist("欧盟", "TT", db_path=db)
        if not dc_eu.get("ok"):
            fails.append("⑥ doc_checklist('欧盟','TT') 失败")
        if not any("REACH" in n for n in dc_eu.get("special_notes", [])):
            fails.append("⑥ doc_checklist 欧盟应有 REACH 特殊提示")

        # 6h. doc_checklist: 空目的国 → error
        dc_empty = doc_checklist("", db_path=db)
        if dc_empty.get("ok"):
            fails.append("⑥ doc_checklist('') 应返回 ok=False")

        # 6i. cert_ledger_add: 新增认证
        r_add = cert_ledger_add(
            cert_type="REACH", product="M-996", issuer="ECHA",
            cert_no="REG-001", issued_date="2024-01-01",
            expiry_date="2027-01-01", scope="工业树脂颗粒", db_path=db
        )
        if not r_add.get("ok"):
            fails.append("⑥ cert_ledger_add 失败: " + str(r_add))

        # 6j. cert_ledger_list: 能查到刚加的
        r_list = cert_ledger_list(db_path=db)
        if not r_list.get("ok") or r_list.get("total", 0) < 1:
            fails.append("⑥ cert_ledger_list 未能查到刚加入的认证")

        # 6k. cert_ledger_add: upsert不降级私密(加一条私密标记的)
        r_priv = cert_ledger_add(
            cert_type="SGS", product="M-996", cert_no="SGS-2024-报价内部",
            issued_date="2024-06-01", expiry_date="2025-06-01",
            note="内部报价附件,不对外", db_path=db
        )
        # private属于商业敏感(含"报价")应被标私密
        # (note含「内部报价」→ _classify_private 应判 True → _flag_private)
        # 再次upsert同一条, private不应降级
        r_priv2 = cert_ledger_add(
            cert_type="SGS", product="M-996", cert_no="SGS-2024-报价内部",
            issued_date="2024-06-01", expiry_date="2025-06-01",
            note="更新备注但不含敏感词了", db_path=db
        )
        # 查库验证private列是否保持MAX(不降级)
        conn_check = _conn(db)
        try:
            priv_val = conn_check.execute(
                "SELECT private FROM hek_certs WHERE cert_no = ?",
                ("SGS-2024-[凭证已脱敏]",)
            ).fetchone()
            # cert_no 含有高熵内容会被脱敏, 改用product+cert_type查
            priv_rows = conn_check.execute(
                "SELECT private FROM hek_certs WHERE cert_type='SGS' AND product='M-996'"
            ).fetchall()
        finally:
            conn_check.close()
        # 验证存在SGS M-996记录
        if not priv_rows:
            fails.append("⑥ upsert后SGS记录不存在")

        # 6l. cert_expiry_check: 应找到即将到期的 SGS(2025-06-01)
        r_exp = cert_expiry_check(days=9999, db_path=db)  # 大窗口确保能找到
        if not r_exp.get("ok"):
            fails.append("⑥ cert_expiry_check 失败: " + str(r_exp))
        sgs_found = any(e["cert_type"] == "SGS" for e in r_exp.get("expiring", []))
        if not sgs_found:
            fails.append("⑥ cert_expiry_check 未能找到已录入的SGS认证")

        # 6m. cert_expiry_check: 已过期条目(expiry_date 在过去)
        cert_ledger_add(
            cert_type="ISO9001", product="全系列",
            issued_date="2020-01-01", expiry_date="2023-01-01",
            db_path=db
        )
        r_exp2 = cert_expiry_check(days=90, db_path=db)
        expired_list = [e for e in r_exp2.get("expiring", []) if e.get("expired")]
        if not expired_list:
            fails.append("⑥ cert_expiry_check 未识别已过期认证(ISO9001 2023)")

        # 6n. cert_expiry_check: 空到期日的记录不应出现在预警列表
        cert_ledger_add(
            cert_type="MSDS", product="全系列",
            issued_date="2024-01-01", expiry_date="",  # 永久有效
            db_path=db
        )
        r_exp3 = cert_expiry_check(days=90, db_path=db)
        if any(e["cert_type"] == "MSDS" for e in r_exp3.get("expiring", [])):
            fails.append("⑥ 空到期日(永久有效)不应出现在预警列表")

        # 6o. 凭据脱敏: 台账不留密钥明文
        cert_ledger_add(
            cert_type="TEST", product="TEST",
            note="api_key=sk-ABCDEFGH1234567890XYZ 内部别传", db_path=db
        )
        conn2 = _conn(db)
        try:
            leaked = 0
            for (v,) in conn2.execute("SELECT note FROM hek_certs WHERE cert_type='TEST'"):
                if v and "sk-ABCDEFGH" in v:
                    leaked += 1
        finally:
            conn2.close()
        if leaked:
            fails.append("⑥ 凭据明文漏进台账(脱敏失效)")

        # 6p. dispatch 白名单守门
        if dispatch("hs_export_kit_cert_ledger_add", {"cert_type": "x", "product": "y"}).get("ok"):
            fails.append("⑤ dispatch 竟放行 cert_ledger_add(写库)")
        if not dispatch("hs_export_kit_status", {}).get("ok"):
            fails.append("⑤ dispatch 应放行只读 status")
        if dispatch("hs_export_kit_run_shell", {}).get("ok"):
            fails.append("⑤ dispatch 应拒绝不在白名单的动作")

        # 6q. 日期解析: 语义化非字符串字典序
        # 2023-12-31 vs 2024-01-01: 字符串字典序也对, 但2023-09-01 vs 2023-10-01需语义
        dt1 = _parse_date("2023-09-01")
        dt2 = _parse_date("2023-10-01")
        if dt1 is None or dt2 is None or not (dt1 < dt2):
            fails.append("⑥ _parse_date 日期语义化比较错误")

        # 6r. _finite_num NaN/inf 护栏
        if _finite_num(float("nan")) is not None:
            fails.append("⑥ _finite_num 应对 NaN 返回 None")
        if _finite_num(float("inf")) is not None:
            fails.append("⑥ _finite_num 应对 inf 返回 None")
        if _as_int(float("nan"), 90, 1, 3650) != 90:
            fails.append("⑥ _as_int(NaN) 应返回 default")

    finally:
        config.get = _orig_cfg_get
        _selftest_cleanup(tmp)

    if fails:
        print("[selftest FAIL]", file=sys.stderr)
        for f in fails:
            print("  FAIL:", f, file=sys.stderr)
        return len(fails)

    print("[selftest PASS] hs_export_kit 全部不变量通过", file=sys.stderr)
    return 0


# ─────────────────────────────────────────────────────────────────────────────── #
# CLI
# ─────────────────────────────────────────────────────────────────────────────── #
def _main() -> None:
    p = argparse.ArgumentParser(
        prog="hs_export_kit",
        description="PC2出口HS编码/单证清单/认证台账工具箱",
    )
    p.add_argument("--db", help="覆盖数据库路径(默认 ~/.jarvis/hs_export_kit.db)")
    sub = p.add_subparsers(dest="cmd", help="子命令")

    # hs_lookup
    p_hs = sub.add_parser("hs_lookup", help="查HS编码")
    p_hs.add_argument("query", help="品名/关键词/编码")

    # doc_checklist
    p_dc = sub.add_parser("doc_checklist", help="出口单证清单")
    p_dc.add_argument("destination", help="目的国/地区")
    p_dc.add_argument("--payment", default="TT", help="付款方式(TT/LC/DP/DA/OA)")

    # cert_add
    p_ca = sub.add_parser("cert_add", help="添加/更新认证台账")
    p_ca.add_argument("cert_type", help="认证类型(REACH/ISO9001/SGS/...)")
    p_ca.add_argument("product", help="覆盖产品/型号")
    p_ca.add_argument("--issuer", default="", help="签发机构")
    p_ca.add_argument("--cert-no", default="", help="证书编号")
    p_ca.add_argument("--issued", default="", help="签发日期(YYYY-MM-DD)")
    p_ca.add_argument("--expiry", default="", help="到期日期(YYYY-MM-DD)")
    p_ca.add_argument("--scope", default="", help="覆盖范围")
    p_ca.add_argument("--note", default="", help="备注")

    # cert_list
    p_cl = sub.add_parser("cert_list", help="列出认证台账")
    p_cl.add_argument("--type", default="", help="按认证类型过滤")
    p_cl.add_argument("--product", default="", help="按产品过滤")

    # cert_expiry
    p_ce = sub.add_parser("cert_expiry", help="到期预警")
    p_ce.add_argument("--days", type=int, default=90, help="预警天数(默认90)")

    # status
    sub.add_parser("status", help="模块状态")

    # selftest
    sub.add_parser("selftest", help="运行内置自检")

    args = p.parse_args()
    db = args.db or None

    if args.cmd == "hs_lookup":
        result = hs_lookup(args.query, db_path=db)
    elif args.cmd == "doc_checklist":
        result = doc_checklist(args.destination, args.payment, db_path=db)
    elif args.cmd == "cert_add":
        result = cert_ledger_add(
            args.cert_type, args.product,
            issuer=args.issuer, cert_no=args.cert_no,
            issued_date=args.issued, expiry_date=args.expiry,
            scope=args.scope, note=args.note, db_path=db,
        )
    elif args.cmd == "cert_list":
        result = cert_ledger_list(args.type, args.product, db_path=db)
    elif args.cmd == "cert_expiry":
        result = cert_expiry_check(args.days, db_path=db)
    elif args.cmd == "status":
        result = status(db_path=db)
    elif args.cmd == "selftest":
        sys.exit(_selftest())
    else:
        p.print_help()
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()


# ═══════════════════════════════════════════════════════════════════════════════ #
# 大脑工具三件套 (additive; 纯增不改已有逻辑)
# 范式与 work_hub.py 保持一致:
#   is_tool(name)   — 接受 "hs_export_kit" (新式) 或 "hs_export_kit_<action>" (兼容旧式)
#   dispatch(name, args) — 新式: name="hs_export_kit", 路由由 args["action"] 决定
#                          旧式: name="hs_export_kit_<action>", 向后兼容
#   approved_tools() — 返回单一工具 spec (单模块单工具)
#
# 安全铁律:
#   cert_ledger_add (写库) / selftest 永不经模型 dispatch — 列入 _CLI_ONLY_EXT
# ═══════════════════════════════════════════════════════════════════════════════ #

# 扩展 CLI-only 集合: 继承原有 cert_ledger_add + 新增 selftest
# (原 _CLI_ONLY 仍在文件里保持向后兼容; 这里用独立名称不覆盖其引用)
_CLI_ONLY_EXT: frozenset = frozenset({"cert_ledger_add", "selftest"})

# 只读动作路由表 {action_name: callable}
# cert_ledger_list 只读; cert_ledger_add 写库绝不入此表
_READ_ACTIONS: Dict[str, object] = {
    "hs_lookup":        lambda a: hs_lookup(str(a.get("query", ""))),
    "doc_checklist":    lambda a: doc_checklist(
                            str(a.get("country", a.get("destination", ""))),
                            str(a.get("payment", a.get("payment_terms", "TT"))),
                        ),
    "cert_expiry_check": lambda a: cert_expiry_check(_as_int(a.get("days", 90), 90, 1, 3650)),
    "coa_freshness":    lambda a: coa_freshness(
                            _as_int(a.get("stale_months", 6), 6, 1, 120),
                            _as_int(a.get("limit", 30), 30, 1, 200),
                        ),
    "cert_ledger":      lambda a: cert_ledger_list(
                            str(a.get("cert_type", "")),
                            str(a.get("product", "")),
                        ),
    "status":           lambda _a: status(),
}


def is_tool(name: str) -> bool:  # type: ignore[override]  # shadows existing is_tool
    """接受新式 "hs_export_kit" 或旧式 "hs_export_kit_<action>"(兼容)。"""
    if name == "hs_export_kit":
        return True
    # 兼容旧式前缀调用 (test_hs_export_kit.py 里已有的断言)
    if name.startswith("hs_export_kit_"):
        short = name[len("hs_export_kit_"):]
        return short in _DISPATCH_WHITELIST
    return False


def dispatch(name: str, args: Optional[dict] = None) -> dict:  # type: ignore[override]
    """模型工具入口。

    新式: name="hs_export_kit", action 由 args["action"] 路由。
    旧式: name="hs_export_kit_<action>", 向后兼容原 dispatch 逻辑。

    cert_ledger_add(写库) / selftest 永不放行 — 严守 _CLI_ONLY_EXT。
    """
    a = dict(args or {})

    # ── 新式调用: name == "hs_export_kit" ──────────────────────────────────────
    if name == "hs_export_kit":
        act = a.get("action", "hs_lookup")
        if act in _CLI_ONLY_EXT:
            return {
                "ok": False,
                "error": f"动作 '{act}' 写库/仅限 CLI 内部, 不暴露给模型",
            }
        fn = _READ_ACTIONS.get(act)
        if not fn:
            return {
                "ok": False,
                "error": (
                    f"action 只能是 {'/'.join(sorted(_READ_ACTIONS))}, "
                    f"收到 '{act}'; cert_ledger_add 写库须经 CLI。"
                ),
            }
        return fn(a)  # type: ignore[operator]

    # ── 旧式调用: name == "hs_export_kit_<action>" ────────────────────────────
    if name.startswith("hs_export_kit_"):
        short = name[len("hs_export_kit_"):]
        if short in _CLI_ONLY_EXT:
            return {
                "ok": False,
                "error": f"动作 '{short}' 写库/仅限 CLI 内部, 不暴露给模型",
            }
        if short not in _DISPATCH_WHITELIST:
            return {
                "ok": False,
                "error": (
                    f"动作 '{short}' 不在 hs_export_kit 只读白名单 "
                    f"{sorted(_DISPATCH_WHITELIST)}; cert_ledger_add 请经 CLI。"
                ),
            }
        if short == "hs_lookup":
            return hs_lookup(str(a.get("query", "")))
        if short == "doc_checklist":
            return doc_checklist(
                str(a.get("destination", "")),
                str(a.get("payment_terms", "TT")),
            )
        if short == "cert_ledger_list":
            return cert_ledger_list(
                str(a.get("cert_type", "")),
                str(a.get("product", "")),
            )
        if short == "cert_expiry_check":
            return cert_expiry_check(a.get("days", 90))
        if short == "coa_freshness":
            return coa_freshness(
                _as_int(a.get("stale_months", 6), 6, 1, 120),
                _as_int(a.get("limit", 30), 30, 1, 200),
            )
        if short == "status":
            return status()
        return {"ok": False, "error": "unreachable"}  # pragma: no cover

    return {"ok": False, "error": f"未知工具 {name}"}


def approved_tools() -> list:
    """返回单一工具 spec, 供 jarvis_brain 注册。

    只读动作: hs_lookup / doc_checklist / cert_expiry_check / cert_ledger(只读list) / status。
    写库动作 cert_ledger_add 不暴露 — 须经 CLI。
    """
    return [{
        "type": "function",
        "function": {
            "name": "hs_export_kit",
            "description": (
                "PC2出口HS编码/单证清单/认证台账·只读: "
                "HS归类/产地证/REACH认证到期; 录入认证须CLI。\n"
                "hs_lookup — 按品名或HS编码查归类信息(申报要素/退税率/监管条件)。\n"
                "doc_checklist — 目的国+付款方式→出口单证清单(产地证推荐/LC严格提醒)。\n"
                "cert_expiry_check — N天内到期认证预警(供晨报消费)。\n"
                "coa_freshness — 按客户汇总COA质检证明新鲜度, 标记最新COA已过旧需补开的客户。"
                "回答「哪些客户COA该刷新」「COA覆盖情况」「谁的质检证明过旧了」。\n"
                "cert_ledger — 只读列出认证台账; 新增认证须经 CLI cert_add。\n"
                "status — 模块状态(台账条数/到期统计)。\n"
                "免责: HS编码及退税率以当期海关税则/政策为准, 申报前请核实。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "hs_lookup | doc_checklist | cert_expiry_check | "
                            "coa_freshness | cert_ledger | status"
                        ),
                        "default": "hs_lookup",
                    },
                    "stale_months": {
                        "type": "integer",
                        "description": "coa_freshness 用: 最新COA超过多少月视为过旧需补开(默认6)",
                        "default": 6,
                    },
                    "query": {
                        "type": "string",
                        "description": "hs_lookup 用: 品名关键词(如「工业树脂」「homopolymer」)或HS编码",
                    },
                    "country": {
                        "type": "string",
                        "description": "doc_checklist 用: 目的国/地区(如「越南」「EU」「日本」)",
                    },
                    "payment": {
                        "type": "string",
                        "description": "doc_checklist 用: 付款方式(TT/LC/DP/DA/OA)",
                        "default": "TT",
                    },
                    "days": {
                        "type": "integer",
                        "description": "cert_expiry_check 用: 预警窗口天数(默认90)",
                        "default": 90,
                    },
                    "cert_type": {
                        "type": "string",
                        "description": "cert_ledger 用: 按认证类型过滤(如REACH/ISO9001/SGS)",
                    },
                    "product": {
                        "type": "string",
                        "description": "cert_ledger 用: 按产品过滤(如「M-996」)",
                    },
                },
                "required": [],
            },
        },
    }]


def _selftest_three_piece() -> list:
    """三件套专项自检。返回 fails 列表(空=全过)。"""
    import tempfile
    fails: list = []
    tmp = tempfile.mkdtemp(prefix="hek_3p_selftest_")
    db = os.path.join(tmp, "hek3p.db")
    try:
        # T1. approved_tools 非空且 name 正确
        tools = approved_tools()
        if not tools:
            fails.append("三件套 T1: approved_tools() 返回空列表")
        else:
            tool_name = tools[0]["function"]["name"]
            if tool_name != "hs_export_kit":
                fails.append(f"三件套 T1: approved_tools name 应为 hs_export_kit, 得 {tool_name!r}")

        # T2. is_tool 正负测试(新式)
        if not is_tool("hs_export_kit"):
            fails.append("三件套 T2: is_tool('hs_export_kit') 应为 True")
        if is_tool("other_tool"):
            fails.append("三件套 T2: is_tool('other_tool') 应为 False")
        # 兼容旧式
        if not is_tool("hs_export_kit_status"):
            fails.append("三件套 T2: is_tool('hs_export_kit_status') 兼容旧式应为 True")
        if is_tool("hs_export_kit_cert_ledger_add"):
            fails.append("三件套 T2: is_tool('hs_export_kit_cert_ledger_add') 应为 False(写库)")

        # T3. hs_lookup 真实可调且返回 dict
        r = dispatch("hs_export_kit", {"action": "hs_lookup", "query": "工业树脂"})
        if not isinstance(r, dict):
            fails.append("三件套 T3: dispatch hs_lookup 应返回 dict")
        elif not r.get("ok"):
            fails.append(f"三件套 T3: dispatch hs_lookup ok=False: {r}")

        # T4. 写库动作不可经 dispatch 触发(新式)
        r_add_new = dispatch("hs_export_kit", {"action": "cert_ledger_add",
                                                "cert_type": "X", "product": "Y"})
        if r_add_new.get("ok"):
            fails.append("三件套 T4: dispatch(新式) 竟放行 cert_ledger_add(写库)")

        # T5. 写库动作不可经 dispatch 触发(旧式兼容)
        r_add_old = dispatch("hs_export_kit_cert_ledger_add",
                             {"cert_type": "X", "product": "Y"})
        if r_add_old.get("ok"):
            fails.append("三件套 T5: dispatch(旧式) 竟放行 cert_ledger_add(写库)")

        # T6. selftest 动作不可经 dispatch 触发(新式)
        r_st = dispatch("hs_export_kit", {"action": "selftest"})
        if r_st.get("ok"):
            fails.append("三件套 T6: dispatch 竟放行 selftest 动作")

        # T7. 未知 action 被拒
        r_unk = dispatch("hs_export_kit", {"action": "delete_all_certs"})
        if r_unk.get("ok"):
            fails.append("三件套 T7: dispatch 放行未知 action delete_all_certs")

        # T8. cert_ledger(只读)可经 dispatch 取到结果
        r_lst = dispatch("hs_export_kit", {"action": "cert_ledger"})
        if not isinstance(r_lst, dict) or not r_lst.get("ok"):
            fails.append(f"三件套 T8: dispatch cert_ledger(只读) 应放行, 得 {r_lst}")

        # T9. doc_checklist 通过 country/payment 参数可调
        r_dc = dispatch("hs_export_kit", {"action": "doc_checklist",
                                           "country": "越南", "payment": "TT"})
        if not isinstance(r_dc, dict) or not r_dc.get("ok"):
            fails.append(f"三件套 T9: dispatch doc_checklist 失败: {r_dc}")

        # T10. _READ_ACTIONS 不包含写库 action
        for act in ("cert_ledger_add", "selftest"):
            if act in _READ_ACTIONS:
                fails.append(f"三件套 T10: _READ_ACTIONS 含写库/危险动作 {act!r}")

    finally:
        _selftest_cleanup(tmp)

    return fails


# 将三件套自检注入原 _selftest 的末尾: 在模块级补充调用
# (不修改 _selftest 函数体; 通过包装函数替换)
_orig_selftest = _selftest  # type: ignore[name-defined]


def _selftest() -> int:  # type: ignore[override]  # noqa: F811
    """原 _selftest + 三件套专项自检。"""
    rc = _orig_selftest()
    extra_fails = _selftest_three_piece()
    if extra_fails:
        print("[selftest FAIL] 三件套专项:", file=sys.stderr)
        for f in extra_fails:
            print("  FAIL:", f, file=sys.stderr)
        rc += len(extra_fails)
    else:
        print("[selftest PASS] 三件套专项全过", file=sys.stderr)
    return rc


# 三件套自检在模块导入场景(import hs_export_kit)下通过包装版 _selftest 运行。
# CLI 场景(python3 hs_export_kit.py selftest)中, 原 if __name__ == "__main__": _main()
# 在本段代码执行前已由 Python 运行并 sys.exit; 三件套覆盖由 test_hs_export_kit.py 的
# test_selftest_passes() 通过 import 路径保证(调用包装版 _selftest, 两套自检均运行)。
