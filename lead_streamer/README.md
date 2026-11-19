# Lead Streamer

Standalone HTTP pull service (`Python 3.10+` + `psycopg` v3) that reads eligible leads from PostgreSQL and serves up to `1,000` leads per request to the dograh caller, tracking delivery state solely in `lead_stream.sent_leads`.

This directory is completely self-contained and can be copied anywhere on a server or removed from the repository without affecting any other component.

## 1. Install

```bash
sudo mkdir -p /opt/lead_streamer && sudo cp -r lead_streamer/* /opt/lead_streamer/
cd /opt/lead_streamer
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 2. Configure

Create `.env` from the template and lock down permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Generate a `STREAM_TOKEN` (minimum 32 characters):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Edit `.env` and set `DATABASE_URL` and `STREAM_TOKEN`. Once your HTTPS subdomain is ready, set `PUBLIC_BASE_URL` in `.env` (or paste it at the top of `lead_streamer.py`).

### Least-privilege PostgreSQL setup (optional)

If the runtime database role does not have `CREATE` privilege on the database, run this SQL once as a DBA:

```sql
CREATE SCHEMA IF NOT EXISTS lead_stream;

CREATE TABLE IF NOT EXISTS lead_stream.sent_leads (
    lead_id      varchar(100) PRIMARY KEY,
    sent_version integer      NOT NULL,
    sent_at      timestamptz  NOT NULL DEFAULT now()
);

GRANT USAGE ON SCHEMA lead_stream TO lead_streamer_user;
GRANT SELECT, INSERT, UPDATE ON lead_stream.sent_leads TO lead_streamer_user;
GRANT SELECT ON public.leads, public.organizations, public.contacts, public.phones, public.emails, public.locations TO lead_streamer_user;
```

## 3. Run

### Local / manual run

```bash
.venv/bin/python lead_streamer.py
```

### Production (systemd + nginx)

Install the systemd service:

```bash
sudo cp lead-streamer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lead-streamer
journalctl -u lead-streamer -f
```

Configure nginx using `nginx-lead-streamer.conf` with a valid TLS certificate (e.g., via certbot), then reload nginx and restart `lead-streamer` after setting `PUBLIC_BASE_URL`.

## 4. API & Ping Examples

```bash
# Liveness (no token required)
curl -s https://leads.yourcompany.com/health

# Progress stats (token required)
curl -s -H "Authorization: Bearer $STREAM_TOKEN" https://leads.yourcompany.com/stats

# Pull next batch of up to 1,000 leads
curl -s -H "Authorization: Bearer $STREAM_TOKEN" https://leads.yourcompany.com/leads/next
```

## 5. Verify & Operations

```bash
# Service status and today's logs
systemctl status lead-streamer
journalctl -u lead-streamer --since today | tail -50
```

To re-send leads without restarting:

```sql
-- Re-send a single lead
DELETE FROM lead_stream.sent_leads WHERE lead_id = '<lead id>';

-- Re-send everything sent today
DELETE FROM lead_stream.sent_leads WHERE sent_at >= date_trunc('day', now());

-- Start over completely
TRUNCATE lead_stream.sent_leads;
```

To remove the streamer completely:

```bash
sudo systemctl disable --now lead-streamer
```

```sql
DROP SCHEMA lead_stream CASCADE;
```
