# 线上排查助手 —— 飞书机器人（第一步：消息链路骨架）

非研发同学在群里 @ 机器人反馈线上问题，机器人自动排查并返回报告 —— 这是分步实施项目的**第一步**：
只打通飞书消息链路（收消息 -> 发「已受理」卡片 -> 3 秒后原地更新为「处理完成」并回显收到的信息 -> 存 SQLite），
不接排查能力。

## 目录结构

```
src/
  main.py      入口：启动长连接客户端，处理优雅退出
  config.py    pydantic-settings 配置
  handler.py   消息事件处理主逻辑（去重、@判断、异步后台处理）
  feishu.py    飞书 API 封装（发卡片 / 更新卡片 / 查用户名）+ ws/http 两种接入方式
  cards.py     卡片 JSON 构造
  store.py     SQLite 读写
scripts/
  fake_event.py   本地自测，不依赖真实飞书凭证
deploy/
  badcase-bot.service   systemd unit
  README.md             部署步骤
```

## 快速开始

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python scripts/fake_event.py   # 不需要飞书凭证也能跑通
```

真实运行前把 `.env.example` 拷成 `.env` 填好凭证，见 `deploy/README.md`。

## 接入方式

默认走飞书长连接（`FEISHU_MODE=ws`），不需要公网 IP / 域名 / HTTPS 证书。
HTTP 回调模式只留了接口和 TODO（`feishu.py: run_http_callback_server`），第一步不实现。

## 明确没做的事（属于第二步）

不接 Agent SDK / 大模型，不连生产数据库、日志系统、trace 系统，不做实体抽取/意图识别/排查逻辑，
不引入 Redis/Postgres/消息队列，不做用户权限系统。

## 已知限制

- 飞书事件本身不带发送人姓名，`user_name` 靠一次额外的 `contact.v3.user.get` 查询获取，
  需要应用有对应权限；没权限时会退化成"未知"，不影响主流程。
- 判断"是否 @ 了机器人"依赖 `.env` 里手填的 `FEISHU_BOT_OPEN_ID`（SDK 没有现成的"查自己"接口）。
