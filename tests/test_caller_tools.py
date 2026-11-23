"""Caller tools bind permissions and lead ownership to trusted live turns."""
from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hermes_stubs import PACKAGE, load_plugin, use_memory_db


@pytest.fixture()
def caller(monkeypatch, tmp_path):
    load_plugin()
    conn = use_memory_db(monkeypatch)
    module = importlib.import_module(PACKAGE + ".caller_tools")
    identity = module.CallerIdentity("session-one", "+15550001111", "CA-one", "CA-one-1")
    monkeypatch.setattr(module, "current_identity", lambda: identity)
    module.grant_turn(identity, ttl=300, is_active=lambda: True)
    hours_file = tmp_path / "hours.json"
    hours_file.write_text(json.dumps({"timezone": "UTC", "weekly": {d: ["09:00-17:00"] for d in
                                      ("mon", "tue", "wed", "thu", "fri")}}))
    monkeypatch.setenv("VOICE_HOURS_PATH", str(hours_file))
    yield module, identity, conn
    module.revoke_turn(identity)
    conn.close()


LEAD = {"name": "Pat Doe", "reason": "Leaking water heater, wants a quote", "consent": True}


def test_availability_reads_the_hours_file_only_and_books_nothing(caller):
    from datetime import datetime, timedelta, timezone

    module, _identity, _conn = caller
    today = datetime.now(timezone.utc).date()
    monday = today + timedelta(days=7 - today.weekday())
    result = json.loads(module.check_availability({"date": monday.isoformat(), "days": 2}))
    assert result["ok"] and result["booked"] is False and result["timezone"] == "UTC"
    assert [d["open"] for d in result["days"]] == [["09:00-17:00"], ["09:00-17:00"]]
    assert "nothing has been booked" in result["notice"].lower()


def test_lead_persists_caller_number_and_retry_is_idempotent(caller):
    module, identity, conn = caller
    first = json.loads(module.capture_lead(LEAD))
    assert first["ok"] and first["status"] == "saved_for_follow_up"
    assert first["called_back"] is False and first["booked"] is False
    second = json.loads(module.capture_lead(LEAD))
    assert second["lead_id"] == first["lead_id"]
    rows = conn.execute("SELECT id, call_sid, phone, name, reason, callback_number FROM voice_leads").fetchall()
    assert rows == [(first["lead_id"], identity.call_sid, identity.caller, LEAD["name"], LEAD["reason"], "")]


def test_lead_keeps_an_offered_callback_number_but_not_as_owner(caller):
    module, identity, conn = caller
    out = json.loads(module.capture_lead({**LEAD, "callback_number": "+15557654321"}))
    assert out["ok"]
    assert conn.execute("SELECT phone, callback_number FROM voice_leads").fetchone() == (
        identity.caller, "+15557654321")


def test_leads_command_lists_saved_messages(caller):
    module, identity, _conn = caller
    tools = importlib.import_module(PACKAGE + ".tools")
    assert "No phone messages" in asyncio.run(tools.leads_command(""))
    module.capture_lead(LEAD)
    listing = asyncio.run(tools.leads_command("5"))
    assert LEAD["name"] in listing and identity.caller in listing and "water heater" in listing


@pytest.mark.parametrize("tool", ["terminal", "files.read", "delegate_task", "execute_code", "tool_call",
                                   "voice_propose_callback", "voice_place_callback", "voice_check_call_status"])
def test_voice_hook_denies_every_tool_outside_the_caller_allowlist(caller, monkeypatch, tool):
    module, _identity, _conn = caller
    hooks = importlib.import_module(PACKAGE + ".hooks")
    monkeypatch.setattr(module, "is_voice_context", lambda: True, raising=False)
    result = hooks.on_pre_tool_call(tool_name=tool)
    assert result is not None and result["action"] == "block"


def test_adapter_grants_only_the_live_turn_and_revokes_on_completion(caller, monkeypatch):
    module, _identity, conn = caller
    admod = importlib.import_module(PACKAGE + ".adapter")
    adapter = admod.VoiceAdapter(config=object())
    call = admod._Call(caller="+15550002222", call_sid="CA-live")
    adapter._calls[call.caller] = call
    hooks = importlib.import_module(PACKAGE + ".hooks")
    monkeypatch.setattr(module, "verify_tool_surface", lambda: None)
    monkeypatch.setattr(module, "is_voice_context", lambda: True)
    checked = []

    async def scenario():
        async def handle(event):
            identity = module.CallerIdentity(adapter._event_session_key(event), call.caller,
                                             call.call_sid, event.message_id)
            monkeypatch.setattr(module, "current_identity", lambda: identity)
            assert adapter.toolsets_for_source(event.source) == ["voice_caller", "no_mcp"]
            for name in module.TOOL_NAMES:
                assert hooks.on_pre_tool_call(tool_name=name) is None
            event._gateway_accepted = True
            await adapter.on_processing_complete(event, "success")
            for name in module.TOOL_NAMES:
                assert hooks.on_pre_tool_call(tool_name=name)["action"] == "block"
            checked.append(True)

        monkeypatch.setattr(adapter, "handle_message", handle)
        adapter._relay_to_hermes(call, "are you open tomorrow")
        await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), 2)
        assert checked == [True]
        assert call.event_outcomes == {"CA-live-1": "success"}
        assert conn.execute("SELECT COUNT(*) FROM voice_caller_grants WHERE call_sid='CA-live'").fetchone() == (0,)

    asyncio.run(scenario())


@pytest.mark.parametrize("args", [
    {"hours_path": "/etc/passwd"}, {"phone": "+15550002222"},
    {"days": 8}, {"days": 0}, {"days": True}, {"days": "2"}, {"date": None}, {"date": "tomorrow"},
    {"date": "2000-01-01"},
])
def test_availability_rejects_paths_identity_overrides_and_invalid_filters(caller, args):
    module, _identity, _conn = caller
    assert json.loads(module.check_availability(args))["ok"] is False


@pytest.mark.parametrize("field,value", [
    ("consent", False), ("consent", "true"), ("name", " "), ("reason", ""),
    ("reason", "x" * 1001), ("callback_number", "555-1234"), ("callback_number", 15551234567),
    ("phone", "+15550002222"), ("call_sid", "CA-other"),
    ("lead_id", "another-persons-record"), ("db_path", "/etc/passwd"),
])
def test_invalid_or_owner_overriding_lead_is_not_saved(caller, field, value):
    module, _identity, conn = caller
    args = {**LEAD, field: value}
    assert json.loads(module.capture_lead(args))["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM voice_leads").fetchone() == (0,)


def test_conflicting_retry_does_not_replace_lead(caller):
    module, _identity, conn = caller
    assert json.loads(module.capture_lead(LEAD))["ok"]
    assert json.loads(module.capture_lead({**LEAD, "reason": "Different"}))["ok"] is False
    assert conn.execute("SELECT reason FROM voice_leads").fetchone() == (LEAD["reason"],)


@pytest.mark.parametrize("field,value", [
    ("session_key", "other-session"), ("caller", "+15550003333"),
    ("call_sid", "CA-other"), ("message_id", "CA-one-2"),
])
def test_cross_session_caller_call_or_turn_cannot_borrow_permission(caller, monkeypatch, field, value):
    module, identity, conn = caller
    monkeypatch.setattr(module, "current_identity", lambda: replace(identity, **{field: value}))
    assert json.loads(module.check_availability({}))["ok"] is False
    assert json.loads(module.capture_lead(LEAD))["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM voice_leads").fetchone() == (0,)


@pytest.mark.parametrize("state", ["revoked", "expired", "other_process", "hung_up"])
def test_inactive_permissions_deny_both_handlers_and_hook(caller, monkeypatch, state):
    module, identity, conn = caller
    hooks = importlib.import_module(PACKAGE + ".hooks")
    monkeypatch.setattr(module, "is_voice_context", lambda: True)
    if state == "revoked":
        module.revoke_turn(identity)
    elif state == "expired":
        conn.execute("UPDATE voice_caller_grants SET expires_at=0")
        conn.commit()
    elif state == "other_process":
        module._live_turns.clear()
    else:
        module._live_turns[identity] = lambda: False
    assert json.loads(module.check_availability({}))["ok"] is False
    assert json.loads(module.capture_lead(LEAD))["ok"] is False
    assert hooks.on_pre_tool_call(tool_name="voice_check_availability")["action"] == "block"
    assert hooks.on_pre_tool_call(tool_name="voice_capture_lead")["action"] == "block"


def test_revocation_storage_failure_still_closes_local_permission(caller, monkeypatch):
    module, identity, _conn = caller

    def fail_revoke(self, identity):
        raise sqlite3.OperationalError("storage unavailable")

    with monkeypatch.context() as patcher:
        patcher.setattr(module.CallerStore, "revoke", fail_revoke)
        with pytest.raises(sqlite3.OperationalError):
            module.revoke_turn(identity)
        assert json.loads(module.check_availability({}))["ok"] is False


@pytest.mark.parametrize("missing", ["bound_key", "platform", "thread_id", "message_id"])
def test_real_identity_reader_rejects_env_only_or_incomplete_context(monkeypatch, missing):
    load_plugin()
    module = importlib.import_module(PACKAGE + ".caller_tools")
    context = ModuleType("gateway.session_context")
    values = {"HERMES_SESSION_PLATFORM": "voice", "HERMES_SESSION_CHAT_ID": "+15550001111",
              "HERMES_SESSION_THREAD_ID": "CA-one", "HERMES_SESSION_MESSAGE_ID": "CA-one-1"}
    if missing == "platform":
        values["HERMES_SESSION_PLATFORM"] = "telegram"
    elif missing != "bound_key":
        values["HERMES_SESSION_" + missing.upper()] = ""
    context.__dict__.update(
        get_session_env=lambda name, default="": values.get(name, default),
        bound_session_key=lambda: "" if missing == "bound_key" else "session-one",
    )
    monkeypatch.setitem(sys.modules, "gateway.session_context", context)
    assert json.loads(module.check_availability({}, caller="+15550001111", call_sid="CA-one"))["ok"] is False
    assert json.loads(module.capture_lead(LEAD))["ok"] is False


@pytest.mark.parametrize("memory", [
    {"memory_enabled": False, "user_profile_enabled": False},
    {}, None, "invalid", [],
    {"memory_enabled": False},
    {"user_profile_enabled": False},
    {"memory_enabled": False, "user_profile_enabled": False, "provider": None},
    {"memory_enabled": False, "user_profile_enabled": False, "provider": ""},
    {"memory_enabled": False, "user_profile_enabled": False, "provider": False},
    {"memory_enabled": True, "user_profile_enabled": False},
    {"memory_enabled": False, "user_profile_enabled": True},
    {"memory_enabled": "false", "user_profile_enabled": False},
    {"memory_enabled": False, "user_profile_enabled": 0},
    {"memory_enabled": False, "user_profile_enabled": False, "provider": "honcho"},
    {"memory_enabled": False, "user_profile_enabled": False, "provider": "hindsight"},
])
@pytest.mark.parametrize("extra", [None, "terminal", "delegate_task", "tool_call"])
def test_effective_tool_surface_must_equal_the_two_caller_tools(caller, monkeypatch, extra, memory):
    module, _identity, _conn = caller
    context = ModuleType("gateway.session_context")
    context.__dict__.update(bound_session_key=lambda: "", get_session_env=lambda name: "")
    skill_utils = ModuleType("agent.skill_utils")
    skill_utils.__dict__["parse_config_string_list"] = lambda value: value or []
    config = ModuleType("hermes_cli.config")
    config.__dict__["load_config"] = lambda: {"platform_toolsets": {"voice": ["terminal"]}, "memory": memory}
    tool_config = ModuleType("hermes_cli.tools_config")

    def resolve(config, platform):
        assert platform == "voice"
        assert config["platform_toolsets"]["voice"] == ["voice_caller", "no_mcp"]
        return {"voice_caller"}

    tool_config.__dict__["_get_platform_tools"] = resolve
    model_tools = ModuleType("model_tools")
    names = set(module.TOOL_NAMES) | ({extra} if extra else set())
    model_tools.__dict__["get_tool_definitions"] = lambda **kwargs: [{"function": {"name": name}} for name in names]
    for fake in (context, skill_utils, config, tool_config, model_tools):
        monkeypatch.setitem(sys.modules, fake.__name__, fake)
    safe_memory = (isinstance(memory, dict) and memory.get("memory_enabled") is False
                   and memory.get("user_profile_enabled") is False and memory.get("provider") in (None, ""))
    if extra or not safe_memory:
        with pytest.raises(PermissionError):
            module.verify_tool_surface()
    else:
        module.verify_tool_surface()


def test_concurrent_duplicate_leads_are_committed_once(caller, monkeypatch, tmp_path):
    module, identity, _conn = caller
    path = tmp_path / "caller.db"
    monkeypatch.setattr(module.callbacks, "_connect", lambda: sqlite3.connect(path))
    monkeypatch.setattr(module.callbacks, "_release", lambda conn: conn.close())
    module.grant_turn(identity, ttl=300, is_active=lambda: True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: json.loads(module.capture_lead(LEAD)), range(2)))
    assert all(result["ok"] for result in results)
    assert results[0]["lead_id"] == results[1]["lead_id"]
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_leads").fetchone() == (1,)
        assert conn.execute("SELECT name, reason FROM voice_leads").fetchone() == (LEAD["name"], LEAD["reason"])


def test_late_revoke_cannot_remove_the_next_turns_permission(caller):
    module, identity, _conn = caller
    module.revoke_turn(identity)
    next_turn = replace(identity, message_id="CA-one-2")
    module.grant_turn(next_turn, ttl=300, is_active=lambda: True)
    try:
        module.revoke_turn(identity)
        with module.open_caller_store() as store:
            store.authorize(next_turn)
    finally:
        module.revoke_turn(next_turn)


def test_a_live_lease_cannot_be_replaced_by_a_different_turn(caller):
    module, identity, _conn = caller
    with pytest.raises(PermissionError):
        module.grant_turn(replace(identity, message_id="CA-one-2"), ttl=300, is_active=lambda: True)
    assert module.require_access() == identity


@pytest.mark.parametrize("check", ["is_voice_context", "require_access"])
def test_unexpected_authorization_errors_return_a_block_not_a_fail_open_exception(caller, monkeypatch, check):
    module, _identity, _conn = caller
    hooks = importlib.import_module(PACKAGE + ".hooks")

    def broken(*args, **kwargs):
        raise RuntimeError("permission service failed")

    monkeypatch.setattr(module, check, broken)
    assert hooks.on_pre_tool_call(tool_name="voice_check_availability")["action"] == "block"


def test_adapter_refuses_inference_when_tool_resolution_is_not_safe(caller, monkeypatch):
    module, _identity, conn = caller
    admod = importlib.import_module(PACKAGE + ".adapter")
    adapter = admod.VoiceAdapter(config=object())
    call = admod._Call(caller="+15550002222", call_sid="CA-unsafe")
    adapter._calls[call.caller] = call

    def unsafe():
        raise PermissionError("additional plugin tools were resolved")

    monkeypatch.setattr(module, "verify_tool_surface", unsafe)

    async def scenario():
        adapter._relay_to_hermes(call, "request")
        await asyncio.wait_for(asyncio.gather(*adapter._background_tasks), 2)
        assert adapter.handled == []
        assert call.event_outcomes == {"CA-unsafe-1": "failure"}
        assert not call.tool_grants
        assert conn.execute("SELECT COUNT(*) FROM voice_caller_grants WHERE call_sid='CA-unsafe'").fetchone() == (0,)

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["hangup", "timeout", "rejected", "cancelled"])
def test_adapter_terminal_paths_revoke_permissions(caller, monkeypatch, finish):
    module, _identity, conn = caller
    admod = importlib.import_module(PACKAGE + ".adapter")
    adapter = admod.VoiceAdapter(config=object())
    call = admod._Call(caller="+15550002222", call_sid="CA-terminal")
    adapter._calls[call.caller] = call
    monkeypatch.setattr(module, "verify_tool_surface", lambda: None)
    if finish == "timeout":
        monkeypatch.setattr(admod, "_INGRESS_WAIT_SECONDS", 0.01)

    async def scenario():
        entered = asyncio.Event()

        async def handle(event):
            identity = module.CallerIdentity(adapter._event_session_key(event), call.caller,
                                             call.call_sid, event.message_id)
            monkeypatch.setattr(module, "current_identity", lambda: identity)
            assert module.require_access() == identity
            event._gateway_accepted = finish != "rejected"
            entered.set()
            if finish == "cancelled":
                raise asyncio.CancelledError
            if finish != "rejected":
                await asyncio.Event().wait()

        monkeypatch.setattr(adapter, "handle_message", handle)
        adapter._relay_to_hermes(call, "request")
        tasks = list(adapter._background_tasks)
        await asyncio.wait_for(entered.wait(), 2)
        if finish == "hangup":
            await adapter._close_call_ingress(call)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)
        assert not call.tool_grants
        assert not call.pending_events
        assert json.loads(module.check_availability({}))["ok"] is False
        assert conn.execute("SELECT COUNT(*) FROM voice_caller_grants WHERE call_sid='CA-terminal'").fetchone() == (0,)

    asyncio.run(scenario())


def test_caller_schemas_and_distribution_do_not_expose_ownership_or_paths():
    module = load_plugin()
    for schema in (module.CALLER_AVAILABILITY_SCHEMA, module.CALLER_LEAD_SCHEMA):
        assert schema["additionalProperties"] is False
        assert not ({"caller", "phone", "destination", "call_sid", "session_key", "hours_path", "db_path"}
                    & set(schema["properties"]))
    assert module.CALLER_LEAD_SCHEMA["required"] == ["name", "reason", "consent"]
    assert module.CALLER_LEAD_SCHEMA["properties"]["consent"]["enum"] == [True]
    root = Path(__file__).resolve().parents[1]
    includes = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["only-include"]
    assert {"caller_tools.py", "callbacks.py", "twilio_rest.py", "hours.py", "_engine.py"} <= set(includes)


def test_missing_private_platform_tools_symbol_fails_clearly_and_closed(caller, monkeypatch):
    module, _identity, _conn = caller
    for name, attrs in (("gateway.session_context", {"bound_session_key": lambda: "", "get_session_env": lambda n: ""}),
                        ("agent.skill_utils", {"parse_config_string_list": lambda v: v or []}),
                        ("hermes_cli.config", {"load_config": lambda: {"memory": {
                            "memory_enabled": False, "user_profile_enabled": False}}}),
                        ("hermes_cli.tools_config", {}),  # engine without _get_platform_tools
                        ("model_tools", {"get_tool_definitions": lambda **kw: []})):
        fake = ModuleType(name)
        fake.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, fake)
    engine = importlib.import_module(PACKAGE + "._engine")
    for check in (module.verify_tool_surface, module.verify_runtime_contract):
        with pytest.raises(engine.EngineContractError, match="_get_platform_tools.*v0.23.3"):
            check()


@pytest.mark.parametrize("memory,ok", [
    ({"memory_enabled": False, "user_profile_enabled": False}, True),
    ({"memory_enabled": True, "user_profile_enabled": False}, False),
    ({"memory_enabled": False, "user_profile_enabled": False, "provider": "honcho"}, False),
    (None, False),
])
def test_startup_contract_requires_memory_off(caller, monkeypatch, memory, ok):
    module, _identity, _conn = caller
    config = ModuleType("hermes_cli.config")
    config.__dict__["load_config"] = lambda: {"memory": memory}
    tool_config = ModuleType("hermes_cli.tools_config")
    tool_config.__dict__["_get_platform_tools"] = lambda config, platform: set()
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)
    monkeypatch.setitem(sys.modules, "hermes_cli.tools_config", tool_config)
    if ok:
        module.verify_runtime_contract()
    else:
        with pytest.raises(PermissionError, match="memory"):
            module.verify_runtime_contract()
