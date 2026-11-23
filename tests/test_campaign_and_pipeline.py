import asyncio
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import config
from campaign_manager import CampaignJob, CampaignLead, _ts
from decision_maker import Decision, Service
from dograh_client import Agent, DograhClient
from pipeline import LeadPipeline


def test_ts_treats_naive_isoformat_as_utc():
    naive = _ts("2026-11-19T12:00:00")
    aware = _ts("2026-11-19T12:00:00Z")
    assert naive == aware == datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    assert _ts(None) is None
    assert _ts("not-a-timestamp") is None


def test_retry_pending_uses_completed_mono_and_safe_fallback(monkeypatch):
    monkeypatch.setattr(config, "RETRY_GRACE_SECONDS", 10)
    cfg = {"enabled": True, "max_retries": 1, "retry_delay_seconds": 30, "retry_on_no_answer": True}
    latest = {"gathered_context": {"call_disposition": "no-answer"}, "is_completed": True}

    now_mono = time.monotonic()
    # Within retry window measured from completion monotonic time
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg, completed_mono=now_mono - 5) is True
    # Past retry window
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg, completed_mono=now_mono - 45) is False
    # Max retries exhausted
    assert CampaignJob._retry_pending(latest, attempts=2, retry_cfg=cfg, completed_mono=now_mono - 5) is False
    # Missing timestamps in standalone mode must not hang forever
    assert CampaignJob._retry_pending(latest, attempts=1, retry_cfg=cfg) is False


def test_monitor_shared_progress_clock_and_undialed_vs_dialed_timeout(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "CAMPAIGN_IDLE_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)

    svc = Service(id="svc", name="Svc", agent_id=1, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=1, name="Agent", version_status="published")
    lead_dialed = CampaignLead(ref="L00001", phone="+15125550101", lead={"company": "Dialed Co"})
    lead_undialed = CampaignLead(ref="L00002", phone="+15125550102", lead={"company": "Undialed Co"})
    job = CampaignJob(svc, agent, [lead_dialed, lead_undialed])
    job.campaign_id = 10

    class FakeClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            # L00001 started a run that never completes; L00002 has never been dialed.
            return [
                {
                    "id": 99,
                    "is_completed": False,
                    "initial_context": {"lead_ref": "L00001", "phone_number": "+15125550101"},
                }
            ]

    shared_clock = [time.monotonic()]
    results = {}

    async def run():
        # Verify shared progress_clock keeps sibling monitor alive past single-campaign idle timeout
        async def bump_clock():
            for _ in range(4):
                await asyncio.sleep(0.03)
                shared_clock[0] = time.monotonic()

        bumper = asyncio.create_task(bump_clock())
        await job.monitor(
            FakeClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
            progress_clock=shared_clock,
        )
        await bumper

    asyncio.run(run())
    assert results["L00001"].status == "call_timeout"
    assert results["L00002"].status == "call_failed"
    assert "Not dialed" in (results["L00002"].error or "")


def test_monitor_exits_on_external_paused_or_cancelled_state(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    svc = Service(id="svc", name="Svc", agent_id=1, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=1, name="Agent", version_status="published")
    lead = CampaignLead(ref="L00001", phone="+15125550101", lead={"company": "Acme"})
    job = CampaignJob(svc, agent, [lead])
    job.campaign_id = 7

    class PausedClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "paused", "retry_config": {}}

        def get_campaign_runs(self, cid):
            return []

    results = {}
    asyncio.run(
        job.monitor(
            PausedClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert results["L00001"].status == "call_failed"
    assert "paused" in (results["L00001"].error or "")


def test_dograh_client_uses_thread_local_sessions_and_closes_all():
    created = []

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.closed = False
            created.append(self)

        def request(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"id": 1, "name": "A"})

        def close(self):
            self.closed = True

    import dograh_client

    orig_session_cls = dograh_client.requests.Session
    dograh_client.requests.Session = FakeSession
    try:
        client = DograhClient(base_url="https://example.test/api/v1", api_key="secret-key")
        sessions_seen = []

        def worker():
            sessions_seen.append(client._get_session())
            client.get_agent(1)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(created) == 4
        assert len({id(s) for s in sessions_seen}) == 4
        client.close()
        assert all(s.closed for s in created)
    finally:
        dograh_client.requests.Session = orig_session_cls


def test_pipeline_abort_drains_queued_results_and_stops_campaigns(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    stopped_campaigns = []

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def stop_campaign(self, cid):
            stopped_campaigns.append(cid)
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"draftliner": 0.10}, service=None, probability=0.10)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        pipeline = LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tmp_path / "tracker.xlsx",
        )
        await pipeline.start()
        pipeline._live_campaigns[42] = "Draftliner_2026-11-19"
        await pipeline.submit({"company": "Test Bistro", "category": "Restaurant", "phone": "(512) 555-0101"})
        # Wait until verifier + decider push the not_interested row into _results_q
        await pipeline._verify_q.join()
        await pipeline._decision_q.join()
        # Simulate Ctrl+C / abort right after scoring
        await pipeline.close(abort=True)

    asyncio.run(run())
    assert stopped_campaigns == [42]
    assert results_xlsx.exists()


def test_all_skipped_campaign_does_not_log_or_raise_failed_start(tmp_path, monkeypatch, capsys):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    tracker_xlsx = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(config, "TRANSFERS_FILE", tmp_path / "transfers.xlsx")

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def get_required_variables(self, agent_id):
            return {"city"}  # lead lacks city -> every lead skipped

        def upload_csv(self, *args, **kwargs):
            raise AssertionError("upload_csv must not be called when all leads are skipped")

        def create_campaign(self, *args, **kwargs):
            raise AssertionError("create_campaign must not be called when all leads are skipped")

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"draftliner": 0.95},
                service=self.services[0],
                probability=0.95,
                threshold=0.80,
                model="gemini-3.8-flash",
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        async with LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tracker_xlsx,
        ) as p:
            await p.submit({"company": "No City Co", "phone": "(512) 555-0101"})
            return p.stats

    stats = asyncio.run(run())
    out = capsys.readouterr().out
    assert "FAILED to start" not in out
    assert stats["skipped_missing_data"] == 1
    assert stats["call_failed"] == 0


def test_stop_event_interrupts_pipeline_and_pauses_campaign(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"
    tracker_xlsx = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(config, "TRANSFERS_FILE", tmp_path / "transfers.xlsx")
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.02)

    stop_event = threading.Event()
    stopped_campaigns = []

    class FakeDograh:
        def get_agent(self, agent_id):
            return Agent(id=agent_id, name="Danial", version_status="published")

        def get_required_variables(self, agent_id):
            return set()

        def upload_csv(self, rows, file_name):
            return "file-key"

        def create_campaign(self, name, agent_id, file_key):
            return {"id": 88, "name": name}

        def start_campaign(self, cid):
            pass

        def get_campaign(self, cid):
            stop_event.set()
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            return []

        def stop_campaign(self, cid):
            stopped_campaigns.append(cid)
            return "paused"

        def close(self):
            pass

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(
                probabilities={"draftliner": 0.91},
                service=self.services[0],
                probability=0.91,
                threshold=0.80,
                model="gemini-3.8-flash",
            )

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DograhClient", FakeDograh)
    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        async with LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            tracker_file=tracker_xlsx,
            stop_event=stop_event,
        ) as p:
            await p.submit({"company": "Stop Me Co", "phone": "(512) 555-0102"})
            return p.stats

    stats = asyncio.run(run())
    assert 88 in stopped_campaigns
    assert stats["interrupted"] == 1

    from openpyxl import load_workbook
    wb = load_workbook(tracker_xlsx, read_only=True, data_only=True)
    try:
        rows = list(wb["leads"].iter_rows(values_only=True))
        header = rows[0]
        row = dict(zip(header, rows[1]))
        assert row["stage"] == "interrupted"
        assert row["status_detail"] == "Stopped from dashboard"
    finally:
        wb.close()


def test_close_drains_pending_results_when_join_interrupted(tmp_path, monkeypatch):
    services_yaml = tmp_path / "services.yaml"
    services_yaml.write_text(
        "services:\n"
        "  - id: draftliner\n"
        "    name: Draftliner\n"
        "    agent_id: 2\n"
        "    description: AEC staffing\n",
        encoding="utf-8",
    )
    results_xlsx = tmp_path / "results.xlsx"

    class FakeDecider:
        def __init__(self, services, threshold, api_key, model):
            self.services = services

        async def decide(self, lead):
            return Decision(probabilities={"draftliner": 0.1}, service=None, probability=0.1)

        async def close(self):
            pass

    monkeypatch.setattr("pipeline.DecisionMaker", FakeDecider)

    async def run():
        p = LeadPipeline(
            results_file=results_xlsx,
            services_file=services_yaml,
            score_only=True,
            tracker_file=tmp_path / "tracker.xlsx",
        )
        await p.start()
        await p._results_q.put((
            {"processed_at": "2026-11-23 12:00:00", "status": "not_interested", "phone_e164": "+15125550177"},
            {"company": "Drained Co"},
        ))

        async def interrupt_join():
            raise KeyboardInterrupt()

        monkeypatch.setattr(p._results_q, "join", interrupt_join)
        await p.close(abort=True)

    asyncio.run(run())
    from openpyxl import load_workbook
    wb = load_workbook(results_xlsx, read_only=True, data_only=True)
    try:
        rows = list(wb.active.iter_rows(values_only=True))
        assert any("Drained Co" in map(str, r) for r in rows)
    finally:
        wb.close()


def test_monitor_ignores_late_retries_for_already_finished_lead(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(config, "RETRY_GRACE_SECONDS", 0)

    svc = Service(id="svc", name="Svc", agent_id=2, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=2, name="Agent", version_status="published")
    lead1 = CampaignLead(ref="L000001", phone="+15125550101", lead={"company": "Fictional One"})
    lead2 = CampaignLead(ref="L000002", phone="+15125550102", lead={"company": "Fictional Two"})
    job = CampaignJob(svc, agent, [lead1, lead2])
    job.campaign_id = 12

    poll = 0

    class LateRetryClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            nonlocal poll
            poll += 1
            runs = [
                {
                    "id": 1001,
                    "is_completed": True,
                    "initial_context": {"lead_ref": "L000001", "phone_number": "+15125550101"},
                    "gathered_context": {"call_disposition": "no-answer"},
                }
            ]
            if poll >= 2:
                # Late retry arrives for already-finished L000001, alongside L000002 completing
                runs.append({
                    "id": 1002,
                    "is_completed": False,
                    "initial_context": {"lead_ref": "L000001", "phone_number": "+15125550101"},
                })
                runs.append({
                    "id": 1003,
                    "is_completed": True,
                    "initial_context": {"lead_ref": "L000002", "phone_number": "+15125550102"},
                    "gathered_context": {"call_disposition": "completed"},
                })
            return runs

    started = []
    results = {}
    asyncio.run(
        job.monitor(
            LateRetryClient(),
            lambda cl, r, att: started.append((cl.ref, r.get("id"), att)),
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert ("L000001", 1002, 2) not in started
    assert results["L000001"].run_id == 1001
    assert results["L000002"].run_id == 1003


def test_monitor_enriches_user_hangup_from_get_workflow_run_and_handles_failures(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)

    svc = Service(id="svc", name="Svc", agent_id=2, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=2, name="Agent", version_status="published")
    lead1 = CampaignLead(ref="L000001", phone="+15125550101", lead={"company": "Fictional DeadAir"})
    lead2 = CampaignLead(ref="L000002", phone="+15125550102", lead={"company": "Fictional ErrorDetail"})
    job = CampaignJob(svc, agent, [lead1, lead2])
    job.campaign_id = 13

    tts_err = "ElevenLabsTTSService#153 no BotStartedSpeakingFrame within 3.0s"
    detail_calls = []

    class EnrichClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "workflow_id": 2, "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            return [
                {
                    "id": 2001,
                    "is_completed": True,
                    "initial_context": {"lead_ref": "L000001", "phone_number": "+15125550101"},
                    "gathered_context": {"call_disposition": "user_hangup"},
                    "usage_info": {"call_duration_seconds": 5},
                },
                {
                    "id": 2002,
                    "is_completed": True,
                    "initial_context": {"lead_ref": "L000002", "phone_number": "+15125550102"},
                    "gathered_context": {"call_disposition": "failed"},
                },
            ]

        def get_workflow_run(self, workflow_id, run_id):
            detail_calls.append((workflow_id, run_id))
            if run_id == 2001:
                return {
                    "id": 2001,
                    "workflow_id": workflow_id,
                    "logs": {
                        "realtime_feedback_events": [
                            {"type": "rtf-pipeline-error", "payload": {"error": tts_err}}
                        ]
                    },
                }
            raise RuntimeError("transient lookup failure")

    results = {}
    asyncio.run(
        job.monitor(
            EnrichClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert detail_calls == [(2, 2001), (2, 2002)]
    assert results["L000001"].status == "call_failed"
    assert results["L000001"].duration_seconds == 5.0
    assert "ElevenLabsTTSService#153" in (results["L000001"].error or "")
    # Lookup failure for 2002 falls back cleanly and still classifies disposition="failed" as call_failed
    assert results["L000002"].status == "call_failed"


def test_monitor_circuit_breaker_pauses_campaign_after_consecutive_tts_errors(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(config, "CAMPAIGN_PIPELINE_ERROR_LIMIT", 3)

    svc = Service(id="svc", name="Svc", agent_id=2, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=2, name="Agent", version_status="published")
    leads = [
        CampaignLead(ref=f"L00000{i}", phone=f"+1512555010{i}", lead={"company": f"Fictional {i}"})
        for i in range(1, 6)
    ]
    job = CampaignJob(svc, agent, leads)
    job.campaign_id = 14

    paused_campaigns = []

    class BrokenTtsClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "workflow_id": 2, "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            # Leads 1, 2, 3 complete with the same TTS error (different instance #); leads 4, 5 not started.
            return [
                {
                    "id": 3000 + i,
                    "is_completed": True,
                    "initial_context": {"lead_ref": f"L00000{i}", "phone_number": f"+1512555010{i}"},
                    "gathered_context": {"call_disposition": "user_hangup"},
                    "usage_info": {"call_duration_seconds": 4},
                }
                for i in (1, 2, 3)
            ]

        def get_workflow_run(self, workflow_id, run_id):
            return {
                "id": run_id,
                "workflow_id": workflow_id,
                "logs": {
                    "realtime_feedback_events": [
                        {
                            "type": "rtf-pipeline-error",
                            "payload": {
                                "error": (
                                    f"ElevenLabsTTSService#{150 + run_id % 10} no BotStartedSpeakingFrame "
                                    "within 3.0s of pausing frame processing"
                                )
                            },
                        }
                    ]
                },
            }

        def pause_campaign(self, cid):
            paused_campaigns.append(cid)
            return "paused"

    results = {}
    asyncio.run(
        job.monitor(
            BrokenTtsClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert paused_campaigns == [14]
    assert len(results) == 5
    for i in (1, 2, 3):
        assert results[f"L00000{i}"].status == "call_failed"
        assert results[f"L00000{i}"].run_id == 3000 + i
        assert "no BotStartedSpeakingFrame" in (results[f"L00000{i}"].error or "")
    for i in (4, 5):
        assert results[f"L00000{i}"].status == "call_failed"
        assert results[f"L00000{i}"].run_id is None
        assert "no BotStartedSpeakingFrame" in (results[f"L00000{i}"].error or "")


def test_monitor_circuit_breaker_resets_on_non_pipeline_outcome(monkeypatch):
    monkeypatch.setattr(config, "CAMPAIGN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(config, "RESULT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(config, "CAMPAIGN_PIPELINE_ERROR_LIMIT", 3)

    svc = Service(id="svc", name="Svc", agent_id=2, description="d", good_fit="", not_a_fit="")
    agent = Agent(id=2, name="Agent", version_status="published")
    leads = [
        CampaignLead(ref=f"L00000{i}", phone=f"+1512555011{i}", lead={"company": f"Fictional {i}"})
        for i in range(1, 6)
    ]
    job = CampaignJob(svc, agent, leads)
    job.campaign_id = 15
    paused = []

    class IntermittentClient:
        def get_campaign(self, cid):
            return {"id": cid, "state": "running", "workflow_id": 2, "retry_config": {"enabled": False}}

        def get_campaign_runs(self, cid):
            # 1 & 2: TTS error, 3: normal called, 4 & 5: TTS error -> never 3 consecutive!
            disps = {1: "user_hangup", 2: "user_hangup", 3: "completed", 4: "user_hangup", 5: "user_hangup"}
            return [
                {
                    "id": 4000 + i,
                    "is_completed": True,
                    "initial_context": {"lead_ref": f"L00000{i}", "phone_number": f"+1512555011{i}"},
                    "gathered_context": {"call_disposition": disps[i]},
                    "usage_info": {"call_duration_seconds": 60 if i == 3 else 5},
                }
                for i in range(1, 6)
            ]

        def get_workflow_run(self, workflow_id, run_id):
            if run_id == 4003:
                return {"id": run_id, "logs": {"realtime_feedback_events": []}}
            return {
                "id": run_id,
                "logs": {
                    "realtime_feedback_events": [
                        {
                            "type": "rtf-pipeline-error",
                            "payload": {"error": "ElevenLabsTTSService#153 no BotStartedSpeakingFrame"},
                        }
                    ]
                },
            }

        def pause_campaign(self, cid):
            paused.append(cid)
            return "paused"

    results = {}
    asyncio.run(
        job.monitor(
            IntermittentClient(),
            lambda cl, r, att: None,
            lambda cl, outcome: results.__setitem__(cl.ref, outcome),
        )
    )
    assert paused == []
    assert results["L000003"].status == "called"
    assert results["L000001"].status == "call_failed"
    assert results["L000005"].status == "call_failed"
