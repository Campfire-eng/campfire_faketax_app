"""SQLite storage: one short-lived connection per call so request threads and event workers can share it."""

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS grants (
    customer_id INTEGER PRIMARY KEY,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at REAL NOT NULL,
    install_config TEXT NOT NULL DEFAULT '{}',
    connected_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_jti (jti TEXT PRIMARY KEY, exp REAL NOT NULL);
CREATE TABLE IF NOT EXISTS frame_sessions (
    jti TEXT PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    exp REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    customer_id INTEGER PRIMARY KEY,
    simulate_failure TEXT NOT NULL DEFAULT 'none'
);
CREATE TABLE IF NOT EXISTS invoices (
    customer_id INTEGER NOT NULL,
    invoice_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (customer_id, invoice_id)
);
"""

SIMULATE_OPTIONS: tuple[str, ...] = ("none", "500", "slow")


@dataclass(frozen=True)
class Grant:
    customer_id: int
    access_token: str
    refresh_token: str | None
    expires_at: float
    install_config: dict[str, str]
    connected_at: float


class Store:
    def __init__(self, *, path: str) -> None:
        self.path: str = path
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db: sqlite3.Connection = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def save_tokens(self, *, customer_id: int, tokens: dict[str, Any]) -> None:
        expires_at: float = time.time() + float(tokens.get("expires_in") or 3600)
        with self._db() as db:
            db.execute(
                """
                INSERT INTO grants (customer_id, access_token, refresh_token, expires_at, connected_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(customer_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = COALESCE(excluded.refresh_token, grants.refresh_token),
                    expires_at = excluded.expires_at
                """,
                (customer_id, tokens["access_token"], tokens.get("refresh_token"), expires_at, time.time()),
            )

    def save_install_config(self, *, customer_id: int, config: dict[str, str]) -> None:
        with self._db() as db:
            db.execute("UPDATE grants SET install_config = ? WHERE customer_id = ?", (json.dumps(config), customer_id))

    def grant(self, *, customer_id: int) -> Grant | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM grants WHERE customer_id = ?", (customer_id,)).fetchone()
        return _grant(row) if row else None

    def grants(self) -> list[Grant]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM grants ORDER BY connected_at").fetchall()
        return [_grant(row) for row in rows]

    def delete_grant(self, *, customer_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM grants WHERE customer_id = ?", (customer_id,))

    def claim_jti(self, jti: str, exp: float) -> bool:
        with self._db() as db:
            db.execute("DELETE FROM seen_jti WHERE exp < ?", (time.time() - 60,))
            cursor = db.execute("INSERT OR IGNORE INTO seen_jti (jti, exp) VALUES (?, ?)", (jti, exp))
            return cursor.rowcount == 1

    def open_frame_session(self, *, jti: str, customer_id: int, exp: float) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO frame_sessions (jti, customer_id, exp) VALUES (?, ?, ?)",
                (jti, customer_id, exp),
            )

    def frame_session_open(self, *, jti: str, customer_id: int) -> bool:
        with self._db() as db:
            db.execute("DELETE FROM frame_sessions WHERE exp < ?", (time.time(),))
            row = db.execute(
                "SELECT 1 FROM frame_sessions WHERE jti = ? AND customer_id = ?", (jti, customer_id)
            ).fetchone()
        return row is not None

    def simulate_failure(self, *, customer_id: int) -> str:
        with self._db() as db:
            row = db.execute("SELECT simulate_failure FROM settings WHERE customer_id = ?", (customer_id,)).fetchone()
        return row["simulate_failure"] if row else "none"

    def set_simulate_failure(self, *, customer_id: int, value: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings (customer_id, simulate_failure) VALUES (?, ?)",
                (customer_id, value if value in SIMULATE_OPTIONS else "none"),
            )

    def cache_invoice(self, *, customer_id: int, invoice: dict[str, Any]) -> None:
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO invoices (customer_id, invoice_id, data, fetched_at) VALUES (?, ?, ?, ?)",
                (customer_id, invoice["id"], json.dumps(invoice), time.time()),
            )

    def forget_invoice(self, *, customer_id: int, invoice_id: int) -> None:
        with self._db() as db:
            db.execute("DELETE FROM invoices WHERE customer_id = ? AND invoice_id = ?", (customer_id, invoice_id))

    def cached_invoice_count(self, *, customer_id: int) -> int:
        with self._db() as db:
            row = db.execute("SELECT COUNT(*) AS n FROM invoices WHERE customer_id = ?", (customer_id,)).fetchone()
        return int(row["n"])


def _grant(row: sqlite3.Row) -> Grant:
    return Grant(
        customer_id=row["customer_id"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        expires_at=row["expires_at"],
        install_config=json.loads(row["install_config"] or "{}"),
        connected_at=row["connected_at"],
    )
