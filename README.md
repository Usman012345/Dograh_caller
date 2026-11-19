# dograh_caller

Qualifies scraped leads with **Jev** and calls the interested ones with the
matching **Dograh** voice agent. Results go to a spreadsheet.

Full documentation: [DOCUMENTATION.md](DOCUMENTATION.md)

```
lead dict ─► Number verifier ─► Decision maker (Jev) ─► one Dograh campaign per service ─► output/call_results.xlsx
              US numbers only     P(interested) ≥ 85%     "Draftliner_2026-11-19" (agent 2)
              drops the rest      per service             "Paradise Estimating_2026-11-19" (agent 3)
```

Leads are processed in **batches of 1,000** (`LEAD_BATCH_SIZE`). Each batch is
verified and scored (concurrently), then **one campaign per service** that has
interested leads is created automatically, named `<service name>_<date>`, run
by that service's agent and containing only that service's numbers. Every call
of the batch finishes before the next 1,000 are taken. Calls and results are
printed and written to the sheet. Campaigns are created per batch because Dograh
can't add numbers to a campaign once it exists.

Campaigns are **never left running**: each one is paused as soon as all its
calls are done, and on exit (normal end, `Ctrl+C` or an error) any campaign of
the run still running is paused. If the process stops, leads still in flight
are lost (rows already written are kept).

## Setup

```bash
pip install -r requirements.txt
```

All secrets live in `.env` (in this folder or the parent `Task10` folder) and
nowhere else. Copy [.env.example](.env.example) to `.env` and fill it in:

```ini
Jev_Key=...                 # TypeSafe/Jev API key (TYPESAFE_API_KEY also works)
DOGRAH_API_KEY=...          # Dograh API key (Settings -> API Keys)
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

Optional settings (same `.env`): `INTEREST_THRESHOLD` (0.85), `JEV_MODEL`
(jev-1.14.0), `DECISION_CONCURRENCY` (8), `QUEUE_MAXSIZE` (100), `RESULTS_FILE`,
`CAMPAIGN_MAX_RETRIES` (1), `CAMPAIGN_RETRY_DELAY_SECONDS` (120),
`CAMPAIGN_IDLE_TIMEOUT_SECONDS` (900). See [config.py](config.py).

## Run

**1. Set the input at the top of [main.py](main.py):**

```python
INPUT_FILE_PATH = r"C:\Users\adil.zia\Desktop\Tasks\Task10\leads.xlsx"   # or None
INPUT_SHEET     = None        # worksheet name, None = first sheet
INPUT_LEADS     = []          # lead dicts (e.g. from the scraper); both inputs can be used together
LIMIT           = None        # e.g. 5 for a first test
```

**2. Run it** - every run is **live**: qualified leads are called.

```bash
python main.py
```

The input file can be `.xlsx` (first row = column names), `.csv` or `.json`.
Any columns are allowed; one must be the phone number.

**From other code (the AI-powered scraper)** - pass a dict, a list of dicts,
or a file path to `run()`:

```python
import sys; sys.path.insert(0, r"C:\Users\adil.zia\Desktop\Tasks\Task10\dograh_caller")
from main import run

run({"company": "...", "category": "...", "description": "...", "location": "...",
     "phone": "+1 512 555 0101"})                 # one lead
run(list_of_lead_dicts)                           # many leads
run(r"C:\path\to\leads.xlsx")                     # a file
```

For a continuous stream, `pipeline.LeadPipeline` accepts leads one at a time
(`await pipeline.submit(lead)`) - see [examples/from_python.py](examples/from_python.py).
Campaigns are created when the stream ends; for a scraper that never stops,
call `await pipeline.flush()` periodically to create campaigns for what has
been collected so far.

**Command-line overrides** (optional): `python main.py other.xlsx`,
`--threshold 0.9`, `--output other.xlsx`, `--sheet Sheet2`, `--limit 5`.

## Lead format

Any dict. Recognized phone keys: `phone_number`, `phone`, `mobile`, `number`,
`telephone`, `tel`, `cell`, `contact_number`, ... (case/spaces ignored).
All other fields (company, category, description, location, ...) are shown to
Jev, copied to the results sheet, and sent to the Dograh agent as template
variables, e.g. `{{company}}`, `{{location}}`, plus `{{interested_service}}`,
`{{phone_number}}` and `{{current_datetime}}`.

## Stages

| Stage | File | What it does |
|---|---|---|
| Number verifier | [number_verifier.py](number_verifier.py) | Reads every phone-like column, finds all numbers in each (comma / slash / "and" / labelled lists), validates them with Google's libphonenumber and calls the first **valid US landline, mobile or toll-free** number. Drops Canada / territories / foreign, premium-rate (900) and other special numbers, invalid numbers, numbers already called in earlier runs, and duplicate businesses. Normalizes to `+1XXXXXXXXXX` and records the number's state and time zone. |
| Decision maker | [decision_maker.py](decision_maker.py) | One Jev request per lead with a yes/no question per enabled service from [services.yaml](services.yaml). Picks the most likely service if ≥ threshold. |
| Campaigns | [campaign_manager.py](campaign_manager.py), [dograh_client.py](dograh_client.py) | Per service with interested leads: uploads a contacts CSV, creates campaign `<service>_<date>` with the service's agent, starts it, and follows every call (incl. Dograh retries) until each lead has a final outcome. |
| Results | [results_store.py](results_store.py) | Appends one row per lead to the .xlsx. |
| Wiring | [pipeline.py](pipeline.py) | Queues, workers, terminal output, summary. |

## Results sheet (`output/call_results.xlsx`)

One row per lead, grouped by day with the **newest day on top**. Each day starts
with a date row (only the date); that day's results follow underneath in time
order, so later results of the same day go below earlier ones. When a new day
starts, its date row is inserted at the top and earlier days move down.

| Column | Meaning |
|---|---|
| `status` | `called`, `call_failed`, `call_timeout`, `not_interested`, `skipped_missing_data`, `decision_error`, or `dropped_*` (`dropped_not_us`, `dropped_special_number`, `dropped_already_called`, `dropped_invalid_number`, `dropped_duplicate`, `dropped_no_phone`, `dropped_unreadable_number`) |
| `interested_service`, `interest_probability` | Service chosen by Jev and its probability |
| `prob_<service>` | Jev's probability for every service |
| `agent_id`, `agent_name` | Dograh agent that placed the call |
| `campaign_id`, `campaign_name` | Campaign the number was dialed in, e.g. `Draftliner_2026-11-19` |
| `dograh_run_id`, `call_attempts` | Last call attempt and number of attempts (incl. retries) |
| `call_disposition`, `call_duration_s`, `call_tags` | Call outcome from Dograh (last attempt) |
| `transcript_url`, `recording_url` | Links to the call artifacts |
| `dograh_response` | The full run returned by Dograh (JSON) |
| *everything else* | The original lead fields |

Close the sheet in Excel while the pipeline runs - Excel locks the file. If it
is locked, the pipeline keeps retrying and, at the end, saves to a timestamped
copy instead.

## Adding services

Edit [services.yaml](services.yaml) - no code changes. Guide: [SERVICES.md](SERVICES.md).

## Important notes

- **Publish agent changes in Dograh.** Calls always use an agent's *published*
  version; the pipeline warns at startup if an agent has unpublished edits.
- **Calling rules.** AI-voice calls to US numbers fall under the TCPA (consent
  requirements, Do-Not-Call lists, calling hours). Check compliance before
  calling scraped leads at scale.
