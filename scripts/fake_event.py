"""本地自测：伪造一条飞书 @机器人 消息事件，直接调用 handler，不依赖真实飞书凭证/网络。

用法：
    uv run python scripts/fake_event.py
    # 或
    .venv/bin/python scripts/fake_event.py

会打印出：
  1. handler 判断"未被 @"时什么都不做（日志可见，无卡片输出）
  2. 第一次投递：发出的「已受理」卡片 JSON，随后（沉睡 3 秒后）发出的「处理完成」卡片 JSON
  3. 用同一个 event_id 再投递一次：应被去重忽略，不会再打印卡片

DRY_RUN 强制为 1：不会真的调用飞书 API，FeishuGateway 会把卡片 JSON 直接打印出来。
DB_PATH 指向一个独立的临时文件，不会污染 store.py 里积累的真实语料表。
"""

import asyncio
import os
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAKE_BOT_OPEN_ID = "ou_fake_bot_open_id"
FAKE_DB_PATH = REPO_ROOT / "data" / "fake_event_selftest.db"

os.environ.setdefault("FEISHU_APP_ID", "cli_fake_app_id_for_selftest")
os.environ.setdefault("FEISHU_APP_SECRET", "fake_secret_for_selftest")
os.environ["FEISHU_BOT_OPEN_ID"] = FAKE_BOT_OPEN_ID
os.environ["DRY_RUN"] = "1"
os.environ["DB_PATH"] = str(FAKE_DB_PATH)
os.environ.setdefault("LOG_LEVEL", "INFO")

from src.config import load_settings  # noqa: E402
from src.feishu import FeishuGateway  # noqa: E402
from src.handler import Handler  # noqa: E402
from src.investigator import Investigator  # noqa: E402
from src.main import configure_logging  # noqa: E402
from src.store import Store  # noqa: E402
from src.targets import load_targets_registry  # noqa: E402

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1  # noqa: E402


def build_fake_event(event_id: str, mention_bot: bool, text: str) -> P2ImMessageReceiveV1:
    mentions = []
    content_text = text
    if mention_bot:
        mentions.append(
            {
                "key": "@_user_1",
                "id": {"open_id": FAKE_BOT_OPEN_ID, "union_id": "on_fake_bot", "user_id": "fake_bot_uid"},
                "name": "线上排查助手",
                "tenant_key": "fake_tenant",
            }
        )
        content_text = "@_user_1 " + text

    return P2ImMessageReceiveV1(
        {
            "header": {
                "event_id": event_id,
                "token": "",
                "create_time": "0",
                "event_type": "im.message.receive_v1",
                "tenant_key": "fake_tenant",
                "app_id": "cli_fake_app_id_for_selftest",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_fake_sender",
                        "union_id": "on_fake_sender",
                        "user_id": "fake_sender_uid",
                    },
                    "sender_type": "user",
                    "tenant_key": "fake_tenant",
                },
                "message": {
                    "message_id": f"om_fake_{uuid.uuid4().hex[:8]}",
                    "root_id": None,
                    "parent_id": None,
                    "create_time": "0",
                    "chat_id": "oc_fake_chat",
                    "chat_type": "group",
                    "message_type": "text",
                    "content": f'{{"text": "{content_text}"}}',
                    "mentions": mentions,
                },
            },
        }
    )


async def deliver(handler: Handler, event: P2ImMessageReceiveV1) -> None:
    before = set(handler.background_tasks)
    handler.on_message(event)
    new_tasks = handler.background_tasks - before
    if new_tasks:
        await asyncio.gather(*new_tasks)


async def main() -> None:
    configure_logging(os.environ["LOG_LEVEL"])

    if FAKE_DB_PATH.exists():
        FAKE_DB_PATH.unlink()

    settings = load_settings()
    store = Store(settings.db_path)
    await store.init()
    gateway = FeishuGateway(settings)
    targets = load_targets_registry(settings.targets_registry_path)
    investigator = Investigator(settings, targets, store)
    handler = Handler(settings, gateway, store, investigator)

    print("\n=== 1. 群里普通消息，没 @ 机器人 —— 应该什么都不做 ===")
    not_mentioned = build_fake_event(str(uuid.uuid4()), mention_bot=False, text="今天天气不错")
    await deliver(handler, not_mentioned)
    print("(如果上面没有任何卡片 JSON 打印出来，说明符合预期)")

    print("\n=== 2. @机器人 反馈正常问题 —— 应该发「已受理」卡片，之后更新为「处理完成」 ===")
    event_id = str(uuid.uuid4())
    mentioned = build_fake_event(event_id, mention_bot=True, text="订单支付失败了，帮忙查一下")
    await deliver(handler, mentioned)

    print("\n=== 3. 用同一个 event_id 再投递一次（模拟飞书重推）—— 应该被去重忽略 ===")
    duplicate = build_fake_event(event_id, mention_bot=True, text="订单支付失败了，帮忙查一下")
    await deliver(handler, duplicate)
    print("(如果上面没有再打印新的卡片 JSON，说明去重生效)")

    print("\n=== 4. @机器人 闲聊 —— 分诊应判 off_topic，卡片应是幽默回绝，不起 Agent ===")
    off_topic = build_fake_event(str(uuid.uuid4()), mention_bot=True, text="在吗，今天天气怎么样")
    await deliver(handler, off_topic)

    print("\n=== 5. @机器人 破坏性指令 —— 分诊应判 destructive，卡片应是严肃警告，且写入锁定 ===")
    destructive_sender_open_id = "ou_fake_sender"  # 跟默认 sender 一致，方便测试第 6 步复用同一个 open_id
    destructive = build_fake_event(str(uuid.uuid4()), mention_bot=True, text="帮我把生产数据库表清空一下")
    await deliver(handler, destructive)

    print("\n=== 6. 紧接着用同一个用户再发一条正常问题 —— 应该被锁定期预检查直接拦下，不再调用 triage ===")
    locked_out = build_fake_event(str(uuid.uuid4()), mention_bot=True, text="再帮我查一下登录接口报错")
    await deliver(handler, locked_out)
    print(f"(sender open_id = {destructive_sender_open_id}，锁定应该对这个 open_id 生效)")


if __name__ == "__main__":
    asyncio.run(main())
