import kvstore


# Checks: the kvstore package is importable from the test environment (M0 scaffolding).
# Setup: none; the package is imported at module level.
# Action: read the package docstring.
# Expected: it starts with "Distributed key-value store", proving the right package loaded.
def test_package_imports():
    assert kvstore.__doc__.startswith("Distributed key-value store")
