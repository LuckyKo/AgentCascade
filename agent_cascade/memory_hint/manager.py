"""MemoryHintManager — async orchestration for the memory-hint feature.

One low-priority **daemon** worker thread consumes a job queue and, on a strong
match (1–4 memories past the adaptive floor, not already read, not recently
hinted), delivers a hint via ``_queue_tool_warning`` onto the instance's
existing ``_tool_warnings`` queue (plan §1.3). There is **NO USER-message
injection** — the hint rides the normal tool-result drain and is dropped if no
tool result ever follows (plan §0 / R1).

The gate is a self-calibrating *specificity* check, not a fixed threshold:
``top1 < floor`` → skip (no signal), ``top1 - top2 < GAP`` → skip (diffuse tie),
more than ``MAX_HINTS_PER_TURN`` docs at/above the floor → skip (noise). The
floor is an EWMA of per-turn top-1 scores (bounded below by ``FLOOR_MIN``);
the ``memory_hint_threshold`` setting is an optional override that can only
RAISE the floor (0 = pure adaptive behavior). The skill sub-pipeline has its OWN
independently calibrated floor (``SKILL_FLOOR_*``); it never reads or writes the
memory cosine floor.

The main loop never blocks: ``submit()`` is a non-blocking put of a small dict.
All matching + I/O happens in the background thread. Any exception anywhere in
this path is swallowed (best-effort, logged at debug) — hints are non-critical.
"""

import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from agent_cascade.log import logger
from agent_cascade.settings import SKILL_MATCH_THRESHOLD, MAX_AUTO_SKILLS_PER_CALL

from .matcher import MemoryMatcher
from .vault import VaultIndex, discover_vaults

# Cooldown (seconds) before the same memory may be re-hinted to an instance.
# This is now a UI setting (``memory_hint_cooldown_seconds``); this constant is
# only the fallback default when the setting is absent/invalid.
HINT_COOLDOWN_SECONDS = 600.0

# NOTE: this worker thread's stack size is set process-wide by AgentPool.__init__ via
# threading.stack_size() (see BACKGROUND_THREAD_STACKSIZE in pool/core.py) before the
# thread is created — Windows' 32KB default is too small for the deep C-extension chains
# this worker runs, and a 32KB thread overflowing its stack kills the process with an
# uncatchable hard access violation. No per-thread kwarg exists for this.

# A job older than this (monotonic seconds) is dropped on drain so we never hint
# a turn the agent has long since passed (plan §4.3).
JOB_TTL_SECONDS = 30.0

# Noise gate: more than this many strong matches means the query is too generic;
# skip rather than spam (plan §8 noise gate / R2).
MAX_HINTS_PER_TURN = 4

# Per-entry text cap inside a hint, for readability. Internal constant — NOT a
# UI setting (the user-facing knob is ``memory_hint_max_entries``, which caps the
# NUMBER of entries listed, not their length). Sized so ABSOLUTE display paths
# survive the clip: a Windows vault path like
# ``N:\work\WD\AgentWorkspace\.agent_lessons\xxx.md`` is ~55 chars, plus any
# subdirs. Current value 256 (raised from the original 120 when entries switched
# from bare rel_paths to absolute display paths — see :meth:`MemoryHintManager._display_path`).
HINT_ENTRY_MAX_CHARS = 256

# ── Self-calibrating gate constants ─────────────────────────────────────────
# These were TUNED empirically from a ~47-sample labeled replay of real agent
# queries through the actual matcher against the real vault index (precision
# ≈ 1.0 at a ~9–11% fire rate), NOT derived from first principles. Re-tune
# when the vault or the query mix shifts materially. See
# plans/memory_hint_TUNING.md for the measurement methodology.
#
# GAP: minimum top1 − top2 separation for a "single clear winner". A diffuse
# multi-topic query has top1 ≈ top2 (gap < GAP) and is skipped as ambiguous.
GAP = 0.03
# FLOOR_SEED: initial value of the adaptive floor before any turns are seen.
FLOOR_SEED = 0.20
# FLOOR_MIN: hard lower bound for the EWMA floor — the signal check must never
# degenerate into "any non-zero score fires".
FLOOR_MIN = 0.12
# EWMA_ALPHA: per-turn learning rate for the adaptive floor (floor ← α·top1 +
# (1−α)·floor). Small on purpose: the floor tracks the corpus's own score
# distribution slowly and must not chase a single spike.
EWMA_ALPHA = 0.05

# ── Skill-hint gate (feature: skills-in-memory-hints) ───────────────────────
# INDEPENDENT of the memory cosine floor (plan §D1): skill scores are
# coverage-normalized IDF (a different scale than TF-IDF cosine) — never mix them,
# and never reuse GAP / FLOOR_* / _floor_lock from the memory pipeline above.
#
# BUG_0041: a FIXED floor is structurally incompatible with the G+C3 scorer. That
# scorer's denominator is the query's IN-VOCABULARY IDF MASS
# (matcher.py: `total = sum(self._idf.get(kw, 0.0) for kw in query_tokens) * wmax`),
# so the score decays as more in-vocab terms enter the query while a constant floor
# does not move. Measured against the real 222-skill corpus:
#   33 chars → 0.317, 127 → 0.141, 488 → 0.081, 1000 → 0.066
# — only small-in-vocab-mass queries cleared 0.15, so the gate almost never fired on
# real agent turns.
#
# WORKAROUND (not the root-cause fix): mirror the memory pipeline's self-calibrating
# EWMA floor, but (a) feed it a LENGTH-CORRECTED score so the EWMA does not merely
# average over the bimodal length distribution, (b) use a RELATIVE noise gate evaluated
# AFTER cooldown/loaded dedup, and (c) keep an ABSOLUTE junk guard so no amount of floor
# decay can admit the junk band. The root cause is the length-dependent denominator in
# matcher.py (`score / total`, where total = in-vocab IDF mass). Fixing that would change
# match() for every caller (scan_skills, AUTO-mode call_agent, skill advisor), so this
# instead bounds the symptom in the hint gate. Future work: normalize the denominator
# and retire SKILL_REF_TOKENS / SKILL_FLOOR_* entirely.
#
# SKILL_HINT_MIN_SCORE is retained for BACKWARDS COMPAT ONLY (existing exports +
# test_skill_gate_constants_reuse_settings). It is NOT the gate any more and must not
# be read as one. The env var SKILL_MATCH_THRESHOLD reaches only this constant (AUTO
# mode and existing exports) — it does NOT influence the hint gate's adaptive behavior.
SKILL_HINT_MIN_SCORE = SKILL_MATCH_THRESHOLD      # 0.15 — nominal AUTO-mode score; NOT the gate
SKILL_HINT_MAX_ENTRIES = MAX_AUTO_SKILLS_PER_CALL # 3    — max skills listed (plan §D3)

# Skill adaptive-floor constants (BUG_0041). TUNED for the G+C3 scale — these are
# NOT the memory cosine constants and must never be shared with them.
#
# SKILL_FLOOR_SEED is THE tuned constant, not a placeholder. The floor is in-memory
# only (never persisted) and converges in ~20 turns, but most agent sessions are
# SHORTER than that — so for the whole life of a typical session the operating point
# is the seed. It is validated by a SEED-ONLY replay (verification V1), because no
# amount of EWMA convergence rescues a bad seed.
SKILL_FLOOR_SEED = 0.055   # operating point for sessions that never converge
SKILL_FLOOR_MIN = 0.035    # hard lower bound (applied at read time only)
SKILL_EWMA_ALPHA = 0.05    # per-turn learning rate: floor ← α·norm + (1−α)·floor
# SKILL_REF_TOKENS: the in-vocab token count at which the length correction reaches
# 1.0. See _update_skill_floor docstring for the full length-correction rationale.
SKILL_REF_TOKENS = 40
# SKILL_HINT_NOISE_RATIO: fraction of matched skills that may survive dedup before
# the query is judged generic. Relative, not absolute, because `matches` is capped at
# _TOP_K = 10 (matcher.py:238) — an absolute cap silently re-suppresses high-in-vocab
# queries, which is the very class this fix enables. Final threshold is
# max(SKILL_HINT_MAX_ENTRIES, len(matches) * SKILL_HINT_NOISE_RATIO).
SKILL_HINT_NOISE_RATIO = 0.6
# SKILL_JUNK_MIN: ABSOLUTE, non-adaptive guard on top1. The adaptive floor can decay
# toward the junk band (matcher.match() returns up to 10 results for any query sharing
# ≥2 in-vocab terms — _MIN_MATCHED_TERMS = 2), and cooldown is per-skill-name, so a
# rotating junk suggestion set is fully compatible with a 0.035 floor + 600 s cooldown.
# This one comparison bounds the worst case regardless of EWMA state.
SKILL_JUNK_MIN = 0.0375   # pinned literal; was derived from SKILL_HINT_MIN_SCORE * 0.25


class MemoryHintManager:
    """Owns the daemon worker, job queue, vault indexes and hint delivery."""

    def __init__(self, pool):
        self._pool = pool
        self._job_queue: 'queue.Queue' = queue.Queue()
        # Per-instance most-recent-wins pending set (authoritative; the Queue is
        # only transport). Guarded by _pending_lock.
        self._pending: Dict[str, dict] = {}
        # Per-instance monotonically-increasing generation counter. Each submit()
        # bumps it and stamps the job with the new value; the worker drops any popped
        # job whose generation no longer matches (a newer submit replaced it). This is
        # what makes most-recent-wins real — queue.Queue can't remove arbitrary items,
        # so stale duplicates are tolerated in transport and filtered here. Guarded by
        # _pending_lock.
        self._generation: Dict[str, int] = {}
        self._pending_lock = threading.Lock()

        self._matcher = MemoryMatcher()
        self._vault_indexes: Dict[Path, VaultIndex] = {}
        self._index_lock = threading.RLock()

        # Adaptive signal floor: EWMA of per-turn top-1 scores (in-memory only —
        # intentionally NOT persisted; a fresh process re-seeds and re-calibrates).
        # Guarded by _floor_lock because tests may drive _process_job from multiple
        # threads even though the production worker is single-threaded.
        self._adaptive_floor = FLOOR_SEED
        self._floor_lock = threading.Lock()

        # Adaptive SKILL signal floor (BUG_0041). SEPARATE state + lock from the
        # memory cosine floor above: the two pipelines score on different measures
        # (TF-IDF cosine vs coverage-normalized in-vocab IDF) and must never share a
        # threshold. In-memory only (re-seeds per process), exactly like the memory floor.
        self._skill_adaptive_floor = SKILL_FLOOR_SEED
        self._skill_floor_lock = threading.Lock()

        # Safety-net rescan trigger (plan §6.2): worker compares this each idle tick.
        self._last_config_version = -1

        self._worker: Optional[threading.Thread] = None
        self._started = False
        self._start_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the daemon worker.

        Idempotent while the worker is alive, but able to RESPAWN a dead one. The
        original implementation guarded purely on ``self._started`` — once set, a later
        ``start()`` was a no-op even after the thread had died (e.g. a pool stop→resume
        cycle calls :meth:`stop` and never brings the worker back). Now we also check
        liveness: if the flag is set but the thread object is gone or not alive, reset
        state and spawn a fresh thread. This makes ``start()`` safe to call repeatedly —
        idempotent when alive, reviving when dead. Guarded by ``_start_lock`` so the
        liveness check + respawn are atomic under concurrent calls (plan §2.1/§2.3).
        """
        with self._start_lock:
            if self._started and self._worker is not None and self._worker.is_alive():
                return  # already running — idempotent no-op
            # Dead (or never started): reset state and respawn a fresh worker.
            self._started = True
            self._worker = threading.Thread(target=self._run, name='memory-hint-worker', daemon=True)
            self._worker.start()

    def stop(self) -> None:
        """Best-effort stop (daemon threads die with the process anyway)."""
        # Put a sentinel; the worker exits. Not joined — main loop never blocks.
        try:
            self._job_queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        # Reset the lifecycle flag so a later start() is NOT blocked by the stale
        # ``_started`` flag (see :meth:`start` liveness check). The thread object is left
        # as-is — start()'s ``is_alive()`` check decides whether to respawn.
        with self._start_lock:
            self._started = False

    # ── Producer (turn-loop hook) ────────────────────────────────────────────

    def submit(self, instance_name: str, query: str, agent_class: str, turn: int = -1) -> None:
        """Non-blocking submit of a hint job. Most-recent-wins per instance.

        A newer submit REPLACES any still-pending job for the same instance: the pending
        dict is updated in place and its generation counter bumped. The transport queue
        may therefore hold stale duplicates (queue.Queue can't remove arbitrary items);
        the worker filters them by comparing each popped job's generation against the
        current one, so only the most recent submission for an instance is ever processed.
        """
        if not (instance_name and query and query.strip()):
            return
        now = time.monotonic()
        with self._pending_lock:
            gen = self._generation.get(instance_name, 0) + 1
            self._generation[instance_name] = gen
            job = {
                'instance_name': instance_name,
                'query': query,
                'agent_class': agent_class,
                'submitted_at': now,
                'turn': turn,
                '_gen': gen,
            }
            # Replace any existing pending job for this instance (most-recent-wins).
            self._pending[instance_name] = job
        try:
            self._job_queue.put_nowait(job)
            logger.debug('[MEMORY_HINT] %s: hint job submitted (turn=%d, query_len=%d)',
                         instance_name, turn, len(query))
        except Exception as e:  # noqa: BLE001 — never block/raise on the main path
            logger.debug('[MEMORY_HINT] submit failed for %s: %s', instance_name, e)

    # ── Vault index management ───────────────────────────────────────────────

    def rescan_vaults(self) -> None:
        """(Re)discover vaults and rebuild the merged matcher index (background-safe).

        ALL reads/writes of ``self._vault_indexes`` happen inside ONE
        ``with self._index_lock:`` block. A concurrent ``_display_path`` / match
        iterates that dict under the same lock, so mutating it outside would risk a
        RuntimeError (dict changed size during iteration) or a torn read. The lock is
        an RLock, so nesting is fine. We hold it across the per-vault ``rescan()``
        calls on purpose: rescans are infrequent and small, and keeping everything in
        one critical section guarantees the merged index + matcher see one consistent
        dict (no half-built state ever visible to a reader).
        """
        try:
            om = getattr(self._pool, 'operation_manager', None)
            vault_roots = discover_vaults(om)
            with self._index_lock:
                new_indexes: Dict[Path, VaultIndex] = {}
                changed = False
                for root in vault_roots:
                    idx = self._vault_indexes.get(root)
                    if idx is None:
                        idx = VaultIndex(root)
                        self._vault_indexes[root] = idx
                        # A brand-new index has no documents yet — build it now.
                        changed = idx.rescan() or True
                    else:
                        changed = idx.rescan() or changed
                    new_indexes[root] = idx

                # Drop indexes for vaults that no longer exist.
                for root in list(self._vault_indexes.keys()):
                    if root not in new_indexes:
                        del self._vault_indexes[root]
                        changed = True

                # Key by BARE vault-relative path (plan §3.1/§3.4): the dedup set, cooldown
                # map and stats sidecar all use bare rel_path, so the matcher must too —
                # otherwise a read recorded as "x.md" never matches the hinted key
                # "proj/x.md". Cross-vault filename collisions are rare and acceptable here
                # (first vault wins); correctness of dedup is the priority.
                merged = {}
                for idx in self._vault_indexes.values():
                    for rel, doc in idx.documents.items():
                        merged.setdefault(rel, doc)
                self._matcher.set_documents(merged)
            # Rescan completion is a meaningful state change (index rebuilt) — INFO.
            logger.info('[MEMORY_HINT] rescan_vaults: %d vault(s), %d lesson(s), changed=%s',
                        len(self._vault_indexes), len(merged), changed)
        except Exception as e:  # noqa: BLE001 — best-effort, never raise
            logger.debug('[MEMORY_HINT] rescan_vaults failed: %s', e)

    def _maybe_rescan_if_config_changed(self) -> None:
        """Safety net: rescan if pool._config_version advanced (plan §6.2)."""
        try:
            version = getattr(self._pool, '_config_version', -1)
            if version != self._last_config_version:
                self._last_config_version = version
                self.rescan_vaults()
        except Exception as e:  # noqa: BLE001
            logger.debug('[MEMORY_HINT] config-version poll failed: %s', e)

    # ── Settings (read live each cycle) ──────────────────────────────────────

    def _settings(self) -> dict:
        cfg = getattr(self._pool, 'llm_cfg', None) or {}
        # All numeric reads are live UI settings; each falls back to its module
        # default when absent OR invalid (defensive — a bad persisted value must
        # never crash the worker).
        try:
            cooldown = float(cfg.get('memory_hint_cooldown_seconds', HINT_COOLDOWN_SECONDS))
        except (TypeError, ValueError):
            cooldown = HINT_COOLDOWN_SECONDS
        # ``memory_hint_threshold`` is now an optional OVERRIDE / kill-switch:
        # the primary gate is the adaptive floor (EWMA of per-turn top-1 scores).
        # A value > 0 is used as a MINIMUM floor (max(floor, threshold)) — it can
        # only make hints rarer, never more frequent. 0 (or absent) = pure
        # adaptive behavior. See the module docstring for the gate design.
        try:
            threshold = float(cfg.get('memory_hint_threshold', 0.0))
        except (TypeError, ValueError):
            threshold = 0.0
        try:
            max_entries = int(cfg.get('memory_hint_max_entries', 3))
        except (TypeError, ValueError):
            max_entries = 3
        try:
            query_chars = int(cfg.get('memory_hint_query_chars', 1000))
        except (TypeError, ValueError):
            query_chars = 1000
        # Master sub-toggle for skill suggestions within memory hints (plan §D7):
        # ON by default; a user who finds skill spam can silence just the skill part
        # without disabling memory hints entirely.
        skill_suggestions = bool(cfg.get('memory_hint_skill_suggestions', True))
        # ``memory_hint_skill_threshold``: raise-only OVERRIDE floor for the SKILL
        # sub-pipeline. Deliberately SEPARATE from ``memory_hint_threshold`` (memory
        # cosine scale) — reusing one setting across two score measures is precisely
        # the bug class BUG_0041 is about. 0 / absent = pure adaptive.
        try:
            skill_threshold = float(cfg.get('memory_hint_skill_threshold', 0.0))
        except (TypeError, ValueError):
            skill_threshold = 0.0
        return {
            'enabled': bool(cfg.get('memory_hint_enabled', False)),
            'threshold': threshold,
            'max_entries': max_entries,
            'cooldown_seconds': cooldown,
            'query_chars': query_chars,
            'skill_suggestions': skill_suggestions,
            'skill_threshold': skill_threshold,
        }

    def _current_floor(self, override: float) -> float:
        """Effective signal floor for this turn.

        The adaptive EWMA floor, optionally raised by the user's
        ``memory_hint_threshold`` override — which can only make the gate stricter,
        never looser. The ``FLOOR_MIN`` bound is applied HERE, in exactly one place
        (the read path): ``_update_floor`` may store an unbounded EWMA value, and
        the seed is always ≥ FLOOR_MIN, so this single clamp covers every case.
        """
        with self._floor_lock:
            floor = max(FLOOR_MIN, self._adaptive_floor)
        if override > 0.0:
            floor = max(floor, override)
        return floor

    def _update_floor(self, top1: float) -> None:
        """Feed one processed turn's top-1 score into the EWMA floor (unbounded).

        Called on EVERY job that produced matches (fire or skip), so the floor
        tracks the corpus's own score distribution regardless of gate outcome.
        No ``FLOOR_MIN`` clamp here — :meth:`_current_floor` applies the bound at
        read time, in exactly one place. In-memory only — no persistence by design.
        """
        with self._floor_lock:
            self._adaptive_floor = EWMA_ALPHA * top1 + (1.0 - EWMA_ALPHA) * self._adaptive_floor

    def _current_skill_floor(self, override: float) -> float:
        """Effective SKILL signal floor for this turn (BUG_0041).

        The adaptive EWMA floor, optionally raised by ``memory_hint_skill_threshold``.
        Raise-only, same contract as :meth:`_current_floor`. ``SKILL_FLOOR_MIN`` is
        applied HERE, in exactly one place (the read path); the seed is already
        >= SKILL_FLOOR_MIN so this single clamp covers every case. Completely
        independent of the memory cosine floor (different measure, different lock).
        """
        with self._skill_floor_lock:
            floor = max(SKILL_FLOOR_MIN, self._skill_adaptive_floor)
        if override > 0.0:
            # Clamp to 1.0 (the scorer's own ceiling) and NOT to
            # SKILL_HINT_MIN_SCORE: capping at 0.15 — the very value that never fires
            # on real queries — would make the only user-facing "too many hints" knob
            # unable to reach the range where it matters. 1.0 also keeps this gate
            # decoupled from the env-configurable SKILL_MATCH_THRESHOLD. Mirrors
            # _handle_memory_hint_threshold (config_handlers.py:700).
            floor = max(floor, min(override, 1.0))
        return floor

    def _update_skill_floor(self, top1: float, n_in_vocab: int) -> None:
        """Feed one processed turn's top-1 SKILL score into the skill EWMA (unbounded).

        Called on EVERY job that produced skill matches (fire or skip) so the floor
        tracks the corpus's own score distribution regardless of gate outcome. Jobs
        with NO matches never reach this method, so they cannot pull the floor down
        (the memory pipeline's equivalent contract, mirrored).

        The score is LENGTH-CORRECTED before averaging (review finding 2). Raw ``top1``
        is bimodal — ~0.32 at small in-vocab mass, ~0.07 at large — and an EWMA of a
        bimodal stream converges to a weighted MEAN, which re-suppresses one class
        permanently. Correcting for the mass dependence first makes the EWMA track a
        comparable signal:

            norm = top1 * (n_in_vocab / max(n_in_vocab, SKILL_REF_TOKENS))

        The factor ``n / max(n, REF)`` is ALWAYS ``<= 1``, so this **DEFLATES** rather
        than boosts: short queries (high scores caused by a tiny in-vocab denominator)
        are pulled DOWN toward the long-query scale; at or above ``REF`` the feed passes
        through at its true value. Example: ``top1=0.32, n_in_vocab=6, REF=40`` →
        ``norm = 0.048``, not 0.32. The long class passes through UNCHANGED — the
        correction moves the converged floor only ~3% (0.0717 → 0.0698 at a
        15%/85% short/long mix), and that is still above the measured 0.066 long-query
        top, so convergence can stop the long class from firing. KNOWN-OPEN ITEM:
        if post-deployment monitoring confirms the long class is suppressed,
        ``SKILL_FLOOR_MIN`` must come down below 0.066 (the measured long-query top).

        ``n_in_vocab <= 0`` means "stats unavailable" (the fallback path in
        :meth:`_match_skills_for_hint`), NOT "zero in-vocab tokens" — zero is a
        legitimate value inside this formula, so an explicit branch treats unknown as
        pass-through rather than computing ``norm = top1 * (0/REF) = 0.0``, which would
        drive the floor to its hard bound in ~20 turns and silently disable calibration.

        No ``SKILL_FLOOR_MIN`` clamp here — :meth:`_current_skill_floor` bounds it at
        read time, in exactly one place. In-memory only, no persistence by design.
        """
        if n_in_vocab > 0:
            norm = top1 * (n_in_vocab / max(n_in_vocab, SKILL_REF_TOKENS))
        else:
            norm = top1          # stats unavailable (fallback path) → no correction
        with self._skill_floor_lock:
            self._skill_adaptive_floor = (
                SKILL_EWMA_ALPHA * norm + (1.0 - SKILL_EWMA_ALPHA) * self._skill_adaptive_floor
            )

    # ── Worker loop ──────────────────────────────────────────────────────────

    def _run(self) -> None:
        logger.debug('[MEMORY_HINT] worker started')
        while True:
            try:
                job = self._job_queue.get(timeout=0.5)
            except queue.Empty:
                # Idle tick: cheap config-version safety-net rescan.
                self._maybe_rescan_if_config_changed()
                continue
            if job is None:  # sentinel → stop
                break
            name = job.get('instance_name')
            # Stale-duplicate filter (most-recent-wins): a newer submit() may have bumped
            # this instance's generation and replaced the pending entry. If the popped
            # job's generation no longer matches, it was superseded — drop it WITHOUT
            # clearing the current (newer) pending entry.
            with self._pending_lock:
                if name is not None and job.get('_gen') != self._generation.get(name):
                    continue
            try:
                self._process_job(job)
            except Exception as e:  # noqa: BLE001 — never let the worker die
                logger.debug('[MEMORY_HINT] process_job failed for %s: %s', name, e)
            finally:
                with self._pending_lock:
                    if name is not None and job.get('_gen') == self._generation.get(name):
                        # Only clear the pending entry + generation if this was still the
                        # current job (a concurrent submit may have replaced it meanwhile).
                        self._pending.pop(name, None)
                        self._generation.pop(name, None)

    def _process_job(self, job: dict) -> None:
        name = job['instance_name']
        query = job['query']

        # TTL drop: don't hint a turn the agent has long since passed.
        if time.monotonic() - job.get('submitted_at', 0.0) > JOB_TTL_SECONDS:
            logger.debug('[MEMORY_HINT] %s: dropped (job expired, ttl=%.0fs)', name, JOB_TTL_SECONDS)
            return

        settings = self._settings()
        if not settings['enabled']:
            logger.debug('[MEMORY_HINT] %s: skipped (feature disabled)', name)
            return

        # Re-resolve the instance (may be gone/dismissed by delivery time).
        inst = self._pool.get_instance(name)
        if inst is None:
            logger.debug('[MEMORY_HINT] %s: dropped (instance gone)', name)
            return

        # State-change guard: skip sleeping instances (plan §4.3).
        try:
            from agent_cascade.agent_instance import AgentState
            if getattr(inst, 'state', None) == AgentState.SLEEPING:
                logger.debug('[MEMORY_HINT] %s: skipped (SLEEPING)', name)
                return
        except Exception:  # noqa: BLE001
            pass

        # ── Memory sub-pipeline (existing gate; returns bare + display paths) ──
        memory_bare, memory_display = self._match_memories(job, settings, inst)

        # ── Skill sub-pipeline (NEW independent gate; returns skill names) ──
        skill_names = self._match_skills_for_hint(job, settings, inst)

        if not memory_bare and not skill_names:
            return

        hint_text = self._build_combined(memory_display, skill_names)
        if not hint_text:
            return

        # Record cooldowns BEFORE delivery (only for what we actually deliver), so a
        # fast re-match is throttled. Memory and skill state are recorded together in
        # one lock block — they must never be partially updated.
        now = time.monotonic()
        with inst._compression_lock:
            for path in memory_bare:
                inst._recently_hinted[path] = now
            if memory_bare:
                inst._last_memory_hint_turn = job.get('turn', -1)
            for skill_name in skill_names:
                inst._recently_skill_hinted[skill_name] = now
            if skill_names:
                inst._last_skill_hint_turn = job.get('turn', -1)

        self._deliver(inst, name, hint_text)

    def _match_memories(self, job: dict, settings: dict, inst) -> 'tuple[List[str], List[str]]':
        """Memory sub-pipeline: the existing specificity gate over the lesson index.

        Returns ``(bare_paths, display_paths)`` — both empty when nothing fires. The
        bare paths are the vault-relative identities used for bookkeeping (dedup /
        cooldown); the display paths are the ABSOLUTE strings shown in the hint. This
        method is a VERBATIM move of the former inline gate in ``_process_job``: every
        early ``return`` became ``return [], []`` so a strong skill match can still fire
        on a turn with no memory match (plan §3.1c/§D1). The skill score scale is NEVER
        fed into the floor/gap/noise arithmetic here.
        """
        name = job['instance_name']
        query = job['query']

        # Match against the current index snapshot, and capture an ORDERED snapshot of
        # the vault roots in the SAME lock block. Display resolution later uses this
        # snapshot (not the live dict) so a concurrent rescan can't change iteration
        # order or drop a vault between scoring and display — keeping "first vault wins"
        # consistent between the scored file and the displayed absolute path.
        with self._index_lock:
            matches = self._matcher.match(query)
            roots_snapshot = list(self._vault_indexes.keys())
        if not matches:
            # Log only the query length — never the raw text (sensitive data).
            logger.debug('[MEMORY_HINT] %s: no matches (query_len=%d)', name, len(query))
            return [], []

        # Self-calibrating specificity gate (replaces the fixed-threshold check):
        #   1) signal floor — is there ANY real signal? (top1 < floor → skip)
        #   2) specificity gap — a SINGLE clear winner, not a diffuse tie?
        #      (top1 − top2 < GAP → skip; top2 = 0.0 for a lone match)
        #   3) noise gate — unchanged in effect: > MAX_HINTS_PER_TURN docs at/above
        #      the floor means the query is generic; skip (plan §8).
        scores = [score for _path, score in matches]  # matches already sorted desc
        top1 = scores[0]
        top2 = scores[1] if len(scores) > 1 else 0.0
        floor = self._current_floor(settings['threshold'])

        # Feed the EWMA on EVERY matched job (fire or skip) so the floor tracks
        # the corpus's own score distribution regardless of gate outcome.
        self._update_floor(top1)

        gap = top1 - top2
        if top1 < floor:
            logger.debug('[MEMORY_HINT] %s: gate top1=%.3f top2=%.3f gap=%.3f floor=%.3f → skip(floor)',
                         name, top1, top2, gap, floor)
            return [], []
        if gap < GAP:
            logger.debug('[MEMORY_HINT] %s: gate top1=%.3f top2=%.3f gap=%.3f floor=%.3f → skip(gap)',
                         name, top1, top2, gap, floor)
            return [], []

        strong = [(path, score) for path, score in matches if score >= floor]
        if len(strong) > MAX_HINTS_PER_TURN:
            logger.debug('[MEMORY_HINT] %s: gate top1=%.3f top2=%.3f gap=%.3f floor=%.3f → skip(noise) (%d strong)',
                         name, top1, top2, gap, floor, len(strong))
            return [], []

        logger.debug('[MEMORY_HINT] %s: gate top1=%.3f top2=%.3f gap=%.3f floor=%.3f → fire',
                     name, top1, top2, gap, floor)

        # Dedup: skip memories already read or recently hinted (under the instance lock).
        now = time.monotonic()
        cooldown = settings['cooldown_seconds']
        to_hint: List[str] = []
        skipped_read: List[str] = []
        skipped_cooldown: List[str] = []
        with inst._compression_lock:
            # Prune cooldown entries older than the cooldown window — they can no
            # longer affect the gate, so dropping them bounds the dict's growth.
            # (Same time.monotonic() clock as when entries were stamped.)
            expired = [p for p, ts in inst._recently_hinted.items() if now - ts >= cooldown]
            for path in expired:
                del inst._recently_hinted[path]

            for path, _score in strong:
                if path in inst._memories_read:
                    skipped_read.append(path)
                    continue
                last = inst._recently_hinted.get(path)
                if last is not None and (now - last) < cooldown:
                    skipped_cooldown.append(path)
                    continue
                to_hint.append(path)

        if not to_hint:
            logger.debug('[MEMORY_HINT] %s: all %d strong match(es) filtered out '
                         '(already_read=%s, in_cooldown=%s)',
                         name, len(strong), skipped_read or '-', skipped_cooldown or '-')
            return [], []

        # ``strong`` is already score-descending (inherited from matcher.match), so the
        # post-cooldown ``to_hint`` list keeps that order. Cap to the top-N entries
        # BEFORE building the hint text (the cap applies after the cooldown skip).
        max_entries = settings['max_entries']
        if max_entries > 0:
            to_hint = to_hint[:max_entries]

        # Display text only: resolve each vault-relative path to an ABSOLUTE file
        # path so the agent knows WHICH same-named lesson to read across multiple
        # vaults. Bookkeeping above (dedup / cooldown / _memories_read) already ran
        # on the bare rel_path identity and is left untouched — only what we SHOW
        # changes here. Pass the match-time root snapshot so display resolves against
        # the same vault set/order that produced the scores (see roots_snapshot above).
        display = [self._display_path(p, roots=roots_snapshot) for p in to_hint]
        return to_hint, display

    def _match_skills_for_hint(self, job: dict, settings: dict, inst) -> List[str]:
        """Skill sub-pipeline: an INDEPENDENT gate over ``SkillManager.match_skills``.

        Returns the skill names to suggest (score-descending, capped), or ``[]``. The
        gate is a self-calibrating **absolute junk guard + adaptive floor** (BUG_0041),
        then the unchanged cooldown/loaded dedup, then a **relative noise gate**
        evaluated on the post-dedup survivors, then the entry cap. There is *no*
        specificity-gap requirement — multiple relevant skills are legitimate. All
        floor state is independent of the memory pipeline (different measure, different
        lock). A failure here can never break the memory path (plan §D6).
        """
        name = job['instance_name']
        if not settings['skill_suggestions']:          # master sub-toggle (plan §D7)
            return []
        sm = getattr(self._pool, 'skill_manager', None)
        if sm is None or not hasattr(sm, 'match_skills'):   # missing manager (defensive / tests)
            return []
        # Prefer the stats-returning variant so the length-corrected EWMA feed and the
        # DEBUG line can see the in-vocab token count (review findings 1 and 2).
        #
        # The stats method is validated by RESULT SHAPE, not by `callable()`: a bare
        # MagicMock auto-creates any attribute, so `callable(...)` is True and the 3-tuple
        # unpack raises ValueError — which the surrounding `except Exception` (plan §D6)
        # would swallow, silently returning [] and killing the hint in EVERY test that
        # uses the existing `_stub_skill_manager`. `isinstance(out[0], list)` is the real
        # discriminator, and the `if not matches` fallthrough degrades to the plain call
        # rather than swallowing the turn.
        #
        # On that fallback path n_in_vocab = 0 means "stats unavailable" — the correction
        # is then SKIPPED inside _update_skill_floor (norm = top1, pass-through), NOT
        # zeroed (which would pin the floor to SKILL_FLOOR_MIN).
        matches, n_tokens, n_in_vocab = [], 0, 0
        try:
            raw = getattr(sm, 'match_skills_with_stats', None)
            if callable(raw):
                out = raw(job['query'])
                if isinstance(out, tuple) and len(out) == 3 and isinstance(out[0], list):
                    matches, n_tokens, n_in_vocab = out
            if not matches:
                matches = sm.match_skills(job['query'])     # [(name, score)] desc
        except Exception as e:                          # plan §D6 — never break the memory path
            logger.debug('[MEMORY_HINT] %s: skill match failed: %s', name, e)
            return []
        # Deterministic no-op guard for `MagicMock` pools (existing tests): a bare MagicMock's
        # match_skills returns a MagicMock, NOT a list → skip. If a test stubs match_skills to
        # return a REAL list, that is intentional and SHOULD be processed (the guard correctly passes).
        if not isinstance(matches, list):
            return []
        if not matches:
            return []

        # ── Gate 0: ABSOLUTE junk guard (BUG_0041) ───────────────────────────
        # Non-adaptive: bounds the worst case no matter what the EWMA has learned.
        top1 = matches[0][1]                            # match() returns score-descending
        if top1 < SKILL_JUNK_MIN:
            logger.debug('[MEMORY_HINT] %s: skill gate top1=%.4f junk_min=%.4f '
                         'tokens=%d in_vocab=%d → skip(junk)', name, top1,
                         SKILL_JUNK_MIN, n_tokens, n_in_vocab)
            return []

        # ── Adaptive floor, fed a LENGTH-CORRECTED score (BUG_0041) ──────────
        floor = self._current_skill_floor(settings.get('skill_threshold', 0.0))
        # Feed on EVERY matched job that clears the junk guard (fire or skip) so the
        # floor tracks the corpus's own score distribution regardless of gate outcome.
        self._update_skill_floor(top1, n_in_vocab)

        if top1 < floor:
            logger.debug('[MEMORY_HINT] %s: skill gate top1=%.4f floor=%.4f tokens=%d '
                         'in_vocab=%d → skip(floor)', name, top1, floor, n_tokens, n_in_vocab)
            return []

        strong = [(n, s) for n, s in matches if s >= floor]
        if not strong:
            return []

        now = time.monotonic(); cooldown = settings['cooldown_seconds']      # reuse memory cooldown (§D4)
        to_hint, skipped_loaded, skipped_cd = [], [], []
        with inst._compression_lock:
            # Read _loaded_skill_names UNDER the lock: the engine main loop writes it cross-thread
            # (engine/core.py), so an unlocked read from this worker thread is a data race.
            # Matches how _memories_read / _recently_hinted are always accessed here.
            loaded = {str(x).lower() for x in (getattr(inst, '_loaded_skill_names', None) or [])}
            expired = [n for n, ts in inst._recently_skill_hinted.items() if now - ts >= cooldown]
            for n in expired:
                del inst._recently_skill_hinted[n]
            for n, _s in strong:
                if n.lower() in loaded:                 # already active this run (§D4)
                    skipped_loaded.append(n); continue
                last = inst._recently_skill_hinted.get(n)
                if last is not None and (now - last) < cooldown:
                    skipped_cd.append(n); continue
                to_hint.append(n)
        if not to_hint:
            logger.debug('[MEMORY_HINT] %s: all %d skill(s) filtered (loaded=%s, cooldown=%s)',
                         name, len(strong), skipped_loaded or '-', skipped_cd or '-')
            return []

        # ── Gate 2: RELATIVE noise gate, evaluated POST-dedup (BUG_0041) ───────
        # A diffuse/generic query lifts many skills over the low floor at once.
        # Listing them is spam, not a hint. Two properties matter:
        #   * RELATIVE, because `matches` is capped at _TOP_K = 10 (matcher.py:238) —
        #     an absolute count would re-suppress exactly the high-in-vocab queries
        #     this fix enables, and would break if _TOP_K or a cap= call changed.
        #   * POST-dedup (on to_hint, not on strong), because skills already loaded or
        #     in cooldown were never going to be shown — counting them suppresses a
        #     perfectly good hint for no reason.
        noise_max = max(SKILL_HINT_MAX_ENTRIES, int(len(matches) * SKILL_HINT_NOISE_RATIO))
        if len(to_hint) > noise_max:
            logger.debug('[MEMORY_HINT] %s: skill gate top1=%.4f floor=%.4f tokens=%d '
                         'in_vocab=%d → skip(noise) %d/%d after dedup (max=%d)',
                         name, top1, floor, n_tokens, n_in_vocab, len(to_hint),
                         len(matches), noise_max)
            return []

        max_entries = SKILL_HINT_MAX_ENTRIES            # plan §D3 cap (after dedup/cooldown)
        if max_entries > 0:
            to_hint = to_hint[:max_entries]
        logger.debug('[MEMORY_HINT] %s: skill hint fire → %s', name, to_hint)
        return to_hint

    def _skill_block(self, names: List[str]) -> str:
        """Skill section of a combined hint ('' when empty). Skill names are short
        snake_case strings — no per-entry clipping needed."""
        if not names:
            return ''
        lines = [f"  - {n}" for n in names]
        return f"Skills you may want to load ({len(names)}):\n" + '\n'.join(lines)

    def _build_combined(self, memory_display: List[str], skill_names: List[str]) -> str:
        """Merge the two sub-pipelines into ONE hint string (plan §D5).

        Memory-only output is BYTE-IDENTICAL to the legacy ``_build_hint`` result; a
        skill-only hint gets the ``[MEMORY HINT]`` umbrella tag so log/UI filtering keyed
        on that tag still catches it.
        """
        mem_block = self._build_hint(memory_display)   # UNCHANGED method; '' if empty
        skill_block = self._skill_block(skill_names)   # '' if empty
        if mem_block and skill_block:
            return mem_block + '\n' + skill_block      # both sections, memory first (stable order)
        if mem_block:
            return mem_block                           # memory-only → BYTE-IDENTICAL to today's output
        if skill_block:
            return '[MEMORY HINT] ' + skill_block      # skill-only → umbrella tag + skill block
        return ''

    def _display_path(self, rel: str, roots: Optional[List[Path]] = None) -> str:
        """Resolve a vault-relative lesson path to an ABSOLUTE display string.

        Multiple vaults can hold same-named lessons, so the hint must show which
        file to read. Walk the candidate vault roots (first hit wins — consistent with
        the merged-index "first vault wins" rule) and return the first root under
        which ``(root / rel)`` exists as a file. If no vault holds it (e.g. the
        vault was removed since the index was built), fall back to ``rel`` unchanged:
        this is a best-effort display feature and must never crash.

        Args:
            rel: Vault-relative lesson path (bare identity used by bookkeeping).
            roots: Ordered snapshot of vault roots captured at match time. When given,
                resolution walks THIS list instead of the live ``self._vault_indexes``
                dict — so a concurrent rescan can't change iteration order or drop a
                vault between scoring and display (keeps first-vault-wins consistent).
                When omitted, falls back to the live dict under ``_index_lock``.

        NOTE: only the DISPLAYED text changes here — dedup / cooldown / read-tracking
        all key on the bare vault-relative identity (see :meth:`on_memory_read`).
        """
        rel = str(rel)
        # Vault indexes store rel_paths in POSIX form (as_posix()); normalize so
        # joining against a Windows root resolves correctly.
        normalized = rel.replace('\\', '/')
        try:
            if roots is not None:
                candidates = list(roots)
            else:
                with self._index_lock:
                    candidates = list(self._vault_indexes.keys())
            for root in candidates:
                if (root / normalized).is_file():
                    return str(root / normalized)
        except Exception as e:  # noqa: BLE001 — best-effort, never raise
            logger.debug('[MEMORY_HINT] _display_path failed for %s: %s', rel, e)
        return rel

    def _build_hint(self, paths: List[str]) -> str:
        """Deterministic hint text listing the matched memory paths (plan §3.3).

        ``paths`` are ABSOLUTE display paths (resolved from vault-relative identities
        by :meth:`_display_path`); they are expected to already be capped to the top-N
        entries by the caller, and each entry's text is truncated to
        ``HINT_ENTRY_MAX_CHARS`` for readability. Bookkeeping identity stays
        vault-relative — only the displayed text here is absolute.
        """
        if not paths:
            return ''
        header = f'[MEMORY HINT] Relevant memories you may want to re-read ({len(paths)}):'

        def _clip(p: str) -> str:
            p = str(p)
            if len(p) > HINT_ENTRY_MAX_CHARS:
                return p[:HINT_ENTRY_MAX_CHARS - 1] + '…'
            return p

        lines = [f"  - {_clip(p)}" for p in paths]
        return header + '\n' + '\n'.join(lines)

    def _deliver(self, inst, name: str, hint_text: str) -> None:
        """Queue the hint via the existing tool-warning path (NO user injection)."""
        try:
            from agent_cascade.operation_manager.path_security import _queue_tool_warning
            _queue_tool_warning(self._pool, name, hint_text)
            logger.info('[MEMORY_HINT] %s: queued hint (%d entries):\n%s',
                        name, hint_text.count('\n  - '), hint_text)
        except Exception as e:  # noqa: BLE001 — best-effort delivery
            logger.warning('[MEMORY_HINT] delivery failed for %s: %s', name, e)

    # ── Read-tracking callback (wired to the ReadFile hook) ──────────────────

    def on_memory_read(self, instance_name: str, vault_root, rel_path: str) -> None:
        """Record that ``instance_name`` read a lesson (dedup + stats bump)."""
        try:
            from .stats import bump_read_count
            bump_read_count(vault_root, rel_path)
            inst = self._pool.get_instance(instance_name)
            if inst is not None:
                with inst._compression_lock:
                    inst._memories_read.add(rel_path)
        except Exception as e:  # noqa: BLE001 — never affect the read itself
            logger.debug('[MEMORY_HINT] on_memory_read failed for %s: %s', instance_name, e)
