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

from number_verifier import is_phone_key


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


def lead_state(lead: Dict[str, Any]) -> Dict[str, Any]:
    """The lead as shown to Jev: all fields except phone numbers and empty values."""
    state = {}
    for key, value in lead.items():
        if is_phone_key(key) or value in (None, ""):
            continue
        state[str(key)] = value if isinstance(value, (str, int, float, bool)) else str(value)
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
        response = await self._client.system_one(state=lead_state(lead), questions=self._questions)

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
