# AGENTS.md

Cross-border care agent: finds EU patients the fastest legal route to treatment
they are entitled to (Directive 2011/24/EU vs. S2 / Reg. 883/2004), computes
which route pays, shows where the procedure is offered, and drafts the paperwork.
Background: [README.md](./README.md), [ARCHITECTURE.md](./ARCHITECTURE.md),
[VERIFY.md](./VERIFY.md).

## Rules that must not be broken

1. **Decisions are arithmetic, never model output.** `cbh/core/routes.py` makes no
   LLM call (`grep -c LLMClient cbh/core/routes.py` must stay `0`). Models read,
   match and write; they never choose a route or produce a figure.
2. **No figure without a source.** Money and wait values are `Cited` objects
   (`cbh/core/provenance.py`) carrying source id, page and the verbatim raw line.
   `require()` refuses untraceable values. Do not add a bare number to output.
3. **Models only pick from closed lists.** Cross-country matching goes through
   `match_procedure()` in `cbh/core/matcher.py`: the answer must be one of the
   candidates we supplied, above a confidence threshold. Never let a model emit
   a code, provider or figure that was not in its input.
4. **Never invent data.** If a country publishes no open per-procedure data, add
   it to `GAPS` in `cbh/core/providers.py` with the reason. Do not fill gaps with
   plausible hospitals, tariffs or waits. Anything modelled or assumed must be
   labelled as such on screen (see the "assumed" and "modelled" wording in
   `ui.html` / `server.py`).
5. **No clinical claims.** Decision support only. Medically justifiable delay is a
   clinician's judgement; the statutory guarantee is a documented proxy.
6. **Compression proxy stays out of ingestion and matching.** condense.chat is
   for drafting only; it destroyed extraction and changed route conclusions
   (VERIFY.md §7). `agent.py` and `matcher.py` use `LLMClient(use_condense=False)`.
7. **Never commit secrets.** `.env` is gitignored; only `.env.example` is tracked.
   `python3 cbh/core/config.py` prints masked status and is safe to screen-share.

## Commands

```bash
pip3 install -r requirements.txt           # pdfplumber, requests
cp .env.example .env && chmod 600 .env     # add GEMINI_API_KEY

python3 server.py                          # UI + JSON API, http://localhost:8000
PORT=8011 python3 server.py                # another port if 8000 is taken

python3 cbh/ingest/se_norddrg.py           # Swedish tariffs (1 438, 0 unresolved)
python3 cbh/ingest/translate.py            # DRG shorthand -> English (needs key)
python3 cbh/ingest/nl_wachttijden.py       # Dutch waits (105 MB download; --csv for local)
python3 cbh/ingest/pl_jgp.py               # NFZ JGP catalogue from data/raw/pl_jgp_zarz48_2025.pdf
python3 wake.py [--every 2592000] [--simulate-tariff 8 --simulate-wait 60]   # wake live cases
python3 cohort.py                          # headline numbers
python3 pipeline.py --patient "Anna L." --drg H02E --wait-days 240
python3 tick.py --case case-H02E-anna --demo
python3 tick.py --case case-H02E-live --live [--simulate 60] [--every 3600]   # needs a live case: choose a provider in the UI first
python3 export_snapshot.py                 # freeze the ledger before deploying
```

There is no test suite or linter configured. Verify by running the relevant
command and exercising the endpoint; [VERIFY.md](./VERIFY.md) lists independent
checks for each claim. For UI changes, load the page and check the browser, not
just the JSON.

## Layout

| Path | Purpose |
|---|---|
| `server.py` | `BaseHTTPRequestHandler` JSON API + serves `ui.html`. Standard library only. |
| `ui.html` | Single-file UI (vanilla JS, no build). Edit independently of the server. |
| `api/index.py` | Vercel entry; re-exports the same handler. `vercel.json` rewrites all paths to it. |
| `cbh/core/routes.py` | `compare_routes()`: both routes, capital at risk, Art. 20(2) argument. |
| `cbh/core/countries.py` | Per-country statutory wait guarantee, currency, National Contact Points. |
| `cbh/core/provenance.py` | `Cited`, `Source`, `SOURCES`, SHA-256 hashing. |
| `cbh/core/matcher.py` | Candidate-constrained procedure matching. |
| `cbh/core/providers.py` | Aggregator over country adapters + `GAPS` (countries not searched). |
| `cbh/core/nfz.py` | Poland: NFZ waiting-list API (live, cached 6 h). DRG→benefit match cached in `out/nfz_match.json`. |
| `cbh/core/nl.py` | Netherlands: NZa Zorgbeeld snapshot `out/nl_waits.json`. Match cached in `out/nl_match.json`. |
| `cbh/core/wake.py` | Monthly wake: fingerprints sources (HTTP HEAD), re-ingests changed ones, reads provider and home queues, ticks, reopens closed loops. |
| `cbh/core/casefile.py` | The guided case: intake → documents → identify → rules → compare → evidence → revise → paperwork. State in `cases.payload`; one `events` row per step. |
| `cbh/core/docs.py` | Reads uploaded PDF/text; a field survives only if its verbatim quote is in the document and numbers/dates trace to it. |
| `cbh/core/intake.py` | Patient's words → quoted facts; free-text procedure → NordDRG from a closed candidate list. |
| `case.html`, `samples/` | The case UI (front door at `/`, voice via Web Speech API) and one synthetic document set per operation, with `samples/scenarios.json` as the manifest served at `/api/scenarios` (`samples/make_samples.py` regenerates both; needs `fpdf2`, dev only). |
| `cbh/core/replan.py` | On escalation: ranks other providers by current wait, drafts the replacement letter, stores a pending proposal; `resolve()` approves/rejects. Nothing changes without approval. |
| `cbh/core/jgp.py` | NordDRG → NFZ JGP group (code, points, page/line). Points are not PLN. |
| `cbh/core/se_waits.py` | Real Swedish median waits from SKR (app.vantetider.se), by procedure and region. |
| `cbh/core/watch.py` | Live stage 6: re-reads the chosen provider's wait from the register and ticks the ledger. Simulated readings are labelled. |
| `cbh/core/draft.py` | S2 letter drafting + verifier. |
| `cbh/core/cases.py` | SQLite ledger (`out/cases.db`): cases, loops, append-only events, `tick()`. |
| `cbh/core/llm.py` | `LLMClient`: Gemini direct or via condense; retries and fallbacks. |
| `cbh/ingest/` | Swedish parser (self-validating), Gemini document agent, translator, Dutch snapshot builder. |
| `out/` | Generated data. Mostly gitignored; see below. |

## API

`GET /api/procedures?q=` · `/api/route?drg=&wait=&home=[&provider=]` · `/api/providers?drg=&home=` · `POST /api/case/{start,upload,sample,step,paperwork}` · `/api/scenarios` · `/api/watch?drg=&provider=&action=open|check|wake|approve|reject[&simulate=&simulate_tariff=&proposal=]` · `/api/match?drg=` · `/api/homewait?drg=&region=` ·
`/api/draft?drg=&wait=&home=&patient=` · `/api/replay` · `/api/timeline?case=` ·
`/api/cohort` · `/api/countries`. The UI deep-links as `/?drg=H02E&wait=240&home=SE`.
Handler exceptions are returned as `{"error": ...}` with HTTP 200 so a demo
survives; check the body, not the status code.

## Conventions and gotchas

- **The DRG code is the identity of a procedure.** Translated labels are
  presentational only; never match, price or decide on them.
- **Adding a provider country** (adapter shape in `providers.py`): return
  `{matched, match{benefit, confidence, reasoning}, total, providers[], source{name,url}}`;
  provider rows use `provider, place, address, postcode, locality, region, phone,
  avg_wait_days, awaiting, updated`; use `None` for fields the source lacks. Register
  it in `ADAPTERS` and remove it from `GAPS`. Only add a country if its data is open,
  machine-readable and per procedure. Matching should reuse `match_procedure()`.
- **NFZ API:** page size max 25, aggressive 429s (retry with backoff, keep
  concurrency ≤ 3). `dates` is always null; the usable wait is
  `statistics.provider-data.average-period` (0 means "none reported").
- **Dutch data** is ISO-8859-1, `;`-separated; `INSUFFICIENT_OBSERVATIONS` rows have
  no wait. The snapshot keeps the latest reading per location, dropping any older
  than 60 days.
- **Choosing a provider** (UI "Use this provider", `provider=<key>` on `/api/route`, `/api/draft`, `/api/watch`) makes the abroad wait that provider's reported figure from the register and names it in the letter. The key is `providers.provider_key()`; the server re-resolves it from the register rather than trusting client numbers. Without one the wait is the 21-day assumption (`ABROAD_WAIT_ASSUMED`), labelled as such.
- **Tariffs exist for Sweden only.** Other home countries get a country-correct
  entitlement test but an illustrative (Swedish-based) financial comparison, and
  the UI says so. The treatment-country tariff and the 21-day abroad wait are
  modelled, not ingested.
- **Standard library only on the serving path.** `server.py` and anything it
  imports at module load must not require third-party packages. `requests` and
  `pdfplumber` are used only by `llm.py` and the ingest scripts, imported lazily.
- **Serverless is read-only and ephemeral.** Cache writes (`_write` in `nfz.py`)
  are best-effort and must not raise. The case ledger falls back to
  `out/timeline_snapshot.json`; re-run `export_snapshot.py` after changing it.
- **`out/` is gitignored except** `se_tariffs.json`, `se_labels_en.json`,
  `cohort.json`, `timeline_snapshot.json`, `nfz_match.json`, `nl_waits.json`,
  `nl_match.json`, `pl_jgp.json`, `jgp_match.json`, `se_wait_match.json`. A new runtime data file needs a `!out/...` line in `.gitignore`.
- **Sample documents are synthetic and must stay labelled.** Clinic offers and replies in `samples/` carry a SAMPLE banner and show as SAMPLE in the UI. Never present a sample figure as real, and never add real patient data. Document facts need a quote that occurs in the document; extend `docs.verify_field` rather than loosening it.
- **Nothing may be specific to one operation.** Example cases are data: add a scenario to `SCENARIOS` in `samples/make_samples.py` and rerun it, never a new branch in `casefile.py` or `case.html`. fpdf's Helvetica is Latin-1 only, so Polish, Czech and Lithuanian names use unaccented spellings; Swedish and German text is written normally. An offer in a currency missing from `casefile.PER_EUR` is reported as uncomparable, never converted at a guessed rate.
- **Identification tries every wording, then stops.** `casefile._procedure_texts` feeds the referral, the patient's words, the waiting-list notice and each clinic offer to `match_drg` in turn and takes the first verified match; the source is recorded and shown. If none matches, `compare()` writes `comparison.unavailable` with the attempts and the UI shows no figures. Do not lower `matcher.min_confidence` to force a match.
- **The case recommendation is arithmetic** (`casefile._recommend`): any S2 route beats paying up front for a patient who cannot advance money; a route a clinic has ruled out is never a candidate. Timing figures not in a source (authorisation or reimbursement time) are stated as unknown, never invented.
- **The agent proposes, the patient disposes.** Never apply a provider switch (or any change to a case) from `wake`/`check`; write a proposal and wait for `replan.resolve(..., approve=True)`. Proposal figures come from the registers; the model only drafts the letter, and a missing key must degrade to a proposal without a letter.
- **Languages.** Letters are written in the home country's language (`countries.py`, `Country.language/authority`) or, for the provider enquiry, the treatment country's. The figure and clinical-claim checks in `draft.py` apply in every language; extend `CLINICAL_TERMS` when adding one. A modelled tariff is withheld from letters.
- **Swedish waits** come from an undocumented SKR endpoint; data lags months, and Region Stockholm has no operation-wait series. Show the month; never fill a missing region.
- **JGP points** are not zloty (the contract price per point is not in the document), so no Polish tariff is derived.
- **FX rates are constants** (`SEK_PER_EUR`, `PLN_PER_EUR` in `server.py`,
  `PER_EUR` in `casefile.py`), not a
  dated source.
- **Port 8000** may already be held by another process (a Matrix OS helper was
  seen there). Check with `lsof -nP -iTCP:8000 -sTCP:LISTEN` before assuming a
  fresh server is what you are seeing.
- Escape provider/source strings in `ui.html` with `esc()` before inserting into
  HTML.

## Definition of done

The change runs end to end, new figures carry provenance, anything assumed is
labelled, no secret is staged, and the relevant docs (README, ARCHITECTURE
module map and known limits, this file) reflect it.
