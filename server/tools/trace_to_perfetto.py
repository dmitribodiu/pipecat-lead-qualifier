"""Convert a FrameTraceObserver JSONL trace into Chrome Trace Format for Perfetto.

Usage:
    python tools/trace_to_perfetto.py traces/call-YYYYMMDD-HHMMSS-PID.jsonl
    # -> writes traces/call-...perfetto.json
    # Then open https://ui.perfetto.dev and drag the .json file in.

Layout in the timeline:
- One track per processor (named by the frame's *source* processor). Each frame push
  is an instant marker on that track, so you can read the sequence a processor emitted
  and see exactly where a frame stalled or when idle fired.
- A synthesized "turn latency" lane at the top with duration blocks:
    * "reply"  = caller stopped speaking -> bot started speaking (voice-to-voice latency)
    * "bot speaking" = bot started -> bot stopped
  This is where the "gap between the user's 'yes' and the repeated tts_say" shows up.
- ``mark:*`` lines injected by the bot get their own track.
"""

import json
import sys
from collections import OrderedDict


LATENCY_TID = 1  # top lane
FIRST_PROC_TID = 10

# Canonical pipeline order (substring match on processor name) so Perfetto stacks the
# tracks input -> STT -> user-agg -> LLM -> TTS -> output -> assistant-agg, top to bottom.
# The linear chain sits just under the latency lane (0) and the mark lane (1); anything
# not matched here (Pipeline source/sink, RTVI, workers) sinks to the bottom.
PIPELINE_ORDER = [
    "InputTransport",     # transport.input()
    "STTService",         # DeepgramSTTService
    "UserAggregator",     # LLMUserAggregator
    "LLMService",         # GoogleLLMService / OpenAILLMService
    "TTSService",         # DeepgramTTSService
    "OutputTransport",    # transport.output()
    "AssistantAggregator",  # LLMAssistantAggregator
]


def pipeline_rank(name):
    """Vertical sort index for a track; lower = higher on screen. None = infra (bottom)."""
    if name.startswith("mark:"):
        return 1  # annotations right under the latency lane
    for i, kw in enumerate(PIPELINE_ORDER):
        if kw in name:
            return 2 + i
    return None


def load(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def first_occurrence_by_id(rows, frame_name):
    """First ts (us) each distinct frame id of the given type was seen.

    The same logical frame is pushed processor->processor, producing several lines
    with one shared id; we want the moment it first appeared.
    """
    seen = OrderedDict()
    for r in rows:
        if r.get("frame") == frame_name:
            fid = r.get("id")
            key = fid if fid is not None else id(r)
            if key not in seen:
                seen[key] = r["ts"]
    return sorted(seen.values())


def build_latency_spans(rows):
    """Duration blocks pairing user/bot speaking boundaries."""
    spans = []
    user_stop = first_occurrence_by_id(rows, "UserStoppedSpeakingFrame")
    bot_start = first_occurrence_by_id(rows, "BotStartedSpeakingFrame")
    bot_stop = first_occurrence_by_id(rows, "BotStoppedSpeakingFrame")

    # reply latency: each user-stop -> the next bot-start after it
    bi = 0
    for t_u in user_stop:
        while bi < len(bot_start) and bot_start[bi] < t_u:
            bi += 1
        if bi < len(bot_start):
            t_b = bot_start[bi]
            spans.append(("reply", t_u, t_b))

    # bot speaking: each bot-start -> the next bot-stop after it
    si = 0
    for t_b in bot_start:
        while si < len(bot_stop) and bot_stop[si] < t_b:
            si += 1
        if si < len(bot_stop):
            spans.append(("bot speaking", t_b, bot_stop[si]))

    return spans


def convert(path, out_path):
    rows = load(path)
    if not rows:
        print("empty trace, nothing to convert")
        return
    t0 = min(r["ts"] for r in rows)  # normalize timeline to start at 0

    events = []
    # process + track names
    events.append({"ph": "M", "name": "process_name", "pid": 1, "tid": 0,
                   "args": {"name": "call"}})
    events.append({"ph": "M", "name": "thread_name", "pid": 1, "tid": LATENCY_TID,
                   "args": {"name": "⏱ turn latency"}})
    events.append({"ph": "M", "name": "thread_sort_index", "pid": 1, "tid": LATENCY_TID,
                   "args": {"sort_index": 0}})

    # assign a track per source processor (stable, in first-appearance order), and give
    # each a sort index so Perfetto stacks them in pipeline order (infra sinks to bottom).
    tids = {}
    next_tid = FIRST_PROC_TID
    infra_rank = 100
    for r in rows:
        src = r.get("src", "?")
        if src not in tids:
            tids[src] = next_tid
            rank = pipeline_rank(src)
            if rank is None:
                rank = infra_rank
                infra_rank += 1
            events.append({"ph": "M", "name": "thread_name", "pid": 1, "tid": next_tid,
                           "args": {"name": src}})
            events.append({"ph": "M", "name": "thread_sort_index", "pid": 1, "tid": next_tid,
                           "args": {"sort_index": rank}})
            next_tid += 1

    # synthesized latency spans (complete events)
    for name, ts_a, ts_b in build_latency_spans(rows):
        dur = max(1, ts_b - ts_a)
        events.append({
            "ph": "X", "name": f"{name} {dur/1000:.0f}ms", "pid": 1, "tid": LATENCY_TID,
            "ts": ts_a - t0, "dur": dur,
        })

    # per-frame instant markers on the source processor's track
    for r in rows:
        args = {"dir": r.get("dir"), "dst": r.get("dst"), "id": r.get("id")}
        if r.get("payload"):
            args["payload"] = r["payload"]
        if r.get("note"):
            args["note"] = r["note"]
        if r.get("extra"):
            args["extra"] = r["extra"]
        # Marks are named by the node they set (e.g. "set_node: confirm") so the node
        # name is visible on the timeline and searchable in Perfetto's search bar.
        if r.get("frame") == "MARK":
            name = f"{r.get('dst', 'mark')}: {r.get('note', '')}".strip()
        else:
            name = r.get("frame", "?")
        events.append({
            "ph": "i", "s": "t", "name": name, "pid": 1,
            "tid": tids[r.get("src", "?")], "ts": r["ts"] - t0, "args": args,
        })

    doc = {"traceEvents": events, "displayTimeUnit": "ms"}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    span_count = sum(1 for e in events if e["ph"] == "X")
    print(f"wrote {out_path}")
    print(f"  {len(rows)} frames, {len(tids)} processor tracks, {span_count} latency spans")
    print("  open https://ui.perfetto.dev and drag the .json file in")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    inp = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else inp.rsplit(".jsonl", 1)[0] + ".perfetto.json"
    convert(inp, out)


if __name__ == "__main__":
    main()
