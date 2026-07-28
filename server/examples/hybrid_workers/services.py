"""Mock domain backend for the hybrid-workers example.

Pure, synchronous business logic — the kind of work that has NO caller
attached and therefore belongs behind a job, not in the conversation.
Swap these for real DB / payment-gateway calls; the worker layer above
them (``workers.py``) stays identical.
"""

from dataclasses import dataclass


@dataclass
class BillInfo:
    """A resolved bill balance — bill-type-agnostic."""

    bill_type: str
    reference: str
    payee: str
    amount_due: float
    currency: str = "GBP"


# (reference -> (payee, amount_due)); same figures as the multiagent bot for continuity.
_PARKING = {
    "1001": ("City Parking", 54.30),
    "1002": ("City Parking", 25.00),
    "234": ("City Parking", 40.00),
    "9999": ("City Parking", 40.00),  # resolves fine, but charge_card fails on it (gateway demo)
}
_WATER = {
    "AB1234567": ("Anytown Water", 88.75),
    "2220": ("Anytown Water", 61.20),
}
_TABLES = {"parking": _PARKING, "water": _WATER}


def lookup_bill(bill_type: str, reference: str) -> BillInfo | None:
    """Resolve a balance, or ``None`` if the reference isn't on file."""
    row = _TABLES.get(bill_type, {}).get(str(reference))
    if row is None:
        return None
    payee, amount = row
    return BillInfo(bill_type=bill_type, reference=str(reference), payee=payee, amount_due=amount)


def charge_card(bill_type: str, reference: str, amount: float) -> str:
    """Pretend to move money; return an order id, or raise on a gateway failure.

    Reference ``9999`` simulates a gateway outage so the example can show the
    FAILED job path (a real error, distinct from a business "not found").
    """
    if str(reference) == "9999":
        raise RuntimeError("payment gateway timeout")
    return f"ORD-{bill_type[:2].upper()}-{reference}-{int(amount)}"
