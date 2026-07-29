# Architecture assessment — 28 questions, answered against the code

Scope: answers are grounded in the actual repository state (not the roadmap docs), reading
`server/main.py`, `server/runner.py`, `server/bots/*`, `server/config/*`,
`server/serializers/audio_fork.py`, `server/services/*`, `server/observers/frame_trace.py`,
`server/esl_client.py`, `server/dtmf_bridge.py`, `docker/freeswitch/*`, `deploy/azure/*`,
and `docs/FEATURES.md` / `docs/PIPECAT_PATTERNS.md`. Where a capability is only aspirational,
that is stated. Legend: **[Built]** = implemented and runnable today; **[Partial]** = scaffold
/ one path only; **[Roadmap]** = named in docs but not in code; **[Absent]** = no trace in code.

The single most important architectural fact, because it underpins ~15 of the answers:

> On the FreeSWITCH/WebSocket transport (the default, `TRANSPORT=websocket`), **a call is not a
> separate OS process. It is one `async` task inside the single FastAPI/uvicorn server process**
> (`main.py` `audio_websocket` builds a bot and runs it in-process for the life of the socket).
> "One process per call" is only literally true on the **Daily** transport, where `main.py`
> `start_bot_process` spawns `runner.py` as a `subprocess.Popen` per call. Multi-tenancy, HA,
> and blast-radius answers all follow from this.

---

## 1. PCI-safe capture

> **Original question:** PCI-safe capture. When a caller provides card data, does the audio and its transcript pass through the STT, LLM, TTS, or recording paths? Can card data be captured and tokenized without bringing those components into PCI-DSS scope? Would a QSA certify the resulting flow?

**[Absent] for card data. No card capture, no tokenization exists today.**

The payment flow never asks for a PAN/CVV. The caller gives an **invoice number + amount**; code
looks the invoice up and calls `create_payment_order(invoice_number, amount)` — see
`bots/payment.py` (`collect_payment_details`, `confirm_payment`) and `services/invoice_api.py`
(`PaymentApi`, currently `MockPaymentApi`). So today card data does **not** pass through STT/LLM/TTS
because it is never collected.

If card data *were* collected by voice with the current pipeline
(`transport.input → STT → user aggregator → LLM → TTS → transport.output`, `base_bot.py:280`),
it would pass through **Deepgram STT (cloud), the LLM provider (Google/OpenAI/Anthropic cloud),
and TTS** — pulling all three vendors, plus any recording, into PCI-DSS scope. `docs/FEATURES.md`
itself flags DTMF as the "prerequisite for PCI-compliant card entry" (#10) and recording as needing
"pause during card entry" (#11) — i.e. acknowledged as not-yet-built.

Would a QSA certify the current flow? For card capture, no — there is nothing to certify, and a
naïve voice capture would be in scope on three vendors. Valid PCI-safe designs (none built):
1. **DTMF capture with media suppression / descoping** — collect the PAN as keypad tones on a leg
   where audio is not forked to STT (pause `mod_audio_fork`, or don't fork during entry), so the
   PAN never reaches STT/LLM/TTS/recording. Tokenize via a PCI-scoped gateway.
2. **Hand-off to a separate PCI-scoped DTMF/pause-and-resume service** (e.g. a dedicated captured-
   payment component) and resume the bot after a token is returned.
3. **Warm/blind transfer to an existing PCI IVR or agent** for the card step (FreeSWITCH
   `uuid_transfer`; sketched in `docs/PIPECAT_PATTERNS.md` C, not implemented).

---

## 2. Verbatim mandated disclosures

> **Original question:** Verbatim mandated disclosures. How is a legally-required disclosure delivered word-for-word on every call, with no paraphrasing or variation?

**[Built] — deterministic, non-LLM playback of exact strings exists and is used.**

Exact wording is delivered by **actions, not the LLM**: `tts_say` pre-actions and
`end_conversation` post-actions carry literal text that goes straight to TTS with no model in the
loop. Examples: the terminal line `TERMINATE_TEXT = "I will terminate the call, bye."`
(`bots/payment.py:52`, spoken via `create_terminate_node`'s `end_conversation`), the read-back and
success lines spoken via `tts_say` (`create_confirm_node`, `create_success_node`), and the
`api_failure` node's fixed "No payment has been taken" line. The role prompt also instructs
"Read statements in double quotes exactly as written" (`_ROLE`), but that path relies on the LLM
and is **not** a guarantee.

For a legally-mandated word-for-word disclosure, the reliable mechanism is the deterministic action
path (`tts_say` / `end_conversation`), which bypasses the LLM entirely. Two caveats to close before
calling it audit-grade: (a) TTS still renders the text, so a `VoiceFormatter`/pre-recorded-audio
step guarantees pronunciation (roadmap, `docs/PIPECAT_PATTERNS.md` A/B); (b) nothing currently
*asserts* the disclosure was played uninterrupted — that needs the audit hook in Q3.

---

## 3. Auditable decisions

> **Original question:** Auditable decisions. What record shows why the system took a given action on a call, and is it sufficient for a dispute or a regulator?

**[Partial] — per-call frame trace + logs exist; no durable, decision-level audit ledger.**

What exists: (a) `observers/frame_trace.py` writes a JSONL line per frame push to
`server/traces/call-*.jsonl` when `TRACE_CALLS=1`, including node transitions and context snapshots
(`base_bot.py:trace_flow_nodes`); (b) `loguru` logs every handler decision, e.g.
`MockPaymentApi: created <order_id>`, retry strikes, idle strikes; (c) `UserBotLatencyObserver`
metrics. Routing decisions are deterministic in code (`_strike`, `_route`, `collect_payment_details`
return `(result, next_node)`), so the "why" is reconstructable from the trace + logs.

What is missing for a dispute/regulator: this is **debug telemetry, not an audit record**. Traces
are local files gated off by default, keyed by PID+timestamp (not a stable call-id), with no caller
identity, no tamper-evidence, no retention policy, and no link to the payment-order id. `docs/
PIPECAT_PATTERNS.md` B explicitly lists "Audit trail" (a `BaseObserver` → DB per call-id) as a
**production item to adopt**, i.e. not built. As-is it is insufficient for a regulator; it would be
sufficient once (i) a stable call-id threads STT/LLM/tool/disclosure events into durable storage,
and (ii) the payment-order id is joined in.

---

## 4. PII handling & residency

> **Original question:** PII handling & residency. How is caller PII handled, retained, and segregated across each third-party provider (speech, model, voice), and does this meet our data-residency and retention obligations?

**[Absent] as a managed concern. PII flows to third parties with no retention/residency controls in code.**

Caller audio → **Deepgram** (STT, `base_bot.py:86`); transcripts + conversation →
**Google/OpenAI/Anthropic** (LLM); bot text → **Deepgram/Cartesia/ElevenLabs/Rime** (TTS). All are
cloud SaaS reached over the vendor SDKs with API keys from env (`config/bot.py`). The repo contains:
no data-processing config, no region pinning, no retention/TTL, no redaction, no DPA references, and
no segregation logic. Frame traces (`server/traces/*.jsonl`) persist whatever was said to local disk
indefinitely. So there is nothing in the codebase that meets a data-residency or retention
obligation; that would have to be designed and added (region-locked vendor endpoints or self-hosted
models, redaction before the LLM, trace retention limits). Residency for the LLM specifically is
constrained by provider region availability, which is a contractual/config choice not present here.

---

## 5. Tenant isolation

> **Original question:** Tenant isolation. Is one tenant's data, credentials, and connections separated from another's by architecture, or only by coding convention? What makes a cross-tenant leak impossible?

**[Absent] — there is no tenant concept. Isolation would be by *process*, not by design, and only on the Daily path.**

`grep` for `tenant` across the repo returns nothing. Configuration is **global, per-process** via
environment variables and `.env` (`config/bot.py` reads `os.getenv` for keys, provider, `BOT_TYPE`,
`PAYMENT_*` limits). One server serves exactly one configuration; `BOT_TYPE` selects one bot for the
whole process. There is no per-tenant routing, credential store, or data partition. Nothing makes a
cross-tenant leak "impossible"; today the answer is "run one deployment per tenant" (isolation by
separate process/container/namespace), which is an operational convention, not an architectural
guarantee. Multi-tenancy is unbuilt.

---

## 6. Noisy-neighbour / blast radius

> **Original question:** Noisy-neighbour / blast radius. Can one tenant's traffic spike or failure degrade another tenant's calls? What bounds the blast radius?

**[Built-in risk] — on the default transport, one call CAN degrade others; blast radius = the whole server process.**

On the WebSocket/FreeSWITCH path every call is an async task in **one** uvicorn process
(`main.py:audio_websocket`). They share one event loop, one CPU-bound Silero VAD path per call, one
GIL, and one set of vendor connections. A traffic spike or a wedged call (e.g. a stuck vendor call
with no heartbeat) contends for and can starve the shared loop, and an unhandled crash of the
process takes down **every** concurrent call on it. There is no per-call CPU/concurrency cap on this
path (the only limit, `max_bots_per_room`, is Daily-only, `main.py:230`). Bounding the blast radius
is left to deployment: the Azure/AKS templates run multiple bot pods behind a Service with an HPA
(`deploy/azure/README.md`), so the blast radius is "one pod's worth of calls," and `enable_heartbeats`
(wedged-call detection) is listed as a production add in `docs/PIPECAT_PATTERNS.md` B — not yet on.

---

## 7. Per-tenant secrets, config, limits

> **Original question:** Per-tenant secrets, config, limits. How are per-tenant credentials, configuration, and concurrency limits scoped and enforced?

**[Absent per-tenant] / [Built globally].** Secrets and limits are **process-global env vars**:
provider API keys (`GOOGLE_API_KEY`, `DEEPGRAM_API_KEY`, …), `PAYMENT_MIN_AMOUNT` /
`PAYMENT_MAX_AMOUNT` / `PAYMENT_MAX_ATTEMPTS` / `PAYMENT_IDLE_TIMEOUT_S` (`bots/payment.py:45-48`),
and concurrency (`max_bots_per_room`, Daily-only). There is no per-tenant scoping of any of these —
no secrets manager, no per-tenant config object, no per-tenant rate/concurrency enforcement. Enforcing
them per tenant requires the tenancy layer that does not exist (Q5).

---

## 8. Worker failure mid-call

> **Original question:** Worker failure mid-call. If the process handling a call crashes mid-transaction, what does the caller experience and what state is the backend/ledger left in?

**[Weak] — caller is dropped; no transactional state is kept.**

If the process/task handling a call crashes, the WebSocket to `mod_audio_fork` closes; `main.py`'s
`finally` attempts an ESL `uuid_kill` so the caller isn't left parked in silence
(`esl_client.hangup`), but if the whole process died that cleanup may not run and FreeSWITCH tears
the leg down on socket loss anyway. The caller experiences a dropped call. Backend/ledger state:
there is **none to be left inconsistent today** because the payment API is a mock that returns
success synchronously (`MockPaymentApi.create_payment_order`) and flow state lives only in
`flow_manager.state` in memory — it is lost on crash. With a *real* payment API and a crash between
"charge sent" and "success spoken," there is nothing in the code to detect or reconcile that
in-flight state (no ledger, no persisted context). Call-resumption via serialized context is a
roadmap item (`docs/PIPECAT_PATTERNS.md` B, "Call resumption").

---

## 9. Exactly-once / no double-charge

> **Original question:** Exactly-once / no double-charge. If a call resumes after a failure, what prevents a payment being taken twice?

**[Absent] — no idempotency, no dedup, no resume. A resumed call would re-charge.**

There is no idempotency key, no request de-duplication, and no persisted "already charged" marker.
`confirm_payment` calls `payment_api.create_payment_order(invoice.number, amount)` directly
(`bots/payment.py:476`); the only mention of the concept is a literal TODO:
`# TODO(you): replace with the real payment order API (idempotent — never double-charge).`
(`bots/multiagent/services.py:49`). Because state is in-memory only, a caller who redials after a
mid-transaction failure starts fresh and, against a real gateway, could be charged again. Preventing
this requires (not built): an idempotency key per payment attempt sent to the gateway, and/or a
durable ledger consulted before charging.

---

## 10. Recovery & failover

> **Original question:** Recovery & failover. How long is a dropped call unavailable? Is "the caller redials and resumes" acceptable for high-value/regulated flows, or is a layer needed to keep the call up while its worker is replaced?

**[Absent] — a dropped call is gone; "redial and resume" is not implemented, and no call-preserving layer exists.**

There is no failover, no state persistence, and no session that survives worker replacement. A
dropped call is unavailable until the caller redials, and a redial starts a **new** call from the
first node (in-memory `flow_manager.state` is lost). So "the caller redials and resumes" is not even
true today — they redial and *restart*. For high-value/regulated flows that is not acceptable, and
the codebase has no layer to keep the call up while a worker is replaced. The building blocks named
as roadmap: context serialize/restore for resumption, and heartbeats for stall detection
(`docs/PIPECAT_PATTERNS.md` B). A media-anchoring layer (SBC/FreeSWITCH holding the leg while the bot
backend reconnects) would be required for true keep-alive and is not present.

---

## 11. Cost at concurrency

> **Original question:** Cost at concurrency. What does one call-minute cost at peak concurrency (compute per call plus speech/model/voice usage), and how does it compare to a deterministic IVR?

**[Not measured in-repo] — no cost model exists; here is the honest basis and the one hard number the repo gives.**

The repo has **no** cost instrumentation or pricing model (only `enable_usage_metrics=True` on the
pipeline, `base_bot.py:330`, which emits token/usage metrics but does not price them). A per-call-
minute cost is the sum of: (a) compute — one shared-process async task (cheap; the CPU cost is
Silero VAD + serialization, which the AKS mocks exist to measure, `deploy/azure/README.md`); plus
(b) usage — Deepgram STT per minute + LLM tokens per turn (Google/OpenAI/Anthropic; the default is
`gemini-2.0-flash` / `claude-haiku-4-5`, chosen for low cost) + TTS per character. Those are external
list prices, not in the code, so any figure I gave would be invented — I won't. Versus a
deterministic IVR the qualitative answer *is* in the code: an IVR has **zero** STT/LLM/TTS usage
cost, while this stack pays all three per call; the mitigations present are cheap-model defaults and
`tts_say`/pre-recorded lines that skip LLM round-trips on scripted turns (`bots/payment.py`
comments). To get real numbers, wire a usage→cost observer (unbuilt) and run the AKS capacity test.

---

## 12. Concurrency ceiling

> **Original question:** Concurrency ceiling. What is the maximum sustained concurrent-call count, and how does cost grow with volume?

**[Bounded by externals, not measured] — the code imposes no sustained-concurrency limit; three real ceilings apply.**

No global concurrent-call cap exists on the WebSocket path (again, `max_bots_per_room` is Daily-only).
The practical ceilings, all documented in-repo:
1. **Media module licensing** — `docs/FEATURES.md` notes the FreeSWITCH audio module is **free ≤10
   concurrent channels**, "a prod-scale licensing decision pending." That is the first hard wall.
2. **Provider rate limits / spend** — Deepgram/LLM/TTS per-account concurrency and RPM caps
   (`docs/PIPECAT_PATTERNS.md` targets "3000–5000 concurrent" as an *aspiration*, not a tested
   figure; also names LLM/STT failover as required at scale).
3. **Per-pod event-loop saturation** — since calls share one process (Q6), sustained ceiling per pod
   is CPU/loop-bound and must be measured (the AKS "mocked-AI capacity test" exists precisely to find
   this; it "has not been run", `deploy/azure/README.md`).
Cost grows roughly linearly with concurrent minutes (usage-dominated), plus step costs when adding
pods/nodes. No measured maximum exists in the repo.

---

## 13. Authoring without a deploy

> **Original question:** Authoring without a deploy. Can non-developers create and change call flows without an engineering release, and what is the review/approval step before a change goes live?

**[Partial] — a visual editor round-trip exists for the *graph*, but changes are re-imported as files and still ship via redeploy; no approval workflow.**

`bots/payment_editor/` is a copy of the payment bot reshaped so its **routing graph** can be edited
in the Pipecat Flows visual editor (https://flows.pipecat.ai/) and round-tripped through
`flow.json` (`payment_editor/README.md`). So a non-developer can rewire node order / conditions /
static prompts in a UI. **But**: (a) the editor controls only the graph and static text — the
"handler bodies" (lookup, money limits, retry budgets) live in `payment_editor.py` and are opaque to
the canvas; (b) the edited `flow.json` is a file in the repo that is still loaded at process start,
so a live change today implies a redeploy (see Q23); (c) there is **no review/approval step** in the
code — no staging, sign-off, or gate. So "authoring without a deploy" is only partially true and
only for graph/prompt changes; money logic and go-live still require engineering.

---

## 14. Versioning & access control

> **Original question:** Versioning & access control. How are flow definitions versioned and rolled back, and who is authorized to change a sensitive flow (e.g. payment or collections)?

**[Partial] — versioning is whatever git/the flow-file gives; no role-based authorization on sensitive flows.**

Flow definitions are either Python (`bots/payment.py`, node factories) or the JSON graph
(`flow.json`, which carries a `meta.version` field, currently `"1.0"`). Versioning and rollback
therefore ride on **git** (and the JSON `version` string) — there is no application-level flow
registry with versions/rollback. Access control: **none in code**. There is no auth on who can
change a flow, and notably the HTTP server itself is wide open — CORS `allow_origins=["*"]`
(`main.py:106`) and no authentication on `/`, `/connect`, or `/audio`. Who may change a payment or
collections flow is, today, "whoever can commit to the repo / deploy," not an enforced role.

---

## 15. DTMF collection & deterministic flows

> **Original question:** DTMF collection & deterministic flows. Can the system perform DTMF collection end-to-end — menu selection and multi-digit collection with length / terminator / timeout, validation, and retry — driven deterministically without an LLM? Who builds and owns that deterministic flow logic?

**[Partial] — DTMF is captured and aggregated deterministically, but it is then handed to the LLM as a user turn, not run by a deterministic collector.**

DTMF works end-to-end mechanically: `dtmf_bridge.py` subscribes to FreeSWITCH ESL `DTMF` events,
**aggregates digits per channel with an inter-digit timeout and a `#` terminator**
(`DTMF_INTERDIGIT_MS`, `DTMF_TERMINATOR`), and forwards the completed string over the fork socket;
`serializers/audio_fork.py:deserialize` then injects it. The important nuance: it injects the digits
as an `LLMMessagesAppendFrame` (`{"role":"user","content": digits}, run_llm=True`) — i.e. **the
digit string is fed to the LLM as a normal user turn**, and validation/length/retry are then done by
the *handler code* (`_invoice_format_ok`, `_strike`), not by a menu/length/terminator state machine.
So "menu selection and multi-digit collection with length/terminator/timeout" is partly deterministic
(the bridge does timeout+terminator+aggregation) and partly LLM-mediated (interpretation + routing).
It is **not** a pure no-LLM DTMF collector. `docs/PIPECAT_PATTERNS.md` A flags the intended fix:
switch the serializer to emit canonical `InputDTMFFrame` via `DTMFAggregator()` so keypad entry uses
the same deterministic turn machinery — roadmap. Ownership: the deterministic pieces (bridge,
serializer, handler budgets) are **this repo's** code; the interpretation currently leans on the LLM.

---

## 16. Classic IVR without an LLM (end-to-end)

> **Original question:** Classic IVR without an LLM (end-to-end). Can the system run a complete traditional IVR flow with no LLM anywhere — DTMF menus, bounded speech recognition (STT constrained to a fixed grammar / option set, not open NLU), and pre-recorded audio prompts (not TTS) — driven by deterministic flow logic? What in the stack provides the bounded-grammar STT and the pre-recorded prompt playback, and is the per-call footprint/cost acceptable for high-volume simple flows?

**[Absent] — the stack cannot today run a complete IVR with no LLM anywhere.**

Three gaps, all real in the code:
- **Prompts:** playback is TTS-only. The serializer streams TTS PCM; there is no pre-recorded-audio
  prompt player in the flow (FreeSWITCH `uuid_broadcast`/`playback` of files is mentioned only as a
  *fallback* idea in the dialplan comments, `docker/freeswitch/.../mrf.xml`, not wired into the bot).
- **Bounded-grammar STT:** STT is Deepgram `nova-3-general` open transcription (`base_bot.py:88`);
  there is no fixed-grammar / option-set recognizer. Turn logic is VAD + an LLM completeness judge.
- **Flow driver:** every bot node runs through the LLM (`FlowManager` + LLM service); nodes that
  "speak only" still instantiate the LLM in the pipeline.
DTMF is the one input that can be interpreted without the LLM *in principle* (Q15), but as wired it
still posts to the LLM. So a true "no LLM anywhere" IVR — DTMF menus + fixed-grammar STT +
pre-recorded prompts on a deterministic engine — is not something this stack does today; it would be
new work (and arguably a different, cheaper engine). For high-volume simple flows the current
per-call footprint (STT+LLM+TTS every call) is *not* the cost-appropriate choice versus a classic IVR
(see Q11).

---

## 17. Prompt-injection / policy adherence

> **Original question:** Prompt-injection / policy adherence. Can a caller induce the agent to skip a required step, waive a fee, or mishandle data, and how is that prevented?

**[Partial] — the "code decides money" split is the main defense; it is real but not complete.**

The core mitigation is architectural and genuinely present: the LLM decides *conversation*, but
**code decides money and state** — invoice lookup, `MIN/MAX_AMOUNT` limits, 3-strike budgets,
routing, and the payment call are all in Python handlers the caller cannot talk their way past
(`bots/payment.py`, module docstring + `collect_payment_details`/`confirm_payment`). A caller cannot
"waive a fee" or skip validation because the LLM has no tool to do so — it can only call the
constrained functions. Verbatim terminal/disclosure lines are action-driven, not LLM-phrased (Q2).
Gaps: the *conversational* surface is still an LLM with a prose system prompt, so it could be talked
into off-script chatter, mis-reading a value back, or answering out-of-scope; there is no input
sanitization, no jailbreak classifier, and the schema-level arg constraints
(`FlowsFunctionSchema` min/max, "hard-constrained money args") are listed as **not-yet-adopted** in
`docs/PIPECAT_PATTERNS.md` A. So required *steps* are well protected; *wording and scope* rely on
prompt discipline plus the deterministic read-backs.

---

## 18. Dependency exposure

> **Original question:** Dependency exposure. What is the exposure to breaking changes in Pipecat and to the pricing, latency, or deprecation of the LLM provider, and what effort would it take to move off either later?

**Pipecat: [High but pinned]. LLM provider: [Low-to-moderate, swappable].**

Pipecat is a **deep** dependency and is installed **editable from a vendored submodule pinned to
v1.5.0** (`external/pipecat`, README "Server Setup"; `.gitmodules`). The bot builds directly on
Pipecat internals — `FlowManager`, `PipelineWorker`/`WorkerRunner`, aggregators, turn strategies,
transports, service classes — so a breaking change between Pipecat majors would touch `base_bot.py`
and every bot. The pin protects you (you upgrade deliberately), but the coupling means a later
upgrade is real work. Moving *off* Pipecat entirely would be a rewrite of the pipeline layer.

The LLM provider is deliberately **swappable**: `LLM_PROVIDER` selects Google/OpenAI/Anthropic behind
one `match` in `base_bot.py:132`, each with model/temperature config. Switching providers, or their
pricing/latency/deprecation, is a config change — low effort. `LLMSwitcher`-based live failover is
available in Pipecat but **not wired** here (`docs/PIPECAT_PATTERNS.md` B/C). Same swappability holds
for STT/TTS. Net: your concentrated risk is Pipecat, not the model vendor.

---

## 19. What we build & operate ourselves

> **Original question:** What we build & operate ourselves. Given everything must be open-source and on-prem (the only external dependency being paid API keys to cloud-hosted LLMs), which components must we build and operate ourselves — call spawning, scaling, high-availability, tenancy, the front-door router — and what is the build-plus-operate cost?

**[Mixed]** — on an open-source/on-prem stance (only paid API keys leaving the box), the components
you must build and run yourself are, per the code:
- **Already built here:** the FreeSWITCH↔Pipecat wire format + serializer (`serializers/audio_fork.py`),
  the `/audio` call entry + in-process bot lifecycle (`main.py`), ESL call-control (`esl_client.py`),
  the DTMF bridge (`dtmf_bridge.py`), the FreeSWITCH dialplan/Docker stack (`docker/freeswitch/`), the
  guided payment flow + limits/budgets (`bots/payment.py`), and env-driven config (`config/`).
- **Must still build/operate (Roadmap/Absent):** call **spawning at scale** and a **front-door
  router** that maps an inbound call to a worker (Q28 — today it's a stateless WS to one server/pool),
  **horizontal scaling & autoscaling on active calls** (only a CPU-HPA placeholder exists,
  `deploy/azure`), **high availability / failover / call-preservation** (Q10, none), **multi-tenancy**
  (Q5, none), **audit/PII/retention** (Q3/Q4, none), **exactly-once payments** (Q9, none), and the
  media-module **licensing/scale** decision (Q12).
- **The only unavoidable external dependency** in the on-prem posture is exactly what you said —
  paid API keys to cloud STT/LLM/TTS. Those can be reduced (self-hosted STT/TTS, local models) but are
  cloud today.
Build-plus-operate cost is substantial and mostly in the second bullet: the router, scaling/HA, and
the compliance layers are greenfield, each non-trivial. The telephony media plane and the bot logic
are the parts that already exist.

---

## 20. Integration with existing infrastructure (tenant-service, instance-registry, clicktopay)

> **Original question:** Integration with existing infrastructure. How does the system integrate with our existing infrastructure and services (e.g. tenant-service, instance-registry, clicktopay)?

**[Absent] — there is no integration with any of these in the repo.**

A search across the repository for `tenant`, `instance-registry`/`instance_registry`,
`tenant-service`, and `clicktopay`/`click-to-pay` returns **nothing** (outside vendored deps). The
system's only external touchpoints in code are the AI vendors (STT/LLM/TTS SDKs), FreeSWITCH (media +
ESL), and the invoice/payment APIs — and the latter are **mocks** (`services/invoice_api.py`,
`bots/multiagent/services.py`) with abstract interfaces meant to be swapped for real REST clients.
So integration with tenant-service / instance-registry / clicktopay is entirely to-be-built; the
clean seam for it is those API interface classes and the (unbuilt) tenancy/router layer.

---

## 21. Deployment model

> **Original question:** Deployment model. Do we deploy on bare metal or as containers on our Kubernetes, and what does a per-call-process model require of that choice?

**[Both exist] — bare-metal/dev on Windows, and containers on Kubernetes (AKS) as the scale target.**

The bot runs natively on Windows for dev on the FreeSWITCH path (README "Server Setup"; the whole
point of the WebSocket transport is avoiding `daily-python`'s missing Windows wheels). For scale,
`deploy/azure/` ships Bicep + k8s manifests: bot `Deployment` behind a ClusterIP `bot-svc:7860` with
an HPA, and FreeSWITCH as a `hostNetwork` pod on a dedicated, public-IP node pool
(`deploy/azure/README.md`). A `server/Dockerfile` exists. What the **per-call-process model requires
of that choice**: because a WebSocket call is a long-lived, stateful, in-process task, the container
platform must (a) keep that TCP/WS connection pinned to one pod for the call's whole life (no
mid-call rebalancing), and (b) **drain gracefully** on scale-down — which the templates handle with a
`preStop` drain + long termination grace so in-flight calls finish (`deploy/azure/README.md`). Bare
metal avoids the routing/drain complexity but gives up autoscaling.

---

## 22. Bare-metal dependencies

> **Original question:** Bare-metal dependencies. If bare metal, what must be installed on the host — Python runtime and app libraries, native audio/codec libraries, a SIP/WebRTC media stack or provider client, the provider SDKs, a process manager, and outbound access to the speech/model providers?

On a bare-metal host the code implies you must install: **Python runtime + app libs** (`server/
requirements.txt` + the editable Pipecat extras — README pins
`pip install -e "../external/pipecat[websocket,google,openai,deepgram,cartesia,elevenlabs,rime,silero]"`);
**native audio/VAD/codec libs** pulled by those extras (Silero VAD via ONNX Runtime, `soxr`/`resampy`
resamplers, `numpy`/`scipy` — all present in the venv); **a SIP/WebRTC media stack** — here
**FreeSWITCH** with the `mod_audio_fork`/`mod_audio_stream` module and a dialplan
(`docker/freeswitch/`), reached over WebSocket + ESL; **the provider SDKs** (Deepgram, Cartesia,
ElevenLabs, Rime, Google, OpenAI, Anthropic — installed as Pipecat extras); **a process manager** to
run uvicorn (and, per `dtmf_bridge.py`'s docstring, the DTMF bridge as a side service) — none is
bundled, so systemd/supervisor is yours to add; and **outbound network access** to the STT/LLM/TTS
endpoints (the only egress the on-prem posture needs). Note the Daily transport additionally needs
`daily-python`, which has no Windows wheels — hence FreeSWITCH is the native path.

---

## 23. Runtime flow deployment

> **Original question:** Runtime flow deployment. How is a new or changed flow deployed while the system is running, and does it require downtime? If flows are code, does a change require a full redeploy; if flows are to be updated without a redeploy, what loads flow definitions as data?

**[Absent for zero-downtime] — flows load at process start; a change is effectively a redeploy today.**

Flows are **code and start-time data**: Python node factories (`bots/payment.py`) and, for the
editor variant, a `flow.json` that is read when the bot is constructed. There is **no** hot-reload,
no flow service that fetches definitions per call, and no versioned flow store. So a new/changed flow
requires restarting the process — and because calls are in-process tasks, a naïve restart drops live
calls (mitigated only by the k8s drain in Q21). Answering the sub-questions directly: flows today are
effectively **code**, so a change needs a redeploy; to update flows *without* a redeploy you would
need a loader that reads flow definitions as **data** at call start from an external store (DB/config
service) and a `FlowManager` initialized from that — the `flow.json` schema is the natural format for
it, but the "load from a store at runtime" piece is not built.

---

## 24. Horizontal scaling

> **Original question:** Horizontal scaling. How does the system scale out across machines? Given each call runs as its own process, how are concurrent calls distributed across processes/pods, and what is the scaling ceiling (provider rate limits, cost)?

**[Partial] — scales by running more bot pods behind a Service + HPA; each call is one task on one pod; ceilings are external.**

Scale-out model (from `deploy/azure/`): FreeSWITCH forks each call's media to `ws://bot-svc:7860/
audio?uuid=…`; `bot-svc` is a ClusterIP Service load-balancing across N bot pods; an HPA grows/shrinks
the pod count. Concurrent calls are distributed by **which pod the Service sends each new WebSocket
to**, and that pod runs the whole call in-process (note: this is per-*task* concurrency within a pod,
not per-OS-process — Q6). Real ceilings: the **media-module license (≤10 free channels)**, **provider
rate limits/spend**, and **per-pod loop saturation** (Q12). Caveats in the code/docs: the HPA metric
is a **CPU placeholder** ("swap for KEDA active-calls later", `deploy/azure/README.md`) — scaling on
active calls is not yet real — and the "3000–5000 concurrent" target is an aspiration, untested. The
richer multi-worker/bus scale-out (Redis/PGMQ) is roadmap only (`docs/PIPECAT_PATTERNS.md` C).

---

## 25. Worker / LLM failure handling

> **Original question:** Worker / LLM failure handling. What happens when the LLM provider is down, tokens are exhausted, the network drops, or the model misbehaves mid-call? Is the caller left in silence? Can the call escalate (to a human or a fallback)? Is the failure logged with enough detail to diagnose?

**[Weak/Partial] — errors are logged and the payment path fails safe, but there is no fallback/escalation and the caller can be left silent.**

Payment-API failure is handled well and safely: `confirm_payment` routes to `create_api_failure_node`
which speaks a verbatim "No payment has been taken… please try again later" and ends
(`bots/payment.py:330,476`) — money operations are never blindly retried. But **LLM/STT/TTS provider
failure mid-call is not handled**: there is no `LLMSwitcher`/`ServiceSwitcher` failover (listed as a
production add, `docs/PIPECAT_PATTERNS.md` B/C), no fallback model, and no human/IVR escalation wired
(warm transfer is roadmap). If the LLM is down, tokens are exhausted, or the network drops, the turn
simply fails; barring the idle-timeout path (which reprompts up to 3× then terminates,
`_handle_idle`), the caller can be left in silence until the socket drops. Diagnosis: yes — `loguru`
logs exceptions with backtraces (`main.py` `logger.exception`; `diagnose=True`) and the optional
frame trace captures the sequence, so failures are diagnosable even though they are not *recoverable*.

---

## 26. Telephony-connection failure

> **Original question:** Telephony-connection failure. If the telephony connection fails (e.g. FreeSWITCH down, network issue), are resources released cleanly, and can partial changes (e.g. a charge already made) be rolled back or compensated?

**[Partial] — media resources release cleanly on disconnect; there is no charge compensation/rollback.**

Resource release is handled: transport `on_client_disconnected` / `on_participant_left` →
`_shutdown_workers()` cancels the pipeline worker (and Whisker if on), and `cleanup()` runs in
`main.py`'s `finally`; the worker owns the transport in Pipecat 1.x, so cancelling it stops the
pipeline and closes the connection (`base_bot.py:248-278,409-429`). At end-of-flow an ESL `uuid_kill`
drops a parked FreeSWITCH leg so the caller isn't stranded (`esl_client.hangup`), and it's a harmless
no-op if the channel is already gone. What is **not** handled: compensation for a partial money
change. There is no rollback/void path and no ledger — if a charge had been made against a real
gateway and the telephony leg then failed, nothing in the code reverses or reconciles it (ties back to
Q9). With the current mock that can't happen; with a real gateway it is an open gap.

---

## 27. Per-tenant/solution narration

> **Original question:** Per-tenant/solution narration. How is it configured, per tenant and per solution, how a given step or requirement is worded to the caller, and how is exact wording guaranteed where it is required (cf. #2)?

**[Partial] — per-step wording is configurable in the flow definition, and exact wording is guaranteed by the deterministic action path; but there is no *per-tenant* layer.**

How a step is worded lives in the node: `tts_say`/`announce` pre-action text and `task_messages`
(`bots/payment.py`, `payment_editor.py`, `flow.json`). Static wording is editable — including in the
visual editor for the graph variant (Q13) — so "how a given step is worded to the caller" is a
first-class, data-shaped thing. Exact/verbatim wording where required is guaranteed by the same
mechanism as Q2: the deterministic `tts_say`/`end_conversation`/`announce` actions bypass the LLM, so
the string is spoken as written (subject to the TTS-rendering caveat). The missing piece is the
**per-tenant / per-solution** dimension: because there is no tenancy (Q5), there is no place to vary
narration by tenant beyond running a different deployment or editing the flow file. So: per-*step* and
per-*solution* (via the flow definition) — yes; per-*tenant* at runtime — not without the tenancy
layer.

---

## 28. Inbound call → worker routing

> **Original question:** Inbound call → worker routing. With one process per call across many pods/containers, how does the telephony layer know which worker to connect a call to? What component accepts the call, selects or starts a worker, connects the media to that specific worker, tracks the mapping for the call's lifetime, and handles "all workers busy"?

**[Partial] — there is no worker-selecting router; it relies on a stateless load-balancer + a long-lived socket, and "all workers busy" is unhandled.**

Today the mapping is implicit, not managed. FreeSWITCH's dialplan forks media to a fixed URL
(`ws://<host>:7860/audio?uuid=${uuid}`, `docker/freeswitch/.../mrf.xml`); in k8s that host is the
`bot-svc` Service, which load-balances the **new WebSocket connection** to some bot pod
(`deploy/azure/README.md`). That pod's `main.py:audio_websocket` then **builds and starts the worker
in-process** and keeps it for the life of the socket — so "which worker handles the call" is decided
by ordinary L4 load-balancing of one long-lived connection, and the mapping persists only as that live
socket (plus the `?uuid=` used for ESL hangup). Answering the component checklist directly: *accepts
the call* = the `/audio` WebSocket handler; *selects/starts a worker* = there is no selector — the LB
picks a pod and the pod self-starts a task; *connects media to that worker* = the WS itself;
*tracks the mapping* = only the live socket + `uuid` (Daily path additionally tracks `bot_procs` by
PID, `main.py:34`); *handles "all workers busy"* = **not handled** on the WebSocket path (no admission
control or queue; the Daily-only `max_bots_per_room` 429 is the sole capacity check). A real front-
door router (admission control, worker registry, busy handling, and — if calls must survive worker
replacement — media anchoring) is greenfield, and is exactly what `docs/PIPECAT_PATTERNS.md` C and
Q10/Q19 flag as the unbuilt scaling work.

---

## Cross-cutting summary

Built and solid: the guided payment **flow** with code-owned money logic and 3-strike budgets (Q17),
**verbatim** action-driven disclosures (Q2/Q27), the **FreeSWITCH media plane** + serializer + ESL
control + DTMF bridge (Q15/Q19/Q22), provider **swappability** (Q18), and a **container deployment**
with graceful drain (Q21).

Not built (the gap between "working demo" and "regulated multi-tenant platform"): **PCI card
capture/tokenization** (Q1), **audit/PII/residency/retention** (Q3/Q4), **multi-tenancy and per-tenant
isolation/secrets/limits** (Q5/Q7/Q27), **HA / call-preservation / exactly-once payments**
(Q8/Q9/Q10/Q26), a **front-door router with admission control** (Q28), **active-call autoscaling and a
tested concurrency/cost model** (Q11/Q12/Q24), **provider failover / human escalation** (Q25),
**runtime flow deployment without redeploy** (Q23), a **no-LLM classic-IVR path** (Q16), and any
**integration with tenant-service / instance-registry / clicktopay** (Q20). The payment/invoice
backends are still **mocks**.
