"""Inquiry — the shared READ flow (balance lookup).

Same shape for every bill type, so it's ONE flow parameterised by bill_type (which
lookup, which ref word) — not per-type agents. Ends at the shared post-lookup
junction, which is where inquiry funnels into payment.

``resolve_and_junction`` is the reusable "look it up and land at the junction" step,
called by inquiry AND by the Concierge when it dispatches an inquire item whose
reference is already known.
"""

from typing import Optional, Tuple

from pipecat.flows import FlowManager, NodeConfig

from bots.multiagent.intent import BILL_TYPES
from bots.multiagent.faq import get_business_info
from bots.multiagent.services import lookup
from bots.multiagent.settlement import _ROLE


async def resolve_and_junction(
    flow_manager: FlowManager, bill_type: str, reference: str, arrival: str
) -> NodeConfig:
    """Look the reference up and return the junction node (or a not-found node).

    Args:
        arrival: "inquire" or "pay" — sets the junction's opening line.
    """
    state = flow_manager.state
    info = await lookup(bill_type, str(reference))
    if info is None:
        return build_not_found_node(flow_manager, bill_type, reference)
    state["bill_info"] = info
    state["arrival"] = arrival
    from bots.multiagent.settlement import build_junction_node

    return build_junction_node(info, arrival)


def build_ask_reference_node(flow_manager: FlowManager, bill_type: str) -> NodeConfig:
    """Ask for the reference when the caller wants a balance but didn't give one."""
    state = flow_manager.state
    state["bill_type"] = bill_type
    bt = BILL_TYPES[bill_type]
    return {
        "name": f"inquire_{bill_type}",
        "role_message": _ROLE,
        "pre_actions": [{"type": "tts_say", "text": f"What's your {bt.ref_word}?"}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller wants the balance on a {bt.noun}. You asked for their {bt.ref_word}.
</task>
<instructions>
*   [ CONDITION: caller gives the {bt.ref_word} ] -> call `submit_reference` with it.
*   [ CONDITION: caller asks where to find it ] -> answer briefly (it's the {bt.ref_word}
    on their {bt.noun}), then re-ask.
</instructions>""",
            }
        ],
        "functions": [submit_reference, get_business_info],
    }


async def submit_reference(
    flow_manager: FlowManager, reference: str
) -> Tuple[dict, NodeConfig]:
    """Look up the balance for the reference the caller just gave (inquiry path).

    Args:
        reference: the ticket / account number.
    """
    bill_type = flow_manager.state.get("bill_type")
    node = await resolve_and_junction(flow_manager, bill_type, reference, arrival="inquire")
    return {"status": "looked_up", "reference": reference}, node


def build_not_found_node(flow_manager: FlowManager, bill_type: str, reference: str) -> NodeConfig:
    """Reference didn't resolve — offer to try again or go back."""
    bt = BILL_TYPES.get(bill_type)
    ref_word = bt.ref_word if bt else "reference"
    return {
        "name": "not_found",
        "role_message": _ROLE,
        "pre_actions": [
            {"type": "tts_say", "text": f"I couldn't find {ref_word} {reference}. "
             "Would you like to try again?"}
        ],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": "*   [ caller gives another reference ] -> call `submit_reference`.\n"
                "*   [ caller gives up ] -> call `give_up`.",
            }
        ],
        "functions": [submit_reference, give_up],
    }


async def give_up(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Caller abandons the inquiry — hand back to the Concierge."""
    from bots.multiagent.concierge import resume

    return {"status": "ok"}, await resume(flow_manager)
