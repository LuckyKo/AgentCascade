"""Stub — Gradio GUI was removed in the unified branch."""


def __getattr__(name):
    raise ImportError(
        "The Gradio-based GUI was removed. Use start_api_server.py instead.\n"
        "Open http://127.0.0.1:8765 in your browser."
    )