"""Export human-reviewed trajectories; never infer SFT/DPO labels from workflow success."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factoryio_agents.training_export import export_reviewed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", action="append", type=Path, required=True, help="run/training-data; repeat for multiple runs")
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="fresh export directory")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export_reviewed(args.raw, args.reviews, args.out), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error), "type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
