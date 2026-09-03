"""只读代码访问工具：read_file / list_dir / grep_code。"""

import fnmatch
import re
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from ..targets import TargetsRegistry
from ._pathsafety import PathEscapeError, resolve_within


def _matches_any(relative_posix: str, patterns: list[str]) -> bool:
    """fnmatch 里 `/` 是字面字符，`**/*.md` 要求路径里真的有一个 `/`，所以匹配不到根目录下的
    文件（比如 README.md）。真实 glob 语义里 `**/` 应该也能匹配零层目录，这里补上这个情况。"""
    for p in patterns:
        if fnmatch.fnmatch(relative_posix, p):
            return True
        if p.startswith("**/") and fnmatch.fnmatch(relative_posix, p[3:]):
            return True
    return False


_MAX_LIST_ENTRIES = 500
_MAX_GREP_MATCHES_DEFAULT = 200
_MAX_GREP_FILES_SCANNED = 3000


def build_fs_tools(registry: TargetsRegistry) -> list[SdkMcpTool[Any]]:
    targets_by_name = {t.name: t for t in registry.code_targets}

    def _error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    def _resolve_target(name: str):
        target = targets_by_name.get(name)
        if target is None:
            names = ", ".join(targets_by_name) or "(暂无已配置目标)"
            return None, _error(f"unknown target {name!r}, available: {names}")
        return target, None

    @tool(
        "read_file",
        "只读读取某个已配置代码目标下的一个文件内容（target 是目标名，path 是相对该目标根目录的相对路径）",
        {"target": str, "path": str},
    )
    async def read_file(args: dict) -> dict:
        target, err = _resolve_target(args["target"])
        if err:
            return err

        try:
            resolved = resolve_within(target.root, args["path"])
        except PathEscapeError as e:
            return _error(str(e))

        relative_posix = resolved.relative_to(target.root.resolve()).as_posix()
        if _matches_any(relative_posix, target.deny_globs):
            return _error(f"denied by deny_globs: {relative_posix}")
        if not _matches_any(relative_posix, target.readable_globs):
            return _error(f"not covered by readable_globs: {relative_posix}")

        if not resolved.is_file():
            return _error(f"not found: {relative_posix}")
        if resolved.stat().st_size > target.max_file_bytes:
            return _error(f"file too large (> {target.max_file_bytes} bytes): {relative_posix}")

        return {"content": [{"type": "text", "text": resolved.read_text(encoding="utf-8", errors="replace")}]}

    @tool(
        "list_dir",
        "列出某个已配置代码目标下某个目录里的条目（target 是目标名，path 是相对该目标根目录的相对路径，空字符串表示根目录）",
        {"target": str, "path": str},
    )
    async def list_dir(args: dict) -> dict:
        target, err = _resolve_target(args["target"])
        if err:
            return err

        try:
            resolved = resolve_within(target.root, args["path"])
        except PathEscapeError as e:
            return _error(str(e))

        if not resolved.is_dir():
            relative_posix = resolved.relative_to(target.root.resolve()).as_posix()
            return _error(f"not a directory: {relative_posix}")

        root = target.root.resolve()
        lines: list[str] = []
        for entry in sorted(resolved.iterdir()):
            relative_posix = entry.relative_to(root).as_posix()
            if _matches_any(relative_posix, target.deny_globs):
                continue
            kind = "dir" if entry.is_dir() else "file"
            lines.append(f"{kind}\t{entry.name}")
            if len(lines) >= _MAX_LIST_ENTRIES:
                lines.append(f"... 已达 {_MAX_LIST_ENTRIES} 条上限，未列全")
                break

        return {"content": [{"type": "text", "text": "\n".join(lines) if lines else "(空目录)"}]}

    @tool(
        "grep_code",
        "在某个已配置代码目标下按正则表达式搜索文件内容（target 是目标名，pattern 是正则，"
        "path 可选表示只搜某个子目录，max_matches 可选默认 200）",
        {
            "type": "object",
            "properties": {
                "target": {"type": "string"},
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "可选，只搜某个子目录，默认整个目标根目录"},
                "max_matches": {"type": "integer", "description": f"可选，默认 {_MAX_GREP_MATCHES_DEFAULT}"},
            },
            "required": ["target", "pattern"],
        },
    )
    async def grep_code(args: dict) -> dict:
        target, err = _resolve_target(args["target"])
        if err:
            return err

        sub_path = args.get("path") or ""
        try:
            search_root = resolve_within(target.root, sub_path)
        except PathEscapeError as e:
            return _error(str(e))

        try:
            regex = re.compile(args["pattern"])
        except re.error as e:
            return _error(f"invalid regex: {e}")

        max_matches = args.get("max_matches") or _MAX_GREP_MATCHES_DEFAULT
        max_matches = min(max_matches, _MAX_GREP_MATCHES_DEFAULT)

        root = target.root.resolve()
        matches: list[str] = []
        scanned = 0
        candidates = [search_root] if search_root.is_file() else search_root.rglob("*")
        for path in candidates:
            if not path.is_file():
                continue
            scanned += 1
            if scanned > _MAX_GREP_FILES_SCANNED:
                matches.append(f"... 已扫描 {_MAX_GREP_FILES_SCANNED} 个文件仍未搜完，结果可能不全")
                break
            relative_posix = path.relative_to(root).as_posix()
            if _matches_any(relative_posix, target.deny_globs):
                continue
            if not _matches_any(relative_posix, target.readable_globs):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(f"{relative_posix}:{lineno}: {line.strip()[:300]}")
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break

        return {"content": [{"type": "text", "text": "\n".join(matches) if matches else "(无匹配)"}]}

    return [read_file, list_dir, grep_code]
