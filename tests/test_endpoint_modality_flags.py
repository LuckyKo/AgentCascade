"""Phase-1 omni-multimodal schema tests (M1) + adapter-gate invariant guard.

Covers:
  1. ``APIEndpoint`` per-modality capability flags (M1):
       * ``audio_enabled`` / ``video_enabled`` default False, so every existing
         endpoint/config behaves exactly as today;
       * ``from_dict`` round-trips them (default False when absent);
       * ``to_dict`` (asdict) and ``to_llm_cfg`` both carry them to the adapter cfg.
  2. Adapter invariant guard: a ``qwenvl_oai`` endpoint keeps audio/video → TEXT
     (``[Audio file: …]`` / ``[Video file: …]``) even when the cfg carries
     ``audio_enabled=True`` / ``video_enabled=True``. The audio/video emission
     gates are NOT opened in Phase 1 (they are class-property driven, not
     endpoint-driven), so the new flags are forward-looking only.

All tests are self-contained (no LLM / HTTP / network).
"""

from agent_cascade.api_router_pkg.endpoints import APIEndpoint


class TestModalityFlags:

    def test_defaults_are_false(self):
        """New flags default False so existing endpoints are unchanged."""
        ep = APIEndpoint(name='x', api_base='http://x/v1', model='m')
        assert ep.vision_enabled is True
        assert ep.audio_enabled is False
        assert ep.video_enabled is False

    def test_from_dict_defaults_when_absent(self):
        """An existing config dict with NO audio/video keys loads unchanged."""
        ep = APIEndpoint.from_dict({'name': 'x', 'api_base': 'http://x/v1', 'model': 'm'})
        assert ep.vision_enabled is True
        assert ep.audio_enabled is False
        assert ep.video_enabled is False

    def test_from_dict_round_trip(self):
        """Round-trip preserves explicitly-set audio/video values."""
        data = {
            'id': 'ep_x',
            'name': 'x',
            'api_base': 'http://x/v1',
            'model': 'm',
            'vision_enabled': True,
            'audio_enabled': True,
            'video_enabled': True,
        }
        ep = APIEndpoint.from_dict(data)
        assert ep.audio_enabled is True
        assert ep.video_enabled is True
        out = ep.to_dict()
        assert out['audio_enabled'] is True
        assert out['video_enabled'] is True

    def test_to_llm_cfg_emits_flags(self):
        """to_llm_cfg carries the flags through to the adapter cfg dict."""
        ep = APIEndpoint(name='x', api_base='http://x/v1', model='m',
                         audio_enabled=True, video_enabled=False)
        cfg = ep.to_llm_cfg()
        assert cfg['audio_enabled'] is True
        assert cfg['video_enabled'] is False
        assert cfg['vision_enabled'] is True


class TestAdapterGateInvariant:
    """The Phase-1 schema does NOT open the adapter audio/video emission gate."""

    def test_qwenvl_oai_keeps_audio_video_as_text(self):
        from agent_cascade.llm import get_chat_model
        from agent_cascade.llm.schema import ContentItem, Message

        cfg = {
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
            # Forward-looking flags — must NOT open the emission gate in Phase 1.
            'audio_enabled': True,
            'video_enabled': True,
        }
        adapter = get_chat_model(cfg)
        assert adapter.support_audio_input is False, \
            'Phase 1: audio gate remains class-driven (endpoint flag must not open it)'

        messages = [Message(role='user', content=[
            ContentItem(text='hi'),
            ContentItem(audio='media/a.wav'),
            ContentItem(video='media/v.mp4'),
        ])]
        parts = adapter.convert_messages_to_dicts(messages)[0]['content']
        types = [p['type'] for p in parts]
        assert 'input_audio' not in types, 'audio must stay text-gated in Phase 1'
        assert 'video_url' not in types and 'video' not in types, \
            'video must stay text-gated in Phase 1'
        assert {'type': 'text', 'text': '[Audio file: media/a.wav]'} in parts
        assert {'type': 'text', 'text': '[Video file: media/v.mp4]'} in parts
