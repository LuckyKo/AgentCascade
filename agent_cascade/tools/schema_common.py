"""
Shared JSON-Schema fragments and a builder for tool parameter schemas.

AgentCascade tool classes each declare an OpenAI-style ``parameters`` dict
(``{'type': 'object', 'properties': {...}, 'required': [...]}``). Two problems
this module addresses (BUG_0329, Increment A):

* Only ``read_file`` and ``shell_cmd`` set ``additionalProperties: False``, so a
  misnamed parameter on every other tool is silently dropped and the tool then
  runs with defaults (a wrong-behaviour class). ``tool_params`` sets
  ``additionalProperties: False`` by default so unknown keys are rejected loudly
  (BaseTool converts the rejection into a friendly ``unknown parameter(s) ...``
  message). Alias coercions that run BEFORE validation are preserved by callers
  and must keep working — see each tool's ``call()``/``_verify_json_format_args``.
* The ``path`` parameter, ``justification`` parameter, and string-or-array
  parameters are repeated ~12x across tools. ``PATH_PROP`` / ``JUSTIFICATION_PROP``
  / ``STRING_OR_STRING_ARRAY`` are description-free base fragments merged with a
  per-tool ``description`` (dna.py ``TOOL_METADATA`` remains the single source of
  description text — these carry structure only, so the
  ``TestToolMetadataSingleSourceOfTruth`` invariant is unaffected).
"""

# Base fragment for a single-string path-style parameter. Merge with the tool's
# own ``description`` (from TOOL_METADATA): {**PATH_PROP, 'description': ...}.
PATH_PROP = {'type': 'string'}

# Base fragment for the optional ``justification`` parameter. Merge with a
# description; tools that hardcode the text inline still supply their own
# ``description`` key. {**JUSTIFICATION_PROP, 'description': ...}.
JUSTIFICATION_PROP = {'type': 'string'}

# Base fragment for a parameter that accepts a single string OR an array of
# strings. oneOf (not ``type: ['string','array']``) for maximum LLM-API
# compatibility — mirrors the historical delete_file / load_skill pattern.
STRING_OR_STRING_ARRAY = {
    'oneOf': [
        {'type': 'string'},
        {'type': 'array', 'items': {'type': 'string'}},
    ],
}


def tool_params(properties, required, *, additionalProperties=False, **kw):
    """Build a tool ``parameters`` block with ``additionalProperties: False`` by default.

    Args:
        properties: The ``properties`` mapping (param name -> JSON-Schema node).
        required: Iterable of required parameter names (must be a subset of
            ``properties``).
        additionalProperties: When False (default) unknown keys are rejected
            loudly. Pass True for the rare tool that must accept open properties.
        **kw: Any additional top-level JSON-Schema keywords to include
            (e.g. ``anyOf``, ``allOf``, ``$defs``).

    Returns:
        A dict shaped ``{'type': 'object', 'additionalProperties': <bool>,
        'properties': ..., 'required': [...]}`` plus any ``kw`` entries.
    """
    params = {
        'type': 'object',
        'additionalProperties': additionalProperties,
        'properties': properties,
        'required': list(required),
    }
    params.update(kw)
    return params
