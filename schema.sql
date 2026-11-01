-- Daily Routine bot - SQLite schema (applied idempotently on startup).

PRAGMA journal_mode = WAL;      -- safer against power loss on an SD card
PRAGMA foreign_keys = ON;

-- One row per task per day. "task_date" is the LOCAL calendar date (ISO 8601),
-- so a midnight rollover is just "insert rows for the new date".
CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    title         TEXT    NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    task_date     TEXT    NOT NULL,                       -- 'YYYY-MM-DD' (local)
    is_done       INTEGER NOT NULL DEFAULT 0 CHECK (is_done IN (0, 1)),
    completed_at  TEXT,                                   -- ISO timestamp, NULL if not done
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_chat_date ON tasks (chat_id, task_date);

-- One row per Telegram chat: access status, settings and bookkeeping.
-- (Databases created by older versions are upgraded in database.py::_migrate.)
CREATE TABLE IF NOT EXISTS user_state (
    chat_id             INTEGER PRIMARY KEY,
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'denied', 'revoked')),
    display_name        TEXT,                             -- shown to the admin in /users
    timezone            TEXT,                             -- IANA name; NULL = server default
    last_rollover_date  TEXT,                             -- day tasks were last generated for
    last_congrats_date  TEXT,                             -- day the "all done" message was sent
    last_reminder_at    TEXT,                             -- ISO timestamp of the last reminder
    reminders_enabled   INTEGER NOT NULL DEFAULT 1 CHECK (reminders_enabled IN (0, 1))
);
