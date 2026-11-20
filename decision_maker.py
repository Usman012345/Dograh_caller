"""
Decision maker - second stage of the pipeline, powered by Jev (TypeSafe).

For every lead, one Jev request asks one yes/no ("noul") question per enabled
service in services.yaml: "is this lead a likely customer for <service>?".
Jev returns a calibrated probability for each. The lead is routed to the
service with the highest probability, provided it is >= INTEREST_THRESHOLD.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from typesafe_sdk import AsyncTypeSafeClient, Noul

import config
from lead_fields import (
    cell_text, is_email_key, is_fax_key, is_phone_key, is_placeholder, key_words, walk_scalars,
)


# ------------------------------------------------------------------------------
# Services
# ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Service:
    id: str
    name: str
    agent_id: int
    description: str
    good_fit: str
    not_a_fit: str
    enabled: bool = True


def load_services(path: Path, include_disabled: bool = False) -> List[Service]:
    """Load and validate services.yaml."""
    if not path.exists():
        raise FileNotFoundError(f"Services file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("services") or []
    if not isinstance(entries, list):
        raise ValueError(f"{path.name}: 'services' must be a list.")

    services: List[Service] = []
    seen_ids = set()
    for i, entry in enumerate(entries, start=1):
        where = f"{path.name} service #{i}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: must be a mapping of fields.")
        missing = [k for k in ("id", "name", "agent_id", "description") if not entry.get(k)]
        if missing:
            raise ValueError(f"{where}: missing required field(s): {', '.join(missing)}")

        sid = str(entry["id"]).strip()
        if not re.fullmatch(r"[a-z0-9_]+", sid):
            raise ValueError(f"{where}: id '{sid}' must use only lowercase letters, digits and _.")
        if sid in seen_ids:
            raise ValueError(f"{where}: duplicate id '{sid}'.")
        seen_ids.add(sid)

        try:
            agent_id = int(entry["agent_id"])
        except (TypeError, ValueError):
            raise ValueError(f"{where}: agent_id must be a number (the Dograh agent ID).")

        service = Service(
            id=sid,
            name=str(entry["name"]).strip(),
            agent_id=agent_id,
            description=str(entry["description"]).strip(),
            good_fit=str(entry.get("good_fit") or "").strip(),
            not_a_fit=str(entry.get("not_a_fit") or "").strip(),
            enabled=bool(entry.get("enabled", True)),
        )
        if service.enabled or include_disabled:
            services.append(service)

    return services


# ------------------------------------------------------------------------------
# Decisions
# ------------------------------------------------------------------------------

@dataclass
class Decision:
    probabilities: Dict[str, float] = field(default_factory=dict)  # service id -> P(interested)
    service: Optional[Service] = None   # chosen service (None = no service passed the threshold)
    probability: float = 0.0            # probability of the chosen / best service

    @property
    def qualified(self) -> bool:
        return self.service is not None


# ------------------------------------------------------------------------------
# What Jev sees
# ------------------------------------------------------------------------------
# Leads are unstructured, so fields are judged by the words in their name and
# by their value. Jev only gets information that describes the business.

# Field-name words for bookkeeping, contact details and links: never shown to Jev
_NOISE_WORDS = frozenset({
    "id", "ids", "uuid", "guid", "version", "event", "created", "updated", "modified",
    "seen", "timestamp", "retry", "retries", "source", "sources", "dataset", "scrapper",
    "scraper", "url", "urls", "uri", "link", "links", "href", "website", "websites",
    "web", "domain", "linkedin", "facebook", "instagram", "twitter", "youtube", "tiktok", "yelp",
    "status", "timeline", "follow", "logged", "log", "external", "zip",
    "postal", "postcode", "lat", "lng", "lon", "latitude", "longitude", "coordinates", "geo",
    "score", "deliverability", "verified", "assigned", "rep", "photo", "photos", "image",
    "images", "logo", "avatar", "hours", "enriched",
})

# Exact multi-word/single-word field names that are noise on their own (without dropping
# descriptive compounds like "primary_category", "site_work", or "headquarters_city")
_NOISE_EXACT_PATHS = frozenset({
    ("is", "primary"), ("primary",), ("is", "headquarters"), ("headquarters",),
    ("site",), ("web", "site"), ("last", "activity"), ("recent", "activity"),
    ("recent", "action"), ("activity", "log"), ("activities", "log"),
})

# Field-name words in priority order: what matters most for deciding fit
_PRIORITY_WORDS = (
    # 0: who they are
    {"company", "business", "organization", "organisation", "org", "dba", "firm", "brand"},
    # 1: what they do
    {"category", "categories", "industry", "industries", "trade", "trades", "service", "services",
     "specialty", "specialties", "speciality", "specialization", "sector", "sectors", "type", "types",
     "kind", "naics", "sic", "classification", "keywords", "products", "capabilities", "scope", "work"},
    # 2: free-text descriptions
    {"description", "about", "summary", "notes", "note", "details", "overview", "bio", "profile",
     "info", "information", "comments", "tagline"},
    # 3: size and track record
    {"size", "employees", "employee", "staff", "revenue", "years", "founded", "established",
     "projects", "project", "volume", "bonding", "license", "licenses", "licensed", "certification",
     "certifications", "union", "rating", "accredited"},
    # 4: where
    {"city", "state", "country", "region", "county", "location", "locations", "market", "markets",
     "area", "areas", "served", "territory"},
)

# People's names don't help decide fit (and are personal data): a name field
# under a person-like field ("contact_name", "contacts > name", "owner > first_name")
# is not shown to Jev. Job titles under the same fields are kept.
_PERSON_WORDS = frozenset({
    "contact", "contacts", "person", "people", "owner", "owners", "employee", "employees",
    "principal", "principals", "manager", "managers", "agent", "agents", "decision", "maker",
})
_PERSON_NAME_WORDS = frozenset({"name", "names", "first", "last", "full", "given", "surname", "middle"})

_URL_VALUE = re.compile(r"^\s*(https?://|www\.)\S+\s*$", re.IGNORECASE)
_EMAIL_VALUE = re.compile(r"^\s*[^@\s]+@[^@\s]+\.[a-z]{2,}\s*$", re.IGNORECASE)
_DATE_VALUE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}([T ][\d:.]+)?(Z|[+-]\d{2}:?\d{2})?\s*$")


def _priority(path: tuple) -> int:
    words = set(w for part in path for w in key_words(part))
    if path and key_words(path[-1]) in (["name"], ["title"]) and not words & _PERSON_WORDS:
        return 0                               # the business name / opportunity title
    for rank, group in enumerate(_PRIORITY_WORDS):
        if words & group:
            return rank
    return len(_PRIORITY_WORDS)


def _is_noise_field(path: tuple) -> bool:
    for part in path:
        if is_phone_key(part) or is_fax_key(part) or is_email_key(part):
            return True
        words = tuple(key_words(part))
        if (set(words) & _NOISE_WORDS) or (words in _NOISE_EXACT_PATHS):
            return True
    all_words = set(w for part in path for w in key_words(part))
    if all_words & _PERSON_WORDS and set(key_words(path[-1])) & _PERSON_NAME_WORDS:
        return True                            # a person's name
    return False


def _state_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return cell_text(value)


def _is_noise_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if is_placeholder(value):
        return True
    text = _state_text(value)
    if not text or _URL_VALUE.match(text) or _EMAIL_VALUE.match(text) or _DATE_VALUE.match(text):
        return True
    digits = sum(c.isdigit() for c in text)
    return digits >= 10 and digits / len(text) >= 0.6      # looks like a phone/fax number


def lead_state(lead: Dict[str, Any]) -> Dict[str, Any]:
    """
    The lead as shown to Jev: only fields that describe the business.

    Works on unstructured leads (any field names, nesting, JSON text). Removed:
    phone / fax / e-mail fields, links, IDs, versions, dates, statuses and other
    bookkeeping, placeholders ("n/a", "Not specified"), and values that look like
    phone numbers, URLs, e-mails or timestamps. Nested fields are flattened to
    "parent > child". Each value is cut to JEV_MAX_FIELD_CHARS, and the most
    useful fields (name, category/services, descriptions, size, location) are
    kept first until JEV_MAX_STATE_CHARS is reached.
    """
    items = []  # (priority, order, key, text)
    merged: Dict[str, int] = {}
    for order, (path, value) in enumerate(walk_scalars(lead)):
        if not path or _is_noise_field(path) or _is_noise_value(value):
            continue
        text = _state_text(value)
        if len(text) > config.JEV_MAX_FIELD_CHARS:
            text = text[: config.JEV_MAX_FIELD_CHARS].rstrip() + " ..."
        key = " > ".join(path)
        if key in merged:                      # repeated field from a list: join the values
            idx = merged[key]
            p, o, k, t = items[idx]
            if text.casefold() not in t.casefold():
                combined = f"{t}; {text}"
                if len(combined) > config.JEV_MAX_FIELD_CHARS:
                    combined = combined[: config.JEV_MAX_FIELD_CHARS].rstrip() + " ..."
                items[idx] = (p, o, k, combined)
            continue
        merged[key] = len(items)
        items.append((_priority(path), order, key, text))

    state: Dict[str, Any] = {}
    budget = config.JEV_MAX_STATE_CHARS
    for _p, _o, key, text in sorted(items, key=lambda it: (it[0], it[1])):
        cost = len(key) + len(text) + 6
        if cost > budget:
            room = budget - len(key) - 10
            if room < 200:
                continue                       # not enough room left for a useful snippet
            text = text[:room].rstrip() + " ..."
            cost = len(key) + len(text) + 6
        state[key] = text
        budget -= cost
    return state


def build_question(service: Service) -> Noul:
    instructions = (
        f"This lead is a business that could be contacted by phone. Decide whether it is "
        f"realistically a potential customer that would be interested in {service.name}'s "
        f"services, based on what the business does, its category, description and location.\n\n"
        f"About {service.name}:\n{service.description}"
    )
    criteria = {}
    if service.good_fit:
        criteria["true"] = f"Yes - the lead fits this profile: {service.good_fit}"
    if service.not_a_fit:
        criteria["false"] = f"No - for example: {service.not_a_fit}"
    return Noul(instructions=instructions, criteria=criteria or None)


class DecisionMaker:
    """Asks Jev which service (if any) each lead is likely to buy."""

    def __init__(self, services: List[Service], threshold: float, api_key: str, model: str):
        if not services:
            raise ValueError("No enabled services in services.yaml - nothing to decide on.")
        if not api_key:
            raise ValueError(
                "Jev API key not found. Put it in .env as TYPESAFE_API_KEY=... (or Jev_Key=...)."
            )
        self.services = services
        self.threshold = threshold
        self.model = model
        self._questions = {s.id: build_question(s) for s in services}
        self._client = AsyncTypeSafeClient(api_key=api_key, model=model)

    async def decide(self, lead: Dict[str, Any]) -> Decision:
        state = lead_state(lead)
        if not state:
            return Decision()
        response = await self._client.system_one(state=state, questions=self._questions)

        probabilities = {sid: float(ans.noul) for sid, ans in response.nouls.items()}
        decision = Decision(probabilities=probabilities)
        if not probabilities:
            return decision

        best_id = max(probabilities, key=probabilities.get)
        decision.probability = probabilities[best_id]
        if decision.probability >= self.threshold:
            decision.service = next(s for s in self.services if s.id == best_id)
        return decision

    async def close(self) -> None:
        await self._client.aclose()
