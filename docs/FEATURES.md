# What we can build with this stack

An inventory of capabilities available to us, ordered by importance to our goal
(a telephony payments bot on FreeSWITCH, scaling later). Each entry says **where the
feature lives**: `pipecat` (core framework, module path), `pipecat.flows` (bundled
flows engine), or `this repo` (built here on top of Pipecat).

Pipecat version: 1.5.0, installed editable from `external/pipecat` (v1.5.0 tag).

---

## Tier 1 — the payments product core (working today)

| # | Feature | Where | Notes |
|---|---------|-------|-------|
| 1 | **Structured voice flows** — node-based conversations with per-node prompts, functions, and actions | `pipecat.flows` (`FlowManager`, `NodeConfig`, `FlowsFunctionSchema`) | The backbone of every scripted call. Dynamic flows (Python node factories) for anything data-driven. |
| 2 | **Guided hybrid collection pattern** — LLM decides conversation (order, phrasing, volunteered info); code decides money (lookups, limits, retry budgets, routing) | this repo — `server/bots/payment.py` | Our core IP: multi-slot collect node, consolidated handlers returning `(result, next_node)`, 3-strikes budgets, verbatim terminal lines. |
| 3 | **FreeSWITCH telephony transport** — real phone calls into Pipecat over `mod_audio_stream` WebSocket (8 kHz L16 + streamAudio JSON) | this repo — `server/serializers/audio_stream.py`, `/audio` endpoint in `server/main.py`, `docker/freeswitch/` | Pipecat core has the WebSocket transport (`pipecat.transports.websocket.fastapi`); the FreeSWITCH wire format, dialplan, and Docker stack are ours. Runs on native Windows. |
| 4 | **External API integration inside a flow** — look up invoices, create payment orders mid-conversation | this repo — `server/services/invoice_api.py` (interface + mocks) | Function handlers are plain async Python; swap mocks for REST clients without touching the flow. |
| 5 | **Business FAQ at any point** — caller can ask general questions (hours, "where do I find my invoice number?") mid-task; bot answers and returns to the task | `pipecat.flows` `global_functions` + this repo (`get_business_info` in `payment.py`) | Registered once, available at every node; handler returns `next_node=None` to stay put. |
| 6 | **Turn detection / endpointing** — knowing when the caller finished speaking | `pipecat` — `pipecat.turns.*` (VAD strategies, `SpeechTimeoutUserTurnStopStrategy`, smart-turn v3 ONNX model) | We use VAD+timeout at 8 kHz telephony (smart-turn expects 16 kHz); Daily path uses smart-turn. Tuned in `server/bots/base_bot.py`. |
| 7 | **Barge-in** — caller can interrupt the bot mid-sentence | `pipecat` — interruption system (`broadcast_interruption`, VAD turn-start strategies) | On by default; optional `ECHO_MUTE=true` (this repo) disables it to survive echo-prone lines. |
| 8 | **No-input / idle handling** — silence → reprompt → terminate | `pipecat` (`user_idle_timeout` on user aggregator) + this repo (idle budget logic in `PaymentBot._handle_idle`) | Deterministic reprompts per pending slot; shares the 3-strikes budget. |
| 9 | **Provider-swappable STT / TTS / LLM** — Deepgram, Cartesia, ElevenLabs, Rime, Google, OpenAI (60+ services exist) | `pipecat` — `pipecat.services.*` | Selected via env (`TTS_PROVIDER`, `LLM_PROVIDER`, …) in this repo's `config/bot.py`. Better voices = config change, not code. |

## Tier 2 — high value, small effort from here

| # | Feature | Where | Notes |
|---|---------|-------|-------|
| 10 | **DTMF keypad input** — caller types digits instead of speaking (invoice numbers, menu choices; prerequisite for PCI-compliant card entry) | this repo — serializer already turns `{"type":"dtmf"}` text frames into user turns; FreeSWITCH-side ESL bridge still to wire (pattern exists in demo-ivr) | Pipecat core also has DTMF frame types + aggregators (`pipecat.audio.dtmf`, RTVI keypress input). |
| 11 | **Call recording / audio capture** — record calls, or tap the audio for QA | `pipecat` — `AudioBufferProcessor`, transport recording; FreeSWITCH `uuid_record` (stereo legs) | FreeSWITCH-side recording was the demo-ivr debug workhorse. Pause/resume matters for PCI. |
| 12 | **Conversation transcripts + analytics** — store what was said, both sides | `pipecat` — transcript processors, `observers` (monitor frames without touching the pipeline), metrics (TTFB, usage) | Metrics already enabled in our `PipelineParams`; an observer → DB is a small add. |
| 13 | **Behavioral evals** — scripted test calls asserting bot behavior (function called, latency, judge of replies) | `pipecat` — `pipecat.evals`, `pipecat eval run scenarios/*.yaml` | Run the bot with `-t eval`; write YAML scenarios for the payment flow (e.g. "3 bad amounts → terminate"). Our scratch check script could graduate into these. |
| 14 | **Static flow visualization** — see/share the call graph | this repo — `server/bots/payment_flow_static.json` + https://flows.pipecat.ai/editor | Viewing export; Python stays the source of truth. |
| 15 | **Structured lead-qualification flow** (existing second bot) | this repo — `server/bots/flow.py` (+ `simple.py`) | Same framework, different product; shows multi-bot serving from one server (`BOT_TYPE`). |

## Tier 3 — capabilities we haven't used yet (available when needed)

| # | Feature | Where | Notes |
|---|---------|-------|-------|
| 16 | **Multi-worker orchestration** — several cooperating bots/agents over a message bus (local, Redis, PGMQ), job RPC between them | `pipecat` — `pipecat.workers`, `pipecat.bus`, `pipecat.registry` | E.g. a supervisor agent, a fraud-check worker, or distributed scaling of bot workers. |
| 17 | **LLM switching / fallback** — swap or fail over between LLMs mid-pipeline | `pipecat` — `LLMSwitcher` (`pipecat.pipeline.llm_switcher`) | Cost/latency routing (cheap model for FAQ, stronger for payment steps). |
| 18 | **Warm transfer to a human** — bot hands the call to an agent | FreeSWITCH (ESL `uuid_transfer`/conference) + this repo dialplan | The demo-ivr blueprint sketches this; needs ESL wiring. |
| 19 | **Outbound calling** — the system dials customers (reminders, collections) | FreeSWITCH `originate` via ESL + this repo | Media path identical; needs trunk + campaign logic. |
| 20 | **Web/RTVI client** — browser-based voice widget with live transcripts, speaking indicators | `pipecat` — RTVI protocol (`pipecat.processors.frameworks.rtvi`) + `client/` (Next.js) in this repo (currently retired) | The old Daily web path can come back for demos/QA without touching telephony. |
| 21 | **Vision / multimodal** — image input, video avatars | `pipecat` — vision services, video frames | Not relevant to phone; exists if a web channel ever wants it. |
| 22 | **Speech-to-speech realtime models** — e.g. Gemini Live / OpenAI Realtime instead of STT→LLM→TTS | `pipecat` — `services/google/gemini_live`, `services/openai/realtime` | Lower latency, but less control and weaker fit for the code-validated payment pattern. |
| 23 | **Audio filters / noise reduction** — input cleanup (e.g. Krisp), mixers, resampling | `pipecat` — `pipecat.audio.filters`, mixers | Useful on noisy PSTN lines; Krisp needs a license. |
| 24 | **Heartbeats & pipeline health** — watchdog for stalled pipelines | `pipecat` — `PipelineParams.enable_heartbeats`, `on_idle_timeout` | Production hardening for long-running calls. |

## Repo-specific glue worth knowing (not features, but load-bearing)

- `server/bots/base_bot.py` — one bot framework, **two transports** (FreeSWITCH WebSocket default; Daily kept for WSL/Linux), transport-appropriate endpointing, provider selection.
- `server/config/` — env-driven config (`TRANSPORT`, `BOT_TYPE`, `ECHO_MUTE`, `PAYMENT_*` limits, provider keys).
- `docker/freeswitch/` — pinned FS 1.10.12 + `mod_audio_stream` v1.0.3 (**free ≤10 concurrent channels — licensing decision pending** for scale), dialplan (`bothost:7860/audio`), Windows Docker-Desktop networking fix.
- Known constraint: `daily-python` has no Windows wheels → Daily transport is Linux/WSL-only; FreeSWITCH path is the native-Windows one.
