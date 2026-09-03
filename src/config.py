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
    agent_max_turns: int = 90
    agent_max_budget_usd: float | None = 1.0
    agent_timeout_seconds: int = 600
    agent_progress_update_interval_seconds: int = 20
    targets_registry_path: Path = Path("./config/targets.yaml")
    scratch_dir: Path = Path("./data/scratch")
    lockout_duration_seconds: int = 3600


def load_settings() -> Settings:
    return Settings()
