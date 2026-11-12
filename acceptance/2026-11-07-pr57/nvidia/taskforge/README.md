# TaskForge

A stdlib-only Python task service backed by SQLite. No third-party
dependencies — just the Python standard library.

## Layout

- `taskforge/` — package (`__main__.py` CLI, `db.py` storage, `server.py` HTTP API)
- `test_taskforge.py` — unittest suite
- `README.md` — this file
- `docs/API.md` — HTTP API reference
- `docs/MIGRATION.md` — v1 → v2 schema migration
- `docs/RECOVERY.md` — recovery from an interrupted run

## CLI

Start the HTTP service (binds `127.0.0.1`):

```
python -m taskforge serve --db /path/to/tasks.db --port 8000
```

Add a task (optional `--due` RFC3339 timestamp with timezone offset):

```
python -m taskforge add --db /path/to/tasks.db --title "Write docs"
python -m taskforge add --db /path/to/tasks.db --title "Review" --due "2026-12-31T23:59:59Z"
```

List task titles (filters: `--done`, `--due-before`, `--archived`):

```
python -m taskforge list --db /path/to/tasks.db
python -m taskforge list --db /path/to/tasks.db --done true
python -m taskforge list --db /path/to/tasks.db --archived true
python -m taskforge list --db /path/to/tasks.db --due-before 2026-12-31T23:59:59Z
```

## HTTP API

All bodies and responses are JSON.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | HTML web UI for managing tasks (toggles `done`) |
| `GET` | `/tasks` | List tasks with optional `done`, `due_before`, `archived` filters |
| `POST` | `/tasks` | Create `{"title": "...", "due": "..."}` → `201 {id, title, done, due, archived}` |
| `PATCH` | `/tasks/ID` | Update any of `done`, `archived`, `due` → updated task object |

Query parameters for `GET /tasks` (all optional, combine with AND):

- `done=true` / `done=false` — filter by completion. Any other value → `400`.
- `due_before=TIMESTAMP` — strict earlier-than comparison (`due < value`).
  Tasks with null `due` are excluded. Invalid or timezone-less value → `400`.
- `archived=true` — only archived tasks; `archived=false` — only active tasks.
  Any other value → `400`. Default (no param): archived tasks excluded.

`due` is an RFC3339 timestamp with a timezone offset, normalized to UTC as
`YYYY-MM-DDTHH:MM:SSZ`. Naive (timezone-less), invalid, or non-string values
→ `400`. `null` or omitted → stored as `null`.

Errors:

- Blank or non-string `title`, missing/invalid JSON → `400`
- Non-boolean `done` / `archived` in PATCH → `400`
- Bad `due` / `due_before` → `400`
- Unknown task ID (PATCH) → `404`
- Unknown path → `404`

### Idempotency

`POST /tasks` with an `Idempotency-Key` header replays the same task:
repeating the key returns the original task's `id` instead of creating a
duplicate. The check and insert happen in a single `BEGIN IMMEDIATE`
transaction, so it is safe under concurrent requests.

### Concurrency and persistence

Writes use `BEGIN IMMEDIATE` transactions on a WAL-mode SQLite database,
so concurrent writers are serialized by SQLite itself. All data lives in
the SQLite file given by `--db` and persists across restarts.

## Schema (v2)

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    due TEXT,
    archived INTEGER NOT NULL DEFAULT 0
);
```

An `idempotency_keys` table is also maintained internally.

Existing v1 databases are migrated in place on startup: `due` (nullable
TEXT) and `archived` (INTEGER NOT NULL DEFAULT 0) columns are added only
when absent, preserving all existing data. See `docs/MIGRATION.md`.

## Tests

Run the test suite with the standard library test runner:

```
python -m unittest discover
```
