"""Extensible slash-command dispatcher for the Telegram bridge (Phase 2).

Registered system commands are intercepted by ``bot.on_message`` BEFORE any agent
injection, dispatched through the ``COMMANDS`` registry below, and answered
directly. They NEVER reach the AC agent and are never logged as agent messages —
the only path into AC from this module is the thin ``ACClient`` REST methods
(stop/restart/...), never ``inject_message``.

Unregistered ``/...`` commands (e.g. ``/compress x``) are NOT rejected here: they
fall through (dispatch returns None) so ``bot.on_message`` forwards them to the AC
agent via ``inject_message`` — AC-side slash commands keep working from Telegram.

Design for easy expansion: adding a command = one ``CommandHandler`` entry in
``COMMANDS``. No if/elif chains anywhere; /help is generated from the registry, so
new commands appear automatically.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from agent_cascade.log import logger

from .ac_client import ACClient, ACError
from .config import BridgeConfig


@dataclass
class CommandContext:
    """Everything a command handler needs to do its job."""

    ac: ACClient
    cfg: BridgeConfig
    args: str = ''  # raw text after the command word (may be empty)


@dataclass
class CommandHandler:
    """One slash-command: name, /help description, and an async run()."""

    name: str
    description: str
    run: Any  # async callable(CommandContext) -> str; kept untyped to avoid import cycles

    async def __call__(self, ctx: CommandContext) -> str:
        return await self.run(ctx)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _cmd_new(ctx: CommandContext) -> str:
    await ctx.ac.reset()
    return '🆕 New session started'


def _approval_label(approval: Dict[str, Any]) -> str:
    """Short human label for a pending approval: '<tool> (<request_id>)'."""
    tool = approval.get('tool_name') or 'tool'
    rid = approval.get('request_id') or '?'
    return f'{tool} ({rid})'


def _find_pending(status: Dict[str, Any], arg: str) -> Optional[Dict[str, Any]]:
    """Pick the pending approval to act on.

    With an arg: exact match on request_id, else substring match on request_id /
    tool_name (first hit). Without an arg: the first pending approval.
    """
    pending = status.get('pending_approvals') or []
    if not pending:
        return None
    if not arg:
        return pending[0]
    for a in pending:
        if str(a.get('request_id', '')) == arg:
            return a
    for a in pending:
        hay = f"{a.get('request_id', '')} {a.get('tool_name', '')}"
        if arg.lower() in hay.lower():
            return a
    return None


async def _cmd_status(ctx: CommandContext) -> str:
    token, _secret = await ctx.ac.ensure_token()
    status = await ctx.ac.get_status(token)
    lines: List[str] = []
    if status.get('generating'):
        lines.append(f"🏃 Generating (active agent: {status.get('active_agent') or '?'})")
    else:
        lines.append('💤 Idle (not generating)')
    pending = status.get('pending_approvals') or []
    if pending:
        lines.append(f'⏳ Pending approvals ({len(pending)}):')
        for a in pending[:10]:
            lines.append(f"  • {_approval_label(a)}")
        if len(pending) > 10:
            lines.append(f'  … and {len(pending) - 10} more')
    else:
        lines.append('⏳ No pending approvals')
    return '\n'.join(lines)


async def _cmd_yes(ctx: CommandContext) -> str:
    token, _secret = await ctx.ac.ensure_token()
    status = await ctx.ac.get_status(token)
    approval = _find_pending(status, ctx.args.strip())
    if approval is None:
        return 'Nothing pending to approve.'
    result = await ctx.ac.approve(approval['request_id'])
    outcome = result.get('result', '') if isinstance(result, dict) else ''
    if isinstance(outcome, str) and outcome.startswith('ERROR'):
        # "already resolved" is a benign no-op (race with UI or a double /yes).
        logger.info('/yes: %s', outcome)
        return '✅ Already resolved.'
    return f"✅ Approved {_approval_label(approval)}"


async def _cmd_no(ctx: CommandContext) -> str:
    token, _secret = await ctx.ac.ensure_token()
    status = await ctx.ac.get_status(token)
    approval = _find_pending(status, ctx.args.strip())
    if approval is None:
        return 'Nothing pending to reject.'
    result = await ctx.ac.reject(approval['request_id'])
    outcome = result.get('result', '') if isinstance(result, dict) else ''
    if isinstance(outcome, str) and outcome.startswith('ERROR'):
        logger.info('/no: %s', outcome)
        return '❌ Already resolved.'
    return f"❌ Rejected {_approval_label(approval)}"


async def _cmd_stop(ctx: CommandContext) -> str:
    await ctx.ac.stop()
    return '🛑 Stopped the current agent'


async def _cmd_restart(ctx: CommandContext) -> str:
    await ctx.ac.restart()
    return '🔄 Restarting AC server…'


def _build_help_text() -> str:
    lines = ['Available commands:']
    for cmd in COMMANDS.values():
        lines.append(f"/{cmd.name} — {cmd.description}")
    return '\n'.join(lines)


async def _cmd_help(ctx: CommandContext) -> str:
    return _build_help_text()


def _parse_on_off(arg: str) -> Optional[bool]:
    a = (arg or '').strip().lower()
    if a in ('on', '1', 'true', 'yes'):
        return True
    if a in ('off', '0', 'false', 'no'):
        return False
    return None


async def _cmd_afk(ctx: CommandContext) -> str:
    parts = ctx.args.split()
    if not parts:
        return 'Usage: /afk on|off [seconds]'
    enabled = _parse_on_off(parts[0])
    if enabled is None:
        return 'Usage: /afk on|off [seconds]'
    timeout_seconds: Optional[int] = None
    if len(parts) > 1:
        try:
            timeout_seconds = int(parts[1])
        except ValueError:
            return 'Seconds must be an integer, e.g. /afk on 300'
    result = await ctx.ac.set_afk(enabled, timeout_seconds=timeout_seconds)
    if enabled:
        t = (result or {}).get('timeout_seconds')
        extra = f' (auto-reject after {t}s)' if t else ''
        return f'🌙 AFK mode on{extra}'
    return '🌙 AFK mode off (waiting for approval is unlimited)'


async def _cmd_security(ctx: CommandContext) -> str:
    enabled = _parse_on_off(ctx.args)
    if enabled is None:
        return 'Usage: /security on|off'
    await ctx.ac.set_auto_security(enabled)
    return '🔒 Auto-security ON' if enabled else '🔓 Auto-security OFF'


async def _cmd_restore(ctx: CommandContext) -> str:
    name = ctx.args.strip()
    if not name:
        return 'Usage: /restore <session-name>'
    await ctx.ac.restore_session(name)
    return f"📂 Restored session '{name}'"


# ---------------------------------------------------------------------------
# Registry — adding a command is one entry here.
# ---------------------------------------------------------------------------

COMMANDS: Dict[str, CommandHandler] = {
    c.name: c for c in [
        CommandHandler('new', 'Start a fresh AC session (reset)', _cmd_new),
        CommandHandler('status', 'Show generating state and pending approvals', _cmd_status),
        CommandHandler('yes', 'Approve the first (or named) pending approval', _cmd_yes),
        CommandHandler('no', 'Reject the first (or named) pending approval', _cmd_no),
        CommandHandler('stop', 'Stop the current agent', _cmd_stop),
        CommandHandler('restart', 'Restart the AC server', _cmd_restart),
        CommandHandler('afk', 'Toggle AFK auto-reject: /afk on|off [seconds]', _cmd_afk),
        CommandHandler('security', 'Toggle auto-ask security: /security on|off', _cmd_security),
        CommandHandler('restore', 'Restore a saved session: /restore <name>', _cmd_restore),
        CommandHandler('help', 'List all commands (alias: /?)', _cmd_help),
    ]
}

# Aliases: bare '?' is the help command.
_ALIASES: Dict[str, str] = {'?': 'help'}


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    """Parse ``/name arg1 arg2`` into ``(command_name, args)``.

    Returns None for non-command text (anything not starting with '/').
    Aliases are resolved ('/?' -> 'help'). Case-insensitive command names.
    """
    if not text or not text.startswith('/'):
        return None
    body = text[1:]
    # Telegram may append a bot mention for group chats: /cmd@botname — strip it.
    word, _, rest = body.partition(' ')
    name = word.split('@', 1)[0].lower()
    name = _ALIASES.get(name, name)
    return name, rest.strip()


async def dispatch_command(text: str, ac: ACClient, cfg: BridgeConfig) -> Optional[str]:
    """Handle one message if it is a registered command.

    Returns the reply text to send to Telegram, or None when the caller should
    fall through to the normal inject-and-wait flow — both for non-command text
    and for UNREGISTERED ``/...`` commands (forwarded to the agent so AC-side
    slash commands like /compress x reach it). Registered commands are intercepted
    and answered directly. AC errors are converted to short human-readable failure
    replies — this function never raises and never touches ``ac.inject_message``.
    """
    parsed = parse_command(text)
    if parsed is None:
        return None
    name, args = parsed
    cmd = COMMANDS.get(name)
    if cmd is None:
        # Unregistered slash command: forward to the agent (fall through). This lets
        # AC-side slash commands like /compress x reach the agent instead of being
        # rejected here. Returns None so bot.on_message proceeds to inject_message.
        logger.info('Unregistered command /%s forwarded to agent', name)
        return None

    ctx = CommandContext(ac=ac, cfg=cfg, args=args)
    try:
        reply = await cmd(ctx)
    except ACError as e:
        logger.error('command /%s failed: %s', name, e)
        return f"⚠️ /{name} failed: {e}"
    except Exception as e:  # noqa: BLE001 - a handler bug must not kill the bot
        logger.exception('command /%s raised unexpectedly', name)
        return f'⚠️ /{name} hit an unexpected error.'
    logger.info('command /%s handled (args=%r)', name, args[:80])
    return reply
