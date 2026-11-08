"""Single source of truth for client-side token→cost conversion.

No LLM provider returns a dollar cost in its response body — cost is always
derived locally by multiplying the server-reported ``usage`` token counts by a
per-endpoint pricing table. This module owns that math; telemetry, the
``system_info`` tool and the WebUI are consumers and must not re-implement it.

Pricing is per-endpoint (per-1M-token USD), never a global per-model table, so
the exact endpoint that served a call must supply its own ``pricing`` dict (the
telemetry fingerprint is model-only and cannot distinguish two endpoints
serving the same model at different rates).
"""

from typing import Optional

# Canonical pricing keys (per-1M-token USD). A pricing dict is considered
# configured (priced) if any of these is > 0; all-zero means "unpriced".
PRICING_KEYS = (
    'input_per_m',
    'output_per_m',
    'cache_read_per_m',
    'cache_write_per_m',
)


def is_priced(pricing: Optional[dict]) -> bool:
    """True when *pricing* carries at least one non-zero rate.

    ``None``, ``{}`` and an all-zero dict are all treated as unpriced, so a
    default-off install never reports a cost (no misleading ``$0``).
    """
    if not pricing:
        return False
    try:
        return any(float(pricing.get(key, 0.0) or 0.0) > 0.0 for key in PRICING_KEYS)
    except (TypeError, ValueError):
        return False


def tokens_to_cost(usage: dict, pricing: Optional[dict]) -> Optional[float]:
    """Compute one call's cost in USD from authoritative ``usage`` tokens.

    Args:
        usage: ``{prompt_tokens, completion_tokens, cached_tokens,
                cache_write_tokens}``. ``cached_tokens`` is the subset of the
                prompt served from cache; ``prompt_tokens`` is the *total*
                prompt (cache-read + non-cached). Keys may be missing or None.
        pricing: ``{input_per_m, output_per_m, cache_read_per_m,
                 cache_write_per_m}`` in per-1M-token USD, or ``None``/empty.

    Returns:
        Cost in USD, or ``None`` when unpriced (pricing falsy or all rates 0),
        so callers keep the cost key absent for default-off / unpriced installs.

    Billing model (cache-aware): non-cached input is billed at ``input_per_m``
    and the cached subset at the cheaper ``cache_read_per_m``; output at
    ``output_per_m``; cache-creation tokens at ``cache_write_per_m``. OpenAI
    compatible backends report no separate cache-creation count, so
    ``cache_write_tokens`` is 0 there and that term contributes nothing.

    Pure and total: never raises (bad values degrade to 0). Never reads a
    char/byte estimate — *usage* must come from the server.
    """
    if not is_priced(pricing):
        return None

    def _rate(key: str) -> float:
        try:
            return float(pricing.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _tok(key: str) -> int:
        try:
            return max(0, int(usage.get(key, 0) or 0))
        except (TypeError, ValueError, AttributeError):
            return 0

    prompt = _tok('prompt_tokens')
    completion = _tok('completion_tokens')
    cached = min(_tok('cached_tokens'), prompt)  # defensive: never negative input
    cache_write = _tok('cache_write_tokens')
    non_cached_input = max(0, prompt - cached)

    cost = (
        non_cached_input * _rate('input_per_m')
        + cached * _rate('cache_read_per_m')
        + completion * _rate('output_per_m')
        + cache_write * _rate('cache_write_per_m')
    ) / 1e6
    return cost
