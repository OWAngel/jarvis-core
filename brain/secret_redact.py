#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""secret_redact.py — 权威单一脱敏出口(Hermes v0.18 借鉴 B5 #12/#13/#14/#16)。

背景: jarvis 47 个模块各自内联了 45 个**各不相同**的 `_redact`(4~39 行), 覆盖强弱不一,
最弱的(wechat_search/rag_search 4 行)恰在最该脱敏的不可信内容读取器上。本模块把各模块
脱敏正则**取并集**(只加不减)+ 补齐新场景, 做成单一权威源, 供弱模块委托升级。

脱敏顺序铁律(order of operations, 穿透 30+ 模块 docstring 的血泪教训):
  ① NFKC 归一化 —— 最先, 防全角/兼容字符混淆绕过后续所有正则
  ② key=value 赋值脱敏 —— 在【原文】上、在调库前跑; 否则先遮裸关键词会让"锚点"消失、值残留
  ③ 库 best-effort 叠加(awareness_sensors.redact, 软导入) —— 只加一层, 绝不首发权威
  ④ 裸令牌 / 凭据路径 / PEM —— 本地正则永远再跑, 不被库返回值短路
  ⑤ 高熵长串兜底 —— 抓未知形态密钥(纯统计特征: 同时含大小写+数字, ≥24)
  ⑥ 长串数字(卡号/账号/电话) —— 排最后, 误伤范围最大, 让更精确的凭据类先跑完
  ⑦ 邮箱掩码 ***@domain —— 保留域名利于业务实体识别

fail-safe 铁律(零例外): 宁可误报(整段占位), 绝不漏报(因异常放行明文)。
  - redact(): 任何异常 → 返回整段占位 _REDACTED, 绝不返回原文。
  - classify_private()/is_secret_path(): 本地永远先跑; 库只能加正例、其 False 不短路本地;
    库不可达且文本非空 → True(当私密)。

并集覆盖(取各模块最强版): AWS AKIA/STS ASIA·AROA / GitHub gh[posru]_ 全系列 / OpenAI sk-·sk-proj-
/ Slack xox / Google AIza / JWT eyJ / Bearer / key=value(含中文密码·口令·密钥·凭证·令牌, 长别名
优先, 无 \\b 用前后界防下划线陷阱) / 高熵(base64 全字母表 +/= 防 AWS secret 含 / 漏切) /
长数字(含中点·分隔 + 信用卡分组 + 大陆手机号) / PEM / 邮箱掩码。纯 stdlib, 3.9 兼容。
"""
from __future__ import annotations

import re
import unicodedata

# 分类占位符(保留类型信息, 利于审计/下游二次判断)
_PH_CRED = "[凭证已脱敏]"
_PH_TOKEN = "[令牌已脱敏]"
_PH_ENTROPY = "[高熵串已脱敏]"
_PH_DIGIT = "[数字已脱敏]"
_REDACTED = "〔已脱敏〕"          # 异常兜底: 整段占位

# ── A1 凭据文件路径 / basename ───────────────────────────────────────────────── #
_SECRET_PATH_RX = re.compile(
    r"\.gmail_creds\.json|\.gcal_creds\.json|\.feishu_creds\.json|\.localkey|"
    r"/\.jarvis/config\.toml|\.pem(?![A-Za-z0-9])|(?:^|/)id_rsa\b|(?:^|/)\.ssh/|"
    r"\.aws/credentials|\.git-credentials\b|\.netrc\b|\.npmrc\b|\.pypirc\b",
    re.IGNORECASE)
_SECRET_BASENAMES = frozenset({
    ".gmail_creds.json", ".gcal_creds.json", ".feishu_creds.json",
    ".localkey", "config.toml", "credentials", ".git-credentials",
    "id_rsa", ".netrc", ".npmrc", ".pypirc", "authorized_keys",
})

# ── A2 现代裸令牌(全前缀并集; 大小写敏感前缀, 不用 IGNORECASE 防误伤) ─────────── #
_BARE_TOKEN_RX = re.compile(
    r"(?<![A-Za-z0-9])A(?:KIA|SIA|ROA)[0-9A-Z]{12,}(?![A-Za-z0-9])|"     # AWS + STS 临时
    r"(?<![A-Za-z0-9])gh[posru]_[A-Za-z0-9]{20,}|"                        # GitHub p/o/s/r/u 全系
    r"(?<![A-Za-z0-9])sk-(?:proj-)?[A-Za-z0-9]{16,}|"                     # OpenAI(含 sk-proj-)
    r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}|"                      # Slack
    r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{20,60}|"                          # Google API key
    r"(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")  # JWT 三段
_BEARER_RX = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE)

# ── A3 key=value 赋值型密钥(长别名优先; 前后界用 lookaround 非 \b, 防下划线陷阱) ── #
_KV_SECRET_RX = re.compile(
    r"(?i)("
    r"(?<![A-Za-z0-9])"
    r"(?:pass(?:word)?|passwd|pwd|"
    r"secret[_-]?access[_-]?key|client[_-]?secret|access[_-]?key|api[_-]?key|"
    r"private[_-]?key|refresh[_-]?token|secret|token|"
    r"credential|authorization|cookie|session[_-]?id|bearer|"
    r"密码|口令|密钥|凭证|令牌)"
    r"(?![A-Za-z0-9])\s*[:=：]\s*)"
    r"(\S{1,512})")

# ── A4 高熵通用兜底(24-512 有界防 ReDoS; base64 全字母表含 +/= 防 AWS secret 漏切) ─ #
_HIENTROPY_RX = re.compile(
    r"(?<![A-Za-z0-9+/=])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}[A-Z])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}[a-z])"
    r"(?=[A-Za-z0-9_\-+/=]{0,512}\d)"
    r"[A-Za-z0-9_\-+/=]{24,512}(?![A-Za-z0-9+/=])")

# ── A5 长串数字(卡号/账号/电话); 含中点·分隔 + 信用卡分组 + 大陆手机号 ─────────── #
_CREDITCARD_RX = re.compile(r"(?<!\d)(?:\d{4}[ \-]?){3}\d{4}(?!\d)")
_PHONE_CN_RX = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_LONGNUM_RX = re.compile(r"\d[\d .·\-]{9,40}\d")

# ── A6 邮箱(有界防 ReDoS; 掩码 ***@domain 保留域名) ──────────────────────────── #
_EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,24}")

# ── A8 PEM 私钥块(有界 40 覆盖 ENCRYPTED/RSA 等最长变体) ──────────────────────── #
_PEM_RX = re.compile(r"-----BEGIN[A-Z ]{0,40}PRIVATE KEY-----|\bBEGIN [A-Z ]{0,40}PRIVATE KEY\b",
                     re.IGNORECASE)

# ── A7 私密关键词分类(business + personal; 只判布尔, 不替换) ───────────────────── #
_PRIVATE_RX = re.compile(
    r"银行|工资|薪资|薪水|月薪|年薪|年终奖|身份证|社保|公积金|存款|余额|卡号|账号|账户|密码|"
    r"验证码|转账|汇款|报销|病历|诊断|体检|处方|医保|住院|离婚|分手|贷款|按揭|抵押|"
    r"保密|机密|内部消息|收购|并购|裁员|股权|期权|融资|估值|offer|"
    r"报价|底价|成本价|利润|回扣|佣金|返点|客户名单|采购价|进价|"
    r"password|salary|ssn|\bbank\b|\bpin\b|medical|confidential|\bnda\b|"
    r"secret|token|api[_-]?key|client[_-]?secret|"
    r"\d[\d \-]{9,}\d",
    re.IGNORECASE)

# 供旧模块迁移期对照/单测复用(而非各自重定义)
SECRET_PATH_RX = _SECRET_PATH_RX
BARE_TOKEN_RX = _BARE_TOKEN_RX
KV_SECRET_RX = _KV_SECRET_RX
HIENTROPY_RX = _HIENTROPY_RX
EMAIL_RX = _EMAIL_RX
PRIVATE_RX = _PRIVATE_RX
PEM_RX = _PEM_RX


def _lib():
    """软导入 awareness_sensors 母版(best-effort 叠加层)。缺失/失败 → None。"""
    try:
        import awareness_sensors as _aw  # type: ignore
        return _aw
    except Exception:
        return None


def looks_entropic(s: str) -> bool:
    """高熵候选二次复核: 同时含大写+小写+数字三态才算(过滤长单词/URL 误伤)。"""
    if not s or len(s) < 24:
        return False
    return (any(c.isupper() for c in s) and any(c.islower() for c in s)
            and any(c.isdigit() for c in s))


def _mask_email(m: "re.Match") -> str:
    addr = m.group(0)
    parts = addr.partition("@")
    return f"***@{parts[2]}" if parts[1] and parts[2] else "[邮箱已脱敏]"


def _redact_credentials(s: str, use_lib: bool = True) -> str:
    """只脱凭据类(KV / PEM / 路径 / 裸令牌 / 高熵), 不碰数字/邮箱。供 redact_error 复用。

    use_lib=False 时跳过库层 —— redact_error 用它, 因为库层会连长数字(行号/地址)一起脱,
    抹掉排障必要信息。"""
    s = _KV_SECRET_RX.sub(lambda m: m.group(1) + _PH_CRED, s)   # ② key=value(值替换, 保留键名)
    if use_lib:
        aw = _lib()                                             # ③ 库 best-effort(可能连数字一起脱)
        if aw is not None:
            try:
                s = aw.redact(s)
            except Exception:
                pass
    s = _PEM_RX.sub(_PH_CRED, s)                                 # ④ PEM / 路径 / 裸令牌
    s = _SECRET_PATH_RX.sub(_PH_CRED, s)
    s = _BARE_TOKEN_RX.sub(_PH_TOKEN, s)
    s = _BEARER_RX.sub(_PH_TOKEN, s)
    s = _HIENTROPY_RX.sub(_PH_ENTROPY, s)                        # ⑤ 高熵兜底
    return s


def redact(text) -> str:
    """权威脱敏出口。按顺序铁律执行; 任何异常 → 整段占位 _REDACTED(绝不返回原文)。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return _REDACTED
    if not text:
        return text
    try:
        s = unicodedata.normalize("NFKC", text)                 # ① NFKC
        s = _redact_credentials(s)                              # ②③④⑤
        s = _CREDITCARD_RX.sub(_PH_DIGIT, s)                    # ⑥ 长数字(卡号→电话→通用)
        s = _PHONE_CN_RX.sub(_PH_DIGIT, s)
        s = _LONGNUM_RX.sub(_PH_DIGIT, s)
        s = _EMAIL_RX.sub(_mask_email, s)                       # ⑦ 邮箱掩码
        return s
    except Exception:
        return _REDACTED


def redact_error(text) -> str:
    """给工具报错出口(#16)用: 只脱凭据类, 保留数字/文件名/行号等排障必要信息。
    异常 → 整段占位。"""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return _REDACTED
    if not text:
        return text
    try:
        # use_lib=False: 只脱凭据类, 保留数字/行号/地址等排障信息
        return _redact_credentials(unicodedata.normalize("NFKC", text), use_lib=False)
    except Exception:
        return _REDACTED


def is_secret_path(path: str) -> bool:
    """双层判定: 本地 basename 精确集 + 路径正则永远先跑; 库只能加正例; 任一异常 → True。"""
    if not path:
        return False
    try:
        import os as _os
        base = _os.path.basename(path)
        if base in _SECRET_BASENAMES:
            return True
        if _SECRET_PATH_RX.search(path) or _BARE_TOKEN_RX.search(path):
            return True
    except Exception:
        return True   # fail-safe: 判定型异常 → 当机密
    aw = _lib()
    if aw is not None:
        try:
            if aw.is_secret_path(path):
                return True
        except Exception:
            return True
    return False


def classify_private(text: str = "", app: str = "", window: str = "") -> bool:
    """本地私密并集永远先跑(权威); 库只能加正例(其 False 不短路本地); 任一评估异常 → True。

    与旧「弱本地」模块的差异: 那些模块本地判定弱、靠库兜底, 故「库不可达 → 一律 True」。
    本模块的**本地并集已是强权威分类器**, 库缺席时信任本地结果(非私密就返 False), 保持确定性
    与可用性; fail-safe True 只用于本地/库评估**抛异常**的真失败(over-report 安全方向)。"""
    blob = " ".join(x for x in (text, app, window) if x)
    if not blob:
        return False
    try:
        if _PRIVATE_RX.search(blob) or _KV_SECRET_RX.search(blob) or _BARE_TOKEN_RX.search(blob):
            return True
    except Exception:
        return True   # 本地正则异常 → fail-safe True
    aw = _lib()
    if aw is not None:
        try:
            if aw.classify_private(text, app, window):
                return True
        except Exception:
            return True   # 库调用异常 → fail-safe True
    return False   # 本地未命中 + (库不在 或 库也判非私密) → 非私密(信任强本地并集)


def _redacted_marker(s: str) -> bool:
    """结果里出现任一脱敏标记(本模块分类占位符 或 库的 [已脱敏]/〔已脱敏〕)。"""
    return any(mk in s for mk in ("[凭证已脱敏]", "[令牌已脱敏]", "[高熵串已脱敏]",
                                  "[数字已脱敏]", "[邮箱已脱敏]", "[已脱敏]", "〔已脱敏〕", "***@"))


def _selftest() -> None:
    # 铁律断言口径: 秘密**值**必须消失 + 出现脱敏标记(具体占位符可能被库层改写, 不锁死)。
    def _gone(secret, text):
        out = redact(text)
        assert secret not in out, f"秘密未脱敏: {secret!r} 仍在 {out!r}"
        assert _redacted_marker(out), f"无脱敏标记: {out!r}"
        return out

    _gone("sk-abcdefghijklmnop1234", "api_key=sk-abcdefghijklmnop1234")
    _gone("hunter2secretval", "password: hunter2secretval")
    _gone("ghp_" + "a" * 30, "token is ghp_" + "a" * 30)
    _gone("AKIA" + "A" * 16, "AKIA" + "A" * 16)
    _gone("ASIA" + "B" * 16, "use ASIA" + "B" * 16 + " for sts")     # STS 前缀(原只有 lc_guard)
    _gone("wJalrXUtnFEMIabcdef1234", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdef1234")  # 下划线陷阱
    _gone("mySecretPass123", "密码：mySecretPass123")                 # 中文关键词
    _gone("aB3d/Ef+gh9IjkLmn0PqrsTuvw2", "key aB3d/Ef+gh9IjkLmn0PqrsTuvw2")  # 高熵含 / 防漏切
    _gone("4111 1111 1111 1111", "卡号 4111 1111 1111 1111")           # 信用卡分组
    _gone("13812345678", "电话 13812345678 打过来")                    # 大陆手机号
    _gone("6228480402564890018", "account 6228480402564890018")      # 长账号
    # JWT(真实长度三段式)
    jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
           "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
           "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c")
    assert jwt not in redact("这是我的 jwt " + jwt)
    # 邮箱掩码保留域名(仅本模块层做)
    assert "***@example.com" in redact("联系 alice@example.com 谈")
    # PEM
    assert redact("-----BEGIN RSA PRIVATE KEY-----") != "-----BEGIN RSA PRIVATE KEY-----"
    # redact_error 保留数字/文件名/行号(排障信息), 只脱凭据
    e = redact_error("Traceback line 42 in foo.py: token=ghp_" + "z" * 30)
    assert "42" in e and "foo.py" in e and ("ghp_" + "z" * 30) not in e
    # 普通文本不误伤
    assert redact("今天天气不错, 我们去公园吧") == "今天天气不错, 我们去公园吧"
    assert redact("") == "" and redact(None) == ""
    assert isinstance(redact(12345), str)          # 非字符串 fail-safe
    # 分类
    assert classify_private("我的工资是多少") is True
    assert classify_private("今天吃什么") is False
    assert looks_entropic("aB3dEf9hIjkLmn0PqrsTuvwx") is True
    assert looks_entropic("alllowercaseword") is False
    # 路径
    assert is_secret_path("/Users/x/.ssh/id_rsa") is True
    assert is_secret_path("/Users/x/notes.txt") is False
    print("secret_redact selftest OK")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        print("usage: secret_redact.py selftest")
