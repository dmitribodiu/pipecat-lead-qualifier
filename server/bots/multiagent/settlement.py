"""Settlement — the shared money-move (confirm -> execute -> success).

Consumes a normalised ``PaymentIntent`` from state and never knows which collection
agent produced it. This is where the confirm read-back + payment execution + order
reference live — written ONCE, so the confirm-narration fix exists in one place.

Also hosts the post-lookup JUNCTION: every successful lookup (inquiry OR pay) lands
here — "found it, now what?" — with the opening line keyed to how the caller arrived.
"""

from typing import Optional, Tuple

from loguru import logger

from pipecat.flows import FlowManager, NodeConfig

from bots.multiagent.intent import BillInfo, PaymentIntent
from bots.multiagent.faq import get_business_info
from bots.multiagent.services import execute_payment

_ROLE = {
    "role": "system",
    "content": "You are Marissa, an automated payments assistant on a phone line. Be brief, "
    "warm and conversational. Convert spoken numbers to digits. Never invent a value the "
    "caller did not provide, and never verbalize function parameters.",
}


def _spaced(s: str) -> str:
    """Read a reference digit-by-digit so TTS doesn't say 'one thousand one'."""
    return " ".join(str(s))


# ── post-lookup junction ──────────────────────────────────────────────────────
def build_junction_node(info: BillInfo, arrival: str) -> NodeConfig:
    """The convergence point after any successful lookup.

    Args:
        info: the resolved bill.
        arrival: "inquire" (offer to pay) or "pay" (proceed toward payment).
    """
    balance = (
        f"{info.payee}: {_spaced(info.reference)} has an outstanding balance of "
        f"{info.amount_due:.2f} pounds."
    )
    opener = (
        "Would you like to pay it now?" if arrival == "inquire"
        else "How much would you like to pay?"
    )
    return {
        "name": "junction",
        "role_message": _ROLE,
        "pre_actions": [{"type": "tts_say", "text": f"{balance} {opener}"}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has been told: "{balance} {opener}" — do not repeat it. Handle their reply.
</task>
<instructions>
*   [ CONDITION: caller wants to pay (or gives an amount) ] -> call `proceed_payment` with
    the amount if they gave one, else no amount.
*   [ CONDITION: caller does not want to pay / is just asking ] -> call `back_to_menu`.
</instructions>""",
            }
        ],
        "functions": [proceed_payment, back_to_menu, get_business_info],
    }


async def proceed_payment(
    flow_manager: FlowManager, amount: float = 0
) -> Tuple[dict, Optional[NodeConfig]]:
    """Caller wants to pay the looked-up bill.

    Args:
        amount: the amount in pounds, if the caller stated one (omit / 0 if not).
    """
    state = flow_manager.state
    info: BillInfo = state.get("bill_info")
    if info is None:
        return {"status": "error", "error": "no bill in context"}, None

    # If we still need more than ref+amount for this bill type, route into its
    # collection with the known slots seeded. Otherwise settle directly.
    from bots.multiagent.intent import BILL_TYPES

    bt = BILL_TYPES.get(info.bill_type)
    needs_more = bool(bt and any(s.slot not in ("reference", "amount") for s in bt.steps))
    if amount:
        state.setdefault("intent_slots", {})["amount"] = amount

    if needs_more:
        from bots.multiagent.collection import advance_collection

        return {"status": "collecting"}, await advance_collection(flow_manager, info.bill_type)

    # ref + amount is enough -> straight to settlement
    if not amount:
        return {"status": "need_amount", "hint": "Ask how much they want to pay."}, None
    intent = PaymentIntent(info.bill_type, info.reference, float(amount), info.payee, info.currency)
    state["intent"] = intent
    return {"status": "confirming"}, build_confirm_node(intent)


async def back_to_menu(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Caller does not want to pay — hand back to the Concierge (drains any queued items)."""
    from bots.multiagent.concierge import resume

    return {"status": "ok"}, await resume(flow_manager)


# ── confirm -> execute -> success (the money-move) ────────────────────────────
def build_confirm_node(intent: PaymentIntent) -> NodeConfig:
    """Read-back confirmation. Prompt forbids narrating the result (see payment.py fix)."""
    readback = (
        f"So you wish to pay {intent.amount:.2f} pounds for {intent.bill_type} "
        f"{_spaced(intent.reference)}. Is that correct?"
    )
    return {
        "name": "confirm",
        "role_message": _ROLE,
        "pre_actions": [{"type": "tts_say", "text": readback}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has been asked: "{readback}" — do not repeat it. Respond ONLY by calling a
function; you cannot process a payment yourself and must NEVER say it is done/processed —
the next node speaks the result.
</task>
<instructions>
*   [ CONDITION: caller confirms (yes/correct) ] -> call `confirm_payment(confirmed=true)`,
    say nothing else.
*   [ CONDITION: caller wants to cancel ] -> call `confirm_payment(confirmed=false, cancel=true)`.
*   [ CONDITION: caller wants to change something ] -> ask what to change.
</instructions>""",
            }
        ],
        "functions": [confirm_payment],
    }


async def confirm_payment(
    flow_manager: FlowManager, confirmed: bool, cancel: bool = False
) -> Tuple[dict, Optional[NodeConfig]]:
    """Finalize or cancel the payment after the read-back.

    Args:
        confirmed: True if the caller confirmed as read back.
        cancel: True if the caller wants to abandon this payment.
    """
    state = flow_manager.state
    intent: PaymentIntent = state.get("intent")
    if cancel:
        from bots.multiagent.concierge import resume

        _clear_payment(state)
        return {"status": "cancelled"}, await resume(flow_manager, "No problem, cancelled.")
    if not confirmed or intent is None:
        return {"status": "not_confirmed", "hint": "Ask what to change — reference or amount."}, None

    result = await execute_payment(intent.bill_type, intent.reference, intent.amount)
    if not result.ok:
        logger.error(f"payment failed: {result.error}")
        return {"status": "error", "error": result.error}, build_failure_node()

    _clear_payment(state)
    return {"status": "success", "order_id": result.order_id}, build_success_node(result.order_id, intent)


def build_success_node(order_id: str, intent: PaymentIntent) -> NodeConfig:
    """Scripted success line, then hand back to the Concierge to drain the queue."""
    line = (
        f"Your payment of {intent.amount:.2f} pounds for {intent.bill_type} "
        f"{_spaced(intent.reference)} is done. Your reference is {_spaced(order_id)}."
    )
    return {
        "name": "success",
        "role_message": _ROLE,
        "pre_actions": [{"type": "tts_say", "text": line}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f'The caller was told: "{line}" Now call `after_payment` to '
                "continue (it moves on to the next queued item or asks what's next).",
            }
        ],
        "functions": [after_payment],
    }


async def after_payment(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Return to the Concierge, which drains the queue or asks what's next."""
    from bots.multiagent.concierge import resume

    return {"status": "ok"}, await resume(flow_manager)


def build_failure_node() -> NodeConfig:
    """Payment API failed — never retry a money-move blindly; apologize and hand back."""
    return {
        "name": "settlement_failure",
        "role_message": _ROLE,
        "pre_actions": [
            {
                "type": "tts_say",
                "text": "I'm sorry, there was a problem taking that payment. No money has "
                "been taken.",
            }
        ],
        "respond_immediately": False,
        "task_messages": [
            {"role": "system", "content": "Call `after_payment` to return to the menu."}
        ],
        "functions": [after_payment],
    }


def _clear_payment(state: dict) -> None:
    for k in ("intent", "intent_slots", "bill_info", "arrival"):
        state.pop(k, None)
