"""The hard-coded image source list (section 13, CLAUDE.md security
principle 2): "The prefix list of allowed registries lives as a constant
in the `agent` package. The cloud only names version and digest, never the
source." This module is that constant, plus the one function that checks a
cloud-supplied `ServiceState.image` against it.

**Never a prefix/substring match.** `ghcr.io/magicalwig34653/thermoctl-evil`
must not pass a check for the `thermoctl` service just because it starts
with the same registry and owner; `ghcr.io/magicalwig34653/thermoctl-agent`
(a real, valid source -- for the *agent* service) must not pass a check for
`thermoctl` either. `image_repo_matches_source` therefore always compares
two fully **normalized** repository strings for **exact equality**, never
`str.startswith`.

**Normalization** follows the same rules the Docker/OCI reference grammar
and `docker.io`'s own registry use, just enough of them to make the four
short forms in section 13's own JSON example (`"koenkk/zigbee2mqtt"`,
`"eclipse-mosquitto"`) resolve to the same canonical string as their fully
qualified form (`"docker.io/koenkk/zigbee2mqtt"`,
`"docker.io/library/eclipse-mosquitto"`) -- **not** a general-purpose
permissive reference parser: anything this module cannot confidently
normalize (an embedded tag or digest, a userinfo/port trick, uppercase, an
empty path component, a leading/trailing slash) is rejected outright,
`None`, rather than guessed at. A rejected `image` value can never match
any source, by construction (`image_repo_matches_source` treats `None` as
"does not match").
"""

from __future__ import annotations

import re

# Section 13's own JSON example, one canonical repository per service --
# the cloud only ever names `version`/`digest`
# (`protocol.desired_state.ServiceState`), never the source. Adding a fifth
# service, or a second allowed repository for one of these four, is exactly
# the kind of change CLAUDE.md's security principle 2 requires explicit
# project-owner sign-off for, not something this scaffold does on its own.
ALLOWED_SOURCES: dict[str, str] = {
    "thermoctl": "ghcr.io/magicalwig34653/thermoctl",
    "agent": "ghcr.io/magicalwig34653/thermoctl-agent",
    "zigbee2mqtt": "docker.io/koenkk/zigbee2mqtt",
    "mosquitto": "docker.io/library/eclipse-mosquitto",
}

# The digest a `ServiceState` must carry before a single byte is ever
# pulled (section 13: "'latest' or a tag without a digest is rejected").
# `protocol.desired_state.ServiceState.digest` already enforces this same
# pattern at the model level (`Field(pattern=...)`) -- this constant is
# `agent.loop`'s own defense-in-depth check, for a `ServiceState` that
# reached this module by a path other than normal pydantic validation
# (`model_construct`, a future caller, a test), consistent with security
# principle 5 ("every check ... belongs in `agent/` and is enforced there,
# even if the cloud says otherwise").
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")

_DOMAIN_COMPONENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PATH_COMPONENT = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*$")

# Both of these strings identify Docker Hub itself -- `index.docker.io` is
# the historical/canonical registry hostname, `docker.io` the modern one;
# both normalize to the same canonical domain below.
_DOCKER_HUB_ALIASES = ("docker.io", "index.docker.io")
_DOCKER_HUB_CANONICAL = "docker.io"


def normalize_repository(image: str) -> str | None:
    """Returns `image`'s canonical `registry/path` form, or `None` if
    `image` cannot be confidently normalized at all (see this module's own
    docstring for the reasoning). Never raises.

    Deliberately rejects, rather than trying to parse around: any
    whitespace, any uppercase letter (Docker/OCI repository paths are
    lowercase only; rejecting uppercase anywhere -- including in a
    registry hostname -- is stricter than the grammar technically
    requires, but nothing in `ALLOWED_SOURCES` needs an uppercase
    hostname, and it closes off `GHCR.IO/...`-style tricks without having
    to reason about case-folding registries correctly), any `@` (a digest
    belongs in `ServiceState.digest`, never embedded in `image`), and any
    `:` at all (a tag belongs in `ServiceState.version`, never embedded in
    `image`; this also happens to reject every registry-port and
    userinfo:password-style trick, since none of `ALLOWED_SOURCES` needs a
    port either).
    """

    if not image or image != image.strip() or any(char.isspace() for char in image):
        return None
    if any(char.isupper() for char in image):
        return None
    if "@" in image or ":" in image:
        return None
    if image.startswith("/") or image.endswith("/") or "//" in image:
        return None

    parts = image.split("/")
    if any(part == "" for part in parts):
        return None

    first = parts[0]
    looks_like_domain = len(parts) > 1 and ("." in first or first == "localhost")
    if looks_like_domain:
        domain = first
        path_parts = parts[1:]
        for component in domain.split("."):
            if not _DOMAIN_COMPONENT.fullmatch(component):
                return None
    else:
        domain = _DOCKER_HUB_CANONICAL
        path_parts = parts

    if domain in _DOCKER_HUB_ALIASES:
        domain = _DOCKER_HUB_CANONICAL

    if domain == _DOCKER_HUB_CANONICAL and len(path_parts) == 1:
        path_parts = ["library", *path_parts]

    for component in path_parts:
        if not _PATH_COMPONENT.fullmatch(component):
            return None

    return domain + "/" + "/".join(path_parts)


def image_repo_matches_source(service: str, image: str) -> bool:
    """`True` iff `image` normalizes to exactly `ALLOWED_SOURCES[service]`
    -- an unknown `service` name, or an `image` that fails to normalize at
    all, is always `False`, never an exception (a caller with an unknown
    service name has already gone wrong somewhere the four fixed
    `protocol.desired_state.Services` field names should have prevented,
    and treating that as "no match" is the fail-closed answer either way).
    """

    allowed = ALLOWED_SOURCES.get(service)
    if allowed is None:
        return False
    normalized = normalize_repository(image)
    if normalized is None:
        return False
    return normalized == allowed


def digest_is_well_formed(digest: str) -> bool:
    """`True` iff `digest` matches `^sha256:[0-9a-f]{64}$` -- see
    `DIGEST_PATTERN`'s own docstring for why this check exists here too,
    in addition to `protocol.desired_state.ServiceState`'s own model-level
    pattern."""

    return bool(DIGEST_PATTERN.fullmatch(digest))
