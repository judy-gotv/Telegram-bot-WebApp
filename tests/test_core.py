# -*- coding: utf-8 -*-
# Author: Passwd Word
"""核心逻辑单元测试：代号派生、昵称打码、initData 验签、WebApp i18n 完整性。"""
import hashlib
import hmac
import json
import os
import re
import sys
import tempfile
import time
import unittest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

import main as bot  # noqa: E402

TOKEN = "123456:TESTTOKEN"


def sign_init_data(fields, token=TOKEN):
    check = "\n".join("%s=%s" % (k, fields[k]) for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields = dict(fields)
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return "&".join("%s=%s" % (k, v) for k, v in fields.items())


class CodenameTest(unittest.TestCase):
    def test_format(self):
        for uid in (1, 42, 777000, 123456789, 9876543210):
            code = bot.derive_codename(uid)
            self.assertRegex(code, r"^[A-Za-z]{2}·[A-Z0-9]{3}$", code)

    def test_stable_and_unique(self):
        self.assertEqual(bot.derive_codename(555), bot.derive_codename(555))
        codes = {bot.derive_codename(i) for i in range(2000)}
        self.assertGreater(len(codes), 1990)  # 碰撞率极低

    def test_db_user_keeps_codename(self):
        u1 = bot.db.get_or_create_user({"id": 424242, "first_name": "Paul", "last_name": "d"})
        u2 = bot.db.get_or_create_user({"id": 424242, "first_name": "Paul", "last_name": "d"})
        self.assertEqual(u1["codename"], u2["codename"])
        self.assertEqual(u1["codename"], bot.derive_codename(424242))


class MaskTest(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(bot.mask_name("Pseudod"), "P******d")
        self.assertEqual(bot.mask_name("ab"), "a******b")
        self.assertEqual(bot.mask_name("a"), "a******a")
        self.assertEqual(bot.mask_name(""), "******")


class InitDataTest(unittest.TestCase):
    def test_valid(self):
        data = sign_init_data({
            "auth_date": str(int(time.time())),
            "query_id": "AAE1",
            "user": json.dumps({"id": 777, "first_name": "Pd"}),
            "start_param": "reply_123",
        })
        user, start_param = bot.validate_init_data(data, token=TOKEN)
        self.assertEqual(user["id"], 777)
        self.assertEqual(start_param, "reply_123")

    def test_tampered(self):
        data = sign_init_data({
            "auth_date": str(int(time.time())),
            "user": json.dumps({"id": 777, "first_name": "Pd"}),
        }).replace("777", "888")
        with self.assertRaises(ValueError):
            bot.validate_init_data(data, token=TOKEN)

    def test_expired(self):
        data = sign_init_data({
            "auth_date": str(int(time.time()) - 90000),
            "user": json.dumps({"id": 777}),
        })
        with self.assertRaises(ValueError):
            bot.validate_init_data(data, token=TOKEN)


class I18nTest(unittest.TestCase):
    EXPECTED_LANGS = {"zh-CN", "en", "zh-TW", "fr", "ja", "vi", "es",
                      "pt-BR", "de", "ko", "id", "tr", "ar"}
    EXPECTED_KEYS = {"subtitle", "intro", "ask", "placeholder", "hint_keys",
                     "add_image", "max_images", "drag_hint", "nickname_label",
                     "return_group", "ready", "sending", "sent", "send_btn",
                     "footer", "replying", "too_many", "need_content", "failed"}

    def _load_i18n(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "app", "webapp", "index.html")
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        m = re.search(r"const I18N = (\{.*?\});\n", raw, re.S)
        self.assertIsNotNone(m, "I18N block not found")
        return json.loads(m.group(1))

    def test_all_langs_complete(self):
        i18n = self._load_i18n()
        self.assertEqual(set(i18n.keys()), self.EXPECTED_LANGS)
        for lang, d in i18n.items():
            self.assertEqual(set(d.keys()), self.EXPECTED_KEYS, lang)
            for k, v in d.items():
                self.assertTrue(v.strip(), "%s.%s empty" % (lang, k))

    def test_lang_options_match(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "app", "webapp", "index.html")
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        opts = set(re.findall(r'<option value="([^"]+)">', raw))
        self.assertEqual(opts, self.EXPECTED_LANGS)


class AvatarTest(unittest.TestCase):
    def test_preview_no_avatar(self):
        # 无 Token 预览模式 / 未知 BOT_ID 时应返回 None，前端回退 A6 字标
        data, _ctype = bot.get_bot_avatar()
        self.assertIsNone(data)


class KeyboardTest(unittest.TestCase):
    def test_main_app_link_without_short_name(self):
        bot.BOT_USERNAME = "mybot"
        kb = bot.post_keyboard(123)
        btns = kb["inline_keyboard"][0]
        self.assertEqual(btns[0]["text"], "✍️ 弹窗发言")
        self.assertEqual(btns[0]["url"], "https://t.me/mybot?startapp=reply_123")
        self.assertEqual(btns[1]["text"], "💬 私聊发言")
        self.assertEqual(btns[1]["url"], "https://t.me/mybot?start=reply_123")

    def test_named_app_link_with_short_name(self):
        bot.BOT_USERNAME = "mybot"
        old = bot.APP_SHORT_NAME
        bot.APP_SHORT_NAME = "app"
        try:
            kb = bot.post_keyboard(123)
            self.assertEqual(kb["inline_keyboard"][0][0]["url"],
                             "https://t.me/mybot/app?startapp=reply_123")
        finally:
            bot.APP_SHORT_NAME = old


if __name__ == "__main__":
    unittest.main()
