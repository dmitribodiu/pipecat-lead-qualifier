# payment_editor — editor-controllable copy of the payment bot

A copy of `bots/payment.py`, reshaped so the **flow/routing** can be controlled from the
[Pipecat Flows visual editor](https://flows.pipecat.ai/). `bots/payment.py` is untouched.

| File | What it is |
|---|---|
| `payment_editor.py` | The running bot. Same behaviour as payment.py, editor-shaped. |
| `flow.json` | The flow graph in the editor's schema — **import this into the UI** to view/rewire routing. |

## The split of responsibilities

The editor controls the **graph** (which node follows which, on what condition). The Python
owns the **handler bodies** (invoice lookup, money limits, retry budgets) — the editor treats
those as opaque node internals.

Three things make that possible:

1. **Named, parameterless node factories** — `create_collect_invoice_node()`, `…amount…`,
   `…confirm…`, etc. No `prefix=` args (the editor calls nodes by id with no arguments).
   Dynamic spoken lines (balance read-back, retry re-prompts) are stashed in
   `state["announce"]` by the handler and spoken by the `announce` pre-action; each node's
   fixed opening is that action's `fallback`.
2. **One discrete `result` per routing handler** — a status string (`RESULT_*`), e.g.
   `ready_to_confirm`, `need_amount`, `invalid_invoice`, `retries_exhausted`. This is the
   variable the editor's conditions compare against.
3. **One decision block** — `_route()` maps `result` → `create_<id>_node()`. That is the
   `if/elif` the editor generates and re-imports; in `flow.json` it's the `decision.conditions`
   on each function.

## Routing (what you can rewire in the UI)

```
collect_invoice ──need_amount──▶ collect_amount ──ready_to_confirm──▶ confirm ──confirmed──▶ success
      ▲  ▲ invalid/not_found          │ ready_to_confirm                 │ cancelled            │ finish_call
      │  └──────────────(re-ask)──────┘                                  ▼                      ▼
      │                                                              goodbye ◀───────────── goodbye
      └── retries_exhausted (invoice ×3 / amount ×3) ──▶ terminate    payment_failed ──▶ api_failure
```

An amount volunteered on `collect_invoice` ("reference 343 and I wish to pay 100 pounds")
is captured in the **same** `collect_payment_details` call and routes straight to `confirm`,
skipping `collect_amount` — because the invoice node's function still takes both slots.

## Import into the editor

1. Open <https://flows.pipecat.ai/> → **Import** → choose `flow.json`.
2. You'll see the seven nodes and the decision edges. Rewire by editing a function's
   **conditions** (e.g. add a "partial payment" branch: on `amount < balance` route to a new
   confirm variant), add/rename nodes, or change openings.
3. **Export → JSON** to save your changes back to `flow.json`.

## Important — the round-trip is graph-only

The editor can rewire routing, node identities, and static prompts. It **cannot** author the
dynamic parts that live in `payment_editor.py`:

- the invoice **lookup** and the **balance** line built from it,
- the **money limits** and **retry budgets** (the `result` values `retries_exhausted` /
  `amount_out_of_range` come from handler code the canvas can't see).

So: **`flow.json` is the source of truth for the graph; `payment_editor.py` is the source of
truth for handler bodies.** If you *Export → Python* from the editor, treat it as scaffolding
to diff against — don't overwrite `payment_editor.py` with it, or you'll lose the lookup /
limits / budget logic. Change routing in the UI → re-import the JSON; change logic in the `.py`.

## Running it

It's a standalone module (not wired into `BOT_TYPE` dispatch). To try it, point a bot-type
case at `PaymentEditorBot` in `base_bot.py`/`main.py`, or import it directly. It uses the same
`config`, transport, and `.env` as the other bots (currently Claude via `LLM_PROVIDER=anthropic`).
