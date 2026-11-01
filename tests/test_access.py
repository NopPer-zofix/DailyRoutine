"""Invite-only access: requests, admin approval, revocation and isolation."""
from __future__ import annotations

from aiogram.dispatcher.event.bases import UNHANDLED

from database import MAX_PENDING_REQUESTS
from keyboards import AccessCB, manage_user_keyboard
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
    [edit] = fed.calls("EditMessageText")  # the request message turns into a result line
    assert "approved" in edit.text and edit.reply_markup is None


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
def test_users_text_is_numbered_and_escapes_names(db):
    db.ensure_chat(CHAT)
    db.request_access(1, "<Pat>")
    db.ensure_chat(2)
    users = db.list_users()

    text = users_text(users, CHAT)

    # Rows are ordered by chat id (the table's primary key): 1, 2, then the admin (CHAT).
    assert "1. ⏳ &lt;Pat&gt;" in text and "2. ✅" in text and "3. 👑" in text


def test_manage_keyboard_offers_actions_that_fit_the_status(db):
    db.request_access(1, "pending one")
    db.ensure_chat(2)
    db.set_status(3, "revoked")
    db.set_status(4, "denied")
    by_id = {u.chat_id: u for u in db.list_users()}

    def labels(chat_id):
        return [b.text for b in _buttons(manage_user_keyboard(by_id[chat_id]))]

    assert labels(1) == ["✅ Approve", "❌ Deny", "✖ Cancel"]
    assert labels(2) == ["🚫 Revoke", "✖ Cancel"]
    assert labels(3) == ["↩️ Unban", "✖ Cancel"]
    assert labels(4) == ["↩️ Unban", "✖ Cancel"]
    # Unban re-approves; Cancel is the only button that is not about this user.
    unban = _buttons(manage_user_keyboard(by_id[3]))[0]
    assert AccessCB.unpack(unban.callback_data) == AccessCB(action="approve", chat_id=3)


# ------------------------------------------- /users: pick a number, then act (FSM)
class Session:
    """One dispatcher kept alive across updates, so FSM state survives between them."""

    def __init__(self, db, config):
        from unittest.mock import AsyncMock, MagicMock

        from aiogram import Bot, Dispatcher
        from aiogram.fsm.storage.memory import MemoryStorage

        import handlers

        self.dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
        self.dp.include_router(handlers.build_router(config))
        self.bot = Bot("123:abc")
        self.bot.session.make_request = AsyncMock(return_value=MagicMock())
        self.n = 0

    def _calls(self, name):
        return [
            c.args[1]
            for c in self.bot.session.make_request.await_args_list
            if type(c.args[1]).__name__ == name
        ]

    async def send(self, chat_id, text=None, *, data=None):
        from aiogram.types import CallbackQuery, Chat, Message, Update, User

        from tests.conftest import NOW

        self.n += 1
        self.bot.session.make_request.reset_mock()
        user = User(id=chat_id, is_bot=False, first_name="T")
        message = Message(
            message_id=self.n, date=NOW, chat=Chat(id=chat_id, type="private"),
            from_user=user, text=None if data else text,
        )
        if data:
            update = Update(update_id=self.n, callback_query=CallbackQuery(
                id=str(self.n), from_user=user, chat_instance="x", data=data, message=message))
        else:
            update = Update(update_id=self.n, message=message)
        await self.dp.feed_update(self.bot, update)
        return self

    def sent(self, chat_id):
        return [m for m in self._calls("SendMessage") if m.chat_id == chat_id]

    def edits(self):
        return self._calls("EditMessageText")


def _two_users(db):
    db.ensure_chat(CHAT)
    db.request_access(STRANGER, "alice")  # pending, number 2
    db.ensure_chat(777)
    db.set_status(777, "revoked")  # number 3


async def test_users_lists_numbers_without_action_buttons(db, config):
    _two_users(db)
    s = Session(db, config)

    await s.send(CHAT, "/users")

    [msg] = s.sent(CHAT)
    assert "1. 👑" in msg.text and "2. ⏳ alice" in msg.text and "3. 🚫" in msg.text
    assert "number" in msg.text
    # No per-user buttons under the list - only Cancel.
    assert [b.text for b in _buttons(msg.reply_markup)] == ["✖ Cancel"]


async def test_admin_picks_number_then_unbans(db, config):
    _two_users(db)
    s = Session(db, config)
    await s.send(CHAT, "/users")

    await s.send(CHAT, "3")
    [menu] = s.sent(CHAT)
    assert "777" in menu.text and "revoked" in menu.text
    assert [b.text for b in _buttons(menu.reply_markup)] == ["↩️ Unban", "✖ Cancel"]

    await s.send(CHAT, data=AccessCB(action="approve", chat_id=777).pack())
    assert db.get_status(777) == "approved"
    assert "given access" in s.sent(777)[0].text
    assert "approved" in s.edits()[0].text


async def test_admin_picks_number_then_revokes(db, config):
    _two_users(db)
    db.set_status(STRANGER, "approved")
    s = Session(db, config)
    await s.send(CHAT, "/users")
    await s.send(CHAT, "2")

    await s.send(CHAT, data=AccessCB(action="revoke", chat_id=STRANGER).pack())

    assert db.get_status(STRANGER) == "revoked"
    assert "removed" in s.sent(STRANGER)[0].text


async def test_cancel_button_leaves_everything_unchanged(db, config):
    _two_users(db)
    s = Session(db, config)
    await s.send(CHAT, "/users")
    await s.send(CHAT, "2")

    await s.send(CHAT, data=AccessCB(action="cancel", chat_id=0).pack())

    assert db.get_status(STRANGER) == "pending"
    assert s.edits()[0].text == "Cancelled." and s.edits()[0].reply_markup is None
    # Back to normal: a number typed now is not treated as a pick.
    await s.send(CHAT, "2")
    assert s.sent(CHAT) == []


async def test_cancel_works_from_the_list_prompt_and_by_command(db, config):
    _two_users(db)
    s = Session(db, config)

    await s.send(CHAT, "/users")
    await s.send(CHAT, data=AccessCB(action="cancel", chat_id=0).pack())
    await s.send(CHAT, "2")
    assert s.sent(CHAT) == []  # state was cleared by the button

    await s.send(CHAT, "/users")
    await s.send(CHAT, "/cancel")
    await s.send(CHAT, "2")
    assert [m.text for m in s.sent(CHAT)] == []  # and by /cancel


async def test_bad_numbers_keep_waiting_and_explain(db, config):
    _two_users(db)
    s = Session(db, config)
    await s.send(CHAT, "/users")

    for bad in ("0", "4", "-1", "alice", "1.5", "²"):
        await s.send(CHAT, bad)
        [reply] = s.sent(CHAT)
        assert "from 1 to 3" in reply.text, bad

    await s.send(CHAT, "1")  # the admin's own row
    assert "That's you" in s.sent(CHAT)[0].text

    await s.send(CHAT, "#2")  # still waiting: a valid pick works
    assert "alice" in s.sent(CHAT)[0].text


async def test_other_commands_abandon_the_pick(db, config):
    _two_users(db)
    s = Session(db, config)
    await s.send(CHAT, "/users")
    await s.send(CHAT, "/tasks")

    await s.send(CHAT, "2")

    assert s.sent(CHAT) == []


async def test_only_the_admin_can_use_the_number_flow(db, config):
    _two_users(db)
    db.set_status(STRANGER, "approved")
    s = Session(db, config)

    await s.send(STRANGER, "/users")  # not admin: not handled
    await s.send(STRANGER, "3")

    assert s.sent(STRANGER) == []
    assert db.get_status(777) == "revoked"
