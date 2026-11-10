"""Tests for unified media (image/audio/video) auto-captioning.

Covers the audio+video extension of the image caption pipeline:
  1. Capability-driven gate in ``APIRouter.caption_media()`` — skip when the active
     endpoint already handles the modality; caption the omni Captioner when it does
     not and the item has no caption. ``image_caption_mode`` is the unified gate.
  2. Modality-aware caption-endpoint resolver (``_get_caption_endpoint_cfg``) and the
     Risk R3 absent-flag asymmetry: vision absent == capable, audio/video absent ==
     NOT capable.
  3. The ``qwenvl_oai`` adapter injecting ``[Modality] <caption>`` in the gate-closed
     branch while preserving the byte-for-byte ``[Modality file: <v>]`` fallback.

All tests are self-contained (no LLM or API server required).
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from agent_cascade.agent_instance import PoolSettings
from agent_cascade.api_router import APIRouter
from agent_cascade.api_router_pkg.endpoints import APIEndpoint
from agent_cascade.llm.schema import USER, ContentItem, Message


# ── Shared helpers (isolated config dir, mocked pool) — mirrors
# tests/test_image_caption_mode.py so the two suites stay consistent. ──


def _make_router(pool, default_cfg=None):
    """Build a lightweight APIRouter with its own isolated config dir + a mocked pool."""
    d = tempfile.mkdtemp(prefix='media_cap_')
    orig = os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = d
    cfg = {'model': 'default-model', 'api_base': 'http://localhost:1234/v1'}
    if default_cfg:
        cfg.update(default_cfg)
    try:
        router = APIRouter(default_llm_cfg=cfg, config_dir=d)
    finally:
        if orig is None:
            os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
        else:
            os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = orig
    router._pool = pool
    return router


def _make_pool(mode='auto', instance=None):
    pool = MagicMock()
    pool.settings = PoolSettings(image_caption_mode=mode)
    pool.get_instance.return_value = instance
    return pool


def _instance_with_endpoint(api_base, model):
    inst = MagicMock()
    inst._last_endpoint_config = {'api_base': api_base, 'model': model}
    inst.agent_class = 'generalist'
    return inst


def _endpoint(router, name, api_base, model, vision=False, audio=False, video=False):
    ep = APIEndpoint(
        name=name,
        api_base=api_base,
        model=model,
        vision_enabled=vision,
        audio_enabled=audio,
        video_enabled=video,
    )
    router.add_endpoint(ep)
    return ep


def _msg_with_audio(audio='media/a.mp3', caption=None):
    ci = ContentItem(audio=audio)
    if caption is not None:
        ci.caption = caption
    return Message(role=USER, content=[ci])


def _msg_with_video(video='media/a.mp4', caption=None):
    ci = ContentItem(video=video)
    if caption is not None:
        ci.caption = caption
    return Message(role=USER, content=[ci])


def _fake_chat(caption):
    """Return a side_effect fn for chat_model.chat that yields one final caption chunk."""
    def _f(*args, **kwargs):
        return iter([[{'role': 'assistant', 'content': caption}]])
    return _f


# ── 1. Capability-driven gate in caption_media() ───────────────────────────────


class TestMediaCaptionGate:
    def test_auto_skips_audio_when_active_endpoint_is_audio_capable(self):
        """auto + audio-capable active endpoint: NO caption call; caption stays empty."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://o:8080/v1', 'omni-model'))
        router = _make_router(pool)
        _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model', vision=True, audio=True, video=True)

        messages = [_msg_with_audio()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')

        assert not mock_gcm.called, 'auto must NOT caption audio when the active endpoint is audio-capable'
        assert getattr(messages[0].content[0], 'caption', None) is None

    def test_auto_captions_audio_when_gate_closed_and_uncaptioned(self):
        """auto + audio-lacking active endpoint + no caption -> exactly one omni caption call."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        _endpoint(router, 'text', 'http://t:8080/v1', 'text-model')  # active, text-only
        omni = _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model',
                         vision=True, audio=True, video=True)
        router.set_agent_priorities('caption', [omni.id])

        messages = [_msg_with_audio()]
        calls = []
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            def fake_chat(*args, **kwargs):
                calls.append(1)
                return iter([[{'role': 'assistant', 'content': 'a dog barks'}]])
            mock_gcm.return_value.chat.side_effect = fake_chat
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')

        assert calls == [1], 'audio gate closed must fire exactly one caption call'
        assert getattr(messages[0].content[0], 'caption', None) == 'a dog barks'

    def test_existing_good_caption_not_recaptioned(self):
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        _endpoint(router, 'text', 'http://t:8080/v1', 'text-model')
        omni = _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model', audio=True)
        router.set_agent_priorities('caption', [omni.id])

        messages = [_msg_with_audio(caption='a real caption')]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')
        assert not mock_gcm.called, 'a good existing caption must not be re-captioned'
        assert messages[0].content[0].caption == 'a real caption'

    def test_placeholder_caption_is_recaptioned(self):
        """A prior '[Audio]' failure placeholder is cleared and re-captioned in auto."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        _endpoint(router, 'text', 'http://t:8080/v1', 'text-model')
        omni = _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model', audio=True)
        router.set_agent_priorities('caption', [omni.id])

        messages = [_msg_with_audio(caption='[Audio]')]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            mock_gcm.return_value.chat.side_effect = _fake_chat('fresh transcript')
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')
        assert mock_gcm.called, "auto must re-caption a failed '[Audio]' placeholder"
        assert messages[0].content[0].caption == 'fresh transcript'

    def test_off_never_captions_audio(self):
        pool = _make_pool(mode='off', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        messages = [_msg_with_audio()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')
        assert not mock_gcm.called, 'off mode must never fire a caption call'
        assert getattr(messages[0].content[0], 'caption', None) is None

    def test_mixed_modalities_caption_only_the_incapable_one(self):
        """A vision-capable but audio-lacking endpoint: images skipped, audio captioned."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://v:8080/v1', 'vision-model'))
        router = _make_router(pool)
        _endpoint(router, 'vision', 'http://v:8080/v1', 'vision-model', vision=True, audio=False)
        omni = _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model',
                         vision=True, audio=True, video=True)
        router.set_agent_priorities('caption', [omni.id])

        messages = [Message(role=USER, content=[ContentItem(image='a.png'), ContentItem(audio='a.mp3')])]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            mock_gcm.return_value.chat.side_effect = _fake_chat('an audio clip')
            router.caption_media(messages,
                                 modalities={'image', 'audio'},
                                 agent_type='generalist',
                                 instance_name='inst1')
        # Image handled natively (vision active) -> no caption; audio gated -> captioned.
        assert getattr(messages[0].content[0], 'caption', None) is None, 'image must NOT be captioned'
        assert messages[0].content[1].caption == 'an audio clip', 'audio must be captioned'

    def test_unresolved_caption_list_degrades_to_audio_placeholder(self):
        """No resolvable audio caption endpoint -> '[Audio]' placeholder, no HTTP (R9)."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        router.add_endpoint(APIEndpoint(name='text', api_base='http://t:8080/v1',
                                        model='text-model', vision_enabled=False))
        # caption list empty, default has vision/audio/video absent -> audio resolver returns None
        messages = [_msg_with_audio()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_media(messages, modalities={'audio'}, agent_type='generalist', instance_name='inst1')
        assert not mock_gcm.called, 'no audio-capable endpoint must not fire HTTP'
        assert messages[0].content[0].caption == '[Audio]'


# ── 2. Modality-aware resolver + Risk R3 absent-flag asymmetry ──────────────────


class TestModalityAwareResolver:
    def test_caption_resolver_skips_vision_only_for_audio(self):
        """A vision-only endpoint is NOT chosen for audio; the omni is."""
        pool = _make_pool(mode='auto', instance=None)
        router = _make_router(pool)
        vision_only = _endpoint(router, 'v', 'http://v:8080/v1', 'v-model', vision=True, audio=False)
        router.set_agent_priorities('caption', [vision_only.id])
        assert router._get_caption_endpoint_cfg('generalist', modality='audio') is None, \
            'a vision-only endpoint must not satisfy audio'
        # Adding an audio-capable omni after it wins.
        omni = _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model',
                         vision=True, audio=True, video=True)
        router.set_agent_priorities('caption', [vision_only.id, omni.id])
        cfg = router._get_caption_endpoint_cfg('generalist', modality='audio')
        assert cfg is not None and cfg['model'] == 'omni-model'

    def test_caption_resolver_image_still_returns_vision_endpoint(self):
        pool = _make_pool(mode='auto', instance=None)
        router = _make_router(pool)
        vision_only = _endpoint(router, 'v', 'http://v:8080/v1', 'v-model', vision=True, audio=False)
        router.set_agent_priorities('caption', [vision_only.id])
        cfg = router._get_caption_endpoint_cfg('generalist', modality='image')
        assert cfg is not None and cfg['model'] == 'v-model'

    def test_active_endpoint_audio_absent_flag_is_not_capable(self):
        """R3: audio absent flag == NOT capable (-> caption). Opposite of vision."""
        inst = MagicMock()
        inst._last_endpoint_config = None
        inst.agent_class = 'generalist'
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)  # bare Tier-4 default, no *_enabled flags
        # Vision absent defaults to capable (True); audio/video absent to NOT capable (False).
        assert router._is_active_endpoint_vision('inst1') is True
        assert router._is_active_endpoint_audio('inst1') is False
        assert router._is_active_endpoint_video('inst1') is False

    def test_active_endpoint_audio_matching_registry_flag_is_trusted(self):
        """Matched-registry audio_enabled flag wins (default False there is dead)."""
        inst = _instance_with_endpoint('http://o:8080/v1', 'omni-model')
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        _endpoint(router, 'omni', 'http://o:8080/v1', 'omni-model', audio=True)
        assert router._is_active_endpoint_audio('inst1') is True
        assert router._is_active_endpoint_video('inst1') is False  # video_enabled default False


# ── 3. Adapter caption-vs-bare-path injection (inspect emitted payload, not reply) ─


class TestAdapterCaptionInjection:
    @staticmethod
    def _adapter():
        from agent_cascade.llm import get_chat_model
        return get_chat_model({
            'model': 'x',
            'api_base': 'http://localhost:9/v1',
            'model_server': 'http://localhost:9/v1',
            'api_key': 'EMPTY',
            'model_type': 'qwenvl_oai',
        })

    def _content_with(self, messages, generate_cfg):
        return self._adapter().convert_messages_to_dicts(messages, generate_cfg=generate_cfg)[0]['content']

    @staticmethod
    def _msg(media_key, value, caption=None):
        ci = ContentItem(**{media_key: value})
        if caption is not None:
            ci.caption = caption
        return [Message(role='user', content=[ci])]

    def test_audio_gate_closed_with_caption_emits_caption(self):
        parts = self._content_with(self._msg('audio', 'data:;base64,AAAA', caption='a bell rings'),
                                   {'audio_enabled': False})
        assert {'type': 'text', 'text': '[Audio] a bell rings'} in parts
        assert 'input_audio' not in [p['type'] for p in parts]

    def test_audio_gate_closed_absent_flag_with_caption(self):
        parts = self._content_with(self._msg('audio', 'data:;base64,AAAA', caption='a bell rings'), None)
        assert {'type': 'text', 'text': '[Audio] a bell rings'} in parts

    def test_audio_gate_closed_no_caption_keeps_bare_path(self):
        """Default-off invariant: no caption -> byte-for-byte [Audio file: <v>]."""
        parts = self._content_with(self._msg('audio', 'data:;base64,AAAA'), None)
        assert {'type': 'text', 'text': '[Audio file: data:;base64,AAAA]'} in parts

    def test_video_gate_closed_with_and_without_caption(self):
        cap = self._content_with(self._msg('video', 'data:video/mp4;base64,BBB', caption='a scene'),
                                 {'video_enabled': False})
        assert {'type': 'text', 'text': '[Video] a scene'} in cap
        noc = self._content_with(self._msg('video', 'data:video/mp4;base64,BBB'), {'video_enabled': False})
        assert {'type': 'text', 'text': '[Video file: data:video/mp4;base64,BBB]'} in noc

    def test_gate_open_emits_real_media_and_ignores_caption(self):
        """Gate OPEN -> input_audio emitted; caption ignored (model sees the media)."""
        parts = self._content_with(self._msg('audio', 'data:;base64,AAAA', caption='ignored'),
                                   {'audio_enabled': True})
        audio = [p for p in parts if p['type'] == 'input_audio']
        assert len(audio) == 1
        assert '[Audio]' not in ' '.join(p.get('text', '') for p in parts)
