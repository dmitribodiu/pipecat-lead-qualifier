"""Headless workers — the lookup/money half of the hybrid.

Each is a bus-only ``BaseWorker`` (no pipeline, no LLM, no audio). They do
work that does NOT need the caller on the line, so they can run in their own
process and scale independently of the single conversation. A ``@job``
handler is auto-collected by ``BaseWorker`` and dispatched when a matching
job request arrives; it replies with :meth:`send_job_response`.

Job contract:
    <bill_type>-worker   name="lookup"   payload={"reference"}
        -> {"found": bool, ...BillInfo fields}          (always COMPLETED)
    payments-worker      name="charge"   payload={bill_type, reference, amount}
        -> {"order_id"}  COMPLETED   |   {"error"}  FAILED (gateway down)

Pattern source: pipecat's own ``examples/multi-worker/code-assistant``
(bus-only ``BaseWorker``) and ``sensor-controller`` (job request/response).
"""

from loguru import logger

from pipecat.bus import BusJobRequestMessage
from pipecat.pipeline.job_context import JobStatus
from pipecat.pipeline.job_decorator import job
from pipecat.workers.base_worker import BaseWorker

from services import charge_card, lookup_bill


class LookupWorker(BaseWorker):
    """Resolves a bill balance for ONE bill type. Read-only, parallelisable."""

    def __init__(self, name: str, bill_type: str):
        """Initialize the lookup worker.

        Args:
            name: Unique worker name the concierge targets (e.g. ``parking-worker``).
            bill_type: Which table to read (``parking`` / ``water``).
        """
        super().__init__(name)
        self._bill_type = bill_type

    @job(name="lookup")
    async def on_lookup(self, message: BusJobRequestMessage) -> None:
        reference = str(message.payload.get("reference", ""))
        info = lookup_bill(self._bill_type, reference)
        if info is None:
            # A business "not found" is a normal COMPLETED result, NOT an error.
            logger.info(f"{self.name}: {reference!r} not found")
            await self.send_job_response(
                message.job_id, {"found": False, "reference": reference}
            )
            return
        logger.info(f"{self.name}: {reference} -> {info.amount_due:.2f} {info.currency}")
        await self.send_job_response(
            message.job_id,
            {
                "found": True,
                "bill_type": info.bill_type,
                "reference": info.reference,
                "payee": info.payee,
                "amount_due": info.amount_due,
                "currency": info.currency,
            },
        )


class PaymentsWorker(BaseWorker):
    """Shared money-move for every bill type — one place the gateway lives."""

    @job(name="charge")
    async def on_charge(self, message: BusJobRequestMessage) -> None:
        p = message.payload
        try:
            order_id = charge_card(p["bill_type"], p["reference"], float(p["amount"]))
        except Exception as e:
            # A gateway failure is a real error — the caller's job() raises JobError.
            logger.error(f"{self.name}: charge failed: {e}")
            await self.send_job_response(
                message.job_id, {"error": str(e)}, status=JobStatus.FAILED
            )
            return
        logger.info(
            f"{self.name}: charged {p['amount']} for {p['bill_type']} {p['reference']} -> {order_id}"
        )
        await self.send_job_response(message.job_id, {"order_id": order_id})
