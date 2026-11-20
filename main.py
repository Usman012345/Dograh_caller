#!/usr/bin/env python3
"""
Entry point: number verifier -> Jev decision maker -> Dograh calling agent -> results .xlsx

HOW TO USE
==========
1) Edit the INPUT section below:
     - INPUT_FILE_PATH : path to your leads spreadsheet (.xlsx / .csv / .json), or None
     - INPUT_LEADS     : lead dicts (e.g. from the scraper), or an empty list
2) Run:   python main.py      (always LIVE: qualified leads are called)

From other code (e.g. the AI-powered scraper), call run() with a file path,
one dict, or a list of dicts:

    from main import run
    run({"company": "...", "category": "...", "phone": "+1 512 555 0101"})
    run([lead1, lead2, ...])
    run(r"C:\\path\\to\\leads.xlsx")

(Command-line arguments still work and override the variables:
 python main.py leads.xlsx --limit 5)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

import config
from pipeline import run_leads_sync

# ==============================================================================
# INPUT - edit these
# ==============================================================================

# Path to a leads file: .xlsx (first row = column names), .csv or .json.
# Relative paths are relative to this folder. None = no file (use INPUT_LEADS).
# Left empty on purpose: every run is LIVE, so the program only calls leads you
# point it at. Don't use examples/sample_leads.xlsx here - its numbers are fictional.
INPUT_FILE_PATH: Optional[str] = None

# Worksheet to read from INPUT_FILE_PATH (None = first sheet).
INPUT_SHEET: Optional[str] = None

# Leads as Python dicts, e.g. data points coming from the AI-powered scraper.
# Each dict needs a phone field (phone / phone_number / mobile / ...) plus
# fields describing the business (company, category, description, location, ...).
# If both INPUT_FILE_PATH and INPUT_LEADS are set, both are processed.
INPUT_LEADS: List[Dict[str, Any]] = [
    # {
    #     "company": "Summit Concrete Co",
    #     "category": "Concrete subcontractor",
    #     "description": "Commercial flatwork and foundations, bids public work",
    #     "location": "Denver, CO",
    #     "phone": "(303) 555-0142",
    # },
]

# Only process the first N leads (None = all). Handy for a first real test.
LIMIT: Optional[int] = None

# ==============================================================================

LeadSource = Union[str, Path, Dict[str, Any], Iterable[Dict[str, Any]]]


def _clean(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)          # 14155550199.0 -> 14155550199
    if isinstance(value, str):
        return value.strip()
    return value


def read_leads(path: Union[str, Path], sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read leads from an .xlsx, .csv or .json file into a list of dicts."""
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = config.PROJECT_DIR / path
    if not path.is_file():
        raise FileNotFoundError(f"Input file not found: {path}")

    ext = path.suffix.lower()
    if ext == ".xls":
        raise ValueError("Old .xls files are not supported - open it in Excel and save as .xlsx.")
    if ext in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb[sheet] if sheet else wb.active
            rows = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
            leads = []
            for values in rows:
                lead = {h: _clean(v) for h, v in zip(header, values) if h and v not in (None, "")}
                if lead:
                    leads.append(lead)
            return leads
        finally:
            wb.close()
    if ext == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            leads = []
            for row in csv.DictReader(f):
                lead = {k.strip(): v.strip() for k, v in row.items() if k and v and v.strip()}
                if lead:
                    leads.append(lead)
            return leads
    if ext == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
            raise ValueError("JSON input must be an object or a list of objects.")
        return data
    raise ValueError(f"Unsupported input type '{ext}'. Use .xlsx, .csv or .json.")


def collect_leads(source: LeadSource, sheet: Optional[str] = None) -> List[Dict[str, Any]]:
    """Turn a file path, a single dict, or an iterable of dicts into a list of leads."""
    if source is None:
        return []
    if isinstance(source, (str, Path)):
        return read_leads(source, sheet)
    if isinstance(source, dict):
        return [source]
    leads = list(source)
    bad = [type(x).__name__ for x in leads if not isinstance(x, dict)]
    if bad:
        raise TypeError(f"Leads must be dicts; got {', '.join(sorted(set(bad)))}")
    return leads


def run(
    source: LeadSource,
    *,
    sheet: Optional[str] = None,
    limit: Optional[int] = None,
    threshold: Optional[float] = None,
    output: Optional[str] = None,
) -> int:
    """
    Run leads through the pipeline (live: qualified leads are called).
    `source` can be a file path, one lead dict, or a list of lead dicts.
    Returns 0 on success, 1 on error.
    """
    try:
        leads = collect_leads(source, sheet)
    except Exception as e:
        print(f"Could not load leads: {e}")
        return 1
    if limit is not None:
        if limit <= 0:
            print("Limit must be a positive integer.")
            return 1
        leads = leads[:limit]
    if not leads:
        print("No leads to process.")
        return 1

    print(f"Loaded {len(leads)} lead(s) - LIVE: qualified leads will be called")
    try:
        run_leads_sync(leads, threshold=threshold, results_file=output)
    except KeyboardInterrupt:
        print("\nStopped by user. This run's campaigns were paused; leads still in progress were not saved.")
        return 130
    except Exception as e:
        print(f"\nERROR: {e}")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify leads with Jev and call them with Dograh. "
                    "Without arguments, uses INPUT_FILE_PATH / INPUT_LEADS from main.py.")
    parser.add_argument("input", nargs="?", help="Leads file (.xlsx, .csv or .json); overrides INPUT_FILE_PATH")
    parser.add_argument("--sheet", help="Worksheet name (default: INPUT_SHEET or first sheet)")
    parser.add_argument("--threshold", type=float, help=f"Call threshold 0-1 (default {config.INTEREST_THRESHOLD})")
    parser.add_argument("--output", help=f"Results .xlsx (default {config.RESULTS_FILE})")
    parser.add_argument("--limit", type=int, help="Only process the first N leads")
    args = parser.parse_args()

    sheet = args.sheet or INPUT_SHEET

    try:
        if args.input:
            leads = collect_leads(args.input, sheet)
        else:
            leads = collect_leads(INPUT_FILE_PATH, sheet) + collect_leads(INPUT_LEADS)
    except Exception as e:
        print(f"Could not load leads: {e}")
        return 1
    if not leads:
        if not args.input and INPUT_FILE_PATH is None and not INPUT_LEADS:
            print("No input set. Set INPUT_FILE_PATH (or INPUT_LEADS) at the top of main.py,\n"
                  "or run: python main.py path\\to\\leads.xlsx")
        else:
            print("No leads found in the input.")
        return 1

    limit = args.limit if args.limit is not None else LIMIT
    return run(leads, limit=limit,
               threshold=args.threshold, output=args.output)


if __name__ == "__main__":
    sys.exit(main())
