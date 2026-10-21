# Telegram 匿名发言机器人（Bot + Mini App）

1:1 复刻 **A6api助理（@a6apicombot）** 的功能形态：群成员通过机器人**匿名发言**到目标群，群内只展示专属代号（如 `Pd·MTD`），管理员可核查真实身份。支持**私聊投稿**与**弹窗发言（Telegram Mini App）**两种入口。

仅用 Python 标准库实现，无第三方依赖；Docker 一条命令部署。

## 功能对照（按原版截图逐项实现）

| 原版 | 本项目 |
|---|---|
| 私聊欢迎语 +「开始匿名发言」按钮 | ✅ `/start` 原文案，回复键盘 |
| 开启后提示「你的代号：Pd·MTD」 | ✅ 代号由用户 ID 确定性派生，格式 `XX·XXX`，终身不变 |
| 私聊发文字/图片/贴纸 → 代发进群 | ✅ 另支持相册多图、视频、动图、文件、音频、语音 |
| 群内消息格式「代号：内容」 | ✅ 机器人代发，代号加粗 |
| 每条消息下方「✍️ 弹窗发言」「💬 私聊发言」按钮 | ✅ 与原版同文案同排版 |
| 点按钮发言自动引用原消息（蓝色引用块） | ✅ 弹窗经 `startapp=reply_<消息ID>`、私聊经 `start=reply_<消息ID>` 带入回复目标 |
| 弹窗：群名头部 + A6 标志 + 语言下拉 | ✅ 群名启动时自动读取群真实标题 |
| 语言：简体中文/English/繁體中文/Français/日本語/Tiếng Việt/Español/Português (Brasil)/Deutsch/한국어/Bahasa Indonesia/Türkçe/العربية | ✅ 13 种语言全量 UI 翻译，含阿拉伯语 RTL |
| 「想说些什么？」+ 0/3800 计数 | ✅ 3800 字上限 + 实时计数 |
| Enter 发送、Ctrl/⌘+Enter 换行 | ✅ 与原版一致 |
| 添加图片：最多 10 张、可多选、拖入、Ctrl+V 粘贴、点击放大预览 | ✅ 全部实现，缩略图可删除 |
| 发言昵称打码 `P******d` | ✅ 按 Telegram 姓名首尾字打码（6 个星号） |
| 「发送后返回群聊」勾选（默认勾选） | ✅ 勾选时发送成功自动关闭弹窗回群 |
| 「准备就绪。」状态行 +「匿名发送 ↗」大按钮 | ✅ 发送中/已发送/失败状态齐全，空内容时按钮置浅蓝 |
| 底部「群内仅展示打码昵称，管理员可核查发言身份。」+ @机器人 | ✅ |
| 私聊键盘「查看详情 / 使用说明 / 退出投稿」 | ✅ 查看详情跳转到群内自己最近一条消息（t.me 链接） |
| 管理员可核查身份 | ✅ 管理员在群里**回复**任意匿名消息发 `/who`，机器人**私信**告知姓名/@用户名/用户ID/时间/内容，群内不泄露 |

> 原版群消息右侧的「会员/管理员」小徽章是 Telegram 客户端的**群成员标签**（群设置里给成员打的 tag），不是机器人 API 能控制的内容，需要在群设置中自行给机器人/成员设置，不在本项目范围内。

## 管理员命令

在群里**回复**某条匿名消息发送：

| 命令 | 作用 |
|---|---|
| `/who` | 核查发言人真实身份（结果只私信给管理员） |
| `/del` | 删除这条匿名消息 |
| `/ban` | 封禁发言人（禁止其再通过机器人发言，私聊与弹窗都拦） |
| `/unban` | 解封发言人 |
| `/id` | 查看本群 ID 与你的用户 ID（30 秒后自动删除，配置时用） |

私聊机器人同样可用：`/who <群消息ID>`、`/ban <群消息ID或用户ID>`、`/unban <用户ID>`。
群管理员自动有权限；也可在 `.env` 的 `ADMIN_IDS` 里追加用户 ID。

## 快速部署

### 1）创建机器人（BotFather）

1. 私聊 [@BotFather](https://t.me/BotFather) → `/newbot`，按提示取名、设用户名，拿到 **BOT_TOKEN**。
2. （可选，建议）`/setuserpic` 给机器人换头像；`/setdescription`、`/setabouttext` 填介绍。
3. **创建 Mini App**（弹窗发言必需）：`/newapp` → 选择机器人 → 填标题/简介/图片 →
   - Web App URL 填你的公网地址，如 `https://bot.example.com/`
   - Short name 填一个短名，如 `app` —— 记下来填进 `.env` 的 `APP_SHORT_NAME`
4. 把机器人拉进目标群，并设为**管理员**（需要发消息、删消息权限）。

### 2）启动服务

```bash
git clone https://github.com/judy-gotv/Telegram-bot-WebApp.git
cd Telegram-bot-WebApp
cp .env.example .env   # 填 BOT_TOKEN；GROUP_CHAT_ID 可先随便填，进群后用 /id 查到再改
docker compose up -d
```

或直接用 GHCR 镜像（main 推送自动构建，amd64+arm64）：

```bash
docker run -d --name anon-bot --restart unless-stopped \
  -p 8080:8080 -v /opt/anon-bot-data:/data \
  --env-file .env \
  ghcr.io/judy-gotv/telegram-bot-webapp:latest
```

查群 ID：服务跑起来后，在目标群里发送 `/id`，机器人会回本群 ID（如 `-1001234567890`），填回 `.env` 的 `GROUP_CHAT_ID` 后重启容器即可。

### 3）给 Mini App 准备 HTTPS

Telegram 小程序只接受 **HTTPS** 地址。用你现有的反向代理把域名指到本服务即可，例如 Caddy：

```
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

或 Nginx / Cloudflare Tunnel 同理。然后把 `.env` 的 `WEBAPP_URL` 设为 `https://bot.example.com/` 并重启。
不配 HTTPS 时私聊投稿仍可正常使用，仅「弹窗发言」不可用。

> 本地预览：在没有 Token 的情况下直接 `python3 app/main.py` 会进入预览模式，只提供小程序页面（投稿返回模拟成功），可先看 UI。

## 配置项

见 [.env.example](.env.example)，全部通过环境变量注入。数据（用户代号、发言记录、封禁名单）存放在数据卷的 SQLite 文件 `anon.db` 里，备份拷走该文件即可。

## 目录结构

```
app/
  main.py            # 机器人长轮询 + WebApp 后端（initData 验签、代发、落库）
  webapp/index.html  # Mini App 前端（单文件，13 语言 i18n 内嵌）
tests/test_core.py   # 单元测试：代号/打码/initData 验签/i18n 完整性
Dockerfile / docker-compose.yml / .github/workflows/docker.yml  # GHCR 多架构自动构建
```

## 安全与隐私

- WebApp 投稿接口按 Telegram 官方算法校验 `initData` 的 HMAC 签名并检查 `auth_date` 时效，伪造请求无法代发。
- 群内永不显示真实账号；身份映射只存放在你自己的服务器数据库中，仅群管理员与 `ADMIN_IDS` 可查询。
- BOT_TOKEN 只放 `.env`（已 gitignore），切勿提交进仓库。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
python3 app/main.py   # 无 Token 时为预览模式，打开 http://127.0.0.1:8080/
```
