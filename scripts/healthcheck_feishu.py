"""飞书链路健康检查：只读，不发消息。

检查项：
1. app_id/app_secret 能否换到 tenant_access_token
2. 机器人自身信息（bot open_id 是否与 .env 里配的一致）
3. 机器人当前所在的群列表（确认还在目标群里）

用法：.venv/bin/python scripts/healthcheck_feishu.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lark_oapi as lark

from src.config import load_settings


def main() -> int:
    settings = load_settings()
    client = (
        lark.Client.builder()
        .app_id(settings.feishu_app_id)
        .app_secret(settings.feishu_app_secret)
        .build()
    )

    print(f"app_id            = {settings.feishu_app_id}")
    print(f"配置的 bot open_id = {settings.feishu_bot_open_id}")
    print("-" * 60)

    # 1) 机器人自身信息（SDK 会自动用 app_id/app_secret 换 tenant_access_token，
    #    凭证不对这一步就会失败，等于顺带验证了鉴权）
    req = (
        lark.BaseRequest.builder()
        .http_method(lark.HttpMethod.GET)
        .uri("/open-apis/bot/v3/info")
        .token_types({lark.AccessTokenType.TENANT})
        .build()
    )
    resp = client.request(req)
    print(f"[1] bot/v3/info: HTTP {resp.raw.status_code}")
    if resp.raw and resp.raw.content:
        import json

        data = json.loads(resp.raw.content)
        print(f"    code={data.get('code')} msg={data.get('msg')}")
        bot = data.get("bot") or {}
        if bot:
            print(f"    name={bot.get('app_name')}  open_id={bot.get('open_id')}")
            if settings.feishu_bot_open_id and bot.get("open_id") != settings.feishu_bot_open_id:
                print("    !! open_id 与 .env 不一致 —— @ 机器人将永远匹配不上，消息会被静默丢弃")

    # 2) 机器人所在群
    req = (
        lark.BaseRequest.builder()
        .http_method(lark.HttpMethod.GET)
        .uri("/open-apis/im/v1/chats?page_size=20")
        .token_types({lark.AccessTokenType.TENANT})
        .build()
    )
    resp = client.request(req)
    print(f"[2] im/v1/chats (机器人所在群): HTTP {resp.raw.status_code}")
    if resp.raw and resp.raw.content:
        import json

        data = json.loads(resp.raw.content)
        print(f"    code={data.get('code')} msg={data.get('msg')}")
        items = ((data.get("data") or {}).get("items")) or []
        if not items:
            print("    (没有群，或缺少 im:chat:readonly 权限)")
        for it in items:
            print(f"    - {it.get('chat_id')}  {it.get('name')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
