"""Skill selection strategies for :meth:`SkillManager.match_skills`.

Two interchangeable strategies sit behind the single ``match_skills`` choke point:

* :class:`KeywordSelector` — wraps the existing keyword matcher. This is the
  default and the guaranteed fallback; it changes nothing about today's behavior.
* :class:`ApiSelector` — asks a decision-model endpoint (``POST /v1/systemone``)
  to pick the single best skill from the keyword matcher's top-k candidate pool,
  walking an ordered endpoint fallback list.

Degradation contract (the whole point of this module): ``ApiSelector.select``
MUST NEVER raise. Any exception, timeout, empty model with no default, missing
endpoint chain, HTTP error, malformed response, a choice of ``'none'``, or a
choice not present in the pool all yield ``None`` — meaning "no opinion, fall
back to keyword matching". The caller (``match_skills``) treats ``None`` as
"keep going down the unchanged keyword path", so an API outage can never block
skill auto-matching or delegation.

The Cloudflare WAF on the Codiv endpoint blocks the default Python HTTP
User-Agent with error 1010 (HTTP 403); a browser-like ``User-Agent`` header is
therefore sent on every request (mirrors skill-selection-eval/compare_codiv.py).
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, List, Optional, Tuple

import requests

if TYPE_CHECKING:  # pragma: no cover - type hints only, avoids a runtime import cycle
    from agent_cascade.api_router_pkg.endpoints import APIEndpoint

logger = logging.getLogger(__name__)

# Bounded per-endpoint timeout so an API selector can never stall delegation.
_API_TIMEOUT_SECONDS: float = 10.0
# How many keyword-matched candidates form the decision-model candidate pool.
_CANDIDATE_POOL_SIZE: int = 8
# Max characters of query context forwarded to the decision model.
_STATE_MAX_CHARS: int = 6000
# Model id sent when an endpoint's configured ``model`` is empty. Some decision-model
# endpoints (e.g. Codiv's /v1/systemone) are configured with a blank model field in
# api_endpoints.json; without a default the selector would skip them and silently do
# nothing. Overridable via env so users can pin a specific release (e.g. openjev-0.1).
DEFAULT_SKILL_SELECTOR_MODEL: str = os.getenv('AGENT_CASCADE_SKILL_SELECTOR_MODEL', 'openjev-latest')
# Browser-like UA — Cloudflare WAF rejects the default Python UA (error 1010).
_BROWSER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0 Safari/537.36'
)

_NONE_CRITERION = (
    'No specialized skill is needed for this task; it is a generic request '
    'that does not require any domain-specific procedure.'
)

# Classifier-specific timeout: tighter than skill selection because the completion
# classifier blocks the normal-completion path of a finished agent.
_COMPLETION_CLASSIFIER_TIMEOUT_SECONDS: float = 3.0

# The instructions the skill selector always asks (question key 'pick').
_SKILL_PICK_INSTRUCTIONS = (
    'Select the single most relevant skill to load for this task. '
    "Choose 'none' if no skill is needed."
)


def resolve_endpoints(pool, priority_key: str = 'skill_selector',
                      log_prefix: str = 'SKILL-SELECTOR') -> List[APIEndpoint]:
    """Return the ordered usable endpoints for ``agent_priorities[priority_key]``.

    Endpoints with a missing/empty ``api_base`` are dropped here (not just at
    request time) so we never fire a guaranteed-failing relative-URL request.

    ``log_prefix`` names the CALLING subsystem in log lines; each caller passes its
    own so a shared-helper message is never attributed to the wrong component.
    """
    api_router = getattr(pool, 'api_router', None)
    if api_router is None:
        return []
    endpoints: List[APIEndpoint] = []
    for endpoint_id in api_router.agent_priorities.get(priority_key, []) or []:
        endpoint = api_router.get_endpoint(endpoint_id)
        if endpoint is None:
            continue
        if not getattr(endpoint, 'api_base', None):
            logger.debug('[%s] endpoint %s has empty api_base; skipping', log_prefix,
                         getattr(endpoint, 'id', '?'))
            continue
        endpoints.append(endpoint)
    return endpoints


def ask_decision_model(endpoint, model: str, state: str, question_key: str,
                       instructions: str, criteria: dict,
                       timeout: float = _API_TIMEOUT_SECONDS,
                       log_prefix: str = 'SKILL-SELECTOR') -> Optional[str]:
    """POST one ``/v1/systemone`` choice question; return the chosen criterion or ``None``.

    Shared by every decision-model consumer (skill selection, completion classifier).
    Never raises — a per-endpoint failure yields ``None`` ("no opinion").
    ``log_prefix`` names the calling subsystem in the failure log line (see
    :func:`resolve_endpoints`).
    """
    url = ApiSelector._endpoint_url(getattr(endpoint, 'api_base', ''))
    payload = {
        'model': model,
        'state': state,
        'questions': {
            question_key: {
                'type': 'choice',
                'instructions': instructions,
                'criteria': criteria,
            }
        },
    }
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f"Bearer {getattr(endpoint, 'api_key', '')}",
        # Cloudflare WAF blocks the default Python UA (error 1010); send a real one.
        'User-Agent': _BROWSER_USER_AGENT,
    }
    try:
        resp = requests.post(url, data=json.dumps(payload).encode('utf-8'),
                             headers=headers, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        answer = (body.get('answers') or {}).get(question_key, {})
        choice = answer.get('choice')
        if not choice:
            return None
        return str(choice)
    except Exception as e:  # noqa: BLE001 — per-endpoint failure → no opinion
        logger.debug('[%s] endpoint %s failed: %s', log_prefix,
                     getattr(endpoint, 'id', '?'), e)
        return None


class KeywordSelector:
    """Strategy that delegates to the existing keyword matcher (default path)."""

    def __init__(self, manager):
        self._manager = manager

    def select(self, query: str, include_inactive: bool, cap: Optional[int]) -> List[Tuple[str, float]]:
        """Return the keyword matcher's results unchanged."""
        return self._manager.match_skills(query, include_inactive, cap)


class ApiSelector:
    """Strategy that asks a decision-model endpoint to pick the best skill.

    Never raises; returns ``None`` on any failure so the caller falls back to
    keyword matching.
    """

    def __init__(self, manager):
        self._manager = manager

    # ── Public entry point ───────────────────────────────────────────────────

    def select(self, query: str, include_inactive: bool, cap: Optional[int]) -> Optional[List[Tuple[str, float]]]:
        """Pick the best skill via a decision-model endpoint.

        Returns an ordered ``(name, score)`` list with the model's choice first,
        or ``None`` if there is no valid answer (fall back to keyword).
        """
        try:
            # Keyword top-k forms the candidate pool; its scores are reused so
            # downstream threshold/cap logic is unchanged. Call the pure keyword path
            # directly (NOT match_skills) to avoid re-entering the API-mode dispatch.
            ranked = self._manager._keyword_match(query, include_inactive, cap=None)
            if not ranked:
                return None
            pool_names = [name for name, _ in ranked[:_CANDIDATE_POOL_SIZE]]
            score_by_name = dict(ranked)

            endpoints = self._resolve_endpoints()
            if not endpoints:
                return None

            criteria = self._build_criteria(pool_names)  # noqa: keep call site explicit
            state = 'Agent task context:\n' + (query or '')[:_STATE_MAX_CHARS]

            for endpoint in endpoints:
                # An endpoint may be configured with a blank model (e.g. Codiv's
                # /v1/systemone). Use the module default so such endpoints still work
                # out of the box; the user can pin a specific release via env.
                model = getattr(endpoint, 'model', '') or DEFAULT_SKILL_SELECTOR_MODEL
                answer = self._ask_endpoint(endpoint, model, state, criteria)
                if answer is None:
                    continue  # this endpoint failed/abstained → try the next one
                return self._map_answer(answer, pool_names, score_by_name)

            # Every endpoint failed or abstained.
            return None
        except Exception as e:  # noqa: BLE001 — must never propagate to callers
            logger.warning('[SKILL-SELECTOR] API selector failed; falling back to keyword: %s', e)
            return None

    # ── Endpoint resolution (ordered fallback list) ──────────────────────────

    def _resolve_endpoints(self) -> List[APIEndpoint]:
        """Ordered usable endpoints for ``agent_priorities['skill_selector']``."""
        return resolve_endpoints(getattr(self._manager, 'pool', None))

    # ── Request construction ─────────────────────────────────────────────────

    def _build_criteria(self, pool_names: List[str]) -> dict:
        """Build the ``/v1/systemone`` criteria map for the candidate pool + none."""
        criteria = {}
        for name in pool_names:
            meta = self._manager.get_skill_metadata(name) or {}
            desc = meta.get('description', '') or ''
            triggers = meta.get('triggers', []) or []
            if triggers:
                desc += ' | typical triggers: ' + '; '.join(str(t) for t in triggers[:4])
            criteria[name] = desc
        criteria['none'] = _NONE_CRITERION
        return criteria

    @staticmethod
    def _endpoint_url(api_base: str) -> str:
        """Return the ``/v1/systemone`` URL, appending it if the base lacks it."""
        base = (api_base or '').rstrip('/')
        if not base.endswith('/systemone'):
            base += '/systemone'
        return base

    def _ask_endpoint(self, endpoint, model: str, state: str, criteria: dict) -> Optional[str]:
        """POST to one endpoint; return the chosen criterion name or ``None``."""
        return ask_decision_model(endpoint, model, state, 'pick',
                                  _SKILL_PICK_INSTRUCTIONS, criteria)

    # ── Answer mapping ───────────────────────────────────────────────────────

    @staticmethod
    def _map_answer(choice: str, pool_names: List[str], score_by_name: dict) -> Optional[List[Tuple[str, float]]]:
        """Map the model's choice back to an ordered (name, score) list.

        Chosen skill first (with its keyword score), then remaining candidates in
        keyword order. ``None`` if the choice is 'none' or not in the pool.
        """
        if choice == 'none' or choice not in pool_names:
            return None
        ordered = [(choice, score_by_name.get(choice, 0.0))]
        for name in pool_names:
            if name != choice:
                ordered.append((name, score_by_name.get(name, 0.0)))
        return ordered


# ── Completion classifier (BUG_0048) ──────────────────────────────────────────

_COMPLETION_INSTRUCTIONS = (
    'Decide whether the agent turn is a genuine final answer or an unfinished '
    "announcement. Choose 'completed' or 'incomplete'."
)

# Log subsystem name owned by this feature; the shared helpers take it as a parameter
# so a classifier-side failure is never logged under [SKILL-SELECTOR].
_LOG_PREFIX = 'COMPLETION-CLASSIFIER'


def classify_completion(pool, state_text: str) -> Optional[str]:
    """Return ``'completed'`` | ``'incomplete'`` | ``None`` (no opinion). Never raises.

    Reuses the skill selector's endpoint list and decision-model HTTP envelope via the
    module-level helpers above (no duplicated request logic). Fails OPEN: any error,
    timeout, abstention or unparseable answer yields ``None``, which callers treat as
    "treat the turn as complete" — the pre-existing behavior.
    """
    try:
        criteria = {
            'completed': 'The agent has genuinely finished; this text was its final answer '
                         'and no further tool calls are needed.',
            'incomplete': 'The agent announced or implied further work but did not emit the '
                          'tool calls; more steps remain to be done.',
        }
        endpoints = resolve_endpoints(pool, 'skill_selector', log_prefix=_LOG_PREFIX)
        if not endpoints:
            return None
        state = state_text[:_STATE_MAX_CHARS]
        # Single-endpoint attempt (latency ceiling): the classifier runs on every
        # natural-end turn, so we bound worst-case latency at ~3s instead of walking
        # the whole fallback chain. A failure here fails open (None).
        endpoint = endpoints[0]
        model = getattr(endpoint, 'model', '') or DEFAULT_SKILL_SELECTOR_MODEL
        choice = ask_decision_model(endpoint, model, state, 'verdict',
                                    _COMPLETION_INSTRUCTIONS, criteria,
                                    timeout=_COMPLETION_CLASSIFIER_TIMEOUT_SECONDS,
                                    log_prefix=_LOG_PREFIX)
        # 'none' and any unknown string collapse to None here — the whole
        # degradation contract in one expression.
        return choice if choice in criteria else None
    except Exception as e:  # noqa: BLE001 — fail-open contract
        logger.debug('[COMPLETION-CLASSIFIER] failed; no opinion: %s', e)
        return None
