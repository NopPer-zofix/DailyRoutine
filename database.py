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
MAX_PENDING_REQUESTS = 20  # stops a flood of strangers from filling the DB / spamming the admin

STATUSES = ("pending", "approved", "denied", "revoked")

# Columns added after the first release. Rows that already exist were created
# by the single admin, so they default to 'approved'.
_MIGRATIONS = {
    "status": "TEXT NOT NULL DEFAULT 'approved'",
    "display_name": "TEXT",
    "timezone": "TEXT",
    "reminder_interval_hours": "INTEGER",
}


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
    status: str = "approved"
    timezone: Optional[str] = None
    display_name: Optional[str] = None
    reminder_interval_hours: Optional[int] = None  # None = server default


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
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade a database created by an older version (idempotent)."""
        existing = {r["name"] for r in self.conn.execute("PRAGMA table_info(user_state)")}
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE user_state ADD COLUMN {column} {ddl}")

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
    def ensure_chat(self, chat_id: int, status: str = "approved") -> None:
        """Register a chat with ``status`` if it is not known yet (idempotent).

        Only trusted code (start-up, admin actions) should call this with the
        default; it never changes the status of an existing row.
        """
        self._check_status(status)
        self.conn.execute(
            "INSERT OR IGNORE INTO user_state (chat_id, status) VALUES (?, ?)",
            (chat_id, status),
        )

    def _touch(self, chat_id: int) -> None:
        """Make sure a row exists for internal bookkeeping *without* granting access."""
        self.ensure_chat(chat_id, "pending")

    @staticmethod
    def _check_status(status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown status {status!r}")

    def list_chats(self) -> List[int]:
        """Return the chat ids that are allowed to use the bot (get reminders etc.)."""
        rows = self.conn.execute(
            "SELECT chat_id FROM user_state WHERE status = 'approved' ORDER BY chat_id"
        )
        return [r["chat_id"] for r in rows]

    # ----------------------------------------------------------------- access
    def get_status(self, chat_id: int) -> Optional[str]:
        """``pending`` / ``approved`` / ``denied`` / ``revoked``, or None if unknown."""
        row = self.conn.execute(
            "SELECT status FROM user_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["status"] if row else None

    def set_status(self, chat_id: int, status: str) -> None:
        """Change a chat's access status (creates the row if needed)."""
        self._check_status(status)
        self._touch(chat_id)
        self.conn.execute(
            "UPDATE user_state SET status = ? WHERE chat_id = ?", (status, chat_id)
        )

    def pending_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM user_state WHERE status = 'pending'"
        ).fetchone()
        return int(row["n"])

    def request_access(self, chat_id: int, display_name: str) -> bool:
        """Record an access request from an unknown chat.

        Returns:
            True if a new pending request was created (the admin should be
            notified); False if the chat was already known in any status.
        """
        if self.get_status(chat_id) is not None:
            return False
        self.conn.execute(
            "INSERT INTO user_state (chat_id, status, display_name) VALUES (?, 'pending', ?)",
            (chat_id, display_name),
        )
        return True

    def list_users(self) -> List[UserState]:
        """All known chats, newest requests last (for the admin's /users)."""
        rows = self.conn.execute("SELECT chat_id FROM user_state ORDER BY rowid").fetchall()
        return [self.get_state(r["chat_id"]) for r in rows]

    def set_timezone(self, chat_id: int, name: Optional[str]) -> None:
        """Store an IANA timezone name (None = use the server default)."""
        self._touch(chat_id)
        self.conn.execute(
            "UPDATE user_state SET timezone = ? WHERE chat_id = ?", (name, chat_id)
        )

    def get_state(self, chat_id: int) -> UserState:
        """Return the chat's state (creating a non-approved row if it is new)."""
        self._touch(chat_id)
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
            status=row["status"],
            timezone=row["timezone"],
            display_name=row["display_name"],
            reminder_interval_hours=row["reminder_interval_hours"],
        )

    def set_reminder_interval(self, chat_id: int, hours: Optional[int]) -> None:
        """Store how many hours pass between reminders (None = server default)."""
        if hours is not None and not 1 <= hours <= 24:
            raise ValueError("Reminder interval must be between 1 and 24 hours")
        self._touch(chat_id)
        self.conn.execute(
            "UPDATE user_state SET reminder_interval_hours = ? WHERE chat_id = ?",
            (hours, chat_id),
        )

    def set_reminders_enabled(self, chat_id: int, enabled: bool) -> None:
        self._touch(chat_id)
        self.conn.execute(
            "UPDATE user_state SET reminders_enabled = ? WHERE chat_id = ?",
            (int(enabled), chat_id),
        )

    def mark_reminded(self, chat_id: int, when: datetime) -> None:
        """Record that a reminder was sent at ``when``."""
        self._touch(chat_id)
        self.conn.execute(
            "UPDATE user_state SET last_reminder_at = ? WHERE chat_id = ?",
            (when.isoformat(), chat_id),
        )

    def mark_congratulated(self, chat_id: int, day: date) -> None:
        """Record that the "all done" message was sent for ``day``."""
        self._touch(chat_id)
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
        self._touch(chat_id)
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
