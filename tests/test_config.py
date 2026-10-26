import hashlib
import json

import pytest

from kvstore.config import ClusterConfig, ServerConfig, load_config, owner_of


def write(tmp_path, servers, max_batch=256):
    p = tmp_path / "cluster.json"
    p.write_text(json.dumps({"servers": servers, "max_batch": max_batch}))
    return p


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


def test_max_batch_defaults_to_256(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"servers": [{"id": 0, "host": "h", "port": 1, "data_dir": "d"}]}))
    assert load_config(p).max_batch == 256


def test_ids_must_match_positions(tmp_path):
    p = write(tmp_path, [{"id": 1, "host": "h", "port": 1, "data_dir": "d"}])
    with pytest.raises(ValueError, match="id"):
        load_config(p)


@pytest.mark.parametrize("n", [0, 5])
def test_server_count_must_be_1_to_4(tmp_path, n):
    servers = [{"id": i, "host": "h", "port": 1 + i, "data_dir": f"d{i}"} for i in range(n)]
    with pytest.raises(ValueError, match="1 and 4"):
        load_config(write(tmp_path, servers))


def test_owner_of_is_md5_mod_n():
    key = b"job-42"
    expected = int(hashlib.md5(key).hexdigest(), 16) % 3
    assert owner_of(key, 3) == expected


def test_owner_of_single_server_is_always_zero():
    for k in (b"", b"a", b"x" * 256):
        assert owner_of(k, 1) == 0


def test_owner_of_covers_all_servers():
    owners = {owner_of(f"k{i}".encode(), 4) for i in range(200)}
    assert owners == {0, 1, 2, 3}
