"""收到排查请求后的"指令合理性判断"闸门。

定位：防滥用 + UX 层，不是安全边界——真正的安全边界是 src/tools/ 根本不注册任何
写/删/重启类工具，所以这里哪怕漏判一条破坏性请求，Agent 也没有能力真的执行。
这个定位决定了下面的判断偏向"宁可漏判也不误伤"：解析失败 fail open 到 investigate，
destructive 的判定要求清晰的祈使句式指令，不能只是提到破坏性操作。
"""

import json
from dataclasses import dataclass
from typing import Literal

import structlog
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import Settings
from .investigator import _auth_env

log = structlog.get_logger()

TriageCategory = Literal["investigate", "off_topic", "destructive"]

_SYSTEM_PROMPT = """你是一个线上问题排查机器人（tech_oncall）的分诊闸门，负责的业务是 plum
（前端 plum_chat、管理后台 plum_admin、后端 ai4all_bridge）。收到一条群消息后，判断它属于
三类之一：

- "investigate"：任何跟技术排查、故障、报错、性能、数据异常等相关的正常请求——只要是在
  问"为什么/是不是/能不能查一下"这类事情，哪怕提到了停服/删除等词但只是在描述过去发生的
  事或者怀疑的原因，也算 investigate，不算 destructive。拿不准就归到这一类。
- "off_topic"：明显的闲聊、跟这几个业务的技术排查毫无关系的内容（打招呼、问天气、讲笑话等）。
- "destructive"：清晰的祈使句式指令，要求执行会造成破坏性后果的操作，比如"帮我把 xxx 服务
  停掉/删掉/重启一下生产库/清空 xxx 表"——必须是明确要求执行操作，不能只是提到相关词汇。

用 JSON 输出：{"category": "investigate|off_topic|destructive", "reply_text": "..."}

reply_text 只在 category 不是 investigate 时有意义：
- off_topic 时：用工程师之间聊天的语气回复，带点幽默感，别生硬拒绝，中文，不超过 80 字。
- destructive 时：用严肃语气说明这类操作不受理，并且接下来 1 小时内不再处理这个人的新请求，
  中文，不超过 80 字。
- investigate 时：reply_text 留空字符串即可，不会被使用。
"""

_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["investigate", "off_topic", "destructive"]},
            "reply_text": {"type": "string"},
        },
        "required": ["category", "reply_text"],
    },
}


@dataclass
class TriageResult:
    category: TriageCategory
    reply_text: str = ""


def _dry_run_classify(problem_text: str) -> TriageResult:
    """DRY_RUN 下不真的调用模型，用简单关键词兜底，只为了让 fake_event.py 能覆盖三条路径。"""
    off_topic_hints = ("天气", "笑话", "你好呀", "在吗", "hello", "闲聊两句")
    destructive_hints = ("删掉", "删除", "停掉", "停止", "关闭", "drop table", "rm -rf", "清空")

    if any(k in problem_text for k in off_topic_hints):
        return TriageResult(category="off_topic", reply_text="兄弟，这里是线上排查助手不是天气预报站，有问题再叫我 😄")
    if any(k in problem_text for k in destructive_hints):
        return TriageResult(
            category="destructive",
            reply_text="这类操作我不会执行，接下来 1 小时内也不再处理你的新请求。",
        )
    return TriageResult(category="investigate")


async def classify(problem_text: str, settings: Settings) -> TriageResult:
    if settings.dry_run:
        return _dry_run_classify(problem_text)

    options = ClaudeAgentOptions(
        tools=[],
        allowed_tools=[],
        system_prompt=_SYSTEM_PROMPT,
        max_turns=1,
        output_format=_OUTPUT_SCHEMA,
        model=settings.triage_model or settings.claude_model,
        env=_auth_env(settings),
    )

    try:
        structured: dict | None = None
        raw_text: str | None = None
        async for message in query(prompt=problem_text, options=options):
            if isinstance(message, ResultMessage):
                structured = message.structured_output if isinstance(message.structured_output, dict) else None
                raw_text = message.result

        if structured is None and raw_text:
            try:
                structured = json.loads(raw_text)
            except json.JSONDecodeError:
                structured = None

        if not structured:
            log.warning("triage_no_structured_output", raw_text=raw_text)
            return TriageResult(category="investigate")

        category = structured.get("category")
        if category not in ("investigate", "off_topic", "destructive"):
            log.warning("triage_invalid_category", category=category)
            return TriageResult(category="investigate")

        return TriageResult(category=category, reply_text=structured.get("reply_text") or "")
    except Exception:
        log.exception("triage_failed")
        return TriageResult(category="investigate")
