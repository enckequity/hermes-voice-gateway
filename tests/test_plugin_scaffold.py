"""Plugin wiring: tools, hook, platform registration. Mirrors Hermes' loader."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _hermes_stubs import load_plugin  # noqa: E402


class FakeCtx:
    def __init__(self) -> None:
        self.tools, self.hooks, self.platforms, self.commands = [], [], [], []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append((name, toolset, handler.__name__))

    def register_hook(self, hook_name, callback, **kwargs):
        self.hooks.append((hook_name, callback.__name__))

    def register_platform(self, **kwargs):
        self.platforms.append(kwargs)

    def register_command(self, name, handler, **kwargs):
        self.commands.append((name, handler.__name__))


def test_register_wires_platform_tools_and_hook(monkeypatch) -> None:
    mod = load_plugin()
    ctx = FakeCtx()
    mod.register(ctx)

    assert all("." not in t[0] for t in ctx.tools)  # model APIs reject dots in tool names
    assert sorted(t[0] for t in ctx.tools) == [
        "voice_capture_lead",
        "voice_check_availability",
        "voice_check_call_status",
        "voice_place_callback",
        "voice_propose_callback",
    ]
    assert ctx.hooks == [("pre_tool_call", "on_pre_tool_call")]
    assert ctx.commands == [("voice-callback", "callback_command"), ("voice-leads", "leads_command")]
    assert not any("approve" in t[0] for t in ctx.tools)  # approval is the native card, never a tool
    assert {toolset for _, toolset, _ in ctx.tools} == {"voice", "voice_caller"}
    assert {name for name, toolset, _ in ctx.tools if toolset == "voice_caller"} == {
        "voice_capture_lead", "voice_check_availability",
    }
    (platform,) = ctx.platforms
    assert platform["name"] == "voice"
    assert platform["allowed_users_env"] == "VOICE_ALLOWED_USERS"
    assert "VOICE_WEBHOOK_URL" in platform["required_env"]
    assert "VOICE_ELEVENLABS_API_KEY" in platform["required_env"]
    assert "ELEVENLABS_API_KEY" not in platform["required_env"]
    assert "pipecat-ai[elevenlabs,silero,websocket]==1.7.0" in platform["install_hint"]


def test_platform_factory_is_lazy_and_requirements_probe_is_passive(monkeypatch) -> None:
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "VOICE_ELEVENLABS_API_KEY", "VOICE_WEBHOOK_URL"):
        monkeypatch.delenv(var, raising=False)
    mod = load_plugin()
    assert mod._check_requirements() is False
    assert mod._env_enablement() is None
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "VOICE_ELEVENLABS_API_KEY"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("VOICE_WEBHOOK_URL", "https://example.test")
    assert mod._env_enablement() == {"webhook_url": "https://example.test"}


def test_a_shared_elevenlabs_key_does_not_enable_the_platform(monkeypatch) -> None:
    monkeypatch.delenv("VOICE_ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", "shared")
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        monkeypatch.setenv(var, "x")
    monkeypatch.setenv("VOICE_WEBHOOK_URL", "https://example.test")
    mod = load_plugin()
    assert mod._env_enablement() is None
    assert mod._check_requirements() is False


def test_hook_blocks_tools_missing_their_env_and_allows_others(monkeypatch) -> None:
    for var in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    mod = load_plugin()
    blocked = mod.on_pre_tool_call(tool_name="voice_check_call_status")
    assert blocked["action"] == "block" and "Hermes" not in blocked["message"]
    assert mod.on_pre_tool_call(tool_name="files.read") is None

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "a")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "b")
    assert mod.on_pre_tool_call(tool_name="voice_check_call_status") is None


def test_local_dev_pipeline_is_gone() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[1]
    for gone in ("pipeline.py", "refurbo_demo.py", "frontend", "scripts", "voice/refurbo-inventory.json"):
        assert not (root / gone).exists(), gone
    manifest = (root / "plugin.yaml").read_text()
    assert "voice_start_local_pipeline" not in manifest and "OPENAI_API_KEY" not in manifest
    assert "VOICE_INSECURE_NO_SIGNATURE" not in manifest
    includes = tomllib.loads((root / "pyproject.toml").read_text())["tool"]["hatch"]["build"]["only-include"]
    assert {"_engine.py", "hours.py", "caller_tools.py", "callbacks.py"} <= set(includes)
    assert not {"pipeline.py", "refurbo_demo.py", "voice/refurbo-inventory.json"} & set(includes)


def test_no_engine_name_in_what_callers_hear_or_the_owner_sees() -> None:
    import importlib
    import re

    mod = load_plugin()
    vb = importlib.import_module(mod.__name__ + ".voice_behavior")
    ct = importlib.import_module(mod.__name__ + ".caller_tools")
    cb = importlib.import_module(mod.__name__ + ".callbacks")
    hours = importlib.import_module(mod.__name__ + ".hours")
    ctx = FakeCtx()
    descriptions = []
    ctx.register_tool = lambda name, toolset, schema, handler, **kw: descriptions.append(
        (kw.get("description", ""), repr(schema)))
    ctx.register_command = lambda name, handler, **kw: descriptions.append((kw.get("description", ""), ""))
    mod.register(ctx)
    spoken = [vb.DEFAULT_GREETING, vb.DEFAULT_PROFILE, vb.ERROR_REPLY, vb.ERROR_GOODBYE, vb.IDLE_PROMPT,
              vb.IDLE_GOODBYE, vb.NOT_UNDERSTOOD, ct.CALLER_INSTRUCTIONS, ct.LEAD_NOTICE, hours.NOTICE,
              mod._PLATFORM_HINT, cb.USAGE,
              cb.approval_message({"destination": "+15550001111", "brief": "b", "staff_member": ""})]
    root = Path(__file__).resolve().parents[1]
    spoken.append((root / "voice" / "PROFILE.md").read_text())
    spoken.append(re.search(r'^description: "(.*)"$', (root / "plugin.yaml").read_text(), re.M).group(1))
    for text in spoken + [d for pair in descriptions for d in pair]:
        assert "hermes" not in text.lower(), text
