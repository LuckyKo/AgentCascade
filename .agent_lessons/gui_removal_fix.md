# Lessons: GUI Module Removal (Unified Branch)

## What happened
The Gradio-based `WebUI` class from the original Qwen-Agent framework was removed during the unified branch refactoring. The custom HTML/JS frontend (`web_ui/` directory, served via FastAPI at port 8765) replaced it entirely.

However, 29+ files still imported from `agent_cascade.gui`:
- `start_multi_agent.py` — main entry point (fixed to use API server)
- 2 demo files (`demo_soul_webui.py`, `demo_webui.py`)
- 21 example files in `examples/`
- 2 agent_server files (`assistant_server.py`, `workstation_server.py`)
- 3 documentation files

## What was done

### 1. Fixed `start_multi_agent.py`
- Commented out the `from agent_cascade.gui import WebUI` import
- Removed the dead Gradio-specific `chatbot_config` 
- Replaced `WebUI(all_agents, ...).run()` with the FastAPI API server approach:
  ```python
  from agent_cascade.api_server import create_app
  import uvicorn
  app = create_app(all_agents, agent_pool, chatbot_config)
  uvicorn.run(app, host='0.0.0.0', port=8765)
  ```

### 2. Created stub `agent_cascade/gui/` package
Created a stub package that gives clear error messages instead of cryptic ImportErrors:
- `agent_cascade/gui/__init__.py` — raises `_WebUIRemovedError` with guidance
- `agent_cascade/gui/utils.py` — stub for `from agent_cascade.gui.utils import get_avatar_image`
- `agent_cascade/gui/gradio_dep.py` — stub for `from agent_cascade.gui.gradio_dep import gr, mgr, ms`

### 3. Legacy demo/example files left as-is
The 24 legacy demo/example files were NOT modified. They still have their imports and will fail at runtime with the informative error. These files are from the original Qwen-Agent framework and aren't part of the unified architecture — they're reference/demo code that needs a full rewrite to work with the new system.

## Key insight
`start_api_server.py` was already working correctly — it never imported from gui. It uses `create_app()` from `agent_cascade.api_server` which sets up FastAPI + WebSocket routes. The `start_multi_agent.py` entry point now mirrors this approach.