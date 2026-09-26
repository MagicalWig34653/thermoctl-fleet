"""Makes `tests/` a proper package so `tests.tls_support` (P5.0's shared TLS
test helper, imported from several test modules) resolves to one, unambiguous
module under both `pytest` and `mypy .` -- see `tests/tls_support.py`.
"""
