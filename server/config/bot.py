"""Bot configuration management module."""

import os
from typing import TypedDict, Literal, NotRequired
from dotenv import load_dotenv
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.openai.base_llm import BaseOpenAILLMService


class DailyConfig(TypedDict):
    api_key: str
    api_url: str
    room_url: NotRequired[str]


BotType = Literal["simple", "flow", "payment", "multiagent"]


class BotConfig:
    def __init__(self):
        load_dotenv()

        # Validate required vars. Deepgram (STT) is always required; the Daily key is
        # only needed for the Daily transport; the LLM key depends on the provider.
        required = {
            "DEEPGRAM_API_KEY": os.getenv("DEEPGRAM_API_KEY"),
        }
        if os.getenv("TRANSPORT", "websocket").lower() == "daily":
            required["DAILY_API_KEY"] = os.getenv("DAILY_API_KEY")
        if os.getenv("LLM_PROVIDER", "google").lower() == "openai":
            required["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
        else:
            required["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY")

        missing = [k for k, v in required.items() if not v]
        if missing:
            raise ValueError(f"Missing required env vars: {', '.join(missing)}")

        self.daily: DailyConfig = {
            "api_key": os.getenv("DAILY_API_KEY", ""),
            "api_url": os.getenv("DAILY_API_URL", "https://api.daily.co/v1"),
        }

        # Bot configuration
        self._bot_type: BotType = os.getenv("BOT_TYPE", "flow")
        if self._bot_type not in ("simple", "flow", "payment", "multiagent"):
            self._bot_type = "flow"  # Default to flow bot if invalid value

    def __repr__(self) -> str:
        return f"BotConfig(bot_type={self.bot_type}, bot_name={self.bot_name}, llm_provider={self.llm_provider}, google_model={self.google_model}, google_params={self.google_params}, openai_model={self.openai_model}, openai_params={self.openai_params}, tts_provider={self.tts_provider}, deepgram_voice={self.deepgram_voice}, cartesia_voice={self.cartesia_voice}, elevenlabs_voice_id={self.elevenlabs_voice_id}, rime_voice_id={self.rime_voice_id}, rime_reduce_latency={self.rime_reduce_latency}, rime_speed_alpha={self.rime_speed_alpha}, enable_stt_mute_filter={self.enable_stt_mute_filter}, classifier_model={self.classifier_model})"

    def _is_truthy(self, value: str) -> bool:
        return value.lower() in (
            "true",
            "1",
            "t",
            "yes",
            "y",
            "on",
            "enable",
            "enabled",
        )

    ###########################################################################
    # API keys
    ###########################################################################

    @property
    def google_api_key(self) -> str:
        return os.getenv("GOOGLE_API_KEY")

    @property
    def openai_api_key(self) -> str:
        return os.getenv("OPENAI_API_KEY")

    @property
    def deepgram_api_key(self) -> str:
        return os.getenv("DEEPGRAM_API_KEY")

    @property
    def cartesia_api_key(self) -> str:
        return os.getenv("CARTESIA_API_KEY")

    @property
    def elevenlabs_api_key(self) -> str:
        return os.getenv("ELEVENLABS_API_KEY")

    @property
    def rime_api_key(self) -> str:
        return os.getenv("RIME_API_KEY")

    ###########################################################################
    # Bot configuration
    ###########################################################################

    @property
    def bot_type(self) -> BotType:
        return self._bot_type

    @bot_type.setter
    def bot_type(self, value: BotType):
        self._bot_type = value
        os.environ["BOT_TYPE"] = value

    @property
    def transport(self) -> str:
        """Transport backend: 'websocket' (FreeSWITCH/mod_audio_fork) or 'daily'."""
        value = os.getenv("TRANSPORT", "websocket").lower()
        return value if value in ("websocket", "daily") else "websocket"

    @property
    def ws_sample_rate(self) -> int:
        """PCM sample rate for the WebSocket/FreeSWITCH transport (telephony = 8000 Hz)."""
        return int(os.getenv("WS_SAMPLE_RATE", "8000"))

    @property
    def enable_echo_mute(self) -> bool:
        """Mute the caller while the bot speaks (AlwaysUserMuteStrategy). Prevents the bot
        interrupting itself on phone echo, but DISABLES barge-in. Default off (barge-in on)."""
        return self._is_truthy(os.getenv("ECHO_MUTE", "false"))

    @property
    def trace_calls(self) -> bool:
        """Write a per-call frame trace (JSONL) via FrameTraceObserver. Off by default;
        set TRACE_CALLS=1 to record. Set TRACE_AUDIO=1 to also include raw audio frames."""
        return self._is_truthy(os.getenv("TRACE_CALLS", "false"))

    @property
    def trace_audio(self) -> bool:
        """Include raw audio frames in the call trace (very high frequency). Default off."""
        return self._is_truthy(os.getenv("TRACE_AUDIO", "false"))

    @property
    def enable_whisker(self) -> bool:
        """Attach the Whisker debugger: a WhiskerServer (WebSocket, default :9090) that the
        Whisker web UI connects to for live frame inspection. Transport-independent, so it
        works with FreeSWITCH calls. Off by default; set WHISKER=1 to enable."""
        return self._is_truthy(os.getenv("WHISKER", "false"))

    @property
    def whisker_port(self) -> int:
        """Port the Whisker WebSocket server binds (the UI connects here)."""
        return int(os.getenv("WHISKER_PORT", "9090"))

    @property
    def bot_name(self) -> str:
        return os.getenv("BOT_NAME", "Marissa")

    @bot_name.setter
    def bot_name(self, value: str):
        os.environ["BOT_NAME"] = value

    @property
    def llm_provider(self) -> str:
        return os.getenv("LLM_PROVIDER", "google").lower()

    @llm_provider.setter
    def llm_provider(self, value: str):
        value = value.lower()
        if value not in ("google", "openai"):
            raise ValueError(f"Invalid LLM provider: {value}")

        os.environ["LLM_PROVIDER"] = value

    @property
    def google_model(self) -> str:
        """Model used for conversation."""
        return os.getenv("GOOGLE_MODEL", "gemini-2.0-flash-001")

    @google_model.setter
    def google_model(self, value: str):
        os.environ["GOOGLE_MODEL"] = value

    @property
    def google_params(self) -> GoogleLLMService.InputParams:
        temperature = os.getenv("GOOGLE_TEMPERATURE", 1.0)
        return GoogleLLMService.InputParams(temperature=temperature)

    @google_params.setter
    def google_params(self, value: GoogleLLMService.InputParams):
        os.environ["GOOGLE_TEMPERATURE"] = str(value.temperature)

    @property
    def openai_model(self) -> str:
        return os.getenv("OPENAI_MODEL", "gpt-4o")

    @openai_model.setter
    def openai_model(self, value: str):
        os.environ["OPENAI_MODEL"] = value

    @property
    def openai_params(self) -> BaseOpenAILLMService.InputParams:
        temperature = os.getenv("OPENAI_TEMPERATURE", 0.2)
        return BaseOpenAILLMService.InputParams(temperature=temperature)

    @openai_params.setter
    def openai_params(self, value: BaseOpenAILLMService.InputParams):
        os.environ["OPENAI_TEMPERATURE"] = str(value.temperature)

    @property
    def tts_provider(self) -> str:
        return os.getenv("TTS_PROVIDER", "deepgram").lower()

    @tts_provider.setter
    def tts_provider(self, value: str):
        value = value.lower()
        if value not in ("deepgram", "cartesia", "elevenlabs", "rime"):
            raise ValueError(f"Invalid TTS provider: {value}")

        os.environ["TTS_PROVIDER"] = value

    @property
    def deepgram_voice(self) -> str:
        return os.getenv("DEEPGRAM_VOICE", "aura-athena-en")

    @deepgram_voice.setter
    def deepgram_voice(self, value: str):
        os.environ["DEEPGRAM_VOICE"] = value

    @property
    def cartesia_voice(self) -> str:
        return os.getenv("CARTESIA_VOICE", "79a125e8-cd45-4c13-8a67-188112f4dd22")

    @cartesia_voice.setter
    def cartesia_voice(self, value: str):
        os.environ["CARTESIA_VOICE"] = value

    @property
    def elevenlabs_voice_id(self) -> str:
        return os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")

    @elevenlabs_voice_id.setter
    def elevenlabs_voice_id(self, value: str):
        os.environ["ELEVENLABS_VOICE_ID"] = value

    @property
    def rime_voice_id(self) -> str:
        return os.getenv("RIME_VOICE_ID", "marissa")

    @rime_voice_id.setter
    def rime_voice_id(self, value: str):
        os.environ["RIME_VOICE_ID"] = value

    @property
    def rime_reduce_latency(self) -> bool:
        return self._is_truthy(os.getenv("RIME_REDUCE_LATENCY", "false"))

    @rime_reduce_latency.setter
    def rime_reduce_latency(self, value: bool):
        os.environ["RIME_REDUCE_LATENCY"] = str(value)

    @property
    def rime_speed_alpha(self) -> float:
        return float(os.getenv("RIME_SPEED_ALPHA", 1.0))

    @rime_speed_alpha.setter
    def rime_speed_alpha(self, value: float):
        os.environ["RIME_SPEED_ALPHA"] = str(value)

    @property
    def enable_stt_mute_filter(self) -> bool:
        return self._is_truthy(os.getenv("ENABLE_STT_MUTE_FILTER", "false"))

    @enable_stt_mute_filter.setter
    def enable_stt_mute_filter(self, value: bool):
        os.environ["ENABLE_STT_MUTE_FILTER"] = str(value)

    ###########################################################################
    # Smart Endpointing Configuration
    ###########################################################################

    @property
    def classifier_model(self) -> str:
        """Model used for classifying speech completeness."""
        return os.getenv("CLASSIFIER_MODEL", "gemini-2.0-flash-001")

    @classifier_model.setter
    def classifier_model(self, value: str):
        os.environ["CLASSIFIER_MODEL"] = value
