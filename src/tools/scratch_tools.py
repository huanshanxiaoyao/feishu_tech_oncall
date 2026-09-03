"""全系统唯一的写工具：读写限定在 settings.scratch_dir 内。"""

from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ._pathsafety import PathEscapeError, resolve_within


def build_scratch_tools(scratch_dir: Path) -> list[SdkMcpTool[Any]]:
    scratch_dir.mkdir(parents=True, exist_ok=True)

    @tool("write_scratch", "把内容写到临时目录下的一个文件（唯一有写权限的地方）", {"path": str, "content": str})
    async def write_scratch(args: dict) -> dict:
        try:
            target = resolve_within(scratch_dir, args["path"])
        except PathEscapeError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args["content"], encoding="utf-8")
        return {"content": [{"type": "text", "text": f"written: {target.relative_to(scratch_dir.resolve())}"}]}

    @tool("read_scratch", "读取之前写到临时目录下的文件", {"path": str})
    async def read_scratch(args: dict) -> dict:
        try:
            target = resolve_within(scratch_dir, args["path"])
        except PathEscapeError as e:
            return {"content": [{"type": "text", "text": str(e)}], "is_error": True}
        if not target.is_file():
            return {"content": [{"type": "text", "text": f"not found: {args['path']}"}], "is_error": True}
        return {"content": [{"type": "text", "text": target.read_text(encoding="utf-8", errors="replace")}]}

    return [write_scratch, read_scratch]
