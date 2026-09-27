#!/bin/bash
# Builds three "v2" fixture images from the REAL localhost:5000/thermoctl-agent:v1
# (same base image, same uid 10002, same Dockerfile.agent -- only the CMD
# differs), pushes them to the local registry and pulls each back by
# digest so the watchdog's own `docker tag <repo>@<digest> ...` resolves:
#
#   v2-success: runs tools/e2e/provision/build-v2-fixtures.py -- writes a
#               health report matching its own desired digest, standing in
#               for the not-yet-built agent/loop.py health-report writer
#               (P5.2). Used by scenarios/c_watchdog_swap.sh.
#   v2-hang:    sleeps forever, never writes a health report, never
#               crashes -- exercises the real 10-minute deadline path.
#               Used by scenarios/d_watchdog_rollback.sh.
#   v2-crash:   identical content to v1 (just a distinct digest via a
#               LABEL) -- the real placeholder `python -m agent` CMD exits
#               1 immediately, so Docker's restart:on-failure hits the
#               "three restarts in a row" rollback path fast. Used by
#               scenarios/d_watchdog_rollback.sh.
#
# Run as root inside the VM (called by the scenario scripts, not directly).
set -euo pipefail
WORK=/tmp/e2e-fixtures
rm -rf "$WORK"
mkdir -p "$WORK"
cp /repo/tools/e2e/provision/build-v2-fixtures.py "$WORK/v2-fixture.py"

cat > "$WORK/Dockerfile.v2-success" <<'EOF'
FROM localhost:5000/thermoctl-agent:v1
COPY v2-fixture.py /app/v2-fixture.py
CMD ["python3", "/app/v2-fixture.py"]
EOF
cat > "$WORK/Dockerfile.v2-hang" <<'EOF'
FROM localhost:5000/thermoctl-agent:v1
CMD ["python3", "-c", "import time; time.sleep(999999)"]
EOF
cat > "$WORK/Dockerfile.v2-crash" <<'EOF'
FROM localhost:5000/thermoctl-agent:v1
LABEL e2e.fixture=v2-crash
EOF

for tag in success hang crash; do
  docker build -f "$WORK/Dockerfile.v2-$tag" -t "localhost:5000/thermoctl-agent:v2-$tag" "$WORK" >&2
  docker push "localhost:5000/thermoctl-agent:v2-$tag" >&2
done
for tag in success hang crash; do
  d=$(docker inspect --format='{{index .RepoDigests 0}}' "localhost:5000/thermoctl-agent:v2-$tag" | cut -d@ -f2)
  docker pull "localhost:5000/thermoctl-agent@${d}" >&2
  echo "v2-$tag=$d"
done
