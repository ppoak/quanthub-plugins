"""Store frozen datasets, models and scores."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import hashlib
import importlib.metadata
import json
import pickle
import re
import shutil
import sys
import tempfile
import numpy as np
import pandas as pd
from factor_protocol import read_config, write_config, resolve_artifacts_root
from factor_protocol.paths import validate_identifier
from .util import configuration, resolve


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def inventory(directory, *, exclude=("manifest.json",)):
    root = Path(directory)
    return {p.relative_to(root).as_posix(): file_digest(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).as_posix() not in exclude}


def verify_inventory(directory, files, *, exclude=("manifest.json",)):
    if inventory(directory, exclude=exclude) != files:
        raise ValueError("artifact files changed")


def _versions(libraries):
    return {name: importlib.metadata.version(name) for name in libraries}


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        return resolve(f"{module}:{name}")


def _save_state(value, path, libraries):
    with Path(path).open("xb") as stream:
        pickle.dump({"versions": _versions(libraries), "value": value}, stream, protocol=pickle.HIGHEST_PROTOCOL)


def _load_state(path):
    with Path(path).open("rb") as stream:
        record = _Unpickler(stream).load()
    if _versions(record["versions"]) != record["versions"]:
        raise ValueError("saved state runtime versions differ from training")
    return record["value"]


class Store:
    """Keep generated artifacts under the project's configured ARTIFACTS root."""

    def __init__(self, *, name="experiment", project_root="."):
        self.name = validate_identifier(name, "study")
        self.project_root = str(Path(project_root).resolve())
        self.root = resolve_artifacts_root(project_root=self.project_root) / "compositions"

    def parameters(self):
        return {"name": self.name, "project_root": self.project_root}

    @contextmanager
    def workspace(self, name, *, reset=False):
        """Retain interrupted work, exclude concurrent writers and clear successful work."""
        validate_identifier(name, "workspace")
        base = self.root / ".cache"
        base.mkdir(parents=True, exist_ok=True)
        directory = base / name
        with (base / (name + ".lock")).open("a+b") as lock:
            if lock.seek(0, 2) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                directory.resolve().relative_to(base.resolve())
                if reset and directory.exists():
                    shutil.rmtree(directory)
                directory.mkdir(exist_ok=True)
                yield directory
                if directory.exists():
                    shutil.rmtree(directory)
            finally:
                lock.seek(0)
                if sys.platform == "win32":
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def dataset_path(self, identity):
        identifier = digest(configuration(identity))[:24] if isinstance(identity, dict) else identity
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{24}", identifier):
            raise ValueError("dataset_id must contain 24 lowercase hexadecimal characters")
        return self.root / "datasets" / identifier

    def dataset_manifest(self, dataset_id):
        path = self.dataset_path(dataset_id)
        record = read_config(path / "manifest.json")
        if (record.get("status") != "complete" or record["dataset_id"] != dataset_id
                or digest(record["definition"])[:24] != dataset_id):
            raise ValueError("dataset identity or status is invalid")
        if len(record["features"]) != len(record["columns"]) or len(set(record["columns"])) != len(record["columns"]):
            raise ValueError("dataset feature mapping is invalid")
        verify_inventory(path, record["files"])
        return record

    def freeze(self, definition, batches, *, features, target="ret", label_time="exit_date",
               feature_time=None, metadata=None, state=None, reports=None, resume=True):
        from .preprocessor import Preprocessor
        features = tuple(features)
        if not features:
            raise ValueError("a frozen dataset requires feature columns")
        definition = configuration(definition)
        validate_identifier(definition["name"], "dataset")
        path = self.dataset_path(definition)
        if path.exists():
            record = self.dataset_manifest(path.name)
            contract = {"target": target, "label_time": label_time, "feature_time": feature_time}
            if tuple(record["columns"]) != features or record["contract"] != contract:
                raise ValueError("frozen dataset schema differs from the requested features or target")
            return path.name
        path.parent.mkdir(parents=True, exist_ok=True)
        rows, dates = 0, []
        identity = digest({"definition": definition, "features": features, "target": target,
                           "label_time": label_time, "feature_time": feature_time,
                           "inputs": (metadata or {}).get("inputs", {})})
        with self.workspace("freeze_" + path.name, reset=not resume) as staging:
            for temporary in staging.rglob("*.tmp"):
                temporary.unlink()
            progress = read_config(staging / "progress.json") or {"identity": identity, "schema": None, "files": {}}
            if progress["identity"] != identity:
                raise ValueError("freeze inputs changed; rebuild with resume=false")
            if progress["files"] and progress.get("schema") is None:
                raise ValueError("freeze checkpoint schema is missing; rebuild with resume=false")
            for frame in batches:
                frame = Preprocessor.validate(frame, features, target=target, label_time=label_time,
                                              feature_time=feature_time)
                schema = [[name, str(dtype)] for name, dtype in frame.dtypes.items()]
                if progress.get("schema") is not None and progress["schema"] != schema:
                    raise ValueError("dataset batch schema changed; rebuild with resume=false")
                progress["schema"] = schema
                for date, day in frame.groupby("date", sort=True):
                    partition = staging / f"date={date.date()}"
                    if str(date.date()) in dates:
                        raise ValueError("dataset dates must occur in only one batch")
                    partition.mkdir(exist_ok=True)
                    output = partition / "data.parquet"
                    relative = output.relative_to(staging).as_posix()
                    values = day.drop(columns="date").sort_values("code").reset_index(drop=True)
                    payload = hashlib.sha256(pd.util.hash_pandas_object(values, index=False).values.tobytes()).hexdigest()
                    saved = progress["files"].get(relative, {})
                    if saved and saved["payload"] != payload:
                        raise ValueError("freeze batch values changed; rebuild with resume=false")
                    if not output.exists() or saved.get("sha256") != file_digest(output):
                        temporary = partition / "data.parquet.tmp"
                        values.to_parquet(temporary, index=False)
                        temporary.replace(output)
                        progress["files"][relative] = {"sha256": file_digest(output), "payload": payload}
                    dates.append(str(date.date()))
                write_config(staging / "progress.json", progress)
                rows += len(frame)
            if not rows:
                raise ValueError("cannot freeze an empty dataset")
            if state is not None:
                temporary = staging / "preprocessor.pkl.tmp"
                temporary.unlink(missing_ok=True)
                _save_state(state, temporary, ("factor-compose", "numpy", "pandas"))
                temporary.replace(staging / "preprocessor.pkl")
            for name, frame in (reports or {}).items():
                validate_identifier(name, "report")
                frame.to_csv(staging / f"{name}.csv", index=False, float_format="%.6g")
            details = configuration(metadata or {})
            record = {**details, "status": "complete", "dataset_id": path.name,
                      "definition": definition,
                      "features": details.get("features", list(features)), "columns": list(features),
                      "contract": {"target": target, "label_time": label_time, "feature_time": feature_time},
                      "rows": rows, "calendar": sorted(dates), "files": inventory(staging, exclude=("manifest.json", "progress.json"))}
            write_config(staging / "manifest.json", record)
            (staging / "progress.json").unlink()
            staging.rename(path)
        return path.name

    def load_preprocessor(self, dataset_id, *, record=None):
        """Load the frozen preprocessing state after dataset integrity checks."""
        if record is None:
            self.dataset_manifest(dataset_id)
        return _load_state(self.dataset_path(dataset_id) / "preprocessor.pkl")

    def read_dataset(self, dataset_id, begin=None, end=None, features=None, *, lookback=0,
                     record=None, threads=1, memory_limit="256MB"):
        import duckdb
        with tempfile.TemporaryDirectory(prefix="compose_read_") as temporary:
            with duckdb.connect(config={"threads": threads, "memory_limit": memory_limit,
                                        "temp_directory": temporary, "preserve_insertion_order": False}) as connection:
                return self.scan_dataset(connection, dataset_id, begin, end, features, lookback=lookback,
                                         record=record).order("date, code").df()

    def scan_dataset(self, connection, dataset_id, begin=None, end=None, features=None, *, lookback=0, record=None):
        record = record if record is not None else self.dataset_manifest(dataset_id)
        dates = record["calendar"]
        begin = str(pd.Timestamp(begin).date()) if begin is not None else dates[0]
        end = str(pd.Timestamp(end).date()) if end is not None else dates[-1]
        wanted = [date for date in dates if begin <= date <= end]
        if not wanted:
            raise ValueError("requested dates are outside the frozen dataset")
        if lookback < 0:
            raise ValueError("lookback must be non-negative")
        begin, end = dates[max(0, dates.index(wanted[0]) - lookback)], wanted[-1]
        features = tuple(record["columns"] if features is None else features)
        if len(set(features)) != len(features) or set(features) - set(record["columns"]):
            raise ValueError("requested features must be unique frozen columns")
        path = self.dataset_path(dataset_id)
        files = [p.as_posix() for directory in sorted(path.glob("date=*"))
                 if begin <= directory.name[5:] <= end for p in directory.glob("*.parquet")]
        if not files:
            raise ValueError("no dataset partitions in the requested interval")
        relation = connection.read_parquet(files, hive_partitioning=True, union_by_name=True)
        auxiliary = [c for c in relation.columns if c not in {"date", "code", *record["columns"]}]
        columns = ["date", "code", *features, *auxiliary]
        selected = ", ".join('"' + c.replace('"', '""') + '"' for c in columns)
        return relation.select(selected)

    def paths(self, run_id):
        validate_identifier(run_id, "run_id")
        path = self.root / self.name / run_id
        return path, path / "signals"

    def result(self, run_id):
        path, scores = self.paths(run_id)
        return {"run_id": run_id, "name": self.name, "artifacts": path.as_posix(), "signals": scores.as_posix()}

    def start(self, record):
        run_id = "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8]
        path, _ = self.paths(run_id)
        path.mkdir(parents=True)
        write_config(path / "run.json", {**configuration(record), "name": self.name,
                     "run_id": run_id, "status": "running", "folds": {}})
        return run_id

    def update(self, run_id, **changes):
        path, _ = self.paths(run_id)
        record = read_config(path / "run.json")
        write_config(path / "run.json", {**record, **configuration(changes)})

    def save_model(self, run_id, fold_id, model, *, metadata, role="model"):
        validate_identifier(fold_id, "fold_id")
        validate_identifier(role, "model_role")
        root, _ = self.paths(run_id)
        models = root / "models"
        models.mkdir(exist_ok=True)
        path = models / (fold_id + ("" if role == "model" else "_" + role) + ".pkl")
        _save_state({"model": model, "metadata": metadata}, path, model.dependencies())
        record = read_config(root / "run.json")
        fold = record["folds"].setdefault(fold_id, {})
        if role == "model":
            fold.update(configuration(metadata))
        fold["path" if role == "model" else role + "_path"] = path.relative_to(root).as_posix()
        write_config(root / "run.json", record)

    def load_model(self, run_id, fold_id="fold_0000", *, role="model", allow_incomplete=False):
        root, _ = self.paths(run_id)
        record = read_config(root / "run.json")
        if record.get("status") == "complete":
            verify_inventory(root, record["files"], exclude=("run.json",))
        elif not allow_incomplete:
            raise ValueError("model run is incomplete")
        key = "path" if role == "model" else role + "_path"
        return _load_state(root / record["folds"][fold_id][key])

    @staticmethod
    def _write_scores(path, scores):
        from .preprocessor import Preprocessor
        scores = Preprocessor.validate(scores, ("score",))
        if np.isinf(scores.score.to_numpy(dtype=float)).any():
            raise ValueError("scores must be finite or missing")
        for date, day in scores.groupby("date", sort=True):
            partition = path / f"date={date.date()}"
            partition.mkdir(parents=True)
            day.drop(columns="date").sort_values("code").to_parquet(partition / "signal.parquet", index=False)

    def write_scores(self, run_id, scores):
        self._write_scores(self.paths(run_id)[1], scores)

    def write_prediction(self, name, scores):
        validate_identifier(name, "prediction")
        path = self.root / "predictions" / name
        if path.exists():
            raise FileExistsError(path)
        self._write_scores(path, scores)
        return {"prediction_path": path.as_posix(), "rows": len(scores)}

    def read_scores(self, prediction):
        import duckdb
        from .preprocessor import Preprocessor
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
            frame = self.scan_scores(connection, prediction).order("date, code").df()
        return Preprocessor.validate(frame, ("score",))

    def scan_scores(self, connection, prediction):
        if isinstance(prediction, dict) and "prediction_path" not in prediction:
            source = Store(name=prediction["name"], project_root=self.project_root)
            root, path = source.paths(prediction["run_id"])
            record = read_config(root / "run.json")
            if record.get("status") != "complete":
                raise ValueError("prediction run is incomplete")
            verify_inventory(root, record["files"], exclude=("run.json",))
        else:
            path = Path(prediction["prediction_path"] if isinstance(prediction, dict) else prediction)
        return connection.read_parquet(str(path / "date=*/*.parquet"), hive_partitioning=True)

    def complete(self, run_id):
        root, _ = self.paths(run_id)
        self.update(run_id, status="complete", files=inventory(root, exclude=("run.json",)))

    def fail(self, run_id, error):
        self.update(run_id, status="failed", error=str(error))
