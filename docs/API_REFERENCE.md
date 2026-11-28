# API Reference

This document provides the technical specification for the AgentCascade API, including REST endpoints and the WebSocket protocol.

## 0. Authentication & Encryption

AgentCascade implements a hybrid security model for remote communication.

### X25519 Key Exchange
To securely inject messages, clients must establish a shared secret with the server using a Diffie-Hellman key exchange:
1. Client fetches the server's public key via `/api/keys`. Response includes `public_key` (Base64) and `algorithm` ("X25519").
2. Client generates an X25519 key pair and sends its public key to `/api/handshake`.
3. The server computes the shared secret and returns a `session_token`.

### AES-GCM Encryption
The shared secret derived from the handshake is used to encrypt payloads sent to `/api/message`.
- **Algorithm**: AES-GCM (Authenticated Encryption with Associated Data).
- **Scope**: Only the payload of the `/api/message` endpoint is encrypted.
- **WebSocket**: Messages sent via the WebSocket interface are **not** encrypted by this mechanism.

### Session Tokens
- **Generation**: Created during the `/api/handshake` process.
- **Persistence**: Stored in-memory on the server.
- **Lifecycle**: Tokens are valid until the server restarts.
- **Scope**: Endpoints marked `Y` in the Endpoint Reference require a valid session token (e.g. `/api/message`, `/api/status`, `/api/approve`, `/api/reject`, and the command endpoints).

### API-Key Auth Gate (remote control)
On top of the session-token model, a global middleware gate (`agent_cascade/auth.py`) protects the
server when it is exposed beyond loopback. It is **inert by default**: with a loopback bind
(`AGENT_CASCADE_HOST=127.0.0.1`, the default) and no key configured, every request passes through
unchanged (byte-identical to pre-gate behavior).

- **Enforced mode** is active when the server binds a non-loopback interface **or** a key is set.
- **Fail-closed:** a non-loopback bind with **no** key **refuses to boot**.

| Env var | Purpose |
|---|---|
| `AGENT_CASCADE_HOST` | Bind interface (default `127.0.0.1`). |
| `AGENT_CASCADE_ADMIN_API_KEY` | Controller key (read + mutate). |
| `AGENT_CASCADE_VIEWER_API_KEYS` | Comma-separated read-only keys. |

**Credential on the wire** (priority order): `X-API-Key` header → `Authorization: Bearer <key>` →
`?api_key=` query param.

**Roles & status codes:** an admin key can read and mutate; a viewer key is read-only (mutations →
`403`). `401` = missing/invalid credential, `429` = rate-limited. `GET /api/health` is always open
(liveness probe). The Web UI and Telegram bridge are **unaffected** — they use the loopback
session-token path and never carry the admin key. See
[secure-remote-control.md](secure-remote-control.md) for the operator runbook.

---

## 1. Error Handling

All API errors follow a consistent JSON format:

```json
{
    "message": "Detailed error description"
}
```

### Common HTTP Status Codes
| Code | Meaning | Description |
|------|---------|-------------|
| 400  | Bad Request | Invalid parameters or malformed JSON |
| 401  | Unauthorized | Missing or invalid session token |
| 403  | Forbidden | Operation not permitted |
| 404  | Not Found | Resource not found |
| 500  | Server Error | Internal server failure |

---

## 2. Endpoint Reference

### Group A: Session & Security (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/health` | N | Liveness probe: returns `200` + `{"status":"ok"}`. Always open (no auth). |
| GET | `/api/keys` | N | Returns the server's X25519 public key. |
| POST | `/api/handshake` | N | Performs X25519 handshake; returns `session_token`. |

### Group B: State & Agent Control (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/agents` | N | Lists all active agent instances. |
| GET | `/api/state` | N | Returns a full snapshot of the current system state. |
| POST | `/api/reset` | N | Resets the current agent session. |
| POST | `/api/resume_all` | N | Resumes all halted agents. |
| GET | `/api/sessions` | N | Lists available saved sessions. |

### Group C: Operation Governance (Session Token)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/approve/{request_id}?token=...` | Y | Approves a pending operation request. Token must be provided as a query parameter. |
| POST | `/api/reject/{request_id}?token=...` | Y | Rejects a pending operation request. Token and `reason` are query parameters. |

### Group D: File & Data Management (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/file?path=...` | N | Serves a file from the workspace. |
| POST | `/api/find_file` | N | Searches for files within the workspace. |
| POST | `/api/parse` | N | Parses an uploaded document (`multipart/form-data` with `file` field). |

### Group E: Telemetry (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/telemetry` | N | Returns a summary of system telemetry. |
| GET | `/api/telemetry/export` | N | Downloads the full telemetry log as a JSONL file. |

### Group F: Endpoint Configuration (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/endpoints` | N | Lists all configured API endpoints. |
| POST | `/api/endpoints` | N | Adds a new endpoint configuration. |
| PUT | `/api/endpoints/{id}` | N | Updates an existing endpoint configuration. |
| DELETE | `/api/endpoints/{id}` | N | Deletes an endpoint configuration. |
| POST | `/api/endpoints/priorities` | N | Updates priorities for specific endpoints. |
| POST | `/api/endpoints/bulk` | N | Performs bulk updates to endpoint configurations. |

### Group G: Authenticated Endpoints (Auth-Required)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/message` | Y | Inject an encrypted message. Token must be in the JSON body as `session_token`. |
| GET | `/api/status?token=...` | Y | Returns current state. Token must be provided as a query parameter. |

### Group H: Settings (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/settings` | N | Returns the pool-settings snapshot with a monotonically increasing `revision` (sensitive keys excluded). |
| PATCH | `/api/settings` | N | Applies a per-key settings delta (`{base_revision?, changes:{key:value}}`); returns `{revision, applied, rejected}`. |

### Group I: Server Control (Session Token)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/stop?token=...` | Y | Stops all streaming and sets active agents to IDLE (mirrors WS `stop`). Token must be provided as a query parameter. |
| POST | `/api/restart?token=...` | Y | Restarts the AC server process (destructive). Token must be provided as a query parameter. |
| POST | `/api/auto_security?token=...` | Y | Toggles Auto-Ask Security mode. Body `{enabled}`. Token must be provided as a query parameter. |
| POST | `/api/afk?token=...` | Y | Toggles the server-backed AFK flag. Body `{enabled, message?, mode?}`. Token must be provided as a query parameter. |

### Group J: KV State & Session Restore (Session Token)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/state/save?token=...` | Y | Manually saves KV state for an instance. Body `{instance_name?}`. Token must be provided as a query parameter. |
| POST | `/api/state/load?token=...` | Y | Manually loads KV state for an instance. Body `{instance_name?}`. Token must be provided as a query parameter. |
| POST | `/api/session/restore?token=...` | Y | Restores a named session from its log. Body `{name, mode?}`. Token must be provided as a query parameter. |

### Group K: DNA Cohort Analysis (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/dna_cohort` | N | Read-only DNA-cohort analysis over the live telemetry dir (dry-run, never stages or writes). |
| POST | `/api/dna_cohort` | N | Runs DNA-cohort analysis; body with `stage_cut` + `candidate_content` stages a human-approve-only cut (no auto-commit). |
| POST | `/api/dna_cohort/approve` | N | Human-only apply of a staged DNA cut to the git INDEX. Body `{candidate_hash, commit?}`; commits only when `commit:true`. |

### Group L: MCP Server Management (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/mcp-config` | N | Returns the MCP server config (`config/mcp_servers.json`) verbatim; `{}` if absent. |
| PUT | `/api/mcp-config` | N | Validates and writes the MCP servers map (`{name:{command, args, url, env, enabled?}}`). |
| GET | `/api/mcp-status` | N | Returns per-server status: `[{name, enabled, connected, tool_count}]`. |
| POST | `/api/mcp-toggle` | N | Connects/disconnects one MCP server live and persists its `enabled` flag. Body `{server_name, enabled}`. |

### Group M: Image & Media Generation (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/image_gen` | N | Returns the current image_gen config. |
| POST | `/api/image_gen` | N | Validates and saves the image_gen config. |
| GET | `/api/image_gen/workflows` | N | Lists workflow files from the configured `workflow_dir`. |
| GET | `/api/image_gen/sdcpp_configs` | N | Lists sdcpp model config files from the configured `config_dir`. |
| GET | `/api/media_gen` | N | Returns the full media_gen settings plus a read-only `project_root`. |
| POST | `/api/media_gen` | N | Validates and merge-saves media_gen settings (busts the config cache). |
| GET | `/api/media_gen/manifest` | N | Returns the data-driven backend manifest (`{backends:[...]}`) used to build the UI. |
| GET | `/api/media_gen/workflows` | N | Lists ComfyUI workflow stems grouped by media type (`{image, video, audio}`). |
| GET | `/api/media_gen/sdcpp_configs` | N | Lists sdcpp per-model config stems. |
| GET | `/api/media_gen/tts_configs` | N | Lists TTS voice-config stems. |
| POST | `/api/media/upload` | N | Saves an uploaded image/audio/video (`multipart` `file`) and returns `{path}`. |

> **Security Note**: While most endpoints are unauthenticated, they are accessible to any client with network access to the server. Implement network-level security (firewalls, TLS) for production deployments.

---

## 3. WebSocket Specification

**Endpoint**: `/ws/chat`
**Authentication**: Unauthenticated. No token verification is performed upon connection, and all subsequent WebSocket messages are sent without authentication.

### Client → Server Messages

All messages are JSON objects with a required `type` field. Field requirements below are derived from `agent_cascade/ws_handlers.py`.

#### message
Send a new message to start agent generation.
```json
{
    "type": "message",
    "text": "<required: message content>",
    "target_agent": "<optional: target instance name, defaults to session_name>",
    "agent_index": "<optional: integer index of selected agent>",
    "session_name": "<optional: new session name>",
    "generate_cfg": "<optional: generation configuration object>"
}
```

#### continue
Send current stack as-is to the LLM.
```json
{
    "type": "continue",
    "target_agent": "<optional>",
    "agent_index": "<optional>",
    "session_name": "<optional>",
    "generate_cfg": "<optional>"
}
```

#### stop
Stop all streaming and set active agents to IDLE.
```json
{
    "type": "stop"
}
```

#### pause
Pause ALL running instances (blocks Phase 4 tool execution).
```json
{
    "type": "pause"
}
```

#### resume_all
Resume ALL paused instances.
```json
{
    "type": "resume_all"
}
```

#### resume
Resume a specific agent instance.
```json
{
    "type": "resume",
    "instance_name": "<optional: defaults to session_name>"
}
```

#### terminate_agent_instance / terminate_sub_agent
Forcefully terminate an agent instance. Root orchestrator is protected and transitions to IDLE instead.
```json
{
    "type": "terminate_agent_instance",
    "instance_name": "<required: name of instance to terminate>"
}
```

#### retry
Remove trailing assistant/function messages and resend stack.
```json
{
    "type": "retry",
    "target_agent": "<optional>",
    "agent_index": "<optional>",
    "session_name": "<optional>",
    "generate_cfg": "<optional>"
}
```

#### reset
Clear conversation and reset session.
```json
{
    "type": "reset"
}
```

#### refresh_souls
Reload agent templates from disk and broadcast updated state to all connected clients. Does not return a specific response to the caller — use `refresh_agents` if you need the list of discovered agents back programmatically.
```json
{
    "type": "refresh_souls"
}
```

#### refresh_agents
Reload agent templates from disk and return the list of discovered agents. Unlike `refresh_souls` which only broadcasts state changes, this command returns the agent names via WebSocket so callers (including agents invoking via tool calls) can programmatically determine what's loaded after refresh.

Request:
```json
{
    "type": "refresh_agents"
}
```

Response (sent via send_queue to caller):
```json
{
    "type": "refresh_agents_result",
    "agents": ["orchestrator", "coder", "reviewer", ...]
}
```

On error, the response includes an `error` field instead of agents:
```json
{
    "type": "refresh_agents_result",
    "agents": [],
    "error": "<error message>"
}
```

Both `refresh_souls` and `refresh_agents` also broadcast a full state update to all connected clients after completing.

#### restart_server
Trigger a server restart via os.execl.
```json
{
    "type": "restart_server"
}
```

#### update_config *(deprecated)*
Update LLM or agent configuration. Delegates to ConfigUpdateRouter. **Deprecated** in
favour of `update_settings`: the full-push body is now routed through the settings
registry (unknown keys are dropped, not applied). New clients should send `update_settings`
deltas instead.
```json
{
    "type": "update_config",
    "generate_cfg": "<required: config object with fields like disabled_tools, etc.>"
}
```

#### update_settings
Backend-authoritative per-key settings delta. Each key is validated against the settings
registry (unknown key / type / enum violation → rejected per-key); accepted keys apply to
live state, each touched bucket persists once, and the shared `settings_revision` bumps.
A stale `base_revision` is logged but never rejects the batch (no modal).

Broadcast (to ALL clients): `settings_applied {revision, applied, rejected}`.
Broadcast (to ALL clients): `settings_changed {revision, changes}`.
```json
{
    "type": "update_settings",
    "base_revision": "<optional int: the settings_revision the client last saw>",
    "changes": {"<registry key>": "<value>", "...": "..."}
}
```
REST equivalent: `PATCH /api/settings` with the same body; returns `{revision, applied, rejected}`.

#### migrate_settings_local
One-time migration from browser localStorage to server-side persistence. Sent by the UI on
first connect after upgrade; carries the legacy `agent-cascade-settings` blob. The server
applies ONLY keys it does not already have persisted (server always wins on conflict) and
marks `_settings_migrated_v1` in the session state file.

```json
{
    "type": "migrate_settings_local",
    "settings": {"<key>": "<value>", "...": "..."}
}
```
Reply (to the sender): `migrate_settings_local {status: "ok", applied: [...], rejected: [...]}`.

> **UI-prefs split (Phase 4):** Presentation-only (class D) settings — line numbers, message
> metadata, sound toggles, user/assistant/raw-edit colors, font size, truncate-tools,
> auto-tab-focus, image detail, max image size — are stored in the browser's
> `agent-cascade-ui-prefs` localStorage key and are **never** sent to the server. The legacy
> `agent-cascade-settings` blob is kept read-only for one release (no `removeItem`).
> `GET /api/settings` returns the full server-side settings snapshot (all registry keys);
> it does not include the browser-local ui-prefs keys.

#### set_work_folders
Change workspace directory settings. Empty arrays are treated as intentional clears.
```json
{
    "type": "set_work_folders",
    "work_access_folders_ro": ["<optional: read-only folder paths>"],
    "work_access_folders_rw": ["<optional: read-write folder paths>"]
}
```

#### update_endpoints
Bulk update all endpoints and priorities.
```json
{
    "type": "update_endpoints",
    "endpoints": ["<array of endpoint config objects>"],
    "agent_priorities": {"<agent_type>": ["<endpoint_id priority order>"]}
}
```

#### update_api_priorities
Update agent-type → endpoint priority mappings.
```json
{
    "type": "update_api_priorities",
    "agent_priorities": {"<agent_type>": ["<endpoint_id priority order>"]}
}
```

#### approve
Approve a pending operation request.
```json
{
    "type": "approve",
    "request_id": "<required: ID of request to approve>",
    "automated": "<optional: boolean, true if auto-approved>"
}
```

#### reject
Reject a pending operation request.
```json
{
    "type": "reject",
    "request_id": "<required: ID of request to reject>",
    "reason": "<optional: rejection reason, defaults to 'Rejected by user'>",
    "automated": "<optional: boolean>"
}
```

#### ask_security
Consult the Security advisor for pending tool approvals.
```json
{
    "type": "ask_security"
}
```

#### set_auto_security
Toggle automatic security checks.
```json
{
    "type": "set_auto_security",
    "enabled": "<required: boolean>"
}
```

#### edit_message
Modify the content of a previous message in conversation history.
```json
{
    "type": "edit_message",
    "index": "<required: integer index of message to edit>",
    "content": "<required: new message content>",
    "instance_name": "<optional: defaults to session_name>"
}
```

#### delete_messages
Remove specific messages from history.
```json
{
    "type": "delete_messages",
    "indices": ["<required: array of integer indices to delete>"],
    "instance_name": "<optional: defaults to session_name>"
}
```

#### select_agent
Switch the active agent context.
```json
{
    "type": "select_agent",
    "index": "<required: integer index of agent to select>"
}
```

#### set_session_name
Rename the current session.
```json
{
    "type": "set_session_name",
    "name": "<required: new session name, defaults to 'Maine' if missing>"
}
```

#### load_session
Load a previously saved session from a log file.
```json
{
    "type": "load_session",
    "path": "<required: absolute path to JSONL log file>"
}
```

#### inject
Inject a message into an agent's queue without starting generation.
```json
{
    "type": "inject",
    "text": "<required: message content>",
    "target_agent": "<optional: defaults to session_name>"
}
```

#### dismiss_queue
Clear the pending message queue for an instance.
```json
{
    "type": "dismiss_queue",
    "instance_name": "<optional: defaults to session_name>",
    "message_index": "<required: integer index to remove, or -1 to clear all>"
}
```

#### Agent Tool Restrictions for Dynamically Loaded Agents

When a new agent soul file (`*_soul.md`) is discovered during refresh, it starts with default tool restrictions unless explicit `disabled_tools` configuration is provided. This prevents dynamically loaded agents from having unrestricted access to sensitive operations.

**Tools disabled by default:**
- `shell_cmd` — Execute shell commands on host system
- `code_interpreter` — Run Python code in sandbox
- `write_file`, `edit_file`, `delete_file`, `copy_file`, `re_indent` — File mutations
- `web_search`, `web_extractor` — Network access
- `propose_skill` — Create new reusable skills
- All MCP tools (via `__all_mcp_tools__` sentinel)

**Tools enabled by default:**
- `call_agent`, `dismiss_agent` — Sub-agent coordination (`list_agents` is deprecated; use `system_info(help="agents")`)
- `read_file`, `view_image`, `list_dir`, `grep` — Read-only file operations
- `compress_context`, `forget_last` — Context management
- `system_info`, `read_logs`, `code_map`, `calculate`, `syntax_check` — Information utilities
- `scan_skills`, `load_skill` — Skill access (read-only)

To grant a new agent additional capabilities, set `disabled_tools` explicitly in its soul file's `generate_cfg` or via the UI tool assignment panel. Core system agents (`orchestrator`, `security`, `compressor`) are excluded from default restrictions.

### Server → Client Messages

| Type | Description |
|------|-------------|
| `state` | Full system state snapshot. |
| `done` | Notification that an execution has completed. |
| `error` | Error notification with a descriptive message. |
| `approvals` | Request for operation approval. |

**Note**: Unlike the `/api/message` REST endpoint, WebSocket messages are **not** encrypted via the AES-GCM shared secret.
