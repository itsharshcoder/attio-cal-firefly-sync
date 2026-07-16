# Edge cases & how they are handled (table-less)

This build keeps **no local database**. Every piece of state — identity,
de-duplication, meeting count, status — lives in **Attio itself**. This document
lists the edge cases the sync handles, the most reliable matching parameter, and
where each case is solved in the code.

---

## 1. Most reliable parameter (and why)

| Object | Reliable key (Attio attribute) | Why it is the most reliable |
|---|---|---|
| **Person** | **`email_addresses`** | An email is globally unique, canonical, and **multi-value** in Attio — one Person can hold several emails, so the *same human* stays one record even across addresses. This is Attio's "assert" key, so a repeat event **updates** the same Person instead of duplicating it. |
| **Company** | **`domains`** (work email) → **`name`** (free email) | The email domain (e.g. `acme.com`) is the strongest company signal: everyone at Acme shares it, so all their People collapse onto one Company. Free/personal emails (gmail, etc.) have no company domain, so we fall back to matching by company **name** (exact) and mark `dsv_domain_unverified = true`. |
| **Meeting → account** | **client `email` → Person → linked Company** | A Fireflies meeting is tied to an account by the client attendee's email: we search People on `email_addresses` and read that Person's linked `company`. Email is the only field reliably present on both the booking and the meeting. |

**Ranking:** `email` (exact, canonical) > `domain` (shared, work only) > `name`
(fuzzy, last resort). Name is used **only** for free-email companies and for the
same-person-different-email fallback below — never as a primary Person key.

### How the reliable key handles each identity edge case

| Situation | What the reliable key does |
|---|---|
| Same person books twice, **same email** | `email_addresses` assert → same Person updated. No duplicate. |
| Same person, **same company, different email** | Email search misses, so we look for the **same name at the same Company** and **append** the new email to the existing Person (email is multi-value), flag `dsv_needs_review`. One human, one record. See `attio.upsert_person`. |
| Same company, many people | All People share the company `domains` key → all link to one Company. |
| Two different people, same free-email provider (both gmail) | Company matched by **name**, not the gmail domain, so unrelated people are **not** merged. |
| Company typed differently (`Acme` vs `Acme Inc.`) | Work email → matched by **domain**, so the same Company is reused no matter how the name is typed. Free email → matched by exact name (rare variants stay separate). |

---

## 2. How a Fireflies meeting is correlated + where notes are searched

The link between a Fireflies meeting and the booking's Attio records is the
**client's email address**.

**A meeting reaches the app two ways (both table-less, both feed the same handler):**
- **Webhook (real-time):** Fireflies POSTs `meeting.transcribed` + `meeting_id` to
  `/webhooks/fireflies`.
- **Auto-fetch poller (pull):** every `FIREFLIES_POLL_SECONDS` the app lists recent
  transcripts from Fireflies and processes any new ones — no webhook/ngrok needed
  (`fireflies_service.poll_once`).

Then, for each `meeting_id`:

1. **Fetch** the transcript (`fetch_transcript`) and pick the client attendee.
2. **Find the account** — search the **People** object with a filter on
   **`email_addresses`** (the client's email) and read that Person's linked
   **`company`** (`attio.find_company_for_email`).
3. **Decide new vs duplicate** — read the Company's **`dsv_fireflies_meeting_ids`**
   text field. If the `meeting_id` is already in that comma-separated list → skip
   (no duplicate Note). Otherwise append it and set `dsv_meeting_count = len(list)`.
4. **Attach the Note** to that Company (`parent_object = "companies"`,
   `parent_record_id = company_id`); each meeting is a new Note.

**Which field does the search use?**
| Purpose | Field searched |
|---|---|
| Find the account for a meeting | **People `email_addresses`** → linked **`company`** |
| Detect a duplicate meeting (already noted?) | **Company `dsv_fireflies_meeting_ids`** |
| Where the Note lives | attached to the **Company** record |

The whole step 3–4 read-modify-write runs under a per-company lock so concurrent
meetings can't lose an id or double-post a Note.

---

## 3. Identity & de-duplication

| Edge case | How it is handled | Where |
|---|---|---|
| Same person books again (same email) | Matched on `email_addresses` → same Person updated | `attio.assert_person` / `upsert_person` |
| Same person, same company, **different email** | Same name at same Company → append the new email, flag review | `attio.upsert_person`, `find_person_by_name_at_company`, `append_person_email` |
| Same company, different people | Company matched on `domains` → all people link to one Company | `attio.assert_company` |
| Free/personal email (gmail, etc.) | No work domain → match Company by **name**, set `dsv_domain_unverified` | `identity.resolve_company_key`, `assert_company` |
| Company name typed differently (Acme / Acme Inc) | Work email → same Company via **domain** match (name text ignored); free email → exact-name match (variants not merged — rare) | `attio.assert_company` |
| Blank company on a later work-email booking | Existing Company name is preserved (a blank/"Unknown" name is never written over a good one) | `attio.assert_company` (name guard) |
| Junk company name (test, n/a, …) | Flagged `dsv_needs_review` | `identity.is_junk_company` |
| Role/shared mailbox (sales@, info@) | Flagged `dsv_needs_review` | `identity.is_role_mailbox` |
| Internal teammate as booker (@deepsolv.com) | Flagged `dsv_needs_review` | `identity.is_internal` |
| Missing required fields in the payload | Filled as "Unknown" and flagged for review | `identity.extract_booking_info` (`missing_required`) |

## 4. Cal.com booking lifecycle

| Edge case | How it is handled | Where |
|---|---|---|
| Duplicate webhook (same uid + event) | Attio "assert" is idempotent — the same records are re-upserted, no duplicates created | `attio.assert_company` / `assert_person` |
| Reschedule | No duplicate, no status change (logged) | `calcom_service.handle_booking_rescheduled` |
| Cancellation | Logged | `calcom_service.handle_booking_cancelled` |
| Guests on the booking | Extra People created, linked to the same Company | `identity._extract_guests`, guest loop |
| Varying Cal.com form field names | Fuzzy match by keyword include/exclude | `identity._find` |
| Answers as string / dict / list | Normalized to a clean string | `identity._clean` |

## 5. Fireflies & meeting handling

| Edge case | How it is handled | Where |
|---|---|---|
| Which attendee is the client? | Exclude internal domains and the organizer | `fireflies_service._pick_client_email` |
| Internal-only meeting (no client) | Skipped (or tracked under organizer if `TRACK_INTERNAL_MEETINGS`) | `handle_transcription_completed` |
| Same meeting webhook twice | Meeting id already in `dsv_fireflies_meeting_ids` → skipped, no duplicate Note | `handle_transcription_completed` (read-modify-write) |
| Repeat meetings for one account | Id appended, `dsv_meeting_count` recomputed, status progresses, new Note each time | `handle_transcription_completed`, `_next_status` |
| Meeting arrives before its booking | No matching account yet → meeting is **skipped** (rare; re-send the Fireflies event after the booking is in Attio) | `handle_transcription_completed` (`account_not_found`) |
| Real Fireflies payload shape | Accepts `event: meeting.transcribed` + `meeting_id` (and the spec's names) | `routers/fireflies.py` |
| Incomplete participant list | Merges `meeting_attendees` emails into participants | `clients/fireflies.fetch_transcript` |
| Correlate meeting to account | By client **email** (Attio `email_addresses` search → linked Company) | `attio.find_company_for_email` |
| **Auto-fetch future meetings (no webhook)** | Poller lists recent transcripts every `FIREFLIES_POLL_SECONDS` and processes new ones; dedup stays in `dsv_fireflies_meeting_ids` | `fireflies_service.poll_once` / `poll_loop` |
| Meeting fetched before its booking exists | Poller returns `account_not_found` and does **not** cache it, so the next poll retries once the booking lands (out-of-order auto-heals) | `fireflies_service.poll_once` |
| Store the meeting link | `transcript_url` saved in **each Note** (full history) + on the Company as `dsv_last_meeting_link` (latest) | `_build_note`, `handle_transcription_completed` |
| Meeting has no `transcript_url` | Link line omitted, `dsv_last_meeting_link` left empty — no crash | `_build_note` |

## 6. Attio / data

| Edge case | How it is handled | Where |
|---|---|---|
| Custom `dsv_*` fields not present | Auto-created on startup | `attio.ensure_custom_attributes` |
| Invalid phone number | Skipped (not sent), Person still created | `attio._valid_phone` |
| Person name format (first/last) | Split into Attio personal-name shape | `attio._person_name` |
| Attio API error | Logged with status + body, then raised | `_assert`, `update_record`, `create_note` |
| Attio rate limit / transient 5xx (429/502/503/504) | Retried up to 3× with backoff that honors `Retry-After` | `attio._http` |
| Attio rate limit budget (100 reads/s, 25 writes/s) | Work is per-webhook and low-volume; one read + a few writes per event stays well under the cap | whole flow |

## 7. Security & concurrency

| Edge case | How it is handled | Where |
|---|---|---|
| Forged webhook | HMAC-SHA256 signature verification (`compare_digest`) | `security.verify_signature` |
| Unsigned webhook / missing secret | **Fail closed** — rejected with 401 unless `ALLOW_UNSIGNED_WEBHOOKS=true` (local testing only) | `security.verify_signature` |
| Webhook URL verification (GET / non-JSON ping) | Answered with 200, no processing | `routers/fireflies.py` |
| Handler failure in background | Logged as one line, never crashes the server | `_safe` wrappers in routers |
| Two meeting webhooks for the **same company** at once | Per-company `asyncio.Lock` serializes the read-modify-write so no meeting id / Note is lost | `fireflies_service._lock_for` |

> **Deploy note (concurrency):** the lock protects within one process. Run a
> **single uvicorn worker** for full safety — Attio has no atomic increment, so
> multiple worker processes could still race on `dsv_fireflies_meeting_ids`.

---

## 8. Rare cases intentionally not handled

Kept out to stay table-less and simple (all low-probability):

- **Out-of-order events** (a meeting transcript arriving *before* its Cal.com
  booking). Over the **webhook** path alone the meeting is skipped; but with the
  **auto-fetch poller enabled** (`FIREFLIES_POLL_SECONDS>0`) it auto-heals — an
  unmatched meeting is not cached, so the next poll retries it once the booking
  lands. In practice a demo is booked before it happens anyway.
- **Cross-provider webhook replay after long delays** — Attio's assert idempotency
  covers replays for records; a duplicate Note is prevented by the meeting-id list,
  so the only true gap is a meeting whose booking never arrives.
- **Same name, same company, genuinely different humans** (e.g. two "John Smith"
  at one firm with different emails) — the name fallback would merge them; this is
  rare and is surfaced by the `dsv_needs_review` flag for a human to split.
