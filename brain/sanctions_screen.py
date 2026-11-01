#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sanctions_screen — OFAC/制裁清单筛查(Pythia/Osiris feed 目录内化: OFAC SDN)。

把公开的制裁名单(OpenSanctions 免密钥 simple.csv: 美国 OFAC SDN + OFAC 非 SDN 合并
+ 欧盟 FSF + 联合国安理会)拉进本地 SQLite(~/.jarvis/sanctions.db), 向对话脑/报价脑/
payment_guard 提供**只读**筛查工具:

  sanctions_screen(action=check, name=...)  — 拿客户/银行/收货人名字比对制裁库,
      给命中项 + 相似度 + 制裁项目 + 判定(HIT/REVIEW/CLEAR)。发货或收汇前先筛。
  sanctions_screen(action=status)           — 制裁库健康(各数据集条数/同步时间/库路径)。
  sanctions_screen(action=datasets)         — 当前配置的数据集清单 + 上游地址。

核心价值: 外贸对 USD 计价、走美国清算体系, 客户/银行一旦在 OFAC SDN 上, 交易会被
冻结/罚没。本模块让小美在报价/收汇/发货前一键筛名, 而不是等银行退单才发现。

数据源(反空壳铁律, 真实公开清单):
  OpenSanctions latest simple.csv —— 免密钥、去重、带别名(aliases)/国别/schema/项目。
  https://data.opensanctions.org/datasets/latest/<dataset>/targets.simple.csv
  默认数据集: us_ofac_sdn, us_ofac_cons, eu_fsf, un_sc_sanctions

铁律:
  - refresh(联网重建库) 是写动作 → 进 _CLI_ONLY, LLM 工具面只读, dispatch 硬拦。
  - 筛查输入是客户身份 → 所有 check 返回标 _private=True(隐私只升不降)。
  - 脱敏顺序: 本地正则 _redact 必先于任何库调用; 输出前统一过 _safe_text。
  - 无网/库空: 优雅降级, 返回 loaded=False + 提示 CLI refresh, 绝不假装 CLEAR。
  - int/float 外部强转用 math.isfinite 守卫; 相似度阈值判定用方向性区间。
  - 纯 stdlib(urllib.request), Python 3.9+ 兼容; refresh 逐数据集 try/except 不整挂。

架构: approved_tools / is_tool / dispatch 三件套 + _CLI_ONLY 硬拦 + AST _selftest。
"""
from __future__ import annotations

import ast
import csv
import io
import json
import math
import re
import sqlite3
import sys
import time
import unicodedata
import unittest
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 路径 ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import config  # noqa: E402 — 纯读取, 无副作用

_DEFAULT_DB = Path.home() / ".jarvis" / "sanctions.db"

# ── 上游数据集(OpenSanctions 免密钥 simple.csv) ─────────────────────────────
_DATASET_URL = "https://data.opensanctions.org/datasets/latest/{}/targets.simple.csv"
_DEFAULT_DATASETS: Tuple[str, ...] = (
    "us_ofac_sdn",    # 美国 OFAC 特别指定国民(SDN)—— 对 USD 结算最关键
    "us_ofac_cons",   # 美国 OFAC 非 SDN 合并清单
    "eu_fsf",         # 欧盟金融制裁名单
    "un_sc_sanctions",  # 联合国安理会制裁
)
_HTTP_TIMEOUT = 30
_UA = "jarvis-sanctions-screen/1.0 (+local; keyless)"

# ── schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sanctions_entities (
    entity_id   TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    name_norm   TEXT NOT NULL DEFAULT '',
    aliases     TEXT NOT NULL DEFAULT '',
    aliases_norm TEXT NOT NULL DEFAULT '',
    schema      TEXT NOT NULL DEFAULT '',
    countries   TEXT NOT NULL DEFAULT '',
    program     TEXT NOT NULL DEFAULT '',
    dataset     TEXT NOT NULL DEFAULT '',
    synced_at   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (dataset, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_name_norm ON sanctions_entities(name_norm);
CREATE TABLE IF NOT EXISTS sanctions_meta (
    dataset   TEXT PRIMARY KEY,
    n_rows    INTEGER NOT NULL DEFAULT 0,
    source    TEXT NOT NULL DEFAULT '',
    synced_at TEXT NOT NULL DEFAULT ''
);
"""

# ── 隐私/安全工具(与 freight_advisor / deal_advisor 同源) ────────────────────
_SECRET_RX = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:AKIA|ASIA|AROA|AGPA|AIPA|ANPA|ANVA|AIDA)[A-Z0-9]{16}"
    r"|ghp_[A-Za-z0-9]{36,}"
    r"|sk-[A-Za-z0-9]{32,}"
    r"|Bearer\s+[A-Za-z0-9\-._~+/]{20,}",
    re.IGNORECASE,
)
_HIENTROPY_RX = re.compile(
    r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/_\-]*[A-Z])(?=[A-Za-z0-9+/_\-]*[a-z])"
    r"(?=[A-Za-z0-9+/_\-]*\d)[A-Za-z0-9+/_\-]{24,512}(?![A-Za-z0-9+/])"
)


def _redact(text: str) -> str:
    """脱敏: 凭据/令牌先抹。顺序铁律: 本地正则必须先于任何库调用。
    数据层隐私总开关(User 2026-07-08 放弃): 关(默认)→ 返原文, 不脱敏(本模块无对外出口,
    数据层不阉割; 制裁清单是公开数据, 出口另有 L3 守卫)。开 → 完整脱敏(可逆)。"""
    s = str(text or "")
    if not s:
        return ""
    if not config.get("safety.data_layer_privacy_enabled", False):
        return s
    s = _SECRET_RX.sub("[凭证已脱敏]", s)
    s = re.sub(
        r"(?i)(?<![A-Za-z0-9])[A-Za-z0-9]*"
        r"(?:pass(?:word)?|pwd|token|secret|api[_\-]?key|access[_\-]?key|"
        r"private[_\-]?key|client[_\-]?secret|refresh[_\-]?token)"
        r"[A-Za-z0-9_\-]*\s*[:=：]\s*\S+",
        "[凭证已脱敏]", s,
    )
    s = _HIENTROPY_RX.sub("[令牌已脱敏]", s)
    return s


def _safe_text(text: str, limit: int = 240) -> str:
    s = _redact(str(text or ""))
    return s if len(s) <= limit else s[:limit] + "…"


def _flag_private(out: Any) -> Any:
    """统一标 _private=True(隐私只升不降)。筛查触及客户身份, 一律私密。"""
    if isinstance(out, dict):
        out["_private"] = True
    return out


# ── 名称归一 ─────────────────────────────────────────────────────────────────
_STRIP_RX = re.compile(r"[^0-9A-Z一-鿿 ]+")
_WS_RX = re.compile(r"\s+")
# 商业实体后缀噪音(归一时剔除, 提升匹配召回)
_ENTITY_NOISE = frozenset({
    "CO", "LTD", "LIMITED", "INC", "INCORPORATED", "LLC", "PLC", "CORP",
    "CORPORATION", "COMPANY", "GMBH", "SA", "AG", "BV", "PTE", "PVT",
    "GROUP", "HOLDING", "HOLDINGS", "INTERNATIONAL", "TRADING", "THE",
})


def _norm_name(s: str) -> str:
    """归一: 去重音 → 大写 → 去标点(保留中日韩/字母数字/空格) → 折叠空格。"""
    s = str(s or "")
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    s = _STRIP_RX.sub(" ", s)
    s = _WS_RX.sub(" ", s).strip()
    return s


def _sig_tokens(norm: str) -> List[str]:
    """归一名的显著 token(去掉商业后缀噪音 + 单字符)。"""
    return [t for t in norm.split(" ") if len(t) >= 2 and t not in _ENTITY_NOISE]


def _ratio(a: str, b: str) -> float:
    """difflib 相似度(0~1); 空串保护。"""
    if not a or not b:
        return 0.0
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()


# ── DB ───────────────────────────────────────────────────────────────────────
def _conn(db: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(db) if db else _DEFAULT_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p), timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ── refresh(CLI-only 写动作: 联网重建库) ─────────────────────────────────────
def _fetch_csv(dataset: str) -> str:
    url = _DATASET_URL.format(dataset)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310 (https only)
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def refresh(datasets: Optional[List[str]] = None, db: Optional[Path] = None) -> dict:
    """[CLI-only] 从 OpenSanctions 拉各数据集 simple.csv, 重建本地筛查库。

    逐数据集独立 try/except: 某个 404/超时只跳过, 其余照常。返回每个数据集的落库条数。
    """
    ds = list(datasets) if datasets else list(_DEFAULT_DATASETS)
    conn = _conn(db)
    per: Dict[str, Any] = {}
    total = 0
    try:
        for name in ds:
            try:
                text = _fetch_csv(name)
            except Exception as e:  # noqa: BLE001
                per[name] = {"ok": False, "error": _safe_text(str(e), 160)}
                continue
            rows = _parse_simple_csv(text, name)
            with conn:
                conn.execute("DELETE FROM sanctions_entities WHERE dataset=?", (name,))
                conn.executemany(
                    "INSERT OR REPLACE INTO sanctions_entities"
                    " (entity_id,name,name_norm,aliases,aliases_norm,schema,countries,program,dataset,synced_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.execute(
                    "INSERT OR REPLACE INTO sanctions_meta (dataset,n_rows,source,synced_at)"
                    " VALUES (?,?,?,?)",
                    (name, len(rows), _DATASET_URL.format(name), _now()),
                )
            per[name] = {"ok": True, "rows": len(rows)}
            total += len(rows)
    finally:
        conn.close()
    return {"ok": total > 0, "module": "sanctions_screen", "total_rows": total,
            "datasets": per, "db": str(db or _DEFAULT_DB)}


def _parse_simple_csv(text: str, dataset: str) -> List[Tuple]:
    """解析 OpenSanctions simple.csv → 落库行元组。列: id,schema,name,aliases,...,program_ids。"""
    out: List[Tuple] = []
    reader = csv.DictReader(io.StringIO(text))
    stamp = _now()
    for r in reader:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        aliases = (r.get("aliases") or "").strip()
        alias_norm = " ; ".join(_norm_name(a) for a in aliases.split(";") if a.strip())
        out.append((
            (r.get("id") or "").strip(),
            name,
            _norm_name(name),
            aliases,
            alias_norm,
            (r.get("schema") or "").strip(),
            (r.get("countries") or "").strip(),
            (r.get("sanctions") or r.get("program_ids") or "").strip(),
            dataset,
            stamp,
        ))
    return out


# ── screen(只读筛查) ─────────────────────────────────────────────────────────
_HIT_THRESHOLD = 0.92
_REVIEW_THRESHOLD = 0.82


def screen(name: str, threshold: float = _REVIEW_THRESHOLD,
           limit: int = 8, db: Optional[Path] = None) -> dict:
    """拿名字比对制裁库。返回命中项 + 相似度 + 判定。所有返回标 _private。"""
    q = str(name or "").strip()
    if not q:
        return _flag_private({"ok": False, "error": "缺筛查名(name)"})
    try:
        thr = float(threshold)
        if not math.isfinite(thr):
            thr = _REVIEW_THRESHOLD
    except (TypeError, ValueError):
        thr = _REVIEW_THRESHOLD
    thr = min(max(thr, 0.5), 0.99)
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = 8
    lim = min(max(lim, 1), 25)

    qn = _norm_name(q)
    qtok = set(_sig_tokens(qn))
    if not qn:
        return _flag_private({"ok": False, "error": "筛查名归一后为空"})

    conn = _conn(db)
    try:
        meta = conn.execute("SELECT COUNT(*) AS n FROM sanctions_entities").fetchone()
        loaded = int(meta["n"]) if meta else 0
        if loaded == 0:
            return _flag_private({
                "ok": True, "loaded": False, "verdict": "UNKNOWN",
                "query": _safe_text(q, 120),
                "note": "制裁库未加载, 无法判定。请先 CLI: python3 brain/sanctions_screen.py refresh",
            })
        # 候选召回: 任一显著 token 作子串命中 name_norm 或 aliases_norm。
        cands = _recall(conn, qn, qtok)
    finally:
        conn.close()

    scored: List[dict] = []
    for row in cands:
        nn = row["name_norm"]
        best = _ratio(qn, nn)
        mtype = "name"
        # 别名逐条比
        for al in (row["aliases_norm"] or "").split(" ; "):
            al = al.strip()
            if not al:
                continue
            rr = _ratio(qn, al)
            if rr > best:
                best, mtype = rr, "alias"
        # token 全包含(查询是库中实体的子集或反之)→ 提分, 抓"A公司" ⊂ "A国际贸易公司"
        etok = set(_sig_tokens(nn))
        if qtok and etok and (qtok <= etok or etok <= qtok):
            best = max(best, 0.90)
            if mtype == "name":
                mtype = "token-subset"
        if best >= thr:
            scored.append({
                "name": _safe_text(row["name"], 160),
                "score": round(best, 3),
                "match": mtype,
                "schema": row["schema"],
                "countries": row["countries"],
                "program": _safe_text(row["program"], 120),
                "dataset": row["dataset"],
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    scored = scored[:lim]
    top = scored[0]["score"] if scored else 0.0
    if top >= _HIT_THRESHOLD:
        verdict = "HIT"
    elif scored:
        verdict = "REVIEW"
    else:
        verdict = "CLEAR"
    return _flag_private({
        "ok": True, "loaded": True, "verdict": verdict,
        "query": _safe_text(q, 120), "matched": len(scored),
        "matches": scored,
        "note": {
            "HIT": "强匹配制裁名单 —— 高危, 交易前务必人工核实身份(生日/国别/证件)后再决定。",
            "REVIEW": "存在近似匹配 —— 需人工复核是否同一主体, 别直接放行。",
            "CLEAR": "未匹配到制裁名单(仅限已加载数据集; 不构成合规免责)。",
        }[verdict],
    })


def _recall(conn: sqlite3.Connection, qn: str, qtok: set, cap: int = 4000) -> List[sqlite3.Row]:
    """SQL 子串召回候选: 显著 token 命中 name_norm/aliases_norm; 无 token 时退化整名。"""
    toks = [t for t in qtok if len(t) >= 3] or [qn]
    toks = toks[:6]
    where = " OR ".join(["name_norm LIKE ? OR aliases_norm LIKE ?"] * len(toks))
    params: List[Any] = []
    for t in toks:
        params.extend(["%" + t + "%", "%" + t + "%"])
    sql = ("SELECT name,name_norm,aliases_norm,schema,countries,program,dataset"
           " FROM sanctions_entities WHERE " + where + " LIMIT ?")
    params.append(cap)
    return conn.execute(sql, params).fetchall()


def status(db: Optional[Path] = None) -> dict:
    """制裁库健康: 各数据集条数/同步时间/库路径。非私密(无客户数据)。"""
    p = Path(db) if db else _DEFAULT_DB
    if not p.exists():
        return {"ok": True, "loaded": False, "module": "sanctions_screen",
                "db": str(p), "total_rows": 0, "datasets": [],
                "note": "制裁库未建。CLI: python3 brain/sanctions_screen.py refresh"}
    conn = _conn(p)
    try:
        rows = conn.execute(
            "SELECT dataset,n_rows,synced_at FROM sanctions_meta ORDER BY dataset").fetchall()
        total = conn.execute("SELECT COUNT(*) AS n FROM sanctions_entities").fetchone()["n"]
    finally:
        conn.close()
    return {
        "ok": True, "loaded": total > 0, "module": "sanctions_screen",
        "db": str(p), "total_rows": int(total),
        "datasets": [{"dataset": r["dataset"], "rows": r["n_rows"],
                      "synced_at": r["synced_at"]} for r in rows],
    }


def datasets() -> dict:
    """当前配置的数据集清单 + 上游地址。"""
    return {
        "ok": True, "module": "sanctions_screen",
        "source": "OpenSanctions (免密钥 simple.csv)",
        "datasets": [{"dataset": d, "url": _DATASET_URL.format(d)} for d in _DEFAULT_DATASETS],
        "note": "CLI refresh 重建库; refresh 是写动作不经 LLM。",
    }


# ── _CLI_ONLY 硬拦集合(action 名) ────────────────────────────────────────────
_CLI_ONLY: frozenset = frozenset({"refresh"})


# ── 三件套 ────────────────────────────────────────────────────────────────────
def approved_tools() -> List[dict]:
    """单只读工具(action 内部路由)。refresh 绝不出现在此处。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "sanctions_screen",
                "description": (
                    "OFAC/制裁名单筛查(本地 OpenSanctions 库)。发货/收汇/报价前, 拿客户、"
                    "银行、收货人或中间商的名字比对美国 OFAC SDN + 欧盟 + 联合国制裁清单, "
                    "给命中项+相似度+判定(HIT高危/REVIEW待核/CLEAR未匹配)。"
                    "action=check 需 name; status 查库健康; datasets 看数据集。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["check", "status", "datasets"],
                                   "description": "check=筛名 / status=库健康 / datasets=数据集", "default": "check"},
                        "name": {"type": "string", "description": "要筛查的实体名(公司/个人/银行)"},
                        "threshold": {"type": "number",
                                      "description": "相似度阈值 0.5~0.99(默认 0.82, 越高越严)"},
                        "limit": {"type": "integer", "description": "最多返回命中数(默认 8)"},
                    },
                    "required": ["action"],
                },
            },
        },
    ]


def is_tool(name: str) -> bool:
    return name == "sanctions_screen"


def dispatch(name: str, args: dict) -> dict:
    if name in _CLI_ONLY:
        return {"ok": False, "error": f"动作 '{name}' 仅限 CLI 调用，不允许通过 LLM 工具接口执行"}
    if not is_tool(name):
        return {"ok": False, "error": f"未知工具: {name}"}
    args = args if isinstance(args, dict) else {}
    action = str(args.get("action", "check")).strip().lower()
    try:
        if action in _CLI_ONLY:
            return {"ok": False, "error": f"动作 '{action}' 仅限 CLI(联网重建库), 不经 LLM 执行"}
        if action == "check":
            return screen(name=str(args.get("name", "")),
                          threshold=args.get("threshold", _REVIEW_THRESHOLD),
                          limit=args.get("limit", 8))
        if action == "status":
            return status()
        if action == "datasets":
            return datasets()
        return {"ok": False, "error": f"未知 action: {_safe_text(action, 40)}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"dispatch 异常: {_safe_text(str(e), 200)}"}


# ── AST 自检(禁 subprocess/os.system/eval/write_file 等) ─────────────────────
_FORBIDDEN_EXEC_MODULES = frozenset({"subprocess", "pty", "commands", "popen2"})
_FORBIDDEN_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})
_FORBIDDEN_DOTTED = frozenset({
    "os.system", "os.popen", "os.spawnl", "os.spawnv", "os.execv", "os.execl",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
    "subprocess.check_output", "subprocess.check_call", "subprocess.getoutput",
})
_FORBIDDEN_ATTRS = frozenset({"run_shell", "write_file", "applescript"})


def _dotted_name(func: ast.Attribute) -> str:
    parts: List[str] = []
    cur: ast.AST = func
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _selftest_ast() -> List[str]:
    fails: List[str] = []
    src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _FORBIDDEN_EXEC_MODULES:
                    fails.append("forbidden import: " + alias.name)
        if isinstance(node, ast.ImportFrom):
            if (node.module or "") in _FORBIDDEN_EXEC_MODULES:
                fails.append("forbidden from-import: " + str(node.module))
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_BUILTINS:
                fails.append("forbidden builtin: " + node.func.id)
            if isinstance(node.func, ast.Attribute):
                dn = _dotted_name(node.func)
                if dn in _FORBIDDEN_DOTTED:
                    fails.append("forbidden call: " + dn)
                if node.func.attr in _FORBIDDEN_ATTRS:
                    fails.append("forbidden attr call: " + node.func.attr)
    return fails


def _selftest() -> int:
    # selftest 验证的是隐私契约本身(_redact 在开关打开时应完整脱敏这条可逆路径),
    # 与运行时默认值(safety.data_layer_privacy_enabled=False, 2026-07-08 User 放弃数据层隐私)
    # 是两回事: 契约只在开关 ON 时成立。这里只在 selftest 期间强制开关为 True,
    # 用 try/finally 恢复原 config.get, 避免污染同进程内其它测试(pytest/unittest 共进程运行)。
    _orig_config_get = config.get

    def _forced_config_get(key: str, default: Any = None) -> Any:
        if key == "safety.data_layer_privacy_enabled":
            return True
        return _orig_config_get(key, default)

    config.get = _forced_config_get
    try:
        return _selftest_body()
    finally:
        config.get = _orig_config_get


def _selftest_body() -> int:
    fails: List[str] = []

    def check(cond: bool, msg: str) -> None:
        if not cond:
            fails.append(msg)

    # 1) AST: 无危险调用
    for f in _selftest_ast():
        fails.append("AST: " + f)

    # 2) 归一/token
    check(_norm_name("Bank Mellī  Iran, PLC.") == "BANK MELLI IRAN PLC", "归一应去重音/标点/折叠(保留后缀词, 后缀由token层剔)")
    check("MELLI" in _sig_tokens(_norm_name("Bank Melli Iran")), "显著token应含MELLI")
    check(_norm_name("") == "", "空名归一为空")

    # 3) 脱敏
    check("[凭证已脱敏]" in _redact("api_key=SECRET123456"), "_redact 应抹 api_key")

    # 4) 种子库 → 筛查行为(临时库, 不联网)
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "s.db"
    conn = _conn(tmp)
    with conn:
        seed = [
            ("id1", "Bank Melli Iran", _norm_name("Bank Melli Iran"), "Bank Melli;BMI",
             " ; ".join([_norm_name("Bank Melli"), _norm_name("BMI")]),
             "Organization", "ir", "IRAN", "us_ofac_sdn", _now()),
            ("id2", "Islamic Revolutionary Guard Corps", _norm_name("Islamic Revolutionary Guard Corps"),
             "IRGC", _norm_name("IRGC"), "Organization", "ir", "SDGT", "us_ofac_sdn", _now()),
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO sanctions_entities"
            " (entity_id,name,name_norm,aliases,aliases_norm,schema,countries,program,dataset,synced_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)", seed)
        conn.execute("INSERT OR REPLACE INTO sanctions_meta VALUES (?,?,?,?)",
                     ("us_ofac_sdn", 2, "seed", _now()))
    conn.close()

    hit = screen("Bank Melli Iran", db=tmp)
    check(hit.get("verdict") == "HIT", "精确名应判 HIT")
    check(hit.get("_private") is True, "check 返回应标 _private")
    check(bool(hit.get("matches")) and hit["matches"][0]["score"] >= _HIT_THRESHOLD, "HIT 分应 ≥ 阈值")

    alias = screen("IRGC", db=tmp)
    check(alias.get("verdict") in ("HIT", "REVIEW"), "别名 IRGC 应命中")

    clean = screen("Shenzhen Happy Trading Co Ltd", db=tmp)
    check(clean.get("verdict") == "CLEAR", "无关名应判 CLEAR")
    check(clean.get("_private") is True, "CLEAR 也应标 _private")

    empty_db = Path(tempfile.mkdtemp()) / "empty.db"
    unk = screen("Anyone", db=empty_db)
    check(unk.get("verdict") == "UNKNOWN" and unk.get("loaded") is False, "空库应判 UNKNOWN 不假装 CLEAR")

    # 5) 三件套 + 安全闸
    specs = approved_tools()
    check(len(specs) == 1 and specs[0]["function"]["name"] == "sanctions_screen", "应只暴露 1 只读工具")
    check(is_tool("sanctions_screen") and not is_tool("refresh"), "refresh 不应是 LLM 工具")
    check(not dispatch("refresh", {}).get("ok"), "dispatch refresh 应被硬拦")
    check(not dispatch("sanctions_screen", {"action": "refresh"}).get("ok"), "action=refresh 应被硬拦")
    st = dispatch("sanctions_screen", {"action": "status"})
    check(isinstance(st, dict), "status 应返回 dict")
    ds = dispatch("sanctions_screen", {"action": "datasets"})
    check(bool(ds.get("ok")) and bool(ds.get("datasets")), "datasets 应返回配置")

    if fails:
        print("sanctions_screen selftest FAIL:")
        for f in fails:
            print("  - " + f)
        return 1
    print("sanctions_screen selftest: OK")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    args = sys.argv[1:]
    act = args[0] if args else "status"
    if act == "selftest":
        sys.exit(_selftest())
    elif act == "refresh":
        ds = args[1].split(",") if len(args) > 1 else None
        print(json.dumps(refresh(ds), ensure_ascii=False, indent=2))
    elif act == "check":
        nm = args[1] if len(args) > 1 else ""
        print(json.dumps(screen(nm), ensure_ascii=False, indent=2))
    elif act == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif act == "datasets":
        print(json.dumps(datasets(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"ok": False, "error": "未知动作: " + act,
                          "usage": "selftest|refresh [ds,ds]|check <name>|status|datasets"},
                         ensure_ascii=False))


# ── 单元测试(co-located, 可 python3 -m pytest / 直接跑) ──────────────────────
class _SanctionsTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        conn = _conn(self.db)
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO sanctions_entities"
                " (entity_id,name,name_norm,aliases,aliases_norm,schema,countries,program,dataset,synced_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                [("x1", "Rosneft Oil Company", _norm_name("Rosneft Oil Company"),
                  "Rosneft;NK Rosneft", " ; ".join([_norm_name("Rosneft"), _norm_name("NK Rosneft")]),
                  "Organization", "ru", "UKRAINE-EO13662", "us_ofac_sdn", _now())])
            conn.execute("INSERT OR REPLACE INTO sanctions_meta VALUES (?,?,?,?)",
                         ("us_ofac_sdn", 1, "seed", _now()))
        conn.close()

    def test_hit_exact(self) -> None:
        r = screen("Rosneft Oil Company", db=self.db)
        self.assertEqual(r["verdict"], "HIT")
        self.assertTrue(r["_private"])

    def test_partial_review_or_hit(self) -> None:
        r = screen("Rosneft", db=self.db)
        self.assertIn(r["verdict"], ("HIT", "REVIEW"))
        self.assertGreaterEqual(r["matched"], 1)

    def test_clear(self) -> None:
        r = screen("Acme Materials Corp", db=self.db)
        self.assertEqual(r["verdict"], "CLEAR")

    def test_empty_query(self) -> None:
        self.assertFalse(screen("", db=self.db)["ok"])

    def test_cli_only_blocked(self) -> None:
        self.assertFalse(dispatch("refresh", {}).get("ok"))
        self.assertFalse(dispatch("sanctions_screen", {"action": "refresh"}).get("ok"))

    def test_kit_shape(self) -> None:
        self.assertTrue(is_tool("sanctions_screen"))
        self.assertEqual(len(approved_tools()), 1)
        self.assertIsInstance(dispatch("sanctions_screen", {"action": "status"}), dict)

    def test_selftest_green(self) -> None:
        self.assertEqual(_selftest(), 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        sys.argv = [sys.argv[0]]
        unittest.main()
    else:
        main()
