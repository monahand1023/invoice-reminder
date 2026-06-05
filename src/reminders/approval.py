"""ApprovalQueue — money-touching email's human gate (SQLite).

In ``--send`` mode, rendered reminders are enqueued here as a *pending* batch.
Nothing is delivered until someone runs ``approve <batch-id>``. Even then the
SMTPNotifier is still independently gated by REMINDERS_ALLOW_SEND=1. This is the
"impossible to accidentally blast real emails" guarantee.

Status lifecycle:  pending -> approved -> sent
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from reminders.models import Reminder


class UnknownBatchError(Exception):
    """No batch with the given id exists."""


class ApprovalError(Exception):
    """Illegal status transition (e.g. approving a non-pending batch)."""


@dataclass(frozen=True)
class Batch:
    batch_id: str
    status: str
    created_at: str
    reminders: list[Reminder]


@dataclass(frozen=True)
class BatchSummary:
    batch_id: str
    status: str
    created_at: str
    count: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id   TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS batch_reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    stage      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);
"""


class ApprovalQueue:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- mutation ---------------------------------------------------------

    def enqueue(self, reminders: list[Reminder], *, batch_id: str, created_at: datetime) -> str:
        self._conn.execute(
            "INSERT INTO batches (batch_id, created_at, status) VALUES (?, ?, 'pending')",
            (batch_id, created_at.isoformat()),
        )
        self._conn.executemany(
            "INSERT INTO batch_reminders (batch_id, invoice_id, stage, payload) "
            "VALUES (?, ?, ?, ?)",
            [
                (batch_id, r.invoice_id, r.stage, r.model_dump_json())
                for r in reminders
            ],
        )
        self._conn.commit()
        return batch_id

    def approve(self, batch_id: str) -> None:
        status = self._status(batch_id)
        if status != "pending":
            raise ApprovalError(
                f"batch {batch_id} is '{status}', not 'pending'; cannot approve"
            )
        self._conn.execute(
            "UPDATE batches SET status = 'approved' WHERE batch_id = ?", (batch_id,)
        )
        self._conn.commit()

    def mark_sent(self, batch_id: str) -> None:
        self._status(batch_id)  # existence check
        self._conn.execute(
            "UPDATE batches SET status = 'sent' WHERE batch_id = ?", (batch_id,)
        )
        self._conn.commit()

    def cancel(self, batch_id: str) -> None:
        """Discard a batch that was never approved. A 'sent' batch can't be canceled."""
        status = self._status(batch_id)
        if status == "sent":
            raise ApprovalError(f"batch {batch_id} is already 'sent'; cannot cancel")
        self._conn.execute(
            "UPDATE batches SET status = 'canceled' WHERE batch_id = ?", (batch_id,)
        )
        self._conn.commit()

    # --- queries ----------------------------------------------------------

    def is_approved(self, batch_id: str) -> bool:
        return self._status(batch_id) == "approved"

    def get_batch(self, batch_id: str) -> Batch:
        status = self._status(batch_id)
        meta = self._conn.execute(
            "SELECT created_at FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        rows = self._conn.execute(
            "SELECT payload FROM batch_reminders WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        reminders = [Reminder.model_validate_json(r["payload"]) for r in rows]
        return Batch(batch_id=batch_id, status=status,
                     created_at=meta["created_at"], reminders=reminders)

    def list_batches(self) -> list[BatchSummary]:
        rows = self._conn.execute(
            "SELECT b.batch_id, b.status, b.created_at, "
            "       COUNT(r.id) AS count "
            "FROM batches b LEFT JOIN batch_reminders r ON r.batch_id = b.batch_id "
            "GROUP BY b.batch_id, b.status, b.created_at "
            "ORDER BY b.created_at, b.batch_id"
        ).fetchall()
        return [
            BatchSummary(batch_id=r["batch_id"], status=r["status"],
                         created_at=r["created_at"], count=r["count"])
            for r in rows
        ]

    # --- internals --------------------------------------------------------

    def _status(self, batch_id: str) -> str:
        row = self._conn.execute(
            "SELECT status FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise UnknownBatchError(f"no such batch: {batch_id}")
        return row["status"]

    def close(self) -> None:
        self._conn.close()
