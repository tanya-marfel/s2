"""Find where a procedure is offered across the EU countries we hold data for.

Each country is an adapter returning the same shape (matched, match, total,
providers, source). Adapters exist only where a country publishes open,
machine-readable, per-provider data for specific procedures. Where it does not
we say so (`GAPS`) rather than listing hospitals we cannot show offer the
procedure.
"""

from concurrent.futures import ThreadPoolExecutor

from . import nfz, nl

ADAPTERS = {
    "PL": ("Poland", nfz.lookup),
    "NL": ("Netherlands", nl.lookup),
}

GAPS = {
    "PT": "publishes hospital-level surgical waits only, not per procedure",
    "CZ": "open provider register by specialty, but no waiting times",
    "DE": "quality reports only; no public waiting times",
    "DK": "no open waiting-time API",
    "FI": "waits published per hospital district, not per hospital",
    "ES": "waiting lists published regionally, not per hospital",
    "IT": "waiting lists published regionally, no national feed",
}


def provider_key(code, p):
    """Stable identifier so the UI can name a provider and the server can re-find it."""
    return "|".join([code, p.get("provider") or "", p.get("place") or "",
                     p.get("address") or "", p.get("locality") or ""])


def _one(code, drg, label_sv, label_en, **kw):
    name, fn = ADAPTERS[code]
    out = {"code": code, "name": name}
    try:
        out.update(fn(drg, label_sv, label_en, **kw))
    except Exception as exc:  # noqa: BLE001 - one country failing must not hide the rest
        out.update(matched=False, error=str(exc))
    out["providers"] = [dict(p, key=provider_key(code, p))
                        for p in out.get("providers") or []]
    return out


def find(key, drg, label_sv, label_en, **kw):
    """Re-resolve a provider from the registers, so a figure always comes from them.

    Extra keyword arguments reach the adapter (`max_age=0` forces a live read).
    Returns (country_name, provider_row, source) or None.
    """
    code = (key or "").split("|", 1)[0].upper()
    if code not in ADAPTERS:
        return None
    c = _one(code, drg, label_sv, label_en, **kw)
    row = next((p for p in c["providers"] if p["key"] == key), None)
    return (c["name"], row, c.get("source")) if row else None


def lookup(drg, label_sv, label_en, exclude=(), **kw):
    skip = {e.upper() for e in exclude}
    codes = [c for c in ADAPTERS if c not in skip]
    with ThreadPoolExecutor(len(codes) or 1) as ex:
        countries = list(ex.map(lambda c: _one(c, drg, label_sv, label_en, **kw), codes))
    return {
        "countries": countries,
        "gaps": [{"code": c, "reason": r} for c, r in GAPS.items() if c not in skip],
    }
