"""Ingest Dutch hospital waiting times (NZa Zorgbeeld) into a compact snapshot.

Dutch hospitals must publish the wait for a fixed list of treatments, per
location. The national bulk file is ~105 MB with a rolling year of fortnightly
readings, so this keeps only the latest valid reading per location and
treatment and writes a small JSON the server reads at runtime.

    python3 cbh/ingest/nl_wachttijden.py
    python3 cbh/ingest/nl_wachttijden.py --csv /path/to/already-downloaded.csv
"""

import argparse
import csv
import datetime
import io
import json
import os
import urllib.request

URL = ("https://puc.overheid.nl/PUC/Handlers/DownloadDocument.ashx"
       "?identifier=PUC_809508_22&versienummer=1")
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "out", "nl_waits.json")
TREATMENT = "Behandeling"
STALE_DAYS = 60


def parse(fh):
    best = {}
    for r in csv.DictReader(fh, delimiter=";"):
        # Rows flagged INSUFFICIENT_OBSERVATIONS carry no wait figure.
        if r["TYPE_WACHTTIJD"] != TREATMENT or not r["WACHTTIJD"]:
            continue
        key = (r["KVK_VESTIGING_NR"], r["LOCATIECODE"], r["OMS_WACHTTIJD"])
        if key not in best or r["PEILDATUM"] > best[key]["PEILDATUM"]:
            best[key] = r

    # Drop readings a location has not refreshed recently; they are not current.
    newest = max(r["PEILDATUM"] for r in best.values())
    cutoff = (datetime.date.fromisoformat(newest)
              - datetime.timedelta(days=STALE_DAYS)).isoformat()

    procedures = {}
    for r in best.values():
        if r["PEILDATUM"] < cutoff:
            continue
        procedures.setdefault(r["OMS_WACHTTIJD"], []).append({
            "provider": r["NAAM_INSTELLING"],
            "place": r["NAAM_VESTIGING"],
            "address": r["STRAAT"],
            "postcode": r["POSTCODE"],
            "locality": r["PLAATS"],
            "region": r["ROAZ_REGIO"],
            "specialty": r["SPECIALISME"],
            "avg_wait_days": int(r["WACHTTIJD"]),
            "updated": r["PEILDATUM"],
        })
    for rows in procedures.values():
        rows.sort(key=lambda p: (p["avg_wait_days"], p["provider"]))
    return procedures


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="use a local copy instead of downloading")
    args = ap.parse_args()

    if args.csv:
        fh = open(args.csv, encoding="iso-8859-1", newline="")
    else:
        raw = urllib.request.urlopen(URL, timeout=300).read()
        fh = io.StringIO(raw.decode("iso-8859-1"), newline="")
    procedures = parse(fh)

    data = {
        "source": {"name": "NZa Zorgbeeld wachttijden (Dutch Healthcare Authority)",
                   "url": "https://puc.overheid.nl/nza/doc/PUC_651798_22/1/"},
        "procedures": procedures,
    }
    with open(OUT, "w", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, separators=(",", ":"))
    print("%d treatments, %d location readings -> %s" % (
        len(procedures), sum(len(v) for v in procedures.values()),
        os.path.normpath(OUT)))


if __name__ == "__main__":
    main()
