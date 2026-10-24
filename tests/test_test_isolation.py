import sys


def test_settings_module_identity_intact():
    mod = sys.modules['agent_cascade.settings']
    assert mod.__name__ == 'agent_cascade.settings', (
        f"settings.py re-loaded under foreign name {mod.__name__!r} and rebound "
        f"into sys.modules - breaks oai.py's late-import patch target")
    import agent_cascade
    assert agent_cascade.settings is mod
