# 部署步骤

服务代码在 `/opt/workspace/tech_oncall/`，owner 是 `ubuntu`，以下步骤不需要 `sudo`，
除了把 unit 文件拷到 `/etc/systemd/system/` 和 `systemctl` 相关命令。

## 1. 创建虚拟环境、装依赖

```bash
cd /opt/workspace/tech_oncall
python3 -m venv .venv
.venv/bin/pip install -e .
```

## 2. 填 .env

```bash
cp .env.example .env   # 如果 .env 已经存在（比如已经填过凭证）就跳过这步
```

需要填的值：

| 变量 | 说明 |
|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书开发者后台 -> 凭证与基础信息 |
| `FEISHU_BOT_OPEN_ID` | 同一页面"机器人"卡片里的 open_id，用来判断消息里 @ 的是不是本机器人 |
| `FEISHU_MODE` | 默认 `ws`（长连接），本次不实现 `http` |
| `DRY_RUN` | 生产环境填 `0` |
| `DB_PATH` | 默认 `./data/badcase_bot.db`，相对 `WorkingDirectory` |
| `LOG_LEVEL` | 默认 `INFO` |

## 3. 本地自测（不需要飞书凭证也能跑）

```bash
.venv/bin/python scripts/fake_event.py
```

## 4. 安装 systemd 服务

```bash
sudo cp deploy/badcase-bot.service /etc/systemd/system/badcase-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now badcase-bot
```

## 5. 看日志

```bash
journalctl -u badcase-bot -f
```

每行是一条 JSON（structlog 输出）。

## 6. 停止 / 重启

```bash
sudo systemctl stop badcase-bot     # SIGTERM，最多等 15s（TimeoutStopSec），
                                     # 服务内部逻辑最多等在途后台任务 10s
sudo systemctl restart badcase-bot
```

## 飞书应用侧配置（需要在开发者后台操作，审批通过后才能真机联调）

1. 开发者后台 -> 事件与回调 -> 订阅方式选「使用长连接接收事件」
2. 添加事件 `im.message.receive_v1`（接收消息 v1.0）
3. 权限管理里加上：
   - `im:message` / `im:message:send_as_bot`（发消息、发卡片）
   - `contact:user.base:readonly`（查发送人姓名，`handler.py` 里有一次这样的查询；
     没有这个权限也不会报错，只是卡片里 `user_name` 会显示"未知"）
4. 把机器人拉进要测试的群，@ 它发一条消息
