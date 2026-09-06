from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    feishu_app_id: str
    feishu_app_secret: str
    feishu_bot_open_id: str = ""

    feishu_mode: Literal["ws", "http"] = "ws"
    dry_run: bool = False

    db_path: Path = Path("./data/badcase_bot.db")
    log_level: str = "INFO"

    anthropic_api_key: str = ""
    # 备用鉴权方式：走内部代理（AUTH_TOKEN + BASE_URL）而不是官方 API_KEY。
    # 两者任填一组即可，都留空则 investigator 在真实调用时会失败（被 try/except 兜住，卡片显示失败）。
    anthropic_auth_token: str = ""
    anthropic_base_url: str = ""
    claude_model: str = "claude-sonnet-4-5-20250929"
    # 分诊用的模型，留空则回退到 claude_model；分诊只是单轮零工具的分类调用，
    # 有更便宜/更快的模型可用时可以单独配置。
    triage_model: str = ""
    # 排查资源上限。这几个值是「兜底防跑飞」，不是预期开销——按线上实测校准：
    # 跑完的 case 用掉 19/49/62 轮、$0.35/$0.42/$0.68、14/36/48 次工具调用；
    # 而失败的两次分别是 53 次工具调用烧完 $1、以及撞上 90 轮上限。
    # 也就是说旧的 90 轮 / $1 只有实测成功值的 1.5 倍余量，稍微复杂一点的问题就会
    # 在「差一点就查出来」的地方被砍断。放宽到约 3~4 倍余量。
    agent_max_turns: int = 180
    agent_max_budget_usd: float | None = 3.0
    # 实测约 4.5 秒/轮，180 轮约需 810 秒，超时上限要留在这之上，
    # 否则轮次预算还没用完就先被超时砍掉。
    agent_timeout_seconds: int = 1200
    agent_progress_update_interval_seconds: int = 20
    targets_registry_path: Path = Path("./config/targets.yaml")
    scratch_dir: Path = Path("./data/scratch")
    lockout_duration_seconds: int = 3600

    # 收到 SIGTERM 后，留给"还在跑的排查"的收尾时间。超过就 cancel，
    # 被 cancel 的 case 会写库 + 更新卡片，不会留一张永远停在「已受理」的卡片。
    # 必须显著小于 systemd 的 TimeoutStopSec，否则还是会被 SIGKILL。
    shutdown_grace_seconds: int = 20


def load_settings() -> Settings:
    return Settings()
