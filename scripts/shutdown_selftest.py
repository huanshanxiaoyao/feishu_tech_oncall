"""关停路径自测：模拟「排查跑到一半收到 SIGTERM」，验证 main._shutdown 的行为。

刻意不连真飞书——本地再开一条 WS 会和生产实例抢同一个 app 的长连接，
所以这里用一个 stub client 顶替 lark.ws.Client（_shutdown 只用到它的 _disconnect）。

验证三件事：
  1. 宽限期内没跑完的排查会被 cancel，而不是拖着进程等 systemd 来 SIGKILL
  2. 被 cancel 的 case 会落库成 failed + 「已中断」文案，卡片会被更新（DRY_RUN 下打印出来）
  3. 整个关停在宽限期 + 少量开销内结束，并报告是否有赖着不退的非 daemon 线程

用法：.venv/bin/python scripts/shutdown_selftest.py
"""

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SHUTDOWN_DB_PATH = REPO_ROOT / "data" / "shutdown_selftest.db"
GRACE_SECONDS = 3
ABORT_TEXT = "服务重启，本次排查已中断，请重新 @ 我发起一次。"


class _StubWsClient:
    """_shutdown 只会调 _disconnect()，够用了。"""

    def __init__(self) -> None:
        self.disconnected = False

    async def _disconnect(self) -> None:
        self.disconnected = True


async def _scenario(build_fake_event):
    """把一个「永远跑不完的排查」推进 handler，然后返回，交给 _shutdown 收场。"""
    from src.config import load_settings
    from src.feishu import FeishuGateway
    from src.handler import Handler
    from src.investigator import Investigator
    from src.store import Store
    from src.targets import load_targets_registry

    settings = load_settings()
    store = Store(settings.db_path)
    await store.init()
    gateway = FeishuGateway(settings)
    targets = load_targets_registry(settings.targets_registry_path)
    investigator = Investigator(settings, targets, store)

    # 让排查"卡住"：DRY_RUN 下 Investigator.run 本来秒回，这里换成一个长睡眠，
    # 模拟真实排查跑到一半（能撑过宽限期）。
    async def _never_finishes(*args, **kwargs):
        await asyncio.sleep(3600)

    investigator.run = _never_finishes

    handler = Handler(settings, gateway, store, investigator)
    handler.on_message(build_fake_event(str(uuid.uuid4()), mention_bot=True, text="订单支付失败了，帮忙查一下"))

    # 等「已受理」卡片发出、排查真正进入睡眠，模拟 SIGTERM 打在排查中途
    await asyncio.sleep(1)
    return handler, settings


def _run_child() -> None:
    # 先 import fake_event —— 它在模块级会硬设 DB_PATH/DRY_RUN 等环境变量，
    # 必须赶在 load_settings() 之前 import 掉，然后把我们要的 DB_PATH 覆盖回来，
    # 否则这个自测会写到 fake_event 自己的库里去。
    saved_db_path = os.environ["DB_PATH"]
    from scripts.fake_event import build_fake_event

    os.environ["DB_PATH"] = saved_db_path

    import structlog

    from src.main import _shutdown, configure_logging

    configure_logging("INFO")
    log = structlog.get_logger()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    handler, settings = loop.run_until_complete(_scenario(build_fake_event))

    alive = [t for t in handler.background_tasks if not t.done()]
    print(f"[child] SIGTERM 前在跑的排查任务数 = {len(alive)}", flush=True)

    started = time.monotonic()
    _shutdown(loop, _StubWsClient(), handler, settings, log, exit_code=0)
    # 有残留非 daemon 线程时 _shutdown 会 os._exit，走不到这一行
    print(f"[child] 关停耗时 {time.monotonic() - started:.2f}s，无残留线程，正常退出", flush=True)


def _verify() -> int:
    import sqlite3

    db = sqlite3.connect(SHUTDOWN_DB_PATH)
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("select short_id, status, error_text from cases")]
    print("\n=== 库里的 case ===")
    for r in rows:
        print(f"  {r}")

    if not rows:
        print("!! 失败：一条 case 都没有，被中断的请求在库里没留痕")
        return 1
    bad = [r for r in rows if r["status"] != "failed" or r["error_text"] != ABORT_TEXT]
    if bad:
        print(f"!! 失败：case 状态不是预期的 failed/「已中断」：{bad}")
        return 1
    print("✓ 被中断的 case 已落库成 failed + 「已中断」文案")
    return 0


def main() -> int:
    if "--child" in sys.argv:
        _run_child()
        return 0

    if SHUTDOWN_DB_PATH.exists():
        SHUTDOWN_DB_PATH.unlink()

    env = dict(os.environ)
    env["DB_PATH"] = str(SHUTDOWN_DB_PATH)
    env["SHUTDOWN_GRACE_SECONDS"] = str(GRACE_SECONDS)
    env["DRY_RUN"] = "1"
    env["FEISHU_BOT_OPEN_ID"] = "ou_fake_bot_open_id"
    env.setdefault("FEISHU_APP_ID", "cli_fake_app_id_for_selftest")
    env.setdefault("FEISHU_APP_SECRET", "fake_secret_for_selftest")

    print(f"宽限期设为 {GRACE_SECONDS}s，排查任务会一直跑不完 —— 预期被 cancel 而不是拖死进程\n")
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, __file__, "--child"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - started

    for line in proc.stdout.splitlines():
        if any(k in line for k in ("[child]", "cancelling_unfinished", "waiting_for_background",
                                   "processing_cancelled", "forcing_exit_lingering",
                                   "shutdown_complete", "update_card", "accepted_card_sent")):
            print(f"  {line}")
    if proc.stderr.strip():
        print("--- stderr ---")
        print(proc.stderr[-2000:])

    print(f"\n子进程总耗时 {elapsed:.2f}s，退出码 {proc.returncode}")
    failed = 0
    if proc.returncode != 0:
        print("!! 失败：子进程退出码非 0")
        failed = 1
    # 宽限期 + 收尾开销；真卡住的话这里会顶到 subprocess 的 120s timeout
    if elapsed > GRACE_SECONDS + 25:
        print(f"!! 失败：关停耗时 {elapsed:.2f}s，远超宽限期，说明还是卡住了")
        failed = 1
    else:
        print(f"✓ 关停在宽限期 + 收尾开销内完成，没有卡死")

    return _verify() or failed


if __name__ == "__main__":
    raise SystemExit(main())
