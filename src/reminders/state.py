"""ReminderStateStore — the idempotency + audit backbone (SQLite).

Every real send is recorded as one row keyed UNIQUE(invoice_id, stage). That
unique constraint is what makes "never send the same stage twice" a hard
guarantee even if the job runs repeatedly or crashes mid-batch. The stored
message_hash lets a re-run prove it already sent *this exact* message.

Nothing here is redacted except that secrets are never written in the first
place — the full who/what/when audit trail is kept intact.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from reminders.models import SendResult


class AlreadySentError(Exception):
    """Raised when trying to record a send for an (invoice_id, stage) already sent."""


@dataclass(frozen=True)
class SentRecord:
    invoice_id: str
    stage: str
    sent_at: str          # ISO-8601
    channel: str
    message_hash: str
    to_email: str
    batch_id: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    TEXT NOT NULL,
    stage         TEXT NOT NULL,
    sent_at       TEXT NOT NULL,
    channel       TEXT NOT NULL,
    message_hash  TEXT NOT NULL,
    to_email      TEXT NOT NULL,
    batch_id      TEXT,
    UNIQUE(invoice_id, stage)
);

-- Cache for the optional LLM tone-rewrite. Keyed per (invoice, stage) and on the
-- source-body hash so a re-send is byte-identical (idempotency/audit stay intact)
-- and editing a template re-rewrites. Empty/unused unless tone-rewrite is on.
CREATE TABLE IF NOT EXISTS tone_rewrites (
    invoice_id     TEXT NOT NULL,
    stage          TEXT NOT NULL,
    source_hash    TEXT NOT NULL,
    rewritten_body TEXT NOT NULL,
    rewritten_hash TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (invoice_id, stage)
);
"""


class ReminderStateStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- queries ----------------------------------------------------------

    def sent_stages(self, invoice_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT stage FROM sent_reminders WHERE invoice_id = ?", (invoice_id,)
        ).fetchall()
        return {r["stage"] for r in rows}

    def already_sent(self, invoice_id: str, stage: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sent_reminders WHERE invoice_id = ? AND stage = ? LIMIT 1",
            (invoice_id, stage),
        ).fetchone()
        return row is not None

    # --- tone-rewrite cache (only used when the LLM seam is enabled) -------

    def get_cached_rewrite(self, invoice_id: str, stage: str, *, source_hash: str) -> str | None:
        """Return a previously cached rewrite for this (invoice, stage), but only
        if it was produced from the same source body (``source_hash``). A template
        edit changes the source hash and forces a fresh rewrite."""
        row = self._conn.execute(
            "SELECT rewritten_body FROM tone_rewrites "
            "WHERE invoice_id = ? AND stage = ? AND source_hash = ?",
            (invoice_id, stage, source_hash),
        ).fetchone()
        return row["rewritten_body"] if row else None

    def cache_rewrite(
        self,
        invoice_id: str,
        stage: str,
        *,
        source_hash: str,
        rewritten_body: str,
        rewritten_hash: str,
        created_at: str,
    ) -> None:
        """Persist the body we actually intend to send for this (invoice, stage),
        so a retry/re-approve sends identical bytes."""
        self._conn.execute(
            "INSERT OR REPLACE INTO tone_rewrites "
            "(invoice_id, stage, source_hash, rewritten_body, rewritten_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (invoice_id, stage, source_hash, rewritten_body, rewritten_hash, created_at),
        )
        self._conn.commit()

    def history(self, invoice_id: str | None = None) -> list[SentRecord]:
        if invoice_id is None:
            rows = self._conn.execute(
                "SELECT * FROM sent_reminders ORDER BY id"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM sent_reminders WHERE invoice_id = ? ORDER BY id",
                (invoice_id,),
            ).fetchall()
        return [
            SentRecord(
                invoice_id=r["invoice_id"],
                stage=r["stage"],
                sent_at=r["sent_at"],
                channel=r["channel"],
                message_hash=r["message_hash"],
                to_email=r["to_email"],
                batch_id=r["batch_id"],
            )
            for r in rows
        ]

    # --- mutation ---------------------------------------------------------

    def record_send(
        self,
        result: SendResult,
        *,
        to_email: str,
        batch_id: str | None = None,
    ) -> None:
        """Persist a successful send. Raises AlreadySentError on a duplicate
        (invoice_id, stage), which is the idempotency backstop."""
        sent_at = result.sent_at
        sent_at_str = sent_at.isoformat() if isinstance(sent_at, datetime) else str(sent_at)
        try:
            self._conn.execute(
                "INSERT INTO sent_reminders "
                "(invoice_id, stage, sent_at, channel, message_hash, to_email, batch_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result.invoice_id,
                    result.stage,
                    sent_at_str,
                    result.channel,
                    result.message_hash,
                    to_email,
                    batch_id,
                ),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise AlreadySentError(
                f"{result.invoice_id} stage '{result.stage}' already recorded as sent"
            ) from exc

    def close(self) -> None:
        self._conn.close()
