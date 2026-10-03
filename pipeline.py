"""End-to-end pipeline: patient -> entitlement -> route -> paperwork -> open loops.

    python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240

Runs the six stages described in README.md and leaves a persistent case with
open loops that `tick.py` can re-evaluate later.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cbh.core import cases  # noqa: E402
from cbh.core.config import get_secret  # noqa: E402
from cbh.core.provenance import SOURCES, Cited  # noqa: E402
from cbh.core.routes import Tariff, Waiting, compare_routes  # noqa: E402
from cbh.core import countries  # noqa: E402

SEK_PER_EUR = 11.45
PLN_PER_EUR = 4.30
GUARANTEE_DAYS = countries.get("SE").guarantee_days


def load_se_tariff(drg):
    path = os.path.join("out", "se_tariffs.json")
    if not os.path.exists(path):
        raise SystemExit("run: python3 cbh/ingest/se_norddrg.py")
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for row in data["tariffs"]:
        if row["drg"] == drg:
            return row, data
    raise SystemExit("DRG %s not found in %s" % (drg, path))


def build(patient, drg, wait_days, abroad_eur, abroad_wait, travel_eur):
    row, bundle = load_se_tariff(drg)
    price = row["price_stockholm"]
    sek = price["value"]

    home = Tariff(
        country="SE", code=drg, label=row["label_sv"],
        amount_eur=Cited(
            value=round(sek / SEK_PER_EUR, 2), unit="EUR",
            source_id=price["source_id"], page=price["page"],
            raw_line=price["raw_line"],
            note="%d SEK at %.2f SEK/EUR" % (int(sek), SEK_PER_EUR)),
    )
    abroad = Tariff(
        country="PL", code="H51",
        label="Alloplastyka stawu biodrowego pierwotna",
        amount_eur=Cited(
            value=abroad_eur, unit="EUR", source_id="pl_nfz_jgp_2025", page=0,
            raw_line="NFZ JGP catalogue value",
            note="NFZ statistics API in maintenance; pending automated ingestion"),
    )
    home_w = Waiting("SE", row["label_sv"], Cited(
        value=wait_days, unit="days", source_id="se_vantetider", page=0,
        raw_line="regional reported wait", note="vantetiderivarden.se"))
    abroad_w = Waiting("PL", "hip replacement", Cited(
        value=abroad_wait, unit="days", source_id="pl_provider", page=0,
        raw_line="provider-quoted admission", note=""))

    se = countries.get("SE")
    result = compare_routes(home, abroad, home_w, abroad_w,
                            local_patient_charge_eur=0.0,
                            travel_cost_eur=travel_eur,
                            guarantee_days=se.guarantee_days,
                            guarantee_name=se.guarantee_name,
                            guarantee_binding=se.binding)
    result["patient"] = patient
    result["source_documents"] = [
        SOURCES[price["source_id"]].title,
        SOURCES["pl_nfz_jgp_2025"].title,
    ]
    return result, row


def seed_loops(conn, case_id, result):
    rec = result["recommendation"]
    ent = result["entitlement"]

    cases.open_loop(
        conn, case_id + ":auth", case_id, "filing",
        "File S2 prior-authorisation application with the home region",
        trigger="immediate", owner="patient+agent",
        closure="authorisation_granted")

    cases.open_loop(
        conn, case_id + ":provider", case_id, "retrieval",
        "Obtain provider options and a confirmed admission date via the "
        "treatment-country National Contact Point",
        trigger="immediate", owner="agent",
        closure="provider_confirmed")

    cases.open_loop(
        conn, case_id + ":decision", case_id, "timed",
        "Chase the authority if no decision is returned",
        trigger="days_elapsed:30", owner="agent",
        closure="decision_received")

    # Only meaningful while the domestic queue stays long enough to argue
    # undue delay; if it shortens, the entitlement argument weakens.
    cases.open_loop(
        conn, case_id + ":wait-watch", case_id, "conditional",
        "Re-check the domestic waiting time; entitlement depends on it",
        trigger="home_wait_days > %d" % GUARANTEE_DAYS, owner="agent",
        closure="treated_or_wait_within_guarantee")

    cases.open_loop(
        conn, case_id + ":tariff-watch", case_id, "conditional",
        "Re-check tariffs; a change alters which route pays better",
        trigger="tariff_change_pct > 5", owner="agent",
        closure="treatment_completed")

    if ent.get("undue_delay_argument"):
        cases.open_loop(
            conn, case_id + ":appeal", case_id, "filing",
            "Prepare an Art. 20(2) appeal in case authorisation is refused",
            trigger="on_refusal", owner="agent",
            closure="authorisation_granted")

    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--patient", default="Anna L.")
    ap.add_argument("--drg", default="H02E")
    ap.add_argument("--wait-days", type=float, default=240)
    ap.add_argument("--abroad-eur", type=float, default=5116.28)
    ap.add_argument("--abroad-wait", type=float, default=21)
    ap.add_argument("--travel-eur", type=float, default=250)
    ap.add_argument("--draft", action="store_true", help="also draft the S2 application (uses Gemini)")
    ap.add_argument("--case-id", default=None)
    args = ap.parse_args()

    result, row = build(args.patient, args.drg, args.wait_days,
                        args.abroad_eur, args.abroad_wait, args.travel_eur)
    case_id = args.case_id or ("case-%s-%s" % (args.drg, args.patient.split()[0].lower()))

    conn = cases.connect()
    cases.open_case(conn, case_id, args.patient, "SE", row["label_sv"], args.drg,
                    payload=result["recommendation"])
    rec = seed_loops(conn, case_id, result)

    print("=" * 70)
    print("CASE %s  -  %s" % (case_id, args.patient))
    print("=" * 70)
    print("Procedure : %s (%s)" % (row["label_sv"], args.drg))
    print("Home wait : %d days   (statutory guarantee %d)" % (args.wait_days, GUARANTEE_DAYS))
    print()
    for r in result["routes"]:
        print("  %-28s up front EUR %8.2f   out of pocket EUR %8.2f"
              % (r["route"], r["upfront_cost_eur"], r["out_of_pocket_eur"]))
    print()
    print("RECOMMENDED : %s" % rec["route"])
    print("  decided on        : %s" % rec["decided_on"])
    print("  capital not risked: EUR %.2f" % rec["capital_at_risk_avoided_eur"])
    print("  days saved        : %d" % int(rec["days_saved"]))
    print("  undue delay       : %s" % result["entitlement"]["undue_delay_argument"])
    print()
    print("OPEN LOOPS")
    for loop in cases.loops_for(conn, case_id):
        print("  [%-11s] %-9s %s" % (loop["loop_type"], loop["status"], loop["description"]))

    if args.draft:
        from cbh.core.draft import draft_application
        get_secret("GEMINI_API_KEY")
        facts = {
            "patient": args.patient, "home": "Region Stockholm, Sweden",
            "procedure": row["label_sv"], "drg": args.drg,
            "treatment_country": "Poland",
            "home_tariff_eur": result["tariffs"]["home_eur"]["value"],
            "abroad_tariff_eur": result["tariffs"]["abroad_eur"]["value"],
            "home_wait_days": int(args.wait_days), "guarantee_days": GUARANTEE_DAYS,
            "abroad_wait_days": int(args.abroad_wait), "route": rec["route"],
            "capital_at_risk_eur": rec["capital_at_risk_avoided_eur"],
            "sources": result["source_documents"],
        }
        print("\n" + "=" * 70)
        print("DRAFT S2 APPLICATION")
        print("=" * 70)
        letter, problems = draft_application(facts)
        print(letter)
        print("-" * 70)
        if problems:
            print("VERIFICATION FAILED - do not file:")
            for p in problems:
                print("  - %s" % p)
        else:
            print("verified: no unsupplied figures, no clinical claims, within length")
        result["draft_application"] = letter
        result["draft_problems"] = problems

    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", "%s.json" % case_id), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("\nwritten: out/%s.json   |   ledger: %s" % (case_id, cases.DB_PATH))


if __name__ == "__main__":
    main()
