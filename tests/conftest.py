"""Pytest configuration for AgentCascade test suite.

Provides test isolation fixtures so tests never touch production config files.
"""

import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_config_dir(tmp_path_factory):
    """Set AGENT_CASCADE_TEST_CONFIG_DIR to a temp directory for the entire test session.

    This ensures all APIRouter instances created during tests use an isolated config
    directory instead of the production project-root/config directory.
    """
    test_config = tmp_path_factory.mktemp("test_config")
    os.environ["AGENT_CASCADE_TEST_CONFIG_DIR"] = str(test_config)
    yield test_config
    # Cleanup not strictly necessary (tmp_path handles it), but explicit is clear
    os.environ.pop("AGENT_CASCADE_TEST_CONFIG_DIR", None)