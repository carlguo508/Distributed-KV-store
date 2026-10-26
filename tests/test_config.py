import hashlib
import json

import pytest

from kvstore.config import ClusterConfig, ServerConfig, load_config, owner_of


def write(tmp_path, servers, max_batch=256):
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps({"servers": servers, "max_batch": max_batch}))
    return p


# Checks: cluster.json is parsed into typed server entries (design.md §6).
# Setup: a two-server config file with max_batch=64.
# Action: load it.
# Expected: a ClusterConfig with 2 servers, max_batch 64, and server 1 equal to its JSON entry.
def test_load_config_parses_servers(tmp_path):
    p = write(tmp_path, [
        {"id": 0, "host": "127.0.0.1", "port": 7300, "data_dir": "data/s0"},
        {"id": 1, "host": "127.0.0.1", "port": 7301, "data_dir": "data/s1"},
    ], max_batch=64)
    cfg = load_config(p)
    assert isinstance(cfg, ClusterConfig)
    assert cfg.max_batch == 64
    assert cfg.servers[1] == ServerConfig(id=1, host="127.0.0.1", port=7301, data_dir="data/s1")
    assert len(cfg.servers) == 2


# Checks: the group-commit batch size defaults to 256 (design.md §2, §6).
# Setup: a one-server config file with no max_batch field.
# Action: load it.
# Expected: max_batch is 256.
def test_max_batch_defaults_to_256(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"servers": [{"id": 0, "host": "h", "port": 1, "data_dir": "d"}]}))
    assert load_config(p).max_batch == 256


# Checks: server ids must equal their list position, so routing and owner checks agree (design.md §6).
# Setup: a one-server config whose only server has id 1 instead of 0.
# Action: load it.
# Expected: ValueError mentioning "id".
def test_ids_must_match_positions(tmp_path):
    p = write(tmp_path, [{"id": 1, "host": "h", "port": 1, "data_dir": "d"}])
    with pytest.raises(ValueError, match="id"):
        load_config(p)


# Checks: only 1-4 servers are allowed (requirement F9).
# Setup: a config with 0 servers, then one with 5.
# Action: load each.
# Expected: ValueError mentioning "1 and 4" for both.
@pytest.mark.parametrize("n", [0, 5])
def test_server_count_must_be_1_to_4(tmp_path, n):
    servers = [{"id": i, "host": "h", "port": 1 + i, "data_dir": f"d{i}"} for i in range(n)]
    with pytest.raises(ValueError, match="1 and 4"):
        load_config(write(tmp_path, servers))


# Checks: the owner of a key is md5(key) mod N (design.md §1, §6).
# Setup: the key b"job-42" and N=3.
# Action: compute owner_of and compute md5 mod 3 independently.
# Expected: both give the same index.
def test_owner_of_is_md5_mod_n():
    key = b"job-42"
    expected = int(hashlib.md5(key).hexdigest(), 16) % 3
    assert owner_of(key, 3) == expected


# Checks: N=1 is the degenerate case where one server owns everything (requirement A1).
# Setup: an empty key, a 1-byte key and a 256-byte key.
# Action: compute owner_of with N=1 for each.
# Expected: every owner is 0.
def test_owner_of_single_server_is_always_zero():
    for k in (b"", b"a", b"x" * 256):
        assert owner_of(k, 1) == 0


# Checks: hashing spreads keys over every server, so no server is left without keys (requirement A1).
# Setup: 200 distinct keys and N=4.
# Action: compute the owner of each key.
# Expected: the set of owners is exactly {0, 1, 2, 3}.
def test_owner_of_covers_all_servers():
    owners = {owner_of(f"k{i}".encode(), 4) for i in range(200)}
    assert owners == {0, 1, 2, 3}
