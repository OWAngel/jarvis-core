#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_secret_redact.py — 权威脱敏出口测试(Hermes v0.18 借鉴 B5 #12/#13/#14/#16)。

口径铁律: 断言秘密**值消失** + 出现脱敏标记(具体占位符可能被库层改写, 不锁死)。
双层验证: lib 在场 与 强制 lib 缺席 都要过(保证库在不在都对)。
Python 3.9 兼容, stdlib only。可直接 python3 test_secret_redact.py。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import secret_redact as sr  # noqa: E402

_MARKERS = ("[凭证已脱敏]", "[令牌已脱敏]", "[高熵串已脱敏]", "[数字已脱敏]",
            "[邮箱已脱敏]", "[已脱敏]", "〔已脱敏〕", "***@")


def _has_marker(s):
    return any(m in s for m in _MARKERS)


class _Base(unittest.TestCase):
    def _gone(self, secret, text):
        out = sr.redact(text)
        self.assertNotIn(secret, out, f"秘密未脱敏: {secret!r} 仍在 {out!r}")
        self.assertTrue(_has_marker(out), f"无脱敏标记: {out!r}")
        return out


class TestBareTokens(_Base):
    def test_aws_akia(self):
        self._gone("AKIA" + "A" * 16, "aws key AKIA" + "A" * 16)

    def test_aws_sts_asia_aroa(self):
        # STS 临时前缀 —— 原来只有 lc_guard 覆盖
        self._gone("ASIA" + "B" * 16, "sts ASIA" + "B" * 16)
        self._gone("AROA" + "C" * 16, "role AROA" + "C" * 16)

    def test_github_all_prefixes(self):
        for pfx in ("ghp_", "gho_", "ghu_", "ghs_", "ghr_"):
            self._gone(pfx + "x" * 30, "token " + pfx + "x" * 30)

    def test_openai_sk_and_proj(self):
        self._gone("sk-abcdefghijklmnop123", "key sk-abcdefghijklmnop123")
        self._gone("sk-proj-abcdefghijklmnop", "key sk-proj-abcdefghijklmnop")

    def test_slack_and_google(self):
        self._gone("xoxb-1234567890-abcdef", "slack xoxb-1234567890-abcdef")
        self._gone("AIza" + "a" * 35, "gmaps AIza" + "a" * 35)

    def test_jwt(self):
        jwt = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
               "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ."
               "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c")
        self.assertNotIn(jwt, sr.redact("jwt " + jwt))


class TestKeyValue(_Base):
    def test_basic(self):
        self._gone("hunter2secretval", "password: hunter2secretval")

    def test_underscore_trap(self):
        # aws_secret_access_key= 嵌 UPPER_SNAKE —— \b 陷阱, 必须用 lookaround
        self._gone("wJalrXUtnFEMI1234abcd", "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI1234abcd")

    def test_chinese_keywords(self):
        self._gone("mySecretPass123", "密码：mySecretPass123")
        self._gone("tokenval12345678", "令牌=tokenval12345678")

    def test_long_alias_first(self):
        # client_secret 长别名不能被短 secret 截断
        self._gone("topsecretvalue123", "client_secret=topsecretvalue123")


class TestHighEntropy(_Base):
    def test_base64_with_slash(self):
        # 含 / 的 AWS secret 形态: 字符类必须含 +/= 否则漏切
        self._gone("aB3d/Ef+gh9IjkLmn0PqrsTuvw2", "secret aB3d/Ef+gh9IjkLmn0PqrsTuvw2")

    def test_looks_entropic_filter(self):
        self.assertTrue(sr.looks_entropic("aB3dEf9hIjkLmn0PqrsTuvwx"))
        self.assertFalse(sr.looks_entropic("alllowercaselongword"))
        self.assertFalse(sr.looks_entropic("short"))


class TestLongDigits(_Base):
    def test_credit_card(self):
        self._gone("4111 1111 1111 1111", "卡 4111 1111 1111 1111")

    def test_cn_phone(self):
        self._gone("13812345678", "手机 13812345678")

    def test_long_account(self):
        self._gone("6228480402564890018", "账号 6228480402564890018")

    def test_short_number_not_touched(self):
        # 年份/小数量不误伤
        out = sr.redact("2026 年第 42 号文件")
        self.assertIn("2026", out)
        self.assertIn("42", out)


class TestEmail(_Base):
    def test_mask_preserves_domain(self):
        self.assertIn("***@example.com", sr.redact("联系 alice@example.com"))

    def test_no_username_leak(self):
        self.assertNotIn("alice", sr.redact("mail alice@example.com"))


class TestPEM(_Base):
    def test_pem_block(self):
        self.assertNotEqual(sr.redact("-----BEGIN RSA PRIVATE KEY-----"),
                            "-----BEGIN RSA PRIVATE KEY-----")


class TestRedactError(unittest.TestCase):
    def test_keeps_debug_info_strips_creds(self):
        e = sr.redact_error("Traceback line 42 in foo.py: token=ghp_" + "z" * 30)
        self.assertIn("42", e)
        self.assertIn("foo.py", e)
        self.assertNotIn("ghp_" + "z" * 30, e)

    def test_error_keeps_numbers(self):
        # 报错里的行号/地址不该被当长数字脱掉
        e = sr.redact_error("error at 0x00007fff in module, code 1234567890123")
        self.assertIn("1234567890123", e)   # redact_error 不碰长数字


class TestNoFalsePositive(unittest.TestCase):
    def test_plain_text_unchanged(self):
        for s in ("今天天气不错, 我们去公园吧", "明天开会讨论方案", "the quick brown fox jumps"):
            self.assertEqual(sr.redact(s), s)

    def test_empty_and_none(self):
        self.assertEqual(sr.redact(""), "")
        self.assertEqual(sr.redact(None), "")

    def test_non_string_safe(self):
        self.assertIsInstance(sr.redact(12345), str)


class TestClassifyPrivate(unittest.TestCase):
    def test_private_terms(self):
        self.assertTrue(sr.classify_private("我的工资是多少"))
        self.assertTrue(sr.classify_private("银行卡号"))

    def test_benign_false(self):
        self.assertFalse(sr.classify_private("今天吃什么"))
        self.assertFalse(sr.classify_private(""))


class TestIsSecretPath(unittest.TestCase):
    def test_secret_paths(self):
        for p in ("/Users/x/.ssh/id_rsa", "/a/.gmail_creds.json", "/b/.aws/credentials"):
            self.assertTrue(sr.is_secret_path(p), p)

    def test_normal_paths(self):
        for p in ("/Users/x/notes.txt", "/tmp/report.pdf"):
            self.assertFalse(sr.is_secret_path(p), p)


class TestLibAbsent(_Base):
    """强制 awareness_sensors 缺席 —— 本地强并集单独也要能脱敏(库在不在都对)。"""
    def setUp(self):
        self._orig = sr._lib
        sr._lib = lambda: None

    def tearDown(self):
        sr._lib = self._orig

    def test_redact_without_lib(self):
        self._gone("ghp_" + "q" * 30, "token ghp_" + "q" * 30)
        self._gone("13812345678", "手机 13812345678")
        self.assertIn("***@x.com", sr.redact("a@x.com"))

    def test_classify_deterministic_without_lib(self):
        self.assertTrue(sr.classify_private("工资多少"))
        self.assertFalse(sr.classify_private("今天吃什么"))   # 无库不该盲目 True


if __name__ == "__main__":
    unittest.main(verbosity=2)
