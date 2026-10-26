"""HTTP API (design.md §5) and server entry point (design.md §6, §7)."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from concurrent.futures import Future

from aiohttp import web

from .config import load_config, owner_of
from .keycodec import MAX_VALUE, decode_key
from .storage import Storage

# Typed app keys (aiohttp's recommended form) instead of plain strings.
STORAGE = web.AppKey("storage", Storage)
SERVER_ID = web.AppKey("server_id", int)
N_SERVERS = web.AppKey("n_servers", int)
# design.md §5: values are raw bytes, so responses are octet-stream.
OCTET_STREAM = "application/octet-stream"


def create_app(storage: Storage, server_id: int, n_servers: int) -> web.Application:
    # client_max_size makes aiohttp itself refuse bodies over 64 KiB with 413
    # (requirement F5), before the whole oversize body is buffered.
    app = web.Application(client_max_size=MAX_VALUE)
    app[STORAGE], app[SERVER_ID], app[N_SERVERS] = storage, server_id, n_servers
    app.add_routes([
        # "/keys/" never matches "/keys/{key}" (the segment would be empty) and would
        # otherwise be a 404, which means "key absent". design.md §5 says an empty key is 400.
        web.route("*", "/keys/", handle_empty_key),
        web.get("/keys/{key}", handle_get),
        web.put("/keys/{key}", handle_put),
        web.delete("/keys/{key}", handle_delete),
        web.get("/info", handle_info),
    ])
    return app


def _key(request: web.Request) -> bytes:
    """Decode the key and check ownership, shared by all three operations (design.md §3)."""
    try:
        key = decode_key(request.match_info["key"])
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"bad key: {exc}")
    # design.md §5: 421 WRONG_OWNER. A misrouted request (e.g. a client with a stale or
    # different cluster.json) is refused, so a key can never end up on two servers.
    owner = owner_of(key, request.app[N_SERVERS])
    if owner != request.app[SERVER_ID]:
        raise web.HTTPMisdirectedRequest(text=f"WRONG_OWNER {owner}")
    return key


async def _await_commit(fut: Future) -> None:
    # The storage future is resolved on the writer thread; wrap_future lets the event loop
    # await it without blocking other requests (design.md §2 handlers).
    try:
        await asyncio.wrap_future(fut)
    except Exception as exc:  # noqa: BLE001 - surfaced as 500; the process is exiting
        raise web.HTTPInternalServerError(text=f"commit failed: {exc}")


async def handle_empty_key(request: web.Request) -> web.Response:
    raise web.HTTPBadRequest(text="bad key: empty key")


async def handle_info(request: web.Request) -> web.Response:
    # Identifies the process behind this port, so start.sh can tell the server it launched
    # from a leftover one (e.g. an n=1 server still holding an n=3 server's port).
    return web.json_response({"pid": os.getpid(), "id": request.app[SERVER_ID],
                              "n": request.app[N_SERVERS]})


async def handle_get(request: web.Request) -> web.Response:
    # design.md §3 Get: a dict lookup only, never SQLite or the writer. The dict holds only
    # committed values, so a Get can never see a write that could vanish on restart.
    value = request.app[STORAGE].data.get(_key(request))
    # `is None`, not falsiness: an empty value b"" is present and must return 200
    # (requirement F4).
    if value is None:
        raise web.HTTPNotFound(text="NotFound")
    return web.Response(body=value, content_type=OCTET_STREAM)


async def handle_put(request: web.Request) -> web.Response:
    key = _key(request)
    value = await request.read()          # aiohttp returns 413 above client_max_size
    # Second check in case a future aiohttp version changes how client_max_size is enforced.
    if len(value) > MAX_VALUE:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_VALUE, actual_size=len(value), text="value too large")
    # requirement F1: the 200 is sent only after the batch containing this write committed
    # with a full fsync and was applied to the dict.
    await _await_commit(request.app[STORAGE].put(key, value))
    return web.Response(status=200)


async def handle_delete(request: web.Request) -> web.Response:
    await _await_commit(request.app[STORAGE].delete(_key(request)))
    return web.Response(status=200)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kvserver")
    parser.add_argument("--config", required=True)
    parser.add_argument("--id", type=int, required=True)
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    me = cfg.servers[args.id]
    os.makedirs(me.data_dir, exist_ok=True)

    # design.md §7: the startup order matters. The port opens only in step 4, so any
    # client that can connect sees a dict that already holds every committed key.
    storage = Storage(os.path.join(me.data_dir, "kv.db"), max_batch=cfg.max_batch)
    storage.load()                        # 1-2: open (WAL replay) and load committed rows
    storage.start()                       # 3: writer thread
    print(f"kvserver {me.id}: loaded {len(storage.data)} keys, listening on {me.host}:{me.port}",
          file=sys.stderr, flush=True)
    # access_log=None: no per-request logging, which would cost CPU on the hot path
    # (design.md §9: server-side latency logging is out of scope).
    web.run_app(create_app(storage, me.id, cfg.n), host=me.host, port=me.port,
                access_log=None, print=None)   # 4: listen only after the dict is complete


if __name__ == "__main__":
    main()
