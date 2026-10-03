"""Extract the NFZ JGP group catalogue (code, name, points) from the official PDF.

Zarzadzenie 48/2025/DSOZ lists every Polish hospital group (JGP) with its product
code and point value. Each row looks like

    265 H02 5.51.01.0008002 Endoprotezoplastyka pierwotna czesciowa stawu biodrowego * 5 626 5 176 ...

The first number after the name is the point value for an ordinary stay. The
columns after it are per-length-of-stay variants that this ingest ignores.
Point values are NOT zloty: converting them needs the contract price per point,
which this document does not give, so no PLN tariff is derived here.

    python3 cbh/ingest/pl_jgp.py
"""

import json
import os
import re
import sys

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from cbh.core.provenance import SOURCES  # noqa: E402

SOURCE_ID = "pl_nfz_jgp_zarz48_2025"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "out", "pl_jgp.json")

ROW = re.compile(
    r"^(\d{1,4})\s+([A-Z]\d{2}[A-Z]?)\s+(5\.51\.\d\d\.\d{7})\s+(.+?)\s*\*?\s+"
    r"(\d{1,3}(?: \d{3})*)(?=\s\d|\s*$)"
)


# No group is worth more than this many points. A run such as "826 760 702" is
# three small values, not 826 760, so a longer reading above the bound is dropped
# in favour of a shorter one.
MAX_POINTS = 120000


def parse_line(line):
    m = ROW.match(line.strip())
    if not m:
        return None
    _, group, product, name, points = m.groups()
    # A wrapped name leaves only numbers after the product code; skip those.
    if not re.search(r"[^\W\d_]{3}", name):
        return None
    tokens = points.split(" ")
    # Prefer the longest reading that stays within MAX_POINTS.
    value = next(int("".join(tokens[:j])) for j in range(len(tokens), 0, -1)
                 if int("".join(tokens[:j])) <= MAX_POINTS or j == 1)
    return {"code": group, "product_code": product, "label_pl": name.strip(" *"),
            "points": value}


def main():
    src = SOURCES[SOURCE_ID]
    pdf = pdfplumber.open(src.local_path)
    rows, seen = [], set()
    for page_no, page in enumerate(pdf.pages):
        for line in (page.extract_text() or "").split("\n"):
            r = parse_line(line)
            if r and r["product_code"] not in seen:
                seen.add(r["product_code"])
                r.update(page=page_no, raw_line=line.strip(), source_id=SOURCE_ID)
                rows.append(r)
    data = {"source": {"source_id": SOURCE_ID, "sha256": src.sha256()}, "groups": rows}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
    print("%d JGP groups -> %s" % (len(rows), os.path.normpath(OUT)))


if __name__ == "__main__":
    main()
