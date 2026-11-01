# Daily Routine Telegram Bot

A personal Telegram bot for a Raspberry Pi that tracks today's routine tasks and
nudges you about whatever is still open - every 4 hours by default, or as often as you choose.

- **Stack:** Python 3.10+, aiogram 3, APScheduler 3, SQLite, python-dotenv
- **Invite-only, multi-user:** `ADMIN_CHAT_ID` is the owner and is always allowed. Anyone else who
  messages the bot creates an *access request*; the admin gets **Approve / Deny** buttons. Approved
  users each get their own private task list, reminders and timezone (the admin cannot see their
  tasks). See [Access control](#access-control).

## Features

| Area           | Behaviour                                                                                                                                                                                                                                         |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Tasks          | `/add <text>`, ➕ button, 🗑 delete mode, `/tasks` checklist                                                                                                                                                                                      |
| UI             | Tap a task to toggle ⬜ ↔ ✅; header shows `███░░░░░░░ 3/10 tasks completed (30%)`                                                                                                                                                                |
| Midnight reset | At 00:00 **local** time a fresh list is generated and posted silently (no notification sound; skipped if reminders are off or the list is empty) (`ROLLOVER_MODE=reset` copies every task as not-done; `carry` copies only unfinished ones). It also runs lazily on first use and at startup, so a Pi that was off at midnight is still correct. |
| Reminders      | At 08:00, 12:00, 16:00, 20:00 by default the bot checks completion. **Each user picks their own frequency** (1-12 h, or off) with `/frequency`; the schedule restarts from the first hour every day. Open tasks → reminder with one-tap ✅ buttons. All done → nothing is sent.                                                                      |
| Reliability    | Local timezone, send retries with backoff, flood-wait handling, reconnecting polling loop, graceful SIGINT/SIGTERM shutdown.                                                                                                                      |

## Project structure

```
DailyRoutine/
├── main.py                  # entry point, polling loop, signal handling
├── config.py                # .env loading + validation, timezone detection
├── database.py              # SQLite access, toggling, daily rollover
├── schema.sql               # database schema (applied on startup)
├── handlers.py              # commands + inline-button callbacks (admin-only router)
├── keyboards.py             # inline keyboards + typed callback data
├── texts.py                 # message rendering (progress bar, reminder text)
├── menu.py                  # Telegram command menu (the Menu button)
├── access.py                # middleware: admin / approved users in, strangers request access
├── reminders.py             # 4-hour rule, per-user-timezone scheduler tick, retrying sender
├── daily-routine-bot.service# systemd unit
├── .env.example
├── requirements.txt / requirements-dev.txt
├── pytest.ini
└── tests/                   # offline pytest suite (130 tests)
```

## Database schema

See [`schema.sql`](schema.sql).

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    task_date TEXT NOT NULL,                    -- local 'YYYY-MM-DD'
    is_done INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE user_state (
    chat_id INTEGER PRIMARY KEY,
    last_rollover_date TEXT,                    -- makes rollover idempotent
    last_congrats_date TEXT,                    -- "all done" message once per day
    last_reminder_at TEXT,
    reminders_enabled INTEGER NOT NULL DEFAULT 1,
    reminder_interval_hours INTEGER             -- per-user /frequency; NULL = REMINDER_INTERVAL_HOURS
);
```

Each day's list is its own set of rows, so yesterday's history stays in the file.

## Setup on Raspberry Pi

### 1. Prerequisites

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git
python3 --version        # needs 3.10+ (Raspberry Pi OS Bookworm ships 3.11)
```

**Set the Pi's timezone** - reminders follow the system clock's zone:

```bash
timedatectl                         # check "Time zone"
sudo timedatectl set-timezone Europe/Berlin
```

(Or set `TIMEZONE=Europe/Berlin` in `.env` to override.)

### 2. Create the bot and find your chat id

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token.
2. Message **@userinfobot** to get your numeric id (this is `ADMIN_CHAT_ID`).
3. Open your new bot and press **Start** once (a bot cannot message you before that).

### 3. Install

```bash
cd ~
git clone <your-repo-url> DailyRoutine      # or copy the folder over with scp/rsync
cd DailyRoutine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env                                   # set BOT_TOKEN and ADMIN_CHAT_ID
chmod 600 .env
```

### 4. Try it

```bash
.venv/bin/python main.py
```

Send `/start` to the bot, then Ctrl+C - it should log "Shutting down... Bye."

### 5. Run 24/7 with systemd

Edit `daily-routine-bot.service` if your user is not `pi` or the folder differs, then:

```bash
sudo cp daily-routine-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily-routine-bot

systemctl status daily-routine-bot          # is it running?
journalctl -u daily-routine-bot -f          # live logs
sudo systemctl restart daily-routine-bot    # after changing .env or code
```

The unit waits for `network-online.target`, restarts on any exit, and stops with
SIGTERM, which the bot handles by stopping polling and the scheduler and closing
the database.

### Backup

All state is the single file `data/routine.db` (plus `-wal`/`-shm` while running):

```bash
sqlite3 data/routine.db ".backup 'routine-backup.db'"
```

## Configuration (`.env`)

| Variable                                    | Default           | Meaning                                                                                |
| ------------------------------------------- | ----------------- | -------------------------------------------------------------------------------------- |
| `BOT_TOKEN`                                 | - (required)      | Token from @BotFather                                                                  |
| `ADMIN_CHAT_ID`                             | - (required)      | The owner: always allowed, approves other users                                                 |
| `TIMEZONE`                                  | system zone       | IANA name, e.g. `Europe/Berlin`                                                        |
| `DB_PATH`                                   | `data/routine.db` | SQLite file                                                                            |
| `REMINDER_INTERVAL_HOURS`                   | `4`               | Default hours between checks (users can override with `/frequency`)                                                                   |
| `REMINDER_START_HOUR` / `REMINDER_END_HOUR` | `8` / `22`        | Checks fire at start, start+N, ... ≤ end (local hours), so nothing wakes you at 4 a.m. |
| `ROLLOVER_MODE`                             | `reset`           | `reset` or `carry` (see above)                                                         |
| `SEND_CONGRATS`                             | `true`            | (currently unused - the all-done 🎉 message is switched off)                                                 |
| `LOG_LEVEL`                                 | `INFO`            |                                                                                        |

## Access control

| Who | What happens |
|---|---|
| Admin (`ADMIN_CHAT_ID`) | Always allowed. `/users` shows a numbered list of everyone; send a number to open that user's menu (Revoke / Unban / Approve / Deny, or ✖ Cancel). |
| New person | Their first message creates a *pending* request and sends the admin a notification with ✅ Approve / ❌ Deny. They are told to wait. Nothing is stored except their name and chat id. |
| Approved | Full use of the bot; own private list, reminders, timezone. |
| Pending | Told "still waiting"; the admin is not notified again. |
| Denied / revoked | Ignored silently. The admin can re-approve from `/users`; a revoked user's tasks are kept. |

Notes:
* Only **private** chats are served for non-admins; groups are ignored.
* At most 20 requests can be pending at once, so a stranger cannot flood you.
* Each user's timezone defaults to the Pi's. They change it with `/timezone Europe/Berlin`;
  midnight rollover and reminder hours (`REMINDER_*`, the same for everybody) follow it.
  The scheduler wakes every 15 minutes and checks each user's local clock.
* Upgrading from the single-user version is automatic: on first start the database gets the new
  columns and the existing chat stays approved. No manual steps.

## Bot commands

`/tasks` · `/add <text>` · `/delete` · `/frequency [hours]` (buttons, or e.g. `/frequency 3`) · `/reminders` (toggle on/off) · `/remind` (send the reminder now - a preview that ignores the schedule and changes no state) · `/timezone <Area/City>` · `/cancel` · `/help` · admin only: `/users`. All of these appear in the bot's **Menu** button (registered on startup); `/start` no longer lists them.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The suite runs fully offline: SQLite is in-memory, the `Bot` is a `MagicMock`
with an `AsyncMock` `send_message`, and handler tests use mocked `Message` /
`CallbackQuery` objects. A few tests push real `Update`s through a real
`Dispatcher` with the HTTP layer mocked (to verify the admin-only filter).

- `test_database.py` - toggling, deletion, scoping, daily rollover (reset/carry, idempotency, multi-day gaps)
- `test_reminders.py` - the 4-hour rule, congratulate-once, scheduler hours/timezone, retry/backoff
- `test_handlers.py` - commands, buttons, stale buttons, access control
- `test_texts_keyboards_config.py` - rendering, 64-byte callback limit, config validation

## Design notes

- **Why sync `sqlite3`?** One process, a few rows per day; calls take microseconds, so
  it avoids an extra dependency and keeps tests simple.
- **Why fixed reminder hours instead of "every 4 hours from startup"?** Restarts would shift
  the schedule and could ping you at night. Set `REMINDER_START_HOUR=0 REMINDER_END_HOUR=23`
  for a plain round-the-clock 4-hour cycle (00, 04, 08, ...).
- **State lives in SQLite, not memory** - except the transient "waiting for task text" state
  (aiogram `MemoryStorage`), which is simply lost on restart (send `/add` again).
