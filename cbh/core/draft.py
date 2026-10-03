"""Draft the paperwork a patient would otherwise have to write themselves.

Nothing here invents facts. The drafter receives figures that were already
extracted and verified, and renders an application citing them.

Generation alone is not enough. Measured against gemini-flash-latest, an
unverified drafter produced a clinical diagnosis that was never supplied
("severe osteoarthritis ... progressive joint deterioration"), cited case law it
was not given, and ignored the length limit. So output is checked the same way
extracted figures are, and a draft that fails its checks is reported as failed.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from .llm import LLMClient

MAX_WORDS = 340

SYSTEM_PROMPT = """You draft formal applications for cross-border healthcare in the EU.

Write a prior-authorisation application (S2 / Regulation (EC) 883/2004) as a
LETTER from the patient to their home health authority.

Hard requirements:
- A letter. Not a report, not a table, not a markdown document. No headings.
- Under 300 words.
- Cite Article 20(2) of Regulation (EC) No 883/2004.
- Use ONLY the figures supplied. Introduce no number that was not given.
- Make NO clinical claim. Do not name, infer or describe any diagnosis,
  symptom, or disease progression. You have not been told the diagnosis.
- Cite no case law, statute or source other than those supplied.
- State that the figures come from published official tariffs and registers.
- If a treatment provider is named, name it as the intended provider.
- End with a signature placeholder.
"""

PROVIDER_REQUEST_PROMPT = """You draft a short request from a patient to a hospital abroad.

The patient has received the hospital's written offer and needs specific information
before an S2 prior-authorisation application can be completed.

Hard requirements:
- A letter. No headings, no tables, no markdown.
- Under 200 words.
- Ask ONLY the questions listed under "Questions", each as its own short sentence.
- Say any authorisation is being applied for, not yet granted.
- Use ONLY the figures and dates supplied. Introduce no number that was not given.
- Make NO clinical claim. You have not been told the diagnosis.
- End with a signature placeholder.
"""

REFERRAL_PROMPT = """You draft a short enquiry from a patient to a hospital abroad.

The patient is covered by a public health system in another EU country and is
applying for S2 prior authorisation (Regulation (EC) No 883/2004, Art. 20(2)) to
be treated at this hospital as a public patient.

Hard requirements:
- A letter. No headings, no tables, no markdown.
- Under 200 words.
- Ask three things: the earliest admission date, whether the hospital treats
  patients on an S2 authorisation, and what patient charges would apply.
- Say the authorisation is being applied for, not yet granted.
- Use ONLY the figures supplied. Introduce no number that was not given.
- Make NO clinical claim. You have not been told the diagnosis.
- End with a signature placeholder.
"""

# Language that would amount to a clinical claim the drafter was never given.
CLINICAL_TERMS = (
    "osteoarthritis", "arthritis", "artros", "degenerative", "deterioration",
    "chronic pain", "symptom", "diagnosis", "diagnosed", "disease progression",
    "suffering", "severe", "painful", "immobility", "mobility impairment",
    # Swedish, Dutch, Polish, Danish, Finnish equivalents of the same claims.
    "diagnos", "artros", "symtom", "smärt", "sjukdomsförlopp",
    "artrose", "pijn", "ziekteverloop", "klachten",
    "objaw", "dolegliw", "choroba zwyrod",
    "sygdomsforløb", "smerter", "kipu", "oireet",
)

# Article and regulation numbers the letter is expected to cite.
LEGAL_NUMBERS = {2, 4, 7, 8, 20, 24, 883, 2004, 2011}


def _numbers(text: str) -> set:
    """Integer values of the number-like tokens in a string.

    Space-grouped figures are read both whole and in parts, so "86 945" yields
    86945 as well as 86 and 945. Being slightly permissive here avoids false
    accusations of invention; the clinical-claim check is the stricter guard.
    """
    out = set()
    for raw in re.findall(r"\d[\d \u00a0.,]*", text or ""):
        # The token regex is greedy and will swallow trailing punctuation
        # ("7593.45, "), which otherwise defeats the decimal rule below and
        # turns 7593.45 into 759345.
        token = raw.strip().rstrip(" .,\u00a0")
        token = re.sub(r"[,.]\d{1,2}$", "", token)
        digits = re.sub(r"[^\d]", "", token)
        if digits:
            out.add(int(digits))
        for part in re.split(r"[ \u00a0]+", token):
            part_digits = re.sub(r"[^\d]", "", part)
            if part_digits:
                out.add(int(part_digits))
    return out


def _supplied_numbers(facts: Dict[str, Any]) -> set:
    """Figures the drafter was given.

    Parsed per value: joining them first lets the token regex run across the
    separators and fuse "240 90 21" into one meaningless number, which made
    every genuine figure look invented.
    """
    out = set()
    for value in facts.values():
        if isinstance(value, (list, tuple)):
            for item in value:
                out |= _numbers(str(item))
        else:
            out |= _numbers(str(value))
    return out


def verify_draft(text: str, facts: Dict[str, Any]) -> List[str]:
    """Return the reasons a draft is unacceptable. Empty means it passed."""
    problems: List[str] = []

    words = len(text.split())
    if words > MAX_WORDS:
        problems.append("too long: %d words (limit %d)" % (words, MAX_WORDS))

    lowered = text.lower()
    found = [t for t in CLINICAL_TERMS if t in lowered]
    if found:
        problems.append("clinical claim not supplied: %s" % ", ".join(found[:4]))

    # Every figure in the letter must trace to one we handed it.
    allowed = _supplied_numbers(facts) | LEGAL_NUMBERS
    for n in sorted(_numbers(text)):
        if n < 10 or 1900 < n < 2100:
            continue
        if n not in allowed:
            problems.append("figure %s was not supplied" % n)

    return problems


def draft_application(
    facts: Dict[str, Any],
    client: Optional[LLMClient] = None,
    max_attempts: int = 3,
    language: str = "English",
    kind: str = "application",
) -> Tuple[str, List[str]]:
    """Draft and verify. Returns (text, problems); empty problems means verified.

    `kind` is "application" (to the home authority), "referral" (an enquiry to
    the chosen provider) or "provider_request" (specific missing information). `language` is the language the letter is written in;
    the figure and clinical-claim checks apply in every language.
    """
    # The supplied figures must survive intact, so drafting does not go through
    # a compressing proxy either.
    client = client or LLMClient(use_condense=False)

    body = "\n".join([
        "Patient: %s" % facts.get("patient"),
        "Home country / region: %s" % facts.get("home"),
        "Procedure: %s (%s)" % (facts.get("procedure"), facts.get("drg")),
        "Treatment country: %s" % facts.get("treatment_country"),
    ] + (["Treatment provider: %s" % facts["provider"]] if facts.get("provider") else [])
      + (["Home tariff: EUR %s" % facts["home_tariff_eur"]]
         if facts.get("home_tariff_eur") is not None else [])
      + (["Treatment-country tariff: EUR %s" % facts["abroad_tariff_eur"]]
         if facts.get("abroad_tariff_eur") is not None else [])
      + (["Current wait at home: %s days" % facts["home_wait_days"]]
         if facts.get("home_wait_days") is not None else [])
      + (["Statutory treatment guarantee: %s days" % facts["guarantee_days"]]
         if facts.get("guarantee_days") is not None else []) + ([
        ("Available abroad within: %s days (assumed, to be confirmed with the provider)"
         if facts.get("abroad_wait_is_assumption")
         else "Average wait reported by the provider: %s days")
        % facts.get("abroad_wait_days")]
         if facts.get("abroad_wait_days") is not None else []) + [
    ] + (["Recommended route: %s" % facts["route"]] if facts.get("route") else [])
      + (["Capital the patient would otherwise advance: EUR %s"
          % facts["capital_at_risk_eur"]]
         if facts.get("capital_at_risk_eur") is not None else [])
      + (["Source documents: %s" % "; ".join(facts["sources"])]
         if facts.get("sources") else []) + list(facts.get("extra_lines", [])) + (
        ["Questions:"] + ["- %s" % q for q in facts["questions"]]
        if facts.get("questions") else []))

    # Constraints travel with the request rather than sitting only in the system
    # prompt, which the model was observed to disregard.
    addressee = (
        "Addressee: %s\n" % (facts.get("authority") or "the competent health authority")
        if kind == "application"
        else "Addressee: %s\n" % (facts.get("provider") or "the hospital")
    )
    body = "Write the letter in %s.\n%s%s" % (language, addressee, body)
    system = {"application": SYSTEM_PROMPT, "referral": REFERRAL_PROMPT,
              "provider_request": PROVIDER_REQUEST_PROMPT}[kind]
    user = (
        body
        + "\n\nWrite the letter now. Under 300 words, plain letter format, no "
          "headings or tables, no diagnosis or clinical description of any "
          "kind, and no number that does not appear above."
    )

    text, problems = "", ["not attempted"]
    for attempt in range(max_attempts):
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if attempt:
            messages.append({
                "role": "user",
                "content": "The previous attempt failed these checks: %s. "
                           "Rewrite the letter fixing them."
                           % "; ".join(problems),
            })
        text = client.chat(messages)
        problems = verify_draft(text, facts)
        if not problems:
            return text, []

    return text, problems
