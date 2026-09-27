#!/bin/bash
# DISCREPANCY 0a (image/common/packages.txt, "docker-compose-v2"): that
# package name does not exist in Debian 13 "trixie", and trixie's own
# "docker-compose" package only installs the legacy hyphenated v1 script
# (a `docker-compose` command), never the `docker compose` (space) v2 CLI
# subcommand watchdog/runtime.go actually invokes
# (`execCommand(r.bin, "compose", ...)`). Stock Debian has no package at
# all providing the v2 plugin -- only Docker's own third-party apt repo
# does (docker-compose-plugin), which image/README.md's whole premise ("a
# prepared Debian image") argues against adding as a real fix.
#
# Worked around here, FOR THIS TEST ENVIRONMENT ONLY, by installing the
# official static v2 plugin binary directly. This is not a proposed
# production fix -- the main session decides how
# image/common/packages.txt should actually name this dependency.
set -euo pipefail
mkdir -p /usr/libexec/docker/cli-plugins
curl -fsSL -o /tmp/docker-compose "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-aarch64"
install -m 0755 /tmp/docker-compose /usr/libexec/docker/cli-plugins/docker-compose
docker compose version
