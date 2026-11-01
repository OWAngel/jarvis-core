import json
import store


def test_enqueue_and_next_pending(tmp_jarvis):
    s = store.Store(base=str(tmp_jarvis))
    rid = s.enqueue_review("abc123", ["brain/x.py"], "加个工具")
    row = s.next_pending()
    assert row["id"] == rid
    assert row["commit_sha"] == "abc123"
    assert row["files"] == ["brain/x.py"]
    assert row["state"] == "pending"


def test_set_state_and_verdict(tmp_jarvis):
    s = store.Store(base=str(tmp_jarvis))
    rid = s.enqueue_review("abc", ["a.py"], "g")
    s.set_state(rid, "verifying")
    s.set_state(rid, "safe_activated", verdict={"verdict": "SAFE"})
    row = s.get(rid)
    assert row["state"] == "safe_activated"
    assert row["verdict"]["verdict"] == "SAFE"
    assert s.next_pending() is None


def test_decision_audit_appends(tmp_jarvis):
    s = store.Store(base=str(tmp_jarvis))
    rid = s.enqueue_review("abc", ["a.py"], "g")
    s.record_decision(rid, "approve", "admin", "看着没问题")
    audit = (tmp_jarvis / "audit.jsonl").read_text().strip().splitlines()
    assert len(audit) >= 1
    last = json.loads(audit[-1])
    assert last["action"] == "approve" and last["actor"] == "admin"


def test_list_reviews_filter(tmp_jarvis):
    s = store.Store(base=str(tmp_jarvis))
    a = s.enqueue_review("a", [], "g1")
    b = s.enqueue_review("b", [], "g2")
    s.set_state(b, "escalated")
    pend = s.list_reviews("pending")
    esc = s.list_reviews("escalated")
    assert any(r["id"] == a for r in pend)
    assert [r["id"] for r in esc] == [b]
