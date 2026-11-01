#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""privacy_stream.py — 流数据 in-stream 隐私打标(共享底座③, 2026-06-05)。

背景(评审痛点): 现有 `_private` 是**文件路径制**(查 index.db private=1), 对**没有文件路径
的流数据**集体失效 —— GPS 坐标、剪贴板文本、通知中心正文(验证码/OTP)、摄像头帧描述、
蜂群黑板证据条目。这些会随 context_brain / 云端主脑外泄。

ears 已有成功先例: 用 `_force_local` 标志(按"来源"而非路径)强制本机收尾。本模块把这套
扩到"按内容形状"判定: 扫流文本里的敏感模式 → 命中即应强制本机(force_local), 绝不上云。

定位: **单一真相源**。device_mesh(定位/剪贴板/通知)、swarm 黑板、eyes OCR、任何"流入"
context_brain/记忆/云端的非文件数据, 都该先过 `should_force_local()` 打标, 而不是各写各的。

铁律:
  - 只读判定 + 打标, 绝不执行副作用, 绝不放宽任何现有隐私门。
  - 命中 → force_local=True(本机模型收尾, 正文不出本机)。fail-safe: 拿不准从严。
  - 即便 User 开了 allow_cloud_private, 高敏感类(OTP/凭证/定位)默认仍本机(可单独授权)。

全本机, 纯 stdlib。
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import config  # noqa: E402 — 纯读取
except Exception:  # pragma: no cover
    config = None


# --------------------------------------------------------------------------- #
# 敏感内容形状(按形状判定, 不靠来源标签 —— 流数据常没有可信来源标签)。
# 每条: (severity, name, regex)。severity: high = 即便 allow_cloud_private 也默认本机。
# --------------------------------------------------------------------------- #
_PATTERNS = [
    # —— 验证码 / OTP / 一次性口令(最高敏感: 拿到即可冒用)——
    ("high", "otp_verification",
     re.compile(r"(验证码|动态码|校验码|one[\s-]?time|verification\s+code|\bOTP\b|"
                r"安全码|短信码)[^\d]{0,8}\d{4,8}", re.I)),
    ("high", "otp_bare",
     re.compile(r"\b\d{6}\b(?=[^\d]{0,12}(验证|code|登录|login|有效|expire))", re.I)),
    # —— 凭证 / 密钥 / 密码 ——
    ("high", "credential",
     re.compile(r"(password|passwd|密码|口令|api[_\s-]?key|secret|token|私钥|"
                r"private[_\s-]?key|凭证|credential)\s*[:=：]\s*\S+", re.I)),
    ("high", "bearer_token",
     re.compile(r"\b(sk-[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}|"
                r"ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,})\b")),
    # —— 定位 / GPS 坐标(device_mesh location)——
    ("high", "gps_coords",
     re.compile(r"(纬度|经度|latitude|longitude|lat|lng|lon)\s*[:=：]?\s*[-+]?\d{1,3}\.\d{3,}", re.I)),
    ("high", "gps_pair",
     re.compile(r"[-+]?\d{1,2}\.\d{4,}\s*,\s*[-+]?\d{1,3}\.\d{4,}")),  # 30.2741,120.1551
    # —— 银行卡 / 支付 ——
    # 卡号上下文(卡号/card/银行…)在数字前或后都可能, 故分两步: 有上下文关键词 + 一长串数字(13-19位, 可空格分隔)。
    ("high", "credit_card",
     re.compile(r"(卡号|银行卡|card\s*(no|number)?|信用卡|借记卡)[^\d]{0,6}(?:\d[ -]?){13,19}", re.I)),
    ("high", "credit_card_bare",
     re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{3,7}\b")),  # 4-4-4-N 分组卡号形状
    ("high", "cvv",
     re.compile(r"\bcvv\b\s*[:=：]?\s*\d{3,4}", re.I)),
    # —— 身份证 / 护照 ——
    ("high", "cn_id",
     re.compile(r"\b\d{17}[\dXx]\b")),  # 18 位身份证
    ("high", "passport",
     re.compile(r"(护照|passport)\s*[:#：]?\s*[A-Za-z0-9]{6,9}", re.I)),
    # —— 手机号 / 联系方式(中等: 个人可识别)——
    ("med", "cn_phone",
     re.compile(r"\b1[3-9]\d{9}\b")),
    ("med", "email_addr",
     re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # —— 银行 / 金额上下文(中等)——
    ("med", "bank_ctx",
     re.compile(r"(银行卡|账户余额|account\s+balance|工资|薪资|salary|存款)", re.I)),
]


def _enabled() -> bool:
    """流隐私打标总开关。默认开(最安全); User 可关但需显式。"""
    if config is None:
        return True
    try:
        return bool(config.get("privacy.stream_guard_enabled", True))
    except Exception:  # pragma: no cover
        return True


def scan(text: str) -> list[dict]:
    """扫文本里的敏感模式, 返回命中列表 [{severity, name}]。只读, 不抛。"""
    if not _enabled() or not text:
        return []
    hits = []
    seen = set()
    for severity, name, rx in _PATTERNS:
        if name in seen:
            continue
        try:
            if rx.search(text):
                hits.append({"severity": severity, "name": name})
                seen.add(name)
        except Exception:  # pragma: no cover - 防御
            continue
    return hits


def should_force_local(text: str, *, allow_cloud_private: bool | None = None) -> dict:
    """判断一段流文本是否必须强制本机收尾(不上云)。

    allow_cloud_private: User 是否已授权"私密内容可上云"。None=读 config。
      即便授权, **high 敏感类(OTP/凭证/定位/卡号/身份证)默认仍强制本机** —— 这些泄露后果
      不可逆, 需 User 对"高敏感上云"单独授权(privacy.allow_cloud_high, 默认 False)。

    返回 {"force_local": bool, "private": bool, "hits": [...], "reason": str}。
    与 ears 的契约一致: force_local=True ⇒ 调用方应把 _private/_force_local 标上, 走本机模型。
    """
    hits = scan(text)
    if not hits:
        return {"force_local": False, "private": False, "hits": [], "reason": ""}

    if allow_cloud_private is None and config is not None:
        try:
            allow_cloud_private = bool(config.get("safety.allow_cloud_private", False))
        except Exception:  # pragma: no cover
            allow_cloud_private = False
    allow_cloud_private = bool(allow_cloud_private)

    has_high = any(h["severity"] == "high" for h in hits)
    allow_high = False
    if config is not None:
        try:
            allow_high = bool(config.get("privacy.allow_cloud_high", False))
        except Exception:  # pragma: no cover
            allow_high = False

    # high 敏感: 除非 User 显式开 allow_cloud_high, 否则一律本机(无视 allow_cloud_private)。
    if has_high and not allow_high:
        names = ",".join(h["name"] for h in hits if h["severity"] == "high")
        return {"force_local": True, "private": True, "hits": hits,
                "reason": f"高敏感内容({names})强制本机, 不上云"}
    # med 敏感: 跟随 allow_cloud_private —— 没授权就本机, 授权了可上云。
    if not allow_cloud_private:
        names = ",".join(h["name"] for h in hits)
        return {"force_local": True, "private": True, "hits": hits,
                "reason": f"私密内容({names}), 未授权上云 → 本机"}
    return {"force_local": False, "private": True, "hits": hits,
            "reason": "私密但 User 已授权上云"}


def mark(result: dict, text: str, *, allow_cloud_private: bool | None = None) -> dict:
    """便捷打标: 就地给一个结果 dict 标上 _private/_force_local(命中时)。返回同一 dict。
    给 device_mesh / 黑板 ingest / context_brain 流入口用 —— 一行接入。"""
    v = should_force_local(text, allow_cloud_private=allow_cloud_private)
    if v["force_local"]:
        if isinstance(result, dict):
            result["_private"] = True
            result["_force_local"] = True
            result.setdefault("_privacy_reason", v["reason"])
    return result


# 应进 config._READONLY_KEYS 的键(User-only): 关流隐私 / 允许高敏感上云 都很危险。
READONLY_KEYS = ("privacy.stream_guard_enabled", "privacy.allow_cloud_high")


if __name__ == "__main__":
    import json
    demo = {
        "otp": should_force_local("你的验证码是 384751,5分钟内有效"),
        "gps": should_force_local("当前位置 纬度:30.2741 经度:120.1551"),
        "phone": should_force_local("联系电话 13800138000"),
        "clean": should_force_local("今天天气不错, 适合散步"),
    }
    print(json.dumps(demo, ensure_ascii=False, indent=2))
