"""Data contracts shared between the agents (the anti-corruption boundary).

Agents are sub-flows; these dataclasses are what flows BETWEEN them through
``flow_manager.state``. Divergent collection agents each produce a normalised
``PaymentIntent``; the one shared Settlement consumes it without knowing which
agent produced it. Same idea as AudioForkSerializer between FreeSWITCH and frames.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


# ── lookup / payment contracts ────────────────────────────────────────────────
@dataclass
class BillInfo:
    """Result of a lookup — what inquiry and settlement need, bill-type-agnostic."""

    bill_type: str
    reference: str
    payee: str
    amount_due: float
    currency: str = "GBP"
    extra: dict = field(default_factory=dict)


@dataclass
class PaymentIntent:
    """Normalised "pay this" produced by a collection agent, consumed by Settlement."""

    bill_type: str
    reference: str
    amount: float
    payee: str
    currency: str = "GBP"
    extra: dict = field(default_factory=dict)  # bill-specific (meter reading, reg) — settlement ignores


# ── orchestration contract ────────────────────────────────────────────────────
@dataclass
class QueueItem:
    """One thing the caller asked for, parsed up front by the Concierge.

    The queue holds a mix of these — some read (inquire), some write (pay).
    """

    action: str  # "pay" | "inquire"
    bill_type: Optional[str] = None  # "parking" | "water" | None (ask)
    reference: Optional[str] = None
    amount: Optional[float] = None


# ── collection-step contract ──────────────────────────────────────────────────
@dataclass
class CollectStep:
    """One question in a collection agent.

    Self-documents its own help so "what is this / where do I find it" is answered
    inline from ``help`` — the caller never leaves the node, so state is preserved
    for free. ``branches`` models bot-initiated disambiguation (new vs old ticket).
    """

    slot: str  # where the answer lands in the PaymentIntent / state
    ask: str  # the question the bot asks
    help: str = ""  # answer to "what is this / where do I find it"
    example: str = ""  # "e.g. 1234567890"
    validate: Optional[Callable[[str], bool]] = None
    branches: Optional[dict] = None  # {"new": [CollectStep...], "old": [...]} — optional


# ── per-bill-type config + registry ───────────────────────────────────────────
@dataclass
class BillType:
    """Per-bill-type config: terminology + the divergent collection step list.

    ``steps`` is the ONLY place parking and water differ structurally. The lookup
    and payment execution are shared (services.py); settlement is shared.
    """

    key: str  # "parking"
    noun: str  # "parking ticket"
    ref_word: str  # "ticket number"
    steps: list  # list[CollectStep] — parking has fewer, water has more
    allow_partial: bool = False  # water may allow part-payment; parking may not


# TODO(you): replace these placeholder steps with the real ones. Parking = 3 steps,
# water = 5 steps, per your requirements. `help`/`branches` are where step-local help
# and new/old-style disambiguation live.
PARKING = BillType(
    key="parking",
    noun="parking ticket",
    ref_word="ticket number",
    allow_partial=False,
    steps=[
        CollectStep(
            slot="reference",
            ask="Is this a new or an older ticket?",
            help="It tells me where your number is printed.",
            branches={
                "new": [CollectStep(slot="reference", ask="What's the 10-digit number, top-right of the ticket?",
                                    help="New tickets: 10-digit code, top-right corner.", example="1234567890")],
                "old": [CollectStep(slot="reference", ask="What's the 8-digit number, bottom-left of the ticket?",
                                    help="Older tickets: 8-digit code, bottom-left.", example="12345678")],
            },
        ),
        CollectStep(slot="amount", ask="How much would you like to pay?",
                    help="The amount in pounds; parking fines are paid in full."),
    ],
)

WATER = BillType(
    key="water",
    noun="water bill",
    ref_word="account number",
    allow_partial=True,
    steps=[
        CollectStep(slot="reference", ask="What's your water account number?",
                    help="It's on the top of your bill, next to your name.", example="AB1234567"),
        CollectStep(slot="meter_reading", ask="What's your current meter reading?",
                    help="The black digits on your meter dial (ignore the red ones)."),
        CollectStep(slot="tariff", ask="Are you on a metered or fixed tariff?",
                    help="Metered = you pay for what you use; fixed = a flat charge."),
        CollectStep(slot="amount", ask="How much would you like to pay?",
                    help="You can pay all or part of the outstanding balance."),
        CollectStep(slot="account_holder", ask="And your name, as the account holder?",
                    help="The name the account is registered under."),
    ],
)

BILL_TYPES = {"parking": PARKING, "water": WATER}


# ── queue helpers (thin wrappers over flow_manager.state) ─────────────────────
def enqueue(state: dict, items: list) -> None:
    """Append parsed QueueItems to the caller's queue."""
    state.setdefault("queue", []).extend(items)


def next_item(state: dict) -> Optional[QueueItem]:
    """Pop the next thing to handle, or None when the queue is drained."""
    q = state.get("queue") or []
    return q.pop(0) if q else None
