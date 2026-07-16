"""End-to-end edge-case harness for the table-less build.

Runs the REAL app code (attio.py + services + identity) but replaces the HTTP
layer with an in-memory fake that mimics Attio's assert / query / patch semantics,
and stubs Fireflies + Claude. No network, no real Attio calls.
"""
import asyncio
import sys

# ---------------------------------------------------------------- fake Attio store
STORE = {"companies": {}, "people": {}, "notes": {}}
IDS = {"companies": 0, "people": 0, "notes": 0}


def _new_id(kind):
    IDS[kind] += 1
    return f"{kind[:4]}_{IDS[kind]}"


def _cval(values, slug):
    arr = values.get(slug) or []
    return arr[0].get("value") if arr and isinstance(arr[0], dict) else None


class FakeResp:
    def __init__(self, status_code, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


def _match(slug, values, matching_attribute):
    if slug == "companies":
        if matching_attribute == "domains":
            new = {d.get("domain") for d in values.get("domains", [])}
            for rid, v in STORE["companies"].items():
                if new & {d.get("domain") for d in v.get("domains", [])}:
                    return rid
        elif matching_attribute == "name":
            for rid, v in STORE["companies"].items():
                if _cval(v, "name") == _cval(values, "name"):
                    return rid
    elif slug == "people" and matching_attribute == "email_addresses":
        new = {e.get("email_address") for e in values.get("email_addresses", [])}
        for rid, v in STORE["people"].items():
            if new & {e.get("email_address") for e in v.get("email_addresses", [])}:
                return rid
    return None


class FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _slug(self, url):
        return url.split("/objects/")[1].split("/")[0]

    async def put(self, url, headers=None, params=None, json=None):
        slug = self._slug(url)
        values = json["data"]["values"]
        rid = _match(slug, values, params["matching_attribute"])
        if rid:
            STORE[slug][rid].update(values)
        else:
            rid = _new_id(slug)
            STORE[slug][rid] = dict(values)
        return FakeResp(200, {"data": {"id": {"record_id": rid}}})

    async def patch(self, url, headers=None, json=None):
        slug = self._slug(url)
        rid = url.rstrip("/").split("/records/")[1]
        STORE[slug][rid].update(json["data"]["values"])
        return FakeResp(200, {"data": {"id": {"record_id": rid}}})

    async def get(self, url, headers=None):
        slug = self._slug(url)
        rid = url.rstrip("/").split("/records/")[1]
        return FakeResp(200, {"data": {"values": STORE[slug].get(rid, {})}})

    async def post(self, url, headers=None, json=None):
        if url.endswith("/notes"):
            nid = _new_id("notes")
            STORE["notes"][nid] = dict(json["data"])
            return FakeResp(200, {"data": {"id": {"note_id": nid}}})
        if url.endswith("/attributes"):
            return FakeResp(200, {"data": {}})
        if url.endswith("/records/query"):
            slug = self._slug(url)
            flt = json.get("filter", {})
            out = []
            for rid, v in STORE[slug].items():
                if "email_addresses" in flt:
                    emails = {e.get("email_address") for e in v.get("email_addresses", [])}
                    if flt["email_addresses"] in emails:
                        out.append({"id": {"record_id": rid}, "values": v})
                elif "company" in flt:
                    want = flt["company"].get("target_record_id")
                    comp = v.get("company") or []
                    if comp and comp[0].get("target_record_id") == want:
                        out.append({"id": {"record_id": rid}, "values": v})
            return FakeResp(200, {"data": out})
        return FakeResp(404, {}, "not found")


# ---------------------------------------------------------------- patch + imports
import app.clients.attio as attio_mod

class _FakeHttpx:
    AsyncClient = FakeAsyncClient

attio_mod.httpx = _FakeHttpx

from app.clients.attio import attio  # noqa: E402
from app.models import CalcomAttendee, CalcomBookingPayload, MeetingAnalysis  # noqa: E402
from app.services import calcom_service, fireflies_service  # noqa: E402

# Stub Fireflies + Claude for the meeting flow.
_TRANSCRIPT = {}
_ANALYSIS = MeetingAnalysis()

async def _fake_fetch(meeting_id):
    return dict(_TRANSCRIPT, _id=meeting_id)

async def _fake_analyze(transcript):
    return _ANALYSIS

fireflies_service.fetch_transcript = _fake_fetch
fireflies_service.analyze_meeting = _fake_analyze

# ---------------------------------------------------------------- test helpers
PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def n_companies():
    return len(STORE["companies"])

def n_people():
    return len(STORE["people"])

def n_notes():
    return len(STORE["notes"])

def person_by_email(email):
    for v in STORE["people"].values():
        if email in {e.get("email_address") for e in v.get("email_addresses", [])}:
            return v
    return None

def company_named(name):
    for v in STORE["companies"].values():
        if _cval(v, "name") == name:
            return v
    return None


def booking(uid, name, email, phone=None, company="Acme",
            ctype="Agency", spend="$5k/mo", source="Google", prep=None, guests=None):
    responses = {}
    if company is not None:
        responses["company-name"] = company
    if ctype is not None:
        responses["describe-your-company-best"] = ctype
    if spend is not None:
        responses["monthly-meta-spend"] = spend
    if source is not None:
        responses["how-did-you-hear-about-us"] = source
    if prep:
        responses["how-can-we-help-you-prepare"] = prep
    if guests:
        responses["guests"] = guests
    return CalcomBookingPayload(
        uid=uid, title="Demo", startTime="2026-07-14T10:00:00Z",
        attendees=[CalcomAttendee(name=name, email=email, phoneNumber=phone)],
        responses=responses,
    )


async def main():
    global _TRANSCRIPT, _ANALYSIS

    print("\n== A. Clean work-email booking ==")
    r = await calcom_service.handle_booking_created(
        booking("b1", "Aman Verma", "aman@acme.com", phone="+919876543210"))
    check("1 company created", n_companies() == 1)
    check("1 person created", n_people() == 1)
    check("needs_review False", r["needs_review"] is False, r)
    acme = company_named("Acme")
    check("company matched by domain acme.com",
          {d.get("domain") for d in acme.get("domains", [])} == {"acme.com"})
    check("domain NOT flagged unverified", "dsv_domain_unverified" not in acme)
    check("status = New - Demo booked",
          _cval(acme, "dsv_account_status") == "New - Demo booked")
    p = person_by_email("aman@acme.com")
    check("person linked to acme company",
          (p.get("company") or [{}])[0].get("target_record_id") in STORE["companies"])
    check("valid phone stored", bool(p.get("phone_numbers")))

    print("\n== B. Same person, same email (re-book) ==")
    await calcom_service.handle_booking_created(booking("b2", "Aman Verma", "aman@acme.com"))
    check("still 1 company (no dup)", n_companies() == 1)
    check("still 1 person (email assert)", n_people() == 1)

    print("\n== C. Same company, different person ==")
    await calcom_service.handle_booking_created(booking("b3", "Neha Rao", "neha@acme.com"))
    check("still 1 company (domain match)", n_companies() == 1)
    check("now 2 people", n_people() == 2)

    print("\n== D. Same person, same company, DIFFERENT email (key case) ==")
    r = await calcom_service.handle_booking_created(
        booking("b4", "Aman Verma", "aman.verma@acme.com"))
    check("NO new person created (merged by name)", n_people() == 2, f"people={n_people()}")
    check("needs_review True (merged)", r["needs_review"] is True, r)
    p = person_by_email("aman.verma@acme.com")
    check("both emails on one Person",
          p is not None and
          {"aman@acme.com", "aman.verma@acme.com"} <=
          {e.get("email_address") for e in p.get("email_addresses", [])},
          p)
    check("merged person flagged needs_review", _cval(p, "dsv_needs_review") is True)

    print("\n== E. Free email booking (gmail) ==")
    await calcom_service.handle_booking_created(
        booking("b5", "Ravi", "ravi@gmail.com", company="RaviShop"))
    rs = company_named("RaviShop")
    check("new company by name", rs is not None and n_companies() == 2)
    check("dsv_domain_unverified True", _cval(rs, "dsv_domain_unverified") is True)
    check("no domains on free-email company", not rs.get("domains"))

    print("\n== F. Two different gmail people NOT merged ==")
    await calcom_service.handle_booking_created(
        booking("b6", "Sara", "sara@gmail.com", company="SaraCo"))
    check("separate company created (not merged on gmail)",
          company_named("SaraCo") is not None and n_companies() == 3)

    print("\n== G. Junk company name ==")
    r = await calcom_service.handle_booking_created(
        booking("b7", "X", "x@testco.com", company="test"))
    check("junk company -> needs_review", r["needs_review"] is True, r)

    print("\n== H. Role mailbox ==")
    r = await calcom_service.handle_booking_created(
        booking("b8", "Sales Team", "sales@acme.com"))
    check("role mailbox -> needs_review", r["needs_review"] is True, r)

    print("\n== I. Internal teammate as booker ==")
    r = await calcom_service.handle_booking_created(
        booking("b9", "Insider", "me@deepsolv.com", company="DeepSolv"))
    check("internal booker -> needs_review", r["needs_review"] is True, r)

    print("\n== J. Missing required fields ==")
    r = await calcom_service.handle_booking_created(
        CalcomBookingPayload(uid="b10",
                             attendees=[CalcomAttendee(name="NoInfo", email="ni@zeta.com")],
                             responses={}))
    check("missing fields -> needs_review", r["needs_review"] is True, r)

    print("\n== K. Guests linked to same company ==")
    before = n_people()
    await calcom_service.handle_booking_created(
        booking("b11", "Priya", "priya@beta.com", company="Beta",
                guests=["guest1@beta.com"]))
    g = person_by_email("guest1@beta.com")
    beta = company_named("Beta")
    beta_id = [rid for rid, v in STORE["companies"].items() if v is beta][0]
    check("guest person created", g is not None and n_people() == before + 2)
    check("guest linked to same company",
          (g.get("company") or [{}])[0].get("target_record_id") == beta_id)

    print("\n== L. Invalid phone skipped ==")
    await calcom_service.handle_booking_created(
        booking("b12", "BadPhone", "bp@gamma.com", phone="12345", company="Gamma"))
    bp = person_by_email("bp@gamma.com")
    check("invalid phone NOT stored", not bp.get("phone_numbers"))

    # ---------------- Fireflies flow ----------------
    _ANALYSIS = MeetingAnalysis(fit="Yes", category="Agency",
                                conversion_likelihood="High", next_steps="Send proposal",
                                one_line_summary="Great fit")

    print("\n== M. Meeting for known client -> Note + count ==")
    _TRANSCRIPT = {"title": "Acme demo", "date": "2026-07-14",
                   "organizer_email": "host@deepsolv.com",
                   "participants": ["aman@acme.com", "host@deepsolv.com"],
                   "summary": {"overview": "Talked shop", "action_items": ["follow up"],
                               "keywords": ["ads"], "bullet_gist": ["good"]},
                   "transcript_text": "..."}
    notes_before = n_notes()
    r = await fireflies_service.handle_transcription_completed("M1")
    check("meeting processed ok", r.get("status") == "ok" and r.get("meeting_number") == 1, r)
    check("1 note created", n_notes() == notes_before + 1)
    acme = company_named("Acme")
    check("meeting id stored on company",
          _cval(acme, "dsv_fireflies_meeting_ids") == "M1")
    check("meeting count = 1", _cval(acme, "dsv_meeting_count") == 1)
    check("status -> Demo completed", _cval(acme, "dsv_account_status") == "Demo completed")

    print("\n== N. Same meeting again -> dedup (no new note) ==")
    notes_before = n_notes()
    r = await fireflies_service.handle_transcription_completed("M1")
    check("dedup: already_processed", r.get("action") == "already_processed", r)
    check("no new note", n_notes() == notes_before)
    check("count still 1", _cval(company_named("Acme"), "dsv_meeting_count") == 1)

    print("\n== O. Second distinct meeting -> count 2, status Evaluating ==")
    r = await fireflies_service.handle_transcription_completed("M2")
    acme = company_named("Acme")
    check("meeting_number 2", r.get("meeting_number") == 2, r)
    check("ids = M1,M2", _cval(acme, "dsv_fireflies_meeting_ids") == "M1,M2")
    check("status -> Evaluating", _cval(acme, "dsv_account_status") == "Evaluating")

    print("\n== P. Meeting, unknown client (no booking) -> skipped ==")
    _TRANSCRIPT = {"title": "x", "date": "2026-07-14", "organizer_email": "host@deepsolv.com",
                   "participants": ["nobody@nowhere.io", "host@deepsolv.com"],
                   "summary": {}, "transcript_text": ""}
    notes_before = n_notes()
    r = await fireflies_service.handle_transcription_completed("M3")
    check("unknown client -> skipped", r.get("status") == "skipped", r)
    check("no note created", n_notes() == notes_before)

    print("\n== Q. Internal-only meeting -> skipped ==")
    _TRANSCRIPT = {"title": "standup", "date": "2026-07-14", "organizer_email": "host@deepsolv.com",
                   "participants": ["host@deepsolv.com", "other@deepsolv.ai"],
                   "summary": {}, "transcript_text": ""}
    r = await fireflies_service.handle_transcription_completed("M4")
    check("internal-only -> skipped", r.get("action") == "internal_only_skipped", r)

    print("\n== R. fit=No meeting -> Disqualified ==")
    _ANALYSIS = MeetingAnalysis(fit="No", category="Tool-or-Brand",
                                conversion_likelihood="Low", next_steps="Pass",
                                one_line_summary="Not ICP")
    _TRANSCRIPT = {"title": "Beta demo", "date": "2026-07-15", "organizer_email": "host@deepsolv.com",
                   "participants": ["priya@beta.com", "host@deepsolv.com"],
                   "summary": {"overview": "meh", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    r = await fireflies_service.handle_transcription_completed("M5")
    check("fit=No -> Disqualified", r.get("account_status") == "Disqualified", r)

    print("\n== T. Blank-company work-email booking must NOT clobber good name ==")
    # Aman's company 'Acme' already exists (domain acme.com). A new acme.com
    # booking with the company field left blank should not rename it to 'Unknown'.
    await calcom_service.handle_booking_created(
        CalcomBookingPayload(uid="b13",
                             attendees=[CalcomAttendee(name="Blank", email="blank@acme.com")],
                             responses={}))
    acme = company_named("Acme")
    check("existing company name preserved (not 'Unknown')", acme is not None, "Acme was renamed")

    # ---------------- Signature verification ----------------
    print("\n== T. Name guard: blank company can't overwrite a good name ==")
    await calcom_service.handle_booking_created(
        booking("b13", "Zeta", "zeta@acme.com", company=None))  # company blank -> "Unknown"
    check("existing company name 'Acme' preserved (not overwritten)",
          company_named("Acme") is not None)
    check("no 'Unknown'-named company created for acme.com",
          company_named("Unknown") is None)

    print("\n== U. Uppercase email domain still matches same company ==")
    before = n_companies()
    await calcom_service.handle_booking_created(booking("b14", "Cap", "cap@ACME.com"))
    check("uppercase domain reuses acme.com company (no new company)",
          n_companies() == before)
    check("cap person stored with lowercased email", person_by_email("cap@acme.com") is not None)

    print("\n== V. Same name, different domains -> separate companies ==")
    before = n_companies()
    await calcom_service.handle_booking_created(
        booking("b15", "Bob", "bob@acme.io", company="Acme"))
    check("acme.io is a distinct company from acme.com", n_companies() == before + 1)

    print("\n== W. Guest == booker is de-duplicated ==")
    before = n_people()
    r = await calcom_service.handle_booking_created(
        booking("b16", "Dup", "dup@delta.com", company="Delta", guests=["dup@delta.com"]))
    check("guest equal to booker not created twice", n_people() == before + 1, f"+{n_people()-before}")
    check("no guests in result", r["guests"] == [])

    print("\n== X. Free-email same person, different free email -> merged ==")
    await calcom_service.handle_booking_created(
        booking("b17", "Meera", "meera@gmail.com", company="MeeraCo"))
    before = n_people()
    r = await calcom_service.handle_booking_created(
        booking("b18", "Meera", "meera2@gmail.com", company="MeeraCo"))
    check("no new person (merged by name at free-email company)", n_people() == before)
    check("merged flagged", r["needs_review"] is True, r)
    m = person_by_email("meera2@gmail.com")
    check("both gmail addresses on one Person",
          m is not None and
          {"meera@gmail.com", "meera2@gmail.com"} <=
          {e.get("email_address") for e in m.get("email_addresses", [])})

    print("\n== Y. Empty attendees -> no crash, no person ==")
    r = await calcom_service.handle_booking_created(
        CalcomBookingPayload(uid="b19", attendees=[], responses={"company-name": "Ghost"}))
    check("handled without crash", r["status"] == "ok")
    check("no person created (no email)", r["person_id"] is None)

    print("\n== Z. Person job change (new domain) -> new person at new company ==")
    before = n_people()
    await calcom_service.handle_booking_created(
        booking("b20", "Aman Verma", "aman@newcorp.com", company="NewCorp"))
    check("new person created at the new company", n_people() == before + 1)
    check("newcorp person carries the new email", person_by_email("aman@newcorp.com") is not None)

    # ---------------- Fireflies extra cases ----------------
    print("\n== AA. fit=Unknown -> NOT Disqualified ==")
    _ANALYSIS = MeetingAnalysis(fit="Unknown", category="Unknown",
                                conversion_likelihood="Medium", next_steps="TBD",
                                one_line_summary="unclear")
    _TRANSCRIPT = {"title": "Delta demo", "date": "2026-07-16",
                   "organizer_email": "host@deepsolv.com",
                   "participants": ["dup@delta.com", "host@deepsolv.com"],
                   "summary": {"overview": "ok", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    r = await fireflies_service.handle_transcription_completed("MD1")
    check("Unknown fit -> Demo completed (not Disqualified)",
          r.get("account_status") == "Demo completed", r)

    print("\n== AB. Uppercase participant email correlates to account ==")
    _TRANSCRIPT["participants"] = ["DUP@Delta.com", "host@deepsolv.com"]
    r = await fireflies_service.handle_transcription_completed("MD2")
    check("uppercase participant matched to same account", r.get("status") == "ok", r)
    check("count is now 2 for delta", r.get("meeting_number") == 2, r)

    print("\n== AC. Third meeting stays Evaluating ==")
    _TRANSCRIPT["participants"] = ["dup@delta.com", "host@deepsolv.com"]
    r = await fireflies_service.handle_transcription_completed("MD3")
    check("3rd meeting -> Evaluating, number 3",
          r.get("meeting_number") == 3 and r.get("account_status") == "Evaluating", r)

    # ---------------- Signature (fail-closed) ----------------
    print("\n== AD. Webhook signature verification (fail-closed) ==")
    import hashlib
    import hmac

    from app.config import settings
    from app.security import verify_signature
    body = b'{"a":1}'
    secret = "s3cret"
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    settings.allow_unsigned_webhooks = False
    check("blank secret + flag OFF -> REJECTED", verify_signature(body, "", "") is False)
    settings.allow_unsigned_webhooks = True
    check("blank secret + flag ON -> allowed (dev)", verify_signature(body, "", "") is True)
    check("correct signature -> True", verify_signature(body, good, secret) is True)
    check("wrong signature -> False", verify_signature(body, "deadbeef", secret) is False)
    check("missing signature w/ secret -> False", verify_signature(body, "", secret) is False)

    # ---------------- Router-level (end-to-end via TestClient) ----------------
    print("\n== AE. Router: fail-closed rejects unsigned when flag OFF ==")
    import json

    from starlette.testclient import TestClient

    import app.main as appmain
    pl = booking("r1", "Router User", "router@rco.com", company="RCo")
    cal_body = json.dumps({"triggerEvent": "BOOKING_CREATED", "payload": pl.model_dump()})
    settings.allow_unsigned_webhooks = False
    with TestClient(appmain.app) as tc:
        resp = tc.post("/webhooks/cal", content=cal_body,
                       headers={"content-type": "application/json"})
        check("unsigned booking rejected 401", resp.status_code == 401, resp.status_code)

        print("\n== AF. Router: accepts + processes when flag ON ==")
        settings.allow_unsigned_webhooks = True
        before = n_companies()
        resp = tc.post("/webhooks/cal", content=cal_body,
                       headers={"content-type": "application/json"})
        check("accepted 200", resp.status_code == 200, resp.status_code)
        check("booking processed -> RCo company created",
              company_named("RCo") is not None and n_companies() == before + 1)

        print("\n== AG. Router: fireflies non-JSON ping -> 200 ==")
        resp = tc.post("/webhooks/fireflies", content=b"not-json",
                       headers={"content-type": "text/plain"})
        check("ping acknowledged 200", resp.status_code == 200, resp.status_code)

        print("\n== AH. Router: unhandled Cal event -> accepted, ignored ==")
        other = json.dumps({"triggerEvent": "BOOKING_REQUESTED", "payload": pl.model_dump()})
        resp = tc.post("/webhooks/cal", content=other,
                       headers={"content-type": "application/json"})
        check("unhandled event accepted 200", resp.status_code == 200, resp.status_code)

        print("\n== AI. Router: GET fireflies verify -> 200 ==")
        resp = tc.get("/webhooks/fireflies")
        check("GET verify 200", resp.status_code == 200, resp.status_code)

        print("\n== AJ. Router: health + root ==")
        check("health ok", tc.get("/health").json().get("status") == "ok")
        check("root lists endpoints", "/webhooks/cal" in tc.get("/").json().get("endpoints", []))

    # ---------------- Concurrency (proves the per-company lock) ----------------
    # Make analysis yield so parallel handlers interleave inside the read-modify-
    # write. WITHOUT the lock this loses updates (final count would be 1, not N).
    async def _yielding_analyze(_transcript):
        await asyncio.sleep(0)
        return MeetingAnalysis(fit="Yes", category="Agency",
                               conversion_likelihood="High", next_steps="x",
                               one_line_summary="y")
    fireflies_service.analyze_meeting = _yielding_analyze

    print("\n== AK. N parallel meetings, same company -> no lost updates ==")
    await calcom_service.handle_booking_created(
        booking("bk_c", "Conc", "conc@parallel.com", company="Parallel"))
    _TRANSCRIPT = {"title": "p", "date": "2026-07-17", "organizer_email": "host@deepsolv.com",
                   "participants": ["conc@parallel.com", "host@deepsolv.com"],
                   "summary": {"overview": "", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    N = 8
    notes_before = n_notes()
    await asyncio.gather(*[
        fireflies_service.handle_transcription_completed(f"C{i}") for i in range(N)])
    comp = company_named("Parallel")
    ids = [x for x in (_cval(comp, "dsv_fireflies_meeting_ids") or "").split(",") if x]
    check(f"all {N} ids recorded (no lost update)", len(ids) == N, ids)
    check(f"meeting count == {N}", _cval(comp, "dsv_meeting_count") == N,
          _cval(comp, "dsv_meeting_count"))
    check(f"exactly {N} notes created", n_notes() == notes_before + N, n_notes() - notes_before)

    print("\n== AL. Same meeting id fired 5x in parallel -> dedup to one ==")
    await calcom_service.handle_booking_created(
        booking("bk_d", "Dedup", "dedup@par2.com", company="Par2"))
    _TRANSCRIPT["participants"] = ["dedup@par2.com", "host@deepsolv.com"]
    notes_before = n_notes()
    await asyncio.gather(*[
        fireflies_service.handle_transcription_completed("DUP1") for _ in range(5)])
    comp = company_named("Par2")
    ids = [x for x in (_cval(comp, "dsv_fireflies_meeting_ids") or "").split(",") if x]
    check("id recorded exactly once", ids == ["DUP1"], ids)
    check("only one note despite 5 parallel fires", n_notes() == notes_before + 1,
          n_notes() - notes_before)

    # ---------------- Auto-fetch poller (table-less) ----------------
    fireflies_service.analyze_meeting = _fake_analyze  # restore simple analysis

    print("\n== AM. Poller auto-fetches new transcripts -> notes ==")
    async def _fake_list(limit):
        return [{"id": "PP1"}, {"id": "PP2"}, {"id": "PP3"}]
    fireflies_service.list_recent_transcripts = _fake_list
    fireflies_service._seen_meetings.clear()
    await calcom_service.handle_booking_created(
        booking("bk_p", "Poll", "poll@pollco.com", company="PollCo"))
    _TRANSCRIPT = {"title": "poll", "date": "2026-07-18", "organizer_email": "host@deepsolv.com",
                   "participants": ["poll@pollco.com", "host@deepsolv.com"],
                   "summary": {"overview": "", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    notes_before = n_notes()
    r = await fireflies_service.poll_once()
    check("poll scanned 3 transcripts", r["polled"] == 3, r)
    check("poll added 3 new notes", r["new_notes"] == 3 and n_notes() == notes_before + 3, r)
    comp = company_named("PollCo")
    check("all 3 ids recorded on company",
          set((_cval(comp, "dsv_fireflies_meeting_ids") or "").split(",")) == {"PP1", "PP2", "PP3"})

    print("\n== AN. Re-poll same transcripts -> nothing new (dedup) ==")
    notes_before = n_notes()
    r = await fireflies_service.poll_once()
    check("re-poll adds 0 notes (in-process + Attio dedup)", r["new_notes"] == 0)
    check("no extra notes created", n_notes() == notes_before)

    print("\n== AO. Poller: meeting with no booking is retried later (not cached) ==")
    async def _fake_list2(limit):
        return [{"id": "PX9"}]
    fireflies_service.list_recent_transcripts = _fake_list2
    fireflies_service._seen_meetings.clear()
    _TRANSCRIPT["participants"] = ["ghost@nobooking.io", "host@deepsolv.com"]
    await fireflies_service.poll_once()
    check("unbooked meeting NOT cached (will retry next poll)",
          "PX9" not in fireflies_service._seen_meetings)

    print("\n== AP. Meeting link stored in the Note AND on the Company ==")
    await calcom_service.handle_booking_created(
        booking("bk_l", "Link", "link@linkco.com", company="LinkCo"))
    url = "https://app.fireflies.ai/view/XYZ999"
    _TRANSCRIPT = {"title": "Link demo", "date": "2026-07-19", "transcript_url": url,
                   "organizer_email": "host@deepsolv.com",
                   "participants": ["link@linkco.com", "host@deepsolv.com"],
                   "summary": {"overview": "o", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    await fireflies_service.handle_transcription_completed("ML1")
    comp = company_named("LinkCo")
    check("company stores dsv_last_meeting_link", _cval(comp, "dsv_last_meeting_link") == url,
          _cval(comp, "dsv_last_meeting_link"))
    check("note body contains the meeting link",
          any(url in (n.get("content", "")) for n in STORE["notes"].values()))

    print("\n== AQ. Missing transcript_url -> no crash, link empty ==")
    await calcom_service.handle_booking_created(
        booking("bk_n", "NoLink", "nolink@nl.com", company="NL"))
    _TRANSCRIPT = {"title": "No link", "date": "2026-07-20",  # no transcript_url key
                   "organizer_email": "host@deepsolv.com",
                   "participants": ["nolink@nl.com", "host@deepsolv.com"],
                   "summary": {"overview": "o", "action_items": [], "keywords": [], "bullet_gist": []},
                   "transcript_text": "..."}
    r = await fireflies_service.handle_transcription_completed("ML2")
    check("processed fine without a link", r.get("status") == "ok", r)
    check("link field empty (not crashed)", _cval(company_named("NL"), "dsv_last_meeting_link") == "")

    print(f"\n================  RESULT: {PASS} passed, {FAIL} failed  ================")
    return FAIL


sys.exit(asyncio.run(main()))
