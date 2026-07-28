"""Concierge-side dispatch — the ONE line that crosses the boundary.

Call these from the conversational agent once collection is complete.
``worker`` is the running conversational worker on the bus:

- inside a Pipecat LLM tool / flow function it's ``params.pipeline_worker``
- inside ``run_demo.py`` it's the ``ConciergeDriver`` itself

Both open a single-worker job, wait for the response, and return a plain
dict. A gateway failure surfaces as ``JobError`` (charge sent FAILED); a
business "not found" comes back as a normal ``{"found": False}`` dict.
"""

from pipecat.workers.base_worker import BaseWorker


async def lookup_bill_job(
    worker: BaseWorker, *, bill_type: str, reference: str, timeout: float = 15
) -> dict:
    """Ask the per-bill-type lookup worker for a balance. Never raises on not-found."""
    async with worker.job(
        f"{bill_type}-worker",
        name="lookup",
        payload={"reference": reference},
        timeout=timeout,
    ) as t:
        pass
    return t.response


async def charge_bill_job(
    worker: BaseWorker,
    *,
    bill_type: str,
    reference: str,
    amount: float,
    currency: str = "GBP",
    timeout: float = 30,
) -> dict:
    """Charge via the shared payments worker. Raises ``JobError`` if the gateway fails."""
    async with worker.job(
        "payments-worker",
        name="charge",
        payload={
            "bill_type": bill_type,
            "reference": reference,
            "amount": amount,
            "currency": currency,
        },
        timeout=timeout,
    ) as t:
        pass
    return t.response
