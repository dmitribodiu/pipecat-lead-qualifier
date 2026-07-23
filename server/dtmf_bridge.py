"""Standalone DTMF bridge: FreeSWITCH ESL DTMF events -> the bot's fork WebSocket.

mod_audio_fork streams audio but does NOT carry DTMF. FreeSWITCH detects DTMF
(RFC2833 or inband) and fires ``DTMF`` ESL events; this bridge subscribes to them,
**aggregates** the digits per channel (inter-digit timeout, or a ``#`` terminator), and
forwards the completed digit string back over the SAME fork websocket using
``uuid_audio_fork <uuid> send_text {"type":"dtmf","digits":"..."}``. The bot receives it
as a text frame and ``AudioForkSerializer.deserialize`` injects it as a user turn — so
keypad entry flows through the exact same flow logic as speech (and, per the payment
prompt, a digits-only message takes priority over speech).

Aggregation avoids the fragmentation problem: pressing "5300" yields one turn
(``digits":"5300"``), not four separate single-digit turns.

This is the same pattern jambonz uses (ESL DTMF listener + fork send_text). Run it
alongside FreeSWITCH (in WSL for dev; a media-control service in prod)::

    python dtmf_bridge.py            # uses env FS_ESL_HOST/PORT/PASSWORD

Env: FS_ESL_HOST (127.0.0.1), FS_ESL_PORT (8021), FS_ESL_PASSWORD (ClueCon),
     DTMF_INTERDIGIT_MS (1500), DTMF_TERMINATOR (#).
"""

import os
import socket
import time
import urllib.parse

HOST = os.getenv("FS_ESL_HOST", "127.0.0.1")
PORT = int(os.getenv("FS_ESL_PORT", "8021"))
PASSWORD = os.getenv("FS_ESL_PASSWORD", "ClueCon")
INTERDIGIT_S = int(os.getenv("DTMF_INTERDIGIT_MS", "1500")) / 1000.0
TERMINATOR = os.getenv("DTMF_TERMINATOR", "#")


class EslClient:
    """Minimal ESL inbound client (no external deps)."""

    def __init__(self, host: str, port: int, password: str):
        self._sock = socket.create_connection((host, port), timeout=10)
        self._buf = b""
        self._read_headers()  # auth/request
        self._send(f"auth {password}")
        self._read_headers()  # command/reply
        self._send("event plain DTMF")
        self._read_headers()

    def _send(self, line: str):
        self._sock.sendall(line.encode() + b"\n\n")

    def bgapi(self, cmd: str):
        """Fire-and-forget API call (won't block on a reply while events flow)."""
        self._send(f"bgapi {cmd}")

    def _read_headers(self):
        while b"\n\n" not in self._buf:
            data = self._sock.recv(4096)
            if not data:
                return None, b""
            self._buf += data
        head, self._buf = self._buf.split(b"\n\n", 1)
        headers = {}
        for line in head.decode(errors="replace").split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
        body = b""
        cl = int(headers.get("Content-Length", 0))
        if cl:
            while len(self._buf) < cl:
                data = self._sock.recv(4096)
                if not data:
                    break
                self._buf += data
            body, self._buf = self._buf[:cl], self._buf[cl:]
        return headers, body

    def next_event(self, timeout: float):
        """Return the next DTMF event's field dict, or None on timeout."""
        self._sock.settimeout(timeout)
        try:
            headers, body = self._read_headers()
        except socket.timeout:
            return None
        if not headers or headers.get("Content-Type") != "text/event-plain":
            return None
        ev = {}
        for line in body.decode(errors="replace").split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                ev[k.strip()] = urllib.parse.unquote(v.strip())
        return ev


def main():
    print(f"[dtmf_bridge] connecting to ESL {HOST}:{PORT}", flush=True)
    esl = EslClient(HOST, PORT, PASSWORD)
    print("[dtmf_bridge] subscribed to DTMF events", flush=True)

    buffers: dict[str, str] = {}
    last_ts: dict[str, float] = {}

    def flush(uuid: str):
        digits = buffers.pop(uuid, "")
        last_ts.pop(uuid, None)
        if not digits:
            return
        payload = '{"type":"dtmf","digits":"%s"}' % digits
        esl.bgapi(f"uuid_audio_fork ^^|{uuid}|send_text|{payload}")
        print(f"[dtmf_bridge] {uuid[:8]} -> send_text digits={digits}", flush=True)

    while True:
        ev = esl.next_event(timeout=0.25)
        now = time.monotonic()
        if ev and ev.get("Event-Name") == "DTMF":
            uuid = ev.get("Unique-ID", "")
            digit = ev.get("DTMF-Digit", "")
            if uuid and digit:
                if digit == TERMINATOR:
                    flush(uuid)  # terminator ends entry immediately (stripped)
                else:
                    buffers[uuid] = buffers.get(uuid, "") + digit
                    last_ts[uuid] = now
        # inter-digit timeout flush
        for uuid in [u for u, t in last_ts.items() if now - t >= INTERDIGIT_S]:
            flush(uuid)


if __name__ == "__main__":
    main()
