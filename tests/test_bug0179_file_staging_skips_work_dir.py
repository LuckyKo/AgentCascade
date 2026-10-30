"""BUG_0179: file staging must not copy conversation media into the tool's own work_dir.

The incident: ten ``img_20261024_*.jpg`` files appeared in the *workspace root*
(``N:\\work\\WD\\AgentWorkspace\\``) even though ``media_gen`` saves only to
``logs/media/images/``. Each root file was a byte-identical twin of a media file.

Root cause (not media_gen): for every tool whose ``file_access`` is True,
``fncall_agent._call_tool`` passes ``extract_files_from_messages(messages,
include_images=True)`` to ``BaseToolWithFileAccess.call``, which staged every referenced
file into ``self.work_dir`` via ``save_url_to_local_work_dir``. For local paths that is
``shutil.copy(url, new_path)`` after an unconditional ``os.remove(new_path)``. The only
such tool that is actually used is ``CodeInterpreter``, whose work_dir is
``operation_manager.base_dir`` — the workspace root. So the next ``code_interpreter`` call
after a batch of ``media_gen`` images copied *every* image in the conversation from
``logs/media/images/`` to ``<workspace>/``.

Why the two batches looked like a "media_gen leak":
  - 2026-10-24 09:33:55 — three images (generated 09:27:43, 09:31:41, 09:32:42) land in
    the root exactly when the favicon session's code_interpreter call is dispatched.
  - 2026-10-24 13:30:57 — seven images (13:01:26 .. 13:19:38) land in the root exactly
    when the ComfyUI-RIFE session's code_interpreter call is dispatched.
Creation time == mtime == the batch second, and identical byte sizes, because they are
plain copies made at that instant.

The fix: skip staging for a local file already inside ``work_dir`` — it is readable at
its own path, so the copy adds nothing and (worse) deletes any same-named file already
in work_dir. Remote references (http/https/data:) are still staged exactly as before.

These tests use a minimal ``BaseToolWithFileAccess`` subclass rather than
``CodeInterpreter`` because the real class's ``__init__`` probes Docker.
"""

import sys

import pytest

from agent_cascade.llm.schema import ContentItem, Message
from agent_cascade.tools import base as base_mod
from agent_cascade.tools.base import BaseToolWithFileAccess
from agent_cascade.utils.utils import extract_files_from_messages

MEDIA_NAME = 'img_20261024_092743_4b333d7e.jpg'


class _StagingTool(BaseToolWithFileAccess):
    """Minimal concrete subclass: same staging path as CodeInterpreter, no Docker."""

    name = 'bug0179_staging_tool'
    description = 'test tool'
    parameters = {'type': 'object', 'properties': {}, 'required': []}

    def call(self, params, **kwargs):
        return BaseToolWithFileAccess.call(self, params, **kwargs)


@pytest.fixture
def workspace(tmp_path):
    """A fake workspace laid out like the real one: media lives under logs/media/images."""
    media_dir = tmp_path / 'logs' / 'media' / 'images'
    media_dir.mkdir(parents=True)
    return tmp_path


def _touch(path, payload=b'media-bytes'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _tool(workspace):
    return _StagingTool(cfg={'work_dir': str(workspace)})


# ────────────────────────────────────────────────────────────────────────────
# The leak itself
# ────────────────────────────────────────────────────────────────────────────


class TestMediaIsNotDuplicatedAtWorkspaceRoot:
    def test_media_file_under_work_dir_is_not_copied_to_root(self, workspace):
        src = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        _tool(workspace).call('{}', files=[str(src)])

        assert not (workspace / MEDIA_NAME).exists(), \
            'a media file already under work_dir must not be duplicated at the root'
        assert src.read_bytes() == b'media-bytes'  # source untouched

    def test_existing_root_file_is_not_deleted_or_overwritten(self, workspace):
        """The pre-fix copy did os.remove() on an existing root file — real data loss."""
        src = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        user_file = _touch(workspace / MEDIA_NAME, b'user-authored-content')

        _tool(workspace).call('{}', files=[str(src)])

        assert user_file.read_bytes() == b'user-authored-content', \
            'staging a media file must never overwrite a same-named workspace-root file'

    def test_conversation_images_are_the_staging_input_and_are_skipped(self, workspace):
        """Drive the real chain: message images -> extract_files_from_messages -> staging."""
        src = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        messages = [Message(role='user', content=[ContentItem(image=str(src))])]

        files = extract_files_from_messages(messages, include_images=True)
        assert files == [str(src)], files

        _tool(workspace).call('{}', files=files)

        assert not (workspace / MEDIA_NAME).exists()
        assert sorted(p.name for p in workspace.iterdir()) == ['logs']

    def test_forward_slash_and_file_url_forms_are_recognised_as_inside(self, workspace):
        """Windows paths reach here in several spellings; all must count as 'inside'."""
        src = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        spellings = [
            str(src).replace('\\', '/'),
            'file:///' + str(src).replace('\\', '/').lstrip('/'),
        ]

        for spelling in spellings:
            _tool(workspace).call('{}', files=[spelling])
            assert not (workspace / MEDIA_NAME).exists(), spelling


# ────────────────────────────────────────────────────────────────────────────
# The feature must still work: files from OUTSIDE work_dir are staged
# ────────────────────────────────────────────────────────────────────────────


class TestOutsideFilesAreStillStaged:
    def test_local_file_outside_work_dir_is_copied_in(self, workspace, tmp_path_factory):
        outside = tmp_path_factory.mktemp('outside') / 'chart.png'
        _touch(outside, b'outside-bytes')

        _tool(workspace).call('{}', files=[str(outside)])

        staged = workspace / 'chart.png'
        assert staged.read_bytes() == b'outside-bytes', \
            'the staging feature must keep copying files the tool cannot otherwise reach'

    def test_remote_and_data_urls_still_route_through_the_downloader(self, workspace, monkeypatch):
        calls = []
        monkeypatch.setattr(base_mod, 'save_url_to_local_work_dir',
                            lambda file, work_dir: calls.append(file))

        _tool(workspace).call('{}', files=[
            'https://example.com/a.png',
            'data:image/jpeg;base64,AAAA',
        ])

        assert calls == ['https://example.com/a.png', 'data:image/jpeg;base64,AAAA']

    def test_missing_local_path_still_falls_through_to_the_downloader(self, workspace, monkeypatch):
        """A path that does not exist cannot be 'inside'; keep the old error path."""
        calls = []
        monkeypatch.setattr(base_mod, 'save_url_to_local_work_dir',
                            lambda file, work_dir: calls.append(file))
        missing = str(workspace / 'logs' / 'media' / 'images' / 'nope.jpg')

        _tool(workspace).call('{}', files=[missing])

        assert calls == [missing]


# ────────────────────────────────────────────────────────────────────────────
# Containment helper contracts
# ────────────────────────────────────────────────────────────────────────────


class TestIsInsideWorkDir:
    def test_direct_child_and_nested_child_are_inside(self, workspace):
        child = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        assert base_mod._is_inside_work_dir(str(child), str(workspace))

    def test_sibling_directory_with_common_prefix_is_not_inside(self, tmp_path):
        """'<root>-phase7' shares a string prefix with '<root>' but is a different tree."""
        work = tmp_path / 'workspace'
        work.mkdir()
        sibling = _touch(tmp_path / 'workspace-phase7' / 'x.jpg')

        assert not base_mod._is_inside_work_dir(str(sibling), str(work))

    def test_work_dir_itself_is_not_inside(self, workspace):
        assert not base_mod._is_inside_work_dir(str(workspace), str(workspace))

    def test_pathlib_argument_is_accepted(self, workspace):
        """``files`` is normally str; a Path must not turn the check into a hard error."""
        child = _touch(workspace / 'logs' / 'media' / 'images' / MEDIA_NAME)
        assert base_mod._is_inside_work_dir(child, workspace)
        # And end-to-end: a Path entry is staged/skipped, never raised out of call().
        _tool(workspace).call('{}', files=[child])
        assert not (workspace / MEDIA_NAME).exists()

    @pytest.mark.skipif(sys.platform != 'win32', reason='drive-relative paths are Windows-only')
    def test_other_drive_does_not_raise(self, workspace):
        """os.path.commonpath raises ValueError across drives; that must mean 'not inside'."""
        assert not base_mod._is_inside_work_dir('Z:/somewhere/x.jpg', str(workspace))


# ────────────────────────────────────────────────────────────────────────────
# The coupling that made this a workspace-root leak
# ────────────────────────────────────────────────────────────────────────────


def test_code_interpreter_work_dir_is_the_workspace_root():
    """Pin the reason the copy surfaced at the workspace root (agent_factory choice).

    If this ever changes to a dedicated staging directory the leak disappears for a
    second reason; the assertion documents the contract these tests rely on.
    """
    import inspect

    from agent_cascade.agent_factory import register_standard_tools

    src = inspect.getsource(register_standard_tools)
    assert "code_cfg = {'work_dir': str(om.base_dir)}" in src
