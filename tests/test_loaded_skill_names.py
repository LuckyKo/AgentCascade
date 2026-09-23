"""Regression tests for recording loaded-skill names on an AgentInstance.

The auto-skill reflection prompt (``prompts/dna.py``) renders a "Skills loaded this
run" list from ``instance._loaded_skill_names`` during extended turns. Two paths feed
that field and both were previously missing:

1. **Runtime ``load_skill``** (Gap 1): the tool queued skill content but never recorded
   the names, so mid-task loads were invisible to the reflection list.
2. **Root/main agent injection** (Gap 2): only the sub-agent spawn path in ``core.py``
   set the field; ``_inject_self_augmentation_skill`` (the root path) never did, so the
   root agent's list was always "(none)".

These tests drive the real production code deterministically (no LLM, no filesystem
skill scans): ``LoadSkill.call`` is bound onto a real tool instance with a stubbed pool,
and ``_inject_self_augmentation_skill`` is called with a real ``AgentInstance`` and a
stubbed pool. Stubs expose only the attributes each code path touches (same technique
as ``test_candidate_eval_gate.py``).
"""

import time
from types import SimpleNamespace

from agent_cascade.agent_instance import AgentInstance
from agent_cascade.engine.helpers import (
    _SKILL_MSG_PREFIX,
    _inject_self_augmentation_skill,
    _merge_loaded_skill_names,
    rederive_loaded_skill_names,
)
from agent_cascade.llm.schema import Message
from agent_cascade.tools.custom.load_skill import LoadSkill


# ===========================================================================
# Change A — runtime load_skill records names on the instance
# ===========================================================================


def _make_pool(inst, skill_bodies):
    """Build a minimal pool exposing only what ``LoadSkill.call`` touches.

    ``skill_bodies`` maps name -> body string; a missing name (or None) means "not
    found" so the tool routes it to the failed list. Returns ``(pool, enqueued)`` where
    ``enqueued`` records every ``(agent_name, content)`` passed to ``enqueue_message``.
    """
    def _load_full(name):
        """Mirror SkillManager.load_full_instructions: exact match first, then a
        case-insensitive fallback (manager.py:716-723). Returns the body for the
        canonical registry name, or None if not found."""
        if name in skill_bodies:
            return skill_bodies[name]
        lower = name.lower()
        for key, body in skill_bodies.items():
            if key.lower() == lower:
                return body
        return None

    skill_manager = SimpleNamespace(
        _ensure_discovered=lambda: None,
        load_full_instructions=_load_full,
        # Stub must expose every attribute the real LoadSkill.call path touches. No skill in
        # these fixtures is disabled, so this always returns False (mirrors SkillManager).
        is_skill_disabled=lambda name: False,
    )
    enqueued = []
    pool = SimpleNamespace(
        skill_manager=skill_manager,
        get_instance=lambda name: inst,
        enqueue_message=lambda name, content: enqueued.append((name, content)),
        telemetry=None,  # skip the telemetry branch; we only care about _loaded_skill_names
    )
    return pool, enqueued


def _make_inst(existing):
    """An AgentInstance-like object exposing the field under test."""
    return SimpleNamespace(_loaded_skill_names=existing, agent_class='coder')


def _call_load_skill(inst, skill_names, skill_bodies):
    """Run the real ``LoadSkill.call`` against a stub pool and return ``(result, enqueued)``."""
    pool, enqueued = _make_pool(inst, skill_bodies)
    tool = LoadSkill(agent_pool=pool)  # real unbound call() bound onto this instance
    result = tool.call({'skill_names': skill_names}, agent_instance_name='Main')
    return result, enqueued


class TestLoadSkillRecordsNames:
    """``LoadSkill.call`` must record successfully-loaded names on the instance."""

    def test_records_newly_loaded_name(self):
        inst = _make_inst(None)
        result, _ = _call_load_skill(inst, ['skill-a'], {'skill-a': 'BODY-A'})
        assert 'Successfully loaded 1 skill(s)' in result
        assert inst._loaded_skill_names == ['skill-a']

    def test_preserves_preexisting_init_names(self):
        """Init-time AUTO-matched names must survive a runtime load (not be clobbered)."""
        inst = _make_inst(['init-skill'])
        _call_load_skill(inst, ['skill-a'], {'skill-a': 'BODY-A'})
        assert inst._loaded_skill_names == ['init-skill', 'skill-a']

    def test_dedupes_without_reordering(self):
        """A name already on the instance must not be added twice."""
        inst = _make_inst(['skill-a'])
        _call_load_skill(inst, ['skill-a'], {'skill-a': 'BODY-A'})
        assert inst._loaded_skill_names == ['skill-a']

    def test_failed_names_not_recorded(self):
        """Not-found / failed names must not be added to the list."""
        inst = _make_inst(None)
        result, _ = _call_load_skill(inst, ['skill-a', 'missing'], {'skill-a': 'BODY-A'})
        assert 'Failed to load 1 skill(s)' in result
        assert inst._loaded_skill_names == ['skill-a']

    def test_all_failed_leaves_field_untouched(self):
        """If nothing loads, the field is left exactly as it was (None stays None)."""
        inst = _make_inst(None)
        _call_load_skill(inst, ['missing'], {})
        assert inst._loaded_skill_names is None

    def test_enqueues_only_loaded_skills(self):
        """Sanity: only successfully-loaded skills are queued as messages."""
        inst = _make_inst(None)
        _, enqueued = _call_load_skill(inst, ['skill-a', 'missing'], {'skill-a': 'BODY-A'})
        assert len(enqueued) == 1
        assert enqueued[0][0] == 'Main'


# ===========================================================================
# Change C — runtime load_skill dedups: no double-injection of the same skill
# ===========================================================================


class TestLoadSkillDedup:
    """A skill must be injected as a USER message at most once per instance.

    Regression for the duplicate "Apply the above guidelines to your current task."
    line: the closing line is appended once per loop iteration with no dedup guard, so
    re-loading an already-active skill (or listing it twice in one call) produced two
    copies of the same user message. The fix keys on ``inst._loaded_skill_names``
    (seeded at init for self-augmentation / AUTO-matched skills and appended by prior
    runtime loads) plus a per-call ``seen`` set, comparing case-insensitively against
    the resolved canonical name.
    """

    def test_same_skill_twice_in_one_call_injected_once(self):
        """(a) Listing the same skill twice in one call must enqueue exactly once."""
        inst = _make_inst(None)
        result, enqueued = _call_load_skill(inst, ['skill-a', 'skill-a'], {'skill-a': 'BODY-A'})
        assert len(enqueued) == 1
        # The closing line appears exactly once across all injected messages.
        total_closing = sum(content.count('Apply the above guidelines to your current task.')
                            for _, content in enqueued)
        assert total_closing == 1
        # Second occurrence is reported as already-loaded, not failed or loaded.
        assert 'Successfully loaded 1 skill(s)' in result
        assert 'Already loaded (skipped): skill-a' in result
        assert 'Failed to load' not in result

    def test_already_loaded_at_init_not_reinjected(self):
        """(b) A skill already in _loaded_skill_names must NOT be re-enqueued."""
        inst = _make_inst(['skill-a'])  # e.g. injected at init-time (self-augmentation / AUTO)
        result, enqueued = _call_load_skill(inst, ['skill-a'], {'skill-a': 'BODY-A'})
        assert len(enqueued) == 0  # no second user message
        assert 'Already loaded (skipped): skill-a' in result
        assert 'Failed to load' not in result
        assert inst._loaded_skill_names == ['skill-a']  # field untouched, no dup

    def test_already_loaded_summary_dedupes_repeats(self):
        """A skill listed multiple times while already active must appear once in the summary."""
        inst = _make_inst(['skill-a'])
        result, enqueued = _call_load_skill(inst, ['skill-a', 'skill-a'], {'skill-a': 'BODY-A'})
        assert len(enqueued) == 0
        # Exactly one occurrence of skill-a in the skipped list (not "skill-a, skill-a").
        assert result.count('skill-a') == 1

    def test_case_insensitive_dedup(self):
        """(c) Casing variations of the same skill dedup (Self-Augmentation vs self-augmentation)."""
        inst = _make_inst(['self-augmentation'])  # canonical name seeded at init
        result, enqueued = _call_load_skill(
            inst, ['Self-Augmentation'], {'self-augmentation': 'BODY-SA'})
        assert len(enqueued) == 0  # case-insensitive match against the active skill
        assert 'Already loaded (skipped)' in result
        assert 'Failed to load' not in result

    def test_case_insensitive_dedup_within_one_call(self):
        """(c, within-call) Different casings of the same skill in one call → injected once."""
        inst = _make_inst(None)
        result, enqueued = _call_load_skill(
            inst, ['Code-Review', 'code-review'], {'code-review': 'BODY-CR'})
        assert len(enqueued) == 1
        total_closing = sum(content.count('Apply the above guidelines to your current task.')
                            for _, content in enqueued)
        assert total_closing == 1
        assert 'Already loaded (skipped)' in result

    def test_distinct_skills_still_all_load(self):
        """Guard: dedup must not over-suppress genuinely different skills."""
        inst = _make_inst(None)
        result, enqueued = _call_load_skill(
            inst, ['skill-a', 'skill-b'], {'skill-a': 'BODY-A', 'skill-b': 'BODY-B'})
        assert len(enqueued) == 2
        assert 'Successfully loaded 2 skill(s)' in result
        assert 'Already loaded' not in result


# ===========================================================================
# Change B — root-agent self-augmentation injection seeds the field
# ===========================================================================


def _make_self_aug_pool(load_result='SELF-AUG-BODY', load_skill_mode='AUTO'):
    """Build a pool whose ``skill_manager`` returns ``load_result`` for self-augmentation."""
    skill_manager = SimpleNamespace(
        _ensure_discovered=lambda: None,
        load_full_instructions=lambda name: load_result if name == 'self-augmentation' else None,
        # Stub must expose every attribute the real LoadSkill.call path touches (see _make_pool).
        is_skill_disabled=lambda name: False,
    )
    return SimpleNamespace(
        settings=SimpleNamespace(default_load_skill_mode=load_skill_mode),
        skill_manager=skill_manager,
        telemetry=None,  # skip the telemetry branch; we only care about _loaded_skill_names
    )


def _make_root_inst(existing):
    """A real root AgentInstance with a fresh system message (no '## Active Skills' yet)."""
    return AgentInstance(
        instance_name='Main',
        agent_class='Orchestrator',
        conversation=[Message(role='system', content='# Base system prompt\n\nSome content.')],
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        _loaded_skill_names=existing,
    )


class TestSelfAugSeedsNames:
    """``_inject_self_augmentation_skill`` must seed the field on the root path."""

    def test_seeds_when_field_is_none(self):
        inst = _make_root_inst(None)
        injected = _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert injected is True
        assert inst._loaded_skill_names == ['self-augmentation']

    def test_preserves_existing_and_no_duplicate(self):
        """A pre-existing name survives; self-augmentation is added once, not twice."""
        inst = _make_root_inst(['init-skill'])
        injected = _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert injected is True
        assert inst._loaded_skill_names == ['init-skill', 'self-augmentation']

    def test_no_duplicate_when_already_present(self):
        inst = _make_root_inst(['self-augmentation'])
        # Force a fresh injection (idempotency guard would otherwise skip); the seed
        # logic must still not duplicate the name.
        injected = _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert injected is True
        assert inst._loaded_skill_names == ['self-augmentation']

    def test_seeds_even_when_injection_skipped_by_idempotency_guard(self):
        """Session restore: system msg already has '## Active Skills' → injection is skipped
        (returns False) but the seed must still run so the reflection list isn't '(none)'."""
        inst = AgentInstance(
            instance_name='Main',
            agent_class='Orchestrator',
            conversation=[Message(role='system', content=(
                '# Base system prompt\n\n## Active Skills\n\n### Skill self-augmentation\nsome body'))],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
            _loaded_skill_names=None,
        )
        injected = _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert injected is False          # idempotency guard skipped the injection
        assert inst._loaded_skill_names == ['self-augmentation']   # but the seed still ran

    def test_field_untouched_when_skills_disabled(self):
        """NONE mode skips injection entirely; the field must be left exactly as-is."""
        inst = _make_root_inst(['init-skill'])
        injected = _inject_self_augmentation_skill(
            _make_self_aug_pool(load_skill_mode='NONE'), inst)
        assert injected is False
        assert inst._loaded_skill_names == ['init-skill']

    def test_field_untouched_when_skill_not_found(self):
        """If self-augmentation isn't in the registry, injection fails and field is untouched."""
        inst = _make_root_inst(None)
        injected = _inject_self_augmentation_skill(
            _make_self_aug_pool(load_result=None), inst)
        assert injected is False
        assert inst._loaded_skill_names is None


# ===========================================================================
# todo.md:148 — restore/external-load dedup regression (root-cause fix §5.1–§5.3)
# ===========================================================================
#
# Root cause: on session restore / external load a FRESH AgentInstance is built whose
# conversation already contains prior runtime-injected skill user-messages, but
# ``_loaded_skill_names`` is re-seeded with init-time/self-aug skills only. The runtime
# dedup guard (load_skill.py) reads only that field, so it can't see the prior runtime
# loads and re-injects a duplicate "Apply the above guidelines…" message. The fix
# re-derives ``_loaded_skill_names`` from the conversation at the two restore chokepoints
# via the shared helper ``rederive_loaded_skill_names``. These tests are revert-proof:
# T1/T3/T4 assert the *absent* post-fix state, so they FAIL pre-fix and PASS post-fix.


def _skill_user_msg(name, body='SKILL-BODY'):
    """A runtime-injected skill USER message, built via the shared header constant so it
    matches exactly what ``LoadSkill.call`` enqueues."""
    return Message(role='user', content=(
        f"{_SKILL_MSG_PREFIX}{name}\n\n{body}\n\nApply the above guidelines to your current task."))


def _make_inst_with_history(existing, injected_user_msgs):
    """A real AgentInstance whose conversation = [system] + ``injected_user_msgs`` and whose
    ``_loaded_skill_names`` field is ``existing``. Models the post-restore state where the
    history already holds runtime-injected skills but the field does NOT yet list them."""
    return AgentInstance(
        instance_name='Main',
        agent_class='Orchestrator',
        conversation=[Message(role='system', content='# Base system prompt\n\nSome content.')]
                     + list(injected_user_msgs),
        created_at=time.monotonic(),
        last_activity=time.monotonic(),
        latest_marker_index=-1,
        _loaded_skill_names=existing,
    )


class TestRederiveHelper:
    """T2 — unit tests for the pure ``rederive_loaded_skill_names`` helper (§5.1)."""

    def test_extracts_user_injected_skills_only(self):
        msgs = [
            Message(role='system', content='## Active Skills\n### Skill a\nbody'),  # SYSTEM: ignored
            _skill_user_msg('a'),
            _skill_user_msg('b'),
            Message(role='user', content='just a plain user message'),  # no header: ignored
        ]
        assert rederive_loaded_skill_names(msgs) == ['a', 'b']

    def test_empty_and_none_safe(self):
        assert rederive_loaded_skill_names([]) == []
        assert rederive_loaded_skill_names(None) == []

    def test_dedupes_repeats_preserving_order(self):
        msgs = [_skill_user_msg('a'), _skill_user_msg('b'), _skill_user_msg('a')]
        assert rederive_loaded_skill_names(msgs) == ['a', 'b']

    def test_ignores_system_active_skills_block(self):
        """Init-time skills live in the SYSTEM message and must never be picked up."""
        msgs = [Message(role='system', content='## Active Skills\n### Skill self-augmentation\nbody')]
        assert rederive_loaded_skill_names(msgs) == []

    def test_prefix_collision_yields_both(self):
        """Skills ``a`` and ``ab`` must BOTH be returned (guards against a startswith bug)."""
        msgs = [_skill_user_msg('a'), _skill_user_msg('ab')]
        assert rederive_loaded_skill_names(msgs) == ['a', 'ab']

    def test_non_string_content_ignored(self):
        """A USER message with non-str content (e.g. a list) must be skipped, not crash."""
        # A raw dict stands in for a Message whose content is a non-str payload; the helper
        # must skip it (isinstance(content, str) guard) without raising.
        msgs = [{'role': 'user', 'content': [{'type': 'text', 'text': _SKILL_MSG_PREFIX + 'a'}]},
                _skill_user_msg('b')]
        assert rederive_loaded_skill_names(msgs) == ['b']


class TestRestoreDedupRegression:
    """T1/T3/T4/T6 — the restore/external-load dedup regression, revert-proof."""

    def test_t1_runtime_reload_after_restore_not_reinjected(self):
        """CORE REGRESSION. Post-restore state: the conversation already holds skill X's injected
        user-msg, and the restore seed (chokepoint B) has populated ``_loaded_skill_names``. The
        runtime tool then reads that field to dedup — so re-loading X must NOT re-enqueue and must
        be reported as already loaded.

        Revert-proof: pre-fix the seed produced only ['self-augmentation'] (X dropped), so the
        guard can't see X and a 2nd copy is enqueued (FAIL). Post-fix the seed folds in X →
        ['skill-x','self-augmentation'], the guard sees it, no re-injection (PASS)."""
        inst = _make_inst_with_history(None, [_skill_user_msg('skill-x')])
        # The restore boundary runs chokepoint B first — this is what populates the field that
        # the runtime load_skill tool then reads for its dedup guard.
        _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert 'skill-x' in (inst._loaded_skill_names or [])  # seed recovered X from history
        result, enqueued = _call_load_skill(inst, ['skill-x'], {'skill-x': 'BODY-X'})
        assert len(enqueued) == 0, f"skill re-injected after restore: {enqueued}"
        assert 'Already loaded (skipped): skill-x' in result
        assert 'Successfully loaded' not in result

    def test_t3_root_restore_seeds_conversation_skills(self):
        """ROOT RESTORE. ``_inject_self_augmentation_skill`` on a restored instance (system has
        '## Active Skills', conversation has runtime skill X, field=None) must seed the field to
        ['X','self-augmentation'] in §5.3 order. FAILS pre-fix (['self-augmentation']), PASSES."""
        inst = AgentInstance(
            instance_name='Main',
            agent_class='Orchestrator',
            conversation=[Message(role='system', content=(
                '# Base system prompt\n\n## Active Skills\n\n### Skill self-augmentation\nsome body'))]
                         + [_skill_user_msg('skill-x')],
            created_at=time.monotonic(),
            last_activity=time.monotonic(),
            latest_marker_index=-1,
            _loaded_skill_names=None,
        )
        injected = _inject_self_augmentation_skill(_make_self_aug_pool(), inst)
        assert injected is False  # idempotency guard skipped the injection (restore)
        assert inst._loaded_skill_names == ['skill-x', 'self-augmentation']

    def test_t4_subagent_rebuild_merge_preserves_prior(self):
        """SUB-AGENT REBUILD (§5.2). Drive the production merge helper: conversation has prior
        runtime X, loaded_skills=[('self-augmentation',…)] → field preserves X + adds self-aug.
        FAILS pre-fix (X dropped), PASSES post-fix."""
        inst = _make_inst_with_history(None, [_skill_user_msg('skill-x')])
        loaded_skills = [('self-augmentation', 'SELF-AUG-BODY')]
        # Exact §5.2 expression as wired in engine/core.py:3435 (shared helper).
        inst._loaded_skill_names = _merge_loaded_skill_names(
            inst.conversation, [name for name, _body in loaded_skills])
        assert inst._loaded_skill_names == ['skill-x', 'self-augmentation']

    def test_t4_brand_new_instance_byte_identity(self):
        """SUB-AGENT REBUILD byte-identity guard (§5.2). A brand-new instance (no runtime msgs in
        conversation) must yield exactly [resolved] — identical to the pre-fix expression."""
        inst = _make_inst_with_history(None, [])  # conversation = [system] only, no runtime msgs
        loaded_skills = [('self-augmentation', 'SELF-AUG-BODY')]
        new_value = _merge_loaded_skill_names(
            inst.conversation, [name for name, _body in loaded_skills])
        old_value = [name for name, _body in loaded_skills] if loaded_skills else None
        assert new_value == old_value == ['self-augmentation']

    def test_t4_brand_new_empty_resolved_is_none(self):
        """Byte-identity guard: fresh instance with nothing resolved → field is None (as before)."""
        inst = _make_inst_with_history(None, [])
        assert _merge_loaded_skill_names(inst.conversation, []) is None

    def test_t6_no_over_suppression_distinct_skills(self):
        """NO OVER-SUPPRESSION. Distinct skills still all load after a restore."""
        inst = _make_inst_with_history(None, [_skill_user_msg('skill-x')])
        result, enqueued = _call_load_skill(
            inst, ['skill-y', 'skill-z'], {'skill-y': 'BODY-Y', 'skill-z': 'BODY-Z'})
        assert len(enqueued) == 2
        assert 'Successfully loaded 2 skill(s)' in result

    def test_t6_compressed_away_skill_reloadable(self):
        """NO OVER-SUPPRESSION. A skill whose injected msg was compressed away (NOT in the
        conversation) is correctly re-loadable — re-derivation returns nothing for it."""
        # 'skill-gone' is NOT in the conversation (compressed away); only 'skill-x' is present.
        inst = _make_inst_with_history(None, [_skill_user_msg('skill-x')])
        result, enqueued = _call_load_skill(
            inst, ['skill-gone'], {'skill-gone': 'BODY-GONE'})
        assert len(enqueued) == 1  # legitimately re-injected (no longer in context)
        assert 'Successfully loaded 1 skill(s)' in result
