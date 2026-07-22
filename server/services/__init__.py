"""External service integrations package.

This package contains modules for interacting with external services and APIs.
Each module provides a typed interface and follows consistent patterns for:
- Configuration management
- Error handling
- Retry mechanisms
- Logging
"""

# NOTE: no eager submodule imports here. CalComAPI validates CALCOM_API_KEY at import
# time, which would break bots that don't use it — import service clients directly from
# their modules (e.g. `from services.calcom_api import CalComAPI`,
# `from services.invoice_api import MockInvoiceApi`).

__all__ = []
