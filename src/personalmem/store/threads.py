"""Thread-based routing store: alternative to fixed time-window sessions.

Threads are semantic continuity units. A capture either continues an existing
open thread (same task/topic) or opens a new one. Closure happens on idle
timeout or router signal — never on a wall-clock flush.

Schema is additive: this module is NOT wired into store/fts.py:connect() yet,
so the live daemon is unaffected. Call ensure_schema(conn) explicitly from
the replay validator or from a future hook.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass


SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL,                  -- 'open' | 'closed'
    opened_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    closed_at TEXT,
    summary TEXT,                          -- legacy alias of narrative; kept for back-compat
    narrative TEXT,                        -- summarizer-generated running narrative
    key_events_json TEXT,                  -- JSON array of bullet strings
    outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_threads_status ON threads(status, last_active_at);

CREATE TABLE IF NOT EXISTS thread_captures (
    thread_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    description TEXT,                      -- one-line activity description from router
    PRIMARY KEY (thread_id, capture_id)
);

CREATE INDEX IF NOT EXISTS idx_thread_captures_capture ON thread_captures(capture_id);
"""


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Lightweight migrations for older dbs."""
    tc_cols = [r[1] for r in conn.execute("PRAGMA table_info(thread_captures)")]
    if "description" not in tc_cols:
        conn.execute("ALTER TABLE thread_captures ADD COLUMN description TEXT")
    t_cols = [r[1] for r in conn.execute("PRAGMA table_info(threads)")]
    for col in ("narrative", "key_events_json", "outcome"):
        if col not in t_cols:
            conn.execute(f"ALTER TABLE threads ADD COLUMN {col} TEXT")


@dataclass
class Thread:
    id: str
    title: str
    status: str
    opened_at: str
    last_active_at: str
    closed_at: str | None
    summary: str | None
    narrative: str | None = None
    key_events_json: str | None = None
    outcome: str | None = None


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _ensure_columns(conn)


def new_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex[:12]}"


def open_thread(conn: sqlite3.Connection, *, title: str, opened_at: str) -> str:
    tid = new_thread_id()
    conn.execute(
        "INSERT INTO threads(id, title, status, opened_at, last_active_at) "
        "VALUES (?, ?, 'open', ?, ?)",
        (tid, title, opened_at, opened_at),
    )
    return tid


def append_capture(
    conn: sqlite3.Connection, *,
    thread_id: str, capture_id: str, at: str,
    description: str | None = None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO thread_captures(thread_id, capture_id, joined_at, description) "
        "VALUES (?, ?, ?, ?)",
        (thread_id, capture_id, at, description),
    )
    # Reopen the thread if it was previously closed: appending a capture
    # means the user returned to the topic, so the thread is no longer a
    # closed historical record. The router only ever picks "continue"
    # when it judges the new capture genuinely belongs here.
    conn.execute(
        "UPDATE threads "
        "   SET last_active_at = ?, "
        "       status = 'open', "
        "       closed_at = NULL "
        " WHERE id = ?",
        (at, thread_id),
    )


def close_thread(
    conn: sqlite3.Connection, *, thread_id: str, closed_at: str, summary: str | None = None
) -> None:
    conn.execute(
        "UPDATE threads SET status='closed', closed_at=?, summary=? WHERE id=?",
        (closed_at, summary, thread_id),
    )


def update_title(conn: sqlite3.Connection, *, thread_id: str, title: str) -> None:
    conn.execute("UPDATE threads SET title=? WHERE id=?", (title, thread_id))


def set_summary(conn: sqlite3.Connection, *, thread_id: str, summary: str) -> None:
    """Overwrite a thread's running narrative. Used by the router after each
    routing decision so the next decision sees the updated topic context."""
    conn.execute("UPDATE threads SET summary=? WHERE id=?", (summary, thread_id))


def save_full_summary(
    conn: sqlite3.Connection, *,
    thread_id: str, title: str, narrative: str,
    key_events: list[str], outcome: str,
) -> None:
    """Cache a complete summarizer output on the thread row. Lets the
    end-of-run .md writer skip another LLM call — incremental summarize
    has already produced the latest summary after each routing decision.
    """
    import json as _json
    conn.execute(
        "UPDATE threads "
        "   SET title = ?, narrative = ?, key_events_json = ?, outcome = ? "
        " WHERE id = ?",
        (title, narrative, _json.dumps(key_events, ensure_ascii=False), outcome, thread_id),
    )


def list_open_threads(
    conn: sqlite3.Connection, *, top_k: int | None = None
) -> list[Thread]:
    sql = "SELECT * FROM threads WHERE status='open' ORDER BY last_active_at DESC"
    args: list = []
    if top_k is not None:
        sql += " LIMIT ?"
        args.append(top_k)
    rows = conn.execute(sql, args).fetchall()
    return [_row_to_thread(r) for r in rows]


def list_recent_threads(
    conn: sqlite3.Connection, *, top_k: int | None = None
) -> list[Thread]:
    """All threads ordered by recency, regardless of open/closed status.

    Threads represent topics, not time blocks — a sleeping thread can be
    resumed when a related capture arrives, even hours/days later. The router
    sees the top-K most-recently-active threads in its prompt; threads
    outside the window aren't visible THIS routing decision but stay in the
    DB and can resurface later if their app/topic returns to the top-K.
    """
    sql = "SELECT * FROM threads ORDER BY last_active_at DESC"
    args: list = []
    if top_k is not None:
        sql += " LIMIT ?"
        args.append(top_k)
    rows = conn.execute(sql, args).fetchall()
    return [_row_to_thread(r) for r in rows]


def get_thread(conn: sqlite3.Connection, thread_id: str) -> Thread | None:
    r = conn.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    return _row_to_thread(r) if r else None


def thread_capture_ids(conn: sqlite3.Connection, thread_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT capture_id FROM thread_captures WHERE thread_id=? ORDER BY joined_at",
        (thread_id,),
    ).fetchall()
    return [r["capture_id"] if isinstance(r, sqlite3.Row) else r[0] for r in rows]


def thread_capture_count(conn: sqlite3.Connection, thread_id: str) -> int:
    r = conn.execute(
        "SELECT COUNT(*) AS c FROM thread_captures WHERE thread_id=?", (thread_id,)
    ).fetchone()
    return r["c"] if isinstance(r, sqlite3.Row) else r[0]


def _row_to_thread(r) -> Thread:
    return Thread(
        id=r["id"],
        title=r["title"],
        status=r["status"],
        opened_at=r["opened_at"],
        last_active_at=r["last_active_at"],
        closed_at=r["closed_at"],
        summary=r["summary"],
        narrative=_safe_col(r, "narrative"),
        key_events_json=_safe_col(r, "key_events_json"),
        outcome=_safe_col(r, "outcome"),
    )


def _safe_col(r, name: str):
    """sqlite3.Row keyed access raises if the column wasn't selected."""
    try:
        return r[name]
    except (IndexError, KeyError):
        return None
