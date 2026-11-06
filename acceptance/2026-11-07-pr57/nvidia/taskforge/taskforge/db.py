"""SQLite storage layer for TaskForge."""

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    due TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    task_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"""


def connect(db_path):
    """Open a connection to the SQLite database file.

    Uses autocommit mode (isolation_level=None) so callers manage
    transactions explicitly with BEGIN IMMEDIATE / COMMIT, which is what
    makes concurrent writes safe.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _table_columns(conn, table_name):
    """Return the set of column names for a table."""
    cur = conn.execute("PRAGMA table_info(%s)" % table_name)
    return {row["name"] for row in cur.fetchall()}


def init_db(conn):
    """Create tables (if missing) and migrate any v1 schema in place.

    Safe to run on every startup: CREATE TABLE IF NOT EXISTS is a no-op
    for existing tables, and columns are only added when absent, so
    repeated startups are idempotent and existing data is preserved.
    """
    conn.executescript(SCHEMA)
    columns = _table_columns(conn, "tasks")
    if "due" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN due TEXT")
    if "archived" not in columns:
        conn.execute("ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")


def parse_due(value):
    """Parse an RFC3339 due timestamp and normalize to UTC.

    Returns a string like ``YYYY-MM-DDTHH:MM:SSZ``, or ``None`` when
    ``value`` is ``None`` (null is allowed).

    Raises ``ValueError`` if the value is not a string, is naive (no
    timezone offset), or is not a valid RFC3339 timestamp.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("due must be a string or null")
    try:
        # Python 3.11+ fromisoformat understands a trailing "Z".
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid due timestamp")
    if dt.tzinfo is None:
        raise ValueError("due must include a timezone offset (naive timestamps rejected)")
    dt_utc = dt.astimezone(timezone.utc)
    # Truncate any sub-second precision to match the canonical format.
    return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def create_task(conn, title, due=None):
    """Insert a task and return its new row id."""
    due_normalized = parse_due(due)  # raises ValueError on bad input
    cur = conn.execute(
        "INSERT INTO tasks (title, done, due) VALUES (?, 0, ?)",
        (title, due_normalized),
    )
    return cur.lastrowid


def list_tasks(conn, done=None, due_before=None, archived=None):
    """List tasks with optional done / due_before / archived filters.

    By default archived tasks are excluded (archived=false). All filters
    combine with AND. ``due_before`` uses a strict earlier-than comparison
    and automatically excludes tasks with a null due.
    """
    conditions = []
    params = []

    # Default: exclude archived tasks.
    if archived is None:
        archived = False
    if archived:
        conditions.append("archived = 1")
    else:
        conditions.append("archived = 0")

    if done is not None:
        conditions.append("done = ?")
        params.append(1 if done else 0)

    if due_before is not None:
        conditions.append("due IS NOT NULL AND due < ?")
        params.append(due_before)

    sql = "SELECT id, title, done, due, archived FROM tasks"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY id"

    cur = conn.execute(sql, params)
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "done": bool(row["done"]),
            "due": row["due"],
            "archived": bool(row["archived"]),
        }
        for row in cur.fetchall()
    ]


def get_task(conn, task_id):
    cur = conn.execute(
        "SELECT id, title, done, due, archived FROM tasks WHERE id = ?", (task_id,)
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "done": bool(row["done"]),
        "due": row["due"],
        "archived": bool(row["archived"]),
    }


def set_done(conn, task_id, done):
    cur = conn.execute(
        "UPDATE tasks SET done = ? WHERE id = ?", (1 if done else 0, task_id)
    )
    if cur.rowcount == 0:
        return None
    return get_task(conn, task_id)


def set_archived(conn, task_id, archived):
    cur = conn.execute(
        "UPDATE tasks SET archived = ? WHERE id = ?", (1 if archived else 0, task_id)
    )
    if cur.rowcount == 0:
        return None
    return get_task(conn, task_id)


def set_due(conn, task_id, due):
    """Set the due timestamp (string or None). Raises ValueError on bad input."""
    due_normalized = parse_due(due)
    cur = conn.execute(
        "UPDATE tasks SET due = ? WHERE id = ?", (due_normalized, task_id)
    )
    if cur.rowcount == 0:
        return None
    return get_task(conn, task_id)


def find_or_create_task(conn, title, idempotency_key=None, due=None):
    """Atomically reuse an idempotency key or insert a new task.

    Must be called inside a write transaction (BEGIN IMMEDIATE) so the
    check-then-insert is serialized against concurrent requests.
    Returns (task, created).
    """
    if idempotency_key is not None:
        cur = conn.execute(
            "SELECT task_id FROM idempotency_keys WHERE key = ?", (idempotency_key,)
        )
        row = cur.fetchone()
        if row is not None:
            return get_task(conn, row["task_id"]), False
    task_id = create_task(conn, title, due=due)
    task = get_task(conn, task_id)
    if idempotency_key is not None:
        conn.execute(
            "INSERT INTO idempotency_keys (key, task_id) VALUES (?, ?)",
            (idempotency_key, task_id),
        )
    return task, True
