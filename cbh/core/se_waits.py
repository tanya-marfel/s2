"""Real Swedish waiting times: SKR "Vantetider i varden", by procedure and region.

The site's widgets read an undocumented but unauthenticated JSON API at
app.vantetider.se. We use the series "median wait in days, completed
operations/procedures". Data lags by months and a region can have no series at all
(Region Stockholm has not reported operation waits for years), so every figure is
shown with its month and a missing region is reported as missing, never filled.

As elsewhere, a model only picks WHICH of the 77 listed procedures matches the
DRG, from that closed list. The numbers come straight from the API.
"""

import json
import urllib.parse
import urllib.request

from .nfz import OUT, _read, _write
import os

BASE = "https://app.vantetider.se/spa/1.1/rest"
SOURCE = {
    "name": "SKR Väntetider i vården — median wait, completed operations",
    "url": "https://extra.skr.se/vantetiderivarden/vantetidsstatistik/"
           "utokaduppfoljningavspecialiseradvard.43536.html",
}
MATCH_CACHE = os.path.join(OUT, "se_wait_match.json")
NATIONAL = "Alla regioner"
_FILTERS = None


def _get(path, **params):
    q = urllib.parse.urlencode(params)
    url = "%s/%s%s" % (BASE, path, "?" + q if q else "")
    with urllib.request.urlopen(url, timeout=25) as resp:
        return json.load(resp)


def filters():
    global _FILTERS
    if _FILTERS is None:
        _FILTERS = {
            "services": _get("filters/filterchoices/availableHealthcareSpecifilter"),
            "regions": _get("filters/filterchoices/availabilityfilter").get("regions", []),
        }
    return _FILTERS


def services():
    out = []
    for cat in filters()["services"].get("operations", []):
        for s in cat.get("services", []):
            out.append({"categoryId": cat["categoryId"], "category": cat["categoryName"],
                        "serviceId": s["serviceId"], "serviceName": s["serviceName"]})
    return out


def regions():
    return [{"regionId": r["regionId"], "regionName": r["regionName"]}
            for r in filters()["regions"]]


def match_service(drg, label_sv, label_en, client=None):
    cache = _read(MATCH_CACHE)
    if drg in cache:
        return cache[drg]
    from .llm import LLMClient
    from .matcher import match_procedure
    client = client or LLMClient(use_condense=False)
    svc = services()
    m = match_procedure(
        drg, "%s (%s)" % (label_en or label_sv, label_sv), "SE-DRG",
        [{"code": str(s["serviceId"]), "label": "%s — %s" % (s["category"], s["serviceName"])}
         for s in svc], "SE-waits", client=client)
    proposed = m["proposed"] or {}
    if m["verified"]:
        result = {"serviceId": int(m["match"]["code"]),
                  "confidence": proposed.get("confidence"),
                  "reasoning": proposed.get("reasoning")}
    else:
        result = {"serviceId": None, "reason": m["rejection"]}
    cache[drg] = result
    _write(MATCH_CACHE, cache)
    return result


def median_wait(service, region_id=None):
    """Latest median wait in days for one procedure, nationally or for one region."""
    params = dict(production=1, phase=1, query=7, categoryId=service["categoryId"],
                  serviceId=service["serviceId"], measurementYear=2026,
                  measurementMonth=8, viewType=0)
    if region_id:
        params["regionIds"] = region_id
    series = _get("availableHealthcareSpeci/trend", **params).get("series", [])
    want = None
    if region_id:
        name = next((r["regionName"] for r in regions() if r["regionId"] == int(region_id)), None)
        want = name
    s = next((x for x in series if x["name"] == (want or NATIONAL)), None)
    points = [p for p in (s or {}).get("data", []) if p.get("quantity") is not None]
    if not points:
        return None
    last = points[-1]
    return {"region": s["name"], "month": last["name"],
            "median_days": last["quantity"], "operations": last["y"]}


def lookup(drg, label_sv, label_en, region_id=None):
    m = match_service(drg, label_sv, label_en)
    if not m.get("serviceId"):
        return {"matched": False, "reason": m.get("reason")}
    svc = next((s for s in services() if s["serviceId"] == m["serviceId"]), None)
    if svc is None:
        return {"matched": False, "reason": "matched procedure is no longer listed"}
    w = median_wait(svc, region_id)
    return {
        "matched": True, "service": svc["serviceName"], "category": svc["category"],
        "confidence": m.get("confidence"), "reasoning": m.get("reasoning"),
        "wait": w, "source": SOURCE,
        "missing": None if w else "no waiting-time series reported for this region",
    }
