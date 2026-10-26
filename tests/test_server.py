import os

import pytest

from kvstore.config import owner_of
from kvstore.keycodec import MAX_VALUE, encode_key
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
async def client(aiohttp_client, storage):
    return await aiohttp_client(create_app(storage, server_id=0, n_servers=1))


def url(key: bytes) -> str:
    return f"/keys/{encode_key(key)}"


# Checks: Get on a missing key returns an explicit NotFound (requirement F2, design.md §5: 404).
# Setup: a single-server app on an empty database.
# Action: GET a key that was never written.
# Expected: status 404 with body "NotFound".
async def test_get_missing_is_404_notfound(client):
    r = await client.get(url(b"nope"))
    assert r.status == 404
    assert await r.text() == "NotFound"


# Checks: Put acks with an empty 200, and Get returns the raw value as octet-stream (design.md §5).
# Setup: a single-server app on an empty database.
# Action: PUT job-1="running", then GET job-1.
# Expected: PUT is 200 with empty body; GET is 200, Content-Type application/octet-stream, body "running".
async def test_put_then_get_returns_value(client):
    r = await client.put(url(b"job-1"), data=b"running")
    assert r.status == 200
    assert await r.read() == b""
    r = await client.get(url(b"job-1"))
    assert r.status == 200
    assert r.headers["Content-Type"] == "application/octet-stream"
    assert await r.read() == b"running"


# Checks: Put fully replaces the old value, with no leftover bytes (requirement F1).
# Setup: a single-server app.
# Action: PUT k to a long value, PUT k to "2", then GET k.
# Expected: the body is exactly "2".
async def test_put_replaces_whole_value(client):
    await client.put(url(b"k"), data=b"first-long-value")
    await client.put(url(b"k"), data=b"2")
    r = await client.get(url(b"k"))
    assert await r.read() == b"2"


# Checks: an empty value is stored and returned as 200 with an empty body, not 404 (requirement F4).
# Setup: a single-server app.
# Action: PUT k with an empty body, then GET k.
# Expected: both are 200 and the GET body is empty.
async def test_empty_value_is_200_with_empty_body(client):
    r = await client.put(url(b"k"), data=b"")
    assert r.status == 200
    r = await client.get(url(b"k"))
    assert r.status == 200
    assert await r.read() == b""


# Checks: Delete makes a key NotFound, and deleting an absent key still succeeds (requirement F3, design.md §5).
# Setup: a single-server app with k=v written.
# Action: DELETE k, GET k, DELETE k again.
# Expected: 200, then 404, then 200.
async def test_delete_then_get_is_404_and_delete_absent_is_200(client):
    await client.put(url(b"k"), data=b"v")
    r = await client.delete(url(b"k"))
    assert r.status == 200
    r = await client.get(url(b"k"))
    assert r.status == 404
    r = await client.delete(url(b"k"))
    assert r.status == 200


# Checks: arbitrary binary keys and values pass through HTTP unchanged (requirement F4).
# Setup: a single-server app; a key of all 256 byte values and a 1 KiB binary value.
# Action: PUT then GET that key.
# Expected: PUT is 200 and GET returns the exact value bytes.
async def test_binary_key_and_value_roundtrip(client):
    key, value = bytes(range(256)), bytes(range(256)) * 4
    assert (await client.put(url(key), data=value)).status == 200
    assert await (await client.get(url(key))).read() == value


# Checks: values up to 64 KiB are accepted and larger ones rejected (requirement F5, design.md §5: 413).
# Setup: a single-server app.
# Action: PUT a value of exactly 65536 bytes, then one of 65537 bytes.
# Expected: 200 for the first, 413 for the second.
async def test_value_at_limit_ok_and_over_limit_413(client):
    assert (await client.put(url(b"k"), data=b"x" * MAX_VALUE)).status == 200
    r = await client.put(url(b"k"), data=b"x" * (MAX_VALUE + 1))
    assert r.status == 413


# Checks: a key that is not valid base64url is rejected (design.md §5: 400).
# Setup: a single-server app.
# Action: GET /keys/not+base64url.
# Expected: 400.
async def test_bad_key_encoding_is_400(client):
    r = await client.get("/keys/not+base64url")
    assert r.status == 400


# Checks: a key longer than 256 bytes is rejected (requirement F5, design.md §5: 400).
# Setup: a single-server app.
# Action: PUT a 257-byte key.
# Expected: 400.
async def test_key_over_256_bytes_is_400(client):
    r = await client.put(url(b"x" * 257), data=b"v")
    assert r.status == 400


# Checks: an empty key is rejected (design.md §5: 400).
# Setup: a single-server app.
# Action: GET /keys/ with nothing after the slash.
# Expected: 400.
async def test_empty_key_path_is_400(client):
    r = await client.get("/keys/")
    assert r.status == 400


# Checks: a server refuses keys it does not own and names the owner (design.md §5: 421 WRONG_OWNER).
# Setup: an app that is server 0 of 3, and a key whose owner is server 2.
# Action: PUT that key, then GET it.
# Expected: both 421; PUT body "WRONG_OWNER 2"; the key is not stored.
async def test_wrong_owner_is_421_with_owner_index(aiohttp_client, storage):
    app = create_app(storage, server_id=0, n_servers=3)
    c = await aiohttp_client(app)
    key = next(f"k{i}".encode() for i in range(100) if owner_of(f"k{i}".encode(), 3) == 2)
    r = await c.put(url(key), data=b"v")
    assert r.status == 421
    assert await r.text() == "WRONG_OWNER 2"
    r = await c.get(url(key))
    assert r.status == 421
    assert key not in storage.data


# Checks: a failed COMMIT becomes a 500 and triggers fail-stop (design.md §1 Failure policy, §5: 500).
# Setup: a storage whose COMMIT always raises, with on_fatal recorded instead of exiting.
# Action: PUT k=v.
# Expected: 500 with "commit failed" in the body, and on_fatal was called.
async def test_commit_failure_is_500(aiohttp_client, tmp_path):
    import sqlite3
    from kvstore.storage import connect

    class FailingCommit:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return self._conn.execute(sql, params)

        def close(self):
            self._conn.close()

    fatal = []
    s = Storage(str(tmp_path / "kv.db"), connect=lambda p: FailingCommit(connect(p)), on_fatal=fatal.append)
    s.load()
    s.start()
    c = await aiohttp_client(create_app(s, server_id=0, n_servers=1))
    r = await c.put(url(b"k"), data=b"v")
    assert r.status == 500
    assert "commit failed" in await r.text()
    assert fatal
    s.close()


# Checks: GET /info identifies the process behind a port, so start.sh and clients can tell
#   their own server from a leftover one (design.md §6 Scripts).
# Setup: an app for server 2 of 3, running in this test process.
# Action: GET /info.
# Expected: 200 JSON {"pid": this process's pid, "id": 2, "n": 3}.
async def test_info_reports_pid_id_and_n(aiohttp_client, storage):
    c = await aiohttp_client(create_app(storage, server_id=2, n_servers=3))
    r = await c.get("/info")
    assert r.status == 200
    assert await r.json() == {"pid": os.getpid(), "id": 2, "n": 3}
