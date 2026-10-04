"""
Short-lived, HMAC-signed capability tokens.

Purpose
-------
The browser is used as a compute node, so two endpoints move real bytes:
`GET /api/models/separator/onnx` (64 MB of weights) and
`POST /api/videos/{id}/stems` (tens of MB of audio). Left open, both are
usable by anyone as a free model CDN / free separation backend, and the second
one lets a stranger write files into a video directory.

A token is bound to a specific `video_id` and expires quickly, and issuance
itself requires a `video_id` that already exists — so an abuser has to go
through the normal upload path, and rate limiting caps how many tokens (and
therefore how many downloads) a single client can mint.

**This is an abuse speed bump, not authentication.** There are no user
accounts in this app; anyone who uploads a video can obtain a token for it.
Real protection means adding authentication and per-account quotas.
"""
import base64
import hashlib
import hmac
import logging
import threading
import time
from collections import defaultdict, deque

from app import config

logger = logging.getLogger(__name__)

_TOKEN_VERSION = "v1"

# Issuance is capped per client address in a sliding window.
_recent_issues: dict[str, deque] = defaultdict(deque)
_issue_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str) -> str:
    digest = hmac.new(
        config.SIGNING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _b64e(digest)


# ---------------------------------------------------------------------------
# Issue / verify
# ---------------------------------------------------------------------------

def issue_token(video_id: str, ttl: int | None = None) -> tuple[str, int]:
    """
    Mint a token for one video. Returns (token, ttl_seconds).
    """
    ttl = int(ttl if ttl is not None else config.TOKEN_TTL_SECONDS)
    expiry = int(time.time()) + ttl
    payload = f"{_TOKEN_VERSION}|{video_id}|{expiry}"
    return f"{_b64e(payload.encode('utf-8'))}.{_sign(payload)}", ttl


def _parse(token: str) -> tuple[str, int] | None:
    """
    Validate structure and signature. Returns `(video_id, expiry)` or None.

    Expiry is parsed but not enforced here — callers decide (see
    `video_id_from_token`, which deliberately ignores it).
    """
    if not token or "." not in token:
        return None
    try:
        encoded_payload, encoded_sig = token.split(".", 1)
        payload = _b64d(encoded_payload).decode("utf-8")
        provided_sig = _b64d(encoded_sig)
    except (ValueError, UnicodeDecodeError):
        return None

    expected_sig = _b64d(_sign(payload))
    if not hmac.compare_digest(provided_sig, expected_sig):
        return None

    parts = payload.split("|")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return None
    try:
        expiry = int(parts[2])
    except ValueError:
        return None

    return parts[1], expiry


def verify_token(token: str, video_id: str) -> bool:
    """
    True when the token is well formed, correctly signed, unexpired and bound
    to `video_id`.
    """
    parsed = _parse(token)
    if parsed is None:
        return False

    bound_video_id, expiry = parsed
    if bound_video_id != video_id:
        return False
    return expiry >= time.time()


def video_id_from_token(token: str) -> str | None:
    """
    The video_id a token is bound to, or None when it is malformed or the
    signature does not check out.

    Needed because the model download is issued as
    `GET /api/models/separator/onnx?v=<fingerprint>&token=<token>` — there is no
    `video_id` in the URL, so the binding has to be recovered from the token.
    The payload is covered by the HMAC, so it cannot be forged.

    Expiry is not checked here; `verify_token` does that as part of the normal
    gate.
    """
    parsed = _parse(token)
    return parsed[0] if parsed else None


# ---------------------------------------------------------------------------
# Issuance rate limiting
# ---------------------------------------------------------------------------

def check_issue_rate(client_key: str) -> bool:
    """
    Sliding-window rate limit on token issuance. Returns False when the client
    has exceeded `TOKEN_ISSUE_LIMIT` issues within `TOKEN_ISSUE_WINDOW`.
    """
    limit = config.TOKEN_ISSUE_LIMIT
    window = config.TOKEN_ISSUE_WINDOW
    if limit <= 0:
        return True

    now = time.time()
    with _issue_lock:
        bucket = _recent_issues[client_key]
        while bucket and bucket[0] < now - window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)

        # Keep the map from growing without bound on a long-running process.
        if len(_recent_issues) > 4096:
            for key in [k for k, v in _recent_issues.items() if not v]:
                _recent_issues.pop(key, None)

    return True
