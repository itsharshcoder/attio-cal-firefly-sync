# Cal.com + Fireflies → Attio sync

A FastAPI service that turns Cal.com bookings and Fireflies meeting transcripts
into clean, de-duplicated records in the Attio CRM.

- **Booking** (Cal.com webhook) → find/create **Company**, find/create **Person**
  (linked), link guests, set the initial status.
- **Meeting** (Fireflies webhook) → fetch transcript → **LLM analysis** (Claude)
  → post a dated **Note** on the Company, update quick-glance fields, increment the
  meeting count, and progress the account status.

Identity is de-duplicated: a Person is keyed by email, a Company by its work-email
domain (or by name for free/personal emails).

## Architecture

```
Cal.com  ─┐
          ├─► FastAPI ─ verify sig ─► persist (idempotency) ─► background worker ─► Attio
Fireflies ┘   /webhooks/cal                                        │
              /webhooks/fireflies                                  ├─► Fireflies GraphQL (transcript)
                                                                   └─► Claude (analysis)
```

```
app/
  main.py                 # FastAPI app + startup (auto-creates Attio custom fields)
  config.py               # settings from .env
  logging_conf.py         # shared logger
  db.py                   # SQLite: idempotency, note map, id cache, meeting counts, retry queue
  models.py               # Pydantic models
  security.py             # HMAC-SHA256 webhook signature verification
  clients/
    attio.py              # Attio API client
    fireflies.py          # Fireflies GraphQL client
  services/
    identity.py           # field extraction + de-duplication rules
    analysis.py           # Claude meeting analysis
    calcom_service.py     # booking → Company + Person
    fireflies_service.py  # transcript → Note + fields + status
  routers/
    calcom.py             # POST /webhooks/cal
    fireflies.py          # POST /webhooks/fireflies
```

## Setup

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate     |  macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the keys
```

Fill `.env`:

| Key | Purpose |
|---|---|
| `ATTIO_API_KEY` | Attio access token — Records R/W, Notes R/W, Object configuration R/W |
| `ANTHROPIC_API_KEY` | Claude API key for the meeting analysis |
| `FIREFLIES_API_KEY` | Fireflies API key (to fetch transcripts) |
| `CALCOM_WEBHOOK_SECRET` / `FIREFLIES_WEBHOOK_SECRET` | webhook signing secrets |
| `INTERNAL_EMAIL_DOMAINS` | your team's domains (never treated as the client) |

The `ATTIO_API_KEY` needs **Object configuration: write** so the app can auto-create
its custom `dsv_*` attributes on startup (Company type, status, fit, meeting count,
etc.). Without that scope, create those attributes manually in Attio
(see `app/clients/attio.py` → `CUSTOM_ATTRIBUTES`).

## Run

```bash
uvicorn app.main:app --port 8000
```

Point the Cal.com and Fireflies webhooks at your public URL:

- `https://<host>/webhooks/cal`
- `https://<host>/webhooks/fireflies`

For local development, expose `localhost:8000` with a tunnel (e.g. ngrok) and use
that URL in the webhook settings.

## Data written to Attio

- **Companies** — one record per account: name, domain, company type, monthly Meta
  spend, lead source, account status, fit, category, conversion likelihood, next
  steps, last summary, last meeting date, meeting count, review flags.
- **People** — one record per person: name, email, phone, linked company, source,
  lead source, prep notes.
- **Notes** — one dated Note per meeting, attached to the Company.

## Edge cases handled

Duplicate bookings, free/personal emails (matched by name), missing required
fields, junk company names, role/shared mailboxes, internal-teammate bookers,
guests, internal-only meetings, duplicate Fireflies events, and out-of-order
events (a summary arriving before its booking is parked and retried).
