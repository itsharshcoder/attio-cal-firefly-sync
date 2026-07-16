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

**Table-less:** there is **no local database**. All state — identity,
de-duplication, meeting count, status — lives in **Attio itself**. See
[`EDGE_CASES.md`](EDGE_CASES.md) for the most reliable matching parameter and how
every edge case is handled.

## Architecture

```
Cal.com  ─┐
          ├─► FastAPI ─ verify sig ─► background worker ─► Attio (system of record)
Fireflies ┘   /webhooks/cal                                    │
              /webhooks/fireflies                              ├─► Fireflies GraphQL (transcript)
                                                               └─► Claude (analysis)
```

```
app/
  main.py                 # FastAPI app + startup (auto-creates Attio custom fields)
  config.py               # settings from .env
  logging_conf.py         # shared logger
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
uvicorn app.main:app --port 8000 --workers 1
```

Run a **single worker** (`--workers 1`): the meeting de-dup/count is a
read-modify-write guarded by an in-process lock, which only holds within one
process (see `EDGE_CASES.md` §7).

Point the Cal.com and Fireflies webhooks at your public URL:

- `https://<host>/webhooks/cal`
- `https://<host>/webhooks/fireflies`

For local development, expose `localhost:8000` with a tunnel (e.g. ngrok) and use
that URL in the webhook settings.

**Security:** webhook signatures are verified (HMAC-SHA256) and **fail closed** —
if a provider's secret is blank the request is rejected. For local testing with
unsigned tools set `ALLOW_UNSIGNED_WEBHOOKS=true`; keep it `false` in production
and set the real signing secrets.

**Auto-fetch future meetings (no webhook needed):** set `FIREFLIES_POLL_SECONDS`
(e.g. `300`) and the app polls Fireflies for new transcripts on that interval and
writes notes automatically — de-dup stays in Attio (`dsv_fireflies_meeting_ids`).
Leave it `0` to rely on the `/webhooks/fireflies` push only. Both paths run the
same handler; you can use either or both.

## Test

```bash
# no network / no real Attio — mocks the HTTP layer, runs 79 edge-case checks
python test_edge_cases.py          # expect: 79 passed, 0 failed
```

## Data written to Attio

- **Companies** — one record per account: name, domain, company type, monthly Meta
  spend, lead source, account status, fit, category, conversion likelihood, next
  steps, last summary, last meeting date, **last meeting link**, meeting count,
  review flags. Every meeting's link is also kept in its Note.
- **People** — one record per person: name, email, phone, linked company, source,
  lead source, prep notes.
- **Notes** — one dated Note per meeting, attached to the Company.

## De-duplication (table-less)

- **Person** is keyed by `email_addresses` (Attio "assert") — the most reliable
  key. The same human with a **new email** at the same company is matched by name
  and the email is appended to the existing Person (see `attio.upsert_person`).
- **Company** is keyed by `domains` (work email) or `name` (free email).
- **Meetings** de-dupe and count via `dsv_fireflies_meeting_ids` on the Company (a
  read-modify-write), so no local tables are needed.

## Edge cases handled

Duplicate bookings, same-person-different-email, free/personal emails (matched by
name), missing required fields, junk company names, role/shared mailboxes,
internal-teammate bookers, guests, internal-only meetings, and duplicate Fireflies
events. See [`EDGE_CASES.md`](EDGE_CASES.md) for the full list, the reliability
ranking, and the few rare cases intentionally left out.
