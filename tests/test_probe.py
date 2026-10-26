import asyncio
import os
import socket

import pytest
from aiohttp import web

from kvstore.probe import NotListening, check
from kvstore.server import create_app
from kvstore.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "kv.db"))
    s.load()
    s.start()
    yield s
    s.close()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Checks: the probe accepts a server whose /info matches the expected pid, id and n
#   (design.md §6 Scripts: start.sh reports ready only for its own process).
# Setup: a real server 1 of 3 in this test process.
# Action: check() with this process's pid, id 1, n 3.
# Expected: None (ready).
async def test_check_accepts_matching_server(aiohttp_server, storage):
    srv = await aiohttp_server(create_app(storage, server_id=1, n_servers=3))
    assert await asyncio.to_thread(check, "127.0.0.1", srv.port, os.getpid(), 1, 3) is None


# Checks: the probe rejects a server from another cluster, e.g. an n=1 server left on the
#   port of an n=3 server.
# Setup: a real server 0 of 1.
# Action: check() expecting this pid, id 0, n 3.
# Expected: a message naming what answered (n=1) and what was expected (n=3).
async def test_check_rejects_server_from_other_cluster(aiohttp_server, storage):
    srv = await aiohttp_server(create_app(storage, server_id=0, n_servers=1))
    msg = await asyncio.to_thread(check, "127.0.0.1", srv.port, os.getpid(), 0, 3)
    assert "n=1" in msg and "n=3" in msg


# Checks: the probe rejects a server that is not the process start.sh launched.
# Setup: a real server 0 of 1 in this test process.
# Action: check() expecting a different pid.
# Expected: a message containing both pids.
async def test_check_rejects_other_pid(aiohttp_server, storage):
    srv = await aiohttp_server(create_app(storage, server_id=0, n_servers=1))
    other = os.getpid() + 100000
    msg = await asyncio.to_thread(check, "127.0.0.1", srv.port, other, 0, 1)
    assert str(os.getpid()) in msg and str(other) in msg


# Checks: a listener without /info (an older kvserver, or not a kvserver) is rejected,
#   not treated as ready.
# Setup: an aiohttp app with no routes.
# Action: check().
# Expected: a message saying it is not an identifiable kvserver.
async def test_check_rejects_listener_without_info(aiohttp_server):
    srv = await aiohttp_server(web.Application())
    msg = await asyncio.to_thread(check, "127.0.0.1", srv.port, os.getpid(), 0, 1)
    assert "/info" in msg


# Checks: nothing listening is "not ready yet", distinct from "wrong server".
# Setup: a free port.
# Action: check().
# Expected: NotListening is raised.
def test_check_raises_when_nothing_listens():
    with pytest.raises(NotListening):
        check("127.0.0.1", free_port(), os.getpid(), 0, 1)
