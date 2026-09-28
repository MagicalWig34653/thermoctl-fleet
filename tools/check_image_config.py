"""Checks that the image configuration under `image/` is plausible.

**Not a buildability check** in the sense of an actual image build. A full
pi-gen or mkosi/debos run takes 30-60 minutes and, per the task, does not belong
in every commit (see `image/README.md`, "State of this scaffold"). This tool
instead only reads the configuration files and checks them for obvious errors --
an empty or duplicated package list, a missing udev rule, a template for
`agent-registration.json` whose fields no longer match
`protocol.registration.AgentRegistrationFile`. Called by
`.github/workflows/image.yml` on every run and directly by
`tests/test_image_config.py`.

Model: thermoctl's `tools/env_nach_addon.py` and the Home-Assistant-add-on-side
`pruefe-konfiguration.py` (see thermoctl/CLAUDE.md) -- the same role for this
repository: a fast check that runs both locally and in CI, no substitute for an
actual build.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from protocol.registration import AgentRegistrationFile

IMAGE_DIR = Path(__file__).resolve().parent.parent / "image"

# Debian package names: lowercase letters, digits, +, -, . -- see Debian Policy
# §5.6.7. No spaces, no version spec (see packages.txt).
_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


class ImageError(Exception):
    """A configuration file under `image/` is not plausible."""


def read_package_list(path: Path) -> list[str]:
    """Reads a package list, without comment and blank lines."""

    lines = path.read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


def check_package_list(path: Path) -> list[str]:
    """Checks the shared package list (section 19.3) and returns it.

    Raises `ImageError` if the list is empty, a package name does not look like a
    Debian package name, or a name appears more than once.
    """

    packages = read_package_list(path)
    if not packages:
        raise ImageError(f"{path}: contains not a single package.")

    invalid = [p for p in packages if not _PACKAGE_NAME_PATTERN.match(p)]
    if invalid:
        raise ImageError(f"{path}: does not look like a Debian package name: {invalid!r}.")

    duplicates = {p for p in packages if packages.count(p) > 1}
    if duplicates:
        raise ImageError(f"{path}: package(s) listed twice: {sorted(duplicates)!r}.")

    return packages


# The four packages that actually provide "docker compose" (the v2 CLI
# subcommand watchdog/runtime.go invokes) and the container runtime it
# swaps -- from Docker's own apt repository (image/common/apt/), per the
# project owner's 2026-09-27 decision, not from Debian. Checked in one
# place so a future edit of packages.txt can't silently drop one of the
# four without a test failing.
_DOCKER_REPO_PACKAGES = frozenset(
    {"docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"}
)

# Names that must never reappear in packages.txt: docker.io is Debian's own
# (obsolete for this purpose) Docker package, and docker-compose-v2 never
# existed as a Debian 13 "trixie" package at all (the P5.E finding this
# fix responds to, docs/STATUS.md).
_FORBIDDEN_DOCKER_PACKAGES = frozenset({"docker.io", "docker-compose-v2"})


def check_docker_packages_from_official_repo(packages: list[str]) -> None:
    """Checks that `packages.txt` names the Docker-repo packages (section 19,
    P5.E fix) instead of Debian's own `docker.io`/`docker-compose-v2` --
    the latter either only provides the legacy v1 script or, for
    `docker-compose-v2`, does not exist in Debian 13 "trixie" at all
    (`docs/STATUS.md`, P5.E). Does not by itself check *where* apt would
    install these four packages from -- that is `check_docker_apt_source`
    and `check_docker_apt_preferences` below.
    """

    missing = sorted(_DOCKER_REPO_PACKAGES - set(packages))
    if missing:
        raise ImageError(
            f"packages.txt: missing Docker-repo package(s) {missing!r} "
            f"(image/common/apt/, section 19)."
        )

    present_forbidden = sorted(_FORBIDDEN_DOCKER_PACKAGES & set(packages))
    if present_forbidden:
        raise ImageError(
            f"packages.txt: forbidden package(s) still present {present_forbidden!r} -- "
            f"docker.io is Debian's own package and docker-compose-v2 does not exist "
            f"in Debian 13 'trixie' at all (docs/STATUS.md, P5.E)."
        )


# Docker's published release key fingerprint, pinned here so a change to it
# in image/common/apt/fetch-docker-key.sh cannot silently drift from what
# this check expects -- see that script's own comment for how to verify it
# against Docker's documentation before ever changing this value.
_DOCKER_KEY_FINGERPRINT = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"


def check_docker_apt_source(path: Path) -> None:
    """Checks the deb822 repository definition for Docker's official apt
    repository (image/common/apt/docker.sources).

    Asserts the file exists, is a deb822 stanza naming Docker's repository,
    and uses `Signed-By:` (a keyring file, not a system-wide `apt-key add`,
    which would trust the key for every repository on the system) rather
    than being unsigned.
    """

    if not path.is_file():
        raise ImageError(f"{path}: Docker apt repository definition is missing.")
    content = path.read_text(encoding="utf-8")
    required = [
        "URIs: https://download.docker.com/linux/debian",
        "Suites: trixie",
        "Signed-By:",
    ]
    missing = [line for line in required if line not in content]
    if missing:
        raise ImageError(f"{path}: missing required field(s): {missing!r}.")


def check_docker_apt_preferences(path: Path) -> None:
    """Checks the apt pinning file that restricts Docker's official apt
    repository to exactly the four packages it is meant to provide
    (`_DOCKER_REPO_PACKAGES`), not arbitrary packages that happen to share
    a name with something published there.

    Cross-review finding: a low-but-*positive* priority for "everything
    else from this origin" (e.g. 1) still lets apt install a package that
    exists ONLY at that origin -- exactly what would happen for docker-ce's
    own Recommends (`docker-ce-rootless-extras`, `docker-buildx-plugin`) if
    they were ever pulled in. Only a **negative** priority
    (`apt_preferences(5)`: "never installed") actually forecloses that, so
    this asserts the wildcard stanza is pinned to exactly `-1`, not merely
    "some low number" -- and that nothing is left at the old, too-permissive
    `1`.
    """

    if not path.is_file():
        raise ImageError(f"{path}: Docker apt preferences file is missing.")
    content = path.read_text(encoding="utf-8")
    if 'origin "download.docker.com"' not in content:
        raise ImageError(f"{path}: does not pin origin \"download.docker.com\".")

    priorities = [
        line.split(":", 1)[1].strip()
        for line in content.splitlines()
        if line.strip().startswith("Pin-Priority:")
    ]
    if "-1" not in priorities:
        raise ImageError(
            f"{path}: does not pin everything else from this origin to -1 -- a "
            f"low-but-positive priority would still let apt install a "
            f"Docker-repo-only package (e.g. docker-buildx-plugin via Recommends)."
        )
    if "600" not in priorities:
        raise ImageError(f"{path}: does not pin the named Docker-repo packages to 600.")
    if "1" in priorities:
        raise ImageError(
            f"{path}: still pins something at priority 1 -- too permissive, must be -1."
        )

    missing_packages = [pkg for pkg in _DOCKER_REPO_PACKAGES if pkg not in content]
    if missing_packages:
        raise ImageError(
            f"{path}: does not name Docker-repo package(s) {sorted(missing_packages)!r}."
        )


def check_docker_key_fetch(path: Path) -> None:
    """Checks the documented, verified fetch step for Docker's apt signing
    key (image/common/apt/fetch-docker-key.sh) -- the keyring itself is not
    committed to this repository (a stale or silently-replaced binary blob
    is a worse failure mode than a fetch step that fails loudly), so this
    script has to actually verify what it fetches.

    Asserts the pinned fingerprint (`_DOCKER_KEY_FINGERPRINT`) appears in
    the script, and that the script both fetches a key from Docker's
    repository and refuses to proceed on a mismatch -- not just that a
    fingerprint-looking string is quoted somewhere for documentation.

    Cross-review finding: also asserts the script actually rejects a
    download containing more than one primary key. Comparing only the
    *first* fingerprint in the download is not enough -- a file can validly
    contain several concatenated OpenPGP key blocks, and the whole raw file
    still gets installed as the keyring even if only the first key's
    fingerprint was ever checked, letting an unverified second key ride
    along and be trusted by apt for this repository too.
    `tests/test_fetch_docker_key.py` exercises this behaviourally (with a
    real `gpg`, if installed); this only checks the script's own source for
    the "exactly one primary key" enforcement so a regression can't drop it
    silently.
    """

    if not path.is_file():
        raise ImageError(f"{path}: Docker apt key fetch script is missing.")
    content = path.read_text(encoding="utf-8")
    if _DOCKER_KEY_FINGERPRINT not in content:
        raise ImageError(
            f"{path}: pinned fingerprint {_DOCKER_KEY_FINGERPRINT!r} not found."
        )
    if "download.docker.com/linux/debian/gpg" not in content:
        raise ImageError(f"{path}: does not fetch Docker's published key.")
    if "exit 1" not in content:
        raise ImageError(f"{path}: does not appear to refuse a fingerprint mismatch.")
    if "PUB_COUNT" not in content or "pub:" not in content:
        raise ImageError(
            f"{path}: does not appear to reject a download containing more than "
            f"one primary key (no primary-key count check found)."
        )


def check_udev_rule(path: Path) -> None:
    """Checks that the Zigbee stick rule contains at least one real rule line."""

    lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not any("SUBSYSTEM" in line for line in lines):
        raise ImageError(f"{path}: contains no SUBSYSTEM rule line.")


def check_agent_registration_template(path: Path) -> None:
    """Checks that the empty template carries exactly the fields of
    `AgentRegistrationFile`.

    The actual purpose: a drift between `protocol/registration.py` and this
    template shows up here, instead of only at the preparation tool (section
    19.5) that fills the template at runtime.
    """

    content = json.loads(path.read_text(encoding="utf-8"))
    expected_fields = set(AgentRegistrationFile.model_fields)
    actual_fields = set(content)
    if actual_fields != expected_fields:
        raise ImageError(
            f"{path}: fields {sorted(actual_fields)!r} do not match "
            f"protocol.registration.AgentRegistrationFile {sorted(expected_fields)!r}."
        )


def check_watchdog_unit(path: Path) -> None:
    """Checks that the systemd unit reused by both images exists."""

    if not path.is_file():
        raise ImageError(f"{path}: watchdog unit is missing.")


def check_leds_unit(path: Path) -> None:
    """Checks that the status-LED program's systemd unit exists (P5.7,
    section 23, "Decided afterward") -- same reasoning and same shape as
    `check_watchdog_unit` above, for the separate program's own unit."""

    if not path.is_file():
        raise ImageError(f"{path}: thermoctl-leds unit is missing.")


def check_agent_compose_file(path: Path) -> None:
    """Checks the fixed compose file the watchdog re-applies on every swap
    (P5.6, cross-review R5) -- not a real YAML parse (no third-party
    dependency needed for a plausibility check), just the handful of plain
    substrings that would silently defeat R1/R5 if lost: never pulling from
    a registry, never fighting the watchdog's own restart-policy semantics,
    and referencing the exact fixed tag `watchdog/runtime.go`'s Start
    always tags to before re-applying this file.

    **P5.7 hot-fix, cross-review finding:** also asserts the two mounts the
    agent's atomically-replaced files (state file, health report, the new
    status-LED file) depend on are **directories**, not individual files --
    a single-file bind mount cannot be replaced by the temp-file-plus-
    rename pattern `agent/loop.py` uses throughout (rename only succeeds
    within the filesystem/directory it started in; see this file's own
    comment and `docs/STATUS.md`'s P5.7 hot-fix entry for the full
    account). Checked both ways: the two directory-mount lines must be
    present, and the old single-file mount lines this bug shipped with
    must **not** be -- a regression back to file-level mounts would
    otherwise pass every other check here unnoticed.

    **P5.7 hot-fix, round 2:** also asserts `group_add` puts the agent
    into the host's Docker group via `${DOCKER_GID:?...}` -- without it,
    the agent (an unprivileged uid, 10002, not root) can have the socket
    bind-mounted into its container and still be refused by the daemon at
    the far end of it, since the socket itself is owned `root:docker
    0660` on the host.

    **P5.5a:** also asserts the three read-only mounts backups need are
    present -- the boot-partition recipients file
    (`agent/encryption.py`'s own documented path,
    `/boot/firmware/thermoctl`), thermoctl's own data directory, and
    Zigbee2MQTT's data directory. All three carry an explicit `:ro` suffix
    in the required line itself, so a future edit that widened one of them
    to read-write would fail this check immediately, the same "structural
    check, not just presence" reasoning the mount-type check above already
    applies to the state-file/health-report directories.
    """

    if not path.is_file():
        raise ImageError(f"{path}: agent compose file is missing.")
    content = path.read_text(encoding="utf-8")
    required = [
        "pull_policy: never",
        "restart: on-failure",
        "image: thermoctl-agent:current",
        "- /var/lib/thermoctl-watchdog:/var/lib/thermoctl-watchdog",
        "- /run/thermoctl-agent:/run/thermoctl-agent",
        "group_add:",
        "${DOCKER_GID:?",
        "- /boot/firmware/thermoctl:/boot/firmware/thermoctl:ro",
        "- /var/lib/thermoctl:/var/lib/thermoctl:ro",
        "- /var/lib/zigbee2mqtt:/var/lib/zigbee2mqtt:ro",
    ]
    missing = [line for line in required if line not in content]
    if missing:
        raise ImageError(f"{path}: missing required line(s): {missing!r}.")

    forbidden = [
        "/var/lib/thermoctl-watchdog/state.env:/var/lib/thermoctl-watchdog/state.env",
        "/run/thermoctl-agent-health.env:/run/thermoctl-agent-health.env",
    ]
    present = [line for line in forbidden if line in content]
    if present:
        raise ImageError(
            f"{path}: single-file bind mount(s) present, must be directory "
            f"mounts instead (P5.7 hot-fix): {present!r}."
        )


# The uid/gid docker/Dockerfile.agent's `agent` user/group is pinned to --
# checked in two independent places (this module and the Dockerfile
# itself) precisely so neither can silently drift from the other (P5.7
# hot-fix, cross-review: a root-owned /run/thermoctl-agent gave the
# actually-unprivileged agent container no write permission at all).
_AGENT_UID_GID = "10002"


def check_tmpfiles_entry(path: Path) -> None:
    """Checks that the tmpfiles.d snippet recreating `/run/thermoctl-agent`
    on every boot exists, actually names that directory, and -- the
    cross-review finding this check exists for -- owns it by the agent
    container's own uid/gid (`_AGENT_UID_GID`, matching
    `docker/Dockerfile.agent`'s pinned `agent` user/group), not root.
    Without the directory existing at all, `image/common/agent-compose
    .yml`'s directory mount has nothing to bind to before the agent
    container starts; without the right ownership, the agent can open the
    directory (mode 0755, world-readable) but never write into it.

    Parses the one real `d <path> <mode> <uid> <gid> <age> [argument]`
    line structurally (comments and blank lines skipped) rather than a
    plain substring match: a substring check for "10002" would pass even
    if that number showed up in the wrong field (or in a comment) while
    the actual uid/gid line still said "root".
    """

    if not path.is_file():
        raise ImageError(f"{path}: tmpfiles.d entry for /run/thermoctl-agent is missing.")
    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    entries = [line for line in lines if line.split()[1:2] == ["/run/thermoctl-agent"]]
    if not entries:
        raise ImageError(f"{path}: does not define /run/thermoctl-agent.")
    fields = entries[0].split()
    if len(fields) < 5:
        raise ImageError(
            f"{path}: entry for /run/thermoctl-agent has too few fields: {entries[0]!r}."
        )
    _entry_type, _entry_path, _mode, uid, gid = fields[:5]
    if uid != _AGENT_UID_GID or gid != _AGENT_UID_GID:
        raise ImageError(
            f"{path}: /run/thermoctl-agent is owned by {uid}:{gid}, must be "
            f"{_AGENT_UID_GID}:{_AGENT_UID_GID} (docker/Dockerfile.agent's "
            f"pinned agent uid/gid) -- a root-owned directory gives the "
            f"unprivileged agent container no write permission."
        )


def check_all(root: Path = IMAGE_DIR) -> None:
    """Runs all checks; raises on the first failure."""

    common = root / "common"
    packages = check_package_list(common / "packages.txt")
    check_docker_packages_from_official_repo(packages)
    check_docker_apt_source(common / "apt" / "docker.sources")
    check_docker_apt_preferences(common / "apt" / "preferences.d" / "docker")
    check_docker_key_fetch(common / "apt" / "fetch-docker-key.sh")
    check_udev_rule(common / "udev" / "99-zigbee-stick.rules")
    check_agent_registration_template(common / "agent-registration.empty.json")
    check_watchdog_unit(root.parent / "watchdog" / "thermoctl-watchdog.service")
    check_leds_unit(root.parent / "watchdog" / "cmd" / "thermoctl-leds" / "thermoctl-leds.service")
    check_agent_compose_file(common / "agent-compose.yml")
    check_tmpfiles_entry(common / "tmpfiles.d" / "thermoctl-agent.conf")


def main() -> int:
    try:
        check_all()
    except ImageError as exc:
        print(f"Image configuration invalid: {exc}", file=sys.stderr)
        return 1
    print("Image configuration plausible (not a real build -- see docstring).")
    return 0


if __name__ == "__main__":  # pragma: no cover -- just an entry point
    sys.exit(main())
