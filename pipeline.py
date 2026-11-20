"""
The lead pipeline.

    submit(lead) ─► [Number verifier] ─► [Decision maker (Jev)] ─► per-service list
                         │ dropped              │ not interested          │
                         ▼                      ▼                         ▼  (when input ends)
                    results .xlsx          results .xlsx      one Dograh campaign per service
                                                              "<service>_<YYYY-MM-DD>" with its
                                                              agent ─► call results ─► results .xlsx

Verification and Jev scoring run concurrently while leads are being submitted.
Dograh campaigns can't receive numbers after they are created, so each
service's campaign is created once all leads of the run have been scored
(or whenever flush() is called), then followed until every call has a result.

Usage from other code (e.g. the AI-powered scraper):

    from pipeline import LeadPipeline

    async with LeadPipeline() as pipeline:
        async for lead in scraper():          # each lead is a plain dict
            await pipeline.submit(lead)
    # leaving the block creates the campaigns and waits for all calls

Queues live in memory only: if the process stops, leads still in flight are lost.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterable, Dict, Iterable, List, Optional, Union

import config
from campaign_manager import CampaignJob, CampaignLead
from decision_maker import Decision, DecisionMaker, Service, load_services
from dograh_client import Agent, CallOutcome, DograhClient, is_transferred
from number_verifier import NumberVerifier, Verification, load_called_numbers
from results_store import TRANSFER_COLUMNS, TRANSFERS_SHEET_NAME, ResultsStore, transfer_row

_STOP_TIMEOUT = 5


def log(stage: str, message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {stage:<8} {message}", flush=True)


class _Item:
    """A lead travelling through the pipeline."""
    __slots__ = ("lead", "verification", "decision", "service", "agent", "outcome",
                 "campaign_id", "campaign_name", "status", "detail")

    def __init__(self, lead: Dict[str, Any]):
        self.lead = lead
        self.verification: Optional[Verification] = None
        self.decision: Optional[Decision] = None
        self.service: Optional[Service] = None
        self.agent: Optional[Agent] = None
        self.outcome: Optional[CallOutcome] = None
        self.campaign_id: Optional[int] = None
        self.campaign_name: Optional[str] = None
        self.status = ""
        self.detail = ""

    @property
    def label(self) -> str:
        for key in ("company", "company_name", "business_name", "name", "title"):
            for k, v in self.lead.items():
                if str(k).strip().lower().replace(" ", "_") == key and v:
                    return str(v)[:40]
        return "(no company name)"


class LeadPipeline:
    def __init__(
        self,
        *,
        results_file: Optional[Union[str, Path]] = None,
        threshold: Optional[float] = None,
        services_file: Optional[Union[str, Path]] = None,
    ):
        self.results_file = Path(results_file) if results_file else config.RESULTS_FILE
        self.threshold = config.INTEREST_THRESHOLD if threshold is None else threshold
        self.services_file = Path(services_file) if services_file else config.SERVICES_FILE
        self.stats: Counter = Counter()

        self._verify_q: asyncio.Queue = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self._decision_q: asyncio.Queue = asyncio.Queue(maxsize=config.QUEUE_MAXSIZE)
        self._results_q: asyncio.Queue = asyncio.Queue()
        self._pending: Dict[str, List[_Item]] = {}     # service id -> interested leads awaiting a campaign
        self._flush_lock = asyncio.Lock()
        self._live_campaigns: Dict[int, str] = {}       # campaign id -> name, started and not yet stopped
        self._lead_counter = 0
        self._stage_tasks: List[asyncio.Task] = []
        self._writer_task: Optional[asyncio.Task] = None
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._started:
            return
        self.services = load_services(self.services_file)
        already_called = (
            await asyncio.to_thread(load_called_numbers, self.results_file)
            if config.SKIP_ALREADY_CALLED else set()
        )
        self.verifier = NumberVerifier(already_called=already_called)
        self.decider = DecisionMaker(self.services, self.threshold, config.JEV_API_KEY, config.JEV_MODEL)
        self.store = await asyncio.to_thread(ResultsStore, self.results_file)
        # Only businesses whose call was transferred; the file is created on the first transfer
        self.transfers = await asyncio.to_thread(
            lambda: ResultsStore(config.TRANSFERS_FILE, fixed_columns=TRANSFER_COLUMNS,
                                 sheet_name=TRANSFERS_SHEET_NAME, time_column="transferred_at"))
        self.dograh = DograhClient()
        self.agents: Dict[str, Agent] = {}
        # Campaign names use the date the run started ("Draftliner_<start date>")
        self.run_started = datetime.now()

        log("setup", f"Services: {', '.join(f'{s.name} (agent {s.agent_id})' for s in self.services)}")
        log("setup", f"Jev model {config.JEV_MODEL}, call threshold {self.threshold:.0%}")
        log("setup", f"Phone regions: {', '.join(sorted(self.verifier.allowed_regions))} | "
                     f"already called in earlier runs: {len(already_called)} number(s)")
        for s in self.services:
            agent = await asyncio.to_thread(self.dograh.get_agent, s.agent_id)
            self.agents[s.id] = agent
            note = ""
            if agent.version_status and agent.version_status != "published":
                note = " - WARNING: has unpublished changes; calls use the last PUBLISHED version"
            log("setup", f"{s.name} -> agent {agent.id} '{agent.name}'{note}")
        log("setup", f"Results file: {self.results_file}")
        print("-" * 100, flush=True)

        self._stage_tasks.append(asyncio.create_task(self._verifier_worker()))
        for _ in range(max(1, config.DECISION_CONCURRENCY)):
            self._stage_tasks.append(asyncio.create_task(self._decision_worker()))
        self._writer_task = asyncio.create_task(self._writer_worker())
        self._started = True

    async def submit(self, lead: Dict[str, Any]) -> None:
        """Hand one lead (a dict) to the pipeline. Waits if the pipeline is full."""
        if not self._started:
            await self.start()
        if not isinstance(lead, dict):
            raise TypeError(f"Leads must be dicts, got {type(lead).__name__}")
        self.stats["received"] += 1
        await self._verify_q.put(_Item(dict(lead)))

    async def flush(self) -> None:
        """
        Finish scoring every lead submitted so far, then create one campaign per
        service that has interested leads and wait until all their calls are done.
        close() calls this automatically; long-running callers (a scraper that
        never stops) can call it periodically to dial what has been collected.
        """
        async with self._flush_lock:
            await self._verify_q.join()
            await self._decision_q.join()
            pending, self._pending = self._pending, {}
            if not pending:
                return
            await self._run_campaigns(pending)
            await self._results_q.join()

    async def close(self, abort: bool = False) -> None:
        """
        Process every submitted lead (including all calls), then shut down.
        With abort=True (Ctrl+C or an error) nothing more is processed, though
        already-queued results are flushed to disk before exiting.
        Either way, every campaign this run started is stopped before exiting.
        """
        if not self._started:
            return
        try:
            if not abort:
                await self.flush()
        finally:
            try:
                for t in self._stage_tasks:
                    t.cancel()
                if self._stage_tasks:
                    await asyncio.gather(*self._stage_tasks, return_exceptions=True)
                self._stage_tasks.clear()

                if self._writer_task is not None and not self._writer_task.done():
                    try:
                        await self._results_q.join()
                    except BaseException:
                        pass
                    self._writer_task.cancel()
                    await asyncio.gather(self._writer_task, return_exceptions=True)
                self._writer_task = None
            finally:
                self._stop_live_campaigns()
                for store in (self.store, self.transfers):
                    try:
                        await self._final_save(store)
                    except BaseException:
                        store.save()
                await self.decider.close()
                self.dograh.close()
                self._started = False
                self._print_summary()

    async def __aenter__(self) -> "LeadPipeline":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close(abort=exc_type is not None)

    def _stop_live_campaigns(self) -> None:
        """Pause every campaign of this run that is still running (blocking; used at exit)."""
        for cid, name in list(self._live_campaigns.items()):
            try:
                state = self.dograh.stop_campaign(cid)
                log("campaign", f"'{name}' (campaign {cid}) {'paused' if state == 'paused' else 'is ' + state}")
                del self._live_campaigns[cid]
            except Exception as e:
                log("campaign", f"WARNING: could not stop '{name}' (campaign {cid}) - pause it in Dograh: {e}")

    # ------------------------------------------------------------------ stages

    async def _verifier_worker(self) -> None:
        while True:
            item: _Item = await self._verify_q.get()
            try:
                item.verification = self.verifier.verify(item.lead)
                if item.verification.ok:
                    await self._decision_q.put(item)
                else:
                    raw = item.verification.raw or "-"
                    log("verify", f"DROP {raw} ({item.verification.reason}) - {item.label}")
                    await self._finish(item, item.verification.reason)
            except Exception as e:  # never let one bad lead kill the stage
                await self._finish(item, "error", f"verifier: {e}")
            finally:
                self._verify_q.task_done()

    async def _decision_worker(self) -> None:
        while True:
            item: _Item = await self._decision_q.get()
            try:
                item.decision = await self.decider.decide(item.lead)
                probs = ", ".join(f"{k} {v:.0%}" for k, v in item.decision.probabilities.items()) or "no business details"
                phone = item.verification.e164
                if not item.decision.qualified:
                    log("decide", f"SKIP {phone} {item.label} - not interested ({probs})")
                    await self._finish(item, "not_interested")
                    continue

                item.service = item.decision.service
                item.agent = self.agents[item.service.id]
                self._pending.setdefault(item.service.id, []).append(item)
                log("decide", f"PASS {phone} {item.label} -> {item.service.name} ({probs})")
            except Exception as e:
                log("decide", f"ERROR {item.label}: {e}")
                await self._finish(item, "decision_error", str(e))
            finally:
                self._decision_q.task_done()

    async def _run_campaigns(self, pending: Dict[str, List[_Item]]) -> None:
        """Create, start and follow one campaign per service."""
        print("-" * 100, flush=True)
        jobs: List[CampaignJob] = []
        for service in self.services:
            items = pending.get(service.id) or []
            if not items:
                continue
            leads = []
            for item in items:
                self._lead_counter += 1
                leads.append(CampaignLead(ref=f"L{self._lead_counter:05d}", phone=item.verification.e164,
                                          lead=item.lead, payload=item))
            job = CampaignJob(service, self.agents[service.id], leads, name_date=self.run_started)
            try:
                await asyncio.to_thread(job.launch, self.dograh)
                error = None
            except Exception as e:
                error = e
            if job.campaign_id is not None:
                # Track it even if starting failed, so it is never left running on exit
                self._live_campaigns[job.campaign_id] = job.name

            # Leads left out for lacking a value the agent's prompt needs
            skipped_ids = set()
            for cl, reason in job.skipped:
                skipped_ids.add(id(cl.payload))
                log("campaign", f"SKIP {cl.phone} {cl.payload.label} - {reason}")
                await self._finish(cl.payload, "skipped_missing_data", reason)

            if error is not None:
                log("campaign", f"FAILED to start '{job.name}': {error}")
                for item in items:
                    if id(item) not in skipped_ids:
                        item.campaign_name = job.name
                        await self._finish(item, "call_failed", f"Campaign could not be started: {error}")
                continue

            for cl in job.leads:
                cl.payload.campaign_id, cl.payload.campaign_name = job.campaign_id, job.name
            log("campaign", f"Started '{job.name}' (campaign {job.campaign_id}) with {len(job.leads)} "
                            f"number(s) via agent '{job.agent.name}'")
            jobs.append(job)

        if not jobs:
            return

        progress_clock = [time.monotonic()]

        def on_started(cl: CampaignLead, run: Dict[str, Any], attempt: int) -> None:
            item: _Item = cl.payload
            retry = f" (retry #{attempt - 1})" if attempt > 1 else ""
            log("call", f"-> Calling {cl.phone}{retry} ({item.label}) [{item.campaign_name}, run {run.get('id')}]")

        def on_result(cl: CampaignLead, outcome: CallOutcome) -> None:
            item: _Item = cl.payload
            item.outcome = outcome
            if outcome.status == "called":
                dur = f"{outcome.duration_seconds:.0f}s" if outcome.duration_seconds is not None else "-"
                tries = f" | attempts: {outcome.attempts}" if outcome.attempts > 1 else ""
                moved = " | TRANSFERRED" if is_transferred(outcome.raw) else ""
                log("call", f"<- {cl.phone} Result: {outcome.disposition or 'unknown'} | {dur} | "
                            f"tags: {', '.join(map(str, outcome.tags)) or '-'}{tries}{moved}")
            else:
                log("call", f"<- {cl.phone} {outcome.status.upper()}: {outcome.error}")
            self._finish_nowait(item, outcome.status, outcome.error or "")

        async def follow(job: CampaignJob) -> None:
            await job.monitor(self.dograh, on_started, on_result, progress_clock=progress_clock)
            # Every lead has its final result: make sure Dograh isn't left "running"
            try:
                state = await asyncio.to_thread(self.dograh.stop_campaign, job.campaign_id)
                self._live_campaigns.pop(job.campaign_id, None)
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) finished - "
                                f"{'paused' if state == 'paused' else state}")
            except Exception as e:
                log("campaign", f"'{job.name}' (campaign {job.campaign_id}) finished - "
                                f"could not pause it now, will retry on exit: {e}")

        await asyncio.gather(*(follow(job) for job in jobs))

    async def _writer_worker(self) -> None:
        warned_locked = False
        warned_transfers_locked = False
        while True:
            first = await self._results_q.get()
            batch = [first]
            while True:
                try:
                    batch.append(self._results_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                def append_batch() -> None:
                    for row, lead in batch:
                        self.store.append(row, lead)
                        if row.get("call_transferred"):
                            t_row, rest = transfer_row(row, lead or {})
                            self.transfers.append(t_row, rest)

                await asyncio.to_thread(append_batch)
                if self._results_q.empty():  # batch saves while rows keep arriving
                    if not await asyncio.to_thread(self.transfers.save):
                        if not warned_transfers_locked:
                            log("results", f"WARNING: {self.transfers.path.name} is open in another program; "
                                           "will keep retrying - close it to let transfers save")
                            warned_transfers_locked = True
                    else:
                        warned_transfers_locked = False
                    if not await asyncio.to_thread(self.store.save):
                        if not warned_locked:
                            log("results", f"WARNING: {self.store.path.name} is open in another program; "
                                           "will keep retrying - close it to let results save")
                            warned_locked = True
                    else:
                        warned_locked = False
            except Exception as e:
                log("results", f"ERROR writing row: {e}")
            finally:
                for _ in batch:
                    self._results_q.task_done()

    # ------------------------------------------------------------------ helpers

    def _finish_nowait(self, item: _Item, status: str, detail: str = "") -> None:
        item.status, item.detail = status, detail
        self.stats[status.split(" ")[0]] += 1
        if item.outcome is not None and is_transferred(item.outcome.raw):
            self.stats["transferred"] += 1
        if status in ("called", "call_timeout") and item.verification and item.verification.e164:
            self.verifier.mark_called(item.verification.e164)
        self._results_q.put_nowait((self._row(item), item.lead))

    async def _finish(self, item: _Item, status: str, detail: str = "") -> None:
        self._finish_nowait(item, status, detail)

    def _row(self, item: _Item) -> Dict[str, Any]:
        """This program's columns for one lead (the lead's own fields are added by the results store)."""
        v, d, o = item.verification, item.decision, item.outcome
        row: Dict[str, Any] = {
            "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": item.status,
            "detail": item.detail,
            "phone_raw": v.raw if v else None,
            "phone_e164": v.e164 if v else None,
            "phone_location": (v.location or None) if v else None,
            "phone_timezone": (v.time_zone or None) if v else None,
        }
        if d:
            row["interested_service"] = item.service.name if item.service else None
            row["interest_probability"] = round(d.probability, 4) if d.probabilities else None
            for sid, p in d.probabilities.items():
                row[f"prob_{sid}"] = round(p, 4)
        if item.agent:
            row["agent_id"] = item.agent.id
            row["agent_name"] = item.agent.name
        if item.campaign_name:
            row["campaign_id"] = item.campaign_id
            row["campaign_name"] = item.campaign_name
        if o:
            row.update({
                "dograh_run_id": o.run_id,
                "call_attempts": o.attempts or None,
                "call_disposition": o.disposition,
                "call_duration_s": o.duration_seconds,
                "call_tags": ", ".join(map(str, o.tags)) if o.tags else None,
                "transcript_url": o.transcript_url,
                "recording_url": o.recording_url,
                "dograh_response": o.raw or None,
                "call_transferred": True if is_transferred(o.raw) else None,
            })
        return row

    async def _final_save(self, store: ResultsStore) -> None:
        for attempt in range(12):
            if await asyncio.to_thread(store.save):
                return
            if attempt == 0:
                log("results", f"{store.path.name} is open in another program - close it to save "
                               "(retrying for 60s)")
            await asyncio.sleep(_STOP_TIMEOUT)
        fallback = store.path.with_name(
            f"{store.path.stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx")
        store.path = fallback
        await asyncio.to_thread(store.save)
        log("results", f"Saved to {fallback} instead")

    def _print_summary(self) -> None:
        print("-" * 100, flush=True)
        s = self.stats
        dropped = sum(n for k, n in s.items() if k.startswith("dropped"))
        print(f"Done. Leads received: {s['received']} | dropped by verifier: {dropped} | "
              f"not interested: {s['not_interested']} | called: {s['called']} | "
              f"call failed/timeout: {s['call_failed'] + s['call_timeout']} | "
              f"skipped (missing data): {s['skipped_missing_data']} | "
              f"errors: {s['decision_error'] + s['error']}")
        print(f"Results: {self.store.path}", flush=True)
        if s["transferred"]:
            print(f"Transferred calls: {s['transferred']} -> {self.transfers.path}", flush=True)


async def run_leads(
    leads: Union[Iterable[Dict[str, Any]], AsyncIterable[Dict[str, Any]]],
    batch_size: Optional[int] = None,
    **pipeline_kwargs: Any,
) -> Counter:
    """
    Run leads (list, generator or async generator of dicts) through the pipeline
    in batches of `batch_size` (default LEAD_BATCH_SIZE = 300): each batch is
    verified, scored, put into campaigns and fully called before the next one.
    """
    size = max(1, batch_size or config.LEAD_BATCH_SIZE)
    total = len(leads) if hasattr(leads, "__len__") else None  # type: ignore[arg-type]
    async with LeadPipeline(**pipeline_kwargs) as pipeline:
        count = 0

        async def add(lead: Dict[str, Any]) -> None:
            nonlocal count
            if count % size == 0:
                last = count + size if total is None else min(count + size, total)
                log("batch", f"Batch {count // size + 1}: leads {count + 1}-{last}"
                             + (f" of {total}" if total is not None else ""))
            await pipeline.submit(lead)
            count += 1
            if count % size == 0:
                await pipeline.flush()      # call this batch completely before taking more

        if hasattr(leads, "__aiter__"):
            async for lead in leads:  # type: ignore[union-attr]
                await add(lead)
        else:
            for lead in leads:  # type: ignore[union-attr]
                await add(lead)
    return pipeline.stats


def run_leads_sync(leads: Iterable[Dict[str, Any]], batch_size: Optional[int] = None,
                   **pipeline_kwargs: Any) -> Counter:
    """Blocking version of run_leads() for plain (non-async) scripts."""
    return asyncio.run(run_leads(leads, batch_size=batch_size, **pipeline_kwargs))
