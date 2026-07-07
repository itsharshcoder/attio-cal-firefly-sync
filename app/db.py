"""SQLite persistence: webhook idempotency, note de-duplication, an id cache,
per-company meeting counts, and a retry queue for out-of-order events."""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "app.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables if they don't exist yet."""
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS webhook_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                raw_body        TEXT NOT NULL,
                received_at     TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meeting_note_map (
                fireflies_meeting_id TEXT PRIMARY KEY,
                attio_note_id        TEXT NOT NULL,
                created_at           TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS attio_id_cache (
                lookup_key TEXT PRIMARY KEY,
                record_id  TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS meeting_counts (
                company_id TEXT PRIMARY KEY,
                count      INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS pending_fireflies (
                meeting_id   TEXT PRIMARY KEY,
                client_email TEXT NOT NULL,
                created_at   TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )


def already_processed(idempotency_key: str) -> bool:
    """True if this exact webhook was already handled."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM webhook_events WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return row is not None


def store_event(source: str, idempotency_key: str, raw_body: str) -> None:
    """Record a raw webhook (duplicates are ignored)."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO webhook_events (source, idempotency_key, raw_body) "
            "VALUES (?, ?, ?)",
            (source, idempotency_key, raw_body),
        )


def get_note_id(fireflies_meeting_id: str) -> str | None:
    """Return the Attio note id already created for a meeting, if any."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT attio_note_id FROM meeting_note_map WHERE fireflies_meeting_id = ?",
            (fireflies_meeting_id,),
        ).fetchone()
        return row["attio_note_id"] if row else None


def save_note_id(fireflies_meeting_id: str, attio_note_id: str) -> None:
    """Remember the note created for a meeting (prevents duplicate notes)."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meeting_note_map (fireflies_meeting_id, attio_note_id) "
            "VALUES (?, ?)",
            (fireflies_meeting_id, attio_note_id),
        )


def cache_get(lookup_key: str) -> str | None:
    """Read a cached identity->record-id mapping."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT record_id FROM attio_id_cache WHERE lookup_key = ?", (lookup_key,)
        ).fetchone()
        return row["record_id"] if row else None


def cache_put(lookup_key: str, record_id: str) -> None:
    """Store an identity->record-id mapping."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO attio_id_cache (lookup_key, record_id) VALUES (?, ?)",
            (lookup_key, record_id),
        )


def add_pending_fireflies(meeting_id: str, client_email: str) -> None:
    """Park a Fireflies meeting whose booking hasn't been processed yet."""
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_fireflies (meeting_id, client_email) VALUES (?, ?)",
            (meeting_id, client_email.lower()),
        )


def take_pending_fireflies_for_email(client_email: str) -> list[str]:
    """Return and remove any parked Fireflies meeting ids for this email."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT meeting_id FROM pending_fireflies WHERE client_email = ?",
            (client_email.lower(),),
        ).fetchall()
        conn.execute(
            "DELETE FROM pending_fireflies WHERE client_email = ?", (client_email.lower(),)
        )
        return [r["meeting_id"] for r in rows]


def bump_meeting_count(company_id: str) -> int:
    """Increment and return this company's meeting count (1 for the first)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM meeting_counts WHERE company_id = ?", (company_id,)
        ).fetchone()
        new_count = (row["count"] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO meeting_counts (company_id, count, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (company_id, new_count),
        )
        return new_count
