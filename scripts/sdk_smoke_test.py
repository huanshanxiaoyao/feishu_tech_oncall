"""Phase 0 冒烟测试：确认这台机器上 Claude Agent SDK 能启动 CLI 子进程并连通 Anthropic API。

跟 fake_event.py 不同，这个脚本会真的发一次请求（几乎零成本的一句话对话，不挂任何自定义工具），
需要 .env 里配置真实的 ANTHROPIC_API_KEY 才能跑通。

用法：
    .venv/bin/python scripts/sdk_smoke_test.py
"""

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query  # noqa: E402

from src.config import load_settings  # noqa: E402


async def main() -> None:
    settings = load_settings()
    has_creds = settings.anthropic_api_key or (settings.anthropic_auth_token and settings.anthropic_base_url)
    if not has_creds:
        print("没配置 ANTHROPIC_API_KEY，也没配置 ANTHROPIC_AUTH_TOKEN+ANTHROPIC_BASE_URL，先在 .env 里填一组再跑这个脚本。")
        return

    env: dict[str, str] = {}
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.anthropic_auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    if settings.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        model=settings.claude_model,
        max_turns=1,
        env=env,
    )

    print(f"正在用模型 {settings.claude_model} 发一句测试消息…")
    async for message in query(prompt="只回复两个字：正常", options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print("模型回复:", block.text)
        elif isinstance(message, ResultMessage):
            print(f"完成，耗时 {message.duration_ms}ms，花费 ${message.total_cost_usd}")
            if message.is_error:
                print("!! 返回了错误状态，subtype =", message.subtype)


if __name__ == "__main__":
    asyncio.run(main())
