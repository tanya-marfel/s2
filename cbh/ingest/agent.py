"""Agentic tariff extraction for countries with no hand-written parser.

Design: deterministic where we can, agent where we must, provenance always.

    cbh/ingest/se_norddrg.py  - deterministic regex + arithmetic cross-check.
                                Highest confidence, but one parser per format.
    this module               - Gemini reads any national tariff document in any
                                language and returns the same provenance-bearing
                                schema.

Every figure the agent returns is verified to actually occur in the source line
it cited. Anything that fails that check is dropped rather than surfaced, so a
hallucinated number cannot reach a patient.
"""

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

import pdfplumber

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from cbh.core.llm import LLMClient  # noqa: E402

SYSTEM_PROMPT = """You extract public healthcare tariffs from official national documents.

The text comes from one page of a government or public-insurer price list. It may
be in any European language. Identify rows that state a price a public health
system pays for a medical procedure.

Return ONLY a JSON array. One object per row:
[
  {
    "code": "<national procedure/DRG/JGP code exactly as printed>",
    "label": "<procedure name exactly as printed, original language>",
    "label_en": "<short English translation>",
    "amount": <number, digits only, no spaces or thousands separators>,
    "currency": "<ISO code: SEK, PLN, EUR, ...>",
    "raw_line": "<the complete source line, copied verbatim>"
  }
]

Rules:
- Copy raw_line EXACTLY as it appears. It is used to verify your answer.
- "amount" must be the price the payer reimburses. If a row shows several
  amounts, choose the principal national/public tariff and say which in label_en.
- Numbers are often grouped with spaces or dots ("86 945", "22.000"). Return the
  integer value: 86945, 22000.
- Skip headers, footnotes, totals and any row without both a code and a price.
- If the page contains no tariff rows, return [].
"""


def _digits(text: str) -> str:
    return re.sub(r"[^\d]", "", text or "")


# Numbers in these documents appear as "86 945", "22.000", "2 650,00" or "1234".
_NUMBER = re.compile(r"\d{1,3}(?:[ .\u00a0]\d{3})+(?:[,.]\d+)?|\d+(?:[,.]\d+)?")


def number_tokens(line: str) -> set:
    """Integer values of the number-like tokens on a line.

    Token-aligned so that an amount cannot be matched by accident across
    neighbouring figures, which a flat digit-substring test would allow.
    """
    values = set()
    for raw in _NUMBER.findall(line or ""):
        token = raw.strip()
        # Trailing decimals ("2 650,00" -> 2650); thousands separators dropped.
        token = re.sub(r"[,.]\d{1,2}$", "", token)
        digits = re.sub(r"[^\d]", "", token)
        if digits:
            values.add(int(digits))
    return values


def verify_row(row: Dict[str, Any], page_text: str) -> Optional[str]:
    """Return a rejection reason, or None when the row is corroborated.

    Guards against the two failure modes that matter: a line the model invented,
    and a number that does not occur on that line.
    """
    raw = (row.get("raw_line") or "").strip()
    if not raw:
        return "no raw_line"

    # The cited line must genuinely occur on the page (whitespace-normalised).
    norm_page = re.sub(r"\s+", " ", page_text)
    norm_raw = re.sub(r"\s+", " ", raw)
    if norm_raw not in norm_page:
        return "cited line not found on page"

    amount = row.get("amount")
    if amount is None:
        return "no amount"
    try:
        amount_i = int(float(amount))
    except (TypeError, ValueError):
        return "amount not numeric"

    # The figure must appear as a distinct number on the cited line.
    if amount_i not in number_tokens(norm_raw):
        return "amount %s not present in cited line" % amount_i

    code = (row.get("code") or "").strip()
    if not code:
        return "no code"
    if code.lower() not in norm_raw.lower():
        return "code %s not present in cited line" % code

    return None


def extract_page(client: LLMClient, page_text: str) -> List[Dict[str, Any]]:
    reply = client.chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": page_text},
        ]
    )
    cleaned = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", cleaned, re.S)
        if not match:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return parsed if isinstance(parsed, list) else []


def ingest(
    pdf_path: str,
    country: str,
    source_id: str,
    max_pages: Optional[int] = None,
    start_page: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    # Ingestion must not go through a compressing proxy. Here the document text
    # IS the payload rather than repeated reference context, so compaction
    # removes the very rows we are trying to read. Measured on one page of the
    # NFZ catalogue: 43 rows extracted direct, 0 via the proxy.
    client = None if dry_run else LLMClient(use_condense=False)

    rows: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    pages_scanned = 0

    with pdfplumber.open(pdf_path) as pdf:
        end = len(pdf.pages) if max_pages is None else min(len(pdf.pages), start_page + max_pages)
        for page_no in range(start_page, end):
            page_text = pdf.pages[page_no].extract_text() or ""
            if not page_text.strip():
                continue
            pages_scanned += 1

            if dry_run:
                print("  p%-3d %d chars | %s" % (page_no, len(page_text),
                                                 page_text.strip().split("\n")[0][:62]))
                continue

            try:
                candidates = extract_page(client, page_text)
            except Exception as exc:  # noqa: BLE001 - one bad page must not abort
                rejected.append({"page": page_no, "reason": "call failed: %s" % str(exc)[:120]})
                continue

            for row in candidates:
                reason = verify_row(row, page_text)
                if reason:
                    rejected.append({"page": page_no, "reason": reason, "row": row})
                    continue
                row["page"] = page_no
                row["country"] = country
                row["source_id"] = source_id
                row["extraction"] = "agent"
                rows.append(row)

            print("  p%-3d kept %-3d rejected %d" % (page_no, len(rows), len(rejected)))

    return {
        "country": country,
        "source_id": source_id,
        "pdf": pdf_path,
        "pages_scanned": pages_scanned,
        "verified_count": len(rows),
        "rejected_count": len(rejected),
        "tariffs": rows,
        "rejected": rejected[:50],
        "llm": client.report() if client else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Agentic tariff extraction")
    ap.add_argument("pdf")
    ap.add_argument("--country", required=True)
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="show pages without calling the model")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    print("ingesting %s (%s)" % (args.pdf, args.country))
    result = ingest(
        args.pdf,
        country=args.country,
        source_id=args.source_id,
        max_pages=args.pages,
        start_page=args.start,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print("\ndry run: %d pages with text" % result["pages_scanned"])
        return

    print("\nverified %d rows, rejected %d"
          % (result["verified_count"], result["rejected_count"]))
    for row in result["tariffs"][:8]:
        print("  %-16s %10s %-4s  %s"
              % (row.get("code"), row.get("amount"), row.get("currency"),
                 (row.get("label_en") or row.get("label") or "")[:42]))
    if result["rejected"]:
        print("\nrejected samples (not surfaced to patients):")
        for r in result["rejected"][:4]:
            print("  p%-3d %s" % (r["page"], r["reason"]))

    out = args.out or os.path.join("out", "agent_%s.json" % args.country.lower())
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("\nwritten: %s" % out)
    if result["llm"]:
        print("route: %s  tokens: %s"
              % (result["llm"]["route"], result["llm"]["usage"]["total_tokens"]))


if __name__ == "__main__":
    main()
