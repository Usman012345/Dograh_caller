"""
Campaign stage - one Dograh campaign per service per run.

For every service that has interested leads, a campaign named
"<service name>_<YYYY-MM-DD>" is created with that service's Dograh agent and
exactly those leads, then started. Its calls (including Dograh's automatic
retries) are followed until every lead has a final outcome.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

import config
from decision_maker import Service
from dograh_client import (
    Agent, CallOutcome, DograhClient, DograhError, call_context, outcome_from_run, run_disposition,
)

RETRYABLE = {
    "retry_on_busy": {"busy"},
    "retry_on_no_answer": {"no-answer", "no_answer"},
    "retry_on_voicemail": {"voicemail", "machine"},
}
FINAL_CAMPAIGN_STATES = {"completed", "failed"}


@dataclass
class CampaignLead:
    ref: str                 # our id for the lead inside the campaign (CSV column lead_ref)
    phone: str               # E.164
    lead: Dict[str, Any]     # original lead fields
    payload: Any = None      # opaque object handed back in callbacks (the pipeline item)


def campaign_name(service: Service, when: Optional[datetime] = None) -> str:
    return f"{service.name}_{(when or datetime.now()).strftime(config.CAMPAIGN_NAME_DATE_FORMAT)}"


def _ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class CampaignJob:
    """One campaign: one service, one agent, a fixed list of leads."""

    def __init__(self, service: Service, agent: Agent, leads: List[CampaignLead],
                 name_date: Optional[datetime] = None):
        self.service = service
        self.agent = agent
        self.leads = leads
        self.name = campaign_name(service, name_date)
        self.campaign_id: Optional[int] = None
        # Leads left out because they lack a value the agent's prompt needs: (lead, reason)
        self.skipped: List[tuple] = []

    # ------------------------------------------------------------------ launch

    def launch(self, client: DograhClient) -> None:
        """Upload contacts, create and start the campaign (blocking; run in a thread)."""
        # Dograh refuses the WHOLE campaign if any row lacks a value for a
        # {{variable}} the agent uses, so leave out only the incomplete leads.
        required = client.get_required_variables(self.agent.id)
        rows, included = [], []
        for cl in self.leads:
            row = call_context(cl.lead, cl.phone, self.service.name)
            row["lead_ref"] = cl.ref
            missing = sorted(v for v in required if not str(row.get(v, "")).strip())
            if missing:
                self.skipped.append((cl, f"Missing value for {', '.join(missing)} "
                                         f"(used in agent '{self.agent.name}' prompt)"))
                continue
            rows.append(row)
            included.append(cl)
        self.leads = included
        if not rows:
            raise DograhError(f"No lead has the values agent '{self.agent.name}' needs: {', '.join(sorted(required))}")
        file_key = client.upload_csv(rows, f"{self.name}.csv")
        campaign = client.create_campaign(self.name, self.agent.id, file_key)
        self.campaign_id = int(campaign["id"])
        client.start_campaign(self.campaign_id)

    # ------------------------------------------------------------------ monitor

    async def monitor(
        self,
        client: DograhClient,
        on_call_started: Callable[[CampaignLead, Dict[str, Any], int], None],
        on_result: Callable[[CampaignLead, CallOutcome], None],
    ) -> None:
        """Follow the campaign until every lead has a final outcome."""
        assert self.campaign_id is not None
        by_ref = {cl.ref: cl for cl in self.leads}
        by_phone = {cl.phone: cl for cl in self.leads}
        announced: Set[Any] = set()
        finished: Set[str] = set()
        completed_at: Dict[Any, float] = {}
        last_progress = time.monotonic()
        errors = 0

        def finish(cl: CampaignLead, outcome: CallOutcome) -> None:
            finished.add(cl.ref)
            on_result(cl, outcome)

        while True:
            await asyncio.sleep(config.CAMPAIGN_POLL_SECONDS)
            try:
                details = await asyncio.to_thread(client.get_campaign, self.campaign_id)
                runs = await asyncio.to_thread(client.get_campaign_runs, self.campaign_id)
                errors = 0
            except DograhError as e:
                errors += 1
                if errors >= 10:
                    for cl in self.leads:
                        if cl.ref not in finished:
                            finish(cl, CallOutcome(status="call_failed", error=f"Lost contact with Dograh: {e}"))
                    return
                continue

            # Group call attempts by lead
            attempts: Dict[str, List[Dict[str, Any]]] = {}
            for run in sorted(runs, key=lambda r: r.get("id") or 0):
                ctx = run.get("initial_context") or {}
                cl = by_ref.get(str(ctx.get("lead_ref") or "")) or by_phone.get(
                    str(ctx.get("phone_number") or ctx.get("called_number") or ""))
                if cl is None:
                    continue
                attempts.setdefault(cl.ref, []).append(run)
                if run.get("id") not in announced:
                    announced.add(run.get("id"))
                    last_progress = time.monotonic()
                    on_call_started(cl, run, len(attempts[cl.ref]))

            now = time.monotonic()
            retry_cfg = details.get("retry_config") or {}
            for cl in self.leads:
                if cl.ref in finished or cl.ref not in attempts:
                    continue
                tries = attempts[cl.ref]
                latest = tries[-1]
                if not latest.get("is_completed"):
                    continue
                completed_at.setdefault(latest.get("id"), now)
                # Dograh may write the outcome a moment after the call ends
                if not run_disposition(latest) and now - completed_at[latest.get("id")] < config.RESULT_WAIT_SECONDS:
                    continue
                if self._retry_pending(latest, len(tries), retry_cfg):
                    continue
                last_progress = now
                finish(cl, outcome_from_run(latest, attempts=len(tries)))

            if len(finished) == len(self.leads):
                return

            state = str(details.get("state") or "").lower()
            if state in FINAL_CAMPAIGN_STATES:
                for cl in self.leads:
                    if cl.ref in finished:
                        continue
                    tries = attempts.get(cl.ref) or []
                    if tries and not tries[-1].get("is_completed"):
                        continue  # still on the phone; wait for it
                    if tries:
                        finish(cl, outcome_from_run(tries[-1], attempts=len(tries)))
                    else:
                        finish(cl, CallOutcome(status="call_failed",
                                               error=f"Not dialed: campaign ended in state '{state}'"))
                if len(finished) == len(self.leads):
                    return

            if time.monotonic() - last_progress > config.CAMPAIGN_IDLE_TIMEOUT_SECONDS:
                for cl in self.leads:
                    if cl.ref not in finished:
                        tries = attempts.get(cl.ref) or []
                        outcome = (outcome_from_run(tries[-1], attempts=len(tries)) if tries
                                   else CallOutcome(status="call_timeout"))
                        outcome.status = "call_timeout"
                        outcome.error = (f"No progress in campaign for {config.CAMPAIGN_IDLE_TIMEOUT_SECONDS}s "
                                         f"(state '{state}')")
                        finish(cl, outcome)
                return

    @staticmethod
    def _retry_pending(latest: Dict[str, Any], attempts: int, retry_cfg: Dict[str, Any]) -> bool:
        """True if Dograh is expected to call this lead again."""
        if not retry_cfg.get("enabled"):
            return False
        retryable = set()
        for flag, dispositions in RETRYABLE.items():
            if retry_cfg.get(flag):
                retryable |= dispositions
        if (run_disposition(latest) or "").lower() not in retryable:
            return False
        if attempts > int(retry_cfg.get("max_retries") or 0):
            return False
        started = _ts(latest.get("created_at"))
        if started is None:
            return True
        window = int(retry_cfg.get("retry_delay_seconds") or 0) + config.RETRY_GRACE_SECONDS
        return time.time() < started + window
