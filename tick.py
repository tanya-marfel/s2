"""Wake the agent and re-evaluate a case against current facts.

This is stage 6: the part that runs on a schedule rather than when a human is
looking. Each tick re-reads the world and closes, escalates or reopens loops.

    python3 tick.py --case case-H02E-anna --demo

`--demo` replays a plausible sequence so the behaviour is visible in seconds
rather than months.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cbh.core import cases, watch  # noqa: E402

# Each step is (label, facts presented to the agent at that moment).
DEMO_TIMELINE = [
    ("Day 7 - application filed, nothing due yet", {
        "days_elapsed": 7, "home_wait_days": 240,
    }),
    ("Day 35 - no decision returned", {
        "days_elapsed": 35, "home_wait_days": 240,
    }),
    ("Day 48 - authorisation granted", {
        "days_elapsed": 48, "home_wait_days": 240,
        "authorisation_granted": True,
    }),
    ("Day 95 - region reports the domestic queue has collapsed to 60 days", {
        "days_elapsed": 95, "home_wait_days": 60,
        "authorisation_granted": True,
    }),
]


def show(conn, case_id):
    for loop in cases.loops_for(conn, case_id):
        print("    [%-11s] %-9s %s" % (loop["loop_type"], loop["status"],
                                       loop["description"][:54]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--wait-days", type=float, default=None)
    ap.add_argument("--live", action="store_true",
                    help="re-read the chosen provider's wait from the national register")
    ap.add_argument("--simulate", type=float, default=None,
                    help="with --live, use this wait instead of the register (labelled simulated)")
    ap.add_argument("--every", type=float, default=None,
                    help="with --live, repeat every N seconds until interrupted")
    args = ap.parse_args()

    if not os.path.exists(cases.DB_PATH):
        raise SystemExit("no ledger yet; run pipeline.py first")

    conn = cases.connect()
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (args.case,)).fetchone()
    if row is None:
        raise SystemExit("unknown case %s" % args.case)

    if args.live:
        # A live case is opened from the UI ("Use this provider"); see watch.py.
        while True:
            res = watch.check(conn, args.case, args.simulate)
            o = res["observation"]
            print("%s  %s reports %s days (plan %s, escalates above %s)%s" % (
                time.strftime("%H:%M:%S"), o["provider"], o.get("wait_days"),
                o["filed_wait_days"], o["threshold_days"],
                "  [simulated]" if o.get("simulated") else ""))
            for a in res["actions"]:
                print("    -> %-9s %s" % (a["action"].upper(), a["why"]))
            if not args.every:
                return
            time.sleep(args.every)

    print("=" * 70)
    print("AGENT TICK  -  %s (%s)" % (args.case, row["patient"]))
    print("=" * 70)
    print("  initial state:")
    show(conn, args.case)

    if args.demo:
        steps = list(DEMO_TIMELINE)
    else:
        facts = {}
        if args.days is not None:
            facts["days_elapsed"] = args.days
        if args.wait_days is not None:
            facts["home_wait_days"] = args.wait_days
        steps = [("manual tick", facts)]

    for label, facts in steps:
        print("\n  %s" % label)
        actions = cases.tick(conn, args.case, facts)
        if not actions:
            print("    (no change)")
        for a in actions:
            verb = a["action"].upper()
            print("    -> %-9s %-26s  %s" % (verb, a["loop"].split(":")[-1], a["why"]))

    print("\n  final state:")
    show(conn, args.case)

    print("\n  ledger summary: %s" % cases.summary(conn))
    print("\n  audit trail:")
    for ev in cases.history(conn, args.case):
        print("    %-16s %s" % (ev["action"], (ev["detail"] or "")[:60]))


if __name__ == "__main__":
    main()
