"""Serializer bridging FreeSWITCH's mod_audio_fork wire format and Pipecat frames.

FreeSWITCH (with drachtio's `mod_audio_fork`, MIT-licensed, no concurrency cap)
connects to our bot as a WebSocket client and:

  * sends the caller's audio as **binary** WebSocket frames — raw signed 16-bit
    little-endian PCM, mono, at the rate requested in the dialplan
    (`uuid_audio_fork <uuid> start ws://host:PORT/audio mono 8k` -> 8000 Hz).
  * plays audio back into the call when we send a **JSON text** frame:
        {"type":"playAudio","data":{"audioContentType":"raw",
                                    "sampleRate":8000,"audioContent":"<base64 L16>"}}
  * flushes queued playback on barge-in when we send:
        {"type":"killAudio"}

So: binary in -> InputAudioRawFrame ; TTS out -> playAudio JSON ; barge-in ->
killAudio. Text control frames (e.g. DTMF forwarded as `{"type":"dtmf","digits":"35"}`)
are injected as a user turn.

Pipecat 1.5.0's FastAPI WebSocket transport dispatches text vs binary by the return
type of `serialize`/`deserialize` and reads both frame types off one socket, so no
serializer `type` property is needed.

Protocol reference: https://github.com/mdslaney/drachtio-freeswitch-modules/blob/main/modules/mod_audio_fork/README.md
"""

import base64
import json

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    LLMMessagesAppendFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class AudioForkSerializer(FrameSerializer):
    """mod_audio_fork <-> Pipecat frame serializer."""

    def __init__(self, sample_rate: int = 8000):
        """Initialize the serializer.

        Args:
            sample_rate: PCM sample rate of the audio streamed to/from FreeSWITCH.
                Must match the rate in the dialplan's ``uuid_audio_fork`` command
                (``8k`` -> 8000, ``16k`` -> 16000).
        """
        super().__init__()
        self._sample_rate = sample_rate
        self._sent_msgs = 0
        self._sent_bytes = 0

    async def setup(self, frame: StartFrame):
        """No setup required."""
        pass

    async def deserialize(self, data: str | bytes) -> Frame | None:
        """FreeSWITCH -> us. Binary = raw L16 PCM (caller audio); text = control JSON."""
        if isinstance(data, (bytes, bytearray)):
            return InputAudioRawFrame(
                audio=bytes(data), sample_rate=self._sample_rate, num_channels=1
            )

        # Text control frame. DTMF keypad entries (forwarded via
        # `uuid_audio_fork <uuid> send_text {"type":"dtmf","digits":"35"}`) are injected
        # as a user message so the flow node interprets them like a spoken reply.
        # mod_audio_fork's own events (transcription/transfer/disconnect/error) are
        # ignored — we run STT ourselves and drive the call from the flow.
        try:
            msg = json.loads(data)
        except (ValueError, TypeError):
            return None

        if isinstance(msg, dict) and msg.get("type") == "dtmf" and msg.get("digits"):
            digits = str(msg["digits"])
            logger.info(f"DTMF in: '{digits}' -> injecting as user turn")
            return LLMMessagesAppendFrame(
                messages=[{"role": "user", "content": digits}], run_llm=True
            )
        return None  # other control events ignored

    async def serialize(self, frame: Frame) -> str | bytes | None:
        """us -> FreeSWITCH. TTS audio -> raw binary L16; barge-in -> killAudio.

        mod_audio_fork bidirectional STREAM mode injects raw binary frames into the call
        in real time (via WRITE_REPLACE). It must be enabled on the dialplan's
        ``uuid_audio_fork start`` with the PIPE-delimited (``^^|``) form and
        ``...|true|true|<rate>`` (enabled|streaming|samplerate), plus
        ``send_silence_when_idle`` on the channel so there are write frames to replace.
        See docker/freeswitch/.../dialplan/mrf.xml. This streams frame-by-frame with no
        buffering; it does NOT use the ``playAudio`` JSON / temp-file path.
        """
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "killAudio"})
        if isinstance(frame, OutputAudioRawFrame):
            self._sent_msgs += 1
            self._sent_bytes += len(frame.audio)
            if self._sent_msgs == 1 or self._sent_msgs % 100 == 0:
                secs = self._sent_bytes / (frame.sample_rate * 2)
                logger.debug(
                    f"binary audio out: msg #{self._sent_msgs}, rate={frame.sample_rate}, "
                    f"chunk={len(frame.audio)}B, total {self._sent_bytes}B (~{secs:.2f}s)"
                )
            return frame.audio  # raw L16 PCM -> binary WS frame -> mod_audio_fork inject
        return None
