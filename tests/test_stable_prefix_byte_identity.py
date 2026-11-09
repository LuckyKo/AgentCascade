"""L1 prefix-stability regression (BUG_0278).

Two guarantees the rest of the suite does not provide:

1. conversation[0] for a STABLE system-agent class (STABLE_SYSTEM_PROMPT_CLASSES) is
   byte-identical across runs with DIFFERENT callers. _build_session_metadata gates the
   per-run identity lines (Supervisor + log filename, own log path) out of the stable
   prefix, so two callers must not change conversation[0]. Every existing test drives a
   SINGLE supervisor, so none could see caller-driven volatility — which is exactly the
   class of bust this guards. (test_stable_class_conversation0_identical_across_two_callers)

2. The build-time lint (_lint_stable_prefix) actually fires: it rejects a volatile field
   that has leaked into a stable prefix, accepts a correctly-gated one, and does not
   false-positive on the per-instance-constant System line. The non-stable detector test
   proves the byte-identity harness itself is not vacuous.

Fixtures use a MagicMock pool (same idiom as test_session_metadata_fix.py). Production
path reproduced exactly: build_system_message + _inject_metadata_into_message, the same
two writers initialize_conversation uses to form conversation[0].
"""
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.lifecycle_manager import (
    _extract_stable_metadata_lines,
    _inject_metadata_into_message,
    _lint_stable_prefix,
    AgentLifecycleManager,
    PrefixStabilityError,
)
from agent_cascade.shared_init import STABLE_SYSTEM_PROMPT_CLASSES

# Fixed values so the only thing that can vary between renders is the caller.
FIXED_INSTANCE = 'stable_reuse_1'
FIXED_STARTED_AT = '2026-01-02 03:04'
FIXED_WORKDIR = r'C:/fixed/workspace'
# build_system_message replaces the first line with the instance identity; the rest is
# static soul text that must stay byte-identical between renders.
FIXED_SOUL = 'You are placeholder.\n\n# Role\nDeterministic soul text.\nYou write code.\n'


def _make_pool():
    """Minimal MagicMock pool sufficient for build_system_message + metadata injection."""
    pool = MagicMock()
    om = MagicMock()
    om.base_dir = FIXED_WORKDIR
    om.extra_work_folders_ro = []
    om.extra_work_folders_rw = []
    pool.operation_manager = om

    template = MagicMock()
    template.base_system_message = FIXED_SOUL
    pool.get_template.return_value = template

    log_inst = MagicMock()
    log_inst.data = {'metadata': {}}
    log_inst.log_path = '/fake/log.jsonl'
    pool.get_logger.return_value = log_inst
    return pool


def _render_conversation0(agent_class, instance_name, supervisor, started_at=FIXED_STARTED_AT):
    """Reproduce the production conversation[0] build and return its text.

    Mirrors initialize_conversation's two writers for the system message:
    build_system_message (identity + soul) then _inject_metadata_into_message
    (Session Metadata block). This is the exact artifact the KV prefix cache keys on.
    """
    pool = _make_pool()
    manager = AgentLifecycleManager(pool)
    sys_msg = manager.build_system_message(agent_class, instance_name)

    instance = AgentInstance(
        instance_name=instance_name,
        agent_class=agent_class,
        conversation=[],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        parent_instance=supervisor,
        system_started_at=started_at,
    )
    _inject_metadata_into_message(sys_msg, pool, instance)
    return sys_msg.content


def _insert_into_metadata_block(content, line, after_index=1):
    """Insert `line` into the `## Session Metadata` section of `content`.

    Used to simulate a broken runtime gate that leaks a volatile field into a stable
    prefix, so the lint's rejection path can be exercised deterministically.
    """
    lines = content.split('\n')
    start = next(i for i, l in enumerate(lines) if l.startswith('## Session Metadata'))
    lines.insert(start + after_index, line)
    return '\n'.join(lines)


# ── Deliverable 2: two-run, different-caller byte-identity ──────────────────

def test_stable_class_conversation0_identical_across_two_callers():
    """The core L1 invariant: a stable class renders identical conversation[0] for
    two different callers (the case single-caller tests cannot see)."""
    assert 'security' in STABLE_SYSTEM_PROMPT_CLASSES, (
        'security is no longer a stable class — this test no longer covers the gate')

    caller_a = _render_conversation0('security', FIXED_INSTANCE, 'OrchestratorA')
    caller_b = _render_conversation0('security', FIXED_INSTANCE, 'Maine')

    assert caller_a == caller_b, (
        'stable-class conversation[0] must be byte-identical across callers (KV prefix). '
        'Got differing output:\n'
        f'--- caller A ---\n{caller_a}\n--- caller B ---\n{caller_b}'
    )


def test_stable_class_conversation0_identical_across_callers_compressor():
    """Same invariant for the other stable class, to guard against per-class drift."""
    assert 'compressor' in STABLE_SYSTEM_PROMPT_CLASSES

    caller_a = _render_conversation0('compressor', 'compressor_1', 'OrchestratorA')
    caller_b = _render_conversation0('compressor', 'compressor_1', 'Maine')
    assert caller_a == caller_b, (
        'stable-class conversation[0] must be byte-identical across callers. Got:\n'
        f'--- caller A ---\n{caller_a}\n--- caller B ---\n{caller_b}'
    )


def test_nonstable_class_conversation0_differs_across_callers():
    """Detector / anti-vacuity guard.

    A NON-stable class DOES render the caller into conversation[0], so two callers must
    produce DIFFERENT bytes. This proves the byte-identity comparison above can actually
    fail when caller volatility is present — it is not a tautology that always passes.
    """
    assert 'coder' not in STABLE_SYSTEM_PROMPT_CLASSES

    caller_a = _render_conversation0('coder', FIXED_INSTANCE, 'OrchestratorA')
    caller_b = _render_conversation0('coder', FIXED_INSTANCE, 'Maine')

    assert caller_a != caller_b, (
        'sanity: a non-stable class should embed the caller (Supervisor:) and thus differ '
        'across callers; if it does not, the byte-identity detector is broken and the L1 '
        'tests above prove nothing')
    assert '- Supervisor: OrchestratorA' in caller_a, caller_a
    assert '- Supervisor: Maine' in caller_b, caller_b


# ── Deliverable 1: the build-time lint ──────────────────────────────────────

def test_lint_rejects_supervisor_leaked_into_stable_prefix():
    """Positive control: a Supervisor: field forced into the stable metadata section
    (simulating a broken runtime gate) must make the lint raise."""
    clean = _render_conversation0('security', FIXED_INSTANCE, 'OrchestratorA')
    broken = _insert_into_metadata_block(
        clean, '- Supervisor: Maine (log: orchestrator_20260101_010101.jsonl)')

    with pytest.raises(PrefixStabilityError):
        _lint_stable_prefix(broken)


def test_lint_rejects_log_path_leaked_into_stable_prefix():
    """Positive control: a per-run Your log Path: field forced into the stable prefix
    must make the lint raise."""
    clean = _render_conversation0('security', FIXED_INSTANCE, 'OrchestratorA')
    broken = _insert_into_metadata_block(
        clean, '- Your log Path: security_reuse_1_20260101_010101.jsonl')

    with pytest.raises(PrefixStabilityError):
        _lint_stable_prefix(broken)


def test_forced_volatile_breaks_byte_identity():
    """End-to-end red evidence: if a volatile field leaked into a "stable" prefix under
    two callers, the byte-identity check would catch it (they no longer match). Pairs with
    the lint rejection tests — together they show both the guard and the identity check
    fail on volatility and pass when it is correctly gated."""
    base = _render_conversation0('security', FIXED_INSTANCE, 'ignored')
    caller_x = _insert_into_metadata_block(
        base, '- Supervisor: CallerX (log: x_20260101_010101.jsonl)')
    caller_y = _insert_into_metadata_block(
        base, '- Supervisor: CallerY (log: y_20260101_010102.jsonl)')

    assert caller_x != caller_y, (
        'forced-volatile prefixes should differ; if they match, the byte-identity check '
        'cannot detect a broken stable gate')


def test_lint_accepts_correctly_gated_stable_prefix():
    """The real stable render must NOT raise — proves the lint is not always-firing."""
    clean = _render_conversation0('security', FIXED_INSTANCE, 'OrchestratorA')
    # No exception means pass.
    _lint_stable_prefix(clean)


# `_lint_stable_prefix` is exercised via the metadata-region extractor; assert the
# constant-line exception really covers the System line so this test stays meaningful.
_STABLE_SYSTEM_LINE_WITH_DATE = '- System: AgentCascade v9.9.9 — 2026-01-02 03:04'


def test_lint_scopes_wall_clock_to_constant_system_line():
    """The only date in a stable metadata section lives on the per-instance-constant
    System line. The wall-clock pattern must not trip on it (correct scoping, so the
    guard neither misses a real leak nor false-positives on the constant line)."""
    block = ('You are x.\n\n## Session Metadata\n'
             '- System: AgentCascade v9.9.9 — 2026-01-02 03:04\n'
             '- Working Dir: C:/fixed/workspace\n'
             'Use your logs to recall details from turns that were compressed.\n'
             '# Role\nsoul\n')
    # The constant line IS present and carries a date; lint must accept it.
    assert _STABLE_SYSTEM_LINE_WITH_DATE in block
    _lint_stable_prefix(block)  # must not raise

    # The same date on a NON-System line (a genuine leak) must be rejected.
    leaked = block.replace('Working Dir: C:/fixed/workspace',
                           'Working Dir: 2026-01-02 03:04')
    with pytest.raises(PrefixStabilityError):
        _lint_stable_prefix(leaked)


def test_lint_tool_schema_reordering_rejected_and_sorted_accepted():
    """The tool-schema-reordering guard (part of the L1 lint spec): a shuffled tool order
    busts the KV prefix just like a changed token, so it must be rejected; the canonical
    (sorted) order is accepted. The schema is assembled outside lifecycle_manager, so no
    production caller supplies it yet — the regression test is what drives this branch."""
    # Canonical order passes (no exception).
    _lint_stable_prefix('## Session Metadata\n', tool_names=['calculate', 'edit_file', 'read_file'])

    shuffled = ['edit_file', 'calculate', 'read_file']
    with pytest.raises(PrefixStabilityError):
        _lint_stable_prefix('## Session Metadata\n', tool_names=shuffled)


def test_metadata_trailer_is_a_non_bullet_so_scan_window_stops_there():
    """The scan window stops at the first non-`- ` line, which in the real render is the
    block trailer. If that trailer were ever reworded into a `- ` bullet, the scan window
    would silently widen and absorb following content. Assert it stays a non-bullet."""
    rendered = _render_conversation0('security', FIXED_INSTANCE, 'Maine')
    trailer = next((l for l in rendered.split('\n')
                    if l.startswith('Use your logs to recall')), None)
    assert trailer is not None, 'expected the session-metadata trailer in the render'
    assert not trailer.startswith('- '), (
        'session-metadata trailer became a `- ` bullet; _extract_stable_metadata_lines '
        'would now absorb it (and any following bullets) into the scanned region')
    # Sanity: the trailer terminates the scanned region — it is never itself scanned.
    scanned = _extract_stable_metadata_lines(rendered)
    assert trailer not in scanned
    assert scanned and all(s.startswith('- ') for s in scanned)


def test_metadata_line_extraction_is_bullets_only_and_stops_at_trailer():
    """The lint scans ONLY the contiguous `- ` bullets of the Session Metadata section,
    stopping at the trailer — so soul/skill prose is never scanned (scoping claim proven)."""
    rendered = _render_conversation0('security', FIXED_INSTANCE, 'Maine')
    lines = _extract_stable_metadata_lines(rendered)
    assert lines, 'expected metadata bullets to be extracted'
    assert all(line.startswith('- ') for line in lines), lines
    # The trailer terminates the bullet run and is NOT part of the scanned region.
    assert not any('Use your logs to recall' in line for line in lines), lines
    # No soul text leaks into the region.
    assert not any('Deterministic soul text' in line for line in lines), lines


def test_metadata_region_at_end_of_message_is_scanned():
    """Cover the boundary where the metadata bullets are the LAST lines of the message
    (no trailer / no soul body) — the region must still be captured, not dropped."""
    block = ('You are x.\n\n## Session Metadata\n'
             '- System: AgentCascade v1.0 — 2026-01-02 03:04\n'
             '- Your log Path: leaked_20260102_030400.jsonl')
    lines = _extract_stable_metadata_lines(block)
    assert any('Your log Path:' in line for line in lines), lines
    with pytest.raises(PrefixStabilityError):
        _lint_stable_prefix(block)


# Real production soul templates live here; read them straight from the repo (hermetic,
# no pool/Assistant/llm_cfg needed). Using the real bodies makes the scoping test
# adversarial: souls contain `##` headings and `- ` bullets.
_REAL_SOULS = {
    'security': 'Security_soul.md',
    'compressor': 'Compressor_soul.md',
}


def _soul_body(filename):
    """Return the markdown body of a soul file (frontmatter stripped), like build_system_prompt."""
    path = Path(__file__).resolve().parents[1] / 'agents' / filename
    text = path.read_text(encoding='utf-8')
    # soul files start with a `---\n...\n---\n` YAML frontmatter block.
    parts = text.split('---', 2)
    return parts[2].strip()


def _render_with_soul(agent_class, instance_name, supervisor, soul_body):
    """conversation[0] built the production way, but over a caller-supplied soul body."""
    pool = _make_pool()
    pool.get_template.return_value.base_system_message = soul_body
    manager = AgentLifecycleManager(pool)
    sys_msg = manager.build_system_message(agent_class, instance_name)
    instance = AgentInstance(
        instance_name=instance_name, agent_class=agent_class, conversation=[],
        created_at=time.monotonic(), last_activity=time.monotonic(),
        latest_marker_index=-1, parent_instance=supervisor,
        system_started_at=FIXED_STARTED_AT,
    )
    _inject_metadata_into_message(sys_msg, pool, instance)
    return sys_msg.content


def test_real_stable_soul_lint_clean_and_soul_prose_not_scanned():
    """Robustness proof for the scan scoping (review finding 2).

    Build conversation[0] over the REAL Security/Compressor soul bodies: it must be lint
    clean. Plant a `YYYY-MM-DD` date deep in the soul body — it must STILL be clean (the
    date lives in soul prose, which the bullet-only scan never reads). Then plant the same
    date on a metadata bullet (simulating a broken gate) — that must be rejected. This shows
    the guard neither false-positives on real soul text nor misses a genuine metadata leak,
    and that the guard cannot crash a live turn on an innocent soul edit.
    """
    planted_date = '2031-12-25'
    for agent_class, filename in _REAL_SOULS.items():
        soul = _soul_body(filename)

        clean = _render_with_soul(agent_class, 'stable_reuse_1', 'Maine', soul)
        _lint_stable_prefix(clean)  # must not raise

        # Date planted in the soul body (not a metadata bullet) must NOT trip the guard.
        soul_with_date = soul + f'\nChangelog: {planted_date} something\n'
        with_date_in_soul = _render_with_soul(
            agent_class, 'stable_reuse_1', 'Maine', soul_with_date)
        assert planted_date in with_date_in_soul  # sanity: it really is present
        _lint_stable_prefix(with_date_in_soul)     # must NOT raise (soul prose not scanned)

        # Same date on a metadata bullet (a real leak) MUST be rejected.
        leaked = _insert_into_metadata_block(clean, f'- Some Date Field: {planted_date}')
        with pytest.raises(PrefixStabilityError):
            _lint_stable_prefix(leaked)
