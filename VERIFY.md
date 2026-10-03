# How to verify this system

Nothing here asks to be trusted. Every number traces to a published government
document, and every check below can be run against the primary source.

Work through it in order; each section is self-contained.

---

## 0. Setup

```bash
pip3 install -r requirements.txt
cp .env.example .env && chmod 600 .env
python3 cbh/core/config.py
```

The last command prints **masked** credentials, so it is safe to run while
screen-sharing:

```
GEMINI_API_KEY         AQ.A...WA (53 chars)
CONDENSE_API_KEY       <unset>
```

Confirm no secret is tracked by git:

```bash
git check-ignore -v .env          # expect a .gitignore match
git ls-files | grep -c '\.env$'   # expect 0
```

---

## 1. Verify the source documents are authentic

The Swedish tariff is published by Samverkansnämnden Stockholm-Gotland. Fetch it
yourself and compare the hash to the one the pipeline reports:

```bash
curl -sL -o /tmp/check.pdf \
 "https://www.regionstockholm.se/4ade31/contentassets/915b67a74a4545e0bbc5e8fd226c81bd/bilaga-1-utomregional-prislista-nord-drg-cc-somatisk-vard-2025.pdf"
shasum -a 256 /tmp/check.pdf
shasum -a 256 data/raw/norddrg_2025.pdf
```

Both should read `793494b4e256b6f72884e3559b6e0115...`.

The document index is reachable from Region Stockholm's own site — navigate to
*Samverkansnämnden Stockholm-Gotland* and you will find the same Bilaga 1, plus
Bilaga 2, the *"Utomregional prislista samt prislista för EU/EES och Schweiz"*.

---

## 2. Verify the Swedish extraction (1 438 tariffs, 0 unresolved)

```bash
python3 cbh/ingest/se_norddrg.py
```

```
weight 1.0 = 84194 SEK (p0)
extracted 1438 DRG tariffs (0 unresolved) -> out/se_tariffs.json
  H02E Prim ledprot höft U   w=1.082  86945 SEK  (p11)
```

### Check a figure against the PDF by hand

Open `data/raw/norddrg_2025.pdf` at **page 12** (0-indexed page 11) and find:

```
SV H02E 08 Prim ledprot höft U 1,082 86 945 81 040 145 454
```

86 945 SEK is the Region Stockholm price for a primary hip replacement.

### Why the extraction is self-validating

Swedish numbers are space-grouped, so `86 945 81 040 145 454` is genuinely
ambiguous as text — a naive parser reads `8694581040`. Instead of guessing, the
parser enumerates every valid way to split the tokens and keeps the one matching
`weight × published point price`:

```
1.082 × 80 356 (Stockholm point price) = 86 945  ✓
1.082 × 74 898 (Gotland point price)   = 81 040  ✓
```

So each figure is confirmed by the document's own arithmetic. Verify the
arithmetic yourself:

```bash
python3 -c "print(round(1.082*80356), round(1.082*74898))"   # 86945 81040
```

Confirm no row was silently dropped:

```bash
python3 -c "
import json; d=json.load(open('out/se_tariffs.json'))
print('tariffs', d['tariff_count'], 'unresolved', d['unresolved_count'])"
```

---

## 3. Verify the agent cannot invent figures

This is the important one: Gemini reads documents we have no parser for, so its
output must be checkable.

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from cbh.ingest.agent import verify_row
import pdfplumber
page = pdfplumber.open('data/raw/pl_jgp_katalog_2025.pdf').pages[1].extract_text()
real = [l for l in page.split(chr(10)) if 'Brachyterapia' in l][0]
for name, row in [
  ('valid',              {'code':'5.52.01.0001476','amount':2650,'raw_line':real}),
  ('invented line',      {'code':'5.52.01.0001476','amount':2650,'raw_line':'5.52.01.0001476 Fake 9 999,00'}),
  ('inflated amount',    {'code':'5.52.01.0001476','amount':9999,'raw_line':real}),
  ('cross-boundary hit', {'code':'5.52.01.0001476','amount':1252,'raw_line':real}),
  ('code not in line',   {'code':'H51','amount':2650,'raw_line':real}),
]:
    r = verify_row(row, page)
    print('%-20s %s' % (name, 'ACCEPTED' if r is None else 'REJECTED: '+r))"
```

Expected — only the truthful row survives:

```
valid                ACCEPTED
invented line        REJECTED: cited line not found on page
inflated amount      REJECTED: amount 9999 not present in cited line
cross-boundary hit   REJECTED: amount 1252 not present in cited line
code not in line     REJECTED: code H51 not present in cited line
```

Three independent conditions must hold: the cited line must occur on the page,
the amount must appear as a **token-aligned** number on that line, and the code
must appear there too.

The cross-boundary case is worth understanding. The line reads
`... 125I 2 650,00`. A naive check that flattens digits would let `1252` match
across `125` and `2`. Matching whole number tokens rejects it.

### Watch it reject a real error

```bash
python3 cbh/ingest/agent.py data/raw/pl_jgp_katalog_2025.pdf \
    --country PL --source-id pl_nfz_jgp_2025 --pages 2 --start 1
```

```
verified 43 rows, rejected 1
rejected samples (not surfaced to patients):
  p1   amount 845 not present in cited line
```

That rejection was not staged. The model reported `845` for a paediatric
cardiology row whose price had wrapped onto the next line. Inspect it:

```bash
python3 -c "
import json; d=json.load(open('out/agent_pl.json'))
for r in d['rejected']: print(r['reason']); print(r['row']['raw_line'])"
```

The cited line contains no such figure, so it never reached the output.

---

## 4. Verify the route comparison

```bash
python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240
```

```
Directive 2011/24/EU       up front EUR  5366.28   out of pocket EUR 250.00
S2 / Regulation 883/2004   up front EUR   250.00   out of pocket EUR 250.00

RECOMMENDED : S2 / Regulation 883/2004
  decided on        : up-front capital required (out-of-pocket is equal)
  capital not risked: EUR 5116.28
```

### Check the arithmetic

- Home tariff: 86 945 SEK ÷ 11.45 = **€7 593**
- Poland: 22 000 PLN ÷ 4.30 = **€5 116**
- Directive: pay €5 116 + €250 travel up front; reclaim `min(7 593, 5 116)` = €5 116 → **€250** out of pocket, but **€5 366 fronted**
- S2: institutions settle; patient pays travel only → **€250** out of pocket, **€250** fronted

Identical cost, €5 116 difference in capital required. That is the finding.

### Check the legal reasoning

`--wait-days 240` exceeds Sweden's 90-day treatment guarantee, so the agent
asserts an undue-delay argument under Art. 20(2). Drop below the threshold and
it should withdraw the claim:

```bash
python3 pipeline.py --patient "Test" --drg H02E --wait-days 60 --case-id t1 | grep "undue delay"
```

Expect `False`.

---

## 5. Verify the agent revises itself

```bash
python3 tick.py --case case-H02E-anna --demo
```

```
Day  7  -> ESCALATED wait-watch   home_wait_days exceeded 90
Day 35  -> ESCALATED decision     timer elapsed
Day 48  -> CLOSED    auth         authorisation_granted
Day 48  -> CLOSED    appeal       authorisation_granted
Day 95  -> RETRACTED wait-watch   home_wait_days fell to 60, within 90
```

The last line is the point. At day 7 the agent concluded the domestic wait
justified an undue-delay claim. At day 95 the queue had shortened, so it
**withdrew its own earlier conclusion** rather than letting a stale judgement
stand. Conditional loops are re-evaluated on every tick regardless of their
current status, which is what makes retraction possible.

Every transition is in an append-only audit log:

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from cbh.core import cases
conn = cases.connect()
for e in cases.history(conn,'case-H02E-anna'):
    print(e['action'], e['detail'] or '')"
```

---

## 6. Verify the cohort numbers

```bash
python3 cohort.py
```

```
procedures  : 1438  (799 inpatient, 639 outpatient)
unresolved  : 0
  median procedure        EUR       3979
  95th percentile         EUR      22575
  EUR   5000     613 procedures  (42.6% of the catalogue)
  EUR  10000     257 procedures  (17.9% of the catalogue)
```

Recompute independently from the extracted data:

```bash
python3 -c "
import json; d=json.load(open('out/se_tariffs.json'))
e=sorted(r['price_stockholm']['value']/11.45 for r in d['tariffs'])
print('n', len(e))
print('median', round((e[718]+e[719])/2))
print('>5000', sum(1 for x in e if x>5000))"
```

Expect `n 1438`, `median 3979`, `>5000 613`. The median interpolates between the
two middle values, which is what `cohort.py` reports.

These are drawn solely from the verified Swedish price list — no foreign tariffs
are assumed. The claim is about the **structure** of the Directive route: it
requires fronting the full cost, and for 42.6% of the catalogue that exceeds
€5 000. That is a plausible mechanism behind the <1% uptake the European
Parliament reports.

---

## 7. Verify the partner routing

```bash
python3 -c "
import sys; sys.path.insert(0,'.')
from cbh.core.llm import LLMClient
c=LLMClient(use_condense=True); print('condense:', c._endpoint()); print(sorted(c._headers()))
d=LLMClient(use_condense=False); print('direct  :', d._endpoint())"
```

With `CONDENSE_API_KEY` set, calls go to `api.condense.chat` carrying
`X-Condense-Upstream-Url` pointing at Gemini — one request exercising both
partners. Without it, the client silently uses Gemini directly.

Confirm the override is what makes Gemini reachable through the proxy: send the
same request with and without the header. With it, HTTP 200. Without it,
condense defaults to OpenAI and the Gemini key is rejected with HTTP 401.

Measure the saving on this workload rather than taking it on trust:

```bash
python3 bench_condense.py --cases 6
```

Measured on six route-reasoning cases:

```
                     direct   condense
input tokens           2697        783    -71.0%
output tokens           800        828
TOTAL tokens           3497       1611    -53.9% net
```

### Two findings worth knowing

**Compression can remove instructions.** In the first version the task
instruction ("answer in at most four sentences") sat in the system prompt
alongside the legal corpus. Compression compacted the repeated block and took
the instruction with it: output grew from 861 to 5 197 tokens and the length
limit stopped being obeyed, turning a 80% input saving into a 60% *net loss*.
Moving anything the model must obey into the per-case message fixed it. The
benchmark now reports NET tokens and warns when input savings are outweighed.

**Compression can change conclusions.** Even after the fix, the two routes
disagree on roughly half the cases: direct answers recommend S2 consistently,
while compressed ones sometimes recommend the Directive route. This is semantic
drift, not a formatting artefact.

It does not affect patient-facing output, by design: the route recommendation is
computed arithmetically in `cbh/core/routes.py` and the LLM never selects a
route. It drafts the application and reads documents; it does not decide. Verify
this by reading `compare_routes()` — there is no model call in it.

The operating conclusion: compression is confined to **drafting**, where the
output is prose a human reviews before filing. It is not used for ingestion
(the document text is the payload, not repeated context) and not for decisions
(route selection is arithmetic). Reproduce the ingestion failure:

```bash
USE_CONDENSE=1 python3 cbh/ingest/agent.py data/raw/pl_jgp_katalog_2025.pdf \
    --country PL --source-id pl_nfz_jgp_2025 --pages 1 --start 1   # 0 rows
USE_CONDENSE=0 python3 cbh/ingest/agent.py data/raw/pl_jgp_katalog_2025.pdf \
    --country PL --source-id pl_nfz_jgp_2025 --pages 1 --start 1   # 43 rows
```

`agent.py` now builds its client with `use_condense=False` explicitly, so the
`USE_CONDENSE=1` form above is only reachable by editing the call — the finding
is enforced in code, not left to configuration.

---

## Known limits

- **DRG label translations are not verifiable the way figures are.** There is no
  source line to check an English expansion against, so they are treated as
  presentational: the DRG code is the identity, and the Swedish source label is
  always displayed alongside. Spot-check a few against the PDF if you like —
  `H02E` "Prim ledprot höft U" → "Primary hip replacement, uncomplicated".
- **Polish tariffs in the route demo are a cited placeholder.** The NFZ
  statistics API returns `Maintenance` (HTTP 500); the agentic path works
  against the static catalogue. Marked as such in the output, not hidden.
- **Waiting times are supplied on the command line.** Automated ingestion from
  `vantetiderivarden.se` is not yet wired.
- **FX rates are fixed constants** in `pipeline.py`, not a dated source.
- **Medically justifiable delay is a clinical judgement.** The 90-day guarantee
  is used as a documented proxy; it is not a substitute for a clinician.
