"""Provenance primitives.

Every figure surfaced to a patient must trace back to a primary source
document, page and line. Claims without provenance are refused.
"""

from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, Any
import hashlib
import os


@dataclass(frozen=True)
class Source:
    """A primary source document."""

    source_id: str
    publisher: str
    title: str
    url: str
    year: int
    country: str
    local_path: Optional[str] = None

    def sha256(self) -> Optional[str]:
        if not self.local_path or not os.path.exists(self.local_path):
            return None
        h = hashlib.sha256()
        with open(self.local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["sha256"] = self.sha256()
        return d


@dataclass(frozen=True)
class Cited:
    """A value bound to the exact place it came from."""

    value: float
    unit: str
    source_id: str
    page: int
    raw_line: str
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProvenanceError(RuntimeError):
    """Raised when a figure would be presented without a traceable source."""


def require(cited: Optional[Cited], what: str) -> Cited:
    """Gate any patient-facing figure behind a provenance check."""
    if cited is None:
        raise ProvenanceError(
            "refusing to present '%s': no primary source available" % what
        )
    return cited


SOURCES: Dict[str, Source] = {
    "se_norddrg_2025": Source(
        source_id="se_norddrg_2025",
        publisher="Samverkansnamnden Stockholm-Gotland / Region Stockholm",
        title="Bilaga 1 Utomregional prislista Nord DRG-CC, somatisk vard 2025",
        url=(
            "https://www.regionstockholm.se/4ade31/contentassets/"
            "915b67a74a4545e0bbc5e8fd226c81bd/"
            "bilaga-1-utomregional-prislista-nord-drg-cc-somatisk-vard-2025.pdf"
        ),
        year=2025,
        country="SE",
        local_path="data/raw/norddrg_2025.pdf",
    ),
    "se_karolinska_eu_2026": Source(
        source_id="se_karolinska_eu_2026",
        publisher="Karolinska Universitetssjukhuset / Region Stockholm",
        title="Utomregional prislista samt prislista for EU/EES och Schweiz 2026",
        url=(
            "https://www.regionstockholm.se/48f3d4/contentassets/"
            "cb439326bdc843f8b25867babfc21eee/"
            "karolinska---utomregional-prislista-samt-prislista-for-eu-ees-och-schweiz-2026-1.pdf"
        ),
        year=2026,
        country="SE",
        local_path="data/raw/ka_eu_2026.pdf",
    ),
    "pl_nfz_jgp_zarz48_2025": Source(
        source_id="pl_nfz_jgp_zarz48_2025",
        publisher="Narodowy Fundusz Zdrowia (NFZ)",
        title="Zarzadzenie nr 48/2025/DSOZ - katalog grup JGP (zalacznik 1a)",
        url="https://mbrk.pl/wp-content/uploads/2025/07/ZARZDZ_3.pdf",
        year=2025,
        country="PL",
        local_path="data/raw/pl_jgp_zarz48_2025.pdf",
    ),
    "pl_nfz_jgp_2025": Source(
        source_id="pl_nfz_jgp_2025",
        publisher="Narodowy Fundusz Zdrowia (NFZ)",
        title="Katalog JGP / cennik hospitalizacje 2025",
        url=(
            "https://wszzkielce.pl/wp-content/uploads/2025/01/"
            "Cennik2_hospitalizacje_katalog_odrebny_20250101.pdf"
        ),
        year=2025,
        country="PL",
        local_path="data/raw/pl_jgp_katalog_2025.pdf",
    ),
}
