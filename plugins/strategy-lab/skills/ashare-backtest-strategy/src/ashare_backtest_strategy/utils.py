"""A-share broker utility components."""

from __future__ import annotations

from .models import AShareOrder


class FixedRateCommission:
    """Fixed-rate commission model.

    Parameters
    ----------
    rate:
        Proportional commission rate applied to trade notional.
    min_commission:
        Minimum commission charged per order side.
    stamp_duty:
        Extra proportional duty charged on sells.

    Notes
    -----
    Pass an instance as ``AShareExchange(commission=FixedRateCommission(...))``.
    Any custom callable with signature ``(order, price, qty) -> float`` can be
    used instead.
    """

    def __init__(
        self,
        rate: float = 0.0003,
        min_commission: float = 5.0,
        stamp_duty: float = 0.0,
    ):
        self.rate = rate
        self.min_commission = min_commission
        self.stamp_duty = stamp_duty

    def __call__(self, order: AShareOrder, price: float, qty: float) -> float:
        amount = qty * price
        commission = max(amount * self.rate, self.min_commission)
        if order.side == AShareOrder.SELL:
            commission += amount * self.stamp_duty
        return commission

    def __str__(self) -> str:
        return (
            f"{self.__class__.__name__}: "
            f"rate={self.rate}, stamp_duty={self.stamp_duty}, min={self.min_commission}"
        )

    def __repr__(self) -> str:
        return self.__str__()


class VolumeShareSlippage:
    """Volume-proportional local slippage model.

    The broker injects a ``base_px`` into each bar before calling this model.
    Buys slip from base price toward the high price. Sells slip from base price
    toward the low price. If bar volume is missing or zero, no slippage is
    applied.
    """

    def __init__(self, bar_key: str = "base_px"):
        self.bar_key = bar_key

    def __call__(self, order: AShareOrder, bar: dict) -> float:
        base_px = bar.get(self.bar_key, bar.get("open", bar.get("close", 0.0)))
        high_px = bar.get("high", base_px)
        low_px = bar.get("low", base_px)
        volume = bar.get("volume", 0)
        bound = high_px if order.side == AShareOrder.BUY else low_px

        if volume > 0 and bound != base_px:
            vol_ratio = min(order.remaining, volume) / volume
            return base_px + (bound - base_px) * vol_ratio
        return base_px

    def __str__(self) -> str:
        return f"{self.__class__.__name__}(bar_key={self.bar_key!r})"

    def __repr__(self) -> str:
        return self.__str__()
