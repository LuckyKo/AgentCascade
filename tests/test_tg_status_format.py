"""Tests for Telegram /status active-agent rendering helpers.

Covers plan §6.2 (T7–T9): _fmt_usage compact counts, _active_inst_line formatting
(with/without tokens + unknown-state fallback), and _cmd_status output when agents
are present vs. empty.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from agent_cascade.telegram_bridge.commands import (
    _TG_STATE_ICONS,
    _active_inst_line,
    _cmd_status,
    _fmt_usage,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestFmtUsage:

    def test_t7_boundaries(self):
        assert _fmt_usage(0) == '0'
        assert _fmt_usage(999) == '999'
        assert _fmt_usage(12400) == '12.4k'
        assert _fmt_usage(1_200_000) == '1.2M'

    def test_none_returns_empty(self):
        assert _fmt_usage(None) == ''


class TestActiveInstLine:

    def test_t8_with_tokens_running(self):
        line = _active_inst_line({
            'name': 'coder', 'agent_class': 'coder', 'state': 'RUNNING',
            'turn': 3, 'max_turns': 250, 'tokens': 12400, 'words': 100,
        })
        assert _TG_STATE_ICONS['RUNNING'] in line
        assert 'coder' in line
        assert '[3/250]' in line
        assert '~12.4ktok' in line

    def test_t8_without_tokens(self):
        line = _active_inst_line({
            'name': 'researcher', 'state': 'SLEEPING', 'turn': 1,
            'max_turns': 250, 'tokens': 0, 'words': 0,
        })
        assert '[1/250]' in line
        # tokens=0 -> _fmt_usage(0) == '0' (truthy as a string), so "~0tok" appears.
        assert '~0tok' in line

    def test_t8_unknown_state_falls_back_to_bullet(self):
        line = _active_inst_line({
            'name': 'mystery', 'state': 'SOME_NEW_STATE', 'turn': 0,
            'max_turns': None, 'tokens': 500,
        })
        # Unknown state -> default bullet '•' (U+2022), not a KeyError.
        assert '\u2022' in line
        assert 'mystery' in line
        # max_turns None -> no [turn/max] segment
        assert '[' not in line


class TestCmdStatus:

    def _ctx(self, status):
        ac = MagicMock()
        ac.ensure_token = AsyncMock(return_value=('tok', b'secret'))
        ac.get_status = AsyncMock(return_value=status)
        ctx = MagicMock()
        ctx.ac = ac
        return ctx

    def test_t9_with_active_agents(self):
        status = {
            'generating': True,
            'active_agent': 'orchestrator',
            'active_instances': [
                {'name': 'coder', 'agent_class': 'coder', 'state': 'RUNNING',
                 'turn': 3, 'max_turns': 250, 'tokens': 12400, 'words': 100},
                {'name': 'researcher', 'agent_class': 'researcher', 'state': 'SLEEPING',
                 'turn': 1, 'max_turns': 250, 'tokens': 3200, 'words': 50},
            ],
            'pending_approvals': [],
        }
        out = _run(_cmd_status(self._ctx(status)))
        assert '🏃 Generating' in out
        assert '🤖 Active agents (2):' in out
        # each agent row carries name + [turn/max] + usage
        assert 'coder' in out and '[3/250]' in out and '~12.4ktok' in out
        assert 'researcher' in out and '[1/250]' in out and '~3.2ktok' in out
        # approvals still rendered
        assert '⏳ No pending approvals' in out

    def test_t9_empty_active_agents(self):
        status = {'generating': False, 'active_instances': [], 'pending_approvals': []}
        out = _run(_cmd_status(self._ctx(status)))
        assert '💤 Idle (not generating)' in out
        assert '🤖 No active agents' in out

    def test_t9_missing_key_defaults_to_empty(self):
        # Old clients / partial payloads: absent key -> "No active agents", no crash.
        status = {'generating': False, 'pending_approvals': []}
        out = _run(_cmd_status(self._ctx(status)))
        assert '🤖 No active agents' in out

    def test_t9_cap_at_8_with_more(self):
        insts = [
            {'name': f'agent{i}', 'state': 'RUNNING', 'turn': i,
             'max_turns': 250, 'tokens': 100} for i in range(10)
        ]
        status = {'generating': True, 'active_agent': 'x',
                  'active_instances': insts, 'pending_approvals': []}
        out = _run(_cmd_status(self._ctx(status)))
        assert '🤖 Active agents (10):' in out
        # only 8 rows shown
        for i in range(8):
            assert f'agent{i}' in out
        assert 'agent8' not in out
        assert 'agent9' not in out
        assert '… and 2 more' in out
