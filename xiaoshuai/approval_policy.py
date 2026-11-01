#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小帅行动审批策略 —— User 2026-06-06 授权: 付钱/删资料留 User 手动, 其余小帅自动批。

定位: 小帅是独立监军(第二人)。小美各引擎把有副作用的步骤 enqueue_approval 进
autopilot 审批队列后, 原本全部等 User 手动 `jarvis autopilot approve` —— 本模块让
小帅替 User 消化其中的低危项, 高危项(见下)永远留给 User。

铁律(与小帅既有铁律同源):
  · 不 import 小美脑 —— 批准走 CLI 边界 `<python> bin/jarvis autopilot approve <id>`
    (子进程), 复用 autopilot 全部门: 原子认领防并发 / critical 二次 token 门 / 执行链。
    小帅**永不带 --confirm token** → critical(不可逆/物理/对外/付费)天然批不动。
  · 队列只读经 autopilot_store(薄 sqlite 层, 无脑依赖); 绝不裸改 DB 状态。
  · 永不放行(HOLD 给 User):
      1. 付钱类(付款/转账/订阅/checkout/billing...)         —— User 红线
      2. 删资料类(rm/删除/清空/rmtree/废纸篓/DROP TABLE...) —— User 红线
      3. 对外发送类(发邮件/发帖/群发/nexus_send...)          —— 影响他人(全局红线 4)
      4. 护栏/监军篡改 + 数据外传(config.toml/xiaoshuai//scp/curl 上传) + sudo
      5. risk=critical 一律不试
      6. classify.kind=red_line 一律 HOLD —— 唯一例外: 装包类(pip/npm/brew install,
         User 全局工作模式本就默认 YES 装包)
  · 每条决策落审计 ~/.jarvis/xiaoshuai/auto_approvals.jsonl(自动批+首见 HOLD 都记)。
  · 总开关 config `xiaoshuai.auto_approve_actions`(默认 False; 锁 _READONLY_KEYS,
    小美 model 经 config_set 改不动, 只 User 手改)。
"""
from __future__ import annotations
import os
import re
import sys
import json
import subprocess
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_BRAIN = os.path.join(_REPO, "brain")
if _BRAIN not in sys.path:
    sys.path.insert(0, _BRAIN)

import config  # noqa: E402  (brain, 与 xiaoshuai_daemon 同款)

JARVIS_BIN = os.path.join(_REPO, "bin", "jarvis")
# 每条 approve 即执行(hive 步骤可能真跑活), 给足超时; 单轮上限防一次清太猛。
APPROVE_TIMEOUT = int(config.get("xiaoshuai.approve_timeout", 600))
MAX_PER_DRAIN = int(config.get("xiaoshuai.approve_max_per_drain", 12))

# ---------------------------------------------------------------------------
# HOLD 词表(宁可误拦不可误放; 文本 = action + why + payload JSON 拼接)
# ---------------------------------------------------------------------------
_HOLD_RULES = [
    # 1. 付钱
    (r"付款|支付|转账|汇款|下单|结算|订阅|充值|缴费|打钱|"
     r"\bpayment\b|\bcheckout\b|/charge|\bcharge\b|\bbilling\b|subscri|"
     r"\bwire\b|\btransfer\b|\bpay\b|\bpurchase\b|\bbuy\b",
     "付钱类 → User 手动"),
    # 2. 删资料(含废纸篓铁律: 废纸篓永久保存只许 User 清)
    (r"\brm\s|删除|删掉|删库|清空|清除|抹掉|擦除|"
     r"\bunlink\b|rmtree|\brmdir\b|\bshred\b|\bsrm\b|\btrash\b|废纸篓|\.Trash|empty trash|"
     r"DROP\s+TABLE|TRUNCATE|DELETE\s+FROM|格式化|\bmkfs\b|diskutil\s+(erase|reformat)|"
     r"\bfind\b[\s\S]*-delete|\bwipe\b|\bdd\s+.*of=/dev/",
     "删资料类 → User 手动"),
    # 3. 对外发送(影响 User 之外的人)
    (r"发邮件|发送邮件|寄邮件|发给客户|回复客户|群发|发帖|发布到|对外发送|发短信|"
     r"nexus_send|\bsend\b[\s\S]{0,20}\b(mail|email|sms|message to)\b|\btweet\b|\bpost to\b",
     "对外发送(影响他人) → User 手动"),
    # 4. 护栏/监军篡改 + 数据外传 + 提权
    (r"xiaoshuai/|config\.toml|guard_enabled|_READONLY|local_powers|"
     r"\bscp\s+[^\n]*@|\brsync\b[^\n]*@\S+:|"
     r"\bcurl\b[^\n]*(-F\b|--form|-T\b|--upload-file|--data-binary)|"
     r"(^|\s)sudo\s",
     "护栏/监军/外传/提权 → User 手动"),
]
_HOLD_RX = [(re.compile(p, re.IGNORECASE), why) for p, why in _HOLD_RULES]

# 红线类唯一放行例外: 装包(User 全局工作模式默认 YES 装包)
_INSTALL_RX = re.compile(
    r"\b(pip[0-9.]*|pip3|npm|pnpm|yarn|gem|cargo|brew)\s+(install|add|i)\b|装包",
    re.IGNORECASE)


def _audit_path(base: str | None = None) -> str:
    b = base or os.path.expanduser(config.get("xiaoshuai.dir", "~/.jarvis/xiaoshuai"))
    os.makedirs(b, exist_ok=True)
    return os.path.join(b, "auto_approvals.jsonl")


def _audit(rec: dict, base: str | None = None) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), **rec}
    try:
        with open(_audit_path(base), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def classify(ap: dict) -> tuple[str, str]:
    """对一条 pending 审批分类 → ("auto"|"hold", 理由)。宁可误拦不可误放。"""
    # clarify-before-execute 未就绪项不是“执行失败”，而是要等信息补齐。
    # 在调 approve CLI 之前 HOLD，否则 CLI 会正确 fail-closed 但仍保持
    # pending，daemon 下一轮又重试，形成 30 秒死循环。缺字段保持旧语义。
    if "ready_to_execute" in ap:
        ready = ap.get("ready_to_execute")
        ready_ok = ready in (1, True)
        if isinstance(ready, str):
            ready_ok = ready.strip() in ("1", "true", "True", "yes")
        if not ready_ok:
            return "hold", "未就绪(clarify-before-execute) → 补齐信息后再批"
    risk = str(ap.get("risk") or "").lower()
    if risk == "critical":
        return "hold", "risk=critical(不可逆/物理/对外/付费)"
    payload = ap.get("payload") or {}
    text = " ".join([
        str(ap.get("action") or ""),
        str(ap.get("why") or ""),
        json.dumps(payload, ensure_ascii=False)[:2000],
    ])
    for rx, why in _HOLD_RX:
        if rx.search(text):
            return "hold", why
    kind = str(((payload.get("classify") or {}) if isinstance(payload, dict) else {})
               .get("kind") or "")
    if kind == "red_line":
        if _INSTALL_RX.search(text):
            return "auto", "红线-装包类(User 全局默认 YES 装包)"
        return "hold", "red_line 红线类"
    return "auto", f"非付钱/非删资料/非对外(kind={kind or '-'} risk={risk or '-'})"


def _approve_via_cli(approval_id: int) -> tuple[bool, str]:
    """走 CLI 边界批准(复用 autopilot 原子认领 + critical token 门 + 执行链)。"""
    cmd = [sys.executable, JARVIS_BIN, "autopilot", "approve", str(approval_id)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=APPROVE_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and "needs_confirm_token" not in out
        return ok, out[-500:]
    except subprocess.TimeoutExpired:
        return False, f"approve #{approval_id} 超时({APPROVE_TIMEOUT}s), CLI 已被杀"
    except Exception as e:  # noqa: BLE001
        return False, f"approve #{approval_id} 异常: {e}"


# 进程内已上报过的 HOLD(避免每轮重复刷审计/通知; 重启后重报一次可接受)
_seen_hold: set = set()


def drain(notifier=None, base: str | None = None, store=None, approve_fn=None) -> dict:
    """消化一轮 pending 审批。store/approve_fn 可注入(测试用)。"""
    if not config.get("xiaoshuai.auto_approve_actions", False):
        return {"enabled": False, "auto": 0, "hold": 0, "fail": 0}
    if store is None:
        import autopilot_store as store  # noqa: PLC0415  (薄 sqlite 层, 只读队列)
    approve_fn = approve_fn or _approve_via_cli

    stats = {"enabled": True, "auto": 0, "hold": 0, "fail": 0, "held_ids": [], "auto_ids": []}
    pend = store.list_approvals("pending", limit=200)
    for ap in sorted(pend, key=lambda a: a.get("id") or 0):  # 老的先批
        aid = ap.get("id")
        verdict, why = classify(ap)
        if verdict == "hold":
            stats["hold"] += 1
            stats["held_ids"].append(aid)
            if aid not in _seen_hold:
                _seen_hold.add(aid)
                _audit({"id": aid, "verdict": "hold", "rule": why,
                        "action": str(ap.get("action") or "")[:120]}, base)
            continue
        if stats["auto"] >= MAX_PER_DRAIN:
            break
        ok, snippet = approve_fn(aid)
        stats["auto" if ok else "fail"] += 1
        if ok:
            stats["auto_ids"].append(aid)
        _audit({"id": aid, "verdict": "auto_approve", "rule": why, "exec_ok": ok,
                "action": str(ap.get("action") or "")[:120],
                "result": snippet[:200]}, base)
    if stats["auto"] or stats["fail"]:
        msg = (f"自动批 {stats['auto']} 条"
               + (f", 失败 {stats['fail']}" if stats["fail"] else "")
               + (f", 留 User {stats['hold']} 条" if stats["hold"] else ""))
        print(f"[小帅·审批] {msg}", flush=True)
        if notifier is not None:
            try:
                notifier.fyi("小帅代批", msg)
            except Exception:  # noqa: BLE001
                pass
    return stats
