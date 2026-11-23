"""Voice gateway plugin: ``register(ctx)`` entry point.

Registers a ``voice`` platform (Twilio phone calls, see ``adapter.py``), operator
callback tools (the dial waits on the native approval card), restricted caller
tools (business-hours availability and lead capture), the ``/voice-callback`` and
``/voice-leads`` review commands, and a ``pre_tool_call`` guard. Nothing here
imports engine internals, Pipecat or Twilio at load time.
"""
from __future__ import annotations

import importlib.util
import logging

from ._env import secret
from . import caller_tools
from .hooks import on_pre_tool_call
from .schemas import (
    CALLER_AVAILABILITY_SCHEMA,
    CALLER_LEAD_SCHEMA,
    CHECK_STATUS_SCHEMA,
    PLACE_CALLBACK_SCHEMA,
    PROPOSE_CALLBACK_SCHEMA,
)
from .callbacks import callback_command, place_callback
from .tools import check_call_status, leads_command, propose_callback

logger = logging.getLogger(__name__)

TOOLSET = "voice"
# The business's own ElevenLabs key, under a voice-specific name so a generic or
# shared ELEVENLABS_API_KEY elsewhere in the environment is never picked up.
SPEECH_KEY_ENV = "VOICE_ELEVENLABS_API_KEY"
PLATFORM_ENV = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", SPEECH_KEY_ENV, "VOICE_WEBHOOK_URL")
INSTALL_HINT = "pip install 'pipecat-ai[elevenlabs,silero,websocket]==1.7.0' 'uvicorn>=0.32,<1'"

_PLATFORM_HINT = (
    "You are speaking with a caller on a live phone call. Reply in short, natural spoken "
    "sentences. No markdown, lists, links or emojis. If you need a moment, say so briefly."
)


def _make_adapter(config):
    from .adapter import VoiceAdapter  # imported late: needs the engine's gateway package

    return VoiceAdapter(config)


def _check_requirements() -> bool:
    """Passive probe: credentials (including the business's own speech key) set and the call-server libraries importable."""
    if not all(secret(name) for name in PLATFORM_ENV):
        return False
    return all(importlib.util.find_spec(mod) for mod in ("fastapi", "uvicorn", "pipecat"))


def _env_enablement():
    """Auto-enable the platform when its credentials are present."""
    if not all(secret(name) for name in PLATFORM_ENV):
        return None
    return {"webhook_url": secret("VOICE_WEBHOOK_URL")}


def register(ctx):
    """Register the voice platform, tools, commands and the pre_tool_call guard."""
    ctx.register_platform(
        name="voice",
        label="Voice (Twilio calls)",
        adapter_factory=_make_adapter,
        check_fn=_check_requirements,
        required_env=list(PLATFORM_ENV),
        install_hint=INSTALL_HINT,
        env_enablement_fn=_env_enablement,
        allowed_users_env="VOICE_ALLOWED_USERS",
        allow_all_env="VOICE_ALLOW_ALL_USERS",
        allowlist_suppresses_pairing=True,
        max_message_length=1200,
        platform_hint=_PLATFORM_HINT,
        pii_safe=True,
        emoji="📞",
    )
    ctx.register_tool(
        name="voice_propose_callback",
        toolset=TOOLSET,
        schema=PROPOSE_CALLBACK_SCHEMA,
        handler=propose_callback,
        description=(
            "Record a phone callback request (number on the allowed list and a one-line purpose). "
            "It is not dialed: follow with voice_place_callback, which needs the person's approval."
        ),
    )
    ctx.register_tool(
        name="voice_place_callback",
        toolset=TOOLSET,
        schema=PLACE_CALLBACK_SCHEMA,
        handler=place_callback,
        is_async=True,
        description=(
            "Dial a recorded callback. The person sees an approval card with the number and purpose; "
            "the call is placed only if they approve it."
        ),
    )
    ctx.register_tool(
        name="voice_check_call_status",
        toolset=TOOLSET,
        schema=CHECK_STATUS_SCHEMA,
        handler=check_call_status,
        description="Look up a callback's call status, by callback id or call SID.",
    )
    ctx.register_command(
        "voice-callback",
        callback_command,
        description="Review or deny callbacks the assistant proposed (each dial needs your approval card).",
        args_hint="list | show <id> | deny <id>",
    )
    ctx.register_command(
        "voice-leads",
        leads_command,
        description="Show the latest messages callers left on the phone line.",
        args_hint="[count]",
    )
    ctx.register_tool(
        name="voice_check_availability",
        toolset=caller_tools.TOOLSET,
        schema=CALLER_AVAILABILITY_SCHEMA,
        handler=caller_tools.check_availability,
        description="Read the business's opening times for a day or a few days. Read-only: nothing is booked.",
    )
    ctx.register_tool(
        name="voice_capture_lead",
        toolset=caller_tools.TOOLSET,
        schema=CALLER_LEAD_SCHEMA,
        handler=caller_tools.capture_lead,
        description=(
            "Save this caller's name, number and reason for the business to follow up, after explicit consent. "
            "The number is the live caller's. Nobody is called back or booked by saving."
        ),
    )
    ctx.register_hook("pre_tool_call", on_pre_tool_call)

    logger.info("voice gateway registered (voice platform, 5 tools, 2 commands, 1 hook)")
