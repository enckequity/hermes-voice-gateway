# AGENTS.md

Hermes plugin (`kind: platform`) that answers Twilio phone calls, speaks the agent's replies with the business's own ElevenLabs key (Pipecat), tells callers opening times, takes messages, and places callbacks only after a native approval card. This is the enckequity fork (TeamShift #4638); see the README's fork note. See [README.md](README.md) for behaviour and setup, [docs/](docs/) for design notes.

## Layout
The repo root **is** the plugin package (flat modules, relative imports). Hermes loads it by file path.

- `__init__.py`: `register(ctx)` entry point. Must not import Hermes internals, Pipecat or Twilio at load time.
- `adapter.py`: Twilio call server (`/voice`, `/voice/outbound`, `/media`) and Hermes platform adapter.
- `callbacks.py`: callback proposals + `/voice-callback` operator approval; state in the plugin SQLite file.
- `tools.py` / `schemas.py`: agent tools (`voice_propose_callback`, `voice_check_call_status`), `/voice-leads`, and all schemas. `voice_place_callback` and its approval directive live in `callbacks.py`.
- `caller_tools.py`: restricted `voice_caller` availability/lead handlers, live-turn permissions and SQLite leads. `hours.py`: read-only business-hours file.
- `_engine.py`: every private engine symbol (`_get_platform_tools`, approval presence helpers) behind one guarded lookup that fails closed with `EngineContractError`. Call ownership comes from gateway context, never model arguments.
- `hooks.py`: `pre_tool_call` guard. `_env.py`: read config/secrets here, not via raw `os.getenv`.
- `twilio_util.py` (pure: signatures, TwiML, allowlist, stream tokens), `twilio_rest.py` (stdlib REST client, blocking: use `asyncio.to_thread`).
- `voice_behavior.py`: greeting/profile/guardrails for the adapter. Plain Python, no Pipecat import.
- `voice/`: caller-profile template and an example hours file (not loaded automatically).

## Dev environment
- Python >= 3.11; install runtime and dev dependencies with `pip install -e '.[dev]'` from the repo root. Packaging uses Hatchling (`pyproject.toml`).
- Phone setup requires `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `VOICE_ELEVENLABS_API_KEY` (the business's own; never read `ELEVENLABS_API_KEY`), and `VOICE_WEBHOOK_URL`; callbacks also need `TWILIO_PHONE_NUMBER`.
- The phone server defaults to `127.0.0.1:8081`. Twilio needs a public HTTPS base URL with no trailing slash, an exact `/voice` webhook URL for signature validation, and proxy WebSocket upgrades on `/media`.

## Commands
- Tests: `pytest tests` from the repo root (needs the project deps installed, e.g. `pip install -e '.[dev]'`; `test_adapter.py` imports `fastapi`, which comes via Pipecat).
- Lint: `ruff check .` (dev extra; no repo-specific Ruff configuration).
- Engine contract: `PYTHONPATH=<hermes-agent v0.23.3 checkout> python -m pytest <repo>/tests/test_engine_contract.py`, run from outside the repo (the root `tools.py` shadows the engine's `tools` package otherwise).
- `tests/pytest.ini` makes `tests/` the rootdir so the root `__init__.py` is never imported by pytest; `tests/_hermes_stubs.py` stubs Hermes' `gateway` package and loads the plugin the way Hermes does. Don't run bare `pytest` at the root.
- No checked-in CI workflow, lockfile, or dedicated build/type-check script; the build backend and explicit packaged-file list live in `pyproject.toml`.

## Conventions and invariants
- **Pipecat is pinned to exactly `1.7.0`**, in both `pyproject.toml` and `plugin.yaml` `python_dependencies` (and install hints). Change all together; `tests/test_pipecat_version.py` checks this.
- New runtime modules must be added to `[tool.hatch.build] only-include` in `pyproject.toml`; new env vars to `plugin.yaml` (`requires_env`/`optional_env`) and the README.
- Python >= 3.11; modules start with `from __future__ import annotations` and a docstring explaining purpose.
- Tool handlers: `handler(args: dict, **kwargs)` returning a JSON string; tool names use underscores.
- Keep the plugin loadable without Pipecat/Twilio SDK installed (lazy imports).
- Version lives in both `pyproject.toml` and `plugin.yaml`.

## Pitfalls
- Voice ingress is deliberately serialized through gateway task cleanup, not just `on_processing_complete`; preserve the guarded `_event_session_key` / `_session_tasks` / `_active_sessions` contract and `tests/test_voice_ingress.py` coverage. Do not re-enable gateway steer/merge for phone turns without per-event terminal receipts.
- `tests/test_pipecat_version.py` checks the installed Pipecat version too, not just dependency declarations. Shell Python and the Hermes-managed runtime may differ; missing Pipecat is not a valid full-suite environment.
- `voice/PROFILE.md` is a template, not automatically loaded. Set `VOICE_PROFILE_PATH` or copy it to `<HERMES_HOME>/voice/PROFILE.md`; profiles truncate at 6,000 characters. Set `VOICE_GREETING` separately.

## Security rules (do not weaken)
- Phone toolsets must resolve to exactly `voice_check_availability` and `voice_capture_lead`; retain the adapter's policy check, deny-by-default caller hook and handler authorization. Revoke grants on every terminal path. Other plugin/MCP/context tools must not expand the caller surface; see README policy prerequisites and `tests/test_caller_tools.py`.
- A callback dials only through `voice_place_callback`, whose hook returns the native approve directive (one `rule_key` per callback) or blocks; never add an approve tool or a dial path that skips the hook, and spoken words never run `/commands`.
- Twilio webhooks are always signature-checked; never reintroduce a bypass such as `VOICE_INSECURE_NO_SIGNATURE`. Callers must be on `VOICE_ALLOWED_USERS` (or explicit `VOICE_ALLOW_ALL_USERS=true`); the allowlist is a filter, not authentication.
- Never log or commit secrets (`TWILIO_*`, `VOICE_ELEVENLABS_API_KEY`); `.env` is local only (use `.env.example`). Don't log caller transcripts above DEBUG.
- Nothing callers hear or the owner sees names the engine; `tests/test_plugin_scaffold.py` checks the fixed strings.
