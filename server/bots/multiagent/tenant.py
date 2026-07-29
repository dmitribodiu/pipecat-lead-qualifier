"""Tenant-config helpers — turn the seeded config into what the flow SAYS.

The tenant's config row is seeded into ``flow_manager.state["tenant"]`` at connect
(see ``bot.py``). Every node builder reads its wording through these helpers rather
than hard-coding strings, so the SAME flow speaks differently per tenant:

    role_message(state)          -> the system/role message (assistant name + persona + brand)
    render(state, "greeting")    -> a Jinja-templated spoken line from the config
    money(amount, currency_code) -> "54.30 pounds" / "54.30 lei"
    term(state, "parking", "noun") -> "parking ticket" / "parking fine"

Why state-based and not a module global: bots run in-process, one per call, so a
module-level "current tenant" would race across concurrent calls. State is per-call.
"""

from __future__ import annotations

from jinja2 import Environment, StrictUndefined

# StrictUndefined: a typo in a tenant's template fails loudly in testing rather than
# silently rendering an empty string on a live call.
_env = Environment(undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True)


def cfg(state: dict) -> dict:
    """The tenant settings for this call (empty dict if not seeded — safe defaults apply)."""
    return state.get("tenant", {}) or {}


def _render_vars(state: dict) -> dict:
    """Variables a prompt template can reference: the tenant settings plus any scalar
    runtime values already in state (e.g. ``amount``, ``bill_type``)."""
    scalars = {k: v for k, v in state.items() if isinstance(v, (str, int, float, bool))}
    return {**cfg(state), **scalars}


def render(state: dict, key: str, **extra) -> str:
    """Render the tenant's ``prompts[key]`` Jinja template against config + state + extra."""
    template = cfg(state).get("prompts", {}).get(key, "")
    if not template:
        return ""
    return _env.from_string(template).render(**_render_vars(state), **extra)


def role_message(state: dict) -> dict:
    """Build the system/role message from the tenant's persona + branding.

    With the default tenant (empty ``tenant_display``) this reproduces the original
    static role text exactly.
    """
    c = cfg(state)
    name = c.get("assistant_name", "Marissa")
    persona = c.get("persona", "an automated payments assistant on a phone line")
    brand = c.get("tenant_display", "")
    lead = f"You are {name}, {persona}"
    if brand:
        lead += f" for {brand}"
    lead += "."
    return {
        "role": "system",
        "content": (
            f"{lead} Be brief, warm and conversational. Convert spoken numbers to digits. "
            "Never invent a value the caller did not provide, and never verbalize function "
            "parameters."
        ),
    }


# ── money ──────────────────────────────────────────────────────────────────────
_CURRENCY_WORDS = {
    "GBP": "pounds",
    "EUR": "euros",
    "USD": "dollars",
    "MDL": "lei",
    "RON": "lei",
}


def money_word(code: str) -> str:
    """Spoken currency word for a currency code ('GBP' -> 'pounds'); code itself if unknown."""
    return _CURRENCY_WORDS.get((code or "").upper(), code or "")


def money(amount, code: str) -> str:
    """Format an amount for TTS: ``money(54.3, "GBP")`` -> ``'54.30 pounds'``."""
    return f"{float(amount):.2f} {money_word(code)}".strip()


# ── terminology ─────────────────────────────────────────────────────────────────
def term(state: dict, bill_type: str, which: str) -> str:
    """Spoken term for a bill type: config override first, then the intent registry.

    Args:
        which: "noun" (e.g. "parking ticket") or "ref_word" (e.g. "ticket number").
    """
    bt_cfg = cfg(state).get("bill_types", {}).get(bill_type, {})
    if which in bt_cfg:
        return bt_cfg[which]
    # Fall back to the structural registry so terminology is optional in the config.
    from bots.multiagent.intent import BILL_TYPES

    bt = BILL_TYPES.get(bill_type)
    return getattr(bt, which, bill_type) if bt else bill_type


# ── per-slot validation rules ────────────────────────────────────────────────
_DEFAULT_MAX_ATTEMPTS = 3


def slot_rules(state: dict, bill_type: str, slot: str) -> dict:
    """Validation rules for one collection slot.

    Read from ``config.bill_types[bill_type].slots[slot]``; ``max_attempts`` falls back
    to the tenant-level ``max_attempts`` then a global default. Missing min/max = no bound.
    Example config:  "slots": {"amount": {"min": 1, "max": 500, "max_attempts": 3}}
    """
    r = (
        cfg(state)
        .get("bill_types", {})
        .get(bill_type, {})
        .get("slots", {})
        .get(slot, {})
    )
    return {
        "min": r.get("min"),
        "max": r.get("max"),
        "max_attempts": r.get(
            "max_attempts", cfg(state).get("max_attempts", _DEFAULT_MAX_ATTEMPTS)
        ),
    }


def validate_slot(slot: str, value, rules: dict):
    """Return (ok, reason). Only value-ranged slots (amount) are checked today; extend here."""
    if slot == "amount":
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False, "not_a_number"
        if rules.get("min") is not None and v < rules["min"]:
            return False, "too_low"
        if rules.get("max") is not None and v > rules["max"]:
            return False, "too_high"
    return True, None


def retry_message(state: dict, slot: str, reason: str, rules: dict) -> str:
    """Corrective sentence spoken before re-asking a slot that failed validation."""
    currency = cfg(state).get("currency", "GBP")
    if slot == "amount":
        if reason == "too_low":
            return f"That's below the minimum of {money(rules['min'], currency)}."
        if reason == "too_high":
            return f"That's above the maximum of {money(rules['max'], currency)}."
        return "That amount doesn't look valid."
    return "That value wasn't valid."


# ── per-tenant capabilities (which menu options this tenant offers) ───────────
def capabilities(state: dict) -> dict:
    """The tenant's allowed operations: ``{bill_type: [actions]}`` (actions: pay|inquire)."""
    return cfg(state).get("capabilities") or {}


def enabled_bill_types(state: dict) -> list:
    """Bill types this tenant offers at all (any action). Unconfigured => all registered."""
    caps = capabilities(state)
    if caps:
        return [bt for bt, actions in caps.items() if actions]
    from bots.multiagent.intent import BILL_TYPES

    return list(BILL_TYPES)


def is_allowed(state: dict, action: str, bill_type: str) -> bool:
    """Whether this tenant offers ``action`` on ``bill_type``. Unconfigured => allow all."""
    caps = capabilities(state)
    if not caps:
        return True
    return action in (caps.get(bill_type) or [])
