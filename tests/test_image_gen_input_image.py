"""Tests for the optional ``input_image`` parameter of the image_gen tool.

Covers the ponytail-reviewed design:
  * ``_upload_image`` helper — multipart upload, response parsing (name/subfolder),
    and failure modes (non-200, missing name, connection error).
  * Wiring into ``_handle_text_prompt`` — exactly-one-LoadImage contract, upload
    AFTER ``_inject_params`` / BEFORE the VRAM block, clean errors, no orphan upload.
  * SVG guard — ``input_image`` rejected for local SVG rendering.
  * Backward compatibility — absent ``input_image`` yields a byte-identical
    POST /prompt payload (the LoadImage node is never touched).

All external I/O is mocked via ``httpx.MockTransport``. No network, no real
ComfyUI, no cairosvg.
"""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agent_cascade.tools.image_gen import (
    ImageGen,
    _invalidate_image_gen_config,
    _upload_image,
)

SERVER = 'http://comfyui:8188'
PROMPT_ID = 'test-prompt-id-001'
FAKE_IMAGE_BYTES = b'\x89PNG\r\n\x1a\n' + b'fake image data'


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bust_config_cache():
    """Ensure the image_gen config cache is clean before and after every test."""
    _invalidate_image_gen_config()
    yield
    _invalidate_image_gen_config()


def _wf_with_loadimage(load_count=1):
    """A zimg-style workflow with a positive CLIPTextEncode and ``load_count`` LoadImage nodes.

    Node id ``'9'`` (and ``'10'``, ...) are the LoadImage nodes with scalar string
    ``image`` inputs — the shape the feature is allowed to wire.
    """
    wf = {
        '6': {'class_type': 'CLIPTextEncode', 'inputs': {'text': 'positive'}},
        '7': {'class_type': 'CLIPTextEncode', 'inputs': {'text': ''}},
        '8': {'class_type': 'KSampler', 'inputs': {'width': 1024, 'height': 1024, 'seed': 1}},
    }
    for i in range(load_count):
        wf[str(9 + i)] = {'class_type': 'LoadImage', 'inputs': {'image': f'ref_{i}.png'}}
    return wf


def _write_wf(tmp_path, load_count=1) -> str:
    p = tmp_path / 'wf.json'
    p.write_text(json.dumps(_wf_with_loadimage(load_count)), encoding='utf-8')
    return str(p)


def _cfg(wf_path: str) -> dict:
    return {'url': SERVER, 'timeout': 60, 'default_workflow': wf_path}


def _error_text(result) -> str | None:
    """Return the error string if ``result`` is the single-item ERROR shape, else None.

    On success the tool returns ``[ContentItem(image=...), ContentItem(text=...)]`` so
    ``result[0].text`` is None — this helper distinguishes the two shapes cleanly.
    """
    if len(result) == 1 and result[0].text and result[0].text.startswith('ERROR'):
        return result[0].text
    return None


def _make_upload_transport(payload, captured=None, status_code=200, raise_exc=None) -> httpx.MockTransport:
    """MockTransport that serves POST /upload/image.

    ``captured`` (a list) collects the raw upload requests for inspection.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path == '/upload/image':
            if captured is not None:
                captured.append(request)
            if raise_exc is not None:
                raise raise_exc
            return httpx.Response(status_code, json=payload)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def _make_full_transport(prompts, upload_payload=None) -> httpx.MockTransport:
    """MockTransport that serves /upload/image, /prompt, /history and /view.

    Every POST /prompt body is appended to ``prompts`` (for byte-identity checks).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'POST' and request.url.path == '/upload/image':
            return httpx.Response(
                200,
                json=upload_payload or {'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            )
        if request.method == 'POST' and request.url.path == '/prompt':
            prompts.append(json.loads(request.content))
            return httpx.Response(200, json={'prompt_id': PROMPT_ID})
        if request.method == 'GET' and request.url.path.startswith('/history/'):
            return httpx.Response(200, json={
                PROMPT_ID: {
                    'status': {'status_str': 'success', 'completed': True},
                    'outputs': {
                        '3': {'images': [{'filename': 'out.png', 'subfolder': '', 'type': 'output'}]},
                    },
                }
            })
        if request.method == 'GET' and request.url.path == '/view':
            return httpx.Response(200, content=FAKE_IMAGE_BYTES)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 1. _upload_image helper
# ---------------------------------------------------------------------------


class TestUploadImage:

    def test_returns_server_name(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={'name': 'cat.png', 'subfolder': '', 'type': 'input'})
        )
        ref = _upload_image(SERVER, img, client=httpx.Client(transport=transport))
        assert ref == 'cat.png'

    def test_subfolder_prefix(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={'name': 'cat.png', 'subfolder': 'refs', 'type': 'input'})
        )
        ref = _upload_image(SERVER, img, client=httpx.Client(transport=transport))
        assert ref == 'refs/cat.png'

    def test_non200_raises(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        transport = httpx.MockTransport(lambda req: httpx.Response(500, text='boom'))
        with pytest.raises(RuntimeError, match='upload failed'):
            _upload_image(SERVER, img, client=httpx.Client(transport=transport))

    def test_missing_name_raises(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={'subfolder': '', 'type': 'input'})
        )
        with pytest.raises(RuntimeError, match="missing 'name'"):
            _upload_image(SERVER, img, client=httpx.Client(transport=transport))

    def test_connection_error_raises(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')

        def handler(req):
            raise httpx.ConnectError('connection refused')

        with pytest.raises(RuntimeError, match='upload failed at'):
            _upload_image(SERVER, img, client=httpx.Client(transport=httpx.MockTransport(handler)))

    def test_sends_multipart_file_not_raw_string(self, tmp_path):
        """The upload must be a real multipart file part, not a raw string form field."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'MYIMAGEBYTES')
        captured = []

        def handler(req):
            captured.append(req)
            return httpx.Response(200, json={'name': 'cat.png', 'subfolder': '', 'type': 'input'})

        _upload_image(SERVER, img, client=httpx.Client(transport=httpx.MockTransport(handler)))
        assert len(captured) == 1
        req = captured[0]
        assert req.headers['content-type'].startswith('multipart/form-data')
        assert 'application/x-www-form-urlencoded' not in req.headers['content-type']
        body = req.content
        assert b'name="image"' in body      # multipart field name
        assert b'cat.png' in body           # filename part
        assert b'MYIMAGEBYTES' in body      # file content

    def test_read_error_raises(self, tmp_path):
        """A path that can't be opened for reading (a directory) raises IsADirectoryError
        (an OSError) → clean RuntimeError, not an unhandled exception."""
        d = tmp_path / 'adir'
        d.mkdir()
        transport = httpx.MockTransport(
            lambda req: httpx.Response(200, json={'name': 'cat.png', 'subfolder': '', 'type': 'input'})
        )
        with pytest.raises(RuntimeError, match='failed to read'):
            _upload_image(SERVER, d, client=httpx.Client(transport=transport))


# ---------------------------------------------------------------------------
# 2. Wiring into _handle_text_prompt (integration via tool.call)
# ---------------------------------------------------------------------------


class TestInputImageWiring:

    def _call_with_input(self, tmp_path, input_image, load_count=1,
                         upload_payload=None, upload_status=200, upload_exc=None,
                         resolve_path=None, resolve_exc=None):
        """Drive the full text-prompt path with a mocked upload + mocked _comfyui_generate.

        Returns ``(result, mock_gen, uploaded_requests)``.
        """
        wf_path = _write_wf(tmp_path, load_count)
        captured = []
        up_transport = _make_upload_transport(
            upload_payload, captured, status_code=upload_status, raise_exc=upload_exc
        )
        mock_gen = MagicMock(return_value=(FAKE_IMAGE_BYTES, {'seed': 42}))
        resolve_kwargs = {}
        if resolve_path is not None:
            resolve_kwargs['return_value'] = resolve_path
        if resolve_exc is not None:
            resolve_kwargs['side_effect'] = resolve_exc

        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
             patch('agent_cascade.tools.image_gen.httpx.Client',
                   return_value=httpx.Client(transport=up_transport)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate', mock_gen), \
             patch('agent_cascade.tools.image_gen.save_image_to_media',
                   return_value='/tmp/media/out.png'), \
             patch('agent_cascade.utils.tool_path_resolver.resolve_tool_path', **resolve_kwargs):
            tool = ImageGen()
            result = tool.call({'prompt': 'a cat', 'seed': 42, 'input_image': input_image})
        return result, mock_gen, captured

    def test_success_wires_loadimage_to_server_name(self, tmp_path):
        """Upload succeeds → the single LoadImage node is wired to the server name."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        result, mock_gen, uploaded = self._call_with_input(
            tmp_path, str(img), upload_payload={'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            resolve_path=img,
        )
        assert _error_text(result) is None
        assert result[0].image == '/tmp/media/out.png'
        assert len(uploaded) == 1
        wf = mock_gen.call_args.args[1]
        assert wf['9']['inputs']['image'] == 'cat.png'

    def test_subfolder_handling(self, tmp_path):
        """Non-empty subfolder → graph value is 'subfolder/name'."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        result, mock_gen, _ = self._call_with_input(
            tmp_path, str(img),
            upload_payload={'name': 'cat.png', 'subfolder': 'refs', 'type': 'input'},
            resolve_path=img,
        )
        assert _error_text(result) is None
        assert mock_gen.call_args.args[1]['9']['inputs']['image'] == 'refs/cat.png'

    def test_duplicate_rename_uses_server_name(self, tmp_path):
        """ComfyUI renames a duplicate → the tool uses the returned name, not the local filename."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        result, mock_gen, _ = self._call_with_input(
            tmp_path, str(img),
            upload_payload={'name': 'cat (1).png', 'subfolder': '', 'type': 'input'},
            resolve_path=img,
        )
        assert _error_text(result) is None
        wired = mock_gen.call_args.args[1]['9']['inputs']['image']
        assert wired == 'cat (1).png'
        assert wired != 'cat.png'  # not the local filename

    def test_multiple_loadimage_nodes_errors_no_upload(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        result, mock_gen, uploaded = self._call_with_input(
            tmp_path, str(img), load_count=2,
            upload_payload={'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            resolve_path=img,
        )
        assert 'ERROR' in result[0].text
        assert 'exactly one' in result[0].text
        assert '2' in result[0].text
        assert len(uploaded) == 0          # no orphan upload
        mock_gen.assert_not_called()

    def test_no_loadimage_node_errors_no_upload(self, tmp_path):
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        result, mock_gen, uploaded = self._call_with_input(
            tmp_path, str(img), load_count=0,
            upload_payload={'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            resolve_path=img,
        )
        assert 'ERROR' in result[0].text
        assert 'LoadImage' in result[0].text
        assert len(uploaded) == 0
        mock_gen.assert_not_called()

    def test_missing_file_errors_before_upload(self, tmp_path):
        missing = tmp_path / 'nope.png'   # does not exist
        result, mock_gen, uploaded = self._call_with_input(
            tmp_path, str(missing),
            upload_payload={'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            resolve_path=missing,         # resolver returns a path that is not a file
        )
        assert 'ERROR' in result[0].text
        assert 'not found' in result[0].text
        assert len(uploaded) == 0
        mock_gen.assert_not_called()

    def test_path_outside_allowed_dirs_errors(self, tmp_path):
        """resolve_tool_path ValueError → clean ERROR, no upload."""
        result, mock_gen, uploaded = self._call_with_input(
            tmp_path, '/outside/allowed/cat.png',
            upload_payload={'name': 'cat.png', 'subfolder': '', 'type': 'input'},
            resolve_exc=ValueError("Path '/outside/allowed/cat.png' is outside the allowed RO directories"),
        )
        assert 'ERROR' in result[0].text
        assert 'Invalid input_image path' in result[0].text
        assert len(uploaded) == 0
        mock_gen.assert_not_called()

    @pytest.mark.parametrize('bad', [['a.png'], {'path': 'a.png'}, True, 42])
    def test_non_string_input_image_errors(self, tmp_path, bad):
        """Non-string input_image → the guard returns a clean ERROR.

        The guard is exercised by calling ``_handle_text_prompt`` directly: the
        JSON-schema layer separately rejects non-string values with a ``type``
        ValidationError at ``call()`` (a pre-existing behaviour for any
        type-mismatched param).
        """
        wf_path = _write_wf(tmp_path, 1)
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate',
                   return_value=(FAKE_IMAGE_BYTES, {'seed': 42})) as mock_gen:
            tool = ImageGen()
            result = tool._handle_text_prompt({'prompt': 'a cat', 'seed': 42, 'input_image': bad}, {})
        assert len(result) == 1
        assert 'ERROR' in result[0].text
        assert 'non-empty string' in result[0].text
        mock_gen.assert_not_called()

    @pytest.mark.parametrize('ws', ['   ', '\t'])
    def test_whitespace_only_input_image_errors(self, tmp_path, ws):
        """Whitespace-only input_image → the `.strip()` guard returns a clean ERROR.

        Empty string ('') is falsy and skips the entire block (same as absent),
        so it is NOT an error — only non-empty whitespace triggers the guard.
        """
        wf_path = _write_wf(tmp_path, 1)
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate',
                   return_value=(FAKE_IMAGE_BYTES, {'seed': 42})) as mock_gen:
            tool = ImageGen()
            result = tool._handle_text_prompt({'prompt': 'a cat', 'seed': 42, 'input_image': ws}, {})
        assert len(result) == 1
        assert 'ERROR' in result[0].text
        assert 'non-empty string' in result[0].text
        mock_gen.assert_not_called()

    def test_empty_string_input_image_is_noop(self, tmp_path):
        """Empty-string input_image is falsy → skips the block entirely (same as absent)."""
        wf_path = _write_wf(tmp_path, 1)
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate',
                   return_value=(FAKE_IMAGE_BYTES, {'seed': 42})) as mock_gen:
            tool = ImageGen()
            result = tool._handle_text_prompt({'prompt': 'a cat', 'seed': 42, 'input_image': ''}, {})
        # Should proceed with normal generation (no input_image wiring)
        mock_gen.assert_called_once()

    @pytest.mark.parametrize('mode', ['non200', 'missing_name', 'conn_error'])
    def test_upload_failure_clean_error_no_vram_save(self, tmp_path, mode):
        """Upload failure → clean ERROR and NO VRAM save (upload precedes the VRAM block)."""
        if mode == 'non200':
            payload, status, exc = {'name': 'cat.png'}, 500, None
        elif mode == 'missing_name':
            payload, status, exc = {'subfolder': '', 'type': 'input'}, 200, None
        else:
            payload, status, exc = None, 200, httpx.ConnectError('connection refused')

        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        wf_path = _write_wf(tmp_path, 1)
        captured = []
        up_transport = _make_upload_transport(payload, captured, status_code=status, raise_exc=exc)

        mock_instance = MagicMock()
        mock_instance._last_endpoint_config = {
            'state_save_enabled': True,
            'api_base': 'http://localhost:1234',
            'model': 'test-model',
        }

        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
             patch('agent_cascade.tools.image_gen.httpx.Client',
                   return_value=httpx.Client(transport=up_transport)), \
             patch('agent_cascade.tools.image_gen.save_image_to_media',
                   return_value='/tmp/media/out.png'), \
             patch.object(ImageGen, '_get_instance', return_value=mock_instance), \
             patch('agent_cascade.state_ops.is_autoloader_endpoint', return_value=True), \
             patch('agent_cascade.state_ops.save_instance_state', return_value=True) as mock_save, \
             patch('agent_cascade.utils.tool_path_resolver.resolve_tool_path', return_value=img):
            tool = ImageGen()
            result = tool.call({'prompt': 'a cat', 'seed': 42, 'input_image': str(img)})

        assert len(result) == 1
        assert 'ERROR' in result[0].text
        mock_save.assert_not_called()  # returned before the VRAM block

    def test_svg_with_input_image_errors(self):
        """SVG prompt + input_image → clean ERROR (input_image is ComfyUI-only)."""
        tool = ImageGen()
        svg = '<svg width="10" height="10"><rect/></svg>'
        result = tool.call({'prompt': svg, 'input_image': '/tmp/cat.png'})
        assert len(result) == 1
        assert 'ERROR' in result[0].text
        assert 'SVG' in result[0].text

    def test_graph_ref_loadimage_ignored(self, tmp_path):
        """A LoadImage node with a graph-ref image input (['id', 0]) is NOT wireable and is
        ignored; only the scalar-string LoadImage node is wired (the v1 contract)."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        wf = _wf_with_loadimage(1)  # node '9' = scalar-string LoadImage
        wf['20'] = {'class_type': 'LoadImage', 'inputs': {'image': ['15', 0]}}  # graph ref
        wf_path = tmp_path / 'wf.json'
        wf_path.write_text(json.dumps(wf), encoding='utf-8')

        captured = []
        up_transport = _make_upload_transport(
            {'name': 'cat.png', 'subfolder': '', 'type': 'input'}, captured
        )
        mock_gen = MagicMock(return_value=(FAKE_IMAGE_BYTES, {'seed': 42}))
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(str(wf_path))), \
             patch('agent_cascade.tools.image_gen.httpx.Client',
                   return_value=httpx.Client(transport=up_transport)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate', mock_gen), \
             patch('agent_cascade.tools.image_gen.save_image_to_media',
                   return_value='/tmp/media/out.png'), \
             patch('agent_cascade.utils.tool_path_resolver.resolve_tool_path', return_value=img):
            tool = ImageGen()
            result = tool.call({'prompt': 'a cat', 'seed': 42, 'input_image': str(img)})

        assert _error_text(result) is None
        wf_out = mock_gen.call_args.args[1]
        assert wf_out['9']['inputs']['image'] == 'cat.png'    # scalar node wired
        assert wf_out['20']['inputs']['image'] == ['15', 0]   # graph-ref node untouched

    def test_only_graph_ref_loadimage_errors(self, tmp_path):
        """If the only LoadImage node uses a graph-ref image input, it's not wireable → clean
        'no standard LoadImage node' error, no upload."""
        img = tmp_path / 'cat.png'
        img.write_bytes(b'x')
        wf = _wf_with_loadimage(0)  # no scalar-string LoadImage
        wf['20'] = {'class_type': 'LoadImage', 'inputs': {'image': ['15', 0]}}
        wf_path = tmp_path / 'wf.json'
        wf_path.write_text(json.dumps(wf), encoding='utf-8')

        captured = []
        up_transport = _make_upload_transport(
            {'name': 'cat.png', 'subfolder': '', 'type': 'input'}, captured
        )
        mock_gen = MagicMock(return_value=(FAKE_IMAGE_BYTES, {'seed': 42}))
        with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(str(wf_path))), \
             patch('agent_cascade.tools.image_gen.httpx.Client',
                   return_value=httpx.Client(transport=up_transport)), \
             patch('agent_cascade.tools.image_gen._comfyui_generate', mock_gen), \
             patch('agent_cascade.tools.image_gen.save_image_to_media',
                   return_value='/tmp/media/out.png'), \
             patch('agent_cascade.utils.tool_path_resolver.resolve_tool_path', return_value=img):
            tool = ImageGen()
            result = tool.call({'prompt': 'a cat', 'seed': 42, 'input_image': str(img)})

        assert 'ERROR' in result[0].text
        assert 'LoadImage' in result[0].text
        assert len(captured) == 0  # no orphan upload
        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Backward compatibility — absent param = byte-identical
# ---------------------------------------------------------------------------


class TestBackwardCompat:

    def test_absent_param_is_byte_identical(self, tmp_path):
        """Without input_image the POST /prompt payload is byte-identical across runs and
        the LoadImage node is never touched; with it, the payload differs (discriminates)."""
        wf_path = _write_wf(tmp_path, 1)
        img = tmp_path / 'cat.png'
        img.write_bytes(b'PNGDATA')

        def run(params):
            prompts = []
            transport = _make_full_transport(prompts)
            # Capture the REAL client class BEFORE patching (image_gen.httpx IS the httpx
            # module, so patching image_gen.httpx.Client replaces httpx.Client itself).
            real_client_cls = httpx.Client
            # side_effect (not return_value): the present-param run calls httpx.Client()
            # twice — once in _upload_image (which closes it) and once in _comfyui_generate —
            # so each call must get a FRESH client sharing the same transport.
            with patch('agent_cascade.tools.image_gen._get_image_gen_config', return_value=_cfg(wf_path)), \
                 patch('agent_cascade.tools.image_gen.httpx.Client',
                       side_effect=lambda *a, **k: real_client_cls(transport=transport)), \
                 patch('agent_cascade.tools.image_gen.save_image_to_media',
                       return_value='/tmp/media/out.png'), \
                 patch('agent_cascade.tools.image_gen.time.sleep'), \
                 patch('agent_cascade.utils.tool_path_resolver.resolve_tool_path', return_value=img):
                tool = ImageGen()
                result = tool.call(params)
            return result, prompts

        # Two absent-param runs → byte-identical payload.
        res_a, p_a = run({'prompt': 'a cat', 'seed': 42})
        res_b, p_b = run({'prompt': 'a cat', 'seed': 42})
        assert _error_text(res_a) is None
        assert _error_text(res_b) is None
        assert len(p_a) == 1 and len(p_b) == 1
        assert json.dumps(p_a[0], sort_keys=True) == json.dumps(p_b[0], sort_keys=True)
        # The LoadImage node is untouched when input_image is absent.
        assert p_a[0]['prompt']['9']['inputs']['image'] == 'ref_0.png'

        # Present-param run → payload DIFFERS (LoadImage wired) — proves the test discriminates.
        res_c, p_c = run({'prompt': 'a cat', 'seed': 42, 'input_image': str(img)})
        assert _error_text(res_c) is None
        assert p_c[0]['prompt']['9']['inputs']['image'] == 'cat.png'
        assert json.dumps(p_c[0], sort_keys=True) != json.dumps(p_a[0], sort_keys=True)
