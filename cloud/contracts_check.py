"""Validate a lease against the shared schema; raises InvalidLeaseError (kept apart so modules need not import the orchestrator)."""

import contracts


def require_valid(lease: dict) -> None:
    from .orchestrator import InvalidLeaseError
    errors = contracts.validate_document("lease", lease)
    if errors:
        raise InvalidLeaseError(errors)
