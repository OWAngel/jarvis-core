"""小帅 verdict 状态机:消费待审进化, 跑 verifier, 按 verdict 落子激活/回滚/升级。"""
from __future__ import annotations


class Gate:
    def __init__(self, store, verifier, control, notify, repo: str = ".", fix_max_rounds: int = 1):
        self.store = store
        self.verifier = verifier
        self.control = control
        self.notify = notify
        self.repo = repo
        self.fix_max_rounds = fix_max_rounds

    def _verdict_ok(self, v: dict) -> bool:
        # touches_guard 必须**显式 False** 才算不碰护栏(fail-open 修复, 小帅终验 wu78etv3j):
        # None/null/0/"" 等"模型没明确断言 false"的情况一律保守按碰护栏 → 不自动激活、升级 User。
        return v.get("verdict") == "SAFE" and v.get("touches_guard") is False

    def process_one(self, review: dict) -> str:
        rid, sha = review["id"], review["commit_sha"]
        self.store.set_state(rid, "verifying")
        v = self.verifier.verify(sha, review["files"], review["goal"], repo=self.repo)

        # FIX_FIRST: 自动修 fix_max_rounds 轮, 每轮修后重验
        rounds = 0
        while v.get("verdict") == "FIX_FIRST" and rounds < self.fix_max_rounds:
            self.store.set_state(rid, "fixing", verdict=v)
            self.control.attempt_fix(sha, v.get("findings", []))
            v = self.verifier.verify(sha, review["files"], review["goal"], repo=self.repo)
            rounds += 1

        branch = review.get("branch", "") or ""
        worktree = review.get("worktree", "") or ""
        if self._verdict_ok(v):
            # 隔离分支模式: SAFE 才 merge 回 main(control.activate 内做); 无 branch 走旧重启路径。
            ok = self.control.activate(sha, branch=branch, worktree=worktree)
            state = "safe_activated" if ok else "error"
            self.store.set_state(rid, state, verdict=v)
            self.store.record_decision(rid, state, "xiaoshuai", v.get("summary", ""))
            if ok:
                self.notify.fyi("自动激活", f"进化 {sha[:8]} 验证 SAFE, 已 merge 回 main 并激活")
            else:
                # 验证过了但 merge 回 main 机械失败(切不回干净 main / 冲突 abort 等)。
                # error 是终态 —— store.next_pending() 只捞 pending, 系统永不重试,
                # 所以这里不告警就等于永久静默。分支/worktree 都还在, 必须点名让人去捞。
                self.notify.alert(
                    "high", "SAFE 但激活失败(需人工)",
                    f"进化 {sha[:8]} 验证 SAFE, 但 merge 回 main 失败。"
                    f"分支 {branch or '(无)'} 与 worktree {worktree or '(无)'} 均保留, "
                    f"需人工合并; 本条已置 error, 不会自动重试。")
            return state

        if v.get("verdict") == "BLOCK":
            # 隔离分支模式: 删分支即天然回滚(main 从没 merge); 无 branch 才走旧 git revert。
            if branch:
                self.control.discard_branch(branch, worktree)
            else:
                self.control.revert(sha)
            self.store.set_state(rid, "blocked_reverted", verdict=v)
            self.store.record_decision(rid, "blocked_reverted", "xiaoshuai", v.get("summary", ""))
            self.notify.alert("high", "BLOCK 已丢弃分支", f"进化 {sha[:8]}: {v.get('summary', '')}")
            return "blocked_reverted"

        # SAFE 但碰护栏, 或 FIX 修不好 → 升级 User
        self.store.set_state(rid, "escalated", verdict=v)
        self.store.record_decision(rid, "escalated", "xiaoshuai", v.get("summary", ""))
        self.notify.alert("med", "需你审批", f"进化 {sha[:8]}: {v.get('verdict')} {v.get('summary', '')}")
        return "escalated"

    def tick(self) -> bool:
        row = self.store.next_pending()
        if not row:
            return False
        try:
            self.process_one(row)
        except Exception as e:
            self.store.set_state(row["id"], "error")
            self.notify.alert("high", "处理异常", f"{row['commit_sha'][:8]}: {e}")
        return True
