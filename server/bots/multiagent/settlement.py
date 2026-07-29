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
from bots.multiagent.tenant import role_message, money, term

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
def build_junction_node(info: BillInfo, arrival: str, state: dict) -> NodeConfig:
    """The convergence point after any successful lookup.

    Args:
        info: the resolved bill.
        arrival: "inquire" (offer to pay) or "pay" (proceed toward payment).
        state: flow state, for tenant currency wording.
    """
    balance = (
        f"{info.payee}: {_spaced(info.reference)} has an outstanding balance of "
        f"{money(info.amount_due, info.currency)}."
    )
    opener = (
        "Would you like to pay it now?" if arrival == "inquire"
        else "How much would you like to pay?"
    )
    return {
        "name": "junction",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": f"{balance} {opener}"}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has been told: "{balance} {opener}" — do not repeat it. Respond ONLY by
calling a function, never with plain text.
</task>
<instructions>
*   [ CONDITION: caller agrees to pay ("yes", "pay it", "go ahead") ] -> call
    `proceed_payment` (we then ask how much they'd like to pay). If they already named an
    amount, pass it as `amount`.
*   [ CONDITION: caller declines, or only wanted the balance ] -> call `back_to_menu`.
</instructions>""",
            }
        ],
        "functions": [proceed_payment, back_to_menu],
    }


async def proceed_payment(
    flow_manager: FlowManager, amount: float = 0
) -> Tuple[dict, Optional[NodeConfig]]:
    """Caller agreed to pay the looked-up bill.

    Move into that bill type's collection to gather whatever's still needed — chiefly the
    amount, so the caller can pay a partial amount — seeding what the inquiry already
    resolved (the reference, and the amount if they named one).

    Args:
        amount: the amount in pounds if the caller named a specific one; else 0 and we ask.
    """
    state = flow_manager.state
    info: BillInfo = state.get("bill_info")
    if info is None:
        return {"status": "error", "error": "no bill in context"}, None

    state["bill_type"] = info.bill_type
    slots = state.setdefault("intent_slots", {})
    slots["reference"] = info.reference  # already resolved by the inquiry lookup
    if amount:
        slots["amount"] = amount

    from bots.multiagent.collection import advance_collection

    return {"status": "collecting"}, await advance_collection(flow_manager, info.bill_type)


async def back_to_menu(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Caller does not want to pay — hand back to the Concierge (drains any queued items)."""
    from bots.multiagent.concierge import resume

    return {"status": "ok"}, await resume(flow_manager)


# ── confirm -> execute -> success (the money-move) ────────────────────────────
def build_confirm_node(intent: PaymentIntent, state: dict) -> NodeConfig:
    """Read-back confirmation. Prompt forbids narrating the result (see payment.py fix)."""
    readback = (
        f"So you wish to pay {money(intent.amount, intent.currency)} for "
        f"{term(state, intent.bill_type, 'noun')} {_spaced(intent.reference)}. Is that correct?"
    )
    return {
        "name": "confirm",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": readback}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has been asked: "{readback}" — do not repeat it. You cannot process a payment
yourself and must NEVER say it is done/processed — the next node speaks the result.
</task>
<instructions>
*   [ CONDITION: caller confirms (yes/correct) ] -> call `confirm_payment(confirmed=true)`,
    say nothing else.
*   [ CONDITION: caller names a different amount to pay instead ] -> call `proceed_payment`
    with that `amount` (it re-reads the new amount back for confirmation).
*   [ CONDITION: caller wants to change the amount but hasn't said how much ] -> ask
    "How much would you like to pay instead?"
*   [ CONDITION: caller wants to cancel entirely ] -> call
    `confirm_payment(confirmed=false, cancel=true)`.
</instructions>""",
            }
        ],
        "functions": [confirm_payment, proceed_payment],
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
        return {"status": "error", "error": result.error}, build_failure_node(state)

    # Announce success and flow straight into the menu (or the next queued payment) so the
    # bot proactively asks "anything else?" instead of going silent.
    line = (
        f"Your payment of {money(intent.amount, intent.currency)} for "
        f"{term(state, intent.bill_type, 'noun')} {_spaced(intent.reference)} is done. "
        f"Your reference is {_spaced(result.order_id)}."
    )
    _clear_payment(state)
    from bots.multiagent.concierge import resume

    return {"status": "success", "order_id": result.order_id}, await resume(flow_manager, preface=line)


async def after_payment(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Return to the Concierge, which drains the queue or asks what's next."""
    from bots.multiagent.concierge import resume

    return {"status": "ok"}, await resume(flow_manager)


def build_failure_node(state: dict) -> NodeConfig:
    """Payment API failed — never retry a money-move blindly; apologize and hand back."""
    return {
        "name": "settlement_failure",
        "role_message": role_message(state),
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
    for k in ("intent", "intent_slots", "bill_info", "arrival", "slot_attempts"):
        state.pop(k, None)
