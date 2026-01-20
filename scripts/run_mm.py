#!/usr/bin/env python3
import argparse
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mm.config import load_config
from mm.engine import MarketMaker


def main() -> None:
    ap = argparse.ArgumentParser(description="Opinion market maker loop.")
    ap.add_argument("--config", default="mm_config.json", help="Config JSON path.")
    ap.add_argument("--once", action="store_true", help="Run one iteration and exit.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    maker = MarketMaker(cfg)
    if args.once:
        maker.run_once()
    else:
        maker.run_forever()


if __name__ == "__main__":
    main()
