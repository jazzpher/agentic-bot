"""Progress guard: notice when the model loops without making progress.

Small local models (e.g. a 0.8B Qwen) rarely break tool-call JSON; they break on
judgement: they repeat the same call while nothing changes, or retry a call or a
path that already failed. This guard watches for exactly that, per turn:

- The same call (tool + arguments) giving the same result while the workspace is
  unchanged: the repeat gets a harness note telling the model to do something
  different, and the next identical call is blocked (AGENTIC_LOOP_GUARD_REPEATS,
  default 2: nudge on the 2nd, block the 3rd).
- An exact call that already failed, retried while nothing changed: blocked at
  once with the earlier error, since it can only fail the same way.
- A path rejected as outside the workspace: the error gets a hint (relative
  paths, the file's in-workspace name when it exists, the top-level entries) and
  that path is refused from then on without running anything.

"Nothing changed" is a fingerprint of the workspace tree (names, sizes, mtimes;
caches such as __pycache__ left out). Mutating tools are not exempt: a test run
that prints the same failure twice with no edit in between is a loop too. A call
that follows a mutating call in the same step is never judged a repeat, since
its state may have changed. AGENTIC_LOOP_GUARD=off turns the guard off.
"""
import hashlib
import json
import os

from agenticbot import config

PATH_ERROR = 'Path must stay inside this workspace'
SKIP_DIRS = {'__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'}
PATH_KEYS = ('path', 'source_path')
FINGERPRINT_LIMIT = 5000
CONTENT_FILE_LIMIT = 256 * 1024
CONTENT_TOTAL_LIMIT = 8 * 1024 * 1024


def workspace_fingerprint(root, limit=FINGERPRINT_LIMIT):
    """A hash of the tree's names, sizes and mtimes, or None when it can't tell (missing or too big)."""
    if not root or not os.path.isdir(root):
        return None
    digest = hashlib.sha1()
    count = hashed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        rel_dir = os.path.relpath(dirpath, root)
        digest.update(('D' + rel_dir + '\n').encode('utf-8', 'replace'))
        for name in sorted(filenames):
            if name.endswith(('.pyc', '.pyo')):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            digest.update(('%s/%s\0%d\0%d\n' % (rel_dir, name, st.st_size, st.st_mtime_ns)).encode('utf-8', 'replace'))
            # mtimes can tick coarsely (a same-size rewrite within one tick looks unchanged), so small files
            # are hashed by content too, within a total byte budget.
            if st.st_size <= CONTENT_FILE_LIMIT and hashed + st.st_size <= CONTENT_TOTAL_LIMIT:
                try:
                    with open(os.path.join(dirpath, name), 'rb') as fh:
                        digest.update(hashlib.sha1(fh.read()).digest())
                    hashed += st.st_size
                except OSError:
                    pass
            count += 1
            if count > limit:
                return None
    return digest.hexdigest()


def _arguments(call):
    raw = call.get('arguments', '')
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except ValueError:
        return {'_raw': str(raw)}
    return value if isinstance(value, dict) else {'_raw': str(raw)}


def _key(call):
    return call.get('name', ''), json.dumps(_arguments(call), sort_keys=True, ensure_ascii=False)


class ProgressGuard:
    def __init__(self, workspace, repeats=None):
        root = getattr(workspace, 'root', None)
        self.root = str(root) if root else None
        self.limit = max(1, int(repeats or config.LOOP_GUARD_REPEATS))
        self.seen = {}        # call key -> {'fp', 'out', 'count'}
        self.failed = {}      # call key -> {'fp', 'error'}
        self.bad_paths = {}   # path -> True (rejected as outside the workspace)
        self.nudges = self.blocks = 0
        self._start = None
        self._certain = []

    def fingerprint(self):
        return workspace_fingerprint(self.root) if self.root else None

    # Before the step's calls run --------------------------------------------------------
    def check_batch(self, calls, registry):
        """One denial reason (or None) per call, in the model's order."""
        self._start = self.fingerprint()
        self._certain = []
        denials = []
        changed = False
        for call in calls:
            certain = not changed and self._start is not None
            self._certain.append(certain)
            reason = self._check_repeat(call) if certain else None
            denials.append(reason or self._check_path(call))
            spec = registry.get(call.get('name')) if registry is not None else None
            if spec is not None and spec.mutates:
                changed = True
        self.blocks += sum(1 for d in denials if d)
        return denials

    def _check_repeat(self, call):
        key = _key(call)
        failed = self.failed.get(key)
        if failed and failed['fp'] == self._start:
            return ('Loop guard: this exact call already failed and nothing has changed since, so it would fail '
                    'the same way: "%s". Do not retry it. Read that error and fix its cause, or try a different '
                    'tool or different arguments.' % failed['error'][:240].strip())
        seen = self.seen.get(key)
        if seen and seen['fp'] == self._start and seen['count'] >= self.limit:
            return ('Loop guard: you made this exact call %d times with nothing changed in between, so the result '
                    'would be the same. Do not repeat it. Use the result you already have and take a different next '
                    'step (edit, a different command, another file), or finish with what you know.' % seen['count'])
        return None

    def _check_path(self, call):
        args = _arguments(call)
        for k in PATH_KEYS:
            path = args.get(k)
            if isinstance(path, str) and path in self.bad_paths:
                return ('Loop guard: "%s" already failed as outside the workspace; it will not work. '
                        % path) + self.path_hint(path)
        return None

    # After the step's calls ran ---------------------------------------------------------
    def observe(self, calls, results, denials=None):
        """Record what happened; a harness note (or '') per call to add to its result."""
        end = self.fingerprint()
        notes = []
        for i, (call, result) in enumerate(zip(calls, results)):
            if denials and denials[i]:
                notes.append('')
                continue
            key = _key(call)
            certain = i < len(self._certain) and self._certain[i]
            note = ''
            output = (result.output if result is not None else '') or ''
            if result is not None and not result.ok:
                self.failed[key] = {'fp': end, 'error': output.replace('Tool error: ', '', 1)}
                self.seen.pop(key, None)
                if PATH_ERROR in output:
                    args = _arguments(call)
                    path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str)), '')
                    if path:
                        self.bad_paths[path] = True
                    note = 'Loop guard: ' + self.path_hint(path)
            else:
                digest = hashlib.sha1(output.encode('utf-8', 'replace')).hexdigest()
                seen = self.seen.get(key)
                # A repeat: same call, same result, nothing changed since the last one, and this call itself
                # changed nothing either (a script that writes files is making progress).
                if certain and seen and seen['fp'] == self._start and seen['out'] == digest and end == self._start:
                    count = seen['count'] + 1
                    note = ('Loop guard: same call, same result as before, and nothing changed in between. Do not '
                            'repeat it: use this result and do something different next.')
                else:
                    count = 1
                self.seen[key] = {'fp': end, 'out': digest, 'count': count}
                self.failed.pop(key, None)
            if note:
                self.nudges += 1
            notes.append(note)
        return notes

    def top_level(self, limit=15):
        if not self.root:
            return []
        try:
            names = sorted(n for n in os.listdir(self.root) if not n.startswith('.') and n not in SKIP_DIRS)
        except OSError:
            return []
        shown = [n + ('/' if os.path.isdir(os.path.join(self.root, n)) else '') for n in names[:limit]]
        if len(names) > limit:
            shown.append('... (%d more)' % (len(names) - limit))
        return shown

    def path_hint(self, path):
        path = path if isinstance(path, str) else ''
        if path.strip() in ('', '.', './'):
            tip = ('"." is the workspace folder itself, not a file. Use list_files to see what is in it, then '
                   'read_file a file by its relative name.')
        else:
            tip = ('Paths are relative to the workspace folder (your current directory), e.g. "notes.txt" or '
                   '"src/app.py". Never use absolute paths like "/app/..." or "/workspace/...", ~ or "..".')
            base = path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]
            if self.root and base and base not in ('.', '..') and os.path.exists(os.path.join(self.root, base)):
                tip += ' "%s" is in the workspace: use "%s".' % (base, base)
        entries = self.top_level()
        if entries:
            tip += ' Top-level entries: ' + ', '.join(entries) + '.'
        return tip
