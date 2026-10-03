"""A patient's case, from first words to filed paperwork.

The case is a story the agent works through, one inspectable step at a time:

    start      the patient's own words become quoted, verified facts
    documents  referral, waiting-list notice, clinic offers: read, quoted, verified
    identify   which procedure is this? (NordDRG, from the referral's own wording)
    rules      does the home wait support an Art. 20(2) undue-delay argument?
    evidence   what do the rules need that the documents do not yet show?
    compare    each clinic offer under both legal routes, in separate figures
    revise     a clinic's reply changes the picture; recompute and say what is needed
    paperwork  application, attachment checklist, request for missing information

Every figure is arithmetic over values read from a document (with its quote) or from
a source registry. A model only reads, matches and writes. Clinic offers in the demo
are synthetic samples and are labelled as such wherever they appear.

State lives in the ledger (`cases.payload`) so a case survives restarts; each step
also writes an `events` row.
"""

import datetime
import json
import os
import re
import uuid
from typing import Any, Dict, List, Optional

from . import cases, countries, docs, intake, se_waits

# Fixed demo rates, not a dated source. Same constants the rest of the app uses.
# A clinic offer may be priced in any EU/EEA currency, so every one we can convert
# is listed here; an offer in a currency we do not hold is reported as unusable
# rather than converted at a guessed rate.
PER_EUR = {"EUR": 1.0, "PLN": 4.30, "SEK": 11.45, "DKK": 7.46, "CZK": 25.20,
           "HUF": 395.0, "RON": 4.98, "BGN": 1.96, "NOK": 11.80, "ISK": 150.0}
TRAVEL_EUR = 250  # same flat estimate as the route comparison


# --- persistence -----------------------------------------------------------

def load(conn, case_id: str) -> Dict[str, Any]:
    row = conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError(case_id)
    return json.loads(row["payload"] or "{}")


def save(conn, case_id: str, state: Dict[str, Any]) -> None:
    conn.execute("UPDATE cases SET payload=? WHERE case_id=?",
                 (json.dumps(state, ensure_ascii=False), case_id))
    conn.commit()


def trace(conn, case_id: str, state: Dict[str, Any], step: str, text: str, **detail) -> None:
    """One line of the investigation, kept in the state and in the audit log."""
    state.setdefault("trace", []).append(
        {"at": datetime.datetime.now().strftime("%H:%M:%S"), "step": step, "text": text})
    cases._log(conn, "case_" + step, case_id=case_id, detail=dict(detail, text=text))


def _eur(value) -> str:
    """A euro amount in a sentence. Rounded to whole euro, grouped for reading."""
    return "\u20ac{:,}".format(int(round(value)))


def _today(state) -> datetime.date:
    return datetime.date.fromisoformat(state["today"]) if state.get("today") \
        else datetime.date.today()


def _d(iso: Optional[str]) -> Optional[datetime.date]:
    try:
        return datetime.date.fromisoformat(iso) if iso else None
    except ValueError:
        return None


def public(state: Dict[str, Any]) -> Dict[str, Any]:
    """State for the UI: drop the full document text."""
    out = dict(state)
    out["documents"] = [{k: v for k, v in d.items() if k != "text"}
                        for d in state.get("documents", [])]
    return out


# --- start and documents ---------------------------------------------------

def new_case(conn, statement: str, home: str = "SE", today: Optional[str] = None,
             client=None) -> Dict[str, Any]:
    case_id = "case-" + uuid.uuid4().hex[:8]
    state = {"kind": "intake_case", "home": home.upper(), "documents": [], "offers": [],
             "revisions": [], "trace": [], "today": today}
    cases.open_case(conn, case_id, "[Patient]", state["home"], "intake", None, payload=state)
    state["case_id"] = case_id
    state["intake"] = intake.extract_intake(statement, client)
    got = state["intake"]["fields"]
    trace(conn, case_id, state, "intake",
          "Heard: " + ("; ".join('"%s"' % f["quote"] for f in got.values()) or "nothing usable"))
    for r in state["intake"]["rejected"]:
        trace(conn, case_id, state, "intake_rejected",
              "Dropped %s: %s" % (r["field"], r["reason"]))
    save(conn, case_id, state)
    return state


def add_document(conn, case_id: str, filename: str, data: bytes, client=None) -> Dict[str, Any]:
    state = load(conn, case_id)
    doc = docs.extract(filename, data, client)
    doc["applied"] = False
    state["documents"] = [d for d in state["documents"] if d["filename"] != filename] + [doc]
    if doc.get("error"):
        trace(conn, case_id, state, "document", "%s: %s" % (filename, doc["error"]))
    else:
        trace(conn, case_id, state, "document",
              "Read %s as %s: %d fact(s) quoted, %d rejected%s" % (
                  filename, doc["kind"].replace("_", " "), len(doc["fields"]),
                  len(doc["rejected"]), " (synthetic sample)" if doc.get("synthetic") else ""))
    save(conn, case_id, state)
    return state


def _val(doc, name):
    f = doc["fields"].get(name)
    return f["value"] if f else None


def _first(state, kind):
    return next((d for d in state["documents"] if d["kind"] == kind and not d.get("error")), None)


# --- identify --------------------------------------------------------------

def _procedure_texts(state) -> List[Dict[str, Any]]:
    """Every independent wording of the operation in the case, best source first.

    A referral is written in the home country's clinical shorthand, the patient
    speaks plain language and a clinic's offer describes the same operation in its
    own words. Each is a separate shot at the catalogue, so a wording the matcher
    will not commit to does not sink the case.
    """
    out = []
    ref = _first(state, "referral")
    if ref and _val(ref, "procedure"):
        out.append({"text": _val(ref, "procedure"), "source": "the referral",
                    "file": ref["filename"]})
    f = (state.get("intake") or {}).get("fields", {}).get("procedure")
    if f:
        out.append({"text": f["value"], "source": "what you said", "file": None})
    wl = _first(state, "waiting_letter")
    if wl and _val(wl, "procedure"):
        out.append({"text": _val(wl, "procedure"), "source": "the waiting-list letter",
                    "file": wl["filename"]})
    for d in state["documents"]:
        if d["kind"] == "clinic_offer" and not d.get("error") and _val(d, "procedure"):
            out.append({"text": _val(d, "procedure"), "source": "a clinic's offer",
                        "file": d["filename"]})
    seen, uniq = set(), []
    for cand in out:
        key = re.sub(r"\s+", " ", cand["text"].lower()).strip()
        if key and key not in seen:
            seen.add(key)
            uniq.append(cand)
    return uniq


def identify(conn, case_id: str, client=None) -> Dict[str, Any]:
    state = load(conn, case_id)
    cands = _procedure_texts(state)
    if not cands:
        trace(conn, case_id, state, "identify",
              "No operation is named in your documents or in what you said.")
        state["procedure"] = {"matched": False, "reason": "no procedure stated", "attempts": []}
        save(conn, case_id, state)
        return state

    attempts, m = [], None
    for cand in cands:
        m = intake.match_drg(cand["text"], client)
        m["from_text"], m["source"], m["source_file"] = cand["text"], cand["source"], cand["file"]
        attempts.append({"text": cand["text"], "source": cand["source"],
                         "matched": m["matched"], "reason": m.get("reason")})
        if m["matched"]:
            break
        trace(conn, case_id, state, "identify",
              "Could not place \"%s\" (from %s): %s." % (cand["text"], cand["source"], m["reason"]))
    m["attempts"] = attempts
    state["procedure"] = m
    if m["matched"]:
        tariff = _home_tariff(m["drg"])
        state["home_tariff"] = tariff
        trace(conn, case_id, state, "identify",
              "From %s (\"%s\"): matched to the Swedish price-list group %s, \"%s\" (%d%% match, %s). "
              "Sweden's tariff for it is %s." % (
                  m["source"], m["from_text"], m["drg"], m["label_en"] or m["label_sv"],
                  round((m.get("confidence") or 0) * 100),
                  "day care" if tariff["care_type"] == "outpatient" else "inpatient",
                  _eur(tariff["eur"])))
    else:
        trace(conn, case_id, state, "identify",
              "None of the %d description(s) of the operation could be matched to the Swedish "
              "catalogue with enough confidence, so no figures are shown." % len(attempts))
    save(conn, case_id, state)
    return state


def _home_tariff(drg: str) -> Dict[str, Any]:
    from .nfz import OUT
    with open(os.path.join(OUT, "se_tariffs.json"), encoding="utf-8") as fh:
        row = next(r for r in json.load(fh)["tariffs"] if r["drg"] == drg)
    p = row["price_stockholm"]
    return {"sek": p["value"], "eur": p["value"] / PER_EUR["SEK"], "page": p["page"] + 1,
            "raw_line": p["raw_line"], "source_id": p["source_id"],
            "care_type": row["care_type"]}


# --- rules -----------------------------------------------------------------

def rules(conn, case_id: str) -> Dict[str, Any]:
    state = load(conn, case_id)
    c = countries.get(state["home"])
    wl = _first(state, "waiting_letter")
    out: Dict[str, Any] = {"guarantee_days": c.guarantee_days, "guarantee_name": c.guarantee_name,
                           "binding": c.binding}
    registered = _d(_val(wl, "registered_date")) if wl else None
    expected = _d(_val(wl, "expected_date")) if wl else None
    today = _today(state)
    if registered and expected:
        out.update(registered=registered.isoformat(), expected=expected.isoformat(),
                   waited_so_far_days=(today - registered).days,
                   projected_total_wait_days=(expected - registered).days,
                   quotes={"registered_date": wl["fields"]["registered_date"]["quote"],
                           "expected_date": wl["fields"]["expected_date"]["quote"]})
        out["undue_delay_argument"] = out["projected_total_wait_days"] > c.guarantee_days
        out["basis"] = (
            "Registered %s, treatment expected %s: a projected wait of %d days against the "
            "%d-day %s. %s" % (
                registered, expected, out["projected_total_wait_days"], c.guarantee_days,
                c.guarantee_name,
                "That supports an Art. 20(2) argument that treatment cannot be given within a "
                "medically justifiable time." if out["undue_delay_argument"]
                else "That is within the guarantee, so an undue-delay argument is weak."))
        trace(conn, case_id, state, "rules", out["basis"])
    else:
        out["undue_delay_argument"] = None
        out["basis"] = "No waiting-list notice with both dates, so the delay cannot be assessed."
        trace(conn, case_id, state, "rules", out["basis"])

    proc = state.get("procedure") or {}
    if proc.get("matched") and state["home"] == "SE":
        try:
            ctx = se_waits.lookup(proc["drg"], proc["label_sv"], proc.get("label_en", ""))
            if ctx.get("wait"):
                out["context"] = {"median_days": ctx["wait"]["median_days"],
                                  "month": ctx["wait"]["month"], "region": ctx["wait"]["region"],
                                  "source": ctx["source"]}
                trace(conn, case_id, state, "rules",
                      "For context, Sweden's reported median wait for this operation is %s days "
                      "(%s). Your own wait is what counts." % (
                          ctx["wait"]["median_days"], ctx["wait"]["month"]))
        except (OSError, ValueError):
            pass
    state["rules"] = out
    save(conn, case_id, state)
    return state


# --- offers and comparison --------------------------------------------------

def _offers(state) -> List[Dict[str, Any]]:
    offers = []
    for d in state["documents"]:
        if d["kind"] != "clinic_offer" or d.get("error"):
            continue
        f = {k: _val(d, k) for k in docs.FIELDS["clinic_offer"]}
        offers.append({
            "clinic": f.get("clinic"), "country": f.get("country"), "price": f.get("price"),
            "currency": f.get("currency"), "earliest_admission": f.get("earliest_admission"),
            "valid_until": f.get("valid_until"), "accepts_s2": f.get("accepts_s2") or "not_stated",
            "accepts_s2_source": "offer", "file": d["filename"], "synthetic": d.get("synthetic"),
            "quotes": {k: d["fields"][k]["quote"] for k in d["fields"]},
        })
    for d in state["documents"]:  # replies override what the offer said
        if d["kind"] != "clinic_reply" or d.get("error") or not d["fields"].get("accepts_s2"):
            continue
        name = (_val(d, "clinic") or "").lower()
        for o in offers:
            if name and o["clinic"] and (name in o["clinic"].lower() or o["clinic"].lower() in name):
                o["accepts_s2"] = _val(d, "accepts_s2")
                o["accepts_s2_source"] = "reply"
                o["reply_file"] = d["filename"]
                o["quotes"]["accepts_s2"] = d["fields"]["accepts_s2"]["quote"]
    return offers


def compare(conn, case_id: str) -> Dict[str, Any]:
    state = load(conn, case_id)
    proc, tariff = state.get("procedure") or {}, state.get("home_tariff")
    c = countries.get(state["home"])
    if not (proc.get("matched") and tariff):
        why = ("The operation named in your papers could not be placed in the Swedish price "
               "list with enough confidence, so there is no tariff to measure a refund against."
               if proc.get("attempts") else
               "No operation has been identified yet. A referral naming the operation is needed.")
        state["comparison"] = {"rows": [], "unavailable": why, "home_country": c.name,
                               "tariff_country": "Sweden",
                               "cannot_advance": "cannot_advance_money" in
                                                 state.get("intake", {}).get("fields", {}),
                               "recommendation": {"clinic": None, "route": None,
                                                  "route_key": None, "why": why}}
        trace(conn, case_id, state, "compare", "Cannot compare: " + why)
        save(conn, case_id, state)
        return state
    today = _today(state)
    expected = _d((state.get("rules") or {}).get("expected"))
    cannot_advance = "cannot_advance_money" in state.get("intake", {}).get("fields", {})
    rows = []
    for o in _offers(state):
        rate = PER_EUR.get(o["currency"] or "")
        if not (o["price"] and rate):
            reason = ("no price could be read from the offer" if not o["price"]
                      else "no exchange rate is held for %s" % (o["currency"] or "an unstated currency"))
            rows.append({"clinic": o["clinic"], "country": o["country"], "file": o["file"],
                         "synthetic": o["synthetic"], "error": reason, "offer": o})
            continue
        price_eur = o["price"] / rate
        adm = _d(o["earliest_admission"])
        timing = {"admission": o["earliest_admission"],
                  "days_until": (adm - today).days if adm else None,
                  "days_earlier_than_home": (expected - adm).days if adm and expected else None,
                  "offer_valid_until": o["valid_until"]}
        reimb = min(tariff["eur"], price_eur)
        directive = {
            "route": "Directive 2011/24/EU", "available": True,
            "upfront_eur": round(price_eur + TRAVEL_EUR, 2),
            "reimbursement_eur": round(reimb, 2),
            "remaining_eur": round(price_eur + TRAVEL_EUR - reimb, 2),
            "conflicts_with_constraint": cannot_advance,
            "when": "Full payment due on the day of admission (%s)." % (
                o["earliest_admission"] or "date not stated"),
            "timing": ["Pay the full price at admission (%s)." % (o["earliest_admission"] or "date not stated"),
                       "Claim reimbursement afterwards, capped at the Swedish tariff (%s)." % _eur(tariff["eur"]),
                       "Time to reimbursement: not in our sources; ask Försäkringskassan."],
            "notes": ["Reimbursed at home tariff, never above what was paid (Directive Art. 7(4))."],
        }
        if tariff["care_type"] == "outpatient":
            directive["timing"].insert(0, "No prior authorisation is normally needed for care without "
                                          "an overnight stay; confirm with Försäkringskassan.")
        s2_state = o["accepts_s2"]
        s2 = {
            "route": "S2 / Regulation 883/2004",
            "available": {"yes": True, "no": False, "not_stated": None}[s2_state],
            "availability_basis": {"yes": "the clinic states it treats S2 patients",
                                   "no": "the clinic states it does not accept S2",
                                   "not_stated": "the offer does not say; ask the clinic"}[s2_state],
            "upfront_eur": float(TRAVEL_EUR),
            "reimbursement_eur": None,
            "reimbursement_note": "Settled between the two health authorities at the treatment "
                                  "country's public tariff; not paid by you.",
            "remaining_eur": float(TRAVEL_EUR),
            "remaining_note": "Travel estimate only. Patient charges under S2 are not stated.",
            "when": "Authorisation must be granted before admission (%s)." % (
                o["earliest_admission"] or "date not stated"),
            "timing": ["Authorisation must be granted BEFORE admission (%s)." % (o["earliest_admission"] or "date not stated"),
                       "Authorisation decision time: not in our sources; ask Försäkringskassan.",
                       "The offer is valid until %s." % (o["valid_until"] or "an unstated date")],
            "notes": ["Needs the home authority's prior authorisation (Art. 20(2))."],
        }
        rows.append({"clinic": o["clinic"], "country": o["country"], "file": o["file"],
                     "synthetic": o["synthetic"], "price": o["price"], "currency": o["currency"],
                     "price_eur": round(price_eur, 2), "accepts_s2_source": o["accepts_s2_source"],
                     "timing": timing, "directive": directive, "s2": s2, "quotes": o["quotes"]})

    state["comparison"] = {"rows": rows, "home_tariff_eur": round(tariff["eur"], 2),
                           "home_country": c.name, "tariff_country": "Sweden",
                           "care_type": tariff["care_type"],
                           "cannot_advance": cannot_advance,
                           "rates_note": "Fixed demo exchange rates, not a dated source.",
                           "recommendation": _recommend(rows, cannot_advance)}
    rec = state["comparison"]["recommendation"]
    trace(conn, case_id, state, "compare",
          "Compared %d offer(s) under both routes. %s" % (len(rows), rec["why"]))
    save(conn, case_id, state)
    return state


def _recommend(rows, cannot_advance) -> Dict[str, Any]:
    """Rank feasible clinic+route pairs. Arithmetic over the table, no model.

    If the patient cannot advance a large sum, any S2 route (confirmed or still to be
    confirmed) beats paying up front, and the earliest admission wins among them. A
    route the clinic has ruled out is never a candidate.
    """
    cands = []
    for r in rows:
        if r.get("error"):
            continue
        for key, route in (("s2", r["s2"]), ("directive", r["directive"])):
            if route["available"] is False:
                continue
            tier = 0 if (key == "s2" or not cannot_advance) else 1
            cost = route["remaining_eur"] if not cannot_advance else 0
            cands.append((tier, cost, r["timing"]["admission"] or "9999", r["clinic"], r, key, route))
    if not cands:
        return {"clinic": None, "route": None, "why": "No feasible clinic and route."}
    cands.sort(key=lambda c: c[:4])
    _, _, _, _, r, key, route = cands[0]
    why = "Best fit: %s via %s, admission %s" % (r["clinic"], route["route"], r["timing"]["admission"])
    if cannot_advance and key == "s2":
        why += ", because you said you cannot advance a large sum"
    pending = key == "s2" and route["available"] is None
    if pending:
        why += ". It must first confirm it accepts S2"
    out = {"clinic": r["clinic"], "route": route["route"], "route_key": key,
           "pending_s2_confirmation": pending, "why": why + "."}
    alt = next((c for c in cands[1:] if c[3] != r["clinic"]), None)
    if alt:
        out["fallback"] = {"clinic": alt[3], "route": alt[6]["route"],
                           "admission": alt[4]["timing"]["admission"]}
        out["why"] += " Fallback: %s via %s (%s)." % (alt[3], alt[6]["route"],
                                                      alt[4]["timing"]["admission"])
    return out


# --- evidence ---------------------------------------------------------------

def evidence(conn, case_id: str) -> Dict[str, Any]:
    state = load(conn, case_id)
    ref, wl = _first(state, "referral"), _first(state, "waiting_letter")
    offers = _offers(state)
    items = []

    def item(key, label, needed_for, ok, source=None, missing_note=None, status=None):
        items.append({"key": key, "label": label, "needed_for": needed_for,
                      "status": status or ("present" if ok else "missing"),
                      "source": source, "note": None if ok else missing_note})

    item("referral", "Treating doctor's referral naming the operation", ["S2", "Directive"],
         bool(ref and ref["fields"].get("procedure")),
         ref and {"file": ref["filename"], "quote": ref["fields"].get("procedure", {}).get("quote")},
         "Ask your doctor for a referral naming the operation.")
    item("waiting_notice", "Waiting-list notice with registration date and expected date", ["S2"],
         bool(wl and wl["fields"].get("registered_date") and wl["fields"].get("expected_date")),
         wl and {"file": wl["filename"], "quote": wl["fields"].get("expected_date", {}).get("quote")},
         "Ask the hospital for a dated notice stating when you were registered and when treatment is expected.")
    for o in offers:
        item("provider_offer", "Written offer from %s" % o["clinic"], ["S2", "Directive"], True,
             {"file": o["file"], "quote": o["quotes"].get("clinic")})
        items[-1]["clinic"] = o["clinic"]
    if not offers:
        item("provider_offer", "Written offer or details from a clinic abroad", ["S2", "Directive"], False,
             missing_note="Obtain a written offer from a clinic.")
    for o in offers:
        label = "%s confirms it treats patients on an S2 authorisation" % o["clinic"]
        if o["accepts_s2"] == "yes":
            item("clinic_accepts_s2", label, ["S2"], True,
                 {"file": o.get("reply_file") or o["file"], "quote": o["quotes"].get("accepts_s2")})
        elif o["accepts_s2"] == "no":
            item("clinic_accepts_s2", label, ["S2"], False, status="blocked",
                 source={"file": o.get("reply_file") or o["file"], "quote": o["quotes"].get("accepts_s2")},
                 missing_note="The clinic says it does not accept S2. This route is closed there.")
        else:
            item("clinic_accepts_s2", label, ["S2"], False,
                 missing_note="The offer does not say whether S2 is accepted. Ask the clinic.")
        items[-1]["clinic"] = o["clinic"]
    if not offers:
        item("clinic_accepts_s2", "A clinic confirms it treats S2 patients", ["S2"], False,
             missing_note="No clinic offers yet.")
    item("patient_charges", "Patient charges under S2 stated in writing", ["S2"], False,
         missing_note="No offer states the patient charges. Ask the clinic.")
    item("identity", "Your identity and insurance details", ["S2", "Directive"], False,
         status="you_supply", missing_note="Personal details you add yourself; never uploaded here.")
    item("claim_documents", "After treatment: itemised invoice, proof of payment, medical report",
         ["Directive"], False, status="later",
         missing_note="Needed to claim reimbursement after you have paid.")

    state["evidence"] = {
        "items": items,
        "basis": "The agent's checklist from Regulation 883/2004 Art. 20 and Directive 2011/24/EU "
                 "Arts. 7-9. Confirm the exact requirements with Försäkringskassan.",
    }
    miss = [i["label"] for i in items if i["status"] == "missing"]
    blocked = [i["label"] for i in items if i["status"] == "blocked"]
    trace(conn, case_id, state, "evidence",
          "Evidence: %d of %d items present; missing: %s.%s" % (
              sum(1 for i in items if i["status"] == "present"), len(items),
              "; ".join(miss) or "none",
              " Blocked: %s." % "; ".join(blocked) if blocked else ""))
    save(conn, case_id, state)
    return state


# --- the complication -------------------------------------------------------

def revise(conn, case_id: str) -> Dict[str, Any]:
    """Apply clinic replies not yet applied: recompute and say what is now needed."""
    state = load(conn, case_id)
    new = [d for d in state["documents"] if d["kind"] == "clinic_reply" and not d.get("applied")
           and not d.get("error")]
    if not new:
        return state
    before = (state.get("comparison") or {}).get("recommendation")
    for d in new:
        d["applied"] = True
    save(conn, case_id, state)
    compare(conn, case_id)
    evidence(conn, case_id)
    state = load(conn, case_id)
    after = state["comparison"]["recommendation"]

    declined = [o for o in _offers(state) if o["accepts_s2"] == "no"]
    rows = {r["clinic"]: r for r in state["comparison"]["rows"] if not r.get("error")}
    needed, changes = [], []
    for o in declined:
        r = rows.get(o["clinic"])
        changes.append("%s does not accept S2 (\"%s\"): the S2 route is unavailable there." % (
            o["clinic"], o["quotes"].get("accepts_s2")))
        if r:
            d = r["directive"]
            needed.append({
                "route": "Directive at %s" % o["clinic"],
                "summary": "Pay %s up front at admission; claim back about %s afterwards, "
                           "leaving about %s." % (_eur(d["upfront_eur"]), _eur(d["reimbursement_eur"]),
                                                _eur(d["remaining_eur"])),
                "requires": [
                    "Funds for %s on %s, which conflicts with what you told us." % (
                        _eur(d["upfront_eur"]), r["timing"]["admission"])
                    if state["comparison"]["cannot_advance"] else
                    "Funds for %s on %s." % (_eur(d["upfront_eur"]), r["timing"]["admission"]),
                    "Check with Försäkringskassan whether prior authorisation is required. It is "
                    "normally not needed for care without an overnight stay."
                    if state["home_tariff"]["care_type"] == "outpatient" else
                    "Check with Försäkringskassan whether prior authorisation is required "
                    "(Directive Art. 8 allows it for overnight hospital care).",
                    "Keep the itemised invoice, proof of payment and the medical report for the claim."]})
    for r in rows.values():
        if r["clinic"] in {o["clinic"] for o in declined}:
            continue
        s2 = r["s2"]
        if s2["available"] is not False:
            needed.append({
                "route": "S2 at %s" % r["clinic"],
                "summary": "Upfront about %s (travel). Admission %s, %s days earlier than your "
                           "expected date at home." % (_eur(s2["upfront_eur"]), r["timing"]["admission"],
                                                       r["timing"]["days_earlier_than_home"]),
                "requires": [
                    "S2 authorisation granted before %s." % r["timing"]["admission"],
                    "Written confirmation of patient charges.",
                    "Complete the attachment checklist (referral, waiting-list notice, the clinic's S2 confirmation)."]})
    state["revisions"].append({"changes": changes, "before": before, "after": after, "needed": needed,
                               "files": [d["filename"] for d in new]})
    trace(conn, case_id, state, "revise",
          "Revised. %s Recommendation now: %s." % (" ".join(changes), after["why"]))
    save(conn, case_id, state)
    return state


# --- paperwork --------------------------------------------------------------

def paperwork(conn, case_id: str, language: str = "English") -> Dict[str, Any]:
    """Application draft, attachment checklist, and a request for missing information."""
    from .draft import draft_application

    state = load(conn, case_id)
    cmp_, rules_ = state.get("comparison"), state.get("rules") or {}
    if not cmp_ or not cmp_["recommendation"].get("clinic"):
        return {"error": "Run the investigation first."}
    rec = cmp_["recommendation"]
    row = next(r for r in cmp_["rows"] if r["clinic"] == rec["clinic"])
    proc, c = state["procedure"], countries.get(state["home"])
    ev = state["evidence"]["items"]

    extra = ["Sweden's price-list group for this procedure: %s." % (proc.get("label_en") or proc["label_sv"])]
    if rules_.get("registered"):
        extra.append("Registered on the waiting list on %s; treatment expected at home on %s."
                     % (rules_["registered"], rules_["expected"]))
    extra.append("The clinic's written offer gives its earliest admission as %s." % row["timing"]["admission"])
    if row["s2"]["available"]:
        extra.append("The clinic states in writing that it treats patients on an S2 authorisation.")
    facts = {
        "patient": "[Patient name]", "home": c.name, "authority": c.authority,
        "procedure": proc.get("from_text") or proc.get("label_en") or proc["label_sv"],
        "drg": proc["drg"],
        "treatment_country": row["country"], "provider": row["clinic"],
        "home_tariff_eur": cmp_["home_tariff_eur"],
        "home_wait_days": rules_.get("projected_total_wait_days"),
        "guarantee_days": c.guarantee_days, "route": rec["route"],
        "sources": [], "extra_lines": extra,
    }
    sample_note = " (a SAMPLE clinic from a synthetic document)" if row.get("synthetic") else ""
    out: Dict[str, Any] = {"clinic": row["clinic"], "sample_note": sample_note}

    application = {"language": c.language, "addressee": c.authority or "the competent health authority"}
    request = {"language": language, "addressee": row["clinic"]}
    questions = []
    for i in ev:
        if (i["key"] == "clinic_accepts_s2" and i["status"] == "missing"
                and i.get("clinic") == row["clinic"]):
            questions.append("Does the clinic treat patients holding an S2 authorisation as public patients?")
        if i["key"] == "patient_charges" and i["status"] == "missing":
            questions.append("What patient charges, if any, apply to an S2 patient?")
    questions.append("Can the earliest admission date of %s be held while the S2 authorisation is processed?"
                     % row["timing"]["admission"])
    try:
        text, problems = draft_application(facts, language=c.language, kind="application")
        application.update(text=text, problems=problems)
        rtext, rproblems = draft_application(
            dict(facts, questions=questions, route=None, sources=[], extra_lines=[],
                 home_tariff_eur=None, home_wait_days=None, guarantee_days=None),
            language=language, kind="provider_request")
        request.update(text=rtext, problems=rproblems, questions=questions)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - e.g. no model key
        application.setdefault("text", None)
        application["error"] = "draft unavailable: %s" % exc
        request.setdefault("text", None)
        request["error"] = application["error"]

    checklist = []
    for i in ev:
        if i.get("clinic") not in (None, row["clinic"]):
            continue
        if "S2" in i["needed_for"]:
            checklist.append({"label": i["label"], "status": i["status"],
                              "attach": i["source"]["file"] if i.get("source") else None,
                              "note": i.get("note")})
    out.update(application=application, request=request, checklist=checklist)
    state["paperwork"] = out
    trace(conn, case_id, state, "paperwork",
          "Drafted the S2 application (%s), an attachment checklist and a request for missing "
          "information to %s." % (c.language, row["clinic"]))
    save(conn, case_id, state)
    return out
