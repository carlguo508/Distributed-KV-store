import kvstore


def test_package_imports():
    assert kvstore.__doc__.startswith("Distributed key-value store")
