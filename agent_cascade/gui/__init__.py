"""
Stub module — Gradio-based WebUI was removed in the unified branch.

The custom HTML/JS frontend (served via FastAPI at port 8765) replaced
the old Gradio WebUI. Use start_api_server.py or start_multi_agent.py
to launch the server, then open http://127.0.0.1:8765 in your browser.

This stub exists to give legacy demo/example scripts a clear error message
rather than a cryptic "No module named agent_cascade.gui" ImportError.
"""


class _WebUIRemovedError(ImportError):
    """Raised when any attribute of this module is accessed."""

    def __init__(self):
        msg = (
            "The Gradio-based WebUI has been removed in the AgentCascade unified branch.\n"
            "The custom HTML/JS frontend (served via FastAPI) replaced it.\n\n"
            "To launch the web interface:\n"
            "  python start_api_server.py          # standalone API server\n"
            "  python start_multi_agent.py         # multi-agent entry point\n"
            "Then open http://127.0.0.1:8765 in your browser.\n\n"
            "The legacy demo/example scripts that use WebUI() are no longer supported."
        )
        super().__init__(msg)


def __getattr__(name):
    """Raise informative error for any attribute access (WebUI, gr, mgr, ms, etc.)."""
    raise _WebUIRemovedError()