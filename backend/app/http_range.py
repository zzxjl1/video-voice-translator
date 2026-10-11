"""
HTTP `Range` header parsing, shared by every endpoint that serves a file in pieces.

Two routers used to carry their own parser, and both mishandled the suffix form
`bytes=-N` ("the last N bytes") by reading it as "from byte 0". A player that asks
for the tail of an MP4 (to find the `moov` atom) was then handed the wrong bytes.
"""
import re
from typing import Optional

from fastapi import HTTPException

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str, size: int) -> Optional[tuple[int, int]]:
    """
    Inclusive `(start, end)` for a single-range `Range` header, or None to ignore it.

    Handles the three legal single-range shapes — `a-b`, `a-` and the suffix form
    `-n` — and raises 416 for a range that cannot be satisfied. A malformed header
    or a multi-range request returns None, which means "serve the whole file";
    the spec allows ignoring a Range the server does not support.
    """
    match = _RANGE_RE.match((header or "").strip())
    if not match:
        return None
    first, last = match.groups()
    if not first and not last:
        return None

    unsatisfiable = HTTPException(
        status_code=416, detail="Range not satisfiable", headers={"Content-Range": f"bytes */{size}"}
    )
    if not first:                          # suffix: the last n bytes
        length = int(last)
        if length == 0 or size == 0:
            raise unsatisfiable
        return max(0, size - length), size - 1

    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or start > end:
        raise unsatisfiable
    return start, min(end, size - 1)


def iter_file_range(path: str, start: int, length: int, chunk: int = 256 * 1024):
    """Yield `length` bytes of `path` beginning at `start`."""
    with open(path, "rb") as f:
        f.seek(start)
        remaining = length
        while remaining > 0:
            data = f.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data
