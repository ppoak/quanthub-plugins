"""Import, construct and serialize configured research objects."""
from datetime import date
import importlib
import inspect
from pathlib import Path
import numpy as np
import pandas as pd


def location(value):
    cls = value if isinstance(value, type) else type(value)
    if cls.__module__ == "__main__" or "<locals>" in cls.__qualname__:
        raise ValueError("persistent components must be defined in an importable module")
    return f"{cls.__module__}:{cls.__qualname__}"


def resolve(path):
    module, name = path.split(":", 1)
    value = importlib.import_module(module)
    for part in name.split("."):
        value = getattr(value, part)
    return value


def configuration(value):
    """Serialize object classes and constructor parameters for run records."""
    if not isinstance(value, type) and hasattr(value, "parameters"):
        value = {"class": location(value), "params": value.parameters()}
    if isinstance(value, dict):
        return {key: configuration(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [configuration(item) for item in value]
    if isinstance(value, pd.DataFrame):
        raise TypeError("configuration inputs must refer to persisted data")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (np.ndarray, np.generic)):
        return configuration(value.tolist())
    return value


def parameters(value):
    result = {}
    for cls in type(value).__mro__:
        fields = inspect.signature(cls.__init__).parameters
        result.update({name: getattr(value, name) for name, field in fields.items()
                       if name != "self" and field.kind in
                       (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)})
        if not any(field.kind == inspect.Parameter.VAR_KEYWORD for field in fields.values()):
            break
    return result


def load(path):
    import yaml
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("configuration must be a mapping")
    return configuration(value)


def build(spec, base=None):
    if base is not None and isinstance(spec, base):
        return spec
    if isinstance(spec, (str, Path)):
        spec = load(spec)
    if isinstance(spec, dict) and set(spec) == {"$config"}:
        return build(load(spec["$config"]), base)
    if not isinstance(spec, dict) or "class" not in spec or set(spec) - {"class", "params"}:
        raise ValueError("configuration requires class and optional params")

    def argument(value):
        if isinstance(value, dict) and (set(value) == {"$config"} or "class" in value and not set(value) - {"class", "params"}):
            return build(value)
        if isinstance(value, list):
            return [argument(item) for item in value]
        return value

    value = resolve(spec["class"])(**{key: argument(item) for key, item in spec.get("params", {}).items()})
    if base is not None and not isinstance(value, base):
        raise TypeError(f"configured class must inherit {base.__name__}")
    return value


def summary(value):
    if isinstance(value, pd.Series):
        return {"rows": len(value), "name": value.name}
    if isinstance(value, pd.DataFrame):
        return {"rows": len(value), "columns": list(value.columns)}
    if hasattr(value, "parameters"):
        return {"class": location(value), "params": summary(value.parameters())}
    if isinstance(value, dict):
        return {key: summary(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [summary(item) for item in value]
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype)}
    if isinstance(value, np.generic):
        return value.item()
    return value
