#!/bin/bash
# Local registry (registry:2, in the VM) + the agent image, tagged v1,
# pushed and pulled BY DIGEST so it carries a real RepoDigests entry the
# watchdog's `docker tag <repo>@<digest> ...` (runtime.go) can resolve --
# exactly like an image the real agent pulled from ghcr.io by digest
# (section 13) would. Prints the resulting digest on the last stdout line
# (the "shipped" digest, section 22.5's desired == proven).
set -euo pipefail
docker rm -f thermoctl-e2e-registry >/dev/null 2>&1 || true
docker run -d --name thermoctl-e2e-registry --restart=always -p 127.0.0.1:5000:5000 registry:2 >&2
sleep 2
docker build -f /repo/docker/Dockerfile.agent -t localhost:5000/thermoctl-agent:v1 /repo >&2
docker push localhost:5000/thermoctl-agent:v1 >&2
DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' localhost:5000/thermoctl-agent:v1 | cut -d@ -f2)
docker pull "localhost:5000/thermoctl-agent@${DIGEST}" >&2
echo "$DIGEST"
