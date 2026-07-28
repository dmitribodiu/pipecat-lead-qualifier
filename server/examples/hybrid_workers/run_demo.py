"""Runnable, audio-free demo of the hybrid worker layer.

Starts the three headless workers plus a ``ConciergeDriver`` that plays a few
scripted requests through the SAME job dispatch the real voice bot would use
(``dispatch.py``) — so you can watch the Workers + Bus + Jobs handoff in the
logs on native Windows, with no audio, no API keys, no FreeSWITCH.

    cd server/examples/hybrid_workers
    python run_demo.py
"""

import asyncio

from loguru import logger

from pipecat.pipeline.job_context import JobError
from pipecat.workers.base_worker import BaseWorker
from pipecat.workers.runner import WorkerRunner

from dispatch import charge_bill_job, lookup_bill_job
from workers import LookupWorker, PaymentsWorker

# What the caller "said". In the real bot these come from the conversation; here
# they're scripted so the run is deterministic and repeatable.
SCENARIOS = [
    {"bill_type": "parking", "reference": "1001", "amount": 34},    # happy path, partial pay
    {"bill_type": "water", "reference": "2220", "amount": 61.20},   # different bill type, same payments worker
    {"bill_type": "parking", "reference": "7777", "amount": 10},    # lookup returns not-found
    {"bill_type": "parking", "reference": "9999", "amount": 20},    # gateway FAILED path
]


class ConciergeDriver(BaseWorker):
    """Stands in for the conversational Concierge — but with no audio.

    In the real bot this logic lives in an LLM tool / flow function and
    ``worker`` is ``params.pipeline_worker``. Here the driver IS the worker on
    the bus, so it calls ``dispatch`` with ``self``.
    """

    def __init__(self, name: str = "concierge", *, runner: WorkerRunner):
        super().__init__(name)
        self._runner = runner
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        await super().start()
        self._task = self.create_task(self._run(), "scenarios")

    async def stop(self) -> None:
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().stop()

    async def _run(self) -> None:
        for s in SCENARIOS:
            await self._pay(**s)
        logger.info("all scenarios done — shutting down")
        await self._runner.cancel()

    async def _pay(self, *, bill_type: str, reference: str, amount: float) -> None:
        logger.info(f"── caller: pay {bill_type} {reference}, £{amount}")

        # 1) headless lookup (needs no caller) -> job
        found = await lookup_bill_job(self, bill_type=bill_type, reference=reference)
        if not found.get("found"):
            logger.info(f"   concierge would say: I couldn't find {bill_type} {reference}.")
            return
        logger.info(f"   balance is £{found['amount_due']:.2f} for {found['payee']}")

        # 2) headless charge (needs no caller) -> job
        try:
            result = await charge_bill_job(self, bill_type=bill_type, reference=reference, amount=amount)
        except JobError as e:
            logger.info(f"   concierge would say: sorry, the payment didn't go through ({e}).")
            return
        if "order_id" not in result:  # belt-and-braces if FAILED didn't raise
            logger.info(f"   concierge would say: sorry, the payment failed ({result.get('error')}).")
            return
        logger.info(f"   concierge would say: paid £{amount}. Your reference is {result['order_id']}.")


async def main() -> None:
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(
        LookupWorker("parking-worker", "parking"),
        LookupWorker("water-worker", "water"),
        PaymentsWorker("payments-worker"),
        ConciergeDriver(runner=runner),
    )
    await runner.run()


if __name__ == "__main__":
    asyncio.run(main())
