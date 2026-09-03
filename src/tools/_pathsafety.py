"""路径包含校验：所有工具（fs/log/scratch）访问文件前都要过这一关。

必须在每次访问时对请求路径重新 resolve() 再比对前缀，而不是只在启动时校验一次配置——
否则允许目录内部埋一个指向目录外的软链接就能绕过限制。
"""

from pathlib import Path


class PathEscapeError(ValueError):
    pass


def resolve_within(base: Path, relative: str) -> Path:
    """把 relative 拼到 base 下并 resolve()，确认结果仍在 base 内，否则抛 PathEscapeError。"""
    base = base.resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise PathEscapeError(f"path escapes allowed root: {relative!r}") from None
    return candidate
