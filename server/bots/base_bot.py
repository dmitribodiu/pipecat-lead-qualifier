"""Base bot framework for shared functionality (Pipecat 1.x).

Supports two transports, selected by ``config.transport``:

- ``websocket`` (default): FreeSWITCH via ``mod_audio_fork`` over a FastAPI
  WebSocket (``FastAPIWebsocketTransport`` + ``AudioForkSerializer``), 8 kHz
  telephony audio. Runs natively on Windows (no ``daily-python``). Endpointing is
  VAD-only (``SpeechTimeoutUserTurnStopStrategy``) — the smart-turn v3 model expects
  16 kHz and mis-detects turn ends on 8 kHz telephony audio.
- ``daily``: Daily WebRTC (``DailyTransport``), 16 kHz, with the built-in smart-turn
  analyzer (the 1.5.0 default). ``daily-python`` has no Windows wheels, so this path
  is Linux/WSL only; the import is done lazily so it never blocks the WebSocket path.

Common pipeline (both transports):
    transport.input() -> STT -> user aggregator -> LLM -> TTS -> transport.output()
                      -> assistant aggregator
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.observers.user_bot_latency_observer import UserBotLatencyObserver
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.turns.user_turn_completion_mixin import (
    USER_TURN_COMPLETION_INSTRUCTIONS,
    UserTurnCompletionConfig,
)
from pipecat.turns.user_turn_strategies import (
    FilterIncompleteUserTurnStrategies,
    UserTurnStrategies,
)
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_mute.function_call_user_mute_strategy import (
    FunctionCallUserMuteStrategy,
)
from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketTransport,
    FastAPIWebsocketParams,
)

from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.rime.tts import RimeHttpTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService

from serializers.audio_fork import AudioForkSerializer


class BaseBot(ABC):
    """Abstract base class for bot implementations."""

    def __init__(self, config, system_messages: Optional[List[Dict[str, str]]] = None):
        """Initialize bot with core services and pipeline components.

        Args:
            config: Application configuration.
            system_messages: Optional initial system messages for the LLM context.
        """
        self.config = config

        # Audio sample rate: 8 kHz for telephony (WebSocket/FreeSWITCH), 16 kHz for Daily.
        self.sample_rate = config.ws_sample_rate if config.transport == "websocket" else 16000

        # Initialize STT service (Deepgram nova-3 is a standard model in 1.x).
        self.stt = DeepgramSTTService(
            api_key=config.deepgram_api_key,
            model="nova-3-general",
            sample_rate=self.sample_rate,
        )

        # Initialize TTS service
        match config.tts_provider:
            case "elevenlabs":
                if not config.elevenlabs_api_key:
                    raise ValueError("ElevenLabs API key is required for ElevenLabs TTS")

                self.tts = ElevenLabsTTSService(
                    api_key=config.elevenlabs_api_key,
                    voice_id=config.elevenlabs_voice_id,
                )
            case "cartesia":
                if not config.cartesia_api_key:
                    raise ValueError("Cartesia API key is required for Cartesia TTS")

                self.tts = CartesiaTTSService(
                    api_key=config.cartesia_api_key, voice_id=config.cartesia_voice
                )
            case "deepgram":
                if not config.deepgram_api_key:
                    raise ValueError("Deepgram API key is required for Deepgram TTS")

                self.tts = DeepgramTTSService(
                    api_key=config.deepgram_api_key, voice=config.deepgram_voice
                )
            case "rime":
                if not config.rime_api_key:
                    raise ValueError("Rime API key is required for Rime TTS")

                self.tts = RimeHttpTTSService(
                    api_key=config.rime_api_key,
                    voice_id=config.rime_voice_id,
                    params=RimeHttpTTSService.InputParams(
                        reduce_latency=config.rime_reduce_latency,
                        speed_alpha=config.rime_speed_alpha,
                    ),
                )
            case _:
                raise ValueError(f"Invalid TTS provider: {config.tts_provider}")

        # Initialize LLM service
        match config.llm_provider:
            case "google":
                if not config.google_api_key:
                    raise ValueError("Google API key is required for Google LLM")

                self.llm = GoogleLLMService(
                    api_key=config.google_api_key,
                    model=config.google_model,
                    params=config.google_params,
                )

            case "openai":
                if not config.openai_api_key:
                    raise ValueError("OpenAI API key is required for OpenAI LLM")

                self.llm = OpenAILLMService(
                    api_key=config.openai_api_key,
                    model=config.openai_model,
                    params=config.openai_params,
                )

            case _:
                raise ValueError(f"Invalid LLM provider: {config.llm_provider}")

        # STT muting (1.x: strategies live on the user aggregator, not a filter).
        user_mute_strategies = []
        if config.transport == "websocket" and config.enable_echo_mute:
            # Optional: mute the caller while the bot speaks so the bot's own TTS echoing
            # back through the phone can't trip VAD and interrupt it. This DISABLES barge-in,
            # so it's off by default (set ECHO_MUTE=true to enable). Prefer real echo
            # cancellation if you need both no-self-interrupt and barge-in.
            user_mute_strategies.append(AlwaysUserMuteStrategy())
        if config.enable_stt_mute_filter:
            user_mute_strategies += [
                MuteUntilFirstBotCompleteUserMuteStrategy(),
                FunctionCallUserMuteStrategy(),
            ]

        # Endpointing depends on the audio rate. Telephony (8 kHz) uses VAD-only turn
        # detection; the smart-turn v3 model expects 16 kHz and holds turns open on 8 kHz
        # audio. Daily (16 kHz) keeps the built-in smart-turn analyzer (1.5.0 default).
        if config.transport == "websocket":
            # Smart endpointing for 8 kHz telephony (smart-turn v3 needs 16 kHz audio, so
            # it can't be used here). The silence detector (VAD 0.2s + speech-timeout 0.2s)
            # only TRIGGERS an LLM inference; the LLM starts its reply with a completion
            # marker and only a COMPLETE user thought finalizes the turn. Incomplete
            # utterances ("my invoice number is...") keep the turn open instead of the
            # bot talking over the caller; trail-offs get re-prompted at 5s/10s.
            # NOTE: the detector list must be passed explicitly — the container's default
            # stop strategy is smart-turn v3, which mis-detects on telephony audio.
            # Bots can append domain rules to the completeness judgment (e.g. "fewer
            # digits than an invoice number needs = the caller is still dictating") by
            # defining a TURN_COMPLETION_GUIDANCE class attribute.
            vad_stop_secs = 0.2
            guidance = getattr(self, "TURN_COMPLETION_GUIDANCE", "")
            self.turn_completion_config = (
                UserTurnCompletionConfig(
                    instructions=USER_TURN_COMPLETION_INSTRUCTIONS + guidance
                )
                if guidance
                else None
            )
            user_turn_strategies = FilterIncompleteUserTurnStrategies(
                config=self.turn_completion_config,
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.2)],
            )
        else:
            vad_stop_secs = 0.2
            user_turn_strategies = None  # None -> default (smart-turn v3)

        # Initialize context + universal aggregator pair. Subclasses may define
        # IDLE_TIMEOUT_S (seconds of caller silence after the bot stops speaking that
        # fires on_user_turn_idle); 0 disables idle detection.
        self.context = LLMContext(messages=system_messages or [])
        self.context_aggregator = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=vad_stop_secs)),
                user_mute_strategies=user_mute_strategies,
                user_turn_strategies=user_turn_strategies,
                user_idle_timeout=float(getattr(self, "IDLE_TIMEOUT_S", 0.0)),
            ),
        )

        logger.debug(f"Initialised bot with config: {config}")

        # WebSocket/FreeSWITCH transport params (VAD lives on the aggregator in 1.x).
        self.ws_params = FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # Output audio buffering. Each playAudio message carries this many 10ms
            # chunks; larger = more cushion for mod_audio_fork against host<->container
            # WebSocket jitter (fewer playback under-runs / less choppiness), at the cost
            # of a little more latency. 4 = 40ms. Raise to 6-8 if still choppy; lower
            # toward 2 to trim latency.
            audio_out_10ms_chunks=4,
            serializer=AudioForkSerializer(self.sample_rate),
        )

        # Populated by a setup_*_transport call.
        self.transport = None
        self.worker: Optional[PipelineWorker] = None
        self.runner: Optional[WorkerRunner] = None

    async def setup_websocket_transport(self, websocket):
        """Set up the FastAPI WebSocket transport for a FreeSWITCH mod_audio_fork call."""
        self.transport = FastAPIWebsocketTransport(websocket=websocket, params=self.ws_params)

        @self.transport.event_handler("on_client_connected")
        async def on_client_connected(transport, client):
            await self._handle_first_participant()

        @self.transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport, client):
            if self.worker:
                await self.worker.cancel()

    async def setup_daily_transport(self, url: str, token: str):
        """Set up the Daily transport (Linux/WSL only — daily-python has no Windows wheel)."""
        # Imported lazily so the WebSocket/native-Windows path never requires daily-python.
        from pipecat.transports.daily.transport import DailyTransport, DailyParams

        self.transport = DailyTransport(
            url,
            token,
            self.config.bot_name,
            params=DailyParams(audio_in_enabled=True, audio_out_enabled=True),
        )

        @self.transport.event_handler("on_participant_left")
        async def on_participant_left(transport, participant, reason):
            if self.worker:
                await self.worker.cancel()

        @self.transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            await self._handle_first_participant()

    def create_pipeline(self):
        """Create the processing pipeline (linear 1.x shape)."""
        if not self.transport:
            raise RuntimeError("Transport must be set up before creating pipeline")

        pipeline = Pipeline(
            [
                self.transport.input(),
                self.stt,  # Deepgram transcribes incoming audio
                self.context_aggregator.user(),
                self.llm,
                self.tts,
                self.transport.output(),
                self.context_aggregator.assistant(),
            ]
        )

        # Voice-to-voice latency: time from the caller finishing speaking to the bot
        # starting to speak, logged per turn with a per-service (STT/LLM/TTS) breakdown.
        latency_observer = UserBotLatencyObserver()

        observers = [latency_observer]

        # Optional per-call frame trace (TRACE_CALLS=1). Records every frame push to a
        # JSONL file under server/traces/ for post-call inspection (convert to a Perfetto
        # timeline with tools/trace_to_perfetto.py). Exposed on self so subclasses can
        # inject custom markers (e.g. a context dump after a flow node change).
        self.trace_observer = None
        if getattr(self.config, "trace_calls", False):
            from observers.frame_trace import FrameTraceObserver

            self.trace_observer = FrameTraceObserver(
                include_audio=getattr(self.config, "trace_audio", False)
            )
            observers.append(self.trace_observer)

        @latency_observer.event_handler("on_latency_measured")
        async def _on_latency(_observer, latency: float):
            logger.info(f"Voice-to-voice latency: {latency:.2f}s (user stopped -> bot speaking)")

        @latency_observer.event_handler("on_latency_breakdown")
        async def _on_latency_breakdown(_observer, breakdown):
            logger.info(f"Latency breakdown: {breakdown}")

        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                audio_in_sample_rate=self.sample_rate,
                audio_out_sample_rate=self.sample_rate,
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=observers,
        )
        self.runner = WorkerRunner(handle_sigint=False)

    def trace_flow_nodes(self, flow_manager):
        """When call tracing is on, dump a compact context snapshot on every flow node
        change (a ``mark:set_node`` line in the trace).

        On every ``_set_node`` (initialize, transitions, ``set_node_from_config`` all
        funnel through it), captures the context right after the node is applied — answers
        "do the new node's ``task_messages`` actually land in the context the LLM sees?"
        No-op if tracing is off, so bots can call it unconditionally.
        """
        if flow_manager is None or not self.trace_observer:
            return

        original = flow_manager._set_node

        async def _traced_set_node(node_id, node_config, *args, **kwargs):
            result = await original(node_id, node_config, *args, **kwargs)
            try:
                msgs = self.context.messages
                snapshot = [
                    {"role": getattr(m, "role", None)
                     or (m.get("role") if isinstance(m, dict) else None),
                     "preview": repr(m)[:160]}
                    for m in msgs
                ]
                self.trace_observer.mark(
                    "set_node", note=node_id, count=len(msgs), context=snapshot
                )
            except Exception as e:
                logger.debug(f"trace_flow_nodes mark failed: {e}")
            return result

        flow_manager._set_node = _traced_set_node

    def set_turn_completion_rules(self, rules: str):
        """Swap the domain-specific part of the turn-completeness instructions.

        Called on node transitions so the completeness judge only carries the rules
        relevant to what the current node is asking (instead of one global blob).
        """
        if getattr(self, "turn_completion_config", None) is None:
            return
        self.turn_completion_config.instructions = USER_TURN_COMPLETION_INSTRUCTIONS + rules
        # The LLM service composes its system instruction from this config; rebuild it
        # so the new rules take effect on the next inference.
        if hasattr(self.llm, "_compose_system_instruction"):
            self.llm._compose_system_instruction()

    async def start(self):
        """Start the bot's main worker."""
        if not self.runner or not self.worker:
            raise RuntimeError("Bot not properly initialized. Call create_pipeline first.")
        await self.runner.add_workers(self.worker)
        await self.runner.run()

    async def cleanup(self):
        """Clean up resources.

        The worker owns the transport in 1.x; cancelling it stops the pipeline and
        closes the underlying connection. (There is no transport.close() to call.)
        """
        if self.worker:
            try:
                await self.worker.cancel()
            except Exception as e:
                logger.debug(f"cleanup: worker.cancel() raised (already stopped?): {e}")

    @abstractmethod
    async def _handle_first_participant(self):
        """Override in subclass to handle the first participant joining."""
        pass
