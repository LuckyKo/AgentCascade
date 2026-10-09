"""Collection guard for the example/demo tests.

The example modules import heavyweight optional deps at *collection* time —
notably `gradio` (via examples/group_chat_demo.py) — which is NOT in
requirements.txt. A collection-time ImportError aborts the ENTIRE pytest run
before any `-m` marker filter is applied, so a marker cannot fix it. The only
correct remedy is `collect_ignore`: skip collection of the dir when the dep is
absent.

This also fixes a clean dev machine (which lacks gradio), not just CI. When
gradio IS installed the examples are collected normally.

See docs/ci_testing.md. To run them deliberately:
    pip install gradio && pytest tests/examples -p no:cacheprovider
"""
import importlib.util

collect_ignore_glob = [] if importlib.util.find_spec('gradio') else ['*.py']
