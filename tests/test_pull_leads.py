"""Offline tests for pull_leads.py: a local HTTP server stands in for the lead streamer,
and the pipeline is replaced by a fake, so nobody is called."""

import gzip
import http.server
import json
import threading
from collections import Counter

import pytest

import config
import pull_leads
from pull_leads import BATCH, EMPTY, FATAL, RETRY, BatchStore, PingResult, Puller, ping

TOKEN = "s" * 48


# ------------------------------------------------------------------ fake streamer

class FakeStreamer:
    """Answers each GET with the next queued (status, headers, body) and records the requests."""

    def __init__(self):
        self.answers = []
        self.requests = []
        streamer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                streamer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                          "accept_encoding": self.headers.get("Accept-Encoding", "")})
                status, headers, body = streamer.answers.pop(0)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def queue_json(self, status, data, gz=False, headers=None):
        body = json.dumps(data).encode()
        h = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
        if gz:
            body = gzip.compress(body)
            h["Content-Encoding"] = "gzip"
        self.answers.append((status, h, body))

    def queue_html(self, status, html="<html><title>404 Not Found</title></html>"):
        self.answers.append((status, {"Content-Type": "text/html"}, html.encode()))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def streamer():
    s = FakeStreamer()
    yield s
    s.close()


def _leads(n, start=0):
    return [{"lead_id": f"L{i}", "content_version": 1, "event": "new", "company": f"Org {i}",
             "phone": "(512) 555-0101", "category": "General Contractors"} for i in range(start, start + n)]


def _ok(leads):
    return {"status": "ok", "count": len(leads), "new": len(leads), "updated": 0,
            "served_at": "2026-11-20T10:00:00Z", "leads": leads}


# ------------------------------------------------------------------ ping()

def test_ping_ok_gzip_batch_of_300(streamer):
    streamer.queue_json(200, _ok(_leads(300)), gz=True)
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == BATCH and len(r.leads) == 300
    assert r.meta["count"] == 300 and r.meta["served_at"] == "2026-11-20T10:00:00Z"
    req = streamer.requests[0]
    assert req["path"] == "/leads/next" and req["auth"] == f"Bearer {TOKEN}"
    assert "gzip" in req["accept_encoding"]


def test_ping_no_new_leads(streamer):
    streamer.queue_json(200, {"status": "no_new_leads", "message": "No new Leads available",
                              "count": 0, "leads": []})
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == EMPTY and "No new Leads" in r.message


@pytest.mark.parametrize("status,body,kind,text", [
    (401, {"status": "error", "error": "unauthorized"}, FATAL, "token"),
    (421, {"status": "error", "error": "wrong_host"}, FATAL, "PUBLIC_BASE_URL"),
    (503, {"status": "error", "error": "busy", "retry_after_seconds": 5}, RETRY, "Wait 5 seconds"),
    (503, {"status": "error", "error": "database_unavailable", "retry_after_seconds": 30}, RETRY, "30"),
    (500, {"status": "error", "error": "internal_error"}, RETRY, "internal_error"),
    (200, {"status": "ok", "leads": "nope"}, RETRY, "no 'leads' list"),
    (200, {"status": "weird"}, RETRY, "Unexpected status"),
])
def test_ping_errors(streamer, status, body, kind, text):
    streamer.queue_json(status, body)
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == kind and text in r.message and not r.leads


def test_ping_html_pages_are_not_mistaken_for_leads(streamer):
    streamer.queue_html(404)                                   # what ai-sdr.grobots.ai/leads/next gives today
    assert ping(streamer.url, TOKEN, 120).kind == RETRY
    streamer.queue_html(200, "<h1>CyberPanel Installed</h1>")  # a default site page
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == RETRY and "not JSON" in r.message


def test_ping_unreachable():
    r = ping("http://127.0.0.1:9", TOKEN, 120)
    assert r.kind == RETRY and "Could not reach" in r.message


def test_ping_skips_non_object_items(streamer):
    streamer.queue_json(200, _ok(_leads(2) + ["x", 5]))
    r = ping(streamer.url, TOKEN, 120)
    assert r.kind == BATCH and len(r.leads) == 2 and "2 item(s)" in r.message


# ------------------------------------------------------------------ the loop

class Script:
    """Scripted answers for the prompts; records every prompt and event in order."""

    def __init__(self, answers, events):
        self.answers = list(answers)
        self.events = events

    def __call__(self, prompt):
        self.events.append(("ask", prompt.strip().splitlines()[0][:40]))
        return self.answers.pop(0) if self.answers else "q"


def make_puller(tmp_path, pings, answers, process=None, events=None):
    events = [] if events is None else events
    pings = list(pings)

    def fake_ping(url, token, timeout):
        events.append(("ping",))
        return pings.pop(0)

    def fake_process(leads):
        events.append(("process", len(leads)))
        return Counter(received=len(leads))

    p = Puller(source_url="https://ai-sdr.grobots.ai", token=TOKEN, timeout=180,
               store=BatchStore(tmp_path / "pulled"), process=process or fake_process,
               ask_fn=Script(answers, events), ping_fn=fake_ping)
    return p, events


def kinds(events):
    return [e[0] for e in events]


def test_waits_for_a_person_after_every_batch(tmp_path):
    pings = [PingResult(BATCH, "Received 300", _leads(300)), PingResult(BATCH, "Received 120", _leads(120))]
    p, events = make_puller(tmp_path, pings, answers=["", "q"])
    assert p.run() == 0
    # ping -> process fully -> ask -> (Enter) ping -> process -> ask -> (q) stop
    assert kinds(events) == ["ping", "process", "ask", "ping", "process", "ask"]
    assert [e[1] for e in events if e[0] == "process"] == [300, 120]
    done = sorted((tmp_path / "pulled").glob("*.done.json"))
    assert len(done) == 2 and not list((tmp_path / "pulled").glob("*.pending.json"))
    assert len(json.loads(done[0].read_text(encoding="utf-8"))["leads"]) in (300, 120)


def test_quit_after_first_batch_means_exactly_one_ping(tmp_path):
    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(300))], answers=["q"])
    assert p.run() == 0
    assert kinds(events).count("ping") == 1


def test_no_new_leads_and_errors_also_wait_for_a_person(tmp_path):
    pings = [PingResult(EMPTY, "No new Leads available"), PingResult(RETRY, "HTTP 503"),
             PingResult(BATCH, "x", _leads(3))]
    p, events = make_puller(tmp_path, pings, answers=["", "", "q"])
    assert p.run() == 0
    assert kinds(events) == ["ping", "ask", "ping", "ask", "ping", "process", "ask"]


def test_fatal_stops_without_asking(tmp_path):
    p, events = make_puller(tmp_path, [PingResult(FATAL, "HTTP 401")], answers=[])
    assert p.run() == 2
    assert kinds(events) == ["ping"]


def test_batch_is_on_disk_before_processing(tmp_path):
    seen = []

    def process(leads):
        seen.append(len(list((tmp_path / "pulled").glob("*.pending.json"))))
        return Counter()

    p, _ = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(5))], answers=["q"], process=process)
    p.run()
    assert seen == [1]


def test_processing_error_keeps_batch_and_can_be_retried(tmp_path):
    calls = []

    def flaky(leads):
        calls.append(len(leads))
        if len(calls) == 1:
            raise RuntimeError("Dograh rejected the API key (HTTP 401)")
        return Counter()

    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(7))], answers=["", "q"], process=flaky)
    assert p.run() == 0
    assert calls == [7, 7]                    # same batch retried, no new ping in between
    assert kinds(events).count("ping") == 1
    assert len(list((tmp_path / "pulled").glob("*.done.json"))) == 1


def test_ctrl_c_mid_batch_keeps_it_and_next_start_offers_to_resume(tmp_path):
    def interrupted(leads):
        raise KeyboardInterrupt

    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(9))], answers=[], process=interrupted)
    assert p.run() == 130
    assert len(list((tmp_path / "pulled").glob("*.pending.json"))) == 1
    assert "ask" not in kinds(events)

    # Next start: asked about the unfinished batch first, processes it, then pings
    p2, events2 = make_puller(tmp_path, [PingResult(EMPTY, "No new Leads available")], answers=["", "q"])
    assert p2.run() == 0
    assert kinds(events2) == ["ask", "process", "ping", "ask"]
    assert events2[1] == ("process", 9)
    assert len(list((tmp_path / "pulled").glob("*.done.json"))) == 1


def test_unfinished_batch_can_be_left_for_later(tmp_path):
    store = BatchStore(tmp_path / "pulled")
    store.save(PingResult(BATCH, "x", _leads(4)), "test")
    p, events = make_puller(tmp_path, [PingResult(EMPTY, "none")], answers=["l", "q"])
    assert p.run() == 0
    assert "process" not in kinds(events)
    assert len(list((tmp_path / "pulled").glob("*.pending.json"))) == 1


def test_oversized_batch_is_still_processed_whole(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "LEAD_BATCH_SIZE", 300)
    p, events = make_puller(tmp_path, [PingResult(BATCH, "x", _leads(1000))], answers=["q"])
    p.run()
    assert ("process", 1000) in events
    assert "BATCH_SIZE=300" in capsys.readouterr().out


def test_default_batch_size_is_300():
    assert config.LEAD_BATCH_SIZE == 300


def test_end_to_end_with_real_http(tmp_path, streamer):
    """Real HTTP + gzip through Puller.run(): two batches, one Enter in between."""
    streamer.queue_json(200, _ok(_leads(300)), gz=True)
    streamer.queue_json(200, {"status": "no_new_leads", "message": "No new Leads available", "leads": []})
    processed = []
    p = Puller(source_url=streamer.url, token=TOKEN, timeout=120, store=BatchStore(tmp_path / "pulled"),
               process=lambda leads: processed.append([l["lead_id"] for l in leads]) or Counter(),
               ask_fn=Script(["", "q"], []))
    assert p.run() == 0
    assert len(streamer.requests) == 2
    assert processed == [[f"L{i}" for i in range(300)]]


def test_preflight_requires_token(monkeypatch):
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "")
    assert "LEAD_SOURCE_TOKEN" in pull_leads.preflight()


# ------------------------------------------------------------------ deployment helpers

def test_stop_signals_become_ctrl_c():
    import signal
    saved = {s: signal.getsignal(s) for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s}
    try:
        handled = pull_leads.stop_signals_act_like_ctrl_c()
        assert signal.SIGTERM in handled
        with pytest.raises(KeyboardInterrupt):     # same path as Ctrl+C: campaigns get paused
            signal.raise_signal(signal.SIGTERM)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


def _stats():
    return {"status": "ok", "eligible": 650, "sent_current": 300, "updated_since_sent": 0, "never_sent": 350}


def test_check_passes_and_takes_no_leads(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", TOKEN)
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(200, _stats())
    assert pull_leads.check_setup(streamer.url) == 0
    assert [r["path"] for r in streamer.requests] == ["/health", "/stats"]   # never /leads/next
    assert "350 lead(s) never sent" in capsys.readouterr().out


def test_check_spots_a_server_that_is_not_the_streamer(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    streamer.queue_json(200, {"status": "ok"})          # e.g. the old receiver on ai-sdr.grobots.ai
    assert pull_leads.check_setup(streamer.url) == 1
    assert "not the lead streamer" in capsys.readouterr().out


def test_check_reports_wrong_token(streamer, monkeypatch, capsys):
    monkeypatch.setattr(pull_leads, "preflight", lambda: None)
    monkeypatch.setattr(config, "LEAD_SOURCE_TOKEN", "wrong")
    streamer.queue_json(200, {"status": "alive", "database": "ok"})
    streamer.queue_json(401, {"status": "error", "error": "unauthorized"})
    assert pull_leads.check_setup(streamer.url) == 1
    assert "rejected" in capsys.readouterr().out


def test_after_hangup_output_is_silenced_so_cleanup_can_finish(monkeypatch):
    import sys

    class DeadTerminal:
        def write(self, *_):
            raise OSError(5, "Input/output error")   # what a vanished SSH terminal does

        def flush(self):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stdout", DeadTerminal())
    monkeypatch.setattr(sys, "stderr", DeadTerminal())
    with pytest.raises(OSError):
        pull_leads.say("campaign paused")            # without the fix, cleanup logging would crash
    pull_leads._silence_output()
    pull_leads.say("campaign paused")                # now harmless
    print("summary line")
