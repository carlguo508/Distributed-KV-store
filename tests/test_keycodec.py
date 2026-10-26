import pytest

from kvstore.keycodec import MAX_KEY, MAX_VALUE, decode_key, encode_key


def test_limits_match_requirements():
    assert MAX_KEY == 256
    assert MAX_VALUE == 64 * 1024


def test_encode_is_base64url_without_padding():
    assert encode_key(b"job-1") == "am9iLTE"
    assert "=" not in encode_key(b"\xff\xfe\xfd\xfc\xfb")
    assert "+" not in encode_key(bytes(range(256))) and "/" not in encode_key(bytes(range(256)))


@pytest.mark.parametrize("key", [b"a", b"job-1", b"\x00\xff", bytes(range(256)), b"x" * 256])
def test_roundtrip(key):
    assert decode_key(encode_key(key)) == key


def test_decode_accepts_padded_input():
    assert decode_key("am9iLTE=") == b"job-1"


def test_decode_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        decode_key("")


def test_decode_rejects_non_base64url_characters():
    with pytest.raises(ValueError):
        decode_key("am9i+LTE")
    with pytest.raises(ValueError):
        decode_key("am9i/LTE")
    with pytest.raises(ValueError):
        decode_key("am9i LTE")


def test_decode_rejects_invalid_length():
    with pytest.raises(ValueError):
        decode_key("a")  # one base64 char can never form a byte


def test_decode_rejects_key_over_256_bytes():
    with pytest.raises(ValueError, match="256"):
        decode_key(encode_key(b"x" * 257))
