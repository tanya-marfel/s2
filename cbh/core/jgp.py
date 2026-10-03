"""Map a Swedish NordDRG group to the Polish NFZ JGP group for the same procedure.

The candidates are the groups ingested by `cbh/ingest/pl_jgp.py`, narrowed by
Polish word stems so the list stays short. The model must choose one of them
(see matcher.py); the code, name and point value shown afterwards are read from
the ingested row, with its page and raw line, never from the model.
"""

import json
import os

from .nfz import OUT, _read, _write, propose_terms
from .provenance import SOURCES

JGP = os.path.join(OUT, "pl_jgp.json")
MATCH_CACHE = os.path.join(OUT, "jgp_match.json")
MAX_CANDIDATES = 60
_DATA = None


def data():
    global _DATA
    if _DATA is None:
        with open(JGP, encoding="utf-8") as fh:
            _DATA = json.load(fh)
    return _DATA


def _candidates(terms):
    stems = [t.lower() for t in terms]
    scored = []
    for g in data()["groups"]:
        name = g["label_pl"].lower()
        hits = sum(1 for t in stems if t in name)
        if hits:
            scored.append((-hits, g))
    scored.sort(key=lambda x: x[0])
    return [g for _, g in scored[:MAX_CANDIDATES]]


def match_group(drg, label_sv, label_en, client=None):
    cache = _read(MATCH_CACHE)
    if drg in cache:
        return cache[drg]

    from .llm import LLMClient
    from .matcher import match_procedure
    client = client or LLMClient(use_condense=False)
    terms = propose_terms(label_sv, label_en, client)
    cands = _candidates(terms)
    if not cands:
        result = {"code": None, "reason": "no JGP group name contains %s" % terms}
    else:
        m = match_procedure(
            drg, "%s (%s)" % (label_en or label_sv, label_sv), "SE",
            [{"code": g["product_code"], "label": g["label_pl"]} for g in cands],
            "PL", client=client)
        proposed = m["proposed"] or {}
        if m["verified"]:
            result = {"product_code": m["match"]["code"],
                      "confidence": proposed.get("confidence"),
                      "reasoning": proposed.get("reasoning")}
        else:
            result = {"code": None, "reason": m["rejection"]}
    cache[drg] = result
    _write(MATCH_CACHE, cache)
    return result


def lookup(drg, label_sv, label_en):
    if not os.path.exists(JGP):
        return {"matched": False, "reason": "run: python3 cbh/ingest/pl_jgp.py"}
    m = match_group(drg, label_sv, label_en)
    if not m.get("product_code"):
        return {"matched": False, "reason": m.get("reason")}
    g = next((x for x in data()["groups"] if x["product_code"] == m["product_code"]), None)
    if g is None:
        return {"matched": False, "reason": "matched group is not in the ingested catalogue"}
    src = SOURCES[g["source_id"]]
    return {
        "matched": True, "confidence": m.get("confidence"), "reasoning": m.get("reasoning"),
        "group": {"code": g["code"], "product_code": g["product_code"],
                  "label_pl": g["label_pl"], "points": g["points"]},
        "provenance": {"title": src.title, "publisher": src.publisher, "url": src.url,
                       "page": g["page"] + 1, "raw_line": g["raw_line"],
                       "sha256": ((data()["source"] or {}).get("sha256") or "")[:32]},
    }
