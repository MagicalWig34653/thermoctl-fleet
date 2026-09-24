"""Individual migration scripts for the fleet service (P1.3).

Kept as a proper Python package (this `__init__.py`) purely so that
`[tool.setuptools.packages.find]` in `pyproject.toml` discovers and installs
it automatically as part of the `fleet` distribution -- Alembic itself does
not require it, it only lists `.py` files in this directory.
"""
