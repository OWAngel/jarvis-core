"""小帅持久化:reviews/decisions sqlite + append-only audit.jsonl。与小美库全隔离。"""
from __future__ import annotations
import os
import json
import sqlite3
import time
from typing import Optional


def _default_base() -> str:
    return os.environ.get("XIAOSHUAI_DIR") or os.path.expanduser("~/.jarvis/xiaoshuai")


class Store:
    def __init__(self, base: Optional[str] = None):
        self.base = base or _default_base()
        os.makedirs(self.base, exist_ok=True)
        self.db_path = os.path.join(self.base, "xiaoshuai.db")
        self.audit_path = os.path.join(self.base, "audit.jsonl")
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=5.0)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error:
            pass
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS reviews(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_sha TEXT, created_at REAL, files_json TEXT, goal TEXT,
                state TEXT DEFAULT 'pending', verdict_json TEXT,
                decided_by TEXT, decided_at REAL,
                branch TEXT, worktree TEXT)""")
            # 兼容旧库: 闭环改造(2026-06-05)加 branch/worktree —— self-improve 在隔离分支改,
            # SAFE 才 merge 回 main。旧库 ALTER 补列(幂等, 已存在则忽略)。
            for col in ("branch", "worktree"):
                try:
                    c.execute(f"ALTER TABLE reviews ADD COLUMN {col} TEXT")
                except Exception:
                    pass
            c.execute("""CREATE TABLE IF NOT EXISTS decisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER, action TEXT, actor TEXT, ts REAL, note TEXT)""")

    def enqueue_review(self, commit_sha: str, files: list, goal: str,
                       branch: str = "", worktree: str = "") -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO reviews(commit_sha,created_at,files_json,goal,state,branch,worktree) "
                "VALUES(?,?,?,?, 'pending',?,?)",
                (commit_sha, time.time(), json.dumps(files), goal, branch, worktree))
            return int(cur.lastrowid or -1)

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        d["files"] = json.loads(d.pop("files_json") or "[]")
        vj = d.pop("verdict_json", None)
        d["verdict"] = json.loads(vj) if vj else None
        return d

    def next_pending(self) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM reviews WHERE state='pending' ORDER BY id LIMIT 1").fetchone()
            return self._row(r) if r else None

    def get(self, rid: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
            return self._row(r) if r else None

    def set_state(self, rid: int, state: str, verdict: Optional[dict] = None) -> None:
        with self._conn() as c:
            if verdict is not None:
                c.execute("UPDATE reviews SET state=?, verdict_json=? WHERE id=?",
                          (state, json.dumps(verdict, ensure_ascii=False), rid))
            else:
                c.execute("UPDATE reviews SET state=? WHERE id=?", (state, rid))

    def record_decision(self, rid: int, action: str, actor: str, note: str = "") -> None:
        ts = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO decisions(review_id,action,actor,ts,note) VALUES(?,?,?,?,?)",
                      (rid, action, actor, ts, note))
            c.execute("UPDATE reviews SET decided_by=?, decided_at=? WHERE id=?", (actor, ts, rid))
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"review_id": rid, "action": action, "actor": actor,
                                "ts": ts, "note": note}, ensure_ascii=False) + "\n")
        # 实时导出监军决策到 Obsidian+Logseq 记忆区(User 2026-06-05)。best-effort, 失败不影响主流程。
        # 小帅独立性: knowledge_export 只读 config + 写 wiki/logseq, 不 import 小美能力模块, 不破依赖方向。
        try:
            import sys as _sys, os as _os
            _bp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "brain")
            if _bp not in _sys.path:
                _sys.path.insert(0, _bp)
            import knowledge_export
            knowledge_export.export_decision(action, actor=actor, note=note, ts=str(ts))
        except Exception:
            pass

    def list_reviews(self, state: Optional[str] = None, limit: int = 50) -> list:
        with self._conn() as c:
            if state:
                rows = c.execute("SELECT * FROM reviews WHERE state=? ORDER BY id DESC LIMIT ?",
                                 (state, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT ?",
                                 (limit,)).fetchall()
            return [self._row(r) for r in rows]
