"""When the plan breaks, propose a better one — and wait for the patient.

If the chosen provider's reported wait rises past the filed figure (or the
provider disappears from its register), the agent:

1. re-reads every register it can search and ranks the other providers by their
   current reported wait,
2. picks the best one that is actually faster,
3. drafts the replacement S2 application naming it, verified like any letter,
4. records a PROPOSAL in the ledger.

Nothing changes until the patient approves. Approval re-points the case at the new
provider and re-arms the watch on it; rejection leaves the escalation standing. If
the wait later falls back, the agent withdraws its own proposal.

Every figure comes from the registers; the model only writes the letter.
"""

import json
import time
from typing import Any, Callable, Dict, List, Optional

from . import cases, providers

Drafter = Callable[..., Dict[str, Any]]


def _rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def pending(conn, case_id: str) -> List[Dict[str, Any]]:
    out = _rows(conn, "SELECT * FROM proposals WHERE case_id=? AND status='pending' "
                      "ORDER BY proposal_id", (case_id,))
    for p in out:
        p["payload"] = json.loads(p["payload"])
    return out


def _set_status(conn, proposal_id: int, status: str) -> None:
    conn.execute("UPDATE proposals SET status=?, resolved_at=? WHERE proposal_id=?",
                 (status, time.time(), proposal_id))


def best_alternative(pl: Dict[str, Any], current_wait: Optional[float],
                     home: str, exclude_key: str) -> Optional[Dict[str, Any]]:
    """The fastest other provider by reported wait, if it beats the current one."""
    res = providers.lookup(pl["drg"], pl["label_sv"], pl["label_en"],
                           exclude=[home], max_age=0)
    best = None
    for c in res["countries"]:
        for p in c.get("providers") or []:
            if p["key"] == exclude_key or p.get("avg_wait_days") is None:
                continue
            if current_wait is not None and p["avg_wait_days"] >= current_wait:
                continue
            rank = (p["avg_wait_days"], -(p.get("awaiting") or 0))
            if best is None or rank < best[0]:
                best = (rank, dict(p, country_code=c["code"], country=c["name"],
                                   source=c.get("source")))
    return best[1] if best else None


def propose_switch(conn, case_id: str, obs: Dict[str, Any], drafter: Optional[Drafter],
                   patient: str = "[Patient name]") -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    pl = json.loads(row["payload"] or "{}")
    current_key = pl["provider_key"]
    alt = best_alternative(pl, obs.get("wait_days"), row["home_country"], current_key)
    if alt is None:
        cases._log(conn, "no_better_provider", case_id=case_id,
                   detail={"current_wait_days": obs.get("wait_days")})
        conn.commit()
        return None

    existing = pending(conn, case_id)
    if any(p["payload"]["to"]["key"] == alt["key"] for p in existing):
        return existing[0]
    for p in existing:  # a newer, different proposal supersedes the old one
        _set_status(conn, p["proposal_id"], "superseded")

    now = obs.get("wait_days")
    reason = ("%s now reports %s days against the %s the plan was filed on."
              % (pl["provider"], "no wait" if now is None else int(now),
                 int(pl["filed_wait_days"]))
              if obs.get("listed", True) else
              "%s is no longer listed in its register." % pl["provider"])
    home_wait = pl.get("home_wait_days")
    payload = {
        "reason": reason, "simulated": bool(obs.get("simulated")),
        "from": {"key": current_key, "provider": pl["provider"],
                 "country": pl["country"], "wait_days": now},
        "to": {k: alt.get(k) for k in ("key", "provider", "place", "address", "postcode",
                                       "locality", "region", "phone", "country",
                                       "country_code", "avg_wait_days", "awaiting",
                                       "updated")},
        "days_saved_now": None if home_wait is None else home_wait - alt["avg_wait_days"],
        "register": (alt.get("source") or {}).get("name"),
    }

    letter = None
    if drafter is not None:
        try:
            d = drafter("application", alt["key"])
            letter = {"text": d.get("letter"), "language": d.get("language"),
                      "addressee": d.get("addressee"), "problems": d.get("problems"),
                      "error": d.get("error")}
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - e.g. no model key
            letter = {"text": None, "error": "draft unavailable: %s" % exc}
    payload["letter"] = letter

    cur = conn.execute(
        "INSERT INTO proposals (case_id, created_at, status, payload) VALUES (?,?,?,?)",
        (case_id, time.time(), "pending", json.dumps(payload, ensure_ascii=False)))
    cases._log(conn, "proposal_created", case_id=case_id, detail={
        "proposal": cur.lastrowid, "to": alt["provider"], "reason": reason})
    conn.commit()
    return dict(proposal_id=cur.lastrowid, case_id=case_id, status="pending",
                payload=payload)


def withdraw_if_recovered(conn, case_id: str, actions: List[Dict[str, str]]) -> int:
    """The agent revises itself: if the wait recovered, the proposal falls away."""
    if not any(a["action"] == "retracted" and a["loop"].endswith(":provider-wait")
               for a in actions):
        return 0
    n = 0
    for p in pending(conn, case_id):
        _set_status(conn, p["proposal_id"], "withdrawn")
        cases._log(conn, "proposal_withdrawn", case_id=case_id, detail={
            "proposal": p["proposal_id"], "why": "provider wait recovered"})
        n += 1
    conn.commit()
    return n


def after_tick(conn, case_id: str, obs: Dict[str, Any], actions: List[Dict[str, str]],
               drafter: Optional[Drafter]) -> Optional[Dict[str, Any]]:
    """Call after every tick: propose on escalation, withdraw on recovery."""
    withdraw_if_recovered(conn, case_id, actions)
    escalated = any(a["action"] == "escalated" and a["loop"].endswith(":provider-wait")
                    for a in actions)
    unlisted = obs.get("listed") is False
    if escalated or (unlisted and not pending(conn, case_id)):
        return propose_switch(conn, case_id, obs, drafter)
    return None


def resolve(conn, case_id: str, proposal_id: int, approve: bool) -> Dict[str, Any]:
    p = next((x for x in pending(conn, case_id) if x["proposal_id"] == int(proposal_id)), None)
    if p is None:
        raise KeyError("no pending proposal %s" % proposal_id)
    if not approve:
        _set_status(conn, p["proposal_id"], "rejected")
        cases._log(conn, "proposal_rejected", case_id=case_id,
                   detail={"proposal": p["proposal_id"]})
        conn.commit()
        return {"status": "rejected"}

    to = p["payload"]["to"]
    row = conn.execute("SELECT payload FROM cases WHERE case_id=?", (case_id,)).fetchone()
    pl = json.loads(row["payload"])
    pl.update(provider_key=to["key"], provider=to["provider"], country=to["country"],
              filed_wait_days=to["avg_wait_days"],
              threshold_days=int(to["avg_wait_days"]) + 14)
    conn.execute("UPDATE cases SET payload=? WHERE case_id=?",
                 (json.dumps(pl, ensure_ascii=False), case_id))
    # Re-arm the watch on the new provider; INSERT OR REPLACE reopens the loops.
    cases.open_loop(
        conn, case_id + ":auth", case_id, "filing",
        "File S2 prior-authorisation application naming %s" % to["provider"],
        trigger="immediate", owner="patient+agent", closure="authorisation_granted")
    cases.open_loop(
        conn, case_id + ":provider-wait", case_id, "conditional",
        "Re-read %s's reported wait; the plan assumed %d days"
        % (to["provider"], to["avg_wait_days"]),
        trigger="provider_wait_days > %d" % pl["threshold_days"], owner="agent",
        closure="treated")
    _set_status(conn, p["proposal_id"], "approved")
    cases._log(conn, "proposal_approved", case_id=case_id, detail={
        "proposal": p["proposal_id"], "provider": to["provider"]})
    conn.commit()
    return {"status": "approved", "provider_key": to["key"]}
