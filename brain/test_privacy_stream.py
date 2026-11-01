#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_privacy_stream.py — privacy_stream.py 流数据隐私打标测试(unittest, CI 可 python3 直跑)。

被测核心: should_force_local() —— 按内容形状判定是否强制本机收尾(不上云)。
真实返回形状(已用 python3 探测):
  {"force_local": bool, "private": bool, "hits": [{"severity","name"}...], "reason": str}

语义铁律(已读代码核实, 写进断言):
  - high 类(OTP/凭证/定位/卡号/身份证…): force_local=True, **即便 allow_cloud_private=True
    也强制本机**, 除非 config[privacy.allow_cloud_high]=True。
  - med 类(手机/邮箱/银行上下文): 跟随 allow_cloud_private(没授权→本机, 授权→可上云)。
  - 普通业务文本(报价/吨数/销额…)不误伤。
  - 空串 / None / 超长输入不抛, fail-safe。

注: 旧版本是 pytest-only(用 monkeypatch fixture, 无 __main__ runner, python3 直跑不了)。
本版改写为 unittest, 保留旧覆盖并补全, 干净 HOME 下 python3 直跑全绿。

stub: config 经 sys.modules 注入(默认全 default), 不碰真 ~/.jarvis。
Python 3.9 兼容, stdlib only。可直接 python3 test_privacy_stream.py 运行。
"""
import importlib
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


def _stub_config(get_fn):
    m = types.ModuleType("config")
    m.get = get_fn
    sys.modules["config"] = m
    return m


# 默认 config: get(key, default) → default(等价 config.toml 缺省 → 隐私门最严)
_stub_config(lambda key, default=None: default)

import privacy_stream as ps  # noqa: E402 — 必须在 stub 之后


def _names(text):
    return [h["name"] for h in ps.scan(text)]


# =========================================================================== #
# 1. scan — 每类 _PATTERNS 至少 1 正例
# =========================================================================== #
class TestScanPositives(unittest.TestCase):
    """每个敏感形状都能被扫到。"""

    def test_otp_verification(self):
        self.assertIn("otp_verification", _names("你的验证码是 384751,5分钟内有效"))

    def test_otp_bare(self):
        self.assertIn("otp_bare", _names("请输入 123456 完成登录"))

    def test_credential_password(self):
        self.assertIn("credential", _names("password: hunter2secret"))

    def test_credential_apikey(self):
        self.assertIn("credential", _names("api_key = abc123XYZ"))

    def test_bearer_sk(self):
        self.assertIn("bearer_token", _names("sk-abcdefghij1234567890"))

    def test_bearer_ghp(self):
        self.assertIn("bearer_token", _names("ghp_abcdefghij1234567890abcd"))

    def test_bearer_akia(self):
        self.assertIn("bearer_token", _names("AKIA1234567890ABCD"))

    def test_gps_coords(self):
        self.assertIn("gps_coords", _names("当前位置 纬度:30.2741 经度:120.1551"))

    def test_gps_pair(self):
        self.assertIn("gps_pair", _names("坐标 30.2741,120.1551"))

    def test_credit_card_ctx(self):
        self.assertIn("credit_card", _names("卡号 4111 1111 1111 1111"))

    def test_credit_card_bare(self):
        self.assertIn("credit_card_bare", _names("4111-1111-1111-1111"))

    def test_cvv(self):
        self.assertIn("cvv", _names("cvv: 123"))

    def test_cn_id(self):
        self.assertIn("cn_id", _names("身份证 11010119900307123X"))

    def test_passport(self):
        self.assertIn("passport", _names("passport E12345678"))

    def test_cn_phone(self):
        self.assertIn("cn_phone", _names("联系电话 13800138000"))

    def test_email(self):
        self.assertIn("email_addr", _names("联系 sales@example.com"))

    def test_bank_ctx(self):
        self.assertIn("bank_ctx", _names("本月工资已发放"))

    def test_scan_severity_field(self):
        hits = ps.scan("你的验证码是 384751")
        self.assertTrue(hits)
        for h in hits:
            self.assertIn(h["severity"], ("high", "med"))
            self.assertIn("name", h)

    def test_scan_lists_multiple_hits(self):
        names = set(_names("验证码 384751,电话 13800138000"))
        self.assertIn("otp_verification", names)
        self.assertIn("cn_phone", names)


# =========================================================================== #
# 2. scan — 普通业务文本不误伤(工业树脂外贸日常)
# =========================================================================== #
class TestScanNegatives(unittest.TestCase):
    """普通业务文本不该触发任何敏感命中。"""

    def _assert_clean(self, text):
        self.assertEqual(_names(text), [], f"业务文本被误判敏感: {text!r} → {_names(text)}")

    def test_quote(self):
        self._assert_clean("报价606吨数100")

    def test_sales(self):
        self._assert_clean("本月销量1200吨,销额300万")

    def test_spec(self):
        self._assert_clean("客户要PC2含量98%的M-996")

    def test_meeting(self):
        self._assert_clean("今天开会讨论交期")

    def test_price(self):
        self._assert_clean("均价2500美元每吨")

    def test_order_no(self):
        self._assert_clean("订单编号PO20260612")

    def test_plain_chat(self):
        self._assert_clean("今天天气不错, 适合散步")

    def test_dev_chat(self):
        for t in ("帮我整理一下这个文件夹", "git status 看看改了什么", "这个函数返回值有问题"):
            self._assert_clean(t)


# =========================================================================== #
# 3. should_force_local — 返回形状 + high/med 语义
# =========================================================================== #
class TestShouldForceLocalShape(unittest.TestCase):

    def test_return_keys(self):
        r = ps.should_force_local("验证码 123456 登录")
        self.assertEqual(set(r.keys()), {"force_local", "private", "hits", "reason"})
        self.assertIsInstance(r["force_local"], bool)
        self.assertIsInstance(r["private"], bool)
        self.assertIsInstance(r["hits"], list)
        self.assertIsInstance(r["reason"], str)

    def test_clean_text_not_private(self):
        r = ps.should_force_local("报价606吨数100")
        self.assertFalse(r["force_local"])
        self.assertFalse(r["private"])
        self.assertEqual(r["hits"], [])

    def test_high_forces_local_default(self):
        r = ps.should_force_local("你的验证码是 384751")
        self.assertTrue(r["force_local"])
        self.assertTrue(r["private"])

    def test_gps_forces_local(self):
        self.assertTrue(ps.should_force_local("当前位置 纬度:30.2741 经度:120.1551")["force_local"])
        self.assertTrue(ps.should_force_local("30.2741, 120.1551")["force_local"])

    def test_credential_forces_local(self):
        self.assertTrue(ps.should_force_local("password: hunter2xyz")["force_local"])
        self.assertTrue(ps.should_force_local("api_key = sk-abcdef1234567890ABCDEF")["force_local"])
        self.assertTrue(ps.should_force_local("token: Bearer abcdEFGH12345678ijkl")["force_local"])

    def test_cn_id_and_card_force_local(self):
        self.assertTrue(ps.should_force_local("身份证 11010119900307123X")["force_local"])
        self.assertTrue(ps.should_force_local("卡号 6222 0212 3456 7890 123")["force_local"])

    def test_high_forces_local_even_with_allow_cloud_private(self):
        # 铁律: high 敏感即便 User 授权私密上云, 默认仍本机(需单独 allow_cloud_high)
        r = ps.should_force_local("你的验证码是 384751", allow_cloud_private=True)
        self.assertTrue(r["force_local"], "high 敏感不该因 allow_cloud_private 上云")
        self.assertTrue(r["private"])

    def test_med_local_when_not_authorized(self):
        r = ps.should_force_local("联系电话 13800138000", allow_cloud_private=False)
        self.assertTrue(r["force_local"])
        self.assertTrue(r["private"])

    def test_med_cloud_when_authorized(self):
        # med 敏感 + 已授权 → 可上云(force_local=False 但仍标 private)
        r = ps.should_force_local("联系电话 13800138000", allow_cloud_private=True)
        self.assertFalse(r["force_local"])
        self.assertTrue(r["private"])


# =========================================================================== #
# 4. should_force_local — config 驱动的 allow_cloud_high 语义
# =========================================================================== #
class TestAllowCloudHigh(unittest.TestCase):
    """config[privacy.allow_cloud_high]=True 时, high 敏感可随 allow_cloud_private 上云。"""

    def tearDown(self):
        # 还原默认 config 并 reload, 避免污染其他用例
        _stub_config(lambda key, default=None: default)
        importlib.reload(ps)

    def test_high_can_cloud_when_allow_high_set(self):
        _stub_config(lambda key, default=None:
                     True if key == "privacy.allow_cloud_high" else default)
        importlib.reload(ps)
        r = ps.should_force_local("你的验证码是 384751", allow_cloud_private=True)
        self.assertFalse(r["force_local"], "allow_cloud_high=True 时 high 应可上云")
        self.assertTrue(r["private"])

    def test_high_still_local_if_allow_high_but_no_cloud_private(self):
        # allow_high=True 但 allow_cloud_private=False → high 落到 med 分支判 False → 本机
        _stub_config(lambda key, default=None:
                     True if key == "privacy.allow_cloud_high" else default)
        importlib.reload(ps)
        r = ps.should_force_local("你的验证码是 384751", allow_cloud_private=False)
        self.assertTrue(r["force_local"])


# =========================================================================== #
# 5. 边界 / fail-safe — 空串 / None / 超长 不抛
# =========================================================================== #
class TestEdgeCases(unittest.TestCase):

    def test_empty_string(self):
        r = ps.should_force_local("")
        self.assertFalse(r["force_local"])
        self.assertFalse(r["private"])

    def test_none(self):
        r = ps.should_force_local(None)  # type: ignore[arg-type]
        self.assertFalse(r["force_local"])
        self.assertFalse(r["private"])

    def test_scan_empty_and_none(self):
        self.assertEqual(ps.scan(""), [])
        self.assertEqual(ps.scan(None), [])  # type: ignore[arg-type]

    def test_very_long_clean_input(self):
        r = ps.should_force_local("吨" * 200000)
        self.assertFalse(r["force_local"])

    def test_very_long_with_secret_at_end(self):
        # 超长输入 + 末尾藏验证码: 不抛, 且应被捕(fail-safe 从严)
        r = ps.should_force_local("吨" * 50000 + " 你的验证码是 384751")
        self.assertTrue(r["force_local"])

    def test_unicode_and_special_chars(self):
        # 不抛即可
        ps.should_force_local("🔒💰\x00\t\n 测试 ©®")


# =========================================================================== #
# 6. mark — 便捷打标(命中时就地标 _private/_force_local)
# =========================================================================== #
class TestMark(unittest.TestCase):

    def test_mark_high_stamps_flags(self):
        out = ps.mark({"ok": True}, "你的验证码是 384751")
        self.assertTrue(out.get("_private"))
        self.assertTrue(out.get("_force_local"))
        self.assertIn("_privacy_reason", out)

    def test_mark_clean_no_flags(self):
        out = ps.mark({"ok": True}, "天气不错")
        self.assertNotIn("_private", out)
        self.assertNotIn("_force_local", out)

    def test_mark_returns_same_dict(self):
        d = {"ok": True}
        out = ps.mark(d, "你的验证码是 384751")
        self.assertIs(out, d)

    def test_mark_non_dict_safe(self):
        # 非 dict 不抛(就地打标只对 dict 生效)
        out = ps.mark("not a dict", "你的验证码是 384751")  # type: ignore[arg-type]
        self.assertEqual(out, "not a dict")


# =========================================================================== #
# 7. 模块常量 / 开关语义
# =========================================================================== #
class TestModuleContract(unittest.TestCase):

    def test_readonly_keys_present(self):
        # 关流隐私 / 允许高敏感上云 都是 User-only 危险开关
        self.assertIn("privacy.stream_guard_enabled", ps.READONLY_KEYS)
        self.assertIn("privacy.allow_cloud_high", ps.READONLY_KEYS)

    def test_guard_disabled_returns_empty_scan(self):
        try:
            _stub_config(lambda key, default=None:
                         False if key == "privacy.stream_guard_enabled" else default)
            importlib.reload(ps)
            # 总开关关 → scan 直接返回空(等于不打标)
            self.assertEqual(ps.scan("你的验证码是 384751"), [])
            self.assertFalse(ps.should_force_local("你的验证码是 384751")["force_local"])
        finally:
            _stub_config(lambda key, default=None: default)
            importlib.reload(ps)

    def test_enabled_monkeypatch_disables(self):
        # 直接 monkeypatch _enabled(对齐旧 pytest 用例语义), 不依赖 config
        saved = ps._enabled
        try:
            ps._enabled = lambda: False
            self.assertEqual(ps.scan("验证码 384751"), [])
            self.assertFalse(ps.should_force_local("验证码 384751")["force_local"])
        finally:
            ps._enabled = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
