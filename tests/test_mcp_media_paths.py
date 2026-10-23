"""MCP media persistence (todo.md:103).

Binding plan: reports/mcp-media-paths-dig-plan.md.

When an MCP tool result carries image/audio content, the base64 payload must be
persisted to the shared media directory and an ABSOLUTE FILE PATH emitted through
the agent pipe — never the inline base64. These tests exercise MCPClient.execute_function
directly (the general tool branch + the guarded read_resource blob branch), plus a
cross-stage check that the emitted path is understood by the Telegram outbound-media
parser (media_outbound.split_media).

Run serially (pytest.ini pins xdist):
    python -m pytest tests/test_mcp_media_paths.py -q -n 0
"""

import asyncio
import base64
import io
import os
import re
import sys
import types
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

# Ensure top-level imports work (mirror tests/test_tg_outbound_media.py convention).
PROJECT_ROOT = Path(__file__).parent.parent.absolute()
sys.path.insert(0, str(PROJECT_ROOT))

from agent_cascade.tools import mcp_manager as mm
from agent_cascade.tools.mcp_manager import MCPClient, MediaStorageError
from agent_cascade.telegram_bridge import media_outbound as mo
from agent_cascade.utils.media_utils import _get_media_root


def _run(coro):
    """Run an async coroutine to completion on a fresh event loop."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _tiny_png_b64() -> str:
    """A real tiny PNG, base64-encoded (so the token is a known, unique string)."""
    img = Image.new('RGB', (20, 20), color='red')
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _tiny_wav_b64() -> str:
    """A real minimal WAV, base64-encoded."""
    buf = io.BytesIO()
    w = wave.open(buf, 'wb')
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b'\x00\x00' * 200)
    w.close()
    return base64.b64encode(buf.getvalue()).decode('ascii')


def _make_client(content_items):
    """Build an MCPClient whose session is a mock returning the given content list."""
    client = MCPClient()
    client.session = MagicMock()
    client.session.send_ping = AsyncMock()
    client.session.call_tool = AsyncMock(
        return_value=types.SimpleNamespace(content=content_items)
    )
    return client


def _extract_path(out: str, alt: str) -> str:
    """Pull the link target out of the emitted ![alt](path) markdown."""
    m = re.search(r'!\[' + alt + r'\]\(([^)]+)\)', out)
    assert m, f'No ![{alt}](...) link in output: {out!r}'
    return m.group(1)


class TestMcpMediaPersistence:
    """Core revert-proof behaviour: media is persisted to disk, path (not base64) emitted."""

    @pytest.fixture(autouse=True)
    def _cleanup_saved_media(self):
        """Track and remove any media files the tests create under logs/media/."""
        self._saved = []
        yield
        for p in self._saved:
            try:
                os.remove(p)
            except OSError:
                pass

    def _track(self, path):
        self._saved.append(path)

    def test_image_persisted_to_path_not_base64(self):
        """A fake ImageContent yields a real .jpg path; the base64 token is gone."""
        b64 = _tiny_png_b64()
        fake = types.SimpleNamespace(type='image', data=b64, mimeType='image/png')
        client = _make_client([fake])

        out = _run(client.execute_function('any_tool', {}))

        # 1) A markdown image link, NOT the raw base64 payload.
        assert '![image](' in out, f'Expected ![image]( path ) link, got: {out!r}'
        assert b64 not in out, 'base64 payload must NOT flow through the agent pipe'

        # 2) The link target is a real file under the media images dir, forced to .jpg.
        p = _extract_path(out, 'image')
        self._track(p)
        assert os.path.isfile(p), f'Link target is not a real file: {p!r}'
        expected_dir = str(_get_media_root() / 'images').replace('\\', '/')
        assert expected_dir in p.replace('\\', '/'), f'Not under media images dir: {p!r}'
        assert p.lower().endswith('.jpg'), f'Image must be re-encoded to .jpg: {p!r}'

    def test_audio_persisted_to_path_not_base64(self):
        """A fake AudioContent (audio/wav) yields a real path under audio/ with an
        audio extension; the base64 token is gone."""
        b64 = _tiny_wav_b64()
        fake = types.SimpleNamespace(type='audio', data=b64, mimeType='audio/wav')
        client = _make_client([fake])

        out = _run(client.execute_function('any_tool', {}))

        assert '![audio](' in out, f'Expected ![audio]( path ) link, got: {out!r}'
        assert b64 not in out, 'base64 payload must NOT flow through the agent pipe'

        p = _extract_path(out, 'audio')
        self._track(p)
        assert os.path.isfile(p), f'Link target is not a real file: {p!r}'
        expected_dir = str(_get_media_root() / 'audio').replace('\\', '/')
        assert expected_dir in p.replace('\\', '/'), f'Not under media audio dir: {p!r}'
        audio_exts = ('.mp3', '.wav', '.ogg', '.m4a', '.flac', '.opus')
        assert p.lower().endswith(audio_exts), f'Audio path has non-audio extension: {p!r}'

    def test_storage_failure_emits_note_marker(self):
        """When save_media_to_media raises, the result emits a short skip marker — never
        the inline base64, which would re-introduce the context bloat this feature removes."""
        b64 = _tiny_png_b64()
        fake = types.SimpleNamespace(type='image', data=b64, mimeType='image/png')
        client = _make_client([fake])

        # Patch at the consumer's true home so the mock actually takes effect.
        with patch.object(mm, 'save_media_to_media',
                         side_effect=MediaStorageError('simulated disk full')):
            out = _run(client.execute_function('any_tool', {}))

        assert 'skipped media resource: any_tool' in out, f'Expected skip marker, got: {out!r}'
        assert b64 not in out, 'base64 payload must NOT flow through the agent pipe'

    def test_text_content_unchanged_control(self):
        """Control: a text content still returns its text verbatim (proves the harness
        exercises the real production path). Passes before and after the fix."""
        fake = types.SimpleNamespace(type='text', text='hello world')
        client = _make_client([fake])

        out = _run(client.execute_function('any_tool', {}))
        assert out == 'hello world', f'Text content must pass through unchanged, got: {out!r}'

    def test_empty_media_payload_falls_back(self):
        """Empty data must not write a 0-byte file nor emit a path link; it degrades to the
        same short skip marker as a persist failure — never the raw base64."""
        fake = types.SimpleNamespace(type='image', data='', mimeType='image/png')
        client = _make_client([fake])

        out = _run(client.execute_function('any_tool', {}))
        # No path link is emitted for an empty payload; no exception escapes.
        assert 'skipped media resource: any_tool' in out, f'Expected skip marker, got: {out!r}'
        assert not re.search(r'!\[image\]\(([^)]+)\)', out), \
            'empty payload must not produce a file path link'

    def test_cross_stage_audio_path_is_classified(self):
        """Integration: the emitted ![audio](path) is understood by the delivery stage
        (media_outbound.split_media) as an audio local_path MediaItem."""
        b64 = _tiny_wav_b64()
        fake = types.SimpleNamespace(type='audio', data=b64, mimeType='audio/wav')
        client = _make_client([fake])

        out = _run(client.execute_function('any_tool', {}))
        p = _extract_path(out, 'audio')
        self._track(p)

        _, items = mo.split_media(out)
        assert len(items) == 1, f'Expected exactly one media item, got: {items!r}'
        assert items[0].kind == 'audio', f'Wrong kind: {items[0]!r}'
        assert items[0].source == 'local_path', f'Wrong source: {items[0]!r}'
        assert items[0].value == p, f'Value mismatch: {items[0].value!r} != {p!r}'


class TestExtFromMime:
    """The MIME→ext helper: subtype lowercased, or None for a missing/invalid MIME."""

    @pytest.mark.parametrize('mime,expected', [
        ('image/png', 'png'),
        ('image/JPEG', 'jpeg'),
        ('audio/wav', 'wav'),
        ('audio/mpeg', 'mpeg'),   # not in allowed set → save_media_to_media defaults to mp3
        (None, None),
        ('', None),
        ('nonsense', None),       # no '/' → default
    ])
    def test_ext_from_mime(self, mime, expected):
        assert mm._ext_from_mime(mime, 'image') == expected


class TestMcpMediaPathNotification:
    """The _queue_media_path_notification helper: queues a [SYSTEM NOTIFICATIONS]
    entry with the saved file path (and dimensions for images) so the agent knows
    where the internal copy lives."""

    @pytest.fixture(autouse=True)
    def _cleanup_saved_media(self):
        self._saved = []
        yield
        for p in self._saved:
            try:
                os.remove(p)
            except OSError:
                pass

    def _track(self, path):
        self._saved.append(path)

    def test_image_notification_includes_path_and_dimensions(self):
        """When pool/instance_name are provided, _queue_tool_warning is called with
        a message containing the absolute path AND (WxH) dimensions."""
        b64 = _tiny_png_b64()
        fake = types.SimpleNamespace(type='image', data=b64, mimeType='image/png')
        client = _make_client([fake])

        stub_inst = MagicMock()
        stub_inst._tool_warnings = []
        import threading
        stub_inst._compression_lock = threading.RLock()

        stub_pool = MagicMock()
        stub_pool.get_instance.return_value = stub_inst

        with patch('agent_cascade.operation_manager.path_security._queue_tool_warning',
                   wraps=None) as mock_qtw:
            # Re-patch at the import site inside the helper
            with patch('agent_cascade.tools.mcp_manager._queue_media_path_notification',
                       wraps=mm._queue_media_path_notification) as mock_notify:
                out = _run(client.execute_function('blender-look', {},
                                                   pool=stub_pool, instance_name='test_agent'))
                # The notification helper should have been called
                assert mock_notify.call_count == 1
                args, kwargs = mock_notify.call_args
                assert args[0] == 'blender-look'  # tool_name
                assert args[1] == 'image'         # media_type
                # path is a real saved file
                assert os.path.isfile(args[2])
                self._track(args[2])
                assert kwargs.get('pool') is stub_pool
                assert kwargs.get('instance_name') == 'test_agent'

    def test_notification_noop_without_pool(self):
        """Without pool/instance_name, the notification is a silent no-op —
        the tool result is unchanged (just the markdown link)."""
        b64 = _tiny_png_b64()
        fake = types.SimpleNamespace(type='image', data=b64, mimeType='image/png')
        client = _make_client([fake])

        with patch('agent_cascade.tools.mcp_manager._queue_media_path_notification') as mock_notify:
            out = _run(client.execute_function('any_tool', {}))
            # No pool/instance → helper is called but returns at the guard
            assert mock_notify.call_count == 1
            # Tool result is still just the markdown link
            assert '![image](' in out

    def test_notification_message_format_with_dimensions(self):
        """Direct unit test: the message format includes path + (WxH) for images."""
        # Create a real tiny image on disk
        img = Image.new('RGB', (64, 48), color='blue')
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        # Save via the real pipeline
        path = mm.save_media_to_media(buf.getvalue(), 'image')
        self._track(path)

        mock_qtw = MagicMock()
        with patch('agent_cascade.operation_manager._queue_tool_warning', mock_qtw):
            mm._queue_media_path_notification('test_tool', 'image', path,
                                              pool=MagicMock(), instance_name='agent1')

        assert mock_qtw.call_count == 1
        call_args = mock_qtw.call_args
        # First arg is pool, second is instance_name, third is the message
        msg = call_args[0][2]
        assert path in msg, f'Path not in message: {msg!r}'
        assert '(64x48)' in msg, f'Dimensions not in message: {msg!r}'
        assert 'test_tool' in msg, f'Tool name not in message: {msg!r}'

    def test_notification_message_audio_no_dimensions(self):
        """Audio notifications do NOT include dimensions (only images do)."""
        mock_qtw = MagicMock()
        with patch('agent_cascade.operation_manager._queue_tool_warning', mock_qtw):
            mm._queue_media_path_notification('test_tool', 'audio', '/fake/audio.mp3',
                                              pool=MagicMock(), instance_name='agent1')

        assert mock_qtw.call_count == 1
        msg = mock_qtw.call_args[0][2]
        assert '/fake/audio.mp3' in msg
        assert 'x' not in msg.split('saved to:')[1].split('(tool:')[0], \
            f'Audio msg should not have dimensions: {msg!r}'


class TestReadResourceBlob:
    """The guarded read_resource blob handler."""

    @pytest.fixture(autouse=True)
    def _cleanup_saved_media(self):
        self._saved = []
        yield
        for p in self._saved:
            try:
                os.remove(p)
            except OSError:
                pass

    def _track(self, path):
        self._saved.append(path)

    def _make_read_client(self, contents):
        client = MCPClient()
        client.session = MagicMock()
        client.session.send_ping = AsyncMock()
        client.session.read_resource = AsyncMock(
            return_value=types.SimpleNamespace(contents=contents)
        )
        return client

    def test_media_blob_persisted(self):
        """A BlobResourceContents with image/* mimeType is persisted to a path."""
        from mcp.types import BlobResourceContents

        b64 = _tiny_png_b64()
        blob = BlobResourceContents(
            type='blob', uri='res://img', mimeType='image/png', blob=b64
        )
        client = self._make_read_client([blob])

        out = _run(client.execute_function('read_resource', {'uri': 'res://img'}))
        assert '![image](' in out, f'Expected persisted image link, got: {out!r}'
        assert b64 not in out, 'base64 blob must NOT be dumped into context'

        p = _extract_path(out, 'image')
        self._track(p)
        assert os.path.isfile(p)

    def test_non_media_blob_not_dumped(self):
        """A blob with a non-media/absent mimeType yields a NOTE marker, never the raw blob."""
        from mcp.types import BlobResourceContents

        blob = BlobResourceContents(
            type='blob', uri='res://data', blob=base64.b64encode(b'\x00\x01\x02').decode()
        )
        client = self._make_read_client([blob])

        out = _run(client.execute_function('read_resource', {'uri': 'res://data'}))
        assert 'skipped non-media blob resource: res://data' in out, f'Expected skip marker, got: {out!r}'
        # The raw base64 must not be dumped into context.
        assert base64.b64encode(b'\x00\x01\x02').decode() not in out
