"""Per-call frame tracer (Pipecat 1.x observer).

Writes one JSON line per frame push to a ``.jsonl`` file, capturing the flow of
every frame between processors *without* modifying the pipeline. This is the 1.x
idiomatic alternative to inserting passthrough "Trace" processors between each
stage: :class:`BaseObserver.on_push_frame` already sees every hop with the source
and destination processor, so a single observer replaces N pipeline inserts and
never has to be kept in sync with the pipeline shape.

Gated behind ``TRACE_CALLS=1`` (see ``config.trace_calls``); off by default so it
adds nothing to production calls.

Timeline: each line's ``ts`` is the pipeline clock in microseconds (monotonic,
starts at ~0 when the pipeline starts). Convert a trace to a zoomable per-call
timeline with ``tools/trace_to_perfetto.py`` and open it at https://ui.perfetto.dev.

The bot can also inject custom markers (e.g. a full context dump right after a
flow node transition) via :meth:`FrameTraceObserver.mark`.
"""

import json
import os
import time
from typing import Optional

from loguru import logger

from pipecat.observers.base_observer import BaseObserver, FramePushed


# High-frequency frames that would swamp a per-call timeline. Raw audio fires
# ~50x/sec per direction; BotSpeaking/Heartbeat fire continuously. Dropped by
# default; set TRACE_AUDIO=1 to include raw audio frames.
_NOISE_EXACT = frozenset({"BotSpeakingFrame", "HeartbeatFrame", "SystemHeartbeatFrame"})


def _is_audio_frame(name: str) -> bool:
    return name.endswith("AudioRawFrame")


class FrameTraceObserver(BaseObserver):
    """Observer that records every frame push to a JSONL trace file.

    Args:
        path: Trace file path. If omitted, a timestamped file is created under
            ``server/traces/``.
        include_audio: Include raw audio frames (very high frequency). Default False.
        payload_chars: Max characters of ``repr(frame)`` to record per line.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        include_audio: bool = False,
        payload_chars: int = 200,
    ):
        super().__init__()
        if path is None:
            traces_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "traces")
            os.makedirs(traces_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = os.path.join(traces_dir, f"call-{stamp}-{os.getpid()}.jsonl")
        self._path = path
        self._include_audio = include_audio
        self._payload_chars = payload_chars
        # Line-buffered so a trace stays readable even if the call crashes mid-way.
        self._f = open(path, "a", buffering=1, encoding="utf-8")
        # Reconstruct the pipeline clock origin from the first push so mark() timestamps
        # line up with frame timestamps on the same axis.
        self._clock_origin_mono: Optional[int] = None
        self._closed = False
        logger.info(f"FrameTraceObserver writing call trace -> {path}")

    @property
    def path(self) -> str:
        return self._path

    def _write(self, obj: dict):
        try:
            self._f.write(json.dumps(obj, default=str) + "\n")
        except Exception as e:  # never let tracing break the call
            logger.debug(f"FrameTraceObserver write failed: {e}")

    async def on_push_frame(self, data: FramePushed):
        # pipecat awaits observers inline in the frame-push path with no error guard, so a
        # stray exception here (e.g. a frame with a throwing __repr__) would hit audio
        # delivery. Tracing must never affect the call — swallow everything.
        try:
            if self._closed:
                return
            name = type(data.frame).__name__
            if name in _NOISE_EXACT:
                return
            if not self._include_audio and _is_audio_frame(name):
                return

            if self._clock_origin_mono is None:
                self._clock_origin_mono = time.monotonic_ns() - data.timestamp

            self._write(
                {
                    "ts": data.timestamp // 1000,  # ns -> us (Chrome Trace Format unit)
                    "frame": name,
                    "src": getattr(data.source, "name", str(data.source)),
                    "dst": getattr(data.destination, "name", str(data.destination)),
                    "dir": data.direction.name,
                    "id": getattr(data.frame, "id", None),
                    "payload": repr(data.frame)[: self._payload_chars],
                }
            )

            # The call is winding down: flush and close so the trace is complete on disk.
            if name in ("EndFrame", "CancelFrame"):
                self.close()
        except Exception:
            pass

    def mark(self, at: str, note: str = "", **extra):
        """Inject a custom marker line (e.g. a full context dump after a node change).

        Appears on its own ``mark:<at>`` track in the Perfetto view. Timestamp is
        derived from the same pipeline clock as frame lines so it lines up.
        """
        if self._clock_origin_mono is not None:
            ts = (time.monotonic_ns() - self._clock_origin_mono) // 1000
        else:
            ts = 0
        line = {"ts": ts, "frame": "MARK", "src": f"mark:{at}", "dst": at, "dir": "MARK", "note": note}
        if extra:
            line["extra"] = extra
        self._write(line)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._f.flush()
            self._f.close()
            logger.info(f"FrameTraceObserver closed call trace: {self._path}")
        except Exception:
            pass
