# Feasibility, approach & effort — building the 28 on Pipecat + Pipecat Flows

Companion to `ARCHITECTURE_ASSESSMENT.md`. That doc said what exists today; this one answers,
for each concern: **is it possible, how would it be done (configure Pipecat vs. assemble Pipecat
parts vs. write custom Python vs. build outside Pipecat), and how much effort** — with the working
assumption that you want to *configure an existing product*, not write a platform from scratch.

Verified against the pinned source (`external/pipecat` @ v1.5.0): the components referenced below
genuinely ship — `pipeline/llm_switcher.py`, `processors/audio/audio_buffer_processor.py`,
`processors/aggregators/dtmf_aggregator.py`, `flows/manager.py` (+ static `FlowConfig`),
`workers/proxy/websocket/`, `registry/`, `bus/network/{redis,pgmq}.py`,
`utils/context/llm_context_summarization.py`, `extensions/voicemail/`, `evals/`.

### The honest framing (read this first)

Pipecat is a **per-call media-pipeline framework** (audio in → STT → LLM/Flows → TTS → audio out,
one call at a time) plus a **Flows** engine for structured conversations, plus optional **multi-worker**
plumbing (bus/registry/proxy). It is *excellent* for everything that happens **inside one call**, and
most of those items really are "configure / assemble." It is **not** a tenancy platform, an HA/media-
anchoring layer, a PCI descoping boundary, an approval/RBAC system, or a call router with admission
control. Those live **around** Pipecat and are real engineering regardless of framework.

So the realistic split across the 28:

- **~40% configure/assemble** existing Pipecat features (in-call behaviour, disclosures, DTMF,
  failover, recording, evals).
- **~35% custom Python** on Pipecat's clean seams (audit sink, cost observer, API integrations,
  idempotency, flow-from-store loader, per-call tenant/config resolver).
- **~25% platform engineering mostly outside Pipecat** (multi-tenancy, HA/keep-alive, front-door
  router + admission control, PCI capture boundary, RBAC/versioned flow store).

Effort legend (one mid-level engineer, order-of-magnitude, excludes QSA/procurement/load-test time):
**XS** <1d · **S** 1–3d · **M** 1–2wk · **L** 3–6wk · **XL** 2mo+/multi-person.
Approach tags: **[CONFIG]** env/params · **[ASSEMBLE]** wire existing Pipecat components ·
**[CUSTOM]** write Python on a Pipecat seam · **[PLATFORM]** build around Pipecat.

---

## Summary table

| # | Concern | Possible? | Approach | Effort |
|---|---------|-----------|----------|--------|
| 1 | PCI-safe capture | Yes (with a descoping boundary) | CUSTOM + PLATFORM | L |
| 2 | Verbatim disclosures | Yes — pattern already built | CONFIG/ASSEMBLE | S |
| 3 | Auditable decisions | Yes | ASSEMBLE + CUSTOM | M |
| 4 | PII handling & residency | Yes (varies by strictness) | CONFIG + CUSTOM (+PLATFORM if self-host) | M–L |
| 5 | Tenant isolation | Yes | PLATFORM (per-tenant deploy = M; shared = L) | M–L |
| 6 | Noisy-neighbour / blast radius | Yes (bound it) | CONFIG + CUSTOM | M |
| 7 | Per-tenant secrets/config/limits | Yes | CUSTOM (needs #5) | M |
| 8 | Worker failure mid-call | Yes (graceful + checkpoint) | CUSTOM | M |
| 9 | Exactly-once / no double-charge | Yes | CUSTOM (your gateway) | M |
| 10 | Recovery & failover | Redial-resume: yes. Keep-alive: hard | ASSEMBLE (resume) / PLATFORM (anchor) | M / L–XL |
| 11 | Cost at concurrency | Yes — measure | ASSEMBLE | S–M |
| 12 | Concurrency ceiling | Yes — measure + license | CONFIG + procurement | S |
| 13 | Authoring without deploy | Graph/prompts: yes. Money logic: no | CUSTOM + PLATFORM | M |
| 14 | Versioning & access control | Yes | PLATFORM (+ add API auth) | M |
| 15 | DTMF deterministic collection | Yes | ASSEMBLE + CUSTOM | S–M |
| 16 | Classic IVR, no LLM anywhere | Yes (you build the deterministic bits) | CUSTOM | M–L |
| 17 | Prompt-injection / policy | Yes — hardening | CONFIG/ASSEMBLE | S–M |
| 18 | Dependency exposure | Yes — mitigate | CONFIG/ASSEMBLE | S |
| 19 | What we build ourselves | (summary of the rest) | — | — |
| 20 | Integrate tenant-svc/registry/clicktopay | Yes | CUSTOM | M each |
| 21 | Deployment model | Yes — templates exist | CONFIG/PLATFORM | S–M |
| 22 | Bare-metal dependencies | Yes | CONFIG | S–M |
| 23 | Runtime flow deployment | Yes (graph as data) | CUSTOM | S–M |
| 24 | Horizontal scaling | Yes | CONFIG/PLATFORM (+ASSEMBLE if distributed) | M (L if bus) |
| 25 | Worker/LLM failure handling | Yes | ASSEMBLE | M |
| 26 | Telephony-connection failure | Mostly done; add compensation | CUSTOM | S–M |
| 27 | Per-tenant/solution narration | Yes | CUSTOM (needs #5/#7) | S–M |
| 28 | Inbound call → worker routing | Yes | PLATFORM (+ASSEMBLE if using bus/registry) | L |

---

## Per-item: how it would be done

> **Original question:** PCI-safe capture. When a caller provides card data, does the audio and its transcript pass through the STT, LLM, TTS, or recording paths? Can card data be captured and tokenized without bringing those components into PCI-DSS scope? Would a QSA certify the resulting flow?

**1 — PCI-safe capture.** Possible, but the value is in *keeping card data out of STT/LLM/TTS*, which
is a boundary you build, not a Pipecat setting. Approach: collect the PAN as **DTMF** on a call leg
where `mod_audio_fork` is paused (so audio never reaches Deepgram/LLM/TTS/recording), aggregate with
Pipecat's `DTMFAggregator`, and tokenize via a PCI-scoped gateway; resume the bot with only a token.
Pipecat parts (`DTMFAggregator`, pause/resume via ESL) are small; the descoping boundary + gateway +
QSA evidence are the work. **[CUSTOM+PLATFORM], L.** Alternatives: (a) blind/warm transfer to an
existing PCI IVR for the card step; (b) a dedicated pause-and-resume capture microservice.

> **Original question:** Verbatim mandated disclosures. How is a legally-required disclosure delivered word-for-word on every call, with no paraphrasing or variation?

**2 — Verbatim disclosures.** Already works via `tts_say`/`end_conversation` actions (no LLM in the
loop). To make it audit-grade: swap TTS for **pre-recorded audio prompts** on mandated lines (play a
file via a prompt processor / FreeSWITCH `playback`) and log a "disclosure played" marker. Mostly a
content + config task. **[CONFIG/ASSEMBLE], S.**

> **Original question:** Auditable decisions. What record shows why the system took a given action on a call, and is it sufficient for a dispute or a regulator?

**3 — Auditable decisions.** Assemble a custom `BaseObserver.on_push_frame` sink (the hook Pipecat
gives you — the repo already uses one for tracing) that writes turns, tool calls, node transitions and
the payment-order id to a **durable store keyed by a stable call-id**. Pipecat emits the events; you
write the schema + DB writer + retention. **[ASSEMBLE+CUSTOM], M.**

> **Original question:** PII handling & residency. How is caller PII handled, retained, and segregated across each third-party provider (speech, model, voice), and does this meet our data-residency and retention obligations?

**4 — PII handling & residency.** Three levers: pin **regional vendor endpoints** where offered
(config), insert a **redaction processor** before the LLM for anything you must not send (custom
processor on the pipeline), and set **trace/recording retention** (custom). If residency is strict
enough to forbid cloud AI, swap to **self-hosted STT/TTS/LLM** services (Pipecat supports local/OSS
services) — that's the expensive path. **[CONFIG+CUSTOM], M** (self-host pushes it to **L/PLATFORM**).

> **Original question:** Tenant isolation. Is one tenant's data, credentials, and connections separated from another's by architecture, or only by coding convention? What makes a cross-tenant leak impossible?

**5 — Tenant isolation.** Two viable models. (a) **One deployment per tenant** (separate
container/namespace, per-tenant `.env`): near-zero code, strong isolation, scales operationally —
**[PLATFORM/CONFIG], M**. (b) **Shared multi-tenant service**: resolve tenant from DNIS/SIP header at
call start and inject per-tenant config/secrets into the per-call bot — Pipecat makes (b) natural
because a bot is built per call, but you must build the tenant resolver, secret scoping, and data
partitioning — **[PLATFORM+CUSTOM], L**. Recommendation for a regulated start: (a) first, (b) later.

> **Original question:** Noisy-neighbour / blast radius. Can one tenant's traffic spike or failure degrade another tenant's calls? What bounds the blast radius?

**6 — Noisy-neighbour / blast radius.** Bound it with: `enable_heartbeats` for wedged-call detection
(config), a **per-pod concurrency cap + admission control** so one pod can't be oversubscribed
(custom gatekeeper — see #28), and process/pod-per-tenant or small pods so a crash is contained
(deploy). **[CONFIG+CUSTOM], M.**

> **Original question:** Per-tenant secrets, config, limits. How are per-tenant credentials, configuration, and concurrency limits scoped and enforced?

**7 — Per-tenant secrets/config/limits.** A **config resolver** invoked at call start keyed by
tenant, returning provider keys, `PAYMENT_*` limits, concurrency budget; back it with a secrets
manager. Pipecat services already take keys/limits as constructor args, so injection is trivial once
the resolver exists. Depends on #5. **[CUSTOM], M.**

> **Original question:** Worker failure mid-call. If the process handling a call crashes mid-transaction, what does the caller experience and what state is the backend/ledger left in?

**8 — Worker failure mid-call.** Handle disconnect gracefully (largely done) and **checkpoint flow
state** to a store at each node using `LLMContext.get_messages()/set_messages()` + your own `state`
snapshot, so a failure is detectable and reconcilable. Full mid-call survival needs #10. **[CUSTOM], M.**

> **Original question:** Exactly-once / no double-charge. If a call resumes after a failure, what prevents a payment being taken twice?

**9 — Exactly-once / no double-charge.** Not Pipecat's domain — it's your payment integration.
Generate an **idempotency key per payment attempt** (stash in flow state, persist), send it to the
gateway, and **check a durable ledger** before charging. The repo already isolates this behind
`PaymentApi` (`services/invoice_api.py`), so it's a contained change. **[CUSTOM], M.**

> **Original question:** Recovery & failover. How long is a dropped call unavailable? Is "the caller redials and resumes" acceptable for high-value/regulated flows, or is a layer needed to keep the call up while its worker is replaced?

**10 — Recovery & failover.** Two tiers. **Redial-and-resume**: serialize context + flow state to a
store (Pipecat `get_messages/set_messages`; the summarization util helps for long calls) and rehydrate
on redial — **[ASSEMBLE], M**. **Keep the call up while a worker is replaced**: needs a media-
anchoring layer (SBC or FreeSWITCH holding the leg while the bot backend reconnects) — **outside
Pipecat, [PLATFORM], L–XL**. For high-value flows, do redial-resume now; treat anchoring as a later
program.

> **Original question:** Cost at concurrency. What does one call-minute cost at peak concurrency (compute per call plus speech/model/voice usage), and how does it compare to a deterministic IVR?

**11 — Cost at concurrency.** Usage metrics are already on (`enable_usage_metrics=True`). Add a small
**usage→cost observer** (tokens×price, STT-min×price, TTS-chars×price) and run the existing AKS
**mocked-AI capacity test** for per-pod compute. Measurement, not construction. **[ASSEMBLE], S–M.**

> **Original question:** Concurrency ceiling. What is the maximum sustained concurrent-call count, and how does cost grow with volume?

**12 — Concurrency ceiling.** Measure per-pod loop saturation (via #11) and settle the **media-module
license** (the free tier caps at ~10 concurrent channels) and provider quota. Mostly config +
procurement. **[CONFIG + procurement], S** (plus the license decision).

> **Original question:** Authoring without a deploy. Can non-developers create and change call flows without an engineering release, and what is the review/approval step before a change goes live?

**13 — Authoring without a deploy.** Feasible for the **graph and static prompts**: store Flows static
JSON (the `flow.json` schema the visual editor already round-trips) in a config store and load it at
call start (custom loader). The **money logic stays in Python** and can't be pure config — the repo's
own `payment_editor/README.md` is explicit about this. Add an approval workflow (see #14).
**[CUSTOM + PLATFORM], M.**

> **Original question:** Versioning & access control. How are flow definitions versioned and rolled back, and who is authorized to change a sensitive flow (e.g. payment or collections)?

**14 — Versioning & access control.** Build a **versioned flow store** (git-backed or DB) with
rollback and **RBAC** on who edits sensitive flows, plus a staging/sign-off gate. Also **add
authentication to the FastAPI server** — today it has none and CORS `*` (`main.py`). **[PLATFORM +
add auth], M.**

> **Original question:** DTMF collection & deterministic flows. Can the system perform DTMF collection end-to-end — menu selection and multi-digit collection with length / terminator / timeout, validation, and retry — driven deterministically without an LLM? Who builds and owns that deterministic flow logic?

**15 — DTMF deterministic collection.** Switch the serializer to emit Pipecat's canonical
`InputDTMFFrame` and use `DTMFAggregator` (length/terminator/timeout handled) instead of today's
LLM-injected string. For fully LLM-free digit collection, add a small **deterministic collection
node/processor** (validate length, terminator, retry). Owned by this repo. **[ASSEMBLE+CUSTOM], S–M.**

> **Original question:** Classic IVR without an LLM (end-to-end). Can the system run a complete traditional IVR flow with no LLM anywhere — DTMF menus, bounded speech recognition (STT constrained to a fixed grammar / option set, not open NLU), and pre-recorded audio prompts (not TTS) — driven by deterministic flow logic? What in the stack provides the bounded-grammar STT and the pre-recorded prompt playback, and is the per-call footprint/cost acceptable for high-volume simple flows?

**16 — Classic IVR, no LLM anywhere.** Possible, but you're assembling a deterministic engine on top
of the media plane: pre-recorded **prompt playback** (file player / FreeSWITCH `playback`), the DTMF
collector from #15, and **bounded-grammar recognition** (Deepgram keyterm/keyword-constrained, or map
a fixed option set) instead of open NLU. Pipecat Flows can drive it with **speak-only nodes and
deterministic transitions (no LLM node)**. **[CUSTOM], M–L.** Honest flag: for pure high-volume simple
IVR, a purpose-built IVR engine may be cheaper to run than this stack — worth a make-vs-reuse check.

> **Original question:** Prompt-injection / policy adherence. Can a caller induce the agent to skip a required step, waive a fee, or mishandle data, and how is that prevented?

**17 — Prompt-injection / policy adherence.** Mostly hardening of what's there. Keep "code decides
money" (already the design), add **`FlowsFunctionSchema` hard constraints** (min/max amount, invoice
pattern) so limits are schema-enforced not prose, use `RESET_WITH_SUMMARY` context on the confirm
node, and optionally a lightweight guard/classifier turn. **[CONFIG/ASSEMBLE], S–M.**

> **Original question:** Dependency exposure. What is the exposure to breaking changes in Pipecat and to the pricing, latency, or deprecation of the LLM provider, and what effort would it take to move off either later?

**18 — Dependency exposure.** Pipecat is pinned (good). Add **`LLMSwitcher`** for provider failover
(cheap insurance against pricing/outage/deprecation) and keep the existing provider abstraction. The
residual risk is doing deliberate Pipecat upgrades — an ongoing ops cost, not a build. **[CONFIG/
ASSEMBLE], S.**

> **Original question:** What we build & operate ourselves. Given everything must be open-source and on-prem (the only external dependency being paid API keys to cloud-hosted LLMs), which components must we build and operate ourselves — call spawning, scaling, high-availability, tenancy, the front-door router — and what is the build-plus-operate cost?

**19 — What we build ourselves.** This is the roll-up of the others: build/operate = the front-door
router + admission control (#28), scaling/autoscale (#24), HA/anchoring (#10), multi-tenancy (#5/#7),
audit/PII/retention (#3/#4), exactly-once (#9), PCI boundary (#1), RBAC/flow store (#13/#14), and the
real API integrations (#20). Everything *inside* a call (flows, disclosures, DTMF, failover,
recording, evals) is configure/assemble. See totals below.

> **Original question:** Integration with existing infrastructure. How does the system integrate with our existing infrastructure and services (e.g. tenant-service, instance-registry, clicktopay)?

**20 — Integrate tenant-service / instance-registry / clicktopay.** Clean seams already exist:
implement the abstract `InvoiceApi`/`PaymentApi` against **clicktopay**, and a **tenant/instance
resolver** at call start against tenant-service/instance-registry. Each is a contained REST-client
job. **[CUSTOM], ~M per integration.**

> **Original question:** Deployment model. Do we deploy on bare metal or as containers on our Kubernetes, and what does a per-call-process model require of that choice?

**21 — Deployment model.** Both paths are supported; the AKS templates exist. The per-call-process
model just requires a **sticky long-lived socket + graceful drain** on scale-down (templates already
do `preStop` drain + long grace). Productionizing the templates (secrets, image pipeline, real smoke
test) is the work. **[CONFIG/PLATFORM], S–M.**

> **Original question:** Bare-metal dependencies. If bare metal, what must be installed on the host — Python runtime and app libraries, native audio/codec libraries, a SIP/WebRTC media stack or provider client, the provider SDKs, a process manager, and outbound access to the speech/model providers?

**22 — Bare-metal dependencies.** Known, installable list (Python + Pipecat extras, Silero/ONNX,
resamplers, FreeSWITCH + audio module, provider SDKs, a process manager, outbound egress). Package it
+ systemd units. **[CONFIG], S–M.**

> **Original question:** Runtime flow deployment. How is a new or changed flow deployed while the system is running, and does it require downtime? If flows are code, does a change require a full redeploy; if flows are to be updated without a redeploy, what loads flow definitions as data?

**23 — Runtime flow deployment.** Load Flows **graph JSON from a store at call start** so graph/prompt
changes go live without a redeploy; code (money-logic) changes still ship normally. Custom loader on
top of the static-flow support Pipecat already has. **[CUSTOM], S–M.**

> **Original question:** Horizontal scaling. How does the system scale out across machines? Given each call runs as its own process, how are concurrent calls distributed across processes/pods, and what is the scaling ceiling (provider rate limits, cost)?

**24 — Horizontal scaling.** Near-term: more bot pods behind the Service and **swap the placeholder
CPU HPA for KEDA scaling on active calls** — mostly config/deploy, **M**. Distributed option: adopt
Pipecat's **Redis/PGMQ bus + worker registry** to separate the telephony front-end from LLM workers —
more powerful, more moving parts, **[ASSEMBLE→CUSTOM], L**. Ceilings remain the media license +
provider quota (#12).

> **Original question:** Worker / LLM failure handling. What happens when the LLM provider is down, tokens are exhausted, the network drops, or the model misbehaves mid-call? Is the caller left in silence? Can the call escalate (to a human or a fallback)? Is the failure logged with enough detail to diagnose?

**25 — Worker/LLM failure handling.** Assemble Pipecat's resilience kit: **`LLMSwitcher`/
`ServiceSwitcher`** for live STT/LLM/TTS failover, the **missing-handler guardrail**, `enable_heartbeats`,
and an **escalation node** (fallback line or warm transfer) so the caller is never left silent. Warm
transfer audio is FreeSWITCH ESL work. **[ASSEMBLE], M.**

> **Original question:** Telephony-connection failure. If the telephony connection fails (e.g. FreeSWITCH down, network issue), are resources released cleanly, and can partial changes (e.g. a charge already made) be rolled back or compensated?

**26 — Telephony-connection failure.** Resource release is already clean (worker cancel + ESL
`uuid_kill`). The gap is **compensation for a partial charge** — a void/reconcile path tied to the
ledger in #9. **[CUSTOM], S–M.**

> **Original question:** Per-tenant/solution narration. How is it configured, per tenant and per solution, how a given step or requirement is worded to the caller, and how is exact wording guaranteed where it is required (cf. #2)?

**27 — Per-tenant/solution narration.** Make narration strings **per-tenant data** resolved at call
start (from #5/#7 config), and keep verbatim lines on the deterministic `tts_say`/pre-recorded path
(#2). Small once tenancy exists. **[CUSTOM], S–M.**

> **Original question:** Inbound call → worker routing. With one process per call across many pods/containers, how does the telephony layer know which worker to connect a call to? What component accepts the call, selects or starts a worker, connects the media to that specific worker, tracks the mapping for the call's lifetime, and handles "all workers busy"?

**28 — Inbound call → worker routing.** The big platform piece. Options: (a) **k8s Service L4 LB +
long-lived socket + KEDA**, plus a small **admission/gatekeeper** for "all busy" (queue or reject) —
mostly config with a modest custom piece, **M–L**; (b) a **front-door router using Pipecat's worker
registry + bus** (workers self-register, router dispatches) — more capable, **[ASSEMBLE→CUSTOM], L**;
(c) **SBC/FreeSWITCH-level routing with media anchoring** if calls must survive worker replacement —
**[PLATFORM], L–XL**. Pick per the HA bar you set in #10.

---

## Sequencing & rough program total

A pragmatic order (each phase shippable):

1. **Harden the single-tenant product** (config/assemble, ~3–5 wks): disclosures→pre-recorded (#2),
   DTMF canonicalization (#15), function-schema constraints + guard (#17), LLM/service failover +
   guardrail + heartbeats (#25/#18/#6), audit sink (#3), cost observer + capacity test (#11/#12),
   API auth (#14 part), real clicktopay/invoice integration (#20), idempotency + compensation (#9/#26).
2. **Operability & flows-as-data** (~2–4 wks): flow-from-store loader + runtime deploy (#23/#13
   graph), KEDA active-call autoscaling + productionized deploy (#24/#21/#22), redial-and-resume
   (#10 tier-1, #8).
3. **Multi-tenancy** (~3–6 wks): tenant resolver + per-tenant secrets/config/limits + narration
   (#5 model-a → #7/#27), tenant/instance-registry integration (#20), PII/residency controls (#4).
4. **Regulated-scale platform** (largest, staged): front-door router + admission control (#28), PCI
   capture boundary + QSA evidence (#1), RBAC + versioned flow store + approvals (#14/#13 full), and —
   only if required — HA media anchoring (#10 tier-2) and the distributed bus (#24 distributed).

Order-of-magnitude engineering total (excludes QSA certification, procurement, security review, and
sustained load testing): **Phases 1–3 ≈ 2–4 engineer-months**; **Phase 4 ≈ 3–6+ engineer-months**,
dominated by routing/HA/PCI. So: **the voice product itself is mostly configuration and assembly and
is a few weeks of work; the regulated multi-tenant *platform* around it is the multi-month part**, and
that part is largely outside what Pipecat provides.
