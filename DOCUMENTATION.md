# dograh_caller — Project Documentation

> Quick start: [README.md](README.md) · Describing services for Jev: [SERVICES.md](SERVICES.md)

## Contents

1. [Purpose](#1-purpose)
2. [System overview](#2-system-overview)
3. [Project structure](#3-project-structure)
4. [Installation and configuration](#4-installation-and-configuration)
5. [Running the pipeline](#5-running-the-pipeline)
6. [Stage 1 — Number verifier](#6-stage-1--number-verifier)
7. [Stage 2 — Decision maker (Jev)](#7-stage-2--decision-maker-jev)
8. [Stage 3 — Campaigns](#8-stage-3--campaigns)
9. [Stage 4 — Results spreadsheet](#9-stage-4--results-spreadsheet)
10. [Concurrency model](#10-concurrency-model)
11. [Python API reference](#11-python-api-reference)
12. [Extending the system](#12-extending-the-system)
13. [Troubleshooting](#13-troubleshooting)
14. [Limitations and known behaviour](#14-limitations-and-known-behaviour)
15. [Compliance](#15-compliance)

---

## 1. Purpose

`dograh_caller` takes business leads (scraped from the web or listed in a
spreadsheet), decides with the **Jev** model which of our services each lead is
likely to buy, and has the matching **Dograh** voice agent call the interested
ones. Every lead and its outcome is recorded in an Excel sheet.

| Service | Dograh agent | Status |
|---|---|---|
| Draftliner | 2 — *Testing Lead qualification - outbound* | enabled |
| Paradise Estimating | 3 — *Paradise Agent - outbound* | enabled |

---

## 2. System overview

```
                 ┌───────────────────────────────────────────────────────────────────────────┐
  AI-powered     │                               dograh_caller                               │
  scraper  ──┐   │                                                                           │
  (dicts)    ├──►│  Number verifier ──► Decision maker ──► per-service lists ──► Campaigns   │
  .xlsx /    │   │   US numbers only     Jev: P(interested)   Draftliner: [...]   one per    │
  .csv /   ──┘   │   dedupe, E.164       per service ≥ 85%    Paradise:   [...]   service    │
  .json          │        │                    │                                    │        │
                 │        └── dropped ─────────┴── not interested ──────── outcome ─┘        │──► output/call_results.xlsx
                 └───────────────────────────────────────────────────────────────────────────┘
```

**Lifecycle of a run**

1. Leads (`dict`s) are submitted.
2. The **verifier** finds the phone field, validates it and keeps it only if it
   is a valid, not-yet-seen US number. Otherwise → row with `dropped_*` status.
3. The **decision maker** asks Jev one yes/no question per enabled service. If the
   highest probability is ≥ the threshold (85%), the lead is added to that
   service's list. Otherwise → row with `not_interested`.
4. When all leads are scored, **one Dograh campaign per service** that has
   interested leads is created automatically, named `<service name>_<date>`
   (e.g. `Draftliner_2026-11-19`), using that service's agent and containing only
   that service's numbers. The campaigns are started and run in parallel.
5. Each call (and Dograh's automatic retries) is followed until the lead has a
   final outcome → row with `called` (or `call_failed` / `call_timeout`).
6. The **results store** writes each row into today's block of the spreadsheet.

Campaigns appear on the Dograh dashboard under **Campaigns**.

---

## 3. Project structure

```
dograh_caller/
├── main.py              Command-line entry point (reads .xlsx / .csv / .json)
├── pipeline.py          LeadPipeline: queues, workers, logging, summary
├── number_verifier.py   Stage 1 – phone validation (US only)
├── decision_maker.py    Stage 2 – Jev questions, service loading
├── campaign_manager.py  Stage 3 – one campaign per service: create, start, follow calls
├── dograh_client.py     Dograh HTTP client (agents, CSV upload, campaigns, runs)
├── results_store.py     Stage 4 – .xlsx writer
├── config.py            All settings (read from .env / environment)
├── services.yaml        Our services, as described to Jev  ← edit this to add services
├── requirements.txt
├── README.md            Quick start
├── SERVICES.md          How to write service descriptions
├── DOCUMENTATION.md     This file
├── examples/
│   ├── from_python.py   Feeding dicts from Python code (scraper integration)
│   └── sample_leads.xlsx  Test leads with fictional 555-01xx numbers
└── output/              Created on first run
    └── call_results.xlsx
```

---

## 4. Installation and configuration

### Requirements

- Python 3.10+
- `pip install -r requirements.txt`
  (`typesafe-sdk`, `phonenumbers`, `requests`, `openpyxl`, `PyYAML`, `python-dotenv`)

### `.env`

Settings are read from, in priority order: real environment variables →
`dograh_caller/.env` → `Task10/.env` (parent folder).

```ini
# Required
Jev_Key=...                       # TypeSafe / Jev API key (TYPESAFE_API_KEY also accepted)
DOGRAH_API_KEY=...                # Dograh: Settings -> API Keys (same deployment as the URL)
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

### All settings

| Variable | Default | Meaning |
|---|---|---|
| `Jev_Key` / `TYPESAFE_API_KEY` | — | Jev API key (required) |
| `JEV_MODEL` | `jev-1.14.0` | Jev model; pinned so behaviour doesn't change silently (`jev-latest` moves) |
| `INTEREST_THRESHOLD` | `0.85` | Minimum probability for a lead to be called |
| `ALLOWED_PHONE_REGIONS` | `US` | Phone regions accepted, comma-separated ISO codes. `US,PR,VI,GU,MP,AS` adds the US territories |
| `SKIP_ALREADY_CALLED` | `true` | Block numbers already called in earlier runs (read from the results sheet) |
| `SERVICES_FILE` | `services.yaml` | Service definitions |
| `DOGRAH_API_KEY` | — | Dograh API key (required to place calls) |
| `DOGRAH_BASE_URL` | `https://dograh.grobots.ai/api/v1` | Dograh API base URL, must end in `/api/v1` |
| `DOGRAH_VERIFY_SSL` | `true` | Set `false` only for self-signed certificates |
| `DOGRAH_REQUEST_TIMEOUT` | `30` | Seconds per HTTP request |
| `CAMPAIGN_NAME_DATE_FORMAT` | `%Y-%m-%d` | Date part of campaign names (`Draftliner_2026-11-19`) |
| `CAMPAIGN_RETRY_ENABLED` | `true` | Let Dograh retry unanswered calls |
| `CAMPAIGN_MAX_RETRIES` | `1` | Extra attempts per number |
| `CAMPAIGN_RETRY_DELAY_SECONDS` | `120` | Wait before a retry (Dograh minimum 30) |
| `CAMPAIGN_RETRY_ON_BUSY` / `_NO_ANSWER` / `_VOICEMAIL` | `true` / `true` / `false` | Which outcomes are retried |
| `CAMPAIGN_POLL_SECONDS` | `5` | How often running campaigns are checked |
| `CAMPAIGN_IDLE_TIMEOUT_SECONDS` | `900` | If a campaign makes no progress for this long, its unfinished leads become `call_timeout` |
| `RESULT_WAIT_SECONDS` | `20` | After a call ends, wait this long for Dograh to fill in the outcome |
| `RETRY_GRACE_SECONDS` | `120` | Extra wait beyond the retry delay for a retry call to appear |
| `DECISION_CONCURRENCY` | `8` | Parallel Jev requests |
| `LEAD_BATCH_SIZE` | `1000` | Leads per batch; each batch is scored, put into campaigns and fully called before the next |
| `QUEUE_MAXSIZE` | `100` | Max leads waiting between two stages |
| `RESULTS_FILE` | `output/call_results.xlsx` | Results spreadsheet |

---

## 5. Running the pipeline

### Input variables in `main.py`

Edit the **INPUT** section at the top of [main.py](main.py), then run `python main.py`.
Every run is **live**: leads that pass the threshold are called.

| Variable | Default | Meaning |
|---|---|---|
| `INPUT_FILE_PATH` | `examples\sample_leads.xlsx` | Leads file (.xlsx / .csv / .json). Relative paths are relative to `dograh_caller/`. `None` = no file. |
| `INPUT_SHEET` | `None` | Worksheet to read (`None` = first sheet) |
| `INPUT_LEADS` | `[]` | Lead dicts, e.g. from the scraper. Processed together with the file if both are set. |
| `LIMIT` | `None` | Process only the first N leads |

### From other code: `run()`

```python
from main import run      # add dograh_caller to sys.path first if calling from another folder

run(source, sheet=None, limit=None, threshold=None, output=None) -> int
```

`source` may be a **file path**, **one lead dict**, or a **list of lead dicts**.
Returns `0` on success, `1` on error, `130` if interrupted.

### Command-line overrides (optional)

```bash
python main.py [file] [--threshold 0.9] [--output path.xlsx] [--sheet NAME] [--limit N]
```

| Option | Effect |
|---|---|
| `file` | Use this file instead of `INPUT_FILE_PATH` / `INPUT_LEADS` |
| `--threshold` | Override `INTEREST_THRESHOLD` for this run (0–1) |
| `--output` | Write results to another .xlsx |
| `--sheet` | Worksheet to read (default: `INPUT_SHEET` or first sheet) |
| `--limit` | Process only the first N leads (overrides `LIMIT`) |

**Input files**

- `.xlsx` — first row = column names; empty rows skipped.
- `.csv` — first row = column names (UTF-8).
- `.json` — one object or a list of objects.
- `.xls` (old Excel format) is not supported: save it as `.xlsx`.

Any columns are allowed; one must contain the phone number (see §6).

### Terminal output

```
[23:47:01] setup    Services: Draftliner (agent 2)
[23:47:01] setup    Jev model jev-1.14.0, call threshold 85%
[23:47:02] setup    Draftliner -> agent 2 'Testing Lead qualification - outbound'
[23:47:02] verify   DROP +1 416 555 0107 (dropped_not_us (CA)) - Maple Leaf Builders
[23:47:03] decide   SKIP +16025550105 Taco Loco - not interested (draftliner 2%)
[23:47:03] decide   PASS +12155550103 Keystone MEP Engineers -> Draftliner (draftliner 94%)
[23:47:29] call     -> Calling +12155550103 (Keystone MEP Engineers) via 'Testing Lead ...' [run 2201]
[23:48:41] call     <- +12155550103 Result: user_qualified | 67s | tags: user_qualified, user_speech
Done. Leads received: 10 | dropped by verifier: 4 | not interested: 2 | called: 4 | ...
```

**Batches.** Leads are processed **1,000 at a time** (`LEAD_BATCH_SIZE`): the
batch is verified and scored, one campaign per service is created for it, and
every call finishes before the next 1,000 are taken:

```
[batch]    Batch 1: leads 1-1000 of 2500
...        (verify, score, campaigns, calls)
[campaign] 'Draftliner_2026-11-19' (campaign 21) finished - paused
[batch]    Batch 2: leads 1001-2000 of 2500
```

`Ctrl+C` stops immediately: this run's campaigns are **paused** in Dograh,
rows already written are kept, and leads in progress are lost.

---

## 6. Stage 1 — Number verifier

File: [number_verifier.py](number_verifier.py) · Class: `NumberVerifier`

**Finding the phone fields.** **Every** field whose name looks like a phone
field is examined. Matching ignores case and all symbols, so `Phone(s)`,
`Phone Number`, `phone_number`, `PHONE-NO`, `Mobile`, `Cell`, `Telephone`,
`Tel`, `Contact Number`, `Business Phone`, `Number` and any name containing
"phone" all count. Fields like `Phone Verified: Yes` or `Phone Type: Office`
simply contain no number, so they don't hide the real phone column; if the
`Phone` column is junk but `Mobile` holds a good number, `Mobile` is used.

**Reading numbers.** Numbers are found inside each value with libphonenumber's
number finder, so all of these work: `+19292659900, +19174102250`,
`512-555-0102 / 512-555-0103`, `512-555-0106 and 512-555-0107`,
`Office: (512) 555-0104 Cell: 512-555-0105`, `Tel #: 512 555 0124`,
`(512) 555-0108 ext. 23` (extension dropped), `512.555.0111`, Excel values like
`15125550125.0` or `'+15125550110`, and vanity numbers like `1-800-FLOWERS`.
Numbers without a `+country` prefix are read as US numbers.

**Checks** (Google's libphonenumber):

| Check | Status if no number of the lead passes |
|---|---|
| A phone field exists | `dropped_no_phone` |
| A number can be read | `dropped_unreadable_number` |
| Valid for the numbering plan (real area code, length, ...) | `dropped_invalid_number` |
| Region allowed — default **US only** (Canada, Puerto Rico, Guam, Caribbean... excluded; see `ALLOWED_PHONE_REGIONS`) | `dropped_not_us (<REGION>)` |
| Callable type: landline, mobile or toll-free. Premium-rate (900), shared-cost, personal (500-type), pager and other special numbers are dropped | `dropped_special_number (<TYPE>)` |
| None of the lead's good numbers was **already called in an earlier run** (status `called` / `call_timeout` in the results sheet) | `dropped_already_called` |
| None was already seen earlier **in this run** | `dropped_duplicate` |

**Already called.** At startup the results sheet is read and every number with
status `called` or `call_timeout` is blocked, so restarts and later batches
never call the same business twice. Leads that were only `not_interested`,
`call_failed` or skipped are evaluated again. Turn off with `SKIP_ALREADY_CALLED=false`.

**State and time zone.** The chosen number's US state and time zone (from the
area code) are saved as `phone_location` / `phone_timezone`, e.g. `Texas` /
`America/Chicago` — for calling-hours rules and reporting. Toll-free numbers
have no location and are left blank.

- The **first** number that passes all checks is called (normalized to E.164,
  `+1XXXXXXXXXX`).
- If several numbers fail for different reasons, the most informative reason
  is reported (not-US > special > invalid > unreadable).
- **Duplicates:** if *any* good number of a lead was seen before in this run,
  the lead is the same business again and is dropped — a business is never
  called twice, even when it is listed with a different number first.
  Detection is per process run (in memory).
- **Limitation:** "valid" means correctly formed for the US numbering plan.
  Whether a number is currently in service needs a carrier lookup (e.g.
  Twilio Lookup), which is not done.

---

## 7. Stage 2 — Decision maker (Jev)

File: [decision_maker.py](decision_maker.py) · Class: `DecisionMaker`

### What Jev sees

- **State:** the lead dict **without** phone fields and empty values, e.g.
  `{"company": "...", "category": "...", "description": "...", "location": "..."}`.
- **Questions:** one `Noul` (yes/no) question per enabled service:
  - *instructions:* "Decide whether [the lead] is realistically a potential
    customer that would be interested in `<name>`'s services…" + the service's
    `description`;
  - *criteria:* `true` = the service's `good_fit`, `false` = its `not_a_fit`.

All services are asked in a **single** Jev request per lead (~1–2 s, a few
hundred input tokens, output free).

### Routing rule

```
best = service with the highest probability
if probability(best) >= INTEREST_THRESHOLD: call via best.agent_id
else: not_interested
```

One lead is called at most once, by one agent. Every service's probability is
saved as a `prob_<id>` column.

### Why Jev

Jev returns a probability per question instead of generated text, so outputs
always match the expected type (no parsing, no invented answers) and the
probabilities can be thresholded. Its probabilities are meant to be calibrated;
validate this by labelling ~100–200 real leads by hand, running them,
comparing the `prob_*` columns with the call outcomes, and tuning
`INTEREST_THRESHOLD` against the results.

---

## 8. Stage 3 — Campaigns

Files: [campaign_manager.py](campaign_manager.py) (`CampaignJob`) ·
[dograh_client.py](dograh_client.py) (`DograhClient`)

### One campaign per service per run

When all leads of the run have been scored, for **each service with at least one
interested lead**:

1. A contacts CSV is built with that service's numbers only.
2. A campaign named **`<service name>_<YYYY-MM-DD>`** — the date the run
   **started**, so a run that passes midnight keeps its name (e.g.
   `Draftliner_2026-11-19`, `Paradise Estimating_2026-11-19`) — is created with **that service's agent**
   (`agent_id` in services.yaml) and the retry settings from config.
3. The campaign is started. All campaigns of the run run in parallel; they share
   the Dograh organization's concurrent-call limit.

Services with no interested leads get no campaign (Dograh can't create a campaign
without contacts). Running twice on the same day creates a second campaign with
the same name (Dograh shows them with different IDs).

**Why campaigns are created after scoring, not at the start:** a Dograh
campaign's contact list is fixed when the campaign is created — the API has no
way to add numbers to an existing campaign. For a scraper that runs for a long
time, call `pipeline.flush()` periodically: each flush creates a new set of
campaigns for the leads collected since the previous flush.

### Dograh endpoints used

All requests send header `X-API-Key: <DOGRAH_API_KEY>`.

| When | Request | Purpose |
|---|---|---|
| Startup | `GET /workflow/fetch/{agent_id}` | Check each service's agent exists; warn about unpublished changes |
| Per campaign | `GET /workflow/{agent_id}/versions` | Find the `{{variables}}` the published agent needs |
| Per campaign | `POST /s3/presigned-upload-url` + `PUT` | Upload the contacts CSV |
| Per campaign | `POST /campaign/create` | Create `<service>_<date>` with the agent, CSV and retry config |
| Per campaign | `POST /campaign/{id}/start` | Start dialing |
| Every `CAMPAIGN_POLL_SECONDS` | `GET /campaign/{id}` and `GET /campaign/{id}/runs` | State, retry config, and one run per call attempt |
| Campaign done, or program exits | `POST /campaign/{id}/pause` | Make sure the campaign isn't left running |

### Following the calls

- Every call attempt is a Dograh **run**. Runs are matched to leads by the
  `lead_ref` column in the CSV (falls back to the phone number).
- `-> Calling …` is printed when a run appears; `<- … Result: …` when the lead
  is final.
- A lead is **final** when its latest attempt is completed, its outcome is known
  (or `RESULT_WAIT_SECONDS` passed), and **no retry is expected** — i.e. the
  outcome isn't a retryable one (busy / no-answer by default) with retries left
  and the retry window (`retry_delay` + `RETRY_GRACE_SECONDS`) still open.
- The result row records the **last** attempt (`dograh_run_id`) and the number
  of attempts (`call_attempts`).
- If the campaign reaches `completed`/`failed`, leads never dialed become
  `call_failed`. If the campaign makes no progress for
  `CAMPAIGN_IDLE_TIMEOUT_SECONDS`, unfinished leads become `call_timeout`.
- Before creating a campaign, the agent's **published** prompts are scanned for
  `{{variables}}` (same rules as Dograh). Dograh refuses a whole campaign if any
  row lacks one of them, so leads without such a value are left out and recorded
  as `skipped_missing_data`; the rest of the campaign runs normally.
- If a campaign can't be created for another reason, its leads become
  `call_failed` with Dograh's message.

Campaign progress counters on the Dograh dashboard can lag; this project
tracks the actual runs instead.

### Campaigns are never left running

Dograh doesn't always mark a finished campaign `completed` by itself (it can
stay `running` at e.g. 9/9), and it has no "complete" or "cancel" API. So:

- as soon as every lead of a campaign has its final result, the campaign is
  **paused** (`'Draftliner_2026-11-19' (campaign 21) finished - paused`);
- when the program exits for any reason — normal end, `Ctrl+C`, or an error —
  every campaign this run started that is still `running`/`syncing` is paused.
  Calls already in progress finish; no new calls are placed.

Finished campaigns therefore show as **paused** on the dashboard. If the
process is killed hard (e.g. the window is closed or the machine powers off),
the exit step can't run — pause any leftover campaign in Dograh.

### Variables sent to the agent (CSV columns)

Every lead field, with its name lower-cased and non-alphanumeric characters
replaced by `_` (`"Company Name"` → `company_name`), plus:

| Variable | Value |
|---|---|
| `phone_number` | E.164 number being called |
| `interested_service` | Service name Jev chose, e.g. `Draftliner` |
| `current_datetime` | Local time, e.g. `Wednesday, November 18, 2026, 11:47 PM` |
| `lead_ref` | Internal ID used to match calls to leads |

Use them in the agent's prompts as `{{company}}`, `{{location}}`,
`{{interested_service}}`, etc.

### Call outcome fields

`disposition` (e.g. `user_qualified`, `user_hangup`, `user_idle_max_duration_exceeded`,
`end_call_tool`, `no-answer`, `busy`, `failed`), `duration_seconds`, `tags`,
`transcript_url`, `recording_url`, and the full run (`raw`, minus logs).

---

## 9. Stage 4 — Results spreadsheet

File: [results_store.py](results_store.py) · Class: `ResultsStore`

- Sheet `results` in `output/call_results.xlsx` (or `--output`). Created on first
  use and reused on later runs. Header row is frozen and styled.
- One row per submitted lead, whatever happened to it.
- **Grouped by day, newest day on top:**

  ```
  row 1   header
  row 2   2026-11-19            <- date row (grey, only the date)
  row 3   today 08:00 result
  row 4   today 12:00 result    <- later results of the same day go BELOW
  row 5   2026-11-18            <- earlier days move down when a new day starts
  row 6   yesterday 09:00 result
  ```

  The first result of a new day inserts a new date row directly under the
  header — also in the middle of a long run: results after midnight start the
  new day's block on top, while the run keeps using the same campaigns.
  The day comes from `processed_at` (local time). Files written in an older
  layout are regrouped into this layout when opened.
- New lead fields or new services add new columns automatically.
- Saving is batched: the file is written whenever the queue of pending rows is empty.
- If the file is open in Excel (locked), the pipeline warns and keeps retrying;
  at shutdown it retries for 60 s and then saves a timestamped copy
  (`call_results_YYYYMMDD_HHMMSS.xlsx`).

### Columns

| Column | Description |
|---|---|
| `processed_at` | When the row was written |
| `status` | See table below |
| `detail` | Error text or extra info |
| `phone_raw` | Phone as received |
| `phone_e164` | Normalized phone |
| `phone_location`, `phone_timezone` | US state and time zone of the number's area code |
| `interested_service` | Service chosen by Jev (empty if none passed) |
| `interest_probability` | Highest probability across services |
| `agent_id`, `agent_name` | Dograh agent used for the call |
| `campaign_id`, `campaign_name` | Dograh campaign the number was dialed in (e.g. `Draftliner_2026-11-19`) |
| `dograh_run_id` | Run ID of the last call attempt (look it up under *Agent Runs*) |
| `call_attempts` | Number of call attempts including Dograh retries |
| `call_disposition`, `call_duration_s`, `call_tags` | Call outcome |
| `transcript_url`, `recording_url` | Call artifacts |
| `dograh_response` | Full Dograh run as JSON (cut at Excel's 32k-character cell limit) |
| `prob_<service_id>` | Jev probability for each service |
| *lead fields* | Original fields; renamed `lead_<name>` if they clash with a column above |

### Status values

| Status | Meaning |
|---|---|
| `dropped_no_phone` / `dropped_unreadable_number` / `dropped_invalid_number` / `dropped_not_us (XX)` / `dropped_special_number (TYPE)` / `dropped_already_called` / `dropped_duplicate` | Rejected by the verifier (§6) |
| `not_interested` | No service reached the threshold |
| `called` | Call completed; see disposition |
| `skipped_missing_data` | Left out of its campaign because it has no value for a `{{variable}}` the agent's prompt uses (Dograh would otherwise refuse the whole campaign) |
| `call_failed` | Campaign couldn't be created/started, the number was never dialed, or Dograh couldn't be reached |
| `call_timeout` | Campaign made no progress for `CAMPAIGN_IDLE_TIMEOUT_SECONDS` |
| `decision_error` | Jev request failed (network, key, rate limit after retries) |
| `error` | Unexpected error in the verifier |

---

## 10. Concurrency model

The pipeline is built on `asyncio` (see [pipeline.py](pipeline.py)):

| Stage | Workers | Input |
|---|---|---|
| Verifier | 1 | `verify` queue (max `QUEUE_MAXSIZE`) |
| Decision maker | `DECISION_CONCURRENCY` (8) | `decision` queue (max `QUEUE_MAXSIZE`) |
| Campaigns | one monitor task per campaign, all in parallel | per-service lists, launched by `flush()` / `close()` |
| Results writer | 1 | `results` queue (unbounded) |

- **Concurrent scoring:** leads are verified and scored by Jev while more are
  still being submitted.
- **Back-pressure:** bounded queues make `submit()` wait when scoring falls
  behind, so memory use stays bounded.
- **Calls** are placed by Dograh's campaign dispatcher. Campaigns of the same run
  share the organization's concurrent-call limit (currently 1, roughly one call
  per minute).
- **Blocking HTTP** (Dograh via `requests`) runs in worker threads
  (`asyncio.to_thread`); Jev uses its native async client.
- **Shutdown:** `close()` runs `flush()` (score everything, run the campaigns,
  wait for all calls), then stops the workers, pauses any campaign of this run
  still running, saves the sheet and prints the summary. On `Ctrl+C` or an error
  it skips the flush but still pauses the campaigns and saves.
- **Batches:** `run_leads()` calls `flush()` after every `LEAD_BATCH_SIZE`
  leads, so each batch is completely called before the next is taken.
- **No persistence:** queues are in memory only. Stopping the process loses
  leads that were not yet written to the sheet.

---

## 11. Python API reference

### `LeadPipeline` ([pipeline.py](pipeline.py))

```python
LeadPipeline(
    results_file: str | Path = None,   # default RESULTS_FILE
    threshold: float = None,           # default INTEREST_THRESHOLD
    services_file: str | Path = None,  # default services.yaml
)
await pipeline.start()                 # load services, resolve agents, start workers
await pipeline.submit(lead: dict)      # add one lead (waits if queues are full)
await pipeline.flush()                 # score what's submitted, run one campaign per service, wait for calls
await pipeline.close()                 # flush, save, print summary
pipeline.stats                         # Counter of statuses
```

Use it as an async context manager (`start` / `close` are called for you):

```python
async with LeadPipeline() as pipeline:
    async for lead in scraper():
        await pipeline.submit(lead)
```

### Helpers

```python
await run_leads(leads, **pipeline_kwargs)   # leads: list / generator / async generator of dicts
run_leads_sync(leads, **pipeline_kwargs)    # blocking version for normal scripts
```

### Scraper integration

The scraper only needs to produce dicts with a phone field and descriptive
fields (company, category, description, location, …) and call
`pipeline.submit(lead)`. Example: [examples/from_python.py](examples/from_python.py).

Campaigns are created when the input ends (`close()`), because Dograh campaigns
can't take new numbers after creation. If the scraper runs continuously, call
`await pipeline.flush()` at a sensible interval (e.g. every 100 leads or every
hour) to dial what has been collected so far.

### Using stages on their own

```python
from number_verifier import NumberVerifier
NumberVerifier().verify({"phone": "(512) 555-0101"})   # Verification(ok=True, e164='+15125550101', ...)

from decision_maker import DecisionMaker, load_services
dm = DecisionMaker(load_services(path), 0.85, api_key, "jev-1.14.0")
decision = await dm.decide(lead)                       # decision.probabilities, decision.service

from dograh_client import DograhClient
from campaign_manager import CampaignJob, CampaignLead
client = DograhClient()
job = CampaignJob(service, client.get_agent(service.agent_id),
                  [CampaignLead(ref="L1", phone="+15125550101", lead=lead)])
job.launch(client)                                     # creates + starts "<service>_<date>"
await job.monitor(client, on_call_started, on_result)  # callbacks per call / final outcome
```

---

## 12. Extending the system

| Task | How |
|---|---|
| Add a service | Add a block to [services.yaml](services.yaml) with its own `agent_id`; no code change. See [SERVICES.md](SERVICES.md). |
| Change which agent calls a service | Change its `agent_id` in services.yaml. |
| Pause a service | `enabled: false`. |
| Be stricter / looser | `INTEREST_THRESHOLD` in `.env` or `--threshold`. |
| Allow more simultaneous calls | Raise the Dograh organization's concurrent-call limit. |
| Change retries | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_*` in `.env`. |
| Change campaign naming | `CAMPAIGN_NAME_DATE_FORMAT`, or `campaign_name()` in [campaign_manager.py](campaign_manager.py). |
| Accept other countries | Change the region check in `NumberVerifier.verify()`. |
| Add another input format | Add a branch to `read_leads()` in [main.py](main.py). |
| Store results elsewhere | Replace `ResultsStore` (it only needs `append(row)` and `save()`). |

---

## 13. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Jev API key not found` | Add `Jev_Key=...` (or `TYPESAFE_API_KEY`) to `.env`. |
| `Dograh API key not found` | Add `DOGRAH_API_KEY=...` to `.env`. |
| `Dograh rejected the API key (HTTP 401)` | Key was created in a different Dograh deployment than `DOGRAH_BASE_URL`, or it was archived. |
| `Dograh agent N does not exist` | Wrong `agent_id` in services.yaml (see the ID column on Dograh's *Voice Agents* page). |
| Startup warning `has unpublished changes` | The agent has edits saved as a draft. Calls use the last **published** version — publish the agent in Dograh. |
| Every lead is `not_interested` | Service descriptions too narrow, threshold too high, or leads lack descriptive fields. Check the `prob_*` columns in the results. |
| Many `decision_error` rows | Jev rate limit or network issue; lower `DECISION_CONCURRENCY`. |
| `FAILED to start '<service>_<date>'` | Dograh refused the campaign; the message says why (telephony not configured, quota, invalid CSV, ...). |
| Leads marked `skipped_missing_data` | The agent's prompt uses a `{{variable}}` those leads have no value for. Add the field to the leads, give the variable a default in the prompt (`{{company | there}}`), or remove it. |
| Calls stay on "Calling…" for a long time | Normal while the conversation lasts. If calls never finish, Dograh is not receiving the telephony provider's status callbacks — check the deployment's public backend URL. Unfinished leads end as `call_timeout` after `CAMPAIGN_IDLE_TIMEOUT_SECONDS` without progress. |
| Long pauses between calls | One concurrent-call slot shared by all campaigns, plus retry delays. |
| A lead waits ~4 min after "no-answer" | It is waiting for Dograh's retry (`CAMPAIGN_RETRY_DELAY_SECONDS` + `RETRY_GRACE_SECONDS`). |
| `results ... is open in another program` | Close the spreadsheet in Excel. |
| Agent says something unexpected | Check which version is published in Dograh and what the agent's prompt does with the variables in §8. |

---

## 14. Limitations and known behaviour

- **In-memory only.** No recovery after a crash; duplicates are only detected
  within one run (a lead in an earlier run's sheet can be called again).
- **Campaigns are created after scoring.** Dograh can't add numbers to an
  existing campaign, so campaigns are created when the input ends (or on
  `flush()`), not at the start of the run. Services with no interested leads
  get no campaign.
- **One campaign per service per run/flush.** Two runs on the same day create
  two campaigns with the same name.
- **No voicemail detection** in this project; it must be handled by the Dograh
  agent or telephony provider.
- **US only.** By design, all non-US numbers are dropped.
- **Jev is in early access.** Availability, limits and pricing may change;
  the model version is pinned in `JEV_MODEL`.

---

## 15. Compliance

AI-generated-voice calls to US numbers are governed by the TCPA: calls to mobile
numbers generally require prior express consent (written consent for
telemarketing), numbers must be checked against Do-Not-Call lists, and calls
must respect permitted hours in the recipient's time zone. Leads gathered by
scraping usually come with no consent. Obtain legal guidance before calling
scraped leads at scale.
