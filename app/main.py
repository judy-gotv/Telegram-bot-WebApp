# -*- coding: utf-8 -*-
# Author: Passwd Word
"""
Telegram 匿名发言机器人（1:1 复刻 A6api助理 @a6apicombot 的功能形态）

功能：
  - 私聊投稿：/start 欢迎语 +「开始匿名发言」回复键盘；开启后把文字/图片/贴纸
    发给机器人，机器人用专属代号（格式 XX·XXX，如 Pd·MTD）代发到目标群。
  - 群内消息：每条代发消息下方带「✍️ 弹窗发言」「💬 私聊发言」两个按钮；
    通过按钮发言时会自动引用被点的那条消息（群内显示蓝色引用块）。
  - 弹窗发言：Telegram Mini App（WebApp），UI 与原版 1:1：
    13 种语言切换、3800 字计数、Enter 发送 / Ctrl+Enter 换行、
    图片最多 10 张（选择/拖入/粘贴/点击放大预览）、发言昵称打码 P******d、
    「发送后返回群聊」勾选、匿名发送大按钮。
  - 管理员核查：群内只展示代号；真实身份落库。管理员在群里回复任意一条
    匿名消息发送 /who，机器人私信告知真实身份；/del 删除、/ban 封禁发言人、
    /unban 解封。
  - 仅 Python 标准库，无第三方依赖。

配置全部走环境变量，见 .env.example / README.md。
"""

import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import signal
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 配置

def _env(name, default=""):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default

BOT_TOKEN = _env("BOT_TOKEN")
GROUP_CHAT_ID = _env("GROUP_CHAT_ID")            # 目标群 ID，如 -1001234567890
GROUP_NAME = _env("GROUP_NAME", "A6api—Token交易所")
BOT_NAME = _env("BOT_NAME", "A6api助理")
APP_SHORT_NAME = _env("APP_SHORT_NAME")          # BotFather /newapp 的短名，如 app
WEBAPP_URL = _env("WEBAPP_URL")                  # Mini App 的 HTTPS 地址，如 https://bot.example.com/
PORT = int(_env("PORT", _env("SERVER_PORT", _env("HTTP_PORT", "8080"))))
BIND = _env("BIND", "0.0.0.0")
DB_PATH = _env("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "anon.db"))
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", _env("ADMIN_IDS")) if x.lstrip("-").isdigit()}
CODENAME_SALT = _env("CODENAME_SALT") or (hashlib.sha256(BOT_TOKEN.encode()).hexdigest() if BOT_TOKEN else "preview-salt")
ALLOW_PREVIEW = _env("ALLOW_PREVIEW", "1") == "1"   # 无 Token 时只跑 WebApp 预览服务
MAX_TEXT_LEN = 3800
MAX_IMAGES = 10
WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

PREVIEW_MODE = not BOT_TOKEN

BTN_START = "开始匿名发言"
BTN_DETAIL = "查看详情"
BTN_HELP = "使用说明"
BTN_EXIT = "退出投稿"

def log(*a):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), *a, flush=True)

# ---------------------------------------------------------------- 代号 / 打码

_SUFFIX_POOL = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

def derive_codename(user_id, salt=CODENAME_SALT):
    """由 user_id 确定性派生代号：2 个字母 · 3 位大写字母/数字，如 Pd·MTD / mu·33X。"""
    d = hashlib.sha256(("%s:%s" % (salt, user_id)).encode()).digest()
    def letter(b, case_byte):
        c = chr(ord("a") + b % 26)
        return c.upper() if case_byte % 2 else c
    return "%s%s·%s%s%s" % (
        letter(d[0], d[6]), letter(d[1], d[7]),
        _SUFFIX_POOL[d[2] % 36], _SUFFIX_POOL[d[3] % 36], _SUFFIX_POOL[d[4] % 36],
    )

def mask_name(name):
    """发言昵称打码：首字 + ****** + 尾字，如 P******d。"""
    name = (name or "").strip()
    if not name:
        return "******"
    if len(name) == 1:
        return name + "******" + name
    return name[0] + "******" + name[-1]

def esc(s):
    return html.escape(s or "", quote=False)

# ---------------------------------------------------------------- 数据库

class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
              codename TEXT UNIQUE, banned INTEGER DEFAULT 0, last_post_id INTEGER DEFAULT 0,
              post_count INTEGER DEFAULT 0, last_post_at REAL DEFAULT 0, created_at REAL);
            CREATE TABLE IF NOT EXISTS posts(
              id INTEGER PRIMARY KEY AUTOINCREMENT, group_msg_id INTEGER UNIQUE,
              user_id INTEGER, codename TEXT, kind TEXT, text TEXT,
              reply_to INTEGER DEFAULT 0, created_at REAL);
            CREATE TABLE IF NOT EXISTS sessions(
              user_id INTEGER PRIMARY KEY, posting INTEGER DEFAULT 0,
              reply_to INTEGER DEFAULT 0, updated_at REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """)

    def get_setting(self, key, default=None):
        with self.lock:
            r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key, value):
        with self.lock, self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))

    def get_or_create_user(self, tg_user):
        uid = int(tg_user["id"])
        with self.lock:
            r = self.conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            if r:
                with self.conn:
                    self.conn.execute(
                        "UPDATE users SET username=?, first_name=?, last_name=? WHERE user_id=?",
                        (tg_user.get("username"), tg_user.get("first_name"), tg_user.get("last_name"), uid))
                return dict(r)
            code = derive_codename(uid)
            n = 0
            while self.conn.execute("SELECT 1 FROM users WHERE codename=?", (code,)).fetchone():
                n += 1
                code = derive_codename("%s#%d" % (uid, n))
            with self.conn:
                self.conn.execute(
                    "INSERT INTO users(user_id,username,first_name,last_name,codename,created_at) VALUES(?,?,?,?,?,?)",
                    (uid, tg_user.get("username"), tg_user.get("first_name"), tg_user.get("last_name"),
                     code, time.time()))
            r = self.conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
            return dict(r)

    def get_user(self, uid):
        with self.lock:
            r = self.conn.execute("SELECT * FROM users WHERE user_id=?", (int(uid),)).fetchone()
        return dict(r) if r else None

    def set_banned(self, uid, banned):
        with self.lock, self.conn:
            self.conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if banned else 0, int(uid)))

    def record_post(self, group_msg_id, user, kind, text, reply_to=0):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO posts(group_msg_id,user_id,codename,kind,text,reply_to,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (int(group_msg_id), int(user["user_id"]), user["codename"], kind, (text or "")[:500],
                 int(reply_to or 0), time.time()))
            self.conn.execute(
                "UPDATE users SET last_post_id=?, post_count=post_count+1, last_post_at=? WHERE user_id=?",
                (int(group_msg_id), time.time(), int(user["user_id"])))

    def get_post(self, group_msg_id):
        with self.lock:
            r = self.conn.execute("SELECT * FROM posts WHERE group_msg_id=?", (int(group_msg_id),)).fetchone()
        return dict(r) if r else None

    def get_session(self, uid):
        with self.lock:
            r = self.conn.execute("SELECT * FROM sessions WHERE user_id=?", (int(uid),)).fetchone()
        return dict(r) if r else {"user_id": int(uid), "posting": 0, "reply_to": 0}

    def set_session(self, uid, posting=None, reply_to=None):
        cur = self.get_session(uid)
        p = cur["posting"] if posting is None else (1 if posting else 0)
        rt = cur["reply_to"] if reply_to is None else int(reply_to or 0)
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO sessions(user_id,posting,reply_to,updated_at) VALUES(?,?,?,?)",
                (int(uid), p, rt, time.time()))

db = DB(DB_PATH)

# ---------------------------------------------------------------- Telegram API

API_BASE = "https://api.telegram.org/bot%s/" % BOT_TOKEN
BOT_USERNAME = ""          # getMe 后填充
GROUP_USERNAME = ""        # getChat 后填充（群公开用户名，可空）
_admin_cache = {}          # user_id -> (is_admin, ts)

class TGError(Exception):
    def __init__(self, desc, code=None):
        super().__init__(desc)
        self.desc = desc
        self.code = code

def _multipart(fields, files):
    boundary = "----anonbot" + uuid.uuid4().hex
    body = bytearray()
    for k, v in (fields or {}).items():
        if v is None:
            continue
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                 % (boundary, k, v)).encode()
    for k, (fname, data, ctype) in (files or {}).items():
        body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                 "Content-Type: %s\r\n\r\n" % (boundary, k, fname, ctype)).encode()
        body += data + b"\r\n"
    body += ("--%s--\r\n" % boundary).encode()
    return bytes(body), "multipart/form-data; boundary=" + boundary

def tg(method, params=None, files=None, timeout=35):
    """调用 Bot API。params 的值如为 dict/list 会自动 JSON 序列化。"""
    if PREVIEW_MODE:
        raise TGError("preview mode: no BOT_TOKEN")
    fields = {}
    for k, v in (params or {}).items():
        if v is None:
            continue
        fields[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    if files:
        data, ctype = _multipart(fields, files)
    else:
        data = json.dumps({k: (v if isinstance(v, (dict, list)) else _coerce(v))
                           for k, v in fields.items()}).encode()
        # 上面 fields 已把标量转成 str，这里还原数字/布尔，避免 chat_id 变字符串也没关系（API 接受），
        # 但为稳妥直接用原始 params 组 JSON：
        raw = {}
        for k, v in (params or {}).items():
            if v is not None:
                raw[k] = v
        data = json.dumps(raw, ensure_ascii=False).encode()
        ctype = "application/json"
    req = urllib.request.Request(API_BASE + method, data=data,
                                 headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read().decode("utf-8"))
        except Exception:
            raise TGError("HTTP %s" % e.code, e.code)
    except Exception as e:
        raise TGError(str(e))
    if not out.get("ok"):
        raise TGError(out.get("description", "api error"), out.get("error_code"))
    return out.get("result")

def _coerce(v):
    return v

def send_private(user_id, text, reply_markup=None, parse_mode="HTML"):
    p = {"chat_id": user_id, "text": text, "parse_mode": parse_mode,
         "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    return tg("sendMessage", p)

def delete_later(chat_id, msg_id, delay=10):
    def _f():
        time.sleep(delay)
        try:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
        except Exception:
            pass
    threading.Thread(target=_f, daemon=True).start()

def message_link(group_msg_id):
    if GROUP_USERNAME:
        return "https://t.me/%s/%s" % (GROUP_USERNAME, group_msg_id)
    gid = str(GROUP_CHAT_ID)
    if gid.startswith("-100"):
        gid = gid[4:]
    return "https://t.me/c/%s/%s" % (gid, group_msg_id)

def post_keyboard(group_msg_id):
    """群内每条匿名消息下方的两个按钮（与原版 1:1）。"""
    if APP_SHORT_NAME:
        popup_url = "https://t.me/%s/%s?startapp=reply_%s" % (BOT_USERNAME, APP_SHORT_NAME, group_msg_id)
    else:
        popup_url = "https://t.me/%s?start=reply_%s" % (BOT_USERNAME, group_msg_id)
    private_url = "https://t.me/%s?start=reply_%s" % (BOT_USERNAME, group_msg_id)
    return {"inline_keyboard": [[
        {"text": "✍️ 弹窗发言", "url": popup_url},
        {"text": "💬 私聊发言", "url": private_url},
    ]]}

def attach_keyboard(group_msg_id):
    try:
        tg("editMessageReplyMarkup", {
            "chat_id": GROUP_CHAT_ID, "message_id": group_msg_id,
            "reply_markup": post_keyboard(group_msg_id)})
    except Exception as e:
        log("[keyboard] attach failed:", e)

def is_admin(user_id):
    if int(user_id) in ADMIN_IDS:
        return True
    hit = _admin_cache.get(int(user_id))
    if hit and time.time() - hit[1] < 300:
        return hit[0]
    ok = False
    try:
        m = tg("getChatMember", {"chat_id": GROUP_CHAT_ID, "user_id": user_id})
        ok = m.get("status") in ("administrator", "creator")
    except Exception:
        ok = False
    _admin_cache[int(user_id)] = (ok, time.time())
    return ok

# ---------------------------------------------------------------- 代发进群

def _reply_kw(reply_to):
    return {"reply_to_message_id": int(reply_to)} if reply_to else {}

def post_text(user, text, reply_to=0):
    msg = tg("sendMessage", dict({
        "chat_id": GROUP_CHAT_ID,
        "text": "<b>%s:</b> %s" % (esc(user["codename"]), esc(text)),
        "parse_mode": "HTML", "disable_web_page_preview": True,
    }, **_reply_kw(reply_to)))
    mid = msg["message_id"]
    attach_keyboard(mid)
    db.record_post(mid, user, "text", text, reply_to)
    return mid

def post_photo(user, photo, caption="", reply_to=0, is_bytes=False):
    cap = "<b>%s:</b> %s" % (esc(user["codename"]), esc(caption)) if caption else "<b>%s:</b>" % esc(user["codename"])
    params = dict({"chat_id": GROUP_CHAT_ID, "caption": cap[:1024], "parse_mode": "HTML"},
                  **_reply_kw(reply_to))
    if is_bytes:
        fname, data = photo
        msg = tg("sendPhoto", params, files={"photo": (fname, data, mimetypes.guess_type(fname)[0] or "image/jpeg")})
    else:
        params["photo"] = photo
        msg = tg("sendPhoto", params)
    mid = msg["message_id"]
    attach_keyboard(mid)
    db.record_post(mid, user, "photo", caption, reply_to)
    return mid

def post_media_group(user, items, caption="", reply_to=0, is_bytes=False):
    """items: file_id 列表 或 [(filename, bytes), ...]"""
    media, files = [], {}
    for i, it in enumerate(items[:MAX_IMAGES]):
        entry = {"type": "photo"}
        if i == 0:
            entry["caption"] = ("<b>%s:</b> %s" % (esc(user["codename"]), esc(caption)))[:1024] \
                if caption else "<b>%s:</b>" % esc(user["codename"])
            entry["parse_mode"] = "HTML"
        if is_bytes:
            key = "file%d" % i
            entry["media"] = "attach://" + key
            files[key] = (it[0], it[1], mimetypes.guess_type(it[0])[0] or "image/jpeg")
        else:
            entry["media"] = it
        media.append(entry)
    params = dict({"chat_id": GROUP_CHAT_ID, "media": media}, **_reply_kw(reply_to))
    msgs = tg("sendMediaGroup", params, files=files or None)
    mid = msgs[0]["message_id"]
    attach_keyboard(mid)
    db.record_post(mid, user, "photo", caption, reply_to)
    return mid

def post_sticker(user, file_id, reply_to=0):
    head = tg("sendMessage", dict({
        "chat_id": GROUP_CHAT_ID, "text": "<b>%s:</b>" % esc(user["codename"]),
        "parse_mode": "HTML"}, **_reply_kw(reply_to)))
    mid = head["message_id"]
    try:
        tg("sendSticker", {"chat_id": GROUP_CHAT_ID, "sticker": file_id,
                           "reply_to_message_id": mid})
    except Exception as e:
        log("[sticker] failed:", e)
    attach_keyboard(mid)
    db.record_post(mid, user, "sticker", "", reply_to)
    return mid

def post_file(user, kind, file_id, caption="", reply_to=0):
    """video / animation / document / audio / voice 通用代发。"""
    method = {"video": "sendVideo", "animation": "sendAnimation", "document": "sendDocument",
              "audio": "sendAudio", "voice": "sendVoice"}[kind]
    cap = "<b>%s:</b> %s" % (esc(user["codename"]), esc(caption)) if caption else "<b>%s:</b>" % esc(user["codename"])
    params = dict({"chat_id": GROUP_CHAT_ID, kind: file_id, "caption": cap[:1024],
                   "parse_mode": "HTML"}, **_reply_kw(reply_to))
    if kind == "voice":
        params.pop("caption", None)
        params["caption"] = cap[:1024]
    msg = tg(method, params)
    mid = msg["message_id"]
    attach_keyboard(mid)
    db.record_post(mid, user, kind, caption, reply_to)
    return mid

# ---------------------------------------------------------------- 回复键盘

def kb_start():
    return {"keyboard": [[{"text": BTN_START}]], "resize_keyboard": True}

def kb_posting():
    return {"keyboard": [[{"text": BTN_DETAIL}],
                         [{"text": BTN_HELP}, {"text": BTN_EXIT}]],
            "resize_keyboard": True}

def help_text(user):
    return ("📖 <b>使用说明</b>\n\n"
            "1. 点击「开始匿名发言」开启投稿，之后直接把文字、图片或贴纸发给我即可。\n"
            "2. 我会用你的专属代号把内容发布到 %s，群内只展示代号，不展示你的原始账号。\n"
            "3. 点击群内消息下方的「弹窗发言」可在弹窗里发言；点「私聊发言」会带着回复目标来到这里，"
            "下一条投稿会自动引用对方那条消息。\n"
            "4. 发送后点「查看详情」可跳转到群内那条消息。\n"
            "5. 管理员可核查发言身份，请勿发布违法违规内容，违规将被封禁投稿。\n\n"
            "你的代号：<b>%s</b>") % (esc(GROUP_NAME), esc(user["codename"]))

def open_posting(uid, user, reply_to=0):
    db.set_session(uid, posting=True, reply_to=reply_to)
    if reply_to:
        target = db.get_post(reply_to)
        head = "✍️ <b>匿名发言已开启</b>\n正在回复 <b>%s</b>：%s\n" % (
            esc(target["codename"]) if target else "—",
            esc((target["text"] or "（图片/贴纸）")[:60]) if target else "")
    else:
        head = "✍️ <b>匿名发言已开启</b>\n"
    send_private(uid, head +
                 "把想说的话发给我，我会用你的代号发布到 %s。支持文字、图片和贴纸。\n\n"
                 "你的代号：<b>%s</b>\n群内展示匿名代号，管理员可核查身份。"
                 % (esc(GROUP_NAME), esc(user["codename"])),
                 reply_markup=kb_posting())

def confirm_sent(uid, user, mid):
    db.set_session(uid, reply_to=0)
    send_private(uid, "✅ 已匿名发布到群里。",
                 reply_markup={"inline_keyboard": [[
                     {"text": "在群里查看 ↗", "url": message_link(mid)}]]})

def check_can_post(uid, user):
    if user.get("banned"):
        send_private(uid, "🚫 你已被管理员禁止匿名发言。如有疑问请联系群管理员。")
        return False
    if time.time() - float(user.get("last_post_at") or 0) < 2:
        send_private(uid, "发送太快了，请稍等几秒再发。")
        return False
    return True

# 相册（多图一条消息）缓冲：Telegram 会逐张下发同 media_group_id 的更新
_albums = {}
_albums_lock = threading.Lock()

def _flush_album(mgid):
    with _albums_lock:
        alb = _albums.pop(mgid, None)
    if not alb:
        return
    try:
        mid = post_media_group(alb["user"], alb["photos"], alb["caption"], alb["reply_to"])
        confirm_sent(alb["uid"], alb["user"], mid)
    except Exception as e:
        log("[album] post failed:", e)
        try:
            send_private(alb["uid"], "❌ 发布失败：%s" % esc(str(e)))
        except Exception:
            pass

def handle_private(msg):
    uid = msg["from"]["id"]
    user = db.get_or_create_user(msg["from"])
    sess = db.get_session(uid)
    text = (msg.get("text") or "").strip()

    # ---- 命令与键盘按钮
    if text.startswith("/start"):
        param = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        m = re.match(r"reply_(\d+)$", param)
        if m:
            open_posting(uid, user, reply_to=int(m.group(1)))
            return
        send_private(uid,
                     "欢迎来到 %s，点击「开始匿名发言」，就能把文字、图片或贴纸匿名发布到群里。\n"
                     "群内展示匿名代号，管理员可核查身份。" % esc(GROUP_NAME),
                     reply_markup=kb_posting() if sess["posting"] else kb_start())
        return
    if text.startswith("/help") or text == BTN_HELP:
        send_private(uid, help_text(user),
                     reply_markup=kb_posting() if sess["posting"] else kb_start())
        return
    if text.startswith("/id"):
        send_private(uid, "你的 ID：<code>%s</code>\n目标群 ID：<code>%s</code>" % (uid, GROUP_CHAT_ID))
        return
    if text.startswith("/who") or text.startswith("/ban") or text.startswith("/unban"):
        handle_admin_private(uid, user, text)
        return
    if text == BTN_START:
        open_posting(uid, user)
        return
    if text == BTN_EXIT:
        db.set_session(uid, posting=False, reply_to=0)
        send_private(uid, "已退出匿名发言。点「开始匿名发言」可重新开启。",
                     reply_markup=kb_start())
        return
    if text == BTN_DETAIL:
        if user.get("last_post_id"):
            send_private(uid,
                         "你的代号：<b>%s</b>\n累计匿名发言：%d 条\n最近一条：" %
                         (esc(user["codename"]), int(user.get("post_count") or 0)),
                         reply_markup={"inline_keyboard": [[
                             {"text": "在群里查看 ↗", "url": message_link(user["last_post_id"])}]]})
        else:
            send_private(uid, "你还没有发布过匿名发言。点「开始匿名发言」来发第一条吧。")
        return

    # ---- 投稿内容
    if not sess["posting"]:
        send_private(uid, "请先点击「开始匿名发言」开启匿名投稿。",
                     reply_markup=kb_start())
        return
    if not check_can_post(uid, user):
        return
    reply_to = int(sess["reply_to"] or 0)
    caption = (msg.get("caption") or "").strip()
    try:
        if text:
            if len(text) > MAX_TEXT_LEN:
                send_private(uid, "内容太长了，最多 %d 字。" % MAX_TEXT_LEN)
                return
            mid = post_text(user, text, reply_to)
            confirm_sent(uid, user, mid)
        elif "photo" in msg:
            fid = msg["photo"][-1]["file_id"]
            mgid = msg.get("media_group_id")
            if mgid:
                with _albums_lock:
                    alb = _albums.setdefault(mgid, {
                        "uid": uid, "user": user, "photos": [],
                        "caption": "", "reply_to": reply_to, "timer": None})
                    alb["photos"].append(fid)
                    if caption:
                        alb["caption"] = caption
                    if alb["timer"]:
                        alb["timer"].cancel()
                    t = threading.Timer(1.2, _flush_album, args=(mgid,))
                    t.daemon = True
                    alb["timer"] = t
                    t.start()
            else:
                mid = post_photo(user, fid, caption, reply_to)
                confirm_sent(uid, user, mid)
        elif "sticker" in msg:
            mid = post_sticker(user, msg["sticker"]["file_id"], reply_to)
            confirm_sent(uid, user, mid)
        elif "video" in msg:
            mid = post_file(user, "video", msg["video"]["file_id"], caption, reply_to)
            confirm_sent(uid, user, mid)
        elif "animation" in msg:
            mid = post_file(user, "animation", msg["animation"]["file_id"], caption, reply_to)
            confirm_sent(uid, user, mid)
        elif "document" in msg:
            mid = post_file(user, "document", msg["document"]["file_id"], caption, reply_to)
            confirm_sent(uid, user, mid)
        elif "audio" in msg:
            mid = post_file(user, "audio", msg["audio"]["file_id"], caption, reply_to)
            confirm_sent(uid, user, mid)
        elif "voice" in msg:
            mid = post_file(user, "voice", msg["voice"]["file_id"], caption, reply_to)
            confirm_sent(uid, user, mid)
        else:
            send_private(uid, "暂不支持这种消息类型，请发送文字、图片或贴纸。")
    except TGError as e:
        log("[post] failed:", e)
        send_private(uid, "❌ 发布失败：%s" % esc(e.desc))

def handle_admin_private(uid, user, text):
    """私聊管理命令：/who <群消息ID> | /ban <消息ID|用户ID> | /unban <用户ID>"""
    if not is_admin(uid):
        send_private(uid, "无权限。")
        return
    parts = text.split()
    cmd = parts[0].split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/who" and arg.isdigit():
        post = db.get_post(int(arg))
        if not post:
            send_private(uid, "没找到这条匿名消息的记录。")
            return
        author = db.get_user(post["user_id"])
        send_private(uid, identity_text(post, author))
    elif cmd in ("/ban", "/unban") and arg.isdigit():
        target = db.get_post(int(arg))
        target_uid = target["user_id"] if target else int(arg)
        db.set_banned(target_uid, cmd == "/ban")
        send_private(uid, "已%s用户 <code>%s</code> 的匿名发言。" %
                     ("封禁" if cmd == "/ban" else "解封", target_uid))
    else:
        send_private(uid, "用法：\n/who &lt;群消息ID&gt;\n/ban &lt;群消息ID 或 用户ID&gt;\n/unban &lt;用户ID&gt;")

def identity_text(post, author):
    if not author:
        return "这条消息的作者记录不存在。"
    name = ((author.get("first_name") or "") + " " + (author.get("last_name") or "")).strip() or "（无名）"
    uname = "@" + author["username"] if author.get("username") else "（无用户名）"
    tstr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(post["created_at"]))
    return ("🔍 <b>发言身份核查</b>\n\n"
            "代号：<b>%s</b>\n姓名：<a href=\"tg://user?id=%s\">%s</a>\n用户名：%s\n"
            "用户ID：<code>%s</code>\n时间：%s\n内容：%s\n群消息：%s" %
            (esc(post["codename"]), author["user_id"], esc(name), esc(uname),
             author["user_id"], tstr, esc((post["text"] or "（图片/贴纸）")[:200]),
             message_link(post["group_msg_id"])))

# ---------------------------------------------------------------- 群内管理命令

def handle_group(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    cmd = text.split()[0].split("@")[0]
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    if cmd == "/id":
        try:
            r = tg("sendMessage", {"chat_id": chat_id,
                                   "text": "本群 ID：<code>%s</code>\n你的 ID：<code>%s</code>" % (chat_id, uid),
                                   "parse_mode": "HTML",
                                   "reply_to_message_id": msg["message_id"]})
            delete_later(chat_id, msg["message_id"], 1)
            delete_later(chat_id, r["message_id"], 30)
        except Exception:
            pass
        return
    if cmd not in ("/who", "/del", "/ban", "/unban"):
        return
    reply = msg.get("reply_to_message")
    if not reply:
        return
    # 删除命令本身，保持群面干净
    delete_later(chat_id, msg["message_id"], 1)
    if not is_admin(uid):
        return
    post = db.get_post(reply["message_id"])
    if cmd == "/del":
        try:
            tg("deleteMessage", {"chat_id": chat_id, "message_id": reply["message_id"]})
        except Exception as e:
            log("[del] failed:", e)
        return
    if not post:
        try:
            r = tg("sendMessage", {"chat_id": chat_id, "text": "这条消息没有匿名发言记录。"})
            delete_later(chat_id, r["message_id"], 8)
        except Exception:
            pass
        return
    if cmd == "/who":
        author = db.get_user(post["user_id"])
        try:
            send_private(uid, identity_text(post, author))
        except Exception:
            try:
                r = tg("sendMessage", {"chat_id": chat_id,
                                       "text": "无法私信你，请先私聊机器人发送 /start 后再查询。"})
                delete_later(chat_id, r["message_id"], 8)
            except Exception:
                pass
    elif cmd == "/ban":
        db.set_banned(post["user_id"], True)
        try:
            r = tg("sendMessage", {"chat_id": chat_id,
                                   "text": "已封禁代号 %s 的匿名发言。" % post["codename"]})
            delete_later(chat_id, r["message_id"], 8)
        except Exception:
            pass
    elif cmd == "/unban":
        db.set_banned(post["user_id"], False)

# ---------------------------------------------------------------- 轮询

def poll_loop():
    offset = int(db.get_setting("update_offset", "0") or 0)
    tg("deleteWebhook", {"drop_pending_updates": False})
    log("[bot] polling started as @%s -> group %s (%s)" % (BOT_USERNAME, GROUP_CHAT_ID, GROUP_NAME))
    while True:
        try:
            updates = tg("getUpdates", {"offset": offset, "timeout": 30,
                                        "allowed_updates": ["message"]}, timeout=40)
        except Exception as e:
            log("[poll] error:", e)
            time.sleep(3)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            db.set_setting("update_offset", offset)
            msg = upd.get("message")
            if not msg or "from" not in msg:
                continue
            try:
                if msg["chat"]["type"] == "private":
                    handle_private(msg)
                elif int(msg["chat"]["id"]) == int(GROUP_CHAT_ID):
                    handle_group(msg)
            except Exception as e:
                log("[handle] error:", e)

# ---------------------------------------------------------------- WebApp 数据校验

def validate_init_data(init_data, token=BOT_TOKEN, max_age=86400):
    """校验 Telegram Mini App initData，返回 (user_dict, start_param)。"""
    parsed = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    data = dict(parsed)
    got_hash = data.pop("hash", "")
    if not got_hash:
        raise ValueError("missing hash")
    check_str = "\n".join("%s=%s" % (k, data[k]) for k in sorted(data))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got_hash):
        raise ValueError("bad hash")
    if max_age and time.time() - int(data.get("auth_date", "0")) > max_age:
        raise ValueError("initData expired")
    user = json.loads(data.get("user", "{}"))
    if not user.get("id"):
        raise ValueError("missing user")
    return user, data.get("start_param", "")

# ---------------------------------------------------------------- WebApp HTTP 服务

def _load_webapp_html():
    path = os.path.join(WEBAPP_DIR, "index.html")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return (raw.replace("__GROUP_NAME__", html.escape(GROUP_NAME))
               .replace("__BOT_NAME__", html.escape(BOT_NAME))
               .replace("__BOT_USERNAME__", html.escape(BOT_USERNAME or "bot")))

class WebHandler(BaseHTTPRequestHandler):
    server_version = "AnonBot/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = _load_webapp_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/health":
            self._json(200, {"ok": True, "preview": PREVIEW_MODE,
                             "bot": BOT_USERNAME, "group": GROUP_NAME})
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/api/post", "/api/reply-info"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 40 * 1024 * 1024:
                self._json(413, {"ok": False, "error": "too large"})
                return
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return

        init_data = body.get("initData", "")
        preview_fake = PREVIEW_MODE and ALLOW_PREVIEW and init_data == "preview"
        try:
            if preview_fake:
                tg_user = {"id": 999999001, "first_name": "Preview", "username": "preview"}
                start_param = body.get("startParam", "")
            else:
                tg_user, start_param = validate_init_data(init_data)
        except Exception as e:
            self._json(403, {"ok": False, "error": "initData invalid: %s" % e})
            return
        user = db.get_or_create_user(tg_user)

        if path == "/api/reply-info":
            rid = body.get("replyTo") or (start_param[6:] if start_param.startswith("reply_") else "")
            post = db.get_post(int(rid)) if str(rid).isdigit() else None
            if post:
                self._json(200, {"ok": True, "codename": post["codename"],
                                 "snippet": (post["text"] or "（图片/贴纸）")[:80]})
            else:
                self._json(200, {"ok": True, "codename": "", "snippet": ""})
            return

        # /api/post
        if user.get("banned"):
            self._json(403, {"ok": False, "error": "banned"})
            return
        text = (body.get("text") or "").strip()
        images = body.get("images") or []
        if len(text) > MAX_TEXT_LEN:
            self._json(400, {"ok": False, "error": "text too long"})
            return
        if len(images) > MAX_IMAGES:
            self._json(400, {"ok": False, "error": "too many images"})
            return
        if not text and not images:
            self._json(400, {"ok": False, "error": "empty"})
            return
        reply_to = body.get("replyTo") or (start_param[6:] if start_param.startswith("reply_") else 0)
        reply_to = int(reply_to) if str(reply_to).isdigit() else 0
        if preview_fake:
            self._json(200, {"ok": True, "preview": True, "message_id": 0,
                             "codename": user["codename"]})
            return
        try:
            decoded = []
            for i, im in enumerate(images):
                raw = base64.b64decode(im.get("data", ""), validate=True)
                if len(raw) > 10 * 1024 * 1024:
                    self._json(413, {"ok": False, "error": "image too large"})
                    return
                decoded.append((im.get("name") or "photo%d.jpg" % i, raw))
            if not decoded:
                mid = post_text(user, text, reply_to)
            elif len(decoded) == 1:
                mid = post_photo(user, decoded[0], text, reply_to, is_bytes=True)
            else:
                mid = post_media_group(user, decoded, text, reply_to, is_bytes=True)
        except TGError as e:
            self._json(502, {"ok": False, "error": e.desc})
            return
        except Exception as e:
            log("[webapp] post failed:", e)
            self._json(500, {"ok": False, "error": str(e)})
            return
        self._json(200, {"ok": True, "message_id": mid,
                         "link": message_link(mid), "codename": user["codename"]})

def serve_http():
    httpd = ThreadingHTTPServer((BIND, PORT), WebHandler)
    log("[web] listening on %s:%d (preview=%s)" % (BIND, PORT, PREVIEW_MODE))
    httpd.serve_forever()

# ---------------------------------------------------------------- 入口

def setup_bot():
    global BOT_USERNAME, GROUP_USERNAME, GROUP_NAME
    me = tg("getMe")
    BOT_USERNAME = me.get("username", "")
    log("[bot] I am @%s (%s)" % (BOT_USERNAME, me.get("first_name")))
    try:
        chat = tg("getChat", {"chat_id": GROUP_CHAT_ID})
        GROUP_USERNAME = chat.get("username", "") or ""
        if chat.get("title"):
            GROUP_NAME = chat["title"]
        log("[bot] group: %s (@%s)" % (GROUP_NAME, GROUP_USERNAME or "-"))
    except Exception as e:
        log("[bot] getChat failed (机器人进群了吗？群ID对吗？):", e)
    try:
        tg("setMyCommands", {"commands": [
            {"command": "start", "description": "开始匿名发言"},
            {"command": "help", "description": "使用说明"},
            {"command": "id", "description": "查看本群/我的 ID"},
            {"command": "who", "description": "管理员：回复匿名消息核查身份"},
            {"command": "del", "description": "管理员：回复匿名消息删除它"},
            {"command": "ban", "description": "管理员：回复匿名消息封禁发言人"},
            {"command": "unban", "description": "管理员：回复匿名消息解封发言人"},
        ]})
    except Exception as e:
        log("[bot] setMyCommands failed:", e)
    if WEBAPP_URL:
        try:
            tg("setChatMenuButton", {"menu_button": {
                "type": "web_app", "text": "匿名发言",
                "web_app": {"url": WEBAPP_URL}}})
        except Exception as e:
            log("[bot] setChatMenuButton failed:", e)

def main():
    try:
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    except Exception:
        pass  # 非主线程/受限环境下跳过（APTV 等宿主）
    if PREVIEW_MODE:
        log("[!] 未设置 BOT_TOKEN，进入预览模式：仅提供 Mini App 页面，/api/post 返回模拟结果。")
        serve_http()
        return
    if not GROUP_CHAT_ID:
        log("[!] 未设置 GROUP_CHAT_ID，无法启动机器人。")
        sys.exit(1)
    setup_bot()
    threading.Thread(target=serve_http, daemon=True).start()
    poll_loop()

if __name__ == "__main__":
    main()
