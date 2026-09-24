"""Alembic migrations for the fleet service's own tables (P1.3).

Ships inside the `fleet` package (not at the repository root) so that
`docker/Dockerfile.fleet`, which only copies `fleet/` into the image, carries
the migrations along -- see `fleet/storage.py::upgrade` for how they are run
without a repository-root `alembic.ini`.
"""
