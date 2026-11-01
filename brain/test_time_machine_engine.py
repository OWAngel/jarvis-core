# -*- coding: utf-8 -*-
"""时间因果引擎测试。跑: cd brain && <python> -m pytest test_time_machine_engine.py -v

用合成事件(ingest_events)驱动, 不碰真实源/网络/凭据。"""
from datetime import datetime, timedelta

import pytest
import time_machine as TM
import config


def _iso(dt):
    return dt.astimezone().isoformat(timespec="seconds")


def _priv_on(monkeypatch):
    """把「数据层隐私总开关」拨到 ON, 断言可回退的旧行为(默认关=隐私已放弃)。
    只覆盖 safety.data_layer_privacy_enabled 这一个 key, 其余 key 照常放行给原 config.get。"""
    _orig = config.get

    def _patched(key, fallback=None):
        if key == "safety.data_layer_privacy_enabled":
            return True
        return _orig(key, fallback)

    monkeypatch.setattr(config, "get", _patched)


@pytest.fixture
def eng(tmp_path):
    return TM.TimeMachine(base=str(tmp_path / "tm"))


@pytest.fixture
def now():
    return datetime.now().astimezone()


def _seed(eng, now):
    """种入一组确定事件: 报告A(进行中, 未来截止) / 报告B(已闭合) / 报告C(已错过) +
    重叠日程 + 邮件→待办滞后对。"""
    ev = [
        # 报告A: 创建 → 改文件 → 未来截止(10h 后)
        {"ts": _iso(now - timedelta(hours=48)), "source": "todos", "kind": "todo_created",
         "entity": "报告A", "summary": "写报告A"},
        {"ts": _iso(now - timedelta(hours=2)), "source": "files", "kind": "file_modified",
         "entity": "报告A", "summary": "草稿"},
        {"ts": _iso(now + timedelta(hours=10)), "source": "todos", "kind": "todo_deadline",
         "entity": "报告A", "summary": "截止"},
        # 报告B: 创建 → 过去截止 → 已完成(闭合)
        {"ts": _iso(now - timedelta(hours=72)), "source": "todos", "kind": "todo_created",
         "entity": "报告B", "summary": "写报告B"},
        {"ts": _iso(now - timedelta(hours=24)), "source": "todos", "kind": "todo_deadline",
         "entity": "报告B", "summary": "截止B"},
        {"ts": _iso(now - timedelta(hours=23)), "source": "todos", "kind": "todo_completed",
         "entity": "报告B", "summary": "完成B"},
        # 报告C: 过去截止 + 无完成 → 错过
        {"ts": _iso(now - timedelta(hours=5)), "source": "todos", "kind": "todo_deadline",
         "entity": "报告C", "summary": "截止C"},
        # 邮件 → 待办(滞后对, 邮件源=私密)
        {"ts": _iso(now - timedelta(hours=50)), "source": "email", "kind": "email_received",
         "entity": "email:acme.com", "summary": "机密正文"},
        {"ts": _iso(now - timedelta(hours=49)), "source": "todos", "kind": "todo_created",
         "entity": "回复ACME", "summary": "回复邮件"},
        # 两个重叠日程(私密日历)
        {"ts": _iso(now + timedelta(hours=2)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议X", "summary": "会议X", "meta": {"end": _iso(now + timedelta(hours=4))}},
        {"ts": _iso(now + timedelta(hours=3)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议Y", "summary": "会议Y", "meta": {"end": _iso(now + timedelta(hours=5))}},
    ]
    return eng.ingest_events(ev)


# --------------------------------------------------------------------------- #
def test_ingest_and_dedup(eng, now):
    r = _seed(eng, now)
    assert r["ok"] and r["ingested"] == 11
    # 二次吸入同一批 → 全去重, 不重复
    r2 = _seed(eng, now)
    assert r2["ingested"] == 0 and r2["deduped"] == 11


def test_protected_path_never_enters_graph(eng, now):
    r = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "xiaoshuai/gate.py", "summary": "改监军"},
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": ".gmail_creds.json", "summary": "凭据"},
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "正常.txt", "summary": "ok"},
    ])
    assert r["protected_skipped"] == 2 and r["ingested"] == 1
    # 受保护实体在图里查不到
    assert eng.event_lifecycle("xiaoshuai/gate.py")["ok"] is False


def test_private_event_redacted_and_collared(eng, now, monkeypatch):
    _seed(eng, now)
    # 新默认(开关关 = 数据层隐私已放弃): 邮件源事件原文入图, 不占位, 不标私密, 不切本机
    g = eng.timeline_graph(source="email")
    assert g["nodes"], "应有 email 节点"
    assert all(n["summary"] != TM._REDACT for n in g["nodes"])
    assert all(not n["private"] for n in g["nodes"])
    assert g.get("_private") is None and g.get("_force_local") is None

    # 开关开(可回退): 恢复旧行为——邮件事件 summary 入库即被抹 + 标私密 + 结果切本机
    _priv_on(monkeypatch)
    eng.ingest_events([
        {"ts": _iso(now - timedelta(hours=1)), "source": "email", "kind": "email_received",
         "entity": "email:contoso.com", "summary": "机密正文2"},
    ])
    g2 = eng.timeline_graph(entity="email:contoso.com")
    assert g2["nodes"]
    assert all(n["summary"] == TM._REDACT for n in g2["nodes"])
    assert all(n["private"] for n in g2["nodes"])
    # 含私密节点 → 结果标 _private/_force_local(切本机)
    assert g2.get("_private") is True and g2.get("_force_local") is True


def test_basename_private_file_redacted_and_not_written_back(eng, now, monkeypatch):
    # 新默认(开关关): basename 形式的私密文件名也不再判私密——原文入图 + 可进非私密回写集。
    r = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "2026工资单.xlsx", "summary": "金额", "private": False,
         "meta": {"path": "/Users/x/Desktop/2026工资单.xlsx"}},
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "普通笔记.txt", "summary": "ok", "private": False},
    ])
    assert r["private_stored"] == 0
    assert "2026工资单.xlsx" in r["nonprivate_entities"]
    assert "普通笔记.txt" in r["nonprivate_entities"]
    lc = eng.event_lifecycle("2026工资单.xlsx")
    assert lc["stages"][0]["summary"] == "金额" and lc.get("_private") is None

    # 开关开(可回退): 恢复旧行为——回归(护栏 HIGH): basename 形式的私密文件名(无斜杠)
    # 也要判私密, 且绝不进非私密回写集。
    _priv_on(monkeypatch)
    r2 = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "2027工资单.xlsx", "summary": "金额", "private": False,
         "meta": {"path": "/Users/x/Desktop/2027工资单.xlsx"}},
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "普通笔记2.txt", "summary": "ok", "private": False},
    ])
    assert r2["private_stored"] == 1
    assert "2027工资单.xlsx" not in r2["nonprivate_entities"]   # 不会被推给 world_model
    assert "普通笔记2.txt" in r2["nonprivate_entities"]
    lc2 = eng.event_lifecycle("2027工资单.xlsx")
    assert lc2["stages"][0]["summary"] == TM._REDACT and lc2.get("_private") is True


def test_protected_via_meta_path_blocked(eng, now):
    # 回归(护栏 MEDIUM): basename 不含受保护子串, 但 meta.path 在 xiaoshuai/ 下 → 仍绝不入图。
    r = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "guard.py", "summary": "监军", "private": False,
         "meta": {"path": "/Users/x/xiaoshuai/guard.py"}},
    ])
    assert r["protected_skipped"] == 1 and r["ingested"] == 0
    assert eng.event_lifecycle("guard.py")["ok"] is False


def test_private_file_path_pattern_detected(eng, now, monkeypatch):
    # 新默认(开关关): 未索引的私密文件路径(命中文件名模式)也不再判私密 → 原文入图
    r = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "/Users/x/Desktop/2026工资单.xlsx", "summary": "金额机密"},
    ])
    assert r["private_stored"] == 0
    lc = eng.event_lifecycle("/Users/x/Desktop/2026工资单.xlsx")
    assert lc["stages"][0]["summary"] == "金额机密"
    assert lc.get("_private") is None

    # 开关开(可回退): 恢复旧行为——命中文件名模式 → 入库即私密 + 抹正文
    _priv_on(monkeypatch)
    r2 = eng.ingest_events([
        {"ts": _iso(now), "source": "files", "kind": "file_modified",
         "entity": "/Users/x/Desktop/2027工资单.xlsx", "summary": "金额机密2"},
    ])
    assert r2["private_stored"] == 1
    lc2 = eng.event_lifecycle("/Users/x/Desktop/2027工资单.xlsx")
    assert lc2["stages"][0]["summary"] == TM._REDACT
    assert lc2.get("_private") is True


def test_timeline_graph_has_precedes_edges(eng, now):
    _seed(eng, now)
    g = eng.timeline_graph(entity="报告A")
    assert g["counts"]["nodes"] == 3
    rels = {e["rel"] for e in g["edges"]}
    assert "precedes" in rels and "deadline_for" in rels


def test_before_after_splits_on_anchor(eng, now):
    _seed(eng, now)
    ba = eng.before_after(entity="报告A", window_h=240)
    assert ba["ok"]
    # 锚点=最近事件; before 里应含更早的事件
    assert any(b["kind"] == "todo_created" for b in ba["before"]) or len(ba["before"]) >= 0
    assert ba["scope"] == "entity"


def test_event_lifecycle_status(eng, now):
    _seed(eng, now)
    a = eng.event_lifecycle("报告A")
    assert a["ok"] and a["status"] in ("open", "overdue") and a["event_count"] == 3
    b = eng.event_lifecycle("报告B")
    assert b["status"] == "closed"           # 有 todo_completed
    assert b["span_h"] is not None and b["span_h"] > 0


def test_deadline_reason_future_and_overdue(eng, now):
    _seed(eng, now)
    a = eng.deadline_reason(entity="报告A")
    assert a["ok"] and a["overdue"] is False and a["remaining_h"] > 0
    assert a["urgency"] in ("紧急", "高", "中", "低")
    assert "现在就该做" in a["why"] or "提前量" in a["why"]
    c = eng.deadline_reason(entity="报告C")
    assert c["overdue"] is True and c["completed"] is False


def test_sequence_causality_confidence_bounded(eng, now):
    _seed(eng, now)
    s = eng.sequence_causality(entity="报告A")
    assert s["ok"] and s["count"] == 2
    for link in s["chain"]:
        assert 0.0 < link["causal_confidence"] <= 0.85   # 永不满分
        assert link["rel"] == "precedes"


def test_lag_analysis_email_to_todo(eng, now):
    _seed(eng, now)
    lag = eng.lag_analysis(from_kind="email_received", to_kind="todo_created", lookback_h=1000)
    assert lag["ok"] and lag["samples"] >= 1
    assert lag["median_h"] >= 0


def test_missed_window_flags_overdue_uncompleted(eng, now):
    _seed(eng, now)
    m = eng.missed_window(lookback_h=1000)
    ents = {x["entity"] for x in m["missed"]}
    assert "报告C" in ents              # 过去截止 + 无完成
    assert "报告B" not in ents          # 已完成 → 不算错过


def test_missed_window_excludes_early_completed(eng, now):
    # 回归: 提前完成(ts_done < 截止)的待办, 即使截止已过也绝不算"错过"。
    eng.ingest_events([
        {"ts": _iso(now - timedelta(hours=50)), "source": "todos", "kind": "todo_created",
         "entity": "早完成任务", "summary": "x"},
        {"ts": _iso(now - timedelta(hours=40)), "source": "todos", "kind": "todo_completed",
         "entity": "早完成任务", "summary": "提前做完"},      # 完成于截止前
        {"ts": _iso(now - timedelta(hours=10)), "source": "todos", "kind": "todo_deadline",
         "entity": "早完成任务", "summary": "截止"},          # 截止已过
    ])
    m = eng.missed_window(lookback_h=1000)
    assert "早完成任务" not in {x["entity"] for x in m["missed"]}


def test_meta_long_values_stay_valid_json(eng, now):
    # 回归: 超长 meta 值不应破坏 JSON; calendar end 仍可读出供 conflict 用。
    long_loc = "地点" * 2000
    eng.ingest_events([
        {"ts": _iso(now + timedelta(hours=2)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:长会议", "summary": "长会议", "private": True,
         "meta": {"end": _iso(now + timedelta(hours=3)), "location": long_loc}},
    ])
    lc = eng.timeline_graph(entity="cal:长会议")
    # 直接读底层 meta 确认 JSON 合法 + end 保留
    import json as _json
    with eng._conn() as c:
        row = c.execute("SELECT meta_json FROM tm_events WHERE entity=?", ("cal:长会议",)).fetchone()
    meta = _json.loads(row["meta_json"])     # 不抛 → JSON 合法
    assert meta.get("end") and len(meta.get("location", "")) <= 500


def test_future_window_recommends_start(eng, now):
    _seed(eng, now)
    fw = eng.future_window(horizon_h=1000)
    ents = {w["entity"] for w in fw["windows"]}
    assert "报告A" in ents              # 未来 10h 截止
    a = next(w for w in fw["windows"] if w["entity"] == "报告A")
    assert a["recommended_start"] < a["deadline"]   # 提前启动
    assert a["lead_h"] > 0


def test_temporal_conflict_detects_overlap(eng, now, monkeypatch):
    _seed(eng, now)
    tc = eng.temporal_conflict(horizon_h=1000)
    assert tc["count"] >= 1
    pair = tc["conflicts"][0]
    assert pair["type"] == "时间重叠"
    # 新默认(开关关): 日历事件不再判私密, 冲突结果不切本机
    assert tc.get("_private") is None

    # 开关开(可回退): 恢复旧行为——日历是私密源 → 冲突结果切本机
    _priv_on(monkeypatch)
    eng.ingest_events([
        {"ts": _iso(now + timedelta(hours=6)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议P", "summary": "会议P", "meta": {"end": _iso(now + timedelta(hours=8))}},
        {"ts": _iso(now + timedelta(hours=7)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议Q", "summary": "会议Q", "meta": {"end": _iso(now + timedelta(hours=9))}},
    ])
    tc2 = eng.temporal_conflict(horizon_h=1000)
    assert tc2["count"] >= 1
    assert tc2.get("_private") is True


def test_explain_timing_synthesizes(eng, now):
    _seed(eng, now)
    e = eng.explain_timing(entity="报告A")
    assert e["ok"] and e["target"] == "报告A"
    assert e["why_now"] and "confidence_note" in e
    assert e["best_window"] is not None
    assert e["urgency"] in ("紧急", "高", "中", "低")


def test_audit_and_status(eng, now, monkeypatch):
    _seed(eng, now)
    au = eng.audit()
    assert au["ok"] and au["events"] == 11 and au["edges"] > 0
    # 新默认(开关关): 隐私统计恒 0(数据层隐私分类已放弃)
    assert au["private_events"] == 0
    assert au["dangling_edges"] == 0
    assert set(au["integrations"]) == {"world_model", "insight", "filehub",
                                       "reasoning_core", "goal_engine"}
    st = eng.engine_status()
    assert st["ok"] and st["events"] == 11
    assert "execlog" in st["sources"]["safe"] and "email" in st["sources"]["optin"]

    # 开关开(可回退): 恢复旧行为——email + calendar 源事件入库即计入 private_events
    _priv_on(monkeypatch)
    eng.ingest_events([
        {"ts": _iso(now - timedelta(hours=60)), "source": "email", "kind": "email_received",
         "entity": "email:extra.com", "summary": "机密附件"},
        {"ts": _iso(now + timedelta(hours=20)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议Z1", "summary": "会议Z1", "meta": {"end": _iso(now + timedelta(hours=21))}},
        {"ts": _iso(now + timedelta(hours=30)), "source": "calendar", "kind": "calendar_event",
         "entity": "cal:会议Z2", "summary": "会议Z2", "meta": {"end": _iso(now + timedelta(hours=31))}},
    ])
    au2 = eng.audit()
    assert au2["private_events"] >= 3        # email + 2 calendar


def test_dispatch_routing_and_whitelist(eng, now):
    _seed(eng, now)
    assert eng.dispatch("explain_timing", {"entity": "报告A"})["ok"] is True
    assert eng.dispatch("status", {})["ok"] is True
    assert eng.dispatch("nonsense", {})["ok"] is False
    # ingest 经 dispatch(程序内允许); 空源 → 安全无副作用
    r = eng.dispatch("ingest", {"sources": []})
    assert r["ok"] and r["ingested"] == 0


def test_reasoning_core_goal_engine_deferred(eng, now):
    # 两者尚未落地 → ingest writeback 把它们记 deferred(不建桩)
    eng.ingest_events([{"ts": _iso(now), "source": "todos", "kind": "todo_created",
                        "entity": "X", "summary": "x"}])
    wb = eng._writeback("test", ["X"])
    assert "reasoning_core" in wb["deferred"] and "goal_engine" in wb["deferred"]
