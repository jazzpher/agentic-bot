# TaskForge Recovery Guide

This document covers recovery from an interrupted server run and how to
verify that the database is consistent afterward.

## What can be interrupted

- Server process killed / crashed mid-request
- Machine reboot or container restart
- Migration (`init_db`) interrupted while adding columns
- Concurrent write collision (SQLite `SQLITE_BUSY`)

TaskForge uses WAL-mode SQLite with `BEGIN IMMEDIATE` transactions, so a
transaction is either fully committed or fully rolled back — there are no
partial writes visible to other connections.

## Recovery steps

### 1. Restart the server

Simply start the service again:

```
python -m taskforge serve --db /path/to/tasks.db --port 8000
```

`init_db` runs on startup and is idempotent: `CREATE TABLE IF NOT EXISTS` is a
no-op for existing tables, and `ALTER TABLE ... ADD COLUMN` is skipped when the
column already exists. Repeated startups are safe.

### 2. Check for leftover WAL files

If the previous process exited uncleanly, you may see `tasks.db-wal` and
`tasks.db-shm` alongside `tasks.db`. These are normal WAL-mode files; SQLite
will reconcile them on the next connection. Do not delete them manually while
the server is running.

### 3. Verify integrity

Run an integrity check against the database file:

```python
import sqlite3
conn = sqlite3.connect("/path/to/tasks.db")
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
# expect "ok"
conn.close()
```

You can also run:

```python
conn.execute("PRAGMA foreign_key_check").fetchall()
# expect [] (no foreign key violations)
```

### 4. Verify schema is at v2

```python
cur = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
)
print(cur.fetchone()[0])
# expect columns: id, title, done, due, archived
```

Missing columns mean migration did not complete; restarting the server will
run `init_db` and add them.

### 5. Verify data is intact

```python
cur = conn.execute("SELECT COUNT(*) FROM tasks")
print("task count:", cur.fetchone()[0])

cur = conn.execute("SELECT id, title, done, due, archived FROM tasks ORDER BY id")
for row in cur.fetchall():
    print(row)
```

Existing v1 rows should have `due IS NULL` and `archived = 0` after migration.

### 6. Verify the API responds

Start the server and hit `GET /tasks`:

```bash
curl -s http://127.0.0.1:8000/tasks
# expect a JSON array (possibly empty) with 200 status
```

If it returns `200` and the expected array, the service is recovered.

## If integrity check fails

If `PRAGMA integrity_check` returns anything other than `ok`:

1. Stop the server.
2. Restore from the most recent backup (see `docs/MIGRATION.md` for backup
   guidance — back up `.db`, `.db-wal`, `.db-shm` together).
3. Restart the server and re-run the verification steps above.

## Prevention

- Take a backup before deploying a new version.
- Prefer stopping the server cleanly (SIGTERM) rather than `kill -9`.
- Ensure only one server process opens a given database file at a time;
  concurrent servers on the same file can cause `SQLITE_BUSY` errors.
