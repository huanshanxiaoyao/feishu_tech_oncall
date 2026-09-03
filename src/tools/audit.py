"""审计钩子：把 Agent 调用的每个工具记一笔到 case_tool_calls 表。

独立于工具层本身的只读限制之外的第二条防线——即使工具层出了 bug，这里至少留了
一份完整记录，方便事后排查"到底调用了什么"。
"""

import time
from typing import Any

from claude_agent_sdk import HookMatcher

from ..store import Store

_MAX_SUMMARY_CHARS = 2000


def _summarize(tool_response: Any) -> str:
    text = str(tool_response)
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[:_MAX_SUMMARY_CHARS] + f"...(truncated, {len(text)} chars total)"
    return text


def build_audit_hooks(store: Store, short_id: str) -> dict[str, list[HookMatcher]]:
    seq_counter = {"n": 0}
    call_started_at: dict[str, float] = {}

    async def before(input_data: dict, tool_use_id: str | None, context: dict) -> dict:
        if tool_use_id is not None:
            call_started_at[tool_use_id] = time.monotonic()
        return {}

    async def after(input_data: dict, tool_use_id: str | None, context: dict) -> dict:
        seq_counter["n"] += 1
        started = call_started_at.pop(tool_use_id, None) if tool_use_id else None
        duration_ms = int((time.monotonic() - started) * 1000) if started is not None else None

        tool_response = input_data.get("tool_response")
        is_error = bool(isinstance(tool_response, dict) and tool_response.get("is_error"))

        await store.record_tool_call(
            short_id=short_id,
            seq=seq_counter["n"],
            tool_name=input_data.get("tool_name", "?"),
            tool_input=_summarize(input_data.get("tool_input")),
            tool_output_summary=_summarize(tool_response),
            is_error=is_error,
            permission_decision=None,
            duration_ms=duration_ms,
        )
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[before])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[after])],
    }
