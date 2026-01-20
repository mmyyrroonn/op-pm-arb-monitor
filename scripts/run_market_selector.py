#!/usr/bin/env python3
import argparse

from mm.config import load_config
from mm.market_selector import select_and_write


def main() -> None:
    ap = argparse.ArgumentParser(description="Select Opinion markets by volume rank + price target.")
    ap.add_argument("--config", default="mm_config.json", help="Config JSON path.")
    args = ap.parse_args()

    cfg = load_config(args.config)
    selected = select_and_write(cfg)
    selector_cfg = cfg.get("market_selector", {})

    print(
        f"[OK] selected={len(selected)} "
        f"source={selector_cfg.get('source_file', 'opinion_topics_merged.json')} "
        f"output={selector_cfg.get('output_file', 'selected_markets.json')}"
    )


if __name__ == "__main__":
    main()
