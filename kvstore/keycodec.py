"""Key encoding for the URL path (design.md §5) and size limits (requirement F5)."""
from __future__ import annotations

import base64
import binascii
import re

MAX_KEY = 256
MAX_VALUE = 64 * 1024

_B64URL = re.compile(r"^[A-Za-z0-9_-]+=*$")


def encode_key(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii").rstrip("=")


def decode_key(text: str) -> bytes:
    if not text:
        raise ValueError("empty key")
    if not _B64URL.match(text):
        raise ValueError("key is not base64url")
    stripped = text.rstrip("=")
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        key = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"key is not valid base64url: {exc}") from exc
    if not key:
        raise ValueError("empty key")
    if len(key) > MAX_KEY:
        raise ValueError(f"key longer than {MAX_KEY} bytes")
    return key
