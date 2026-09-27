#!/bin/bash
# `python -m fleet.admin create-user` -- the only way a UI account is ever
# created (project owner decision 2026-09-24, see fleet/admin.py). The
# password is a fixed throwaway string for this disposable test database
# only (never a real credential, never reused, dropped with the VM); the
# printed TOTP secret is test-only too and is never committed anywhere.
set -euo pipefail
printf 'e2eTestPassw0rd!\ne2eTestPassw0rd!\n' | \
  docker exec -i -e FLEET_DATABASE_URL=sqlite:////data/fleet.db thermoctl-e2e-fleet \
  python -m fleet.admin create-user e2e-admin
