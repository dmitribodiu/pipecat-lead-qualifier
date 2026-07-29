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
from bots.multiagent.services import lookup
from bots.multiagent.settlement import build_confirm_node, _spaced
from bots.multiagent.tenant import (
    role_message, term, cfg, money, slot_rules, validate_slot, retry_message,
)

# Sentinel: rules enforcement decided the caller exhausted their attempts on a slot.
_BAIL = object()


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
    attempts = state.setdefault("slot_attempts", {})
    not_found = None
    if slots.get("reference") and "bill_info" not in state:
        info = await lookup(bill_type, str(slots["reference"]))
        if info is None:
            # e.g. a queued ticket that turns out not to exist — discovered here, mid-flow.
            not_found = slots.pop("reference", None)
            attempts["reference"] = attempts.get("reference", 0) + 1
            rules = slot_rules(state, bill_type, "reference")
            logger.info(f"{bill_type} reference {not_found} not found "
                        f"(attempt {attempts['reference']}/{rules['max_attempts']})")
            if attempts["reference"] >= rules["max_attempts"]:
                return await _bail(
                    flow_manager,
                    f"I couldn't verify that {term(state, bill_type, 'ref_word')} after "
                    f"{rules['max_attempts']} attempts. No payment was taken.",
                )
        else:
            # Stamp the tenant's currency so settlement read-backs speak it correctly.
            info.currency = cfg(state).get("currency", info.currency)
            state["bill_info"] = info

    # Enforce per-slot value rules (amount min/max, …) on what's collected so far.
    bad = _enforce_rules(state, bill_type)
    if bad is _BAIL:
        return await _bail(flow_manager, state.pop("_bail_reason", "No payment was taken."))

    if bad or _remaining(state, bill_type):
        node = build_collection_entry(flow_manager, bill_type, reason=bad or None)
        if not_found:
            from bots.multiagent.concierge import _prepend_say

            _prepend_say(node, f"I couldn't find {term(state, bill_type, 'noun')} {not_found}.")
        return node
    return _to_settlement(flow_manager)


def _enforce_rules(state: dict, bill_type: str):
    """Validate already-collected value-slots against config rules.

    Runs on every advance so ALL entry paths (record_slots, junction/dispatch seeding)
    are checked in one place. On failure: drop the bad value, count the attempt, and
    return ``(slot, reason)`` to re-ask — or ``_BAIL`` once attempts are exhausted.
    """
    slots = state.get("intent_slots", {})
    attempts = state.setdefault("slot_attempts", {})
    for slot in ("amount",):  # value-validated slots; extend as needed
        if slot not in slots:
            continue
        rules = slot_rules(state, bill_type, slot)
        ok, reason = validate_slot(slot, slots[slot], rules)
        if ok:
            continue
        slots.pop(slot, None)
        attempts[slot] = attempts.get(slot, 0) + 1
        logger.info(f"{bill_type}.{slot} invalid ({reason}); "
                    f"attempt {attempts[slot]}/{rules['max_attempts']}")
        if attempts[slot] >= rules["max_attempts"]:
            state["_bail_reason"] = (
                f"I couldn't accept a valid amount after {rules['max_attempts']} "
                "attempts. No payment was taken."
            )
            return _BAIL
        return (slot, reason)
    return None


async def _bail(flow_manager: FlowManager, message: str) -> NodeConfig:
    """Abandon the current payment (attempts exhausted) and hand back to the Concierge."""
    from bots.multiagent.concierge import resume

    for k in ("intent_slots", "bill_info", "bill_type", "intent", "slot_attempts",
              "balance_announced"):
        flow_manager.state.pop(k, None)
    return await resume(flow_manager, message)


def build_collection_entry(flow_manager: FlowManager, bill_type: str, reason=None) -> NodeConfig:
    """Build the node that asks for the next missing slot (sync — no lookup/settle here).

    Prefer advance_collection() as the entry; this just renders the "ask next" node.

    Args:
        reason: optional ``(slot, why)`` from a failed validation — prepends a corrective
            line (and a "last try" warning) before re-asking.
    """
    state = flow_manager.state
    state["bill_type"] = bill_type
    bt = BILL_TYPES[bill_type]
    remaining = _remaining(state, bill_type)
    if not remaining:
        return _to_settlement(flow_manager)

    step = remaining[0]
    noun = term(state, bill_type, "noun")
    fields_help = "\n".join(f"- {s.slot}: {s.help}" for s in bt.steps)
    node = {
        "name": f"collect_{bill_type}",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": step.ask}],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
You are collecting details to pay a {noun}. You just asked: "{step.ask}".
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
    # After the invoice is retrieved, state the outstanding balance to the caller right
    # before asking how much they'd like to pay (once — flag prevents repeating on re-ask).
    info = state.get("bill_info")
    if step.slot == "amount" and info is not None and not state.get("balance_announced"):
        from bots.multiagent.concierge import _prepend_say

        _prepend_say(
            node,
            f"{noun.capitalize()} {_spaced(str(info.reference))} has an outstanding "
            f"balance of {money(info.amount_due, info.currency)}.",
        )
        state["balance_announced"] = True
    if reason:
        slot, why = reason
        rules = slot_rules(state, bill_type, slot)
        msg = retry_message(state, slot, why, rules)
        left = rules["max_attempts"] - state.get("slot_attempts", {}).get(slot, 0)
        if left == 1:
            msg += " This is your last attempt."
        from bots.multiagent.concierge import _prepend_say

        _prepend_say(node, msg)
    return node


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
        currency=info.currency if info else cfg(state).get("currency", "GBP"),
        extra={k: slots[k] for k in ("meter_reading", "tariff", "account_holder") if k in slots},
    )
    state["intent"] = intent
    return build_confirm_node(intent, state)


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

    for k in ("intent_slots", "bill_info", "bill_type", "intent", "slot_attempts",
              "balance_announced"):
        flow_manager.state.pop(k, None)
    return {"status": "cancelled"}, await resume(flow_manager, "Okay, I've stopped that payment.")
