"""Server configuration management module."""

import os
from dotenv import load_dotenv


class ServerConfig:
    def __init__(self):
        load_dotenv()

        # Server settings
        self.host: str = os.getenv("HOST", "0.0.0.0")
        self.port: int = int(os.getenv("FAST_API_PORT", "7860"))
        self.reload: bool = os.getenv("RELOAD", "false").lower() == "true"

        # Transport selection: "websocket" (FreeSWITCH, default) or "daily".
        self.transport: str = os.getenv("TRANSPORT", "websocket").lower()

        # Daily API settings (only needed for the Daily transport).
        self.daily_api_key: str = os.getenv("DAILY_API_KEY")
        self.daily_api_url: str = os.getenv("DAILY_API_URL", "https://api.daily.co/v1")

        # Bot settings
        self.max_bots_per_room: int = int(os.getenv("MAX_BOTS_PER_ROOM", "1"))

        # DAILY_API_KEY is required only when using the Daily transport.
        if self.transport == "daily" and not self.daily_api_key:
            raise ValueError("DAILY_API_KEY must be set when TRANSPORT=daily")
