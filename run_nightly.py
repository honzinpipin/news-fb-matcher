"""Jeden beh cele pipeline. Spousti ho Planovac uloh Windows kazdou noc."""
import sys

from app import pipeline

if __name__ == "__main__":
    trigger = sys.argv[1] if len(sys.argv) > 1 else "nightly"
    result = pipeline.run(trigger)
    print(result)
    sys.exit(0 if result.get("status") == "ok" else 1)
