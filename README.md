# Coval WebSocket Voice Agent Test Harness

Always-on protocol harness for Coval's generic JSON-audio WebSocket simulator.
It lets `MODEL_TYPE_WEBSOCKET` simulations exercise a deterministic voice agent
without depending on any customer service.

The first commit in this repo intentionally had no OpenTelemetry or Coval trace
instrumentation. The current version was instrumented by following Coval's
external `setup-tracing` skill.

## Protocol Summary

- Canonical endpoint after deployment:
  `wss://coval-websocket-agent-tracing-skills-1.fly.dev/ws`.
- Authentication is optional. When `WS_AGENT_REQUIRE_AUTH=true`, use a Bearer
  token header or `?token=` query parameter.
- No init / no ready / no ping / no end-of-call message. Connection is active
  immediately after upgrade. Standard close frames terminate.
- Audio is JSON only: base64 PCM in `audio_bytes`. Linear PCM, 16000 Hz, 16-bit
  signed little-endian, mono. Preferred chunk duration 20-100 ms.
- `audio_message` is bidirectional. Coval (client) sends `sender: "USER"`.
  Agent (server) sends `sender: "AI"`.
- `system_notify` / `ocb:cart-updated` events are server-originated. Coval
  receives them and may assert against them; Coval does not emit them.

## What It Simulates

- `wss://.../ws` direct WebSocket connection.
- Optional Bearer auth via header or `?token=` query param when
  `WS_AGENT_REQUIRE_AUTH=true`.
- Coval sends initialization JSON containing the simulation output ID.
- No `session_ready` handshake by default.
- Inbound JSON audio frames from Coval:

```json
{
  "action": "audio_message",
  "payload": {},
  "audio_bytes": "<base64 pcm_s16le audio>",
  "sender": "USER"
}
```

- Outbound JSON audio frames from the harness:

```json
{
  "action": "audio_message",
  "payload": {},
  "audio_bytes": "<base64 pcm_s16le audio>",
  "sender": "AI"
}
```

- `system_notify` / `ocb:cart-updated` frames:

```json
{
  "action": "system_notify",
  "payload": {},
  "event": "ocb:cart-updated",
  "data": {
    "cart": "1x latte, 1x blueberry muffin",
    "json_cart_ocb": [],
    "total": 8.52,
    "subtotal": 7.75,
    "tax": 0.77,
    "summary": "Cart contains 1 latte and 1 blueberry muffin."
  }
}
```

By default, the local server echoes received PCM back in the same generic JSON-audio
envelope. The deployed Fly app runs in `canned_speech` mode, sending a short
spoken PCM response so Coval simulations produce audible agent audio and
transcription.

## Endpoints

### `GET /health`

Liveness probe.

### `GET /config`

Returns runtime settings and a ready-to-copy Coval `MODEL_TYPE_WEBSOCKET`
metadata payload.

### `GET /sessions`

Returns recent WebSocket sessions, including frame counts, byte counts, and
non-audio/error events. Raw audio bytes are not stored.

### `POST /sessions/reset`

Clears in-memory session history.

### `WS /ws`

Default WebSocket endpoint.

### `WS /ws/{agent_id}`

Same protocol, with an arbitrary agent id captured in session debug output.

## Local Run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn server:app --port 8000 --reload
```

Smoke test:

```bash
python smoke_client.py ws://127.0.0.1:8000/ws
curl -sS http://127.0.0.1:8000/sessions
```

Unit tests:

```bash
pytest -q
```

## Deploy

The Fly app is `coval-websocket-agent-tracing-skills-1`.

```bash
fly apps create coval-websocket-agent-tracing-skills-1
fly deploy --ha=false
```

Verify:

```bash
curl -sS https://coval-websocket-agent-tracing-skills-1.fly.dev/health
python smoke_client.py wss://coval-websocket-agent-tracing-skills-1.fly.dev/ws
```

## Coval Agent Metadata

Use the `/config` endpoint for the current canonical payload. The default shape
is:

```json
{
  "model_type": "MODEL_TYPE_WEBSOCKET",
  "endpoint": "wss://coval-websocket-agent-tracing-skills-1.fly.dev/ws",
  "connection_mode": "direct",
  "websocket_compat_profile": "json_audio",
  "initialization_json": {"simulation_id": "{{simulation_id}}"},
  "send_sample_rate_hertz": 16000,
  "receive_sample_rate_hertz": 16000,
  "handshake_ready_message_type": "",
  "handshake_requires_session_id": false,
  "send_audio_template": "{\"action\":\"audio_message\",\"payload\":{},\"audio_bytes\":\"{{audio_data}}\",\"sender\":\"USER\"}",
  "message_type_path": "action",
  "audio_message_type_value": "audio_message",
  "audio_data_path": "audio_bytes",
  "audio_encoding": "pcm",
  "receive_audio_channels": 1,
  "non_audio_event_message_types": ["system_notify"]
}
```

## Runtime Knobs

All knobs are optional environment variables:

- `WS_AGENT_SAMPLE_RATE_HZ`: default `16000`.
- `WS_AGENT_CHANNELS`: default `1`.
- `WS_AGENT_ECHO_THRESHOLD_BYTES`: minimum inbound turn audio before responding; default `3200`.
- `WS_AGENT_RESPONSE_MODE`: `echo` or `canned_speech`; local default `echo`.
- `WS_AGENT_CANNED_AUDIO_PATH`: raw PCM16LE mono 16 kHz response file.
- `WS_AGENT_CANNED_RESPONSE_LIMIT`: default `2`.
- `WS_AGENT_CANNED_RESPONSE_CHUNK_MS`: default `100`.
- `WS_AGENT_CANNED_TURN_SILENCE_MS`: silence gap after inbound audio before a canned response; default `800`.
- `WS_AGENT_CANNED_SILENCE_RMS_THRESHOLD`: PCM16 RMS value below which inbound audio counts as silence; default `250`.
- `WS_AGENT_CANNED_FORCE_RESPONSE_BYTES`: optional inbound byte count that forces a canned response even if no silence is detected; default `0`.
- `WS_AGENT_SEND_CART_ON_CONNECT`: default `true`.
- `WS_AGENT_SEND_CART_AFTER_FIRST_ECHO`: default `false`.
- `WS_AGENT_SEND_SESSION_READY`: default `false`.
- `WS_AGENT_SEND_TONE_ON_CONNECT`: default `false`.
- `WS_AGENT_REQUIRE_AUTH`: default `false`.
- `WS_AGENT_AUTH_TOKEN`: bearer/query token when auth is enabled.
- `WS_AGENT_CLOSE_AFTER_ECHOS`: default `0`, meaning keep connection open.
- `COVAL_API_KEY`: Coval organization API key with trace write access.
- `COVAL_TRACES_ENDPOINT`: optional override, default
  `https://api.coval.dev/v1/traces`.

## Validation Intent

The baseline commit is uninstrumented. The tracing commit adds:

- `conversation` root spans for each WebSocket session.
- `transport` spans for inbound and outbound audio frames.
- `tts` spans for canned audio responses.
- `llm_tool_call` spans for structured cart updates.
- Simulation correlation from Coval's initialization JSON:
  `{"simulation_id":"{{simulation_id}}"}`.
