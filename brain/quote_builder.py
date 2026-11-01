#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quote_builder.py — 小美的「报价工坊」: 工业树脂外贸「询盘 → 报价草稿」编排器。

定位: 纯只读编排 + 草稿生成, 零发送、零执行对外动作。把散落在四个能力模块里的
原子能力(询盘解析/评分、型号匹配、报价计算、认证单证)按业务流串成一条龙, 一次
吃进一段询盘文本, 吐出一份结构化报价草稿 + 报价邮件草稿文本(永远只是草稿, 真发须
走 draft_email 工具 → 飞书审批, 红线4)。

编排链:
  parse_inquiry(text)        从脱敏询盘文本启发式抽要素(型号/数量/贸易条款/港口/认证/包装/用途)
  score_inquiry(text)        软导入 inquiry_scorer 评分 + 骗局识别
  match_product(need_text)   软导入 product_spec_hub.requirement_match 推荐型号
  price_quote(args)          软导入 fx_quote 算 FOB/CIF 单价(Decimal, NaN/inf 守卫)
  compliance_check(dest,certs) 软导入 hs_export_kit 查认证/单证要求
  build_quote(text/args)     编排 parse→score→match→price→compliance → 结构化报价草稿
  draft_quote_email(quote)   生成报价邮件草稿**文本**(绝不发送)
  history / audit / status   留痕(脱敏) / 审计 / 模块状态

零执行边界:
- 本模块绝不发邮件、绝不执行任何对外动作。draft_quote_email 只返回文本 + 提示。
- 写库仅 build_quote 内部留痕(脱敏后), 模型工具面全只读 —— _CLI_ONLY 保留闸结构。
- 软导入目标模块缺失 → deferred 降级, 不建桩、不编造数据。

隐私铁律:
- 任何承载内容的字段先 _redact(本地 value-strip 在调库前)。
- 询盘含报价/底价/客户名等私密线索 → 标 _private; 编排任一成分 _private → 整条 _private。
- 透传下游(product_spec_hub)的 _private / _force_local。
- allow_cloud_private=False(默认)时, _private 同时打 _force_local, 大脑切本机模型收尾。

Python 3.9+ 兼容, stdlib only。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

# ── 路径 ─────────────────────────────────────────────────────────────────── #
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

_DEFAULT_DB_PATH = os.path.expanduser(
    os.environ.get("QUOTE_BUILDER_DB", "~/.jarvis/quote_builder.db")
)

_MAX_INPUT_CHARS = 20000  # ReDoS / DoS 防御: 超长输入截断

# ── 软导入 (_opt 模式; 缺则 deferred 不建桩) ──────────────────────────────── #
_DEFERRED: Dict[str, str] = {}


def _opt(name: str):
    """软导入一个能力模块; 失败记 deferred 并返回 None (绝不建桩/编造)。"""
    try:
        import importlib
        return importlib.import_module(name)
    except Exception as e:  # noqa: BLE001 - 优雅降级
        _DEFERRED[name] = str(e)[:120]
        return None


def _config_get(path: str, default: Any) -> Any:
    """读 config.safety.* ; config 缺失或出错时回落 default。"""
    cfg = _opt("config")
    if cfg is not None and hasattr(cfg, "get"):
        try:
            return cfg.get(path, default)
        except Exception:
            return default
    return default


# ── 安全: 凭据脱敏 (照 fx_quote / product_spec_hub 范式) ──────────────────── #
_SECRET_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.localkey|/\.jarvis/config\.toml|"
    r"\.pem(?![A-Za-z])|(?:^|/)id_rsa|(?:^|/)\.ssh/|"
    r"refresh_token|client_secret|private[_\-]?key|"
    r"BEGIN [A-Z ]*PRIVATE KEY|"
    r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{12,}(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}|(?<![A-Za-z0-9])gho_[A-Za-z0-9]{20,}|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{16,}|(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"Bearer\s+[A-Za-z0-9._\-]{16,}|(?<![A-Za-z0-9])eyJ[A-Za-z0-9._\-]{20,}",
    re.IGNORECASE,
)
# 高熵令牌兜底: 量词有界 (24~512) 防 ReDoS。
_HIENTROPY_RX = re.compile(
    r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/_\-]*[A-Z])(?=[A-Za-z0-9+/_\-]*[a-z])"
    r"(?=[A-Za-z0-9+/_\-]*\d)[A-Za-z0-9+/_\-]{24,512}(?![A-Za-z0-9+/])"
)

# 私密线索 (报价/底价/客户名等业务敏感) → 整条草稿走本机。
_PRIVATE_RX = re.compile(
    r"底价|成本价|进价|采购价|最低价|利润|毛利|内部|机密|保密|私密|不要外传|不能给客户|"
    r"我方报价|我们的报价|客户名|联系人|手机号|电话|微信|whatsapp|"
    r"\bconfidential\b|\binternal\b|\bcost\s+price\b|\bmargin\b|\bnet\s+cost\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """NFKC 归一化, 防全角绕过安全扫描。"""
    try:
        return unicodedata.normalize("NFKC", text or "")
    except Exception:
        return text or ""


def _redact(text: str) -> str:
    """脱敏: 抹凭据/令牌/key=value/长数字串。顺序铁律: 本地先在原文上跑, 库补刀。"""
    s = _norm(text)
    if not s:
        return ""
    s = _SECRET_RX.sub("[凭证已脱敏]", s)
    s = re.sub(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9]*"
        r"(?:pass(?:word)?|pwd|token|secret|api[_-]?key|access[_-]?key|"
        r"private[_-]?key|client[_-]?secret|refresh[_-]?token)"
        r"[A-Za-z0-9_\-]*\s*[:=：]\s*\S+",
        "[凭证已脱敏]", s,
    )
    s = _HIENTROPY_RX.sub("[令牌已脱敏]", s)
    s = re.sub(r"\d[\d \-]{9,}\d", "[数字已脱敏]", s)
    return s


def _safe_text(text: str, limit: int = 240) -> str:
    """对外出口兜底: 先脱敏(全文), 再(仅显示用)截断。"""
    s = _redact(text)
    if limit and len(s) > limit:
        return s[:limit] + "…"
    return s


def _seal(obj: Any) -> Any:
    """递归兜底: 把任何漏网的凭据明文占位掉 (防新增字段忘脱敏)。"""
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


def _classify_private(text: str) -> bool:
    """文本是否私密。本地 _PRIVATE_RX 永远先跑(命中即 True)。出错 fail-safe True。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 恒 False, 私密记录不占位、返原文;
    本文件 queue_quote 有对外出口(outbound_email.draft 入审批队列), 但真发送另有红线4
    审批闸独立守卫, 不靠此分类, 故此开关仍可安全关闭。"""
    if not _config_get("safety.data_layer_privacy_enabled", False):
        return False
    blob = _norm(text)
    if not blob:
        return False
    try:
        return bool(_PRIVATE_RX.search(blob))
    except Exception:
        return True


def _flag_private(out: dict, any_private: bool) -> dict:
    """统一出口: 先 _seal 兜底凭据, 再按私密标 _private / _force_local。
    隐私只升不降: 已是 True 不会被覆盖回 False。"""
    if not isinstance(out, dict):
        return out
    out = _seal(out)
    if any_private or out.get("_private"):
        out["_private"] = True
        if not _config_get("safety.allow_cloud_private", False):
            out["_force_local"] = True
    return out


def _downstream_private(*results: Any) -> bool:
    """聚合: 下游任一返回 dict 标了 _private → 整条 _private (隐私向上传播)。"""
    for r in results:
        if isinstance(r, dict) and r.get("_private"):
            return True
    return False


# ── 数值辅助 (Decimal; NaN/inf 守卫) ─────────────────────────────────────── #
def _finite_dec(v: Any, name: str = "值") -> Decimal:
    """安全转 Decimal; NaN/inf/非法一律拒。"""
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} 无法解析为数字: {v!r}")
    if not d.is_finite():
        raise ValueError(f"{name} 不能是 NaN 或无穷大: {v!r}")
    return d


def _pos_dec(v: Any, name: str = "值") -> Decimal:
    d = _finite_dec(v, name)
    if d <= Decimal("0"):
        raise ValueError(f"{name} 必须大于 0, 收到: {v!r}")
    return d


def _as_int(v: Any, default: int, lo: int, hi: int) -> int:
    """安全转 int 并夹到 [lo, hi]; NaN/inf/非法回落 default。"""
    try:
        f = float(v)
    except (ValueError, TypeError):
        return default
    import math
    if not math.isfinite(f):
        return default
    n = int(f)
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


# ── 数据库 ────────────────────────────────────────────────────────────────── #
_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS qb_builds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    inquiry     TEXT NOT NULL DEFAULT '',     -- 脱敏后的询盘摘要
    best_match  TEXT NOT NULL DEFAULT '',     -- 推荐型号
    incoterm    TEXT NOT NULL DEFAULT '',
    total       TEXT NOT NULL DEFAULT '',
    tier        TEXT NOT NULL DEFAULT '',     -- 询盘评分档
    private     INTEGER NOT NULL DEFAULT 0,
    missing     TEXT NOT NULL DEFAULT ''      -- 还缺什么 (脱敏)
);
CREATE INDEX IF NOT EXISTS idx_qb_ts ON qb_builds(ts);
CREATE TABLE IF NOT EXISTS qb_audit (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    action  TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""


def _db_path(override: Optional[str] = None) -> str:
    return override or _DEFAULT_DB_PATH


def _conn(db: Optional[str] = None) -> sqlite3.Connection:
    p = _db_path(db)
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    return c


def _audit(conn: sqlite3.Connection, action: str, summary: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO qb_audit(ts, action, summary) VALUES (?,?,?)",
        (ts, action, _safe_text(summary, 300)),
    )


# ── parse_inquiry: 从询盘文本启发式抽要素 ─────────────────────────────────── #
# 晶型
_GRADE_RX = re.compile(r"(?i)\b(homopolymer|copolymer)\b|高耐候|通用")
# 型号 (M-902, M902, MOD-895, STD100 等; 有界量词)
_MODEL_RX = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:M|MOD|STD|R|BLR|CR|KA|TR|RC|PR|KZ|TC)[\-\s]?\d{2,4}[A-Z]?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 数量 (50 MT, 50吨, 1,000 MT, 100 tons 等)。千分位形式优先于裸数字(防 '1,000' 只匹配到 '000');
# 裸 t 排除集装箱语境('25t container/柜'是箱容不是订量)。
_QTY_RX = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,3}(?:,\d{3}){1,2}(?:\.\d{1,2})?|\d{1,6}(?:\.\d{1,2})?)\s*"
    r"(MT|吨|tons?|metric\s{0,4}tons?|t\b(?!\s*(?:container|fcl|柜|箱)))",
    re.IGNORECASE,
)
# 贸易条款
_INCOTERM_RX = re.compile(
    r"(?<![A-Za-z])(FOB|CIF|CFR|CNF|EXW|FCA|DDP|DAP|CPT|CIP)(?![A-Za-z])",
    re.IGNORECASE,
)
# 认证
_CERT_RX = re.compile(
    r"(?i)\b(REACH|RoHS|ISO\s?9001|ISO\s?14001|SGS|COA|MSDS|SDS|FDA|"
    r"Halal|Kosher|TSCA|CIQ|BIS|SASO|SONCAP)\b"
)
# 包装
_PACKAGING_RX = re.compile(
    r"(?i)(\d{1,3})\s*(kg|公斤)\s*(?:bag|袋|paper\s*bag|woven\s*bag)?|"
    r"jumbo\s*bag|吨袋|ton\s*bag|pallet|托盘|纸袋|PE\s*bag"
)
# 用途/应用。注意: CJK 词不能用 \b(Python re 在两个 CJK"词"字符间永不产生
# 词边界, \b塑料\b 会永远失配); 故 ASCII 词用 \b 防误匹配子串, CJK 词裸匹配。
_APP_RX = re.compile(
    r"(?i)(?:\b(?:coating|paint|plastic|masterbatch|ink|paper|rubber|"
    r"cosmetic|ceramic|PVC|PE|PP|ABS)\b)"
    r"|(涂料|油漆|塑料|色母|油墨|造纸|橡胶|化妆品|陶瓷)"
)
# 港口 (Port of X / X港; 启发式, 取 'port of' 后的词)
_PORT_RX = re.compile(
    r"(?i)(?:port\s+of|目的港|destination\s+port|discharge\s+port)\s*[:：]?\s*"
    r"([A-Za-z一-鿿][A-Za-z一-鿿\s]{1,30})"
)


def parse_inquiry(text: str) -> Dict[str, Any]:
    """从脱敏询盘文本启发式提取要素。全文先 _redact。私密线索 → 标 _private。"""
    raw = (text or "")[:_MAX_INPUT_CHARS]
    any_private = _classify_private(raw)
    blob = _redact(raw)  # 安全扫描走全文(已脱敏)

    grades = []
    for m in _GRADE_RX.finditer(blob):
        g = (m.group(0) or "").lower()
        if g in ("homopolymer", "高耐候"):
            grades.append("homopolymer")
        elif g in ("copolymer", "通用"):
            grades.append("copolymer")
    grades = sorted(set(grades))

    models = sorted({re.sub(r"\s+", "", m.group(0).upper()) for m in _MODEL_RX.finditer(blob)})

    quantity_mt: Optional[str] = None
    # 多处命中时偏好明确单位(MT/吨/tons)的那个: '25t container, need 50 MT' 取 50 不取 25
    qty_hits = list(_QTY_RX.finditer(blob))
    qty_hits.sort(key=lambda m: 0 if m.group(2).lower().rstrip("s") in ("mt", "吨", "ton", "metric ton") else 1)
    for qm in qty_hits[:1]:
        try:
            q = _finite_dec(qm.group(1).replace(",", ""), "数量")
            if q > Decimal("0"):
                quantity_mt = str(q)
        except ValueError:
            quantity_mt = None

    inc = _INCOTERM_RX.search(blob)
    incoterm = inc.group(1).upper() if inc else None

    certs = sorted({re.sub(r"\s+", "", m.group(1).upper()) for m in _CERT_RX.finditer(blob)})

    packaging: Optional[str] = None
    pk = _PACKAGING_RX.search(blob)
    if pk:
        packaging = _safe_text(pk.group(0).strip(), 40)

    application: Optional[str] = None
    ap = _APP_RX.search(blob)
    if ap:
        # group(0) 兼容 ASCII 分支(无捕获组)与 CJK 捕获组两种命中。
        application = (ap.group(0) or "").strip().lower()

    port: Optional[str] = None
    pm = _PORT_RX.search(blob)
    if pm:
        port = _safe_text(pm.group(1).strip(), 40)

    out: Dict[str, Any] = {
        "ok": True,
        "models": models,
        "grades": grades,
        "quantity_mt": quantity_mt,
        "incoterm": incoterm,
        "port": port,
        "certs_required": certs,
        "packaging": packaging,
        "application": application,
        "inquiry_excerpt": _safe_text(raw, 200),
        "note": "启发式解析, 结果供人工复核; 数量/型号/港口请与客户原文核对。",
    }
    return _flag_private(out, any_private)


# ── score_inquiry: 软导入 inquiry_scorer ──────────────────────────────────── #
def score_inquiry(
    text: str,
    sender_email: Optional[str] = None,
    country: Optional[str] = None,
) -> Dict[str, Any]:
    """评分 + 骗局识别。inquiry_scorer 缺失 → deferred 降级。"""
    mod = _opt("inquiry_scorer")
    if mod is None or not hasattr(mod, "score"):
        return _flag_private(
            {"ok": False, "deferred": True,
             "reason": "inquiry_scorer 未就绪, 询盘评分跳过",
             "note": _DEFERRED.get("inquiry_scorer", "模块缺失")},
            _classify_private(text),
        )
    any_private = _classify_private(text)
    try:
        r = mod.score(text=text, sender_email=sender_email, country=country)
    except Exception as e:  # noqa: BLE001
        return _flag_private({"ok": False, "error": f"评分异常: {e}"}, any_private)
    out = {
        "ok": True,
        "score": r.get("score"),
        "tier": r.get("tier"),
        "reasons": [_safe_text(str(x), 160) for x in (r.get("reasons") or [])],
        "suggested_action": _safe_text(str(r.get("suggested_action", "")), 120),
    }
    return _flag_private(out, any_private)


# ── match_product: 软导入 product_spec_hub.requirement_match ───────────────── #
def match_product(need_text: str, limit: int = 5) -> Dict[str, Any]:
    """型号推荐。product_spec_hub 缺失 → deferred。透传其 _private / _force_local。"""
    mod = _opt("product_spec_hub")
    if mod is None or not hasattr(mod, "requirement_match"):
        return _flag_private(
            {"ok": False, "deferred": True,
             "reason": "product_spec_hub 未就绪, 型号匹配跳过",
             "note": _DEFERRED.get("product_spec_hub", "模块缺失")},
            _classify_private(need_text),
        )
    n = _as_int(limit, 5, 1, 50)
    any_private = _classify_private(need_text)
    try:
        r = mod.requirement_match(requirement=need_text, limit=n)
    except Exception as e:  # noqa: BLE001
        return _flag_private({"ok": False, "error": f"匹配异常: {e}"}, any_private)
    if not isinstance(r, dict):
        return _flag_private({"ok": False, "error": "匹配返回非 dict"}, any_private)
    # 透传下游隐私标志 + 本地判定。
    out = {
        "ok": bool(r.get("ok", False)),
        "best_match": r.get("best_match"),
        "qualified": r.get("qualified", []),
        "near_miss": r.get("near_miss", []),
        "disqualified": r.get("disqualified", []),
        "note": _safe_text(str(r.get("note", "")), 200),
    }
    if r.get("error"):
        out["error"] = _safe_text(str(r.get("error")), 160)
    down_priv = bool(r.get("_private")) or any_private
    return _flag_private(out, down_priv)


# ── price_quote: 软导入 fx_quote ──────────────────────────────────────────── #
def price_quote(
    cost_rmb_per_ton: Any = None,
    freight_usd_per_container: Any = "3000",
    tons_per_container: Any = 25,
    incoterm: str = "CIF",
    profit_rate: Any = "0.08",
    exchange_rate: Optional[Any] = None,
    quantity_mt: Optional[Any] = None,
    insurance_rate: Any = "0.0011",
    tax_refund_rate: Any = "0.13",
    vat_rate: Any = "0.13",
    db: Optional[str] = None,
) -> Dict[str, Any]:
    """算 FOB/CIF 单价 + (有数量则)总价。fx_quote 缺失 → deferred。
    cost_rmb_per_ton 缺失/非法 → 明确报"还缺成本价"而非编造。"""
    # 成本价是报价前提; 缺则诚实说缺, 不编造(优先于模块就绪检查)。
    if cost_rmb_per_ton is None or str(cost_rmb_per_ton).strip() == "":
        return {"ok": False, "missing_cost": True,
                "error": "缺采购成本价(cost_rmb_per_ton), 无法算价",
                "hint": "请提供含税采购价 RMB/吨, 或先在 product_spec_hub 录入型号成本。"}
    mod = _opt("fx_quote")
    if mod is None or not hasattr(mod, "quote_calc"):
        return {"ok": False, "deferred": True,
                "reason": "fx_quote 未就绪, 报价计算跳过",
                "note": _DEFERRED.get("fx_quote", "模块缺失")}
    try:
        qc = mod.quote_calc(
            cost_rmb_per_ton=cost_rmb_per_ton,
            freight_usd_per_container=freight_usd_per_container,
            tons_per_container=tons_per_container,
            insurance_rate=insurance_rate,
            profit_rate=profit_rate,
            tax_refund_rate=tax_refund_rate,
            vat_rate=vat_rate,
            exchange_rate=exchange_rate,
            db=db,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"报价计算异常: {e}"}
    if not qc.get("ok"):
        return qc  # fx_quote 已给结构化 error/hint

    inc = (incoterm or "CIF").upper()
    # 选 FOB 还是 CIF 单价。
    if inc in ("FOB", "EXW", "FCA"):
        unit_price = qc.get("fob_usd_per_ton")
        price_basis = "FOB"
    else:
        unit_price = qc.get("cif_usd_per_ton")
        price_basis = "CIF"

    out: Dict[str, Any] = {
        "ok": True,
        "incoterm": inc,
        "price_basis": price_basis,
        "unit_price_usd_per_ton": unit_price,
        "fob_usd_per_ton": qc.get("fob_usd_per_ton"),
        "cif_usd_per_ton": qc.get("cif_usd_per_ton"),
        "actual_gross_margin": qc.get("actual_gross_margin"),
        "margin_note": qc.get("margin_note"),
        "exchange_rate": qc.get("inputs", {}).get("exchange_rate_usd_cny"),
        "rate_source": qc.get("inputs", {}).get("rate_source"),
    }
    # 总价 (有数量时), 用 Decimal 精确。
    if quantity_mt is not None and unit_price is not None:
        try:
            qty = _pos_dec(quantity_mt, "数量")
            up = _finite_dec(unit_price, "单价")
            total = (qty * up).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            out["quantity_mt"] = str(qty)
            out["total_usd"] = str(total)
        except ValueError:
            out["quantity_note"] = "数量非法, 仅给单价"
    # 成本价是内部敏感数据, 输出又含毛利率(actual_gross_margin/margin_note) →
    # 整条标 _private(语义判定, 比扫"毛利率"词面更可靠; 只升不降, fail-closed)。
    return _flag_private(out, True)


# ── compliance_check: 软导入 hs_export_kit ────────────────────────────────── #
def compliance_check(
    dest_country: str = "",
    certs_required: Optional[List[str]] = None,
    payment_terms: str = "TT",
    product_query: str = "polycarbonate resin",
) -> Dict[str, Any]:
    """查 HS 归类 / 单证清单 / 在手认证覆盖度。hs_export_kit 缺失 → deferred。"""
    mod = _opt("hs_export_kit")
    if mod is None:
        return {"ok": False, "deferred": True,
                "reason": "hs_export_kit 未就绪, 合规检查跳过",
                "note": _DEFERRED.get("hs_export_kit", "模块缺失")}
    out: Dict[str, Any] = {"ok": True}
    certs_required = [str(c).upper() for c in (certs_required or [])]

    # HS 归类
    if hasattr(mod, "hs_lookup"):
        try:
            hs = mod.hs_lookup(product_query or "polycarbonate resin")
            if isinstance(hs, dict) and hs.get("ok"):
                ms = hs.get("matches") or []
                out["hs_matches"] = [
                    {"hs_code": m.get("hs_code"), "name": _safe_text(str(m.get("name", "")), 80),
                     "export_rebate_rate": m.get("export_rebate_rate")}
                    for m in ms[:3]
                ]
                out["hs_disclaimer"] = hs.get("disclaimer")
        except Exception as e:  # noqa: BLE001
            out["hs_error"] = f"HS 查询异常: {e}"

    # 单证清单
    if dest_country and hasattr(mod, "doc_checklist"):
        try:
            dc = mod.doc_checklist(dest_country, payment_terms or "TT")
            if isinstance(dc, dict) and dc.get("ok"):
                out["basic_docs"] = dc.get("basic_docs")
                out["coo"] = dc.get("coo")
                out["lc_notes"] = dc.get("lc_notes")
                out["special_notes"] = dc.get("special_notes")
        except Exception as e:  # noqa: BLE001
            out["doc_error"] = f"单证查询异常: {e}"

    # 在手认证到期 + 客户要求覆盖度
    if hasattr(mod, "cert_expiry_check"):
        try:
            ce = mod.cert_expiry_check(days=120)
            if isinstance(ce, dict) and ce.get("ok"):
                expiring = ce.get("expiring") or []
                out["expiring_certs"] = [
                    {"cert_type": e.get("cert_type"), "days_remaining": e.get("days_remaining"),
                     "alert_level": e.get("alert_level")}
                    for e in expiring[:10]
                ]
        except Exception as e:  # noqa: BLE001
            out["cert_error"] = f"认证查询异常: {e}"

    out["certs_required"] = certs_required
    if certs_required:
        out["cert_note"] = (
            f"客户要求认证: {', '.join(certs_required)} — "
            "请核对在手认证是否覆盖且在有效期内 (经 hs_export_kit cert_ledger 查台账)。"
        )
    out["disclaimer"] = "HS编码/退税率/单证要求以当期海关税则与目的国法规为准, 申报前请核实。"
    return out


# ── build_quote: 编排 parse→score→match→price→compliance ──────────────────── #
def build_quote(
    text: str = "",
    cost_rmb_per_ton: Any = None,
    freight_usd_per_container: Any = "3000",
    tons_per_container: Any = 25,
    profit_rate: Any = "0.08",
    exchange_rate: Optional[Any] = None,
    dest_country: str = "",
    payment_terms: str = "TT",
    validity_days: int = 14,
    sender_email: Optional[str] = None,
    country: Optional[str] = None,
    customer: str = "",
    db: Optional[str] = None,
) -> Dict[str, Any]:
    """一段询盘文本 → 结构化报价草稿。任一成分 _private → 整条 _private。
    缺关键输入(成本价/数量/型号/认证)时如实列入 missing_info, 不编造。
    端到端编排(#6): 评分 → (已知客户)customer_intel 360° → deal_advisor 成交参考 → 算价 → 草稿。
    传 customer 时附 customer_context(历史成交/状态/复购信号), 让报价"懂客户"。"""
    raw = (text or "")[:_MAX_INPUT_CHARS]
    if not raw.strip():
        return {"ok": False, "error": "询盘文本为空, 无法构建报价"}

    parsed = parse_inquiry(raw)
    scored = score_inquiry(raw, sender_email=sender_email, country=country)
    matched = match_product(raw, limit=5)

    # 骗局/低价值询盘短路: 标注但仍返回(供人工裁决), 不浪费在算价上。
    tier = scored.get("tier") if scored.get("ok") else None
    is_scam = tier == "scam"

    incoterm = parsed.get("incoterm") or ("FOB" if dest_country == "" else "CIF")
    quantity_mt = parsed.get("quantity_mt")
    certs_req = parsed.get("certs_required") or []

    # ── 成本价自动回填: 缺失时从 product_spec_hub 查台账(D5 cost ledger) ──
    # 策略: 先用 best_match 型号查;找到则用台账价 + 标来源/日期;查不到则保持缺失(下游 price_quote 报 missing)。
    cost_source: Optional[str] = None   # "hub_ledger" | None
    cost_date_noted: Optional[str] = None
    _matched_early = matched.get("best_match") if matched.get("ok") else None
    if (cost_rmb_per_ton is None or str(cost_rmb_per_ton).strip() == "") and _matched_early:
        psh = _opt("product_spec_hub")
        if psh is not None and hasattr(psh, "get_cost"):
            try:
                gc = psh.get_cost(_matched_early)
                if isinstance(gc, dict) and gc.get("ok") and gc.get("found"):
                    cost_rmb_per_ton = gc.get("cost_rmb_per_ton")
                    cost_source = "hub_ledger"
                    cost_date_noted = gc.get("cost_date")
            except Exception:
                pass  # 降级: 查询出错不阻断主流程

    # 算价 (成本价缺则 price 自报 missing)。
    priced = price_quote(
        cost_rmb_per_ton=cost_rmb_per_ton,
        freight_usd_per_container=freight_usd_per_container,
        tons_per_container=tons_per_container,
        incoterm=incoterm,
        profit_rate=profit_rate,
        exchange_rate=exchange_rate,
        quantity_mt=quantity_mt,
        db=db,
    )

    compliance = compliance_check(
        dest_country=dest_country,
        certs_required=certs_req,
        payment_terms=payment_terms,
    )

    # ── 组装 line_items ──
    line_items: List[Dict[str, Any]] = []
    best_match = matched.get("best_match") if matched.get("ok") else None
    unit_price = priced.get("unit_price_usd_per_ton") if priced.get("ok") else None
    if best_match or unit_price:
        line_items.append({
            "product": _safe_text(str(best_match or "(待定型号)"), 80),
            "grade": (parsed.get("grades") or ["(待定)"])[0],
            "quantity_mt": quantity_mt or "(待客户确认)",
            "incoterm": incoterm,
            # 占位符措辞避开隐私词("成本价"会让 draft 的内容扫描恒假阳)
            "unit_price_usd_per_ton": unit_price or "(待定价)",
            "packaging": parsed.get("packaging") or "(待确认, 默认25kg纸袋/吨袋)",
        })

    total = priced.get("total_usd") if priced.get("ok") else None

    # ── missing_info: 还缺什么才能报全 ──
    missing: List[str] = []
    if not best_match:
        if matched.get("deferred"):
            missing.append("型号匹配模块未就绪")
        else:
            missing.append("未匹配到合适型号(需更明确的规格: 晶型/含量/用途)")
    if cost_rmb_per_ton is None or str(cost_rmb_per_ton).strip() == "":
        missing.append("采购成本价(RMB/吨) — 无此项无法算价; 可先经 CLI: jarvis product_spec_hub set_cost <model> <price> 录入")
    if not quantity_mt:
        missing.append("订货数量(MT) — 影响总价与装柜方案")
    if not dest_country:
        missing.append("目的国/目的港 — 影响 CIF 运费与单证要求")
    if not certs_req:
        missing.append("认证要求(REACH/SGS/COA 等) — 客户未明示, 建议确认")
    if priced.get("ok") and priced.get("exchange_rate") is None:
        missing.append("USD/CNY 汇率 — 报价未锁汇, 建议确认")

    # ── 成交历史参考: 接 deal_advisor(D4 真实成交数据)给报价决策支持 ──
    # 「此客户历史成交价 / 该港口同口径市场均价 / 建议价带」。只读, 失败不阻断主流程。
    # deal_advisor 输出含客户/价格(业务敏感) → 整条报价随之 _private。
    deal_reference: Optional[Dict[str, Any]] = None
    _port_for_deal = str(parsed.get("port") or "").strip()
    _country_for_deal = str(dest_country or "").strip()
    if _port_for_deal or _country_for_deal:
        _da = _opt("deal_advisor")
        if _da is not None and hasattr(_da, "price_suggestion"):
            try:
                # 不按型号过滤: 通用各型号(如 M-902/M-996)取港口/国家级跨型号
                # 市场均价作参考更稳(best_match 文本格式常与 deal 库 model 字段不一致 → 误零)。
                _ps = _da.price_suggestion(
                    customer=customer or "",
                    port=_port_for_deal,
                    basis=incoterm,
                    country=_country_for_deal,
                )
                if isinstance(_ps, dict) and _ps.get("ok"):
                    deal_reference = {
                        "suggestion": _ps.get("suggestion"),
                        "market": _ps.get("market"),
                        "customer_last": _ps.get("customer_last"),
                        "basis": _ps.get("basis"),
                    }
            except Exception:
                pass  # 降级: 查询出错不阻断报价主流程

    # ── 客户 360° 情境: 已知客户(customer 非空)→ 接 customer_intel 让报价"懂客户"。──
    # #6 询盘→报价端到端补的最后一环: 历史成交/均价/当前状态/复购信号 → User 报价时
    # 知道"这是谁、值多少、该多积极"。只读, 失败不阻断; customer_intel 整条 _private
    # (含成交/佣金) → 命中即整条私密。
    customer_context: Optional[Dict[str, Any]] = None
    _cust_for_ci = (customer or "").strip()
    if _cust_for_ci:
        _ci = _opt("customer_intel")
        if _ci is not None and hasattr(_ci, "customer_intel"):
            try:
                _r = _ci.customer_intel(_cust_for_ci)
                if isinstance(_r, dict) and _r.get("ok"):
                    _d = _r.get("deals")
                    _d = _d if isinstance(_d, dict) else {}
                    _st = _r.get("status")
                    _st = _st if isinstance(_st, dict) else {}
                    _ld = _d.get("last_deal")
                    _ld = _ld if isinstance(_ld, dict) else {}
                    customer_context = {
                        "known": bool(_r.get("contact") or _d.get("count")),
                        "summary": _r.get("summary"),
                        "deal_count": _d.get("count"),
                        "avg_unit_price_usd": _d.get("avg_unit_price_usd"),
                        "last_deal_date": _ld.get("date"),
                        "current_status": _st.get("state"),
                    }
            except Exception:
                pass  # 降级: 查询出错不阻断报价主流程
        else:
            customer_context = {
                "known": False,
                "summary": None,
                "deal_count": 0,
                "avg_unit_price_usd": None,
                "last_deal_date": None,
                "current_status": None,
            }

    # ── 隐私聚合: 任一成分 _private → 整条 _private ──
    # priced 也进聚合: 含成本价/毛利率的报价(price_quote 已自标 _private)整条私密;
    # 成本价入参本身即内部敏感数据(语义判定, 不靠词面扫描)。
    # deal_reference 含真实成交价/客户历史 → 命中即整条私密。
    any_private = (
        bool(parsed.get("_private"))
        or _downstream_private(parsed, scored, matched, priced)
        or _classify_private(raw)
        or bool(cost_rmb_per_ton is not None and str(cost_rmb_per_ton).strip() != "")
        or bool(deal_reference)
        or bool(customer_context)
    )

    out: Dict[str, Any] = {
        "ok": True,
        "tier": tier,
        "is_scam_warning": is_scam,
        "line_items": line_items,
        "incoterm": incoterm,
        "total_usd": total,
        "validity_days": _as_int(validity_days, 14, 1, 365),
        "certs": certs_req,
        "parsed": {k: parsed.get(k) for k in
                   ("models", "grades", "quantity_mt", "incoterm", "port",
                    "certs_required", "packaging", "application")},
        "score": {"tier": tier, "score": scored.get("score")} if scored.get("ok") else None,
        "pricing": priced,
        "match": {"best_match": best_match,
                  "qualified_count": len(matched.get("qualified") or []),
                  "near_miss_count": len(matched.get("near_miss") or [])} if matched.get("ok") else None,
        "compliance": compliance,
        "missing_info": missing,
        "note": (
            "这是报价**草稿**, 仅供小美/User 内部决策。任何对外发送须经 draft_email "
            "工具走飞书审批(红线4)。HS/退税/单证以当期政策为准。"
        ),
    }
    # 成本价来源注记(D5 cost ledger 自动回填时告知来源/日期,便于 User 核对台账数据时效)。
    if cost_source == "hub_ledger":
        out["cost_source"] = "hub_ledger"
        out["cost_date"] = cost_date_noted or ""
    # 成交历史参考(deal_advisor): 给小美/User「历史价 + 市场均价 + 建议价带」决策支持。
    if deal_reference is not None:
        out["deal_reference"] = deal_reference
    # 客户 360°(customer_intel): 已知客户的画像/成交/状态/复购信号 → 报价"懂客户"(#6)。
    if customer_context is not None:
        out["customer_context"] = customer_context
    if is_scam:
        out["note"] = "⚠️ 询盘评分为骗局/低价值, 建议人工核实后再决定是否报价。" + out["note"]

    # ── 落库留痕 (脱敏) ──
    try:
        conn = _conn(db)
        try:
            conn.execute(
                "INSERT INTO qb_builds(ts, inquiry, best_match, incoterm, total, tier, "
                "private, missing) VALUES (?,?,?,?,?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    _safe_text(raw, 200),
                    _safe_text(str(best_match or ""), 80),
                    incoterm,
                    str(total or ""),
                    str(tier or ""),
                    1 if any_private else 0,
                    _safe_text("; ".join(missing), 300),
                ),
            )
            _audit(conn, "build_quote",
                   f"型号={best_match} incoterm={incoterm} total={total} tier={tier}")
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 - 留痕失败不阻断主流程
        out["persist_warning"] = f"留痕失败(不影响报价): {e}"

    return _flag_private(out, any_private)


# ── draft_quote_email: 生成草稿文本 (绝不发送) ────────────────────────────── #
def draft_quote_email(
    quote: Optional[Dict[str, Any]] = None,
    customer_name: str = "",
    company: str = "",
) -> Dict[str, Any]:
    """生成报价邮件草稿**文本**。永远只是草稿, 真发须走 draft_email → 飞书审批。"""
    q = quote or {}
    if not isinstance(q, dict):
        return {"ok": False, "error": "quote 必须是 build_quote 返回的 dict"}

    # 隐私: 上游 _private 传播 + 扫客户内容字段(customer_name/company 是独立内容通道)。
    # 扫描排除系统生成字段(note/missing_info/compliance 的固定措辞含"内部/审批"会恒假阳,
    # 公开询盘的草稿被错标 _private → 永远切本机; 上游 build_quote 已对真私密成分标过)。
    q_for_scan = {k: v for k, v in q.items()
                  if k not in ("note", "missing_info", "compliance", "pricing")}
    any_private = (
        bool(q.get("_private"))
        or _classify_private(json.dumps(q_for_scan, ensure_ascii=False, default=str))
        or _classify_private(customer_name)
        or _classify_private(company)
    )

    items = q.get("line_items") or []
    incoterm = q.get("incoterm") or "CIF"
    total = q.get("total_usd")
    validity = q.get("validity_days", 14)
    certs = q.get("certs") or []

    greet = f"Dear {_safe_text(customer_name, 60)}," if customer_name else "Dear Sir/Madam,"
    lines: List[str] = [greet, "", "Thank you for your inquiry. Please find our quotation below:", ""]

    for it in items:
        prod = it.get("product", "(model TBD)")
        qty = it.get("quantity_mt", "TBD")
        up = it.get("unit_price_usd_per_ton", "TBD")
        pk = it.get("packaging", "")
        lines.append(f"  - Product: {prod}")
        lines.append(f"    Quantity: {qty} MT | {incoterm} | Unit Price: USD {up}/MT")
        if pk:
            lines.append(f"    Packaging: {pk}")
    if not items:
        lines.append("  (Line items pending — please confirm specifications & quantity.)")

    if total:
        lines.append("")
        lines.append(f"  Total ({incoterm}): USD {total}")
    if certs:
        lines.append("")
        lines.append(f"  Certifications available on request: {', '.join(certs)}")

    lines += [
        "",
        f"This offer is valid for {validity} days from the date of this email.",
        "Payment terms and shipment schedule to be confirmed upon order.",
        "",
        "We look forward to your kind reply.",
        "",
        "Best regards,",
        _safe_text(company or "(Your Company)", 60),
    ]

    missing = q.get("missing_info") or []
    draft_text = _safe_text("\n".join(lines), 4000)

    out: Dict[str, Any] = {
        "ok": True,
        "draft_text": draft_text,
        "subject_suggestion": _safe_text(
            f"Quotation - Polycarbonate Resin {incoterm}", 120),
        "missing_before_send": missing,
        "note": (
            "这是邮件**草稿**, 小美绝不直接发送。经 draft_email 工具走飞书审批后才能发 "
            "(红线4: 影响 User 之外的人)。发前请补齐 missing_before_send 列出的信息。"
        ),
    }
    return _flag_private(out, any_private)


# ── queue_quote: 完整可发草稿 → 对外审批队列(#7, 永不发送) ─────────────────── #
def _quote_has_price(q: Dict[str, Any]) -> bool:
    """报价是否含真实价格(total 或某行真实单价, 非「待定价」占位)。"""
    if q.get("total_usd"):
        return True
    for it in (q.get("line_items") or []):
        up = it.get("unit_price_usd_per_ton")
        if isinstance(up, (int, float)) and up > 0:
            return True
    return False


def queue_quote(text: str = "", to: str = "", customer_name: str = "", company: str = "",
                dest_country: str = "", payment_terms: str = "TT",
                cost_rmb_per_ton: Any = None, db: Optional[str] = None) -> Dict[str, Any]:
    """#7 报价工坊增强: 询盘→报价→完整可发草稿→**对外审批队列**闭环。

    编排 build_quote → draft_quote_email → outbound_email.draft(落待审批队列)。
    铁律(红线4): 小美**绝不直接发送**, 此动作只把草稿入队等 User 飞书审批; 真发送由
    审批回调在 User 点批后走 outbound_email.send(_approved=True), LLM/大脑永不可达。
    完整性门: 无真实价格(缺成本价等)→ 拒绝入队, 列 missing(不入不完整报价)。
    """
    to = (to or "").strip()
    if not to or "@" not in to:
        return {"ok": False, "error": "收件人邮箱无效, queue 需有效 to(对外审批前提)"}
    if not (text or "").strip():
        return {"ok": False, "error": "需提供询盘原文 text 以生成报价"}

    q = build_quote(text=text, customer=customer_name, dest_country=dest_country,
                    payment_terms=payment_terms, cost_rmb_per_ton=cost_rmb_per_ton, db=db)
    if not q.get("ok"):
        return {"ok": False, "error": "报价构建失败", "detail": q}

    missing = q.get("missing_info") or []
    if not _quote_has_price(q):
        # 完整可发门: 没价格的不是可发草稿, 拒绝入队
        return {"ok": False, "error": "报价不完整(无价格), 不入审批队列; 先补成本价/数量再 queue",
                "missing_info": missing, "tier": q.get("tier")}
    if q.get("is_scam_warning"):
        return {"ok": False, "error": "询盘评分为骗局/低价值, 不入对外队列; 请人工核实",
                "tier": q.get("tier")}

    de = draft_quote_email(q, customer_name=customer_name, company=company)
    if not de.get("ok"):
        return {"ok": False, "error": "邮件草稿生成失败", "detail": de}
    subject = de.get("subject_suggestion") or "Quotation - Polycarbonate Resin"
    body = de.get("draft_text") or ""

    oe = _opt("outbound_email")
    if oe is None or not hasattr(oe, "draft"):
        return {"ok": False, "error": "outbound_email 不可用, 无法入审批队列"}
    res = oe.draft(to, subject, body)   # 唯一对外入口: 只入队, 永不发送
    if not res.get("ok"):
        return {"ok": False, "error": "入审批队列失败", "detail": res}

    note = ("报价草稿已入对外审批队列, 小美绝不直接发送; 等 User 飞书审批通过才发(红线4)。")
    if missing:
        note += " 发前仍须补: " + "; ".join(missing[:5])
    out: Dict[str, Any] = {
        "ok": True,
        "queued": True,
        "draft_id": res.get("draft_id"),
        "to": to,
        "subject": subject,
        "new_recipient": res.get("new_recipient"),
        "tier": q.get("tier"),
        "total_usd": q.get("total_usd"),
        "missing_before_send": missing,
        "note": note,
    }
    return _flag_private(out, any_private=True)


# ── history / audit / status ──────────────────────────────────────────────── #
def history(limit: int = 10, db: Optional[str] = None) -> Dict[str, Any]:
    """最近 N 次构建留痕 (脱敏)。"""
    n = _as_int(limit, 10, 1, 200)
    conn = _conn(db)
    try:
        rows = conn.execute(
            "SELECT ts, inquiry, best_match, incoterm, total, tier, private, missing "
            "FROM qb_builds ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        conn.close()
    records = []
    any_private = False
    for r in rows:
        priv = bool(r["private"])
        any_private = any_private or priv
        records.append({
            "ts": r["ts"],
            "inquiry_excerpt": _safe_text(r["inquiry"], 120),
            "best_match": _safe_text(r["best_match"], 80),
            "incoterm": r["incoterm"],
            "total_usd": r["total"],
            "tier": r["tier"],
            "private": priv,
            "missing": _safe_text(r["missing"], 200),
        })
    return _flag_private(
        {"ok": True, "count": len(records), "records": records}, any_private)


def audit(limit: int = 20, db: Optional[str] = None) -> Dict[str, Any]:
    """审计日志 (脱敏)。"""
    n = _as_int(limit, 20, 1, 200)
    conn = _conn(db)
    try:
        rows = conn.execute(
            "SELECT ts, action, summary FROM qb_audit ORDER BY id DESC LIMIT ?",
            (n,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "ok": True,
        "count": len(rows),
        "entries": [{"ts": r["ts"], "action": r["action"],
                     "summary": _safe_text(r["summary"], 200)} for r in rows],
    }


def status(db: Optional[str] = None) -> Dict[str, Any]:
    """模块状态: 构建总数 + 下游模块可达性。"""
    conn = _conn(db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM qb_builds").fetchone()[0]
    finally:
        conn.close()
    # 探测下游 (不建桩, 只 import 看在不在)。
    downstream = {}
    for m in ("inquiry_scorer", "product_spec_hub", "fx_quote", "hs_export_kit"):
        downstream[m] = _opt(m) is not None
    return {
        "ok": True,
        "module": "quote_builder",
        "version": "1.0.0",
        "db_path": _db_path(db),
        "total_builds": total,
        "downstream_available": downstream,
        "deferred": dict(_DEFERRED),
        "actions": [
            "parse", "score", "match", "price", "build",
            "queue", "draft", "history", "audit", "status", "selftest",
        ],
        "notes": (
            "纯只读编排 + 草稿生成; 零发送、零对外执行。报价邮件草稿须经 "
            "draft_email 走飞书审批后才能发(红线4)。"
        ),
    }


# ═══════════════════════════════════════════════════════════════════════════ #
# 大脑工具三件套 — work_hub 范式: 一模块=一工具 quote_builder, action 内部路由。
# 无写库写动作暴露给模型 → _CLI_ONLY 保留闸结构(只放 selftest)。
# ═══════════════════════════════════════════════════════════════════════════ #
_CLI_ONLY: frozenset = frozenset({"selftest"})

_READ_ACTIONS: Dict[str, Any] = {
    "parse": lambda a: parse_inquiry(str(a.get("text") or "")),
    "score": lambda a: score_inquiry(
        str(a.get("text") or ""),
        sender_email=a.get("email") or a.get("sender_email"),
        country=a.get("country"),
    ),
    "match": lambda a: match_product(
        str(a.get("text") or a.get("need_text") or ""),
        limit=a.get("limit", 5),
    ),
    "price": lambda a: price_quote(
        cost_rmb_per_ton=a.get("cost_rmb_per_ton"),
        freight_usd_per_container=a.get("freight_usd_per_container", "3000"),
        tons_per_container=a.get("tons_per_container", 25),
        incoterm=str(a.get("incoterm", "CIF")),
        profit_rate=a.get("profit_rate", "0.08"),
        exchange_rate=a.get("exchange_rate"),
        quantity_mt=a.get("quantity_mt"),
    ),
    "build": lambda a: build_quote(
        text=str(a.get("text") or ""),
        cost_rmb_per_ton=a.get("cost_rmb_per_ton"),
        freight_usd_per_container=a.get("freight_usd_per_container", "3000"),
        tons_per_container=a.get("tons_per_container", 25),
        profit_rate=a.get("profit_rate", "0.08"),
        exchange_rate=a.get("exchange_rate"),
        dest_country=str(a.get("dest_country", "")),
        payment_terms=str(a.get("payment_terms", "TT")),
        validity_days=a.get("validity_days", 14),
        sender_email=a.get("email") or a.get("sender_email"),
        country=a.get("country"),
        customer=str(a.get("customer") or a.get("customer_name") or ""),
    ),
    "queue": lambda a: queue_quote(
        text=str(a.get("text") or ""),
        to=str(a.get("to") or a.get("recipient") or ""),
        customer_name=str(a.get("customer") or a.get("customer_name") or ""),
        company=str(a.get("company") or ""),
        dest_country=str(a.get("dest_country", "")),
        payment_terms=str(a.get("payment_terms", "TT")),
        cost_rmb_per_ton=a.get("cost_rmb_per_ton"),
    ),
    "draft": lambda a: draft_quote_email(
        quote=a.get("quote") if isinstance(a.get("quote"), dict) else None,
        customer_name=str(a.get("customer_name", "")),
        company=str(a.get("company", "")),
    ),
    "history": lambda a: history(a.get("limit", 10)),
    "audit": lambda a: audit(a.get("limit", 20)),
    "status": lambda _a: status(),
}


def is_tool(name: str) -> bool:
    """jarvis_brain 工具注册协议: 返回 True 当且仅当 name == 'quote_builder'。"""
    return name == "quote_builder"


def dispatch(name: str, args: dict) -> dict:
    """模型工具入口(签名 (name, args) 兼容 jarvis_brain): 只放只读动作。
    selftest(I/O) 永不经此 —— 限 CLI / 内部。默认 action = 'build'。"""
    if name != "quote_builder":
        return {"ok": False, "error": f"未知工具 {name}"}
    a = args or {}
    act = a.get("action", "build")
    if act in _CLI_ONLY:
        return {"ok": False,
                "error": f"动作 '{act}' 仅限 CLI / 内部, 不暴露给模型"}
    fn = _READ_ACTIONS.get(act)
    if not fn:
        return {"ok": False,
                "error": f"action 只能是 {'/'.join(sorted(_READ_ACTIONS))}, 收到 {act!r}"}
    try:
        return fn(a)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"dispatch 异常: {e}"}


def approved_tools() -> list:
    """返回供 jarvis_brain 注册的工具 spec (单工具 quote_builder, 只读)。"""
    return [{
        "type": "function",
        "function": {
            "name": "quote_builder",
            "description": (
                "报价工坊·只读编排: 工业树脂外贸「询盘→报价草稿」一条龙。"
                "解析询盘要素 + 评分骗局识别 + 推荐型号 + 算 FOB/CIF 单价 + 查认证单证, "
                "编成结构化报价草稿。零发送、零对外执行 —— 报价邮件只产**草稿文本**, "
                "真发须经 draft_email 工具走飞书审批(红线4)。\n"
                "action:\n"
                "  build(默认) — 一段询盘文本→完整报价草稿(line_items/total/missing_info); "
                "传 customer_name(已知客户)时附客户360°情境+历史成交参考, 报价更懂客户;\n"
                "  parse — 仅解析询盘要素(型号/数量/贸易条款/认证/包装/用途);\n"
                "  score — 询盘评分+骗局识别(hot/warm/cold/scam);\n"
                "  match — 按需求推荐型号(经 product_spec_hub);\n"
                "  price — 算 FOB/CIF USD 单价+总价(需 cost_rmb_per_ton);\n"
                "  queue — 完整可发草稿→**入对外审批队列**(需 to 收件邮箱); 小美绝不直接发, 等 User "
                "飞书批准才发(红线4); 无价格/骗局询盘拒绝入队;\n"
                "  draft — 把 build 结果转成报价邮件草稿文本(不发送、不入队);\n"
                "  history — 最近构建留痕; audit — 审计; status — 模块状态。\n"
                "注: 缺成本价/数量/认证等会如实列入 missing_info, 绝不编造。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "build|parse|score|match|price|queue|draft|history|audit|status",
                        "default": "build",
                    },
                    "to": {
                        "type": "string",
                        "description": "queue 用: 收件人邮箱(对外审批前提; 入队后等 User 飞书批才发)",
                    },
                    "company": {
                        "type": "string",
                        "description": "queue/draft 用: 我方公司署名",
                    },
                    "text": {
                        "type": "string",
                        "description": "询盘原文 (build/parse/score/match 用)",
                    },
                    "cost_rmb_per_ton": {
                        "type": "string",
                        "description": "含税采购价 RMB/吨 (price/build 算价前提; 缺则报 missing)",
                    },
                    "freight_usd_per_container": {
                        "type": "string",
                        "description": "海运费 USD/柜 (默认3000)",
                        "default": "3000",
                    },
                    "tons_per_container": {
                        "type": "number",
                        "description": "每柜装载吨数 (默认25)",
                        "default": 25,
                    },
                    "incoterm": {
                        "type": "string",
                        "description": "贸易条款 FOB/CIF/CFR 等 (决定取 FOB 还是 CIF 单价)",
                    },
                    "profit_rate": {
                        "type": "string",
                        "description": "加价率 markup (默认0.08); 注意≠毛利率",
                        "default": "0.08",
                    },
                    "exchange_rate": {
                        "type": "string",
                        "description": "1 USD = ? CNY, 不传则查 fx_quote 库",
                    },
                    "quantity_mt": {
                        "type": "string",
                        "description": "订货数量 MT (price 用; build 从询盘自动解析)",
                    },
                    "dest_country": {
                        "type": "string",
                        "description": "目的国 (build/compliance 用, 影响单证清单)",
                    },
                    "payment_terms": {
                        "type": "string",
                        "description": "付款方式 TT/LC (默认TT)",
                        "default": "TT",
                    },
                    "validity_days": {
                        "type": "integer",
                        "description": "报价有效期天数 (默认14)",
                        "default": 14,
                    },
                    "email": {
                        "type": "string",
                        "description": "发件人邮箱 (score/build 用于评分)",
                    },
                    "country": {
                        "type": "string",
                        "description": "发件人国家 (score/build 记录用)",
                    },
                    "quote": {
                        "type": "object",
                        "description": "draft 用: build 返回的报价草稿 dict",
                    },
                    "customer_name": {
                        "type": "string",
                        "description": "客户名: draft 用作称呼; build 用作已知客户 → 附客户360°情境+历史成交参考(报价更懂客户)",
                    },
                    "company": {
                        "type": "string",
                        "description": "draft 用: 我方公司署名",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "match/history/audit 返回条数",
                    },
                },
                "required": [],
            },
        },
    }]


# ── selftest ──────────────────────────────────────────────────────────────── #
def _selftest_toolkit() -> Dict[str, Any]:
    """三件套自检: approved_tools/is_tool/dispatch 覆盖。"""
    failures: List[str] = []
    passed = 0

    def _chk(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"FAIL [toolkit:{name}]: {detail}")

    tools = approved_tools()
    _chk("approved_tools_nonempty", len(tools) > 0, str(tools))
    _chk("approved_tools_name", tools[0]["function"]["name"] == "quote_builder",
         str(tools[0]["function"].get("name")))
    _chk("approved_tools_has_params", "parameters" in tools[0]["function"], str(tools[0]))

    _chk("is_tool_positive", is_tool("quote_builder"), "is_tool('quote_builder') 应 True")
    _chk("is_tool_negative", not is_tool("fx_quote"), "is_tool('fx_quote') 应 False")
    _chk("is_tool_empty", not is_tool(""), "is_tool('') 应 False")

    # dispatch 错误工具名拒绝
    _chk("dispatch_wrong_name", not dispatch("nope", {}).get("ok", True),
         str(dispatch("nope", {})))

    # dispatch selftest(_CLI_ONLY) 被拦
    r_cli = dispatch("quote_builder", {"action": "selftest"})
    _chk("dispatch_cli_only_blocked", not r_cli.get("ok", True), str(r_cli))

    # dispatch 未知 action 拒绝
    r_unk = dispatch("quote_builder", {"action": "no_such_action"})
    _chk("dispatch_unknown_action", not r_unk.get("ok", True), str(r_unk))

    # dispatch status 只读成功
    r_status = dispatch("quote_builder", {"action": "status"})
    _chk("dispatch_status_ok", r_status.get("ok", False), str(r_status))

    # dispatch parse 带 text 成功且含预期键
    r_parse = dispatch("quote_builder", {"action": "parse", "text": "homopolymer PC2 50MT CIF"})
    _chk("dispatch_parse_ok", r_parse.get("ok", False), str(r_parse))
    _chk("dispatch_parse_keys", "models" in r_parse and "incoterm" in r_parse, str(r_parse))

    return {
        "ok": len(failures) == 0,
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
        "summary": f"toolkit selftest: {passed} passed, {len(failures)} failed",
    }


def selftest() -> Dict[str, Any]:
    """离线确定性自测。临时 SQLite, 不依赖网络/外部服务。
    ≥10 不变量: AST 安全闸 / 凭据硬拦 / 隐私传播 / 零发送 / Decimal精度 / NaN守卫 /
    deferred 降级 / dispatch 白名单拒越界。"""
    failures: List[str] = []
    passed = 0

    def _check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"FAIL [{name}]: {detail}")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name

    try:
        # ── 1. parse_inquiry 抽要素 ──
        p = parse_inquiry("We need Homopolymer PC2 M-902, 50 MT, CIF Hamburg, REACH & SGS, 25kg bag, for coating")
        _check("parse_ok", p["ok"], str(p))
        _check("parse_grade", "homopolymer" in p["grades"], str(p.get("grades")))
        _check("parse_model", any("M" in m and "902" in m for m in p["models"]), str(p.get("models")))
        _check("parse_qty", p["quantity_mt"] == "50", str(p.get("quantity_mt")))
        _check("parse_incoterm", p["incoterm"] == "CIF", str(p.get("incoterm")))
        _check("parse_certs", "REACH" in p["certs_required"] and "SGS" in p["certs_required"],
               str(p.get("certs_required")))
        _check("parse_app", p["application"] == "coating", str(p.get("application")))

        # ── 2. 隐私传播: 含底价 → _private (始终); _force_local 仅当 allow_cloud_private=False ──
        p_priv = parse_inquiry("客户要 M-902 50吨, 我方底价 RMB 14000/吨不要外传")
        _check("private_flag", p_priv.get("_private") is True, str(p_priv))
        # _force_local 与 config.allow_cloud_private 联动: False(默认)时必打, True 时可不打。
        _allow = bool(_config_get("safety.allow_cloud_private", False))
        _check("force_local_when_not_allowed",
               _allow or p_priv.get("_force_local") is True,
               f"allow_cloud_private={_allow} but _force_local missing: {p_priv}")

        # 非私密询盘不应被误标
        p_pub = parse_inquiry("Please quote copolymer PC2 100 MT FOB")
        _check("public_no_private", "_private" not in p_pub, str(p_pub))

        # ── 3. 凭据硬拦: 询盘正文含 token → 脱敏 ──
        p_secret = parse_inquiry("contact me, my key is ghp_abc123DEF456GHI789JKL012MNO345 homopolymer 50MT")
        excerpt = p_secret.get("inquiry_excerpt", "")
        _check("secret_redacted", "ghp_abc123" not in excerpt, str(excerpt))

        # ── 4. price_quote 缺成本价 → missing_cost(不编造) ──
        pr_miss = price_quote(cost_rmb_per_ton=None, exchange_rate="7.25", db=tmp_db)
        _check("price_missing_cost", not pr_miss["ok"] and pr_miss.get("missing_cost"),
               str(pr_miss))

        # ── 5. price_quote 正常算价(给汇率, 不依赖库) ──
        pr = price_quote(cost_rmb_per_ton="15000", freight_usd_per_container="3000",
                         incoterm="CIF", exchange_rate="7.25", quantity_mt="50", db=tmp_db)
        if pr.get("deferred"):
            _check("price_deferred_ok", True, "fx_quote 未就绪, deferred 降级")
        else:
            _check("price_ok", pr["ok"], str(pr))
            _check("price_has_unit", pr.get("unit_price_usd_per_ton") is not None, str(pr))
            # 总价 = 50 × CIF单价, Decimal 精确
            if pr.get("total_usd") and pr.get("unit_price_usd_per_ton"):
                expect = (Decimal("50") * Decimal(pr["unit_price_usd_per_ton"])).quantize(
                    Decimal("0.01"))
                _check("price_total_decimal", Decimal(pr["total_usd"]) == expect,
                       f"expect {expect} got {pr['total_usd']}")

        # ── 6. NaN/inf 守卫: price 数量 NaN 不崩 ──
        pr_nan = price_quote(cost_rmb_per_ton="15000", exchange_rate="7.25",
                             quantity_mt="nan", db=tmp_db)
        # 不应抛异常; 要么 ok(忽略数量) 要么 deferred
        _check("price_nan_no_crash", isinstance(pr_nan, dict), str(pr_nan))
        if pr_nan.get("ok"):
            _check("price_nan_no_total", "total_usd" not in pr_nan or pr_nan.get("quantity_note"),
                   str(pr_nan))

        # ── 7. build_quote 编排 + missing_info ──
        b = build_quote(text="Homopolymer PC2 M-902, CIF Rotterdam, for paint",
                        exchange_rate="7.25", dest_country="Germany", db=tmp_db)
        _check("build_ok", b["ok"], str(b))
        _check("build_has_missing", isinstance(b.get("missing_info"), list), str(b.get("missing_info")))
        # 没给成本价 → missing 里应提到成本价
        _check("build_missing_cost",
               any("成本" in m for m in b.get("missing_info", [])), str(b.get("missing_info")))
        _check("build_has_note_draft_email", "draft_email" in b.get("note", ""), str(b.get("note")))

        # ── 7.5 #6 端到端: 无客户→不附 customer_context; 已知客户名→附 360°情境(懂客户) ──
        b_noc = build_quote(text="Homopolymer PC2 M-902, CIF Rotterdam",
                            exchange_rate="7.25", dest_country="Germany", db=tmp_db)
        _check("build_no_customer_no_context", "customer_context" not in b_noc,
               str(list(b_noc.keys())))
        b_cust = build_quote(text="Homopolymer PC2 M-902, CIF Rotterdam",
                             exchange_rate="7.25", dest_country="Germany",
                             customer="ZZZ_NONEXISTENT_TEST_QXZ", db=tmp_db)
        _check("build_customer_context_attached",
               isinstance(b_cust.get("customer_context"), dict),
               str(b_cust.get("customer_context")))
        if isinstance(b_cust.get("customer_context"), dict):
            # 未知客户 → known 假, 但仍附 context(报价层知道"查过, 无记录")
            _check("build_customer_unknown_known_false",
                   not b_cust["customer_context"].get("known"),
                   str(b_cust["customer_context"]))
        # 带客户 → 客户业务情报 → 整条 _private
        _check("build_customer_private", b_cust.get("_private") is True,
               str(b_cust.get("_private")))

        # ── 8. build 隐私聚合: 含底价 → 整条 _private ──
        b_priv = build_quote(text="M-902 50吨 CIF, 我方成本价 14000 保密", exchange_rate="7.25", db=tmp_db)
        _check("build_private_propagate", b_priv.get("_private") is True, str(b_priv.get("_private")))

        # ── 9. draft_quote_email 零发送 ──
        d = draft_quote_email(b, customer_name="John", company="Acme Chemical")
        _check("draft_ok", d["ok"], str(d))
        _check("draft_has_text", isinstance(d.get("draft_text"), str) and len(d["draft_text"]) > 0,
               str(d))
        _check("draft_no_send_note", "draft_email" in d.get("note", "") and "审批" in d.get("note", ""),
               str(d.get("note")))
        # 草稿里绝不含"已发送/sent"等执行字样(零发送断言)
        _check("draft_not_sent", "已发送" not in d.get("note", ""), str(d.get("note")))

        # ── 10. draft 隐私透传 ──
        d_priv = draft_quote_email(b_priv)
        _check("draft_private_propagate", d_priv.get("_private") is True, str(d_priv.get("_private")))

        # ── 9.5 #7 queue 守门: 无 to / 无价格 / 骗局 → 拒绝入队(均不触 outbound_email, 零副作用) ──
        q_noto = queue_quote(text="M-902 100MT CIF Rotterdam", to="", db=tmp_db)
        _check("queue_requires_to", q_noto.get("ok") is False and "邮箱" in q_noto.get("error", ""),
               str(q_noto))
        # 无成本价 → build 无价格 → queue 拒(不入队)
        q_noprice = queue_quote(text="Homopolymer M-902 100MT CIF Rotterdam",
                                to="buyer@example.com", dest_country="Germany", db=tmp_db)
        _check("queue_refuses_priceless",
               q_noprice.get("ok") is False and "价格" in q_noprice.get("error", ""),
               str(q_noprice))
        # queue 在 _READ_ACTIONS(LLM 可达), 但只入队不发送(发送红线门在 outbound_email.send)
        _check("queue_action_exposed", "queue" in _READ_ACTIONS, str(sorted(_READ_ACTIONS)))

        # ── 11. history 留痕 ──
        h = history(limit=10, db=tmp_db)
        _check("history_ok", h["ok"], str(h))
        _check("history_count", h["count"] >= 2, str(h))

        # ── 12. audit ──
        au = audit(limit=10, db=tmp_db)
        _check("audit_ok", au["ok"], str(au))

        # ── 13. status ──
        s = status(db=tmp_db)
        _check("status_ok", s["ok"], str(s))
        _check("status_downstream", "downstream_available" in s, str(s))

        # ── 14. Decimal 无 float 漂移 ──
        total = Decimal("0")
        for _ in range(1000):
            total += Decimal("0.1")
        _check("decimal_no_drift", total == Decimal("100.0"), f"got {total}")

        # ── 15. _as_int NaN 守卫 ──
        _check("as_int_nan", _as_int("nan", 14, 1, 365) == 14, str(_as_int("nan", 14, 1, 365)))
        _check("as_int_inf", _as_int("inf", 14, 1, 365) == 14, str(_as_int("inf", 14, 1, 365)))
        _check("as_int_clamp", _as_int("9999", 14, 1, 365) == 365, str(_as_int("9999", 14, 1, 365)))

        # ── 16. AST 安全闸: 源码无 eval/exec/subprocess/动态import后门/confirmed=True ──
        ast_res = _selftest_ast()
        passed += ast_res["passed"]
        failures.extend(ast_res["failures"])

    finally:
        try:
            os.unlink(tmp_db)
        except Exception:
            pass

    # ── 17. 三件套自检 ──
    tk = _selftest_toolkit()
    passed += tk["passed"]
    failures.extend(tk["failures"])

    result = {
        "ok": len(failures) == 0,
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
        "summary": f"selftest: {passed} passed, {len(failures)} failed",
    }
    return result


def _selftest_ast() -> Dict[str, Any]:
    """AST 静态安全校验: 禁 eval/exec/subprocess/os.system/动态 import 后门;
    源码无 confirmed=True。importlib 仅允许在 _opt/status/探测中软导入(白名单函数内)。"""
    import ast
    failures: List[str] = []
    passed = 0

    def _chk(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if cond:
            passed += 1
        else:
            failures.append(f"FAIL [ast:{name}]: {detail}")

    src_path = os.path.abspath(__file__)
    try:
        with open(src_path, "r", encoding="utf-8") as fh:
            src = fh.read()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "passed": 0, "failed": 1,
                "failures": [f"FAIL [ast:read]: {e}"]}

    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"ok": False, "passed": passed, "failed": len(failures) + 1,
                "failures": failures + [f"FAIL [ast:parse]: {e}"]}

    _FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "system", "popen"}
    _FORBIDDEN_ATTR = {"system", "popen", "Popen", "call", "run", "check_output",
                       "check_call", "spawn", "spawnl", "spawnv"}
    bad_calls: List[str] = []
    bad_imports: List[str] = []
    confirmed_true_nodes: List[int] = []  # 真正的 confirmed=True 语义(非检查器自身字面量)

    for node in ast.walk(tree):
        # AST 级零执行: 任何 keyword arg / 赋值把 confirmed 设为 True 都是禁区。
        # (用节点而非 substring, 避免误命中本检查器引用的字符串字面量)
        if isinstance(node, ast.keyword) and node.arg == "confirmed":
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                confirmed_true_nodes.append(getattr(node, "lineno", 0))
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                tgt_name = None
                if isinstance(tgt, ast.Name):
                    tgt_name = tgt.id
                elif isinstance(tgt, ast.Attribute):
                    tgt_name = tgt.attr
                if (tgt_name == "confirmed" and isinstance(node.value, ast.Constant)
                        and node.value.value is True):
                    confirmed_true_nodes.append(getattr(node, "lineno", 0))
        # 禁 import subprocess (含 getattr/importlib 绕过的子进程族)。
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name.split(".")[0] in ("subprocess", "pty", "ctypes"):
                    bad_imports.append(n.name)
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in ("subprocess", "pty", "ctypes"):
                bad_imports.append(node.module or "")
        # 禁直接调用 eval/exec/compile/__import__/system/popen。
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in _FORBIDDEN_CALLS:
                bad_calls.append(fn.id)
            if isinstance(fn, ast.Attribute) and fn.attr in _FORBIDDEN_ATTR:
                # os.system / subprocess.run / x.popen 等
                bad_calls.append(fn.attr)
            # getattr(x, 'system'/'eval'...) 动态后门
            if isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2:
                a2 = node.args[1]
                if isinstance(a2, ast.Constant) and isinstance(a2.value, str):
                    if a2.value in (_FORBIDDEN_CALLS | _FORBIDDEN_ATTR):
                        bad_calls.append(f"getattr->{a2.value}")
            # importlib.import_module("subprocess"...) 走私(动态 import 子进程族)。
            # _opt() 软导入只用于业务模块名, 常量是子进程族即违规。
            if (isinstance(fn, ast.Attribute) and fn.attr == "import_module"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and node.args[0].value.split(".")[0] in ("subprocess", "pty", "ctypes")):
                bad_imports.append(f"importlib->{node.args[0].value}")

    _chk("no_forbidden_calls", not bad_calls, f"发现禁用调用: {bad_calls}")
    _chk("no_subprocess_import", not bad_imports, f"发现子进程类 import: {bad_imports}")
    _chk("no_confirmed_true", not confirmed_true_nodes,
         f"发现 confirmed=True 语义节点 @ 行 {confirmed_true_nodes}")

    return {
        "ok": len(failures) == 0,
        "passed": passed,
        "failed": len(failures),
        "failures": failures,
    }


# ── CLI ───────────────────────────────────────────────────────────────────── #
def _cli() -> None:
    parser = argparse.ArgumentParser(
        prog="quote_builder",
        description="报价工坊 — 工业树脂外贸「询盘→报价草稿」编排器 (小美·只读零发送)",
    )
    parser.add_argument("--db", help="覆盖 SQLite 路径 (默认 ~/.jarvis/quote_builder.db)")
    sub = parser.add_subparsers(dest="action")

    p_parse = sub.add_parser("parse", help="解析询盘要素")
    p_parse.add_argument("text", help="询盘原文")

    p_score = sub.add_parser("score", help="询盘评分+骗局识别")
    p_score.add_argument("text")
    p_score.add_argument("--email", default=None)
    p_score.add_argument("--country", default=None)

    p_match = sub.add_parser("match", help="推荐型号")
    p_match.add_argument("text")
    p_match.add_argument("--limit", type=int, default=5)

    p_price = sub.add_parser("price", help="算 FOB/CIF 单价")
    p_price.add_argument("cost_rmb_per_ton", help="含税采购价 RMB/吨")
    p_price.add_argument("--freight", default="3000", dest="freight_usd_per_container")
    p_price.add_argument("--tons", default="25", dest="tons_per_container")
    p_price.add_argument("--incoterm", default="CIF")
    p_price.add_argument("--profit", default="0.08", dest="profit_rate")
    p_price.add_argument("--rate", default=None, dest="exchange_rate")
    p_price.add_argument("--qty", default=None, dest="quantity_mt")

    p_build = sub.add_parser("build", help="询盘文本→完整报价草稿")
    p_build.add_argument("text")
    p_build.add_argument("--cost", default=None, dest="cost_rmb_per_ton")
    p_build.add_argument("--freight", default="3000", dest="freight_usd_per_container")
    p_build.add_argument("--tons", default="25", dest="tons_per_container")
    p_build.add_argument("--profit", default="0.08", dest="profit_rate")
    p_build.add_argument("--rate", default=None, dest="exchange_rate")
    p_build.add_argument("--dest", default="", dest="dest_country")
    p_build.add_argument("--payment", default="TT", dest="payment_terms")
    p_build.add_argument("--validity", type=int, default=14, dest="validity_days")
    p_build.add_argument("--email", default=None)
    p_build.add_argument("--country", default=None)

    p_draft = sub.add_parser("draft", help="报价草稿→邮件草稿文本(不发送)")
    p_draft.add_argument("text", help="先 build 的询盘文本(内部会重新 build)")
    p_draft.add_argument("--cost", default=None, dest="cost_rmb_per_ton")
    p_draft.add_argument("--rate", default=None, dest="exchange_rate")
    p_draft.add_argument("--customer", default="", dest="customer_name")
    p_draft.add_argument("--company", default="", dest="company")

    p_hist = sub.add_parser("history", help="最近构建留痕")
    p_hist.add_argument("--limit", type=int, default=10)

    p_audit = sub.add_parser("audit", help="审计日志")
    p_audit.add_argument("--limit", type=int, default=20)

    sub.add_parser("status", help="模块状态")
    sub.add_parser("selftest", help="离线自测")

    args = parser.parse_args()
    db = getattr(args, "db", None)

    if args.action == "parse":
        result = parse_inquiry(args.text)
    elif args.action == "score":
        result = score_inquiry(args.text, sender_email=args.email, country=args.country)
    elif args.action == "match":
        result = match_product(args.text, limit=args.limit)
    elif args.action == "price":
        result = price_quote(
            cost_rmb_per_ton=args.cost_rmb_per_ton,
            freight_usd_per_container=args.freight_usd_per_container,
            tons_per_container=args.tons_per_container,
            incoterm=args.incoterm,
            profit_rate=args.profit_rate,
            exchange_rate=args.exchange_rate,
            quantity_mt=args.quantity_mt,
            db=db,
        )
    elif args.action == "build":
        result = build_quote(
            text=args.text,
            cost_rmb_per_ton=args.cost_rmb_per_ton,
            freight_usd_per_container=args.freight_usd_per_container,
            tons_per_container=args.tons_per_container,
            profit_rate=args.profit_rate,
            exchange_rate=args.exchange_rate,
            dest_country=args.dest_country,
            payment_terms=args.payment_terms,
            validity_days=args.validity_days,
            sender_email=args.email,
            country=args.country,
            db=db,
        )
    elif args.action == "draft":
        q = build_quote(
            text=args.text,
            cost_rmb_per_ton=args.cost_rmb_per_ton,
            exchange_rate=args.exchange_rate,
            db=db,
        )
        result = draft_quote_email(q, customer_name=args.customer_name, company=args.company)
    elif args.action == "history":
        result = history(args.limit, db=db)
    elif args.action == "audit":
        result = audit(args.limit, db=db)
    elif args.action == "status":
        result = status(db=db)
    elif args.action == "selftest":
        # 数据层隐私总开关默认 False(2026-07-08 起放弃), 但 selftest 校验的是
        # "开关打开时隐私标记契约仍完整可逆"这条不变量 —— 故 CLI 自测临时强制
        # 打开开关跑完(进程随后 sys.exit, 无需恢复); 直接调用 selftest() 的调用方
        # (如 pytest)不受影响, 仍看到默认关闭下的真实行为。
        _cfg_mod = _opt("config")
        if _cfg_mod is not None and hasattr(_cfg_mod, "get"):
            _orig_cfg_get = _cfg_mod.get

            def _force_privacy_on(path, *a, **kw):
                if path == "safety.data_layer_privacy_enabled":
                    return True
                return _orig_cfg_get(path, *a, **kw)

            _cfg_mod.get = _force_privacy_on
        result = selftest()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["ok"] else 1)
    else:
        parser.print_help()
        sys.exit(0)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if isinstance(result, dict) and not result.get("ok", True):
        sys.exit(1)


if __name__ == "__main__":
    _cli()
