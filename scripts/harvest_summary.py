# scripts/harvest_summary.py
# Print harvest summary. Called by harvest.yml GitHub Action.
# Usage:
#   python scripts/harvest_summary.py count   -- print just the service count
#   python scripts/harvest_summary.py full    -- print full breakdown

import json
import sys

PATH = "directory/services.json"


def load():
    with open(PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_count(data):
    print(data.get("count", 0))


def cmd_full(data):
    svcs = data.get("services", [])
    print("Total:", data.get("count", 0))
    print("Generated:", data.get("generated_at", "?"))

    by_type = {}
    for s in svcs:
        t = s.get("type", "Unknown")
        by_type[t] = by_type.get(t, 0) + 1
    print("By type:")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(" ", t + ":", c)

    by_net = {}
    for s in svcs:
        n = s.get("network", "?")
        by_net[n] = by_net.get(n, 0) + 1
    print("By network:")
    for n, c in sorted(by_net.items(), key=lambda x: -x[1]):
        print(" ", n + ":", c)

    prices = [s["price_min"] for s in svcs if s.get("price_min")]
    if prices:
        print("Price range: $" + str(min(prices)) + " - $" + str(round(max(prices), 2)))


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    try:
        data = load()
    except Exception as e:
        print("Could not load services.json:", e)
        sys.exit(1)
    if mode == "count":
        cmd_count(data)
    else:
        cmd_full(data)
