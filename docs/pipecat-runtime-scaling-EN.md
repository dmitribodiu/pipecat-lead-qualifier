# Pipecat: execution model, call lifecycle and scaling

*How it works per process, what the runner is, the async loop, a FreeSWITCH call top-to-bottom, scaling to 3000 concurrent calls, and a comparison with ASP.NET Core*

## 1. Execution model: one process, one event loop

**The core model is “one call = one bot instance”, running inside a single Python process on a single asyncio event loop.**

In the primary (WebSocket/FreeSWITCH) mode the server is a single FastAPI application under uvicorn. Each incoming call opens its own WebSocket connection to the /audio endpoint. For each connection a bot object, a transport and a pipeline are created — but they all live **in the same process and the same event loop** as the web server itself. There is no per-call process isolation in this mode.

There is a second mode — Daily (WebRTC/browser). There main.py does not run the bot in its own process but spawns a **separate child process** via runner.py per call. That gives isolation but costs more memory and startup time. The multiagent bot is currently wired only to the WebSocket path (it is not branched in runner.py — see section 6).

## 2. What gets created: per process and per call

**Per process (once, at startup):** the uvicorn process, one event loop, the FastAPI app, a background cleanup task, and for Daily a DailyRESTHelper.

**Per call (WebSocket path):**

|                                                 |                                                                                     |
| ----------------------------------------------- | ----------------------------------------------------------------------------------- |
| **Component**                                   | **Purpose**                                                                         |
| BotConfig                                       | Reads environment variables (LLM/TTS/transport, flags)                              |
| Bot (e.g. MultiAgentPaymentBot)                 | The call object; holds transport, services, pipeline, FlowManager                   |
| FastAPIWebsocketTransport + AudioForkSerializer | Audio in/out over WebSocket, 8 kHz codec (telephony)                                |
| STT / LLM / TTS services                        | Deepgram (recognition), Anthropic/Google (LLM), Deepgram/Cartesia/… (synthesis)     |
| Pipeline                                        | Linear processor chain: input → STT → user-agg → LLM → TTS → output → assistant-agg |
| PipelineWorker                                  | The async “engine” that pumps frames through the pipeline                           |
| WorkerRunner                                    | Lifecycle orchestrator for the workers (start/stop)                                 |
| FlowManager (multiagent)                        | Dialogue state machine; holds per-call state, including the tenant config           |

All of this is ordinary Python objects on the process heap. The three network streams (STT, LLM, TTS) per call are external connections to providers, not local CPU load.

## 3. What “runner” means (two different things)

**1) pipecat WorkerRunner** (pipecat.workers.runner) is an orchestrator of asyncio workers inside the process. It registers “root” workers (the main one being PipelineWorker, optionally the Whisker debugger), runs them as background asyncio tasks, and finishes once all root workers are done (auto\_end). It is runner.run() that keeps the call “alive” until disconnect.

**2) The project's runner.py** is a CLI script in this repository. It is used **only for the Daily transport**: main.py uses subprocess.Popen to launch runner.py as a separate process per call, which brings up the Daily transport and calls the same create\_pipeline() and start(). On the WebSocket/FreeSWITCH path this script is not used — the bot runs inside the web-server process.

**Don't confuse them:** the pipecat WorkerRunner is about asyncio inside one call; the project's runner.py is about launching a separate OS process per call (Daily).

## 4. The async loop in plain terms

The asyncio event loop is a **single-threaded cooperative scheduler**. In one thread it runs many coroutines in turn; when a coroutine “waits” on I/O (await to Deepgram/Anthropic/TTS) it yields control, and the loop advances other calls meanwhile. So one thread serves **many concurrent I/O-bound calls**.

PipelineWorker internally is a set of asyncio tasks: frames (audio, transcript, function calls, TTS text) are placed on an asyncio.Queue and pumped through the pipeline processors. All frame movement for a call is coroutines on the shared loop.

**The key constraint:** the loop is single-threaded and subject to the GIL. Any **blocking or heavy CPU operation** (e.g. synchronous VAD/Silero, audio resampling, compression) runs in this same thread and **stalls every call in the process** until it finishes. Rule: no blocking code in the loop — only awaits on I/O and light computation. Heavy work must be offloaded (to separate processes/services or a thread/process pool).

## 5. FreeSWITCH call lifecycle (top to bottom)

**1. Dialplan / mod\_audio\_fork —** On dial, FreeSWITCH runs uuid\_audio\_fork start ws://\<host\>:7860/audio?uuid=…\&did=… — opening a WebSocket and streaming audio (mono, 8000 Hz).

**2. uvicorn accepts the upgrade —** The HTTP→WebSocket upgrade is routed to the audio\_websocket() handler (main.py).

**3. accept + read metadata —** websocket.accept(); uuid (for ESL hangup) and did (for tenant selection) are read from query params.

**4. Create the bot —** BotConfig() reads env; BOT\_TYPE selects the class (MultiAgentPaymentBot); bot.call\_did = did.

**5. Transport —** bot.setup\_websocket\_transport(ws): FastAPIWebsocketTransport + AudioForkSerializer (8 kHz) are created; on\_client\_connected / on\_client\_disconnected handlers are attached.

**6. Build the pipeline —** bot.create\_pipeline(): Pipeline(\[input, STT, user-agg, LLM, TTS, output, assistant-agg\]) + observers (latency, tracing) → PipelineWorker(params) + WorkerRunner.

**7. Start —** bot.start(): runner.add\_workers(worker); await runner.run() — this coroutine keeps the call alive to the end.

**8. First participant → config —** on\_client\_connected → \_handle\_first\_participant(): config\_api.load\_for\_call(did) resolves the tenant and loads its config → seeds flow\_manager.state\['tenant'\] → FlowManager.initialize(greeting).

**9. Conversation flow —** Audio: FreeSWITCH → WS bytes → AudioForkSerializer → input → STT (Deepgram, network) → turn aggregator → LLM (Anthropic, network; function calls → FlowManager transitions) → TTS (network) → output → WS bytes → FreeSWITCH → caller. All of it a stream of frames through a queue on one loop.

**10. Teardown —** Disconnect → on\_client\_disconnected → \_shutdown\_workers() → runner.run() returns → finally in main.py: ESL uuid\_kill if needed (drop the channel), then cleanup().

## 6. Production, or development-only?

pipecat itself is a **production-grade framework** (real voice products run on it). But “production readiness” is a property of the **deployment and configuration**, not the framework alone. As it stands this repository is closer to a pilot/PoC; things to address before production:

  - **In-process model (WebSocket):** all calls share one process and one event loop. One call with blocking code or a panic affects its neighbours. No fault isolation.

  - **Single uvicorn process by default:** one core is used. Multiple processes/workers are needed for multi-core.

  - **CPU in the loop:** Silero VAD and resampling are CPU work on the shared thread. Under high concurrency this adds latency to everyone.

  - **No admission control on /audio:** unlike /connect (which has max\_bots\_per\_room), the WebSocket entry has no cap — the process will accept more calls than it can handle.

  - **CORS allow\_origins=\['\*'\], Silero download, no provider circuit breakers, secrets in .env:** all need hardening for production.

**Bottom line:** the framework is fit for production, but you must add process-based scaling, admission limits, offloaded/controlled CPU work, observability and resilience to provider failures (see section 7).

## 7. Scaling to 3000 concurrent calls

### 7.1 Per-call resource profile

A call is mostly **I/O-bound**: three persistent network streams (STT, LLM, TTS) to external providers. CPU share: VAD/turn detection, audio (de)serialization and resampling. Memory: LLM context and audio buffers. Media traffic: \~128 kbps per call each way (8 kHz, 16-bit, mono).

### 7.2 Why you can't just “add more calls to one process”

One event loop effectively uses one core (GIL). I/O waits scale well, but the total CPU work (VAD, resampling) in one process serializes and, at peak, adds latency to **all** calls. So scaling is **horizontal: processes × cores × pods**, not “more coroutines in one process”.

### 7.3 Practical strategy

  - **Processes = cores:** N uvicorn processes (typically ≈ vCPU count per node), each serving a fixed limit of K calls.

  - **Admission control:** a hard cap on /audio (process/pod capacity); on overflow reject/route elsewhere rather than degrade everyone.

  - **Kubernetes:** pod = process(es) with a fixed capacity; horizontal autoscaling on active calls/CPU; graceful drain (finish in-flight calls before stopping).

  - **WebSocket balancing:** a call is a long-lived connection, so you need a sticky L4/L7 LB with WS support; FreeSWITCH points the fork at the LB.

  - **Reduce CPU in the loop:** for 8 kHz telephony smart-turn is already off (a plus); minimize resampling; offload heavy work (if any) to separate services/pools.

  - **External limits — often the real ceiling:** Deepgram/Anthropic/TTS quotas for 3000 parallel streams; you'll need enterprise contracts, rate-limit headroom, possibly multiple providers/regions.

### 7.4 Rough estimate (order of magnitude)

|                          |                                                                                   |
| ------------------------ | --------------------------------------------------------------------------------- |
| **Parameter**            | **Estimate / comment**                                                            |
| Calls per process (K)    | \~50–150 I/O-bound calls; depends on VAD/resampling CPU share and provider limits |
| Processes for 3000 calls | \~3000 / K ≈ 20–60 processes                                                      |
| Nodes/pods               | spread processes across nodes ≈ vCPU count; keep 20–30% headroom                  |
| Media traffic            | 3000 × \~128 kbps × 2 (in+out) ≈ 0.7–0.8 Gbps total                               |
| Main ceilings            | 1\) VAD CPU share in the loop; 2) STT/LLM/TTS rate limits; 3) memory per call     |

*These numbers are order-of-magnitude guidance, not a guarantee; measure real K and ceilings with a load test on target hardware and real provider quotas.*

## 8. Comparison with ASP.NET Core (scaling perspective)

|                        |                                                    |                                                        |
| ---------------------- | -------------------------------------------------- | ------------------------------------------------------ |
| **Aspect**             | **Pipecat (Python asyncio)**                       | **ASP.NET Core (.NET)**                                |
| Concurrency            | One thread + one event loop per process            | Multi-threaded thread pool + async/await over Kestrel  |
| Multi-core             | Only via multiple processes (GIL)                  | One process uses all cores                             |
| CPU-bound code         | Blocks the whole process — must not block the loop | Takes a pool thread; runtime parallelizes across cores |
| Long-lived connections | Cheaply holds many I/O connections on the loop     | Also cheap (async I/O, SignalR/WebSockets)             |
| Unit of scale          | Process-per-core × pods; sticky WS                 | Pod/instance (already multi-core inside) × pods        |
| Load tooling maturity  | Watch event-loop lag manually                      | Rich profiling/diagnostics out of the box              |

### 8.1 The key difference

In .NET a single process scales **vertically across cores** (no GIL, a thread pool), so one pod already utilizes a multi-core node. In Python/pipecat one process is effectively one core, and scale comes from the **number of processes** (uvicorn/gunicorn workers) = core count, plus pods. Architecturally both look similar in Kubernetes (async, long-lived connections, horizontal autoscaling), but the model “inside the pod” differs.

### 8.2 Pitfalls

  - **Python:** blocking/CPU code in the loop stalls the whole process (in .NET a block takes just one pool thread). For realtime voice this is critical — Silero VAD, synchronous HTTP clients, resampling.

  - **Memory:** per-call memory tends to be higher in Python; the subprocess model (Daily) is especially costly — thousands of processes is not an option.

  - **Deployment:** Python requires an explicit “processes = cores” layer and sticky WS balancing; in .NET it's enough to scale instances.

  - **Shared ceiling for both:** for realtime voice the main limit is often not the web tier but the external STT/LLM/TTS (latency and quotas) and the VAD CPU share — here the platform (Python vs .NET) is secondary.

## 9. Summary and recommendations

  - Model: one process, one event loop; on the WebSocket path all calls are in-process (no isolation), on Daily it's a process per call.

  - “runner” is either the pipecat WorkerRunner (asyncio within a call) or the project's runner.py (an OS process per call, Daily only).

  - The async loop gives cheap concurrency for I/O but demands the discipline of “nothing blocks the loop”.

  - For 3000 calls: scale by processes × pods, admission control on /audio, sticky WS LB, control the VAD CPU share, and secure provider quota headroom.

  - vs ASP.NET Core: .NET utilizes cores within a process; Python needs a process-per-core; otherwise horizontal scale in k8s is similar, and the real ceiling for realtime voice is the external services and VAD.
