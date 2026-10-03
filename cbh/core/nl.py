"""Where a procedure is available in the Netherlands, from NZa Zorgbeeld.

Dutch hospitals must publish waiting times for a fixed list of treatments, per
location. `cbh/ingest/nl_wachttijden.py` freezes the latest readings to
out/nl_waits.json. As with Poland, a model only picks WHICH treatment matches
the Swedish DRG, and only from the names in that file; the providers and
waits are read straight from the data.
"""

import json
import os

from .nfz import OUT, _read, _write

WAITS = os.path.join(OUT, "nl_waits.json")
MATCH_CACHE = os.path.join(OUT, "nl_match.json")
_DATA = None


def data():
    global _DATA
    if _DATA is None:
        with open(WAITS, encoding="utf-8") as fh:
            _DATA = json.load(fh)
    return _DATA


def match_treatment(drg, label_sv, label_en, client=None):
    cache = _read(MATCH_CACHE)
    if drg in cache:
        return cache[drg]

    from .llm import LLMClient
    from .matcher import match_procedure
    client = client or LLMClient(use_condense=False)
    names = sorted(data()["procedures"])
    m = match_procedure(
        drg, "%s (%s)" % (label_en or label_sv, label_sv), "SE",
        [{"code": n, "label": n} for n in names], "NL", client=client)
    proposed = m["proposed"] or {}
    if m["verified"]:
        result = {"benefit": m["match"]["code"],
                  "confidence": proposed.get("confidence"),
                  "reasoning": proposed.get("reasoning")}
    else:
        result = {"benefit": None, "reason": m["rejection"]}
    cache[drg] = result
    _write(MATCH_CACHE, cache)
    return result


def lookup(drg, label_sv, label_en, **_):
    if not os.path.exists(WAITS):
        return {"matched": False,
                "reason": "run: python3 cbh/ingest/nl_wachttijden.py"}
    m = match_treatment(drg, label_sv, label_en)
    if not m.get("benefit"):
        return {"matched": False, "reason": m.get("reason")}
    rows = data()["procedures"][m["benefit"]]
    return {
        "matched": True,
        "match": {k: m.get(k) for k in ("benefit", "confidence", "reasoning")},
        "total": len(rows), "listed": len(rows), "providers": rows,
        "source": data()["source"],
    }
