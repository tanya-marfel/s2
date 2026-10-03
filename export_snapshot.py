"""Freeze the case ledger into a static snapshot for serverless deployment.

Serverless filesystems are ephemeral, so the SQLite ledger cannot travel. This
exports the current state of each case as JSON; `server.py` reads it when no
live database is present.

    python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240
    python3 tick.py --case case-H02E-anna --demo
    python3 export_snapshot.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402
from cbh.core import cases  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def main():
    if not os.path.exists(cases.DB_PATH):
        raise SystemExit("no ledger; run pipeline.py and tick.py first")

    conn = cases.connect()
    ids = [r["case_id"] for r in conn.execute("SELECT case_id FROM cases")]
    snapshot = {cid: server.timeline(cid) for cid in ids}

    path = os.path.join(OUT, "timeline_snapshot.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)

    print("exported %d case(s) -> %s" % (len(ids), path))
    for cid, data in snapshot.items():
        print("  %-22s %d loops, %d events"
              % (cid, len(data["loops"]), len(data["events"])))


if __name__ == "__main__":
    main()
