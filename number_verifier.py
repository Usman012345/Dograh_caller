"""
Number verifier - first stage of the pipeline.

Receives raw lead dicts (from the AI-powered scraper or a spreadsheet), finds
the phone number(s), and only lets through a valid, callable, not-yet-seen US
number in E.164 format (+1XXXXXXXXXX). Everything else is dropped with a reason.

How a lead's number is chosen
-----------------------------
1. Every field whose NAME looks like a phone field is examined ("Phone(s)",
   "Phone Number", "phone_number", "Mobile", "Cell", "Telephone", any name
   containing "phone", ...). Fields such as "Phone Verified: Yes" simply yield
   no number, so they no longer hide the real phone column.
2. Numbers are pulled out of each value with Google's libphonenumber number
   finder, so cells like "+19292659900, +19174102250",
   "512-555-0102 / 512-555-0103" or "Office: (512) 555-0104 Cell: ..." work.
   Vanity numbers ("1-800-FLOWERS") are handled by a direct parse.
3. Each number is checked against the real numbering plan:
     - valid number (real area code, correct length, ...);
     - region is allowed (default: US only - Canada, Puerto Rico, Guam and other
       +1 regions are dropped; see ALLOWED_PHONE_REGIONS in config.py);
     - callable type: premium-rate (900), shared-cost, personal (500-type),
       pager and similar special numbers are dropped.
4. Repeat protection - the lead is dropped if any of its passing numbers is:
     - already called in an earlier run (results sheet)    -> dropped_already_called
     - already seen earlier in this run                    -> dropped_duplicate
5. The first number that passes is the one called. Its US state and time zone
   are recorded (from the area code) for calling-hours checks and reporting.

Limitation: "valid" means the number is correctly formed for the US numbering
plan. Whether it is currently in service can only be checked with a carrier
lookup service (e.g. Twilio Lookup).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

import phonenumbers
from phonenumbers import Leniency, NumberParseException, PhoneNumberFormat, PhoneNumberMatcher
from phonenumbers import PhoneNumberType as NT
from phonenumbers import geocoder
from phonenumbers import timezone as pn_timezone

import config

# Column names that hold a phone number. Matching ignores case and every
# non-letter/digit character. Any column whose name contains "phone" matches too.
PHONE_KEYS = (
    "phone_number", "phone", "phones", "phonenumber", "phonenumbers", "phone_no",
    "mobile", "mobile_number", "cell", "cellphone", "telephone", "tel",
    "contact_number", "contact_phone", "number", "business_phone",
)

# Number types that may be called. Everything else (premium rate, shared cost,
# personal/500-type, pager, UAN, voicemail-only, unknown) is dropped.
CALLABLE_TYPES = frozenset({NT.FIXED_LINE, NT.MOBILE, NT.FIXED_LINE_OR_MOBILE, NT.TOLL_FREE})
_TYPE_NAMES = {v: k for k, v in vars(NT).items() if isinstance(v, int)}

# When a lead has no usable number, the most informative reason is reported.
_REASON_RANK = {
    "dropped_not_us": 4,
    "dropped_special_number": 3,
    "dropped_invalid_number": 2,
    "dropped_unreadable_number": 1,
}


def _norm_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


_PHONE_KEYS_NORMALIZED = {_norm_key(k) for k in PHONE_KEYS}


def is_phone_key(key: Any) -> bool:
    """True if a column/field name looks like a phone number field."""
    norm = _norm_key(key)
    return norm in _PHONE_KEYS_NORMALIZED or "phone" in norm


@dataclass(frozen=True)
class Verification:
    """Outcome of verifying one lead's phone number."""
    ok: bool
    raw: str                 # phone field value(s) as found in the lead
    e164: Optional[str]      # the number that will be called (+1XXXXXXXXXX) when ok
    reason: str              # "ok" or why it was dropped
    location: str = ""       # US state of the number's area code, e.g. "Texas"
    time_zone: str = ""      # e.g. "America/Chicago" ("" for toll-free / multi-zone numbers)


def number_location(e164: str) -> Tuple[str, str]:
    """(state, time zone) of a number from its area code; blanks when not location-specific."""
    try:
        parsed = phonenumbers.parse(e164)
    except NumberParseException:
        return "", ""
    location = geocoder.description_for_number(parsed, "en") or ""
    zones = [z for z in pn_timezone.time_zones_for_number(parsed) if z != "Etc/Unknown"]
    time_zone = zones[0] if len(zones) == 1 else (", ".join(zones) if 1 < len(zones) <= 3 else "")
    return location, time_zone


def _clean_cell(value: Any) -> str:
    # Spreadsheets often store numbers as floats (14155550199.0)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def phone_values(lead: Dict[str, Any]) -> List[Tuple[str, str]]:
    """All non-empty (field name, value) pairs whose field looks like a phone field."""
    out = []
    for key, value in lead.items():
        if value is None or not is_phone_key(key):
            continue
        text = _clean_cell(value)
        if text:
            out.append((str(key), text))
    return out


def find_phone(lead: Dict[str, Any]) -> str:
    """The lead's phone field value(s) as one string, or '' if there are none."""
    values = phone_values(lead)
    if len(values) == 1:
        return values[0][1]
    return " | ".join(f"{k}: {v}" for k, v in values)


def extract_numbers(text: str) -> Tuple[List[phonenumbers.PhoneNumber], Optional[str]]:
    """
    Find every phone number in a text. Returns (numbers, reason) where reason
    explains a failure when no number could be read at all.
    """
    found = [m.number for m in PhoneNumberMatcher(text, "US", leniency=Leniency.VALID)]
    if found:
        return found, None
    # Fallback: parse the whole value (handles vanity numbers like 1-800-FLOWERS
    # and lets invalid numbers report why they are invalid).
    try:
        return [phonenumbers.parse(text, "US")], None
    except NumberParseException:
        return [], "dropped_unreadable_number"


def load_called_numbers(results_file: Path, statuses: Iterable[str] = ("called", "call_timeout")) -> Set[str]:
    """Numbers already dialed in earlier runs, read from the results sheet."""
    path = Path(results_file)
    if not path.is_file():
        return set()
    from openpyxl import load_workbook
    wanted = set(statuses)
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        header = list(next(rows, []) or [])
        if "status" not in header or "phone_e164" not in header:
            return set()
        si, pi = header.index("status"), header.index("phone_e164")
        return {str(r[pi]) for r in rows
                if len(r) > max(si, pi) and r[si] in wanted and r[pi]}
    finally:
        wb.close()


class NumberVerifier:
    """Validates phone numbers; drops non-US, invalid, special, already-called and duplicate ones."""

    def __init__(
        self,
        allowed_regions: Optional[Iterable[str]] = None,
        already_called: Iterable[str] = (),
    ) -> None:
        regions = allowed_regions if allowed_regions is not None else config.ALLOWED_PHONE_REGIONS
        self.allowed_regions: FrozenSet[str] = frozenset(r.strip().upper() for r in regions if r.strip())
        self.already_called: Set[str] = set(already_called)
        self._seen: Set[str] = set()

    def check_number(self, number: Any) -> Tuple[Optional[str], str]:
        """
        Validate one number (a parsed PhoneNumber or text).
        Returns (e164, "ok") or (None, drop reason).
        """
        if not isinstance(number, phonenumbers.PhoneNumber):
            try:
                number = phonenumbers.parse(str(number), "US")
            except NumberParseException:
                return None, "dropped_unreadable_number"
        if not phonenumbers.is_valid_number(number):
            return None, "dropped_invalid_number"
        region = phonenumbers.region_code_for_number(number)
        if region not in self.allowed_regions:
            return None, f"dropped_not_us ({region or '+' + str(number.country_code)})"
        number_type = phonenumbers.number_type(number)
        if number_type not in CALLABLE_TYPES:
            return None, f"dropped_special_number ({_TYPE_NAMES.get(number_type, number_type)})"
        return phonenumbers.format_number(number, PhoneNumberFormat.E164), "ok"

    def verify(self, lead: Dict[str, Any]) -> Verification:
        values = phone_values(lead)
        raw = find_phone(lead)
        if not values:
            return Verification(False, raw, None, "dropped_no_phone")

        best_reason: Optional[str] = None

        def note(reason: str) -> None:
            nonlocal best_reason
            rank = _REASON_RANK.get(reason.split(" ")[0], 0)
            if best_reason is None or rank > _REASON_RANK.get(best_reason.split(" ")[0], 0):
                best_reason = reason

        callable_numbers: List[str] = []
        for _field, text in values:
            numbers, failure = extract_numbers(text)
            if failure:
                note(failure)
            for number in numbers:
                e164, reason = self.check_number(number)
                if e164 is None:
                    note(reason)
                elif e164 not in callable_numbers:
                    callable_numbers.append(e164)

        if not callable_numbers:
            return Verification(False, raw, None, best_reason or "dropped_unreadable_number")

        # Repeat protection: any matching number blocks the whole lead (same business)
        called = next((n for n in callable_numbers if n in self.already_called), None)
        seen = next((n for n in callable_numbers if n in self._seen), None)
        self._seen.update(callable_numbers)
        if called:
            return Verification(False, raw, called, "dropped_already_called", *number_location(called))
        if seen:
            return Verification(False, raw, seen, "dropped_duplicate", *number_location(seen))
        chosen = callable_numbers[0]
        return Verification(True, raw, chosen, "ok", *number_location(chosen))
