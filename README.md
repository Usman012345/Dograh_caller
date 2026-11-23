# dograh_caller

Give it a list of businesses. It finds the ones likely to buy **Draftliner** or
**Paradise Estimating** and has the right AI voice agent call them.

## What it does

1. **Reads your list** of businesses (a spreadsheet, or dicts sent by the scraper).
2. **Checks each phone number.** It keeps only real US numbers, and skips
   duplicates and businesses that were already called.
3. **Asks Gemini (`gemini-3.8-flash`)** to score each enabled service (0–100%)
   and write a short explanation.
4. **Calls the qualified businesses (≥ 80%).** For each service it creates a Dograh
   campaign, e.g. `Draftliner_2026-11-19`, and that service's agent makes the calls.
5. **Saves everything** to `output/call_results.xlsx`, one row per business:
   who was called, which service, and how the call went, plus per-lead
   tracking in `output/pipeline_tracker.xlsx` for the dashboard.

```
your list ─► check numbers ─► Gemini: score services ─► Dograh campaign + agent calls ─► results & tracker
```

| Service | Dograh agent that calls |
|---|---|
| Draftliner | 2 — *Testing Lead qualification - outbound* (Danial) |
| Paradise Estimating | 3 — *Paradise Agent - outbound* (Olivia) |

---

## 1. One-time setup

**a. Install the requirements** (Python 3.10 or newer). Open a terminal in this folder and run:

```bash
pip install -r requirements.txt
```

**b. Add your keys.** Copy `.env.example` to `.env` and fill in:

```ini
GEMINI_API_KEY=your-gemini-key
DOGRAH_API_KEY=your-dograh-key
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

A `.env` file in the parent folder is also checked if present.
Keys belong only in `.env`, never in code.

**c. Publish your agents in Dograh.** Calls always use an agent's *published*
version, so publish after every change you make to an agent.

**d. Check everything** (takes no leads, calls nobody):

```bash
python pull_leads.py --check
```

It confirms the Gemini key, `services.yaml`, each Dograh agent, that the lead
streamer answers at `LEAD_SOURCE_URL`, and that it accepts the token.

### Running it on a server (Linux)

Copy this folder to the server **with its `.env`**, then:

```bash
bash install.sh               # .venv + requirements + the --check above; no root needed
tmux new -s caller            # keeps it running if your SSH connection drops
.venv/bin/python main.py      # runs INPUT_FILE_PATH, then pulls from the streamer automatically
```

Detach with **Ctrl+B, D**; come back with `tmux attach -t caller`. If the
process is killed or the SSH session ends anyway, it stops the same way as
Ctrl+C: this run's Dograh campaigns are paused and the unfinished batch is
kept and continues on the next start.

On Windows: `python -m venv .venv`, `.venv\Scripts\pip install -r requirements.txt`,
then `.venv\Scripts\python main.py`. (`bash install.sh` also works in Git Bash.)

Tests: `pip install -r requirements-dev.txt`, then `python -m pytest`.

---

## 2. Calling a list of businesses

**Step 1 — Prepare the spreadsheet** (`.xlsx`, `.csv` or `.json`).
The first row holds the column names. You need:
- **a phone column**, for example `Phone`, `Phone(s)`, `Phone Number` or `Mobile`
  (several numbers in one cell are fine; **fax numbers are never called**);
- **columns describing the business**, for example company, industry,
  description and city. The more Gemini knows about a business, the better it decides.

Data from the scraper can be unstructured (any field names, nested data, lists,
JSON text): phone numbers are found wherever they are, and Gemini only gets the
fields that describe the business. Details: [DOCUMENTATION.md](DOCUMENTATION.md) §6–7.

| Company | Industry | Notes | City / State | Phone(s) |
|---|---|---|---|---|
| Smith Framing LLC | Framing subcontractor | Bids 10+ jobs a month | Austin, TX | (512) 555-0101 |

**Step 2 — Point the program at your file.** Open [main.py](main.py) and edit the top:

```python
INPUT_FILE_PATH = r"C:\path\to\your\leads.xlsx"   # None = no file, streamer only
LIMIT = None                                     # or 5 for only the first 5 businesses
USE_STREAMER = True                              # keep pulling from the streamer after the file
STREAMER_CHECK_SECONDS = 60
```

**Step 3 — Run it:**

```bash
python main.py                  # live: calls the file's qualified leads, then serves the streamer
python main.py leads.xlsx --score-only   # test scoring only; never calls Dograh
```

After the file finishes, the program keeps running and checks the streamer every
60 seconds. While the reverse proxy is not attached it only waits. Once the real
streamer answers, it takes a batch, calls it completely, and takes the next one.
Press **Ctrl+C** to stop.

> ⚠️ **Normal runs are live.** Businesses that pass the 80% threshold really get called.
> Pass `--score-only` to test verification and Gemini scoring safely without contacting Dograh or touching `output/call_results.xlsx`.

Leads are processed **300 at a time** (`LEAD_BATCH_SIZE`): the program checks and scores a batch,
calls everyone in it, then moves on to the next 300.

---

## 3. What you see while it runs

Example output (the real lines also start with the time):

```
[setup]    Services: Draftliner (agent 2), Paradise Estimating (agent 3)
[setup]    Gemini model gemini-3.8-flash (thinking MEDIUM), threshold 80%
[verify]   DROP +1 416 555 0107 (dropped_not_us (CA)) - Maple Leaf Builders   ← number not usable
[decide]   NON-QUALIFIED +16025550105 Taco Loco (draftliner 3%, paradise 5%)
[decide]   QUALIFIED +18285550102 Blue Ridge Architects -> Draftliner (draftliner 94%, paradise 8%)
[campaign] Started 'Draftliner_2026-11-19' (campaign 21) with 12 number(s)
[call]     -> Calling +18285550102 (Blue Ridge Architects) [Draftliner_2026-11-19, run 2210]
[call]     <- +18285550102 Result: user_qualified | 67s | tags: user_qualified
[campaign] 'Draftliner_2026-11-19' (campaign 21) finished - paused
Done. Leads received: 10 | dropped by verifier: 1 | not interested: 2 | called: 7 | ...
```

- **DROP**: the phone number can't be used (not US, invalid, a duplicate, or already called).
- **NON-QUALIFIED**: Gemini scored every service below the 80% threshold.
- **QUALIFIED**: scored ≥ 80%; the business is added to the highest-scoring service's campaign.
- **Calling / Result**: a call starts, then its outcome from Dograh.
- **finished - paused**: all calls in the campaign are done. Campaigns are
  never left running.

Calls take about a minute each and happen one at a time, so a big list takes hours.

---

## 4. Stopping it

Press **Ctrl+C**. The program pauses this run's campaigns in Dograh, so no new
calls are made, and saves the results so far.

Don't just close the window: then the campaigns stay running in Dograh and you
have to pause them there yourself.

---

## 5. Reading the results

Open **`output/call_results.xlsx`**. Close it in Excel while the program runs,
because Excel locks the file.

**Transferred calls** also go to a separate file, **`output/transferred_calls.xlsx`**:
one row per business whose call the agent transferred, grouped by day. It starts with
their details (business, contact name and title, phone called, email, website, address,
city, state, category), then the call (service, campaign, agent, duration, what the agent
collected on the call, transcript and recording links), then any other information the
lead had. The file appears with the first transfer. In `call_results.xlsx` those rows have
`call_transferred` = TRUE.

- One row per business, grouped by day, **newest day on top**.
- The most useful columns:

| Column | What it tells you |
|---|---|
| `status` | What happened (see below) |
| `interested_service` | Draftliner or Paradise Estimating |
| `prob_draftliner`, `prob_paradise` | How likely Gemini thinks each service is (0–1) |
| `decision_verdict`, `decision_summary` | Qualification verdict and two-line Gemini explanation |
| `campaign_name` | The Dograh campaign it was called in |
| `call_disposition` | How the call ended, e.g. `user_qualified`, `user_hangup`, `no-answer` |
| `transcript_url`, `recording_url` | The conversation |

| `status` | Meaning |
|---|---|
| `called` | The call happened; see `call_disposition` |
| `not_interested` | Gemini scored it below 80% for every service; not called |
| `scored_qualified` | Scored ≥ 80% in a `--score-only` run; not called |
| `decision_error` | Gemini request failed after retries; saved for retry |
| `dropped_...` | Phone problem: `not_us`, `invalid_number`, `special_number` (e.g. 900), `fax_only` (only fax numbers — fax numbers are never called), `duplicate`, `already_called`, `no_phone` |
| `call_failed` / `call_timeout` | Dograh couldn't place the call, or it never finished |
| `skipped_missing_data` | The agent's script needs information this business didn't have |

> **Keep this file.** It is also the program's memory of who was already called.
> If you delete it, those businesses can be called again.

---

## 6. Getting leads from the scraper's lead streamer

Setting it up on the servers for the first time: [GO_LIVE.md](GO_LIVE.md).

The [lead streamer](lead_streamer/) runs on the scraper side and hands out leads
at **`https://ai-sdr.grobots.ai`**, 300 per ping. Put its token in `.env`:

```ini
LEAD_SOURCE_TOKEN=the-streamer's-STREAM_TOKEN
# LEAD_SOURCE_URL=https://ai-sdr.grobots.ai   (default)
```

Then run:

```bash
python main.py
```

1. It confirms the real streamer answers `/health`. Until then it just waits.
2. It pings the streamer and receives one batch of up to **300** leads.
3. It saves the batch to `output/pulled_batches/`, then processes **all** of it
   (check numbers → Gemini → campaigns → every call finished → results sheet).
4. It immediately asks for the next batch. With no new leads, it checks again
   every `STREAMER_CHECK_SECONDS`.

If it is stopped mid-batch (Ctrl+C, crash, error), the batch stays in
`output/pulled_batches/*.pending.json` and is finished first on the next start,
before any new leads are taken. That matters because the streamer marks leads as
sent the moment it hands them out. Numbers that were already called are skipped.

To approve every batch by hand instead, run `python pull_leads.py`: it waits for
**Enter** before each ping.

## 7. Sending leads from other code

```python
import sys; sys.path.insert(0, r"C:\path\to\dograh_caller")
from main import run

run({"company": "Summit Concrete Co", "category": "Concrete subcontractor",
     "location": "Denver, CO", "phone": "(303) 555-0142"})   # one business
run(list_of_business_dicts)                                    # many (processed 300 at a time)
```

See [examples/from_python.py](examples/from_python.py) for streaming leads one by one.

---

## 8. Client Dashboard

The web dashboard shows the live pipeline flow (`Incoming Leads → Number Verifier → Decision Maker → Qualified / Non-Qualified → Calling Agent → Call Results`, plus `Transferred Calls`) and lets you start or stop streamer batch runs from the top bar. Clients can see the conversion funnel and anything needing attention, open any stage, and read a lead's full story: AI reasoning, why it was or was not called, the recording and the conversation.

```bash
python dashboard_server.py
```

Then open <http://127.0.0.1:8090>.

- **What it reads:** `output/pipeline_tracker.xlsx`. Leads appear once a run has processed them, and the server does not need Node at runtime (it serves the pre-built `dashboard/static/` bundle).
- **Start / Stop Batch:** click **Start Batch** in the top bar to pull 1–100 batches of 1–1,000 leads from the streamer and run the pipeline in the background (or click **Stop** to pause campaigns and keep unfinished leads pending for resume). In `--demo` mode, **Start Batch** runs an offline simulation without touching the streamer or Dograh.
- **Recordings and transcripts:** the server fetches them from Dograh with the key in `.env`. The key and the private file links never reach the browser.
- **Security:** the dashboard has **no login**. By default it binds to `127.0.0.1:8090` (this computer only) and refuses requests for other host names. With a non-loopback `--host` it prints a warning, because anyone who can reach the address can see lead details, phone numbers, transcripts, recordings, and batch controls.

**Client demo:** show the dashboard with ~60 fictional leads, conversations and sample audio. It uses no real data and contacts nothing:

```bash
python dashboard_server.py --demo
```

**Changing the interface:** edit `dashboard/web/src`, then run `npm install` and `npm run build` in `dashboard/web`. The built files in `dashboard/static` are what the server uses.

---

## 9. Changing how it behaves

| I want to… | Do this |
|---|---|
| Add or change a service (who it targets, which agent calls) | Edit [services.yaml](services.yaml); no code. See [SERVICES.md](SERVICES.md) |
| Test scoring without placing any calls | `python main.py path\to\leads.xlsx --score-only` |
| Call fewer, surer leads (or more) | Set `INTEREST_THRESHOLD=0.9` (stricter) or `0.75` in `.env` |
| Change the batch size | Set `LEAD_BATCH_SIZE=500` in `.env` (and `BATCH_SIZE` in the streamer's `.env` to match) |
| Change retries for busy / no-answer calls | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_DELAY_SECONDS` in `.env` |
| Run a different file once without editing main.py | `python main.py other.xlsx --limit 5` |

All settings: [DOCUMENTATION.md](DOCUMENTATION.md), section 4.

---

## 10. Common problems

| Problem | Fix |
|---|---|
| `Gemini API key not found` / `Dograh API key not found` | Add the key to `.env` (`GEMINI_API_KEY=...` or `GEMINI_API=...`) |
| `Dograh rejected the API key (HTTP 401)` | The key comes from a different Dograh, or was archived. Create one under Settings → API Keys |
| Every row says `dropped_no_phone` | No column name looks like a phone column. Rename it to `Phone` |
| Almost everyone is `not_interested` | Your list lacks business details, or the services don't fit. Add industry/description columns, or check [services.yaml](services.yaml) |
| Warning `has unpublished changes` | Publish the agent in Dograh, or calls use the older version |
| `FAILED to start '…'` | Dograh refused the campaign; the message says why |
| `… is open in another program` | Close the results spreadsheet in Excel |

---

## Good to know

- **Calling laws.** AI-voice calls to US numbers are regulated (consent and
  calling hours). Make sure your calling is compliant.
- **How it works inside**, every setting, column and status:
  [DOCUMENTATION.md](DOCUMENTATION.md).
