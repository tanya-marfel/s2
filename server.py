"""Single-file web UI. Standard library only - nothing to install.

    python3 server.py        then open http://localhost:8000

Endpoints:
    GET /                      the guided case flow (voice, documents, investigation, paperwork)
    GET /explore, /?drg=       the procedure explorer
    GET /api/procedures?q=     search the ingested tariff catalogue
    GET /api/route?drg=&wait=  two-route comparison with provenance
    GET /api/providers?drg=    where the procedure is available across the EU
    POST /api/case/{start,upload,sample,step,paperwork}
    GET /api/scenarios         the example cases, one per operation
    GET /api/homewait?drg=&region=   reported median wait in the home country
    GET /api/match?drg=        the procedure's code in each national system
    GET /api/watch?drg=&provider=&action=open|check|wake|approve|reject[&simulate=&simulate_tariff=&proposal=]   live agent wake-up on a chosen provider
    GET /api/timeline?case=    loop states and audit trail
    GET /api/cohort            catalogue-wide figures
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cbh.core import cases  # noqa: E402
from cbh.core.provenance import SOURCES, Cited  # noqa: E402
from cbh.core.routes import Tariff, Waiting, compare_routes  # noqa: E402
from cbh.core import countries  # noqa: E402
from cbh.core import providers as providers_mod  # noqa: E402
from cbh.core import watch as watch_mod  # noqa: E402
from cbh.core import wake as wake_mod  # noqa: E402
from cbh.core import replan  # noqa: E402
from cbh.core import casefile  # noqa: E402
from cbh.core import jgp, nfz, nl, se_waits  # noqa: E402
from cbh.core.draft import draft_application  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import tick as tick_mod  # noqa: E402

SEK_PER_EUR = 11.45
PLN_PER_EUR = 4.30
GUARANTEE_DAYS = 90
PORT = int(os.environ.get("PORT", "8000"))

# Resolve data relative to this file, not the working directory, so the same
# code serves locally and from a serverless function.
ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "out")

_TARIFFS = None
_LABELS_EN = None


def tariffs():
    global _TARIFFS
    if _TARIFFS is None:
        path = os.path.join(OUT, "se_tariffs.json")
        if not os.path.exists(path):
            raise SystemExit("run: python3 cbh/ingest/se_norddrg.py")
        with open(path, encoding="utf-8") as fh:
            _TARIFFS = json.load(fh)
    return _TARIFFS


def labels_en():
    """English expansions of the Swedish DRG shorthand, presentational only."""
    global _LABELS_EN
    if _LABELS_EN is None:
        path = os.path.join(OUT, "se_labels_en.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                _LABELS_EN = json.load(fh)
        else:
            _LABELS_EN = {}
    return _LABELS_EN


def search(q, limit=40):
    q = (q or "").strip().lower()
    en = labels_en()
    rows = tariffs()["tariffs"]
    if q:
        rows = [r for r in rows
                if q in r["drg"].lower() or q in r["label_sv"].lower()
                or q in en.get(r["drg"], "").lower()]
    rows = sorted(rows, key=lambda r: -r["price_stockholm"]["value"])[:limit]
    return [{
        "drg": r["drg"], "label": r["label_sv"],
        "label_en": en.get(r["drg"], ""),
        "care_type": r["care_type"],
        "sek": int(r["price_stockholm"]["value"]),
        "eur": round(r["price_stockholm"]["value"] / SEK_PER_EUR),
    } for r in rows]


ABROAD_WAIT_ASSUMED = 21
WAIT_SOURCES = {"PL": "pl_nfz_terminy_leczenia", "NL": "nl_nza_zorgbeeld"}


def resolve_provider(drg, row, key):
    """The chosen provider as the registers report it, or None."""
    if not key:
        return None
    found = providers_mod.find(key, drg, row["label_sv"], labels_en().get(drg, ""))
    if not found:
        return None
    country_name, p, source = found
    return {"code": key.split("|", 1)[0].upper(), "country": country_name,
            "row": p, "source": source}


def route_for(drg, wait_days, home="SE", abroad_eur=None, abroad_wait=None,
              travel_eur=250, provider=None):
    country = countries.get(home)
    bundle = tariffs()
    row = next((r for r in bundle["tariffs"] if r["drg"] == drg), None)
    if row is None:
        return {"error": "unknown DRG %s" % drg}

    # With a chosen provider the abroad wait is that provider's own reported
    # figure from the national register; otherwise it is an assumption.
    chosen = resolve_provider(drg, row, provider)
    treat_code = chosen["code"] if chosen else "PL"
    wait_cited = None
    if chosen and chosen["row"].get("avg_wait_days") is not None:
        abroad_wait = chosen["row"]["avg_wait_days"]
        wait_cited = Cited(
            abroad_wait, "days", WAIT_SOURCES[treat_code], 0,
            "%s, %s: reported average wait, updated %s" % (
                chosen["row"]["provider"], chosen["row"]["locality"],
                chosen["row"].get("updated") or "n/a"),
            chosen["source"]["name"])
    if wait_cited is None:
        abroad_wait = ABROAD_WAIT_ASSUMED if abroad_wait is None else abroad_wait
        wait_cited = Cited(abroad_wait, "days", "assumed_admission", 0,
                           "assumed admission interval", "assumption")

    price = row["price_stockholm"]
    sek = price["value"]
    home_eur = round(sek / SEK_PER_EUR, 2)
    # Absent an ingested foreign tariff, model the treatment state at a
    # representative share of the Swedish tariff and label it as modelled.
    modelled = abroad_eur is None
    abroad_eur = abroad_eur if abroad_eur is not None else round(home_eur * 0.674, 2)

    home = Tariff("SE", drg, row["label_sv"], Cited(
        home_eur, "EUR", price["source_id"], price["page"], price["raw_line"],
        "%d SEK at %.2f SEK/EUR" % (int(sek), SEK_PER_EUR)))
    abroad = Tariff(treat_code, "—", "Treatment-country public tariff", Cited(
        abroad_eur, "EUR", "pl_nfz_jgp_2025", 0,
        "NFZ catalogue" if not modelled else "modelled from SE tariff",
        "modelled" if modelled else "NFZ published catalogue"))

    result = compare_routes(
        home, abroad,
        Waiting("SE", row["label_sv"], Cited(wait_days, "days", "se_vantetider", 0,
                                             "regional reported wait", "")),
        Waiting(treat_code, row["label_sv"], wait_cited),
        local_patient_charge_eur=0.0, travel_cost_eur=travel_eur,
        guarantee_days=country.guarantee_days,
        guarantee_name=country.guarantee_name,
        guarantee_binding=country.binding)

    src = SOURCES[price["source_id"]]
    result["provenance"] = {
        "home": {
            "title": src.title, "publisher": src.publisher, "url": src.url,
            "page": price["page"] + 1, "raw_line": price["raw_line"],
            "sha256": (bundle["source"]["sha256"] or "")[:32],
            "sek": int(sek),
        },
        "abroad_modelled": modelled,
    }
    result["label"] = row["label_sv"]
    result["label_en"] = labels_en().get(drg, "")
    result["drg"] = drg
    result["guarantee_days"] = country.guarantee_days
    result["home_country"] = country.to_dict()
    # Tariffs are ingested for Sweden only, so for any other home country the
    # financial comparison is illustrative and must say so.
    result["tariffs_are_home"] = country.code == "SE"
    # Where to be treated. Without a chosen provider the admission interval is an
    # assumption; with one it is that provider's reported wait from the register.
    result["destination"] = {
        "country": treat_code,
        "country_name": chosen["country"] if chosen else "Poland",
        "ncp": countries.ncp(treat_code),
        "home_ncp": countries.ncp(country.code),
        "eu_directory": countries.EU_NCP_DIRECTORY,
        "provider": chosen["row"] if chosen else None,
        "abroad_wait_days": abroad_wait,
        "abroad_wait_is_assumption": wait_cited.source_id == "assumed_admission",
        "abroad_wait_source": wait_cited.to_dict(),
    }
    return result


def providers_for(drg, home="SE"):
    """Providers across the EU countries we hold data for that offer this procedure."""
    row = next((r for r in tariffs()["tariffs"] if r["drg"] == drg), None)
    if row is None:
        return {"error": "unknown DRG %s" % drg}
    return providers_mod.lookup(drg, row["label_sv"], labels_en().get(drg, ""),
                                exclude=[home])


def home_wait(drg, home="SE", region=None):
    """Reported waiting time in the home country for this procedure."""
    if (home or "").upper() != "SE":
        return {"matched": False,
                "reason": "waiting-time data is ingested for Sweden only"}
    row = next((r for r in tariffs()["tariffs"] if r["drg"] == drg), None)
    if row is None:
        return {"error": "unknown DRG %s" % drg}
    out = se_waits.lookup(drg, row["label_sv"], labels_en().get(drg, ""),
                          int(region) if region else None)
    out["regions"] = se_waits.regions()
    return out


def crosswalk(drg):
    """The same procedure in each national coding system we hold data for."""
    row = next((r for r in tariffs()["tariffs"] if r["drg"] == drg), None)
    if row is None:
        return {"error": "unknown DRG %s" % drg}
    sv, en = row["label_sv"], labels_en().get(drg, "")

    def attempt(fn):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - one system failing must not hide the rest
            return {"matched": False, "reason": str(exc)}

    def wrap(m):
        return {"matched": bool(m.get("benefit")), "benefit": m.get("benefit"),
                "confidence": m.get("confidence"), "reason": m.get("reason")}

    return {
        "sweden": {"code": drg, "label": sv, "label_en": en},
        "poland_jgp": attempt(lambda: jgp.lookup(drg, sv, en)),
        "poland_register": attempt(lambda: wrap(nfz.match_benefit(drg, sv, en))),
        "netherlands_register": attempt(lambda: wrap(nl.match_treatment(drg, sv, en))),
    }


def _provider_line(p):
    if not p:
        return None
    return ", ".join(x for x in (p.get("provider"), p.get("place"), p.get("address"),
                                 p.get("postcode"), p.get("locality")) if x)


def draft(drg, wait_days, home="SE", patient="A. Patient", provider=None,
          kind="application"):
    """Run the drafter for one case and return the letter with its verdict.

    `application` goes to the home authority in the home language; `referral` is
    an enquiry to the chosen provider in the treatment country's language.
    """
    r = route_for(drg, wait_days, home=home, provider=provider)
    if r.get("error"):
        return r
    if kind == "referral" and not r["destination"]["provider"]:
        return {"error": "choose a provider first"}
    modelled_tariff = r["provenance"]["abroad_modelled"]
    home_c = countries.get(home)
    language = (home_c.language if kind == "application"
                else countries.language_of(r["destination"]["country"]))
    rec, hc = r["recommendation"], r["home_country"]
    facts = {
        "patient": patient,
        "home": hc["name"],
        "procedure": r.get("label_en") or r["label"],
        "drg": drg,
        "treatment_country": r["destination"]["country_name"],
        "authority": home_c.authority,
        "provider": _provider_line(r["destination"]["provider"]),
        "home_tariff_eur": r["tariffs"]["home_eur"]["value"],
        # A modelled treatment-country tariff is not an official figure, so it
        # must not reach a letter that tells the authority its figures are.
        "abroad_tariff_eur": None if modelled_tariff else r["tariffs"]["abroad_eur"]["value"],
        "home_wait_days": int(wait_days),
        "guarantee_days": hc["guarantee_days"],
        "abroad_wait_days": r["destination"]["abroad_wait_days"],
        "abroad_wait_is_assumption": r["destination"]["abroad_wait_is_assumption"],
        "route": rec["route"],
        "capital_at_risk_eur": None if modelled_tariff else rec["capital_at_risk_avoided_eur"],
        "sources": [r["provenance"]["home"]["title"]] + (
            [r["destination"]["abroad_wait_source"]["note"]
             + " (" + r["destination"]["abroad_wait_source"]["raw_line"] + ")"]
            if not r["destination"]["abroad_wait_is_assumption"] else []),
    }
    letter, problems = draft_application(facts, language=language, kind=kind)
    return {"letter": letter, "problems": problems, "patient": patient,
            "route": rec["route"], "language": language, "kind": kind,
            "addressee": (r["destination"]["provider"]["provider"]
                          if kind == "referral"
                          else home_c.authority or "the competent health authority")}


def _reset_case(conn, case_id):
    conn.execute("DELETE FROM events WHERE case_id=? OR loop_id IN "
                 "(SELECT loop_id FROM loops WHERE case_id=?)", (case_id, case_id))
    conn.execute("DELETE FROM loops WHERE case_id=?", (case_id,))
    conn.execute("DELETE FROM proposals WHERE case_id=?", (case_id,))
    conn.execute("DELETE FROM cases WHERE case_id=?", (case_id,))
    conn.commit()


def watch(drg, wait_days, home, provider, action="open", simulate=None,
          patient="A. Patient", simulate_tariff=None, proposal=None):
    """Open (or re-read) a live case on the chosen provider's reported wait."""
    r = route_for(drg, wait_days, home=home, provider=provider)
    if r.get("error"):
        return r
    dest = r["destination"]
    if not dest["provider"]:
        return {"error": "choose a provider first"}
    row = next(x for x in tariffs()["tariffs"] if x["drg"] == drg)
    case_id = "case-%s-live" % drg
    conn = cases.connect()

    existing = conn.execute("SELECT payload FROM cases WHERE case_id=?",
                            (case_id,)).fetchone()
    same = existing and json.loads(existing["payload"] or "{}").get(
        "provider_key") == dest["provider"]["key"]
    if action == "open" or not same:
        _reset_case(conn, case_id)
        watch_mod.open_watch(conn, case_id, patient, home, drg, row["label_sv"],
                             labels_en().get(drg, ""), dest,
                             dest["abroad_wait_days"], wait_days,
                             filed_tariff_sek=r["provenance"]["home"]["sek"],
                             guarantee_days=r["guarantee_days"])

    result = {}
    num = lambda v: None if v in (None, "") else float(v)
    # Writes the replacement letter if the agent decides to propose a switch.
    drafter = lambda kind, key: draft(drg, wait_days, home, "[Patient name]",
                                      provider=key, kind=kind)
    if action == "check":
        result = watch_mod.check(conn, case_id, num(simulate), drafter=drafter)
    elif action in ("approve", "reject"):
        result = {"resolution": replan.resolve(conn, case_id, int(proposal),
                                               action == "approve")}
    elif action == "wake":
        rep = wake_mod.wake_case(conn, case_id, simulate_wait=num(simulate),
                                 simulate_tariff_pct=num(simulate_tariff),
                                 drafter=drafter)
        result = {"observation": rep["observation"], "actions": rep["actions"],
                  "sources": rep["sources"], "tariff": rep.get("tariff"),
                  "home_queue": rep.get("home_queue")}
    data = timeline(case_id)
    data.update(result)
    data["proposals"] = replan.pending(conn, case_id)
    data["case_id"] = case_id
    return data


def replay(case_id, step, drg="H02E", wait_days=240, patient="Anna L."):
    """Reset the case and re-apply the demo timeline up to `step`.

    Rebuilding from scratch each time keeps the endpoint idempotent, so the UI
    can step forward, jump back, or be re-run mid-demo without the ledger
    drifting into a confusing half-state.
    """
    conn = cases.connect()
    _reset_case(conn, case_id)

    result, row = pipeline_mod.build(patient, drg, wait_days, 5116.28, 21, 250)
    cases.open_case(conn, case_id, patient, "SE", row["label_sv"], drg,
                    payload=result["recommendation"])
    pipeline_mod.seed_loops(conn, case_id, result)

    steps = tick_mod.DEMO_TIMELINE
    step = max(0, min(int(step), len(steps)))
    actions = []
    for label, facts in steps[:step]:
        actions = cases.tick(conn, case_id, facts)

    data = timeline(case_id)
    data["step"] = step
    data["total_steps"] = len(steps)
    data["label"] = steps[step - 1][0] if step else "Day 0 — case opened"
    data["actions"] = actions
    return data


def timeline(case_id):
    # Serverless filesystems are ephemeral, so fall back to a snapshot exported
    # by `python3 export_snapshot.py` when no live ledger is present.
    if not os.path.exists(cases.DB_PATH):
        snap = os.path.join(OUT, "timeline_snapshot.json")
        if os.path.exists(snap):
            with open(snap, encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get(case_id, {"loops": [], "events": [], "case": None})
        return {"loops": [], "events": [], "case": None}
    conn = cases.connect()
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()

    events = [dict(r) for r in cases.history(conn, case_id)]
    # The events table is append-only, so re-running the pipeline leaves earlier
    # lifecycles in place. Show only the current one.
    last_open = max(
        (i for i, e in enumerate(events) if e["action"] == "case_opened"),
        default=None,
    )
    if last_open is not None:
        events = events[last_open:]

    for e in events:
        # Stored as a JSON string; decode so the UI shows readable text.
        if e.get("detail"):
            try:
                parsed = json.loads(e["detail"])
                e["detail"] = ", ".join("%s: %s" % kv for kv in parsed.items())
            except (ValueError, AttributeError):
                pass

    return {
        "case": dict(row) if row else None,
        "loops": [dict(r) for r in cases.loops_for(conn, case_id)],
        "events": events,
    }


def cohort():
    path = os.path.join(OUT, "cohort.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


_PAGE_CACHE = None


def page():
    """UI markup lives in ui.html so it can be edited without touching server code."""
    global _PAGE_CACHE
    if _PAGE_CACHE is None:
        with open(os.path.join(ROOT, "ui.html"), encoding="utf-8") as fh:
            _PAGE_CACHE = fh.read()
    return _PAGE_CACHE



SAMPLES = os.path.join(ROOT, "samples")
MAX_BODY = 8 * 1024 * 1024
_CASE_PAGE = None
_SCENARIOS = None


def case_page():
    global _CASE_PAGE
    if _CASE_PAGE is None:
        with open(os.path.join(ROOT, "case.html"), encoding="utf-8") as fh:
            _CASE_PAGE = fh.read()
    return _CASE_PAGE


def scenarios():
    """The example cases, one per operation. Written by samples/make_samples.py."""
    global _SCENARIOS
    if _SCENARIOS is None:
        try:
            with open(os.path.join(SAMPLES, "scenarios.json"), encoding="utf-8") as fh:
                _SCENARIOS = json.load(fh)
        except (OSError, ValueError):
            _SCENARIOS = []
    return _SCENARIOS


def case_api(path, body):
    """The guided case flow. Each step is its own call so the UI can show progress."""
    import base64
    conn = cases.connect()
    if path == "/api/case/start":
        st = casefile.new_case(conn, (body.get("statement") or "").strip(),
                               body.get("home") or "SE", body.get("today"))
        return casefile.public(st)
    cid = body.get("case")
    if path == "/api/case/upload":
        data = base64.b64decode(body.get("content_b64") or "")
        return casefile.public(casefile.add_document(conn, cid, body.get("filename") or "upload", data))
    if path == "/api/case/sample":
        name = os.path.basename(body.get("name") or "")
        full = os.path.join(SAMPLES, name)
        if not (name.endswith("_SAMPLE.pdf") and os.path.isfile(full)):
            return {"error": "unknown sample"}
        with open(full, "rb") as fh:
            return casefile.public(casefile.add_document(conn, cid, name, fh.read()))
    if path == "/api/case/step":
        step = body.get("step")
        fn = {"identify": casefile.identify, "rules": casefile.rules, "compare": casefile.compare,
              "evidence": casefile.evidence, "revise": casefile.revise}.get(step)
        if fn is None:
            return {"error": "unknown step"}
        return casefile.public(fn(conn, cid))
    if path == "/api/case/paperwork":
        out = casefile.paperwork(conn, cid, language=body.get("language") or "English")
        return dict(casefile.public(casefile.load(conn, cid)), paperwork=out)
    return {"error": "unknown case endpoint"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        parts = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(json.dumps({"error": "upload too large"}))
            body = json.loads(self.rfile.read(n) or b"{}")
            if parts.path.startswith("/api/case/"):
                return self._send(json.dumps(case_api(parts.path, body)))
        except Exception as exc:  # noqa: BLE001 - keep the demo alive
            return self._send(json.dumps({"error": str(exc)}))
        self.send_error(404)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parts.query)
        one = lambda k, d=None: (qs.get(k) or [d])[0]

        try:
            # The guided case flow is the front door; the explorer keeps its deep
            # links (/?drg=...) and also lives at /explore.
            if parts.path == "/" and "drg" not in qs:
                return self._send(case_page(), "text/html")
            if parts.path in ("/", "/explore"):
                return self._send(page(), "text/html")
            if parts.path == "/case":
                return self._send(case_page(), "text/html")
            if parts.path == "/api/case":
                return self._send(json.dumps(casefile.public(
                    casefile.load(cases.connect(), one("case", "")))))
            if parts.path == "/api/scenarios":
                return self._send(json.dumps(scenarios()))
            if parts.path == "/api/samples":
                return self._send(json.dumps(sorted(
                    f for f in os.listdir(SAMPLES) if f.endswith("_SAMPLE.pdf"))))
            if parts.path.startswith("/samples/"):
                name = os.path.basename(parts.path)
                full = os.path.join(SAMPLES, name)
                if name.endswith("_SAMPLE.pdf") and os.path.isfile(full):
                    with open(full, "rb") as fh:
                        return self._send(fh.read(), "application/pdf")
            if parts.path == "/api/procedures":
                return self._send(json.dumps(search(one("q", ""))))
            if parts.path == "/api/route":
                return self._send(json.dumps(route_for(
                    one("drg", "H02E"), float(one("wait", 240)),
                    home=one("home", "SE"), provider=one("provider"))))
            if parts.path == "/api/providers":
                return self._send(json.dumps(providers_for(one("drg", "H02E"), one("home", "SE"))))
            if parts.path == "/api/homewait":
                return self._send(json.dumps(home_wait(
                    one("drg", "H02E"), one("home", "SE"), one("region"))))
            if parts.path == "/api/match":
                return self._send(json.dumps(crosswalk(one("drg", "H02E"))))
            if parts.path == "/api/watch":
                return self._send(json.dumps(watch(
                    one("drg", "H02E"), float(one("wait", 240)),
                    one("home", "SE"), one("provider"),
                    action=one("action", "open"), simulate=one("simulate"),
                    simulate_tariff=one("simulate_tariff"),
                    proposal=one("proposal"))))
            if parts.path == "/api/replay":
                return self._send(json.dumps(replay(
                    one("case", "case-H02E-anna"), one("step", 0),
                    drg=one("drg", "H02E"), wait_days=float(one("wait", 240)))))
            if parts.path == "/api/draft":
                return self._send(json.dumps(draft(
                    one("drg", "H02E"), float(one("wait", 240)),
                    home=one("home", "SE"),
                    patient=one("patient", "A. Patient"),
                    provider=one("provider"), kind=one("kind", "application"))))
            if parts.path == "/api/countries":
                return self._send(json.dumps(countries.listing()))
            if parts.path == "/api/timeline":
                return self._send(json.dumps(timeline(one("case", ""))))
            if parts.path == "/api/cohort":
                return self._send(json.dumps(cohort()))
        except Exception as exc:  # noqa: BLE001 - keep the demo alive
            return self._send(json.dumps({"error": str(exc)}))

        self.send_error(404)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    tariffs()
    print("S2 running on http://localhost:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
