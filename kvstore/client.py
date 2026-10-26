"""Client library: routes each key to its owner and speaks the HTTP API (design.md §5, §6)."""
from __future__ import annotations

import aiohttp

from .config import ClusterConfig, owner_of
from .keycodec import encode_key


class ServerError(Exception):
    """Any non-success response. `status` lets callers tell 400/413/500 apart."""

    def __init__(self, status: int, text: str):
        super().__init__(f"HTTP {status}: {text}")
        self.status = status
        self.text = text


class WrongOwner(ServerError):
    """The server that received the request does not own the key (HTTP 421).

    A separate type because it signals a configuration mismatch between this client's
    cluster.json and the server's, not a problem with the request (design.md §6).
    """


class KVClient:
    # 32 connections per server matches the workload generator spec (design.md §8).
    def __init__(self, config: ClusterConfig, conns_per_server: int = 32, timeout: float = 10.0):
        self.config = config
        self._conns_per_server = conns_per_server
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KVClient":
        # One session with a keep-alive pool for all servers (design.md §2 "HTTP keep-alive
        # pool"): reusing TCP connections avoids a handshake per request. limit=0 removes the
        # global cap so only the per-server limit applies.
        connector = aiohttp.TCPConnector(limit=0, limit_per_host=self._conns_per_server)
        self._session = aiohttp.ClientSession(connector=connector, timeout=self._timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def url_for(self, key: bytes) -> str:
        # design.md §1 Routing: the client picks the owner itself, so a request is one hop
        # with no router process in between.
        s = self.config.servers[owner_of(key, self.config.n)]
        return f"http://{s.host}:{s.port}/keys/{encode_key(key)}"

    async def put(self, key: bytes, value: bytes) -> None:
        async with self._session.put(self.url_for(key), data=value) as r:
            await self._raise_for_status(r)

    async def get(self, key: bytes) -> bytes | None:
        # NotFound maps to None. A present empty value is b"", which callers must check
        # with `is None` to keep the two distinct (requirement F4).
        async with self._session.get(self.url_for(key)) as r:
            if r.status == 404:
                return None
            await self._raise_for_status(r)
            return await r.read()

    async def delete(self, key: bytes) -> None:
        async with self._session.delete(self.url_for(key)) as r:
            await self._raise_for_status(r)

    @staticmethod
    async def _raise_for_status(r: aiohttp.ClientResponse) -> None:
        if r.status == 200:
            return
        text = await r.text()
        if r.status == 421:
            raise WrongOwner(r.status, text)
        raise ServerError(r.status, text)
