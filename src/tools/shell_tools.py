"""只读诊断命令执行工具：run_diagnostic_command。

安全设计（三层）：
1. 拒绝任何 shell 元字符（; | & > < ` $ 换行）——不给管道/重定向/命令拼接/子命令替换留任何空子。
2. shlex.split 之后要求整条命令逐 token 匹配 targets.shell.allowed_commands 里某一条模式
   （fnmatch 逐段比较，不是子串包含，长度必须一致）。
3. 永远用 asyncio.create_subprocess_exec（禁止 shell=True），sudo/su 显式硬拒绝一次（双重保险，
   即便某天白名单被误配置成包含它们也拦得住）。
"""

import asyncio
import fnmatch
import shlex
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..targets import TargetsRegistry

_FORBIDDEN_CHARS = set(";&|<>`$\n\r")
_FORBIDDEN_COMMANDS = {"sudo", "su"}
_MAX_OUTPUT_CHARS = 8000


def build_shell_tools(registry: TargetsRegistry) -> list[SdkMcpTool[Any]]:
    policy = registry.shell
    allowed_patterns = [shlex.split(p) for p in policy.allowed_commands]

    def _error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _matches_any_pattern(tokens: list[str]) -> bool:
        for pattern in allowed_patterns:
            if len(pattern) != len(tokens):
                continue
            if all(fnmatch.fnmatchcase(t, p) for t, p in zip(tokens, pattern)):
                return True
        return False

    @tool(
        "run_diagnostic_command",
        "执行一条只读诊断命令（服务状态/日志/系统资源/网络），必须完全匹配预先配置的命令白名单，"
        "不支持管道/重定向/多条命令拼接",
        {"command": str},
    )
    async def run_diagnostic_command(args: dict) -> dict:
        command = args["command"]
        if any(ch in _FORBIDDEN_CHARS for ch in command):
            return _error("command rejected: contains shell metacharacters (; | & > < ` $ newline)")

        try:
            tokens = shlex.split(command)
        except ValueError as e:
            return _error(f"command rejected: cannot parse ({e})")

        if not tokens:
            return _error("command rejected: empty")
        if tokens[0] in _FORBIDDEN_COMMANDS:
            return _error(f"command rejected: {tokens[0]} is never allowed")
        if not _matches_any_pattern(tokens):
            return _error(f"command rejected: not covered by allowed_commands whitelist: {command!r}")

        try:
            proc = await asyncio.create_subprocess_exec(
                *tokens,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=policy.timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return _error(f"command timed out after {policy.timeout_seconds}s: {command!r}")
        except OSError as e:
            return _error(f"failed to execute command: {e}")

        output = (stdout + stderr).decode("utf-8", errors="replace")
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + f"\n... (输出超过 {_MAX_OUTPUT_CHARS} 字符已截断)"

        return {
            "content": [{"type": "text", "text": output or "(无输出)"}],
            "is_error": proc.returncode != 0,
        }

    return [run_diagnostic_command]
