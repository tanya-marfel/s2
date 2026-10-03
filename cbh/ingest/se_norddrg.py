"""Extract Swedish NordDRG cross-regional tariffs from the official PDF.

Row format in the source document:
    <TYP> <DRG> <MDC> <short text> <weight> <Sthlm price> <Gotland price> <outlier>

Example:
    SV H02E 08 Prim ledprot hoft U 1,082 86 945 81 040 145 454
"""

import json
import os
import re
import sys
from typing import Dict, List, Optional

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from cbh.core.provenance import SOURCES, Cited  # noqa: E402

SOURCE_ID = "se_norddrg_2025"

# Swedish thousands grouping uses spaces ("86 945"), so the three trailing money
# columns are ambiguous as plain text. Capture them as a token run and resolve the
# split against the published point prices below.
ROW = re.compile(
    r"^(SV|OV)\s+"
    r"([A-Z]\d{2}[A-Z])\s+"
    r"(\d{2})\s+"
    r"(.+?)\s+"
    r"(\d+,\d+)\s+"
    r"([\d\s]+?)\s*$"
)

POINT_PRICE = re.compile(
    r"kostnad f(?:ö|o)r vikten 1 (?:ä|a)r\s+([\d\s]+)\s*kr", re.IGNORECASE
)

# Region point prices (SEK per DRG weight 1.0), from the price-list header.
# price = weight * point_price, which makes every parsed figure checkable.
PP_STOCKHOLM = 80356
PP_GOTLAND = 74898

# Weights are printed to 3 decimals, so the true weight is within +/-0.0005 and the
# implied price can differ from the printed one by up to 0.0005 * point_price per
# column. Incorrect token splits are wrong by orders of magnitude, so this stays safe.
MONEY_TOLERANCE = 0.0005 * (PP_STOCKHOLM + PP_GOTLAND) + 5


def _int(raw: str) -> int:
    return int(re.sub(r"[^\d]", "", raw))


def _dec(raw: str) -> float:
    return float(raw.replace("\xa0", "").replace(" ", "").replace(",", "."))


def _splits(tokens: List[str], want: int) -> List[List[int]]:
    """All ways to read a token run as `want` space-grouped integers.

    A number starts with a 1-3 digit token and continues only through tokens of
    exactly 3 digits.
    """
    out: List[List[int]] = []

    def walk(i: int, acc: List[int]) -> None:
        if len(acc) == want:
            if i == len(tokens):
                out.append(list(acc))
            return
        if i >= len(tokens) or len(tokens[i]) > 3:
            return
        buf = tokens[i]
        j = i + 1
        while True:
            acc.append(int(buf))
            walk(j, acc)
            acc.pop()
            if j < len(tokens) and len(tokens[j]) == 3:
                buf += tokens[j]
                j += 1
            else:
                break

    walk(0, [])
    return out


def resolve_money(run: str, weight: float) -> Optional[List[int]]:
    """Pick the token split whose first two columns match weight * point price."""
    tokens = run.split()
    if not all(t.isdigit() for t in tokens):
        return None

    best = None
    best_err = None
    for cand in _splits(tokens, 3):
        sthlm, gotland, _outlier = cand
        err = abs(sthlm - weight * PP_STOCKHOLM) + abs(gotland - weight * PP_GOTLAND)
        if best_err is None or err < best_err:
            best, best_err = cand, err

    # Rounding of weight*point_price leaves bounded slack per column.
    if best is None or best_err > MONEY_TOLERANCE:
        return None
    return best


def extract(pdf_path: Optional[str] = None) -> Dict:
    src = SOURCES[SOURCE_ID]
    pdf_path = pdf_path or src.local_path

    tariffs: List[Dict] = []
    unresolved: List[Dict] = []
    national_point_price: Optional[Cited] = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_no, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            for line in text.split("\n"):
                line = line.strip()

                if national_point_price is None:
                    m = POINT_PRICE.search(line)
                    if m:
                        national_point_price = Cited(
                            value=float(_int(m.group(1))),
                            unit="SEK",
                            source_id=SOURCE_ID,
                            page=page_no,
                            raw_line=line,
                            note="National cost of DRG weight 1.0 (Socialstyrelsen)",
                        )

                m = ROW.match(line)
                if not m:
                    continue

                care_type, drg, mdc, label, weight_raw, money_run = m.groups()
                weight = _dec(weight_raw)
                money = resolve_money(money_run, weight)
                if money is None:
                    unresolved.append({"page": page_no, "line": line})
                    continue

                sthlm, gotland, outlier = money
                tariffs.append(
                    {
                        "drg": drg,
                        "mdc": mdc,
                        "care_type": "inpatient" if care_type == "SV" else "outpatient",
                        "label_sv": label.strip(),
                        "weight": weight,
                        "price_stockholm": Cited(
                            value=float(sthlm),
                            unit="SEK",
                            source_id=SOURCE_ID,
                            page=page_no,
                            raw_line=line,
                            note="Region Stockholm cross-regional price (excl. Karolinska); "
                            "cross-checked against weight x point price %d"
                            % PP_STOCKHOLM,
                        ).to_dict(),
                        "price_gotland": gotland,
                        "cost_outlier_threshold": outlier,
                    }
                )

    return {
        "source": src.to_dict(),
        "national_point_price": (
            national_point_price.to_dict() if national_point_price else None
        ),
        "point_price_stockholm": PP_STOCKHOLM,
        "point_price_gotland": PP_GOTLAND,
        "tariff_count": len(tariffs),
        "unresolved_count": len(unresolved),
        "tariffs": tariffs,
    }


def main() -> None:
    data = extract()
    os.makedirs("out", exist_ok=True)
    path = os.path.join("out", "se_tariffs.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)

    print("source : %s" % data["source"]["title"])
    print("sha256 : %s" % (data["source"]["sha256"] or "")[:16])
    npp = data["national_point_price"]
    if npp:
        print("weight 1.0 = %s %s (p%s)" % (int(npp["value"]), npp["unit"], npp["page"]))
    print("extracted %d DRG tariffs (%d unresolved) -> %s"
          % (data["tariff_count"], data["unresolved_count"], path))

    for row in data["tariffs"]:
        if row["drg"] in ("H02E", "H02C", "H04N", "H01N"):
            print(
                "  %s %-32s w=%.3f  %s SEK  (p%d)"
                % (
                    row["drg"],
                    row["label_sv"][:32],
                    row["weight"],
                    int(row["price_stockholm"]["value"]),
                    row["price_stockholm"]["page"],
                )
            )


if __name__ == "__main__":
    main()
