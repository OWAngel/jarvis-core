#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_hs_export_kit.py — pytest 风格测试 (可直接运行兜底)。

覆盖:
  - hs_lookup: 查询/前缀/别名/空查询/查无/免责声明一致性
  - doc_checklist: 基础单证/产地证规则/LC严格提醒/特殊国家/空查询
  - cert_ledger_add/list: 增改不降级/upsert/日期验证
  - cert_expiry_check: 语义化比较/已过期/边界/空到期日
  - dispatch: 白名单守门/写库拒绝/不存在动作
  - 对抗: 空日期/已过期/NaN/inf/凭据脱敏/私密标记
"""
from __future__ import annotations

import os
import sys
import tempfile
import shutil

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import hs_export_kit as hek
import config

# ─── 数据层隐私总开关 helper(2026-07-08 默认关闭; 测试须双态覆盖) ──────────────────
#
# hs_export_kit 通过 `config.get("safety.data_layer_privacy_enabled", False)` 每次现读现判。
# 默认(开关关闭, User 放弃数据层隐私隔离): _classify_private 恒 False, 记录不占位/不标 _private。
# 用下面的上下文管理器把开关拨回 ON, 断言旧行为仍可逆保留。
# (用手动 save/restore 的 context manager 而非 pytest monkeypatch fixture,
#  以兼容本文件末尾 __main__ 兜底直接调用 fn() 的运行方式。)

class _privacy_switch_on:
    """把 config.get 打上补丁: safety.data_layer_privacy_enabled 强制返回 True, 其余透传原实现。"""

    def __enter__(self):
        self._orig_get = config.get

        def _patched(key, fallback=None):
            if key == "safety.data_layer_privacy_enabled":
                return True
            return self._orig_get(key, fallback)

        config.get = _patched
        return self

    def __exit__(self, *_exc):
        config.get = self._orig_get
        return False


# ─── 共享临时库 fixture ────────────────────────────────────────────────────────

class _TmpDB:
    """简单上下文管理器: 创建临时目录 + db 路径, 退出时清理。"""
    def __enter__(self):
        self._tmp = tempfile.mkdtemp(prefix="hek_test_")
        self.db = os.path.join(self._tmp, "test.db")
        return self.db

    def __exit__(self, *_):
        shutil.rmtree(self._tmp, ignore_errors=True)


# ─── hs_lookup ────────────────────────────────────────────────────────────────

def test_hs_lookup_industrial_resin_zh():
    """工业树脂 → 应命中 3907.40"""
    r = hek.hs_lookup("工业树脂")
    assert r["ok"], f"ok=False: {r}"
    assert r["total"] > 0, "应有匹配结果"
    codes = [m["hs_code"] for m in r["matches"]]
    assert any("3907.40" in c for c in codes), f"应命中 3907.40, got {codes}"


def test_hs_lookup_homopolymer_en():
    """homopolymer (英文) → 应命中"""
    r = hek.hs_lookup("homopolymer")
    assert r["ok"]
    assert r["total"] > 0, "homopolymer 应有匹配"


def test_hs_lookup_code_prefix_3907():
    """HS编码前缀 3907 → 应命中 3907 品目条目"""
    r = hek.hs_lookup("3907")
    assert r["ok"]
    assert r["total"] > 0, "3907 前缀应有匹配"


def test_hs_lookup_nonmolding_polymer():
    """非注塑级 / 基础聚合物 → 应命中 3901"""
    r = hek.hs_lookup("基础聚合物")
    assert r["ok"]
    codes = [m["hs_code"] for m in r["matches"]]
    assert any("3901" in c for c in codes), f"应命中 3901, got {codes}"


def test_hs_lookup_empty_query():
    """空查询 → ok=False"""
    r = hek.hs_lookup("")
    assert not r["ok"], "空查询应返回 ok=False"
    assert "error" in r


def test_hs_lookup_not_found():
    """查无结果: 不存在的化学品"""
    r = hek.hs_lookup("XYZABC不存在化学品99999")
    assert r["ok"], "查无结果时 ok 仍应为 True"
    assert r["total"] == 0, "不应有匹配"
    assert "suggestion" in r


def test_hs_lookup_disclaimer_consistency():
    """每条匹配结果都含免责声明(「供参考,报关以归类裁定为准」)"""
    r = hek.hs_lookup("工业树脂")
    assert r["ok"]
    for m in r["matches"]:
        assert "供参考" in (m.get("disclaimer") or ""), \
            f"匹配条目缺免责声明: {m.get('hs_code')}"


def test_hs_lookup_declaration_elements_present():
    """每条匹配应含 declaration_elements(申报要素)"""
    r = hek.hs_lookup("工业树脂")
    assert r["ok"]
    for m in r["matches"]:
        elems = m.get("declaration_elements", [])
        assert isinstance(elems, list) and len(elems) > 0, \
            f"{m['hs_code']} 应有申报要素"


def test_hs_lookup_rebate_note_policy():
    """退税率备注应含「以当期」提示(不写死政策)"""
    r = hek.hs_lookup("工业树脂")
    assert r["ok"]
    for m in r["matches"]:
        assert "以当期" in (m.get("rebate_note") or ""), \
            f"{m['hs_code']} 退税备注应含「以当期」"


def test_hs_lookup_private_content_default_off_not_flagged():
    """默认(数据层隐私总开关关闭, 2026-07-08 放弃): 含私密关键词的查询也不再标 _private/原样返回"""
    r = hek.hs_lookup("底价报价客户名单工业树脂")
    assert not r.get("_private"), "开关默认关闭: 不应标 _private"
    assert not r.get("_force_local"), "开关默认关闭: 不应标 _force_local"


def test_hs_lookup_private_content_flagged_when_switch_on():
    """开关拨回 ON(可逆): 含私密关键词的查询应标 _private(旧行为保留)"""
    with _privacy_switch_on():
        r = hek.hs_lookup("底价报价客户名单工业树脂")
        assert r.get("_private"), "开关ON: 含私密词的查询结果应标 _private"


# ─── doc_checklist ────────────────────────────────────────────────────────────

def test_doc_checklist_vietnam_lc():
    """越南 + LC → FORM E + lc_notes"""
    r = hek.doc_checklist("越南", "LC")
    assert r["ok"], f"失败: {r}"
    assert r["lc_notes"], "LC付款应有 lc_notes"
    assert "FORM E" in str(r.get("coo", {})), "越南应推荐 FORM E"


def test_doc_checklist_eu_tt():
    """欧盟 + TT → REACH提示"""
    r = hek.doc_checklist("欧盟", "TT")
    assert r["ok"]
    special = r.get("special_notes", [])
    assert any("REACH" in n for n in special), f"欧盟应有REACH提示, got {special}"


def test_doc_checklist_japan_rcep():
    """日本 → RCEP产地证推荐"""
    r = hek.doc_checklist("日本", "TT")
    assert r["ok"]
    coo = r.get("coo", {})
    assert "RCEP" in str(coo), f"日本应推荐RCEP, got {coo}"


def test_doc_checklist_usa_section301():
    """美国 → Section 301提示"""
    r = hek.doc_checklist("美国", "TT")
    assert r["ok"]
    special = r.get("special_notes", [])
    assert any("301" in n for n in special), f"美国应有Section 301提示, got {special}"


def test_doc_checklist_basic_docs_present():
    """任何目的国都应有 CI/PL/BL 基础单证"""
    r = hek.doc_checklist("巴西", "TT")
    assert r["ok"]
    docs = [d["doc"] for d in r.get("basic_docs", [])]
    assert any("Commercial Invoice" in d or "商业发票" in d for d in docs), "应有商业发票"
    assert any("Packing List" in d or "装箱单" in d for d in docs), "应有装箱单"
    assert any("Bill of Lading" in d or "提单" in d for d in docs), "应有提单"


def test_doc_checklist_lc_reminders():
    """LC付款 → 单单一致提醒"""
    r = hek.doc_checklist("新加坡", "L/C")
    assert r["ok"]
    lc = r.get("lc_notes", [])
    assert lc, "LC 模式应有提醒"
    assert any("一致" in n for n in lc), "应提醒单单一致"


def test_doc_checklist_oa_risk_warning():
    """OA付款 → 收款风险提示"""
    r = hek.doc_checklist("德国", "OA")
    assert r["ok"]
    lc = r.get("lc_notes", [])
    assert lc, "OA模式应有风险提示"
    assert any("风险" in n for n in lc), "OA应有收款风险提示"


def test_doc_checklist_empty_destination():
    """空目的国 → ok=False"""
    r = hek.doc_checklist("")
    assert not r["ok"], "空目的国应返回 ok=False"


def test_doc_checklist_disclaimer_present():
    """结果应含免责说明"""
    r = hek.doc_checklist("泰国", "TT")
    assert r["ok"]
    assert "供参考" in (r.get("disclaimer") or "") or "以" in (r.get("note") or ""), \
        "应含免责/参考说明"


# ─── cert_ledger_add / list ────────────────────────────────────────────────────

def test_cert_ledger_add_and_list():
    """基本增删查: 新增后能查到"""
    with _TmpDB() as db:
        r = hek.cert_ledger_add(
            cert_type="REACH", product="M-996",
            issuer="ECHA", cert_no="REG-2024-001",
            issued_date="2024-01-01", expiry_date="2027-01-01",
            scope="工业树脂颗粒", db_path=db
        )
        assert r["ok"], f"add 失败: {r}"
        lst = hek.cert_ledger_list(db_path=db)
        assert lst["ok"]
        assert lst["total"] >= 1
        types = [c["cert_type"] for c in lst["certs"]]
        assert "REACH" in types


def test_cert_ledger_upsert_default_off_never_marked_private():
    """默认(数据层隐私总开关关闭): 即便 note 含私密关键词, private 列也不再置 1(恒 0)"""
    with _TmpDB() as db:
        hek.cert_ledger_add(
            cert_type="SGS", product="M-996", cert_no="SGS-001",
            issued_date="2024-01-01", expiry_date="2025-12-31",
            note="内部报价附件不对外",  # 含「报价」, 但开关关闭不再判私密
            db_path=db
        )
        conn = hek._conn(db)
        try:
            rows = conn.execute(
                "SELECT private FROM hek_certs WHERE cert_type='SGS' AND product='M-996'"
            ).fetchall()
        finally:
            conn.close()
        assert rows, "SGS记录应存在"
        assert rows[0][0] == 0, f"开关默认关闭: private 应为 0, got {rows[0][0]}"


def test_cert_ledger_upsert_no_downgrade_private_when_switch_on():
    """开关拨回 ON(可逆): upsert 时 private 只升不降(MAX), 旧行为保留"""
    with _privacy_switch_on():
        with _TmpDB() as db:
            # 第一次: 含私密关键词 → private=1
            hek.cert_ledger_add(
                cert_type="SGS", product="M-996", cert_no="SGS-001",
                issued_date="2024-01-01", expiry_date="2025-12-31",
                note="内部报价附件不对外",  # 含「报价」→ 私密
                db_path=db
            )
            # 第二次: 不含私密词 → 但 private 不应降级
            hek.cert_ledger_add(
                cert_type="SGS", product="M-996", cert_no="SGS-001",
                issued_date="2024-01-01", expiry_date="2025-12-31",
                note="正常更新备注",  # 无私密词
                db_path=db
            )
            # 查库验证 private 字段
            conn = hek._conn(db)
            try:
                rows = conn.execute(
                    "SELECT private FROM hek_certs WHERE cert_type='SGS' AND product='M-996'"
                ).fetchall()
            finally:
                conn.close()
            assert rows, "SGS记录应存在"
            # private 应为 1(不降级)
            assert rows[0][0] == 1, f"private 应保持 1, got {rows[0][0]}"


def test_cert_ledger_multiple_types():
    """多种认证类型共存"""
    with _TmpDB() as db:
        for ct in ["REACH", "ISO9001", "RoHS", "MSDS"]:
            r = hek.cert_ledger_add(cert_type=ct, product="全系列", db_path=db)
            assert r["ok"], f"add {ct} 失败: {r}"
        lst = hek.cert_ledger_list(db_path=db)
        assert lst["total"] == 4


def test_cert_ledger_filter_by_type():
    """按 cert_type 过滤"""
    with _TmpDB() as db:
        hek.cert_ledger_add(cert_type="REACH", product="M-996", db_path=db)
        hek.cert_ledger_add(cert_type="ISO9001", product="全系列", db_path=db)
        lst = hek.cert_ledger_list(cert_type="REACH", db_path=db)
        assert lst["ok"]
        assert all(c["cert_type"] == "REACH" for c in lst["certs"]), \
            "过滤后应只有 REACH"


def test_cert_ledger_filter_by_product():
    """按 product 过滤"""
    with _TmpDB() as db:
        hek.cert_ledger_add(cert_type="SGS", product="M-996", db_path=db)
        hek.cert_ledger_add(cert_type="SGS", product="A-100", db_path=db)
        lst = hek.cert_ledger_list(product="M-996", db_path=db)
        assert lst["ok"]
        assert all("M-996" in c["product"] for c in lst["certs"])


def test_cert_ledger_invalid_date_format():
    """无效日期格式 → ok=False"""
    with _TmpDB() as db:
        r = hek.cert_ledger_add(
            cert_type="REACH", product="M-996",
            expiry_date="not-a-date",
            db_path=db
        )
        assert not r["ok"], "无效日期应返回 ok=False"
        assert "error" in r


def test_cert_ledger_missing_required():
    """缺必填字段 → ok=False"""
    with _TmpDB() as db:
        r = hek.cert_ledger_add(cert_type="", product="M-996", db_path=db)
        assert not r["ok"]
        r2 = hek.cert_ledger_add(cert_type="REACH", product="", db_path=db)
        assert not r2["ok"]


def test_cert_ledger_credential_not_stored():
    """note 含凭据 → 落库后凭据不留明文"""
    with _TmpDB() as db:
        hek.cert_ledger_add(
            cert_type="TEST", product="CRED_TEST",
            note="api_key=sk-ABCDEFGH1234567890XYZ 别泄漏",
            db_path=db
        )
        conn = hek._conn(db)
        try:
            rows = conn.execute(
                "SELECT note FROM hek_certs WHERE cert_type='TEST' AND product='CRED_TEST'"
            ).fetchall()
        finally:
            conn.close()
        for (note_val,) in rows:
            assert "sk-ABCDEFGH" not in (note_val or ""), \
                "凭据明文不应落库"


# ─── cert_expiry_check ────────────────────────────────────────────────────────

def test_cert_expiry_check_future():
    """即将到期(未来1年内)→ 应出现在预警列表"""
    with _TmpDB() as db:
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%d")
        hek.cert_ledger_add(
            cert_type="REACH", product="M-996",
            expiry_date=future, db_path=db
        )
        r = hek.cert_expiry_check(days=90, db_path=db)
        assert r["ok"]
        found = any(e["cert_type"] == "REACH" for e in r.get("expiring", []))
        assert found, "60天后到期应在90天预警内"


def test_cert_expiry_check_already_expired():
    """已过期 → expired=True, days_remaining<0"""
    with _TmpDB() as db:
        hek.cert_ledger_add(
            cert_type="ISO9001", product="全系列",
            issued_date="2020-01-01", expiry_date="2022-06-01",
            db_path=db
        )
        r = hek.cert_expiry_check(days=90, db_path=db)
        assert r["ok"]
        expired_items = [e for e in r.get("expiring", []) if e.get("expired")]
        assert expired_items, "已过期条目应出现在预警列表"
        for e in expired_items:
            assert e["days_remaining"] < 0, "已过期 days_remaining 应为负数"
            assert e["alert_level"] == "expired"


def test_cert_expiry_check_permanent_not_in_alert():
    """空到期日(永久有效) → 不出现在预警列表"""
    with _TmpDB() as db:
        hek.cert_ledger_add(
            cert_type="MSDS", product="全系列",
            issued_date="2024-01-01", expiry_date="",
            db_path=db
        )
        r = hek.cert_expiry_check(days=9999, db_path=db)
        assert r["ok"]
        assert not any(e["cert_type"] == "MSDS" for e in r.get("expiring", [])), \
            "空到期日(永久有效)不应在预警列表"


def test_cert_expiry_check_boundary():
    """恰好今天到期 → 应在预警列表, days_remaining<=0"""
    with _TmpDB() as db:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hek.cert_ledger_add(
            cert_type="COA", product="M-996",
            expiry_date=today, db_path=db
        )
        r = hek.cert_expiry_check(days=1, db_path=db)
        assert r["ok"]
        found = any(e["cert_type"] == "COA" for e in r.get("expiring", []))
        assert found, "今天到期应在1天预警内"


def test_cert_expiry_check_far_future_not_in_short_window():
    """2年后到期 → 不在90天预警内"""
    with _TmpDB() as db:
        from datetime import datetime, timezone, timedelta
        far = (datetime.now(timezone.utc) + timedelta(days=730)).strftime("%Y-%m-%d")
        hek.cert_ledger_add(
            cert_type="RoHS", product="全系列",
            expiry_date=far, db_path=db
        )
        r = hek.cert_expiry_check(days=90, db_path=db)
        assert r["ok"]
        assert not any(e["cert_type"] == "RoHS" for e in r.get("expiring", [])), \
            "2年后到期不应在90天预警内"


def test_cert_expiry_check_nan_days():
    """NaN days → 使用默认值 90, 不崩溃"""
    with _TmpDB() as db:
        r = hek.cert_expiry_check(days=float("nan"), db_path=db)  # type: ignore
        assert r["ok"], "NaN days 应降级为默认值而不是崩溃"
        assert r["days"] == 90, f"NaN days 应fallback到90, got {r['days']}"


def test_cert_expiry_check_inf_days():
    """inf days → 使用默认值(夹到max), 不崩溃"""
    with _TmpDB() as db:
        r = hek.cert_expiry_check(days=float("inf"), db_path=db)  # type: ignore
        assert r["ok"], "inf days 应不崩溃"


# ─── dispatch 白名单 ────────────────────────────────────────────────────────────

def test_dispatch_whitelist_allows_readonly():
    """dispatch 放行只读动作"""
    r = hek.dispatch("hs_export_kit_status", {})
    assert r["ok"], f"status 应被放行: {r}"


def test_dispatch_blocks_write():
    """dispatch 拒绝写库动作"""
    r = hek.dispatch("hs_export_kit_cert_ledger_add", {"cert_type": "X", "product": "Y"})
    assert not r["ok"], "cert_ledger_add 不应经 dispatch 放行"


def test_dispatch_blocks_unknown():
    """dispatch 拒绝不在白名单的动作"""
    r = hek.dispatch("hs_export_kit_run_shell", {})
    assert not r["ok"]
    r2 = hek.dispatch("hs_export_kit_nonexistent_action", {})
    assert not r2["ok"]


def test_dispatch_hs_lookup():
    """dispatch hs_lookup 正常工作"""
    r = hek.dispatch("hs_export_kit_hs_lookup", {"query": "工业树脂"})
    assert r["ok"]
    assert r["total"] > 0


def test_dispatch_cert_expiry():
    """dispatch cert_expiry_check 正常工作"""
    with _TmpDB() as db:
        r = hek.dispatch("hs_export_kit_cert_expiry_check", {"days": 30})
        assert r["ok"]


# ─── coa_freshness (COA 覆盖/新鲜度) ─────────────────────────────────────────────

def _add_coa(db, scope, issued, cert_no, product="工业树脂"):
    """seed 一张 COA(cert_no 须唯一, _cert_id=hash(type|no|product) 否则 upsert 合并)。"""
    return hek.cert_ledger_add(
        cert_type="COA", product=product, cert_no=cert_no,
        issued_date=issued, scope=scope, db_path=db,
    )


def test_coa_freshness_fresh_and_stale():
    """最新COA超阈值(>6月)→stale; 近期→fresh。按客户(scope)聚合。"""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc)
    old = (today - timedelta(days=300)).strftime("%Y-%m-%d")
    recent = (today - timedelta(days=10)).strftime("%Y-%m-%d")
    with _TmpDB() as db:
        _add_coa(db, "STALECO", old, "STALECO_001")
        _add_coa(db, "FRESHCO", recent, "FRESHCO_001")
        r = hek.coa_freshness(stale_months=6, db_path=db)
        assert r["ok"]
        assert r["total_coa"] == 2
        assert r["customers_covered"] == 2
        stale_names = {s["customer"] for s in r["stale"]}
        assert "STALECO" in stale_names, "300天前的COA应判过旧"
        assert "FRESHCO" not in stale_names, "10天前的COA应新鲜"
        assert r["stale_customers"] == 1
        assert r["fresh_customers"] == 1


def test_coa_freshness_latest_coa_wins():
    """同客户多张COA取最新签发日: 有近期COA→不算过旧, coa_count计全部。"""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc)
    old = (today - timedelta(days=400)).strftime("%Y-%m-%d")
    recent = (today - timedelta(days=20)).strftime("%Y-%m-%d")
    with _TmpDB() as db:
        _add_coa(db, "MIXEDCO", old, "MIXEDCO_OLD")
        _add_coa(db, "MIXEDCO", recent, "MIXEDCO_NEW")
        r = hek.coa_freshness(stale_months=6, db_path=db)
        assert r["customers_covered"] == 1
        assert r["stale_customers"] == 0, "有近期COA的客户不应算过旧"
        assert not any(s["customer"] == "MIXEDCO" for s in r["stale"])


def test_coa_freshness_future_date_does_not_mask_stale():
    """回归: 客户有真实16月前COA + 一张误解析未来COA → 必判 stale(未来日期不得掩盖陈旧度)。"""
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc)
    old = (today - timedelta(days=480)).strftime("%Y-%m-%d")      # ~16 月前(真实)
    future = (today + timedelta(days=365)).strftime("%Y-%m-%d")   # ingest 误解析未来日
    with _TmpDB() as db:
        _add_coa(db, "MASKCO", old, "MASKCO_REAL")
        _add_coa(db, "MASKCO", future, "MASKCO_FUTURE")
        r = hek.coa_freshness(stale_months=6, db_path=db)
        stale_names = {s["customer"] for s in r["stale"]}
        assert "MASKCO" in stale_names, "未来日期不得掩盖真实陈旧度, 该客户应判过旧"
        # 取的是真实过去日, 非未来日
        maskco = next(s for s in r["stale"] if s["customer"] == "MASKCO")
        assert maskco["latest_coa_date"] == old


def test_coa_freshness_future_only_is_anomaly_not_fresh():
    """客户仅有未来签发日(全部误解析) → 计入 future_dated_customers, 不算 fresh。"""
    from datetime import datetime, timezone, timedelta
    future = (datetime.now(timezone.utc) + timedelta(days=200)).strftime("%Y-%m-%d")
    with _TmpDB() as db:
        _add_coa(db, "FUTUREONLY", future, "FUTUREONLY_1")
        r = hek.coa_freshness(stale_months=6, db_path=db)
        assert r["future_dated_customers"] == 1
        assert r["fresh_customers"] == 0
        assert r["stale_customers"] == 0
        assert not any(s["customer"] == "FUTUREONLY" for s in r["stale"])


def test_coa_freshness_private_flagged():
    """输出标 _private(客户业务情报, 与 dormant/reorder/customer_ranking 一致)。"""
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=300)).strftime("%Y-%m-%d")
    with _TmpDB() as db:
        _add_coa(db, "SOMECO", old, "SOMECO_1")
        r = hek.coa_freshness(stale_months=6, db_path=db)
        assert r.get("_private") is True


def test_coa_freshness_via_dispatch():
    """新式 dispatch action=coa_freshness 可达(只读, 不在 _CLI_ONLY)。"""
    r = hek.dispatch("hs_export_kit", {"action": "coa_freshness", "stale_months": 6})
    assert r["ok"]
    assert "stale" in r
    assert "coa_freshness" in hek._DISPATCH_WHITELIST
    assert "coa_freshness" not in hek._CLI_ONLY_EXT


# ─── 辅助函数护栏 ───────────────────────────────────────────────────────────────

def test_finite_num_nan():
    assert hek._finite_num(float("nan")) is None


def test_finite_num_inf():
    assert hek._finite_num(float("inf")) is None
    assert hek._finite_num(float("-inf")) is None


def test_finite_num_normal():
    assert hek._finite_num(3.14) == 3.14
    assert hek._finite_num("42") == 42.0
    assert hek._finite_num("bad") is None


def test_as_int_nan_returns_default():
    assert hek._as_int(float("nan"), 90, 1, 3650) == 90


def test_as_int_clamp():
    assert hek._as_int(0, 90, 1, 3650) == 1
    assert hek._as_int(99999, 90, 1, 3650) == 3650


def test_parse_date_valid():
    from datetime import datetime, timezone
    dt = hek._parse_date("2024-06-15")
    assert dt is not None
    assert dt.year == 2024 and dt.month == 6 and dt.day == 15


def test_parse_date_invalid():
    assert hek._parse_date("not-a-date") is None
    assert hek._parse_date("") is None
    assert hek._parse_date(None) is None  # type: ignore


def test_parse_date_semantic_comparison():
    """日期语义化比较, 非字符串字典序"""
    dt_sep = hek._parse_date("2023-09-01")
    dt_oct = hek._parse_date("2023-10-01")
    assert dt_sep is not None and dt_oct is not None
    assert dt_sep < dt_oct  # 语义比较 9月 < 10月


def test_redact_removes_token():
    """_redact 抹凭据"""
    s = hek._redact("api_key=sk-ABCDEFGH1234567890XYZ 敏感")
    assert "sk-ABCDEFGH" not in s


def test_redact_hientropy():
    """_redact 高熵令牌兜底"""
    # 含 base64 / 的高熵密钥
    secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    s = hek._redact("secret " + secret)
    assert secret not in s


def test_is_tool():
    # 旧式前缀调用(向后兼容)
    assert hek.is_tool("hs_export_kit_hs_lookup")
    assert hek.is_tool("hs_export_kit_status")
    assert not hek.is_tool("hs_export_kit_cert_ledger_add")
    assert not hek.is_tool("other_module_status")
    # 新式: name == "hs_export_kit"
    assert hek.is_tool("hs_export_kit"), "新式 is_tool('hs_export_kit') 应为 True"
    assert not hek.is_tool("other_tool"), "其他工具名应为 False"


# ─── 三件套专项测试 ────────────────────────────────────────────────────────────

def test_approved_tools_nonempty():
    """approved_tools() 应返回非空列表且 name 正确"""
    tools = hek.approved_tools()
    assert tools, "approved_tools() 不应返回空列表"
    assert tools[0]["function"]["name"] == "hs_export_kit", \
        f"approved_tools name 应为 hs_export_kit, 得 {tools[0]['function']['name']!r}"


def test_approved_tools_spec_structure():
    """approved_tools spec 含必要字段: description, parameters, action enum"""
    tools = hek.approved_tools()
    fn = tools[0]["function"]
    assert "description" in fn, "spec 应含 description"
    assert "parameters" in fn, "spec 应含 parameters"
    params = fn["parameters"]["properties"]
    assert "action" in params, "spec 应含 action 参数"
    assert "query" in params, "spec 应含 query 参数"
    assert "country" in params, "spec 应含 country 参数"
    assert "days" in params, "spec 应含 days 参数"
    # cert_ledger_add 写库动作不应暴露在 description 中作为可用 action
    desc = fn["description"]
    assert "cert_ledger_add" not in desc or "CLI" in desc, \
        "若提到 cert_ledger_add 应注明须经CLI"


def test_dispatch_new_style_hs_lookup():
    """新式 dispatch: name='hs_export_kit', action='hs_lookup'"""
    r = hek.dispatch("hs_export_kit", {"action": "hs_lookup", "query": "工业树脂"})
    assert r["ok"], f"新式 dispatch hs_lookup 失败: {r}"
    assert r["total"] > 0


def test_dispatch_new_style_doc_checklist():
    """新式 dispatch: name='hs_export_kit', action='doc_checklist'"""
    r = hek.dispatch("hs_export_kit", {"action": "doc_checklist",
                                        "country": "越南", "payment": "LC"})
    assert r["ok"], f"新式 dispatch doc_checklist 失败: {r}"
    assert r.get("lc_notes"), "LC 应有 lc_notes"


def test_dispatch_new_style_cert_expiry():
    """新式 dispatch: name='hs_export_kit', action='cert_expiry_check'"""
    r = hek.dispatch("hs_export_kit", {"action": "cert_expiry_check", "days": 30})
    assert r["ok"], f"新式 dispatch cert_expiry_check 失败: {r}"


def test_dispatch_new_style_cert_ledger_readonly():
    """新式 dispatch: cert_ledger 只读 list 应放行"""
    r = hek.dispatch("hs_export_kit", {"action": "cert_ledger"})
    assert r["ok"], f"新式 dispatch cert_ledger(只读) 失败: {r}"


def test_dispatch_new_style_status():
    """新式 dispatch: name='hs_export_kit', action='status'"""
    r = hek.dispatch("hs_export_kit", {"action": "status"})
    assert r["ok"], f"新式 dispatch status 失败: {r}"


def test_dispatch_new_style_blocks_cert_ledger_add():
    """新式 dispatch 绝不放行 cert_ledger_add(写库)"""
    r = hek.dispatch("hs_export_kit", {"action": "cert_ledger_add",
                                        "cert_type": "X", "product": "Y"})
    assert not r["ok"], "新式 dispatch 不应放行 cert_ledger_add(写库)"


def test_dispatch_new_style_blocks_selftest():
    """新式 dispatch 绝不放行 selftest 动作"""
    r = hek.dispatch("hs_export_kit", {"action": "selftest"})
    assert not r["ok"], "新式 dispatch 不应放行 selftest 动作"


def test_dispatch_new_style_blocks_unknown():
    """新式 dispatch 拒绝未知 action"""
    r = hek.dispatch("hs_export_kit", {"action": "drop_table_certs"})
    assert not r["ok"], "新式 dispatch 应拒绝未知 action"


def test_dispatch_unknown_tool_name():
    """完全不认识的工具名 → 拒绝"""
    r = hek.dispatch("totally_unknown_module", {"action": "status"})
    assert not r["ok"], "未知工具名应被拒绝"


def test_read_actions_no_write():
    """_READ_ACTIONS 不包含写库或危险动作"""
    for forbidden in ("cert_ledger_add", "selftest"):
        assert forbidden not in hek._READ_ACTIONS, \
            f"_READ_ACTIONS 不应包含写库/危险动作 {forbidden!r}"


# ─── selftest ─────────────────────────────────────────────────────────────────

def test_selftest_passes():
    """内置 selftest 应全部通过"""
    rc = hek._selftest()
    assert rc == 0, f"selftest 返回非零: {rc}"


# ─── 直接运行兜底 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 收集所有 test_ 函数运行
    import traceback
    passed = 0
    failed = 0
    errors = []
    globs = dict(globals())
    for name, fn in sorted(globs.items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"  PASS  {name}")
            except Exception as e:
                failed += 1
                errors.append((name, e))
                print(f"  FAIL  {name}: {e}")
                traceback.print_exc()

    print(f"\n总计: {passed} PASS, {failed} FAIL")
    if errors:
        sys.exit(1)
    else:
        print("全部通过 ✓")
        sys.exit(0)
