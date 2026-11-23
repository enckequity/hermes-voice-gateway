"""Restricted phone tools: trusted-turn leases, lead capture and business-hours availability.

No Hermes imports at load time. Caller/model arguments never choose ownership,
file paths, or permissions; the gateway binds the source and grants a live turn.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date

from . import _engine, callbacks, hours

logger = logging.getLogger(__name__)

TOOLSET = "voice_caller"
TOOLSET_SELECTION = [TOOLSET, "no_mcp"]
TOOL_NAMES = frozenset({"voice_capture_lead", "voice_check_availability"})
AVAILABILITY_FIELDS = frozenset({"date", "days"})
LEAD_FIELDS = frozenset({"name", "reason", "consent", "callback_number"})
LEAD_NOTICE = (
    "Saved for the business to follow up. Nobody has been called or booked yet."
)
CALLER_INSTRUCTIONS = (
    "Phone tools are limited to voice_check_availability and voice_capture_lead. "
    "Use voice_check_availability before saying when the business is open or has time free; "
    "it only reads opening times and books nothing, so say someone will confirm any time. "
    "To take a message, collect the caller's name and the reason for the call, and a different "
    "callback number only if they offer one; read the details back. "
    "Explain that saving keeps their name, number and reason for the business to follow up, "
    "and ask for explicit consent before voice_capture_lead; never infer consent. "
    "Only confirm it was saved after ok=true. One message per call; retry unchanged details if necessary. "
    "Never name the software, model or vendors behind this call."
)

_GRANTS_SCHEMA = """CREATE TABLE IF NOT EXISTS voice_caller_grants (
    session_key TEXT PRIMARY KEY, caller TEXT NOT NULL, call_sid TEXT NOT NULL,
    message_id TEXT NOT NULL, expires_at REAL NOT NULL)"""
_LEADS_SCHEMA = """CREATE TABLE IF NOT EXISTS voice_leads (
    id TEXT PRIMARY KEY, call_sid TEXT NOT NULL UNIQUE, phone TEXT NOT NULL,
    name TEXT NOT NULL, reason TEXT NOT NULL, callback_number TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""
_LEAD_COLUMNS = ("name", "reason", "callback_number")


@dataclass(frozen=True)
class CallerIdentity:
    session_key: str
    caller: str
    call_sid: str
    message_id: str

    def values(self) -> tuple[str, str, str, str]:
        return self.session_key, self.caller, self.call_sid, self.message_id


_live_turns: dict[CallerIdentity, Callable[[], bool]] = {}
_live_lock = threading.RLock()


def grant_turn(identity: CallerIdentity, *, ttl: float, is_active: Callable[[], bool]) -> None:
    """Require both a process-local live call and a profile-scoped database lease."""
    with open_caller_store() as store:
        store.grant(identity, ttl=ttl)
    with _live_lock:
        _live_turns[identity] = is_active


def revoke_turn(identity: CallerIdentity) -> None:
    # Invalidate locally first: a failed SQLite revoke must not permit tools.
    with _live_lock:
        _live_turns.pop(identity, None)
    with open_caller_store() as store:
        store.revoke(identity)


def _require_live(identity: CallerIdentity) -> None:
    with _live_lock:
        active = _live_turns.get(identity)
        if active is None or not active():
            raise PermissionError("phone turn is not live in this gateway process")


def current_identity() -> CallerIdentity:
    """Require task-bound gateway context, never a model argument or env-only id."""
    try:
        from gateway.session_context import bound_session_key, get_session_env
    except ImportError:
        raise PermissionError("live caller context is unavailable") from None
    identity = CallerIdentity(
        bound_session_key(), get_session_env("HERMES_SESSION_CHAT_ID"),
        get_session_env("HERMES_SESSION_THREAD_ID"), get_session_env("HERMES_SESSION_MESSAGE_ID"),
    )
    if get_session_env("HERMES_SESSION_PLATFORM") != "voice" or not all(identity.values()):
        raise PermissionError("a live phone turn is required")
    return identity


def is_voice_context() -> bool:
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return False
    return get_session_env("HERMES_SESSION_PLATFORM") == "voice"


class CallerStore:
    def __init__(self, conn: sqlite3.Connection, clock=time.time):
        self._db, self._clock = conn, clock
        conn.execute(_GRANTS_SCHEMA)
        conn.execute(_LEADS_SCHEMA)
        conn.commit()

    def grant(self, identity: CallerIdentity, *, ttl: float) -> None:
        """The adapter alone issues a bounded lease before admitting a turn."""
        if not all(identity.values()) or ttl <= 0:
            raise ValueError("a complete caller identity and positive lease are required")
        now = self._clock()
        with self._db:
            cur = self._db.execute(
                "INSERT INTO voice_caller_grants VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(session_key) DO UPDATE SET caller=excluded.caller, "
                "call_sid=excluded.call_sid, message_id=excluded.message_id, expires_at=excluded.expires_at "
                "WHERE voice_caller_grants.expires_at<=? OR "
                "(voice_caller_grants.caller=excluded.caller AND voice_caller_grants.call_sid=excluded.call_sid "
                "AND voice_caller_grants.message_id=excluded.message_id)",
                (*identity.values(), now + ttl, now),
            )
            if cur.rowcount != 1:
                raise PermissionError("another phone turn owns this session")

    def authorize(self, identity: CallerIdentity) -> None:
        _require_live(identity)
        row = self._db.execute(
            "SELECT 1 FROM voice_caller_grants WHERE session_key=? AND caller=? "
            "AND call_sid=? AND message_id=? AND expires_at>?",
            (*identity.values(), self._clock()),
        ).fetchone()
        if row is None:
            raise PermissionError("phone turn permission expired or is no longer active")

    def revoke(self, identity: CallerIdentity) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM voice_caller_grants WHERE session_key=? AND caller=? AND call_sid=? AND message_id=?",
                identity.values(),
            )

    def save_lead(self, identity: CallerIdentity, lead: dict) -> str:
        """Authorize and commit together so hangup cannot race a later write.

        The phone number is the live caller's (gateway-bound), never a model argument.
        """
        values = tuple(lead.get(k, "") for k in _LEAD_COLUMNS)
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self.authorize(identity)
            row = self._db.execute(
                "SELECT id, phone, name, reason, callback_number FROM voice_leads WHERE call_sid=?",
                (identity.call_sid,),
            ).fetchone()
            if row is not None:
                if row[1:] != (identity.caller, *values):
                    raise ValueError("this call already has a message with different details; the business can amend it")
            else:
                self._db.execute(
                    "INSERT INTO voice_leads (id, call_sid, phone, name, reason, callback_number, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uuid.uuid4().hex, identity.call_sid, identity.caller, *values, self._clock()),
                )
        persisted = self._db.execute(
            "SELECT id FROM voice_leads WHERE call_sid=? AND phone=? AND name=? AND reason=? AND callback_number=?",
            (identity.call_sid, identity.caller, *values),
        ).fetchone()
        if persisted is None:
            raise sqlite3.DatabaseError("lead readback failed")
        return persisted[0]

    def leads(self, limit: int = 50) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, phone, name, reason, callback_number, created_at FROM voice_leads "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(("id", "phone", "name", "reason", "callback_number", "created_at"), r)) for r in rows]


@contextmanager
def open_caller_store():
    """Use the same profile-scoped plugin DB/connection lifecycle as callbacks."""
    conn = callbacks._connect()
    try:
        yield CallerStore(conn)
    finally:
        callbacks._release(conn)


def require_access() -> CallerIdentity:
    identity = current_identity()
    with open_caller_store() as store:
        store.authorize(identity)
    return identity


def _memory_problem(memory) -> str:
    if (not isinstance(memory, dict)
            or memory.get("memory_enabled") is not False
            or memory.get("user_profile_enabled") is not False
            or memory.get("provider") not in (None, "")):
        return ("caller profile must disable memory.memory_enabled and memory.user_profile_enabled "
                "and leave memory.provider unset; use a dedicated voice profile")
    return ""


def verify_runtime_contract() -> None:
    """Startup check: memory is off in this profile and the private engine symbols exist.

    Run once when the call server starts so a wrong profile or an engine that lacks
    ``_get_platform_tools`` refuses to answer the phone with a clear reason, instead
    of failing on the first caller's turn. ``verify_tool_surface`` still runs per turn.
    """
    from hermes_cli.config import load_config

    _engine.platform_tools_resolver()
    problem = _memory_problem(load_config().get("memory"))
    if problem:
        raise PermissionError(problem)


def verify_tool_surface() -> None:
    """Require memory isolation and exactly the two caller tools before inference."""
    from agent.skill_utils import parse_config_string_list
    from gateway.session_context import bound_session_key, get_session_env
    from hermes_cli.config import load_config
    from model_tools import get_tool_definitions

    _get_platform_tools = _engine.platform_tools_resolver()  # private: fails clearly if renamed

    if not callable(bound_session_key) or not callable(get_session_env):
        raise PermissionError("unsupported gateway caller context")
    config = load_config()
    problem = _memory_problem(config.get("memory"))
    if problem:
        raise PermissionError(problem)
    selected = {**config, "platform_toolsets": {**(config.get("platform_toolsets") or {}), "voice": TOOLSET_SELECTION}}
    enabled = sorted(_get_platform_tools(selected, "voice"))
    disabled = parse_config_string_list((config.get("agent") or {}).get("disabled_toolsets")) or None
    definitions = get_tool_definitions(enabled_toolsets=enabled, disabled_toolsets=disabled, quiet_mode=True)
    if {tool["function"]["name"] for tool in definitions} != TOOL_NAMES:
        logger.error(
            "[voice] caller tool policy is not isolated; exclude other plugin toolsets for voice "
            "and disable context-engine additions/tool-search deferral before accepting caller turns"
        )
        raise PermissionError("caller tool policy must resolve to availability and lead capture only")


def check_availability(args: dict, **kwargs) -> str:
    """Read the business's hours file only; no caller-controlled path, no external calls."""
    try:
        require_access()
        if not isinstance(args, dict) or set(args) - AVAILABILITY_FIELDS:
            raise ValueError("unknown availability argument")
        start = None
        if "date" in args:
            if not isinstance(args["date"], str):
                raise ValueError("date must be YYYY-MM-DD")
            start = date.fromisoformat(args["date"])
        days = args.get("days", 1)
        return json.dumps(hours.availability(hours.load_hours(), start=start, days=days))
    except hours.HoursError as exc:
        logger.warning("[voice] availability unavailable: %s", exc)
        return json.dumps({"ok": False, "error": "opening times are not available; offer to take a message"})
    except (PermissionError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def capture_lead(args: dict, **kwargs) -> str:
    """Save one consented message (name, reason, the caller's number) per call; retries cannot duplicate."""
    try:
        identity = current_identity()
        if not isinstance(args, dict) or set(args) - LEAD_FIELDS:
            raise ValueError("unknown lead argument")
        if args.get("consent") is not True:
            raise ValueError("explicit consent=true is required to save the message")
        lead = {}
        for name, maximum in (("name", 200), ("reason", 1000)):
            value = args.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{name} must be nonempty text (max {maximum})")
            lead[name] = callbacks.clean_text(value, maximum)
        if "callback_number" in args:
            number = args["callback_number"]
            if not isinstance(number, str) or not callbacks.E164.match(number.strip()):
                raise ValueError("callback_number must be an E.164 number such as +15551234567")
            lead["callback_number"] = number.strip()
        with open_caller_store() as store:
            lead_id = store.save_lead(identity, lead)
        return json.dumps({
            "ok": True, "lead_id": lead_id, "status": "saved_for_follow_up",
            "called_back": False, "booked": False, "notice": LEAD_NOTICE,
        })
    except (PermissionError, ValueError, TypeError) as exc:
        return json.dumps({"ok": False, "error": str(exc)})
    except (OSError, sqlite3.Error):
        return json.dumps({"ok": False, "error": "message storage unavailable; nothing confirmed"})
