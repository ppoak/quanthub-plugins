"""Custom source, model and orchestration examples."""
import duckdb
import numpy as np
import pandas as pd
from factor_compose import Preprocessor, Composer, Workflow, Store, Context


class Market(Preprocessor):
    def __init__(self, *, source, name, features, horizon=5, lag=1, project_root="."):
        super().__init__(name=name, features=features, project_root=project_root)
        self.source = source
        self.horizon, self.lag = horizon, lag

    def read(self):
        with duckdb.connect(config={"threads": 1, "memory_limit": "256MB"}) as connection:
            return connection.execute("SELECT * FROM read_csv(?, types={'code': 'VARCHAR'})", [self.source]).df()

    def transform(self, frame):
        frame["date"] = pd.to_datetime(frame["date"])
        calendar = pd.DatetimeIndex(sorted(frame.date.unique()))
        positions = calendar.get_indexer(frame.date)
        def shifted(offset):
            return pd.to_datetime([calendar[i + offset] if i + offset < len(calendar) else pd.NaT for i in positions])
        frame["entry_date"], frame["exit_date"] = shifted(self.lag), shifted(self.lag + self.horizon)
        prices = frame.set_index(["date", "code"])["open"]
        def lookup(column):
            return prices.reindex(pd.MultiIndex.from_arrays([frame[column], frame.code])).to_numpy(dtype=float)
        entry, exit_price = lookup("entry_date"), lookup("exit_date")
        valid = np.isfinite(entry) & np.isfinite(exit_price) & (entry > 0) & (exit_price > 0)
        frame["ret"] = np.divide(exit_price, entry, out=np.full(len(frame), np.nan), where=valid) - 1
        values = frame[list(self.features)].replace([np.inf, -np.inf], np.nan)
        groups = values.groupby(frame.date)
        frame[list(self.features)] = (groups.rank() - .5) / groups.transform("count") - .5
        frame["calendar_seq"] = positions + 1
        return frame

    def run(self):
        frame = self.transform(self.read())
        definition = {"name": self.name, "features": list(self.features), "begin": str(frame.date.min().date()),
                      "end": str(frame.date.max().date()), "horizon_days": self.horizon, "execution_lag_days": self.lag}
        metadata = {"label": {"horizon_days": self.horizon, "execution_lag_days": self.lag}}
        return Store(project_root=self.project_root).freeze(definition, [frame], features=self.features, metadata=metadata)


class Study(Workflow):
    def __init__(self, *, preprocessor, composer, as_of, name="example"):
        self.preprocessor, self.composer = preprocessor, composer
        self.as_of, self.name = as_of, name

    def run(self):
        dataset = self.preprocessor.run()
        store = Store(name=self.name, project_root=self.preprocessor.project_root)
        record = store.dataset_manifest(dataset)
        data, cutoff = store.read_dataset(dataset, record=record), pd.Timestamp(self.as_of)
        train = self.preprocessor.labeled(data.loc[data.date < cutoff], cutoff, strict=True)
        test = data.loc[data.date >= cutoff].reset_index(drop=True)
        run_id = store.start({"workflow": self, "dataset_id": dataset})
        model = self.composer.configure({})
        try:
            model.fit(train, context=Context(cutoff, tuple(record["columns"])))
            metadata = {"dataset_id": dataset, "features": record["columns"], "as_of": cutoff.isoformat()}
            store.save_model(run_id, "fold_0000", model, metadata=metadata)
            store.write_scores(run_id, model.predict(test))
            store.complete(run_id)
        except Exception as error:
            store.fail(run_id, error)
            raise
        finally:
            model.close()
        result = store.result(run_id)
        return result


class LeastSquares(Composer):
    def fit(self, data, *, context):
        self.features_ = tuple(context.features)
        self.as_of_ = pd.Timestamp(context.as_of)
        train = Preprocessor.labeled(data, self.as_of_, target=context.target, strict=True)
        if len(train) != len(data) or (data.date >= self.as_of_).any():
            raise ValueError("training rows and labels must precede as_of")
        x = data[list(self.features_)].to_numpy(dtype=float)
        y = data[context.target].to_numpy(dtype=float)
        valid = np.isfinite(x).all(axis=1) & np.isfinite(y)
        self.coefficients_ = np.linalg.lstsq(x[valid], y[valid], rcond=None)[0]
        return self

    def predict(self, data):
        if (data.date < self.as_of_).any():
            raise ValueError("prediction dates precede as_of")
        result = data[["date", "code"]].copy()
        result["score"] = data[list(self.features_)].to_numpy() @ self.coefficients_
        return result
