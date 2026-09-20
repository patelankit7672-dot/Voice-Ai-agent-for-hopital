"""
Vercel serverless entrypoint.

Vercel's Python runtime looks for an ASGI application named `app` in the file
a route points at. FastAPI is an ASGI app, so the whole backend is re-exported
here unchanged — there is no second copy of the application.

The project root is added to sys.path because Vercel invokes this file
directly rather than importing the package from the repository root.

READ THIS BEFORE RELYING ON A VERCEL DEPLOYMENT
-----------------------------------------------
Serverless functions are stateless and the deployed bundle is read-only.
Three things behave differently here than on a normal server:

  * Appointments are written to the system temp directory (see
    config._resolve_appointments_path). That storage is EPHEMERAL: it is empty
    after every cold start and is not shared between concurrent instances, so
    a booking can vanish or be invisible to the next request.
  * Staff portal sessions live in process memory, so signing in on one
    instance does not sign you in on another.
  * The rate limiter counts per instance, so the effective limit is higher
    than configured.

Set APPOINTMENTS_PATH to a mounted volume, or move the store to a database,
before treating a deployment as anything other than a demo.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.main import app  # noqa: E402

__all__ = ["app"]
