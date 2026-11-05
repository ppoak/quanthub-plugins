"""Run preprocessing and composition in a temporary project."""
from pathlib import Path
import tempfile
import json
import numpy as np
import pandas as pd
from extensions import Market, Study, LeastSquares


def main():
    with tempfile.TemporaryDirectory(prefix="compose_example_") as temporary:
        path = Path(temporary) / "source.csv"
        generator = np.random.default_rng(7)
        dates = pd.bdate_range("2020-01-01", periods=60)
        frame = pd.DataFrame({"date": np.repeat(dates, 10), "code": np.tile([f"asset_{i}" for i in range(10)], len(dates))})
        frame["open"] = np.exp(generator.normal(0, .01, len(frame)).reshape(60, 10).cumsum(axis=0).reshape(-1)) * 100
        frame["value"], frame["quality"] = generator.normal(size=(2, len(frame)))
        frame.to_csv(path, index=False)
        workflow = Study(preprocessor=Market(source=path.as_posix(), name="example_panel", features=["value", "quality"], project_root=temporary),
                         composer=LeastSquares(), as_of=str(dates[40].date()))
        result = workflow.run()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
