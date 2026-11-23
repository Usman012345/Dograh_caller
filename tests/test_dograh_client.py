from types import SimpleNamespace

import dograh_client
from dograh_client import (
    DograhClient,
    DograhError,
    extract_pipeline_error,
    outcome_from_run,
)


def test_outcome_from_run_maps_failed_disposition_to_call_failed():
    run_failed = {
        "id": 101,
        "gathered_context": {"call_disposition": "failed"},
        "cost_info": {"call_duration_seconds": 0},
    }
    out = outcome_from_run(run_failed)
    assert out.status == "call_failed"
    assert out.disposition == "failed"
    assert out.duration_seconds == 0.0
    assert out.error and "failed" in out.error.lower()

    run_call_failed = {
        "id": 102,
        "gathered_context": {"call_disposition": "call_failed", "error": "SIP 503"},
    }
    out2 = outcome_from_run(run_call_failed)
    assert out2.status == "call_failed"
    assert "SIP 503" in (out2.error or "")


def test_outcome_from_run_duration_falls_back_to_usage_info():
    run_usage_only = {
        "id": 103,
        "gathered_context": {"call_disposition": "completed"},
        "cost_info": {},
        "usage_info": {"call_duration_seconds": 42.5},
    }
    out = outcome_from_run(run_usage_only)
    assert out.status == "called"
    assert out.duration_seconds == 42.5

    run_cost_none = {
        "id": 104,
        "gathered_context": {"call_disposition": "completed"},
        "cost_info": {"call_duration_seconds": None},
        "usage_info": {"call_duration_seconds": 17},
    }
    out2 = outcome_from_run(run_cost_none)
    assert out2.duration_seconds == 17.0


def test_extract_pipeline_error_and_dead_air_classification():
    tts_err = (
        "ElevenLabsTTSService#153 no BotStartedSpeakingFrame within 3.0s of pausing "
        "frame processing (e.g. a TTS context completed with no audio) — force-resuming"
    )
    run = {
        "id": 201,
        "gathered_context": {
            "call_disposition": "user_hangup",
            "nodes_visited": ["start"],
        },
        "usage_info": {"call_duration_seconds": 6, "llm_tokens": 110},
        "logs": {
            "realtime_feedback_events": [
                {"type": "rtf-user-transcription", "payload": {"text": "Hello?"}},
                {"type": "rtf-pipeline-error", "payload": {"error": tts_err}},
            ]
        },
    }
    assert extract_pipeline_error(run) == tts_err

    out = outcome_from_run(run)
    assert out.status == "call_failed"
    assert out.duration_seconds == 6.0
    assert "ElevenLabsTTSService#153" in (out.error or "")
    assert "no BotStartedSpeakingFrame" in (out.error or "")
    assert "logs" not in out.raw


def test_extract_pipeline_error_variants_and_real_conversation_preservation():
    # Alternate event shape with data.detail and no LLM usage
    run_generic = {
        "id": 202,
        "gathered_context": {"call_disposition": "Customer hung up"},
        "usage_info": {"call_duration_seconds": 4},
        "logs": {
            "realtime_feedback_events": [
                {"event": "rtf-pipeline-error", "data": {"detail": "Audio stream aborted"}}
            ]
        },
    }
    out_generic = outcome_from_run(run_generic)
    assert out_generic.status == "call_failed"
    assert "Audio stream aborted" in (out_generic.error or "")

    # Transferred call with an incidental non-fatal pipeline error stays called
    run_transferred = {
        "id": 203,
        "gathered_context": {
            "call_disposition": "transfer_call",
            "nodes_visited": ["start", "pitch", "transfer"],
        },
        "usage_info": {"call_duration_seconds": 95, "llm_tokens": 900},
        "logs": {
            "realtime_feedback_events": [
                {"type": "rtf-pipeline-error", "payload": {"message": "Disconnect warning"}}
            ]
        },
    }
    assert outcome_from_run(run_transferred).status == "called"

    # Qualified multi-node call with non-TTS warning stays called
    run_qualified = {
        "id": 204,
        "gathered_context": {
            "call_disposition": "user_qualified",
            "nodes_visited": ["start", "pitch", "qualify"],
        },
        "usage_info": {"call_duration_seconds": 80, "llm_tokens": 700},
        "logs": {
            "realtime_feedback_events": [
                {"type": "rtf-pipeline-error", "payload": {"message": "Trailing cleanup error"}}
            ]
        },
    }
    assert outcome_from_run(run_qualified).status == "called"

    # Missing / empty logs
    assert extract_pipeline_error({"id": 205, "logs": None}) is None
    assert extract_pipeline_error({"id": 206, "logs": {"realtime_feedback_events": None}}) is None


def test_get_workflow_run_and_pause_campaign_http_calls():
    calls = []

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if url.endswith("/workflow/2/runs/555"):
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"id": 555, "workflow_id": 2, "logs": {"realtime_feedback_events": []}},
                )
            if url.endswith("/campaign/99/pause"):
                return SimpleNamespace(status_code=200, json=lambda: {"status": "paused"})
            return SimpleNamespace(status_code=404, json=lambda: {"detail": "not found"}, text="not found")

        def close(self):
            pass

    orig_session_cls = dograh_client.requests.Session
    dograh_client.requests.Session = FakeSession
    try:
        client = DograhClient(base_url="https://example.test/api/v1", api_key="secret")
        detail = client.get_workflow_run(2, 555)
        assert detail["id"] == 555
        assert client.pause_campaign(99) == "paused"
        try:
            client.get_workflow_run(2, 999)
            raise AssertionError("Expected DograhError for 404")
        except DograhError as err:
            assert "404" in str(err)
        client.close()
    finally:
        dograh_client.requests.Session = orig_session_cls

    assert calls == [
        ("GET", "https://example.test/api/v1/workflow/2/runs/555"),
        ("POST", "https://example.test/api/v1/campaign/99/pause"),
        ("GET", "https://example.test/api/v1/workflow/2/runs/999"),
    ]
