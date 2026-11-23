"""Business-hours availability: read-only, local file, no external calls."""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hours  # noqa: E402

WEEK = {"mon": ["09:00-12:00", "13:00-17:00"], "tue": ["09:00-17:00"], "sat": []}


def _write(tmp_path, **data):
    path = tmp_path / "hours.json"
    path.write_text(json.dumps({"timezone": "America/Chicago", "weekly": WEEK, **data}))
    return path


def test_weekly_windows_closed_days_and_busy_blocks(tmp_path) -> None:
    h = hours.load_hours(_write(tmp_path, closed=["2099-01-06"],
                                busy=[{"start": "2099-01-05T10:00", "end": "2099-01-05T11:30"}]))
    out = hours.availability(h, start=date(2099, 1, 5), days=3, now=datetime(2099, 1, 1, 12, 0, tzinfo=h["tz"]))
    assert out["booked"] is False and out["timezone"] == "America/Chicago"
    assert [(d["weekday"], d["open"]) for d in out["days"]] == [
        ("Monday", ["09:00-10:00", "11:30-12:00", "13:00-17:00"]),
        ("Tuesday", []),  # closed date
        ("Wednesday", []),  # not in weekly
    ]


def test_today_drops_time_already_past(tmp_path) -> None:
    h = hours.load_hours(_write(tmp_path))
    now = datetime(2099, 1, 5, 14, 20, tzinfo=h["tz"])
    out = hours.availability(h, now=now)
    assert out["days"][0]["open"] == ["14:20-17:00"]


def test_busy_block_spanning_midnight(tmp_path) -> None:
    h = hours.load_hours(_write(tmp_path, weekly={"mon": ["00:00-24:00"], "tue": ["00:00-24:00"]},
                                busy=[{"start": "2099-01-05T22:00", "end": "2099-01-06T02:00"}]))
    out = hours.availability(h, start=date(2099, 1, 5), days=2, now=datetime(2099, 1, 1, tzinfo=h["tz"]))
    assert [d["open"] for d in out["days"]] == [["00:00-22:00"], ["02:00-24:00"]]


@pytest.mark.parametrize("data", [
    "not json", [], {"timezone": "Mars/Base", "weekly": {}}, {"timezone": "UTC", "weekly": {"funday": []}},
    {"timezone": "UTC", "weekly": {"mon": ["9-5"]}}, {"timezone": "UTC", "weekly": {"mon": ["17:00-09:00"]}},
    {"timezone": "UTC", "weekly": {}, "closed": ["someday"]},
    {"timezone": "UTC", "weekly": {}, "busy": [{"start": "x"}]},
])
def test_malformed_files_raise_hours_error(tmp_path, data) -> None:
    path = tmp_path / "hours.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    with pytest.raises(hours.HoursError):
        hours.load_hours(path)


def test_missing_file_and_path_resolution(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("VOICE_HOURS_PATH", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hours.hours_path() == tmp_path / "voice" / "hours.json"
    with pytest.raises(hours.HoursError, match="not set up"):
        hours.load_hours()


def test_range_limits(tmp_path) -> None:
    h = hours.load_hours(_write(tmp_path))
    now = datetime(2099, 1, 5, tzinfo=h["tz"])
    for kwargs in ({"days": 8}, {"days": 0}, {"start": date(2099, 1, 4)}, {"start": date(2099, 6, 1)}):
        with pytest.raises(ValueError):
            hours.availability(h, now=now, **kwargs)


def test_example_file_is_valid() -> None:
    example = Path(__file__).resolve().parents[1] / "voice" / "hours.example.json"
    assert hours.load_hours(example)["week"]["fri"] == [(480, 720), (780, 960)]


def test_module_makes_no_network_imports() -> None:
    source = (Path(__file__).resolve().parents[1] / "hours.py").read_text()
    for banned in ("urllib", "http", "socket", "requests", "aiohttp"):
        assert f"import {banned}" not in source and f"from {banned}" not in source
