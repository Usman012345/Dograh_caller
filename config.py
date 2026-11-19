"""
Central configuration for dograh_caller.

Every value can be overridden with an environment variable or a line in a
`.env` file (looked up in this folder first, then in its parent folder).
Secrets (API keys) should ONLY live in `.env`, never in code.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent

# .env in this folder wins over .env in the parent folder; real environment
# variables win over both.
load_dotenv(PROJECT_DIR / ".env", override=False)
load_dotenv(PROJECT_DIR.parent / ".env", override=False)


def _env(name: str, default: str = "", *aliases: str) -> str:
    for key in (name, *aliases):
        value = os.environ.get(key)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


# ------------------------------------------------------------------------------
# Number verifier
# ------------------------------------------------------------------------------
# Phone regions accepted (ISO codes, comma-separated). Default: the 50 states + DC
# only. Other +1 regions are dropped: Canada (CA), Puerto Rico (PR), US Virgin
# Islands (VI), Guam (GU), Northern Mariana Islands (MP), American Samoa (AS),
# and the Caribbean. To include the US territories use "US,PR,VI,GU,MP,AS".
ALLOWED_PHONE_REGIONS = [r for r in _env("ALLOWED_PHONE_REGIONS", "US").split(",") if r.strip()]

# Never call a number again that an earlier run already called (read from the
# results sheet at startup: rows with status "called" or "call_timeout").
SKIP_ALREADY_CALLED: bool = _env("SKIP_ALREADY_CALLED", "true").lower() not in ("0", "false", "no")

# ------------------------------------------------------------------------------
# Jev (TypeSafe) - decision maker
# ------------------------------------------------------------------------------
JEV_API_KEY: str = _env("TYPESAFE_API_KEY", "", "Jev_Key", "JEV_KEY", "JEV_API_KEY")
# Pin an exact version in production; "jev-latest" moves when TypeSafe ships.
JEV_MODEL: str = _env("JEV_MODEL", "jev-1.14.0")

# A lead is passed to a service's calling agent only if Jev's probability that
# the lead is interested in that service is >= this value (0.85 = 85%).
INTEREST_THRESHOLD: float = _env_float("INTEREST_THRESHOLD", 0.85)

# Where services (DraftLiner, Paradise, ...) are described. See SERVICES.md.
SERVICES_FILE: Path = Path(_env("SERVICES_FILE", str(PROJECT_DIR / "services.yaml")))

# ------------------------------------------------------------------------------
# Dograh - calling agents
# ------------------------------------------------------------------------------
DOGRAH_API_KEY: str = _env("DOGRAH_API_KEY")
DOGRAH_BASE_URL: str = _env("DOGRAH_BASE_URL", "https://dograh.grobots.ai/api/v1").rstrip("/")
VERIFY_SSL: bool = _env("DOGRAH_VERIFY_SSL", "true").lower() not in ("0", "false", "no")
REQUEST_TIMEOUT: int = _env_int("DOGRAH_REQUEST_TIMEOUT", 30)

# --- Campaigns ---------------------------------------------------------------
# Each run creates one Dograh campaign per service that has interested leads,
# named "<service name>_<YYYY-MM-DD>" (e.g. "Draftliner_2026-11-19").
CAMPAIGN_NAME_DATE_FORMAT: str = _env("CAMPAIGN_NAME_DATE_FORMAT", "%Y-%m-%d")

# Retry settings sent with each new campaign (Dograh handles the retries).
CAMPAIGN_RETRY_ENABLED: bool = _env("CAMPAIGN_RETRY_ENABLED", "true").lower() not in ("0", "false", "no")
CAMPAIGN_MAX_RETRIES: int = _env_int("CAMPAIGN_MAX_RETRIES", 1)
CAMPAIGN_RETRY_DELAY_SECONDS: int = _env_int("CAMPAIGN_RETRY_DELAY_SECONDS", 120)
CAMPAIGN_RETRY_ON_BUSY: bool = _env("CAMPAIGN_RETRY_ON_BUSY", "true").lower() not in ("0", "false", "no")
CAMPAIGN_RETRY_ON_NO_ANSWER: bool = _env("CAMPAIGN_RETRY_ON_NO_ANSWER", "true").lower() not in ("0", "false", "no")
CAMPAIGN_RETRY_ON_VOICEMAIL: bool = _env("CAMPAIGN_RETRY_ON_VOICEMAIL", "false").lower() not in ("0", "false", "no")

# Seconds between progress checks of running campaigns.
CAMPAIGN_POLL_SECONDS: int = _env_int("CAMPAIGN_POLL_SECONDS", 5)
# If a campaign makes no progress (no new call, no finished call) for this
# long, its remaining leads are recorded as call_timeout and monitoring stops.
CAMPAIGN_IDLE_TIMEOUT_SECONDS: int = _env_int("CAMPAIGN_IDLE_TIMEOUT_SECONDS", 900)
# After a call ends, wait up to this long for Dograh to fill in the outcome.
RESULT_WAIT_SECONDS: int = _env_int("RESULT_WAIT_SECONDS", 20)
# Extra wait beyond the retry delay for a scheduled retry call to appear.
RETRY_GRACE_SECONDS: int = _env_int("RETRY_GRACE_SECONDS", 120)

# ------------------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------------------
# Leads are processed in batches of this size: a batch is verified, scored,
# put into campaigns and fully called before the next batch is taken.
LEAD_BATCH_SIZE: int = _env_int("LEAD_BATCH_SIZE", 1000)

# Max items waiting between two stages (back-pressure: faster stages pause).
QUEUE_MAXSIZE: int = _env_int("QUEUE_MAXSIZE", 100)
# Parallel Jev requests.
DECISION_CONCURRENCY: int = _env_int("DECISION_CONCURRENCY", 8)

# Results spreadsheet (created if missing, appended to if it exists).
RESULTS_FILE: Path = Path(_env("RESULTS_FILE", str(PROJECT_DIR / "output" / "call_results.xlsx")))
