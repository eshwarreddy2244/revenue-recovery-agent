"""
audit.py
SQLite-backed audit trail.

Four tables:
  - audit_log   : one row per (case, step) -- the full recovery
                  timeline for every payment_id.
  - escalations : one row per ticket for a case that needed a human --
                  BLOCKED or UNRECOVERED cases, with a route, priority,
                  a lifecycle STATUS (OPEN / IN_PROGRESS / RESOLVED),
                  and an ASSIGNED_TO owner (NULL until someone claims
                  it from the dashboard). Like batch_runs and
                  issued_links, this is NOT cleared by reset_db -- a
                  ticket is a real queue item a human is meant to work
                  through across multiple pipeline runs, not a
                  snapshot of "what's wrong with the current batch".
                  pipeline.run_batch only opens a NEW ticket for a
                  payment_id if there isn't already an
                  OPEN/IN_PROGRESS one for it, so a still-unresolved
                  case doesn't spawn a fresh ticket every single
                  re-run. It IS cleared by clear_escalations(), which
                  the dashboard calls when you regenerate a fresh
                  synthetic batch -- see that function's docstring for
                  why persistence only makes sense across re-runs of
                  the SAME batch, not across an entirely new one with
                  unrelated payment_ids.
  - batch_runs  : one row per full pipeline run, so the dashboard can
                  show a trend across multiple runs. NOT cleared by
                  reset_db -- history should survive across batches,
                  that's the whole point of it.
  - issued_links: payment_id -> the plink_id already issued for it, so
                  a case that already has a live link is never sent a
                  second one. NOT cleared by reset_db either.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "audit.db"

ESCALATION_STATUSES = ("OPEN", "IN_PROGRESS", "RESOLVED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_id TEXT NOT NULL,
    trigger_event TEXT,
    diagnosed_cause TEXT,
    policy_evaluation TEXT,
    intervention_executed TEXT,
    stopping_rule_hit TEXT,
    plink_id TEXT,
    outcome TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    route TEXT NOT NULL,
    note TEXT,
    priority_score INTEGER,
    amount REAL,
    ltv_tier TEXT,
    timestamp TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    updated_at TEXT,
    assigned_to TEXT
);

CREATE TABLE IF NOT EXISTS batch_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    total_cases INTEGER,
    gross_revenue_recovered REAL,
    net_revenue_preserved REAL,
    net_recovery_rate_pct REAL,
    revenue_at_risk REAL,
    blocked_case_count INTEGER,
    invalid_count INTEGER
);

CREATE TABLE IF NOT EXISTS issued_links (
    payment_id TEXT PRIMARY KEY,
    plink_id TEXT NOT NULL,
    short_url TEXT,
    created_at TEXT NOT NULL
);
"""

# Columns added to `escalations` after its original release. Applied via
# best-effort ALTER TABLE so a database created before this feature existed
# picks them up automatically instead of needing a manual migration step or
# a DB wipe. Safe to run on every connection open: sqlite3 raises
# OperationalError for a column that already exists, which we swallow.
_ESCALATION_MIGRATIONS = [
    "ALTER TABLE escalations ADD COLUMN status TEXT NOT NULL DEFAULT 'OPEN'",
    "ALTER TABLE escalations ADD COLUMN updated_at TEXT",
    "ALTER TABLE escalations ADD COLUMN assigned_to TEXT",
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for statement in _ESCALATION_MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" not in str(exc).lower():
                raise


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """
    `db_path` defaults to the module-level DB_PATH, resolved at CALL
    time (not bound at function-definition time) -- this matters
    because it's what lets tests monkeypatch `audit.DB_PATH` and have
    every caller that doesn't pass an explicit path (including
    pipeline.run_batch, which never does) actually honor it. A plain
    `db_path: Path = DB_PATH` default looks equivalent but silently
    freezes the value at import time and ignores later monkeypatching.
    """
    resolved_path = db_path if db_path is not None else DB_PATH
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(resolved_path)
    # synchronous=NORMAL trades a small amount of crash-durability for far
    # fewer fsync() calls per write -- with FULL (the default), every single
    # commit forces a disk sync, which is what turns a batch of a few hundred
    # log_step() calls into a few hundred fsyncs. On some Docker Desktop
    # volume backends (seen on Windows with a named volume under heavy write
    # load) that many fsyncs in quick succession can surface as
    # "sqlite3.OperationalError: disk I/O error". Reducing sync pressure,
    # combined with batching commits at the caller level (see pipeline.py),
    # is the fix.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    conn.commit()
    return conn


def reset_db(db_path: Path | None = None) -> None:
    """
    Drop and recreate audit_log ONLY -- it reflects just the CURRENT
    batch's step-by-step trail. escalations, batch_runs, and
    issued_links are all deliberately left untouched: an escalation
    ticket is a real queue item meant to be worked and resolved across
    multiple pipeline runs (see the module docstring), batch_runs is
    what the Trends tab needs to survive, and issued_links is what
    idempotency depends on.
    """
    resolved_path = db_path if db_path is not None else DB_PATH
    conn = get_connection(resolved_path)
    conn.executescript("DROP TABLE IF EXISTS audit_log;")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def clear_escalations(db_path: Path | None = None) -> None:
    """
    Delete every escalation ticket, regardless of status.

    Escalation persistence and dedup (see get_open_escalation) are
    built around re-running the SAME batch of events, where a
    payment_id still means the same case from run to run. Clicking
    "Regenerate synthetic batch" breaks that assumption -- it mints a
    fresh set of random payment_ids that share no relationship with
    the previous batch's, so the old batch's tickets can never be
    recognized as resolved or matched again; they'd just sit in the
    queue accumulating forever, growing by a new batch's worth of
    tickets every time someone clicks regenerate. Since those tickets
    reference cases that no longer exist anywhere the app can act on,
    the correct behavior on regenerate is to clear them, not persist
    them - unlike batch_runs (trend history is explicitly meant to
    survive regeneration) or issued_links (harmless if stale, since a
    fresh random payment_id will never collide with an old one).

    This DOES commit immediately, same as update_escalation_status --
    it's a discrete, deliberate, infrequent action, not part of the
    batched pipeline write path.
    """
    conn = get_connection(db_path)
    conn.execute("DELETE FROM escalations")
    conn.commit()
    conn.close()


def log_step(
    conn: sqlite3.Connection,
    recovery_id: str,
    trigger_event: str,
    diagnosed_cause: str | None,
    policy_evaluation: str,
    intervention_executed: str,
    stopping_rule_hit: str | None,
    plink_id: str | None,
    outcome: str,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_log
            (recovery_id, trigger_event, diagnosed_cause, policy_evaluation,
             intervention_executed, stopping_rule_hit, plink_id, outcome, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recovery_id,
            trigger_event,
            diagnosed_cause,
            policy_evaluation,
            intervention_executed,
            stopping_rule_hit,
            plink_id,
            outcome,
            timestamp,
        ),
    )
    # No commit here deliberately -- see pipeline.run_batch, which commits
    # once after the whole batch is logged rather than once per row. With
    # ~150-200 cases x up to 3 steps each, per-row commits meant several
    # hundred fsyncs in one run, which is what was surfacing as
    # "sqlite3.OperationalError: disk I/O error" on some Docker volume
    # backends under write load. Callers that use this function directly
    # outside of pipeline.run_batch are responsible for committing.


def log_escalation(
    conn: sqlite3.Connection,
    payment_id: str,
    reason: str,
    route: str,
    note: str,
    priority_score: int,
    amount: float,
    ltv_tier: str | None,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO escalations
            (payment_id, reason, route, note, priority_score, amount, ltv_tier,
             timestamp, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?)
        """,
        (payment_id, reason, route, note, priority_score, amount, ltv_tier, timestamp, timestamp),
    )
    # Also deliberately uncommitted -- see log_step's comment above.


def get_open_escalation(conn: sqlite3.Connection, payment_id: str) -> dict | None:
    """
    Return the most recent OPEN or IN_PROGRESS escalation ticket for
    this payment_id, if one exists -- used by pipeline.run_batch to
    decide whether a still-unresolved case needs a fresh ticket or
    already has one being worked. A RESOLVED ticket doesn't block a
    new one: if the case escalates again later, that's a legitimate
    new issue, not a duplicate of one that was already closed out.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM escalations
        WHERE payment_id = ? AND status IN ('OPEN', 'IN_PROGRESS')
        ORDER BY id DESC LIMIT 1
        """,
        (payment_id,),
    ).fetchone()
    return dict(row) if row else None


def update_escalation_status(
    escalation_id: int, new_status: str, updated_at: str, db_path: Path | None = None
) -> None:
    """
    Update a single escalation ticket's status. Unlike log_step/
    log_escalation/log_batch_run, this DOES commit immediately -- it's
    a discrete, infrequent, single-row write triggered interactively
    from the dashboard (someone clicking to mark a ticket resolved),
    not part of the few-hundred-row batch write that caused the
    fsync-storm bug those other functions were changed to avoid. A
    single commit per interactive edit is exactly the right amount of
    durability here.
    """
    if new_status not in ESCALATION_STATUSES:
        raise ValueError(f"Invalid escalation status: {new_status!r}, expected one of {ESCALATION_STATUSES}")
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE escalations SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, updated_at, escalation_id),
    )
    conn.commit()
    conn.close()


def update_escalation_assignment(
    escalation_id: int, assigned_to: str | None, updated_at: str, db_path: Path | None = None
) -> None:
    """
    Claim or unclaim a single escalation ticket. `assigned_to=None`
    (or an empty string, normalized to None) unassigns it -- back to
    the shared pool, same as it starts out when a ticket is first
    opened. Commits immediately, same reasoning as
    update_escalation_status: a discrete interactive edit, not part
    of a batched pipeline write.
    """
    normalized = assigned_to if assigned_to else None
    conn = get_connection(db_path)
    conn.execute(
        "UPDATE escalations SET assigned_to = ?, updated_at = ? WHERE id = ?",
        (normalized, updated_at, escalation_id),
    )
    conn.commit()
    conn.close()


def log_batch_run(
    conn: sqlite3.Connection,
    run_timestamp: str,
    total_cases: int,
    gross_revenue_recovered: float,
    net_revenue_preserved: float,
    net_recovery_rate_pct: float,
    revenue_at_risk: float,
    blocked_case_count: int,
    invalid_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO batch_runs
            (run_timestamp, total_cases, gross_revenue_recovered, net_revenue_preserved,
             net_recovery_rate_pct, revenue_at_risk, blocked_case_count, invalid_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_timestamp, total_cases, gross_revenue_recovered, net_revenue_preserved,
            net_recovery_rate_pct, revenue_at_risk, blocked_case_count, invalid_count,
        ),
    )
    # Also deliberately uncommitted -- caller commits once for the whole run.


def query_batch(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return the full audit log as a list of dicts, most recent first."""
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def query_escalations(db_path: Path | None = None, status: str | None = None) -> list[dict[str, Any]]:
    """
    Return escalation records, highest priority first, open work
    before resolved work. Pass `status` to filter to exactly one
    status; omit it to return everything.
    """
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM escalations WHERE status = ? ORDER BY priority_score DESC, id DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM escalations
            ORDER BY (status = 'RESOLVED') ASC, priority_score DESC, id DESC
            """
        ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def query_batch_run_history(db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent `limit` batch runs, oldest first (chart-friendly order)."""
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM batch_runs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in reversed(rows)]


def get_last_plink_for_payment(conn: sqlite3.Connection, payment_id: str) -> str | None:
    """
    Return an already-issued plink_id for this payment_id, if any --
    used to avoid issuing a SECOND live payment link for a case that
    already has one outstanding (idempotency). Backed by the
    `issued_links` table, which -- unlike audit_log -- is never
    cleared by reset_db, so this check holds even across separate
    pipeline runs, not just within one.
    """
    row = conn.execute(
        "SELECT plink_id FROM issued_links WHERE payment_id = ?", (payment_id,)
    ).fetchone()
    return row[0] if row else None


def record_issued_link(
    conn: sqlite3.Connection, payment_id: str, plink_id: str, short_url: str | None, timestamp: str
) -> None:
    """Record that a payment link now exists for this payment_id, for future idempotency checks."""
    conn.execute(
        """
        INSERT INTO issued_links (payment_id, plink_id, short_url, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(payment_id) DO UPDATE SET
            plink_id = excluded.plink_id,
            short_url = excluded.short_url,
            created_at = excluded.created_at
        """,
        (payment_id, plink_id, short_url, timestamp),
    )
    # Also deliberately uncommitted -- see log_step's comment above. This
    # one matters especially for the idempotency check to be correct: an
    # uncommitted INSERT is still visible to reads on the SAME connection
    # within the same run (SQLite read-your-own-writes), so
    # get_last_plink_for_payment still works correctly mid-batch even
    # before the final commit.
