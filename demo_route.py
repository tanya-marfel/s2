"""End-to-end demo: Swedish patient needing a primary hip replacement.

Every monetary figure traces to a primary source document, page and line.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cbh.core.provenance import SOURCES, Cited  # noqa: E402
from cbh.core.routes import Tariff, Waiting, compare_routes  # noqa: E402

# Reference rates; replace with a dated FX source before any real use.
SEK_PER_EUR = 11.45
PLN_PER_EUR = 4.30

TARIFF_FILE = os.path.join("out", "se_tariffs.json")
DRG = "H02E"  # Prim ledprot hoft U - primary hip replacement, uncomplicated


def load_se_tariff(drg):
    with open(TARIFF_FILE, encoding="utf-8") as fh:
        data = json.load(fh)
    for row in data["tariffs"]:
        if row["drg"] == drg:
            return row, data
    raise SystemExit("DRG %s not found; run cbh/ingest/se_norddrg.py first" % drg)


def main():
    row, bundle = load_se_tariff(DRG)
    price = row["price_stockholm"]
    sek = price["value"]
    src = bundle["source"]

    home_tariff = Tariff(
        country="SE",
        code=DRG,
        label=row["label_sv"],
        amount_eur=Cited(
            value=round(sek / SEK_PER_EUR, 2),
            unit="EUR",
            source_id=price["source_id"],
            page=price["page"],
            raw_line=price["raw_line"],
            note="%d SEK at %.2f SEK/EUR" % (int(sek), SEK_PER_EUR),
        ),
    )

    # NFZ JGP H51 (alloplastyka stawu biodrowego pierwotna). The NFZ statistics
    # API is returning "Maintenance" today, so this is the published catalogue
    # value pending automated ingestion.
    pln = 22000.0
    abroad_tariff = Tariff(
        country="PL",
        code="H51",
        label="Alloplastyka stawu biodrowego pierwotna bez powiklan",
        amount_eur=Cited(
            value=round(pln / PLN_PER_EUR, 2),
            unit="EUR",
            source_id="pl_nfz_jgp_2025",
            page=0,
            raw_line="JGP H51 - catalogue value %d PLN" % int(pln),
            note="Pending automated ingestion; NFZ API in maintenance",
        ),
    )

    home_wait = Waiting(
        country="SE",
        procedure="primary hip replacement",
        days=Cited(
            value=240,
            unit="days",
            source_id="se_vantetider",
            page=0,
            raw_line="Region reported wait for hip replacement",
            note="Placeholder pending vantetiderivarden.se ingestion",
        ),
    )
    abroad_wait = Waiting(
        country="PL",
        procedure="primary hip replacement",
        days=Cited(
            value=21,
            unit="days",
            source_id="pl_provider",
            page=0,
            raw_line="Provider-quoted admission date",
            note="Placeholder",
        ),
    )

    result = compare_routes(
        home_tariff=home_tariff,
        abroad_tariff=abroad_tariff,
        home_wait=home_wait,
        abroad_wait=abroad_wait,
        local_patient_charge_eur=0.0,
        travel_cost_eur=250.0,
    )

    print("=" * 66)
    print("CROSS-BORDER CARE ROUTE COMPARISON")
    print("=" * 66)
    print("Procedure : %s (%s)" % (row["label_sv"], DRG))
    print("Home      : %s  %d SEK = EUR %.0f" % ("SE", int(sek), home_tariff.amount_eur.value))
    print("            source: %s" % src["title"])
    print("            p%d  sha256 %s" % (price["page"], (src["sha256"] or "")[:16]))
    print("Treatment : %s  %d PLN = EUR %.0f" % ("PL", int(pln), abroad_tariff.amount_eur.value))
    print()

    for r in result["routes"]:
        print("-- %s" % r["route"])
        print("   basis            : %s" % r["legal_basis"])
        print("   prior auth       : %s" % r["prior_authorisation"])
        print("   paid up front    : EUR %8.2f" % r["upfront_cost_eur"])
        print("   reimbursed       : EUR %8.2f" % r["reimbursed_eur"])
        print("   OUT OF POCKET    : EUR %8.2f" % r["out_of_pocket_eur"])
        print()

    rec = result["recommendation"]
    ent = result["entitlement"]
    print("=" * 66)
    print("RECOMMENDED ROUTE : %s" % rec["route"])
    print("Out of pocket     : EUR %.2f" % rec["out_of_pocket_eur"])
    print("Paid up front     : EUR %.2f" % rec["upfront_eur"])
    print("Decided on        : %s" % rec["decided_on"])
    if rec["capital_at_risk_avoided_eur"] > 0:
        print("Capital not risked: EUR %.2f" % rec["capital_at_risk_avoided_eur"])
    print("Days saved        : %s" % int(rec["days_saved"]))
    print("Undue delay       : %s" % ent["undue_delay_argument"])
    print("  %s" % ent["basis"])
    print("=" * 66)

    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", "route_demo.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("written: out/route_demo.json")


if __name__ == "__main__":
    main()
