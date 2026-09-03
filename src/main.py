import asyncio
import logging
import signal

import structlog

from .config import load_settings
from .feishu import FeishuGateway, build_ws_client
from .handler import Handler
from .investigator import Investigator
from .store import Store
from .targets import load_targets_registry


def configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    log = structlog.get_logger()

    if settings.feishu_mode == "http":
        log.error("http_mode_not_implemented")
        raise NotImplementedError(
            "FEISHU_MODE=http 尚未实现，第一步只做长连接模式，"
            "见 feishu.run_http_callback_server 的 TODO"
        )

    store = Store(settings.db_path)
    gateway = FeishuGateway(settings)
    targets = load_targets_registry(settings.targets_registry_path)
    investigator = Investigator(settings, targets, store)
    handler = Handler(settings, gateway, store, investigator)
    client = build_ws_client(settings, handler.on_message)

    # 复用 lark_oapi.ws.client 模块在 import 时已经创建好的那个 event loop——
    # client.start() 内部认的是它模块级变量捕获的那一个 loop 对象，不能自己另起一个。
    loop = asyncio.get_event_loop()
    loop.run_until_complete(store.init())

    shutdown_requested = False

    def _handle_signal(sig_name: str) -> None:
        nonlocal shutdown_requested
        shutdown_requested = True
        log.info("shutdown_signal_received", signal=sig_name)
        loop.stop()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        loop.add_signal_handler(sig, _handle_signal, name)

    log.info("starting", mode=settings.feishu_mode, dry_run=settings.dry_run)
    try:
        client.start()
    except RuntimeError:
        if not shutdown_requested:
            raise
    finally:
        pending = [t for t in handler.background_tasks if not t.done()]
        if pending:
            log.info("waiting_for_background_tasks", count=len(pending))
            loop.run_until_complete(asyncio.wait(pending, timeout=10))
        log.info("shutdown_complete")


if __name__ == "__main__":
    main()
