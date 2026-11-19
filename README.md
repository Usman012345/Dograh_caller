# dograh_caller

Give it a list of businesses. It finds the ones likely to buy **Draftliner** or
**Paradise Estimating** and has the right AI voice agent call them.

## What it does

1. **Reads your list** of businesses (a spreadsheet, or dicts sent by the scraper).
2. **Checks each phone number.** It keeps only real US numbers, and skips
   duplicates and businesses that were already called.
3. **Asks Jev (an AI model)** whether the business would buy Draftliner,
   Paradise Estimating, or neither.
4. **Calls the interested businesses.** For each service it creates a Dograh
   campaign, e.g. `Draftliner_2026-11-19`, and that service's agent makes the calls.
5. **Saves everything** to `output/call_results.xlsx`, one row per business:
   who was called, which service, and how the call went.

```
your list ─► check numbers ─► Jev: which service? ─► Dograh campaign + agent calls ─► results spreadsheet
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
Jev_Key=your-jev-key
DOGRAH_API_KEY=your-dograh-key
DOGRAH_BASE_URL=https://dograh.grobots.ai/api/v1
```

The `.env` in the parent `Task10` folder works too, and it is already set up.
Keys belong only in `.env`, never in code.

**c. Publish your agents in Dograh.** Calls always use an agent's *published*
version, so publish after every change you make to an agent.

---

## 2. Calling a list of businesses

**Step 1 — Prepare the spreadsheet** (`.xlsx`, `.csv` or `.json`).
The first row holds the column names. You need:
- **a phone column**, for example `Phone`, `Phone(s)`, `Phone Number` or `Mobile`
  (several numbers in one cell are fine);
- **columns describing the business**, for example company, industry,
  description and city. The more Jev knows about a business, the better it decides.

| Company | Industry | Notes | City / State | Phone(s) |
|---|---|---|---|---|
| Smith Framing LLC | Framing subcontractor | Bids 10+ jobs a month | Austin, TX | (512) 555-0101 |

**Step 2 — Point the program at your file.** Open [main.py](main.py) and edit the top:

```python
INPUT_FILE_PATH = r"C:\path\to\your\leads.xlsx"
LIMIT = 5        # optional: only the first 5 businesses, good for a first test. None = all
```

**Step 3 — Run it:**

```bash
python main.py
```

> ⚠️ **Every run is live.** Businesses that pass the check really get called.

Leads are processed **1,000 at a time**: the program checks and scores a batch,
calls everyone in it, then moves on to the next 1,000.

---

## 3. What you see while it runs

Example output (the real lines also start with the time):

```
[setup]    Services: Draftliner (agent 2), Paradise Estimating (agent 3)
[verify]   DROP +1 416 555 0107 (dropped_not_us (CA)) - Maple Leaf Builders   ← number not usable
[decide]   SKIP +16025550105 Taco Loco - not interested (draftliner 3%, paradise 5%)
[decide]   PASS +18285550102 Blue Ridge Architects -> Draftliner (draftliner 94%, paradise 8%)
[campaign] Started 'Draftliner_2026-11-19' (campaign 21) with 12 number(s)
[call]     -> Calling +18285550102 (Blue Ridge Architects) [Draftliner_2026-11-19, run 2210]
[call]     <- +18285550102 Result: user_qualified | 67s | tags: user_qualified
[campaign] 'Draftliner_2026-11-19' (campaign 21) finished - paused
Done. Leads received: 10 | dropped by verifier: 1 | not interested: 2 | called: 7 | ...
```

- **DROP**: the phone number can't be used (not US, invalid, a duplicate, or already called).
- **SKIP**: Jev thinks the business won't be interested in any service.
- **PASS**: interested; the business is added to that service's campaign.
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

- One row per business, grouped by day, **newest day on top**.
- The most useful columns:

| Column | What it tells you |
|---|---|
| `status` | What happened (see below) |
| `interested_service` | Draftliner or Paradise Estimating |
| `prob_draftliner`, `prob_paradise` | How likely Jev thinks each service is (0–1) |
| `campaign_name` | The Dograh campaign it was called in |
| `call_disposition` | How the call ended, e.g. `user_qualified`, `user_hangup`, `no-answer` |
| `transcript_url`, `recording_url` | The conversation |

| `status` | Meaning |
|---|---|
| `called` | The call happened; see `call_disposition` |
| `not_interested` | Jev scored it below 85% for every service; not called |
| `dropped_...` | Phone problem: `not_us`, `invalid_number`, `special_number` (e.g. 900), `duplicate`, `already_called`, `no_phone` |
| `call_failed` / `call_timeout` | Dograh couldn't place the call, or it never finished |
| `skipped_missing_data` | The agent's script needs information this business didn't have |

> **Keep this file.** It is also the program's memory of who was already called.
> If you delete it, those businesses can be called again.

---

## 6. Sending leads from the scraper (code)

```python
import sys; sys.path.insert(0, r"C:\Users\adil.zia\Desktop\Tasks\Task10\dograh_caller")
from main import run

run({"company": "Summit Concrete Co", "category": "Concrete subcontractor",
     "location": "Denver, CO", "phone": "(303) 555-0142"})   # one business
run(list_of_business_dicts)                                    # many (processed 1,000 at a time)
```

See [examples/from_python.py](examples/from_python.py) for streaming leads one by one.

---

## 7. Changing how it behaves

| I want to… | Do this |
|---|---|
| Add or change a service (who it targets, which agent calls) | Edit [services.yaml](services.yaml); no code. See [SERVICES.md](SERVICES.md) |
| Call fewer, surer leads (or more) | Set `INTEREST_THRESHOLD=0.9` (stricter) or `0.8` in `.env` |
| Change the batch size | Set `LEAD_BATCH_SIZE=500` in `.env` |
| Change retries for busy / no-answer calls | `CAMPAIGN_MAX_RETRIES`, `CAMPAIGN_RETRY_DELAY_SECONDS` in `.env` |
| Run a different file once without editing main.py | `python main.py other.xlsx --limit 5` |

All settings: [DOCUMENTATION.md](DOCUMENTATION.md), section 4.

---

## 8. Common problems

| Problem | Fix |
|---|---|
| `Jev API key not found` / `Dograh API key not found` | Add the key to `.env` (step 1b) |
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
