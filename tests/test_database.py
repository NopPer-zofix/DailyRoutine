"""Task CRUD, completion toggling and the daily rollover."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from database import MAX_TITLE_LENGTH, Database
from tests.conftest import CHAT, NOW, TODAY

YESTERDAY = TODAY - timedelta(days=1)


# ----------------------------------------------------------------------- CRUD
def test_add_and_list_tasks_in_creation_order(db: Database):
    db.add_task(CHAT, "Read", TODAY)
    db.add_task(CHAT, "Gym", TODAY)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Read", "Gym"]


def test_add_normalizes_whitespace(db: Database):
    task = db.add_task(CHAT, "  drink \n  water  ", TODAY)
    assert task.title == "drink water"
    assert task.is_done is False


@pytest.mark.parametrize("title", ["", "   ", "\n"])
def test_add_rejects_blank_title(db: Database, title: str):
    with pytest.raises(ValueError):
        db.add_task(CHAT, title, TODAY)


def test_add_rejects_overlong_title(db: Database):
    with pytest.raises(ValueError):
        db.add_task(CHAT, "x" * (MAX_TITLE_LENGTH + 1), TODAY)


def test_tasks_are_scoped_by_day_and_chat(db: Database):
    db.add_task(CHAT, "today", TODAY)
    db.add_task(CHAT, "yesterday", YESTERDAY)
    db.add_task(999, "other chat", TODAY)

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["today"]


def test_delete_task(db: Database):
    task = db.add_task(CHAT, "Read", TODAY)

    assert db.delete_task(CHAT, task.id, TODAY) is True
    assert db.list_tasks(CHAT, TODAY) == []
    assert db.delete_task(CHAT, task.id, TODAY) is False  # already gone


def test_cannot_delete_another_chats_task(db: Database):
    task = db.add_task(999, "secret", TODAY)
    assert db.delete_task(CHAT, task.id, TODAY) is False
    assert len(db.list_tasks(999, TODAY)) == 1


# ------------------------------------------------------------------- toggling
def test_toggle_marks_done_then_not_done(db: Database):
    task = db.add_task(CHAT, "Read", TODAY)

    done = db.toggle_task(CHAT, task.id, TODAY, NOW)
    assert done is not None and done.is_done is True
    assert done.completed_at == NOW

    reopened = db.toggle_task(CHAT, task.id, TODAY, NOW)
    assert reopened is not None and reopened.is_done is False
    assert reopened.completed_at is None


def test_toggle_unknown_task_returns_none(db: Database):
    assert db.toggle_task(CHAT, 12345, TODAY, NOW) is None


def test_toggle_stale_task_from_previous_day_returns_none(db: Database):
    old = db.add_task(CHAT, "Read", YESTERDAY)
    assert db.toggle_task(CHAT, old.id, TODAY, NOW) is None
    assert db.get_task(CHAT, old.id, YESTERDAY).is_done is False  # untouched


def test_progress_counts_done_and_total(db: Database):
    assert db.progress(CHAT, TODAY) == (0, 0)
    ids = [db.add_task(CHAT, f"t{i}", TODAY).id for i in range(5)]
    for task_id in ids[:3]:
        db.toggle_task(CHAT, task_id, TODAY, NOW)

    assert db.progress(CHAT, TODAY) == (3, 5)


# ------------------------------------------------------------------- rollover
def _seed_yesterday(db: Database) -> None:
    done = db.add_task(CHAT, "Gym", YESTERDAY)
    db.add_task(CHAT, "Read", YESTERDAY)
    db.toggle_task(CHAT, done.id, YESTERDAY, NOW - timedelta(days=1))


def test_rollover_reset_copies_everything_as_not_done(db: Database):
    _seed_yesterday(db)

    created = db.roll_over(CHAT, TODAY, "reset")

    tasks = db.list_tasks(CHAT, TODAY)
    assert created == 2
    assert [t.title for t in tasks] == ["Gym", "Read"]
    assert not any(t.is_done for t in tasks)


def test_rollover_carry_copies_only_unfinished(db: Database):
    _seed_yesterday(db)

    created = db.roll_over(CHAT, TODAY, "carry")

    assert created == 1
    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["Read"]


def test_rollover_does_not_modify_previous_day(db: Database):
    _seed_yesterday(db)
    db.roll_over(CHAT, TODAY, "reset")

    assert db.progress(CHAT, YESTERDAY) == (1, 2)


def test_rollover_is_idempotent_even_if_user_deletes_tasks(db: Database):
    _seed_yesterday(db)
    db.roll_over(CHAT, TODAY, "reset")
    for t in db.list_tasks(CHAT, TODAY):
        db.delete_task(CHAT, t.id, TODAY)

    assert db.roll_over(CHAT, TODAY, "reset") == 0  # must not resurrect them
    assert db.list_tasks(CHAT, TODAY) == []


def test_rollover_skips_titles_already_present_today(db: Database):
    _seed_yesterday(db)
    db.add_task(CHAT, "Gym", TODAY)  # added before the rollover ran

    db.roll_over(CHAT, TODAY, "reset")

    assert sorted(t.title for t in db.list_tasks(CHAT, TODAY)) == ["Gym", "Read"]


def test_rollover_after_multi_day_gap_uses_latest_day_with_tasks(db: Database):
    db.add_task(CHAT, "old", TODAY - timedelta(days=5))
    db.add_task(CHAT, "latest", TODAY - timedelta(days=3))

    db.roll_over(CHAT, TODAY, "reset")

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["latest"]


def test_rollover_with_no_history_creates_nothing_but_records_the_day(db: Database):
    assert db.roll_over(CHAT, TODAY, "reset") == 0
    assert db.get_state(CHAT).last_rollover_date == TODAY


def test_rollover_advances_to_next_day(db: Database):
    _seed_yesterday(db)
    db.roll_over(CHAT, TODAY, "reset")
    tomorrow = TODAY + timedelta(days=1)

    assert db.roll_over(CHAT, tomorrow, "reset") == 2
    assert len(db.list_tasks(CHAT, tomorrow)) == 2


def test_rollover_rejects_unknown_mode(db: Database):
    with pytest.raises(ValueError):
        db.roll_over(CHAT, TODAY, "bogus")


# ---------------------------------------------------------------------- state
def test_state_defaults_and_updates(db: Database):
    state = db.get_state(CHAT)
    assert state.reminders_enabled is True
    assert state.last_congrats_date is None

    db.set_reminders_enabled(CHAT, False)
    db.mark_congratulated(CHAT, TODAY)
    db.mark_reminded(CHAT, NOW)

    state = db.get_state(CHAT)
    assert state.reminders_enabled is False
    assert state.last_congrats_date == TODAY
    assert state.last_reminder_at == NOW


def test_list_chats(db: Database):
    db.ensure_chat(2)
    db.ensure_chat(1)
    db.ensure_chat(1)  # idempotent
    assert db.list_chats() == [1, 2]
