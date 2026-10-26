"""Cluster configuration shared by servers and clients (design.md §6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MIN_SERVERS, MAX_SERVERS = 1, 4
DEFAULT_MAX_BATCH = 256


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
    raw = json.loads(Path(path).read_text())
    servers = tuple(ServerConfig(**s) for s in raw["servers"])
    if not MIN_SERVERS <= len(servers) <= MAX_SERVERS:
        raise ValueError(f"server count must be between {MIN_SERVERS} and {MAX_SERVERS}, got {len(servers)}")
    for pos, s in enumerate(servers):
        if s.id != pos:
            raise ValueError(f"server at position {pos} has id {s.id}; ids must equal list positions")
    return ClusterConfig(servers=servers, max_batch=int(raw.get("max_batch", DEFAULT_MAX_BATCH)))


def owner_of(key: bytes, n: int) -> int:
    """Index of the server that owns `key`: md5(key) mod n."""
    return int(hashlib.md5(key).hexdigest(), 16) % n
