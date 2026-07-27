"""Collection agents — the DIVERGENT write flows (parking few steps, water more).

Each bill type's step list lives in intent.BILL_TYPES; this module turns a step list
into a collector node and shares the plumbing every collection agent needs:

    record_slots        - capture whatever the caller gave, look up on first reference,
                          hand to Settlement once complete
    explain_current_field - step-LOCAL help ("what is this / where do I find it"),
                          answered inline from CollectStep.help — never leaves the node
    cancel_payment      - abandon and hand back to the Concierge

The "3 vs 5 steps" difference is entirely data (BillType.steps). To make a step a
real separate node instead of one collector, split build_collection_entry — the
contracts (PaymentIntent, lookup) stay the same.
"""

from typing import Optional, Tuple

from loguru import logger

from pipecat.flows import FlowManager, NodeConfig

from bots.multiagent.intent import BILL_TYPES, PaymentIntent
from bots.multiagent.faq import get_business_info
from bots.multiagent.services import lookup
from bots.multiagent.settlement import build_confirm_node, _ROLE, _spaced


def _remaining(state: dict, bill_type: str) -> list:
    """Steps whose slot isn't filled yet."""
    slots = state.get("intent_slots", {})
    return [s for s in BILL_TYPES[bill_type].steps if s.slot not in slots]


async def advance_collection(flow_manager: FlowManager, bill_type: str) -> NodeConfig:
    """The one async entry into collection: look up on the first reference, then either
    ask the next missing field or (nothing left) go to settlement.

    Everything that starts/continues collection goes through here (Concierge dispatch,
    record_slots, the junction's collection-gap) so the lookup is never skipped — even
    when reference + amount were both known up front.
    """
    state = flow_manager.state
    state["bill_type"] = bill_type
    slots = state.setdefault("intent_slots", {})
    not_found = None
    if slots.get("reference") and "bill_info" not in state:
        info = await lookup(bill_type, str(slots["reference"]))
        if info is None:
            # e.g. a queued ticket that turns out not to exist — discovered here, mid-flow.
            not_found = slots.pop("reference", None)
            logger.info(f"{bill_type} reference {not_found} not found; re-asking")
        else:
            state["bill_info"] = info
    if _remaining(state, bill_type):
        node = build_collection_entry(flow_manager, bill_type)
        if not_found:
            from bots.multiagent.concierge import _prepend_say

            _prepend_say(node, f"I couldn't find {BILL_TYPES[bill_type].noun} {not_found}.")
        return node
    return _to_settlement(flow_manager)


def build_collection_entry(flow_manager: FlowManager, bill_type: str) -> NodeConfig:
    """Build the node that asks for the next missing slot (sync — no lookup/settle here).

    Prefer advance_collection() as the entry; this just renders the "ask next" node.
    """
    state = flow_manager.state
    state["bill_type"] = bill_type
    bt = BILL_TYPES[bill_type]
    remaining = _remaining(state, bill_type)
    if not remaining:
        return _to_settlement(flow_manager)

    step = remaining[0]
    fields_help = "\n".join(f"- {s.slot}: {s.help}" for s in bt.steps)
    return {
        "name": f"collect_{bill_type}",
        "role_message": _ROLE,
        "pre_actions": [{"type": "tts_say", "text": step.ask}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
You are collecting details to pay a {bt.noun}. You just asked: "{step.ask}".
Respond by calling a function, not with plain text. Call `record_slots` with whatever
value(s) the caller provides, and NEVER ask them to repeat a value they already gave.
When everything is collected you'll be moved to confirmation.
</task>
<fields>
{fields_help}
</fields>
<instructions>
*   [ CONDITION: caller gives a value ] -> call `record_slots` with it.
*   [ CONDITION: caller asks what a field is / where to find it ] -> call
    `explain_current_field` (answers from the field's help), then re-ask.
*   [ CONDITION: caller no longer wants to pay ] -> call `cancel_payment`.
</instructions>""",
            }
        ],
        # One collector fn serves both bill types (union of slots). TODO(you): split into
        # record_parking / record_water for cleaner per-agent tool schemas if you prefer.
        "functions": [record_slots, explain_current_field, cancel_payment],
    }


async def record_slots(
    flow_manager: FlowManager,
    reference: str = "",
    amount: float = 0,
    meter_reading: str = "",
    tariff: str = "",
    account_holder: str = "",
) -> Tuple[dict, Optional[NodeConfig]]:
    """Record any collection values the caller provided for the current bill.

    Pass only the fields the caller actually stated (omit the rest). The function looks
    the bill up on the first reference and moves to confirmation once all required
    fields are present.
    """
    state = flow_manager.state
    bill_type = state.get("bill_type")
    slots = state.setdefault("intent_slots", {})
    for k, v in {
        "reference": reference, "amount": amount, "meter_reading": meter_reading,
        "tariff": tariff, "account_holder": account_holder,
    }.items():
        if v:  # falsy ("" / 0) means the caller didn't provide this field
            slots[k] = v

    # advance_collection does the lookup and decides ask-next vs settle.
    return {"status": "recorded", "have": list(slots)}, await advance_collection(flow_manager, bill_type)


def _to_settlement(flow_manager: FlowManager) -> NodeConfig:
    """Build the normalised PaymentIntent from collected slots and enter confirmation."""
    state = flow_manager.state
    slots = state.get("intent_slots", {})
    info = state.get("bill_info")
    intent = PaymentIntent(
        bill_type=state["bill_type"],
        reference=str(slots.get("reference")),
        amount=float(slots.get("amount")),
        payee=info.payee if info else "",
        currency=info.currency if info else "GBP",
        extra={k: slots[k] for k in ("meter_reading", "tariff", "account_holder") if k in slots},
    )
    state["intent"] = intent
    return build_confirm_node(intent)


async def explain_current_field(
    flow_manager: FlowManager, field: str = ""
) -> Tuple[dict, None]:
    """Answer a step-LOCAL 'what is this / where do I find it' from the field's own help.

    This is why field help lives on the step, not in the global FAQ: the answer differs
    per bill type. Stays in the node (returns None) so nothing is lost.

    Args:
        field: which field the caller is asking about (e.g. "reference", "meter_reading").
    """
    state = flow_manager.state
    bt = BILL_TYPES.get(state.get("bill_type"))
    help_text = ""
    if bt:
        for s in bt.steps:
            if s.slot == field or (not field and s.slot not in state.get("intent_slots", {})):
                help_text = s.help + (f" For example, {s.example}." if s.example else "")
                break
    logger.info(f"field help ({field!r}): {help_text!r}")
    return (
        {"status": "success", "help": help_text or "I can explain any field — which one?",
         "hint": "Give this help briefly, then re-ask the current question."},
        None,
    )


async def cancel_payment(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Caller abandons the current payment — clear it and hand back to the Concierge."""
    from bots.multiagent.concierge import resume

    for k in ("intent_slots", "bill_info", "bill_type", "intent"):
        flow_manager.state.pop(k, None)
    return {"status": "cancelled"}, await resume(flow_manager, "Okay, I've stopped that payment.")
