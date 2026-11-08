"""Regression test: the 6 "declared-persist but never seeded" settings are now
seeded into the initial llm_cfg dicts so they persist and round-trip on restart.

Covers:
  * start_api_server.py Tier-4 llm_cfg: all 6 keys present with registry defaults.
  * api_server.py initial_llm_cfg: the 3 LLM keys + the 3 llm_cfg-sourced keys.
  * the 3 llm_cfg-sourced keys now pass the writer's ``if key in self.llm_cfg`` guard.

Expected values are re-derived from ``settings_registry._SEED_DEFAULTS`` (the source
of truth) so the test stays in sync with the registry. The dict literals are captured
via AST because the two modules are a script (start_api_server) and a ``main()`` body
(api_server) with heavy import side effects, so they are not imported directly.
"""

import ast
from pathlib import Path

import agent_cascade.settings as _settings
from agent_cascade.settings_registry import LLM_CFG_PERSIST_KEYS, _SEED_DEFAULTS

ROOT = Path(__file__).resolve().parent.parent

# The 3 LLM generation params and the 3 llm_cfg-sourced skill/memory settings.
LLM_KEYS = ('max_output_tokens', 'stop_sequences', 'timeout')
SOURCED_KEYS = ('memory_hint_skill_suggestions', 'memory_hint_skill_threshold',
                'skill_always_protected')
ALL_SIX = LLM_KEYS + SOURCED_KEYS


def _find_dict(source: str, var_name: str) -> ast.Dict:
    """Return the first dict literal assigned to ``var_name`` in the source."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == var_name:
                    if isinstance(node.value, ast.Dict):
                        return node.value
    raise AssertionError(f"no dict literal assigned to {var_name!r}")


def _dict_items(dict_node: ast.Dict) -> dict:
    """Map each string key to its value node (skipping ``**`` spreads)."""
    items = {}
    for key, value in zip(dict_node.keys, dict_node.values):
        if key is None:  # ** spread
            continue
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            items[key.value] = value
    return items


def _value(value_node: ast.AST) -> object:
    """Evaluate a value node that is a literal or a known settings name ref."""
    ns = {
        'SKILL_ALWAYS_PROTECTED_DEFAULT': _settings.SKILL_ALWAYS_PROTECTED_DEFAULT,
        'format_skill_always_protected': _settings.format_skill_always_protected,
        'DEFAULT_WILD_READ_TRUNCATION_CHARS': _settings.DEFAULT_WILD_READ_TRUNCATION_CHARS,
        'SKILL_SCORE_SETTINGS': _settings.SKILL_SCORE_SETTINGS,
    }
    return eval(compile(ast.Expression(value_node), '<value>', 'eval'), {'__builtins__': {}}, ns)


def _llm_cfg_items(var_name: str, rel_path: str) -> dict:
    source = (ROOT / rel_path).read_text(encoding='utf-8')
    return _dict_items(_find_dict(source, var_name))


def test_start_api_server_tier4_llm_cfg_seeds_all_six():
    """The Tier-4 fallback cfg must seed all 6 keys with the exact registry defaults."""
    items = _llm_cfg_items('llm_cfg', 'start_api_server.py')
    for key in ALL_SIX:
        assert key in items, f"Tier-4 llm_cfg missing {key!r}"
        assert _value(items[key]) == _SEED_DEFAULTS[key], \
            f"{key}: seeded {_value(items[key])!r} != registry default {_SEED_DEFAULTS[key]!r}"


def test_api_server_initial_llm_cfg_seeds_llm_and_sourced_keys():
    """initial_llm_cfg must carry the 3 LLM keys (new) plus the 3 sourced keys (existing)."""
    items = _llm_cfg_items('initial_llm_cfg', 'agent_cascade/api_server.py')
    for key in LLM_KEYS:
        assert key in items, f"initial_llm_cfg missing LLM key {key!r}"
        assert _value(items[key]) == _SEED_DEFAULTS[key], \
            f"{key}: seeded {_value(items[key])!r} != registry default {_SEED_DEFAULTS[key]!r}"
    for key in SOURCED_KEYS:
        assert key in items, f"initial_llm_cfg missing sourced key {key!r}"


def test_sourced_keys_pass_writer_guard():
    """The 3 llm_cfg-sourced keys must now pass ``_save_pool_settings``'s
    ``if key in self.llm_cfg`` guard (driven by LLM_CFG_PERSIST_KEYS) so they reach
    pool_settings.json."""
    items = _llm_cfg_items('llm_cfg', 'start_api_server.py')
    llm_cfg = {k: _value(v) for k, v in items.items()}
    for key in LLM_CFG_PERSIST_KEYS & set(SOURCED_KEYS):
        assert key in llm_cfg, f"sourced key {key!r} fails the writer guard (not in llm_cfg)"
