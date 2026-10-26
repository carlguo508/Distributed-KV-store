"""Key encoding for the URL path (design.md §5) and size limits (requirement F5)."""
from __future__ import annotations

import base64
import binascii
import re

# requirement F5: key <= 256 B, value <= 64 KiB; larger requests get an explicit error
# (400 for keys, 413 for values, design.md §5).
MAX_KEY = 256
MAX_VALUE = 64 * 1024

# Keys are opaque bytes (requirement F4) but travel in the URL path, so they are
# base64url-encoded (design.md §5). Only the URL-safe alphabet is accepted. A '+' or '/'
# from standard base64 would otherwise be silently misdecoded or split the path.
_B64URL = re.compile(r"^[A-Za-z0-9_-]+=*$")


def encode_key(key: bytes) -> str:
    # Padding is stripped because '=' is noise in a URL path (design.md §5: "without padding").
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def decode_key(text: str) -> bytes:
    """Decode a path segment to the raw key. Every failure is a ValueError, which the
    server maps to 400 (design.md §5)."""
    if not text:
        raise ValueError("empty key")
    if not _B64URL.match(text):
        raise ValueError("key is not base64url")
    # Re-pad to a multiple of 4 so padded and unpadded input decode the same way;
    # the stdlib decoder requires correct padding.
    stripped = text.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        # e.g. a single leftover character ("a") cannot form a byte.
        raise ValueError(f"key is not valid base64url: {exc}") from exc
    # Defensive: the regex already demands one data character, but an empty key must
    # never reach storage (requirement §5 assumptions: empty key is rejected).
    if not key:
        raise ValueError("empty key")
    if len(key) > MAX_KEY:
        raise ValueError(f"key longer than {MAX_KEY} bytes")
    return key
