#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""E4 · 本机全域时间机器 + 可逆行动日志 + 时间因果推理/事件生命周期引擎。

两层能力, 共用一个本机库 ~/.jarvis/timemachine/tm.db(独立, 不污染 jarvis.db):

A) 可逆行动日志(原有, 不变):
   记录小美每个有副作用的动作(移动/写入/改配置/shell…)成 append-only 时间线, 并对**可逆**
   动作支持一键撤销(move 移回 / write 还原旧内容 / config_set 改回旧值)。让"做错了能回退"。

B) 时间因果引擎(本次增强):
   把文件/邮件/日历/待办/屏幕事件/业务流程/历史执行日志统一吸进一张可查询的"时间因果图"
   (tm_events 节点 + tm_edges 边), 让小美能判断: 一件事**为什么现在该做**、之前发生了什么
   **导致**它、**错过会引发什么后果**、**最佳行动窗口**是什么。结果经 world_model 主动回写、
   并以模块级公开函数供 reasoning_core/goal_engine/insight/filehub 取用。

   动作(经 dispatch / CLI):
     ingest             从各源吸事件入图(默认纯本机安全源; email/calendar 需显式 opt-in)
     timeline_graph     取时间因果子图(节点+边)
     before_after       某锚点之前/之后发生了什么(候选前因 / 后果)
     event_lifecycle    某实体的完整生命周期(创建→改动→引用→截止/完成)
     deadline_reason    为什么现在该做(截止/剩余/依赖/紧急度)
     sequence_causality 事件序列的前因后果链(启发式置信, 时间相邻≠因果)
     lag_analysis       两类事件之间的典型时间滞后分布
     missed_window      已错过的截止/窗口 + 后果提示
     future_window      未来截止的最佳行动窗口(按提前量算建议启动时刻)
     temporal_conflict  未来时间冲突(重叠日程 / 截止挤堆)
     explain_timing     综合解释一件事的时机(why_now / 来龙 / 错过后果 / 最佳窗口)
     audit              引擎自审(覆盖/边完整性/隐私统计/集成可用性)
     status             引擎概况(库/事件/边/最近吸入/集成)

铁律(User 2026-06-04 + 隐私护栏):
  · **小帅(xiaoshuai/)永久白名单, 排除在外** —— 绝不记录、更绝不撤销任何 xiaoshuai/ 路径或
    安全文件的动作(防经"撤销"通道反向篡改监军/护栏)。_protected() 双保险。
  · revert 只走文件级还原(rename / 写回内容 / config.set), 绝不删 User 文件。
  · 不可逆动作(shell/删除)只记录观测, 不提供 undo。
  · **受保护路径(小帅/凭据/local_powers/config.toml)绝不入时间因果图**(_protected 过滤)。
  · **隐私事件(email/calendar/私密文件/私密屏幕)入库即抹正文(summary 只留占位), 查询结果
    标 _private/_force_local 由大脑切本机模型收尾, 跨云不出。** 绝不读 .gmail_creds/.gcal_creds。
  · reasoning_core/goal_engine 尚未落地 → best-effort 软导入, 缺则记 deferred(不建桩)。

全本机, 纯 stdlib(可选集成模块软导入, 缺即降级)。
"""
from __future__ import annotations

import os
import re
import json
import shutil
import hashlib
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Any

import config  # noqa: E402 — 纯读取, 无副作用

REVERSIBLE_KINDS = ("move", "write", "config_set")

# 永久白名单/受保护: 这些路径片段的动作绝不纳入可撤销面, 更绝不入时间因果图(小帅 + 安全文件)
_PROTECTED = ("xiaoshuai/", "/xiaoshuai", "config.toml", "local_powers.py",
              ".gmail_creds", ".gcal_creds", ".localkey")

# ---- 时间因果引擎: 常量 ------------------------------------------------- #
# 截止/日程类事件(用于 deadline_reason / missed_window / future_window / conflict)
DEADLINE_KINDS = ("todo_deadline", "calendar_event", "deadline", "meeting")
# 完成类事件(用于判断生命周期是否闭合 / 窗口是否已被满足)
COMPLETION_KINDS = ("todo_completed", "done", "ap_result")
# 默认安全源(纯本机, 源头已脱敏): 不联网、不碰凭据。
_SAFE_SOURCES = ("execlog", "files", "todos", "awareness", "process", "autopilot", "nexus")
# opt-in 源(涉隐私 + 联网, 需显式列出才吸): 邮件 / 日历。事件一律标私密。
_OPTIN_SOURCES = ("email", "calendar")
_ALL_SOURCES = _SAFE_SOURCES + _OPTIN_SOURCES
# 这些源的事件强制私密(正文抹除 + 查询切本机)
_PRIVATE_SOURCES = ("email", "calendar")
_REDACT = "[隐私事件·正文留本机]"
_DEFAULT_INGEST_HOURS = 168.0   # 默认回溯 7 天

# 已知"因果对"(仅用于略微抬高启发式置信, 绝非断言因果)
_KNOWN_CAUSAL_PAIRS = {
    ("email_received", "todo_created"), ("inbound_msg", "todo_created"),
    ("inbound_email", "todo_created"), ("file_modified", "ap_action"),
    ("todo_created", "todo_completed"), ("calendar_event", "todo_completed"),
}

# 引擎库表(与 actions 同库, executescript 幂等建)
_ENGINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tm_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, source TEXT, kind TEXT, entity TEXT, summary TEXT,
    private INTEGER DEFAULT 0, meta_json TEXT, ingested_at TEXT,
    dedup TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS ix_tme_ts ON tm_events(ts);
CREATE INDEX IF NOT EXISTS ix_tme_entity ON tm_events(entity);
CREATE INDEX IF NOT EXISTS ix_tme_kind ON tm_events(kind);
CREATE TABLE IF NOT EXISTS tm_edges(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src INTEGER, dst INTEGER, rel TEXT, lag_secs REAL,
    weight REAL DEFAULT 1.0, basis TEXT, ts TEXT,
    UNIQUE(src, dst, rel));
CREATE INDEX IF NOT EXISTS ix_tmd_src ON tm_edges(src);
CREATE INDEX IF NOT EXISTS ix_tmd_dst ON tm_edges(dst);
CREATE TABLE IF NOT EXISTS tm_meta(k TEXT PRIMARY KEY, v TEXT);
"""


# --------------------------------------------------------------------------- #
# 通用 helper
# --------------------------------------------------------------------------- #
def _protected(target: str) -> bool:
    t = target or ""
    return any(p in t for p in _PROTECTED)


def _default_base() -> str:
    return os.environ.get("TIMEMACHINE_DIR") or os.path.expanduser("~/.jarvis/timemachine")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _now_dt() -> datetime:
    return datetime.now().astimezone()


def _opt(name: str):
    """软导入可选集成/源模块; 缺/出错 → None(降级)。importlib, 非 __import__/exec。"""
    try:
        import importlib
        return importlib.import_module(name)
    except Exception:
        return None


def _parse_ts(s: Any) -> Optional[datetime]:
    """宽松解析时间戳 → 带时区 datetime。支持 ISO8601 / RFC2822(邮件) / epoch。失败 → None。"""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.astimezone()
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(float(s)).astimezone()
        except Exception:
            return None
    s = str(s).strip()
    if not s:
        return None
    # epoch(秒或毫秒)
    if re.fullmatch(r"\d{9,13}", s):
        try:
            v = float(s)
            v = v / 1000.0 if v > 1e11 else v
            return datetime.fromtimestamp(v).astimezone()
        except Exception:
            pass
    # ISO8601(含仅日期)
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.astimezone()
    except Exception:
        pass
    # RFC2822(邮件 Date 头)
    try:
        from email.utils import parsedate_to_datetime
        d = parsedate_to_datetime(s)
        if d is not None:
            return d if d.tzinfo else d.astimezone()
    except Exception:
        pass
    return None


def _iso(dt: Optional[datetime]) -> str:
    return dt.astimezone().isoformat(timespec="seconds") if dt else ""


def _hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    return (b - a).total_seconds() / 3600.0


def _dedup(source: str, ts: str, kind: str, entity: str) -> str:
    raw = f"{source}|{ts}|{kind}|{entity}".encode("utf-8", "replace")
    return hashlib.sha1(raw).hexdigest()[:16]


def _is_private_path(path: str) -> bool:
    """文件源隐私判定: 复用 file_tools.is_private_path; 对未索引文件 fail-open(False) → 文件名模式兜底。"""
    ft = _opt("file_tools")
    if ft is not None:
        try:
            if ft.is_private_path(path):
                return True
        except Exception:
            pass
    low = (path or "").lower()
    pats = ("工资", "薪资", "薪酬", "bank", "银行", "身份证", "passport", "护照",
            "密码", "password", "credential", "secret", ".gmail_creds", ".gcal_creds")
    return any(p in low for p in pats)


def _event_is_private(ev: dict, ent: str, path: str, src: str) -> bool:
    """入图事件是否判私密(私密→抹正文占位 + 查询结果强制切本机, 见 _collar)。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认) → 恒 False, 私密事件不占位、原文入图、
    查询结果也不强制切本机(本模块无对外出口; 受保护路径仍由 _protected 独立守卫, 不靠此分类)。
    开 → 原判定不变: 显式 private 标记 / email·calendar 源 / 路径隐私模式, 命中任一即私密。"""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return False
    if bool(ev.get("private")) or (src in _PRIVATE_SOURCES):
        return True
    # 隐私判定对 entity 与 meta.path 都查(含 basename, 不再用 _looks_path 门控漏掉文件名)
    return bool(_is_private_path(ent) or (path and _is_private_path(path)))


def _urgency(rem_h: Optional[float], done: bool, overdue: bool) -> str:
    if done:
        return "已完成"
    if overdue:
        return "已逾期"
    if rem_h is None:
        return "未知"
    if rem_h <= 24:
        return "紧急"
    if rem_h <= 72:
        return "高"
    if rem_h <= 168:
        return "中"
    return "低"


def _deadline_why(name: str, rem_h: Optional[float], overdue: bool, done: bool) -> str:
    if done:
        return f"「{name}」已完成, 无需再做。"
    if overdue and rem_h is not None:
        return f"「{name}」已过截止 {abs(rem_h):.0f}h, 应尽快补救或重新评估。"
    if rem_h is not None and rem_h <= 24:
        return f"「{name}」距截止仅 {rem_h:.0f}h, 现在就该做。"
    if rem_h is not None:
        return f"「{name}」距截止 {rem_h:.0f}h(约 {rem_h / 24:.1f} 天), 按提前量规划启动。"
    return f"「{name}」无明确截止, 依最近活动与依赖判断优先级。"


def _missed_consequence(kind: str, overdue_h: float) -> str:
    base = {
        "todo_deadline": "待办逾期, 相关下游任务/承诺可能连带延误",
        "calendar_event": "日程已过, 可能错过会议/约定",
        "meeting": "会议时间已过, 可能错过纪要/决议",
        "deadline": "截止已过, 可能触发违约/罚则/信任损耗",
    }
    tail = "(逾期越久, 补救成本越高)" if overdue_h and overdue_h > 72 else ""
    return base.get(kind, "窗口已过, 可能产生连带影响") + tail


def _typical_lead(kind: str) -> float:
    """某类截止事件的默认"提前启动量"(小时), 用于算建议启动时刻。"""
    return {"meeting": 1.0, "calendar_event": 1.0, "todo_deadline": 24.0,
            "deadline": 48.0}.get(kind, 24.0)


def _causal_confidence(a: dict, b: dict, lag_h: Optional[float]) -> float:
    """启发式因果置信(0~0.85)。时间相邻 + 同实体 + 已知因果对 → 略抬高; 永不给满分。"""
    conf = 0.3
    if a.get("entity") == b.get("entity"):
        conf += 0.2
    if lag_h is not None:
        if 0 <= lag_h <= 1:
            conf += 0.2
        elif lag_h <= 24:
            conf += 0.1
    if (a.get("kind"), b.get("kind")) in _KNOWN_CAUSAL_PAIRS:
        conf += 0.2
    return round(min(conf, 0.85), 2)


def _sanitize_meta(meta: dict) -> dict:
    """裁剪 meta 各值长度(而非裁剪序列化串), 保证入库 JSON 永远合法 + 体积有界。"""
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in list(meta.items())[:20]:
        if isinstance(v, str):
            out[str(k)[:60]] = v[:500]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[str(k)[:60]] = v
        else:
            out[str(k)[:60]] = str(v)[:500]
    return out


def _safe_node(ev: dict) -> dict:
    """对外暴露的极简事件视图(私密事件 entity 保留供本机用, 由 _private collar 防外泄)。"""
    return {"id": ev.get("id"), "ts": ev.get("ts"), "source": ev.get("source"),
            "kind": ev.get("kind"), "entity": ev.get("entity"),
            "summary": ev.get("summary"), "private": bool(ev.get("private"))}


# --------------------------------------------------------------------------- #
# TimeMachine: 可逆行动日志(A) + 时间因果引擎(B)
# --------------------------------------------------------------------------- #
class TimeMachine:
    def __init__(self, base: Optional[str] = None):
        self.base = base or _default_base()
        os.makedirs(self.base, exist_ok=True)
        self.db_path = os.path.join(self.base, "tm.db")
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
            c.execute("""CREATE TABLE IF NOT EXISTS actions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, kind TEXT, target TEXT, detail TEXT,
                undo_json TEXT, reversible INTEGER DEFAULT 0,
                reverted INTEGER DEFAULT 0, reverted_at TEXT,
                source TEXT DEFAULT 'jarvis')""")
            c.execute("CREATE INDEX IF NOT EXISTS ix_tm_ts ON actions(ts)")
            c.executescript(_ENGINE_SCHEMA)   # 时间因果引擎表(幂等)

    # ===================================================================== #
    # A) 可逆行动日志(原有, 行为不变)
    # ===================================================================== #
    def record(self, kind: str, target: str = "", detail: str = "",
               undo: Optional[dict] = None, reversible: bool = False,
               source: str = "jarvis") -> int:
        """记一条动作。受保护路径(小帅/安全文件)强制记为不可逆观测(绝不给 undo)。"""
        if _protected(target):
            reversible = False
            undo = None
            detail = (detail + " [受保护路径, 仅观测不可撤销]").strip()
        rev = 1 if (reversible and kind in REVERSIBLE_KINDS and undo) else 0
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO actions(ts,kind,target,detail,undo_json,reversible,source) "
                "VALUES(?,?,?,?,?,?,?)",
                (_now_iso(), kind, target, detail,
                 json.dumps(undo, ensure_ascii=False) if undo else None, rev, source))
            return int(cur.lastrowid or -1)

    def _row(self, r: sqlite3.Row) -> dict:
        d = dict(r)
        uj = d.pop("undo_json", None)
        d["undo"] = json.loads(uj) if uj else None
        d["reversible"] = bool(d.get("reversible"))
        d["reverted"] = bool(d.get("reverted"))
        return d

    def timeline(self, limit: int = 50, kind: Optional[str] = None,
                 only_reversible: bool = False) -> list:
        q = "SELECT * FROM actions WHERE 1=1"
        params: list = []
        if kind:
            q += " AND kind=?"
            params.append(kind)
        if only_reversible:
            q += " AND reversible=1 AND reverted=0"
        q += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [self._row(r) for r in c.execute(q, params).fetchall()]

    def get(self, aid: int) -> Optional[dict]:
        with self._conn() as c:
            r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
            return self._row(r) if r else None

    def revert(self, aid: int) -> dict:
        """撤销一条可逆动作。受保护路径一律拒绝。返回 {ok, msg}。"""
        row = self.get(aid)
        if not row:
            return {"ok": False, "msg": f"无 #{aid}"}
        if row["reverted"]:
            return {"ok": False, "msg": f"#{aid} 已撤销过"}
        if not row["reversible"] or not row["undo"]:
            return {"ok": False, "msg": f"#{aid} [{row['kind']}] 不可撤销"}
        if _protected(row["target"]):
            return {"ok": False, "msg": f"#{aid} 受保护路径(小帅/安全), 拒绝撤销"}
        try:
            ok, msg = self._do_revert(row["kind"], row["undo"])
        except Exception as e:
            return {"ok": False, "msg": f"撤销失败: {e}"}
        if ok:
            with self._conn() as c:
                c.execute("UPDATE actions SET reverted=1, reverted_at=? WHERE id=?",
                          (_now_iso(), aid))
        return {"ok": ok, "msg": msg}

    def _do_revert(self, kind: str, undo: dict) -> tuple:
        if kind == "move":
            frm, to = undo.get("from"), undo.get("to")  # 把当前 from 移回 to
            if _protected(frm) or _protected(to):
                return False, "受保护路径, 拒绝"
            if not frm or not os.path.exists(frm):
                return False, f"源不存在: {frm}"
            os.makedirs(os.path.dirname(to) or ".", exist_ok=True)
            shutil.move(frm, to)
            return True, f"已移回 {to}"
        if kind == "write":
            path, old = undo.get("path"), undo.get("old_content")
            if _protected(path):
                return False, "受保护路径, 拒绝"
            if old is None:
                return False, "无旧内容备份"
            with open(path, "w", encoding="utf-8") as f:
                f.write(old)
            return True, f"已还原 {path}"
        if kind == "config_set":
            key, old = undo.get("key"), undo.get("old")
            try:
                import config
                config.set(key, old, _privileged=True)
            except Exception as e:
                return False, f"config 还原失败: {e}"
            return True, f"已还原配置 {key}={old}"
        return False, f"未知可逆类型 {kind}"

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            rev = c.execute("SELECT COUNT(*) FROM actions WHERE reversible=1 AND reverted=0").fetchone()[0]
            done = c.execute("SELECT COUNT(*) FROM actions WHERE reverted=1").fetchone()[0]
            by = {r[0]: r[1] for r in c.execute(
                "SELECT kind,COUNT(*) FROM actions GROUP BY kind").fetchall()}
        return {"total": total, "可撤销": rev, "已撤销": done, "by_kind": by, "db": self.db_path}

    # ===================================================================== #
    # B) 时间因果引擎: 吸入(ingest) + 建图(edges)
    # ===================================================================== #
    def _set_meta(self, c: sqlite3.Connection, k: str, v: Any) -> None:
        c.execute("INSERT OR REPLACE INTO tm_meta(k,v) VALUES(?,?)", (k, str(v)))

    def _get_meta(self, c: sqlite3.Connection, k: str) -> Optional[str]:
        r = c.execute("SELECT v FROM tm_meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def _bump_meta(self, c: sqlite3.Connection, k: str, delta: int) -> None:
        cur = self._get_meta(c, k)
        base = int(cur) if (cur and str(cur).lstrip("-").isdigit()) else 0
        self._set_meta(c, k, base + int(delta))

    def ingest_events(self, events: list, build_edges: bool = True) -> dict:
        """归一化 + 去重 + 入库一批事件。受保护路径绝不入图; 私密事件正文抹除。

        每个 event: {ts, source, kind, entity, summary, private?, meta?}。
        返回 {ok, ingested, deduped, protected_skipped, private_stored, by_source,
              nonprivate_entities}(后者供 writeback 用权威的非私密集合, 防双判定不一致)。"""
        ingested = deduped = protected_skipped = private_stored = 0
        touched: set = set()
        nonpriv_touched: set = set()
        by_source: dict = {}
        with self._conn() as c:
            for ev in (events or []):
                ent = str(ev.get("entity") or "").strip()
                if not ent:
                    continue
                meta = _sanitize_meta(ev.get("meta") or {})   # 截断值而非序列化串, 保证 JSON 永远合法
                path = str(meta.get("path") or "")
                # 受保护: entity 或 meta.path 任一命中 → 绝不入图(防 basename 绕过, 如 ~/xiaoshuai/g.py)
                if _protected(ent) or _protected(path):
                    protected_skipped += 1
                    continue
                src = str(ev.get("source") or "?")
                kind = str(ev.get("kind") or "event")
                tsd = _parse_ts(ev.get("ts")) or _now_dt()
                ts = _iso(tsd)
                priv = _event_is_private(ev, ent, path, src)
                summ = str(ev.get("summary") or "")
                if priv:
                    summ = _REDACT                  # 入库即抹正文(防泄露 + 防外泄)
                    private_stored += 1
                dk = _dedup(src, ts, kind, ent)
                try:
                    # dedup 命中时**绝不**沿用旧 private(冻结路径会让事后升私密的事件留着
                    # private=0 + 明文 summary, 经下游 timeline/before_after 泄云端)。
                    # 先探旧行: 不存在=新插入; 存在=去重命中, 但隐私只升不降——
                    # 旧行 private=0 且本次判私密 → 升 private=1 并把可能明文的 summary 抹成 _REDACT。
                    pre = c.execute(
                        "SELECT id,private FROM tm_events WHERE dedup=?", (dk,)).fetchone()
                    if pre is None:
                        c.execute(
                            "INSERT INTO tm_events"
                            "(ts,source,kind,entity,summary,private,meta_json,ingested_at,dedup) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (ts, src, kind, ent[:300], summ[:500], 1 if priv else 0,
                             json.dumps(meta, ensure_ascii=False), _now_iso(), dk))
                        ingested += 1
                        touched.add(ent)
                        if not priv:                 # 权威非私密集合(供 writeback, 单一判定来源)
                            nonpriv_touched.add(ent)
                        by_source[src] = by_source.get(src, 0) + 1
                    else:
                        deduped += 1
                        if priv and not bool(pre["private"]):   # 隐私单调升级 + 抹明文正文
                            c.execute(
                                "UPDATE tm_events SET private=1, summary=? WHERE id=?",
                                (_REDACT, pre["id"]))
                except sqlite3.Error:
                    continue
            self._set_meta(c, "last_ingest", _now_iso())
            if protected_skipped:
                self._bump_meta(c, "protected_skipped_total", protected_skipped)
        if build_edges and touched:
            self._build_edges(list(touched))
        return {"ok": True, "ingested": ingested, "deduped": deduped,
                "protected_skipped": protected_skipped, "private_stored": private_stored,
                "by_source": by_source,
                "nonprivate_entities": sorted(nonpriv_touched)}

    def _build_edges(self, entities: list) -> None:
        """为受影响实体重建/补建**事实性**时间边: 同实体相邻(precedes) + 截止前导(deadline_for)。

        只存时间先后这一事实; 因果是查询时的启发式推断(sequence_causality), 不固化进库。"""
        with self._conn() as c:
            for ent in entities:
                rows = c.execute(
                    "SELECT id,ts,kind FROM tm_events WHERE entity=? ORDER BY ts ASC, id ASC",
                    (ent,)).fetchall()
                evs = [(r["id"], _parse_ts(r["ts"]), r["kind"]) for r in rows]
                evs = [(i, t, k) for (i, t, k) in evs if t is not None]
                # 相邻 precedes
                for (ia, ta, _ka), (ib, tb, _kb) in zip(evs, evs[1:]):
                    c.execute(
                        "INSERT OR IGNORE INTO tm_edges(src,dst,rel,lag_secs,weight,basis,ts) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (ia, ib, "precedes", (tb - ta).total_seconds(), 1.0,
                         "same-entity time order", _now_iso()))
                # 截止前导: 更早事件 → 该实体的截止事件
                deadlines = [(i, t) for (i, t, k) in evs if k in DEADLINE_KINDS]
                for (di, dt_) in deadlines:
                    for (i, t, _k) in evs:
                        if i != di and t < dt_:
                            c.execute(
                                "INSERT OR IGNORE INTO tm_edges(src,dst,rel,lag_secs,weight,basis,ts) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (i, di, "deadline_for", (dt_ - t).total_seconds(), 0.8,
                                 "leads up to deadline", _now_iso()))

    # ---- 源拉取(各自 try/except → 失败返回 []; 绝不碰凭据文件) ---------- #
    def _pull_execlog(self, hours: float) -> list:
        out = []
        try:
            for r in self.timeline(limit=500):
                out.append({"ts": r.get("ts"), "source": "execlog", "kind": r.get("kind") or "action",
                            "entity": r.get("target") or r.get("kind") or "action",
                            "summary": (r.get("detail") or "")[:160], "private": False})
        except Exception:
            return []
        return out

    def _pull_files(self, hours: float) -> list:
        ft = _opt("file_tools")
        if ft is None:
            return []
        try:
            rows = ft.recent_files(hours=int(hours), limit=200)
        except Exception:
            return []
        out = []
        for r in (rows or []):
            path = r.get("path", "") or ""
            try:
                ts = _iso(datetime.fromtimestamp(os.path.getmtime(path)).astimezone())
            except Exception:
                ts = _now_iso()
            out.append({"ts": ts, "source": "files", "kind": "file_modified",
                        "entity": r.get("name") or path, "summary": r.get("business_tag") or "",
                        "private": bool(r.get("private")), "meta": {"path": path}})
        return out

    def _pull_todos(self, hours: float) -> list:
        ts_mod = _opt("todo_store")
        if ts_mod is None:
            return []
        try:
            rows = ts_mod.all_todos()
        except Exception:
            return []
        out = []
        for r in (rows or []):
            ent = (r.get("content") or "")[:60]
            if not ent:
                continue
            if r.get("ts_created"):
                out.append({"ts": r["ts_created"], "source": "todos", "kind": "todo_created",
                            "entity": ent, "summary": r.get("content") or "", "private": False})
            if r.get("due"):
                out.append({"ts": r["due"], "source": "todos", "kind": "todo_deadline",
                            "entity": ent, "summary": f"截止 {r.get('due')}", "private": False})
            if r.get("done") and r.get("ts_done"):
                out.append({"ts": r["ts_done"], "source": "todos", "kind": "todo_completed",
                            "entity": ent, "summary": "已完成", "private": False})
        return out

    def _pull_awareness(self, hours: float) -> list:
        aw = _opt("awareness_store") or _opt("eyes_store")
        if aw is None:
            return []
        try:
            rows = aw.recent_events(hours=float(hours), limit=300, include_private=True)
        except Exception:
            return []
        out = []
        for r in (rows or []):
            out.append({"ts": r.get("ts"), "source": "awareness", "kind": r.get("kind") or "screen",
                        "entity": r.get("app") or "screen", "summary": r.get("summary") or "",
                        "private": bool(r.get("private"))})
        return out

    def _pull_process(self, hours: float) -> list:
        path = os.path.expanduser("~/.jarvis/process_miner/events.jsonl")
        if not os.path.exists(path):
            return []
        cutoff = _now_dt() - timedelta(hours=float(hours))
        out = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    td = _parse_ts(rec.get("ts"))
                    if td is not None and td < cutoff:
                        continue
                    out.append({"ts": rec.get("ts"), "source": "process",
                                "kind": rec.get("kind") or "process",
                                "entity": rec.get("app") or rec.get("action") or "process",
                                "summary": rec.get("summary") or "", "private": bool(rec.get("private"))})
        except Exception:
            return out[-500:]
        return out[-500:]

    def _pull_autopilot(self, hours: float) -> list:
        ap = _opt("autopilot_store")
        if ap is None:
            return []
        out = []
        try:
            for r in ap.list_evidence(limit=300):
                if r.get("kind") not in ("action", "result", "decision"):
                    continue
                out.append({"ts": r.get("ts"), "source": "autopilot", "kind": "ap_" + str(r.get("kind")),
                            "entity": "goal:" + str(r.get("goal_id")), "summary": (r.get("content") or "")[:160],
                            "private": False})
        except Exception:
            pass
        try:
            for r in ap.list_approvals(status=None, limit=200):
                out.append({"ts": r.get("ts_created") or r.get("ts"), "source": "autopilot",
                            "kind": "approval_" + str(r.get("status") or "?"),
                            "entity": str(r.get("action_kind") or "approval"),
                            "summary": (r.get("action") or "")[:160], "private": False})
        except Exception:
            pass
        return out

    def _pull_nexus(self, hours: float) -> list:
        nx = _opt("nexus_store")
        if nx is None:
            return []
        try:
            rows = nx.read_inbox(limit=200)
        except Exception:
            return []
        out = []
        for r in (rows or []):
            out.append({"ts": r.get("ts") or r.get("ingested_at"), "source": "nexus",
                        "kind": "inbound_" + str(r.get("kind") or "msg"),
                        "entity": str(r.get("service") or "nexus"),
                        "summary": (r.get("summary") or "")[:160], "private": bool(r.get("private"))})
        return out

    def _pull_email(self, hours: float) -> list:
        """opt-in · 隐私源。只取元数据(发件域 + 收件时间), 不取正文/主题; 一律标私密。"""
        em = _opt("email_tools")
        if em is None:
            return []
        try:
            res = em.gmail_recent(max_results=30, with_snippet=False)
        except Exception:
            return []
        out = []
        for m in (res.get("messages") or []):
            frm = (m.get("from") or "")
            dom = frm.split("@")[-1].strip(" <>") if "@" in frm else (frm[:40] or "unknown")
            out.append({"ts": m.get("date"), "source": "email", "kind": "email_received",
                        "entity": "email:" + dom, "summary": "", "private": True})
        return out

    def _pull_calendar(self, hours: float) -> list:
        """opt-in · 隐私源。取日程标题/起止(供窗口/冲突判断), 标私密; summary 入库即抹。"""
        gt = _opt("google_tools")
        if gt is None:
            return []
        try:
            res = gt.calendar_agenda(days=max(1, int(float(hours) / 24) + 1), max_results=50)
        except Exception:
            return []
        out = []
        for e in (res.get("events") or []):
            title = (e.get("title") or "event")[:60]
            out.append({"ts": e.get("start"), "source": "calendar", "kind": "calendar_event",
                        "entity": "cal:" + title, "summary": title, "private": True,
                        "meta": {"end": e.get("end"), "location": e.get("location")}})
        return out

    def ingest(self, sources: Optional[list] = None, hours: Optional[float] = None,
               writeback: bool = True) -> dict:
        """从各源吸事件入时间因果图。默认仅纯本机安全源; email/calendar 须显式列出。"""
        hours = float(hours or _DEFAULT_INGEST_HOURS)
        if sources is None:
            srcs = list(_SAFE_SOURCES)
        else:
            srcs = [s for s in sources if s in _ALL_SOURCES]
        pullers = {
            "execlog": self._pull_execlog, "files": self._pull_files, "todos": self._pull_todos,
            "awareness": self._pull_awareness, "process": self._pull_process,
            "autopilot": self._pull_autopilot, "nexus": self._pull_nexus,
            "email": self._pull_email, "calendar": self._pull_calendar,
        }
        events: list = []
        src_errors: dict = {}
        for s in srcs:
            try:
                events.extend(pullers[s](hours) or [])
            except Exception as e:  # pragma: no cover — 源异常隔离
                src_errors[s] = str(e)[:100]
        res = self.ingest_events(events, build_edges=True)
        res["sources"] = srcs
        if src_errors:
            res["source_errors"] = src_errors
        # 回写: 仅非私密实体 → world_model; reasoning_core/goal_engine 缺则 deferred
        # 用 ingest_events 返回的**权威**非私密集合(已含文件名/路径模式升级), 防双判定不一致漏标。
        written, deferred = [], ["reasoning_core", "goal_engine"]
        if writeback and res.get("ingested"):
            uniq = res.get("nonprivate_entities") or []
            summ = (f"time_machine 吸入 {res['ingested']} 事件(源 {srcs}); "
                    f"活跃实体: " + ", ".join(uniq[:8]))
            wb = self._writeback(summ, uniq)
            written, deferred = wb["written_back"], wb["deferred"]
        res["written_back"] = written
        res["deferred"] = deferred
        res["note"] = "时间因果图已更新; 私密事件仅存本机且正文已抹, 跨云不出。"
        return res

    def _writeback(self, summary_text: str, entities: list) -> dict:
        """把(非私密)时间因果结果回写给 world_model; reasoning_core/goal_engine 缺则 deferred(不建桩)。"""
        written, deferred = [], []
        wm = _opt("world_model")
        if wm is None:
            deferred.append("world_model")
        else:
            try:
                r = wm.extract(text=summary_text[:1000], source="time_machine",
                               ref="ingest", use_llm=False, private=False)
                if isinstance(r, dict) and r.get("ok"):
                    written.append("world_model.extract")
            except Exception:
                deferred.append("world_model")
        payload = {"source": "time_machine", "summary": summary_text[:500],
                   "entities": list(entities)[:50]}
        for name, fn in (("reasoning_core", "record_temporal_causal"),
                         ("goal_engine", "record_temporal_fact")):
            mod = _opt(name)
            if mod is None or not hasattr(mod, fn):
                deferred.append(name)
                continue
            try:
                getattr(mod, fn)(payload)
                written.append(f"{name}.{fn}")
            except Exception:
                deferred.append(name)
        return {"written_back": written, "deferred": deferred}

    # ===================================================================== #
    # B) 时间因果引擎: 查询/推理动作
    # ===================================================================== #
    def _ev_dict(self, r: sqlite3.Row, with_meta: bool = False) -> dict:
        d = {"id": r["id"], "ts": r["ts"], "source": r["source"], "kind": r["kind"],
             "entity": r["entity"], "summary": r["summary"], "private": bool(r["private"])}
        if with_meta:
            try:
                d["meta"] = json.loads(r["meta_json"]) if r["meta_json"] else {}
            except Exception:
                d["meta"] = {}
        return d

    def _query_events(self, c, entity=None, since=None, until=None, source=None,
                      kind=None, limit=300, with_meta=False) -> list:
        q = "SELECT * FROM tm_events WHERE 1=1"
        p: list = []
        if entity:
            q += " AND entity=?"; p.append(entity)
        if source:
            q += " AND source=?"; p.append(source)
        if kind:
            q += " AND kind=?"; p.append(kind)
        if since:
            q += " AND ts>=?"; p.append(since)
        if until:
            q += " AND ts<=?"; p.append(until)
        q += " ORDER BY ts ASC, id ASC LIMIT ?"; p.append(int(limit))
        return [self._ev_dict(r, with_meta) for r in c.execute(q, p).fetchall()]

    def _collar(self, out: dict, nodes: list) -> dict:
        """隐私护栏: 结果里有任何私密事件 → 标 _private/_force_local, 由大脑切本机模型收尾。
        数据层隐私总开关(User 2026-07-08 放弃): 关(默认) → 恒不标记, 直接放行(本模块无对外出口;
        受保护路径仍由 _protected 独立守卫, 不靠此分类)。"""
        if not config.get("safety.data_layer_privacy_enabled", False):
            return out
        if any(n.get("private") for n in (nodes or [])):
            out["_private"] = True
            out["_force_local"] = True
        return out

    def timeline_graph(self, entity=None, since=None, until=None, source=None, limit=300) -> dict:
        with self._conn() as c:
            nodes = self._query_events(c, entity, since, until, source, None, limit)
            ids = [n["id"] for n in nodes]
            edges = []
            if ids:
                ph = ",".join("?" * len(ids))
                q = (f"SELECT src,dst,rel,lag_secs,weight FROM tm_edges "
                     f"WHERE src IN ({ph}) AND dst IN ({ph})")
                for r in c.execute(q, tuple(ids) + tuple(ids)):
                    edges.append({"from": r["src"], "to": r["dst"], "rel": r["rel"],
                                  "lag_h": round((r["lag_secs"] or 0) / 3600.0, 2),
                                  "weight": r["weight"]})
        out = {"ok": True,
               "window": {"entity": entity, "since": since, "until": until, "source": source},
               "nodes": nodes, "edges": edges,
               "counts": {"nodes": len(nodes), "edges": len(edges)}}
        return self._collar(out, nodes)

    def before_after(self, entity=None, event_id=None, ts=None, window_h=72.0) -> dict:
        window_h = float(window_h or 72.0)
        with self._conn() as c:
            anchor = None
            if event_id is not None:
                try:
                    r = c.execute("SELECT * FROM tm_events WHERE id=?", (int(event_id),)).fetchone()
                    anchor = self._ev_dict(r) if r else None
                except (ValueError, TypeError):
                    anchor = None
            anchor_ts = _parse_ts(anchor["ts"]) if anchor else _parse_ts(ts)
            if anchor_ts is None and entity:
                r = c.execute("SELECT * FROM tm_events WHERE entity=? ORDER BY ts DESC LIMIT 1",
                              (entity,)).fetchone()
                if r:
                    anchor = self._ev_dict(r); anchor_ts = _parse_ts(r["ts"])
            if anchor_ts is None:
                return {"ok": False, "error": "需 entity / event_id / ts 之一来定位锚点"}
            ent = entity or (anchor["entity"] if anchor else None)
            lo = _iso(anchor_ts - timedelta(hours=window_h))
            hi = _iso(anchor_ts + timedelta(hours=window_h))
            a_iso = _iso(anchor_ts)
            qb = ("SELECT * FROM tm_events WHERE ts>=? AND ts<? "
                  + ("AND entity=? " if ent else "") + "ORDER BY ts DESC LIMIT 50")
            qa = ("SELECT * FROM tm_events WHERE ts>? AND ts<=? "
                  + ("AND entity=? " if ent else "") + "ORDER BY ts ASC LIMIT 50")
            before = [self._ev_dict(r) for r in c.execute(
                qb, tuple([lo, a_iso] + ([ent] if ent else [])))]
            after = [self._ev_dict(r) for r in c.execute(
                qa, tuple([a_iso, hi] + ([ent] if ent else [])))]
        out = {"ok": True, "anchor": anchor or {"ts": _iso(anchor_ts), "entity": ent},
               "scope": ("entity" if ent else "global"),
               "before": before, "after": after,
               "note": "before=候选前因, after=候选后果; 时间先后≠因果, 仅供判断。"}
        return self._collar(out, [anchor or {}] + before + after)

    def event_lifecycle(self, entity: str) -> dict:
        entity = (entity or "").strip()
        if not entity:
            return {"ok": False, "error": "给一个 entity(待办/项目/文件/日程名)"}
        with self._conn() as c:
            rows = [self._ev_dict(r) for r in c.execute(
                "SELECT * FROM tm_events WHERE entity=? ORDER BY ts ASC, id ASC", (entity,))]
        if not rows:
            return {"ok": False, "error": f"无 '{entity}' 的事件; 先 ingest。"}
        stages = [{"ts": r["ts"], "kind": r["kind"], "source": r["source"], "summary": r["summary"]}
                  for r in rows]
        first, last = _parse_ts(rows[0]["ts"]), _parse_ts(rows[-1]["ts"])
        span = _hours(first, last)
        closed = any(r["kind"] in COMPLETION_KINDS for r in rows)
        now = _now_dt()
        overdue = any((r["kind"] in DEADLINE_KINDS and (_parse_ts(r["ts"]) or now) < now)
                      for r in rows) and not closed
        out = {"ok": True, "entity": entity, "event_count": len(rows),
               "first_ts": rows[0]["ts"], "last_ts": rows[-1]["ts"],
               "span_h": round(span, 2) if span is not None else None,
               "stages": stages,
               "status": "closed" if closed else ("overdue" if overdue else "open")}
        return self._collar(out, rows)

    def deadline_reason(self, entity=None, event_id=None) -> dict:
        with self._conn() as c:
            dl = None
            if event_id is not None:
                try:
                    r = c.execute("SELECT * FROM tm_events WHERE id=?", (int(event_id),)).fetchone()
                    dl = self._ev_dict(r) if r else None
                except (ValueError, TypeError):
                    dl = None
            elif entity:
                ph = ",".join("?" * len(DEADLINE_KINDS))
                now = _now_iso()
                r = c.execute(
                    f"SELECT * FROM tm_events WHERE entity=? AND kind IN ({ph}) AND ts>=? "
                    f"ORDER BY ts ASC LIMIT 1", (entity,) + DEADLINE_KINDS + (now,)).fetchone()
                if r is None:
                    r = c.execute(
                        f"SELECT * FROM tm_events WHERE entity=? AND kind IN ({ph}) "
                        f"ORDER BY ts DESC LIMIT 1", (entity,) + DEADLINE_KINDS).fetchone()
                dl = self._ev_dict(r) if r else None
            if not dl:
                return {"ok": False, "error": "没找到该实体的截止/日程事件"}
            dts = _parse_ts(dl["ts"]); now_dt = _now_dt()
            rem = _hours(now_dt, dts)
            overdue = (rem is not None and rem < 0)
            ph_c = ",".join("?" * len(COMPLETION_KINDS))
            done = c.execute(
                f"SELECT 1 FROM tm_events WHERE entity=? AND kind IN ({ph_c}) LIMIT 1",
                (dl["entity"],) + COMPLETION_KINDS).fetchone() is not None
            deps = [self._ev_dict(r) for r in c.execute(
                "SELECT * FROM tm_events WHERE entity=? AND ts<? ORDER BY ts DESC LIMIT 10",
                (dl["entity"], dl["ts"]))]
        out = {"ok": True, "target": dl["entity"], "deadline": dl["ts"], "kind": dl["kind"],
               "remaining_h": round(rem, 2) if rem is not None else None,
               "overdue": overdue, "completed": done,
               "urgency": _urgency(rem, done, overdue),
               "depends_on": [d["entity"] for d in deps[:5]],
               "why": _deadline_why(dl["entity"], rem, overdue, done)}
        return self._collar(out, [dl] + deps)

    def sequence_causality(self, entity=None, events=None, limit=50) -> dict:
        with self._conn() as c:
            if events:
                evs = []
                for i in events:
                    try:
                        r = c.execute("SELECT * FROM tm_events WHERE id=?", (int(i),)).fetchone()
                    except (ValueError, TypeError):
                        r = None
                    if r:
                        evs.append(self._ev_dict(r))
                evs.sort(key=lambda e: e["ts"] or "")
            elif entity:
                evs = [self._ev_dict(r) for r in c.execute(
                    "SELECT * FROM tm_events WHERE entity=? ORDER BY ts ASC, id ASC LIMIT ?",
                    (entity, int(limit)))]
            else:
                return {"ok": False, "error": "需 entity 或 events(id 列表)"}
        evs = [e for e in evs if _parse_ts(e["ts"])]
        chain = []
        for a, b in zip(evs, evs[1:]):
            lag = _hours(_parse_ts(a["ts"]), _parse_ts(b["ts"]))
            chain.append({"from_id": a["id"], "to_id": b["id"],
                          "from": f"{a['entity']}/{a['kind']}", "to": f"{b['entity']}/{b['kind']}",
                          "rel": "precedes", "lag_h": round(lag, 2) if lag is not None else None,
                          "causal_confidence": _causal_confidence(a, b, lag)})
        out = {"ok": True, "chain": chain, "count": len(chain),
               "note": "链为时间先后(事实); causal_confidence 为启发式推断, 时间相邻≠因果, 仅供参考。"}
        return self._collar(out, evs)

    def lag_analysis(self, from_kind=None, to_kind=None, from_entity=None, to_entity=None,
                     lookback_h=720.0) -> dict:
        lookback_h = float(lookback_h or 720.0)
        lo = _iso(_now_dt() - timedelta(hours=lookback_h))
        with self._conn() as c:
            froms = self._query_events(c, from_entity, lo, None, None, from_kind, 2000)
            tos = self._query_events(c, to_entity, lo, None, None, to_kind, 2000)
        tos_sorted = sorted([(_parse_ts(t["ts"]), t) for t in tos if _parse_ts(t["ts"])],
                            key=lambda x: x[0])
        # 纵深一致: 只要参与查询的事件里有私密项就切本机(不等配出样本才判, 与 _collar 同口径)
        # 数据层隐私总开关关(默认) → 恒 False, 不强制切本机(同 _collar 口径)。
        priv_any = bool(config.get("safety.data_layer_privacy_enabled", False)) and \
            any(e.get("private") for e in (froms + tos))
        lags = []
        for f in froms:
            ft = _parse_ts(f["ts"])
            if ft is None:
                continue
            cand = next((t for (tt, t) in tos_sorted if tt > ft and t["id"] != f["id"]), None)
            if cand is None:
                continue
            lag = _hours(ft, _parse_ts(cand["ts"]))
            if lag is not None:
                lags.append(lag)
        if not lags:
            return {"ok": True, "samples": 0, "from_kind": from_kind, "to_kind": to_kind,
                    "note": "样本不足, 无法估计滞后。"}
        lags.sort()
        import statistics
        n = len(lags)
        p90 = lags[min(n - 1, int(round(0.9 * (n - 1))))]
        out = {"ok": True, "from_kind": from_kind, "to_kind": to_kind,
               "samples": n, "median_h": round(statistics.median(lags), 2),
               "mean_h": round(statistics.fmean(lags), 2), "p90_h": round(p90, 2),
               "min_h": round(lags[0], 2), "max_h": round(lags[-1], 2),
               "note": "经验滞后(从某类事件到其后最近一类事件), 仅描述相关, 非因果。"}
        if priv_any:
            out["_private"] = True; out["_force_local"] = True
        return out

    def missed_window(self, lookback_h=720.0) -> dict:
        lookback_h = float(lookback_h or 720.0)
        now = _now_dt()
        lo = _iso(now - timedelta(hours=lookback_h))
        missed = []
        ph = ",".join("?" * len(DEADLINE_KINDS))
        ph_c = ",".join("?" * len(COMPLETION_KINDS))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM tm_events WHERE kind IN ({ph}) AND ts>=? AND ts<? "
                f"ORDER BY ts ASC", DEADLINE_KINDS + (lo, _iso(now))).fetchall()
            for r in rows:
                ent = r["entity"]
                # 完成即不算错过——含**提前完成**(ts_done < 截止)。与 deadline_reason 一致, 不加 ts 约束。
                done = c.execute(
                    f"SELECT 1 FROM tm_events WHERE entity=? AND kind IN ({ph_c}) LIMIT 1",
                    (ent,) + COMPLETION_KINDS).fetchone()
                if done:
                    continue
                dts = _parse_ts(r["ts"])
                overdue = _hours(dts, now) or 0.0
                missed.append({"entity": ent, "deadline": r["ts"], "kind": r["kind"],
                               "overdue_h": round(overdue, 1),
                               "consequence": _missed_consequence(r["kind"], overdue),
                               "private": bool(r["private"])})
        out = {"ok": True, "missed": missed, "count": len(missed),
               "note": "错过窗口 = 已过截止且无完成记录; consequence 为启发式后果提示。"}
        return self._collar(out, missed)

    def future_window(self, horizon_h=168.0, entity=None) -> dict:
        horizon_h = float(horizon_h or 168.0)
        now = _now_dt()
        hi = _iso(now + timedelta(hours=horizon_h))
        ph = ",".join("?" * len(DEADLINE_KINDS))
        windows = []
        with self._conn() as c:
            q = (f"SELECT * FROM tm_events WHERE kind IN ({ph}) AND ts>? AND ts<=? ")
            params = list(DEADLINE_KINDS) + [_iso(now), hi]
            if entity:
                q += "AND entity=? "; params.append(entity)
            q += "ORDER BY ts ASC"
            rows = [self._ev_dict(r) for r in c.execute(q, tuple(params))]
        for r in rows:
            dts = _parse_ts(r["ts"])
            if dts is None:
                continue
            lead = _typical_lead(r["kind"])
            rec_start = dts - timedelta(hours=lead)
            slack = _hours(now, rec_start)
            windows.append({"entity": r["entity"], "deadline": r["ts"], "kind": r["kind"],
                            "in_h": round(_hours(now, dts) or 0.0, 1),
                            "recommended_start": _iso(rec_start), "lead_h": lead,
                            "slack_h": round(slack, 1) if slack is not None else None,
                            "status": ("现在就该启动" if (slack is not None and slack <= 0) else "尚有缓冲"),
                            "private": r["private"]})
        out = {"ok": True, "windows": windows, "count": len(windows),
               "note": "最佳行动窗口 = 截止 − 该类事件默认提前量; slack<=0 表示已到该启动的时刻。"}
        return self._collar(out, rows)

    def temporal_conflict(self, horizon_h=168.0) -> dict:
        horizon_h = float(horizon_h or 168.0)
        now = _now_dt()
        hi = _iso(now + timedelta(hours=horizon_h))
        ph = ",".join("?" * len(DEADLINE_KINDS))
        with self._conn() as c:
            rows = [self._ev_dict(r, with_meta=True) for r in c.execute(
                f"SELECT * FROM tm_events WHERE kind IN ({ph}) AND ts>? AND ts<=? ORDER BY ts ASC",
                DEADLINE_KINDS + (_iso(now), hi))]
        intervals = []
        for e in rows:
            st = _parse_ts(e["ts"])
            if st is None:
                continue
            en = _parse_ts((e.get("meta") or {}).get("end")) or (st + timedelta(hours=1))
            intervals.append((st, en, e))
        conflicts = []
        for i in range(len(intervals)):
            for j in range(i + 1, len(intervals)):
                a_s, a_e, a_ev = intervals[i]
                b_s, b_e, b_ev = intervals[j]
                if a_s < b_e and b_s < a_e:   # 区间重叠
                    conflicts.append({"a": _safe_node(a_ev), "b": _safe_node(b_ev),
                                      "type": "时间重叠",
                                      "overlap_from": _iso(max(a_s, b_s)),
                                      "overlap_to": _iso(min(a_e, b_e))})
        # 截止挤堆: 任意 2h 窗内 >=3 个截止
        starts = sorted([iv[0] for iv in intervals])
        clusters = []
        for k in range(len(starts)):
            window = [t for t in starts if 0 <= (t - starts[k]).total_seconds() <= 7200]
            if len(window) >= 3:
                clusters.append({"around": _iso(starts[k]), "count": len(window)})
                break
        out = {"ok": True, "conflicts": conflicts, "count": len(conflicts),
               "clusters": clusters,
               "note": "重叠 = 两事件时间区间相交; 挤堆 = 2h 内 ≥3 个截止。"}
        return self._collar(out, rows)

    def explain_timing(self, entity=None, target=None) -> dict:
        """综合解释一件事的时机: 为什么现在该做 / 来龙去脉 / 错过后果 / 最佳窗口 / 冲突。"""
        ent = (entity or target or "").strip()
        if not ent:
            return {"ok": False, "error": "给一个 entity/target(待办/项目/文件/日程名)"}
        dr = self.deadline_reason(entity=ent)
        ba = self.before_after(entity=ent, window_h=168.0)
        lc = self.event_lifecycle(ent)
        fw = self.future_window(horizon_h=720.0, entity=ent)
        priv = any(x.get("_private") for x in (dr, ba, lc, fw))
        why_now = dr.get("why") if dr.get("ok") else "无明确截止信号; 依最近活动与依赖判断。"
        what_led = [(b.get("summary") or b.get("kind")) for b in (ba.get("before") or [])][:5] \
            if ba.get("ok") else []
        if_missed = None
        if dr.get("ok") and dr.get("deadline"):
            if_missed = _missed_consequence(dr.get("kind", ""),
                                            -(dr.get("remaining_h") or 0) if dr.get("overdue") else 0)
        best_window = (fw.get("windows") or [None])[0] if fw.get("ok") else None
        out = {"ok": True, "target": ent, "why_now": why_now,
               "what_led_here": what_led, "if_missed": if_missed,
               "best_window": best_window,
               "lifecycle_status": lc.get("status") if lc.get("ok") else None,
               "urgency": dr.get("urgency") if dr.get("ok") else None,
               "confidence_note": "时间因果为启发式综合, 非确定结论; 关键决策请人确认。"}
        if priv:
            out["_private"] = True; out["_force_local"] = True
        return out

    def audit(self) -> dict:
        with self._conn() as c:
            ev = c.execute("SELECT COUNT(*) FROM tm_events").fetchone()[0]
            edg = c.execute("SELECT COUNT(*) FROM tm_edges").fetchone()[0]
            priv = c.execute("SELECT COUNT(*) FROM tm_events WHERE private=1").fetchone()[0]
            by_source = {r[0]: r[1] for r in c.execute("SELECT source,COUNT(*) FROM tm_events GROUP BY source")}
            by_kind = {r[0]: r[1] for r in c.execute("SELECT kind,COUNT(*) FROM tm_events GROUP BY kind")}
            oldest = c.execute("SELECT MIN(ts) FROM tm_events").fetchone()[0]
            newest = c.execute("SELECT MAX(ts) FROM tm_events").fetchone()[0]
            dangling = c.execute(
                "SELECT COUNT(*) FROM tm_edges WHERE src NOT IN (SELECT id FROM tm_events) "
                "OR dst NOT IN (SELECT id FROM tm_events)").fetchone()[0]
            last_ingest = self._get_meta(c, "last_ingest")
            prot = self._get_meta(c, "protected_skipped_total") or "0"
        return {"ok": True, "events": ev, "edges": edg, "private_events": priv,
                "by_source": by_source, "by_kind": by_kind,
                "span": {"oldest": oldest, "newest": newest},
                "dangling_edges": dangling,
                "protected_skipped_total": int(prot) if str(prot).isdigit() else 0,
                "last_ingest": last_ingest, "integrations": self._integrations(),
                "action_log": self.stats()}

    def engine_status(self) -> dict:
        with self._conn() as c:
            ev = c.execute("SELECT COUNT(*) FROM tm_events").fetchone()[0]
            edg = c.execute("SELECT COUNT(*) FROM tm_edges").fetchone()[0]
            last = self._get_meta(c, "last_ingest")
        return {"ok": True, "db": self.db_path, "events": ev, "edges": edg,
                "last_ingest": last,
                "sources": {"safe": list(_SAFE_SOURCES), "optin": list(_OPTIN_SOURCES)},
                "integrations": self._integrations(), "action_log": self.stats()}

    def _integrations(self) -> dict:
        return {"world_model": _opt("world_model") is not None,
                "insight": _opt("insight") is not None,
                "filehub": _opt("filehub_tools") is not None,
                "reasoning_core": _opt("reasoning_core") is not None,    # 预期 False(待落地)
                "goal_engine": _opt("goal_engine") is not None}          # 预期 False(待落地)

    # ---- 统一调度(供 jarvis_brain 单一 umbrella 工具调用) -------------- #
    def dispatch(self, action: str = "status", args: Optional[dict] = None) -> dict:
        args = args or {}
        a = (action or "status").strip()
        try:
            if a == "ingest":
                return self.ingest(sources=args.get("sources"), hours=args.get("hours"),
                                   writeback=bool(args.get("writeback", True)))
            if a == "timeline_graph":
                return self.timeline_graph(entity=args.get("entity"), since=args.get("since"),
                                           until=args.get("until"), source=args.get("source"),
                                           limit=int(args.get("limit", 300) or 300))
            if a == "before_after":
                return self.before_after(entity=args.get("entity"),
                                         event_id=args.get("event_id") or args.get("id"),
                                         ts=args.get("ts"),
                                         window_h=float(args.get("window_h", 72) or 72))
            if a == "event_lifecycle":
                return self.event_lifecycle(args.get("entity") or args.get("target") or "")
            if a == "deadline_reason":
                return self.deadline_reason(entity=args.get("entity"),
                                            event_id=args.get("event_id") or args.get("id"))
            if a == "sequence_causality":
                return self.sequence_causality(entity=args.get("entity"), events=args.get("events"))
            if a == "lag_analysis":
                return self.lag_analysis(from_kind=args.get("from_kind"), to_kind=args.get("to_kind"),
                                         from_entity=args.get("from_entity"),
                                         to_entity=args.get("to_entity"),
                                         lookback_h=float(args.get("lookback_h", 720) or 720))
            if a == "missed_window":
                return self.missed_window(lookback_h=float(args.get("lookback_h", 720) or 720))
            if a == "future_window":
                return self.future_window(horizon_h=float(args.get("horizon_h", 168) or 168),
                                          entity=args.get("entity"))
            if a == "temporal_conflict":
                return self.temporal_conflict(horizon_h=float(args.get("horizon_h", 168) or 168))
            if a == "explain_timing":
                return self.explain_timing(entity=args.get("entity"), target=args.get("target"))
            if a == "audit":
                return self.audit()
            if a in ("status", "stats"):
                return self.engine_status()
            # legacy 只读直通(action-log)
            if a == "timeline":
                return {"ok": True, "timeline": self.timeline(
                    limit=int(args.get("limit", 30) or 30),
                    only_reversible=bool(args.get("only_reversible", False)))}
            return {"ok": False, "error": f"未知 action: {a}"}
        except Exception as e:  # pragma: no cover — 单动作隔离, 绝不掀翻主流程
            return {"ok": False, "error": f"time_machine {a} 出错: {e}"}


# --------------------------------------------------------------------------- #
# 模块级单例 + 便捷函数(供 local_powers / jarvis_brain / 其他脑模块调用)
# --------------------------------------------------------------------------- #
_DEFAULT: Optional[TimeMachine] = None


def tm() -> TimeMachine:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = TimeMachine()
    return _DEFAULT


# ---- A) 可逆行动日志(原有签名不变) -------------------------------------- #
def record(kind, target="", detail="", undo=None, reversible=False, source="jarvis") -> int:
    try:
        return tm().record(kind, target, detail, undo, reversible, source)
    except Exception:
        return -1  # 记录失败绝不影响主流程


def timeline(limit=50, kind=None, only_reversible=False) -> list:
    return tm().timeline(limit, kind, only_reversible)


def revert(aid: int) -> dict:
    return tm().revert(aid)


def stats() -> dict:
    return tm().stats()


# ---- B) 时间因果引擎(公开 API: 供 reasoning_core/world_model/goal_engine/insight/filehub 取用) -- #
def dispatch(action="status", args=None) -> dict:
    return tm().dispatch(action, args)


def ingest(sources=None, hours=None, writeback=True) -> dict:
    return tm().ingest(sources, hours, writeback)


def ingest_events(events, build_edges=True) -> dict:
    return tm().ingest_events(events, build_edges)


def timeline_graph(entity=None, since=None, until=None, source=None, limit=300) -> dict:
    return tm().timeline_graph(entity, since, until, source, limit)


def before_after(entity=None, event_id=None, ts=None, window_h=72.0) -> dict:
    return tm().before_after(entity, event_id, ts, window_h)


def event_lifecycle(entity) -> dict:
    return tm().event_lifecycle(entity)


def deadline_reason(entity=None, event_id=None) -> dict:
    return tm().deadline_reason(entity, event_id)


def sequence_causality(entity=None, events=None) -> dict:
    return tm().sequence_causality(entity, events)


def lag_analysis(from_kind=None, to_kind=None, from_entity=None, to_entity=None, lookback_h=720.0) -> dict:
    return tm().lag_analysis(from_kind, to_kind, from_entity, to_entity, lookback_h)


def missed_window(lookback_h=720.0) -> dict:
    return tm().missed_window(lookback_h)


def future_window(horizon_h=168.0, entity=None) -> dict:
    return tm().future_window(horizon_h, entity)


def temporal_conflict(horizon_h=168.0) -> dict:
    return tm().temporal_conflict(horizon_h)


def explain_timing(entity=None, target=None) -> dict:
    return tm().explain_timing(entity, target)


def audit() -> dict:
    return tm().audit()


def engine_status() -> dict:
    return tm().engine_status()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _emit(o) -> None:
    print(json.dumps(o, ensure_ascii=False, indent=2, default=str))


def _main():
    import argparse
    p = argparse.ArgumentParser(description="E4 时间机器 / 可逆行动日志 / 时间因果引擎")
    sub = p.add_subparsers(dest="cmd", required=True)
    # A) 行动日志
    pt = sub.add_parser("timeline"); pt.add_argument("-n", type=int, default=30)
    pt.add_argument("--reversible", action="store_true")
    pr = sub.add_parser("revert"); pr.add_argument("id", type=int)
    sub.add_parser("stats")
    # B) 时间因果引擎
    pi = sub.add_parser("ingest")
    pi.add_argument("--sources", default="", help="逗号分隔; 留空=默认安全源; 可加 email,calendar")
    pi.add_argument("--hours", type=float, default=None)
    pi.add_argument("--no-writeback", action="store_true")
    pg = sub.add_parser("graph")
    pg.add_argument("--entity", default=None); pg.add_argument("--source", default=None)
    pg.add_argument("-n", type=int, default=300)
    pba = sub.add_parser("before_after")
    pba.add_argument("--entity", default=None); pba.add_argument("--id", type=int, default=None)
    pba.add_argument("--ts", default=None); pba.add_argument("--window-h", type=float, default=72)
    plc = sub.add_parser("lifecycle"); plc.add_argument("entity")
    pdr = sub.add_parser("deadline_reason")
    pdr.add_argument("--entity", default=None); pdr.add_argument("--id", type=int, default=None)
    psc = sub.add_parser("sequence"); psc.add_argument("entity")
    pla = sub.add_parser("lag")
    pla.add_argument("--from-kind", default=None); pla.add_argument("--to-kind", default=None)
    pla.add_argument("--lookback-h", type=float, default=720)
    pmw = sub.add_parser("missed"); pmw.add_argument("--lookback-h", type=float, default=720)
    pfw = sub.add_parser("future"); pfw.add_argument("--horizon-h", type=float, default=168)
    pfw.add_argument("--entity", default=None)
    ptc = sub.add_parser("conflict"); ptc.add_argument("--horizon-h", type=float, default=168)
    pet = sub.add_parser("explain"); pet.add_argument("entity")
    sub.add_parser("audit")
    sub.add_parser("status")

    a = p.parse_args()
    t = tm()
    if a.cmd == "timeline":
        for r in t.timeline(a.n, only_reversible=a.reversible):
            flag = ("↩可撤销" if (r["reversible"] and not r["reverted"]) else
                    ("✓已撤销" if r["reverted"] else "—"))
            print(f"#{r['id']} {r['ts'][:19]} [{r['kind']}] {flag} {r['target']} {r['detail'][:50]}")
    elif a.cmd == "revert":
        _emit(t.revert(a.id))
    elif a.cmd == "stats":
        _emit(t.stats())
    elif a.cmd == "ingest":
        srcs = [s.strip() for s in a.sources.split(",") if s.strip()] or None
        _emit(t.ingest(sources=srcs, hours=a.hours, writeback=not a.no_writeback))
    elif a.cmd == "graph":
        _emit(t.timeline_graph(entity=a.entity, source=a.source, limit=a.n))
    elif a.cmd == "before_after":
        _emit(t.before_after(entity=a.entity, event_id=a.id, ts=a.ts, window_h=a.window_h))
    elif a.cmd == "lifecycle":
        _emit(t.event_lifecycle(a.entity))
    elif a.cmd == "deadline_reason":
        _emit(t.deadline_reason(entity=a.entity, event_id=a.id))
    elif a.cmd == "sequence":
        _emit(t.sequence_causality(entity=a.entity))
    elif a.cmd == "lag":
        _emit(t.lag_analysis(from_kind=a.from_kind, to_kind=a.to_kind, lookback_h=a.lookback_h))
    elif a.cmd == "missed":
        _emit(t.missed_window(lookback_h=a.lookback_h))
    elif a.cmd == "future":
        _emit(t.future_window(horizon_h=a.horizon_h, entity=a.entity))
    elif a.cmd == "conflict":
        _emit(t.temporal_conflict(horizon_h=a.horizon_h))
    elif a.cmd == "explain":
        _emit(t.explain_timing(entity=a.entity))
    elif a.cmd == "audit":
        _emit(t.audit())
    elif a.cmd == "status":
        _emit(t.engine_status())


if __name__ == "__main__":
    _main()
