"""Payment-order bot — EDITOR-SHAPED, STEP 1 ONLY (invoice collection).

Runtime currently implements only the FIRST handler so it can be run and traced end-to-end:

    collect_invoice --submit_invoice--> found (announce balance, hang up)
          ^   ^ invalid_invoice / invoice_not_found (re-ask)
          |   └───────────────────────────────────────────
          └── retries_exhausted (×3) ──> terminate

`submit_amount` / `confirm_payment` (steps 2-3) are intentionally omitted — add them next.
The 3-node `flow.json` shows the intended full design; this module is the running subset.

Editor-shaping principles are unchanged:
1. Parameterless node factories; dynamic lines come from state["announce"] via the
   `announce` pre-action (fallback = the fixed opening).
2. One discrete `result` per handler (RESULT_* below).
3. One decision block (`_route`) maps result -> node.
"""

import os
import random
from typing import Optional, Tuple

from dotenv import load_dotenv
from loguru import logger

from pipecat.flows import FlowManager, NodeConfig
from pipecat.frames.frames import TTSSpeakFrame

from bots.base_bot import BaseBot
from config.bot import BotConfig
from services.invoice_api import Invoice, InvoiceApi, MockInvoiceApi

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
MAX_ATTEMPTS = int(os.getenv("PAYMENT_MAX_ATTEMPTS", "3"))
IDLE_TIMEOUT_S = float(os.getenv("PAYMENT_IDLE_TIMEOUT_S", "8"))
INVOICE_MIN_LEN = 3
INVOICE_MAX_LEN = 10

TERMINATE_TEXT = "I will terminate the call, bye."

invoice_api: InvoiceApi = MockInvoiceApi()

# ── Discrete routing results (the editor's decision variable) ─────────────────
RESULT_FOUND = "found"                            # invoice found (happy path) -> found node
RESULT_INVALID_INVOICE = "invalid_invoice"        # wrong format -> re-ask invoice
RESULT_INVOICE_NOT_FOUND = "invoice_not_found"    # lookup miss -> re-ask invoice
RESULT_RETRIES_EXHAUSTED = "retries_exhausted"    # budget blown -> terminate

# ── FAQ + role ────────────────────────────────────────────────────────────────
INVOICE_HELP_TEXT = (
    "The invoice number is the reference on your invoice or parking ticket — "
    f"a {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digit number printed at the top of the document."
)
BUSINESS_FAQ = {
    "working_hours": "Our phone lines are open Monday to Friday, 9am to 5:30pm UK time.",
    "contact": "You can reach us via the contact form on our website, or by replying to your invoice email.",
    "invoice_number_help": INVOICE_HELP_TEXT,
    "company": "We are the automated payments line for this business.",
}

_ROLE = (
    "You are Marissa, an automated payments assistant on a phone line. Be brief, warm and "
    "conversational. Convert spoken numbers to digits ('two two two' -> 222). A message "
    "containing only digits is keypad input and takes priority over speech. Never invent a "
    "value the caller did not provide. If the caller asks a general question (hours, contact, "
    "what an invoice number is), call `get_business_info`, answer briefly from its facts, "
    "then return to the task."
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def _spaced(digits: str) -> str:
    return " ".join(str(digits))


def _normalize_invoice(value) -> str:
    return "".join(c for c in str(value) if c.isdigit())


def _invoice_format_ok(digits: str) -> bool:
    return digits.isdigit() and INVOICE_MIN_LEN <= len(digits) <= INVOICE_MAX_LEN


def _charge(state: dict, counter: str) -> bool:
    """Charge one attempt; return True when the budget for this failure class is blown."""
    n = state.get(counter, 0) + 1
    state[counter] = n
    return n >= MAX_ATTEMPTS


# ==============================================================================
# Node factories (parameterless; dynamic openings via state["announce"])
# ==============================================================================


def create_collect_invoice_node() -> NodeConfig:
    """Step 1: collect the invoice number (single-param `submit_invoice`)."""
    return {
        "name": "collect_invoice",
        "role_message": _ROLE,
        "pre_actions": [
            {"type": "announce",
             "fallback": "Welcome to the payments line. May I have your invoice number please?"},
            {"type": "set_completeness_rules", "rules": COLLECT_INVOICE_COMPLETENESS_RULES},
        ],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
Get the invoice number — a {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digit reference — and call
`submit_invoice(invoice_number=...)` the moment they give it.
</task>
<instructions>
*   [ caller gives an invoice number ] -> call `submit_invoice`.
*   [ caller asks what an invoice number is / where to find it ] -> call `get_business_info`,
    answer briefly, then re-ask for the number.
</instructions>""",
            }
        ],
        "functions": [submit_invoice],
    }


def create_found_node() -> NodeConfig:
    """Happy-path terminal for step 1: announce the balance, then end the call.

    (When step 2 is added this becomes a transition into `collect_amount` instead.)
    """
    return {
        "name": "found",
        "role_message": _ROLE,
        # task_messages is required on every node, even terminal ones. With
        # respond_immediately=False it never drives an LLM turn — it just satisfies validation;
        # the spoken content comes from the announce (pre) + end_conversation (post) actions.
        "task_messages": [{"role": "system", "content": "The invoice was found; the call is ending."}],
        "respond_immediately": False,
        "functions": [],
        # announce (pre) speaks the balance stashed in state; end_conversation (post) speaks
        # the sign-off then closes — both queued in order ahead of any LLM turn.
        "pre_actions": [{"type": "announce", "fallback": "I found your invoice."}],
        "post_actions": [{"type": "end_conversation", "text": "Thank you, goodbye."}],
    }


def create_end_call_node() -> NodeConfig:
    """Terminal node — end the call (used when the retry budget is exhausted)."""
    return {
        "name": "end_call",
        "role_message": _ROLE,
        # Required on every node; respond_immediately=False means it never fires a turn.
        "task_messages": [{"role": "system", "content": "The call is ending."}],
        "respond_immediately": False,
        "functions": [],
        "post_actions": [{"type": "end_conversation", "text": TERMINATE_TEXT}],
    }


# ==============================================================================
# Decision block — result -> node (what the editor controls)
# ==============================================================================
_ROUTES = {
    RESULT_FOUND: create_found_node,
    RESULT_INVALID_INVOICE: create_collect_invoice_node,
    RESULT_INVOICE_NOT_FOUND: create_collect_invoice_node,
    RESULT_RETRIES_EXHAUSTED: create_end_call_node,
}


def _route(result: str, payload: dict) -> Tuple[dict, Optional[NodeConfig]]:
    factory = _ROUTES.get(result)
    node = factory() if factory else None
    return {"status": result, **payload}, node


# ==============================================================================
# Handler #1 (the only one implemented for now)
# ==============================================================================


async def submit_invoice(
    flow_manager: FlowManager, invoice_number: str
) -> Tuple[dict, Optional[NodeConfig]]:
    """Record the invoice number and look it up.

    Args:
        invoice_number: the invoice reference as digits (e.g. "222").
    """
    state = flow_manager.state
    digits = _normalize_invoice(invoice_number)

    if not _invoice_format_ok(digits):
        state["awaiting"] = "invoice"
        if _charge(state, "attempts_invoice"):
            return _route(RESULT_RETRIES_EXHAUSTED, {"error": "invalid invoice"})
        state["announce"] = (
            f"I'm sorry, an invoice number is {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digits. "
            "May I have your invoice number again?"
        )
        return _route(RESULT_INVALID_INVOICE, {"error": f"'{invoice_number}' is invalid."})

    found = await invoice_api.get_invoice(digits)
    if found is None:
        state["awaiting"] = "invoice"
        if _charge(state, "attempts_invoice"):
            return _route(RESULT_RETRIES_EXHAUSTED, {"error": "invoice not found"})
        state["announce"] = (
            f"I'm sorry, invoice {_spaced(digits)} does not exist. "
            "May I have your invoice number again?"
        )
        return _route(RESULT_INVOICE_NOT_FOUND, {"error": f"invoice {digits} not found."})

    state["invoice"] = found
    state["announce"] = (
        f"Invoice {_spaced(found.number)}, outstanding balance {found.amount_due:.2f} pounds."
    )
    return _route(RESULT_FOUND, {
        "invoice_number": found.number,
        "amount_due": found.amount_due,
        "currency": found.currency,
    })


async def get_business_info(
    flow_manager: FlowManager, question: str = ""
) -> Tuple[dict, None]:
    """Answer a general question about the business or the payment process.

    Args:
        question: the caller's question, paraphrased briefly.
    """
    logger.info(f"FAQ question: {question!r}")
    return (
        {
            "status": "success",
            "facts": BUSINESS_FAQ,
            "hint": "Answer briefly using ONLY these facts, then re-ask for the invoice number.",
        },
        None,
    )


# ── Completeness rule for the invoice question ────────────────────────────────
COLLECT_INVOICE_COMPLETENESS_RULES = f"""

CURRENT QUESTION: invoice number.
- Invoice numbers are {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digits. Dictating and FEWER
  than {INVOICE_MIN_LEN} digits so far -> `○`.
- "My invoice number is" / "the number is" with no digits yet -> `○`.
- {INVOICE_MIN_LEN}+ digits IS complete -> `✓`."""

_INVOICE_LOOKUP_FILLERS = [
    "One moment, let me find that invoice in our system.",
    "Let me check that for you.",
]


# ==============================================================================
# Bot implementation
# ==============================================================================


class PaymentEditorBot(BaseBot):
    """Editor-shaped payment bot — step 1 only (invoice collection)."""

    IDLE_TIMEOUT_S = IDLE_TIMEOUT_S
    TURN_COMPLETION_GUIDANCE = COLLECT_INVOICE_COMPLETENESS_RULES

    def __init__(self, config: BotConfig):
        super().__init__(config)
        self.flow_manager: Optional[FlowManager] = None

        @self.context_aggregator.user().event_handler("on_user_turn_idle")
        async def _on_idle(_aggregator):
            await self._handle_idle()

        @self.context_aggregator.user().event_handler("on_user_turn_started")
        async def _on_turn_started(_aggregator, _strategy):
            if self.flow_manager:
                self.flow_manager.state.pop("attempts_idle", None)

        @self.llm.event_handler("on_function_calls_started")
        async def _on_function_calls_started(_service, function_calls):
            for call in function_calls:
                if call.function_name == "submit_invoice":
                    await self.tts.queue_frame(TTSSpeakFrame(random.choice(_INVOICE_LOOKUP_FILLERS)))
                    return

    async def _handle_idle(self):
        if not self.flow_manager:
            return
        state = self.flow_manager.state
        attempts = state.get("attempts_idle", 0) + 1
        state["attempts_idle"] = attempts
        if attempts >= MAX_ATTEMPTS:
            await self.flow_manager.set_node_from_config(create_end_call_node())
            return
        logger.info(f"User idle (attempt {attempts}) while awaiting invoice")
        state["announce"] = "I'm sorry, I didn't catch that. May I have your invoice number please?"
        await self.flow_manager.set_node_from_config(create_collect_invoice_node())

    async def _handle_first_participant(self):
        """Initialize the flow when the caller connects."""
        self.flow_manager = FlowManager(
            worker=self.worker,
            llm=self.llm,
            context_aggregator=self.context_aggregator,
            global_functions=[get_business_info],
        )
        self.trace_flow_nodes(self.flow_manager)

        async def _announce(action: dict, flow_manager: FlowManager):
            line = flow_manager.state.pop("announce", None) or action.get("fallback", "")
            if line:
                await self.worker.queue_frame(TTSSpeakFrame(line))

        async def _set_completeness_rules(action: dict, _flow_manager: FlowManager):
            self.set_turn_completion_rules(action.get("rules", ""))

        self.flow_manager.register_action("announce", _announce)
        self.flow_manager.register_action("set_completeness_rules", _set_completeness_rules)
        self.flow_manager.state["awaiting"] = "invoice"
        await self.flow_manager.initialize(create_collect_invoice_node())
