"""Keep local fee amounts and Xueqiu's per-mille rates in agreement."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from itertools import product

from .errors import XueqiuAPIError
from .utils import cash_amount


_PER_MILLE = Decimal(1000)
_RATE_TICK = Decimal("0.0001")
_FEE_FIELDS = ("commission", "tax")


def fee_amount(value, rate, gross: Decimal, name: str) -> Decimal:
    """Resolve a local fee as an exact Decimal amount."""
    if value is None:
        rate = Decimal(str(rate))
        if not rate.is_finite() or rate < 0:
            raise ValueError(f"Execution {name} rate must be finite and non-negative")
        value = rate * gross / _PER_MILLE
    elif isinstance(value, float):
        value = format(value, ".15g")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"Execution {name} must be finite and non-negative")
    return amount


def wire_rates(gross: Decimal, amounts: dict, *, buy: bool, balance: Decimal) -> dict:
    """Choose representable rates that minimize cumulative sub-cent cash drift."""
    net = -gross if buy else gross
    expected = cash_amount(balance + net - sum(amounts.values()))
    choices = []
    for name in _FEE_FIELDS:
        exact = amounts[name] * _PER_MILLE / gross
        rates = {exact.quantize(_RATE_TICK, rounding=mode) for mode in (ROUND_FLOOR, ROUND_CEILING)}
        valid = sorted(
            rate for rate in rates
            if cash_amount(gross * rate / _PER_MILLE) == cash_amount(amounts[name])
        )
        if not valid:
            raise XueqiuAPIError(f"Xueqiu rate precision cannot represent {name} of {amounts[name]}")
        choices.append(valid)

    def remote_balance(pair):
        return cash_amount(balance + net - gross * sum(pair) / _PER_MILLE)

    pair = min(product(*choices), key=lambda rates: abs(remote_balance(rates) - expected))
    if remote_balance(pair) != expected:
        raise XueqiuAPIError("Xueqiu rate precision cannot preserve the local cash balance")
    return {f"{name}_rate": float(rate) for name, rate in zip(_FEE_FIELDS, pair)}


def confirmed_fees(data, *, gross: Decimal, expected: dict, explicit: list[str], rates: dict) -> tuple[dict, Decimal]:
    """Check fee-bearing acknowledgements and return amounts and wire cost."""
    amounts = {}
    wire_cost = Decimal(0)
    for name in _FEE_FIELDS:
        rate_key = f"{name}_rate"
        submitted_rate = Decimal(str(rates[rate_key]))
        reported_rate = data.get(rate_key)
        if reported_rate is not None and Decimal(str(reported_rate)) != submitted_rate:
            raise XueqiuAPIError(f"Xueqiu changed the submitted {rate_key}")
        wire = gross * submitted_rate / _PER_MILLE
        amount = data.get(name)
        if amount is None:
            if cash_amount(wire) != cash_amount(expected[name]):
                raise XueqiuAPIError(f"Xueqiu {name} rate does not match the local fee")
            amounts[name] = expected[name]
        else:
            confirmed = Decimal(str(amount))
            amounts[name] = fee_amount(confirmed, None, gross, name)
            if name in explicit and amounts[name] != expected[name]:
                raise XueqiuAPIError(f"Xueqiu confirmed {name} differs from the local fee")
            if reported_rate is None:
                wire = confirmed
            elif cash_amount(wire) != cash_amount(amounts[name]):
                raise XueqiuAPIError(f"Xueqiu confirmed {name} and rate do not agree")
        wire_cost += wire
    return amounts, wire_cost
