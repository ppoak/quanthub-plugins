"""Exchange abstraction for trader backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseExchange(ABC):
    """Minimal exchange capability used by brokers.

    An exchange accepts order intent, owns matching or remote execution
    interaction, and returns execution facts or order status updates. Local
    exchanges simulate this behavior; remote exchanges delegate it to provider
    APIs.
    """

    @property
    @abstractmethod
    def order_queue(self) -> list[Any]:
        """Return accepted orders that have not reached a terminal status."""


def is_exchange(value: Any) -> bool:
    """Return whether ``value`` is a ``BaseExchange`` instance."""
    return isinstance(value, BaseExchange)
