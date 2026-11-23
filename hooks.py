"""Lifecycle hooks for the voice gateway.

``on_pre_tool_call`` restricts phone callers to their two live-turn tools, puts
every callback dial behind the engine's native approval card, and checks
credentials for the other operator-side voice tools. Permission errors deny access.
"""
from __future__ import annotations

import logging
from contextlib import suppress

from . import callbacks, caller_tools
from ._env import secret

logger = logging.getLogger(__name__)

TWILIO_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")


def on_pre_tool_call(tool_name: str = "", args=None, **kwargs):
    """Return ``{"action": "block", "message"}`` to veto, ``{"action": "approve", "rule_key",
    "message"}`` to require the native approval card, or ``None`` to allow.

    Engine contract (checked against hermes-agent v0.23.3): any block wins over any
    approve; an approve whose gate errors, is denied or times out is blocked.
    """
    # The engine fails open when a hook raises. Default to denial if identity or
    # permission lookup itself breaks; never leak exception details to callers.
    voice_context = None
    with suppress(Exception):
        voice_context = caller_tools.is_voice_context()
    if voice_context is None:
        return {"action": "block", "message": "Caller identity validation is unavailable."}
    if voice_context or tool_name in caller_tools.TOOL_NAMES:
        if tool_name not in caller_tools.TOOL_NAMES:
            return {"action": "block", "message": "Phone callers may only check opening times and leave a message."}
        with suppress(Exception):
            caller_tools.require_access()
            return None
        return {"action": "block", "message": "A currently authorized phone turn is required for this tool."}
    if not str(tool_name).startswith("voice_"):
        return None
    if tool_name == "voice_place_callback":
        try:
            return callbacks.approval_directive(args)
        except Exception:
            logger.exception("[voice] callback approval check failed")
            return {"action": "block", "message": "The callback could not be checked; nothing was dialed."}

    missing = [v for v in TWILIO_ENV if not secret(v)]
    if missing:
        message = (
            "Voice calls are not configured: missing " + ", ".join(missing)
            + "; set them before calling " + tool_name
        )
        logger.warning("blocking %s: %s", tool_name, message)
        return {"action": "block", "message": message}
    return None
