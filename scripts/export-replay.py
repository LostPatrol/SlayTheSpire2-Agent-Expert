"""Export an auditable public run timeline and its correlation manifest."""

import argparse
import json
from pathlib import Path

from sts2_agent.replay import export_replay


def main() -> None:
    """Expose public replay export without invoking the controller or game."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(export_replay(args.run_dir, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
