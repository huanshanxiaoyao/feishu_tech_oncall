import asyncio
import logging
import os
import signal
import sys
import threading

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
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _shutdown(loop, client, handler, settings, log, *, exit_code: int) -> None:
    """尽力优雅关停，最后兜底强制退出。

    背景：lark 的 `client.start()` 结尾是 `loop.run_until_complete(_select())`
    （一个 `while True: await asyncio.sleep(3600)`）。`loop.stop()` 能让它返回，
    但 WS 连接本身不会关，而且实测进程里还会多出非 daemon 线程——解释器退出时
    会去 join 它们，于是进程一直挂到 systemd 的 TimeoutStopSec 才被 SIGKILL。
    这个函数按「先收尾、再关连接、最后兜底」的顺序把这条路走干净。
    """
    # 1. 给还在跑的排查一点收尾时间。loop.stop() 只是停掉了上一次 run，
    #    loop 本身没关，这里可以继续 run_until_complete。
    pending = [t for t in handler.background_tasks if not t.done()]
    if pending:
        log.info(
            "waiting_for_background_tasks",
            count=len(pending),
            grace_seconds=settings.shutdown_grace_seconds,
        )
        loop.run_until_complete(asyncio.wait(pending, timeout=settings.shutdown_grace_seconds))

    # 2. 宽限期内没跑完的直接 cancel。Handler._process 会接住 CancelledError，
    #    把 case 落库并把卡片更新成「已中断」——注意这些收尾动作要靠下面这次
    #    gather 把 loop 继续跑起来才能完成。
    unfinished = [t for t in handler.background_tasks if not t.done()]
    if unfinished:
        log.warning("cancelling_unfinished_tasks", count=len(unfinished))
        for task in unfinished:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*unfinished, return_exceptions=True))

    # 3. 关 WS 连接。SDK 没暴露公开的关闭入口，只能用私有的 _disconnect()；
    #    包在 try 里，SDK 换实现时最多退化成「关不干净」，不能让关停流程本身崩掉。
    try:
        loop.run_until_complete(client._disconnect())
    except Exception:
        log.warning("ws_disconnect_failed", exc_info=True)

    # 4. 收掉 lark 挂在 loop 上的常驻任务（_receive_message_loop / _ping_loop /
    #    ExpiringCache._start_clear_cron）。不显式收的话，loop.close() 之后每次重启
    #    都会刷三条 "Task was destroyed but it is pending!"，外加 ExpiringCache.__del__
    #    在已关闭的 loop 上调 cancel() 抛的 RuntimeError traceback。
    #    先 cancel 再 gather 让它们真正结束——任务 done 之后 Task.cancel() 会直接
    #    返回 False 而不去碰 loop，__del__ 那条 traceback 也就跟着没了。
    #    这是个帮人看日志的机器人，自己的日志不能先脏。
    leftover = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if leftover:
        log.info("cancelling_sdk_tasks", count=len(leftover))
        for task in leftover:
            task.cancel()
        loop.run_until_complete(asyncio.gather(*leftover, return_exceptions=True))

    # 5. 收异步生成器和 asyncio 默认线程池（aiosqlite / getaddrinfo 都会用到它）。
    try:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
    except Exception:
        log.warning("loop_cleanup_failed", exc_info=True)
    finally:
        loop.close()

    log.info("shutdown_complete")

    # 6. 兜底。到这一步还活着的非 daemon 线程会在解释器退出时被 join，从而卡死进程。
    #    先把线程名打出来——这是定位「到底谁不肯退」的唯一线索（py-spy 需要 ptrace
    #    权限，这台机器上用不了），然后强制退出。
    #    强制退出是安全的：所有状态都在 SQLite 里，且每个 store 方法都是独立
    #    connect/commit/close，没有攒在内存里等落盘的东西。
    lingering = [
        t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon
    ]
    if lingering:
        log.warning(
            "forcing_exit_lingering_threads",
            threads=sorted(t.name for t in lingering),
            exit_code=exit_code,
        )
    sys.stdout.flush()
    sys.stderr.flush()
    if lingering:
        os._exit(exit_code)


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
    failure: BaseException | None = None
    try:
        client.start()
    except RuntimeError as exc:
        # loop.stop() 会让 client.start() 里的 run_until_complete 抛
        # "Event loop stopped before Future completed."——这是我们自己触发的，不是故障。
        if not shutdown_requested:
            failure = exc
    except BaseException as exc:  # noqa: BLE001 - 无论怎么挂都要先把关停流程走完
        failure = exc

    if failure is not None:
        log.error("client_start_failed", exc_info=failure)

    _shutdown(loop, client, handler, settings, log, exit_code=1 if failure else 0)

    # _shutdown 有残留线程时会直接 os._exit，走不到这里；没残留时正常把异常抛出去，
    # 让 systemd 看到非零退出码。
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
