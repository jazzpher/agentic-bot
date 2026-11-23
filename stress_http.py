#!/usr/bin/env python3
"""Offline HTTP stress: concurrent turns, approvals, Stop, durable tasks, owner isolation.

Everything here runs against the real Flask app on a real threaded server with a
scripted provider, so the numbers describe the request path (locks, the approval
broker, the stores, worker threads) rather than a model. No provider quota is spent
and nothing leaves the machine.

    python stress_http.py                     # six owners, two rounds
    python stress_http.py --owners 12 --rounds 5

It prints a table and exits non-zero when a check fails or a ceiling is crossed, the
same shape as bench_harness.py. `test_stress_http.py` runs a reduced version inside
the normal suite, so this harness cannot rot between manual runs.
"""
import argparse
import contextlib
import gc
import json
import os
import pathlib
import tempfile
import threading
import time
import types
from unittest import mock

import requests
from werkzeug.serving import make_server

import webui
from agenticbot import runlog
from agenticbot.approvals import ApprovalBroker, PolicyStore
from agenticbot.conversations import ConversationStore
from agenticbot.memory import MemoryStore
from task_harness import TaskManager

# Ceilings. Deliberately loose: the point is to catch a lock-up, a leak or an
# order-of-magnitude regression on a slow runner, not a 20% wobble.
P95_CEILING = 15.0        # seconds for a scripted three-call turn
STOP_CEILING = 10.0       # seconds from POST /stop to the turn's stream closing
EXTRA_THREADS = 4         # tolerated threads above the post-startup baseline
EXTRA_FDS = 32            # tolerated open fds above the baseline (sqlite connections, sockets)
WRITE_POLICY = {'mode': 'workspace-write', 'approval': 'never', 'network': False, 'allow': []}


# --- scripted providers -------------------------------------------------------------------
def _tool_events(calls):
    yield {'type': 'tool_calls', 'calls': calls}
    yield {'type': 'usage', 'prompt_tokens': 12, 'completion_tokens': 3}
    yield {'type': 'finish', 'reason': 'tool_calls'}


def _text_events(text):
    yield {'type': 'text', 'delta': text}
    yield {'type': 'usage', 'prompt_tokens': 20, 'completion_tokens': 6}
    yield {'type': 'finish', 'reason': 'stop'}


def _calls_this_turn(messages):
    """Tool results produced since the latest user message: a reused conversation starts fresh."""
    last_user = max((i for i, m in enumerate(messages) if m.get('role') == 'user'), default=-1)
    return sum(1 for m in messages[last_user + 1:] if m.get('role') == 'tool')


def write_then_done(path, content='ok', text='Written and read back.'):
    """A three-call turn: write the file, read it back, say so.

    The step comes from this turn's own tool results, not a counter shared across
    turns, so a second turn in the same conversation runs the same script.
    """
    def stream_fn(cfg, messages, tools=None, stop=None, **_):
        seen = _calls_this_turn(messages)
        if seen == 0:
            yield from _tool_events([{'id': 'w%d' % len(messages), 'name': 'write_file',
                                      'arguments': json.dumps({'path': path, 'content': content})}])
        elif seen == 1:
            yield from _tool_events([{'id': 'r%d' % len(messages), 'name': 'read_file',
                                      'arguments': json.dumps({'path': path})}])
        else:
            yield from _text_events(text)
    return stream_fn


def slow_stream(cfg, messages, tools=None, stop=None, **_):
    """Hold the stream open until Stop, so a stopped turn is observably in flight."""
    yield {'type': 'text', 'delta': 'working...'}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            break
        time.sleep(0.02)
    yield {'type': 'finish', 'reason': 'stop'}


def task_provider(*_args, **_kwargs):
    """A durable /task: write, read back, then claim done.

    The owner's `file_text` check finishes the task, not the model's claim, which is
    why each submission passes one.
    """
    state = {'n': 0}

    def call(_messages, max_output_bytes=4000):
        state['n'] += 1
        if state['n'] == 1:
            return json.dumps({'action': 'write_file', 'path': 'task.txt', 'content': 'ok'})
        if state['n'] == 2:
            return json.dumps({'action': 'read_file', 'path': 'task.txt'})
        return json.dumps({'action': 'done', 'text': 'Wrote and read task.txt.'})
    return call


# --- helpers ------------------------------------------------------------------------------
def done_status(events):
    for event in reversed(events or []):
        if event.get('type') == 'done':
            return event.get('status')
    return None


def _ids(payload):
    if isinstance(payload, list):
        return {row.get('id') for row in payload if isinstance(row, dict)}
    return set()


def _fds():
    try:
        return len(os.listdir('/proc/self/fd'))
    except OSError:
        return None


class Stress:
    def __init__(self, owners=6, rounds=2):
        self.owners = owners
        self.rounds = rounds
        self.results = []
        self.http_5xx = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.stack = contextlib.ExitStack()
        self.server = None
        self.thread = None
        self.sessions = []
        self.url = ''
        self.baseline_threads = 0
        self.baseline_fds = None
        self.previous_runlog = str(webui.WORKSPACES / '.runlog.jsonl')

    # --- lifecycle ------------------------------------------------------------------------
    def setup(self):
        workspaces = self.root / 'workspaces'
        workspaces.mkdir(parents=True, exist_ok=True)
        self.tasks = TaskManager(self.root / 'tasks.db', workspaces)
        self.conversations = ConversationStore(self.root / 'conversations.sqlite3')
        self.memory = MemoryStore(self.root / 'memory.sqlite3')
        self.policies = PolicyStore(self.root / 'policies')
        self.approvals = ApprovalBroker(self.policies)

        def patch(target, name, value, create=False):
            self.stack.enter_context(mock.patch.object(target, name, value, create=create))

        patch(webui, 'WORKSPACES', workspaces)
        patch(webui, 'PASSWORD', '')
        patch(webui, 'DEPLOYMENT_MODE', 'local')
        patch(webui, 'TASKS', self.tasks, create=True)
        patch(webui, 'CONVERSATIONS', self.conversations, create=True)
        patch(webui, 'MEMORY', self.memory, create=True)
        patch(webui, 'POLICIES', self.policies, create=True)
        patch(webui, 'APPROVALS', self.approvals, create=True)
        patch(webui, 'agent_stream_fn', write_then_done('note.txt'))
        patch(webui, 'make_summarizer', lambda cfg: None)
        # The independent grader is a model call per check; nothing here may spend quota on it.
        patch(webui, 'agent_grader', types.SimpleNamespace(model_grader=lambda cfg: None))
        patch(webui, 'task_provider', task_provider)
        self.stack.enter_context(mock.patch.dict(webui.app.config, {'MOCK': False, 'TESTING': False}))
        self.stack.enter_context(mock.patch.dict(webui.clients, {}, clear=True))
        runlog.configure(str(self.root / 'runs.jsonl'))
        self.stack.callback(runlog.configure, self.previous_runlog)

        self.server = make_server('127.0.0.1', 0, webui.app, threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_port
        self.baseline_threads = len(threading.enumerate())
        self.baseline_fds = _fds()

    def teardown(self):
        with webui.clients_lock:
            for state in list(webui.clients.values()):
                state['stop'].set()
        if self.server is not None:
            self.server.shutdown()
            if self.thread is not None:
                self.thread.join(5)
            self.server.server_close()
        self._close_sessions()
        self.stack.close()
        for store in (self.tasks, self.conversations, self.memory):
            try:
                store.close()
            except Exception:  # noqa: BLE001 - teardown must not hide the result
                pass
        self.tmp.cleanup()

    def _close_sessions(self):
        for session in self.sessions:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
        self.sessions = []

    # --- HTTP helpers ---------------------------------------------------------------------
    def _count(self, response):
        if response.status_code >= 500:
            self.http_5xx += 1
        return response

    def get(self, session, path, timeout=30):
        return self._count(session.get(self.url + path, timeout=timeout))

    def post(self, session, path, payload=None, timeout=30):
        return self._count(session.post(self.url + path, json=payload, timeout=timeout))

    def new_session(self, policy=WRITE_POLICY):
        session = requests.Session()
        self.sessions.append(session)
        first = self.get(session, '/')
        if first.status_code != 200:
            raise RuntimeError('GET / returned %s' % first.status_code)
        if policy is not None:
            saved = self.post(session, '/policy', policy)
            if saved.status_code != 200:
                raise RuntimeError('POST /policy returned %s' % saved.status_code)
        return session

    def owner(self, session):
        with webui.app.test_request_context(headers={'Cookie': 'session=' + session.cookies.get('session')}):
            return webui.session['sid']

    def drain(self, session, payload, approve=None, timeout=120):
        """POST /agent and read the NDJSON stream to its end. Returns (events, code, seconds)."""
        started = time.monotonic()
        events = []
        answered = False
        response = self._count(session.post(self.url + '/agent', json=payload, stream=True,
                                            timeout=(10, timeout)))
        try:
            if response.status_code != 200:
                return events, response.status_code, time.monotonic() - started
            for line in response.iter_lines(decode_unicode=True):
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                events.append(event)
                if approve is not None and not answered and event.get('type') == 'approval_required':
                    answered = True
                    approve(event)
        finally:
            response.close()
        return events, 200, time.monotonic() - started

    def wait_running(self, session, seconds=20):
        """Poll /agent/current until the session reports a running turn."""
        deadline = time.monotonic() + seconds
        last = {}
        while time.monotonic() < deadline:
            last = self.get(session, '/agent/current').json()
            if last.get('running'):
                return last
            time.sleep(0.05)
        return last

    # --- checks ---------------------------------------------------------------------------
    def concurrent_turns(self):
        sessions = [self.new_session() for _ in range(self.owners)]
        outcomes = []
        lock = threading.Lock()

        def one(session):
            try:
                events, code, seconds = self.drain(session, {'message': 'write and read note.txt'})
                # The interactive agent reports a file-changing turn without an explicit
                # verifier as `unverified` on purpose (completion.automatic_check): the
                # tool ran, nobody certified the result. What this check owns is that the
                # turn ran, finished, and really wrote the file.
                written = webui.owner_workspace(self.owner(session)).path('note.txt').is_file()
                outcome = (code, done_status(events), written, seconds)
            except Exception as exc:  # noqa: BLE001 - the check reports, it never crashes
                outcome = (0, type(exc).__name__, False, 0.0)
            with lock:
                outcomes.append(outcome)

        for _ in range(self.rounds):
            workers = [threading.Thread(target=one, args=(session,)) for session in sessions]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=180)

        bad = [o for o in outcomes
               if o[0] != 200 or o[1] not in ('completed', 'unverified') or not o[2]]
        latencies = sorted(o[3] for o in outcomes)
        p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))] if latencies else 0.0
        ok = bool(latencies) and not bad and p95 <= P95_CEILING
        return ok, '%d turn(s), p95 %.2fs, %d not finished%s' % (
            len(outcomes), p95, len(bad), (' -> ' + repr(bad[:3])) if bad else '')

    def serialization(self):
        """One turn per session is the design: a second POST must be refused, not queued."""
        session = self.new_session()
        holder = {}

        def run():
            holder['events'], holder['code'], _ = self.drain(session, {'message': 'hold the session'})

        with mock.patch.object(webui, 'agent_stream_fn', slow_stream):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.wait_running(session)
                second = self.post(session, '/agent', {'message': 'again'})
            finally:
                self.post(session, '/stop')
                worker.join(timeout=STOP_CEILING + 15)
        return second.status_code == 409, 'second /agent while busy -> %s' % second.status_code

    def approval(self, decision, path):
        """A write under read-only + on-request waits for the answer it is given."""
        session = self.new_session(policy={'mode': 'read-only', 'approval': 'on-request',
                                           'network': False, 'allow': []})
        asked = []

        def answer(event):
            asked.append(event)
            self.post(session, '/agent/approve', {'id': event['id'], 'decision': decision})

        with mock.patch.object(webui, 'agent_stream_fn', write_then_done(path)):
            events, code, seconds = self.drain(session, {'message': 'write ' + path}, approve=answer)
        exists = webui.owner_workspace(self.owner(session)).path(path).exists()
        ok = code == 200 and len(asked) == 1 and exists is (decision == 'allow')
        return bool(ok), '%s: asked %d time(s), file exists=%s, turn %.1fs' % (
            decision, len(asked), exists, seconds)

    def stop_mid_turn(self):
        session = self.new_session()
        holder = {}

        def run():
            holder['events'], holder['code'], _ = self.drain(session, {'message': 'hold the session'}, timeout=180)
            holder['closed'] = time.monotonic()

        with mock.patch.object(webui, 'agent_stream_fn', slow_stream):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            current = self.wait_running(session)
            stop_sent = time.monotonic()
            self.post(session, '/stop')
            worker.join(timeout=STOP_CEILING + 20)
        closed_after = holder.get('closed', float('inf')) - stop_sent
        status = done_status(holder.get('events') or [])
        ok = bool(current.get('running')) and bool(current.get('conversation_id')) \
            and closed_after <= STOP_CEILING and status == 'stopped'
        return ok, 'turn seen running=%s, stream closed %.2fs after Stop, status %s' % (
            current.get('running'), closed_after, status)

    def parallel_tasks(self):
        count = min(self.owners, 4)
        sessions = [self.new_session() for _ in range(count)]
        rows = []
        lock = threading.Lock()

        def one(session):
            submitted = self.post(session, '/task', {
                'message': 'write task.txt and read it back',
                'checks': [{'type': 'file_text', 'path': 'task.txt', 'text': 'ok'}]})
            if submitted.status_code != 202:
                with lock:
                    rows.append('http %s' % submitted.status_code)
                return
            tid = str(submitted.json().get('id'))
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                status = self.get(session, '/tasks/' + tid).json().get('status')
                if status not in ('queued', 'running'):
                    with lock:
                        rows.append(status)
                    return
                time.sleep(0.05)
            with lock:
                rows.append('timeout')

        workers = [threading.Thread(target=one, args=(session,)) for session in sessions]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=180)
        ok = len(rows) == count and all(status == 'completed' for status in rows)
        return ok, '%d task(s) -> %s' % (len(rows), rows)

    def isolation(self):
        a, b = self.new_session(), self.new_session()
        with mock.patch.object(webui, 'agent_stream_fn', write_then_done('only-a.txt', 'secret')):
            events, code, _ = self.drain(a, {'message': 'write only-a.txt'})
        status = done_status(events)
        own = self.get(a, '/files/only-a.txt').status_code
        foreign = self.get(b, '/files/only-a.txt').status_code
        a_convs = _ids(self.get(a, '/conversations').json())
        b_convs = _ids(self.get(b, '/conversations').json())
        overlap = a_convs & b_convs
        submitted = self.post(a, '/task', {'message': 'plain',
                                           'checks': [{'type': 'file_text', 'path': 'x', 'text': 'x'}]})
        foreign_task = 'n/a'
        if submitted.status_code == 202:
            foreign_task = self.get(b, '/tasks/' + str(submitted.json().get('id'))).status_code
        ok = (code == 200 and status in ('completed', 'unverified') and own == 200 and foreign == 404
              and bool(a_convs) and not overlap and foreign_task in (404, 'n/a'))
        return ok, ('own=%s foreign=%s conversations a=%d b=%d overlap=%d foreign task=%s'
                    % (own, foreign, len(a_convs), len(b_convs), len(overlap), foreign_task))

    def leaks(self):
        # Close the client sockets first: an open keep-alive connection is not a leak.
        self._close_sessions()
        deadline = time.monotonic() + 10
        threads, fds, settled = None, None, False
        while True:
            gc.collect()
            threads, fds = len(threading.enumerate()), _fds()
            settled = threads <= self.baseline_threads + EXTRA_THREADS and \
                (fds is None or self.baseline_fds is None or fds <= self.baseline_fds + EXTRA_FDS)
            if settled or time.monotonic() > deadline:
                break
            time.sleep(0.25)
        return settled, 'threads %d (baseline %d), fds %s (baseline %s)' % (
            threads, self.baseline_threads, fds, self.baseline_fds)

    def no_server_errors(self):
        return self.http_5xx == 0, '%d response(s) with a 5xx status' % self.http_5xx

    # --- runner ---------------------------------------------------------------------------
    def check(self, name, fn):
        started = time.monotonic()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            ok, detail = False, type(exc).__name__ + ': ' + str(exc)[:200]
        self.results.append((name, bool(ok), time.monotonic() - started, detail))
        return ok


def run(owners=6, rounds=2):
    stress = Stress(owners, rounds)
    try:
        stress.setup()
        stress.check('concurrent /agent turns', stress.concurrent_turns)
        stress.check('one turn per session (409)', stress.serialization)
        stress.check('approval allow reaches the write', lambda: stress.approval('allow', 'allowed.txt'))
        stress.check('approval deny blocks the write', lambda: stress.approval('deny', 'denied.txt'))
        stress.check('Stop closes an in-flight turn', stress.stop_mid_turn)
        stress.check('parallel durable /task', stress.parallel_tasks)
        stress.check('owner isolation', stress.isolation)
        stress.check('no leaked threads or fds', stress.leaks)
        stress.check('no 5xx responses', stress.no_server_errors)
    finally:
        stress.teardown()
    return stress


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owners', type=int, default=6, help='concurrent sessions (default 6, minimum 2)')
    parser.add_argument('--rounds', type=int, default=2, help='turn rounds per session (default 2)')
    args = parser.parse_args()
    owners, rounds = max(2, args.owners), max(1, args.rounds)
    print('agenticbot HTTP stress (%s), %d owner(s), %d round(s)\n'
          % (time.strftime('%Y-%m-%d %H:%M:%S'), owners, rounds))
    stress = run(owners=owners, rounds=rounds)
    for name, ok, seconds, detail in stress.results:
        print('%-36s %-4s %7.2fs  %s' % (name, 'PASS' if ok else 'FAIL', seconds, detail))
    failed = sum(1 for row in stress.results if not row[1])
    print('\n%d passed, %d failed' % (len(stress.results) - failed, failed))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
