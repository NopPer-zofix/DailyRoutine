"""Rendering, callback payloads and configuration parsing."""
from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from config import ConfigError, load_config
from database import Database, Task
from keyboards import TaskCB, delete_keyboard, reminder_keyboard, tasks_keyboard
from texts import dashboard_text, progress_bar, progress_line, reminder_text

DAY = date(2025, 3, 10)


def _task(i: int, title: str, done: bool = False) -> Task:
    return Task(id=i, chat_id=1, title=title, task_date=DAY, is_done=done)


# ---------------------------------------------------------------------- texts
def test_progress_bar_and_line():
    assert progress_bar(0, 0) == "░" * 10
    assert progress_bar(5, 10) == "█████░░░░░"
    assert "3/5" in progress_line(3, 5) and "60%" in progress_line(3, 5)


def test_dashboard_escapes_html_and_handles_empty():
    assert "No tasks yet" in dashboard_text([], DAY)
    assert "Everything is done" in dashboard_text([_task(1, "a", True)], DAY)


def test_reminder_lists_only_open_tasks_and_escapes_html():
    text = reminder_text([_task(1, "<b>x</b>"), _task(2, "done one", True)])
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    assert "done one" not in text


# ------------------------------------------------------------------ keyboards
def test_tasks_keyboard_marks_state_and_has_controls():
    kb = tasks_keyboard([_task(1, "a"), _task(2, "b", True)])
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert texts[:2] == ["⬜ a", "✅ b"]
    assert any("Add" in t for t in texts) and any("Delete" in t for t in texts)


def test_empty_list_has_no_delete_button():
    texts = [b.text for row in tasks_keyboard([]).inline_keyboard for b in row]
    assert not any("Delete" in t for t in texts)


def test_long_titles_are_truncated_and_callbacks_fit_telegram_limit():
    long = _task(10**9, "x" * 200)
    for kb in (tasks_keyboard([long]), delete_keyboard([long]), reminder_keyboard([long])):
        for button in (b for row in kb.inline_keyboard for b in row):
            assert len(button.text) <= 45
            assert len(button.callback_data.encode()) <= 64


def test_reminder_keyboard_only_has_open_tasks():
    kb = reminder_keyboard([_task(1, "open"), _task(2, "finished", True)])
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ open" in texts and "✅ finished" not in texts


def test_callback_roundtrip():
    packed = TaskCB(action="toggle", task_id=7, view="r").pack()
    assert TaskCB.unpack(packed) == TaskCB(action="toggle", task_id=7, view="r")


# --------------------------------------------------------------------- config
ENV = {"BOT_TOKEN": "1:abc", "ADMIN_CHAT_ID": "123", "TIMEZONE": "Europe/Berlin"}


def test_load_config_defaults():
    cfg = load_config(ENV)
    assert cfg.admin_chat_id == 123
    assert cfg.tz == ZoneInfo("Europe/Berlin")
    assert (cfg.reminder_interval_hours, cfg.rollover_mode, cfg.send_congrats) == (4, "reset", True)


def test_load_config_uses_system_timezone_when_unset(monkeypatch):
    sentinel = ZoneInfo("Asia/Tokyo")
    monkeypatch.setattr("config.detect_local_timezone", lambda: sentinel)
    env = {k: v for k, v in ENV.items() if k != "TIMEZONE"}
    assert load_config(env).tz is sentinel


@pytest.mark.parametrize(
    "patch",
    [
        {"BOT_TOKEN": ""},
        {"ADMIN_CHAT_ID": "abc"},
        {"TIMEZONE": "Mars/Base"},
        {"ROLLOVER_MODE": "weekly"},
        {"REMINDER_INTERVAL_HOURS": "0"},
        {"REMINDER_START_HOUR": "20", "REMINDER_END_HOUR": "8"},
    ],
)
def test_load_config_rejects_bad_values(patch):
    with pytest.raises(ConfigError):
        load_config({**ENV, **patch})


# --------------------------------------------------------------------- schema
def test_database_persists_to_file(tmp_path):
    path = tmp_path / "nested" / "routine.db"
    first = Database(path)
    first.connect()
    first.add_task(1, "persisted", DAY)
    first.close()

    second = Database(path)
    second.connect()
    assert [t.title for t in second.list_tasks(1, DAY)] == ["persisted"]
    second.close()
