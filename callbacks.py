"""Callback requests and their approval through the engine's native approval cards.

The agent *proposes* a callback (``voice_propose_callback``), then asks to dial it
with ``voice_place_callback``. That tool's ``pre_tool_call`` hook returns the
engine's ``{"action": "approve", "rule_key", "message"}`` directive with one rule
key per callback, so the dial waits on the same approval card as any other
flagged action and runs only if a person approves it. There is no agent-callable
way to approve, contexts where the gate would not ask a person (yolo, approvals
off, cron, unattended) are refused, and spoken words cannot run slash commands.

State lives in the plugin's SQLite file (``plugin_db``), so a tool call and the
command see the same data even when they run in different processes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager

from . import twilio_rest
from ._env import secret
from .twilio_util import parse_allowed, redact_phone

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-voice-gateway"
E164 = re.compile(r"^\+[1-9]\d{6,14}$")
PENDING_TTL_SECONDS = 24 * 3600
MAX_PENDING = 20
MAX_PENDING_PER_NUMBER = 2
DIAL_TIMEOUT_SECONDS = 180  # Twilio rings for 45s (Timeout) and fetches the answer URL right away
ANSWERED_TTL_SECONDS = 2 * 3600  # longer than the longest allowed call
MAX_BRIEF = 500
ID_RE = re.compile(r"^[0-9a-f]{8}$")

_SCHEMA = """CREATE TABLE IF NOT EXISTS callbacks (
    id TEXT PRIMARY KEY, destination TEXT NOT NULL, brief TEXT NOT NULL,
    staff_member TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    call_sid TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, decided_at REAL)"""
_COLUMNS = ("id", "destination", "brief", "staff_member", "status", "call_sid", "error", "created_at", "decided_at")


def clean_text(text: str, limit: int) -> str:
    """One line, no control characters, capped."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))).strip()[:limit]


class CallbackStore:
    def __init__(self, conn: sqlite3.Connection, clock=time.time):
        self._db, self._clock = conn, clock
        conn.execute(_SCHEMA)
        conn.commit()

    def _row(self, row) -> dict | None:
        return dict(zip(_COLUMNS, row)) if row else None

    def _expire(self) -> None:
        self._db.execute(
            "UPDATE callbacks SET status='expired' WHERE status='pending' AND created_at < ?",
            (self._clock() - PENDING_TTL_SECONDS,))
        # Twilio never reached the answer URL (no answer, busy, rejected, or we crashed mid-dial).
        self._db.execute(
            "UPDATE callbacks SET status='unanswered' WHERE status='dialing' AND decided_at < ?",
            (self._clock() - DIAL_TIMEOUT_SECONDS,))
        self._db.execute(
            "UPDATE callbacks SET status='completed' WHERE status='answered' AND decided_at < ?",
            (self._clock() - ANSWERED_TTL_SECONDS,))
        self._db.commit()

    def propose(self, destination: str, brief: str, staff_member: str) -> str:
        self._expire()
        (pending,) = self._db.execute("SELECT COUNT(*) FROM callbacks WHERE status='pending'").fetchone()
        if pending >= MAX_PENDING:
            raise ValueError("too many callbacks are waiting for approval; ask an operator to clear them")
        (same_number,) = self._db.execute(
            "SELECT COUNT(*) FROM callbacks WHERE status='pending' AND destination=?", (destination,)).fetchone()
        if same_number >= MAX_PENDING_PER_NUMBER:
            raise ValueError("that number already has callbacks waiting for approval")
        cb_id = _new_id(self._db)
        self._db.execute(
            "INSERT INTO callbacks (id, destination, brief, staff_member, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)", (cb_id, destination, brief, staff_member, self._clock()))
        self._db.commit()
        return cb_id

    def get(self, cb_id: str) -> dict | None:
        self._expire()
        return self._row(self._db.execute(
            f"SELECT {','.join(_COLUMNS)} FROM callbacks WHERE id=?", (cb_id,)).fetchone())

    def pending(self) -> list[dict]:
        self._expire()
        rows = self._db.execute(
            f"SELECT {','.join(_COLUMNS)} FROM callbacks WHERE status='pending' ORDER BY created_at").fetchall()
        return [self._row(r) for r in rows]

    def _transition(self, cb_id: str, old: str, new: str, **extra) -> bool:
        sets = ", ".join(["status=?", "decided_at=?"] + [f"{k}=?" for k in extra])
        cur = self._db.execute(
            f"UPDATE callbacks SET {sets} WHERE id=? AND status=?",
            (new, self._clock(), *extra.values(), cb_id, old))
        self._db.commit()
        return cur.rowcount == 1  # only one caller can win a transition, so no double dial

    def claim_for_dial(self, cb_id: str) -> bool:
        self._expire()
        return self._transition(cb_id, "pending", "dialing")

    def deny(self, cb_id: str) -> bool:
        self._expire()
        return self._transition(cb_id, "pending", "denied")

    def set_call_sid(self, cb_id: str, call_sid: str) -> None:
        self._db.execute("UPDATE callbacks SET call_sid=? WHERE id=?", (call_sid, cb_id))
        self._db.commit()

    def fail(self, cb_id: str, error: str) -> None:
        self._transition(cb_id, "dialing", "failed", error=error[:200])

    def release(self, cb_id: str, reason: str) -> None:
        """The callee answered but could not be connected (busy, no longer allowed)."""
        self._transition(cb_id, "answered", "failed", error=reason[:200])

    def knows_call(self, call_sid: str) -> bool:
        return self._db.execute("SELECT 1 FROM callbacks WHERE call_sid=?", (call_sid,)).fetchone() is not None

    def mark_answered(self, cb_id: str, to: str, call_sid: str) -> dict | None:
        """Callee picked up. Succeeds once, only for the number and call we dialed."""
        cur = self._db.execute(
            "UPDATE callbacks SET status='answered', call_sid=?, decided_at=? "
            "WHERE id=? AND status='dialing' AND destination=? AND (call_sid='' OR call_sid=?)",
            (call_sid, self._clock(), cb_id, to, call_sid))
        self._db.commit()
        if cur.rowcount != 1:
            return None
        return self.get(cb_id)


def _new_id(conn) -> str:
    import secrets as _secrets

    for _ in range(5):
        cb_id = _secrets.token_hex(4)
        if conn.execute("SELECT 1 FROM callbacks WHERE id=?", (cb_id,)).fetchone() is None:
            return cb_id
    raise RuntimeError("could not allocate a callback id")


def _connect() -> sqlite3.Connection:
    from plugins.plugin_storage import plugin_db  # Hermes: <home>/plugin-data/<name>/data.db

    return plugin_db(PLUGIN_NAME)


def _release(conn: sqlite3.Connection) -> None:
    conn.close()


@contextmanager
def open_store():
    """Open the store for one operation (the profile can change between calls)."""
    conn = _connect()
    try:
        yield CallbackStore(conn)
    finally:
        _release(conn)


# -- Approval card ----------------------------------------------------------

RULE_PREFIX = "voice_callback"


def rule_key(row: dict) -> str:
    """One approval rule per callback, bound to its number and purpose.

    Ids are never reused (rows are kept), and the digest means an "always" answer
    cannot carry over to a different number or purpose.
    """
    digest = hashlib.sha256(f'{row["id"]}|{row["destination"]}|{row["brief"]}'.encode()).hexdigest()[:12]
    return f"{RULE_PREFIX}:{row['id']}:{digest}"


def approval_message(row: dict) -> str:
    who = row["staff_member"] or "not given"
    return (f"Place a phone call to {row['destination']}?\n"
            f"Purpose: {row['brief']}\n"
            f"Requested by: {who} (unverified). The assistant speaks on the call when they answer.")


def dial_problem(row: dict | None, cb_id: str) -> str:
    """Why this callback cannot be dialed now, or "" (checked before the card and again before dialing)."""
    if row is None:
        return f"No callback {cb_id}."
    if row["status"] != "pending":
        return f"{cb_id} is {row['status']}; only pending callbacks can be placed."
    if not (secret("TWILIO_ACCOUNT_SID") and secret("TWILIO_AUTH_TOKEN") and secret("TWILIO_PHONE_NUMBER")
            and secret("VOICE_WEBHOOK_URL").strip().startswith("https://")):
        return "Cannot dial: set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER and an https VOICE_WEBHOOK_URL."
    if row["destination"] not in parse_allowed(secret("VOICE_ALLOWED_USERS")):
        return f"Refused: {row['destination']} is no longer on VOICE_ALLOWED_USERS."
    return ""


def approval_directive(args) -> dict:
    """``pre_tool_call`` result for ``voice_place_callback``: an approval card, or a block."""
    from . import _engine

    cb_id = str((args or {}).get("callback_id", "")).strip().lower()
    if not ID_RE.match(cb_id):
        return {"action": "block", "message": "Give the callback_id returned by voice_propose_callback."}
    try:
        with open_store() as store:
            row = store.get(cb_id)
    except sqlite3.Error:
        logger.exception("[voice] callback storage error")
        return {"action": "block", "message": "Callback storage is unavailable right now; nothing was dialed."}
    problem = dial_problem(row, cb_id)
    if problem:
        return {"action": "block", "message": problem}
    try:
        problem = _engine.approval_bypass_problem()
    except Exception as exc:  # contract drift or a gate error: never let the dial through unasked
        logger.warning("[voice] approval gate check failed: %s", exc)
        problem = "the approval gate could not be checked"
    if problem:
        return {"action": "block", "message": f"Not dialed: {problem}. Ask again from a chat where the owner can approve it."}
    return {"action": "approve", "rule_key": rule_key(row), "message": approval_message(row)}


async def place_callback(args: dict, **kwargs) -> str:
    """Dial an approved callback. Reached only after the approval card (``approval_directive``) was approved."""
    cb_id = str((args or {}).get("callback_id", "")).strip().lower()
    if not ID_RE.match(cb_id):
        return json.dumps({"ok": False, "error": "give the callback_id returned by voice_propose_callback"})
    try:
        with open_store() as store:
            message = await _dial(store, cb_id, store.get(cb_id))
    except sqlite3.Error:
        logger.exception("[voice] callback storage error")
        return json.dumps({"ok": False, "error": "callback storage is unavailable right now"})
    ok = message.startswith("Dialing")
    return json.dumps({"ok": ok, ("status" if ok else "error"): message})


# -- /voice-callback command (list, show, deny) -------------------------------

USAGE = (
    "Usage: /voice-callback list | show <id> | deny <id>\n"
    "Callbacks dial only after you approve the approval card that appears when the assistant places one."
)


def _describe(row: dict) -> str:
    who = row["staff_member"] or "unknown requester"
    return f'{row["id"]}  {row["destination"]}  ({row["status"]})  requested by {who} (unverified): {json.dumps(row["brief"])}'


async def callback_command(raw_args: str = "") -> str:
    """Slash command ``/voice-callback``: review and deny. Approval is the native approval card."""
    parts = (raw_args or "").split()
    action = parts[0].lower() if parts else "list"
    cb_id = parts[1].lower() if len(parts) > 1 else ""
    if action in ("show", "approve", "deny") and not ID_RE.match(cb_id):
        return "Give the callback id from /voice-callback list.\n" + USAGE
    try:
        return await _run_command(action, cb_id)
    except sqlite3.Error:
        logger.exception("[voice] callback storage error")
        return "Callback storage is unavailable right now; try again."


async def _run_command(action: str, cb_id: str) -> str:
    with open_store() as store:
        if action == "list":
            rows = store.pending()
            return "\n".join(_describe(r) for r in rows) if rows else "No callbacks are waiting for approval."
        row = store.get(cb_id) if cb_id else None
        if action == "show":
            return _describe(row) if row else f"No callback {cb_id}."
        if action == "deny":
            if row is None:
                return f"No callback {cb_id}."
            return f"Denied {cb_id}." if store.deny(cb_id) else f"{cb_id} is {row['status']}; nothing to deny."
        if action == "approve":
            # A slash command cannot raise the native approval card, so it no longer dials.
            return (f"Callbacks are approved on the approval card. Ask the assistant to place callback {cb_id}; "
                    "it dials only if you approve the card.")
    return USAGE


async def _dial(store: CallbackStore, cb_id: str, row: dict | None) -> str:
    problem = dial_problem(row, cb_id)
    if problem:
        return problem
    account_sid, auth_token = secret("TWILIO_ACCOUNT_SID"), secret("TWILIO_AUTH_TOKEN")
    from_number, base = secret("TWILIO_PHONE_NUMBER"), secret("VOICE_WEBHOOK_URL").strip().rstrip("/")
    if not store.claim_for_dial(cb_id):
        return f"{cb_id} was already handled."
    try:
        call = await asyncio.to_thread(
            twilio_rest.create_call, account_sid, auth_token,
            to=row["destination"], from_=from_number, url=f"{base}/voice/outbound?cb={cb_id}")
    except twilio_rest.TwilioError as exc:
        store.fail(cb_id, str(exc))
        logger.warning("[voice] callback %s failed (Twilio status %s)", cb_id, exc.status)
        return f"Twilio refused the call: {exc}"
    except Exception:
        store.fail(cb_id, "unexpected error while dialing")
        logger.exception("[voice] callback %s failed unexpectedly", cb_id)
        return "The call could not be placed (unexpected error). Check Twilio before placing a new request."
    store.set_call_sid(cb_id, call.get("sid", ""))
    logger.info("[voice] callback %s dialing %s", cb_id, redact_phone(row["destination"]))
    return f"Dialing {row['destination']} (call {call.get('sid', '?')}). The assistant will speak when they answer."
