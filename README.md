# Daily Routine Telegram Bot

A small Telegram bot that keeps a checklist of your daily routine and reminds you about whatever is still unfinished. Add tasks in chat, tap them to mark them done, and every few hours (4 by default) the bot pings you with the open ones. At midnight it posts a fresh list for the new day. It's built to run 24/7 on a Raspberry Pi, but works on any machine that can run Python.

It's invite-only: you are the admin, and anyone else who messages the bot has to be approved by you first. Each approved user gets their own private list, reminders and timezone.

## Prerequisites

- Python 3.10 or newer
- A Telegram account
- A bot token: message **@BotFather** on Telegram, send `/newbot`, copy the token
- Your numeric Telegram ID: message **@userinfobot** (this becomes `ADMIN_CHAT_ID`)
- Optional: a Linux machine with systemd (e.g. Raspberry Pi) to keep it running permanently

## Quickstart

```bash
git clone <your-repo-url> DailyRoutine
cd DailyRoutine
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip ...
cp .env.example .env
```

Edit `.env` and set the two required values:

```ini
BOT_TOKEN=123456:your-token-from-botfather
ADMIN_CHAT_ID=123456789
```

Run it:

```bash
.venv/bin/python main.py                      # Windows: .venv\Scripts\python main.py
```

Open your bot in Telegram, press **Start** (a bot can't message you before that), then try `/add Drink water` and `/tasks`. Stop with Ctrl+C.

### Running it permanently (systemd)

Edit `User`, `WorkingDirectory` and `ExecStart` in `daily-routine-bot.service` to match your user and install path, then:

```bash
sudo cp daily-routine-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily-routine-bot
journalctl -u daily-routine-bot -f            # live logs
```

## Configuration

Set these in `.env`. Only the first two are required.

| Variable | Default | What it does |
| --- | --- | --- |
| `BOT_TOKEN` | required | Token from @BotFather |
| `ADMIN_CHAT_ID` | required | The owner. Always allowed, approves other users |
| `TIMEZONE` | system timezone | IANA name, e.g. `Europe/Berlin`. Decides when "midnight" is |
| `REMINDER_INTERVAL_HOURS` | `4` | Default hours between reminders. Users can change theirs with `/frequency` |
| `REMINDER_START_HOUR` / `REMINDER_END_HOUR` | `8` / `22` | Reminders only fire in this window, so nothing wakes you at 4 a.m. |
| `ROLLOVER_MODE` | `reset` | `reset`: every task returns as not-done each day (recurring routine). `carry`: only unfinished tasks carry over |
| `DB_PATH` | `data/routine.db` | SQLite file. This one file is all the state, so back it up if you care |

Per-user settings (`/timezone`, `/frequency`, `/reminders`) are changed from chat. Run `/help` in the bot for the full command list. As admin, `/users` lets you approve, deny or revoke people.

## Project structure

```
main.py          entry point: starts polling and the scheduler, handles shutdown
config.py        loads and validates .env
database.py      SQLite access, task toggling, daily rollover
schema.sql       database schema, applied on startup
handlers.py      commands and inline-button callbacks
access.py        middleware: admin and approved users in, strangers become access requests
reminders.py     scheduler and reminder sending (with retries)
keyboards.py     inline keyboards
texts.py         message rendering (progress bar, reminder text)
menu.py          registers the Telegram command menu
tests/           offline pytest suite
daily-routine-bot.service   systemd unit
```

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run offline: no Telegram connection or token needed.
