# scripts/verify_summary.py
# Print verification summary. Called by verify.yml GitHub Action.
# Usage:
#   python scripts/verify_summary.py commit  -- print one-line summary for commit message
#   python scripts/verify_summary.py full    -- print full breakdown

import json
import sys

PATH = "directory/services.json"


def load():
    with open(PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def cmd_commit(data):
    svcs = data.get("services", [])
    verified = sum(1 for s in svcs if s.get("verified"))
    failed   = sum(1 for s in svcs if s.get("verify_status") == "failed")
    inactive = sum(1 for s in svcs if s.get("status") == "inactive")
    print("verified=" + str(verified) + " failed=" + str(failed) + " delisted=" + str(inactive))


def cmd_full(data):
    svcs     = data.get("services", [])
    total    = len(svcs)
    active   = sum(1 for s in svcs if s.get("status") == "active")
    verified = sum(1 for s in svcs if s.get("verified"))
    failed   = sum(1 for s in svcs if s.get("verify_status") == "failed")
    timeout  = sum(1 for s in svcs if s.get("verify_status") == "timeout")
    pending  = sum(1 for s in svcs if s.get("verify_status") == "pending")
    inactive = sum(1 for s in svcs if s.get("status") == "inactive")
    print("Total:", total)
    print("Active:", active, "| Verified:", verified, "| Failed:", failed)
    print("Timeout:", timeout, "| Pending:", pending, "| Delisted:", inactive)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    try:
        data = load()
    except Exception as e:
        print("Could not load services.json:", e)
        sys.exit(1)
    if mode == "commit":
        cmd_commit(data)
    else:
        cmd_full(data)
