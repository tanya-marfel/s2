"""Cohort sweep across the whole Swedish national tariff.

The question: why do fewer than 1% of Europeans use a cross-border right that
all of them hold?

Part of the answer is structural and computable. Under Directive 2011/24/EU the
patient must pay the provider in full and reclaim afterwards, so the route is
only usable by someone who can front the entire cost. Under S2 the institutions
settle directly and the patient fronts nothing.

This sweeps every procedure in the published Swedish tariff and quantifies the
capital barrier the Directive route imposes. It uses only verified figures from
the ingested price list - no foreign tariffs are assumed.

    python3 cohort.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SEK_PER_EUR = 11.45
TARIFF_FILE = os.path.join("out", "se_tariffs.json")
THRESHOLDS = (1000, 5000, 10000, 25000)


def percentile(values, pct):
    """Linear-interpolation percentile (the standard definition).

    For an even-length list this makes p50 the mean of the two middle values,
    so it agrees with an independently computed median.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (pct / 100.0) * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def main():
    if not os.path.exists(TARIFF_FILE):
        raise SystemExit("run: python3 cbh/ingest/se_norddrg.py")

    with open(TARIFF_FILE, encoding="utf-8") as fh:
        data = json.load(fh)

    rows = data["tariffs"]
    inpatient = [r for r in rows if r["care_type"] == "inpatient"]
    euros = [r["price_stockholm"]["value"] / SEK_PER_EUR for r in rows]
    eur_in = [r["price_stockholm"]["value"] / SEK_PER_EUR for r in inpatient]

    print("=" * 68)
    print("COHORT SWEEP - Swedish national tariff")
    print("=" * 68)
    print("source      : %s" % data["source"]["title"])
    print("sha256      : %s" % (data["source"]["sha256"] or "")[:32])
    print("procedures  : %d  (%d inpatient, %d outpatient)"
          % (len(rows), len(inpatient), len(rows) - len(inpatient)))
    print("unresolved  : %d" % data["unresolved_count"])
    print()

    print("Capital a patient must advance under Directive 2011/24/EU")
    print("(S2 requires none of it - the institutions settle directly)")
    print("-" * 68)
    print("  median procedure        EUR %10.0f" % percentile(euros, 50))
    print("  75th percentile         EUR %10.0f" % percentile(euros, 75))
    print("  95th percentile         EUR %10.0f" % percentile(euros, 95))
    print("  most expensive          EUR %10.0f" % max(euros))
    print("  median inpatient        EUR %10.0f" % percentile(eur_in, 50))
    print()

    print("Procedures requiring the patient to front more than:")
    print("-" * 68)
    for t in THRESHOLDS:
        n = sum(1 for e in euros if e > t)
        print("  EUR %6d   %5d procedures  (%4.1f%% of the catalogue)"
              % (t, n, 100.0 * n / len(euros)))
    print()

    total = sum(euros)
    print("Aggregate capital locked up if one patient used the Directive route")
    print("for each procedure in the catalogue: EUR {:,.0f}".format(total))
    print()
    print("Interpretation: for most of the catalogue the two routes cost the")
    print("patient the same, yet only one of them is usable without several")
    print("thousand euros of free cash. That is a structural barrier, not a")
    print("preference - and it is invisible unless both routes are compared.")
    print("=" * 68)

    out = {
        "source": data["source"],
        "procedure_count": len(rows),
        "inpatient_count": len(inpatient),
        "median_eur": round(percentile(euros, 50), 2),
        "p75_eur": round(percentile(euros, 75), 2),
        "p95_eur": round(percentile(euros, 95), 2),
        "max_eur": round(max(euros), 2),
        "aggregate_eur": round(total, 2),
        "above_threshold": {str(t): sum(1 for e in euros if e > t) for t in THRESHOLDS},
    }
    with open(os.path.join("out", "cohort.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print("written: out/cohort.json")


if __name__ == "__main__":
    main()
