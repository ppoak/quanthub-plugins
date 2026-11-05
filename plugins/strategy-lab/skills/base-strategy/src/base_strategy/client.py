"""Shared strategy-client contract for trader backends."""

from __future__ import annotations

from typing import Any


class BaseClient:
    """Base class for strategy intent or provider transport clients."""

    def buy(self, code: str, quantity: float, **kwargs: Any) -> Any:
        raise NotImplementedError

    def sell(self, code: str, quantity: float, **kwargs: Any) -> Any:
        raise NotImplementedError

    def cancel(self, order_or_id: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
