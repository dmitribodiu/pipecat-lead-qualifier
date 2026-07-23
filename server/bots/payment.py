"""Payment-order bot: helps a caller pay an invoice over the phone.

Flow (the "guided" hybrid pattern — see demo-ivr's guided_payment_flow.py and the
official flows examples):

    collect ──(invoice found + amount valid)──▶ confirm ──yes──▶ create order ──▶ success
       ▲  ▲                                        │ change                        │ another?
       │  └────────────(corrections)───────────────┘                               ▼
       │                                                                     collect (fresh)
       └──── invalid amount ×3 / unknown invoice ×3 / silence ×3 ──▶ terminate

Design split (the rule the prototypes converged on):
- The LLM decides CONVERSATION: question order, phrasing, honoring volunteered info
  ("pay 34 pounds for invoice 222" -> one function call, nothing re-asked), and what
  the caller wants to change at confirmation.
- CODE decides MONEY/STATE: invoice lookup, amount limits, retry budgets, idle
  timeouts, termination, and the payment-order API call. Handlers return
  ``{"status": "error", ...}`` with ``next_node=None`` (stay in node) so the LLM
  re-asks only the invalid value; exhausted budgets route to a terminal node.
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
from services.invoice_api import (
    Invoice,
    InvoiceApi,
    MockInvoiceApi,
    MockPaymentApi,
    PaymentApi,
)

load_dotenv()

# ── Configuration (global limits; see AskUserQuestion decisions) ──────────────
MIN_AMOUNT = float(os.getenv("PAYMENT_MIN_AMOUNT", "1"))
MAX_AMOUNT = float(os.getenv("PAYMENT_MAX_AMOUNT", "10000"))
MAX_ATTEMPTS = int(os.getenv("PAYMENT_MAX_ATTEMPTS", "3"))
IDLE_TIMEOUT_S = float(os.getenv("PAYMENT_IDLE_TIMEOUT_S", "8"))
INVOICE_MIN_LEN = 3
INVOICE_MAX_LEN = 10

TERMINATE_TEXT = "I will terminate the call, bye."

# The API clients (mocks now; swap for real REST clients here).
invoice_api: InvoiceApi = MockInvoiceApi()
payment_api: PaymentApi = MockPaymentApi()

# ── Business FAQ (PLACEHOLDER CONTENT — replace with the real facts) ──────────
# Served by the get_business_info global function, callable from ANY node. The LLM
# answers from these facts only, then returns to its current task.
INVOICE_HELP_TEXT = (
    "The invoice number is the reference on your invoice or parking ticket — "
    f"a {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digit number printed at the top "
    "of the document."
)
BUSINESS_FAQ = {
    "working_hours": "Our phone lines are open Monday to Friday, 9am to 5:30pm UK time.",
    "contact": "You can reach us via the contact form on our website, or by replying to your invoice email.",
    "invoice_number_help": INVOICE_HELP_TEXT,
    "payment_methods": "Through this line we create payment orders against your invoice; other payment methods are described on your invoice.",
    "company": "We are the automated payments line for this business.",
}


_ROLE = (
    "You are Marissa, an automated payments assistant on a phone line. Be brief, warm "
    "and conversational. Read statements in double quotes exactly as written. Convert "
    "spoken numbers to digits ('two two two' -> 222; 'thirty four pounds' -> 34). A "
    "message containing only digits is keypad input and takes priority over speech. "
    "Never invent a value the caller did not provide, and never verbalize function "
    "parameters. If a function result reports an error, it names what is wrong — "
    "re-ask ONLY that value and keep everything else. If at ANY point the caller asks "
    "a general question (working hours, how to contact us, what an invoice number is "
    "or where to find it), call `get_business_info`, answer briefly using ONLY its "
    "facts, then return to the task you were on. If the facts don't cover the "
    "question, say you don't have that information and suggest the contact channel."
)


def _spaced(digits: str) -> str:
    """Digit-by-digit spacing so TTS reads '222' as 'two two two'."""
    return " ".join(digits)


def _normalize_invoice(value) -> str:
    """Extract the digit string from however the caller's invoice number arrived."""
    return "".join(c for c in str(value) if c.isdigit())


def _invoice_format_ok(digits: str) -> bool:
    return digits.isdigit() and INVOICE_MIN_LEN <= len(digits) <= INVOICE_MAX_LEN


def _parse_amount(value) -> Optional[float]:
    """Normalize an amount ('34', '£34.50', '34 pounds') to a float, or None."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        cleaned = (
            str(value).lower().replace("£", "").replace("pounds", "").replace("pound", "")
        )
        cleaned = cleaned.replace(",", "").strip()
        try:
            amount = float(cleaned)
        except ValueError:
            return None
    return round(amount, 2) if amount > 0 else None


# ==============================================================================
# Retry budgets (code-decided; one counter per failure class in flow state)
# ==============================================================================


def _strike(
    flow_manager: FlowManager, counter: str, error_result: dict, say: str
) -> Tuple[dict, Optional[NodeConfig]]:
    """Charge one attempt against ``counter``; re-ask with a scripted line, or terminate.

    The re-ask is spoken via a tts_say node re-entry (``say``) instead of letting the
    LLM phrase the error — saves a whole LLM round trip on every retry.
    """
    attempts = flow_manager.state.get(counter, 0) + 1
    flow_manager.state[counter] = attempts
    if attempts < MAX_ATTEMPTS:
        return error_result, create_collect_node(prefix=say)
    return error_result, create_terminate_node()


# ==============================================================================
# Nodes
# ==============================================================================


def create_collect_node(prefix: str = "") -> NodeConfig:
    """The multi-slot collection node: invoice number + amount, in one node."""
    # The opening line is spoken via tts_say (no LLM round trip — instant), and
    # respond_immediately=False means the LLM only runs once the caller replies.
    opening = prefix or "Welcome to the payments line. May I have your invoice number please?"
    return {
        "name": "collect",
        "role_message": _ROLE,
        "pre_actions": [
            {"type": "tts_say", "text": opening},
            {"type": "set_completeness_rules", "rules": COLLECT_COMPLETENESS_RULES},
        ],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has already been asked: "{opening}" — do NOT repeat it unprompted.
Collect TWO values from the caller, then hand off to the payment confirmation:
1. invoice_number — a {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digit invoice reference.
2. amount — how much they want to pay, in pounds.

Call `collect_payment_details` with EVERYTHING the caller has provided so far, as soon
as they provide it. Do not wait until you have both values — the function looks up the
invoice and will tell you what to say and what is still missing.
</task>

<instructions>
**Step 1 — Invoice number.**
*   [ CONDITION: caller gives an invoice number (and possibly an amount too) ]
    *   Immediately call `collect_payment_details` with everything they gave.
*   [ CONDITION: caller gives both invoice AND amount in one utterance, e.g. "I want to pay 34 pounds for invoice 222" ]
    *   Call `collect_payment_details(invoice_number=222, amount=34)` immediately. Do NOT re-ask anything.
*   [ CONDITION: caller lists SEVERAL payments at once ]
    *   Say "Let's take them one at a time." and call `collect_payment_details` with only the
        FIRST payment's values — the rest will be handled after this one completes.
*   [ CONDITION: caller asks what an invoice number is or where to find it ]
    *   Call `get_business_info`, answer from its facts, then re-ask for the number.

**Step 2 — Amount (only when the function result says the invoice was found).**
*   The function result contains the invoice's outstanding balance. State it back:
    "Invoice <number>, outstanding balance <amount_due> pounds." Then ask: "How much would you like to pay?"
*   [ CONDITION: caller gives an amount ]
    *   Call `collect_payment_details` with the amount (the invoice is already recorded).

**Error results.** If the function returns status "error", say the message conversationally
and re-ask ONLY the invalid value. Keep any value that was accepted.
</instructions>""",
            }
        ],
        "functions": [collect_payment_details],
    }


def create_confirm_node(invoice: Invoice, amount: float) -> NodeConfig:
    """Read-back confirmation node."""
    readback = (
        f"So you wish to pay {amount:.2f} pounds for invoice {_spaced(invoice.number)}. "
        "Is that correct?"
    )
    return {
        "name": "confirm",
        "role_message": _ROLE,
        # Scripted read-back spoken via tts_say — skips the second LLM round trip.
        "pre_actions": [
            {"type": "tts_say", "text": readback},
            {"type": "set_completeness_rules", "rules": CONFIRM_COMPLETENESS_RULES},
        ],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has already been asked: "{readback}" — do NOT repeat it unprompted.
Handle their reply, then call `confirm_payment`.
</task>

<instructions>
**Handle the reply.**
*   [ CONDITION: caller clearly confirms (yes / that's right / correct) ]
    *   Call `confirm_payment(confirmed=true)`.
*   [ CONDITION: caller says no, or wants something changed, but hasn't said what ]
    *   Ask: "What would you like to change — the invoice number or the amount?"
*   [ CONDITION: caller states a corrected value (a different amount or invoice number) ]
    *   Call `collect_payment_details` with ONLY the corrected value — it re-validates and
        brings you back to a fresh confirmation.
*   [ CONDITION: caller wants to cancel entirely ]
    *   Call `confirm_payment(confirmed=false, cancel=true)`.
</instructions>""",
            }
        ],
        # collect_payment_details is registered here too so corrections re-validate
        # through the same code path (and duplicate calls don't error).
        "functions": [confirm_payment, collect_payment_details],
    }


def create_success_node(order_id: str, amount: float, invoice_number: str) -> NodeConfig:
    """Payment order created; offer to pay another invoice (loop)."""
    success_line = (
        f"Your payment order for {amount:.2f} pounds against invoice "
        f"{_spaced(invoice_number)} has been created. Your order reference is "
        f"{_spaced(order_id)}. Would you like to pay another invoice?"
    )
    return {
        "name": "success",
        "role_message": _ROLE,
        # Fully scripted (result + follow-up question) via tts_say, and NO LLM run on
        # node entry — running the LLM here made it re-narrate the function result
        # ("your order was created") despite instructions, so the caller heard it twice.
        "pre_actions": [
            {"type": "tts_say", "text": success_line},
            {"type": "set_completeness_rules", "rules": SUCCESS_COMPLETENESS_RULES},
        ],
        "respond_immediately": False,
        "task_messages": [
            {
                "role": "system",
                "content": f"""<task>
The caller has already been told: "{success_line}" — do NOT repeat any of it.
Handle their reply.
</task>

<instructions>
**Step 1.** Check the conversation history FIRST: if the caller earlier mentioned another
payment that has not been processed yet (e.g. they said "two more invoices, 34 pounds for
invoice 1001 and 33 pounds for invoice 4321" and only the first is done), then on ANY
affirmative reply say "Next, invoice <number>." and call `collect_payment_details` with
that payment's values — do not re-ask for details you already have.

Otherwise:
*   [ CONDITION: yes, WITH details (e.g. "yes, 34 pounds for invoice 1001") ]
    *   Call `collect_payment_details` immediately with the values they gave. If they list
        SEVERAL payments, say "Let's take them one at a time." and submit only the FIRST —
        you will handle the next after this one completes.
*   [ CONDITION: yes, no details ] -> call `pay_another`.
*   [ CONDITION: no ] -> call `finish_call`.
</instructions>""",
            }
        ],
        "functions": [collect_payment_details, pay_another, finish_call],
    }


def create_goodbye_node() -> NodeConfig:
    """Normal end of call."""
    return {
        "name": "goodbye",
        "role_message": _ROLE,
        "task_messages": [
            {
                "role": "system",
                "content": 'Say: "Thank you for using our payments line. Goodbye." Then stop.',
            }
        ],
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


def create_terminate_node() -> NodeConfig:
    """Hard stop after exhausted retries (verbatim line, per requirements)."""
    return {
        "name": "terminate",
        "role_message": _ROLE,
        "task_messages": [
            {"role": "system", "content": f'Say exactly: "{TERMINATE_TEXT}" Then stop.'}
        ],
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


def create_api_failure_node() -> NodeConfig:
    """Payment API failed — apologize and end (never retry money operations blindly)."""
    return {
        "name": "api_failure",
        "role_message": _ROLE,
        "task_messages": [
            {
                "role": "system",
                "content": (
                    'Say: "I\'m sorry, there was a problem creating your payment order. '
                    'No payment has been taken. Please try again later. Goodbye." Then stop.'
                ),
            }
        ],
        "functions": [],
        "post_actions": [{"type": "end_conversation"}],
    }


# ==============================================================================
# Function handlers (code decides: lookup, limits, budgets, routing)
# ==============================================================================


async def collect_payment_details(
    flow_manager: FlowManager, invoice_number: str = "", amount: float = 0
) -> Tuple[dict, Optional[NodeConfig]]:
    """Record the invoice number and/or payment amount the caller has provided so far.

    Args:
        invoice_number: The invoice reference as digits (e.g. "222"). Pass it whenever
            the caller states or corrects an invoice number; omit otherwise.
        amount: The amount to pay in pounds (e.g. 34.50). Pass it whenever the caller
            states or corrects an amount; omit otherwise.
    """
    state = flow_manager.state
    invoice: Optional[Invoice] = state.get("invoice")

    # ── Invoice number (lookup is code's job) ────────────────────────────────
    if invoice_number:
        digits = _normalize_invoice(invoice_number)
        if not _invoice_format_ok(digits):
            state["awaiting"] = "invoice"
            return _strike(
                flow_manager,
                "attempts_invoice",
                {
                    "status": "error",
                    "error": f"'{invoice_number}' is not a valid invoice number — it must be "
                    f"{INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digits.",
                },
                say=f"I'm sorry, an invoice number is {INVOICE_MIN_LEN} to "
                f"{INVOICE_MAX_LEN} digits. May I have your invoice number again?",
            )
        if invoice is None or digits != invoice.number:
            found = await invoice_api.get_invoice(digits)
            if found is None:
                state["awaiting"] = "invoice"
                return _strike(
                    flow_manager,
                    "attempts_invoice",
                    {"status": "error", "error": f"invoice {digits} does not exist."},
                    say=f"I'm sorry, invoice {_spaced(digits)} does not exist. "
                    "May I have your invoice number again?",
                )
            invoice = found
            state["invoice"] = invoice
            state.pop("amount", None)  # a new invoice invalidates any earlier amount

    if invoice is None:
        state["awaiting"] = "invoice"
        return (
            {
                "status": "error",
                "error": "no invoice number has been provided yet.",
                "hint": "Ask the caller for their invoice number.",
            },
            None,
        )

    # ── Amount (limits are code's job) ───────────────────────────────────────
    if amount:
        value = _parse_amount(amount)
        if value is None or not (MIN_AMOUNT <= value <= MAX_AMOUNT):
            state["awaiting"] = "amount"
            return _strike(
                flow_manager,
                "attempts_amount",
                {
                    "status": "error",
                    "error": f"the amount must be between {MIN_AMOUNT:.0f} and "
                    f"{MAX_AMOUNT:.0f} pounds (got {amount!r}).",
                },
                say=f"I'm sorry, the amount must be between {MIN_AMOUNT:.0f} and "
                f"{MAX_AMOUNT:.0f} pounds. How much would you like to pay?",
            )
        state["amount"] = value
        state["awaiting"] = "confirm"
        return (
            {"status": "success", "invoice_number": invoice.number, "amount": value},
            create_confirm_node(invoice, value),
        )

    # Invoice known, amount still missing -> scripted reply via node re-entry
    # (tts_say, no LLM round trip to phrase it).
    state["awaiting"] = "amount"
    return (
        {
            "status": "invoice_found",
            "invoice_number": invoice.number,
            "amount_due": invoice.amount_due,
            "currency": invoice.currency,
        },
        create_collect_node(
            prefix=f"Invoice {_spaced(invoice.number)}, outstanding balance "
            f"{invoice.amount_due:.2f} pounds. How much would you like to pay?"
        ),
    )


async def confirm_payment(
    flow_manager: FlowManager, confirmed: bool, cancel: bool = False
) -> Tuple[dict, Optional[NodeConfig]]:
    """Finalize (or cancel) the payment after the read-back confirmation.

    Args:
        confirmed: True if the caller confirmed the payment as read back.
        cancel: True if the caller wants to abandon the payment entirely.
    """
    state = flow_manager.state
    invoice: Optional[Invoice] = state.get("invoice")
    amount = state.get("amount")

    if cancel:
        return {"status": "cancelled"}, create_goodbye_node()

    if not confirmed or invoice is None or amount is None:
        # "No" without a stated correction: the confirm-node prompt asks what to change.
        return (
            {
                "status": "not_confirmed",
                "hint": "Ask what they would like to change — the invoice number or the amount.",
            },
            None,
        )

    result = await payment_api.create_payment_order(invoice.number, amount)
    if not result.ok:
        logger.error(f"Payment order failed: {result.error}")
        return {"status": "error", "error": result.error}, create_api_failure_node()

    # Reset per-payment state so "pay another" starts clean.
    order_id = result.order_id
    invoice_number = invoice.number
    for key in (
        "invoice",
        "amount",
        "attempts_invoice",
        "attempts_amount",
        "attempts_idle",
        "awaiting",
    ):
        state.pop(key, None)
    return (
        {"status": "success", "order_id": order_id},
        create_success_node(order_id, amount, invoice_number),
    )


async def get_business_info(
    flow_manager: FlowManager, question: str = ""
) -> Tuple[dict, None]:
    """Answer a general question about the business or the payment process.

    Call this whenever the caller asks something that is not a payment value —
    e.g. working hours, how to contact us, what an invoice number is or where to
    find it. Answer from the returned facts only, then return to the current task.

    Args:
        question: The caller's question, paraphrased briefly.
    """
    logger.info(f"FAQ question: {question!r}")
    return (
        {
            "status": "success",
            "facts": BUSINESS_FAQ,
            "hint": "Answer briefly using ONLY these facts, then return to the task "
            "you were on (re-ask the pending question if there is one). If the facts "
            "don't cover it, say you don't have that information.",
        },
        None,  # stay in the current node
    )


async def pay_another(flow_manager: FlowManager) -> Tuple[None, NodeConfig]:
    """The caller wants to pay another invoice."""
    flow_manager.state["awaiting"] = "invoice"
    return None, create_collect_node(
        "Sure. May I have the next invoice number please?"
    )


async def finish_call(flow_manager: FlowManager) -> Tuple[None, NodeConfig]:
    """The caller is done."""
    return None, create_goodbye_node()


# ==============================================================================
# Bot implementation
# ==============================================================================


# ── Per-node turn-completeness rules ─────────────────────────────────────────
# Swapped into the completeness judge on node entry (set_completeness_rules
# pre-action), so each node's judge only carries rules relevant to its question.
COLLECT_COMPLETENESS_RULES = f"""

CURRENT QUESTION: invoice number and payment amount.
- Invoice numbers are {INVOICE_MIN_LEN} to {INVOICE_MAX_LEN} digits. If the caller is
  dictating digits and has said FEWER than {INVOICE_MIN_LEN} so far (e.g. "two two"),
  they are still dictating -> `○`. Callers often dictate digits in groups with pauses.
- "My invoice number is" / "the number is" with no digits yet -> `○`.
- "I want to pay" / "I'd like to pay" with no amount and no invoice yet -> `○`.
- "for invoice" / "against invoice" trailing at the end -> `○` (the number is coming).
- A digit string of {INVOICE_MIN_LEN}+ digits, or an amount ("thirty pounds",
  "34.50"), IS a complete answer -> `✓`."""

CONFIRM_COMPLETENESS_RULES = """

CURRENT QUESTION: yes/no confirmation of the payment read-back.
- A clear yes ("yes", "correct", "that's right") or no -> `✓`.
- "yes but" / "no, actually" / "can you change" trailing without the change -> `○`.
- "change the amount to" / "make it" without a value yet -> `○`.
- A corrected value ("make it fifty pounds", "invoice 4321 instead") -> `✓`."""

SUCCESS_COMPLETENESS_RULES = f"""

CURRENT QUESTION: does the caller want to pay another invoice?
- A clear yes or no -> `✓`.
- "yes, invoice..." followed by digit dictation: fewer than {INVOICE_MIN_LEN} digits
  so far means still dictating -> `○`.
- "yes, I want to pay" with no details yet is a COMPLETE answer to this question -> `✓`."""

# Fillers spoken the moment the LLM starts a slow function call — masks the
# lookup + response-generation latency. Only for functions that hit the network;
# instant functions (FAQ, pay_another, finish_call) get none, silence is better
# than "one moment" followed instantly by the answer.
_INVOICE_LOOKUP_FILLERS = [
    "One moment, let me find that invoice in our system.",
    "Let me check that for you.",
]
_CREATE_ORDER_FILLERS = [
    "One moment while I create your payment order.",
    "Just a second, setting that up now.",
]


class PaymentBot(BaseBot):
    """Payment-order bot on the shared BaseBot framework."""

    # Read by BaseBot when building the user aggregator: silence for this long after
    # the bot stops speaking fires on_user_turn_idle (the no-input path).
    IDLE_TIMEOUT_S = IDLE_TIMEOUT_S

    # Initial completeness rules (BaseBot hook); node transitions swap in the rules
    # relevant to that node via the "set_completeness_rules" pre-action.
    TURN_COMPLETION_GUIDANCE = COLLECT_COMPLETENESS_RULES


    def __init__(self, config: BotConfig):
        super().__init__(config)
        self.flow_manager: Optional[FlowManager] = None

        # No-input handling (code-decided): reprompt, then terminate at the cap.
        @self.context_aggregator.user().event_handler("on_user_turn_idle")
        async def _on_idle(_aggregator):
            await self._handle_idle()

        # Idle strikes measure CONSECUTIVE silence: any user speech clears the budget
        # (otherwise pauses accumulated across the whole call would trigger termination).
        @self.context_aggregator.user().event_handler("on_user_turn_started")
        async def _on_turn_started(_aggregator, _strategy):
            if self.flow_manager:
                self.flow_manager.state.pop("attempts_idle", None)

        # Fillers: speak an acknowledgement the moment a slow function call starts.
        @self.llm.event_handler("on_function_calls_started")
        async def _on_function_calls_started(_service, function_calls):
            for call in function_calls:
                args = call.arguments or {}
                if call.function_name == "collect_payment_details" and args.get(
                    "invoice_number"
                ):
                    # Only when a REAL lookup will happen: the LLM re-passes the known
                    # invoice number with every call, and "let me look that up" right
                    # before an instant reply sounds wrong.
                    known = (
                        self.flow_manager.state.get("invoice") if self.flow_manager else None
                    )
                    digits = _normalize_invoice(args["invoice_number"])
                    if known is not None and digits == known.number:
                        return
                    await self.tts.queue_frame(
                        TTSSpeakFrame(random.choice(_INVOICE_LOOKUP_FILLERS))
                    )
                    return
                if (
                    call.function_name == "confirm_payment"
                    and args.get("confirmed")
                    and not args.get("cancel")
                ):
                    await self.tts.queue_frame(
                        TTSSpeakFrame(random.choice(_CREATE_ORDER_FILLERS))
                    )
                    return

    async def _handle_idle(self):
        if not self.flow_manager:
            return
        state = self.flow_manager.state
        attempts = state.get("attempts_idle", 0) + 1
        state["attempts_idle"] = attempts
        if attempts >= MAX_ATTEMPTS:
            await self.flow_manager.set_node_from_config(create_terminate_node())
            return
        awaiting = state.get("awaiting", "invoice")
        logger.info(f"User idle (attempt {attempts}) while awaiting {awaiting}")
        if awaiting == "confirm":
            invoice, amount = state.get("invoice"), state.get("amount")
            if invoice and amount:
                node = create_confirm_node(invoice, amount)
            else:
                node = create_collect_node()
        elif awaiting == "amount":
            node = create_collect_node(
                "I'm sorry, I didn't catch that. How much would you like to pay?"
            )
        else:
            node = create_collect_node(
                "I'm sorry, I didn't catch that. May I have your invoice number please?"
            )
        await self.flow_manager.set_node_from_config(node)

    async def _handle_first_participant(self):
        """Initialize the flow when the caller connects."""
        self.flow_manager = FlowManager(
            worker=self.worker,
            llm=self.llm,
            context_aggregator=self.context_aggregator,
            # Available at EVERY node: general questions (working hours, what/where is
            # my invoice number, ...) get answered mid-task without leaving the node.
            global_functions=[get_business_info],
        )

        # Per-node completeness rules: nodes carry a set_completeness_rules pre-action
        # so the judge only holds the rules for the question currently being asked.
        async def _set_completeness_rules(action: dict, _flow_manager: FlowManager):
            self.set_turn_completion_rules(action.get("rules", ""))

        self.flow_manager.register_action("set_completeness_rules", _set_completeness_rules)
        self.flow_manager.state["awaiting"] = "invoice"
        await self.flow_manager.initialize(create_collect_node())
