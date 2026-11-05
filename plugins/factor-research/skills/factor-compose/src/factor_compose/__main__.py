"""Run a configured preprocessor or workflow."""
import argparse
import json
import logging
from threading import Event, Thread
from time import perf_counter
from factor_compose import run
from factor_compose.util import summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    started, stopped = perf_counter(), Event()
    logging.info("Starting configuration: %s", args.config)

    def heartbeat():
        while not stopped.wait(15):
            logging.info("Running | elapsed=%.1fs", perf_counter() - started)

    worker = Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        result = run(args.config)
    except KeyboardInterrupt:
        logging.warning("Cancelled | elapsed=%.1fs", perf_counter() - started)
        return 130
    except Exception:
        logging.exception("Failed | elapsed=%.1fs", perf_counter() - started)
        return 1
    finally:
        stopped.set()
        worker.join()
    logging.info("Completed | elapsed=%.1fs", perf_counter() - started)
    print(json.dumps(summary(result), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
