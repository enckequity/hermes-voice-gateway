"""Read-only business-hours availability from a local file. No external calls.

The file is ``VOICE_HOURS_PATH`` or ``<HERMES_HOME>/voice/hours.json``::

    {
      "timezone": "America/Chicago",
      "weekly": {"mon": ["09:00-17:00"], "tue": ["09:00-12:00", "13:00-17:00"], "sat": []},
      "closed": ["2026-12-25"],
      "busy": [{"start": "2026-11-24T10:00", "end": "2026-11-24T11:30"}]
    }

``weekly`` days missing or empty are closed. ``closed`` dates are whole-day
closures. ``busy`` blocks (local time in ``timezone``) are removed from the open
windows; an operator or a routine may rewrite them, the plugin only reads. Nothing
is ever booked: callers are told a person confirms the time.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from ._env import hermes_home, secret
except ImportError:  # imported top-level by unit tests
    from _env import hermes_home, secret

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAX_DAYS = 7
MAX_AHEAD_DAYS = 60
MAX_FILE_BYTES = 64 * 1024
_RANGE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-4]):([0-5]\d)$")
NOTICE = ("These are open times only. Nothing has been booked; "
          "someone from the business confirms the time.")


class HoursError(ValueError):
    """The hours file is missing or malformed (the caller hears a fallback)."""


def hours_path() -> Path:
    explicit = secret("VOICE_HOURS_PATH").strip()
    return Path(explicit).expanduser() if explicit else hermes_home() / "voice" / "hours.json"


def _minutes(hh: str, mm: str) -> int:
    return int(hh) * 60 + int(mm)


def _parse_range(text) -> tuple[int, int]:
    match = _RANGE.match(str(text).strip())
    if not match:
        raise HoursError(f"bad time range {text!r}; use HH:MM-HH:MM")
    start, end = _minutes(*match.group(1, 2)), _minutes(*match.group(3, 4))
    if end <= start or end > 24 * 60:
        raise HoursError(f"time range {text!r} must end after it starts, on the same day")
    return start, end


def load_hours(path: Path | None = None) -> dict:
    path = path or hours_path()
    try:
        raw = path.read_bytes()
    except OSError:
        raise HoursError("business hours are not set up") from None
    if len(raw) > MAX_FILE_BYTES:
        raise HoursError("business hours file is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise HoursError("business hours file is not valid JSON") from None
    if not isinstance(data, dict):
        raise HoursError("business hours file must be a JSON object")
    try:
        tz = ZoneInfo(str(data.get("timezone") or ""))
    except (ZoneInfoNotFoundError, ValueError):
        raise HoursError("business hours file needs a valid IANA timezone") from None
    weekly = data.get("weekly") or {}
    if not isinstance(weekly, dict) or set(weekly) - set(DAYS):
        raise HoursError("weekly must map mon..sun to lists of HH:MM-HH:MM")
    week = {day: sorted(_parse_range(r) for r in (weekly.get(day) or [])) for day in DAYS}
    try:
        closed = {date.fromisoformat(str(d)) for d in (data.get("closed") or [])}
        busy = []
        for block in data.get("busy") or []:
            start = datetime.fromisoformat(str(block["start"])).replace(tzinfo=None)
            end = datetime.fromisoformat(str(block["end"])).replace(tzinfo=None)
            if end > start:
                busy.append((start, end))
    except (TypeError, KeyError, ValueError):
        raise HoursError("closed dates or busy blocks are malformed") from None
    return {"tz": tz, "week": week, "closed": closed, "busy": busy}


def _subtract(windows: list[tuple[int, int]], cut: tuple[int, int]) -> list[tuple[int, int]]:
    out = []
    for start, end in windows:
        if cut[1] <= start or cut[0] >= end:
            out.append((start, end))
            continue
        if cut[0] > start:
            out.append((start, cut[0]))
        if cut[1] < end:
            out.append((cut[1], end))
    return out


def _fmt(minutes: int) -> str:
    return "24:00" if minutes >= 24 * 60 else f"{minutes // 60:02d}:{minutes % 60:02d}"


def availability(hours: dict, start: date | None = None, days: int = 1, now: datetime | None = None) -> dict:
    """Open windows per day, in the business's timezone, minus busy blocks and past time today."""
    if type(days) is not int or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    tz = hours["tz"]
    now_local = (now or datetime.now(tz)).astimezone(tz).replace(tzinfo=None)
    today = now_local.date()
    start = start or today
    if start < today:
        raise ValueError("that date has already passed")
    if start > today + timedelta(days=MAX_AHEAD_DAYS):
        raise ValueError(f"only the next {MAX_AHEAD_DAYS} days can be checked")
    result = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        windows = [] if day in hours["closed"] else list(hours["week"][DAYS[day.weekday()]])
        midnight = datetime.combine(day, time())
        for busy_start, busy_end in hours["busy"]:
            lo = (busy_start - midnight).total_seconds() // 60
            hi = (busy_end - midnight).total_seconds() // 60
            if hi > 0 and lo < 24 * 60:
                windows = _subtract(windows, (max(0, int(lo)), min(24 * 60, int(hi))))
        if day == today:
            windows = _subtract(windows, (0, now_local.hour * 60 + now_local.minute))
        result.append({
            "date": day.isoformat(),
            "weekday": day.strftime("%A"),
            "open": [f"{_fmt(a)}-{_fmt(b)}" for a, b in windows],
        })
    return {"ok": True, "timezone": str(tz), "days": result, "booked": False, "notice": NOTICE}
