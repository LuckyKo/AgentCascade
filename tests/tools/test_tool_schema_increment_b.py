"""BUG_0329 (Increment B) — conditional-`required` schema branch tests.

Increment B tightens four tool schemas with JSON-Schema conditionals so that the
schema enforces the rule the in-tool guard already enforces:

  * edit_file     : ``delete_and_insert`` needs ``range``; every other mode needs ``old_content``
  * media_gen     : ``backend`` is required for generation UNLESS ``prompt`` is ``__help``
  * propose_skill : rate-only (``name``+``rating``) OR create/update (``name``+``skill_content``+``justification``)
  * call_cli_agent: introspect (``status``) OR control (``tool_id``+``task``) OR launch (``harness``+``task``)

For each schema we assert BOTH directions of the quality bar:
  (a) a previously-valid call still validates,
  (b) the documented-invalid call (missing the branch-required field) is rejected.

These validate the schema DIRECTLY (``jsonschema``) rather than driving ``.call()``:
several of these tools build their schema for the model but do not run
``_verify_json_format_args`` on the call path, and the proxy tools are intercepted
by the ExecutionEngine, so the schema is the thing under test. ``tests/
tools/test_tool_schema_registry.py`` is the complementary build-time invariant
(every advertised schema is a well-formed ``additionalProperties:false`` object).
"""

import jsonschema
import pytest

import agent_cascade.tools.custom.file_ops  # noqa: F401 - registers edit_file
import agent_cascade.tools.media_gen  # noqa: F401 - registers media_gen
import agent_cascade.tools.custom.propose_skill  # noqa: F401 - registers propose_skill
from agent_cascade.prompts.dna import TOOL_METADATA
from agent_cascade.tools._agent_instance_proxy import _build_schema_from_metadata
from agent_cascade.tools.base import TOOL_REGISTRY


def _schema(name):
    """Return the parameters block of a registered (static) tool."""
    return TOOL_REGISTRY[name].parameters


def _is_valid(schema, instance):
    """True if ``instance`` satisfies ``schema`` (schema itself first checked valid)."""
    validator = jsonschema.validators.validator_for(schema)
    validator.check_schema(schema)  # raises SchemaError if the schema is malformed
    errors = list(validator(schema).iter_errors(instance))
    return not errors


# ── shared contract: every tightened schema keeps additionalProperties:false ──
def test_all_increment_b_schemas_are_closed_objects():
    for name in ('edit_file', 'media_gen', 'propose_skill'):
        params = _schema(name)
        assert params['type'] == 'object'
        assert params.get('additionalProperties') is False, f'{name} must stay additionalProperties:false'
    cli = _build_schema_from_metadata('call_cli_agent', TOOL_METADATA['call_cli_agent'])['parameters']
    assert cli['type'] == 'object'
    assert cli.get('additionalProperties') is False


# ── edit_file: if(match_mode==delete_and_insert) -> range, else -> old_content ──
def test_edit_file_if_then_else_branches():
    schema = _schema('edit_file')

    # (a) previously-valid non-d&i call (the old flat required: path, old_content, new_content)
    assert _is_valid(schema, {
        'path': 'a.txt', 'old_content': 'x', 'new_content': 'y', 'match_mode': 'exact',
    })
    # (a) previously-valid d&i call carrying a range
    assert _is_valid(schema, {
        'path': 'a.txt', 'new_content': 'y', 'range': '3:5', 'match_mode': 'delete_and_insert',
    })
    # (b) d&i WITHOUT range is now rejected (it was a runtime error in the OM before)
    assert not _is_valid(schema, {
        'path': 'a.txt', 'new_content': 'y', 'match_mode': 'delete_and_insert',
    })
    # (b) non-d&i WITHOUT old_content is rejected (else branch)
    assert not _is_valid(schema, {
        'path': 'a.txt', 'new_content': 'y', 'match_mode': 'exact',
    })
    # non-d&i with no explicit match_mode also needs old_content (else branch fires)
    assert not _is_valid(schema, {'path': 'a.txt', 'new_content': 'y'})
    # d&i may still carry old_content too (then only mandates range) — not rejected
    assert _is_valid(schema, {
        'path': 'a.txt', 'old_content': 'x', 'new_content': 'y', 'range': '3:5',
        'match_mode': 'delete_and_insert',
    })


def test_edit_file_old_content_not_flat_required():
    """The ''-sentinel is gone: old_content is no longer flat-required (it's conditional)."""
    schema = _schema('edit_file')
    assert 'old_content' not in schema['required']
    assert set(schema['required']) == {'path', 'new_content'}


# ── media_gen: backend required UNLESS prompt == __help (case-insensitive) ──
def test_media_gen_backend_or_help_anyof():
    schema = _schema('media_gen')

    # (a) real generation with a backend
    assert _is_valid(schema, {'backend': 'comfyui', 'prompt': 'a cat'})
    # (a) text-output backend: backend present, no prompt — valid (reference_file-driven)
    assert _is_valid(schema, {'backend': 'voice_to_text', 'reference_file': 'a.wav'})
    # (a) the help sentinel, no backend
    assert _is_valid(schema, {'prompt': '__help'})
    # (a) help sentinel is case-insensitive/after-strip, mirroring call()'s gate
    assert _is_valid(schema, {'prompt': '  __HELP  '})
    # (b) no backend and not help → rejected (failed at runtime anyway)
    assert not _is_valid(schema, {'prompt': 'a cat'})
    assert not _is_valid(schema, {})
    # extra unknown property rejected (additionalProperties:false)
    assert not _is_valid(schema, {'backend': 'comfyui', 'prompt': 'a cat', 'bogus': 1})


def test_media_gen_runtime_validation_enforces_anyof():
    """media_gen.call() runs _verify_json_format_args, so the anyOf is hard-enforced.
    Exercise the real boundary (no generation is triggered: validation fails first).
    """
    media_gen_cls = TOOL_REGISTRY['media_gen']
    inst = media_gen_cls()

    # valid: a backend + prompt passes validation (returns the parsed dict)
    assert inst._verify_json_format_args('{"backend": "comfyui", "prompt": "a cat"}') == {
        'backend': 'comfyui', 'prompt': 'a cat',
    }
    # valid: the __help sentinel (no backend) passes validation
    assert inst._verify_json_format_args('{"prompt": "__help"}') == {'prompt': '__help'}
    # the anyOf/pattern failure surfaces as jsonschema.ValidationError from _verify...
    with pytest.raises(jsonschema.exceptions.ValidationError):
        inst._verify_json_format_args('{"prompt": "a cat"}')
    # ...and call() must handle it gracefully (no unhandled crash), returning an error result.
    result = inst.call('{"prompt": "a cat"}')
    assert result and str(result[0].text).startswith('ERROR: Invalid media_gen parameters')
    result = inst.call('{}')
    assert result and str(result[0].text).startswith('ERROR: Invalid media_gen parameters')


# ── propose_skill: rate-only OR create/update; NO multipleOf (coerce only bounds) ──
def test_propose_skill_rate_or_create_anyof():
    schema = _schema('propose_skill')

    # (a) rate-only
    assert _is_valid(schema, {'name': 's', 'rating': 7.5})
    # (a) create/update
    assert _is_valid(schema, {'name': 's', 'skill_content': '...md', 'justification': 'why'})
    # (a) create + rate together
    assert _is_valid(schema, {'name': 's', 'skill_content': '...md', 'justification': 'why', 'rating': 8})
    # (b) create WITHOUT justification → rejected (guard 427 "justification required")
    assert not _is_valid(schema, {'name': 's', 'skill_content': '...md'})
    # (b) neither content nor rating → rejected (guard 420 "No skill content")
    assert not _is_valid(schema, {'name': 's'})
    # no multipleOf: _coerce_rating enforces only [0,10], not 0.5 steps
    assert 'multipleOf' not in schema['properties']['rating']
    # so a non-half-step rating is still schema-valid (coerce handles range only)
    assert _is_valid(schema, {'name': 's', 'rating': 7.3})


# ── call_cli_agent (proxy-built): status OR tool_id+task OR harness+task ──
def _call_cli_agent_schema():
    return _build_schema_from_metadata(
        'call_cli_agent', TOOL_METADATA['call_cli_agent'],
    )['parameters']


def test_call_cli_agent_launch_status_control_anyof():
    schema = _call_cli_agent_schema()

    # (a) introspection
    assert _is_valid(schema, {'status': True})
    # (a) control of a background run
    assert _is_valid(schema, {'tool_id': 5, 'task': '__status', 'execution_mode': 'async'})
    # (a) launch
    assert _is_valid(schema, {'harness': 'claude_code', 'task': 'build the thing'})
    # (a) permission_posture still accepted (deprecated constant, kept for back-compat)
    assert _is_valid(schema, {
        'harness': 'claude_code', 'task': 'x', 'permission_posture': 'acceptEdits',
    })
    # (b) launch missing task → rejected (no branch matches)
    assert not _is_valid(schema, {'harness': 'claude_code'})
    # (b) control missing task → rejected
    assert not _is_valid(schema, {'tool_id': 5})
    # (b) empty → rejected
    assert not _is_valid(schema, {})
    # deprecated constant still single-valued: a different posture is rejected
    assert not _is_valid(schema, {
        'harness': 'claude_code', 'task': 'x', 'permission_posture': 'bypass',
    })
