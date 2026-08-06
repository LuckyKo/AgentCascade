---
name: httpx-connection-pooling
description: Fix connection reuse issues in HTTP clients using httpx connection pooling best practices
triggers:
  - httpx
  - connection pool
  - connection reuse
  - slow API calls
  - connection timeout
source: auto-skill
---

# HTTPX Connection Pooling

Best practices for configuring and troubleshooting httpx connection pools.

## Key Concepts

- Use `httpx.Client` as a context manager or keep-alive instance for repeated requests
- Configure `limits=Limits(max_connections=..., max_keepalive_connections=...)` for pool sizing
- Reuse clients across async contexts where safe; create per-worker in multi-threaded apps

## Common Issues

1. **Too many open connections**: Increase pool limits or add connection timeouts
2. **Connection resets on reuse**: Check keepalive settings and server-side timeout policies
3. **Slow cold starts**: Pre-warm the pool with a health check request at startup

## Example Configuration

```python
from httpx import AsyncClient, Limits

client = AsyncClient(
    limits=Limits(max_connections=100, max_keepalive_connections=20),
    timeout=30.0,
)
```