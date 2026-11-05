"""Run configured research stages in sequence."""
from pathlib import Path
import logging
from time import perf_counter
from .util import build, configuration, load, parameters, summary


logger = logging.getLogger(__name__)


def _references(value, results):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            parts = value["$ref"].split(".")
            if parts[0] not in results:
                raise ValueError(f"workflow reference is unavailable: {value['$ref']}")
            result = results[parts[0]]
            for key in parts[1:]:
                result = result[int(key)] if isinstance(result, (list, tuple)) else result[key]
            return result
        return {key: _references(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [_references(item, results) for item in value]
    return value


def run(config, *, inputs=None):
    from .preprocessor import Preprocessor
    config = load(config) if isinstance(config, (str, Path)) else config
    instance = build({key: value for key, value in config.items() if key != "inputs"})
    if not isinstance(instance, (Preprocessor, Workflow)):
        raise TypeError("execution requires Preprocessor or Workflow")
    return instance.run(**{**config.get("inputs", {}), **(inputs or {})})


class Workflow:
    def __init__(self, *, steps, name="workflow"):
        from factor_protocol.paths import validate_identifier
        self.name = validate_identifier(name, "workflow")
        self.steps = steps
        identifiers = [validate_identifier(step["id"], "step") for step in steps]
        if not identifiers or len(set(identifiers)) != len(identifiers):
            raise ValueError("workflow steps must have unique ids")
        for step in steps:
            if set(step) - {"id", "stage", "params", "inputs"}:
                raise ValueError("workflow steps accept id, stage, params and inputs")

    def parameters(self):
        return parameters(self)

    def run(self):
        from .stores import Store
        store = Store(name=self.name + "_workflow")
        results = {}
        run_id = store.start({"config": configuration(self), "steps": {}})
        try:
            for index, step in enumerate(self.steps, 1):
                started = perf_counter()
                logger.info("Workflow %s: step %d/%d %s started", self.name, index, len(self.steps), step["id"])
                spec = step["stage"]
                spec = load(spec) if isinstance(spec, (str, Path)) else spec
                spec = {**spec, "params": {**spec.get("params", {}), **step.get("params", {})}}
                results[step["id"]] = run(
                    _references(spec, results), inputs=_references(step.get("inputs", {}), results)
                )
                store.update(run_id, steps={key: summary(value) for key, value in results.items()})
                logger.info("Workflow %s: step %s completed | elapsed=%.1fs",
                            self.name, step["id"], perf_counter() - started)
            store.complete(run_id)
        except Exception as error:
            store.fail(run_id, error)
            raise
        return {**store.result(run_id), "results": results}
