"""
Test that the code interpreter Docker image tag is content-derived from the
Dockerfile + requirements file, so editing either file forces a rebuild for
newly spawned containers.

The pure _compute_docker_image_tag helper tests do NOT require Docker (temp files
are used for hash input, subprocess.run is mocked). The tests that construct
CodeInterpreter DO require a Docker daemon — CodeInterpreter.__init__ calls
_check_docker_availability() unconditionally — so they are marked requires_docker
and skip cleanly on a runner without Docker.
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

TAG_RE = re.compile(r'^code-interpreter:[0-9a-f]{12}$')


class TestComputeDockerImageTag(unittest.TestCase):
    """Tests for the _compute_docker_image_tag helper."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.dockerfile = os.path.join(self.tmpdir, 'code_interpreter_image.dockerfile')
        self.requirements = os.path.join(self.tmpdir, 'code_interpreter_requirements.txt')
        with open(self.dockerfile, 'w') as f:
            f.write('FROM python:3.12.12-slim\nRUN pip install -r code_interpreter_requirements.txt\n')
        with open(self.requirements, 'w') as f:
            f.write('aiohttp\nbeautifulsoup4\nnumpy\n')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tag_format_when_both_files_exist(self):
        """Helper returns code-interpreter:<12-hex> when both inputs exist."""
        from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
        tag = _compute_docker_image_tag(self.dockerfile, self.requirements)
        self.assertRegex(tag, TAG_RE)

    def test_requirements_change_changes_tag(self):
        """Editing the requirements file yields a different tag (core guarantee)."""
        from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
        tag_before = _compute_docker_image_tag(self.dockerfile, self.requirements)
        with open(self.requirements, 'w') as f:
            f.write('aiohttp\nbeautifulsoup4\nnumpy\npandas\n')  # added a line
        tag_after = _compute_docker_image_tag(self.dockerfile, self.requirements)
        self.assertNotEqual(tag_before, tag_after)
        self.assertRegex(tag_after, TAG_RE)

    def test_dockerfile_change_changes_tag(self):
        """Editing the Dockerfile yields a different tag."""
        from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
        tag_before = _compute_docker_image_tag(self.dockerfile, self.requirements)
        with open(self.dockerfile, 'w') as f:
            f.write('FROM python:3.12.13-slim\nRUN pip install -r code_interpreter_requirements.txt\n')
        tag_after = _compute_docker_image_tag(self.dockerfile, self.requirements)
        self.assertNotEqual(tag_before, tag_after)

    def test_tag_is_stable_for_unchanged_files(self):
        """Same file contents → same tag across calls (no import-time caching needed)."""
        from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
        self.assertEqual(_compute_docker_image_tag(self.dockerfile, self.requirements),
                         _compute_docker_image_tag(self.dockerfile, self.requirements))

    def test_missing_files_fall_back_to_default(self):
        """Missing input file(s) → safe default tag, no exception raised."""
        from agent_cascade.tools.code_interpreter import _compute_docker_image_tag
        missing = os.path.join(self.tmpdir, 'does_not_exist.txt')
        self.assertEqual(_compute_docker_image_tag(missing, self.requirements), 'code-interpreter:latest')
        self.assertEqual(_compute_docker_image_tag(self.dockerfile, missing), 'code-interpreter:latest')
        self.assertEqual(_compute_docker_image_tag(missing, missing), 'code-interpreter:latest')


class TestCodeInterpreterImageName(unittest.TestCase):
    """Tests that CodeInterpreter.docker_image_name is content-derived."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @pytest.mark.requires_docker  # CodeInterpreter.__init__ -> _check_docker_availability()
    def test_docker_image_name_is_content_derived(self):
        """docker_image_name matches the helper output for the real resource dir."""
        from agent_cascade.tools.code_interpreter import (CodeInterpreter, DOCKER_IMAGE_FILE,
                                                          DOCKER_REQUIREMENTS_FILE, _compute_docker_image_tag)
        ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})
        if os.path.exists(DOCKER_IMAGE_FILE) and os.path.exists(DOCKER_REQUIREMENTS_FILE):
            self.assertEqual(ci.docker_image_name, _compute_docker_image_tag())
            self.assertNotEqual(ci.docker_image_name, 'code-interpreter:latest')
            self.assertRegex(ci.docker_image_name, TAG_RE)
        else:
            # Resource files absent (test env without repo resources) — fallback tag is acceptable.
            self.assertTrue(ci.docker_image_name.startswith('code-interpreter:'))


class TestBuildDockerImage(unittest.TestCase):
    """Tests for _build_docker_image short-circuit and rebuild behavior (mocked subprocess)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        from agent_cascade.tools.code_interpreter import CodeInterpreter
        self.ci = CodeInterpreter(cfg={'work_dir': self.tmpdir})

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _fake_run(stdout='', returncode=0, stderr=''):
        """Build a subprocess.run replacement returning canned results."""
        class Result:
            pass

        r = Result()
        r.stdout = stdout
        r.stderr = stderr
        r.returncode = returncode
        return mock.Mock(return_value=r)

    @pytest.mark.requires_docker  # CodeInterpreter.__init__ -> _check_docker_availability()
    def test_short_circuits_when_image_exists(self):
        """docker images -q returns non-empty → no docker build call."""
        with mock.patch('subprocess.run', side_effect=self._fake_run(stdout='abc123\n')) as m:
            self.ci._build_docker_image()
        for call in m.call_args_list:
            args = call.args[0] if call.args else call.kwargs.get('args', [])
            self.assertNotIn('build', args)

    @pytest.mark.requires_docker  # CodeInterpreter.__init__ -> _check_docker_availability()
    def test_builds_when_image_missing(self):
        """docker images -q returns empty → docker build is called with the hashed tag."""
        with mock.patch('subprocess.run', side_effect=self._fake_run(stdout='')) as m:
            self.ci._build_docker_image()
        build_calls = [c for c in m.call_args_list if 'build' in (c.args[0] if c.args else [])]
        self.assertEqual(len(build_calls), 1)
        cmd = build_calls[0].args[0]
        # Build command must target the content-hashed tag
        idx = cmd.index(self.ci.docker_image_name)
        self.assertEqual(cmd[idx - 1], '-t')


if __name__ == '__main__':
    unittest.main()
