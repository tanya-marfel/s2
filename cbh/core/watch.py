"""Stage 6 against the real world: re-read the register, then re-evaluate.

`cases.tick()` decides from whatever facts it is given. Here the facts are
observed, not scripted: the chosen provider's reported wait is re-read from the
national register and compared with what the plan was built on. If it rises
past the filed figure plus a margin, the time saved no longer holds and the
agent escalates; if it falls back, the escalation is retracted.

An observation can be marked simulated so a demo can show the reaction without
waiting for the register to change. Simulated readings are labelled as such in
the ledger and never presented as register data.
"""

import json
from typing import Any, Dict, Optional

from . import cases, providers, replan

MARGIN_DAYS = 14


def open_watch(conn, case_id, patient, home, drg, label_sv, label_en,
               provider, filed_wait_days, home_wait_days, filed_tariff_sek=None,
               guarantee_days=None):
    """Open a case with a live loop on the chosen provider's wait.

    `provider` is the destination dict from the route comparison.
    """
    row = provider["provider"]
    threshold = int(filed_wait_days) + MARGIN_DAYS
    payload = {
        "provider_key": row["key"], "provider": row["provider"],
        "country": provider["country_name"], "drg": drg,
        "label_sv": label_sv, "label_en": label_en,
        "filed_wait_days": filed_wait_days, "home_wait_days": home_wait_days,
        "threshold_days": threshold, "filed_tariff_sek": filed_tariff_sek,
        "guarantee_days": guarantee_days,
    }
    cases.open_case(conn, case_id, patient, home, label_en or label_sv, drg,
                    payload=payload)
    cases.open_loop(
        conn, case_id + ":auth", case_id, "filing",
        "File S2 prior-authorisation application naming %s" % row["provider"],
        trigger="immediate", owner="patient+agent", closure="authorisation_granted")
    cases.open_loop(
        conn, case_id + ":provider-wait", case_id, "conditional",
        "Re-read %s's reported wait; the plan assumed %d days"
        % (row["provider"], filed_wait_days),
        trigger="provider_wait_days > %d" % threshold, owner="agent",
        closure="treated")
    if filed_tariff_sek:
        cases.open_loop(
            conn, case_id + ":tariff-watch", case_id, "conditional",
            "Re-check the Swedish tariff; a move of more than 5% can change which route pays",
            trigger="tariff_change_pct > 5", owner="agent", closure="treated")
    if guarantee_days and home == "SE":
        cases.open_loop(
            conn, case_id + ":home-queue", case_id, "conditional",
            "Watch Sweden's reported median wait; above the %d-day guarantee the "
            "undue-delay argument no longer rests on the patient's own wait alone"
            % guarantee_days,
            trigger="home_median_wait_days > %d" % guarantee_days, owner="agent",
            closure="treated")
    return payload


def observe(conn, case_id, simulate: Optional[float] = None) -> Dict[str, Any]:
    """Read the chosen provider's current wait. Returns the observation."""
    row = conn.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
    if row is None:
        raise KeyError(case_id)
    pl = json.loads(row["payload"])
    obs = {"provider": pl["provider"], "country": pl["country"],
           "filed_wait_days": pl["filed_wait_days"],
           "threshold_days": pl["threshold_days"]}

    if simulate is not None:
        obs.update(wait_days=float(simulate), simulated=True,
                   source="simulated for demonstration")
        return obs

    found = providers.find(pl["provider_key"], pl["drg"], pl["label_sv"],
                           pl["label_en"], max_age=0)
    obs["simulated"] = False
    if found is None:
        obs.update(wait_days=None, listed=False,
                   source="provider no longer listed in the register")
        return obs
    _, p, source = found
    obs.update(wait_days=p.get("avg_wait_days"), listed=True,
               register_updated=p.get("updated"),
               source=(source or {}).get("name"))
    return obs


def check(conn, case_id, simulate: Optional[float] = None,
          drafter=None) -> Dict[str, Any]:
    """One wake-up: observe, record the observation, let the ledger react.

    On escalation the agent proposes a switch (see replan.py); `drafter` writes
    the replacement letter.
    """
    obs = observe(conn, case_id, simulate)
    facts = {}
    if obs.get("wait_days") is not None:
        facts["provider_wait_days"] = obs["wait_days"]
    actions = cases.tick(conn, case_id, facts)
    cases._log(conn, "observation", case_id=case_id, detail=obs)
    conn.commit()
    proposal = replan.after_tick(conn, case_id, obs, actions, drafter)
    return {"observation": obs, "actions": actions, "proposal": proposal}
