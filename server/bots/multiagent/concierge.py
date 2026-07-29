"""Concierge / Router — orchestration.

Greets, turns what the caller says into a typed-intent QUEUE (a mix of pay/inquire
items), dispatches them one at a time to the right agent with known slots seeded, and
is the hub every agent hands back to. "Cancel / anything else" lives here.

``resume`` is the drain point: it's async because dispatching the next item may need a
lookup, so every agent's "done / bail" handler returns ``await resume(...)``.
"""

import ast
import json
from typing import Tuple

from loguru import logger

from pipecat.flows import FlowManager, NodeConfig

from bots.multiagent.intent import QueueItem, enqueue, next_item, BILL_TYPES
from bots.multiagent.faq import get_business_info
from bots.multiagent.tenant import render, role_message, term, is_allowed, enabled_bill_types

_WORKING_KEYS = ("intent", "intent_slots", "bill_info", "arrival", "bill_type", "current",
                 "slot_attempts")


# ── entry + hub nodes ─────────────────────────────────────────────────────────
def build_greeting_node(state: dict) -> NodeConfig:
    """First node of the call — greet (from the tenant's greeting prompt) and capture intent."""
    return {
        "name": "greeting",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": render(state, "greeting")}],
        "respond_immediately": False,
        "task_messages": [_ROUTER_TASK],
        "functions": [route],
    }


def build_menu_node(flow_manager: FlowManager, preface: str = "") -> NodeConfig:
    """The hub — reached when the queue is empty; asks what's next."""
    state = flow_manager.state
    line = (preface + " " if preface else "") + render(state, "menu")
    return {
        "name": "menu",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": line}],
        "respond_immediately": False,
        "task_messages": [_ROUTER_TASK, {"role": "system",
            "content": "If the caller is finished, call `end_call`."}],
        "functions": [route, end_call],
    }


_ROUTER_TASK = {
    "role": "system",
    "content": """<task>
Your ONLY job here is to call `route`. Do NOT reply with plain text, do NOT ask the
caller any question, and NEVER ask them to repeat a value they already gave — if they
named a ticket or account number, use it. As soon as you know what they want, call
`route(requests_json=...)`; the flow will ask for anything still missing. If you're
unsure of the bill type, still call `route` and just omit bill_type.
If the caller lists SEVERAL bills or tickets (e.g. "two parking tickets, 1001 and 1002"),
include EVERY one as its own object in the requests array in a single call — never drop
any and never handle only the first.

Pass `requests_json` as a JSON array STRING; each element is an object with keys:
  action:    "pay" or "inquire"   (inquire = they only want to know a balance)
  bill_type: "parking" or "water" (omit only if genuinely unclear)
  reference: the ticket/account number if they gave one, else omit
  amount:    the number of pounds if they gave one, else omit
Examples:
  "pay 34 for parking 1001 and my water bill 2220"
     -> route(requests_json='[{"action":"pay","bill_type":"parking","reference":"1001","amount":34},{"action":"pay","bill_type":"water","reference":"2220"}]')
  "check my parking ticket 1001"
     -> route(requests_json='[{"action":"inquire","bill_type":"parking","reference":"1001"}]')
For a general business question (hours, contact) call `get_business_info` instead.
</task>""",
}


# ── router / dispatch / drain ─────────────────────────────────────────────────
def _parse_requests(raw) -> list:
    """Best-effort parse of the router's requests payload into a list of dicts.

    Accepts a JSON string, a Python-repr string, or an already-parsed list/dict —
    models vary in what they hand back, so we're lenient.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        return [raw]
    for loader in (json.loads, ast.literal_eval):
        try:
            val = loader(raw)
            return val if isinstance(val, list) else [val]
        except Exception:
            continue
    logger.warning(f"route: could not parse requests {raw!r}")
    return []


async def route(flow_manager: FlowManager, requests_json: str = "[]") -> Tuple[dict, NodeConfig]:
    """Queue the caller's requests and start on the first.

    Args:
        requests_json: a JSON array (as a string) of request objects, each with keys
            action, bill_type, reference, amount — e.g.
            '[{"action":"pay","bill_type":"parking","reference":"1001","amount":34}]'.
            Passed as a string because Gemini's tool schema can't take an array of
            objects directly; we parse it here.
    """
    parsed = _parse_requests(requests_json)
    items = [
        QueueItem(
            action=(r.get("action") or "pay"),
            bill_type=r.get("bill_type"),
            reference=r.get("reference"),
            amount=r.get("amount"),
        )
        for r in parsed
    ]
    enqueue(flow_manager.state, items)
    node = await resume(flow_manager)
    return {"status": "routed", "queued": len(items)}, node


async def resume(flow_manager: FlowManager, preface: str = "") -> NodeConfig:
    """Clear the finished item's working state, then start the next queued item (or menu).

    ``preface`` (e.g. a just-finished payment confirmation) is spoken before whatever
    comes next — the menu question, or the next queued item's prompt.
    """
    state = flow_manager.state
    for k in _WORKING_KEYS:
        state.pop(k, None)
    item = next_item(state)
    if item is None:
        return build_menu_node(flow_manager, preface)
    state["current"] = item
    node = await dispatch(flow_manager, item)
    if preface:
        _prepend_say(node, preface)
    return node


def _prepend_say(node: NodeConfig, text: str) -> None:
    """Prepend a spoken line to a node's first tts_say (announce a finished action before
    the next node's prompt)."""
    pre = node.setdefault("pre_actions", [])
    for a in pre:
        if a.get("type") == "tts_say":
            a["text"] = f"{text} {a['text']}"
            return
    pre.insert(0, {"type": "tts_say", "text": text})


async def dispatch(flow_manager: FlowManager, item: QueueItem) -> NodeConfig:
    """Route one queue item to the right agent, seeding known slots.

    Enforces the tenant's ``capabilities`` here: an operation the tenant doesn't offer is
    declined and the queue drains on to the next item — the single gate every request
    passes through.
    """
    state = flow_manager.state
    if not item.bill_type or item.bill_type not in BILL_TYPES:
        return build_ask_billtype_node(state, item.action)

    # Capability gate: does this tenant offer this action on this bill type?
    if not is_allowed(state, item.action, item.bill_type):
        verb = "look up" if item.action == "inquire" else "take payment for"
        return await resume(
            flow_manager,
            f"I'm sorry, I can't {verb} a {term(state, item.bill_type, 'noun')} on this line.",
        )

    if item.action == "inquire":
        from bots.multiagent.inquiry import build_ask_reference_node, resolve_and_junction

        if item.reference:
            return await resolve_and_junction(flow_manager, item.bill_type, item.reference, "inquire")
        return build_ask_reference_node(flow_manager, item.bill_type)

    # pay: seed known slots, then into that bill type's collection (advance does the lookup)
    from bots.multiagent.collection import advance_collection

    state["bill_type"] = item.bill_type
    slots = state.setdefault("intent_slots", {})
    if item.reference:
        slots["reference"] = item.reference
    if item.amount is not None:
        slots["amount"] = item.amount
    return await advance_collection(flow_manager, item.bill_type)


def build_ask_billtype_node(state: dict, action: str) -> NodeConfig:
    """Bill type wasn't clear — ask which one, offering ONLY the tenant's enabled types."""
    verb = "look up" if action == "inquire" else "pay"
    nouns = [
        term(state, bt, "noun")
        for bt in enabled_bill_types(state)
        if is_allowed(state, action, bt)
    ]
    if not nouns:
        text = "I'm sorry, that isn't something I can help with on this line."
    else:
        if len(nouns) == 1:
            listing = f"a {nouns[0]}"
        elif len(nouns) == 2:
            listing = f"a {nouns[0]} or a {nouns[1]}"
        else:
            listing = ", ".join(f"a {n}" for n in nouns[:-1]) + f", or a {nouns[-1]}"
        text = f"Sure — which would you like to {verb}: {listing}?"
    return {
        "name": "ask_billtype",
        "role_message": role_message(state),
        "pre_actions": [{"type": "tts_say", "text": text}],
        "respond_immediately": False,
        "task_messages": [_ROUTER_TASK],
        "functions": [route],
    }


async def end_call(flow_manager: FlowManager) -> Tuple[dict, NodeConfig]:
    """Caller is finished — say goodbye and end."""
    # Goodbye rides on the end_conversation action's `text` so its TTSSpeakFrame is queued
    # right before the EndFrame. Letting the LLM speak it races the EndFrame (queued on node
    # entry, before the LLM turn) and gets cut off. respond_immediately=False = no LLM turn.
    return {"status": "bye"}, {
        "name": "goodbye",
        "role_message": role_message(flow_manager.state),
        "respond_immediately": False,
        "functions": [],
        "post_actions": [
            {"type": "end_conversation", "text": render(flow_manager.state, "goodbye")}
        ],
    }
