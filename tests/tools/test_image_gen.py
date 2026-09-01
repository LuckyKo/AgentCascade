"""Unit tests for the image_gen tool (agent_cascade/tools/image_gen.py).

Covers:
  * SVG detection (_is_svg_code)
  * Workflow loading (_load_workflow)
  * Parameter injection (_inject_params) — zimg and flux2 patterns
  * ComfyUI client (_comfyui_generate) via httpx.MockTransport
  * Config access (_get_image_gen_config / _invalidate_image_gen_config)
  * End-to-end return format (SVG path, fully mocked)

All external I/O is mocked. No network calls, no real cairosvg rendering.
"""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest

from agent_cascade.llm.schema import ContentItem
from agent_cascade.tools.image_gen import (
    ImageGen,
    _is_svg_code,
    _load_workflow,
    _list_workflows,
    _inject_params,
    _comfyui_generate,
    _get_image_gen_config,
    _invalidate_image_gen_config,
    _image_gen_config_path,
    _svg_dimensions,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _bust_config_cache():
    """Ensure the image_gen config cache is clean before and after every test."""
    _invalidate_image_gen_config()
    yield
    _invalidate_image_gen_config()


@pytest.fixture
def fake_config_path(tmp_path):
    """Point _image_gen_config_path at a file inside tmp_path."""
    cfg_file = tmp_path / "config" / "image_gen.json"
    cfg_file.parent.mkdir(parents=True, exist_ok=True)

    with patch("agent_cascade.tools.image_gen._image_gen_config_path", return_value=cfg_file):
        yield cfg_file


# ---------------------------------------------------------------------------
# 1. SVG detection
# ---------------------------------------------------------------------------

class TestIsSvgCode:
    @pytest.mark.parametrize("text,expected", [
        ('<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>', True),
        ('<?xml version="1.0" encoding="UTF-8"?><svg><circle r="5"/></svg>', True),
        ('  \n\t<svg width="10"><path d="M0 0"/></svg>', True),
        ("hello world", False),
        ("<svg>", False),
        ("", False),
        (None, False),  # type: ignore[arg-type]
        ('<?xml version="1.0"?><html></html>', False),
        ("<SVG><rect/></SVG>", False),  # uppercase not matched
    ])
    def test_detection(self, text, expected):
        assert _is_svg_code(text) is expected


# ---------------------------------------------------------------------------
# 1b. SVG dimensions extraction
# ---------------------------------------------------------------------------

class TestSvgDimensions:
    def test_extracts_width_height(self):
        svg = '<svg width="800" height="600"><rect/></svg>'
        assert _svg_dimensions(svg) == (800, 600)

    def test_fallback_when_missing(self):
        svg = '<svg><rect/></svg>'
        assert _svg_dimensions(svg) == (1024, 1024)

    def test_custom_fallback(self):
        svg = '<svg><rect/></svg>'
        assert _svg_dimensions(svg, fallback_width=50, fallback_height=75) == (50, 75)

    def test_float_values_truncated_to_int(self):
        svg = '<svg width="100.7" height="200.3"><rect/></svg>'
        assert _svg_dimensions(svg) == (100, 200)

    def test_malformed_svg_never_raises(self):
        # Should return fallback without raising
        assert _svg_dimensions("not an svg at all") == (1024, 1024)


# ---------------------------------------------------------------------------
# 2. Workflow loading
# ---------------------------------------------------------------------------

class TestLoadWorkflow:
    def test_valid_json_returns_dict(self, tmp_path):
        wf = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "hi"}}}
        p = tmp_path / "wf.json"
        p.write_text(json.dumps(wf), encoding="utf-8")
        result = _load_workflow(str(p))
        assert result == wf

    def test_nonexistent_path_raises(self, tmp_path):
        missing = tmp_path / "nope.json"
        with pytest.raises(FileNotFoundError) as exc_info:
            _load_workflow(str(missing))
        assert str(missing) in str(exc_info.value)

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json at all {{{", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            _load_workflow(str(p))

    def test_non_dict_json_raises_value_error(self, tmp_path):
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="must be an object"):
            _load_workflow(str(p))


# ---------------------------------------------------------------------------
# 2b. Workflow listing
# ---------------------------------------------------------------------------

class TestListWorkflows:
    def test_missing_dir_returns_empty(self, tmp_path):
        assert _list_workflows(str(tmp_path / "nonexistent")) == []

    def test_globs_json_files(self, tmp_path):
        (tmp_path / "a.json").write_text("{}", encoding="utf-8")
        (tmp_path / "b.json").write_text("{}", encoding="utf-8")
        (tmp_path / "c.txt").write_text("not json", encoding="utf-8")  # ignored
        result = _list_workflows(str(tmp_path))
        names = [w["name"] for w in result]
        assert names == ["a", "b"]

    def test_sorted_by_name(self, tmp_path):
        (tmp_path / "zeta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "alpha.json").write_text("{}", encoding="utf-8")
        result = _list_workflows(str(tmp_path))
        assert [w["name"] for w in result] == ["alpha", "zeta"]

    def test_returns_name_and_path(self, tmp_path):
        (tmp_path / "wf.json").write_text("{}", encoding="utf-8")
        result = _list_workflows(str(tmp_path))
        assert len(result) == 1
        assert result[0]["name"] == "wf"
        assert result[0]["path"] == str(tmp_path / "wf.json")


# ---------------------------------------------------------------------------
# 3. Parameter injection
# ---------------------------------------------------------------------------

def _zimg_workflow():
    """Minimal zimg-style workflow: two CLIPTextEncode + KSampler with direct int dims."""
    return {
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "positive prompt here"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        "8": {"class_type": "KSampler", "inputs": {
            "width": 1024, "height": 1024, "seed": 12345, "steps": 20,
        }},
    }


def _flux2_workflow():
    """Minimal flux2-style workflow: PrimitiveStringMultiline + node-ref dims via PrimitiveInt."""
    return {
        "10": {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "old prompt"}},
        "75:68": {"class_type": "PrimitiveInt", "inputs": {"value": 512}},
        "75:69": {"class_type": "PrimitiveInt", "inputs": {"value": 512}},
        "11": {"class_type": "KSampler", "inputs": {
            "width": ["75:68", 0], "height": ["75:69", 0], "seed": 999,
        }},
    }


def _zimg_workflow_with_aspect():
    """zimg + CR Aspect Ratio node."""
    wf = _zimg_workflow()
    wf["9"] = {"class_type": "CR Aspect Ratio", "inputs": {
        "aspect_ratio": "16:9", "swap_dimensions": "On",
    }}
    return wf


class TestInjectParamsZimg:
    def test_prompt_injected_into_positive_node(self):
        wf = _zimg_workflow()
        _, report = _inject_params(wf, prompt="a red fox", seed=42)
        # Node "6" had non-empty text → it's the positive slot
        assert wf["6"]["inputs"]["text"] == "a red fox"
        assert any("prompt → 6" in r for r in report)

    def test_negative_prompt_injected_into_second_clip(self):
        wf = _zimg_workflow()
        _, report = _inject_params(wf, prompt="cat", negative_prompt="blurry", seed=1)
        assert wf["7"]["inputs"]["text"] == "blurry"
        assert any("negative → 7" in r for r in report)

    def test_width_height_override_direct_ints(self):
        wf = _zimg_workflow()
        _, report = _inject_params(wf, prompt="x", width=512, height=768, seed=0)
        assert wf["8"]["inputs"]["width"] == 512
        assert wf["8"]["inputs"]["height"] == 768

    def test_seed_injected(self):
        wf = _zimg_workflow()
        _, report = _inject_params(wf, prompt="x", seed=999)
        assert wf["8"]["inputs"]["seed"] == 999

    def test_random_seed_when_omitted(self):
        wf = _zimg_workflow()
        _, report = _inject_params(wf, prompt="x")
        # Seed should be a random int in [0, 2^32-1]
        assert isinstance(wf["8"]["inputs"]["seed"], int)
        assert 0 <= wf["8"]["inputs"]["seed"] < 2**32

    def test_aspect_ratio_forced_custom(self):
        wf = _zimg_workflow_with_aspect()
        _, report = _inject_params(wf, prompt="x", width=100, height=200, seed=5)
        assert wf["9"]["inputs"]["aspect_ratio"] == "custom"
        assert wf["9"]["inputs"]["swap_dimensions"] == "Off"


class TestInjectParamsFlux2:
    def test_prompt_injected_into_primitive(self):
        wf = _flux2_workflow()
        _, report = _inject_params(wf, prompt="a blue whale", seed=7)
        assert wf["10"]["inputs"]["value"] == "a blue whale"
        assert any("prompt → 10 (PrimitiveStringMultiline)" in r for r in report)

    def test_width_height_override_via_node_refs(self):
        wf = _flux2_workflow()
        _, report = _inject_params(wf, prompt="x", width=832, height=1248, seed=0)
        assert wf["75:68"]["inputs"]["value"] == 832
        assert wf["75:69"]["inputs"]["value"] == 1248

    def test_seed_injected(self):
        wf = _flux2_workflow()
        _, report = _inject_params(wf, prompt="x", seed=42)
        assert wf["11"]["inputs"]["seed"] == 42


class TestInjectParamsEdgeCases:
    def test_all_empty_clip_nodes_first_becomes_positive(self):
        """When all CLIPTextEncode nodes have empty text, the first is used for positive."""
        wf = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        }
        _, report = _inject_params(wf, prompt="test", negative_prompt="bad", seed=1)
        # First node (document order) gets the positive prompt
        assert wf["1"]["inputs"]["text"] == "test"
        # Second node gets the negative prompt
        assert wf["2"]["inputs"]["text"] == "bad"

    def test_single_clip_node_negative_ignored(self):
        """With only one CLIPTextEncode, negative prompt is silently ignored (no error)."""
        wf = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}
        _, report = _inject_params(wf, prompt="ok", negative_prompt="bad", seed=1)
        # Prompt goes to the only node; negative is dropped
        assert wf["1"]["inputs"]["text"] == "ok"
        assert not any("negative" in r for r in report)

    def test_multiple_seed_nodes_all_updated(self):
        """Every node with a seed or noise_seed input gets the same seed."""
        wf = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "2": {"class_type": "KSampler", "inputs": {"seed": 0, "width": 512, "height": 512}},
            "3": {"class_type": "SomeOtherNode", "inputs": {"noise_seed": 999}},
        }
        _, report = _inject_params(wf, prompt="x", seed=777)
        assert wf["2"]["inputs"]["seed"] == 777
        assert wf["3"]["inputs"]["noise_seed"] == 777


class TestInjectParamsErrors:
    def test_no_text_nodes_raises_value_error(self):
        wf = {"1": {"class_type": "KSampler", "inputs": {"seed": 1}}}
        with pytest.raises(ValueError, match="Could not inject prompt"):
            _inject_params(wf, prompt="hello")

    def test_empty_workflow_raises(self):
        with pytest.raises(ValueError, match="Could not inject prompt"):
            _inject_params({}, prompt="hello")


# ---------------------------------------------------------------------------
# 4. ComfyUI client (_comfyui_generate) via httpx.MockTransport
# ---------------------------------------------------------------------------

FAKE_IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake image data"
PROMPT_ID = "test-prompt-id-001"


def _make_comfyui_transport(complete_after_polls=1, status_str="success", include_image=True):
    """Build an httpx.MockTransport handler that simulates the ComfyUI API.

    Args:
        complete_after_polls: Number of /history polls before reporting completion.
        status_str: The status_str value returned in history (e.g. "error").
        include_image: Whether to include an image in the completed outputs.
    """
    state = {"poll_count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # POST /prompt → submit
        if request.method == "POST" and request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": PROMPT_ID})

        # GET /history/{id} → poll
        if request.method == "GET" and request.url.path.startswith("/history/"):
            state["poll_count"] += 1
            if state["poll_count"] < complete_after_polls:
                # Not done yet — return empty history (no entry for this prompt_id)
                return httpx.Response(200, json={})

            outputs = {}
            if include_image and status_str != "error":
                outputs = {"3": {"images": [
                    {"filename": "gen_00001.png", "subfolder": "", "type": "output"},
                ]}}

            return httpx.Response(200, json={PROMPT_ID: {
                "status": {"status_str": status_str, "completed": status_str != "error"},
                "outputs": outputs,
            }})

        # GET /view?filename=... → download image bytes
        if request.method == "GET" and request.url.path == "/view":
            return httpx.Response(200, content=FAKE_IMAGE_BYTES)

        return httpx.Response(404, text="Not found")

    return httpx.MockTransport(handler)


class TestComfyUIGenerate:
    def test_success_flow(self):
        transport = _make_comfyui_transport(complete_after_polls=1)
        client = httpx.Client(transport=transport)
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 42}}}

        with patch("agent_cascade.tools.image_gen.time.sleep"):  # skip real sleeps
            image_bytes, meta = _comfyui_generate(
                "http://localhost:8188", wf, timeout=30, client=client
            )

        assert image_bytes == FAKE_IMAGE_BYTES
        assert meta == {"seed": 42}

    def test_server_unreachable_raises_runtime_error(self):
        """Simulate ConnectError by using a transport that raises."""
        def failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        client = httpx.Client(transport=httpx.MockTransport(failing_handler))
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        with pytest.raises(RuntimeError, match="not reachable"):
            _comfyui_generate("http://localhost:8188", wf, timeout=5, client=client)

    def test_timeout_when_never_completes(self):
        """Polling never returns a completed entry → TimeoutError."""
        # POST /prompt succeeds (returns prompt_id), but /history always returns {}
        # (no entry for the prompt_id) so the loop never sees completion.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": PROMPT_ID})
            # /history always returns empty — prompt never appears
            return httpx.Response(200, json={})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        # Mock time so the deadline is reached after a few polls without real waiting.
        fake_now = [1000.0]

        def fake_time():
            return fake_now[0]

        def fake_sleep(secs):
            fake_now[0] += secs  # simulate elapsed time

        with patch("agent_cascade.tools.image_gen.time.time", side_effect=fake_time), \
             patch("agent_cascade.tools.image_gen.time.sleep", side_effect=fake_sleep):
            with pytest.raises(TimeoutError, match="timed out"):
                _comfyui_generate("http://localhost:8188", wf, timeout=5, client=client)

    def test_no_image_in_outputs_raises(self):
        """Completed but outputs have no images → RuntimeError."""
        transport = _make_comfyui_transport(complete_after_polls=1, include_image=False)
        client = httpx.Client(transport=transport)
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        with patch("agent_cascade.tools.image_gen.time.sleep"):
            with pytest.raises(RuntimeError, match="no image found in outputs"):
                _comfyui_generate("http://localhost:8188", wf, timeout=30, client=client)

    def test_comfyui_error_status_raises(self):
        """status_str == 'error' → RuntimeError with the error detail."""
        transport = _make_comfyui_transport(complete_after_polls=1, status_str="error")
        client = httpx.Client(transport=transport)
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        with patch("agent_cascade.tools.image_gen.time.sleep"):
            with pytest.raises(RuntimeError, match="ComfyUI generation error"):
                _comfyui_generate("http://localhost:8188", wf, timeout=30, client=client)

    def test_submit_non_200_raises(self):
        """POST /prompt returns 500 → RuntimeError."""
        transport = httpx.MockTransport(
            lambda req: httpx.Response(500, text="Internal Server Error")
        )
        client = httpx.Client(transport=transport)
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        with pytest.raises(RuntimeError, match="submission failed"):
            _comfyui_generate("http://localhost:8188", wf, timeout=5, client=client)

    def test_transient_polling_error_recovers(self):
        """A transient httpx.RequestError during polling is skipped; next poll succeeds."""
        state = {"poll_count": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "POST" and request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": PROMPT_ID})
            if request.method == "GET" and request.url.path.startswith("/history/"):
                state["poll_count"] += 1
                if state["poll_count"] == 1:
                    raise httpx.RequestError("Transient network hiccup")
                # Second poll succeeds with completion
                return httpx.Response(200, json={PROMPT_ID: {
                    "status": {"status_str": "success", "completed": True},
                    "outputs": {"3": {"images": [
                        {"filename": "out.png", "subfolder": "", "type": "output"},
                    ]}},
                }})
            if request.method == "GET" and request.url.path == "/view":
                return httpx.Response(200, content=FAKE_IMAGE_BYTES)
            return httpx.Response(404)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        wf = {"8": {"class_type": "KSampler", "inputs": {"seed": 1}}}

        with patch("agent_cascade.tools.image_gen.time.sleep"):
            image_bytes, meta = _comfyui_generate(
                "http://localhost:8188", wf, timeout=30, client=client
            )

        assert image_bytes == FAKE_IMAGE_BYTES


# ---------------------------------------------------------------------------
# 5. Config access
# ---------------------------------------------------------------------------

class TestImageGenConfig:
    def test_no_config_file_returns_empty(self, fake_config_path):
        """When the config file doesn't exist, _get_image_gen_config returns {}."""
        assert not fake_config_path.exists()
        result = _get_image_gen_config()
        assert result == {}

    def test_valid_config_file(self, fake_config_path):
        cfg = {"url": "http://localhost:8188", "timeout": 60, "default_workflow": "/wf.json"}
        fake_config_path.write_text(json.dumps(cfg), encoding="utf-8")
        result = _get_image_gen_config()
        assert result["url"] == "http://localhost:8188"
        assert result["timeout"] == 60
        assert result["default_workflow"] == "/wf.json"

    def test_malformed_json_returns_empty(self, fake_config_path):
        fake_config_path.write_text("{{{not json", encoding="utf-8")
        result = _get_image_gen_config()
        assert result == {}

    def test_non_dict_json_returns_empty(self, fake_config_path):
        fake_config_path.write_text("[1, 2, 3]", encoding="utf-8")
        result = _get_image_gen_config()
        assert result == {}

    def test_cache_invalidation(self, fake_config_path):
        # Write initial config
        fake_config_path.write_text(json.dumps({"url": "http://old"}), encoding="utf-8")
        assert _get_image_gen_config()["url"] == "http://old"

        # Change the file — cache should still return old value within TTL
        fake_config_path.write_text(json.dumps({"url": "http://new"}), encoding="utf-8")
        assert _get_image_gen_config()["url"] == "http://old"  # cached

        # Bust the cache
        _invalidate_image_gen_config()
        assert _get_image_gen_config()["url"] == "http://new"


# ---------------------------------------------------------------------------
# 6. Return format (end-to-end via mocked SVG path)
# ---------------------------------------------------------------------------

class TestReturnFormat:
    def test_svg_path_returns_content_items(self):
        """SVG prompt → [ContentItem(image=...), ContentItem(text=caption)]."""
        tool = ImageGen()
        svg = '<svg width="200" height="100"><rect width="200" height="100"/></svg>'

        with patch("agent_cascade.tools.image_gen._render_svg_to_png_bytes",
                   return_value=b"fake_png_bytes") as mock_render, \
             patch("agent_cascade.tools.image_gen.save_image_to_media",
                   return_value="/tmp/media/imggen_test.png") as mock_save:

            result = tool.call({"prompt": svg})

        # Verify render was called with the SVG
        mock_render.assert_called_once_with(svg)
        # Verify save was called with the rendered bytes
        mock_save.assert_called_once_with(image_source=b"fake_png_bytes", source_name="svg_render")

        # Verify return shape
        assert isinstance(result, list)
        assert len(result) == 2
        assert isinstance(result[0], ContentItem)
        assert result[0].image == "/tmp/media/imggen_test.png"
        assert result[0].text is None
        assert isinstance(result[1], ContentItem)
        assert "SVG rendered to image" in result[1].text
        assert "200x100" in result[1].text

    def test_missing_prompt_raises_validation_error(self):
        """jsonschema validation rejects missing required 'prompt' field."""
        import jsonschema
        tool = ImageGen()
        with pytest.raises(jsonschema.ValidationError, match="prompt"):
            tool.call({})

    def test_empty_prompt_returns_error(self):
        tool = ImageGen()
        result = tool.call({"prompt": "   "})
        assert len(result) == 1
        assert "ERROR" in result[0].text

    def test_svg_render_import_error(self):
        """cairosvg not installed → ImportError caught and returned as error message."""
        tool = ImageGen()
        svg = '<svg width="10" height="10"><rect/></svg>'
        with patch("agent_cascade.tools.image_gen._render_svg_to_png_bytes",
                   side_effect=ImportError("cairosvg is required")):
            result = tool.call({"prompt": svg})
        assert len(result) == 1
        assert "ERROR" in result[0].text
        assert "cairosvg" in result[0].text

    def test_svg_render_os_error(self):
        """Native library failure → OSError caught and returned as error message."""
        tool = ImageGen()
        svg = '<svg width="10" height="10"><rect/></svg>'
        with patch("agent_cascade.tools.image_gen._render_svg_to_png_bytes",
                   side_effect=OSError("GTK3 not found")):
            result = tool.call({"prompt": svg})
        assert len(result) == 1
        assert "ERROR" in result[0].text

    def test_svg_render_generic_error(self):
        """Malformed SVG → generic exception caught and returned as error."""
        tool = ImageGen()
        svg = '<svg width="10" height="10"><rect/></svg>'
        with patch("agent_cascade.tools.image_gen._render_svg_to_png_bytes",
                   side_effect=ValueError("Invalid SVG")):
            result = tool.call({"prompt": svg})
        assert len(result) == 1
        assert "ERROR" in result[0].text
        assert "SVG parse/render error" in result[0].text

    def test_no_comfyui_url_returns_error(self, fake_config_path):
        """Text prompt with no URL configured → error message."""
        tool = ImageGen()
        # No config file → empty config → no URL
        result = tool.call({"prompt": "a cat in a hat"})
        assert len(result) == 1
        assert "No ComfyUI server configured" in result[0].text

    def test_workflow_file_not_found_returns_error(self, fake_config_path, tmp_path):
        """Nonexistent workflow path → error with available workflows listed."""
        cfg = {"url": "http://localhost:8188", "default_workflow": str(tmp_path / "missing.json")}
        fake_config_path.write_text(json.dumps(cfg), encoding="utf-8")
        tool = ImageGen()
        result = tool.call({"prompt": "a cat"})
        assert len(result) == 1
        assert "Workflow file not found" in result[0].text

    def test_workflow_invalid_json_returns_error(self, fake_config_path, tmp_path):
        """Malformed workflow JSON → error message."""
        wf_file = tmp_path / "bad.json"
        wf_file.write_text("not valid json {{{", encoding="utf-8")
        cfg = {"url": "http://localhost:8188", "default_workflow": str(wf_file)}
        fake_config_path.write_text(json.dumps(cfg), encoding="utf-8")
        tool = ImageGen()
        result = tool.call({"prompt": "a cat"})
        assert len(result) == 1
        assert "Invalid workflow JSON" in result[0].text

    def test_no_workflow_specified_returns_error(self, fake_config_path):
        """No workflow param and no default → error listing available workflows."""
        cfg = {"url": "http://localhost:8188"}
        fake_config_path.write_text(json.dumps(cfg), encoding="utf-8")
        tool = ImageGen()
        result = tool.call({"prompt": "a cat"})
        assert len(result) == 1
        assert "No workflow specified" in result[0].text

    def test_text_prompt_success_returns_content_items(self, fake_config_path, tmp_path):
        """Full text-prompt path with mocked ComfyUI and media save."""
        # Set up config
        wf_file = tmp_path / "wf.json"
        wf_file.write_text(json.dumps(_zimg_workflow()), encoding="utf-8")

        cfg = {"url": "http://localhost:8188", "timeout": 30, "default_workflow": str(wf_file)}
        fake_config_path.write_text(json.dumps(cfg), encoding="utf-8")

        tool = ImageGen()

        # Mock the ComfyUI client and media save
        transport = _make_comfyui_transport(complete_after_polls=1)
        mock_client = httpx.Client(transport=transport)

        with patch("agent_cascade.tools.image_gen._comfyui_generate",
                   return_value=(b"fake_bytes", {"seed": 42})) as mock_gen, \
             patch("agent_cascade.tools.image_gen.save_image_to_media",
                   return_value="/tmp/media/imggen_comfy.png") as mock_save:

            result = tool.call({"prompt": "a cat", "width": 512, "height": 512})

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].image == "/tmp/media/imggen_comfy.png"
        assert "Generated image" in result[1].text
        assert "a cat" in result[1].text
        assert "512x512" in result[1].text
