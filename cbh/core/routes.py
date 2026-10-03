"""Compare the two legal routes to EU cross-border care.

Route A - Directive 2011/24/EU
    Patient arranges treatment abroad, pays up front, then claims reimbursement
    from the home system. Reimbursement is capped at what the treatment would
    have cost at home, and never exceeds the amount actually paid.

Route B - S2 / Regulation (EC) 883/2004
    Prior authorisation obtained before travel. The patient is treated as a
    public patient of the treatment state, so the two authorities settle between
    themselves and the patient pays only local patient charges.

    Article 20(2): authorisation *must* be granted where the treatment is among
    the benefits the home state covers and cannot be provided within a time limit
    that is medically justifiable. Long domestic waiting lists are therefore the
    legal lever, not merely a motivation.

These produce materially different outcomes and almost no patient compares them.
"""

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Optional, Any

from .provenance import Cited, require


@dataclass
class Tariff:
    """What a public system pays for a procedure."""

    country: str
    code: str
    label: str
    amount_eur: Cited


@dataclass
class Waiting:
    country: str
    procedure: str
    days: Cited


@dataclass
class RouteOutcome:
    route: str
    legal_basis: str
    prior_authorisation: str
    upfront_cost_eur: float
    reimbursed_eur: float
    out_of_pocket_eur: float
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Medically justifiable waiting time is assessed case by case. Member States
# publish statutory or nationally agreed maximums, which serve as the documented
# proxy; see cbh/core/countries.py. Never hardcode one country's figure here.
DEFAULT_GUARANTEE_DAYS = 90


def compare_routes(
    home_tariff: Tariff,
    abroad_tariff: Tariff,
    home_wait: Optional[Waiting] = None,
    abroad_wait: Optional[Waiting] = None,
    local_patient_charge_eur: float = 0.0,
    travel_cost_eur: float = 0.0,
    guarantee_days: int = DEFAULT_GUARANTEE_DAYS,
    guarantee_name: str = "statutory treatment guarantee",
    guarantee_binding: bool = True,
) -> Dict[str, Any]:
    """Compute both routes for one country pair and procedure."""

    home = require(home_tariff.amount_eur, "home tariff").value
    abroad = require(abroad_tariff.amount_eur, "treatment-country tariff").value

    # Route A: pay abroad price, reclaim min(home tariff, amount actually paid).
    directive_reimbursed = min(home, abroad)
    directive = RouteOutcome(
        route="Directive 2011/24/EU",
        legal_basis="Directive 2011/24/EU, Art. 7(4)",
        prior_authorisation=(
            "Often required for overnight hospital stays or highly specialised care; "
            "check the home National Contact Point"
        ),
        upfront_cost_eur=abroad + travel_cost_eur,
        reimbursed_eur=directive_reimbursed,
        out_of_pocket_eur=abroad + travel_cost_eur - directive_reimbursed,
        notes=[
            "Reimbursed at home-country rates, capped at the amount actually paid.",
            "Patient must fund the full cost up front and reclaim afterwards.",
        ],
    )

    # Route B: authorities settle; patient bears only local charges.
    s2 = RouteOutcome(
        route="S2 / Regulation 883/2004",
        legal_basis="Regulation (EC) 883/2004, Art. 20(2)",
        prior_authorisation="Mandatory - must be obtained before travelling",
        upfront_cost_eur=local_patient_charge_eur + travel_cost_eur,
        reimbursed_eur=abroad,
        out_of_pocket_eur=local_patient_charge_eur + travel_cost_eur,
        notes=[
            "Settled directly between the two health authorities.",
            "Patient pays only the treatment country's own patient charges.",
        ],
    )

    entitlement: Dict[str, Any] = {"undue_delay_argument": False}
    if home_wait is not None:
        wait_days = require(home_wait.days, "home waiting time").value
        exceeds = wait_days > guarantee_days
        entitlement = {
            "undue_delay_argument": bool(exceeds),
            "home_wait_days": wait_days,
            "statutory_reference_days": guarantee_days,
            "guarantee_name": guarantee_name,
            "guarantee_binding": guarantee_binding,
            "basis": (
                "Home wait of %d days exceeds the %d-day %s, supporting an "
                "Art. 20(2) claim that treatment cannot be provided within a "
                "medically justifiable time."
                % (wait_days, guarantee_days, guarantee_name)
                if exceeds
                else "Home wait of %d days is within the %d-day %s; an "
                "undue-delay argument is weak."
                % (wait_days, guarantee_days, guarantee_name)
            ),
            "source": home_wait.days.to_dict(),
        }
        if exceeds and not guarantee_binding:
            entitlement["caveat"] = (
                "%s is a nationally agreed norm rather than statute, so it "
                "supports a weaker Art. 20(2) argument than a binding "
                "guarantee." % guarantee_name
            )
        if exceeds:
            s2.notes.append(
                "Undue delay: authorisation should not be refused (Art. 20(2))."
            )

    # Out-of-pocket often ties when the abroad tariff sits below the home tariff,
    # because the Directive then reimburses in full. What still separates the
    # routes is how much the patient must float in the meantime, which is the
    # binding constraint for most people.
    best = min(
        [directive, s2],
        key=lambda r: (round(r.out_of_pocket_eur, 2), round(r.upfront_cost_eur, 2)),
    )
    other = s2 if best is directive else directive
    delta = abs(directive.out_of_pocket_eur - s2.out_of_pocket_eur)
    capital_delta = other.upfront_cost_eur - best.upfront_cost_eur

    time_saved = None
    if home_wait is not None and abroad_wait is not None:
        time_saved = (
            require(home_wait.days, "home waiting time").value
            - require(abroad_wait.days, "abroad waiting time").value
        )

    return {
        "procedure": {
            "home_code": home_tariff.code,
            "home_label": home_tariff.label,
            "abroad_code": abroad_tariff.code,
            "abroad_label": abroad_tariff.label,
        },
        "countries": {"home": home_tariff.country, "treatment": abroad_tariff.country},
        "tariffs": {
            "home_eur": home_tariff.amount_eur.to_dict(),
            "abroad_eur": abroad_tariff.amount_eur.to_dict(),
        },
        "routes": [directive.to_dict(), s2.to_dict()],
        "recommendation": {
            "route": best.route,
            "out_of_pocket_eur": round(best.out_of_pocket_eur, 2),
            "advantage_over_alternative_eur": round(delta, 2),
            "upfront_eur": round(best.upfront_cost_eur, 2),
            "capital_at_risk_avoided_eur": round(capital_delta, 2),
            "decided_on": (
                "out-of-pocket cost"
                if round(delta, 2) > 0
                else "up-front capital required (out-of-pocket is equal)"
            ),
            "days_saved": time_saved,
        },
        "entitlement": entitlement,
    }
