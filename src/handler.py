import asyncio
import contextlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import structlog

from . import triage
from .cards import (
    build_accepted_card,
    build_decline_card,
    build_done_card,
    build_failed_card,
    build_locked_out_card,
    build_progress_card,
    build_warning_card,
)
from .config import Settings
from .feishu import FeishuGateway
from .investigator import Investigator
from .store import Store

log = structlog.get_logger()

_LOCKOUT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 服务收到 SIGTERM 时被砍掉的排查。区别于 investigator 内部的失败：这不是排查本身
# 出了问题，而是运维动作打断的，所以文案要引导用户重发而不是去找管理员看日志。
_SHUTDOWN_ABORT_TEXT = "服务重启，本次排查已中断，请重新 @ 我发起一次。"


@dataclass
class _Request:
    """一次排查请求在「已受理卡片发出之后」需要一路带下去的上下文。

    抽出来是为了让 `_process` 能在最外层接住 CancelledError 并做收尾，
    而不用把十来个局部变量都摊在同一个函数里。
    """

    event_id: str
    chat_id: str
    open_id: str
    message_id: str
    short_id: str
    raw_text: str
    card_message_id: str


def _remaining_minutes(locked_until: str) -> int:
    until = datetime.strptime(locked_until, _LOCKOUT_TIME_FORMAT)
    remaining_seconds = (until - datetime.utcnow()).total_seconds()
    return max(1, int(remaining_seconds // 60) + 1)


class Handler:
    """消息事件处理主逻辑。

    `on_message` 是同步入口（飞书 SDK 的事件分发链路是同步调用的），
    只做「是否 @ 我 + 是否重复」的快速判断，实际处理丢进 asyncio 后台任务，
    立刻返回，不阻塞事件回调。
    """

    def __init__(self, settings: Settings, gateway: FeishuGateway, store: Store, investigator: Investigator):
        self._settings = settings
        self._gateway = gateway
        self._store = store
        self._investigator = investigator
        # 持有后台任务的强引用，防止被 GC 提前回收；main.py 用它来做优雅退出。
        self.background_tasks: set[asyncio.Task] = set()

    def on_message(self, event) -> None:
        header = event.header
        body = event.event
        if header is None or body is None or body.message is None:
            log.warning("event_missing_fields")
            return

        event_id = header.event_id
        message = body.message
        chat_id = message.chat_id
        message_id = message.message_id

        if not self._is_bot_mentioned(message):
            log.debug("ignored_not_mentioned", event_id=event_id, chat_id=chat_id)
            return

        task = asyncio.create_task(self._process(event_id, chat_id, message, body.sender))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    def _is_bot_mentioned(self, message) -> bool:
        if not self._settings.feishu_bot_open_id:
            # 没配置机器人 open_id 就没法判断，宁可不响应也不要误触发。
            return False
        mentions = message.mentions or []
        return any(
            m.id and m.id.open_id == self._settings.feishu_bot_open_id for m in mentions
        )

    async def _process(self, event_id: str, chat_id: str, message, sender) -> None:
        bound_log = log.bind(event_id=event_id, chat_id=chat_id)

        if await self._store.event_already_seen(event_id):
            bound_log.info("duplicate_event_ignored")
            return

        raw_text = _extract_text(message.content)
        open_id = sender.sender_id.open_id if sender and sender.sender_id else ""
        message_id = message.message_id
        short_id = uuid.uuid4().hex[:8]

        bound_log.info("processing_start", short_id=short_id, raw_text=raw_text)

        accepted_card = build_accepted_card(short_id, raw_text)
        card_message_id = await self._gateway.send_card(chat_id, accepted_card)
        bound_log.info("accepted_card_sent", short_id=short_id, card_message_id=card_message_id)

        request = _Request(
            event_id=event_id,
            chat_id=chat_id,
            open_id=open_id,
            message_id=message_id,
            short_id=short_id,
            raw_text=raw_text,
            card_message_id=card_message_id,
        )

        try:
            await self._run_case(request, bound_log)
        except asyncio.CancelledError:
            # 关停时 main.py 会 cancel 掉没跑完的任务。此时 event loop 还在跑
            # （见 main._shutdown 的最后一次 gather），所以这里的 await 仍然有效，
            # 能把 case 落库、把卡片从「已受理」更新成「已中断」，
            # 不会留一张永远转圈的卡片。
            bound_log.warning("processing_cancelled_by_shutdown", short_id=short_id)
            await self._abort_case(request)
            raise

    async def _abort_case(self, request: _Request) -> None:
        """被关停打断时的收尾。已经在异常路径上了，这里的任何失败都不该再往外抛。"""
        # 绝大多数中断发生在排查过程中（此时 case 早就建好了），但也可能卡在
        # 更靠前的 get_user_name/triage 上——那时 case 还不存在，直接 UPDATE 会打空，
        # 请求就在库里彻底没留痕。所以先补一次 create（已存在会抛主键冲突，忽略掉）。
        with contextlib.suppress(Exception):
            await self._store.create_case(short_id=request.short_id, problem_text=request.raw_text)
        with contextlib.suppress(Exception):
            await self._store.complete_case(
                request.short_id, status="failed", error_text=_SHUTDOWN_ABORT_TEXT
            )
        with contextlib.suppress(Exception):
            await self._gateway.update_card(
                request.card_message_id,
                build_failed_card(request.short_id, request.raw_text, _SHUTDOWN_ABORT_TEXT),
            )

    async def _run_case(self, request: _Request, bound_log) -> None:
        short_id = request.short_id
        raw_text = request.raw_text
        open_id = request.open_id
        card_message_id = request.card_message_id

        user_name = await self._gateway.get_user_name(open_id)

        await self._store.save_message(
            short_id=short_id,
            event_id=request.event_id,
            chat_id=request.chat_id,
            open_id=open_id,
            user_name=user_name,
            message_id=request.message_id,
            raw_text=raw_text,
        )
        bound_log.info("message_stored", short_id=short_id)

        lockout = await self._store.get_active_lockout(open_id)
        if lockout is not None:
            remaining_minutes = _remaining_minutes(lockout["locked_until"])
            await self._store.create_case(short_id=short_id, problem_text=raw_text)
            await self._store.complete_case(short_id, status="rejected_locked_out", error_text="命中破坏性指令锁定期")
            await self._gateway.update_card(card_message_id, build_locked_out_card(short_id, remaining_minutes))
            bound_log.info("processing_done", short_id=short_id, status="rejected_locked_out")
            return

        triage_result = await triage.classify(raw_text, self._settings)
        if triage_result.category == "off_topic":
            await self._store.create_case(short_id=short_id, problem_text=raw_text)
            await self._store.complete_case(
                short_id, status="rejected_off_topic", report_text=triage_result.reply_text
            )
            await self._gateway.update_card(
                card_message_id, build_decline_card(short_id, raw_text, triage_result.reply_text)
            )
            bound_log.info("processing_done", short_id=short_id, status="rejected_off_topic")
            return
        if triage_result.category == "destructive":
            locked_until = (
                datetime.utcnow() + timedelta(seconds=self._settings.lockout_duration_seconds)
            ).strftime(_LOCKOUT_TIME_FORMAT)
            await self._store.set_lockout(
                open_id=open_id, locked_until=locked_until, reason=raw_text[:200], case_short_id=short_id
            )
            await self._store.create_case(short_id=short_id, problem_text=raw_text)
            await self._store.complete_case(
                short_id, status="rejected_destructive", report_text=triage_result.reply_text
            )
            await self._gateway.update_card(
                card_message_id, build_warning_card(short_id, raw_text, triage_result.reply_text)
            )
            bound_log.info("processing_done", short_id=short_id, status="rejected_destructive")
            return

        await self._store.create_case(short_id=short_id, problem_text=raw_text)
        started_at = time.monotonic()

        async def _on_progress(action: str) -> None:
            await self._gateway.update_card(
                card_message_id, build_progress_card(short_id, raw_text, action)
            )

        try:
            result = await self._investigator.run(
                short_id=short_id, problem_text=raw_text, on_progress=_on_progress
            )
        except Exception:
            bound_log.exception("investigation_crashed", short_id=short_id)
            result = None

        duration_seconds = time.monotonic() - started_at

        if result is None:
            await self._store.complete_case(short_id, status="failed", error_text="internal error")
            final_card = build_failed_card(short_id, raw_text, "排查过程中出现内部错误，请联系管理员查看日志")
        elif result.status == "done":
            await self._store.complete_case(
                short_id,
                status="done",
                report_text=result.report_text,
                turns_used=result.turns_used,
                cost_usd=result.cost_usd,
            )
            tool_call_count = await self._store.count_tool_calls(short_id)
            final_card = build_done_card(
                short_id=short_id,
                report_text=result.report_text or "(无报告内容)",
                tool_call_count=tool_call_count,
                duration_seconds=duration_seconds,
            )
        else:
            await self._store.complete_case(
                short_id,
                status=result.status,
                error_text=result.error_text,
                turns_used=result.turns_used,
                cost_usd=result.cost_usd,
            )
            final_card = build_failed_card(short_id, raw_text, result.error_text or "排查失败")

        await self._gateway.update_card(card_message_id, final_card)
        bound_log.info("processing_done", short_id=short_id, status=result.status if result else "crashed")


def _extract_text(content: str | None) -> str:
    """飞书文本消息 content 是形如 {"text": "..."} 的 JSON 字符串。"""
    if not content:
        return ""
    try:
        data = json.loads(content)
        return data.get("text", content)
    except (json.JSONDecodeError, AttributeError):
        return content
