"""The vLLM emulator behind a bearer-token check.

The harness mounts this file as a ConfigMap and starts it in place of the
emulator's own entry point (`uvicorn auth_emulator:app`), so a token can be
required without rebuilding the emulator image.

Every path except `/health` and `/__auth/state` needs
`Authorization: Bearer $EMULATOR_API_KEY`. `/health` stays open so the kubelet
probe can reach the server before anyone has a token, and `/__auth/state` stays
open so a test can tell a token that arrived from one that did not: it reports
the fingerprints of the tokens that were presented, never the tokens themselves.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sys

from fastapi.responses import JSONResponse

# The emulator's own sources are in /app, its image's WORKDIR. sys.path[0] is the
# working directory, so this is already true today; inserting it keeps the import
# working if the pod ever starts from somewhere else.
sys.path.insert(0, "/app")

from vllm_emulator import app  # noqa: E402  (imported after the path change)

API_KEY = os.environ.get("EMULATOR_API_KEY", "")
if not API_KEY:
    raise SystemExit("EMULATOR_API_KEY must be set; this wrapper exists to require a token")

BEARER_SCHEME = "bearer"
OPEN_PATHS = frozenset({"/health", "/__auth/state"})
MAX_FINGERPRINTS = 10

_state = {
    "authenticated_requests": 0,
    "rejected_requests": 0,
    "last_authenticated_path": None,
    "last_rejected_reason": None,
    "presented_fingerprints": [],
}


def _fingerprint(value: str) -> str:
    """Short, stable stand-in for a token, so logs and state never carry it."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _remember_presented(fingerprint: str) -> None:
    presented = _state["presented_fingerprints"]
    if fingerprint in presented:
        return
    presented.insert(0, fingerprint)
    del presented[MAX_FINGERPRINTS:]


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
        content={
            "error": {
                "message": f"missing or invalid bearer token ({reason})",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
                "param": None,
            }
        },
    )


@app.get("/__auth/state")
def auth_state() -> dict:
    """What the token check has seen, without any token value."""
    return {
        "authenticated_requests": _state["authenticated_requests"],
        "rejected_requests": _state["rejected_requests"],
        "last_authenticated_path": _state["last_authenticated_path"],
        "last_rejected_reason": _state["last_rejected_reason"],
        "presented_fingerprints": list(_state["presented_fingerprints"]),
    }


@app.middleware("http")
async def require_bearer_token(request, call_next):
    """Reject anything under `/v1` that does not carry the configured token."""
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/__auth/"):
        return await call_next(request)

    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != BEARER_SCHEME or not presented:
        _state["rejected_requests"] += 1
        _state["last_rejected_reason"] = "missing"
        return _unauthorized("missing")

    _remember_presented(_fingerprint(presented))
    if not secrets.compare_digest(presented, API_KEY):
        _state["rejected_requests"] += 1
        _state["last_rejected_reason"] = "invalid"
        return _unauthorized("invalid")

    _state["authenticated_requests"] += 1
    _state["last_authenticated_path"] = path
    return await call_next(request)
