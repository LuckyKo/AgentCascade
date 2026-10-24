---
name: uvicorn-port-rebind-restart
description: Diagnose & fix a uvicorn/asyncio server that dies on process restart because the re-launched process can't rebind its port (EADDRINUSE), incl. Windows detached relaunch and isolated-port e2e verification when the live instance hosts your own session.
source: auto-generated
version: 1.1.0
triggers:
  - "uvicorn restart"
  - "port rebind"
  - "EADDRINUSE"
  - "detached relaunch"
  - "server restart e2e verification"
---

## Goal
Reliably diagnose and fix a uvicorn/asyncio ASGI server that dies on restart because the re-launched process can't rebind its port (EADDRINUSE), especially on Windows — and verify the fix end-to-end even when the live instance is off-limits.

## Core gotchas — verify empirically, don't assume
1. **uvicorn's standard `server.run()` does NOT use `Config.bind_socket()`.** With no pre-created sockets, `Server.startup(sockets=None)` calls `loop.create_server(host, port)` — *asyncio* creates the socket. So `SO_REUSEADDR` may be UNSET even though `Config.bind_socket()` (the gunicorn/pre-bind path) sets it. MEASURE it:
   ```python
   import asyncio, socket
   async def t():
       loop = asyncio.get_running_loop()
       srv = await loop.create_server(lambda: None, host='127.0.0.1', port=PORT)
       print(srv.sockets[0].getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR))  # often 0 on Windows
       srv.close(); await srv.wait_closed()
   asyncio.run(t())
   ```
2. **EADDRINUSE surfaces as `SystemExit(1)`, not `OSError`** (uvicorn >=0.34 catches the bind OSError in `startup()` and calls `sys.exit(1)`). So an `except OSError: if errno==98 ...` handler around `server.run()` is DEAD CODE — SystemExit is a BaseException, not caught by `except Exception`. Reproduce what actually escapes:
   ```python
   holder = socket.socket(); holder.bind(('127.0.0.1', PORT)); holder.listen(5)
   server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=PORT))
   try: server.run()
   except SystemExit as e: print('SystemExit', e.code)   # <- this is what escapes, not OSError
   ```
3. **Windows errno is 10048** (WSAEADDRINUSE); POSIX is 98. Match both (or the 'address already in use' string).

## The robust fix — pre-bind + bounded retry + hand-off
Do NOT rely on catching the exception from `server.run()` (fragile: uvicorn has already run lifespan.shutdown() by then). Bind yourself, then hand the socket to uvicorn:
1. `_bind_socket_with_retry(host, port, max_attempts=20, delay=0.5)` (~10s budget):
   - each attempt: `socket.socket(AF_INET, SOCK_STREAM)` -> `setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)` -> `bind((host,port))` -> `listen(...)`.
   - on EADDRINUSE (errno 98/10048 or 'address already in use'): close + warn + `time.sleep(delay)` + retry.
   - on any OTHER OSError: fail immediately (don't retry unrelated errors).
   - after budget exhausted: raise a clear error -> log FATAL + `SystemExit(1)`.
2. Call `server.run(sockets=[sock])` — uvicorn then uses your socket and skips its own bind (and thus its internal sys.exit). Verified to serve cleanly on Windows.

## Restart re-launch (Windows)
- Detached relaunch: `subprocess.Popen([sys.executable, *sys.argv], creationflags=CREATE_NO_WINDOW|DETACHED_PROCESS, close_fds=True, cwd=os.getcwd())` then `os._exit(0)` — ONLY after a successful spawn (if Popen raises, propagate so the running server stays alive).
- **Set the child's std handles explicitly** (see [[windows-detached-child-stdio-silent-failure]]): with no stdin/stdout/stderr given, the detached no-window child inherits the parent's console std handles which are invalid for it -> silent death/hang before binding. Use `stdin=DEVNULL`, `stdout=<log file append>` (try/except OSError -> DEVNULL fallback), `stderr=STDOUT`.
- POSIX: `os.execl(sys.executable, sys.executable, *sys.argv)`.
- Env vars carry over automatically (Popen inherits os.environ; execl preserves env). DETACHED_PROCESS means the child has NO console/stdin — any stdin-reading feature dies after a restart (document it).

## E2E verification when the LIVE instance hosts your own session
A task may say "restart the server on port X and verify." If port X hosts the running orchestrator/session you're operating under, restarting it is SELF-DESTRUCTIVE (kills your context) — the approval layer will reject it. Do NOT force it; prove the SAME code path on an isolated port:
1. **Confirm off-limits first:** `netstat -ano | findstr :<port>` -> PID -> `wmic process where "ProcessId=<pid>" get CommandLine`. If it's your live session, state in the report "e2e on live port not feasible because <reason>; verified on isolated port instead."
2. **Pick a free non-colliding port** (live=8126 -> use 8130). Never reuse the live port.
3. **Launch an isolated instance of the REAL entrypoint** detached with stdout->a log file; wait for it to bind (poll socket connect, bounded deadline).
4. **Exercise the EXACT helper in-process** (e.g. call `restart_server_process()` directly with `sys.argv` set to the launch args) — NOT a mock. The helper's `os._exit(0)` kills that script, so...
5. **Use an external SUPERVISOR process** that `subprocess.run`s the isolated script, then polls until a NEW pid binds the port, records wall-clock timestamps (start -> helper-exit -> new-ready), dumps the captured log tail (should contain `[OK] ... ready!`), and finally `taskkill /F` the relaunched instance so no strays remain. "Came up in ~0.3s" is the evidence that beats "unit tests pass."
6. **Prove cleanup:** `netstat | findstr :<isolated>` -> only TIME_WAIT (no LISTEN); `tasklist` + `wmic ... get CommandLine` for any leftover PID to confirm it's unrelated, not a stray you spawned.

## Tips
- Keep broadcast/"restarting..." notice in each CALLER, not in a shared re-exec helper (callers use different broadcast mechanisms; keeps the helper pure/sync and NoReturn).
- Read `os.name` at call time (not import) so unit tests can monkeypatch the branch.
- A parent-side "sleep before exit" is counterproductive (holds the port longer); fix the race in the CHILD's bind instead.
- Unit-test the retry hermetically: hold a real socket and release it mid-retry (transient success), or never release (gives up clearly). Mock os.execl / Popen / os._exit for the helper branch tests; use `monkeypatch.chdir(tmp_path)` so any log file lands in tmp, not the real CWD.
- **Timestamp gotcha:** `time.strftime('%H:%M:%S.%f')` is INVALID — `%f` is not a strftime directive and raises `ValueError`. Use `t=time.time(); f'{strftime("%H:%M:%S", localtime(t))}.{int((t%1)*1000):03d}'`.
- The isolated-port rebind is a single-variable control (everything identical except the port), so a fast rebind there is valid evidence for the live port.
