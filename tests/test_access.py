"""Invite-only access: requests, admin approval, revocation and isolation."""
from __future__ import annotations

from aiogram.dispatcher.event.bases import UNHANDLED

from database import MAX_PENDING_REQUESTS
from keyboards import AccessCB, users_keyboard
from tests.conftest import CHAT, TODAY
from texts import users_text

STRANGER = 555


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ------------------------------------------------------------ requesting access
async def test_stranger_gets_pending_request_and_admin_is_notified(feed, db):
    fed = await feed(STRANGER, "/start", username="alice")

    assert db.get_status(STRANGER) == "pending"
    assert db.list_chats() == []  # still no access, no reminders
    assert "asked the owner" in fed.texts_to(STRANGER)[0]

    [notice] = [m for m in fed.calls() if m.chat_id == CHAT]
    assert "alice" in notice.text and str(STRANGER) in notice.text
    actions = [AccessCB.unpack(b.callback_data).action for b in _buttons(notice.reply_markup)]
    assert actions == ["approve", "deny"]


async def test_stranger_cannot_use_commands_or_create_data(feed, db):
    fed = await feed(STRANGER, "/add sneaky")

    assert db.list_tasks(STRANGER, TODAY) == []
    assert not any("sneaky" in t for t in fed.texts_to(STRANGER))


async def test_pending_user_is_told_to_wait_and_admin_not_spammed(feed, db):
    await feed(STRANGER, "/start")
    fed = await feed(STRANGER, "/tasks")

    assert "waiting for approval" in fed.texts_to(STRANGER)[0]
    assert fed.texts_to(CHAT) == []  # no second notification


async def test_denied_and_revoked_users_are_ignored_silently(feed, db):
    db.set_status(STRANGER, "denied")
    db.set_status(STRANGER + 1, "revoked")

    for chat in (STRANGER, STRANGER + 1):
        fed = await feed(chat, "/start")
        assert fed.calls() == []


async def test_group_chats_are_ignored(feed, db):
    fed = await feed(-100123, "/start", chat_type="supergroup")

    assert fed.calls() == []
    assert db.get_status(-100123) is None


async def test_request_flood_is_capped(feed, db):
    for i in range(MAX_PENDING_REQUESTS):
        db.request_access(1000 + i, f"user{i}")

    fed = await feed(STRANGER, "/start")

    assert db.get_status(STRANGER) is None
    assert fed.texts_to(CHAT) == []  # admin not pinged
    assert "isn't taking requests" in fed.texts_to(STRANGER)[0]


# ------------------------------------------------------------ admin decisions
async def test_admin_approves_user(feed, db):
    await feed(STRANGER, "/start")

    fed = await feed(CHAT, data=AccessCB(action="approve", chat_id=STRANGER).pack())

    assert db.get_status(STRANGER) == "approved"
    assert db.list_chats() == [CHAT, STRANGER] or STRANGER in db.list_chats()
    assert "given access" in fed.texts_to(STRANGER)[0]  # user is told
    assert fed.calls("EditMessageText")  # admin's message becomes the /users view


async def test_admin_denies_user(feed, db):
    await feed(STRANGER, "/start")

    fed = await feed(CHAT, data=AccessCB(action="deny", chat_id=STRANGER).pack())

    assert db.get_status(STRANGER) == "denied"
    assert "declined" in fed.texts_to(STRANGER)[0]
    assert STRANGER not in db.list_chats()


async def test_admin_revokes_and_user_is_locked_out(feed, db):
    db.ensure_chat(STRANGER)
    fed = await feed(STRANGER, "/tasks")
    assert fed.texts_to(STRANGER)  # works while approved

    await feed(CHAT, data=AccessCB(action="revoke", chat_id=STRANGER).pack())
    fed = await feed(STRANGER, "/tasks")

    assert db.get_status(STRANGER) == "revoked"
    assert fed.calls() == []


async def test_revoked_user_can_be_reapproved_and_keeps_data(feed, db):
    db.ensure_chat(STRANGER)
    db.add_task(STRANGER, "Gym", TODAY)
    db.set_status(STRANGER, "revoked")

    await feed(CHAT, data=AccessCB(action="approve", chat_id=STRANGER).pack())

    assert [t.title for t in db.list_tasks(STRANGER, TODAY)] == ["Gym"]


async def test_admin_cannot_be_revoked(feed, db):
    db.ensure_chat(CHAT)

    await feed(CHAT, data=AccessCB(action="revoke", chat_id=CHAT).pack())

    assert db.get_status(CHAT) == "approved"


async def test_repeated_button_press_does_not_renotify(feed, db):
    await feed(STRANGER, "/start")
    data = AccessCB(action="approve", chat_id=STRANGER).pack()
    await feed(CHAT, data=data)

    fed = await feed(CHAT, data=data)

    assert fed.texts_to(STRANGER) == []


# ------------------------------------------------- users can't do admin things
async def test_approved_user_cannot_press_admin_buttons(feed, db):
    db.ensure_chat(STRANGER)
    db.set_status(777, "pending")

    fed = await feed(STRANGER, data=AccessCB(action="approve", chat_id=777).pack())

    assert fed.result is UNHANDLED
    assert db.get_status(777) == "pending"


async def test_approved_user_cannot_use_users_command(feed, db):
    db.ensure_chat(STRANGER)

    fed = await feed(STRANGER, "/users")

    assert fed.result is UNHANDLED


async def test_admin_users_command_lists_everyone(feed, db):
    db.ensure_chat(CHAT)
    await feed(STRANGER, "/start", username="alice")

    fed = await feed(CHAT, "/users")

    [reply] = fed.texts_to(CHAT)
    assert "alice" in reply and "pending" in reply


# ------------------------------------------------------------------- isolation
async def test_each_user_has_a_private_list(feed, db):
    db.ensure_chat(CHAT)
    db.ensure_chat(STRANGER)
    await feed(CHAT, "/add admin task")
    await feed(STRANGER, "/add alice task")

    assert [t.title for t in db.list_tasks(CHAT, TODAY)] == ["admin task"]
    assert [t.title for t in db.list_tasks(STRANGER, TODAY)] == ["alice task"]


async def test_user_cannot_toggle_another_users_task(feed, db):
    from keyboards import TaskCB

    db.ensure_chat(CHAT)
    db.ensure_chat(STRANGER)
    admin_task = db.add_task(CHAT, "private", TODAY)

    await feed(STRANGER, data=TaskCB(action="toggle", task_id=admin_task.id).pack())

    assert db.get_task(CHAT, admin_task.id, TODAY).is_done is False


# --------------------------------------------------------------------- rendering
def test_users_text_and_keyboard_depend_on_status(db):
    db.ensure_chat(CHAT)
    db.request_access(1, "<Pat>")
    db.ensure_chat(2)
    db.set_status(3, "revoked")
    users = db.list_users()

    text = users_text(users, CHAT)
    assert "&lt;Pat&gt;" in text and "👑" in text

    labels = [b.text for b in _buttons(users_keyboard(users, CHAT))]
    assert any("Revoke" in t for t in labels)  # chat 2, approved
    assert any("Re-approve" in t for t in labels)  # chat 3, revoked
    assert "❌ Deny" in labels  # chat 1, pending
    assert not any(str(CHAT) in b.callback_data for b in _buttons(users_keyboard(users, CHAT)))
