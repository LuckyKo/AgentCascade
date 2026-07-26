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
- **Scope**: Only `/api/message` and `/api/status` require a valid session token.

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

### Group C: Operation Governance (Auth-Free)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/approve/{request_id}` | N | Approves a pending operation request. |
| POST | `/api/reject/{request_id}` | N | Rejects a pending operation request. |

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

> **Security Note**: While most endpoints are unauthenticated, they are accessible to any client with network access to the server. Implement network-level security (firewalls, TLS) for production deployments.

---

## 3. WebSocket Specification

**Endpoint**: `/ws/chat`
**Authentication**: Unauthenticated. No token verification is performed upon connection, and all subsequent WebSocket messages are sent without authentication.

### Client $\rightarrow$ Server Messages
Messages are sent as JSON objects with a `type` field.

| Type | Description |
|------|-------------|
| `message` | Send a standard message to an agent. |
| `stop` | Stop the current agent execution. |
| `retry` | Retry the last operation. |
| `reset` | Reset the session. |
| `approve` | Approve a pending operation. |
| `reject` | Reject a pending operation. |
| `edit_message` | Modify the content of a previous message. |
| `delete_messages` | Remove specific messages from history. |
| `select_agent` | Switch the active agent context. |
| `set_session_name` | Rename the current session. |
| `inject` | Inject a message into the history. |
| `continue` | Continue a paused execution. |
| `pause` | Pause the current execution. |
| `resume_all` | Resume all halted agents. |
| `resume` | Resume a specific agent. |
| `terminate_agent_instance` | Forcefully terminate an agent instance. |
| `terminate_sub_agent` | Forcefully terminate a sub-agent. |
| `refresh_souls` | Reload agent configurations. |
| `restart_server` | Trigger a server restart. |
| `update_config` | Update LLM or agent configuration. |
| `set_work_folders` | Change workspace directory settings. |
| `update_endpoints` | Modify API endpoint configurations. |
| `update_api_priorities` | Update endpoint priority levels. |
| `ask_security` | Consult the security advisor. |
| `set_auto_security` | Toggle automatic security checks. |
| `load_session` | Load a previously saved session. |
| `dismiss_queue` | Clear the pending message queue. |

### Server $\rightarrow$ Client Messages
| Type | Description |
|------|-------------|
| `state` | Full system state snapshot. |
| `done` | Notification that an execution has completed. |
| `error` | Error notification with a descriptive message. |
| `approvals` | Request for operation approval. |

**Note**: Unlike the `/api/message` REST endpoint, WebSocket messages are **not** encrypted via the AES-GCM shared secret.