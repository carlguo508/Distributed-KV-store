import pytest

from kvstore.keycodec import MAX_KEY, MAX_VALUE, decode_key, encode_key


# Checks: the size limits are key 256 B and value 64 KiB (requirement F5).
# Setup: none.
# Action: read the two constants.
# Expected: MAX_KEY is 256 and MAX_VALUE is 65536.
def test_limits_match_requirements():
    assert MAX_KEY == 256
    assert MAX_VALUE == 64 * 1024


# Checks: keys are encoded as base64url without padding, safe in a URL path (design.md §5).
# Setup: a known ASCII key, a 5-byte key that would need padding, and all 256 byte values.
# Action: encode each.
# Expected: b"job-1" gives "am9iLTE"; no output contains "=", "+" or "/".
def test_encode_is_base64url_without_padding():
    assert encode_key(b"job-1") == "am9iLTE"
    assert "=" not in encode_key(b"\xff\xfe\xfd\xfc\xfb")
    assert "+" not in encode_key(bytes(range(256))) and "/" not in encode_key(bytes(range(256)))


# Checks: any opaque byte key survives encode then decode unchanged (requirement F4).
# Setup: a 1-byte key, an ASCII key, keys with 0x00/0xff, all 256 byte values, and a 256-byte key.
# Action: decode(encode(key)) for each.
# Expected: the result equals the original key.
@pytest.mark.parametrize("key", [b"a", b"job-1", b"\x00\xff", bytes(range(256)), b"x" * 256])
def test_roundtrip(key):
    assert decode_key(encode_key(key)) == key


# Checks: a client that sends padded base64url is still accepted (design.md §5).
# Setup: the padded encoding "am9iLTE=".
# Action: decode it.
# Expected: b"job-1".
def test_decode_accepts_padded_input():
    assert decode_key("am9iLTE=") == b"job-1"


# Checks: an empty key is rejected (design.md §5: 400 for empty key).
# Setup: an empty string.
# Action: decode it.
# Expected: ValueError mentioning "empty".
def test_decode_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        decode_key("")


# Checks: characters outside the base64url alphabet are rejected, not misdecoded (design.md §5: 400).
# Setup: keys containing "+", "/" and a space.
# Action: decode each.
# Expected: ValueError for each.
def test_decode_rejects_non_base64url_characters():
    with pytest.raises(ValueError):
        decode_key("am9i+LTE")
    with pytest.raises(ValueError):
        decode_key("am9i/LTE")
    with pytest.raises(ValueError):
        decode_key("am9i LTE")


# Checks: a structurally impossible base64 length is rejected (design.md §5: 400).
# Setup: a single character "a".
# Action: decode it.
# Expected: ValueError.
def test_decode_rejects_invalid_length():
    with pytest.raises(ValueError):
        decode_key("a")  # one base64 char can never form a byte


# Checks: a key over 256 bytes after decoding is rejected (requirement F5, design.md §5: 400).
# Setup: the encoding of a 257-byte key.
# Action: decode it.
# Expected: ValueError mentioning "256".
def test_decode_rejects_key_over_256_bytes():
    with pytest.raises(ValueError, match="256"):
        decode_key(encode_key(b"x" * 257))
