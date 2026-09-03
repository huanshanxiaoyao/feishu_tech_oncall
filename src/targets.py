"""排查 Agent 能访问哪些代码/日志/数据库/命令的白名单配置。

只读边界的核心防线在 src/tools/ 里的每个工具实现（不注册写工具、每次访问都校验路径包含），
这个模块只负责加载和校验配置本身。配置文件缺失或格式错误时返回一个空注册表并打警告日志，
而不是让整个服务起不来——这样在用户还没填真实目标之前，机器人本身（LLM 推理 + scratch 工具）
也能先跑通。
"""

from pathlib import Path

import structlog
import yaml
from pydantic import BaseModel, Field

log = structlog.get_logger()


class CodeTarget(BaseModel):
    name: str
    root: Path
    readable_globs: list[str] = Field(default_factory=lambda: ["**/*"])
    deny_globs: list[str] = Field(default_factory=lambda: ["**/.env", "**/.git/**", "**/secrets/**"])
    max_file_bytes: int = 200_000


class LogTarget(BaseModel):
    name: str
    path: Path
    pattern: str = "*.log"
    max_lines_per_read: int = 2000


class DatabaseTarget(BaseModel):
    name: str
    engine: str
    dsn_env: str
    readonly_role: bool = True
    query_timeout_seconds: int = 10
    max_rows: int = 500


class ShellPolicy(BaseModel):
    allowed_commands: list[str] = Field(default_factory=list)
    timeout_seconds: int = 15


class TargetsRegistry(BaseModel):
    version: int = 1
    code_targets: list[CodeTarget] = Field(default_factory=list)
    log_targets: list[LogTarget] = Field(default_factory=list)
    databases: list[DatabaseTarget] = Field(default_factory=list)
    shell: ShellPolicy = Field(default_factory=ShellPolicy)

    def resolved(self) -> "TargetsRegistry":
        """把所有 root/path 提前 resolve()，后续工具层每次访问时还要对请求路径
        重新 resolve() 再比对前缀（防御目录内部埋的软链接指向目录外）——这里只是
        把配置本身规范化一次，不能替代每次访问时的校验。"""
        for t in self.code_targets:
            t.root = t.root.resolve()
        for t in self.log_targets:
            t.path = t.path.resolve()
        return self


def load_targets_registry(path: Path) -> TargetsRegistry:
    if not path.exists():
        log.warning("targets_registry_missing", path=str(path))
        return TargetsRegistry()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return TargetsRegistry.model_validate(raw).resolved()
    except Exception:
        log.exception("targets_registry_invalid", path=str(path))
        return TargetsRegistry()
