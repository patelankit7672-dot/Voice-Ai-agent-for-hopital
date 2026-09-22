"""
Shared HTTP client with connection pooling.

WHY
---
Both outbound integrations used to open a brand-new `httpx.AsyncClient` per
call. Every token mint and every sentence of Hindi speech therefore paid for a
fresh DNS lookup, TCP handshake and TLS negotiation to a server on another
continent. Measured against the live AssemblyAI endpoint from India, a token
mint took a median of 3184 ms, and that sat squarely on the path between the
caller pressing Start and the session opening.

A pooled client keeps the connection alive between requests, so everything
after the first pays for the round trip alone. The client is created once at
application startup and closed on shutdown.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("varanasi.http")

# Generous keepalive: these endpoints are called repeatedly during a session,
# and holding a warm connection is the entire point.
_LIMITS = httpx.Limits(
    max_keepalive_connections=20,
    max_connections=50,
    keepalive_expiry=120.0,
)

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """
    The shared pooled client.

    Created lazily so importing this module never opens sockets, which keeps
    it safe to import from tests and from serverless cold starts.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            limits=_LIMITS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            headers={"Accept": "application/json"},
            http2=False,          # h2 is not a dependency; h1 keepalive is enough
        )
        logger.info("Opened pooled HTTP client")
    return _client


async def close_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
        logger.info("Closed pooled HTTP client")
    _client = None


__all__ = ["get_client", "close_client"]
