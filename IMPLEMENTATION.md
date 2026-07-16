# Implementation & Deployment Guide

A hand-off for the engineer deploying this to production. Read top to bottom once.

## What you are deploying

One **stateless FastAPI backend** (no database — all state lives in Attio). It
receives webhooks from **Cal.com** (a demo was booked) and **Fireflies** (a meeting
was transcribed) and writes clean, de-duplicated records into the **Attio** CRM.

**The website frontend does not change.** The existing Cal.com booking flow already
fires Cal.com; you just deploy this service and point the three SaaS integrations
at it:

```
Website's Cal.com booking ─► Cal.com ─► POST /webhooks/cal ─┐
                                                            ├─► this service ─► Attio
Fireflies meeting transcribed ─► POST /webhooks/fireflies ─┘   (or the poller pulls
                                                                from Fireflies)
```

---

## 0. Prerequisites (accounts + keys to collect)

| Need | Where to get it | Used for |
|---|---|---|
| **Attio access token** | Attio → Settings → Developers → Access tokens | Writing Companies/People/Notes |
| **Fireflies API key** | Fireflies → Settings → Developer | Fetching transcripts |
| **Anthropic API key** (optional) | console.anthropic.com | AI meeting analysis (blank = fields say "Unknown") |
| **Cal.com webhook secret** | you choose it when creating the webhook | Verifying Cal.com requests |
| **Fireflies webhook secret** | you choose it when creating the webhook | Verifying Fireflies requests |
| A host with a **public HTTPS URL** | any VM / PaaS / container | Webhooks require HTTPS |

**Attio token scopes (important):** Records **read+write**, Notes **read+write**,
and Object configuration **read+write** (so the app can auto-create its custom
`dsv_*` fields on first start). Without object-config write, create the `dsv_*`
attributes by hand (list in `app/clients/attio.py` → `CUSTOM_ATTRIBUTES`).

---

## 1. Get the code & install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill it in (next step)
```

## 2. Configure `.env`

| Key | Value |
|---|---|
| `ATTIO_API_KEY` | Attio token (scopes above) |
| `FIREFLIES_API_KEY` | Fireflies API key |
| `ANTHROPIC_API_KEY` | Claude key (optional) |
| `CALCOM_WEBHOOK_SECRET` | secret you set in Cal.com |
| `FIREFLIES_WEBHOOK_SECRET` | secret you set in Fireflies |
| `INTERNAL_EMAIL_DOMAINS` | your team domains, e.g. `deepsolv.com,deepsolv.ai` |
| `ALLOW_UNSIGNED_WEBHOOKS` | **`false`** in production (fail closed) |
| `FIREFLIES_POLL_SECONDS` | `0` = webhook only; `300` = also auto-pull every 5 min |
| `TRACK_INTERNAL_MEETINGS` | `false` for real product behavior |

In production set these as real environment variables on the host — do **not**
commit `.env`.

## 3. Verify locally first

```bash
python test_edge_cases.py          # expect: 89 passed, 0 failed  (no network needed)
uvicorn app.main:app --port 8000 --workers 1
# health check:
curl http://localhost:8000/health  # {"status":"ok"}
```

On first start you should see `[ATTIO] custom attributes ready (...)` — that means
the `dsv_*` fields were created/verified in Attio.

## 4. Deploy (get a public HTTPS URL)

Pick one:

- **PaaS (easiest — Render / Railway / Fly.io):** start command
  `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`. Set all env vars
  in the dashboard. You get an HTTPS URL automatically.
- **VM + systemd:** run the uvicorn command under systemd; put nginx or Caddy in
  front for TLS.
- **Docker:** `CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1"]`
  behind an HTTPS reverse proxy.

> **Run ONE worker** (`--workers 1`). The meeting de-dup/count uses an in-process
> lock; multiple workers could race on Attio (which has no atomic increment).

## 5. Connect Cal.com

Cal.com → **Settings → Developer → Webhooks → New**:
- **Subscriber URL:** `https://<your-host>/webhooks/cal`
- **Event triggers:** `Booking Created` (add `Booking Cancelled` / `Rescheduled` if wanted)
- **Secret:** set one, and put the same value in `.env` `CALCOM_WEBHOOK_SECRET`

Cal.com signs the body (`x-cal-signature-256`); the service verifies it.

## 6. Connect Fireflies (choose A, B, or both)

**A. Webhook (real-time):** Fireflies → Settings → Developer → Webhooks:
- **URL:** `https://<your-host>/webhooks/fireflies`
- **Secret:** set one → `.env` `FIREFLIES_WEBHOOK_SECRET`
- Fireflies sends `{"event":"meeting.transcribed","meeting_id":...}` signed with
  `x-hub-signature`; the service accepts and verifies it.

**B. Poller (no webhook needed):** set `FIREFLIES_POLL_SECONDS=300`. The service
pulls recent transcripts from the Fireflies API every 5 minutes and processes new
ones. This also auto-heals out-of-order cases (a meeting whose booking arrives
later is retried on the next poll). Safe to run **with** the webhook too.

## 7. Verify end to end

1. Book a test demo through the Cal.com account → an Attio **Company + Person**
   appears.
2. Have (or simulate) a Fireflies meeting for that same client email → a **Note**
   appears on that Company with the summary + **meeting link**, and
   `DSV Meeting Count` / `DSV Account Status` update.
3. Re-send the same meeting → it is skipped (no duplicate Note).

## 8. Production checklist

- [ ] `ALLOW_UNSIGNED_WEBHOOKS=false` and real webhook secrets set
- [ ] Single worker (`--workers 1`)
- [ ] HTTPS in front of the service
- [ ] Env vars set on the host (no committed `.env`)
- [ ] Attio token has object-config write (or `dsv_*` fields created manually)
- [ ] `python test_edge_cases.py` passes (89/89)
- [ ] Health endpoint monitored (`/health`)

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Webhook returns **401** | Missing/incorrect secret. Secret in the provider must equal the `.env` value; or set `ALLOW_UNSIGNED_WEBHOOKS=true` for a local test only. |
| Startup log: `Could not ensure Attio custom attributes` | Token lacks object-config write. Add the scope or create `dsv_*` fields manually. |
| Logs show `[ATTIO ERROR] 401` | Wrong `ATTIO_API_KEY`. |
| Logs show `[ATTIO] ... retry ... 429` | Rate limited; the service retries automatically. |
| Meeting `account_not_found` / skipped | No matching booking yet. With the poller on, it retries automatically once the booking lands. |
| Note has no AI fields | `ANTHROPIC_API_KEY` blank — analysis is skipped (fields say "Unknown"). |

For how every edge case is handled and which fields are used, see
[`EDGE_CASES.md`](EDGE_CASES.md).
