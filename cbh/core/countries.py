"""Statutory waiting-time guarantees by Member State.

The undue-delay test in Art. 20(2) turns on whether treatment can be provided
"within a time limit which is medically justifiable". That is a clinical
judgement, but most Member States publish a statutory or nationally agreed
maximum waiting time, which is the documented proxy this system uses.

The thresholds differ by an order of magnitude - 30 days in Denmark against 180
in Finland - so the same wait can breach the guarantee in one country and sit
comfortably inside it in another. Hardcoding one country's figure, as an earlier
version did with Sweden's 90 days, silently produces the wrong answer everywhere
else.

`binding` matters legally: a statutory guarantee supports a stronger Art. 20(2)
argument than a nationally agreed norm.
"""

from dataclasses import dataclass, asdict, replace
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Country:
    code: str
    name: str
    guarantee_days: int
    guarantee_name: str
    binding: bool
    currency: str
    per_eur: float
    note: str
    source_url: str
    # Identifier of the ingested tariff catalogue, when one exists.
    tariff_source: Optional[str] = None
    # Language a letter to this country's authorities is written in, and the body
    # that handles S2 prior authorisation where we are sure of it.
    language: str = "English"
    authority: Optional[str] = None

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["has_tariffs"] = self.tariff_source is not None
        return d


COUNTRIES: Dict[str, Country] = {
    "SE": Country(
        code="SE", name="Sweden",
        guarantee_days=90,
        guarantee_name="Vardgaranti (0-7-90-90)",
        binding=True,
        currency="SEK", per_eur=11.45,
        note="90 days to a first specialist appointment, then a further 90 to "
             "treatment after a decision to treat.",
        source_url="https://skr.se/halsaochsjukvard/patientinflytande/"
                   "vardgarantiochtillganglighetivarden.7870.html",
        tariff_source="se_norddrg_2025",
    ),
    "DK": Country(
        code="DK", name="Denmark",
        guarantee_days=30,
        guarantee_name="Behandlingsgaranti (udvidet frit sygehusvalg)",
        binding=True,
        currency="DKK", per_eur=7.46,
        note="If a public hospital cannot treat within 30 days, the patient is "
             "entitled to private treatment at public expense.",
        source_url="https://www.borger.dk/sundhed-og-sygdom/Patientrettigheder",
    ),
    "FI": Country(
        code="FI", name="Finland",
        guarantee_days=180,
        guarantee_name="Hoitotakuu",
        binding=True,
        currency="EUR", per_eur=1.0,
        note="Need for specialist care assessed within 3 weeks; treatment "
             "within 6 months at the latest.",
        source_url="https://www.hus.fi/en/patient/patient-guide/your-rights-patient/"
                   "access-treatment-specialist-medical-care",
    ),
    "NL": Country(
        code="NL", name="Netherlands",
        guarantee_days=49,
        guarantee_name="Treeknormen",
        binding=False,
        currency="EUR", per_eur=1.0,
        note="Nationally agreed standards rather than statute: 4 weeks to a "
             "first appointment, 7 weeks to the start of treatment.",
        source_url="https://www.zorgwijzer.nl/zorgverzekering-2026/treeknormen",
    ),
    "PT": Country(
        code="PT", name="Portugal",
        guarantee_days=150,
        guarantee_name="Tempos Maximos de Resposta Garantidos (TMRG)",
        binding=True,
        currency="EUR", per_eur=1.0,
        note="Legally defined maximum response times, varying by urgency; up to "
             "150 days for non-urgent specialist care.",
        source_url="https://www.sns.gov.pt/",
    ),
}

# (language, S2 authority). The authority is left out where we are not sure of
# it; the letter then addresses "the competent health authority".
_LOCAL = {
    "SE": ("Swedish", "Försäkringskassan"),
    "DK": ("Danish", None),
    "FI": ("Finnish", "Kela"),
    "NL": ("Dutch", "the patient's health insurer (zorgverzekeraar)"),
    "PT": ("Portuguese", None),
}
COUNTRIES = {c: replace(v, language=_LOCAL[c][0], authority=_LOCAL[c][1])
             for c, v in COUNTRIES.items()}

# Languages of treatment countries that are not also home countries here.
LANGUAGES = {"PL": "Polish", "NL": "Dutch", "SE": "Swedish", "DK": "Danish",
             "FI": "Finnish", "PT": "Portuguese"}


def language_of(code):
    return LANGUAGES.get((code or "").upper(), "English")


# Official National Contact Points. Every Member State must run one under
# Art. 6 of Directive 2011/24/EU; its statutory job is to tell patients where
# they can be treated and what the rules are. We route patients there rather
# than inventing a provider directory we have not ingested.
NCP = {
    "SE": ("1177 Vardguiden", "https://www.1177.se/"),
    "PL": ("Krajowy Punkt Kontaktowy (NFZ)", "https://www.kpk.nfz.gov.pl/"),
    "DK": ("Styrelsen for Patientsikkerhed", "https://stps.dk/en"),
    "FI": ("Rajat ylittava terveydenhuolto", "https://www.eu-healthcare.fi/"),
    "NL": ("CAK / Nationaal Contactpunt", "https://www.hetcak.nl/"),
    "PT": ("Servico Nacional de Saude", "https://www.sns.gov.pt/"),
}

EU_NCP_DIRECTORY = "https://health.ec.europa.eu/cross-border-healthcare_en"


def ncp(code):
    """Name and URL of a country's National Contact Point."""
    name, url = NCP.get((code or "").upper(), (None, EU_NCP_DIRECTORY))
    return {"country": (code or "").upper(), "name": name, "url": url}


DEFAULT = "SE"


def get(code: str) -> Country:
    return COUNTRIES.get((code or "").upper(), COUNTRIES[DEFAULT])


def listing() -> List[Dict]:
    """Countries ordered with ingested tariff catalogues first."""
    return [c.to_dict() for c in sorted(
        COUNTRIES.values(),
        key=lambda c: (c.tariff_source is None, c.name),
    )]
