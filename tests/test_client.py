import pytest
from aiohttp.test_utils import TestServer

from kvstore.client import KVClient, ServerError, WrongOwner
from kvstore.config import ClusterConfig, ServerConfig, owner_of
from kvstore.server import create_app
from kvstore.storage import Storage


@pytest.fixture
def storage(tmp_path):
    s = Storage(str(tmp_path / "kv.db"))
    s.load()
    s.start()
    yield s
    s.close()


@pytest.fixture
async def server(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=1))
    await srv.start_server()
    yield srv
    await srv.close()


def config_for(srv, n=1) -> ClusterConfig:
    # Every entry points at the same test server; only entry 0 is the "real" owner.
    return ClusterConfig(servers=tuple(
        ServerConfig(id=i, host=srv.host, port=srv.port, data_dir=f"d{i}") for i in range(n)))


# Checks: the client exposes Put/Get/Delete with NotFound mapped to None (requirement F1-F3).
# Setup: a client for a one-server cluster backed by a real test server.
# Action: put job-1, get it, delete it, get it, delete it again.
# Expected: get returns "running", then None after the delete; the second delete raises nothing.
async def test_put_get_delete_roundtrip(server):
    async with KVClient(config_for(server)) as kv:
        await kv.put(b"job-1", b"running")
        assert await kv.get(b"job-1") == b"running"
        await kv.delete(b"job-1")
        assert await kv.get(b"job-1") is None
        await kv.delete(b"job-1")  # absent delete succeeds


# Checks: an empty value comes back as b"", not None, so it stays distinct from missing (requirement F4).
# Setup: a client for a one-server cluster.
# Action: put k with an empty value, then get k.
# Expected: b"".
async def test_empty_value_roundtrip_is_not_none(server):
    async with KVClient(config_for(server)) as kv:
        await kv.put(b"k", b"")
        assert await kv.get(b"k") == b""


# Checks: the client encodes arbitrary binary keys correctly for the URL (requirement F4, design.md §5).
# Setup: a client for a one-server cluster; a key of all 256 byte values.
# Action: put then get that key.
# Expected: get returns "v".
async def test_binary_key_roundtrip(server):
    async with KVClient(config_for(server)) as kv:
        key = bytes(range(256))
        await kv.put(key, b"v")
        assert await kv.get(key) == b"v"


# Checks: each key is sent to the server md5(key) % N names (design.md §1 Routing, §6).
# Setup: a three-server config with distinct hosts and ports (no network used).
# Action: build the URL for 50 keys.
# Expected: each URL starts with the host:port of that key's owner.
def test_url_routes_to_owner(server):
    cfg = ClusterConfig(servers=(
        ServerConfig(0, "h0", 1, "d0"), ServerConfig(1, "h1", 2, "d1"), ServerConfig(2, "h2", 3, "d2")))
    kv = KVClient(cfg)
    for i in range(50):
        key = f"k{i}".encode()
        owner = cfg.servers[owner_of(key, 3)]
        assert kv.url_for(key).startswith(f"http://{owner.host}:{owner.port}/keys/")


# Checks: a 421 from the server surfaces as a WrongOwner exception (design.md §5).
# Setup: a test server that believes it is server 0 of 2, and a client whose config lists
#   2 servers that both point at it. (The shared `server` fixture is server 0 of 1, which owns
#   every key and can never answer 421, so this test builds its own.)
# Action: put a key whose owner is server 1; the client sends it to entry 1, i.e. server 0.
# Expected: WrongOwner whose message contains "WRONG_OWNER 1".
async def test_wrong_owner_raises(storage):
    srv = TestServer(create_app(storage, server_id=0, n_servers=2))
    await srv.start_server()
    try:
        async with KVClient(config_for(srv, n=2)) as kv:
            key = next(f"k{i}".encode() for i in range(100) if owner_of(f"k{i}".encode(), 2) == 1)
            with pytest.raises(WrongOwner) as exc:
                await kv.put(key, b"v")
            assert "WRONG_OWNER 1" in str(exc.value)
    finally:
        await srv.close()


# Checks: other error statuses surface as ServerError carrying the status (design.md §5: 413).
# Setup: a client for a one-server cluster.
# Action: put a value one byte over 64 KiB.
# Expected: ServerError with status 413.
async def test_oversize_value_raises_server_error_413(server):
    async with KVClient(config_for(server)) as kv:
        with pytest.raises(ServerError) as exc:
            await kv.put(b"k", b"x" * (64 * 1024 + 1))
        assert exc.value.status == 413
