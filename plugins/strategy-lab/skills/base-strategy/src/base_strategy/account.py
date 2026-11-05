"""Shared account-state contract for trader backends."""

from __future__ import annotations

from typing import Any


class BaseAccount:
    """Base class for mutable account state owned by an exchange."""

    account_id: str
    init_cash: float
    cash: float
    positions: dict[str, Any]
