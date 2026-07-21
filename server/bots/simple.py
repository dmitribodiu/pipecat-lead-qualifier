"""Simple bot implementation using the base bot framework (Pipecat 1.x)."""

from pipecat.frames.frames import LLMRunFrame

from bots.base_bot import BaseBot
from config.bot import BotConfig
from prompts import get_simple_prompt
from loguru import logger


class SimpleBot(BaseBot):
    """Simple bot implementation with a single LLM prompt chain."""

    def __init__(self, config: BotConfig):
        # Define the initial system message with conversation instructions
        system_messages = get_simple_prompt()["task_messages"]
        logger.info(f"Initialising SimpleBot with system messages: {system_messages}")
        super().__init__(config, system_messages)

    async def _handle_first_participant(self):
        """Handle actions when the first participant joins.

        Trigger an initial LLM run so the bot greets the caller first.
        """
        await self.worker.queue_frames([LLMRunFrame()])
