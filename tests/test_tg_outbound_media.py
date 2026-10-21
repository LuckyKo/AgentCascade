"""Outbound media delivery for the Telegram bridge (todo.md:102).

Binding plan: plans/tg-outbound-media_PLAN.md. Three layers, mirroring the plan's
test strategy (§10.1-10.3):

  §10.1  the PURE parser (media_outbound.split_media + helpers) — no PTB, no I/O;
  §10.2  the media-aware sender (bot._safe_send / _send_media_aware / _send_one_media)
         with the Bot a MagicMock, including the byte-identical no-op contract for
         text-only messages (the load-bearing constraint for the text-only call sites);
  §10.3  resolution against real files in tmp_path (media_outbound.resolve_media_source).

No network, no real Telegram bot, no running AC. Run serially (pytest.ini pins xdist):
    python -m pytest tests/test_tg_outbound_media.py -m "not live_api and not skip_if_no_local" --timeout=90 -q
"""

import asyncio
import base64
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure top-level imports work (mirror tests/test_telegram_bridge.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade import settings
from agent_cascade.telegram_bridge import bot as bot_mod
from agent_cascade.telegram_bridge.bot import (_safe_send, _send_one, _send_one_media,
                                              chunk_text)
from agent_cascade.telegram_bridge.config import BridgeConfig, load_config
from agent_cascade.telegram_bridge import media_outbound as mo
from telegram.constants import MessageLimit


def _run(coro):
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_bot():
    """MagicMock bot whose send methods are awaitable and record their kwargs."""
    bot = MagicMock()
    for name in ('send_message', 'send_photo', 'send_audio', 'send_video'):
        setattr(bot, name, AsyncMock())
    return bot


def _sent_texts(bot):
    return [c.kwargs['text'] for c in bot.send_message.call_args_list]


def _sent_photos(bot):
    return [c.kwargs for c in bot.send_photo.call_args_list]


def _push_lines(caplog):
    return [r.getMessage() for r in caplog.records if '[TG-PUSH] delivered' in r.getMessage()]


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """tmp_path patched as the allowed outbound-media root.

    One setup for every §10.2 sender test (write the file here, patch the roots, build
    the mock bot, run _safe_send). Yields the directory itself so tests can write into
    it; _media_allowed_roots reads settings at call time, so the patch is honoured.
    """
    monkeypatch.setattr(settings, 'TG_MEDIA_ALLOWED_ROOTS', str(tmp_path))
    return tmp_path


# The resolution tests pass the settings constants instead of literals: they pin behaviour
# at whatever the cap currently is, so a tuned cap cannot silently break them. The shipped
# default value is pinned in exactly one place — test_config_mirrors_the_settings_defaults,
# which is where changing those numbers is supposed to fail loudly.
_CAPS = dict(max_photo_bytes=settings.TG_MEDIA_MAX_PHOTO_BYTES,
             max_other_bytes=settings.TG_MEDIA_MAX_OTHER_BYTES)


# --------------------------------------------------------------------------- #
# §10.1  pure parser — table-driven, text is NEVER edited
# --------------------------------------------------------------------------- #

MD_IMAGE = 'Done: ![image](N:/work/logs/media/images/img_a.jpg) ok'
BARE_IMAGE = 'Generated image: N:/logs/media/images/img_1.jpg (1024x768, config=sdcpp, 3.2s)'
BARE_AUDIO_WIN = 'Saved to: N:\\logs\\media\\audio\\tts_1.mp3 (voice=x)'
POSIX_IMAGE = 'see /srv/media/pic.png now'

NO_MEDIA_CASES = [
    ('plain text, no links at all', 'plain'),
    ('open photo.jpg please', 'relative path is excluded by design (§1.3)'),
    ('![alt](img (1).jpg)', 'E16: target stops at the first ")" -> "img (1" has no ext'),
    ('![a](', 'malformed, no crash'),
    ('![a])', 'malformed, no crash'),
    ('![](doc.pdf) and [text](notes.md)', 'non-media ext + plain link, not a media link'),
    ('![img]()', 'empty target'),
    ('N:/work/notes.pdf and /etc/config.json', 'bare non-media paths'),
]


def test_split_media_plain_link_target_is_found_by_the_bare_pass():
    """The markdown detector needs '![', but with bare detection ON an absolute media
    path is an absolute media path wherever it sits — including inside a plain
    [text](path) link. Delivering it is the point of the feature (plan §1.3)."""
    _, items = mo.split_media('[not media](N:/work/x.jpg)')
    assert len(items) == 1
    assert items[0].source == 'local_path' and items[0].value == 'N:/work/x.jpg'
    _, items = mo.split_media('[not media](N:/work/x.jpg)', detect_bare_paths=False)
    assert items == [], 'and the kill-switch turns exactly this off'


@pytest.mark.parametrize('text,reason', NO_MEDIA_CASES)
def test_split_media_finds_nothing(text, reason):
    out_text, items = mo.split_media(text)
    assert out_text == text, 'parser must never edit the text'
    assert items == [], f'expected no media ({reason}): {text!r}'


def test_split_media_markdown_image():
    out_text, items = mo.split_media(MD_IMAGE)
    assert out_text == MD_IMAGE
    assert len(items) == 1
    item = items[0]
    assert item.kind == 'image' and item.source == 'local_path'
    assert item.value == 'N:/work/logs/media/images/img_a.jpg'
    assert item.alt == 'image'
    assert MD_IMAGE[item.span[0]:item.span[1]] == '![image](N:/work/logs/media/images/img_a.jpg)'


def test_split_media_bare_paths_media_gen_formats():
    """The exact media_gen.py:453 / file_ops.py:859 output shapes must be detected."""
    for text, expected in [
        (BARE_IMAGE, 'N:/logs/media/images/img_1.jpg'),
        (BARE_AUDIO_WIN, 'N:\\logs\\media\\audio\\tts_1.mp3'),
        (POSIX_IMAGE, '/srv/media/pic.png'),
    ]:
        out_text, items = mo.split_media(text)
        assert out_text == text
        assert len(items) == 1, f'bare absolute path not detected: {text!r}'
        assert items[0].value == expected
        assert items[0].alt == ''


def test_split_media_bare_path_trailing_punctuation():
    """A sentence-ending '.' must not become part of the extension (E4)."""
    _, items = mo.split_media('N:/a\\b.png.')
    assert len(items) == 1
    assert items[0].value == 'N:/a\\b.png'


def test_split_media_bare_path_kill_switch():
    """TG_MEDIA_DETECT_BARE_PATHS=False (E17) disables only the bare detector."""
    _, items = mo.split_media(BARE_IMAGE, detect_bare_paths=False)
    assert items == []
    _, items = mo.split_media(MD_IMAGE, detect_bare_paths=False)
    assert len(items) == 1, 'the markdown detector must stay on'


def test_split_media_masking_prevents_double_count():
    """A markdown target must not ALSO be found by the bare pass (verified in plan §2.1)."""
    _, items = mo.split_media(MD_IMAGE)
    assert len(items) == 1
    _, items = mo.split_media('a ![image](N:/work/x.jpg) b N:/work/y.jpg')
    assert len(items) == 2
    assert [i.value for i in items] == ['N:/work/x.jpg', 'N:/work/y.jpg']


def test_split_media_alt_never_decides_kind():
    """Plan §1.2: kind comes from the extension/MIME, never the alt label."""
    _, items = mo.split_media('![audio](N:/work/x.png)')
    assert len(items) == 1 and items[0].kind == 'image'
    _, items = mo.split_media('![image](N:/work/voice.mp3)')
    assert len(items) == 1 and items[0].kind == 'audio'


def test_split_media_relative_markdown_target_stays_text():
    """§1.3: relative targets are excluded — resolving them would guess the bridge CWD."""
    for text in ('![image](photos/a.jpg)', '![audio](a.mp3)', '![x](file://a.jpg)'):
        original, items = mo.split_media(text)
        assert items == [], text
        assert original == text, 'the parser never edits the text either way'


def test_split_media_data_uri():
    payload = base64.b64encode(b'\x89PNG fake').decode()
    _, items = mo.split_media(f'![name](data:image/png;base64,{payload})')
    assert len(items) == 1
    assert items[0].kind == 'image' and items[0].source == 'data_uri'
    assert items[0].alt == 'name'
    _, items = mo.split_media('![x](data:text/html,hi)')
    assert items == [], 'a non-media MIME is not media'


def test_split_media_http_pass_through():
    _, items = mo.split_media('look ![image](https://x.com/img.jpg?v=2) done')
    assert len(items) == 1
    assert items[0].source == 'http_url'
    assert items[0].value == 'https://x.com/img.jpg?v=2'


def test_split_media_bare_url_scheme_is_not_a_drive_path():
    """BUG_0114: the bare-path drive branch '[A-Za-z]:[\\/]' had no left boundary, so the
    'p:' of 'http:', 's:' of 'https:' and 'e:' of 'file:' matched as a Windows drive —
    the URL was classified local_path, resolved against the bridge CWD, reported missing,
    and logged a WARNING per URL. The old '://' post-match guard caught '://' forms but
    still misread the single-slash 'http:/x.jpg'. URL tokens are now masked before the
    bare pass (a scheme is >=2 letters, disjoint from a 1-letter drive), so bare URLs are
    never media — while real drive/POSIX paths are still detected."""
    for text in ('http://example.com/a.jpg',
                 'https://x.com/a.jpg',
                 'file:///N:/work/x.jpg',
                 'ftp://host/a.jpg',
                 'http:/malformed.jpg',      # the residual single-slash case the guard missed
                 'mailto:a@b.jpg'):
        out_text, items = mo.split_media(text)
        assert out_text == text, 'parser never edits text'
        assert items == [], f'bare URL must not be classified as local media: {text!r}'

    # Positive controls: real absolute paths are still delivered unchanged.
    _, items = mo.split_media('N:/work/x.jpg')
    assert len(items) == 1 and items[0].value == 'N:/work/x.jpg'
    _, items = mo.split_media(r'C:\foo\a.jpg')
    assert len(items) == 1 and items[0].value == r'C:\foo\a.jpg'
    _, items = mo.split_media('D:/foo/img.png')
    assert len(items) == 1 and items[0].value == 'D:/foo/img.png'

    # Masking is offset-preserving: a real path on the same line as a URL is still found.
    _, items = mo.split_media('see http://x.com/a.jpg and N:/work/b.jpg')
    assert len(items) == 1 and items[0].value == 'N:/work/b.jpg'


def test_split_media_dedupes_identical_items():
    """Plan O-3: one upload per (kind, normalized target); the duplicate keeps its link."""
    _, items = mo.split_media('dup N:/a/img.jpg and N:/a/img.jpg and ![image](N:/a/img.jpg)')
    assert len(items) == 1
    assert items[0].span == (4, 16), 'the FIRST occurrence is the one delivered'


def test_split_media_video_gate():
    """Plan O-8: video stays text unless TG_MEDIA_SEND_VIDEO (allow_video) is on."""
    _, items = mo.split_media('clip N:/a/v.mp4')
    assert items == []
    _, items = mo.split_media('clip N:/a/v.mp4', allow_video=True)
    assert len(items) == 1 and items[0].kind == 'video'


def test_split_media_empty_text():
    assert mo.split_media('') == ('', [])


def test_media_ext_of_query_and_fragment():
    """Plan E9: strip ?/# first; a media ext living in the query is not trusted."""
    assert mo.media_ext_of('https://a/b/img.jpg?v=2#f') == 'jpg'
    assert mo.media_ext_of('https://a/download?x=.jpg') is None
    assert mo.media_ext_of('N:/a/b.PNG') == 'png'
    assert mo.media_ext_of('N:/a/b') is None


def test_data_uri_mime():
    """The gate returns the KIND only; the matched MIME has no reader (finding 13)."""
    assert mo.data_uri_mime('data:image/png;base64,AAA') == 'image'
    assert mo.data_uri_mime('data:audio/mpeg;base64,AAA') == 'audio'
    assert mo.data_uri_mime('data:text/html,hi') is None
    assert mo.data_uri_mime('not a uri') is None


def test_caption_from_alt_generic_labels():
    """state_builder.py's placeholder alts must never become captions (plan §7.2)."""
    for label in ('', '   ', 'image', 'IMG', 'Audio', 'photo', 'media'):
        assert mo.caption_from_alt(label) is None
    assert mo.caption_from_alt('  Sunset over the lake ') == 'Sunset over the lake'


def test_normalize_local_target_windows_drive_repair(tmp_path):
    """Plan E24/E26: strip file://, repair the drive letter, unquote EXACTLY ONCE."""
    expected = str(Path('C:/work/x y.jpg').resolve())
    assert mo.normalize_local_target('file:///C:/work/x%20y.jpg') == expected
    assert mo.normalize_local_target('/C:/work/x y.jpg') == expected
    assert mo.normalize_local_target('C:/work/x y.jpg') == expected
    # POSIX absolute paths are untouched by the repair.
    assert mo.normalize_local_target('/etc/x.jpg') == str(Path('/etc/x.jpg').resolve())


def test_classify_target_rejects_non_media():
    assert mo.classify_target('') == (None, '', '')
    assert mo.classify_target('![x]') == (None, '', '')
    assert mo.classify_target('doc.pdf') == (None, '', '')
    kind, source, value = mo.classify_target('https://a/b/img.jpg')
    assert (kind, source) == ('image', 'http_url') and value == 'https://a/b/img.jpg'


# --------------------------------------------------------------------------- #
# §10.2  the sender: text-only must be a byte-identical no-op
# --------------------------------------------------------------------------- #

# §10.1 no-op proof corpus: the EXACT fixed strings the other _safe_send call sites pass
# (bot.py's waiter/handlers) plus api_server.py's restart notice — the regression guard for
# "byte-identical for text-only". Keep this list in sync with the call sites; adding a
# call site with a media-looking string is exactly what this catches.
TEXT_ONLY_SAMPLES = [
    '⚠️ Could not reach AC. Try again later.',
    '⚠️ AC appears offline — I stopped waiting.',
    '⌛ Reached the max wait ceiling; stopping here. Check the AC session for the result.',
    "⏳ Still working (2m05s) — I'll keep waiting and send the reply when it's done.",
    '⚠️ Image too large (max 20 MB).',
    '⚠️ Could not download image.',
    '⚠️ Could not reach AC. Is it running?',
    '🏃 Started → coder. I\'ll reply when done.',
    '✅ Restart complete — the server is back up.',   # api_server.py:997
    '/status',
    'x' * 5000,   # long enough to chunk; the chunking path must be untouched too
]


@pytest.mark.parametrize('text', TEXT_ONLY_SAMPLES)
def test_split_media_is_a_noop_on_every_call_site_string(text):
    """§10.1: split_media(s) == (s, []) for every fixed string the other call sites send."""
    original, items = mo.split_media(text,
                                     allow_video=settings.TG_MEDIA_SEND_VIDEO,
                                     detect_bare_paths=settings.TG_MEDIA_DETECT_BARE_PATHS)
    assert items == [], text
    assert original == text, 'the parser never edits the text'


@pytest.mark.parametrize('text', TEXT_ONLY_SAMPLES)
def test_safe_send_text_only_is_byte_identical(text, caplog):
    """The load-bearing constraint: for text-only messages, _safe_send behaves EXACTLY
    as it did before this feature — same send_message calls, same [TG-PUSH] line."""
    bot = _mock_bot()
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run(_safe_send(bot, chat_id=42, text=text))

    expected_texts = chunk_text(text)
    assert _sent_texts(bot) == expected_texts, 'chunk sequence must be identical'
    assert bot.send_photo.call_count == 0 and bot.send_audio.call_count == 0
    line = _push_lines(caplog)[-1]
    assert f'delivered {len(expected_texts)} chunk(s)' in line
    assert f'{len(text)} chars' in line
    assert 'media=' not in line, 'the media suffix belongs to the media path only'
    for record in caplog.records:
        assert text not in record.getMessage(), 'the [TG-PUSH] line stays content-free'


def test_safe_send_feature_flag_off_is_the_old_behaviour(monkeypatch, tmp_path):
    """TG_OUTBOUND_MEDIA=False must restore text-only delivery EXACTLY — including not
    running the parser at all (its cost AND its failure surface belong to the feature)."""
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\x89PNG small')
    text = f'look ![image]({path}) done'
    monkeypatch.setattr(settings, 'TG_OUTBOUND_MEDIA', False)

    def _boom(*_args, **_kwargs):
        raise AssertionError('split_media must not run when the flag is off')

    monkeypatch.setattr(bot_mod, 'split_media', _boom)
    bot = _mock_bot()
    _run(_safe_send(bot, chat_id=42, text=text))
    assert _sent_texts(bot) == [text], 'the link must stay in the text when the flag is off'
    assert bot.send_photo.call_count == 0


def test_safe_send_delivers_photo_and_excises_link(media_root):
    """Happy path: text first (link excised), then the photo with a real caption.

    The 'file:///' + drive-letter form is what the browser's own markdown emits, so
    it must survive normalize_local_target's Windows repair (plan E24).
    """
    path = media_root / 'sunset.jpg'
    path.write_bytes(b'\x89PNG small')
    bot = _mock_bot()
    text = f'look ![Sunset over the lake](file:///{path}) done'
    _run(_safe_send(bot, chat_id=42, text=text))
    assert _sent_texts(bot) == ['look  done'], 'the delivered link must be excised from the text'
    photos = _sent_photos(bot)
    assert len(photos) == 1
    assert photos[0]['chat_id'] == 42
    assert photos[0]['caption'] == 'Sunset over the lake'


def test_safe_send_media_log_line_carries_media_count(media_root, caplog):
    path = media_root / 'photo.jpg'
    path.write_bytes(b'\x89PNG small')
    bot = _mock_bot()
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run(_safe_send(bot, chat_id=42, text=f'look ![image]({path}) done'))
    line = _push_lines(caplog)[-1]
    assert ', media=1' in line
    assert f'delivered {len(_sent_texts(bot))} chunk(s)' in line
    assert str(path) not in line and 'look' not in line, 'no content, no paths on [TG-PUSH]'


def test_safe_send_unresolvable_item_keeps_its_link(media_root, caplog):
    """Plan §3/§12d: only SUCCESSFULLY-SENT spans are excised; a refused item's link stays
    in the text and nothing raises."""
    missing = media_root / 'gone.jpg'
    bot = _mock_bot()
    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _run(_safe_send(bot, chat_id=42, text=f'here {missing} ok'))
    assert _sent_texts(bot) == [f'here {missing} ok']
    assert bot.send_photo.call_count == 0
    assert any('not sent' in r.getMessage() for r in caplog.records), 'refusal must be auditable'


def test_safe_send_excises_multiple_spans_in_document_order(media_root):
    """Descending-offset excision must leave both earlier offsets valid (plan §12d)."""
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    (media_root / 'b.jpg').write_bytes(b'\x89PNG b')
    bot = _mock_bot()
    text = f'first {media_root / 'a.jpg'} middle {media_root / 'b.jpg'} last'
    _run(_safe_send(bot, chat_id=42, text=text))
    assert _sent_texts(bot) == ['first  middle  last']
    assert bot.send_photo.call_count == 2


def test_safe_send_dedupes_before_uploading(media_root):
    path = media_root / 'x.jpg'
    path.write_bytes(b'\x89PNG x')
    bot = _mock_bot()
    _run(_safe_send(bot, chat_id=42, text=f'{path} again {path}'))
    assert bot.send_photo.call_count == 1
    assert _sent_texts(bot) == [f' again {path}'], 'the dropped duplicate keeps its link'


def test_send_one_media_uses_send_audio_not_send_voice():
    """Plan O-5: sendVoice needs audio/ogg <= 1MB; our producers emit mp3/wav/flac."""
    bot = _mock_bot()
    _run(_send_one_media(bot, 7, 'audio', b'ID3fake', None, fingerprint='deadbeef'))
    assert bot.send_audio.call_count == 1 and bot.send_photo.call_count == 0
    assert bot.send_audio.call_args.kwargs['chat_id'] == 7


def test_send_one_media_truncates_caption():
    """Plan E25: an over-long alt would BadRequest and lose a sendable file."""
    bot = _mock_bot()
    long_caption = 'x' * (MessageLimit.CAPTION_LENGTH + 200)
    _run(_send_one_media(bot, 7, 'image', b'\x89PNG', long_caption, fingerprint='ff'))
    caption = bot.send_photo.call_args.kwargs['caption']
    assert caption == 'x' * MessageLimit.CAPTION_LENGTH


def test_send_one_media_video_gate():
    bot = _mock_bot()
    _run(_send_one_media(bot, 7, 'video', b'fake', None, fingerprint='ff'))
    assert bot.send_video.call_count == 1
    # An unknown kind RAISES (it is unreachable by construction): a silent None would
    # let _send_media_aware count an unsent item as sent and skip its E10 fallback.
    bot = _mock_bot()
    with pytest.raises(ValueError):
        _run(_send_one_media(bot, 7, 'document', b'fake', None, fingerprint='ff'))
    assert bot.send_video.call_count == 0 and bot.send_photo.call_count == 0


def test_safe_send_bad_request_on_media_does_not_break_the_rest(media_root, caplog):
    """Plan E10: Telegram itself rejects a payload (bad geometry/content type) -> the
    text and the OTHER items must still go through, no exception may escape _safe_send,
    and the rejected item falls back to its ORIGINAL text link (logged as fallback=1)."""
    from telegram.error import BadRequest
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    (media_root / 'b.jpg').write_bytes(b'\x89PNG b')
    bot = _mock_bot()
    bot.send_photo.side_effect = [BadRequest('wrong image'), None]
    with caplog.at_level(logging.INFO, logger='agent_cascade_logger'):
        _run(_safe_send(bot, chat_id=42, text=f'{media_root / 'a.jpg'} and {media_root / 'b.jpg'}'))
    assert bot.send_photo.call_count == 2, 'a failed item must not abort the remaining ones'
    assert _sent_texts(bot) == [' and ', str(media_root / 'a.jpg')], (
        'caption first, then the failed item\'s own span — the user keeps the link '
        'they would have got before this feature')
    line = _push_lines(caplog)[-1]
    assert 'media=1' in line, 'one media delivered, one fell back'
    assert 'fallback=1' in line, 'the fallback must be visible in the line it exists to diagnose'


def test_safe_send_network_error_on_media_does_not_retry_as_text(media_root, monkeypatch):
    """The E10 fallback is for payloads Telegram REJECTED. On a transient NetworkError
    the text send would fail identically and only double the bounded backoff, so no
    fallback message is sent (worst case per _safe_send stays ~one backoff chain)."""
    from telegram.error import NetworkError
    monkeypatch.setattr(bot_mod, 'TG_SEND_RETRY_BACKOFF_BASE_SEC', 0)
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    bot = _mock_bot()
    bot.send_photo.side_effect = NetworkError('connection dropped')
    _run(_safe_send(bot, chat_id=42, text=f'see {media_root / 'a.jpg'} now'))
    assert bot.send_photo.call_count == bot_mod._SEND_ONE_MAX_ATTEMPTS, 'media retries like text'
    assert _sent_texts(bot) == ['see  now'], 'caption only — no fallback re-send'


def test_safe_send_retry_after_on_media_retries_and_does_not_fall_back(media_root, caplog):
    """429 on a media send follows the SAME policy as text: sleep its retry_after and loop
    until it heals (_attempt_send's 429 branch is unbounded by design).

    A healed 429 never reaches the E10 guard, so what this pins is that the retry stays
    invisible to the delivery counters and logs: one photo, no failure line, caption only.
    """
    from telegram.error import RetryAfter
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    bot = _mock_bot()
    bot.send_photo.side_effect = [RetryAfter(0), None]
    with caplog.at_level(logging.WARNING, logger='agent_cascade_logger'):
        _run(_safe_send(bot, chat_id=42, text=f'see {media_root / 'a.jpg'} now'))
    assert bot.send_photo.call_count == 2, 'retried once, then succeeded'
    lines = [r.getMessage() for r in caplog.records]
    assert not any('media send failed' in line for line in lines), 'a healed 429 is not a failure'
    assert not any('[TG-PUSH] media item not sent' in line for line in lines)
    assert _sent_texts(bot) == ['see  now'], 'caption only — no fallback re-send'


def test_safe_send_permanent_telegram_error_on_media_does_not_retry_as_text(media_root):
    """The corrected E10 guard (finding 1): Forbidden/Conflict/... are TelegramError but
    NOT NetworkError on PTB 22.8, so the old "not a NetworkError" test treated a blocked
    bot as sendable-again and paid for a doomed text re-send plus a second WARNING.
    A text message would fail the same way, so nothing but the caption goes out."""
    from telegram.error import Forbidden
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    bot = _mock_bot()
    bot.send_photo.side_effect = Forbidden('bot was kicked from the group chat')
    _run(_safe_send(bot, chat_id=42, text=f'see {media_root / 'a.jpg'} now'))
    assert bot.send_photo.call_count == 1, 'a permanent Telegram error is not retried'
    assert _sent_texts(bot) == ['see  now'], 'and no text fallback is attempted'


def test_safe_send_fallback_failure_does_not_abort_the_delivery(media_root):
    """The fallback's own send can fail too; that must not lose the OTHER items nor
    raise out of _safe_send (the branch at bot.py's inner except is real code)."""
    from telegram.error import BadRequest
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    (media_root / 'b.jpg').write_bytes(b'\x89PNG b')
    bot = _mock_bot()
    span_a = str(media_root / 'a.jpg')
    bot.send_photo.side_effect = [BadRequest('wrong image'), None]
    bot.send_message.side_effect = lambda **kw: (
        (_ for _ in ()).throw(RuntimeError('text send down')) if kw['text'] == span_a else None)
    _run(_safe_send(bot, chat_id=42, text=f'{span_a} and {media_root / 'b.jpg'}'))
    assert bot.send_photo.call_count == 2, 'the second item still uploads'
    texts = _sent_texts(bot)   # call_args_list records attempts, including the raising one
    assert texts[0] == ' and ', 'the caption went out first'
    assert texts[1] == span_a, 'the fallback was attempted with the original span'
    assert len(texts) == 2, 'the failed fallback is swallowed, not retried or re-raised'


def test_safe_send_media_only_message_sends_exactly_one_message(media_root):
    """§8 E13: the caption of a media-only message is empty, so chunk_text yields nothing
    and exactly one Telegram message goes out (no empty send_message, no stray chunk)."""
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    bot = _mock_bot()
    _run(_safe_send(bot, chat_id=42, text=str(media_root / 'a.jpg')))
    assert bot.send_message.call_count == 0, 'an empty caption must not be sent'
    assert bot.send_photo.call_count == 1


def test_safe_send_delivers_the_caption_before_the_media(media_root):
    """§7.3: text first, then media — asserted on a single shared call list, so a
    reordering of the two loops cannot slip through."""
    calls = []
    bot = _mock_bot()
    for name in ('send_message', 'send_photo'):
        def _record(*_a, _label=name, **_kw):   # _label binds the name at definition time
            calls.append(_label)
            return None
        setattr(bot, name, AsyncMock(side_effect=_record))
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    _run(_safe_send(bot, chat_id=42, text=f'see {media_root / 'a.jpg'} now'))
    assert calls == ['send_message', 'send_photo']


def test_safe_send_text_chunk_failure_aborts_the_media_loop(media_root):
    """The item-isolation guarantee is scoped to ITEMS (finding 5): a failing text CHUNK
    aborts the media loop — the message itself is down, so no media is attempted and
    nothing raises out of _safe_send. Pinning this keeps the comment honest."""
    from telegram.error import BadRequest
    (media_root / 'a.jpg').write_bytes(b'\x89PNG a')
    bot = _mock_bot()
    bot.send_message.side_effect = BadRequest('chat is offline')
    _run(_safe_send(bot, chat_id=42, text=f'see {media_root / 'a.jpg'} now'))
    # Execution witness: the ATTEMPTED chunk is the EXCISED text, so the item really
    # resolved — otherwise the full text would be sent and the test would pass vacuously.
    assert _sent_texts(bot) == ['see  now'], 'resolution happened before the failing chunk'
    assert bot.send_message.call_count == 1, 'the failing chunk is not retried as text'
    assert bot.send_photo.call_count == 0, 'so the media loop never runs'


def test_send_one_media_retries_network_errors_like_text(monkeypatch):
    """Option B's promise: the media path shares the text retry policy, so a dropped
    connection heals instead of dropping the media."""
    from telegram.error import NetworkError
    monkeypatch.setattr(bot_mod, 'TG_SEND_RETRY_BACKOFF_BASE_SEC', 0)  # keep the test fast
    bot = _mock_bot()
    attempts = {'n': 0}

    def _flaky(**_kwargs):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise NetworkError('connection dropped')
    bot.send_photo.side_effect = _flaky
    _run(_send_one_media(bot, 7, 'image', b'\x89PNG a', None, fingerprint='ff'))
    assert attempts['n'] == 3 == bot.send_photo.call_count, 'retried, then succeeded'


def test_safe_send_never_raises(monkeypatch):
    """Every call site keeps its fire-and-forget contract: nothing escapes."""
    bot = _mock_bot()
    bot.send_message.side_effect = RuntimeError('network is down')
    _run(_safe_send(bot, chat_id=42, text='plain text'))
    assert bot.send_message.call_count == 1


def test_attempt_send_bad_request_does_not_retry():
    """The extracted helper keeps the no-retry-on-BadRequest ordering (PTB puts
    BadRequest under NetworkError, so branch order is load-bearing)."""
    from telegram.error import BadRequest
    calls = {'n': 0}

    async def _boom():
        calls['n'] += 1
        raise BadRequest('too long')

    with pytest.raises(BadRequest):
        _run(bot_mod._attempt_send(_boom, 'send', 'ff'))
    assert calls['n'] == 1


def test_attempt_send_bounds_network_errors(monkeypatch):
    """_SEND_ONE_MAX_ATTEMPTS total attempts, then re-raise (plan §9.2 contract).

    The backoff is zeroed so the bounded loop runs instantly: the ATTEMPT COUNT is
    the contract, not the sleep durations.
    """
    from telegram.error import NetworkError
    calls = {'n': 0}

    async def _always_fail():
        calls['n'] += 1
        raise NetworkError('dead connection')

    monkeypatch.setattr(bot_mod, 'TG_SEND_RETRY_BACKOFF_BASE_SEC', 0.0)
    monkeypatch.setattr(bot_mod, 'TG_SEND_RETRY_BACKOFF_CAP_SEC', 0.0)
    with pytest.raises(NetworkError):
        _run(bot_mod._attempt_send(_always_fail, 'send', 'ff'))
    assert calls['n'] == bot_mod._SEND_ONE_MAX_ATTEMPTS


def test_send_one_signature_unchanged(monkeypatch):
    """Option-B extraction must not change _send_one's call shape (E1/E22)."""
    seen = {}

    async def _record(**kwargs):
        seen.update(kwargs)

    bot = MagicMock()
    bot.send_message.side_effect = lambda **kw: _record(**kw)
    _run(_send_one(bot, chat_id=9, text='hello'))
    assert seen == {'chat_id': 9, 'text': 'hello'}


# --------------------------------------------------------------------------- #
# §10.3  resolution against real files (resolve_media_source)
# --------------------------------------------------------------------------- #

def _item(value, kind='image', source='local_path'):
    return mo.MediaItem(kind=kind, source=source, value=value, alt='', span=(0, len(value)))


def test_resolve_local_allowed_file(tmp_path):
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\x89PNG data')
    outcome, payload = mo.resolve_media_source(_item(str(path)),
                                              allowed_roots=[str(tmp_path)], **_CAPS)
    assert outcome == 'local' and payload == str(Path(path).resolve())


def test_resolve_outside_allowed_roots(tmp_path):
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\x89PNG data')
    outcome, reason = mo.resolve_media_source(_item(str(path)), allowed_roots=[], **_CAPS)
    assert (outcome, reason) == (None, 'not_allowed')


def test_resolve_missing_file(tmp_path):
    outcome, reason = mo.resolve_media_source(_item(str(tmp_path / 'gone.jpg')),
                                             allowed_roots=[str(tmp_path)], **_CAPS)
    assert (outcome, reason) == (None, 'missing')


def test_resolve_zero_byte_file(tmp_path):
    path = tmp_path / 'empty.jpg'
    path.write_bytes(b'')
    outcome, reason = mo.resolve_media_source(_item(str(path)),
                                              allowed_roots=[str(tmp_path)], **_CAPS)
    assert (outcome, reason) == (None, 'unreadable')


def test_resolve_unreadable_file(tmp_path, monkeypatch):
    """A file that exists with content but fails the R_OK probe (permissions/ACL) must
    be refused as 'unreadable', not raised out of the resolve path."""
    path = tmp_path / 'photo.jpg'
    path.write_bytes(b'\x89PNG data')
    monkeypatch.setattr(mo.os, 'access', lambda p, mode: False)
    outcome, reason = mo.resolve_media_source(_item(str(path)),
                                              allowed_roots=[str(tmp_path)], **_CAPS)
    assert (outcome, reason) == (None, 'unreadable')


def test_resolve_size_caps_are_per_kind(tmp_path):
    """10 MB for photos, 50 MB for other (plan O-6/O-9): the cap follows item.kind."""
    path = tmp_path / 'clip.mp3'
    path.write_bytes(b'x' * 2048)
    args = dict(allowed_roots=[str(tmp_path)], max_photo_bytes=1, max_other_bytes=4096)
    outcome, _ = mo.resolve_media_source(_item(str(path), kind='audio'), **args)
    assert outcome == 'local', 'audio under its own cap must be sent'
    outcome, reason = mo.resolve_media_source(_item(str(path), kind='image'), **args)
    assert (outcome, reason) == (None, 'oversized'), 'the same file must fail the photo cap'


def test_resolve_data_uri_decodes_and_caps_decoded_bytes():
    payload = base64.b64encode(b'\x89PNG fake').decode()
    outcome, data = mo.resolve_media_source(_item(f'data:image/png;base64,{payload}',
                                                  source='data_uri'), **_CAPS)
    assert outcome == 'bytes' and data == b'\x89PNG fake'
    # A data URI with no ',' is not a URI at all: refused as 'bad_uri' (plan §6).
    assert mo.resolve_media_source(_item('data:image/png', source='data_uri'),
                                   **_CAPS) == (None, 'bad_uri')
    # E20: a payload that decodes to zero bytes is a guaranteed BadRequest on the wire.
    assert mo.resolve_media_source(_item('data:image/png;base64,###', source='data_uri'),
                                   **_CAPS) == (None, 'unreadable')
    # The cap applies to DECODED bytes (base64 inflates by 4/3, plan §6).
    assert mo.resolve_media_source(_item(f'data:image/png;base64,{payload}', source='data_uri'),
                                   max_photo_bytes=1, max_other_bytes=1) == (None, 'oversized')


def test_resolve_http_pass_through():
    outcome, payload = mo.resolve_media_source(_item('https://x.com/img.jpg',
                                                     source='http_url'), **_CAPS)
    assert (outcome, payload) == ('http_url', 'https://x.com/img.jpg')


# --------------------------------------------------------------------------- #
# §10.4  integration through notify_user (real supervisor + live loop thread)
# --------------------------------------------------------------------------- #


def _notify_user_media_roundtrip(tmp_path, monkeypatch, *, last_chat_id, fallback_id=None,
                                live_roots=()):
    """Drive notify_user end-to-end against a stubbed bot on a real event loop.

    Returns the recorded send calls. ``live_roots`` exercises the supervisor's N-1
    wiring (live extra work folders -> allowed_roots) WITHOUT touching
    TG_MEDIA_ALLOWED_ROOTS, so the delivery proves the plumbing, not the setting.
    """
    import queue
    from types import SimpleNamespace
    from tests.test_telegram_bridge_supervisor import (make_supervisor, _start_live_loop_thread,
                                                       _wait_until)

    (tmp_path / 'a.jpg').write_bytes(b'\x89PNG a')
    monkeypatch.delenv('AGENT_CASCADE_TG_MEDIA_ALLOWED_ROOTS', raising=False)
    monkeypatch.setattr(settings, 'TG_MEDIA_ALLOWED_ROOTS', '')

    thread, stop_evt, loop = _start_live_loop_thread()
    try:
        fake_app = MagicMock()
        fake_app.bot_data = {'last_chat_id': last_chat_id} if last_chat_id else {}
        sent = {'photo': [], 'message': []}
        fake_app.bot.send_photo = AsyncMock(
            side_effect=lambda **kw: sent['photo'].append(kw) or None)
        fake_app.bot.send_message = AsyncMock(
            side_effect=lambda **kw: sent['message'].append(kw) or None)

        sup = make_supervisor(tmp_path)
        pool = SimpleNamespace(
            operation_manager=SimpleNamespace(
                extra_work_folders_ro=list(live_roots), extra_work_folders_rw=[]),
            _ws_send_queue=queue.Queue())
        sup._agent_pool = pool
        if fallback_id is not None:
            sup._resolve_fallback_chat_id = lambda: fallback_id
        with sup._lock:
            sup._app = fake_app
            sup._loop = loop

        text = f'see ![image]({tmp_path / 'a.jpg'}) here'
        assert sup.notify_user(text) is True, 'notify_user contract: True once scheduled'
        assert _wait_until(lambda: sent['photo']), 'media must reach the bot'

        assert len(sent['photo']) == 1, f'expected one photo, got {sent['photo']}'
        assert sent['photo'][0]['chat_id'] == (last_chat_id or fallback_id)
        assert [c['text'] for c in sent['message']] == ['see  here'], (
            'the delivered caption has the media span excised')
        assert pool._ws_send_queue.empty(), 'the browser/WS channel must stay untouched'
    finally:
        stop_evt.set()
        thread.join(timeout=3.0)


def test_notify_user_delivers_media_through_the_live_extra_roots(tmp_path, monkeypatch):
    """§10.4 + supervisor N-1: media under a session's LIVE extra work folder is delivered
    with TG_MEDIA_ALLOWED_ROOTS empty — only the live-roots wiring makes it sendable."""
    _notify_user_media_roundtrip(tmp_path, monkeypatch, last_chat_id=42, live_roots=[str(tmp_path)])


def test_notify_user_delivers_media_on_the_no_last_chat_id_fallback(tmp_path, monkeypatch):
    """§8 E11: with no last_chat_id, the allowlisted fallback id still gets the media."""
    _notify_user_media_roundtrip(tmp_path, monkeypatch, last_chat_id=0, fallback_id=7,
                                 live_roots=[str(tmp_path)])


def test_config_mirrors_the_settings_defaults():
    """§12 item 4: the 6 knobs exist on BridgeConfig with the shipped defaults."""
    cfg = BridgeConfig()
    assert cfg.outbound_media is settings.TG_OUTBOUND_MEDIA is True
    assert cfg.media_detect_bare_paths is settings.TG_MEDIA_DETECT_BARE_PATHS is True
    assert cfg.media_send_video is settings.TG_MEDIA_SEND_VIDEO is False
    assert cfg.media_max_photo_bytes == 10 * 1024 * 1024 == settings.TG_MEDIA_MAX_PHOTO_BYTES
    assert cfg.media_max_other_bytes == 50 * 1024 * 1024 == settings.TG_MEDIA_MAX_OTHER_BYTES
    assert cfg.media_allowed_roots == settings.TG_MEDIA_ALLOWED_ROOTS == ''


def test_load_config_resolves_the_media_knobs_from_the_environment(monkeypatch):
    """§11 env naming. The defaults-only check above is near-tautological (BridgeConfig
    defaults ARE the settings constants); this one exercises the parsing that can actually
    break: _parse_bool defaults and the int() coercion."""
    for name in ('AGENT_CASCADE_TG_OUTBOUND_MEDIA', 'AGENT_CASCADE_TG_MEDIA_DETECT_BARE_PATHS',
                 'AGENT_CASCADE_TG_MEDIA_SEND_VIDEO', 'AGENT_CASCADE_TG_MEDIA_ALLOWED_ROOTS',
                 'AGENT_CASCADE_TG_MEDIA_MAX_PHOTO_BYTES', 'AGENT_CASCADE_TG_MEDIA_MAX_OTHER_BYTES'):
        monkeypatch.delenv(name, raising=False)
    roots = os.pathsep.join(['N:/a', 'N:/b'])   # bot._media_allowed_roots splits on os.pathsep
    monkeypatch.setenv('AGENT_CASCADE_TG_OUTBOUND_MEDIA', 'off')
    monkeypatch.setenv('AGENT_CASCADE_TG_MEDIA_DETECT_BARE_PATHS', 'yes')
    monkeypatch.setenv('AGENT_CASCADE_TG_MEDIA_SEND_VIDEO', 'true')
    monkeypatch.setenv('AGENT_CASCADE_TG_MEDIA_ALLOWED_ROOTS', roots)
    monkeypatch.setenv('AGENT_CASCADE_TG_MEDIA_MAX_PHOTO_BYTES', '123')
    cfg = load_config()
    assert cfg.outbound_media is False, "'off' must disable the feature"
    assert cfg.media_detect_bare_paths is True and cfg.media_send_video is True
    assert cfg.media_max_photo_bytes == 123
    assert cfg.media_max_other_bytes == settings.TG_MEDIA_MAX_OTHER_BYTES, 'unset keeps the default'
    assert cfg.media_allowed_roots == roots
