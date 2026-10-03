"""Open clinical loops.

A referral or decision creates obligations that are not appointments: a timed
re-check, a condition to evaluate when new data arrives, a document to retrieve,
a form to file. Most are never closed, which is the problem this system exists
to attack.

Each loop carries a trigger, an owner and a closure criterion. `tick()` is the
agent waking up: it re-evaluates every open loop against current facts and
closes, escalates or reopens them. Reopening matters - a loop closed on a stale
tariff or waiting time must come back when the underlying fact changes.
"""

import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

DB_PATH = os.path.join("out", "cases.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id      TEXT PRIMARY KEY,
    patient      TEXT NOT NULL,
    home_country TEXT NOT NULL,
    procedure    TEXT NOT NULL,
    drg          TEXT,
    created_at   REAL NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    payload      TEXT
);

CREATE TABLE IF NOT EXISTS loops (
    loop_id      TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    loop_type    TEXT NOT NULL,
    description  TEXT NOT NULL,
    trigger      TEXT NOT NULL,
    owner        TEXT NOT NULL,
    closure      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    opened_at    REAL NOT NULL,
    closed_at    REAL,
    evidence     TEXT,
    FOREIGN KEY (case_id) REFERENCES cases(case_id)
);

-- Changes the agent has proposed but the patient has not yet approved.
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    resolved_at  REAL,
    status       TEXT NOT NULL DEFAULT 'pending',
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_id    TEXT,
    case_id    TEXT,
    at         REAL NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT
);
"""

LOOP_TYPES = ("timed", "conditional", "referral", "retrieval", "correlative", "filing")


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _log(conn, action: str, case_id=None, loop_id=None, detail=None) -> None:
    conn.execute(
        "INSERT INTO events (loop_id, case_id, at, action, detail) VALUES (?,?,?,?,?)",
        (loop_id, case_id, time.time(), action, json.dumps(detail) if detail else None),
    )


def open_case(
    conn, case_id: str, patient: str, home_country: str, procedure: str,
    drg: Optional[str] = None, payload: Optional[Dict[str, Any]] = None,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO cases "
        "(case_id, patient, home_country, procedure, drg, created_at, status, payload) "
        "VALUES (?,?,?,?,?,?,'open',?)",
        (case_id, patient, home_country, procedure, drg, time.time(),
         json.dumps(payload or {}, ensure_ascii=False)),
    )
    _log(conn, "case_opened", case_id=case_id, detail={"procedure": procedure})
    conn.commit()


def open_loop(
    conn, loop_id: str, case_id: str, loop_type: str, description: str,
    trigger: str, owner: str, closure: str,
) -> None:
    if loop_type not in LOOP_TYPES:
        raise ValueError("unknown loop type: %s" % loop_type)
    conn.execute(
        "INSERT OR REPLACE INTO loops "
        "(loop_id, case_id, loop_type, description, trigger, owner, closure, "
        " status, opened_at) VALUES (?,?,?,?,?,?,?, 'open', ?)",
        (loop_id, case_id, loop_type, description, trigger, owner, closure, time.time()),
    )
    _log(conn, "loop_opened", case_id=case_id, loop_id=loop_id,
         detail={"type": loop_type, "trigger": trigger})
    conn.commit()


def close_loop(conn, loop_id: str, evidence: str) -> None:
    conn.execute(
        "UPDATE loops SET status='closed', closed_at=?, evidence=? WHERE loop_id=?",
        (time.time(), evidence, loop_id),
    )
    _log(conn, "loop_closed", loop_id=loop_id, detail={"evidence": evidence})
    conn.commit()


def reopen_loop(conn, loop_id: str, reason: str) -> None:
    """Reopen a previously closed loop because the fact behind it changed."""
    conn.execute(
        "UPDATE loops SET status='open', closed_at=NULL, evidence=? WHERE loop_id=?",
        ("REOPENED: %s" % reason, loop_id),
    )
    _log(conn, "loop_reopened", loop_id=loop_id, detail={"reason": reason})
    conn.commit()


def retract_loop(conn, loop_id: str, reason: str) -> None:
    """Withdraw an earlier escalation because its condition no longer holds.

    The agent revising its own past conclusion, rather than letting a stale
    judgement stand.
    """
    conn.execute(
        "UPDATE loops SET status='open', evidence=? WHERE loop_id=?",
        ("RETRACTED: %s" % reason, loop_id),
    )
    _log(conn, "loop_retracted", loop_id=loop_id, detail={"reason": reason})
    conn.commit()


def escalate_loop(conn, loop_id: str, reason: str) -> None:
    conn.execute("UPDATE loops SET status='escalated', evidence=? WHERE loop_id=?",
                 (reason, loop_id))
    _log(conn, "loop_escalated", loop_id=loop_id, detail={"reason": reason})
    conn.commit()


def loops_for(conn, case_id: str, status: Optional[str] = None) -> List[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM loops WHERE case_id=? AND status=? ORDER BY opened_at",
            (case_id, status)).fetchall()
    return conn.execute(
        "SELECT * FROM loops WHERE case_id=? ORDER BY opened_at", (case_id,)).fetchall()


def history(conn, case_id: str) -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM events WHERE case_id=? OR loop_id IN "
        "(SELECT loop_id FROM loops WHERE case_id=?) ORDER BY at, event_id",
        (case_id, case_id)).fetchall()


def tick(conn, case_id: str, facts: Dict[str, Any]) -> List[Dict[str, str]]:
    """Re-evaluate every loop for a case against current facts.

    `facts` carries the live world: elapsed days, current waiting time, whether
    authorisation arrived, current tariffs. Returns the actions taken.
    """
    actions: List[Dict[str, str]] = []

    for loop in loops_for(conn, case_id):
        trigger = loop["trigger"]
        status = loop["status"]

        if status == "open" and loop["loop_type"] == "timed":
            due_in = facts.get("days_elapsed", 0) - int(trigger.split(":")[-1])
            if due_in >= 0:
                escalate_loop(conn, loop["loop_id"],
                              "due: %d days elapsed" % facts.get("days_elapsed", 0))
                actions.append({"loop": loop["loop_id"], "action": "escalated",
                                "why": "timer elapsed"})

        elif loop["loop_type"] == "conditional":
            # Evaluated on every tick regardless of status, so an escalation can
            # be withdrawn when the fact behind it changes. A conclusion that is
            # never revisited is how stale judgements survive.
            key, _, threshold = trigger.partition(">")
            key = key.strip()
            if key in facts and threshold.strip():
                holds = float(facts[key]) > float(threshold)
                if holds and status == "open":
                    escalate_loop(conn, loop["loop_id"],
                                  "%s=%s exceeds %s" % (key, facts[key], threshold.strip()))
                    actions.append({"loop": loop["loop_id"], "action": "escalated",
                                    "why": "%s exceeded %s" % (key, threshold.strip())})
                elif not holds and status == "escalated":
                    retract_loop(conn, loop["loop_id"],
                                 "%s=%s no longer exceeds %s"
                                 % (key, facts[key], threshold.strip()))
                    actions.append({"loop": loop["loop_id"], "action": "retracted",
                                    "why": "%s fell to %s, within %s"
                                           % (key, facts[key], threshold.strip())})

        elif status == "open" and loop["loop_type"] == "filing":
            if facts.get(loop["closure"]):
                close_loop(conn, loop["loop_id"], "closure condition met")
                actions.append({"loop": loop["loop_id"], "action": "closed",
                                "why": loop["closure"]})

        # A closed loop must reopen when the fact it relied on no longer holds.
        if status == "closed" and loop["loop_type"] in ("timed", "conditional"):
            stale_key = "invalidates:%s" % loop["loop_id"]
            if facts.get(stale_key):
                reopen_loop(conn, loop["loop_id"], str(facts[stale_key]))
                actions.append({"loop": loop["loop_id"], "action": "reopened",
                                "why": str(facts[stale_key])})

    return actions


def summary(conn) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) c FROM loops GROUP BY status").fetchall()
    out = {r["status"]: r["c"] for r in rows}
    out["cases"] = conn.execute("SELECT COUNT(*) c FROM cases").fetchone()["c"]
    return out
