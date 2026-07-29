"""Global FAQ service — business facts, available from every agent.

This is GLOBAL knowledge (hours, contact), registered as a global function so any
node can answer without a handoff, then continue the task. It is NOT step-local
help ("where do I find my number") — that lives on each CollectStep and is answered
inline by the collection agent (see collection.explain_current_field).
"""

from typing import Tuple

from loguru import logger

from pipecat.flows import FlowManager

# Fallback used only when a tenant config hasn't set `business_faq`. The live facts come
# from the tenant config seeded into flow_manager.state (see services/config_api.py).
BUSINESS_FAQ = (
    "Opening hours: 9am to 5pm, Monday to Friday. "
    "Contact: 0800 123 4567 or help@example.com. "
    "We take card payments for parking tickets and water bills over the phone."
)


async def get_business_info(flow_manager: FlowManager, question: str = "") -> Tuple[dict, None]:
    """Answer a general business question, then return to the current task.

    Call this for anything that is NOT a payment value — working hours, how to
    contact us, etc. Returns None as the next node so the caller stays exactly where
    they were (state preserved for free).

    Args:
        question: The caller's question, paraphrased briefly.
    """
    logger.info(f"FAQ: {question!r}")
    facts = flow_manager.state.get("tenant", {}).get("business_faq", BUSINESS_FAQ)
    return (
        {
            "status": "success",
            "facts": facts,
            "hint": "Answer briefly using ONLY these facts, then resume the task you were "
            "on and re-ask the pending question. If the facts don't cover it, say you "
            "don't have that information.",
        },
        None,  # stay in the current node
    )
