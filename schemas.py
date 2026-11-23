"""Input schemas for the voice gateway tools.

Each schema is a JSON-schema-ish mapping describing what the LLM may pass to
the tool. Hermes validates tool calls against these schemas before invoking
the handler.
"""
from __future__ import annotations

PROPOSE_CALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "destination": {
            "type": "string",
            "description": "Phone number to call back (E.164). Must be on the gateway's allowed list.",
        },
        "brief": {
            "type": "string",
            "description": "One sentence on why the call is made. It is shown on the approval card, and it is given to the assistant as the call's purpose.",
        },
        "staff_member": {
            "type": "string",
            "description": "Staff member who asked for the callback.",
        },
    },
    "required": ["destination", "brief"],
    "additionalProperties": False,
}

CHECK_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "call_sid": {
            "type": "string",
            "description": "Twilio Call SID (CA followed by 32 hex characters). Give this or callback_id.",
        },
        "callback_id": {
            "type": "string",
            "description": "Id returned by voice_propose_callback. Reports the callback and its call.",
        },
    },
    "additionalProperties": False,
}

PLACE_CALLBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "callback_id": {
            "type": "string",
            "description": "Id returned by voice_propose_callback. The person is shown an approval card; nothing dials unless they approve it.",
        },
    },
    "required": ["callback_id"],
    "additionalProperties": False,
}

CALLER_AVAILABILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string", "maxLength": 10,
                 "description": "First day to check, YYYY-MM-DD in the business's timezone. Default today."},
        "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "How many days to show (default 1)."},
    },
    "additionalProperties": False,
}

CALLER_LEAD_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Caller's name as they gave it."},
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000,
                   "description": "Why they called / what they need, confirmed with the caller."},
        "callback_number": {"type": "string", "maxLength": 16,
                            "description": "Only if the caller asks to be reached on a different number (E.164). "
                                           "Their calling number is recorded automatically."},
        "consent": {"type": "boolean", "enum": [True],
                    "description": "Caller explicitly agreed to saving their name, number and reason for follow-up."},
    },
    "required": ["name", "reason", "consent"],
    "additionalProperties": False,
}
