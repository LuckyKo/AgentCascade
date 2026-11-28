# Secure Remote Control (Operator Runbook)

AgentCascade's REST API and WebSocket server are **secure by default**: they bind to loopback
(`127.0.0.1`) and the auth gate is **inert** in that mode, so local control (Web UI, Telegram
bridge) needs no key and behaves exactly as it always has. This runbook covers how to expose the
server for **remote control** safely.

## Model

- **Inert (default):** loopback bind + no key → the gate passes every request through
  (byte-identical to pre-auth behavior). Nothing to do for local use.
- **Enforced:** a non-loopback bind **or** a configured key → the gate requires a credential on
  mutating routes (and on the WebSocket when bound non-loopback).
- **Fail-closed:** a non-loopback bind with **no** key **refuses to boot**. You cannot
  accidentally expose an unauthenticated server.

## Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `AGENT_CASCADE_HOST` | Bind interface. Loopback values: `127.0.0.1`, `localhost`, `::1`. | `127.0.0.1` |
| `AGENT_CASCADE_ADMIN_API_KEY` | Controller (admin) key — read + mutate. | unset (inert on loopback) |
| `AGENT_CASCADE_VIEWER_API_KEYS` | Comma-separated read-only keys. | unset |

> The names are deliberately **not** `AGENT_CASCADE_API_KEY` — that variable is your LLM provider
> key.

## Credentials on the wire

Remote REST callers send the key via (in priority order):

1. `X-API-Key: <key>` header (recommended)
2. `Authorization: Bearer <key>` header
3. `?api_key=<key>` query param (least preferred)

**Roles:** an **admin** key can read and mutate; a **viewer** key is read-only (mutations →
`403`). **Status codes:** `401` missing/invalid credential, `403` viewer on a mutation, `429`
rate-limited. `GET /api/health` is always open (liveness probe, no auth).

## Step-by-step

1. **Upgrade** to a build with the auth gate. Confirm boot prints the loopback bind
   (`http://127.0.0.1:<port>`) and that a LAN peer's `GET /api/status` now **fails to connect**
   (the old `0.0.0.0` default is gone). Local browser control on the same box is unchanged.
2. **Pick a reachability path** (transport-agnostic — pick one; only the runbook differs, never
   the AC code):
   - **WireGuard (no-vendor, recommended):** self-host a small WG mesh between AC and your
     device(s); keep AC bound to `127.0.0.1` and reach it over the mesh, or bind the WG interface
     directly.
   - **Tailscale:** `tailscale serve` / a localhost-forward so AC never binds non-loopback.
   - **Caddy reverse-proxy:** `reverse_proxy 127.0.0.1:<port>` with auto-TLS; firewall-allow only
     your admin IP.
   - **SSH tunnel:** `ssh -L` a local port to the AC box.
3. **Set the credential** on the AC host:
   ```bash
   export AGENT_CASCADE_ADMIN_API_KEY="$(openssl rand -hex 32)"
   # optional read-only keys:
   export AGENT_CASCADE_VIEWER_API_KEYS="$(openssl rand -hex 32)"
   ```
   If (and only if) you bind a non-loopback/mesh address directly, this is **mandatory** — the
   server refuses to boot without it.
4. **Update remote clients:** send `X-API-Key: <admin key>` on REST calls. The Web UI (browser on
   the same box, or the operator device over the mesh) and the Telegram bridge are **unchanged**
   (session-token path); the browser never holds the admin key.
5. **Verify:**
   ```bash
   curl -H "X-API-Key: <key>" http://<mesh-ip>:<port>/api/status      # -> 200
   curl http://<mesh-ip>:<port>/api/status                            # -> 401
   curl -H "X-API-Key: <viewer>" -X POST http://<mesh-ip>:<port>/api/reset  # -> 403
   ```
6. **Firewall:** allow the AC port only from the mesh range (or the single admin IP for the proxy
   path); no public inbound rule.
7. **Audit:** trigger one admin mutation and one bad-key attempt; confirm both appear under
   `<workspace>/audit/`.

## Treat the admin key like an SSH key

High-entropy, loaded from the environment, never committed to a repo, rotated on suspicion. An
authenticated admin (or a stolen key, or a compromised mesh node) still has full shell access —
this makes remote control *authenticated + encrypted + audited*, **not** safe-by-default. Viewer
keys cannot mutate, which shrinks accidental/low-privilege damage.

## Local default is unchanged

With `AGENT_CASCADE_HOST` unset the server binds `127.0.0.1`, auth is **disabled**, the Web UI
works with zero client change, and the existing test suite passes unchanged. Remote access exists
only when you explicitly add a mesh/proxy + a key.

---

*Design detail, route inventory, and the locked decisions live in
`research/BUG_0435_secure-remote-control-plan.md`.*
