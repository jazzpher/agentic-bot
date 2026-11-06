"""TaskForge HTTP server (stdlib only)."""

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db

UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<meta charset="utf-8">
<title>TaskForge</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;line-height:1.5}
h1{font-size:1.5rem;margin-bottom:1rem}
#add{display:flex;gap:8px;margin-bottom:24px}
#add input[name="title"]{flex:1;padding:8px;font-size:1rem;border:1px solid #ccc;border-radius:6px}
#add button{padding:8px 16px;font-size:1rem;cursor:pointer;border:1px solid #333;border-radius:6px;background:#f7f7f7}
#add button:hover{background:#eee}
#error{color:#b00;display:none;margin-bottom:12px}
ul#list{list-style:none;padding:0}
li{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid #ddd}
li.done .title{text-decoration:line-through;color:#999}
label{cursor:pointer;display:flex;align-items:center;gap:8px;flex:1}
</style>
<div id="error"></div>
<h1>TaskForge</h1>
<form id="add"><input name="title" type="text" placeholder="New task" required><button type="submit">Add</button></form>
<ul id="list"></ul>
<script>
var listEl = document.getElementById("list");
var errorEl = document.getElementById("error");
var formEl = document.getElementById("add");
function escape(s){return String(s).replace(/[&<>"']/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];});}
function load(){fetch("/tasks").then(function(r){return r.json();}).then(function(tasks){render(tasks);}).catch(function(){showError("Could not reach the API."});});
function showError(m){errorEl.textContent = m; errorEl.style.display = "block";}
function render(tasks){
  listEl.innerHTML = "";
  tasks.forEach(function(t){
    var li = document.createElement("li");
    if (t.done) li.classList.add("done");
    var cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = Boolean(t.done);
    var span = document.createElement("span"); span.className = "title"; span.textContent = escape(t.title);
    cb.onchange = function(){ patchTask(t.id, cb.checked); };
    var label = document.createElement("label"); label.appendChild(cb); label.appendChild(span);
    li.appendChild(label);
    listEl.appendChild(li);
  });
}
function patchTask(id, done){
  fetch("/tasks/" + id, {method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({done: done})})
    .then(function(r){ if (!r.ok) showError("Failed to update task"); else load(); });
}
formEl.onsubmit = function(e){
  e.preventDefault();
  var title = formEl.elements.title.value;
  fetch("/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({title: title})})
    .then(function(r){ if (!r.ok) showError("Failed to add task"); else { formEl.elements.title.value = ""; load(); } });
};
load();
setInterval(load, 3000);
</script>
"""

TASK_RE = re.compile(r"^/tasks/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "TaskForge/1.0"
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # Silence per-request log output.
        return

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        return self.rfile.read(length)

    def _parse_json(self, raw):
        if not raw:
            return None, None
        try:
            return json.loads(raw), None
        except (ValueError, json.JSONDecodeError):
            return None, "invalid json"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_ui()
        elif parsed.path == "/tasks":
            self._list_tasks(parsed.query)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/tasks":
            self._send_json(404, {"error": "not found"})
            return
        raw = self._read_body()
        data, err = self._parse_json(raw)
        if err or not isinstance(data, dict):
            self._send_json(400, {"error": "invalid json"})
            return
        title = data.get("title")
        if not isinstance(title, str) or title.strip() == "":
            self._send_json(400, {"error": "title must be a non-blank string"})
            return
        due = data.get("due", None)
        key = self.headers.get("Idempotency-Key")
        conn = db.connect(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                task, _created = db.find_or_create_task(conn, title, key, due=due)
            except ValueError:
                conn.execute("ROLLBACK")
                conn.close()
                self._send_json(400, {"error": "invalid due timestamp"})
                return
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            conn.close()
            self._send_json(500, {"error": "server error"})
            return
        conn.close()
        self._send_json(201, task)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        m = TASK_RE.match(parsed.path)
        if not m:
            self._send_json(404, {"error": "not found"})
            return
        task_id = int(m.group(1))
        raw = self._read_body()
        data, err = self._parse_json(raw)
        if err or not isinstance(data, dict) or not data:
            self._send_json(400, {"error": "request body must be a JSON object"})
            return
        done = data.get("done", None)
        archived = data.get("archived", None)
        due = data.get("due", None)
        if "done" in data and not isinstance(done, bool):
            self._send_json(400, {"error": "done must be a boolean"})
            return
        if "archived" in data and not isinstance(archived, bool):
            self._send_json(400, {"error": "archived must be a boolean"})
            return
        if "due" in data:
            if due is None or not isinstance(due, str):
                self._send_json(400, {"error": "invalid due timestamp"})
                return
        conn = db.connect(self.server.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = None
            if done is not None:
                task = db.set_done(conn, task_id, done)
                if task is None:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._send_json(404, {"error": "task not found"})
                    return
            if archived is not None:
                task = db.set_archived(conn, task_id, archived)
                if task is None:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._send_json(404, {"error": "task not found"})
                    return
            if due is not None:
                try:
                    task = db.set_due(conn, task_id, due)
                except ValueError:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._send_json(400, {"error": "invalid due timestamp"})
                    return
                if task is None:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._send_json(404, {"error": "task not found"})
                    return
            if task is None:
                # No fields to update: fetch the current task.
                task = db.get_task(conn, task_id)
                if task is None:
                    conn.execute("ROLLBACK")
                    conn.close()
                    self._send_json(404, {"error": "task not found"})
                    return
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            conn.close()
            self._send_json(500, {"error": "server error"})
            return
        conn.close()
        self._send_json(200, task)

    def _list_tasks(self, query):
        qs = parse_qs(query, keep_blank_values=True)
        done = None
        if "done" in qs:
            values = qs["done"]
            value = values[0] if isinstance(values, list) else values
            if value == "true":
                done = True
            elif value == "false":
                done = False
            else:
                self._send_json(400, {"error": "done must be 'true' or 'false'"})
                return
        due_before = None
        if "due_before" in qs:
            values = qs["due_before"]
            value = values[0] if isinstance(values, list) else values
            try:
                due_before = db.parse_due(value)
            except ValueError:
                self._send_json(400, {"error": "due_before must be a valid RFC3339 timestamp"})
                return
        archived = None
        if "archived" in qs:
            values = qs["archived"]
            value = values[0] if isinstance(values, list) else values
            if value == "true":
                archived = True
            elif value == "false":
                archived = False
            else:
                self._send_json(400, {"error": "archived must be 'true' or 'false'"})
                return
        conn = db.connect(self.server.db_path)
        try:
            tasks = db.list_tasks(conn, done=done, due_before=due_before, archived=archived)
        finally:
            conn.close()
        self._send_json(200, tasks)

    def _serve_ui(self):
        body = UI_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(db_path, port):
    bootstrap = db.connect(db_path)
    try:
        db.init_db(bootstrap)
    finally:
        bootstrap.close()
    # Raise the listen backlog so bursts of concurrent connections are
    # queued instead of reset under load.
    ThreadingHTTPServer.request_queue_size = 128
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.db_path = db_path
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
