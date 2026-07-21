from datetime import datetime
import pytz
from .types import NodeMessage


def get_system_prompt(content: str) -> NodeMessage:
    """Return a dictionary with a system prompt.

    Returns only ``task_messages`` (the node's instructions). In Pipecat 1.x
    ``NodeConfig``, ``role_messages`` (list) is deprecated in favour of
    ``role_message`` (str); this flow keeps all instruction content in
    ``task_messages`` so no role message is needed.
    """
    return {
        "task_messages": [
            {
                "role": "system",
                "content": content,
            }
        ],
    }


def get_current_date_uk() -> str:
    """Return the current day and date formatted for the UK timezone."""
    current_date = datetime.now(pytz.timezone("Europe/London"))
    return current_date.strftime("%A, %d %B %Y")
