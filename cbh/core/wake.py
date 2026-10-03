"""The monthly wake-up: re-check sources, re-ingest what changed, reopen the case.

`watch.check()` re-reads one provider's wait. A full wake goes further:

1. Fingerprint each source document the case relied on (HTTP ETag / Last-Modified
   / length). A changed fingerprint means the publisher issued a new version.
2. If the Swedish tariff changed, re-run the deterministic parser on the new
   file and measure how far this case's price moved.
3. If the Dutch waiting-time file changed, rebuild the snapshot.
4. Read the provider's wait from the register.
5. Hand the facts to `cases.tick()`, which escalates, retracts or reopens loops.

Readings can be simulated for a demo. A simulated fact is labelled as such in the
ledger and is never written to the data snapshots.
"""

import json
import os
import tempfile
import time
import urllib.request
from typing import Any, Dict, Optional

from . import cases, nl, replan, se_waits, watch
from .nfz import OUT, _read, _write
from .provenance import SOURCES

FINGERPRINTS = os.path.join(OUT, "source_fingerprints.json")
NL_FILE = ("https://puc.overheid.nl/PUC/Handlers/DownloadDocument.ashx"
           "?identifier=PUC_809508_22&versienummer=1")


UA = {"User-Agent": "Mozilla/5.0 (compatible; s2-agent/1.0)"}


def fingerprint(url: str) -> Optional[str]:
    """What the publisher says about the current file, without downloading it."""
    req = urllib.request.Request(url, method="HEAD", headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            h = resp.headers
            return "|".join(h.get(k, "") for k in ("ETag", "Last-Modified", "Content-Length"))
    except OSError:
        return None


def check_source(key: str, url: str) -> Dict[str, Any]:
    """Compare the current fingerprint with the stored one. First sight is a baseline."""
    store = _read(FINGERPRINTS)
    now = fingerprint(url)
    before = store.get(key)
    changed = bool(before and now and before != now)
    if now and (before is None or changed):
        store[key] = now
        _write(FINGERPRINTS, store)
    return {"source": key, "changed": changed, "baseline": before is None,
            "unreachable": now is None}


def _download(url: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".bin")
    req = urllib.request.Request(url, headers=UA)
    with os.fdopen(fd, "wb") as out, urllib.request.urlopen(req, timeout=300) as resp:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    return path


def reingest_se_price(drg: str) -> Optional[float]:
    """Download the current Swedish price list and read this DRG's price from it."""
    from ..ingest import se_norddrg
    path = _download(SOURCES["se_norddrg_2025"].url)
    try:
        data = se_norddrg.extract(path)
    finally:
        os.unlink(path)
    row = next((r for r in data["tariffs"] if r["drg"] == drg), None)
    return row["price_stockholm"]["value"] if row else None


def refresh_nl_snapshot() -> int:
    """Rebuild the Dutch snapshot from the current national file."""
    from ..ingest import nl_wachttijden
    path = _download(NL_FILE)
    try:
        with open(path, encoding="iso-8859-1", newline="") as fh:
            procedures = nl_wachttijden.parse(fh)
    finally:
        os.unlink(path)
    data = {"source": nl.data()["source"], "procedures": procedures}
    _write(nl.WAITS, data)
    nl._DATA = None
    return sum(len(v) for v in procedures.values())


def wake_case(conn, case_id: str, reingest: bool = True,
              simulate_wait: Optional[float] = None,
              simulate_tariff_pct: Optional[float] = None,
              drafter=None) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError(case_id)
    pl = json.loads(row["payload"] or "{}")
    report: Dict[str, Any] = {"sources": [], "actions": []}
    facts: Dict[str, Any] = {}

    # 1-3. sources
    se = check_source("se_norddrg_2025", SOURCES["se_norddrg_2025"].url)
    nlc = check_source("nl_zorgbeeld", NL_FILE)
    report["sources"] = [se, nlc]

    pct = 0.0
    if simulate_tariff_pct is not None:
        pct = float(simulate_tariff_pct)
        report["tariff"] = {"simulated": True, "change_pct": pct}
    elif se["changed"] and reingest and pl.get("filed_tariff_sek"):
        new = reingest_se_price(pl["drg"])
        if new is not None:
            pct = abs(new - pl["filed_tariff_sek"]) / pl["filed_tariff_sek"] * 100
            report["tariff"] = {"simulated": False, "filed_sek": pl["filed_tariff_sek"],
                                "now_sek": new, "change_pct": round(pct, 2)}
    if nlc["changed"] and reingest:
        report["nl_rows"] = refresh_nl_snapshot()
    if "tariff" in report or pl.get("filed_tariff_sek"):
        facts["tariff_change_pct"] = pct

    # 4. provider wait
    obs = watch.observe(conn, case_id, simulate_wait)
    report["observation"] = obs
    if obs.get("wait_days") is not None:
        facts["provider_wait_days"] = obs["wait_days"]

    # 4b. the domestic queue
    if pl.get("guarantee_days") and row["home_country"] == "SE":
        try:
            hw = se_waits.lookup(pl["drg"], pl["label_sv"], pl["label_en"])
        except OSError:
            hw = None
        w = hw and hw.get("wait")
        if w:
            report["home_queue"] = w
            facts["home_median_wait_days"] = w["median_days"]

    # 5. a closed loop whose threshold is exceeded again must reopen
    for loop in cases.loops_for(conn, case_id, status="closed"):
        key, _, threshold = loop["trigger"].partition(">")
        key = key.strip()
        if loop["loop_type"] == "conditional" and key in facts and threshold.strip():
            if float(facts[key]) > float(threshold):
                facts["invalidates:%s" % loop["loop_id"]] = (
                    "%s=%s exceeds %s again" % (key, facts[key], threshold.strip()))

    report["actions"] = cases.tick(conn, case_id, facts)
    report["proposal"] = replan.after_tick(conn, case_id, obs, report["actions"], drafter)
    cases._log(conn, "wake", case_id=case_id, detail={
        "sources_changed": [s["source"] for s in report["sources"] if s["changed"]],
        "provider_wait_days": obs.get("wait_days"),
        "tariff_change_pct": facts.get("tariff_change_pct"),
        "home_median_wait_days": facts.get("home_median_wait_days"),
        "simulated": bool(obs.get("simulated") or report.get("tariff", {}).get("simulated")),
    })
    conn.commit()
    return report


def live_cases(conn):
    out = []
    for r in conn.execute("SELECT case_id, payload FROM cases WHERE status='open'"):
        if json.loads(r["payload"] or "{}").get("provider_key"):
            out.append(r["case_id"])
    return out
