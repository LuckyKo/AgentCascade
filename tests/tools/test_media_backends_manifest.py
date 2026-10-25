"""W7 T11: backend-manifest structure + release_model_around_call resolver.

The manifest is the single source of truth for tool enum, API validation,
__help, and the UI. These tests pin its SHAPE (so the UI and __help cannot
silently degrade) and the resolver's precedence (block key > default > True).
"""
import json

import pytest

from agent_cascade.tools.media_backends import (
    BACKENDS,
    BACKEND_MANIFEST,
    manifest_ids,
)
from agent_cascade.tools.media_backends.base import resolve_release_model_around_call


class TestBackendManifest:
    def test_manifest_matches_registry(self):
        """No silently-empty manifest: ids and ORDER mirror the registry."""
        assert manifest_ids() == list(BACKENDS)
        assert set(BACKEND_MANIFEST) == set(BACKENDS)

    def test_every_entry_has_the_ui_and_help_shape(self):
        for bid, entry in BACKEND_MANIFEST.items():
            assert entry['id'] == bid
            assert isinstance(entry['label'], str) and entry['label']
            assert isinstance(entry['note'], str) and entry['note']
            # TEXT-output backends (output_kind='text', e.g. voice_to_text) own NO media type;
            # an empty media_types list is correct for them. Every other backend must claim one.
            assert isinstance(entry['media_types'], list)
            if entry.get('output_kind') != 'text':
                assert entry['media_types']
            assert isinstance(entry['availability'], str) and entry['availability']
            assert isinstance(entry['agent_args'], list) and entry['agent_args']
            assert isinstance(entry['overrides_keys'], list)
            assert isinstance(entry['defaults'], dict)
            # advanced_fields must exist (every backend exposes at least a timeout);
            # connection_fields MAY be empty — a zero-config backend (svg2png: no
            # binary/server/dir to configure) legitimately renders nothing there.
            assert entry['advanced_fields']
            for group in ('connection_fields', 'advanced_fields'):
                specs = entry[group]
                assert isinstance(specs, list)
                keys = set()
                for spec in specs:
                    assert {'key', 'label', 'kind'} <= set(spec)
                    assert spec['kind'] in ('path', 'url', 'text', 'number', 'checkbox')
                    assert spec['key'] not in keys  # unique within a block
                    keys.add(spec['key'])

    def test_release_flag_field_declared_for_every_backend(self):
        for entry in BACKEND_MANIFEST.values():
            advanced = [s['key'] for s in entry['advanced_fields']]
            assert 'release_model_around_call' in advanced
            assert 'timeout' in advanced
            assert entry['defaults'].get('release_model_around_call') is True

    def test_timeout_limits_match_per_backend_expectations(self):
        expected = {'comfyui': (30, 1800, 5), 'sdcpp': (30, 1800, 30),
                    'kobold_tts': (60, 3600, 30), 'chatterbox': (60, 3600, 30),
                    'voice_to_text': (30, 3600, 30), 'svg2png': (5, 120, 5)}
        for bid, entry in BACKEND_MANIFEST.items():
            tf = [s for s in entry['advanced_fields'] if s['key'] == 'timeout']
            assert len(tf) == 1
            assert (tf[0]['min'], tf[0]['max'], tf[0]['step']) == expected[bid]

    def test_manifest_is_json_serialisable(self):
        """The API serves it verbatim; a non-JSON type would break the endpoint."""
        json.dumps(BACKEND_MANIFEST)


class TestReleaseFlagResolver:
    def test_absent_defaults_true(self):
        assert resolve_release_model_around_call({}, 'sdcpp') is True
        assert resolve_release_model_around_call({'sdcpp': {}}, 'sdcpp') is True
        assert resolve_release_model_around_call(None, 'chatterbox') is True

    def test_block_key_wins(self):
        cfg = {'sdcpp': {'release_model_around_call': False}}
        assert resolve_release_model_around_call(cfg, 'sdcpp') is False
        cfg = {'sdcpp': {'release_model_around_call': True}}
        assert resolve_release_model_around_call(cfg, 'sdcpp') is True

    def test_neighbour_block_flag_ignored(self):
        cfg = {'chatterbox': {'release_model_around_call': False}}
        assert resolve_release_model_around_call(cfg, 'sdcpp') is True

    def test_obvious_strings_parsed_otherwise_default(self):
        for val, want in (('false', False), ('FALSE', False), ('off', False),
                         ('true', True), ('ON', True)):
            assert resolve_release_model_around_call(
                {'sdcpp': {'release_model_around_call': val}}, 'sdcpp') is want
        # Unparseable never silently disables the dance.
        for val in ('banana', 0, 1, [], None):
            assert resolve_release_model_around_call(
                {'sdcpp': {'release_model_around_call': val}}, 'sdcpp') is True
