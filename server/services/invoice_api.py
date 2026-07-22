"""Invoice and payment-order API clients.

The bot only ever talks to the abstract interfaces; swap the mock implementations
for real REST clients (aiohttp) without touching the flow. The unhappy path is
expressed as data (``None`` / ``status`` fields), not exceptions — handlers route
on it (see restaurant_reservation.py in the pipecat flows examples).
"""

import asyncio
import itertools
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class Invoice:
    """An invoice as returned by the invoice API."""

    number: str  # digits-only reference, e.g. "222"
    payee: str
    amount_due: float
    currency: str = "GBP"


@dataclass
class PaymentOrderResult:
    """Result of creating a payment order."""

    ok: bool
    order_id: str = ""
    error: str = ""


class InvoiceApi(ABC):
    """Lookup interface for invoices."""

    @abstractmethod
    async def get_invoice(self, number: str) -> Optional[Invoice]:
        """Return the invoice for ``number``, or None if it does not exist."""


class PaymentApi(ABC):
    """Interface for creating payment orders."""

    @abstractmethod
    async def create_payment_order(self, invoice_number: str, amount: float) -> PaymentOrderResult:
        """Create a payment order for ``amount`` against ``invoice_number``."""


class MockInvoiceApi(InvoiceApi):
    """In-memory invoice store for development and phone testing."""

    _INVOICES = {
        "222": Invoice(number="222", payee="Acme Ltd", amount_due=120.00),
        "1001": Invoice(number="1001", payee="Northwind Traders", amount_due=54.30),
        "4321": Invoice(number="4321", payee="Globex Corporation", amount_due=999.99),
        "55555": Invoice(number="55555", payee="Initech", amount_due=42.00),
    }

    async def get_invoice(self, number: str) -> Optional[Invoice]:
        await asyncio.sleep(0.2)  # simulate REST latency
        invoice = self._INVOICES.get(number)
        logger.debug(f"MockInvoiceApi.get_invoice({number!r}) -> {invoice}")
        return invoice


class MockPaymentApi(PaymentApi):
    """In-memory payment-order creator for development and phone testing."""

    _ids = itertools.count(1)

    async def create_payment_order(self, invoice_number: str, amount: float) -> PaymentOrderResult:
        await asyncio.sleep(0.3)  # simulate REST latency
        order_id = f"PO-{next(self._ids):05d}"
        logger.info(f"MockPaymentApi: created {order_id} ({amount:.2f} for invoice {invoice_number})")
        return PaymentOrderResult(ok=True, order_id=order_id)
