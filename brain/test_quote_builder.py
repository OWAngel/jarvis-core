#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_quote_builder.py — quote_builder 报价工坊编排器测试。

覆盖:
  1. parse_inquiry  — 要素抽取(型号/晶型/数量/贸易条款/认证/包装/用途/港口)
  2. 隐私传播       — 底价/保密 → _private; _force_local 与 config 联动; 非私密不误标
  3. 凭据硬拦       — 询盘正文 token → 脱敏
  4. score/match/price/compliance — 软导入降级(deferred)与正常路径(stub)
  5. price_quote    — 缺成本价 missing / Decimal 总价精度 / NaN 守卫
  6. build_quote    — 编排 + missing_info + 隐私聚合 + 留痕
  7. draft_quote_email — 零发送(草稿文本 + 审批提示, 无"已发送")
  8. history/audit/status
  9. 三件套         — approved_tools/is_tool/dispatch + _CLI_ONLY 拦 selftest + 未知 action 拒
  10. AST 安全闸    — 内置 selftest 跑通(无 eval/exec/subprocess/confirmed=True)

范式照 test_fast_chain.py: 软导入目标全部 stub 进 sys.modules, CI ubuntu 可直跑。
可直接 python3 test_quote_builder.py 运行。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import types
import unittest
from decimal import Decimal

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# --------------------------------------------------------------------------- #
# stub 软导入目标(对齐 test_fast_chain.py 范式), 再 import quote_builder。
# 用可控的假实现, 让编排链在 CI(无真实模块)上确定性可跑。
# --------------------------------------------------------------------------- #
def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


# config: allow_cloud_private 默认 False(走本机), 测试 _force_local 联动用得到。
_stub("config", get=lambda path, default=None: (
    False if path == "safety.allow_cloud_private" else default))

# inquiry_scorer.score(text, sender_email, country) -> dict
def _fake_score(text="", sender_email=None, country=None):
    t = (text or "").lower()
    if "scam" in t or "free sample only" in t:
        return {"score": 10, "tier": "scam", "reasons": ["可疑"],
                "suggested_action": "标记骗局"}
    return {"score": 75, "tier": "hot", "reasons": ["规格具体"],
            "suggested_action": "优先回复"}


_stub("customer_intel", customer_intel=lambda customer="": {"ok": True, "contact": None, "deals": {"count": 0}, "status": {}, "summary": ""})
_stub("inquiry_scorer", score=_fake_score,
      batch=lambda items: [_fake_score(**i) for i in (items or [])])


# product_spec_hub.requirement_match(requirement, ref, limit, db_path) -> dict
def _fake_match(requirement="", ref="", limit=8, db_path=None):
    if not requirement:
        return {"ok": False, "error": "缺需求"}
    out = {
        "ok": True,
        "best_match": "M-902",
        "qualified": [{"model": "M-902", "score": 0.9, "met": 5, "gaps": 0,
                       "missing": 0, "hard_fails": [], "gap_detail": []}],
        "near_miss": [],
        "disqualified": [],
        "note": "matched",
    }
    # 私密需求(底价/保密) → 下游标 _private(模拟 product_spec_hub 行为)
    if any(k in requirement for k in ("底价", "保密", "成本价")):
        out["_private"] = True
    return out


# 成本台账 stub: 默认查不到; 可在测试中临时替换。
_COST_LEDGER: dict = {}  # model -> {"cost_rmb_per_ton": ..., "cost_date": ...}


def _fake_get_cost(model="", db_path=None):
    """模拟 product_spec_hub.get_cost(model) → 成本台账查询。"""
    key = (model or "").strip().upper().replace("-", "").replace(" ", "")
    entry = _COST_LEDGER.get(key)
    if entry:
        return {"ok": True, "found": True,
                "cost_rmb_per_ton": entry["cost_rmb_per_ton"],
                "cost_date": entry.get("cost_date", "2026-06-14"),
                "cost_currency": "CNY",
                "_private": True}
    return {"ok": True, "found": False, "_private": True}


_stub("product_spec_hub", requirement_match=_fake_match, get_cost=_fake_get_cost)


# fx_quote.quote_calc(...) -> dict
def _fake_quote_calc(cost_rmb_per_ton=None, freight_usd_per_container="3000",
                     tons_per_container=25, insurance_rate="0.0011",
                     profit_rate="0.08", tax_refund_rate="0.13", vat_rate="0.13",
                     exchange_rate=None, db=None):
    try:
        c = Decimal(str(cost_rmb_per_ton))
    except Exception:
        return {"ok": False, "error": "bad cost"}
    if c <= 0:
        return {"ok": False, "error": "cost<=0"}
    fob = "2000.00"
    cif = "2050.00"
    return {
        "ok": True,
        "fob_usd_per_ton": fob,
        "cif_usd_per_ton": cif,
        "actual_gross_margin": "0.0741",
        "margin_note": "markup 0.08 → 毛利率 0.0741",
        "inputs": {"exchange_rate_usd_cny": str(exchange_rate or "7.25"),
                   "rate_source": "手工"},
        "breakdown": {},
        "summary": "FOB 2000 / CIF 2050",
    }


_stub("fx_quote", quote_calc=_fake_quote_calc)


# hs_export_kit.* 只读
_stub(
    "hs_export_kit",
    hs_lookup=lambda q, db_path=None: {
        "ok": True,
        "matches": [{"hs_code": "3907400000", "name": "高耐候型工业树脂",
                     "export_rebate_rate": "13%"}],
        "disclaimer": "以海关税则为准",
    },
    doc_checklist=lambda dest, payment="TT", db_path=None: {
        "ok": True,
        "basic_docs": [{"doc": "Commercial Invoice", "mandatory": True, "note": ""}],
        "coo": {"type": "CO", "note": "", "form_a_available": False,
                "rcep_available": False},
        "lc_notes": [],
        "special_notes": [],
    },
    cert_expiry_check=lambda days=90, db_path=None: {
        "ok": True,
        "expiring": [{"cert_type": "REACH", "days_remaining": 30,
                      "alert_level": "warning"}],
    },
)

import config  # noqa: E402  (绑定上面已注入 sys.modules 的 config stub, 供开关 patch 用)
import quote_builder as qb  # noqa: E402


@contextlib.contextmanager
def _privacy_switch_on():
    """临时把 safety.data_layer_privacy_enabled 开关打开(测试用)。

    2026-07-08 起该开关默认 False(数据层隐私已放弃, _classify_private 恒 False,
    原文直出、不占位、不打 _private/_force_local); 打开开关应完整、可逆地恢复
    旧版隐私标记行为(照 test_phrase_macro.py 同款范式)。
    """
    _orig = config.get

    def _on(key, fallback=None):
        if key == "safety.data_layer_privacy_enabled":
            return True
        return _orig(key, fallback)

    config.get = _on
    try:
        yield
    finally:
        config.get = _orig


class TestParseInquiry(unittest.TestCase):
    def test_extract_all_elements(self):
        r = qb.parse_inquiry(
            "We need Homopolymer PC2 M-902, 50 MT, CIF Hamburg, REACH & SGS, "
            "25kg bag, for coating")
        self.assertTrue(r["ok"])
        self.assertIn("homopolymer", r["grades"])
        self.assertTrue(any("902" in m for m in r["models"]))
        self.assertEqual(r["quantity_mt"], "50")
        self.assertEqual(r["incoterm"], "CIF")
        self.assertIn("REACH", r["certs_required"])
        self.assertIn("SGS", r["certs_required"])
        self.assertEqual(r["application"], "coating")

    def test_chinese_inquiry(self):
        r = qb.parse_inquiry("高耐候型工业树脂 100吨 FOB 上海 用于塑料")
        self.assertTrue(r["ok"])
        self.assertIn("homopolymer", r["grades"])
        self.assertEqual(r["quantity_mt"], "100")
        self.assertEqual(r["incoterm"], "FOB")
        self.assertEqual(r["application"], "塑料")

    def test_empty(self):
        r = qb.parse_inquiry("")
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], [])
        self.assertNotIn("_private", r)

    def test_comma_thousands_quantity(self):
        """'1,000 MT' 千分位不能被吞成 '000'(审查 MEDIUM 回归)。"""
        r = qb.parse_inquiry("We need 1,000 MT homopolymer PC2 CIF")
        self.assertEqual(r["quantity_mt"], "1000")
        r2 = qb.parse_inquiry("annual 10,000 MT contract")
        self.assertEqual(r2["quantity_mt"], "10000")

    def test_container_capacity_not_order_qty(self):
        """'25t container, need 50 MT': 集装箱容量不是订量(审查 LOW 回归)。"""
        r = qb.parse_inquiry("shipment in 25t container, we need 50 MT homopolymer")
        self.assertEqual(r["quantity_mt"], "50")


class TestPrivacyPropagation(unittest.TestCase):
    def test_private_flag_on_floor_price_default_off(self):
        """新默认(2026-07-08 起 data_layer_privacy_enabled=False, 数据层隐私已放弃):
        底价询盘不再标 _private/_force_local, 原文直出(真发送另有红线4审批闸独立守卫)。"""
        r = qb.parse_inquiry("客户要 M-902 50吨, 我方底价 RMB 14000/吨不要外传")
        self.assertNotIn("_private", r)
        self.assertNotIn("_force_local", r)

    def test_private_flag_on_floor_price_switch_on(self):
        """开关手动打开(历史行为完整可逆恢复): 底价询盘标 _private;
        config stub: allow_cloud_private=False → 必打 _force_local。"""
        with _privacy_switch_on():
            r = qb.parse_inquiry("客户要 M-902 50吨, 我方底价 RMB 14000/吨不要外传")
            self.assertIs(r.get("_private"), True)
            self.assertIs(r.get("_force_local"), True)

    def test_public_not_flagged(self):
        r = qb.parse_inquiry("Please quote copolymer PC2 100 MT FOB")
        self.assertNotIn("_private", r)
        self.assertNotIn("_force_local", r)

    def test_build_aggregates_private(self):
        b = qb.build_quote(text="M-902 50吨 CIF, 我方成本价 14000 保密",
                           exchange_rate="7.25")
        self.assertIs(b.get("_private"), True)

    def test_downstream_private_propagates(self):
        # match 下游标 _private → build 整条 _private
        b = qb.build_quote(text="M-902 50吨 保密 CIF", exchange_rate="7.25")
        self.assertIs(b.get("_private"), True)

    def test_cost_input_makes_build_private(self):
        """成本价是内部敏感数据: 给了成本价的 build 整条 _private,
        哪怕询盘文本完全公开(审查 MEDIUM 回归: priced/毛利率泄露)。"""
        b = qb.build_quote(text="M-902 50 MT CIF Rotterdam",
                           cost_rmb_per_ton="15000", exchange_rate="7.25")
        self.assertIs(b.get("_private"), True)

    def test_price_quote_output_private(self):
        """price_quote 输出含毛利率(margin_note/actual_gross_margin) → 自标 _private。"""
        r = qb.price_quote(cost_rmb_per_ton="14000", exchange_rate="7.25")
        if r.get("ok"):                      # fx stub 正常路径
            self.assertIs(r.get("_private"), True)

    def test_draft_public_quote_not_false_positive(self):
        """公开询盘(无成本价)的草稿不被系统字段固定措辞('内部/审批')错标 _private
        (审查 MEDIUM 回归: draft 100% 假阳会让公开草稿永远切本机)。"""
        b = qb.build_quote(text="copolymer PC2 100 MT FOB Shanghai")
        self.assertNotIn("_private", b)
        d = qb.draft_quote_email(b)
        self.assertNotIn("_private", d)

    def test_draft_customer_name_content_channel_default_off(self):
        """新默认(数据层隐私已放弃): customer_name 独立内容通道含私密词也不再
        触发 _private(_classify_private 恒 False)。"""
        b = qb.build_quote(text="copolymer PC2 100 MT FOB")
        d = qb.draft_quote_email(b, customer_name="底价内部联系人")
        self.assertNotIn("_private", d)

    def test_draft_customer_name_content_channel_switch_on(self):
        """开关手动打开(历史行为完整可逆恢复): customer_name/company 是独立内容
        通道, 含私密词仍必须触发 _private(审查 MEDIUM 回归原始意图)。"""
        with _privacy_switch_on():
            b = qb.build_quote(text="copolymer PC2 100 MT FOB")
            d = qb.draft_quote_email(b, customer_name="底价内部联系人")
            self.assertIs(d.get("_private"), True)


class TestSecretRedaction(unittest.TestCase):
    def test_token_in_inquiry_redacted(self):
        r = qb.parse_inquiry(
            "homopolymer 50MT, my key ghp_abc123DEF456GHI789JKL012MNO345")
        self.assertNotIn("ghp_abc123", r["inquiry_excerpt"])

    def test_secret_in_build_redacted(self):
        b = qb.build_quote(
            text="M-902 CIF, api_key=sk-abc123DEF456GHI789JKL token",
            exchange_rate="7.25")
        blob = json.dumps(b, ensure_ascii=False, default=str)
        self.assertNotIn("sk-abc123DEF456", blob)

    def test_seal_recursive(self):
        out = qb._seal({"a": "ghp_abc123DEF456GHI789JKL012MNO345", "b": ["x", "y"]})
        self.assertNotIn("ghp_abc123", json.dumps(out))


class TestPriceQuote(unittest.TestCase):
    def test_missing_cost(self):
        r = qb.price_quote(cost_rmb_per_ton=None, exchange_rate="7.25")
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("missing_cost"))

    def test_decimal_total(self):
        r = qb.price_quote(cost_rmb_per_ton="15000", incoterm="CIF",
                           exchange_rate="7.25", quantity_mt="50")
        self.assertTrue(r["ok"])
        # CIF unit = 2050.00 (stub) → total = 50 × 2050 = 102500.00
        self.assertEqual(r["total_usd"], "102500.00")

    def test_fob_vs_cif_basis(self):
        rf = qb.price_quote(cost_rmb_per_ton="15000", incoterm="FOB",
                            exchange_rate="7.25")
        rc = qb.price_quote(cost_rmb_per_ton="15000", incoterm="CIF",
                            exchange_rate="7.25")
        self.assertEqual(rf["price_basis"], "FOB")
        self.assertEqual(rc["price_basis"], "CIF")
        self.assertEqual(rf["unit_price_usd_per_ton"], "2000.00")
        self.assertEqual(rc["unit_price_usd_per_ton"], "2050.00")

    def test_nan_quantity_no_crash(self):
        r = qb.price_quote(cost_rmb_per_ton="15000", exchange_rate="7.25",
                           quantity_mt="nan")
        self.assertIsInstance(r, dict)
        # 数量非法 → 不应有 total_usd, 但单价仍在
        self.assertTrue(r["ok"])
        self.assertNotIn("total_usd", r)

    def test_zero_cost_rejected(self):
        r = qb.price_quote(cost_rmb_per_ton="0", exchange_rate="7.25")
        self.assertFalse(r["ok"])


class TestDeferredDegradation(unittest.TestCase):
    def test_price_deferred_when_fx_missing(self):
        saved = sys.modules.get("fx_quote")
        sys.modules["fx_quote"] = None  # 模拟缺失(import 返回 None)
        try:
            r = qb.price_quote(cost_rmb_per_ton="15000", exchange_rate="7.25")
            self.assertFalse(r["ok"])
            self.assertTrue(r.get("deferred"))
        finally:
            sys.modules["fx_quote"] = saved

    def test_match_deferred_when_psh_missing(self):
        saved = sys.modules.get("product_spec_hub")
        sys.modules["product_spec_hub"] = None
        try:
            r = qb.match_product("homopolymer 50MT")
            self.assertFalse(r["ok"])
            self.assertTrue(r.get("deferred"))
        finally:
            sys.modules["product_spec_hub"] = saved

    def test_score_deferred_when_scorer_missing(self):
        saved = sys.modules.get("inquiry_scorer")
        sys.modules["inquiry_scorer"] = None
        try:
            r = qb.score_inquiry("homopolymer 50MT")
            self.assertFalse(r["ok"])
            self.assertTrue(r.get("deferred"))
        finally:
            sys.modules["inquiry_scorer"] = saved


class TestBuildQuote(unittest.TestCase):
    def test_full_orchestration(self):
        b = qb.build_quote(
            text="Homopolymer PC2 M-902, 50 MT, CIF Hamburg, REACH, for coating",
            cost_rmb_per_ton="15000", exchange_rate="7.25",
            dest_country="Germany")
        self.assertTrue(b["ok"])
        self.assertEqual(b["incoterm"], "CIF")
        self.assertTrue(b["line_items"])
        self.assertEqual(b["line_items"][0]["product"], "M-902")
        self.assertEqual(b["total_usd"], "102500.00")  # 50 × 2050
        self.assertEqual(b["tier"], "hot")
        self.assertIn("draft_email", b["note"])

    def test_missing_info_lists_gaps(self):
        b = qb.build_quote(text="Homopolymer PC2 M-902, for paint",
                           exchange_rate="7.25")
        self.assertTrue(b["ok"])
        # 没成本价/数量/目的国 → missing_info 应非空且含成本
        self.assertTrue(b["missing_info"])
        self.assertTrue(any("成本" in m for m in b["missing_info"]))

    def test_scam_warning(self):
        b = qb.build_quote(text="scam free sample only M-902 CIF",
                           cost_rmb_per_ton="15000", exchange_rate="7.25")
        self.assertTrue(b["ok"])
        self.assertTrue(b["is_scam_warning"])
        self.assertIn("⚠️", b["note"])

    def test_empty_text(self):
        b = qb.build_quote(text="")
        self.assertFalse(b["ok"])


class TestDraftQuoteEmail(unittest.TestCase):
    def setUp(self):
        self.q = qb.build_quote(
            text="Homopolymer PC2 M-902, 50 MT, CIF Hamburg, for paint",
            cost_rmb_per_ton="15000", exchange_rate="7.25",
            dest_country="Germany")

    def test_draft_returns_text_not_send(self):
        d = qb.draft_quote_email(self.q, customer_name="John",
                                 company="Acme Chemical")
        self.assertTrue(d["ok"])
        self.assertIsInstance(d["draft_text"], str)
        self.assertGreater(len(d["draft_text"]), 0)
        # 零发送断言: note 提到审批 + draft_email, 绝无"已发送"
        self.assertIn("审批", d["note"])
        self.assertIn("draft_email", d["note"])
        self.assertNotIn("已发送", d["note"])
        self.assertNotIn("已发送", d["draft_text"])

    def test_draft_contains_quote_details(self):
        d = qb.draft_quote_email(self.q, customer_name="John",
                                 company="Acme Chemical")
        self.assertIn("M-902", d["draft_text"])
        self.assertIn("CIF", d["draft_text"])
        self.assertIn("John", d["draft_text"])
        self.assertIn("Acme Chemical", d["draft_text"])

    def test_draft_private_propagation(self):
        qp = qb.build_quote(text="M-902 CIF, 底价保密 成本价 14000",
                            cost_rmb_per_ton="15000", exchange_rate="7.25")
        d = qb.draft_quote_email(qp)
        self.assertIs(d.get("_private"), True)

    def test_draft_bad_input(self):
        d = qb.draft_quote_email("not a dict")
        self.assertFalse(d["ok"])


class TestHistoryAuditStatus(unittest.TestCase):
    def test_status(self):
        s = qb.status()
        self.assertTrue(s["ok"])
        self.assertEqual(s["module"], "quote_builder")
        self.assertIn("downstream_available", s)

    def test_history_after_build(self):
        qb.build_quote(text="M-902 50MT CIF", cost_rmb_per_ton="15000",
                       exchange_rate="7.25")
        h = qb.history(limit=5)
        self.assertTrue(h["ok"])
        self.assertGreaterEqual(h["count"], 1)

    def test_audit(self):
        a = qb.audit(limit=5)
        self.assertTrue(a["ok"])


class TestToolkit(unittest.TestCase):
    def test_approved_tools(self):
        tools = qb.approved_tools()
        self.assertTrue(tools)
        self.assertEqual(tools[0]["function"]["name"], "quote_builder")
        self.assertIn("parameters", tools[0]["function"])

    def test_is_tool(self):
        self.assertTrue(qb.is_tool("quote_builder"))
        self.assertFalse(qb.is_tool("fx_quote"))
        self.assertFalse(qb.is_tool(""))

    def test_dispatch_wrong_name(self):
        self.assertFalse(qb.dispatch("nope", {}).get("ok", True))

    def test_dispatch_cli_only_blocked(self):
        r = qb.dispatch("quote_builder", {"action": "selftest"})
        self.assertFalse(r.get("ok", True))

    def test_dispatch_unknown_action(self):
        r = qb.dispatch("quote_builder", {"action": "no_such"})
        self.assertFalse(r.get("ok", True))

    def test_dispatch_status(self):
        r = qb.dispatch("quote_builder", {"action": "status"})
        self.assertTrue(r.get("ok"))

    def test_dispatch_build(self):
        r = qb.dispatch("quote_builder", {
            "action": "build", "text": "M-902 50MT CIF",
            "cost_rmb_per_ton": "15000", "exchange_rate": "7.25"})
        self.assertTrue(r.get("ok"))
        self.assertIn("line_items", r)

    def test_dispatch_parse(self):
        r = qb.dispatch("quote_builder", {"action": "parse",
                                          "text": "homopolymer M-902 50MT CIF"})
        self.assertTrue(r.get("ok"))
        self.assertIn("models", r)


class TestCostLedgerAutofill(unittest.TestCase):
    """D5 成本台账回填: build_quote 的 cost_rmb_per_ton 缺失时自动从 product_spec_hub 查台账。"""

    def setUp(self):
        # 注入台账成本价: M-902 → 14500
        _COST_LEDGER.clear()
        _COST_LEDGER["M902"] = {"cost_rmb_per_ton": "14500", "cost_date": "2026-06-14"}

    def tearDown(self):
        _COST_LEDGER.clear()

    def test_autofill_cost_from_hub(self):
        """build_quote 缺 cost_rmb_per_ton, 但 hub 有 M-902 台账 → 自动回填, 不再 missing。"""
        b = qb.build_quote(text="M-902 50 MT CIF Rotterdam",
                           exchange_rate="7.25", dest_country="Netherlands")
        self.assertTrue(b["ok"])
        # 有台账价 → 算出了单价(不再因 missing cost 跳过)
        pricing = b.get("pricing") or {}
        self.assertTrue(pricing.get("ok"), f"pricing failed: {pricing}")
        # missing_info 里不再提成本缺失
        missing_blob = " ".join(b.get("missing_info") or [])
        self.assertNotIn("采购成本价", missing_blob,
                         f"cost should be autofilled, missing_info={b.get('missing_info')}")
        # 来源标注 cost_source = hub_ledger
        self.assertEqual(b.get("cost_source"), "hub_ledger")
        self.assertEqual(b.get("cost_date"), "2026-06-14")
        # 含成本价 → 整条 _private(成本价内部机密, 语义判定)
        self.assertIs(b.get("_private"), True)

    def test_no_hub_cost_still_missing(self):
        """hub 没有该型号的台账价 → 行为与现在一致(missing), 不崩。"""
        _COST_LEDGER.clear()  # 清空台账
        b = qb.build_quote(text="M-902 50 MT CIF",
                           exchange_rate="7.25")
        self.assertTrue(b["ok"])
        # 没有台账价 → 成本仍 missing
        missing_blob = " ".join(b.get("missing_info") or [])
        self.assertIn("采购成本价", missing_blob,
                      f"cost should still be missing when hub has none, missing={b.get('missing_info')}")
        # cost_source 不应设置
        self.assertNotIn("cost_source", b)

    def test_explicit_cost_takes_priority(self):
        """显式传入 cost_rmb_per_ton 时, 不应被台账价覆盖。"""
        b = qb.build_quote(text="M-902 50 MT CIF",
                           cost_rmb_per_ton="20000", exchange_rate="7.25")
        self.assertTrue(b["ok"])
        # cost_source 不设置(显式传入, 非台账)
        self.assertNotIn("cost_source", b)
        # 价格用的是 20000(stub 总价 = 50 × 2050 = 102500.00, 价格与 cost 无关故不测具体值, 只看 ok)
        pricing = b.get("pricing") or {}
        self.assertTrue(pricing.get("ok"))

    def test_hub_unavailable_degrades_gracefully(self):
        """product_spec_hub 不可达 → 降级处理, 不崩(仍返回 ok=True, cost missing)。"""
        saved = sys.modules.get("product_spec_hub")
        sys.modules["product_spec_hub"] = None
        try:
            b = qb.build_quote(text="M-902 50 MT CIF", exchange_rate="7.25")
            self.assertTrue(b["ok"])  # 主流程不崩
        finally:
            sys.modules["product_spec_hub"] = saved

    def test_hub_get_cost_exception_degrades_gracefully(self):
        """product_spec_hub.get_cost 抛异常 → 降级, 不崩, 仍显示 cost missing。"""
        import types as _types
        broken_psh = _types.ModuleType("product_spec_hub")
        broken_psh.requirement_match = _fake_match

        def _raise(*a, **kw):
            raise RuntimeError("故意出错")
        broken_psh.get_cost = _raise

        saved = sys.modules.get("product_spec_hub")
        sys.modules["product_spec_hub"] = broken_psh
        try:
            b = qb.build_quote(text="M-902 50 MT CIF", exchange_rate="7.25")
            self.assertTrue(b["ok"])  # 不崩
        finally:
            sys.modules["product_spec_hub"] = saved

    def test_set_cost_not_in_approved_tools(self):
        """set_cost 是写动作, 不能出现在 approved_tools 暴露的工具面里。"""
        tools = qb.approved_tools()
        tool_names = [t["function"]["name"] for t in tools]
        self.assertNotIn("set_cost", " ".join(tool_names))
        self.assertNotIn("product_spec_hub_set_cost", " ".join(tool_names))


class TestSelftest(unittest.TestCase):
    def test_builtin_selftest_passes_switch_on(self):
        """内置 selftest(含隐私传播不变量 + AST 安全闸)在开关手动打开(历史行为
        完整可逆恢复)下应全绿。"""
        with _privacy_switch_on():
            r = qb.selftest()
            self.assertTrue(r["ok"], f"selftest failures: {r.get('failures')}")
            self.assertEqual(r["failed"], 0)

    def test_builtin_selftest_default_off_still_functions(self):
        """新默认(开关关闭): 隐私传播不变量(private_flag /
        force_local_when_not_allowed)因数据层隐私已放弃而预期失效, 但 selftest
        本身不应崩溃, 且其余不变量(AST 安全闸/凭据脱敏/Decimal精度/NaN守卫/
        三件套等)必须依然全绿, 不允许出现新的意外失败。"""
        r = qb.selftest()
        self.assertIsInstance(r, dict)
        self.assertIn("failures", r)
        known_off_failures = {"private_flag", "force_local_when_not_allowed"}
        unexpected = [
            f for f in r.get("failures", [])
            if not any(f.startswith(f"FAIL [{name}]") for name in known_off_failures)
        ]
        self.assertEqual(unexpected, [], f"unexpected selftest failures: {unexpected}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
