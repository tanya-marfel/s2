"""Where a procedure is actually available in Poland, from the NFZ waiting-list register.

The NFZ "Terminy Leczenia" API (api.nfz.gov.pl/app-itl-api) lists every
contracted provider that offers a scheduled benefit, with its address, phone,
number of patients waiting and the average wait the provider reports. That is a
real provider directory, so we use it instead of pointing patients at a contact
point.

Two steps, kept separate so the model never touches the figures:

1. Match the Swedish DRG to an NFZ benefit name. The NFZ API cannot be searched
   by DRG, so a model proposes Polish search stems, the API returns the benefit
   names containing them, and the matcher picks one of THOSE names (it cannot
   invent a benefit). The result is cached per DRG.
2. Fetch the providers for that benefit. Pure HTTP, no model.

Standard library only for step 2, so it runs on the serverless deployment.
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "https://api.nfz.gov.pl/app-itl-api"
SOURCE_URL = "https://api.nfz.gov.pl/app-itl-api/"
PAGE = 25  # the API's maximum page size
MAX_PAGES = 40
CASE_STABLE = 1

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "out")
MATCH_CACHE = os.path.join(OUT, "nfz_match.json")
PROVIDER_CACHE = os.path.join(OUT, "nfz_providers.json")
PROVIDER_TTL = 6 * 3600

# TERYT voivodeship codes, the first two digits of teryt-provider.
VOIVODESHIPS = {
    "02": "Lower Silesia", "04": "Kuyavian-Pomeranian", "06": "Lublin",
    "08": "Lubusz", "10": "Łódź", "12": "Lesser Poland", "14": "Masovian",
    "16": "Opole", "18": "Subcarpathian", "20": "Podlaskie", "22": "Pomeranian",
    "24": "Silesian", "26": "Holy Cross", "28": "Warmian-Masurian",
    "30": "Greater Poland", "32": "West Pomeranian",
}


def _get(path, **params):
    url = "%s/%s?%s" % (BASE, path, urllib.parse.urlencode(dict(params, format="json")))
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            # The API rate-limits aggressively; back off and retry.
            if exc.code in (429, 502, 503, 504) and attempt < 4:
                time.sleep(0.8 * (attempt + 1))
                continue
            raise
    raise RuntimeError("unreachable")


def _read(path):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {}


def _write(path, data):
    # Best effort: serverless filesystems are read-only.
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


# --- step 1: DRG -> NFZ benefit name ---------------------------------------

def benefit_names(term, limit=25):
    if len(term) < 3:
        return []
    d = _get("benefits", name=term, limit=limit)
    return list(d.get("data") or [])


KEYWORD_PROMPT = """You help find a medical procedure in the Polish NFZ waiting-list register.

Given a procedure (Swedish NordDRG label and an English description), return
2 to 5 short Polish search stems that would appear inside the NFZ benefit name.
Use word stems of at least 5 letters, no diacritics-sensitive endings, and one
concept per stem, for example "endoprotez", "biodrow", "zaćm", "artroskop".

Return ONLY a JSON array of strings.
"""


def propose_terms(label_sv, label_en, client=None):
    from .llm import LLMClient
    client = client or LLMClient(use_condense=False)
    text = client.chat([
        {"role": "system", "content": KEYWORD_PROMPT},
        {"role": "user", "content": "%s | %s" % (label_sv, label_en)},
    ])
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    m = re.search(r"\[.*\]", cleaned, re.S)
    try:
        terms = json.loads(m.group(0)) if m else []
    except ValueError:
        terms = []
    return [t.strip() for t in terms if isinstance(t, str) and len(t.strip()) >= 3][:5]


def match_benefit(drg, label_sv, label_en, client=None, refresh=False):
    """Return {"benefit", "confidence", "reasoning", ...} or {"benefit": None, "reason"}."""
    cache = _read(MATCH_CACHE)
    if drg in cache and not refresh:
        return cache[drg]

    from .llm import LLMClient
    from .matcher import match_procedure
    client = client or LLMClient(use_condense=False)

    terms = propose_terms(label_sv, label_en, client)
    names = []
    for t in terms:
        for n in benefit_names(t):
            if n not in names:
                names.append(n)
    if not names:
        result = {"benefit": None, "reason": "no NFZ benefit name contains %s" % terms,
                  "terms": terms}
    else:
        m = match_procedure(
            drg, "%s (%s)" % (label_en or label_sv, label_sv), "SE",
            [{"code": n, "label": n} for n in names[:60]], "PL", client=client)
        proposed = m["proposed"] or {}
        if m["verified"]:
            result = {"benefit": m["match"]["code"], "terms": terms,
                      "confidence": proposed.get("confidence"),
                      "reasoning": proposed.get("reasoning")}
        else:
            result = {"benefit": None, "terms": terms, "reason": m["rejection"]}

    cache[drg] = result
    _write(MATCH_CACHE, cache)
    return result


# --- step 2: providers ------------------------------------------------------

def _normalise(a):
    stats = ((a.get("statistics") or {}).get("provider-data")) or {}
    teryt = a.get("teryt-provider") or ""
    avg = stats.get("average-period")
    return {
        "provider": a.get("provider"),
        "place": a.get("place"),
        "address": a.get("address"),
        "locality": a.get("locality"),
        "region": VOIVODESHIPS.get(teryt[:2], ""),
        "phone": a.get("phone"),
        "lat": a.get("latitude"),
        "lon": a.get("longitude"),
        "awaiting": stats.get("awaiting"),
        "avg_wait_days": avg if avg else None,
        "updated": stats.get("update"),
    }


def _fetch_all(benefit):
    first = _get("queues", case=CASE_STABLE, benefit=benefit, limit=PAGE, page=1)
    total = (first.get("meta") or {}).get("count") or 0
    rows = [r["attributes"] for r in first.get("data") or []]
    pages = min(MAX_PAGES, -(-total // PAGE))
    if pages > 1:
        with ThreadPoolExecutor(3) as ex:
            for d in ex.map(
                    lambda p: _get("queues", case=CASE_STABLE, benefit=benefit,
                                   limit=PAGE, page=p),
                    range(2, pages + 1)):
                rows += [r["attributes"] for r in d.get("data") or []]
    return total, rows


def _rank(p):
    # Providers that report a wait come first, shortest wait leading.
    return (p["avg_wait_days"] is None, p["avg_wait_days"] or 0,
            -(p["awaiting"] or 0), p["provider"] or "")


def providers(benefit, max_age=PROVIDER_TTL):
    """Providers offering `benefit`, ranked by reported wait. Cached for hours."""
    cache = _read(PROVIDER_CACHE)
    hit = cache.get(benefit)
    if hit and time.time() - hit["fetched_at"] < max_age:
        return dict(hit, cached=True)

    try:
        total, rows = _fetch_all(benefit)
    except (urllib.error.URLError, OSError, ValueError):
        # Serve a stale copy rather than nothing.
        if hit:
            return dict(hit, cached=True, stale=True)
        raise

    items = sorted((_normalise(a) for a in rows), key=_rank)
    result = {
        "benefit": benefit, "total": total, "listed": len(items),
        "fetched_at": time.time(), "providers": items,
        "source": {"name": "NFZ Terminy Leczenia (waiting-list register)",
                   "url": SOURCE_URL},
    }
    cache[benefit] = result
    _write(PROVIDER_CACHE, cache)
    return dict(result, cached=False)


def lookup(drg, label_sv, label_en, refresh=False, max_age=PROVIDER_TTL):
    """Full answer for the UI: the matched benefit and where it is available."""
    m = match_benefit(drg, label_sv, label_en, refresh=refresh)
    if not m.get("benefit"):
        return {"matched": False, "reason": m.get("reason"), "terms": m.get("terms")}
    res = providers(m["benefit"], max_age=max_age)
    res["matched"] = True
    res["match"] = {k: m.get(k) for k in ("benefit", "confidence", "reasoning")}
    return res
