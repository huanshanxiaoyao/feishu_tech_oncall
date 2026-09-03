"""只读日志访问工具：tail_log / grep_log。

对照 targets.log_targets 做跟 fs_tools 一样的思路：target 名字白名单 + 强制上限，
区别是这里没有目录树可穿越（每个 log target 直接指向一个具体文件），所以不需要
resolve_within 那套路径包含校验，但仍然只允许读配置里登记过的文件本身。
"""

import re
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..targets import TargetsRegistry

_MAX_TAIL_LINES_DEFAULT = 200
_MAX_GREP_MATCHES_DEFAULT = 200
_TAIL_READ_WINDOW_BYTES = 2_000_000  # 大文件只从末尾读这么多字节，不整份 load


def build_log_tools(registry: TargetsRegistry) -> list[SdkMcpTool[Any]]:
    targets_by_name = {t.name: t for t in registry.log_targets}

    def _error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _resolve_target(name: str):
        target = targets_by_name.get(name)
        if target is None:
            names = ", ".join(targets_by_name) or "(暂无已配置日志目标)"
            return None, _error(f"unknown log target {name!r}, available: {names}")
        if not target.path.is_file():
            return None, _error(f"log file not found or not a regular file: {target.path}")
        return target, None

    def _read_tail_text(path, max_bytes: int) -> str:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")

    @tool(
        "tail_log",
        "读取某个已配置日志目标的最后若干行（target 是日志目标名，lines 可选默认 200，"
        f"不超过该目标配置的 max_lines_per_read）",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "lines": {"type": "integer", "description": f"可选，默认 {_MAX_TAIL_LINES_DEFAULT}"},
            },
            "required": ["target"],
        },
    )
    async def tail_log(args: dict) -> dict:
        target, err = _resolve_target(args["target"])
        if err:
            return err

        requested = args.get("lines") or _MAX_TAIL_LINES_DEFAULT
        n_lines = min(requested, target.max_lines_per_read)

        text = _read_tail_text(target.path, _TAIL_READ_WINDOW_BYTES)
        all_lines = text.splitlines()
        tail = all_lines[-n_lines:]
        return {"content": [{"type": "text", "text": "\n".join(tail) if tail else "(空文件)"}]}

    @tool(
        "grep_log",
        "在某个已配置日志目标里按正则表达式搜索（target 是日志目标名，pattern 是正则，"
        f"max_matches 可选默认 {_MAX_GREP_MATCHES_DEFAULT}）",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "pattern": {"type": "string"},
                "max_matches": {"type": "integer", "description": f"可选，默认 {_MAX_GREP_MATCHES_DEFAULT}"},
            },
            "required": ["target", "pattern"],
        },
    )
    async def grep_log(args: dict) -> dict:
        target, err = _resolve_target(args["target"])
        if err:
            return err

        try:
            regex = re.compile(args["pattern"])
        except re.error as e:
            return _error(f"invalid regex: {e}")

        max_matches = min(args.get("max_matches") or _MAX_GREP_MATCHES_DEFAULT, _MAX_GREP_MATCHES_DEFAULT)

        text = _read_tail_text(target.path, _TAIL_READ_WINDOW_BYTES)
        matches: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(f"{lineno}: {line.strip()[:300]}")
                if len(matches) >= max_matches:
                    break

        return {"content": [{"type": "text", "text": "\n".join(matches) if matches else "(无匹配)"}]}

    return [tail_log, grep_log]
