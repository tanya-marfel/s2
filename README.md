# Cross-Border Care Agent

An agent that finds EU patients the fastest **legal** route to treatment they are
already entitled to, works out which of the two routes actually pays, and files
the paperwork.

## The problem

Under **Directive 2011/24/EU** any EU citizen may be treated in another member
state and reimbursed at home. When it passed, the expectation was that ~10% of
people on waiting lists would use it. **Fewer than 1% do.** In 2019 total
cross-border spend under the Directive was about €30 million — roughly 0.004% of
EU-27 public health expenditure.

The European Parliament's own research attributes this to low awareness,
administrative complexity, and information scattered across 27 National Contact
Points. Those are exactly the obstacles an agent can remove.

There is also a second route almost nobody knows about.

| | Directive 2011/24/EU | S2 (Reg. 883/2004) |
|---|---|---|
| Prior authorisation | Often not required | Mandatory |
| Reimbursed at | **Home** country rates, capped | **Treatment** country tariffs |
| Who pays first | **The patient, in full** | The institutions settle directly |
| Patient fronts | Entire cost | Local patient charges only |

The out-of-pocket cost is frequently identical. The **capital the patient must
advance** is not — and that difference decides whether the right is usable at all.

## What the system does

| # | Stage | Module | State |
|---|---|---|---|
| 1 | **Ingest** tariff and waiting-time sources, any language | `cbh/ingest/` | Sweden tariffs (1 438, self-validated) and waits, Poland JGP catalogue (566 groups), Dutch waits, Polish provider register |
| 2 | **Match** a procedure across national coding systems | `cbh/core/matcher.py`, `jgp.py`, `nfz.py`, `nl.py`, `se_waits.py` | NordDRG `H02E` ↔ NFZ JGP `H13` ↔ NFZ benefit ↔ NZa treatment ↔ SKR procedure |
| 3 | **Compute** both routes: cost, capital at risk, days saved | `cbh/core/routes.py` | Done; treatment-country tariff still modelled |
| 4 | **Argue** undue-delay entitlement under Art. 20(2) | `cbh/core/routes.py`, `se_waits.py` | Logic plus real reported Swedish waits (median, by region) |
| 5 | **Draft** the application and the provider enquiry in the right language | `cbh/core/draft.py` | S2 application in the home language, enquiry in the provider's language, both verified |
| 6 | **Persist** wake, re-ingest, detect changed tariffs or queues, reopen, act | `cbh/core/watch.py`, `wake.py`, `replan.py`, `cbh/core/cases.py` | `wake.py` re-checks sources, re-ingests what changed, escalates, retracts or reopens. When the chosen provider's wait breaks the plan it proposes a faster provider with a drafted replacement letter and waits for approval |

### Deterministic where we can, agentic where we must, provenance always

- `cbh/ingest/se_norddrg.py` — a hand-written parser for the Swedish price list.
  Every figure is cross-checked against the document's own published point price,
  so the extraction validates itself. **1 438 tariffs, 0 unresolved.**
- `cbh/ingest/agent.py` — Gemini reads any national tariff document in any
  language. Every figure it returns is verified to actually occur in the source
  line it cited; anything that fails is dropped, never surfaced.
- `cbh/core/provenance.py` — figures carry source, page and raw line. Sources are
  SHA-256 hashed. `require()` refuses to present a value with no traceable source.

## Quick start

```bash
pip3 install -r requirements.txt
cp .env.example .env && chmod 600 .env     # add GEMINI_API_KEY
python3 cbh/core/config.py                 # masked check, safe to screen-share

python3 cbh/ingest/se_norddrg.py           # 1 438 Swedish tariffs
python3 cbh/ingest/translate.py            # expand DRG shorthand to English
python3 cbh/ingest/nl_wachttijden.py       # Dutch hospital waits (~105 MB download)
python3 cbh/ingest/pl_jgp.py               # NFZ JGP catalogue (881-page PDF, a few minutes)
python3 wake.py                            # monthly wake-up of every live case
python3 cohort.py                          # headline cohort numbers
python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240
python3 tick.py --case case-H02E-anna --demo
```

### The case flow (front door)

`python3 server.py`, then open `/`. A patient says (or types) what is going on, adds
documents, and the agent works the case through step by step:

1. **Voice or text intake.** "My doctor recommended this operation. I'm waiting, and I
   can't afford a large upfront payment." Browser speech recognition (Chrome/Edge) or
   typing; Gemini extracts what was said, each fact with a verbatim quote.
2. **Documents.** Referral, waiting-list notice, clinic offers (PDF or text). Gemini
   reads them; a field survives only if its quote occurs in the document and its
   number or date traces to that quote.
3. **Investigation.** Identify the procedure (NordDRG, from the referral's own words,
   falling back to the patient's words and the clinics' own descriptions),
   test the rules (projected wait vs the guarantee, Art. 20(2)), compare every clinic
   offer under both routes with **separate figures** — upfront payment, estimated
   reimbursement, remaining cost, timing dependencies — and list the evidence still
   missing.
4. **A complication.** The earliest clinic replies that it does not accept S2. The agent
   revises the comparison and lists what each way forward would need.
5. **Paperwork.** The S2 application in the home language, an attachment checklist, and
   a request to the clinic for exactly the information still missing.

Nothing in the flow is tied to one operation. `samples/` holds a complete example
case per operation — cataract surgery, hip replacement, inguinal hernia repair and
lumbar spinal fusion — each with its own referral, waiting-list notice, two clinic
offers and the reply that closes the S2 route at the earliest clinic. The UI offers
them on the first screen; `samples/scenarios.json` is the manifest, regenerated by
`python3 samples/make_samples.py` (needs `fpdf2`). Adding another operation is a data
change, not a code change.

Those documents are **synthetic**, banner-labelled and shown as SAMPLE in the UI.
Don't upload real medical records: documents are sent to a third-party model. The
procedure explorer is at `/explore`.

### Web UI

```bash
python3 server.py        # http://localhost:8000
```

Standard library only — nothing to install. Search all 1 438 procedures, adjust
the domestic waiting time, and see both routes compared side by side with the
source line and page for every figure, plus the providers that actually offer
the procedure, searched across every country with open per-procedure data:
Poland (NFZ waiting-list register, `cbh/core/nfz.py`) and the Netherlands (NZa
Zorgbeeld, `cbh/core/nl.py`). Other countries are listed as not searched, with
the reason; add one by writing an adapter in `cbh/core/providers.py`.
Choose a provider and its reported wait replaces the assumed one in the
comparison, the S2 letter names it, and the agent then watches that provider's
wait in the register (`cbh/core/watch.py`, `tick.py --live`), escalating if it
rises past the figure the plan was filed on. Views are deep-linkable:
`/?drg=H02E&wait=240`.

### Deploy to Vercel

The UI imports nothing outside the standard library, and Vercel's Python runtime
accepts a `BaseHTTPRequestHandler`, so the same code serves locally and in
production. **No Node or Vercel CLI needed** — deploy from the Git repo via the
Vercel dashboard.

```bash
python3 cbh/ingest/se_norddrg.py     # regenerate runtime data
python3 cbh/ingest/translate.py
python3 cohort.py
python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240
python3 tick.py --case case-H02E-anna --demo
python3 export_snapshot.py           # freeze the ledger for serverless
git add -A && git commit -m "deploy" && git push
```

Then import the repo at vercel.com. `api/index.py` re-exports the handler and
`vercel.json` rewrites all paths to it.

A few small JSON files under `out/` are committed deliberately — they are the
runtime data (~920 KB). Everything else in `out/`, all of `data/raw/`, and
`.env` stay ignored.

**Serverless caveat:** filesystems are ephemeral, so the SQLite case ledger
cannot persist. `export_snapshot.py` freezes it to JSON and the server falls
back to that when no live database is present. The deployed timeline is
therefore read-only; running locally uses the real ledger.

To exercise the agentic ingestion (needs a key):

```bash
python3 cbh/ingest/agent.py data/raw/pl_jgp_katalog_2025.pdf \
    --country PL --source-id pl_nfz_jgp_2025 --pages 2 --start 1
```

See **[VERIFY.md](./VERIFY.md)** to check every claim independently.

## Partner integrations

- **Google DeepMind / Gemini** — multilingual document ingestion, cross-country
  procedure matching, application drafting.
- **condense.chat** — optional proxy in front of Gemini, enabled by adding
  `CONDENSE_API_KEY`. `X-Condense-Upstream-Url` points back at Gemini, so one
  request exercises both partners; absent the key the client falls back to
  direct Gemini with no code change. Measured on route-reasoning prompts:
  **71% fewer input tokens, 53.9% net** (`bench_condense.py`).

  Confined to **drafting**. Benchmarking ruled out the other two uses:
  compression changed the model's conclusion on half the route cases, and during
  ingestion it destroyed the extraction entirely (43 rows direct → 0 via proxy).
  Route selection is arithmetic in `cbh/core/routes.py` with no model call, and
  `cbh/ingest/agent.py` opts out of the proxy explicitly. See VERIFY.md §7.
- **MatrixOS** — intended host for stage 6, where the agent wakes on a schedule.

## Scope and limits

This is a decision-support and administrative tool. It makes **no clinical
claims** and does not assess whether treatment is appropriate — that is the
clinician's judgement. Tariffs change; figures are only as current as the
ingested documents, which is why every one of them is dated and hashed.

Polish tariffs are currently a cited placeholder in the route demo: the NFZ
statistics API returns `Maintenance`. The agentic ingestion path works against
the static catalogue.
