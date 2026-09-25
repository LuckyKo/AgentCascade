---
name: ptb-telegram-bot-test-construction
description: Construct valid python-telegram-bot (PTB 20+) Update/Message objects and filters for hermetic unit tests of Telegram bot handlers, including the CommandFilter entity gotcha.
source: auto-generated
version: "1.0.0"
triggers:
  - "python-telegram-bot"
  - "PTB test"
  - "telegram.Update"
  - "MessageHandler filter"
  - "check_update"
generated_by: coder
generated_from_task: "Phase 2 AC Telegram bridge command dispatcher — needed real Update/Message objects to prove the PTB filter routes /stop to on_message"
---

## Goal
Write hermetic unit tests for python-telegram-bot (PTB 20+) handlers without a live bot — construct valid `Update`/`Message` objects that pass `Filter.check_update()`, and avoid the traps that make filter tests vacuous or crash.

## Procedure

### Step 1 — Construct Message with the RIGHT constructor kwargs
PTB 20+ (e.g. 22.x) `Message.__init__` does NOT accept `chat_id=` or `from_user=` directly. Use:
```python
from telegram import Chat, Message, MessageEntity, Update

update = Update(
    update_id=1,
    message=Message(
        message_id=1,
        date='2026-09-24T00:00:00',  # ISO string is fine
        chat=Chat(id=1, type='private'),   # NOT chat_id=
        text='/stop',
    ),
)
```
`check_update()` requires a real `isinstance(update, Update)` — building via `Update.from_dict()` rejects deliberately-fake user ids, so construct directly.

### Step 2 — CRITICAL: filters.COMMAND needs a bot_command entity
`filters.COMMAND.filter(message)` returns **False** for a message with no entities (it requires `message.entities[0].type == MessageEntity.BOT_COMMAND and offset == 0`). A plain `text='/stop'` message does NOT match COMMAND. So when testing that an old filter (`filters.TEXT & ~filters.COMMAND`) excluded commands, you MUST attach the entity or your "old filter rejected it" assertion is vacuous (both filters match):
```python
message=Message(
    message_id=1, date='2026-09-24T00:00:00',
    chat=Chat(id=1, type='private'), text='/stop',
    entities=[MessageEntity(type='bot_command', offset=0, length=5)],
)
# now: filters.TEXT & ~filters.COMMAND  -> False (excluded)
#      filters.TEXT                     -> True  (routed)
```
Real Telegram updates for `/cmd` always carry this entity, so tests with it are faithful.

### Step 3 — Test the registered handler directly
Pull the actual `MessageHandler` from `app.handlers[0]`, call `handler.check_update(update)` to assert routing, then invoke `await handler.callback(update, context)` with a mocked context (`context.bot_data = {...}`, `context.bot.send_message = AsyncMock(...)` capturing sent text). This proves end-to-end filter→handler wiring without any network.

### Step 4 — Mock the async client surface with AsyncMock
When the handler awaits client methods (`await ac.ensure_token()`, `await ac.get_status(token)`), use `AsyncMock` for each:
```python
ac = MagicMock()
ac.inject_message = AsyncMock(return_value={})
ac.ensure_token = AsyncMock(return_value=('tok_test', b'secret'))  # tuple!
ac.get_status = AsyncMock(return_value=status_payload)
```
Gotchas learned the hard way:
- `MagicMock(side_effect=lambda: coroutine)` is NOT awaitable as a method call in all positions — `await mock(...)` where the side_effect returns a coroutine works, but if production code does `token, secret = await ac.ensure_token()` and the mock returns a plain MagicMock, you get "object MagicMock can't be used in 'await' expression". AsyncMock is always safe.
- Match the real return shape: `ensure_token()` returns a `(token, shared_secret)` tuple — unpacking it against a scalar mock breaks silently or with TypeError.

## Tips / gotchas
- **`filters.TEXT & ~filters.COMMAND` silently drops slash commands** when no CommandHandler is registered for them — if your bot intercepts `/cmd` inside a plain text handler, register `MessageHandler(filters.TEXT, ...)` and parse the command yourself. A test with a real bot_command entity proves this both ways (old filter rejects, new filter matches).
- PTB 22.x emits `PTBDeprecationWarning` about `retry_after` type in some paths — harmless in tests; suppress or ignore.
- Run serially if pytest.ini pins xdist addopts: `python -m pytest tests/test_x.py -o addopts="" --timeout=60`.
- `MessageEntity(type='bot_command', offset=0, length=N)` — length must cover the command word (e.g. 5 for `/stop`); offset must be 0 or COMMAND won't match.
