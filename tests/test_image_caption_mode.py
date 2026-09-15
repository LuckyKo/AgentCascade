"""Tests for the image caption mode feature (auto/always/off) and the image token
estimation fix.

Covers:
  1. Mode gating in ``APIRouter.caption_images()`` — with a mocked vision endpoint and
     mocked pool settings, no live LLM / HTTP.
  2. Token estimation in ``get_message_stats()`` — structured image items (file-path and
     base64) are counted at IMAGE_TOKEN_ESTIMATE each, and the LRU cache key folds in the
     image-item count so same-text/different-image-count messages do not collide.
  3. Settings plumbing — PoolSettings round-trip + config handler validation.

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

# ── Shared router helper (isolated config dir, no persistence side effects) ─────
# NOTE: APIRouter.__init__ prefers the AGENT_CASCADE_TEST_CONFIG_DIR env var over the
# config_dir argument (router.py). conftest sets a SESSION-WIDE value for that env var, so
# every router would otherwise share one api_endpoints.json and contaminate each other via
# add_endpoint()/_save() → _load(). We therefore override the env var with a UNIQUE dir per
# router (same pattern as tests/test_fallback_compression.py) and restore it afterwards.


def _make_router(pool):
    """Build a lightweight APIRouter with its own isolated config dir + a mocked pool."""
    d = tempfile.mkdtemp(prefix='imgcap_mode_')
    orig = os.environ.get('AGENT_CASCADE_TEST_CONFIG_DIR')
    os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = d
    try:
        router = APIRouter(
            default_llm_cfg={
                'model': 'default-model',
                'api_base': 'http://localhost:1234/v1'
            },
            config_dir=d,
        )
    finally:
        if orig is None:
            os.environ.pop('AGENT_CASCADE_TEST_CONFIG_DIR', None)
        else:
            os.environ['AGENT_CASCADE_TEST_CONFIG_DIR'] = orig
    router._pool = pool
    return router


def _make_pool(mode='auto', instance=None):
    """A mocked pool exposing .settings.image_caption_mode and .get_instance()."""
    pool = MagicMock()
    pool.settings = PoolSettings(image_caption_mode=mode)
    if instance is None:
        pool.get_instance.return_value = None
    else:
        pool.get_instance.return_value = instance
    return pool


def _instance_with_endpoint(api_base, model):
    """A mocked AgentInstance whose _last_endpoint_config points at (api_base, model)."""
    inst = MagicMock()
    inst._last_endpoint_config = {'api_base': api_base, 'model': model}
    return inst


def _msg_with_image(image='media/a.png'):
    """A user message with an uncaptioned image ContentItem plus a text item."""
    return Message(role=USER, content=[ContentItem(image=image), ContentItem(text='look')])


def _vision_endpoint(router, api_base='http://v:8080/v1', model='vision-model'):
    """Register a vision-capable endpoint on the router and return it."""
    ep = APIEndpoint(name='vision', api_base=api_base, model=model, vision_enabled=True)
    router.add_endpoint(ep)
    return ep


# ── 1. Mode gating in caption_images() ────────────────────────────────────────


class TestCaptionModeGating:
    """Exercise the auto/always/off gate at the top of caption_images()."""

    def test_always_captions_even_when_active_endpoint_is_vision(self):
        """always: caption fires even though the active endpoint already has vision."""
        pool = _make_pool(mode='always', instance=_instance_with_endpoint('http://v:8080/v1', 'vision-model'))
        router = _make_router(pool)
        _vision_endpoint(router)

        messages = [_msg_with_image()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            fake_model = MagicMock()
            # chat() is a generator; return one final chunk carrying a caption string.
            fake_model.chat.return_value = iter([[{'role': 'assistant', 'content': 'a cat'}]])
            mock_gcm.return_value = fake_model
            router.caption_images(messages, agent_type='generalist', instance_name='inst1')

        assert mock_gcm.called, 'always mode must fire a caption call'
        # The image item should now carry the generated caption.
        img_item = messages[0].content[0]
        assert getattr(img_item, 'caption', None) == 'a cat'

    def test_auto_skips_when_active_endpoint_is_vision(self):
        """auto + vision-capable active endpoint: NO caption call, messages unchanged."""
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://v:8080/v1', 'vision-model'))
        router = _make_router(pool)
        _vision_endpoint(router)

        messages = [_msg_with_image()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_images(messages, agent_type='generalist', instance_name='inst1')

        assert not mock_gcm.called, 'auto mode must NOT caption when the active endpoint has vision'
        # Messages unchanged — no caption was added.
        img_item = messages[0].content[0]
        assert getattr(img_item, 'caption', None) is None

    def test_auto_captions_when_active_endpoint_is_text_only(self):
        """auto + text-only active endpoint: caption fires; '[Image]' placeholders are
        cleared and re-captioned, good existing captions are preserved."""
        # Active endpoint is text-only (vision_enabled=False) but a separate vision
        # endpoint exists in the registry for the caption call to use.
        pool = _make_pool(mode='auto', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        router.add_endpoint(
            APIEndpoint(name='text', api_base='http://t:8080/v1', model='text-model', vision_enabled=False))
        _vision_endpoint(router, api_base='http://v:8080/v1', model='vision-model')

        # msg A: image with a placeholder '[Image]' caption (should be re-captioned).
        msg_a = Message(role=USER, content=[ContentItem(image='media/a.png', caption='[Image]')])
        # msg B: image with a good caption (must be preserved, NOT re-captioned).
        msg_b = Message(role=USER, content=[ContentItem(image='media/b.png', caption='a real cat')])

        calls = []

        def fake_chat(*args, **kwargs):
            calls.append(1)
            return iter([[{'role': 'assistant', 'content': 'fresh caption'}]])

        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            mock_gcm.return_value.chat.side_effect = fake_chat
            router.caption_images([msg_a, msg_b], agent_type='generalist', instance_name='inst1')

        assert calls, 'auto mode must caption when the active endpoint is text-only'
        # Placeholder was cleared and re-captioned with fresh content.
        assert getattr(msg_a.content[0], 'caption', None) == 'fresh caption'
        # Good existing caption preserved (not in the uncaptioned set → untouched).
        assert getattr(msg_b.content[0], 'caption', None) == 'a real cat'

    def test_off_never_captions(self):
        """off: no caption call, messages unchanged."""
        pool = _make_pool(mode='off', instance=_instance_with_endpoint('http://t:8080/v1', 'text-model'))
        router = _make_router(pool)
        # No vision endpoint even — but the gate must short-circuit before that matters.
        messages = [_msg_with_image()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_images(messages, agent_type='generalist', instance_name='inst1')

        assert not mock_gcm.called, 'off mode must never fire a caption call'
        img_item = messages[0].content[0]
        assert getattr(img_item, 'caption', None) is None

    def test_invalid_mode_behaves_as_auto(self):
        """An invalid/unknown mode value normalizes to 'auto'."""
        # auto + vision active → skip (same as a valid 'auto').
        pool = _make_pool(mode='bogus', instance=_instance_with_endpoint('http://v:8080/v1', 'vision-model'))
        router = _make_router(pool)
        _vision_endpoint(router)

        messages = [_msg_with_image()]
        with patch('agent_cascade.llm.get_chat_model') as mock_gcm:
            router.caption_images(messages, agent_type='generalist', instance_name='inst1')

        assert not mock_gcm.called, "invalid mode must normalize to 'auto' (skip when vision active)"


class TestIsActiveEndpointVision:
    """Unit tests for the _is_active_endpoint_vision() helper's conservative posture."""

    def test_returns_false_for_missing_instance(self):
        pool = _make_pool(mode='auto', instance=None)
        router = _make_router(pool)
        _vision_endpoint(router)
        assert router._is_active_endpoint_vision('inst1') is False

    def test_returns_false_for_missing_last_endpoint_config(self):
        inst = MagicMock()
        inst._last_endpoint_config = None
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        _vision_endpoint(router)
        assert router._is_active_endpoint_vision('inst1') is False

    def test_returns_false_for_registry_mismatch(self):
        # Active endpoint points at a base/model not present in the registry.
        inst = _instance_with_endpoint('http://unknown:9999/v1', 'ghost-model')
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        _vision_endpoint(router)
        assert router._is_active_endpoint_vision('inst1') is False

    def test_returns_false_for_disabled_endpoint(self):
        # Inject directly (bypass add_endpoint/_save) so the disabled flag is preserved
        # deterministically for this unit test.
        inst = _instance_with_endpoint('http://v:8080/v1', 'vision-model')
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        ep = APIEndpoint(name='vision',
                         api_base='http://v:8080/v1',
                         model='vision-model',
                         vision_enabled=True,
                         enabled=False)
        with router._lock:
            router.endpoints[ep.id] = ep
        assert router._is_active_endpoint_vision('inst1') is False

    def test_returns_true_for_matching_vision_endpoint(self):
        inst = _instance_with_endpoint('http://v:8080/v1', 'vision-model')
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        _vision_endpoint(router)
        assert router._is_active_endpoint_vision('inst1') is True

    def test_returns_false_for_matching_text_only_endpoint(self):
        inst = _instance_with_endpoint('http://t:8080/v1', 'text-model')
        pool = _make_pool(mode='auto', instance=inst)
        router = _make_router(pool)
        router.add_endpoint(
            APIEndpoint(name='text', api_base='http://t:8080/v1', model='text-model', vision_enabled=False))
        assert router._is_active_endpoint_vision('inst1') is False


# ── 2. Token estimation for structured image items ────────────────────────────


class TestImageTokenEstimation:
    """get_message_stats() must count every structured image content item at
    IMAGE_TOKEN_ESTIMATE each (file-path and base64 alike), with no double counting,
    and the LRU cache key must fold in the image-item count."""

    @staticmethod
    def _clear_cache():
        from agent_cascade.utils import utils as U
        if hasattr(U.get_message_stats, '_msg_stats'):
            U.get_message_stats._msg_stats.clear()

    @pytest.fixture(autouse=True)
    def _isolate_cache(self):
        self._clear_cache()
        yield
        self._clear_cache()

    # Valid 1x1 transparent PNG as a base64 data URI (decodable — no filesystem side effect).
    B64_IMAGE = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=='

    @staticmethod
    def _rendered_tokens(msg):
        """qwen_count of the exact text extract_text_from_message() renders for `msg` —
        i.e. the placeholder/info-line text portion (shared '(Uploaded ...)' line + one
        '[Image]' marker per image). The flat IMAGE_TOKEN_ESTIMATE is added on top by
        get_message_stats(); this isolates the text portion so we can assert the exact real
        total without hardcoding tokenizer output."""
        from agent_cascade.utils.tokenization_qwen import count_tokens as qwen_count
        from agent_cascade.utils.utils import extract_text_from_message
        return qwen_count(extract_text_from_message(msg, add_upload_info=True))

    def test_file_path_image_counted(self):
        """A file-path image item must be counted at IMAGE_TOKEN_ESTIMATE (plus the rendered
        placeholder text tokens) — i.e. NOT 0 as before the fix."""
        from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE
        from agent_cascade.utils.utils import get_message_stats

        img = 'media/a.png'
        text_only = Message(role=USER, content='hello world')
        with_img = Message(role=USER, content=[ContentItem(text='hello world'), ContentItem(image=img)])

        base = get_message_stats(text_only)['tokens']
        total = get_message_stats(with_img)['tokens']
        expected_delta = IMAGE_TOKEN_ESTIMATE \
            + self._rendered_tokens(with_img) - self._rendered_tokens(text_only)
        assert total - base == expected_delta, \
            f"file-path image must add {expected_delta} tokens (got delta {total - base})"

    def test_base64_image_item_counted(self):
        """A base64 data-URI structured item is counted via the same structured path —
        IMAGE_TOKEN_ESTIMATE + placeholder text tokens, NOT 0."""
        from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE
        from agent_cascade.utils.utils import get_message_stats

        b64 = self.B64_IMAGE
        text_only = Message(role=USER, content='hello world')
        with_b64 = Message(role=USER, content=[ContentItem(text='hello world'), ContentItem(image=b64)])

        base = get_message_stats(text_only)['tokens']
        total = get_message_stats(with_b64)['tokens']
        expected_delta = IMAGE_TOKEN_ESTIMATE \
            + self._rendered_tokens(with_b64) - self._rendered_tokens(text_only)
        assert total - base == expected_delta, \
            f"base64 image item must add {expected_delta} tokens (got delta {total - base})"

    def test_mixed_no_double_count(self):
        """Two structured images (file-path + base64) → each counted exactly once at
        IMAGE_TOKEN_ESTIMATE. The rendered placeholder text is ONE shared '(Uploaded ...)'
        info line plus one '[Image]' marker per image. We assert the exact real total so a
        regression (e.g. dropping to 0 for structured items, or double counting) fails loudly."""
        from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE
        from agent_cascade.utils.utils import get_message_stats

        img = 'media/a.png'
        b64 = self.B64_IMAGE
        text_only = Message(role=USER, content='hello world')
        mixed = Message(role=USER,
                        content=[
                            ContentItem(text='hello world'),
                            ContentItem(image=img),
                            ContentItem(image=b64),
                        ])

        base = get_message_stats(text_only)['tokens']
        total = get_message_stats(mixed)['tokens']
        # Expected: 2 flat estimates + (rendered-text tokens of `mixed` minus those of the
        # text-only baseline, which shares the same 'hello world' text).
        expected_delta = 2 * IMAGE_TOKEN_ESTIMATE \
            + self._rendered_tokens(mixed) - self._rendered_tokens(text_only)
        assert total - base == expected_delta, \
            f"two image items must add {expected_delta} tokens (got delta {total - base})"

    def test_lru_cache_key_folds_image_count(self):
        """Two messages with identical text but different image counts must NOT share a
        cache entry — the first call's result must not be returned for the second. The
        multimodal cache key folds in the image-item count (see utils.get_message_stats), so
        the second message gets its own entry instead of reusing the first's."""
        from agent_cascade.settings import IMAGE_TOKEN_ESTIMATE
        from agent_cascade.utils.utils import get_message_stats

        one_img = Message(role=USER, content=[ContentItem(text='same text'), ContentItem(image='media/a.png')])
        two_img = Message(
            role=USER,
            content=[ContentItem(text='same text'),
                     ContentItem(image='media/a.png'),
                     ContentItem(image='media/b.png')])

        t_one = get_message_stats(one_img)['tokens']
        t_two = get_message_stats(two_img)['tokens']

        # tokens = qwen_count(rendered) + N*ESTIMATE + OVERHEAD. For two same-text messages
        # the delta is (N2-N1)*ESTIMATE + [rendered_tokens(two) - rendered_tokens(one)].
        # If the cache key did NOT fold in the image count, t_two would reuse t_one's entry
        # and this delta would be 0.
        expected_delta = IMAGE_TOKEN_ESTIMATE \
            + self._rendered_tokens(two_img) - self._rendered_tokens(one_img)
        assert expected_delta > 0, 'test setup: the two messages must render different placeholder text'
        assert t_two - t_one == expected_delta, \
            f"same-text messages with different image counts must not collide " \
            f"(t_one={t_one}, t_two={t_two})"


# ── 3. Settings plumbing ───────────────────────────────────────────────────────


class TestSettingsPlumbing:
    """PoolSettings round-trip and config handler validation for image_caption_mode."""

    def test_from_dict_round_trip(self):
        ps = PoolSettings(image_caption_mode='always')
        d = ps.to_dict()
        assert d['image_caption_mode'] == 'always'
        restored = PoolSettings.from_dict(d)
        assert restored.image_caption_mode == 'always'

    def test_from_dict_missing_key_defaults_to_auto(self):
        d = PoolSettings().to_dict()
        d.pop('image_caption_mode', None)
        ps = PoolSettings.from_dict(d)
        assert ps.image_caption_mode == 'auto'

    def test_default_is_auto(self):
        assert PoolSettings().image_caption_mode == 'auto'

    def _run_handler(self, value):
        from agent_cascade.config_handlers import CONFIG_HANDLERS
        pool = MagicMock()
        pool.settings = PoolSettings(image_caption_mode='auto')
        handler = CONFIG_HANDLERS['image_caption_mode']
        handler({'image_caption_mode': value}, pool, [])
        return pool.settings.image_caption_mode

    def test_handler_valid_values(self):
        for v in ('auto', 'always', 'off'):
            assert self._run_handler(v) == v

    def test_handler_invalid_normalizes_to_auto(self):
        assert self._run_handler('bogus') == 'auto'
        assert self._run_handler(None) == 'auto'
        assert self._run_handler(123) == 'auto'
