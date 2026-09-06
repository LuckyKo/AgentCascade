set QWEN_AGENT_IDLE_TIMEOUT=900
set QWEN_AGENT_DEBUG=1
REM DIAGNOSTIC: enable streaming backend probe (writes logs/stream_probe_backend.log).
REM Remove this line after capturing evidence to turn the probe off.
set STREAM_BACKEND_DEBUG=1
python start_api_server.py --port 8126