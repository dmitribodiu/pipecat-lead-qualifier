"""Shared services: the Lookup tier and payment execution.

Lookup (reference -> BillInfo) is called by BOTH inquiry and collection — write the
per-bill-type API access ONCE, use it for reads and payments. Payment execution is
the money-move's backend, called only by Settlement.

Everything here is a mock; swap the bodies for real APIs (async DB / HTTP). Keep the
signatures — the agents depend on the return contracts, not the implementations.
"""

from typing import Optional

from loguru import logger

from bots.multiagent.intent import BillInfo


# TODO(you): replace with real per-bill-type lookups (async DB / HTTP client).
_MOCK_PARKING = {
    "1001": BillInfo("parking", "1001", "City Parking", 54.30),
    "1002": BillInfo("parking", "1002", "City Parking", 25.00),
    "1003": BillInfo("parking", "1003", "City Parking", 12.50),
    "234": BillInfo("parking", "234", "City Parking", 40.00),
}
_MOCK_WATER = {
    "AB1234567": BillInfo("water", "AB1234567", "Northwind Water", 88.75),
    "2220": BillInfo("water", "2220", "Northwind Water", 61.20),
}


async def lookup(bill_type: str, reference: str) -> Optional[BillInfo]:
    """Resolve a reference to a BillInfo, or None if not found.

    Dispatches to the per-bill-type source. This is the single shared read path.
    """
    table = {"parking": _MOCK_PARKING, "water": _MOCK_WATER}.get(bill_type, {})
    info = table.get(str(reference).strip())
    logger.debug(f"lookup({bill_type}, {reference!r}) -> {info}")
    return info


class PaymentResult:
    """Outcome of a payment execution."""

    def __init__(self, ok: bool, order_id: str = "", error: str = ""):
        self.ok, self.order_id, self.error = ok, order_id, error


# TODO(you): replace with the real payment order API (idempotent — never double-charge).
async def execute_payment(bill_type: str, reference: str, amount: float) -> PaymentResult:
    """Create the payment order (the actual money-move). Mock always succeeds."""
    order_id = f"{bill_type[:3].upper()}{reference}"
    logger.info(f"execute_payment({bill_type}, {reference}, {amount:.2f}) -> order {order_id}")
    return PaymentResult(ok=True, order_id=order_id)
