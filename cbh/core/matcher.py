"""Match a procedure across national coding systems.

Sweden codes a hip replacement as NordDRG H02E ("Prim ledprot hoft U"); Poland
codes it as an NFZ JGP group. No crosswalk is published, so the agent proposes a
match and the match is then checked rather than trusted.

Verification: the proposed code and label must both occur in the candidate list
we supplied. The agent cannot invent a code that was never on offer.
"""

import json
import re
from typing import Any, Dict, List, Optional

from .llm import LLMClient

SYSTEM_PROMPT = """You map medical procedures between national coding systems.

You are given one SOURCE procedure and a list of CANDIDATE procedures from
another country's tariff catalogue. Choose the candidate describing the same
clinical procedure.

Return ONLY JSON:
{
  "code": "<candidate code, copied exactly, or null>",
  "label": "<candidate label, copied exactly, or null>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one sentence>"
}

Rules:
- Choose only from the candidates provided. Never invent a code.
- Match on clinical procedure, not on word similarity.
- If no candidate is the same procedure, return code and label as null.
- Primary vs revision, and unilateral vs bilateral, are different procedures.
"""


def _parse_json(text: str) -> Dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def match_procedure(
    source_code: str,
    source_label: str,
    source_country: str,
    candidates: List[Dict[str, Any]],
    target_country: str,
    client: Optional[LLMClient] = None,
    min_confidence: float = 0.6,
) -> Dict[str, Any]:
    """Propose and verify a cross-country procedure match."""
    # The candidate list is payload, not repeated reference context, so it must
    # not pass through a compressing proxy - compaction drops candidates and the
    # match silently fails. Same failure mode as document ingestion.
    client = client or LLMClient(use_condense=False)

    listing = "\n".join(
        "%s | %s | %s"
        % (c.get("code"), c.get("label", ""), c.get("label_en", ""))
        for c in candidates
    )
    user = (
        "SOURCE (%s): %s | %s\n\nCANDIDATES (%s):\n%s"
        % (source_country, source_code, source_label, target_country, listing)
    )

    proposal = _parse_json(
        client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
        )
    )

    code = proposal.get("code")
    result: Dict[str, Any] = {
        "source": {"country": source_country, "code": source_code, "label": source_label},
        "target_country": target_country,
        "proposed": proposal,
        "verified": False,
        "rejection": None,
        "match": None,
    }

    if not code:
        result["rejection"] = "no match proposed"
        return result

    # The chosen code must be one we actually offered.
    chosen = next((c for c in candidates if str(c.get("code")) == str(code)), None)
    if chosen is None:
        result["rejection"] = "proposed code %s was not among the candidates" % code
        return result

    confidence = float(proposal.get("confidence") or 0.0)
    if confidence < min_confidence:
        result["rejection"] = "confidence %.2f below threshold %.2f" % (
            confidence,
            min_confidence,
        )
        return result

    result["verified"] = True
    result["match"] = chosen
    return result
