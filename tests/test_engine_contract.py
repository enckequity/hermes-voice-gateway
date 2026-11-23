"""Against the real engine (hermes-agent v0.23.3 source): the private symbols exist and the
callback approval directive is read as a native approval with our rule key.

Skipped unless the engine is importable, e.g.
``PYTHONPATH=<hermes-agent v0.23.3 checkout> python -m pytest tests/test_engine_contract.py``.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
from pathlib import Path

import pytest

engine_plugins = pytest.importorskip("hermes_cli.plugins")
engine_approval = pytest.importorskip("tools.approval")
engine_tools_config = pytest.importorskip("hermes_cli.tools_config")
pytest.importorskip("gateway.session_context")  # real gateway package, restored after the stubs load

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hermes_stubs import PACKAGE, load_plugin, use_memory_db  # noqa: E402

DEST = "+15550001111"


@pytest.fixture(autouse=True)
def real_gateway():
    """The stubs replace the engine's gateway package for the adapter; put the real one back."""
    saved = {k: v for k, v in sys.modules.items() if k == "gateway" or k.startswith("gateway.")}
    yield _restore(saved)
    _restore(saved)()


def _restore(saved):
    def restore():
        for k in [k for k in sys.modules if k == "gateway" or k.startswith("gateway.")]:
            del sys.modules[k]
        sys.modules.update(saved)
    return restore


@pytest.fixture()
def plugin(monkeypatch, real_gateway, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # never read the host's engine config
    for k, v in {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_PHONE_NUMBER": "+15559990000",
                 "VOICE_WEBHOOK_URL": "https://example.test", "VOICE_ALLOWED_USERS": DEST}.items():
        monkeypatch.setenv(k, v)
    mod = load_plugin()
    real_gateway()
    use_memory_db(monkeypatch)
    tools = importlib.import_module(PACKAGE + ".tools")
    cb = json.loads(tools.propose_callback({"destination": DEST, "brief": "Confirm Tuesday"}))["callback_id"]
    yield mod, cb


def test_private_symbols_exist_with_the_expected_shape() -> None:
    load_plugin()
    engine = importlib.import_module(PACKAGE + "._engine")
    resolver = engine.platform_tools_resolver()
    assert resolver is engine_tools_config._get_platform_tools
    assert list(inspect.signature(resolver).parameters)[:2] == ["config", "platform"]
    assert len(engine_approval._presence()) == 4


def test_no_human_present_is_refused_by_our_hook(plugin, monkeypatch) -> None:
    mod, cb = plugin
    for var in ("HERMES_INTERACTIVE", "HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK", "HERMES_YOLO_MODE"):
        monkeypatch.delenv(var, raising=False)
    out = mod.on_pre_tool_call(tool_name="voice_place_callback", args={"callback_id": cb})
    assert out["action"] == "block" and "no one is present" in out["message"]


def test_engine_reads_our_directive_as_a_native_approval_with_our_rule_key(plugin, monkeypatch) -> None:
    mod, cb = plugin
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook",
                        lambda name, **kw: [mod.on_pre_tool_call(**kw)] if name == "pre_tool_call" else [])
    details = engine_plugins._get_pre_tool_call_directive_details("voice_place_callback", {"callback_id": cb})
    assert details.action == "approve"
    assert details.rule_key.startswith(f"voice_callback:{cb}:")
    assert DEST in details.message

    asked = []

    def deny(tool_name, reason, *, rule_key="", approval_callback=None):
        asked.append(rule_key)
        return {"approved": False, "message": "BLOCKED: denied by the person"}

    monkeypatch.setattr(engine_approval, "request_tool_approval", deny)
    assert engine_plugins._resolve_block_from_details(details, "voice_place_callback") == "BLOCKED: denied by the person"
    assert asked == [details.rule_key]

    monkeypatch.setattr(engine_approval, "request_tool_approval", lambda *a, **k: {"approved": True})
    assert engine_plugins._resolve_block_from_details(details, "voice_place_callback") is None

    # Approved: the handler dials exactly once.
    rest = importlib.import_module(PACKAGE + ".twilio_rest")
    calls = []
    monkeypatch.setattr(rest, "create_call", lambda *a, **k: calls.append(k) or {"sid": "CA" + "b" * 32})
    callbacks = importlib.import_module(PACKAGE + ".callbacks")
    out = json.loads(asyncio.run(callbacks.place_callback({"callback_id": cb})))
    assert out["ok"] and len(calls) == 1


def test_a_block_from_any_plugin_beats_our_approve(plugin, monkeypatch) -> None:
    mod, cb = plugin
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda name, **kw: [
        mod.on_pre_tool_call(**kw), {"action": "block", "message": "other plugin says no"}])
    details = engine_plugins._get_pre_tool_call_directive_details("voice_place_callback", {"callback_id": cb})
    assert details.action == "block"
