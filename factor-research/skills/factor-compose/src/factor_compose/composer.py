"""Fit and predict through one model interface, including model ensembles."""
from __future__ import annotations
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
import numpy as np
import pandas as pd
from .util import parameters


@dataclass
class Context:
    as_of: pd.Timestamp
    features: tuple[str, ...]
    target: str = "ret"
    validation: pd.DataFrame | None = None
    weights: np.ndarray | None = None
    seed: int = 0


def signals(result, data):
    if not isinstance(result, pd.DataFrame) or {"date", "code", "score"} - set(result):
        raise TypeError("prediction must return a DataFrame with date, code and score")
    keys = data[["date", "code"]]
    if result.duplicated(["date", "code"]).any() or len(result) != len(keys):
        raise ValueError("prediction keys differ from input")
    merged = keys.merge(result, on=["date", "code"], how="left", validate="one_to_one", indicator=True)
    if (merged["_merge"] != "both").any() or np.isinf(merged["score"].to_numpy(dtype=float)).any():
        raise ValueError("prediction keys or finite score contract violated")
    return merged.drop(columns="_merge")


class Composer(ABC):
    libraries = ("factor-compose", "numpy", "pandas")

    @abstractmethod
    def fit(self, data, *, context):
        """Fit prepared training data using the supplied time boundary."""

    @abstractmethod
    def predict(self, data):
        """Return date/code/score rows with the same keys as the input."""

    def parameters(self):
        return parameters(self)

    def dependencies(self):
        return self.libraries

    def configure(self, params):
        model = deepcopy(self)
        for key, value in params.items():
            if key.startswith("_") or key.endswith("_") or not hasattr(model, key):
                raise ValueError(f"unknown model parameter: {key}")
            if isinstance(value, dict) and isinstance(getattr(model, key), dict):
                value = {**getattr(model, key), **value}
            setattr(model, key, value)
        return model

    def close(self):
        """Release model resources when required."""
