"""Read source data and prepare model-ready features and targets."""
import numpy as np
import pandas as pd
from .util import parameters


class Preprocessor:
    """Read source data, construct features and targets, and freeze the result."""

    def __init__(self, *, name, features, project_root="."):
        self.name, self.features = name, tuple(features)
        self.project_root = str(project_root)

    def parameters(self):
        return parameters(self)

    def read(self):
        """Return source rows as a DataFrame; subclasses own data access."""
        raise NotImplementedError("implement read() or override run() in the preprocessor")

    def transform(self, frame):
        """Override to construct features and targets on a DataFrame."""
        return frame

    def run(self):
        from .stores import Store
        frame = self.transform(self.read())
        definition = {"name": self.name, "features": list(self.features),
                      "begin": str(pd.Timestamp(frame.date.min()).date()),
                      "end": str(pd.Timestamp(frame.date.max()).date())}
        return Store(project_root=self.project_root).freeze(definition, [frame], features=self.features)

    @staticmethod
    def validate(frame, features=(), *, target="ret", label_time="exit_date", feature_time=None):
        features = tuple(features)
        if len(set(features)) != len(features) or set(features) & {"date", "code", target, label_time, feature_time}:
            raise ValueError("feature columns must be unique and distinct from keys and target columns")
        if not frame.columns.is_unique or {"date", "code", *features} - set(frame):
            raise ValueError("panel columns must be unique and contain keys and features")
        frame = frame.copy().reset_index(drop=True)
        frame["date"] = pd.to_datetime(frame["date"])
        if frame[["date", "code"]].isna().any().any():
            raise ValueError("date/code keys must be non-null")
        frame["code"] = frame["code"].astype(str)
        if frame.duplicated(["date", "code"]).any():
            raise ValueError("date/code keys must be unique")
        if (frame.date != frame.date.dt.normalize()).any():
            raise ValueError("panel dates must identify whole days")
        if feature_time:
            visible = pd.to_datetime(frame[feature_time])
            if visible.isna().any() or (visible > frame.date).any():
                raise ValueError("features must be visible by their observation date")
        return frame

    @staticmethod
    def labeled(frame, cutoff, *, target="ret", label_time="exit_date", strict=False):
        values = frame[target].to_numpy(dtype=float)
        visible = pd.to_datetime(frame[label_time])
        finite = np.isfinite(values)
        if (finite & (visible.isna() | (visible < frame.date))).any():
            raise ValueError("finite labels require an availability time on or after observation")
        boundary = visible < pd.Timestamp(cutoff) if strict else visible <= pd.Timestamp(cutoff)
        return frame.loc[finite & boundary].reset_index(drop=True)
