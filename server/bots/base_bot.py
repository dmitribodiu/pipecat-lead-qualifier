"""Base bot framework for shared functionality (Pipecat 1.x).

Migrated from the 0.0.x API to Pipecat 1.5.0:

- ``OpenAILLMContext`` + ``create_context_aggregator`` -> ``LLMContext`` +
  ``LLMContextAggregatorPair`` (universal aggregators).
- Endpointing: the hand-rolled dual-LLM ``smart_endpointing`` pipeline is replaced
  by Pipecat's built-in turn detection. Providing a ``vad_analyzer`` on the user
  aggregator enables VAD; the default user-turn stop strategy is
  ``TurnAnalyzerUserTurnStopStrategy(LocalSmartTurnAnalyzerV3())`` (smart turn).
- Runtime: ``PipelineTask`` + ``PipelineRunner`` -> ``PipelineWorker`` +
  ``WorkerRunner``.
- Transport (Phase 1): ``DailyTransport`` from ``pipecat.transports.daily.transport``.
- STT muting: ``STTMuteFilter`` -> ``user_mute_strategies`` on the user aggregator.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict

from loguru import logger

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.daily.transport import DailyTransport, DailyParams
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.turns.user_mute.function_call_user_mute_strategy import (
    FunctionCallUserMuteStrategy,
)

from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.rime.tts import RimeHttpTTSService
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.llm import OpenAILLMService


class BaseBot(ABC):
    """Abstract base class for bot implementations."""

    def __init__(self, config, system_messages: Optional[List[Dict[str, str]]] = None):
        """Initialize bot with core services and pipeline components.

        Args:
            config: Application configuration.
            system_messages: Optional initial system messages for the LLM context.
        """
        self.config = config

        # Initialize STT service (Deepgram nova-3 is a standard model in 1.x).
        self.stt = DeepgramSTTService(
            api_key=config.deepgram_api_key, model="nova-3-general"
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

        # Optional STT muting (1.x: strategies live on the user aggregator, not a filter).
        user_mute_strategies = (
            [MuteUntilFirstBotCompleteUserMuteStrategy(), FunctionCallUserMuteStrategy()]
            if config.enable_stt_mute_filter
            else []
        )

        # Initialize context + universal aggregator pair.
        # VAD on the user aggregator enables turn detection; the default user-turn
        # stop strategy is built-in smart turn (LocalSmartTurnAnalyzerV3).
        self.context = LLMContext(messages=system_messages or [])
        self.context_aggregator = LLMContextAggregatorPair(
            self.context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=0.2)),
                user_mute_strategies=user_mute_strategies,
            ),
        )

        logger.debug(f"Initialised bot with config: {config}")

        # Daily transport parameters (Phase 1). VAD is no longer a transport concern
        # in 1.x; it lives on the user aggregator above.
        self.transport_params = DailyParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
        )

        # These will be set up when needed.
        self.transport: Optional[DailyTransport] = None
        self.worker: Optional[PipelineWorker] = None
        self.runner: Optional[WorkerRunner] = None

    async def setup_transport(self, url: str, token: str):
        """Set up the Daily transport with the given room URL and token."""
        self.transport = DailyTransport(
            url, token, self.config.bot_name, params=self.transport_params
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

        self.worker = PipelineWorker(
            pipeline,
            params=PipelineParams(
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
        )
        self.runner = WorkerRunner(handle_sigint=False)

    async def start(self):
        """Start the bot's main worker."""
        if not self.runner or not self.worker:
            raise RuntimeError("Bot not properly initialized. Call create_pipeline first.")
        await self.runner.add_workers(self.worker)
        await self.runner.run()

    async def cleanup(self):
        """Clean up resources."""
        if self.worker:
            await self.worker.cancel()
        if self.transport:
            await self.transport.close()

    @abstractmethod
    async def _handle_first_participant(self):
        """Override in subclass to handle the first participant joining."""
        pass
