"""Cluster configuration shared by servers and clients (design.md §6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

# requirement F9: a static deployment of 1-4 storage servers.
MIN_SERVERS, MAX_SERVERS = 1, 4
# design.md §2: the writer drains up to 256 records into one transaction (group commit).
DEFAULT_MAX_BATCH = 256


# Frozen: the config is read once at startup and must not change while data exists
# (design.md §6), so nothing should be able to mutate it at runtime.
@dataclass(frozen=True)
class ServerConfig:
    id: int
    host: str
    port: int
    data_dir: str


@dataclass(frozen=True)
class ClusterConfig:
    servers: tuple[ServerConfig, ...]
    max_batch: int = DEFAULT_MAX_BATCH

    @property
    def n(self) -> int:
        return len(self.servers)


def load_config(path: str | Path) -> ClusterConfig:
    """Read the one cluster file used by every server and client, so N, ordering and
    addresses cannot drift between them (design.md §6)."""
    raw = json.loads(Path(path).read_text())
    servers = tuple(ServerConfig(**s) for s in raw["servers"])
    if not MIN_SERVERS <= len(servers) <= MAX_SERVERS:
        raise ValueError(f"server count must be between {MIN_SERVERS} and {MAX_SERVERS}, got {len(servers)}")
    # Ownership is computed as an index into the list (owner_of below), and a server
    # checks ownership against its own id (421 WRONG_OWNER, design.md §5). The two only
    # agree if id == list position, so a mismatch is rejected rather than silently
    # misrouting keys.
    for pos, s in enumerate(servers):
        if s.id != pos:
            raise ValueError(f"server at position {pos} has id {s.id}; ids must equal list positions")
    return ClusterConfig(servers=servers, max_batch=int(raw.get("max_batch", DEFAULT_MAX_BATCH)))


def owner_of(key: bytes, n: int) -> int:
    """Index of the server that owns `key`: md5(key) mod n.

    Client-side routing over a static list gives one network hop and no router process
    (design.md §1). md5 is used for its stable, uniform spread, not for security; Python's
    built-in hash() is salted per process, so clients and servers would disagree.
    The result depends on n and the list order, which is why both are fixed (design.md §6).
    """
    return int(hashlib.md5(key).hexdigest(), 16) % n
