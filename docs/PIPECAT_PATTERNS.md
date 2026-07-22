# Pipecat patterns for our payment line — full examples sweep

Source: systematic review of ALL `external/pipecat/examples/` (v1.5.0, ~380 files; per-provider
duplicates collapsed). Organized by when we should adopt each pattern. File paths are relative
to `external/pipecat/examples/`.

Target architecture (confirmed): **multi-service customer line** — free-form router worker
dispatching to structured Flows service-workers (payments first), per-worker LLMs
(cheap router / strong payments model), warm transfer to humans, 3000–5000 concurrent calls.

---

## A. Adopt NOW — payment-bot correctness & stability

| Pattern | Example | What to change in our repo |
|---|---|---|
| **Don't act on half-heard turns** | `turn-management/turn-management-filter-incomplete-turns.py` | `FilterIncompleteUserTurnStrategies()` — an LLM classifier holds turn-end until the thought is complete (re-prompts at 5s/10s). Text-based, so it works at 8 kHz where smart-turn can't. Evaluate: adds a little latency per turn; for money inputs it's the right trade. |
| **Noise-proof barge-in** | `turn-management/turn-management-interruption-config.py` | `UserTurnStrategies(start=[MinWordsUserTurnStartStrategy(min_words=2..3)])` — a cough or line noise no longer interrupts the bot mid-sentence. Directly addresses our glitchy-line experience. |
| **Barge-in must NOT cancel a charge** | `function-calling/function-calling-google-async-stream.py` | `@tool_options(cancel_on_interruption=False, timeout_secs=30)` on `confirm_payment`; stream interim "still processing" results (`FunctionCallResultProperties(is_final=False)`); deterministic "One moment…" filler via `llm.event_handler("on_function_calls_started")`. |
| **Mute caller during API calls** | `turn-management/turn-management-user-mute-strategy.py` | Add `FunctionCallUserMuteStrategy()` to `user_mute_strategies` — caller speech during the payment-gateway call can't derail the turn. |
| **Hard-constrained money args** | `flows/food_ordering_advanced_functionschema.py` | Convert `collect_payment_details` to an explicit `FlowsFunctionSchema` with `"minimum"`/`"maximum"` on amount and pattern/length hints on invoice number — schema-level constraints beat prose. (Code still re-validates; defense in depth.) |
| **Spoken amounts read correctly** | `features/features-voice-formatter.py` | `VoiceFormatter` via `text_transforms=[("*", VoiceFormatter())]` on TTS — "£42.50" → "forty-two pounds fifty" instead of TTS guessing. Also `<spell>` tags (`features-user-email-gathering.py`) for digit-by-digit read-back. |
| **Clean confirmation context** | `flows/patient_intake.py`, `flows/warm_transfer.py` | `context_strategy=ContextStrategyConfig(ContextStrategy.RESET_WITH_SUMMARY, summary_prompt=...)` on the confirm node — strips STT noise/detours so the read-back is decided from clean state. |
| **Canonical DTMF** | `features/features-dtmf-menu.py` | `DTMFAggregator()` right after `transport.input()` turns `InputDTMFFrame` into transcription the LLM reads. Change our serializer to emit `InputDTMFFrame` (today it fabricates an `LLMMessagesAppendFrame`) — then keypad entry uses the same turn machinery as speech. Prerequisite for PCI DTMF capture. |
| **Idle-strike reset** | `turn-management/turn-management-detect-user-idle.py` | ✅ already adopted (consecutive-silence budget; reset on `on_user_turn_started`). Also available: `UserIdleTimeoutUpdateFrame(timeout=0)` to suspend idle detection during slow gateway calls. |

## B. Adopt for PRODUCTION — audit, recording, resilience

| Pattern | Example | Notes |
|---|---|---|
| **Call recording** | `audio/audio-recording.py` | `AudioBufferProcessor(auto_start_recording=True)` placed AFTER `transport.output()`; `on_track_audio_data` gives separate caller/bot tracks (QA + disputes). Requires `audio_passthrough=True` on STT. Works at transport sample rate (8 kHz OK). PCI: pause during card entry. |
| **Audit trail** | `observability/observability-observer.py` | Custom `BaseObserver.on_push_frame` → log every turn, tool call, interruption per call-id. This is the money audit hook; pass via `PipelineWorker(observers=[...])`. |
| **Latency/error metrics** | `observability/observability-sentry-metrics.py` | `SentryMetrics()` per service. (Only Sentry ships as a vendor integration; OTel would be custom.) Plus `MetricsLogObserver`, `UserBotLatencyObserver`, `turn_tracking_observer`. |
| **Wedged-call detection** | `observability/observability-heartbeats.py` | `PipelineParams(enable_heartbeats=True)` + `on_heartbeat_timeout` — at 1000s of calls, silent stalls must page someone. |
| **Provider failover** | `features/features-service-switcher.py` | `LLMSwitcher(llms=[...])` / `ServiceSwitcher` — live STT/TTS/LLM failover (manual or strategy-based). At scale, a Deepgram outage must not drop the line. |
| **Call resumption** | `persistent-context/persistent-context-gemini.py` | `context.get_messages()` / `set_messages()` — serialize state so a dropped FreeSWITCH leg can resume when the caller redials (swap file store for DB). |
| **Long-call context control** | `context-summarization/context-summarization-dedicated-llm.py` | Auto-summarization with a dedicated cheap LLM (`LLMAutoContextSummarizationConfig`) caps context growth/cost on long calls. |
| **Line-noise filtering** | `voice/voice-krisp-viva.py` | `audio_in_filter=KrispVivaFilter()` (license needed; verify 8 kHz support). Krisp's `IPUserTurnStartStrategy` also predicts interruptions vs backchannels. Avoid realtime examples' `near_field` noise reduction — tuned for headsets, wrong for phone lines. |
| **Hot settings updates** | `update-settings/*` | `TTSUpdateSettingsFrame` / `LLMUpdateSettingsFrame` deltas mid-call (e.g. slow down TTS for confirmations). |
| **Missing-handler guardrail** | `function-calling/function-calling-missing-handler.py` | `_missing_function_call_handler` — a mis-wired tool degrades to an apology instead of hanging the call. |

## C. Target architecture — router + service workers + humans (confirmed direction)

| Pattern | Example | Notes |
|---|---|---|
| **Router → service workers** | `flows/multi_worker_handoff.py`, `multi-worker/local-handoff/local-handoff-two-agents.py` | Main worker owns transport + shared `LLMContext`; router `LLMWorker` for chit-chat/dispatch; each service a Flows `PipelineWorker` (`bridged=()`), activated via `transfer_to_*` tools with `deactivate_self=True`. Our payment flow drops in as one worker; every payment node gains a `transfer_to_router` escape function. |
| **Scale-out across machines** | `multi-worker/distributed-handoff/redis-handoff/`, `pgmq-handoff/` | Same topology over `RedisBus` / `PgmqBus` — telephony front-end and LLM workers as separate processes/machines on one channel; workers self-register (`registry.watch`). **PGMQ (Postgres) is attractive for us: the bus doubles as durable, auditable infrastructure.** |
| **Fraud/risk fan-out** | `multi-worker/parallel-debate/` | `job_group(*workers, payload=..., timeout=30)` — run fraud/risk/eligibility checks in parallel, collect all responses before creating the payment order. |
| **Slow work off the voice turn** | `multi-worker/sensor-controller/`, `code-assistant/` | `job("worker", payload=...)` — delegate the gateway call to a bus worker; the voice worker keeps talking ("give me a moment") and picks up the `JobStatus` result. |
| **Warm transfer to human** | `flows/warm_transfer.py` | Flow structure ports directly: escalation node with `RESET_WITH_SUMMARY` (summary_prompt asks for error details → agent briefing), `on_participant_joined` advances the node. **Audio mechanics are Daily-specific** — on FreeSWITCH we do hold/mute/bridge via dialplan + ESL (`originate` agent leg, `uuid_transfer`/conference). |
| **Voicemail detection (outbound)** | `features/features-voicemail-detection.py` | `VoicemailDetector(llm=classifier_llm)` + `.gate()` after TTS — for payment-reminder outbound campaigns: detect machine, leave message, hang up. |
| **Point-to-point remote worker** | `multi-worker/remote-proxy-assistant/` | `WebSocketProxyClient/Server` — reach one remote service without standing up Redis; also the FastAPI-host reference (runner per connection, `handle_sigint=False` — matches our server). |

## D. Deliberately NOT adopting (and why)

- **Realtime speech-to-speech models** (`realtime/*`): lower latency but provider-side turn logic and weaker control — wrong fit for code-validated money operations.
- **Wake phrases** (`features-wake-phrase.py`): call lines are already addressed.
- **MCP tools** (`mcp/*`): subprocess dependency per call; our APIs are first-party.
- **Mem0/RAG memory** (`rag-mem0.py`): returning-caller personalization — nice-to-have, GDPR questions, later. (The MAG pattern — cheap huge-context LLM behind a tool — is worth remembering if the FAQ outgrows the prompt.)
- **UI workers** (`multi-worker/ui-worker/*`): no agent screen yet; revisit with warm transfer.
- **Thinking budgets** (`thinking/*`): adds turn latency; our conditional logic lives in code.

## E. Sweep notes

- No example runs FreeSWITCH or explicit 8 kHz — our transport/serializer and telephony endpointing tuning (`SpeechTimeoutUserTurnStopStrategy`, `audio_out_10ms_chunks`) go beyond the examples; nothing contradicts them. Validate any audio-model addition (Krisp, smart-turn) for 8 kHz support before relying on it.
- Examples consistently: `LLMContext` + `LLMContextAggregatorPair`, `PipelineWorker` + `WorkerRunner` per connection, `enable_metrics=True`, disconnect → `runner.cancel()`. Our setup matches.
- `AudioBufferProcessor` placement (after `transport.output()`) and `audio_passthrough=True` are the two recording gotchas.
