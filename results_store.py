"""
Results store - writes one row per lead to an .xlsx file, grouped by day.

Layout (newest day on top):

    row 1   header (processed_at, status, ...)
    row 2   2026-11-19                    <- date row: only the date, nothing else
    row 3   today's 1st result
    row 4   today's 2nd result            <- new results of the same day go BELOW
    row 5   2026-11-18                    <- previous day moved down
    row 6   yesterday's 1st result
    ...

When the first result of a new day arrives, a new date row is inserted
directly under the header and earlier days move down.

The file is created on first use and reused on later runs. Lead fields
(whatever the scraper or input sheet provides) become extra columns; new
fields simply add new columns. A file written in an older layout is
regrouped into this layout when it is opened.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

SHEET_NAME = "results"
EXCEL_CELL_LIMIT = 32_000

FIXED_COLUMNS: List[str] = [
    "processed_at",
    "status",                # dropped_* | not_interested | called | call_failed | call_timeout | ...
    "detail",
    "phone_raw",
    "phone_e164",
    "phone_location",        # US state of the area code
    "phone_timezone",        # e.g. America/Chicago - for calling-hours rules
    "interested_service",
    "interest_probability",
    "agent_id",
    "agent_name",
    "campaign_id",
    "campaign_name",
    "dograh_run_id",         # the lead's last call attempt
    "call_attempts",
    "call_disposition",
    "call_duration_s",
    "call_tags",
    "transcript_url",
    "recording_url",
    "dograh_response",       # the run as returned by Dograh (JSON)
]

_HEADER_FONT = Font(bold=True, color="FFFFFF")
_HEADER_FILL = PatternFill("solid", fgColor="1F2937")
_DATE_FONT = Font(bold=True, color="111827")
_DATE_FILL = PatternFill("solid", fgColor="E5E7EB")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, (dict, list, tuple)):
        value = json.dumps(value, default=str, ensure_ascii=False)
    text = str(value)
    return text if len(text) <= EXCEL_CELL_LIMIT else text[:EXCEL_CELL_LIMIT] + "...[truncated]"


def _day_of(value: Any) -> str:
    """'2026-11-19 14:03:22' -> '2026-11-19'."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")[:10]
    return text if _DATE_RE.match(text) else ""


class ResultsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.wb = load_workbook(self.path)
            self.ws = self.wb[SHEET_NAME] if SHEET_NAME in self.wb.sheetnames else self.wb.active
            self.columns = [c.value for c in self.ws[1] if c.value]
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self.ws.title = SHEET_NAME
            self.columns = []
        self._ensure_columns(FIXED_COLUMNS)
        self.ws.freeze_panes = "A2"
        self._dirty = False

        # Day block currently being written and the row where its next result goes
        self._current_day: Optional[str] = None
        self._next_row = 0

        if self.ws.max_row > 1:
            self._regroup()

    # ------------------------------------------------------------------ layout

    def _ensure_columns(self, names: List[str]) -> None:
        for name in names:
            if name not in self.columns:
                self.columns.append(name)
                col = len(self.columns)
                cell = self.ws.cell(row=1, column=col, value=name)
                cell.font = _HEADER_FONT
                cell.fill = _HEADER_FILL
                self.ws.column_dimensions[get_column_letter(col)].width = max(14, min(len(name) + 4, 40))

    def _is_date_row(self, r: int) -> bool:
        first = self.ws.cell(row=r, column=1).value
        if not (isinstance(first, str) and _DATE_RE.match(first)):
            return False
        return all(self.ws.cell(row=r, column=c).value in (None, "")
                   for c in range(2, self.ws.max_column + 1))

    def _write_date_row(self, r: int, day: str) -> None:
        for c in range(1, len(self.columns) + 1):
            cell = self.ws.cell(row=r, column=c, value=day if c == 1 else None)
            cell.fill = _DATE_FILL
            if c == 1:
                cell.font = _DATE_FONT

    def _write_data_row(self, r: int, values: List[Any]) -> None:
        for c, value in enumerate(values, start=1):
            cell = self.ws.cell(row=r, column=c, value=value)
            cell.fill = PatternFill(fill_type=None)
            cell.font = Font()

    def _regroup(self) -> None:
        """Rewrite existing rows into the newest-day-first layout (idempotent)."""
        ncols = len(self.columns)
        day_col = self.columns.index("processed_at") + 1
        groups: "OrderedDict[str, List[List[Any]]]" = OrderedDict()
        for r in range(2, self.ws.max_row + 1):
            if self._is_date_row(r):
                continue
            values = [self.ws.cell(row=r, column=c).value for c in range(1, ncols + 1)]
            if all(v in (None, "") for v in values):
                continue
            groups.setdefault(_day_of(values[day_col - 1]), []).append(values)

        # Newest day first; rows without a readable date go last.
        # Within a day: oldest first (new results of the day go below).
        days = sorted((d for d in groups if d), reverse=True) + ([""] if "" in groups else [])
        if self.ws.max_row > 1:
            self.ws.delete_rows(2, self.ws.max_row - 1)
        r = 2
        for day in days:
            self._write_date_row(r, day or "unknown date")
            r += 1
            for values in sorted(groups[day], key=lambda v: str(v[day_col - 1] or "")):
                self._write_data_row(r, values)
                r += 1
        self._dirty = True

    # ------------------------------------------------------------------ API

    def append(self, row: Dict[str, Any]) -> None:
        """Add one result under today's date (newest day block is at the top)."""
        self._ensure_columns(list(row.keys()))
        day = _day_of(row.get("processed_at")) or datetime.now().strftime("%Y-%m-%d")

        if day != self._current_day:
            top_is_today = (
                self.ws.max_row >= 2
                and self._is_date_row(2)
                and self.ws.cell(row=2, column=1).value == day
            )
            if top_is_today:
                # Continue today's block: next free row is just above the next date row
                r = 3
                while r <= self.ws.max_row and not self._is_date_row(r):
                    r += 1
                self._next_row = r
            else:
                # New day: put its date row directly under the header
                self.ws.insert_rows(2)
                self._write_date_row(2, day)
                self._next_row = 3
            self._current_day = day

        if self._next_row <= self.ws.max_row:
            self.ws.insert_rows(self._next_row)
        self._write_data_row(self._next_row, [_cell(row.get(name)) for name in self.columns])
        self._next_row += 1
        self._dirty = True

    def save(self) -> bool:
        """Write the file. Returns False if it is locked (e.g. open in Excel)."""
        if not self._dirty:
            return True
        try:
            self.wb.save(self.path)
            self._dirty = False
            return True
        except PermissionError:
            return False
