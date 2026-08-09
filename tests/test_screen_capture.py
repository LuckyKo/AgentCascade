"""Unit tests for view_image screen capture enhancement.

All tests are fully mocked — no display or external dependencies required.
Tests cover:
  - ViewImage directive routing in file_ops.py
  - Platform dispatch and fallback logic in screen_capture.py
"""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

# ---------------------------------------------------------------------------
# ViewImage Directive Routing Tests
# ---------------------------------------------------------------------------


class TestViewImageDirectiveRouting:
    """Tests for __screen_capture and __window_capture directive parsing."""

    @pytest.fixture(autouse=True)
    def _setup_env(self):
        """Ensure screen capture is enabled by default in tests."""
        with patch.dict(os.environ, {'SCREEN_CAPTURE_ENABLED': 'True'}):
            yield

    @pytest.fixture
    def view_image_tool(self):
        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()
        return tool

    @pytest.mark.parametrize("directive,capture_func,expected_pid,text_contains", [
        ("__screen_capture", "capture_screen", None, "Viewing image: __screen_capture"),
        ("__window_capture:1234", "capture_window_by_pid", 1234, "Viewing image: __window_capture:1234"),
    ])
    def test_capture_directive_parsed(self, view_image_tool, directive, capture_func, expected_pid, text_contains):
        """Mock capture functions, verify correct routing for screen and window capture directives."""
        mock_png_bytes = b'\x89PNG\x0d\x0a...'  # fake PNG header
        mock_temp_path = 'C:/tmp/test_capture.png'

        with patch(
            f'agent_cascade.tools.custom.screen_capture.{capture_func}',
            return_value=mock_png_bytes
        ) as mock_capture:
            with patch('tempfile.mkstemp', return_value=(42, mock_temp_path)):
                with patch('os.close'):
                    with patch('builtins.open', mock_open()):
                        result = view_image_tool.call(json.dumps({'path': directive}))

                        if expected_pid is not None:
                            mock_capture.assert_called_once_with(expected_pid)
                        else:
                            mock_capture.assert_called_once()
                        assert isinstance(result, list)
                        # First item is ContentItem(image=...), second is ContentItem(text=...)
                        assert len(result) == 2
                        assert 'image' in result[0].__dict__
                        assert text_contains in result[1].text

    @pytest.mark.parametrize("directive,expected_monitor_index,text_contains", [
        ("__screen_capture:0", 0, "Viewing image: __screen_capture:0"),
        ("__screen_capture:1", 1, "Viewing image: __screen_capture:1"),
        ("__screen_capture:5", 5, "Viewing image: __screen_capture:5"),
    ])
    def test_per_monitor_capture_directive(self, view_image_tool, directive, expected_monitor_index, text_contains):
        """Mock capture_screen(), verify correct routing for __screen_capture:N directives."""
        mock_png_bytes = b'\x89PNG\x0d\x0a...'
        mock_temp_path = 'C:/tmp/test_capture.png'

        with patch(
            'agent_cascade.tools.custom.screen_capture.capture_screen',
            return_value=mock_png_bytes
        ) as mock_capture:
            with patch('tempfile.mkstemp', return_value=(42, mock_temp_path)):
                with patch('os.close'):
                    with patch('builtins.open', mock_open()):
                        result = view_image_tool.call(json.dumps({'path': directive}))

                        mock_capture.assert_called_once_with(monitor_index=expected_monitor_index)
                        assert isinstance(result, list)
                        assert len(result) == 2
                        assert 'image' in result[0].__dict__
                        assert text_contains in result[1].text

    def test_invalid_screen_capture_monitor_index(self, view_image_tool):
        """Paths like '__screen_capture:-1', '__screen_capture:abc', '__screen_capture:' return error messages."""
        invalid_paths = [
            '__screen_capture:-1',
            '__screen_capture:abc',
            '__screen_capture:',
            '__screen_capture:3.14',
        ]

        for path in invalid_paths:
            result = view_image_tool.call(json.dumps({'path': path}))
            assert isinstance(result, str)
            assert 'Invalid screen capture format' in result

    def test_invalid_pid_rejected(self, view_image_tool):
        """Paths like '__window_capture:abc', '__window_capture:-5', '__window_capture:' return error messages."""
        invalid_paths = [
            '__window_capture:abc',
            '__window_capture:-5',
            '__window_capture:',
        ]

        for path in invalid_paths:
            result = view_image_tool.call(json.dumps({'path': path}))
            assert isinstance(result, str)
            assert 'Invalid window capture format' in result

    def test_unknown_directive_falls_through(self, view_image_tool):
        """'__something_else' doesn't trigger capture, falls through to file resolution (mock _resolve_path)."""
        # Ensure it doesn't match screen_capture directives and calls _resolve_path instead
        mock_resolved = MagicMock(spec=Path)
        mock_resolved.exists.return_value = False
        mock_resolved.suffix = ''

        with patch.object(view_image_tool, '_resolve_path', return_value=mock_resolved) as mock_resolve:
            result = view_image_tool.call(json.dumps({'path': '__something_else'}))

            # Should have called _resolve_path (fell through to normal file handling)
            mock_resolve.assert_called_once_with('__something_else')
            assert isinstance(result, str)
            assert 'not found' in result.lower()

    def test_screen_capture_disabled_env_var(self):
        """With SCREEN_CAPTURE_ENABLED=False env var, both directives return disabled error."""
        from agent_cascade.tools.custom.file_ops import ViewImage
        tool = ViewImage()

        # Test various falsy values for the env var
        for false_value in ['False', 'false', '0', 'no', 'NO']:
            with patch.dict(os.environ, {'SCREEN_CAPTURE_ENABLED': false_value}):
                result = tool.call(json.dumps({'path': '__screen_capture'}))
                assert isinstance(result, str)
                assert 'disabled' in result.lower() or 'ERROR' in result

                result2 = tool.call(json.dumps({'path': '__window_capture:1234'}))
                assert isinstance(result2, str)
                assert 'disabled' in result2.lower() or 'ERROR' in result2

    def test_temp_file_created_for_capture(self, view_image_tool):
        """Verify screen capture temp file is created and result goes through media storage."""
        mock_png_bytes = b'\x89PNG\x0d\x0a...'
        mock_temp_path = 'C:/tmp/capture_view_xyz.png'

        with patch(
            'agent_cascade.tools.custom.screen_capture.capture_screen',
            return_value=mock_png_bytes
        ):
            with patch('tempfile.mkstemp', return_value=(42, mock_temp_path)):
                with patch('os.close'):
                    m = mock_open()
                    with patch('builtins.open', m):
                        # After temp file write, save_image_to_media is called which opens the file via PIL
                        with patch('agent_cascade.tools.custom.file_ops.save_image_to_media') as mock_save:
                            mock_save.return_value = 'N:/work/WD/AgentCascade/logs/media/images/img_20260101_120000_abcd.jpg'
                            result = view_image_tool.call(json.dumps({'path': '__screen_capture'}))

                            # Verify temp file was created for the capture
                            m.assert_called_with(mock_temp_path, 'wb')
                            handle = m.return_value
                            handle.write.assert_called_once_with(mock_png_bytes)

                            # Verify media storage was called with the temp file path
                            mock_save.assert_called_once()
                            call_args = mock_save.call_args
                            passed_source = call_args[1].get('image_source', call_args[0][0] if call_args[0] else '')
                            # Normalize path separators for cross-platform comparison
                            assert 'capture_view_xyz.png' in passed_source.replace('\\', '/')

                            # Verify result contains the media path
                            assert isinstance(result, list)
                            assert len(result) == 2
                            assert 'image' in result[0].__dict__
                            assert 'img_20260101_120000_abcd.jpg' in result[0].image


# ---------------------------------------------------------------------------
# screen_capture Module Tests
# ---------------------------------------------------------------------------


class TestScreenCaptureModule:
    """Tests for capture_screen() and capture_window_by_pid() in screen_capture.py."""

    def test_capture_screen_imagegrab_primary(self):
        """Mock ImageGrab.grab(), verify it's called first."""
        from agent_cascade.tools.custom import screen_capture

        mock_img = MagicMock()
        mock_buf = MagicMock()
        mock_img.save = MagicMock()
        mock_buf.getvalue.return_value = b'\x89PNG'

        # PIL.ImageGrab is not pre-imported by PIL package, so use create=True
        with patch('PIL.ImageGrab', create=True) as mock_grab:
            mock_grab.grab.return_value = mock_img
            with patch.object(screen_capture.io, 'BytesIO', return_value=mock_buf):
                result = screen_capture.capture_screen()

                # ImageGrab should be called first (primary method)
                mock_grab.grab.assert_called_once_with(all_screens=True)
                mock_img.save.assert_called_once()
                assert isinstance(result, bytes)

    def test_capture_screen_mss_fallback(self):
        """When ImageGrab raises OSError, mss is used as fallback."""
        from agent_cascade.tools.custom import screen_capture

        # Set up mss mock chain FIRST (before calling capture_screen)
        mock_mss_instance = MagicMock()
        mock_mss_context = MagicMock(__enter__=MagicMock(return_value=mock_mss_instance),
                                     __exit__=MagicMock(return_value=False))

        mock_screenshot = MagicMock()
        mock_screenshot.size = (1920, 1080)
        mock_screenshot.bgra = b'\x00' * (1920 * 1080 * 4)
        # Monitor 0 is the combined virtual screen
        mock_mss_instance.monitors = [{'left': 0, 'top': 0, 'width': 1920, 'height': 1080}]
        mock_mss_instance.grab.return_value = mock_screenshot

        mock_image_frombytes = MagicMock()
        mock_buf = MagicMock()
        mock_image_frombytes.save = MagicMock()
        mock_buf.getvalue.return_value = b'\x89PNG'

        # Patch ImageGrab to fail, mss module in sys.modules, and Image.frombytes/BytesIO
        with patch('PIL.ImageGrab', create=True) as mock_grab_actual:
            mock_grab_actual.grab.side_effect = OSError("No display found")
            with patch.dict(sys.modules, {'mss': MagicMock(mss=MagicMock(return_value=mock_mss_context))}):
                with patch.object(screen_capture.Image, 'frombytes', return_value=mock_image_frombytes):
                    with patch.object(screen_capture.io, 'BytesIO', return_value=mock_buf):
                        result = screen_capture.capture_screen()

                        # ImageGrab was attempted first
                        mock_grab_actual.grab.assert_called_once_with(all_screens=True)
                        # MSS fallback was used — mss.mss() should have been called
                        import mss as mocked_mss
                        mocked_mss.mss.assert_called_once()
                        assert isinstance(result, bytes)

    def test_capture_screen_per_monitor_skips_imagegrab(self):
        """When monitor_index is set, ImageGrab is skipped and mss is used directly."""
        from agent_cascade.tools.custom import screen_capture

        # Set up mss mock with multiple monitors
        mock_mss_instance = MagicMock()
        mock_mss_context = MagicMock(__enter__=MagicMock(return_value=mock_mss_instance),
                                     __exit__=MagicMock(return_value=False))

        mock_screenshot = MagicMock()
        mock_screenshot.size = (1920, 1080)
        mock_screenshot.bgra = b'\x00' * (1920 * 1080 * 4)
        # Monitor 0=combined, 1=first physical, 2=second physical
        mock_mss_instance.monitors = [
            {'left': 0, 'top': 0, 'width': 3840, 'height': 1080},   # 0: combined
            {'left': 0, 'top': 0, 'width': 1920, 'height': 1080},   # 1: left monitor
            {'left': 1920, 'top': 0, 'width': 1920, 'height': 1080} # 2: right monitor
        ]
        mock_mss_instance.grab.return_value = mock_screenshot

        mock_image_frombytes = MagicMock()
        mock_buf = MagicMock()
        mock_image_frombytes.save = MagicMock()
        mock_buf.getvalue.return_value = b'\x89PNG'

        with patch.dict(sys.modules, {'mss': MagicMock(mss=MagicMock(return_value=mock_mss_context))}):
            with patch.object(screen_capture.Image, 'frombytes', return_value=mock_image_frombytes):
                with patch.object(screen_capture.io, 'BytesIO', return_value=mock_buf):
                    # Test monitor index 1 (first physical monitor)
                    result = screen_capture.capture_screen(monitor_index=1)

                    # ImageGrab should NOT be called when monitor_index is specified
                    import mss as mocked_mss
                    mocked_mss.mss.assert_called_once()
                    # Should grab monitor[1], not monitor[0]
                    mock_mss_instance.grab.assert_called_once_with(mock_mss_instance.monitors[1])
                    assert isinstance(result, bytes)

    def test_capture_screen_per_monitor_out_of_range(self):
        """capture_screen with invalid monitor_index raises ValueError."""
        from agent_cascade.tools.custom import screen_capture

        mock_mss_instance = MagicMock()
        mock_mss_context = MagicMock(__enter__=MagicMock(return_value=mock_mss_instance),
                                     __exit__=MagicMock(return_value=False))
        mock_mss_module = MagicMock(mss=mock_mss_context)

        # Only 2 monitors available (0=combined, 1=physical)
        mock_mss_instance.monitors = [
            {'left': 0, 'top': 0, 'width': 1920, 'height': 1080},
            {'left': 1920, 'top': 0, 'width': 1920, 'height': 1080}
        ]

        with patch.dict(sys.modules, {'mss': mock_mss_module}):
            # Test out-of-range index
            with pytest.raises(ValueError) as exc_info:
                screen_capture.capture_screen(monitor_index=5)
            assert "out of range" in str(exc_info.value).lower()

    def test_capture_window_by_pid_dispatches_windows(self):
        """On win32 platform, calls _capture_window_windows."""
        from agent_cascade.tools.custom import screen_capture

        with patch.object(screen_capture, '_capture_window_windows', return_value=b'png') as mock_win:
            with patch.object(sys, 'platform', 'win32'):
                result = screen_capture.capture_window_by_pid(1234)

                mock_win.assert_called_once_with(1234)
                assert result == b'png'

    def test_capture_window_by_pid_dispatches_linux(self):
        """On linux platform, calls _capture_window_linux."""
        from agent_cascade.tools.custom import screen_capture

        with patch.object(screen_capture, '_capture_window_linux', return_value=b'png') as mock_linux:
            with patch.object(sys, 'platform', 'linux'):
                result = screen_capture.capture_window_by_pid(5678)

                mock_linux.assert_called_once_with(5678)
                assert result == b'png'

    def test_invalid_pid_rejected_in_module(self):
        """Negative/zero/non-int PIDs raise ValueError."""
        from agent_cascade.tools.custom import screen_capture

        invalid_pids = [-1, 0, -5, 'abc', 3.14, None]

        for pid in invalid_pids:
            with pytest.raises(ValueError, match='PID must be a positive integer'):
                screen_capture.capture_window_by_pid(pid)

    def test_capture_screen_import_error_handled(self):
        """When capture_screen() raises ImportError (e.g., PIL missing), error is returned gracefully."""
        from agent_cascade.tools.custom.file_ops import ViewImage

        tool = ViewImage()

        with patch.dict(os.environ, {'SCREEN_CAPTURE_ENABLED': 'True'}):
            with patch(
                'agent_cascade.tools.custom.screen_capture.capture_screen',
                side_effect=ImportError("No module named 'PIL'")
            ):
                result = tool.call(json.dumps({'path': '__screen_capture'}))

                # Should return an error string, not crash
                assert isinstance(result, str)
                assert 'ERROR' in result or 'error' in result.lower()