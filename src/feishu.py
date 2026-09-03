import json
import uuid
from typing import Awaitable, Callable

import lark_oapi as lark
from lark_oapi.api.contact.v3 import GetUserRequest
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from .config import Settings

logger = lark.logger


class FeishuGateway:
    """飞书 API 封装：发卡片 / 更新卡片。DRY_RUN=1 时不真的调用飞书，只打印卡片 JSON。"""

    def __init__(self, settings: Settings):
        self._dry_run = settings.dry_run
        self._client = lark.Client.builder().app_id(settings.feishu_app_id).app_secret(
            settings.feishu_app_secret
        ).build()

    async def send_card(self, chat_id: str, card: dict) -> str:
        if self._dry_run:
            fake_message_id = f"dryrun_msg_{uuid.uuid4().hex[:8]}"
            print(json.dumps({"action": "send_card", "chat_id": chat_id, "message_id": fake_message_id, "card": card}, ensure_ascii=False, indent=2))
            return fake_message_id

        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .uuid(str(uuid.uuid4()))
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.acreate(request)
        if not response.success():
            raise RuntimeError(
                f"send_card failed, code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
            )
        return response.data.message_id

    async def update_card(self, message_id: str, card: dict) -> None:
        if self._dry_run:
            print(json.dumps({"action": "update_card", "message_id": message_id, "card": card}, ensure_ascii=False, indent=2))
            return

        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            .build()
        )
        response = await self._client.im.v1.message.apatch(request)
        if not response.success():
            raise RuntimeError(
                f"update_card failed, code={response.code}, msg={response.msg}, log_id={response.get_log_id()}"
            )

    async def get_user_name(self, open_id: str) -> str | None:
        """查发送人姓名。事件本身不带姓名字段，需要一次 contact.v3.user.get 查询，
        需要应用有"获取用户基本信息"权限，没有权限或查询失败时返回 None（不影响主流程）。"""
        if self._dry_run:
            return "(dry-run 用户)"

        request = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
        try:
            response = await self._client.contact.v3.user.aget(request)
            if response.success() and response.data and response.data.user:
                return response.data.user.name
            logger.warning(
                "get_user_name failed, code=%s, msg=%s", response.code, response.msg
            )
        except Exception:
            logger.exception("get_user_name raised")
        return None


def build_ws_client(settings: Settings, on_message: Callable[[lark.im.v1.P2ImMessageReceiveV1], None]) -> lark.ws.Client:
    """长连接模式：构造事件分发器 + WS 客户端。encrypt_key/verification_token 在
    长连接模式下不参与校验（SDK 走 _do_without_validation），传空串即可。"""
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )
    return lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )


async def run_http_callback_server(settings: Settings, on_message: Callable[..., Awaitable[None]]) -> None:
    """HTTP 回调模式：预留位置，本次未实现。

    TODO（第二步或需要切换到回调模式时再做）：
    - 起一个 HTTP server（比如用 aiohttp/FastAPI）监听事件回调端口
    - 用 lark.EventDispatcherHandler.builder(encrypt_key, verification_token) 校验签名/解密
    - 处理 url_verification 挑战请求
    - 收到 im.message.receive_v1 事件后调用 on_message，逻辑与 ws 模式共用 handler.py
    - 需要公网 IP / 域名 / HTTPS 证书，nginx 反代配置需先经用户确认（见 CLAUDE.md）
    """
    raise NotImplementedError("FEISHU_MODE=http 尚未实现，第一步只做长连接模式")
