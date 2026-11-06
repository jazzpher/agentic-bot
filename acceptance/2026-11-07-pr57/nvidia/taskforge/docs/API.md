# TaskForge HTTP API Reference

TaskForge is a stdlib-only Python task service backed by SQLite (WAL mode).
All request bodies and responses are JSON. The service binds to `127.0.0.1`
and is started with `python -m taskforge serve --db PATH --port N`.

## Base URL

```
http://127.0.0.1:<port>
```

## Task object

A task is represented as a JSON object with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `id` | integer | Task identifier (assigned on creation) |
| `title` | string | Task title (non-blank) |
| `done` | boolean | Completion status (`false` by default) |
| `due` | string or null | RFC3339 due timestamp normalized to UTC as `YYYY-MM-DDTHH:MM:SSZ`, or `null` |
| `archived` | boolean | Archive status (`false` by default) |

## Endpoints

### `GET /`

Return the HTML web UI for managing tasks (toggles `done`).

---

### `GET /tasks`

List tasks. Returns a JSON array of task objects ordered by `id`.

Query parameters (all optional, combine with AND):

| Parameter | Values | Effect |
|-----------|--------|--------|
| `done` | `true` / `false` | Filter by completion status. Any other value → `400`. |
| `due_before` | RFC3339 timestamp | Strict earlier-than comparison (`due < value`). Tasks with null `due` are excluded. Invalid or timezone-less value → `400`. |
| `archived` | `true` / `false` | `true` returns only archived tasks; `false` returns only active (non-archived) tasks. Any other value → `400`. |

Default behaviour (no `archived` param): archived tasks are excluded.

**Examples**

```
GET /tasks                    # active tasks only
GET /tasks?done=true          # completed tasks
GET /tasks?archived=true      # only archived tasks
GET /tasks?archived=false     # only active tasks
GET /tasks?due_before=2026-12-31T23:59:59Z
                              # tasks due strictly before that instant
GET /tasks?done=false&archived=false&due_before=2026-06-01T00:00:00Z
                              # combines all three filters
```

---

### `POST /tasks`

Create a new task.

**Request body**

```json
{
  "title": "Write docs",
  "due": "2026-12-31T23:59:59+05:30"   // optional; null or omitted allowed
}
```

- `title` must be a non-blank string → `400` otherwise.
- `due` must be a string RFC3339 timestamp with a timezone offset, normalized
  to UTC (`YYYY-MM-DDTHH:MM:SSZ`). Naive (timezone-less), invalid, or
  non-string values → `400`. `null` or omitted → `due` stored as `null`.

**Response** — `201` with the created task object.

**Idempotency** — include an `Idempotency-Key` header; repeating the same key
returns the original task's `id` instead of creating a duplicate. The check and
insert run in a single `BEGIN IMMEDIATE` transaction, safe under concurrency.

---

### `PATCH /tasks/ID`

Partially update a task. Any subset of `done`, `archived`, `due` may be sent.

**Request body**

```json
{
  "done": true,
  "archived": false,
  "due": "2026-12-31T23:59:59Z"
}
```

- `done` / `archived` must be booleans when present → `400` otherwise.
- `due` must be a string RFC3339 timestamp with timezone → normalized to UTC.
  Naive, invalid, or non-string → `400`. `null` is rejected (send no `due`
  field to leave it unchanged).
- Unknown task ID → `404`.
- If no recognized fields are present, the current task is returned unchanged
  (`200`).

**Response** — `200` with the updated task object.

---

### Errors

| Status | Meaning |
|--------|---------|
| `400` | Bad request: blank/non-string title, invalid JSON, bad boolean filter, bad `due`, bad `due_before`, bad `archived` query value |
| `404` | Unknown task ID or unknown path |
| `500` | Server error |

## Idempotency

`POST /tasks` supports the `Idempotency-Key` header. The same key sent twice
returns the same task `id` (no duplicate created). The lookup and insert are
serialized in a single `BEGIN IMMEDIATE` transaction, so concurrent requests
with the same key are safe.

## Concurrency and persistence

All writes use `BEGIN IMMEDIATE` transactions on a WAL-mode SQLite database,
so concurrent writers are serialized by SQLite itself. All data lives in the
SQLite file given by `--db` and persists across server restarts.
