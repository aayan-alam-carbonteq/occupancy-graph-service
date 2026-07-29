"""Every value leaving the service must survive json.dumps.

The hatch can return any of the 144 columns, including jsonb, bytea, numeric,
timestamptz and arrays; the typed surface returns Decimal identity_confidence.
NaN/Infinity become null: json.dumps emits bare NaN, which is not valid JSON
and breaks strict parsers on the engine side.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from occupancy_graph.service.jsonio import jsonable


def test_scalars_pass_through_unchanged():
    assert jsonable(None) is None
    assert jsonable(True) is True
    assert jsonable(7) == 7
    assert jsonable("x") == "x"


def test_decimal_becomes_a_float():
    assert jsonable(Decimal("40.50")) == 40.5


def test_dates_and_timestamps_become_iso_strings():
    assert jsonable(date(2026, 3, 5)) == "2026-03-05"
    assert jsonable(datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)) == "2026-03-05T12:00:00+00:00"
    assert jsonable(timedelta(seconds=90)) == 90.0


def test_bytes_become_hex_and_uuids_become_strings():
    assert jsonable(b"\x00\xff") == "00ff"
    assert jsonable(UUID("00000000-0000-0000-0000-000000000001")) == (
        "00000000-0000-0000-0000-000000000001"
    )


def test_containers_are_converted_recursively():
    assert jsonable([Decimal("1.5"), {"d": date(2026, 1, 1)}]) == [1.5, {"d": "2026-01-01"}]


def test_non_finite_floats_become_null_and_everything_dumps():
    assert jsonable(float("nan")) is None
    assert jsonable(float("inf")) is None
    payload = {"a": Decimal("1.25"), "b": [date(2026, 1, 1), b"\x01"], "c": float("nan")}
    assert json.loads(json.dumps(jsonable(payload))) == {
        "a": 1.25, "b": ["2026-01-01", "01"], "c": None
    }
