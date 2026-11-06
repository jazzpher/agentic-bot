# TaskForge Schema Migration (v1 → v2)

## Schema change

The v1 `tasks` table:

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
```

becomes v2 by adding two nullable/defaulted columns:

```sql
ALTER TABLE tasks ADD COLUMN due TEXT;              -- nullable
ALTER TABLE tasks ADD COLUMN archived INTEGER NOT NULL DEFAULT 0;
```

The full v2 schema:

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

## How migration happens

Migration is automatic and runs inside `taskforge.db.init_db()`, which is
called on every server start and on every CLI command:

1. `CREATE TABLE IF NOT EXISTS tasks ...` — a no-op if the table already
   exists; existing rows are preserved.
2. `PRAGMA table_info(tasks)` inspects current columns.
3. If the `due` column is absent, `ALTER TABLE tasks ADD COLUMN due TEXT`
   adds it (existing rows get `NULL`).
4. If the `archived` column is absent, `ALTER TABLE tasks ADD COLUMN archived
   INTEGER NOT NULL DEFAULT 0` adds it (existing rows get `0` / false).

Because columns are only added when missing, running `init_db` repeatedly is
idempotent and never destroys data.

## Safe rollout

1. **Back up the database file first** (copy the `.db`, `.db-wal`, and
   `.db-shm` files if present):
   ```
   cp tasks.db tasks.db.bak.$(date +%Y%m%d%H%M%S)
   ```
2. Deploy the new code and start the server. `init_db` migrates in place.
3. Verify existing tasks are intact (titles, `done` values) and that new
   `due` / `archived` columns default to `NULL` / `0`.
4. Test the new endpoints (`due_before`, `archived` filter, PATCH `archived`,
   PATCH `due`) against a copy before pointing production traffic at it.

## Rollback

If a problem is found after migration:

1. Stop the server.
2. Restore the pre-migration database backup over the current file.
3. Deploy the v1 code and restart.

Because v1's schema does not reference `due` or `archived`, a restored v1
database works with the v1 code without further changes. Data written after
migration (tasks with `due` set, archived tasks) will be lost on restore —
this is why the pre-migration backup should be taken before any v2 writes.

## Verifying migration

After startup, inspect the schema:

```sql
SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks';
-- expect the v2 CREATE TABLE with due TEXT and archived columns

SELECT id, title, done, due, archived FROM tasks;
-- existing rows: due IS NULL, archived = 0
```
