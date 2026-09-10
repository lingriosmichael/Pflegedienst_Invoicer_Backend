import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.entlastung_replay import freeze_opening_snapshots


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Persist missing locked snapshots")
    options = parser.parse_args()
    print(json.dumps(freeze_opening_snapshots(dry_run=not options.apply), indent=2, default=str))
