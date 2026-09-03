from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    event_id     TEXT UNIQUE NOT NULL,
    chat_id      TEXT NOT NULL,
    open_id      TEXT NOT NULL,
    user_name    TEXT,
    message_id   TEXT NOT NULL,
    raw_text     TEXT NOT NULL,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cases (
    short_id      TEXT PRIMARY KEY REFERENCES messages(id),
    status        TEXT NOT NULL DEFAULT 'pending',
    problem_text  TEXT NOT NULL,
    report_text   TEXT,
    error_text    TEXT,
    session_id    TEXT,
    turns_used    INTEGER,
    cost_usd      REAL,
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS case_tool_calls (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    short_id             TEXT NOT NULL REFERENCES cases(short_id),
    seq                  INTEGER NOT NULL,
    tool_name            TEXT NOT NULL,
    tool_input           TEXT NOT NULL,
    tool_output_summary  TEXT,
    is_error             INTEGER NOT NULL DEFAULT 0,
    permission_decision  TEXT,
    started_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_ms          INTEGER
);
CREATE INDEX IF NOT EXISTS idx_case_tool_calls_short_id ON case_tool_calls(short_id);

CREATE TABLE IF NOT EXISTS user_lockouts (
    open_id       TEXT PRIMARY KEY,
    locked_until  TIMESTAMP NOT NULL,
    reason        TEXT NOT NULL,
    case_short_id TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""
# cases.status 的合法取值：pending|running|done|failed|timeout|
#   rejected_off_topic|rejected_destructive|rejected_locked_out
# 后三个是分诊闸门产生的——不管请求最终有没有真的起 Agent，都在 cases 里留一行，
# 保证"每次收到的排查请求都要留痕"这条要求不需要额外的表。


class Store:
    def __init__(self, db_path: Path):
        self._db_path = db_path

    async def init(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def event_already_seen(self, event_id: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM messages WHERE event_id = ? LIMIT 1", (event_id,)
            )
            row = await cursor.fetchone()
            return row is not None

    async def save_message(
        self,
        *,
        short_id: str,
        event_id: str,
        chat_id: str,
        open_id: str,
        user_name: str | None,
        message_id: str,
        raw_text: str,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO messages (id, event_id, chat_id, open_id, user_name, message_id, raw_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (short_id, event_id, chat_id, open_id, user_name, message_id, raw_text),
            )
            await db.commit()

    async def create_case(self, *, short_id: str, problem_text: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO cases (short_id, status, problem_text, started_at)
                VALUES (?, 'pending', ?, CURRENT_TIMESTAMP)
                """,
                (short_id, problem_text),
            )
            await db.commit()

    async def mark_case_running(self, short_id: str, session_id: str | None) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                "UPDATE cases SET status = 'running', session_id = ? WHERE short_id = ?",
                (session_id, short_id),
            )
            await db.commit()

    async def record_tool_call(
        self,
        *,
        short_id: str,
        seq: int,
        tool_name: str,
        tool_input: str,
        tool_output_summary: str | None,
        is_error: bool,
        permission_decision: str | None,
        duration_ms: int | None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT INTO case_tool_calls
                    (short_id, seq, tool_name, tool_input, tool_output_summary,
                     is_error, permission_decision, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    short_id,
                    seq,
                    tool_name,
                    tool_input,
                    tool_output_summary,
                    1 if is_error else 0,
                    permission_decision,
                    duration_ms,
                ),
            )
            await db.commit()

    async def complete_case(
        self,
        short_id: str,
        *,
        status: str,
        report_text: str | None = None,
        error_text: str | None = None,
        turns_used: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                UPDATE cases
                SET status = ?, report_text = ?, error_text = ?,
                    turns_used = ?, cost_usd = ?, finished_at = CURRENT_TIMESTAMP
                WHERE short_id = ?
                """,
                (status, report_text, error_text, turns_used, cost_usd, short_id),
            )
            await db.commit()

    async def count_tool_calls(self, short_id: str) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM case_tool_calls WHERE short_id = ?", (short_id,)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def set_lockout(
        self, *, open_id: str, locked_until: str, reason: str, case_short_id: str | None
    ) -> None:
        """新的破坏性指令覆盖旧的锁定，重新计满一个锁定周期。"""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO user_lockouts (open_id, locked_until, reason, case_short_id)
                VALUES (?, ?, ?, ?)
                """,
                (open_id, locked_until, reason, case_short_id),
            )
            await db.commit()

    async def get_active_lockout(self, open_id: str) -> dict | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM user_lockouts WHERE open_id = ? AND locked_until > CURRENT_TIMESTAMP",
                (open_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_case(self, short_id: str) -> dict | None:
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM cases WHERE short_id = ?", (short_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None
