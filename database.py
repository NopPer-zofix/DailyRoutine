"""SQLite persistence layer.

The bot is a single-process asyncio app that stores a handful of rows per day,
so the stdlib ``sqlite3`` driver is used directly from the event-loop thread.
Each call is sub-millisecond; this keeps the code (and the tests) simple and
avoids an extra dependency on the Raspberry Pi.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
MAX_TITLE_LENGTH = 200


@dataclass(frozen=True)
class Task:
    """A single task on a given local day."""

    id: int
    chat_id: int
    title: str
    task_date: date
    is_done: bool
    completed_at: Optional[datetime] = None


@dataclass(frozen=True)
class UserState:
    """Persistent per-chat bookkeeping."""

    chat_id: int
    last_rollover_date: Optional[date]
    last_congrats_date: Optional[date]
    last_reminder_at: Optional[datetime]
    reminders_enabled: bool


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        chat_id=row["chat_id"],
        title=row["title"],
        task_date=date.fromisoformat(row["task_date"]),
        is_done=bool(row["is_done"]),
        completed_at=(
            datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        ),
    )


def _opt_date(value: Optional[str]) -> Optional[date]:
    return date.fromisoformat(value) if value else None


class Database:
    """Thin wrapper around a SQLite connection.

    Args:
        path: File path, or ``":memory:"`` for an ephemeral database (tests).
    """

    def __init__(self, path: Union[str, Path]) -> None:
        self._path = str(path)
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> None:
        """Open the connection and create the schema if needed."""
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)  # autocommit
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    def close(self) -> None:
        """Close the connection (safe to call twice)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been called")
        return self._conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block atomically (connection is in autocommit mode)."""
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ chats
    def ensure_chat(self, chat_id: int) -> None:
        """Register a chat (idempotent)."""
        self.conn.execute(
            "INSERT OR IGNORE INTO user_state (chat_id) VALUES (?)", (chat_id,)
        )

    def list_chats(self) -> List[int]:
        """Return all registered chat ids."""
        rows = self.conn.execute("SELECT chat_id FROM user_state ORDER BY chat_id")
        return [r["chat_id"] for r in rows]

    def get_state(self, chat_id: int) -> UserState:
        """Return the chat's state, registering the chat if it is new."""
        self.ensure_chat(chat_id)
        row = self.conn.execute(
            "SELECT * FROM user_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return UserState(
            chat_id=row["chat_id"],
            last_rollover_date=_opt_date(row["last_rollover_date"]),
            last_congrats_date=_opt_date(row["last_congrats_date"]),
            last_reminder_at=(
                datetime.fromisoformat(row["last_reminder_at"])
                if row["last_reminder_at"]
                else None
            ),
            reminders_enabled=bool(row["reminders_enabled"]),
        )

    def set_reminders_enabled(self, chat_id: int, enabled: bool) -> None:
        self.ensure_chat(chat_id)
        self.conn.execute(
            "UPDATE user_state SET reminders_enabled = ? WHERE chat_id = ?",
            (int(enabled), chat_id),
        )

    def mark_reminded(self, chat_id: int, when: datetime) -> None:
        """Record that a reminder was sent at ``when``."""
        self.ensure_chat(chat_id)
        self.conn.execute(
            "UPDATE user_state SET last_reminder_at = ? WHERE chat_id = ?",
            (when.isoformat(), chat_id),
        )

    def mark_congratulated(self, chat_id: int, day: date) -> None:
        """Record that the "all done" message was sent for ``day``."""
        self.ensure_chat(chat_id)
        self.conn.execute(
            "UPDATE user_state SET last_congrats_date = ? WHERE chat_id = ?",
            (day.isoformat(), chat_id),
        )

    # ------------------------------------------------------------------ tasks
    def add_task(self, chat_id: int, title: str, day: date) -> Task:
        """Add a task to ``day``.

        Raises:
            ValueError: If the title is empty or longer than 200 characters.
        """
        title = " ".join(title.split())  # collapse whitespace/newlines
        if not title:
            raise ValueError("Task title must not be empty")
        if len(title) > MAX_TITLE_LENGTH:
            raise ValueError(f"Task title must be at most {MAX_TITLE_LENGTH} characters")
        self.ensure_chat(chat_id)
        cur = self.conn.execute(
            "INSERT INTO tasks (chat_id, title, task_date) VALUES (?, ?, ?)",
            (chat_id, title, day.isoformat()),
        )
        task = self.get_task(chat_id, cur.lastrowid, day)
        assert task is not None
        return task

    def get_task(self, chat_id: int, task_id: int, day: date) -> Optional[Task]:
        """Fetch a task, scoped to its chat and day (stale buttons return None)."""
        row = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND chat_id = ? AND task_date = ?",
            (task_id, chat_id, day.isoformat()),
        ).fetchone()
        return _row_to_task(row) if row else None

    def list_tasks(self, chat_id: int, day: date) -> List[Task]:
        """All tasks of ``day`` in creation order."""
        rows = self.conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND task_date = ? ORDER BY id",
            (chat_id, day.isoformat()),
        )
        return [_row_to_task(r) for r in rows]

    def delete_task(self, chat_id: int, task_id: int, day: date) -> bool:
        """Delete a task. Returns False if it did not exist."""
        cur = self.conn.execute(
            "DELETE FROM tasks WHERE id = ? AND chat_id = ? AND task_date = ?",
            (task_id, chat_id, day.isoformat()),
        )
        return cur.rowcount > 0

    def toggle_task(
        self, chat_id: int, task_id: int, day: date, now: datetime
    ) -> Optional[Task]:
        """Flip a task between not-done and done.

        Returns:
            The updated task, or ``None`` if it no longer exists for ``day``.
        """
        task = self.get_task(chat_id, task_id, day)
        if task is None:
            return None
        new_done = not task.is_done
        self.conn.execute(
            "UPDATE tasks SET is_done = ?, completed_at = ? WHERE id = ?",
            (int(new_done), now.isoformat() if new_done else None, task_id),
        )
        return self.get_task(chat_id, task_id, day)

    def progress(self, chat_id: int, day: date) -> Tuple[int, int]:
        """Return ``(done, total)`` for ``day``."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(is_done), 0) AS done, COUNT(*) AS total "
            "FROM tasks WHERE chat_id = ? AND task_date = ?",
            (chat_id, day.isoformat()),
        ).fetchone()
        return int(row["done"]), int(row["total"])

    # --------------------------------------------------------------- rollover
    def roll_over(self, chat_id: int, today: date, mode: str = "reset") -> int:
        """Generate ``today``'s task list from the most recent earlier day.

        Idempotent: runs at most once per chat per day (tracked in
        ``user_state.last_rollover_date``), so it is safe to call from both the
        midnight job and lazily from handlers. Because it looks at the latest
        earlier day *with tasks*, it also recovers cleanly if the bot was down
        over midnight (or for several days).

        Args:
            chat_id: Chat to roll over.
            today: The new local date.
            mode: ``"reset"`` copies every task as not-done (recurring routine);
                ``"carry"`` copies only the tasks that were not completed.

        Returns:
            Number of tasks created for ``today``.
        """
        if mode not in ("reset", "carry"):
            raise ValueError(f"Unknown rollover mode {mode!r}")

        state = self.get_state(chat_id)
        if state.last_rollover_date is not None and state.last_rollover_date >= today:
            return 0

        created = 0
        with self._transaction() as conn:
            source = conn.execute(
                "SELECT MAX(task_date) AS d FROM tasks WHERE chat_id = ? AND task_date < ?",
                (chat_id, today.isoformat()),
            ).fetchone()["d"]
            if source is not None:
                query = "SELECT title FROM tasks WHERE chat_id = ? AND task_date = ?"
                if mode == "carry":
                    query += " AND is_done = 0"
                query += " ORDER BY id"
                existing = {
                    r["title"]
                    for r in conn.execute(
                        "SELECT title FROM tasks WHERE chat_id = ? AND task_date = ?",
                        (chat_id, today.isoformat()),
                    )
                }
                for row in conn.execute(query, (chat_id, source)).fetchall():
                    if row["title"] in existing:
                        continue  # never duplicate something already added today
                    conn.execute(
                        "INSERT INTO tasks (chat_id, title, task_date) VALUES (?, ?, ?)",
                        (chat_id, row["title"], today.isoformat()),
                    )
                    created += 1
            conn.execute(
                "UPDATE user_state SET last_rollover_date = ? WHERE chat_id = ?",
                (today.isoformat(), chat_id),
            )
        return created
