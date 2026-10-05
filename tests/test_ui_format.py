"""Tests for `fleet/ui_format.py` -- the one shared place every absolute
timestamp/date/agent-echoed reason the fleet UI shows a landlord goes
through (text-hygiene pass, UI-redesign stage 2 polish). Pure functions,
no storage/HTTP needed."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fleet.ui_format import format_local_date, format_local_datetime, format_technical_reason


def test_format_local_datetime_naive_utc_winter() -> None:
    # Winter: Europe/Berlin is UTC+1 (CET), no DST.
    moment = datetime(2026, 1, 4, 18, 46)
    assert format_local_datetime(moment) == "04.01.2026, 19:46 Uhr"


def test_format_local_datetime_naive_utc_summer_dst() -> None:
    # Summer: Europe/Berlin is UTC+2 (CEST) -- DST must be applied, not a
    # fixed one-hour offset.
    moment = datetime(2026, 7, 4, 17, 46)
    assert format_local_datetime(moment) == "04.07.2026, 19:46 Uhr"


def test_format_local_datetime_already_aware_is_trusted() -> None:
    moment = datetime(2026, 1, 4, 18, 46, tzinfo=UTC)
    assert format_local_datetime(moment) == "04.01.2026, 19:46 Uhr"


def test_format_local_datetime_crosses_midnight_into_next_day() -> None:
    # 23:30 UTC in summer is 01:30 the *next* calendar day in Berlin --
    # exercises the date component actually shifting, not only the time.
    moment = datetime(2026, 7, 4, 23, 30)
    assert format_local_datetime(moment) == "05.07.2026, 01:30 Uhr"


def test_format_local_date_has_no_time_component() -> None:
    assert format_local_date(date(2024, 5, 1)) == "01.05.2024"


def test_format_technical_reason_known_literal_is_translated() -> None:
    assert format_technical_reason("agent rejected") == "Vom Agenten abgelehnt."


def test_format_technical_reason_known_apartment_reason_is_translated() -> None:
    assert (
        format_technical_reason("Wohnung 'musterstr1-we3': agent rejected")
        == "Wohnung musterstr1-we3: Vom Agenten abgelehnt."
    )


def test_format_technical_reason_unknown_apartment_reason_stays_marked() -> None:
    raw = "Wohnung 'musterstr1-we3': unexpected agent error"
    assert format_technical_reason(raw) == f"Technischer Hinweis (Agent): {raw}"


def test_format_technical_reason_known_success_literal_is_translated() -> None:
    assert (
        format_technical_reason("already at the desired revision.")
        == "Bereits auf der gewünschten Revision."
    )


def test_format_technical_reason_unknown_value_is_marked_technical_not_silently_shown() -> None:
    raw = "service: pulling repo@sha256:deadbeef failed (timeout)"
    result = format_technical_reason(raw)
    # Never silently prints the raw (possibly English) agent text as if it
    # were a polished German sentence -- the raw value must still be
    # present (nothing lost) but explicitly marked as a technical pass-
    # through.
    assert raw in result
    assert "Technischer Hinweis" in result
    assert result != raw


def test_format_technical_reason_unknown_value_already_german_still_marked() -> None:
    # Even an already-German raw reason that happens not to be in the
    # fixed translation table is marked, not guessed at -- this function
    # does not attempt to detect language, only known literal matches.
    raw = "Kein aktueller Sollzustand mehr vorhanden."
    assert format_technical_reason(raw) == f"Technischer Hinweis (Agent): {raw}"
