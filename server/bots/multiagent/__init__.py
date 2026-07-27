"""Multi-agent payment bot (scaffold).

A COPY of the single-file `bots/payment.py` flow, decomposed into agent-scoped
sub-flows within one pipeline / one FlowManager (not separate processes). See
`bot.py` for the entry point and the module map below.

Tiers / files:
    intent.py      - data contracts (PaymentIntent, BillInfo, QueueItem, CollectStep)
                     and the per-bill-type registry. The anti-corruption boundary
                     that lets divergent collection agents feed one settlement.
    services.py    - shared Lookup tier (ref -> BillInfo) + payment execution. Mock.
    faq.py         - global FAQ service (business facts), available everywhere.
    concierge.py   - orchestration: greet, NLU -> typed-intent queue, dispatch, cancel.
    inquiry.py     - shared read flow: get ref -> lookup -> post-lookup junction.
    collection.py  - divergent write flows: ParkingAgent (few steps), WaterAgent (more),
                     on a shared base (cancel, explain-current-field, -> settlement).
    settlement.py  - shared money-move: junction -> confirm -> execute -> success.

Cross-agent handoffs use lazy imports inside handler functions to avoid import
cycles (concierge <-> collection <-> settlement <-> concierge).

Scaffold status: contracts + wiring are real and import-clean; collection steps and
the lookup/payment APIs are placeholders marked TODO — swap in the real ones.
"""
