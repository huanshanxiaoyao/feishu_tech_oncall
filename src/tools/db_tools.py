"""只读数据库查询工具。

安全边界是分层的，跟 shell_tools 的思路一致——任何一层单独失手，另一层还能兜住：
1. 真正的硬边界：`targets.yaml` 里配置的数据库账号本身就应该是数据库层面的只读角色
   （`readonly_role` 只是配置里的一个自我声明/文档字段，代码不会去校验数据库账号
   实际权限——账号本身是不是只读，取决于 DBA 怎么建的这个账号）。
2. 代码层防线（防误伤，不是防蓄意绕过）：只接受单条 SELECT 语句（正则挡 INSERT/UPDATE/
   DELETE/DROP 等关键字 + 拒绝任何分号），每次查询都强制包在 `BEGIN READ ONLY` 事务里，
   语句超时、返回行数都有上限。
3. DSN 只在调用这一刻从环境变量读取，不写进 targets.yaml/审计日志；密码部分单独通过
   `PGPASSWORD` 环境变量传给 psql 子进程，不出现在 argv 里——argv 在这台机器上任何用户
   跑 `ps aux` 都能看到，密码不能出现在那里。
"""

import asyncio
import os
import re
from urllib.parse import urlsplit, urlunsplit

from claude_agent_sdk import SdkMcpTool, tool

from ..targets import TargetsRegistry

_MAX_OUTPUT_CHARS = 8000

_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|copy|vacuum|"
    r"call|merge|listen|notify|refresh|reindex|cluster|lock|into)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> str | None:
    """返回拒绝原因；None 表示校验通过。"""
    stripped = sql.strip()
    if not stripped:
        return "empty query"
    body = stripped.rstrip(";").strip()
    if ";" in body:
        return "multiple statements are not allowed"
    if not re.match(r"(?is)^\s*(with\b.*?)?select\b", body):
        return "only SELECT statements are allowed"
    if _FORBIDDEN_KEYWORDS.search(body):
        return "query contains a forbidden keyword"
    return None


def _split_dsn_password(dsn: str) -> tuple[str, str | None]:
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn, None
    netloc = parts.username or ""
    netloc += f"@{parts.hostname or ''}"
    if parts.port:
        netloc += f":{parts.port}"
    safe = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return safe, parts.password


def build_db_tools(registry: TargetsRegistry) -> list[SdkMcpTool]:
    db_targets = {t.name: t for t in registry.databases}

    def _error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    @tool(
        "run_query",
        "在只读数据库账号下执行一条 SELECT 查询（自动包在只读事务里，超时/行数都有上限）",
        {"target": str, "sql": str},
    )
    async def run_query(args: dict) -> dict:
        target_name = args["target"]
        sql = args["sql"]

        target = db_targets.get(target_name)
        if target is None:
            return _error(f"unknown db target: {target_name!r}, available: {sorted(db_targets)}")
        if target.engine != "postgres":
            return _error(
                f"db target {target_name!r} has unsupported engine {target.engine!r} "
                "(only postgres is implemented)"
            )

        dsn = os.environ.get(target.dsn_env)
        if not dsn:
            return _error(f"db target {target_name!r} configured but env var {target.dsn_env} is not set")

        error = _validate_sql(sql)
        if error:
            return _error(f"query rejected: {error}")

        body = sql.strip().rstrip(";").strip()
        wrapped = f"SELECT * FROM ({body}) AS _sub LIMIT {target.max_rows}"

        safe_dsn, password = _split_dsn_password(dsn)
        env = dict(os.environ)
        if password is not None:
            env["PGPASSWORD"] = password

        proc = await asyncio.create_subprocess_exec(
            "psql",
            safe_dsn,
            "-X",
            "--csv",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "BEGIN READ ONLY",
            "-c",
            f"SET statement_timeout = '{target.query_timeout_seconds * 1000}'",
            "-c",
            wrapped,
            "-c",
            "ROLLBACK",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=target.query_timeout_seconds + 10
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return _error("query timed out")

        output = (stdout + stderr).decode("utf-8", errors="replace")
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n...(truncated)"
        return {"content": [{"type": "text", "text": output}], "is_error": proc.returncode != 0}

    return [run_query]
