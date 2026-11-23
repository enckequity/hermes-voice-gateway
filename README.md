# hermes-voice-gateway
Hermes Voice Gateway connects your business phone line to Hermes. It answers calls, tells callers your opening times, takes messages (lead capture), and places callbacks only after a person approves them on the engine's native approval card.

**This is the `enckequity` fork** of [SRai22/hermes-voice-gateway](https://github.com/SRai22/hermes-voice-gateway) at `71f5801`, carried for TeamShift (enckequity/teamshift#4638). Changes: callback dials go through native `pre_tool_call` approval (one rule key per callback); the business's own ElevenLabs key is required (`VOICE_ELEVENLABS_API_KEY`, no shared default); the laptop-inventory demo tools and the local WebRTC pipeline are replaced by `voice_check_availability` and `voice_capture_lead`; `VOICE_INSECURE_NO_SIGNATURE` is gone (signatures are always verified); memory-off and the private `_get_platform_tools` import are checked at startup and fail clearly; engine naming is kept out of what callers hear.

## Status
Early-stage (v0.2). Inbound calls, availability, lead capture, approved callbacks and call-status lookup are implemented. Not production-hardened: it has not been load-tested or verified at scale, and the limits below apply.

## How a call works
Twilio -> `POST <VOICE_WEBHOOK_URL>/voice` (signature checked) -> TwiML opens a media stream to `/media` -> ElevenLabs speech-to-text -> the transcript goes to Hermes as a normal message from the caller's number -> Hermes' reply is spoken with ElevenLabs.

## Setup
1. Create a dedicated caller profile with `hermes profile create voice` (do not clone your personal profile). Install and run this plugin in that profile, not your personal/default profile. Set `memory.memory_enabled: false`, `memory.user_profile_enabled: false`, and leave `memory.provider` unset or empty; see the runtime policy below. Add credentials through Hermes’ secret configuration for that profile:
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`
   - `VOICE_ELEVENLABS_API_KEY`: the business's **own** ElevenLabs key (speech-to-text and voice). The call server refuses to start without it and never falls back to a generic `ELEVENLABS_API_KEY`, so no shared key is ever used.
   - `VOICE_WEBHOOK_URL`: the public https base URL Twilio can reach, no trailing slash (a tunnel such as ngrok works for local testing)
   - `VOICE_ALLOWED_USERS`: comma-separated caller numbers (E.164). With no list, every call is refused, unless `VOICE_ALLOW_ALL_USERS=true`.
2. In the Twilio console, set the number's "A call comes in" webhook to `<VOICE_WEBHOOK_URL>/voice` (HTTP POST). The URL must match exactly, because it is part of the signature check.
3. The call server listens on `127.0.0.1:8081` by default (`VOICE_WEBHOOK_HOST`, `VOICE_WEBHOOK_PORT`). Point your tunnel or reverse proxy at it. It must forward WebSocket upgrades on `/media`.

Optional: `VOICE_MAX_CALLS` (default 5), `VOICE_MAX_CALL_SECONDS` (default 1800; 0 = no limit), `ELEVENLABS_VOICE_ID`, `ELEVENLABS_STT_MODEL`, `ELEVENLABS_TTS_MODEL`, `VOICE_HOURS_PATH`. Twilio signatures are always verified; there is no switch to turn that off (a leftover `VOICE_INSECURE_NO_SIGNATURE` is ignored with a warning).

## Callbacks (outbound calls)
1. The agent calls the `voice_propose_callback` tool with a number, a one-sentence purpose and who asked. The number must be on `VOICE_ALLOWED_USERS`. This only records the request; nothing is dialed.
2. The agent then calls `voice_place_callback` with the id. Its `pre_tool_call` hook returns the engine's `{"action": "approve", "rule_key": "voice_callback:<id>:<digest>", "message": ...}` directive, so the person sees a native approval card with the number and purpose. The dial runs only if they approve; deny or timeout blocks it. The rule key is unique per callback and bound to its number and purpose, so "always" never carries over to another call. The hook blocks instead (nothing dials) where the gate would not ask a person: yolo, `approvals.mode: off`, cron, single-query or unattended runs, or an engine whose approval helpers changed. `/voice-callback list|show|deny` reviews or denies; `/voice-callback approve` no longer dials and points at the card. Proposals expire after 24 hours, and at most 20 can wait at once.
3. On approval the gateway asks Twilio to call the number from `TWILIO_PHONE_NUMBER`. When they answer, Twilio fetches `<VOICE_WEBHOOK_URL>/voice/outbound?cb=<id>` (signature checked, usable once, only for the number that was dialed) and the call runs like an inbound call, except the agent speaks first and is told the approved purpose.
4. The agent can check progress with `voice_check_call_status`, by `callback_id` or by the `call_sid` of a call this gateway placed. It cannot look up other calls on your Twilio account.

Callback statuses: `pending`, `denied`, `expired` (24 h), `dialing`, `unanswered` (Twilio never reached the answer URL within 3 minutes: no answer, busy or rejected), `answered`, `completed` (2 h after answering), `failed`. At most 20 proposals can wait, and at most 2 per number.

There is no agent-callable approve tool, phone callers cannot reach the callback tools, and spoken words cannot run `/commands`. The "requested by" name is whatever the agent supplied and is not verified. State is kept in the plugin's own SQLite file (`<HERMES_HOME>/plugin-data/hermes-voice-gateway/data.db`).

Needs `TWILIO_PHONE_NUMBER` in addition to the inbound settings.

## Caller tools: availability and lead capture
Phone turns can use only `voice_check_availability` and `voice_capture_lead`. The adapter overrides the caller toolsets with `voice_caller` and `no_mcp`; shell, file, browser, delegation, connectors and the callback/status tools are not caller permissions.

- `voice_check_availability` (read-only) reads the business-hours file `VOICE_HOURS_PATH` or `<HERMES_HOME>/voice/hours.json` (see [`voice/hours.example.json`](voice/hours.example.json): IANA `timezone`, `weekly` HH:MM-HH:MM ranges, `closed` dates, optional `busy` blocks). It returns open windows for up to 7 days within the next 60 days, minus busy blocks and time already past today. No external calls, nothing is booked, and there is no caller-controlled path. A missing or malformed file returns a "take a message" fallback.
- `voice_capture_lead` requires a name, a reason and explicit `consent=true`. The phone number recorded is the live caller's (gateway-bound); an optional `callback_number` (E.164) is kept alongside it, never as the owner. Leads are plaintext SQLite rows in `<HERMES_HOME>/plugin-data/hermes-voice-gateway/data.db`, table `voice_leads`. One lead per call: identical retries return the same id; changed details are refused. `/voice-leads [count]` lists the latest.
- Both handlers require matching task-bound gateway session/caller/thread/message identity, a bounded database grant and a live call in the admitting gateway process. Permissions close on completion, rejection, timeout, cancellation or hangup. Treat caller ID as unverified, not proof of identity.

**Runtime policy prerequisite:** Hermes can automatically append other installed plugin toolsets, context-engine tools or tool-search wrappers even after a platform override. Before each caller turn, the plugin checks the resolved tool definitions and refuses inference unless they are exactly the two caller tools. The pre-tool hook also explicitly denies all other tool names and denies permission-check errors; handler checks remain mandatory even for direct dispatch.

Create and configure a dedicated profile before accepting calls:

```sh
hermes profile create voice
hermes --profile voice config set memory.memory_enabled false
hermes --profile voice config set memory.user_profile_enabled false
hermes --profile voice config set memory.provider ''
```

When the call server starts it checks that memory is off and that the engine still provides the private `hermes_cli.tools_config._get_platform_tools` (checked against hermes-agent v0.23.3, see `_engine.py`); either failure stops the voice platform with a clear fatal error instead of answering. Run the gateway with `hermes --profile voice gateway run` after installing the plugin and configuring its credentials in that profile. Do not clone or reuse an owner profile. Before every caller turn, the plugin refuses inference unless both built-in memory flags are explicitly boolean `false` and `memory.provider` is unset or empty. An external provider is refused even if both built-in flags are off, preventing caller turns from being synced to that provider.

**What callers can see:** this is still a normal Hermes agent in the dedicated profile, not a separate prompt sandbox. It can see Hermes’ system/safety instructions, that profile’s persona (`SOUL.md` and configured system prompts), any project/context instructions Hermes loads, the voice instructions (`voice/PROFILE.md` or `VOICE_PROFILE_PATH`), the current call’s history, approved callback purpose when applicable, and results from the two caller tools. Keep all of that context caller-safe; never copy personal persona, secrets or private business notes into it. The disabled memory stores (`MEMORY.md`/`USER.md`) are not loaded as built-in memory and caller turns are not synced to an external memory provider. Hermes session/transcript storage and consented local lead storage still apply; disabling memory is not a no-retention mode.

In the dedicated voice profile, save a Voice tool selection containing only `voice_caller`, with MCP disabled. `known_plugin_toolsets.voice` must include the omitted installed plugin toolsets (including `voice`) so Hermes does not add them by default. For a profile with only this plugin, that list is `[voice, voice_caller]`. Additional plugin toolsets must also be marked known-and-omitted for Voice. Context-engine additions and tool-search deferral must not broaden/replace the two exposed tool names. Use Hermes' tools/configuration interfaces; this plugin does not rewrite profile settings. Misconfiguration produces a caller-policy error in gateway logs and fails closed. This policy/context integration is checked against the installed Hermes APIs and must be reverified on runtime upgrades.

## Speech recognition
Phone calls use `scribe_v2` through `ElevenLabsSTTService`, Silero VAD detects the end of each spoken segment, then the audio is uploaded for transcription. Pause after speaking to let Hermes respond; this is segmented HTTP transcription, not the realtime WebSocket API. Each call owns an HTTP session that closes when the call ends or setup fails.

## Limits (from the Pipecat telephony docs)
- Twilio WebSocket calls have basic call control only: no transfers or reconnects. Those need Daily PSTN/SIP.
- Pipecat's user-turn strategies can interrupt queued speech. Cancellation of Hermes' ongoing reasoning/tools is not coordinated with playback interruption, so full phone-call barge-in remains unverified.
- A dropped connection ends the call. Twilio call-status callbacks are not wired in, so a callback's stored status stays `answered` after the call ends (use `voice_check_call_status` for the live state).
- Calls to voicemail are not detected; the agent may speak to an answering machine.

## Security notes
- The allowlist trusts the caller ID Twilio reports, and phone caller IDs can be spoofed. Treat it as a filter, not as strong authentication. Do not give allowlisted callers more access than you would give anyone who can phone in.
- Pipecat logs caller speech (the transcript) at DEBUG level. Run Hermes with `LOGURU_LEVEL=INFO` or higher so transcripts stay out of logs.
- Spoken words never run Hermes `/commands`; calls are plain conversation only.

## Call behaviour
- **Greeting**: the agent speaks first when a call connects. `VOICE_GREETING` overrides the text; `off` disables it.
- **Voice profile**: `<HERMES_HOME>/voice/PROFILE.md` (or `VOICE_PROFILE_PATH`) supplies short-reply, speakable instructions. If missing or empty, a built-in profile is used. The adapter snapshots it per call and supplies it to Hermes through `channel_prompt`, without changing the caller's transcript or replacing Hermes' own safety instructions.
- **Caller-profile template**: [`voice/PROFILE.md`](voice/PROFILE.md) is a generic receptionist template (opening times, take a message, privacy rules). Copy it to the profile location above or set `VOICE_PROFILE_PATH` to its absolute path; the repo file is not loaded automatically. The caller tool allowlist is enforced independently of the profile; a prompt is not an access-control boundary. Profiles are truncated at 6,000 characters. Set `VOICE_GREETING` separately to an opening that discloses the caller is talking to an AI, because the gateway's fixed greeting is not generated from the profile.
- **Smart turn**: Pipecat's local smart-turn model decides from the audio when the caller has finished, instead of replying after any pause.
- **Guardrails**: a turn with no recognised words makes the agent ask the caller to repeat (up to twice in a row); silence gets "Are you still there?" and then a hang-up; three pipeline errors in one call end it with an apology.
- **Adapter timing**: one completed smart turn becomes one Hermes message. Idle detection pauses while Hermes is processing and while speech is queued or playing. Outbound callbacks retain their approved-purpose, Hermes-first opening instead of speaking a second generic greeting.
- **Reply routing**: each phone call has its own Hermes thread, keyed by Call SID. Speech delivery requires that call's thread or a valid reply-to turn; stale or unscoped replies cannot reach a later call from the same number. Only final user-facing text marked by Hermes with `notify=true` is spoken; busy acknowledgments and delivery-recovery notices stay silent. Direct adapter integrations must preserve that routing metadata.
- **Serialized phone turns**: voice inputs run FIFO per canonical Hermes session, waiting until the gateway processing task exits, including cleanup. Phone input does not steer or merge into an in-progress response; audio playback interruption is still separate. Delivery bookkeeping settles once on completion, rejection, failure or hangup. A five-minute admission/processing wait fails the delivery without cancelling running tools, and late replies to that delivery are refused. Hangup drops queued delivery, not tool side effects already in progress. This relies on guarded private Hermes session/task APIs; incompatible state fails closed instead of guessing that input was accepted.

## Pipecat pin
Use **Pipecat 1.7.0 exactly** (`pipecat-ai[elevenlabs,silero,websocket]==1.7.0`) plus `uvicorn>=0.32,<1`. Both dependency declarations and the install hint use the same pin; verify the engine's interpreter, not just a shell's Python.

## Tests
`pytest tests` from the repo root. The engine contract test runs against a hermes-agent v0.23.3 checkout from another directory: `PYTHONPATH=<checkout> python -m pytest <repo>/tests/test_engine_contract.py` (skipped otherwise).

Tests simulate the model and audio; they are not a live phone or provider test. Caller-permission coverage is in `tests/test_caller_tools.py`.

## License
MIT. See [LICENSE](LICENSE).
