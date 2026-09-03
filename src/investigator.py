"""用 Claude Agent SDK 驱动一次真实排查。

只读边界完全在 src/tools/ 里：这里把 `tools=[]`（清空 SDK 内置工具）、
`allowed_tools`（只允许我们注册的自定义工具）、`can_use_tool`（对其余一切默认拒绝）
三者叠在一起，任何一层出问题另外两层还能兜住。
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultDeny,
    ResultMessage,
    ToolPermissionContext,
    ToolUseBlock,
    query,
)

from .config import Settings
from .store import Store
from .targets import TargetsRegistry
from .tools import build_investigation_toolset
from .tools.audit import build_audit_hooks

log = structlog.get_logger()

SYSTEM_PROMPT = """你是一个线上问题排查助手，正在协助排查一个真实的生产问题。

你只能使用系统提供给你的工具（都是只读的，除了一个用于记笔记的临时目录）。不要假设你
能访问工具之外的任何东西。请自主判断该查看代码、数据库、日志还是系统/网络状态——不要
先问用户要查什么，直接动手排查。

当前可能只配置了少量工具/目标，如果需要的数据源还没有开放，就在报告里明确指出"缺少
XXX 的访问权限，无法进一步确认"，而不是编造结论。

排查结束后，用中文输出一份结构化报告，包含三部分：
1. 根因（如果还不能确定，说明当前最可能的几个方向及各自的置信度）
2. 证据（引用你实际查到的文件/日志/数据，不要凭空编造）
3. 建议（下一步可以做什么，包括需要人工介入的部分）
"""

ProgressCallback = Callable[[str], Awaitable[None]]

_TOOL_LABELS = {
    "read_file": "读取代码",
    "list_dir": "浏览目录",
    "grep_code": "搜索代码",
    "tail_log": "读取日志",
    "grep_log": "搜索日志",
    "run_diagnostic_command": "执行诊断命令",
    "run_query": "查询数据库",
    "write_scratch": "写入临时文件",
    "read_scratch": "读取临时文件",
}


@dataclass
class InvestigationResult:
    status: Literal["done", "failed", "timeout"]
    report_text: str | None = None
    error_text: str | None = None
    session_id: str | None = None
    turns_used: int | None = None
    cost_usd: float | None = None


def _auth_env(settings: Settings) -> dict[str, str]:
    """把配置里的鉴权信息显式塞进子进程 env，不依赖调用方进程的 os.environ 里
    是否已经有这几个变量（systemd EnvironmentFile 会有，本地脚本直接跑不一定有）。"""
    env: dict[str, str] = {}
    if settings.anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
    if settings.anthropic_auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    if settings.anthropic_base_url:
        env["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    return env


async def _deny_by_default(
    tool_name: str, tool_input: dict, context: ToolPermissionContext
) -> PermissionResultDeny:
    log.warning("tool_use_denied_by_default", tool_name=tool_name)
    return PermissionResultDeny(message=f"tool not allowed: {tool_name}")


def _describe_tool_use(block: ToolUseBlock) -> str:
    short_name = block.name.rsplit("__", 1)[-1]
    label = _TOOL_LABELS.get(short_name, short_name)
    target = block.input.get("target") if isinstance(block.input, dict) else None
    return f"{label}: {target}" if target else label


class Investigator:
    def __init__(self, settings: Settings, targets: TargetsRegistry, store: Store):
        self.settings = settings
        self.targets = targets
        self.store = store

    async def run(
        self,
        *,
        short_id: str,
        problem_text: str,
        on_progress: ProgressCallback | None = None,
    ) -> InvestigationResult:
        if self.settings.dry_run:
            return InvestigationResult(
                status="done",
                report_text=f"[DRY_RUN] 未真实调用 Agent。问题描述：{problem_text}",
                session_id=None,
                turns_used=0,
                cost_usd=0.0,
            )

        mcp_servers, allowed_tools = build_investigation_toolset(self.settings, self.targets)
        hooks = build_audit_hooks(self.store, short_id)

        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=allowed_tools,
            mcp_servers=mcp_servers,
            can_use_tool=_deny_by_default,
            system_prompt=SYSTEM_PROMPT,
            max_turns=self.settings.agent_max_turns,
            max_budget_usd=self.settings.agent_max_budget_usd,
            cwd=str(self.settings.scratch_dir),
            hooks=hooks,
            model=self.settings.claude_model,
            env=_auth_env(self.settings),
        )

        try:
            return await asyncio.wait_for(
                self._run_query(problem_text, options, on_progress),
                timeout=self.settings.agent_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("investigation_timeout", short_id=short_id)
            return InvestigationResult(status="timeout", error_text="排查超时，已终止")
        except Exception:
            log.exception("investigation_failed", short_id=short_id)
            return InvestigationResult(status="failed", error_text="排查过程中出现内部错误，请联系管理员查看日志")

    async def _run_query(
        self,
        problem_text: str,
        options: ClaudeAgentOptions,
        on_progress: ProgressCallback | None,
    ) -> InvestigationResult:
        session_id: str | None = None
        last_result: ResultMessage | None = None
        last_progress_at = time.monotonic()

        async for message in query(prompt=problem_text, options=options):
            if isinstance(message, AssistantMessage):
                session_id = message.session_id or session_id
                for block in message.content:
                    if not isinstance(block, ToolUseBlock):
                        continue
                    now = time.monotonic()
                    if (
                        on_progress is not None
                        and now - last_progress_at >= self.settings.agent_progress_update_interval_seconds
                    ):
                        last_progress_at = now
                        await on_progress(_describe_tool_use(block))
            elif isinstance(message, ResultMessage):
                last_result = message

        if last_result is None:
            return InvestigationResult(status="failed", error_text="Agent 未返回结果", session_id=session_id)

        if last_result.is_error:
            return InvestigationResult(
                status="failed",
                error_text=f"排查失败（{last_result.subtype}）",
                session_id=last_result.session_id,
                turns_used=last_result.num_turns,
                cost_usd=last_result.total_cost_usd,
            )

        return InvestigationResult(
            status="done",
            report_text=last_result.result or "(Agent 未生成报告文本)",
            session_id=last_result.session_id,
            turns_used=last_result.num_turns,
            cost_usd=last_result.total_cost_usd,
        )
