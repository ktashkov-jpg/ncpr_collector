# -*- coding: utf-8 -*-
"""SQLite state: work queue, results, request log, and the daily counter.

Two things here are load-bearing safety, not bookkeeping:

1. **The daily counter is persisted and keyed by calendar date.** An
   in-memory counter resets when the container restarts, so a crash-loop or
   a routine `docker restart` would silently blow through the daily cap
   against an allowlisted service. Counting in the database is the only
   version of the cap that survives a restart.

2. **GTINs are stored as TEXT, always.** The official response may carry a
   leading zero (`05712249101367`); any numeric coercion destroys it. This
   project has already been bitten three separate times by Excel doing
   exactly that (HANDOVER §11).
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    task_id        TEXT PRIMARY KEY,   -- 'fwd:15955' | 'rev:05712249101367'
    kind           TEXT NOT NULL,      -- 'forward' | 'reverse'
    key            TEXT NOT NULL,      -- national id, or GTIN as text
    priority       INTEGER NOT NULL,   -- lower runs first
    reason         TEXT,               -- why this row is queued (auditable)
    status         TEXT NOT NULL DEFAULT 'pending',
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_queue_run ON queue(status, priority, task_id);

CREATE TABLE IF NOT EXISTS product (
    task_id                      TEXT NOT NULL,
    medicinal_product_identifier TEXT,
    gtin_raw                     TEXT,   -- exactly as returned, TEXT
    gtin14                       TEXT,
    ean13_derived                TEXT,
    checksum_valid               INTEGER,
    name_bg                      TEXT,
    name_en                      TEXT,
    authorization_number         TEXT,
    final_pack                   TEXT,
    reg_number                   TEXT,   -- carried from Annex 4, not the service
    retrieved_at                 TEXT NOT NULL,
    raw_path                     TEXT,
    raw_sha256                   TEXT
);
CREATE INDEX IF NOT EXISTS ix_product_gtin ON product(gtin14);
CREATE INDEX IF NOT EXISTS ix_product_natid ON product(medicinal_product_identifier);

CREATE TABLE IF NOT EXISTS request_log (
    ts            TEXT NOT NULL,
    task_id       TEXT,
    http_status   INTEGER,
    soap_fault    TEXT,
    elapsed_ms    INTEGER,
    bytes         INTEGER,
    wsdl_sha256   TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS daily_counter (
    day       TEXT PRIMARY KEY,    -- YYYY-MM-DD, local
    used      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Output of listMedicinalProducts. Carries no GTIN (the list item type has
-- no gtins field) but does carry inn, atcCodes, medicamentForm, quantity,
-- medicamentUnit and finalPack for every product -- i.e. the authoritative
-- pack-selection metadata, obtained in a handful of paged calls rather than
-- one call per product.
CREATE TABLE IF NOT EXISTS catalogue (
    medicinal_product_identifier TEXT NOT NULL,
    register_code                TEXT NOT NULL,
    register_medicament_id       TEXT,
    register_name                TEXT,
    name_bg                      TEXT,
    name_en                      TEXT,
    inn                          TEXT,
    atc_codes                    TEXT,
    authorization_holder         TEXT,
    producer                     TEXT,
    medicament_form              TEXT,
    quantity                     TEXT,
    medicament_unit              TEXT,
    final_pack                   TEXT,
    published_at                 TEXT,
    retrieved_at                 TEXT NOT NULL,
    PRIMARY KEY (medicinal_product_identifier, register_code)
);
CREATE INDEX IF NOT EXISTS ix_catalogue_reg ON catalogue(register_code);
"""


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- queue ----------

    def add_task(self, task_id: str, kind: str, key: str,
                 priority: int, reason: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO queue(task_id, kind, key, priority, reason) "
            "VALUES (?,?,?,?,?)", (task_id, kind, key, priority, reason))
        self.db.commit()
        return cur.rowcount > 0

    def next_task(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM queue WHERE status='pending' "
            "ORDER BY priority, task_id LIMIT 1").fetchone()

    def finish(self, task_id: str, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE queue SET status=?, last_error=?, attempts=attempts+1, "
            "completed_at=? WHERE task_id=?",
            (status, error, dt.datetime.now(dt.timezone.utc).isoformat(), task_id))
        self.db.commit()

    def defer(self, task_id: str, error: str) -> None:
        """Record a failure but leave the row runnable for a later day."""
        self.db.execute(
            "UPDATE queue SET attempts=attempts+1, last_error=? WHERE task_id=?",
            (error, task_id))
        self.db.commit()

    def queue_stats(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT status, COUNT(*) n FROM queue GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---------- results ----------

    def save_product(self, **kw) -> None:
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        self.db.execute(f"INSERT INTO product({cols}) VALUES ({marks})",
                        tuple(kw.values()))
        self.db.commit()

    def log(self, **kw) -> None:
        kw.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
        cols = ", ".join(kw)
        marks = ", ".join("?" * len(kw))
        self.db.execute(f"INSERT INTO request_log({cols}) VALUES ({marks})",
                        tuple(kw.values()))
        self.db.commit()

    # ---------- daily cap (restart-safe) ----------

    def used_today(self, day: str) -> int:
        row = self.db.execute(
            "SELECT used FROM daily_counter WHERE day=?", (day,)).fetchone()
        return row["used"] if row else 0

    def consume(self, day: str) -> int:
        """Count a request BEFORE it is sent. Counting after the response
        would fail to count a request that was sent and then crashed."""
        self.db.execute(
            "INSERT INTO daily_counter(day, used) VALUES(?, 1) "
            "ON CONFLICT(day) DO UPDATE SET used = used + 1", (day,))
        self.db.commit()
        return self.used_today(day)

    # ---------- catalogue ----------

    def save_catalogue(self, rows: list[dict]) -> int:
        """Upsert list items. Re-running enumeration refreshes rather than
        duplicating -- the same product legitimately appears in more than one
        register, which is why the key is (identifier, register_code)."""
        if not rows:
            return 0
        columns = list(rows[0])
        marks = ", ".join("?" * len(columns))
        self.db.executemany(
            f"INSERT OR REPLACE INTO catalogue({', '.join(columns)}) "
            f"VALUES ({marks})",
            [tuple(row[c] for c in columns) for row in rows])
        self.db.commit()
        return len(rows)

    def catalogue_stats(self) -> list[tuple[str, int, int]]:
        return [(r["register_code"], r["n"], r["ids"]) for r in self.db.execute(
            "SELECT register_code, COUNT(*) n, "
            "COUNT(DISTINCT medicinal_product_identifier) ids "
            "FROM catalogue GROUP BY register_code ORDER BY register_code")]

    # ---------- meta ----------

    def get_meta(self, k: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute(
            "INSERT INTO meta(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self.db.commit()
