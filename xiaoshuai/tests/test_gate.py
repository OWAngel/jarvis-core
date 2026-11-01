import gate
import store


class FakeVerifier:
    def __init__(self, verdicts):
        self.verdicts = list(verdicts)
        self.calls = 0

    def verify(self, sha, files, goal, repo="."):
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return v


class FakeControl:
    def __init__(self):
        self.acted = []

    def activate(self, sha, branch="", worktree="", restart_cb=None):
        self.acted.append(("activate", sha, branch))
        return True

    def revert(self, sha):
        self.acted.append(("revert", sha))
        return True

    def discard_branch(self, branch, worktree=""):
        self.acted.append(("discard", branch))
        return True

    def merge_branch(self, branch, worktree=""):
        self.acted.append(("merge", branch))
        return True

    def attempt_fix(self, sha, findings):
        self.acted.append(("fix", sha))
        return True


class FakeNotify:
    def alert(self, *a, **k):
        pass

    def fyi(self, *a, **k):
        pass

    def set_badge(self, *a, **k):
        pass


def _mk(tmp_jarvis, verifier, control):
    s = store.Store(base=str(tmp_jarvis))
    return gate.Gate(store=s, verifier=verifier, control=control,
                     notify=FakeNotify(), fix_max_rounds=1), s


def test_safe_clean_auto_activates(tmp_jarvis):
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "SAFE", "touches_guard": False,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "safe_activated"
    assert ("activate", "abc", "") in ctl.acted


def test_safe_touching_guard_escalates(tmp_jarvis):
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "SAFE", "touches_guard": True,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "escalated"
    assert ("activate", "abc", "") not in ctl.acted


def test_block_auto_reverts(tmp_jarvis):
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "BLOCK", "touches_guard": True,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "blocked_reverted"
    assert ("revert", "abc") in ctl.acted


def test_fix_then_safe_activates(tmp_jarvis):
    ctl = FakeControl()
    vf = FakeVerifier([
        {"verdict": "FIX_FIRST", "touches_guard": False, "findings": [{"detail": "d"}], "serious": []},
        {"verdict": "SAFE", "touches_guard": False, "findings": [], "serious": []}])
    g, s = _mk(tmp_jarvis, vf, ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "safe_activated"
    assert ("fix", "abc") in ctl.acted and ("activate", "abc", "") in ctl.acted


def test_null_touches_guard_escalates(tmp_jarvis):
    # fail-open 回归(小帅终验 wu78etv3j): 模型回 touches_guard=null(没明确断言 false)→ 保守不激活
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "SAFE", "touches_guard": None,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "escalated"
    assert ("activate", "abc", "") not in ctl.acted


def test_fix_still_bad_escalates(tmp_jarvis):
    ctl = FakeControl()
    vf = FakeVerifier([
        {"verdict": "FIX_FIRST", "touches_guard": False, "findings": [{"detail": "d"}], "serious": []},
        {"verdict": "FIX_FIRST", "touches_guard": False, "findings": [{"detail": "d"}], "serious": []}])
    g, s = _mk(tmp_jarvis, vf, ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "escalated"


# --------------------------------------------------------------------------- #
# 闭环(2026-06-05): 隔离分支模式 —— SAFE 才 merge 分支, BLOCK 删分支
# --------------------------------------------------------------------------- #
def test_safe_with_branch_merges(tmp_jarvis):
    """带 branch 的 review SAFE → 走 merge_branch(分支模式), 不是裸 activate。"""
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "SAFE", "touches_guard": False,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g",
                           branch="self-improve/si123", worktree="/tmp/wt/si123")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "safe_activated"
    # control.activate 带上了 branch(分支模式 merge 回 main)
    assert ("activate", "abc", "self-improve/si123") in ctl.acted


def test_block_with_branch_discards(tmp_jarvis):
    """带 branch 的 review BLOCK → discard_branch(删分支, 不 git revert main)。"""
    ctl = FakeControl()
    g, s = _mk(tmp_jarvis, FakeVerifier([{"verdict": "BLOCK", "touches_guard": True,
                                          "findings": [], "serious": []}]), ctl)
    rid = s.enqueue_review("abc", ["x.py"], "g",
                           branch="self-improve/si456", worktree="/tmp/wt/si456")
    g.process_one(s.get(rid))
    assert s.get(rid)["state"] == "blocked_reverted"
    assert ("discard", "self-improve/si456") in ctl.acted
    assert ("revert", "abc") not in ctl.acted  # 分支模式不该走 main revert


# ─────────────────────────────────────────────────────────────────────────────
# activate 失败时绝不发"已激活"假通知
#
# 真实事故(2026-07-05, self-improve/si8767120): 小帅判 SAFE、touches_guard=False,
# 但 control.activate() 里的 merge_branch 机械失败 → state=error。当时 notify.fyi
# 在 ok 的两个分支之外**无条件**触发, User 收到"已 merge 回 main 并激活"的假成功,
# 分支就此被遗忘一个月。且 store.next_pending() 只捞 state='pending', error 是终态
# 永不重试 —— 系统自己也不会再管它。所以失败路径必须发**告警**而不是静默或假喜报。
# ─────────────────────────────────────────────────────────────────────────────

class RecordingNotify:
    def __init__(self):
        self.alerts = []
        self.fyis = []

    def alert(self, sev, title, body=""):
        self.alerts.append((sev, title, body))

    def fyi(self, title, body=""):
        self.fyis.append((title, body))

    def set_badge(self, *a, **k):
        pass


class FailingActivateControl(FakeControl):
    """activate 返回 False(merge 回 main 失败), 其余行为同 FakeControl。"""

    def activate(self, sha, branch="", worktree="", restart_cb=None):
        self.acted.append(("activate", sha, branch))
        return False


def _mk_with_notify(tmp_jarvis, verifier, control, notify):
    s = store.Store(base=str(tmp_jarvis))
    return gate.Gate(store=s, verifier=verifier, control=control,
                     notify=notify, fix_max_rounds=1), s


_SAFE = {"verdict": "SAFE", "touches_guard": False, "findings": [], "serious": []}


def test_activate_failure_alerts_and_sends_no_success_fyi(tmp_jarvis):
    ctl, nt = FailingActivateControl(), RecordingNotify()
    g, s = _mk_with_notify(tmp_jarvis, FakeVerifier([_SAFE]), ctl, nt)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    st = g.process_one(s.get(rid))

    assert st == "error"
    assert s.get(rid)["state"] == "error"
    # 铁律: 失败路径绝不发"已激活"喜报
    assert nt.fyis == [], f"activate 失败却发了 fyi: {nt.fyis}"
    # 必须告警, 且是高优(error 是终态, 不告警就永远没人管)
    assert len(nt.alerts) == 1, f"activate 失败必须告警一次, 实际: {nt.alerts}"
    assert nt.alerts[0][0] == "high"


def test_activate_failure_alert_names_the_branch(tmp_jarvis):
    """告警必须点名还留着的分支, 否则 User 不知道去哪捞。"""
    ctl, nt = FailingActivateControl(), RecordingNotify()
    g, s = _mk_with_notify(tmp_jarvis, FakeVerifier([_SAFE]), ctl, nt)
    rid = s.enqueue_review("abc", ["x.py"], "g", branch="self-improve/si999")
    g.process_one(s.get(rid))

    blob = " ".join(nt.alerts[0])
    assert "self-improve/si999" in blob, f"告警没点名分支: {nt.alerts}"


def test_activate_success_still_sends_fyi_and_no_alert(tmp_jarvis):
    """成功路径行为不变: 发 fyi, 不发 alert(防止修复把正常通知也砍了)。"""
    ctl, nt = FakeControl(), RecordingNotify()
    g, s = _mk_with_notify(tmp_jarvis, FakeVerifier([_SAFE]), ctl, nt)
    rid = s.enqueue_review("abc", ["x.py"], "g")
    st = g.process_one(s.get(rid))

    assert st == "safe_activated"
    assert len(nt.fyis) == 1, f"成功路径应发 1 条 fyi, 实际: {nt.fyis}"
    assert nt.alerts == [], f"成功路径不该告警: {nt.alerts}"
