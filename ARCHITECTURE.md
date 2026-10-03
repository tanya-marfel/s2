# Architecture

## What this system is

An agent that finds EU patients the fastest legal route to treatment they are
already entitled to, computes which of the two statutory routes actually pays,
and files the paperwork.

The hard parts are not medical. They are: reading 27 countries' tariff documents
in 24 languages, mapping procedures between incompatible national coding
systems, comparing two legal instruments that pay differently, and following
through over months rather than minutes.

---

## Design principles

### 1. Deterministic where we can, agentic where we must, provenance always

LLMs are used where the problem is genuinely open-ended — reading an unfamiliar
document in an unfamiliar language, mapping a procedure name across coding
systems, drafting prose. They are **not** used where a deterministic answer
exists.

| Task | Mechanism | Why |
|---|---|---|
| Swedish tariff extraction | Regex + arithmetic cross-check | One known format; verifiable exactly |
| Any other national document | Gemini + output verification | No parser exists; format unknown |
| DRG label translation | Gemini, cached | Clinical shorthand expansion; **presentational only** |
| Procedure code matching (JGP, NFZ benefit, NZa treatment, SKR procedure) | Gemini, constrained to a candidate list | Semantic; but the answer space is closed |
| **Route selection** | **Pure arithmetic** | A decision that affects a patient |
| Application and provider enquiry, in the right language | Gemini, then a verifier | Prose, reviewed by a human before filing |
| Provider lists, waits, tariff change, source change | Plain HTTP and arithmetic | Figures come from the registers, never from a model |

NordDRG labels are compressed clinical shorthand rather than plain Swedish
(`Prim ledprot hoft U` → *Primary hip replacement, uncomplicated*), so expanding
them needs a model. But a translated string is never used to match, price or
decide anything: the **DRG code remains the identity** of a procedure. A bad
translation is a display bug, not a wrong recommendation. The UI shows the
source label alongside the English so the original is always visible.

### 2. Decisions are arithmetic, never inference

`cbh/core/routes.py` contains no model call. Verify:

```bash
grep -c "LLMClient" cbh/core/routes.py   # 0
```

This matters because measurement showed the LLM is not stable enough to decide.
Running the same six route cases direct and through a compressing proxy produced
**different recommendations on half of them** (see VERIFY.md §7). Because route
selection is arithmetic, that drift cannot reach a patient. The model reads and
writes; it does not decide.

### 3. No figure without a source

Every monetary value is a `Cited` object carrying `source_id`, `page` and the
verbatim `raw_line` it came from. Source documents are SHA-256 hashed.
`require()` raises rather than present a value with no traceable origin.

---

## System shape

```
                    ┌──────────────────────────────────────────┐
  tariff and wait   │  STAGE 1  INGEST                         │
  sources      ───▶ │  se_norddrg.py   SE tariffs, self-check  │
  (PDF, API, CSV)   │  pl_jgp.py       PL JGP catalogue        │
                    │  nl_wachttijden  NL waits (snapshot)     │
                    │  nfz.py / se_waits.py  live registers    │
                    │  agent.py        Gemini + verification   │
                    └──────────────────┬───────────────────────┘
                                       │  Cited figures
                                       ▼
                    ┌──────────────────────────────────────────┐
                    │  STAGE 2  MATCH   matcher.py             │
                    │  NordDRG H02E ↔ NFZ JGP H13              │
                    │        ↔ NFZ benefit ↔ NZa treatment     │
                    │        ↔ SKR procedure                   │
                    │  proposed by Gemini, verified against    │
                    │  the candidate list it was given         │
                    └──────────────────┬───────────────────────┘
                                       ▼
   reported waits ──▶ ┌────────────────────────────────────────┐
   (SE median, and    │  STAGE 3/4  COMPUTE + ARGUE routes.py  │
   the chosen         │  Directive 2011/24/EU  vs  S2 883/2004 │
   provider's)        │  cost | capital at risk | days saved   │
                      │  Art. 20(2) undue-delay test           │
                      │  ** no model call **                   │
                      └─────────────────┬──────────────────────┘
                                        ▼
                    ┌──────────────────────────────────────────┐
                    │  STAGE 5  DRAFT      draft.py            │
                    │  S2 application (home language) and      │
                    │  provider enquiry (provider's language); │
                    │  figures supplied only, then verified    │
                    └──────────────────┬───────────────────────┘
                                       ▼
                    ┌──────────────────────────────────────────┐
                    │  STAGE 6  PERSIST  cases.py watch.py     │
                    │                    wake.py               │
                    │  wake · fingerprint sources · re-ingest  │
                    │  what changed · tick() · escalate ·      │
                    │  retract · reopen · PROPOSE a switch     │
                    │  (replan.py; patient approves) · audit   │
                    └──────────────────────────────────────────┘
```

---

## The case flow

The front door (`case.html`, `casefile.py`) wraps the same engine around a patient
story. Every step is its own call so progress is visible and each writes an audit row.

| Step | What happens | Model? |
|---|---|---|
| intake | Patient's words become facts, each with a verbatim quote that must occur in the statement | reads |
| documents | Referral, waiting-list notice, clinic offer/reply read; a field is kept only if its quote is in the text and its number or date traces to it (`docs.verify_field`) | reads |
| identify | Free-text procedure → NordDRG, chosen from a closed list of candidates. Every independent wording in the case (referral, the patient's own words, the waiting-list notice, each clinic offer) is tried in turn, so a phrasing the matcher will not commit to does not sink the case | matches |
| rules | Projected wait (expected date − registration date) against the guarantee; Sweden's reported median as context | none |
| compare | Each offer under both routes: upfront, reimbursement, remaining, timing. Offer prices are cited to the document; the home tariff to the price list | none |
| evidence | Checklist derived from Art. 20 and Directive Arts. 7-9, per clinic; present / missing / blocked / you supply / later | none |
| revise | A clinic reply ("we do not accept S2") overrides the offer; recompute, rank again, list what each way forward needs | none |
| paperwork | S2 application (home language), attachment checklist, request for exactly the missing information (clinic's language) | writes, then verified |

The recommendation is arithmetic (`casefile._recommend`): for a patient who cannot
advance a large sum any S2 route, confirmed or pending, beats paying up front; the
earliest admission wins; a route a clinic has ruled out is never a candidate. The
fallback is named. Where a figure is not in any source (S2 decision time,
reimbursement time) the page says so rather than inventing one. Sample clinics are
synthetic and labelled SAMPLE throughout.

Nothing in the flow is specific to one operation. `samples/make_samples.py` writes a
complete story per operation — referral, waiting-list notice, two clinic offers and
the reply that closes S2 at the earliest clinic — plus `samples/scenarios.json`, the
manifest the UI reads from `/api/scenarios` to offer them as example cases. Adding an
operation means adding an entry there and regenerating; no code changes. If no
wording of the operation can be placed in the price list, the case stops with what it
tried and why, and no figures are shown.

---

## Module map

```
cbh/
  core/
    countries.py    guarantees, language and S2 authority per Member State; NCPs
    provenance.py   Source, Cited, require(), SOURCES registry, SHA-256
    config.py       .env loading, masked secret status, fail-fast errors
    llm.py          LLMClient: direct Gemini or via condense; token accounting
    routes.py       compare_routes(): the two legal routes. No model call.
    matcher.py      match_procedure(): cross-country codes, candidate-constrained
    jgp.py          NordDRG -> NFZ JGP group (code, points, page, line)
    se_waits.py     reported Swedish median waits (SKR), by procedure and region
    nfz.py          Poland: NFZ waiting-list register (live provider list, waits)
    nl.py           Netherlands: NZa Zorgbeeld snapshot (provider waits)
    providers.py    where a procedure is offered, across country adapters + GAPS
    draft.py        draft_application(): S2 letter or provider enquiry, any language
    cases.py        SQLite ledger: cases, loops, append-only events; tick()
    watch.py        live case on a chosen provider; observe() and check()
    wake.py         monthly wake: fingerprint sources, re-ingest changes, tick, reopen
    casefile.py     the guided case: intake, documents, identify, rules, compare, evidence, revise, paperwork
    docs.py         reads uploads; keeps only quoted, traceable fields
    intake.py       patient's words -> quoted facts; free text -> NordDRG
    replan.py       on escalation: rank alternatives, draft, propose; approve/reject/withdraw
  ingest/
    se_norddrg.py   Swedish NordDRG parser, self-validating
    pl_jgp.py       NFZ JGP catalogue parser (Zarzadzenie 48/2025/DSOZ)
    nl_wachttijden.py  Dutch waiting-time snapshot builder
    translate.py    expands Swedish DRG shorthand to English (presentational)
    agent.py        Gemini document ingestion + verify_row()

pipeline.py         one patient, end to end, seeds open loops (CLI demo)
tick.py             agent wake-up; --demo replays a timeline, --live reads the register
wake.py             full wake of every live case; --every N for a schedule
cohort.py           sweep the whole catalogue for headline figures
bench_condense.py   A/B measurement of the proxy on this workload
demo_route.py       minimal route comparison
server.py           JSON API + static UI, standard library only
case.html           the patient front door (`/`): voice intake, documents, the case
ui.html             the procedure explorer (`/explore`, `/?drg=`); edited independently of server code
samples/            one synthetic SAMPLE document set per operation + scenarios.json
                    (the manifest the UI reads) + make_samples.py (dev only, needs fpdf2)
export_snapshot.py  freezes the SQLite ledger for serverless deployment
api/index.py        Vercel entry point, re-exports the same handler
```

Generated under `out/`: `se_tariffs.json`, `se_labels_en.json`, `pl_jgp.json`,
`nl_waits.json`, `agent_pl.json`, `cohort.json`, the match caches (`nfz_match.json`,
`nl_match.json`, `jgp_match.json`, `se_wait_match.json`), `source_fingerprints.json`,
`cases.db`, `condense_bench.json`, `<case_id>.json`.

---

## Verification layers

The distinctive property of the system is that **nothing is trusted on output**.
Each stage has an independent check that can reject it.

| Stage | What could go wrong | Check | Failure mode |
|---|---|---|---|
| 1 deterministic | Ambiguous space-grouped numbers misparsed | Every figure must equal `weight × published point price` | Row unresolved, counted, not emitted |
| 1 agentic | Model invents a line, number or code | Cited line must exist on the page; amount must be a **token-aligned** number on it; code must appear on it | Row rejected and logged |
| 2 match | Model invents a procedure code | Code must be one of the candidates supplied; confidence ≥ threshold | `verified: false`, no match returned |
| 3/4 route | Model reasoning drifts | No model involved | n/a |
| 5 draft | Model introduces a figure or a diagnosis | Every number must trace to a supplied fact; clinical vocabulary blocklist; length limit; up to 3 attempts with the failures fed back | Draft returned flagged `do not file` |
| 6 loops | A conclusion goes stale | Conditional loops re-evaluated every tick | Escalation retracted |

### Why the token-aligned number check matters

A line reads `... 125I 2 650,00`. A naive check that flattens all digits would
let a claimed amount of `1252` match across `125` and `2`. `number_tokens()`
parses whole number tokens — handling `86 945`, `22.000`, `2 650,00` — so the
amount must appear as a distinct figure.

### Why drafting needed its own verifier

An unverified drafter is not safe even when every figure is supplied. Measured
on `gemini-flash-latest`, the first version produced a markdown report instead
of a letter, ran four times over the word limit, cited CJEU case law it was
never given, and — worst — asserted a **diagnosis that was never supplied**
("severe osteoarthritis ... progressive joint deterioration, chronic pain").

Fabricating a patient's diagnosis on a document addressed to their health
authority is the most damaging failure in the system, so drafting is now
checked, not trusted: numbers must trace to supplied facts, a clinical
vocabulary blocklist is applied, length is enforced, and failures are fed back
for up to three attempts. A draft that still fails is returned marked
`do not file`.

### Why the Swedish parser validates itself

Swedish tariffs are space-grouped, making `86 945 81 040 145 454` genuinely
ambiguous as text. Rather than guess, `resolve_money()` enumerates every valid
split and selects the one matching the document's own published point prices:

```
1.082 × 80 356 (Stockholm) = 86 945  ✓
1.082 × 74 898 (Gotland)   = 81 040  ✓
```

Tolerance is derived, not guessed: weights are printed to three decimals, so the
implied price can differ by up to `0.0005 × point_price` per column. Wrong splits
are wrong by orders of magnitude, so the bound is safe. Result: **1 438 tariffs,
0 unresolved.**

---

## Data model

### Provenance

```python
Source(source_id, publisher, title, url, year, country, local_path)   # + sha256()
Cited(value, unit, source_id, page, raw_line, note)
```

`Cited` is deliberately immutable and carries `raw_line`, so any figure can be
traced to the exact text it was read from.

### Case ledger (SQLite, `out/cases.db`)

```
cases   case_id, patient, home_country, procedure, drg, created_at, status, payload
loops   loop_id, case_id, loop_type, description, trigger, owner, closure,
        status, opened_at, closed_at, evidence
events  event_id, loop_id, case_id, at, action, detail        -- append-only
```

Loop types: `timed`, `conditional`, `referral`, `retrieval`, `correlative`,
`filing`.

Statuses: `open → escalated → closed`, with `closed → open` (reopen) and
`escalated → open` (retract).

---

## Stage 6: why this is an agent, not a cron job

A timer is not agency. Two mechanisms make the difference:

**Conditional loops** carry a predicate (`home_wait_days > 90`) evaluated against
live facts, not a date. The agent must re-read the world to decide.

**Retraction.** Conditional loops are re-evaluated on **every** tick regardless of
current status. If a loop was escalated because the domestic wait exceeded the
statutory guarantee, and the queue later shortens, the agent withdraws its own
earlier conclusion:

```
Day  7  ESCALATED wait-watch   home_wait_days exceeded 90
Day 95  RETRACTED wait-watch   home_wait_days fell to 60, within 90
```

This was initially broken: loops were only evaluated while `open`, so an
escalation could never be revisited — precisely the failure the system exists to
attack. Every transition is written to the append-only `events` table.

### Deciding and acting: proposals the patient approves

When the provider's wait breaks the plan, the agent does not stop at a flag
(`cbh/core/replan.py`):

1. It re-reads every register it can search and ranks the other providers by
   their current reported wait.
2. It picks the best one that is actually faster than the current reading.
3. It drafts the replacement S2 application naming that provider, in the home
   language, through the same verifier as any letter.
4. It writes a **proposal** to the ledger (`proposals` table) and stops.

Nothing changes until the patient approves. Approving re-points the case at the new
provider and re-arms the watch; rejecting leaves the escalation standing. If the
wait later recovers, the agent **withdraws its own proposal**. A newer, different
proposal supersedes an older one. Each step is an `events` row
(`proposal_created`, `proposal_approved`, `proposal_rejected`,
`proposal_withdrawn`, `no_better_provider`). The ranking uses only register
figures; the model writes the letter and decides nothing.

The register cannot say whether a provider treats patients on an S2
authorisation, so the proposal says so and points to the provider enquiry letter.

### Against the real world, not a script

`watch.py` and `wake.py` feed `tick()` observed facts instead of a canned
timeline. Choosing a provider opens a live case with three conditional loops:
the provider's reported wait (`provider_wait_days > filed + 14`), the Swedish
tariff (`tariff_change_pct > 5`) and Sweden's reported median wait
(`home_median_wait_days > guarantee`). A wake then:

1. Fingerprints each source the case relied on (HTTP ETag, Last-Modified, length).
2. If the Swedish price list changed, re-runs the deterministic parser on the new
   file and measures how far this case's price moved; if the Dutch file changed,
   rebuilds the snapshot.
3. Reads the provider's current wait from its register and Sweden's median wait.
4. Marks closed loops whose threshold is exceeded again as invalidated, so
   `tick()` reopens them.
5. Logs a `wake` event with what was seen.
6. If the provider loop escalated (or the provider vanished from its register),
   `replan.py` acts: see below.

`python3 wake.py --every 2592000` (or cron) repeats it monthly; it runs this way
on the Matrix computer. A reading can be simulated for a demo; simulated values
are labelled in the ledger and never written to the data snapshots.

---

## Partner integration

```
            ┌──────────────┐        X-Condense-Upstream-Url
LLMClient ─▶│ condense.chat│ ──────────────────────────────▶ Gemini
            └──────────────┘                                   ▲
                  │                                            │
                  └── absent CONDENSE_API_KEY, or on 403 ───────┘
                      fall back to direct
```

- **Gemini** — stages 1, 2, 5: document extraction, proposing which entry of a
  closed list matches a procedure, and drafting. It never produces a figure that
  is shown or decides a route. `gemini-flash-latest` was chosen over pro models
  for throughput: 1.2s vs 2.6s per call, across hundreds of pages.
- **condense.chat** — optional proxy. One call exercises both partners via the
  upstream header. Measured on route-reasoning prompts: **71% fewer input
  tokens, 53.9% net**.

  Applied to **drafting only**. Benchmarking ruled it out everywhere else:
  compressing the repeated legal corpus changed the model's conclusion on half
  the test cases, and compressing document text during ingestion destroyed the
  extraction outright (43 rows direct → 0 via proxy, same page). `agent.py`
  therefore constructs its client with `use_condense=False` explicitly.
- **MatrixOS** — hosts the app and the monthly `wake.py`. Files are pushed with
  `matrixos upload` and the server is restarted from a script on the instance;
  the UI is reached through `matrixos forward 8000`. No credentials are copied
  there, so the instance has no Gemini key: matches already cached in `out/`
  work, new ones and letter drafting need a key added on the instance.

### Where compression is safe

Lossy compression interacts badly with two of the three places it could be
applied here, which is why it is confined to drafting.

| Workload | Input is | Effect of compression | Verdict |
|---|---|---|---|
| Document ingestion | The payload itself | Rows vanish: 43 → 0 on one page | **Never** |
| Route reasoning | Repeated legal corpus | Conclusion changed on ~half of cases | **Never** — and no model is used here anyway |
| Application drafting | Repeated template guidance | Prose varies; a human reviews before filing | Acceptable |

A further trap: instructions placed in the repeated block get compacted away
with it. The first benchmark lost its "at most four sentences" constraint, and
output grew 861 → 5 197 tokens, turning an 80% input saving into a 60% net loss.
Anything the model must obey belongs in the per-request message.

### Resilience

`LLMClient` degrades rather than fails: no condense key → direct Gemini; HTTP 403
on the upstream override → retry direct; 429/5xx → backoff and retry. A demo
loses an integration, not the demo.

---

## Known limits

- **The treatment-country tariff is modelled** at about 67% of the Swedish one
  and labelled so. The NFZ JGP catalogue is ingested, but it gives points, not
  zloty, and the contract price per point is not in that document, so no Polish
  tariff is derived. A modelled tariff is withheld from letters. Dutch tariffs
  are not ingested.
- **Waiting times.** Swedish waits are real (SKR, median of completed operations)
  but lag by months and a region can have no series: Region Stockholm reports none
  for operations. The patient's own wait is still an input. Provider waits are
  provider-reported by the Polish and Dutch registers.
- **Provider search covers Poland and the Netherlands only** — the countries
  whose open data names providers per procedure. Others are listed in `GAPS`
  with the reason (Portugal: hospital-level only; Czechia: no waits; and so on).
- **Registers update monthly or fortnightly**, so a change inside a demo is
  simulated, and labelled so in the ledger.
- **JGP extraction is not self-validating.** Rows are parsed by pattern with a
  plausibility bound on points; a few multi-number cells are ambiguous. Every
  figure is shown with its page and raw line so it can be checked by hand.
- **Tariffs are ingested for Sweden only.** Five Member States' statutory
  guarantees are encoded in `cbh/core/countries.py`, so the entitlement test is
  country-correct, but selecting any home country other than Sweden falls back
  to the Swedish catalogue for the financial comparison and says so on screen.
  Adding a country means running `agent.py` over its tariff document.
- **Letters are checked, not certified.** The verifier applies the same figure
  and clinical-claim rules in every language, but its clinical-term list is short
  outside English. The S2 authority is encoded only where certain (Sweden,
  Finland, Netherlands); elsewhere the letter addresses "the competent health
  authority". A human reviews before filing; nothing is submitted.
- **The case flow depends on a model key** (intake, document reading, drafting). It reads PDFs with a text layer and plain text, not scanned images. Voice uses the browser's speech recognition (Chrome/Edge); typing always works. Sample clinics and their prices are invented and labelled; real offers would be read the same way.
- **Proposals rank by reported wait only.** Distance, price and whether the
  provider is contracted for S2 are not in the registers, so the patient decides.
  A reported wait of 0 is taken at face value. The agent proposes a provider
  switch only; tariff and home-queue escalations are still flags.
- **FX rates are constants** in `pipeline.py`, `server.py` and `cbh/core/casefile.py`,
  not a dated source.
- **Medically justifiable delay is a clinical judgement.** The 90-day
  vårdgaranti is used as a documented proxy, not a substitute for a clinician.
- **Scope.** Decision-support and administrative tooling. It makes no clinical
  claim and does not assess whether treatment is appropriate.

## Next

1. Act on tariff and home-queue escalations too (re-run the route comparison and
   propose the better route).
2. A dated Polish or Dutch tariff in currency, to replace the modelled one.
3. Run `agent.py` across further national catalogues to widen country coverage.
