"""Tests for `agent.sources` (P5.4, CLAUDE.md security principle 2, section
13): the hard-coded image source list and the exact-match check against
it. Every case here is chosen to demonstrate a specific trick a compromised
fleet server might try -- a prefix match, a sibling service's own valid
image under the wrong service name, an embedded digest/tag, case-folding,
a trailing slash, a userinfo/port trick -- must be rejected, never merely
"probably fine"."""

from __future__ import annotations

import pytest

from agent.sources import (
    ALLOWED_SOURCES,
    DIGEST_PATTERN,
    digest_is_well_formed,
    image_repo_matches_source,
    normalize_repository,
)

VALID_DIGEST = "sha256:" + "a" * 64


def test_allowed_sources_has_exactly_the_four_services() -> None:
    assert set(ALLOWED_SOURCES) == {"thermoctl", "agent", "zigbee2mqtt", "mosquitto"}
    assert ALLOWED_SOURCES["thermoctl"] == "ghcr.io/magicalwig34653/thermoctl"
    assert ALLOWED_SOURCES["agent"] == "ghcr.io/magicalwig34653/thermoctl-agent"
    assert ALLOWED_SOURCES["zigbee2mqtt"] == "docker.io/koenkk/zigbee2mqtt"
    assert ALLOWED_SOURCES["mosquitto"] == "docker.io/library/eclipse-mosquitto"


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("ghcr.io/magicalwig34653/thermoctl", "ghcr.io/magicalwig34653/thermoctl"),
        ("koenkk/zigbee2mqtt", "docker.io/koenkk/zigbee2mqtt"),
        ("docker.io/koenkk/zigbee2mqtt", "docker.io/koenkk/zigbee2mqtt"),
        ("index.docker.io/koenkk/zigbee2mqtt", "docker.io/koenkk/zigbee2mqtt"),
        ("eclipse-mosquitto", "docker.io/library/eclipse-mosquitto"),
        ("docker.io/eclipse-mosquitto", "docker.io/library/eclipse-mosquitto"),
        ("docker.io/library/eclipse-mosquitto", "docker.io/library/eclipse-mosquitto"),
        ("localhost/foo", "localhost/foo"),
        ("localhost/foo/bar", "localhost/foo/bar"),
    ],
)
def test_normalize_repository_accepts_and_canonicalizes(image: str, expected: str) -> None:
    assert normalize_repository(image) == expected


@pytest.mark.parametrize(
    "image",
    [
        "",
        " ",
        "ghcr.io/magicalwig34653/thermoctl ",
        " ghcr.io/magicalwig34653/thermoctl",
        "ghcr.io/Magicalwig34653/thermoctl",
        "GHCR.IO/magicalwig34653/thermoctl",
        "ghcr.io/magicalwig34653/thermoctl@" + VALID_DIGEST,
        "ghcr.io/magicalwig34653/thermoctl:latest",
        "ghcr.io:1234/magicalwig34653/thermoctl",
        "user@ghcr.io/magicalwig34653/thermoctl",
        "/ghcr.io/magicalwig34653/thermoctl",
        "ghcr.io/magicalwig34653/thermoctl/",
        "ghcr.io//magicalwig34653/thermoctl",
        "ghcr.io/magicalwig34653//thermoctl",
        "ghcr.io/magicalwig-/thermoctl",
        "ghcr.io/-magicalwig/thermoctl",
    ],
)
def test_normalize_repository_rejects(image: str) -> None:
    assert normalize_repository(image) is None


@pytest.mark.parametrize(
    ("service", "image"),
    [
        ("thermoctl", "ghcr.io/magicalwig34653/thermoctl"),
        ("agent", "ghcr.io/magicalwig34653/thermoctl-agent"),
        ("zigbee2mqtt", "koenkk/zigbee2mqtt"),
        ("zigbee2mqtt", "docker.io/koenkk/zigbee2mqtt"),
        ("zigbee2mqtt", "index.docker.io/koenkk/zigbee2mqtt"),
        ("mosquitto", "eclipse-mosquitto"),
        ("mosquitto", "docker.io/library/eclipse-mosquitto"),
    ],
)
def test_image_repo_matches_source_true_for_the_real_source(service: str, image: str) -> None:
    assert image_repo_matches_source(service, image) is True


@pytest.mark.parametrize(
    ("service", "image"),
    [
        # Prefix trick: a sibling repository under the same registry/owner.
        ("thermoctl", "ghcr.io/magicalwig34653/thermoctl-evil"),
        # A real, valid source -- but for a *different* service.
        ("thermoctl", "ghcr.io/magicalwig34653/thermoctl-agent"),
        ("agent", "ghcr.io/magicalwig34653/thermoctl"),
        # An embedded digest -- belongs in `ServiceState.digest`, never here.
        ("thermoctl", "ghcr.io/magicalwig34653/thermoctl@" + VALID_DIGEST),
        # An embedded tag -- belongs in `ServiceState.version`.
        ("zigbee2mqtt", "docker.io/koenkk/zigbee2mqtt:latest"),
        # Case.
        ("thermoctl", "GHCR.IO/magicalwig34653/thermoctl"),
        # Trailing slash.
        ("thermoctl", "ghcr.io/magicalwig34653/thermoctl/"),
        # Userinfo/port tricks.
        ("thermoctl", "user@ghcr.io/magicalwig34653/thermoctl"),
        ("thermoctl", "ghcr.io:1234/magicalwig34653/thermoctl"),
        # A completely different, attacker-controlled registry.
        ("thermoctl", "evil.example.com/magicalwig34653/thermoctl"),
        # Wrong path entirely, under the same registry.
        ("mosquitto", "docker.io/library/eclipse-mosquitto-evil"),
        # Unknown service name.
        ("factory_reset", "ghcr.io/magicalwig34653/thermoctl"),
    ],
)
def test_image_repo_matches_source_false_for_every_trick(service: str, image: str) -> None:
    assert image_repo_matches_source(service, image) is False


def test_digest_pattern_matches_digest_is_well_formed() -> None:
    # `agent.sources.DIGEST_PATTERN` and `digest_is_well_formed` must agree
    # -- the latter is only a thin, documented wrapper around the former.
    assert DIGEST_PATTERN.fullmatch(VALID_DIGEST) is not None
    assert digest_is_well_formed(VALID_DIGEST) is True


@pytest.mark.parametrize(
    "digest",
    [
        "",
        "latest",
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "sha256:" + "A" * 64,
        "sha256:" + "g" * 64,
        "md5:" + "a" * 32,
        VALID_DIGEST + " ",
        " " + VALID_DIGEST,
    ],
)
def test_digest_is_well_formed_false_for_every_malformed_value(digest: str) -> None:
    assert digest_is_well_formed(digest) is False
