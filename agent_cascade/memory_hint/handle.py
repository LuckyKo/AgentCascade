"""Completion handle for same-turn, opportunistic memory/skill hint delivery (BUG_0382).

A tiny, self-contained handoff object shared between the engine turn thread (the
"peek") and the daemon hint worker (the "producer"). It lets the turn do a
NON-BLOCKING "is the hint already computed?" check right after Phase 4 and, if so,
attach it to the SAME turn as a SYSTEM note; otherwise the hint defers to the next
turn exactly as before.

Design rules (BUG_0382 plan §3, R2):
- The only cross-thread primitive the peek path touches is ``ready.is_set()`` — a
  non-blocking boolean read. There is NO ``wait()`` / ``join()`` / ``time.sleep``
  anywhere. The turn's critical path can never block on this handle.
- The single ``lock`` makes the worker's "ready + defer-decision" region and the
  peek's whole "claim inline / abandon" decision mutually exclusive, so every
  interleaving yields EXACTLY ONE delivery (inline xor deferred — never both, never
  neither). See the four-case proof in plan §3.1.
- ``owner`` records the winner once claimed: None (unowned) | 'inline' | 'deferred'.
"""

from __future__ import annotations

import threading


class HintHandle:
    """Non-blocking completion handoff for one hint job.

    Fields:
      instance_name: the instance this hint belongs to (the peek verifies it still
        matches ``inst.instance_name`` so a staler handle is ignored).
      gen: generation stamped at submit time (informational; the worker's most-recent
        -wins filter already drops superseded jobs before they reach a handle).
      ready: set by the worker when ``hint_text`` is final — INCLUDING the empty case,
        so an unowned-but-ready handle is claimed inline and never re-queued.
      abandoned: True once the peek ran and found the job not ready (opt-out); the
        worker then queues the hint to the legacy tool-warning path on completion.
      owner: None | 'inline' | 'deferred' — which path delivered (set under ``lock``).
      text: final rendered hint_text (the exact string the worker would have queued).
      lock: guards ``abandoned``/``owner``/``text`` and the peek's re-read of ``ready``.
    """

    def __init__(self, instance_name: str, gen: int) -> None:
        self.instance_name = instance_name
        self.gen = gen
        self.ready = threading.Event()
        self.abandoned = False
        self.owner = None
        self.text = ''
        self.lock = threading.Lock()
