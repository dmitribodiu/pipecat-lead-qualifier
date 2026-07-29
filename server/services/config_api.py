"""Mock tenant Configuration API — the seam that makes the conversation tenant-driven.

In production this is one HTTP/DB call keyed by the dialed number (DID): resolve which
tenant a call belongs to, then load that tenant's config row. Here it's an in-memory
mock with the SAME async shape, so swapping in a real client later is a body change,
not a call-site change.

Everything the flow SAYS or GATES on lives in the returned config — prompts, currency,
terminology, limits, flags. No code change is needed to onboard or retune a tenant:
edit the config (a portal edit against the store) and the conversation changes.

Concurrency note: bots run in-process, one per call, so tenant config must NOT be a
module global — the loaded config is seeded into each call's ``flow_manager.state``
(see ``bots/multiagent/bot.py``). This module only reads the store; it holds no
per-call state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from loguru import logger


@dataclass
class TenantConfig:
    """One tenant's resolved configuration row.

    ``settings`` is the flat namespace seeded into ``flow_manager.state["tenant"]`` and
    used by the render/role/money/term helpers in ``bots.multiagent.tenant``.
    """

    tenant_id: str
    settings: dict

    def get(self, key, default=None):
        return self.settings.get(key, default)


# ── the "store": one row per tenant ───────────────────────────────────────────
# Two deliberately divergent tenants, to prove the flow is data-driven with zero code
# changes between them:
#   default        - UK county council: GBP, English, parking tickets + water bills.
#                    Its wording reproduces the previous hard-coded behaviour exactly.
#   mdl_municipal  - Moldovan municipality: MDL (lei), parking fines + utility bills,
#                    a different assistant persona and business facts.
_TENANTS: dict[str, dict] = {
    "default": {
        "tenant_display": "",  # empty => role/greeting read as the original wording
        "assistant_name": "Marissa",
        "persona": "an automated payments assistant on a phone line",
        "currency": "GBP",
        "locale": "en-GB",
        # A single blurb keeps the greeting a one-line portal edit.
        "services_blurb": "I can look up a bill or take a payment for parking "
        "tickets or water bills.",
        "business_faq": (
            "Opening hours: 9am to 5pm, Monday to Friday. "
            "Contact: 0800 123 4567 or help@example.com. "
            "We take card payments for parking tickets and water bills over the phone."
        ),
        # Per-bill-type spoken terminology + per-slot validation rules. `slots` gates the
        # collection fields: amount min/max and how many tries before we give up.
        "bill_types": {
            "parking": {
                "noun": "parking ticket",
                "ref_word": "ticket number",
                "slots": {
                    "amount": {"min": 1, "max": 1000, "max_attempts": 3},
                    "reference": {"max_attempts": 3},
                },
            },
            "water": {
                "noun": "water bill",
                "ref_word": "account number",
                "slots": {
                    "amount": {"min": 1, "max": 2000, "max_attempts": 3},
                    "reference": {"max_attempts": 3},
                },
            },
        },
        # What this tenant offers, per bill type: "pay" and/or "inquire" (balance lookup).
        # Anything not listed here is declined by the concierge.
        "capabilities": {
            "parking": ["pay", "inquire"],
            "water": ["pay", "inquire"],
        },
        # Tenant-level fallback for max_attempts when a slot doesn't set its own.
        "amount_limit": 100000,
        "max_attempts": 3,
        # Prompt templates (Jinja). Rendered against the tenant settings + runtime state,
        # so a tenant can add variables/conditionals here and change the wording per call.
        "prompts": {
            "greeting": "Welcome to our payments line. {{ services_blurb }} "
            "What would you like to do?",
            "menu": "Is there anything else I can help you with?",
            "goodbye": "Thank you for using our payments line. Goodbye.",
        },
    },
    "mdl_municipal": {
        "tenant_display": "Chisinau Municipal Services",
        "assistant_name": "Ana",
        "persona": "an automated payments assistant",
        "currency": "MDL",
        "locale": "en-MD",
        "services_blurb": "I can check a balance or take a payment for parking "
        "fines or utility bills.",
        "business_faq": (
            "Program: 8am to 4pm, Monday to Friday. "
            "Contact: 022 000 000 or help@chisinau.md. "
            "We take card payments for parking fines and utility bills over the phone."
        ),
        "bill_types": {
            "parking": {
                "noun": "parking fine",
                "ref_word": "fine number",
                "slots": {
                    "amount": {"min": 1, "max": 5000, "max_attempts": 2},
                    "reference": {"max_attempts": 2},
                },
            },
            "water": {
                "noun": "utility bill",
                "ref_word": "account number",
                "slots": {
                    "amount": {"min": 1, "max": 5000, "max_attempts": 3},
                    "reference": {"max_attempts": 3},
                },
            },
        },
        # This municipality takes payments for both, but only offers balance lookups
        # ("provide invoice information") for parking fines — water inquiries are declined.
        "capabilities": {
            "parking": ["pay", "inquire"],
            "water": ["pay"],
        },
        "amount_limit": 5000,
        "max_attempts": 3,
        "prompts": {
            "greeting": "Welcome to {{ tenant_display }}. {{ services_blurb }} "
            "What would you like to do?",
            "menu": "Is there anything else I can help you with?",
            "goodbye": "Thank you for calling {{ tenant_display }}. Goodbye.",
        },
    },
}

# DID (dialed number) -> tenant. Mock of a routing table; the real one is a lookup.
_DID_ROUTES: dict[str, str] = {
    "+441234567890": "default",
    "+37322000000": "mdl_municipal",
}

_FALLBACK_TENANT = "default"


class MockConfigurationApi:
    """Async, network-shaped mock of the real Configuration API.

    Keep the method signatures when you swap in the real client — the flow depends on
    the return contract (a ``TenantConfig``), not on where the data comes from.
    """

    async def resolve_tenant(self, did: Optional[str] = None) -> str:
        """Map a call to a tenant id.

        Resolution order: explicit DID route -> ``TENANT_ID`` env override (handy for
        local runs / a single-tenant deployment) -> fallback tenant.
        """
        if did and did in _DID_ROUTES:
            return _DID_ROUTES[did]
        env_tenant = os.getenv("TENANT_ID")
        if env_tenant in _TENANTS:
            return env_tenant
        if env_tenant:
            logger.warning(f"TENANT_ID={env_tenant!r} not in store; using {_FALLBACK_TENANT!r}")
        return _FALLBACK_TENANT

    async def get_config(self, tenant_id: str) -> TenantConfig:
        """Load one tenant's config row (falls back if the id is unknown)."""
        row = _TENANTS.get(tenant_id)
        if row is None:
            logger.warning(f"tenant {tenant_id!r} not found; using {_FALLBACK_TENANT!r}")
            tenant_id = _FALLBACK_TENANT
            row = _TENANTS[_FALLBACK_TENANT]
        # Return a shallow copy so a call can mutate its own state without touching the store.
        return TenantConfig(tenant_id=tenant_id, settings=dict(row))

    async def load_for_call(self, did: Optional[str] = None) -> TenantConfig:
        """Resolve the tenant for this call and load its config — the one call the bot makes."""
        tenant_id = await self.resolve_tenant(did)
        cfg = await self.get_config(tenant_id)
        logger.info(f"tenant config loaded: {cfg.tenant_id} (did={did!r})")
        return cfg


# Module singleton — cheap to import, holds no per-call state.
config_api = MockConfigurationApi()
