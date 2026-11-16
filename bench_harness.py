"""Harness overhead benchmark: durable tasks, checkpoints, events, compaction, file tools.

Every task here is driven by a scripted provider, so the numbers describe the
harness (task_harness.TaskManager and agent_loop.Workspace), not a model. That is
the point: model latency hides harness regressions, SQLite write amplification and
compaction cost. Run it on any checkout:

    python bench_harness.py

It prints a table and exits non-zero when a measured value crosses its ceiling, so
CI can gate on harness regressions instead of someone eyeballing numbers. Ceilings
are deliberately loose (they catch an order-of-magnitude regression on a slow CI
runner, not a 20% wobble); compare runs, do not read them as absolute truth.
"""
import json
import pathlib
import platform
import sys
import tempfile
import time

from agent_loop import Workspace
from task_harness import TaskManager

TASKS_SERIAL = 5
TASKS_PARALLEL = 4
SAVES = 300
BIG_FILE = 1024 * 1024
LIST_FILES = 200
COMPACTION_ROUNDS = 20

RESULTS = []


def action(kind, **kw):
    return json.dumps({'action': kind, **kw})


def scripted(replies):
    """Provider with a fixed script; anything past the script becomes done."""
    queue = list(replies)

    def provider(messages, limit=None):
        return queue.pop(0) if queue else action('done', text='bench done')
    return provider


def write_read_done(path, content='bench'):
    return scripted([action('write_file', path=path, content=content),
                     action('read_file', path=path),
                     action('done', text='wrote and read ' + path)])


def wait(m, tid, owner, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = m.get(tid, owner)
        if row and row['status'] not in ('queued', 'running'):
            return row
        time.sleep(.005)
    raise RuntimeError('task did not settle within %ds' % timeout)


def wait_gone(m, tid, timeout=30):
    """The worker pops itself from m.threads after the status is committed."""
    end = time.monotonic() + timeout
    while tid in m.threads and time.monotonic() < end:
        time.sleep(.005)


def record(name, value, unit, ceiling, lower_is_better, note=''):
    ok = None
    if value is not None and ceiling is not None:
        ok = value <= ceiling if lower_is_better else value >= ceiling
    RESULTS.append((name, value, unit, ceiling, lower_is_better, ok, note))
    if value is None:
        shown = 'n/a'
    elif value >= 1000:
        shown = '%.0f' % value
    elif value >= 1:
        shown = '%.2f' % value
    else:
        shown = '%.4f' % value
    limit = '' if ceiling is None else ('%s %s' % ('<=' if lower_is_better else '>=', _short(ceiling)))
    verdict = '' if ok is None else ('ok' if ok else 'FAIL')
    print('%-40s %10s %-16s %-12s %-5s %s' % (name, shown, unit, limit, verdict, note))


def _short(value):
    return ('%.0f' % value) if value >= 1000 else ('%.3f' % value).rstrip('0').rstrip('.')


def measure(name, unit, ceiling, lower_is_better, fn, note=''):
    started = time.monotonic()
    try:
        value = fn()
        note = note or ('measured in %.2fs' % (time.monotonic() - started))
    except Exception as exc:  # noqa: BLE001 - a benchmark reports, it never aborts the run
        value = None
        note = type(exc).__name__ + ': ' + str(exc)[:100]
    record(name, value, unit, ceiling, lower_is_better, note)
    return value


def open_manager(root, tag):
    m = TaskManager(root / (tag + '.db'), root / (tag + '-ws'))
    return m


def close_quietly(m):
    try:
        m.close()
    except Exception:  # noqa: BLE001 - teardown problems are reported by the numbers above
        pass


def bench_serial(root):
    """One task at a time: submit -> write -> read -> done, including SQLite checkpoints."""
    m = open_manager(root, 'serial')
    times = []
    try:
        for i in range(TASKS_SERIAL):
            owner = 'bench%d' % i
            goal = 'Write bench to out%d.txt' % i
            started = time.monotonic()
            tid = m.submit(owner, goal, [], write_read_done('out%d.txt' % i), checks=[
                {'type': 'file_text', 'path': 'out%d.txt' % i, 'text': 'bench'}])
            row = wait(m, tid, owner)
            if row['status'] != 'completed':
                raise RuntimeError('task %d ended %s' % (i, row['status']))
            times.append(time.monotonic() - started)
            wait_gone(m, tid)
    finally:
        close_quietly(m)
    times.sort()
    return times[len(times) // 2]


def bench_parallel(root):
    """Four distinct sessions at once: the documented global worker cap."""
    m = open_manager(root, 'parallel')
    try:
        started = time.monotonic()
        tasks = [m.submit('par%d' % i, 'Write bench to par%d.txt' % i, [],
                          write_read_done('par%d.txt' % i), checks=[
                              {'type': 'file_text', 'path': 'par%d.txt' % i, 'text': 'bench'}])
                 for i in range(TASKS_PARALLEL)]
        for i, tid in enumerate(tasks):
            row = wait(m, tid, 'par%d' % i)
            if row['status'] != 'completed':
                raise RuntimeError('parallel task %d ended %s' % (i, row['status']))
        return time.monotonic() - started
    finally:
        close_quietly(m)


def bench_checkpoint(root):
    """Cost of one checkpoint: a state UPDATE and an event INSERT in one transaction."""
    m = open_manager(root, 'checkpoint')
    try:
        tid = m.submit('cp', 'Write bench to cp.txt', [], write_read_done('cp.txt'))
        row = wait(m, tid, 'cp')
        wait_gone(m, tid)
        state = row['state']
        started = time.monotonic()
        for i in range(SAVES):
            m.save(tid, 'running', state, {'type': 'bench', 'text': 'checkpoint %d' % i})
        return (time.monotonic() - started) / SAVES * 1e6
    finally:
        close_quietly(m)


def bench_events(root):
    """Event replay through the documented ?after= cursor, 200 events per page."""
    m = open_manager(root, 'events')
    try:
        tid = m.submit('ev', 'Write bench to ev.txt', [], write_read_done('ev.txt'))
        row = wait(m, tid, 'ev')
        wait_gone(m, tid)
        state = row['state']
        for i in range(SAVES):
            m.save(tid, 'running', state, {'type': 'bench', 'text': 'event %d' % i})
        started = time.monotonic()
        seen, after = 0, 0
        while True:
            batch = m.events(tid, 'ev', after)
            seen += len(batch)
            if len(batch) < 200:
                break
            after = batch[-1]['seq']
        elapsed = time.monotonic() - started
        return seen / elapsed if elapsed > 0 else None
    finally:
        close_quietly(m)


def bench_compaction(root):
    """One bounded provider context view built from a trajectory over the 24 000-char budget."""
    m = open_manager(root, 'compaction')
    try:
        tid = m.submit('cx', 'Write bench to cx.txt', [], write_read_done('cx.txt'))
        wait(m, tid, 'cx')
        wait_gone(m, tid)
        state = {'steps': 40, 'attempts': 40, 'requirements': 'bench', 'clarification': '',
                 'messages': [{'role': 'system', 'content': 'Task rules. ' * 600}],
                 'written': [], 'checked': [], 'pending': None, 'elapsed': 0, 'output': 0,
                 'failure_signature': None, 'failure_count': 0, 'result_schema': None,
                 'budgets': {'context': 24000}}
        state['messages'] += [{'role': 'user', 'content': 'step %d ' % i + 'x' * 1200}
                              for i in range(40)]
        started = time.monotonic()
        for _ in range(COMPACTION_ROUNDS):
            view = m._context(tid, state, 'bench goal', [])
            if view is None:
                raise RuntimeError('rules do not fit the context budget')
        return (time.monotonic() - started) / COMPACTION_ROUNDS * 1000
    finally:
        close_quietly(m)


def bench_file_tools(root):
    """Workspace write + read of 1 MB: the throughput of artifact-heavy turns."""
    ws = Workspace(root / 'files-ws')
    payload = 'x' * BIG_FILE
    started = time.monotonic()
    try:
        ws.execute({'action': 'write_file', 'path': 'big.txt', 'content': payload})
        got = ws.execute({'action': 'read_file', 'path': 'big.txt'})
    finally:
        ws.close()
    if len(got) != BIG_FILE:
        raise RuntimeError('read back %d bytes' % len(got))
    return (2 * BIG_FILE / (1024 * 1024)) / (time.monotonic() - started)


def bench_list_files(root):
    """list_files over a workspace at the documented keep-it-bounded size."""
    ws = Workspace(root / 'list-ws')
    try:
        for i in range(LIST_FILES):
            (ws.root / ('f%03d.txt' % i)).write_text('x', encoding='utf-8')
        started = time.monotonic()
        listing = ws.execute({'action': 'list_files'})
    finally:
        ws.close()
    if len(json.loads(listing)) != LIST_FILES:
        raise RuntimeError('listing returned %d files' % len(json.loads(listing)))
    return time.monotonic() - started


def main():
    print('harness overhead benchmark (%s, Python %s, %s)'
          % (time.strftime('%Y-%m-%d %H:%M:%S'), platform.python_version(), platform.system()))
    print('scripted provider: no model, no network; numbers are harness overhead only\n')
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        measure('durable task, median wall', 'ms', 2000, True,
                lambda: bench_serial(root) * 1000, 'write+read+done, 3 steps')
        measure('durable task, steps per second', 'steps/s', 1.5, False,
                lambda: 3 / bench_serial(root), 'sequential sessions')
        measure('%d concurrent tasks, wall' % TASKS_PARALLEL, 'ms', 3000, True,
                lambda: bench_parallel(root) * 1000, 'global worker cap')
        measure('checkpoint commit', 'us', 10000, True, lambda: bench_checkpoint(root),
                'one UPDATE + one event INSERT')
        measure('event replay', 'events/s', 500, False, lambda: bench_events(root),
                '?after= cursor, 200 per page')
        measure('context compaction view', 'ms', 50, True, lambda: bench_compaction(root),
                'over the 24 000-char budget')
        measure('workspace file throughput', 'MB/s', 2, False, lambda: bench_file_tools(root),
                '1 MB write + read')
        measure('list_files, %d files' % LIST_FILES, 'ms', 1000, True,
                lambda: bench_list_files(root) * 1000, 'sorted recursive listing')

    failed = [(name, note) for name, _, _, _, _, ok, note in RESULTS if ok is False]
    missing = [name for name, value, _, _, _, _, _ in RESULTS if value is None]
    print('\n%d measured, %d over ceiling, %d not measurable'
          % (len(RESULTS) - len(missing), len(failed), len(missing)))
    for name, note in failed:
        print('  over ceiling: %s (%s)' % (name, note))
    return 1 if failed or missing else 0


if __name__ == '__main__':
    sys.exit(main())
