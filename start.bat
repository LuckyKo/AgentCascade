set AGENT_CASCADE_IDLE_TIMEOUT=900
set AGENT_CASCADE_DEBUG=1
python start_api_server.py --port 8126 2>> logs\server_stderr.log
