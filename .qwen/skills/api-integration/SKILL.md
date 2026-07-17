---
name: api-integration
description: REST API interaction patterns including HTTP methods, authentication handling, rate limiting, error retry logic, response parsing, and webhook setup
source: manual
version: "1.0.0"
triggers:
  - "REST API"
  - "HTTP request"
  - "rate limit"
  - "retry logic"
  - "webhook"
  - "authentication"
  - "API client"
---

## Goal

Interact reliably with REST APIs using proper HTTP methods, authentication handling, rate limiting strategies, retry logic with exponential backoff, robust response parsing, and webhook endpoint setup.

## Procedure

### Step 1 — HTTP method patterns

**Standard REST verb usage:**

| Method | Purpose | Idempotent? | Example |
|---|---|---|---|
| `GET` | Retrieve data | Yes | `GET /api/users/123` |
| `POST` | Create resource | No | `POST /api/users {name: "Alice"}` |
| `PUT` | Replace entire resource | Yes | `PUT /api/users/123 {full_object}` |
| `PATCH` | Partial update | No | `PATCH /api/users/123 {name: "Bob"}` |
| `DELETE` | Remove resource | Yes | `DELETE /api/users/123` |

**Python implementation with httpx:**
```python
import httpx

# GET with query parameters
def get_users(client: httpx.Client, page: int = 1, per_page: int = 50) -> list[dict]:
    resp = client.get("/api/users", params={"page": page, "per_page": per_page})
    resp.raise_for_status()
    return resp.json()["data"]

# POST with JSON body and headers
def create_user(client: httpx.Client, name: str, email: str) -> dict:
    resp = client.post(
        "/api/users",
        json={"name": name, "email": email},
        headers={"Idempotency-Key": generate_uuid()},  # Prevent duplicate creation
    )
    resp.raise_for_status()
    return resp.json()

# PUT (full replacement) vs PATCH (partial update)
def update_user(client: httpx.Client, user_id: int, updates: dict) -> dict:
    resp = client.patch(f"/api/users/{user_id}", json=updates)
    resp.raise_for_status()
    return resp.json()

# DELETE with response validation
def delete_user(client: httpx.Client, user_id: int) -> bool:
    resp = client.delete(f"/api/users/{user_id}")
    if resp.status_code == 204:
        return True  # No content — success
    if resp.status_code == 404:
        return False  # Already deleted
    resp.raise_for_status()
```

### Step 2 — Authentication handling

**Common auth patterns:**

```python
import os, time

# Bearer token (most common for REST APIs)
def create_client(base_url: str, api_key: str | None = None) -> httpx.Client:
    token = api_key or os.getenv("API_KEY")
    if not token:
        raise ValueError("API key required — set API_KEY env var or pass api_key parameter")

    return httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )

# OAuth2 token refresh pattern
class TokenManager:
    def __init__(self, client_id: str, client_secret: str, token_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self._token: str | None = None
        self._expires_at: float = 0

    def get_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token  # Still valid (with 60s buffer)

        resp = httpx.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + data.get("expires_in", 3600)
        return self._token

# API key in query parameter (some APIs use this)
def get_with_key(base_url: str, api_key: str, endpoint: str) -> dict:
    resp = httpx.get(f"{base_url}{endpoint}", params={"api_key": api_key})
    return resp.json()
```

### Step 3 — Rate limiting strategies

**Token bucket and fixed-window approaches:**

```python
import time, threading

class RateLimiter:
    """Fixed-window rate limiter for API calls."""

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: list[float] = []
        self._lock = threading.Lock()

    def wait(self):
        """Block until a request slot is available."""
        while True:
            with self._lock:
                now = time.time()
                # Remove requests outside the current window
                self._requests = [t for t in self._requests if now - t < self.window_seconds]
                if len(self._requests) < self.max_requests:
                    self._requests.append(now)
                    return
            time.sleep(0.1)  # Brief pause before retrying

# Usage with httpx transport wrapper
limiter = RateLimiter(max_requests=30, window_seconds=60.0)

def make_request(client: httpx.Client, method: str, url: str, **kwargs) -> dict:
    limiter.wait()  # Respect rate limit before sending
    resp = client.request(method, url, **kwargs)
    return resp.json()

# Handle RateLimit headers (X-RateLimit-Remaining)
def check_rate_limit(resp: httpx.Response):
    remaining = resp.headers.get("X-RateLimit-Remaining")
    if remaining and int(remaining) == 0:
        retry_after = float(resp.headers.get("Retry-After", 1.0))
        time.sleep(retry_after)
```

### Step 4 — Error handling and retry logic

**Exponential backoff with jitter:**

```python
import random, time

def exponential_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
    """Retry decorator with exponential backoff and jitter."""
    def decorator(func):
        import functools
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
                    last_error = e
                    if attempt < max_retries:
                        # Exponential backoff with jitter
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        jitter = random.uniform(0, delay * 0.3)  # Up to 30% jitter
                        time.sleep(delay + jitter)
                    else:
                        break
            raise RuntimeError(f"Failed after {max_retries} retries: {last_error}") from last_error
        return wrapper
    return decorator

# Usage
@exponential_backoff(max_retries=4, base_delay=0.5)
def fetch_data(url: str) -> dict:
    resp = httpx.get(url, timeout=10.0)
    resp.raise_for_status()
    return resp.json()
```

**HTTP error code handling:**

| Status Code | Meaning | Action |
|---|---|---|
| `200` / `201` | Success | Parse response body |
| `204` | No content (e.g., DELETE success) | Treat as success, no body expected |
| `400` | Bad request | Check payload format and required fields |
| `401` | Unauthorized | Refresh token or check credentials |
| `404` | Not found | Verify resource ID or endpoint path |
| `429` | Too many requests | Apply rate limiting, wait for Retry-After header |
| `500–503` | Server error | Retry with backoff (transient failure) |

### Step 5 — Response parsing and validation

```python
from typing import Any

def parse_response(resp: httpx.Response, expected_keys: list[str] | None = None) -> dict:
    """Parse HTTP response with validation."""
    resp.raise_for_status()

    # Handle empty responses (204 No Content)
    if not resp.content:
        return {}

    data = resp.json()

    # Validate expected structure
    if isinstance(data, dict):
        missing = [k for k in (expected_keys or []) if k not in data]
        if missing:
            raise ValueError(f"Response missing keys: {missing}")
    elif isinstance(data, list) and len(data) > 0:
        if expected_keys:
            missing = [k for k in expected_keys if k not in data[0]]
            if missing:
                raise ValueError(f"List items missing keys: {missing}")

    return data

# Paginated response handling
def fetch_all_pages(client: httpx.Client, endpoint: str) -> list[dict]:
    """Fetch all pages of a paginated API."""
    all_items = []
    page = 1

    while True:
        resp = client.get(endpoint, params={"page": page, "per_page": 100})
        data = parse_response(resp)

        items = data.get("data", data if isinstance(data, list) else [])
        all_items.extend(items)

        # Check for more pages
        total_pages = data.get("total_pages")
        has_next = data.get("next_page_url") or (total_pages and page < total_pages)

        if not items or not has_next:
            break
        page += 1

    return all_items
```

### Step 6 — Webhook setup

**Simple webhook endpoint:**

```python
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class WebhookHandler(BaseHTTPRequestHandler):
    SECRET = "your-webhook-secret"

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body)

        # Verify signature if provided
        signature = self.headers.get("X-Signature")
        if not verify_signature(body, signature, self.SECRET):
            self.send_response(401)
            return

        # Process the webhook event
        event_type = data.get("type", "unknown")
        payload = data.get("payload", {})
        print(f"Received {event_type}: {payload}")

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')

# Run webhook server
server = HTTPServer(("0.0.0.0", 8443), WebhookHandler)
print("Webhook listener on port 8443")
server.serve_forever()
```

## Key Configuration Values

| Parameter | Recommended | Why |
|---|---|---|
| `timeout` (httpx) | 10s total, 5s connect | Prevents hanging while allowing slow responses |
| `max_retries` | 3–4 for transient errors | More retries increase latency; fewer risk data loss |
| `base_delay` (backoff) | 0.5–1.0 seconds | Balances responsiveness against server load |
| Rate limit buffer | Stay at 80% of max requests | Avoids hitting limits during traffic spikes |

## What NOT to do

- Do not fire-and-forget API calls — always check response status codes and parse errors
- Do not retry on `4xx` client errors (except `429`) — they won't succeed with the same request
- Do not store API keys in source code — use environment variables or config files
- Do not assume JSON responses without checking `Content-Type` header
- Do not block indefinitely on rate limits — always have a timeout and fallback strategy