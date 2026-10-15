from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class CallbackInbox:
  """Independent, durable receipt store; never stores decrypted payment identifiers."""

  def __init__(self, path: Path):
    self.path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with self.connect() as db:
      db.execute("PRAGMA journal_mode=WAL")
      db.execute("""CREATE TABLE IF NOT EXISTS receipts (
        id TEXT PRIMARY KEY,
        out_trade_no TEXT NOT NULL,
        received_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',
        acked_at REAL
      )""")
      db.execute("CREATE INDEX IF NOT EXISTS receipts_due ON receipts(status, next_attempt_at)")

  @contextmanager
  def connect(self) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(self.path, timeout=5)
    try:
      connection.execute("PRAGMA busy_timeout=5000")
      connection.execute("PRAGMA synchronous=FULL")
      with connection:
        yield connection
    finally:
      connection.close()

  def record(self, notification_id: str, out_trade_no: str) -> None:
    with self.connect() as db:
      db.execute(
        "INSERT OR IGNORE INTO receipts(id, out_trade_no, received_at) VALUES (?, ?, ?)",
        (notification_id, out_trade_no, time.time()),
      )

  def pending(self, limit: int) -> list[dict]:
    with self.connect() as db:
      now = time.time()
      db.execute("DELETE FROM receipts WHERE status='acked' AND acked_at < ?", (now - 7 * 86400,))
      rows = db.execute(
        "SELECT id, out_trade_no, received_at, attempts FROM receipts "
        "WHERE status='pending' AND next_attempt_at <= ? ORDER BY received_at LIMIT ?",
        (now, limit),
      ).fetchall()
    return [{"id": row[0], "outTradeNo": row[1], "receivedAt": row[2], "attempts": row[3]} for row in rows]

  def acknowledge(self, notification_id: str) -> None:
    with self.connect() as db:
      db.execute("UPDATE receipts SET status='acked', acked_at=? WHERE id=? AND status='pending'", (time.time(), notification_id))

  def defer(self, notification_id: str, reason: str) -> None:
    with self.connect() as db:
      db.execute(
        "UPDATE receipts SET attempts=attempts+1, next_attempt_at=?, last_error=? WHERE id=? AND status='pending'",
        (time.time() + 30, reason[:200], notification_id),
      )

  def health_stats(self) -> dict:
    with self.connect() as db:
      count, oldest = db.execute(
        "SELECT COUNT(*), MIN(received_at) FROM receipts WHERE status='pending'"
      ).fetchone()
    return {"status": "ok", "pendingCount": count, "oldestPendingAgeSeconds": max(0, int(time.time() - oldest)) if oldest else 0}
