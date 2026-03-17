# 明日方舟理智助手使用指南

本插件用于查询森空岛理智数据，并在满理智时主动提醒。

## 1. 功能简介

- 查询当前理智与预计满理智时间
- 满理智主动提醒（按订阅会话推送）
- 多角色绑定时支持指定 `preferred_uid`

## 2. Token 获取

当前代码要求在 `config.json` 的 `token` 字段填写森空岛账号令牌。

推荐获取方式（与插件逻辑一致）：

1. 浏览器登录森空岛后，访问：
   - `https://web-api.skland.com/account/info/hg`
2. 在返回 JSON 中找到 `content` 字段。
3. 将 `content` 的值复制出来，作为插件 `token`。


## 3. Token 填写

编辑插件目录下的 `config.json`：

```json
{
  "token": "你的 token（通常是 account/info/hg 的 content）",
  "check_interval": 60,
  "notify_users": [],
  "preferred_uid": ""
}
```

字段说明：

- `token`：森空岛 token（必填）
- `check_interval`：轮询间隔（秒），用于满理智判定
- `notify_users`：提醒订阅会话列表（由指令自动维护）
- `preferred_uid`：可选，指定要查询的游戏 UID

## 4. 指令功能对照表

| 指令 | 功能 | 说明 |
| --- | --- | --- |
| `/理智` | 查询理智 | 返回当前实时理智、预计满理智时间、账号信息 |
| `/ark notify on` | 开启提醒 | 将当前会话加入满理智提醒列表 |
| `/ark notify off` | 关闭提醒 | 将当前会话移出满理智提醒列表 |

## 5. 满理智提醒逻辑

- 后台每 `check_interval` 秒检查一次。
- 当理智状态从“未满”进入“已满”时触发提醒。
- 至少一次发送成功才会标记为“本轮已提醒”。
- 理智再次下降后会重置提醒状态，下次满理智会再次提醒。

## 6. 常见问题

### 6.1 提示 `10002 用户未登录`

通常是 token 失效或填写错误：

- 重新获取 `account/info/hg` 的 `content`
- 确认 `config.json` 中无 `Bearer ` 前缀

### 6.2 提示 `10000 请求异常`

插件会自动进行签名/platform 兜底重试。若持续失败：

- 先确认 token 是否有效
- 再检查网络与森空岛接口可用性

### 6.3 满理智未收到提醒

- 先执行 `/ark notify on` 订阅当前会话
- 检查 `config.json` 的 `notify_users` 是否为空
- 检查平台是否支持主动私聊发送

## 7. 安全建议

- `token` 等同于登录凭据，请勿泄露。
- 建议定期更换 token。
- 不要将包含真实 token 的 `config.json` 上传到公开仓库。

# astrbot-plugin-helloworld

AstrBot 插件模板 / A template plugin for AstrBot plugin feature

> [!NOTE]
> This repo is just a template of [AstrBot](https://github.com/AstrBotDevs/AstrBot) Plugin.
> 
> [AstrBot](https://github.com/AstrBotDevs/AstrBot) is an agentic assistant for both personal and group conversations. It can be deployed across dozens of mainstream instant messaging platforms, including QQ, Telegram, Feishu, DingTalk, Slack, LINE, Discord, Matrix, etc. In addition, it provides a reliable and extensible conversational AI infrastructure for individuals, developers, and teams. Whether you need a personal AI companion, an intelligent customer support agent, an automation assistant, or an enterprise knowledge base, AstrBot enables you to quickly build AI applications directly within your existing messaging workflows.

# Supports

- [AstrBot Repo](https://github.com/AstrBotDevs/AstrBot)
- [AstrBot Plugin Development Docs (Chinese)](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot Plugin Development Docs (English)](https://docs.astrbot.app/en/dev/star/plugin-new.html)
