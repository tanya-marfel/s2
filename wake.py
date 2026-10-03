"""Wake every live case: re-check sources, re-ingest what changed, react.

    python3 wake.py                       one pass
    python3 wake.py --every 2592000       repeat monthly until interrupted
    python3 wake.py --case case-H02E-live --simulate-tariff 8 --simulate-wait 60

or from cron, first of each month at 06:00:

    0 6 1 * *  cd /path/to/project && python3 wake.py

Simulated readings are labelled as such in the ledger.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json  # noqa: E402

from cbh.core import cases, wake  # noqa: E402


def drafter_for(conn, case_id):
    """Letter writer for a case, reusing the server's drafting path."""
    import server
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    pl = json.loads(row["payload"])
    return lambda kind, key: server.draft(
        pl["drg"], pl["home_wait_days"], row["home_country"], "[Patient name]",
        provider=key, kind=kind)


def once(args):
    conn = cases.connect()
    ids = [args.case] if args.case else wake.live_cases(conn)
    if not ids:
        print("no live cases; choose a provider in the UI first")
    for cid in ids:
        r = wake.wake_case(conn, cid, reingest=not args.no_reingest,
                           simulate_wait=args.simulate_wait,
                           simulate_tariff_pct=args.simulate_tariff,
                           drafter=None if args.no_draft else drafter_for(conn, cid))
        o = r["observation"]
        print("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), cid))
        for s in r["sources"]:
            state = "unreachable" if s["unreachable"] else (
                "baseline recorded" if s["baseline"] else
                "CHANGED" if s["changed"] else "unchanged")
            print("    source %-18s %s" % (s["source"], state))
        if "tariff" in r:
            t = r["tariff"]
            print("    tariff moved %.1f%%%s" % (
                t["change_pct"], "  [simulated]" if t["simulated"] else ""))
        if "home_queue" in r:
            h = r["home_queue"]
            print("    Sweden median wait %s days (%s, %s)" % (
                h["median_days"], h["region"], h["month"]))
        print("    %s reports %s days (plan %s)%s" % (
            o["provider"], o.get("wait_days"), o["filed_wait_days"],
            "  [simulated]" if o.get("simulated") else ""))
        for a in r["actions"] or []:
            print("    -> %-9s %s" % (a["action"].upper(), a["why"]))
        if not r["actions"]:
            print("    (no change)")
        p = r.get("proposal")
        if p:
            x = p["payload"]
            print("    PROPOSAL #%s: switch %s -> %s (%s days). Approve in the UI; nothing "
                  "changes until then." % (p["proposal_id"], x["from"]["provider"],
                                           x["to"]["provider"], x["to"]["avg_wait_days"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case")
    ap.add_argument("--every", type=float, default=None, help="seconds between wake-ups")
    ap.add_argument("--no-reingest", action="store_true")
    ap.add_argument("--no-draft", action="store_true",
                    help="propose switches without drafting the replacement letter")
    ap.add_argument("--simulate-wait", type=float, default=None)
    ap.add_argument("--simulate-tariff", type=float, default=None,
                    help="pretend the tariff moved by this many percent")
    args = ap.parse_args()
    while True:
        once(args)
        if not args.every:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
