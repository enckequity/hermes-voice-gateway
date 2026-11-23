"""The few private engine symbols this plugin relies on, in one place.

Each lookup fails with ``EngineContractError`` naming the missing symbol and the
engine release the plugin was checked against, so an engine upgrade that renames
or drops one is refused clearly (callers fail closed) instead of failing open or
raising a bare ``ImportError`` mid-call. Pin the engine and re-verify here when
moving it.
"""
from __future__ import annotations

import importlib

CHECKED_ENGINE = "hermes-agent v0.23.3"


class EngineContractError(RuntimeError):
    """A private engine symbol this plugin needs is missing or has changed."""


def _symbol(module: str, name: str):
    try:
        value = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise EngineContractError(
            f"the voice gateway needs {module}.{name} (checked against {CHECKED_ENGINE}); "
            "this engine does not provide it, so phone turns and callbacks are refused"
        ) from exc
    if not callable(value):
        raise EngineContractError(f"{module}.{name} is not callable in this engine ({CHECKED_ENGINE} expected)")
    return value


def platform_tools_resolver():
    """``hermes_cli.tools_config._get_platform_tools(config, platform) -> set[str]`` (private)."""
    return _symbol("hermes_cli.tools_config", "_get_platform_tools")


def approval_bypass_problem() -> str:
    """Why the native approval gate would NOT ask a person right now, or "" when it would.

    The gate auto-approves under yolo or ``approvals.mode: off``, and resolves cron,
    single-query and unattended-platform turns from config instead of asking anyone.
    A callback must always be put in front of a person, so those contexts are refused.
    """
    presence = _symbol("tools.approval", "_presence")
    yolo_active = _symbol("tools.approval", "_yolo_active")
    approval_mode = _symbol("tools.approval_context", "_get_approval_mode")
    _callback, is_cli, is_gateway, is_ask = presence()
    if yolo_active() or approval_mode() == "off":
        return "approvals are switched off for this session, so a callback cannot be approved by a person"
    if not (is_cli or is_gateway or is_ask):
        return "no one is present in this session to approve a callback (scheduled or unattended run)"
    return ""
