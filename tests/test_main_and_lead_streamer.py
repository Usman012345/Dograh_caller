import http
import importlib.util
import sys
from pathlib import Path

import main as caller_main


def _load_lead_streamer():
    import pytest
    pytest.importorskip("psycopg", reason="lead streamer tests need psycopg (pip install -r requirements-dev.txt)")
    name = "lead_streamer_mod"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        Path(__file__).resolve().parent.parent / "lead_streamer" / "lead_streamer.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_main_read_leads_filters_blank_csv_rows_and_validates_limit(tmp_path):
    csv_path = tmp_path / "leads.csv"
    csv_path.write_text(
        "company,phone\n"
        "Acme Builders,(512) 555-0101\n"
        " , \n"
        ",\n"
        "Beta Roofing,(512) 555-0102\n",
        encoding="utf-8",
    )
    leads = caller_main.read_leads(csv_path)
    assert len(leads) == 2
    assert [l["company"] for l in leads] == ["Acme Builders", "Beta Roofing"]

    assert caller_main.run(leads, limit=0) == 1
    assert caller_main.run(leads, limit=-2) == 1


def test_lead_streamer_explicit_http_and_eligible_sql_placeholders():
    ls = _load_lead_streamer()
    assert ls.http is http
    assert "NOT IN ('none', 'null', 'n/a')" in ls._ELIGIBLE_SQL


def test_lead_streamer_flatten_extra_filters_camelcase_and_spaced_keys():
    ls = _load_lead_streamer()
    merged = ls._merge_extra_sources(
        {"Extra": {"companyName": "Dup Co", "retryCount": 3, "Years In Business": "12"}},
        {"Email Status": "VALID", "servicesOffered": "Concrete"},
    )
    flattened = dict(ls._flatten_extra(merged))
    assert "company_name" not in flattened
    assert "retry_count" not in flattened
    assert "email_status" not in flattened
    assert flattened["years_in_business"] == "12"
    assert flattened["services_offered"] == "Concrete"


def test_lead_streamer_prevents_cross_contact_phone_and_email_bleed():
    ls = _load_lead_streamer()

    def make_row(contact_id):
        # 37-element row matching _build_lead_payload unpacking
        return (
            f"lead-{contact_id or 'org'}",  # lead_id
            "org-1",                        # org_id
            contact_id,                     # contact_id
            "company",                      # record_kind
            None,                           # lead_title
            "Concrete",                     # lead_category
            "Austin",                       # lead_city
            "TX",                           # lead_us_state
            "bbb",                          # source_code
            None,                           # source_url
            None,                           # due_at
            {},                             # raw_meta
            1,                              # content_version
            None,                           # created_at
            None,                           # updated_at
            None,                           # lead_notes
            None,                           # lead_status
            None,                           # lead_last_activity
            None,                           # lead_next_follow_up
            None,                           # lead_external_id
            None,                           # lead_first_seen_at
            None,                           # lead_last_seen_at
            "Acme Concrete",                # org_name
            None,                           # org_website
            None,                           # org_industry
            None,                           # org_domain
            None,                           # org_company_size
            None,                           # org_linkedin_url
            None,                           # org_raw_data
            f"Person {contact_id}" if contact_id else None,
            None,                           # contact_title
            None,                           # contact_first_name
            None,                           # contact_last_name
            None,                           # contact_department
            None,                           # contact_linkedin_url
            None,                           # contact_raw_data
            False,                          # was_sent
        )

    phones_by_contact = {"c1": ["+15125550101"], "c2": ["+15125550202"]}
    phones_by_org_direct = {"org-1": ["+15125550100"]}
    phones_by_org_all = {"org-1": ["+15125550101", "+15125550202", "+15125550100"]}

    emails_by_contact = {"c1": ["c1@acme.example"], "c2": ["c2@acme.example"]}
    emails_by_org_direct = {"org-1": ["info@acme.example"]}
    emails_by_org_all = {"org-1": ["c1@acme.example", "c2@acme.example", "info@acme.example"]}

    # Contact c1 gets its own + direct org phone/email, NOT c2's phone/email
    p_c1 = ls._build_lead_payload(
        make_row("c1"),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct=phones_by_org_direct,
        emails_by_org_direct=emails_by_org_direct,
        invalid_by_org_direct={},
    )
    assert p_c1["phones"] == ["+15125550101", "+15125550100"]
    assert p_c1["emails"] == ["c1@acme.example", "info@acme.example"]

    # Contact c3 with no own/direct phone falls back to org-wide pool
    p_c3 = ls._build_lead_payload(
        make_row("c3"),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct={},
        emails_by_org_direct={},
        invalid_by_org_direct={},
    )
    assert p_c3["phones"] == ["+15125550101", "+15125550202", "+15125550100"]

    # Org-only lead (contact_id=None) uses all org phones/emails
    p_org = ls._build_lead_payload(
        make_row(None),
        phones_by_contact,
        phones_by_org_all,
        emails_by_contact,
        emails_by_org_all,
        {},
        {},
        {},
        phones_by_org_direct=phones_by_org_direct,
        emails_by_org_direct=emails_by_org_direct,
        invalid_by_org_direct={},
    )
    assert p_org["phones"] == ["+15125550101", "+15125550202", "+15125550100"]


def test_main_without_input_stops_before_contacting_anything(monkeypatch, capsys):
    # Every run is live, so with no input configured main() must stop at once.
    monkeypatch.setattr(caller_main, "INPUT_FILE_PATH", None)
    monkeypatch.setattr(caller_main, "INPUT_LEADS", [])
    monkeypatch.setattr(sys, "argv", ["main.py"])

    def must_not_run(*args, **kwargs):
        raise AssertionError("pipeline must not start without input")

    monkeypatch.setattr(caller_main, "run_leads_sync", must_not_run)
    assert caller_main.main() == 1
    assert "No input set" in capsys.readouterr().out


def test_main_default_input_is_not_the_fictional_sample():
    assert caller_main.INPUT_FILE_PATH is None
