"""Minimal async FreeSWITCH ESL client for outbound call control.

The audio-fork WebSocket is the MEDIA plane and cannot hang up a call. Call control
(hangup, transfer, pause) goes over ESL — the CONTROL plane. This opens a short-lived
inbound-ESL connection, authenticates, and issues ``uuid_kill <uuid>`` to drop the
FreeSWITCH channel.

Async (asyncio streams) so it never blocks the bot's event loop — unlike the blocking
socket client in ``dtmf_bridge.py``. Uses the same ``FS_ESL_*`` settings. One short-lived
connection per command is fine: hangups are infrequent (once per call).
"""

import asyncio
import re

from loguru import logger


async def _read_block(reader: asyncio.StreamReader, timeout: float) -> str:
    """Read one ESL header block (terminated by a blank line)."""
    data = b""
    while b"\n\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout)
        if not chunk:
            break
        data += chunk
    return data.decode(errors="replace")


async def hangup(
    uuid: str, *, host: str, port: int, password: str, timeout: float = 5.0
) -> bool:
    """Hang up a FreeSWITCH channel by UUID via ESL ``uuid_kill``.

    Returns True if the command was sent. A no-op (and harmless) if the channel is
    already gone — e.g. the caller hung up first, so ``uuid_kill`` just reports the
    UUID doesn't exist.
    """
    if not uuid:
        return False
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except Exception as e:
        logger.warning(f"ESL connect {host}:{port} failed: {e}")
        return False
    try:
        await _read_block(reader, timeout)  # "Content-Type: auth/request"
        writer.write(f"auth {password}\n\n".encode())
        await writer.drain()
        reply = await _read_block(reader, timeout)  # auth result
        if "+OK" not in reply and "accepted" not in reply:
            logger.warning(f"ESL auth not accepted: {reply.strip()[:80]}")
            return False
        writer.write(f"api uuid_kill {uuid}\n\n".encode())
        await writer.drain()
        headers = await _read_block(reader, timeout)  # "Content-Type: api/response\nContent-Length: N"
        # Read the response BODY (the actual "+OK" / "-ERR No such channel") so the log
        # shows whether the channel was really killed, not just the headers.
        body = ""
        m = re.search(r"Content-Length:\s*(\d+)", headers)
        if m and int(m.group(1)) > 0:
            try:
                body = (await asyncio.wait_for(
                    reader.readexactly(int(m.group(1))), timeout)).decode(errors="replace")
            except Exception:
                pass
        ok = body.startswith("+OK")
        logger.info(f"ESL uuid_kill {uuid} -> {body.strip() or headers.strip()[:80]}")
        if not ok:
            logger.warning(
                f"uuid_kill did NOT return +OK for {uuid} — FreeSWITCH reports no such "
                "channel. The caller stays connected. Likely the fork URL's ?uuid= is not "
                "the caller's live channel UUID; compare it with `show channels` in fs_cli."
            )
        return ok
    except Exception as e:
        logger.warning(f"ESL hangup {uuid} failed: {e}")
        return False
    finally:
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), 2)
        except Exception:
            pass
