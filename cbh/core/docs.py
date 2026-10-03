"""Read a patient's documents and keep only what the documents actually say.

A referral, a waiting-list notice, a clinic's offer or a clinic's reply goes in;
structured fields come out. As with tariff ingestion, the model proposes and the
text decides: every field must carry a verbatim quote that occurs in the document,
and numbers and dates must be traceable to that quote. A field that fails is
dropped and listed as rejected, never shown.

Documents are sent to a third-party model, so the demo uses synthetic samples
(see samples/). Do not upload real medical records.
"""

import io
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

KINDS = ("referral", "waiting_letter", "clinic_offer", "clinic_reply", "other")

FIELDS = {
    "referral": ("procedure", "date", "issuer"),
    "waiting_letter": ("registered_date", "expected_date", "issuer", "procedure"),
    "clinic_offer": ("clinic", "country", "procedure", "price", "currency",
                     "earliest_admission", "valid_until", "accepts_s2"),
    "clinic_reply": ("clinic", "accepts_s2"),
}

PROMPT = """You read one document a patient has uploaded and extract facts from it.

First classify it as one of: referral, waiting_letter, clinic_offer, clinic_reply, other.

Then return ONLY JSON:
{"kind": "<kind>",
 "fields": {"<field>": {"value": <value>, "quote": "<exact words from the document>"}}}

Fields by kind:
- referral: procedure, date (ISO yyyy-mm-dd), issuer
- waiting_letter: registered_date (ISO), expected_date (ISO, expected treatment date), issuer, procedure
- clinic_offer: clinic, country, procedure, price (number only), currency (ISO code), earliest_admission (ISO), valid_until (ISO), accepts_s2
- clinic_reply: clinic, accepts_s2

accepts_s2 is "yes", "no" or "not_stated". Say "no" only if the document says the
clinic does not accept S2 authorisations; say "not_stated" if S2 is not addressed.

Rules:
- Include a field ONLY if the document states it. Never infer or compute.
- "quote" must be copied exactly from the document, as short as possible.
- Make NO medical judgement and add no diagnosis. Copy what is written.
- If a field is absent, leave it out.
"""


def _pdf_text(data: bytes) -> str:
    """Text layer of a PDF. pdfplumber if installed, else pypdf (pure Python), which
    can be vendored into ./vendor on a machine without pip."""
    try:
        import pdfplumber
    except ImportError:
        vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "vendor")
        if os.path.isdir(vendor) and vendor not in sys.path:
            sys.path.insert(0, vendor)
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages)
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def read_text(filename: str, data: bytes) -> Tuple[str, Optional[str]]:
    """Text of an uploaded file. Returns (text, error). Images are not OCR'd here."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        try:
            text = _pdf_text(data)
        except Exception as exc:  # noqa: BLE001 - a corrupt upload must not crash the server
            return "", "could not read PDF: %s" % exc
        if not text.strip():
            return "", "the PDF has no extractable text (scanned images are not supported)"
        return text, None
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace"), None
    return "", "unsupported file type (use PDF or text)"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _numbers(s: str) -> set:
    out = set()
    for raw in re.findall(r"\d[\d ,.\u00a0]*", s or ""):
        tok = re.sub(r"[,.]\d{1,2}$", "", raw.strip().rstrip(" ,."))
        digits = re.sub(r"[^\d]", "", tok)
        if digits:
            out.add(int(digits))
    return out


def verify_field(name: str, field: Dict[str, Any], text: str) -> Optional[str]:
    """Rejection reason, or None if the document corroborates the field."""
    quote = (field or {}).get("quote") or ""
    value = (field or {}).get("value")
    if not quote.strip():
        return "no quote"
    if _norm(quote) not in _norm(text):
        return "quote not found in document"
    if value is None or value == "":
        return "no value"

    if name == "price":
        try:
            amount = int(float(value))
        except (TypeError, ValueError):
            return "price not numeric"
        if amount not in _numbers(quote):
            return "price %s not in quote" % amount
    elif name.endswith("date") or name in ("earliest_admission", "valid_until"):
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(value))
        if not m:
            return "date not ISO"
        year, month, day = m.groups()
        q = quote
        # The ISO form, or the year together with the day, must occur in the quote.
        if str(value) not in q and not (year in q and re.search(r"\b0?%d\b" % int(day), q)):
            return "date not traceable to quote"
    elif name == "accepts_s2":
        if value not in ("yes", "no", "not_stated"):
            return "accepts_s2 must be yes, no or not_stated"
    return None


def parse_json(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        m = re.search(r"\{.*\}", cleaned, re.S)
        try:
            return json.loads(m.group(0)) if m else {}
        except ValueError:
            return {}


def extract(filename: str, data: bytes, client=None) -> Dict[str, Any]:
    text, err = read_text(filename, data)
    if err:
        return {"filename": filename, "error": err, "kind": "other", "fields": {}, "rejected": []}

    from .llm import LLMClient
    client = client or LLMClient(use_condense=False)
    reply = parse_json(client.chat([
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": text},
    ]))
    kind = reply.get("kind") if reply.get("kind") in KINDS else "other"
    allowed = FIELDS.get(kind, ())

    fields, rejected = {}, []
    for name, field in (reply.get("fields") or {}).items():
        if name not in allowed:
            rejected.append({"field": name, "reason": "not a field of %s" % kind})
            continue
        why = verify_field(name, field, text)
        if why:
            rejected.append({"field": name, "reason": why})
        else:
            fields[name] = {"value": field["value"], "quote": field["quote"].strip()}
    return {"filename": filename, "kind": kind, "fields": fields, "rejected": rejected,
            "text": text, "synthetic": "SAMPLE" in text[:200].upper()}
