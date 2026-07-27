"""
Main entry point for the FastAPI server.

This module defines the FastAPI application, its endpoints,
and lifecycle management. It relies on environment variables for configuration
(e.g., HOST, FAST_API_PORT) and uses run_helpers.py for bot startup.
"""

import os
import subprocess
import sys
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from loguru import logger

from pipecat.transports.daily.utils import (
    DailyRESTHelper,
    DailyRoomParams,
)

from config.server import ServerConfig
from config.bot import BotConfig

# Server configuration
server_config = ServerConfig()

# Runtime state
bot_procs: Dict[int, tuple] = {}  # Track bot processes: {pid: (process, room_url)}
daily_helpers: Dict[str, DailyRESTHelper] = {}  # Store Daily API helpers (initialized in lifespan)
bot_args: list[str] = []

# Configure loguru (removing default handler and adding our custom handler)
logger.remove()
logger.add(
    sys.stderr,
    level="DEBUG",
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    enqueue=True,  # Thread-safe logging
    backtrace=True,  # Include exception context
    diagnose=True,  # Include variables in traceback
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI lifespan manager that handles startup and shutdown tasks.
    It initializes the DailyRESTHelper and starts a background cleanup task.
    """
    aiohttp_session = aiohttp.ClientSession()
    # The Daily REST helper + room-cleanup loop are only needed for the Daily transport.
    # The default WebSocket/FreeSWITCH transport runs bots in-process (see /audio).
    if server_config.daily_api_key:
        daily_helpers["rest"] = DailyRESTHelper(
            daily_api_key=server_config.daily_api_key,
            daily_api_url=server_config.daily_api_url,
            aiohttp_session=aiohttp_session,
        )

    cleanup_task = asyncio.create_task(cleanup_finished_processes())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await aiohttp_session.close()


async def cleanup_finished_processes() -> None:
    """
    Background task to clean up finished bot processes and delete their Daily rooms.
    """
    while True:
        try:
            for pid in list(bot_procs.keys()):
                proc, room_url = bot_procs[pid]
                if proc.poll() is not None:
                    logger.info(f"Cleaning up finished bot process {pid} for room {room_url}")
                    try:
                        await daily_helpers["rest"].delete_room_by_url(room_url)
                        logger.success(f"Successfully deleted room {room_url}")
                    except Exception as e:
                        logger.error(f"Failed to delete room {room_url}: {str(e)}")
                    del bot_procs[pid]
        except Exception as e:
            logger.error(f"Error during cleanup: {str(e)}")
        await asyncio.sleep(5)


# Create the FastAPI app with the lifespan context
app: FastAPI = FastAPI(lifespan=lifespan)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.websocket("/audio")
async def audio_websocket(websocket: WebSocket):
    """FreeSWITCH mod_audio_fork entrypoint.

    The dialplan starts `uuid_audio_fork ... ws://<host>:<port>/audio mono 8k`,
    which opens this WebSocket. We build a bot for the call, run it in-process for the
    duration of the connection, and clean up when FreeSWITCH disconnects.
    """
    await websocket.accept()
    # The dialplan fork URL carries ?uuid=${uuid}, so we know which FreeSWITCH channel
    # this media socket belongs to — needed to hang the call up over ESL at end-of-flow.
    call_uuid = websocket.query_params.get("uuid")
    logger.info(f"FreeSWITCH mod_audio_fork connected on /audio (uuid={call_uuid})")

    config = BotConfig()
    if config.bot_type == "payment":
        from bots.payment import PaymentBot

        bot = PaymentBot(config)
    elif config.bot_type == "multiagent":
        from bots.multiagent.bot import MultiAgentPaymentBot

        bot = MultiAgentPaymentBot(config)
    elif config.bot_type == "flow":
        from bots.flow import FlowBot

        bot = FlowBot(config)
    else:
        from bots.simple import SimpleBot

        bot = SimpleBot(config)

    try:
        await bot.setup_websocket_transport(websocket)
        bot.create_pipeline()
        await bot.start()
    except Exception as e:
        logger.exception(f"Bot terminated with error: {e}")
    finally:
        await bot.cleanup()
        # End-of-flow: the pipeline is done and the fork socket is closed, but the
        # FreeSWITCH channel is still parked. Drop it over ESL so the caller isn't left
        # in silence. No-op if the caller already hung up. Done here (not in a transport
        # event) so it survives worker teardown.
        if call_uuid and config.enable_esl_hangup:
            if config.hangup_delay_s > 0:
                await asyncio.sleep(config.hangup_delay_s)  # let a final "goodbye" flush
            from esl_client import hangup

            await hangup(
                call_uuid,
                host=config.fs_esl_host,
                port=config.fs_esl_port,
                password=config.fs_esl_password,
            )
        logger.info("Bot session ended")


async def create_room_and_token() -> tuple[str, str]:
    """
    Create a Daily room and get an access token.
    """
    room = await daily_helpers["rest"].create_room(DailyRoomParams())
    if not room.url:
        raise HTTPException(status_code=500, detail="Failed to create room")
    token = await daily_helpers["rest"].get_token(room.url)
    if not token:
        raise HTTPException(status_code=500, detail=f"Failed to get token for room: {room.url}")
    return room.url, token


def parse_server_args():
    """Parse server-specific arguments and store remaining args for bot processes"""
    import argparse

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", help="Server host")
    parser.add_argument("--port", type=int, help="Server port")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument(
        "-b",
        "--bot-type",
        type=str.lower,
        choices=["simple", "flow", "payment", "multiagent"],
        help="Bot variant",
    )

    # Parse known server args and keep remaining for bots
    server_args, remaining_args = parser.parse_known_args()

    # Update server config with parsed args
    global server_config
    if server_args.host:
        server_config.host = server_args.host
    if server_args.port:
        server_config.port = server_args.port
    if server_args.reload:
        server_config.reload = server_args.reload
    # Applied via env so both the in-process /audio bot and the Daily subprocess see it.
    if server_args.bot_type:
        os.environ["BOT_TYPE"] = server_args.bot_type

    global bot_args
    bot_args = remaining_args


# Call this before starting the app
parse_server_args()


async def start_bot_process(room_url: str, token: str) -> int:
    """Start a bot subprocess with forwarded CLI arguments"""
    # Check room capacity
    num_bots_in_room = sum(
        1 for proc, url in bot_procs.values() if url == room_url and proc.poll() is None
    )
    if num_bots_in_room >= server_config.max_bots_per_room:
        raise HTTPException(
            status_code=429,
            detail=f"Room {room_url} at capacity ({server_config.max_bots_per_room} bots)",
        )

    try:
        server_dir = os.path.dirname(os.path.abspath(__file__))
        run_helpers_path = os.path.join(server_dir, "runner.py")

        # Build command with forwarded arguments
        cmd = [
            sys.executable,
            run_helpers_path,
            "-u",
            room_url,
            "-t",
            token,
            *bot_args,  # Forward stored CLI arguments
        ]

        # Set up environment with proper Python path
        env = os.environ.copy()
        env["PYTHONPATH"] = server_dir  # Set server directory as Python path

        proc = subprocess.Popen(
            cmd,
            bufsize=1,
            cwd=server_dir,  # Run from server directory
            env=env,
        )
        bot_procs[proc.pid] = (proc, room_url)
        return proc.pid
    except Exception as e:
        logger.error(f"Bot startup failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to start bot process: {e}")


@app.get("/")
async def start_agent(request: Request):
    """
    Endpoint for direct browser access to the bot.
    Creates a room and spawns a bot subprocess.
    """
    logger.info("Creating room for bot (browser access)")
    room_url, token = await create_room_and_token()
    logger.info(f"Room URL: {room_url}")

    # Start bot and redirect to room
    await start_bot_process(room_url, token)
    return RedirectResponse(room_url)


@app.post("/connect")
async def rtvi_connect(request: Request) -> Dict[str, Any]:
    """API-friendly endpoint returning connection credentials.

    Returns:
        Dict containing room_url, token, bot_pid, and status_endpoint
    """
    logger.info("Creating room for RTVI connection")
    room_url, token = await create_room_and_token()
    logger.info(f"Room URL: {room_url}")

    # Start bot and return credentials
    pid = await start_bot_process(room_url, token)
    return {
        "room_url": room_url,
        "token": token,
        "bot_pid": pid,
        "status_endpoint": f"/status/{pid}",
    }


@app.get("/status/{pid}")
def get_status(pid: int):
    """
    Get the status of a specific bot process.
    """
    proc_tuple = bot_procs.get(pid)
    if not proc_tuple:
        raise HTTPException(status_code=404, detail=f"Bot with process id: {pid} not found")
    proc, _ = proc_tuple
    status = "running" if proc.poll() is None else "finished"
    return JSONResponse({"bot_id": pid, "status": status})


if __name__ == "__main__":
    # When running via "python -m main", start the FastAPI server using uvicorn.
    import uvicorn

    logger.info("Starting FastAPI server")
    uvicorn.run(
        "main:app",
        host=server_config.host,
        port=server_config.port,
        reload=server_config.reload,
    )
