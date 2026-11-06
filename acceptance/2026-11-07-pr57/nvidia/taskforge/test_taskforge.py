"""TaskForge test suite.

Runnable with: python -m unittest discover
"""

import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from taskforge import db
from taskforge.server import Handler, serve


class BaseHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="taskforge-test-")
        cls.db_path = os.path.join(cls.tmpdir, "tasks.db")
        conn = db.connect(cls.db_path)
        db.init_db(conn)
        conn.close()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.db_path = cls.db_path
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    def setUp(self):
        conn = db.connect(self.db_path)
        try:
            conn.execute("DELETE FROM tasks")
            conn.execute("DELETE FROM idempotency_keys")
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def request(self, method, path, body=None, headers=None):
        """Issue an HTTP request; return (status, headers_dict, parsed_body, raw)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdr = {}
        if body is not None:
            hdr["Content-Type"] = "application/json"
        if headers:
            hdr.update(headers)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        conn.request(method, path, body=payload, headers=hdr)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        headers_dict = {}
        for key, value in resp.getheaders():
            headers_dict[key] = value
        return resp.status, headers_dict, data, raw

    def post(self, title, key=None, **extra):
        body = {"title": title}
        body.update(extra)
        headers = {"Idempotency-Key": key} if key else None
        return self.request("POST", "/tasks", body, headers=headers)


class ApiTests(BaseHttpTest):
    def test_server_starts_and_list_empty(self):
        status, _, data, _ = self.request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(data, [])

    def test_create_task(self):
        status, _, data, _ = self.post("Buy milk")
        self.assertEqual(status, 201)
        self.assertEqual(data["title"], "Buy milk")
        self.assertEqual(data["done"], False)
        self.assertIsInstance(data["id"], int)

    def test_list_after_create(self):
        self.post("one")
        self.post("two")
        status, _, data, _ = self.request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([t["title"] for t in data], ["one", "two"])
        for t in data:
            self.assertEqual(set(t), {"id", "title", "done", "due", "archived"})
            self.assertEqual(t["archived"], False)

    def test_blank_title_400(self):
        for title in ["", "   ", "\t\n"]:
            status, _, _, _ = self.post(title)
            self.assertEqual(status, 400, title)

    def test_nonstring_title_400(self):
        for title in [7, 3.14, None, True, ["x"], {"t": 1}]:
            status, _, _, _ = self.post(title)
            self.assertEqual(status, 400, title)

    def test_missing_title_400(self):
        status, _, _, _ = self.request("POST", "/tasks", {})
        self.assertEqual(status, 400)

    def test_invalid_json_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks", body=b"{not valid json",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_empty_body_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks", body=b"",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_patch_done(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        status, _, data, _ = self.request("PATCH", "/tasks/%d" % tid, {"done": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], tid)
        self.assertEqual(data["title"], "todo")
        self.assertEqual(data["done"], True)
        status, _, data, _ = self.request("PATCH", "/tasks/%d" % tid, {"done": False})
        self.assertEqual(status, 200)
        self.assertEqual(data["done"], False)

    def test_patch_unknown_id_404(self):
        status, _, _, _ = self.request("PATCH", "/tasks/999999", {"done": True})
        self.assertEqual(status, 404)

    def test_patch_invalid_body_400(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        for body in [{"done": "yes"}, {}, {"done": 1}, "not-an-object"]:
            status, _, _, _ = self.request("PATCH", "/tasks/%d" % tid, body)
            self.assertEqual(status, 400, body)

    def test_patch_invalid_json_400(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("PATCH", "/tasks/%d" % tid, body=b"{bad",
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 400)

    def test_get_unknown_path_404(self):
        status, _, _, _ = self.request("GET", "/tasks/5")
        self.assertEqual(status, 404)
        status, _, _, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_done_filter(self):
        _, _, open_task, _ = self.post("open")
        _, _, closed_task, _ = self.post("closed")
        self.request("PATCH", "/tasks/%d" % closed_task["id"], {"done": True})
        status, _, data, _ = self.request("GET", "/tasks?done=true")
        self.assertEqual(status, 200)
        self.assertEqual(
            [t["id"] for t in data], [closed_task["id"]],
        )
        self.assertTrue(all(t["done"] for t in data))
        status, _, data, _ = self.request("GET", "/tasks?done=false")
        self.assertEqual(status, 200)
        self.assertEqual(
            [t["id"] for t in data], [open_task["id"]],
        )
        self.assertFalse(any(t["done"] for t in data))

    def test_invalid_done_filter_400(self):
        status, _, _, _ = self.request("GET", "/tasks?done=yes")
        self.assertEqual(status, 400)
        status, _, _, _ = self.request("GET", "/tasks?done=1")
        self.assertEqual(status, 400)
        status, _, _, _ = self.request("GET", "/tasks?done=")
        self.assertEqual(status, 400)

    def test_idempotency_repeat_same_id(self):
        key = "idem-key-abc"
        status1, _, data1, _ = self.post("idem task", key=key)
        self.assertEqual(status1, 201)
        # Repeat with the same key (even a different title) returns the same task.
        status2, _, data2, _ = self.post("different title", key=key)
        self.assertEqual(status2, 201)
        self.assertEqual(data2["id"], data1["id"])
        self.assertEqual(data2["title"], "idem task")
        # A new key creates a new task.
        status3, _, data3, _ = self.post("another task", key="idem-key-def")
        self.assertEqual(status3, 201)
        self.assertNotEqual(data3["id"], data1["id"])

    def test_concurrent_writes(self):
        n = 40
        results = [None] * n
        errors = []

        def post(i):
            last_exc = None
            for attempt in range(5):  # retry transient resets under burst load
                try:
                    c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
                    payload = json.dumps({"title": "conc-%d" % i}).encode("utf-8")
                    c.request("POST", "/tasks", body=payload,
                              headers={"Content-Type": "application/json"})
                    resp = c.getresponse()
                    resp.read()
                    results[i] = resp.status
                    c.close()
                    return
                except ConnectionResetError as exc:
                    last_exc = exc
                    try:
                        c.close()
                    except Exception:
                        pass
                    continue
                except Exception as exc:  # pragma: no cover - failure path
                    last_exc = exc
                    break
            errors.append(last_exc)

        threads = [threading.Thread(target=post, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertFalse(errors, errors)
        self.assertTrue(all(s == 201 for s in results if s is not None), results)
        self.assertTrue(all(s is not None for s in results), results)
        status, _, data, _ = self.request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual(len(data), n)
        titles = sorted(t["title"] for t in data)
        self.assertEqual(titles, sorted("conc-%d" % i for i in range(n)))

    def test_web_ui(self):
        status, headers, _, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"TaskForge", raw)
        self.assertIn(b"/tasks", raw)

    def test_persistence_across_restart(self):
        self.post("persist-me")
        # Start a second server attached to the same SQLite file.
        second = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        second.db_path = self.db_path
        port2 = second.server_address[1]
        t = threading.Thread(target=second.serve_forever, daemon=True)
        t.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port2, timeout=10)
            c.request("GET", "/tasks")
            resp = c.getresponse()
            data = json.loads(resp.read())
            c.close()
        finally:
            second.shutdown()
            second.server_close()
        self.assertTrue(any(task["title"] == "persist-me" for task in data),
                        data)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="taskforge-cli-")
        self.db_path = os.path.join(self.tmpdir, "cli.db")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "taskforge", *args],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)),
        )

    def test_add_and_list(self):
        res = self._cli("add", "--db", self.db_path, "--title", "cli task")
        self.assertEqual(res.returncode, 0, res.stderr)
        task_id = res.stdout.strip()
        self.assertTrue(task_id.isdigit(), task_id)
        res = self._cli("list", "--db", self.db_path)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("cli task", res.stdout.splitlines())

    def test_list_empty(self):
        res = self._cli("list", "--db", self.db_path)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")


class ReadmeTests(unittest.TestCase):
    def test_readme_exists_and_documents_contract(self):
        root = os.path.dirname(os.path.abspath(__file__))
        readme = os.path.join(root, "README.md")
        self.assertTrue(os.path.exists(readme))
        with open(readme, encoding="utf-8") as f:
            content = f.read()
        for token in ["python -m taskforge serve", "--db", "--port",
                      "Idempotency-Key", "400", "404", "python -m unittest discover"]:
            self.assertIn(token, content)


class DueArchivedTests(BaseHttpTest):
    def test_create_with_due_normalized_to_utc_z(self):
        _, _, data, _ = self.post("due task", due="2026-12-31T23:59:59+05:30")
        # 23:59:59 +05:30 -> 18:29:59Z same day
        self.assertEqual(data["due"], "2026-12-31T18:29:59Z")

    def test_create_with_due_already_z_passthrough(self):
        _, _, data, _ = self.post("due task", due="2026-12-31T23:59:59Z")
        self.assertEqual(data["due"], "2026-12-31T23:59:59Z")

    def test_create_with_due_null(self):
        _, _, data, _ = self.post("no due", due=None)
        self.assertIsNone(data["due"])

    def test_create_with_due_omitted(self):
        _, _, data, _ = self.post("no due key")
        self.assertIsNone(data["due"])

    def test_create_due_naive_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks",
                     body=json.dumps({"title": "naive", "due": "2026-12-31T23:59:59"}).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        self.assertEqual(resp.status, 400)

    def test_create_due_invalid_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks",
                     body=json.dumps({"title": "bad", "due": "not-a-timestamp"}).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        self.assertEqual(resp.status, 400)

    def test_create_due_nonstring_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/tasks",
                     body=json.dumps({"title": "bad", "due": 12345}).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse(); resp.read(); conn.close()
        self.assertEqual(resp.status, 400)

    def test_due_before_strict_excludes_equal(self):
        _, _, early, _ = self.post("early", due="2026-01-01T00:00:00Z")
        _, _, late, _ = self.post("late", due="2026-06-01T00:00:00Z")
        _, _, nodue, _ = self.post("no due")
        status, _, data, _ = self.request("GET", "/tasks?due_before=2026-06-01T00:00:00Z")
        self.assertEqual(status, 200)
        ids = {t["id"] for t in data}
        self.assertIn(early["id"], ids)
        self.assertNotIn(late["id"], ids)
        self.assertNotIn(nodue["id"], ids)

    def test_due_before_invalid_400(self):
        status, _, _, _ = self.request("GET", "/tasks?due_before=not-a-date")
        self.assertEqual(status, 400)

    def test_due_before_naive_400(self):
        status, _, _, _ = self.request("GET", "/tasks?due_before=2026-06-01T00:00:00")
        self.assertEqual(status, 400)

    def test_archive_default_false_and_filter(self):
        _, _, t1, _ = self.post("active")
        _, _, t2, _ = self.post("to archive")
        self.request("PATCH", "/tasks/%d" % t2["id"], {"archived": True})
        status, _, data, _ = self.request("GET", "/tasks")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data], [t1["id"]])
        status, _, data, _ = self.request("GET", "/tasks?archived=true")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data], [t2["id"]])
        status, _, data, _ = self.request("GET", "/tasks?archived=false")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data], [t1["id"]])

    def test_archive_invalid_query_400(self):
        status, _, _, _ = self.request("GET", "/tasks?archived=yes")
        self.assertEqual(status, 400)

    def test_patch_archive_invalid_400(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        for body in [{"archived": "yes"}, {"archived": 1}, {"archived": None}]:
            status, _, _, _ = self.request("PATCH", "/tasks/%d" % tid, body)
            self.assertEqual(status, 400, body)

    def test_patch_archive_unarchive(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        status, _, data, _ = self.request("PATCH", "/tasks/%d" % tid, {"archived": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["archived"], True)
        status, _, data, _ = self.request("PATCH", "/tasks/%d" % tid, {"archived": False})
        self.assertEqual(status, 200)
        self.assertEqual(data["archived"], False)

    def test_patch_due_and_done_together(self):
        _, _, created, _ = self.post("todo")
        tid = created["id"]
        status, _, data, _ = self.request("PATCH", "/tasks/%d" % tid,
                                          {"done": True, "due": "2026-12-31T00:00:00Z"})
        self.assertEqual(status, 200)
        self.assertEqual(data["done"], True)
        self.assertEqual(data["due"], "2026-12-31T00:00:00Z")

    def test_filters_combine_done_and_archived_and_due_before(self):
        _, _, a, _ = self.post("active due", due="2026-01-01T00:00:00Z")
        _, _, b, _ = self.post("done due", due="2026-01-02T00:00:00Z")
        self.request("PATCH", "/tasks/%d" % b["id"], {"done": True})
        _, _, c, _ = self.post("archived", due="2026-01-03T00:00:00Z")
        self.request("PATCH", "/tasks/%d" % c["id"], {"archived": True})
        status, _, data, _ = self.request("GET",
                                          "/tasks?done=false&archived=false&due_before=2026-01-03T00:00:00Z")
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in data], [a["id"]])

    def test_legacy_table_migration_preserves_data(self):
        import sqlite3
        tmpdir = tempfile.mkdtemp(prefix="taskforge-migrate-")
        try:
            legacy_db = os.path.join(tmpdir, "legacy.db")
            conn = sqlite3.connect(legacy_db)
            conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO tasks (title, done) VALUES ('old task', 0)")
            conn.commit()
            conn.close()
            conn = db.connect(legacy_db)
            db.init_db(conn)
            row = conn.execute("SELECT title, done, due, archived FROM tasks WHERE id=1").fetchone()
            self.assertEqual(row["title"], "old task")
            self.assertEqual(row["done"], 0)
            self.assertIsNone(row["due"])
            self.assertEqual(row["archived"], 0)
            conn.close()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class SchemaTests(unittest.TestCase):
    def test_schema_matches_v1(self):
        tmpdir = tempfile.mkdtemp(prefix="taskforge-schema-")
        try:
            db_path = os.path.join(tmpdir, "s.db")
            conn = db.connect(db_path)
            db.init_db(conn)
            cur = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
            )
            row = cur.fetchone()
            conn.close()
            self.assertIsNotNone(row)
            sql = row["sql"]
            self.assertIn("id INTEGER PRIMARY KEY AUTOINCREMENT", sql)
            self.assertIn("title TEXT NOT NULL", sql)
            self.assertIn("done INTEGER NOT NULL DEFAULT 0", sql)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
