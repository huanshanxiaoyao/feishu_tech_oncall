"""组装排查 Agent 能用的全部自定义 MCP 工具——唯一的读写边界。

`ClaudeAgentOptions(tools=[])` 会清空 SDK 内置工具（Bash/Read/Write/Edit/WebFetch 等），
Claude 只能用这里注册的自定义工具；"只读"承诺完全靠这里注册了什么工具来保证，不靠 prompt 约束。
"""

from claude_agent_sdk import McpServerConfig, create_sdk_mcp_server

from ..config import Settings
from ..targets import TargetsRegistry
from .db_tools import build_db_tools
from .fs_tools import build_fs_tools
from .log_tools import build_log_tools
from .scratch_tools import build_scratch_tools
from .shell_tools import build_shell_tools

SERVER_NAME = "oncall"


def build_investigation_toolset(
    settings: Settings, targets: TargetsRegistry
) -> tuple[dict[str, McpServerConfig], list[str]]:
    """返回 (mcp_servers, allowed_tools)，直接喂给 ClaudeAgentOptions。"""
    tools = (
        build_scratch_tools(settings.scratch_dir)
        + build_fs_tools(targets)
        + build_log_tools(targets)
        + build_shell_tools(targets)
        + build_db_tools(targets)
    )
    server = create_sdk_mcp_server(name=SERVER_NAME, tools=tools)
    mcp_servers: dict[str, McpServerConfig] = {SERVER_NAME: server}
    allowed_tools = [f"mcp__{SERVER_NAME}__{t.name}" for t in tools]
    return mcp_servers, allowed_tools
