"""Turn what the patient says into structured facts, and find the procedure.

Two jobs:

* `extract_intake` reads the patient's own words (typed or transcribed from voice)
  and keeps only what they actually said, each with a verbatim quote that must occur
  in the statement. Anything unquoted is dropped.
* `match_drg` maps a procedure described in free text (a referral, in any language)
  to a NordDRG group. The model sees a short closed list of candidates and must
  choose one of them, as everywhere else in this system.
"""

import json
import os
import re
from typing import Any, Dict, Optional

from .nfz import OUT
from .docs import parse_json

INTAKE_PROMPT = """You read what a patient says about their situation. They are waiting for a planned operation.

Return ONLY JSON with these optional fields, each as {"value": ..., "quote": "<exact words from the statement>"}:
- doctor_recommended: true if they say a doctor recommended the operation
- procedure: the operation or treatment they name
- waiting: true if they say they are waiting for it
- cannot_advance_money: true if they say they cannot pay a large amount up front

Rules: include a field ONLY if the statement says it. Never infer. Make no medical
judgement. The statement may be in any language; keep the quote in that language.
"""

KEYWORD_PROMPT = """You help find a medical procedure in a Swedish DRG catalogue.

Given a procedure description (any language), return 2 to 6 short ENGLISH words that
would appear in an English description of it, e.g. ["hip", "replacement"]. Return ONLY
a JSON array of lowercase strings.
"""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def extract_intake(statement: str, client=None) -> Dict[str, Any]:
    from .llm import LLMClient
    client = client or LLMClient(use_condense=False)
    reply = parse_json(client.chat([
        {"role": "system", "content": INTAKE_PROMPT},
        {"role": "user", "content": statement},
    ]))
    fields, rejected = {}, []
    for name in ("doctor_recommended", "procedure", "waiting", "cannot_advance_money"):
        f = reply.get(name)
        if not f:
            continue
        quote = (f.get("quote") or "").strip()
        if not quote or _norm(quote) not in _norm(statement):
            rejected.append({"field": name, "reason": "quote not found in statement"})
        elif f.get("value") in (None, "", False):
            continue
        else:
            fields[name] = {"value": f["value"], "quote": quote}
    return {"statement": statement, "fields": fields, "rejected": rejected}


def _catalogue():
    with open(os.path.join(OUT, "se_tariffs.json"), encoding="utf-8") as fh:
        tariffs = json.load(fh)["tariffs"]
    try:
        with open(os.path.join(OUT, "se_labels_en.json"), encoding="utf-8") as fh:
            en = json.load(fh)
    except OSError:
        en = {}
    return tariffs, en


def match_drg(procedure_text: str, client=None) -> Dict[str, Any]:
    """NordDRG group for a procedure described in free text, or a reason why not."""
    from .llm import LLMClient
    from .matcher import match_procedure
    client = client or LLMClient(use_condense=False)

    raw = client.chat([
        {"role": "system", "content": KEYWORD_PROMPT},
        {"role": "user", "content": procedure_text},
    ])
    m = re.search(r"\[.*\]", raw, re.S)
    try:
        words = [w.lower() for w in json.loads(m.group(0)) if isinstance(w, str)] if m else []
    except ValueError:
        words = []
    tariffs, en = _catalogue()
    scored = []
    for t in tariffs:
        label = en.get(t["drg"], "").lower()
        hits = sum(1 for w in words if w in label)
        if hits:
            scored.append((-hits, t))
    scored.sort(key=lambda x: x[0])
    cands = [t for _, t in scored[:60]]
    if not cands:
        return {"matched": False, "reason": "no catalogue entry matches %s" % words}

    res = match_procedure(
        "text", procedure_text, "patient-document",
        [{"code": t["drg"], "label": t["label_sv"], "label_en": en.get(t["drg"], "")}
         for t in cands], "SE-DRG", client=client)
    if not res["verified"]:
        return {"matched": False, "reason": res["rejection"]}
    drg = res["match"]["code"]
    prop = res["proposed"] or {}
    return {"matched": True, "drg": drg, "label_sv": res["match"]["label"],
            "label_en": en.get(drg, ""), "confidence": prop.get("confidence"),
            "reasoning": prop.get("reasoning")}
