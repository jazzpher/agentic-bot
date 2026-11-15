"""Independent grader: a fresh-context check of every "done" before it is accepted.

The builder (the model doing the work) never grades itself. When a turn ends
with files changed, the host asks a separate model call that sees only the
user's request and the workspace (never the builder's conversation or claims)
for 3-5 concrete pass/fail criteria. Each criterion is bound, where possible,
to a check the host runs itself:

* sort    - CSV/XLSX column in accent-insensitive A-Z order (verify_sort's rules)
* mobile  - render the HTML at 390 px in headless Chromium and MEASURE overflow
* tests   - run a test runner (unittest/pytest/node --test/npm test) offline;
            existing test files the builder edited this turn fail the check
* file    - output file exists and is nonempty
* judge   - only when no measurement fits: a second fresh call reads the
            produced files (not the builder's words) and answers pass/fail

The criteria are fixed after the first grade, so the goalposts never move.
Only failed criteria go back to the builder, for at most AGENTIC_GRADER_ROUNDS
rounds (default 3), and every graded version is kept on disk.
A grader that cannot run (model error, no browser) fails closed: the turn is
reported "unverified", never "completed".
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import time
import zipfile

from agenticbot import config, evidence, sandbox
from agenticbot.env import env_get

SKIP_DIRS = {'.git', '.venv', 'venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache'}
MAX_SNAPSHOT_FILES = 2000
MAX_HASH_BYTES = 20 * 1024 * 1024
MAX_FILE_CHARS = 12000
MAX_TOTAL_CHARS = 40000
MAX_CRITERIA = 5
KINDS = ('sort', 'mobile', 'tests', 'file', 'judge')
_TEST_FILE = re.compile(r'(^|/)(conftest\.py|pytest\.ini|tox\.ini|test_[^/]+|[^/]+_test\.[A-Za-z0-9]+|[^/]+\.test\.[A-Za-z0-9]+|[^/]+\.spec\.[A-Za-z0-9]+)$|(^|/)tests?/')
_TEST_COMMAND = re.compile(r'^(?:(?:python3?|py)\s+-m\s+(?:unittest|pytest)|pytest|node\s+--test|npm\s+(?:run\s+)?test)(?:\s+[\w./:=-]+)*\s*$')

CRITERIA_PROMPT = '''You are an independent grader. You did NOT do this work and you have not seen the worker's messages.
Write 3 to 5 pass/fail acceptance criteria for the user's request below. Each must be concrete and checkable
("people.csv Surname column is in A-Z order", "index.html has no horizontal overflow at 390 px",
"the unit tests pass"), never a matter of taste ("looks nice", "well written").
Bind each criterion to the strongest check that fits, using only files listed below:
  {"kind": "sort", "path": "<csv/xlsx>", "column": "<header>"}
  {"kind": "mobile", "path": "<html file>"}
  {"kind": "tests", "command": "<python -m unittest ... | python -m pytest ... | node --test ... | npm test>"}
  {"kind": "file", "path": "<output file>"}
  {"kind": "judge", "paths": ["<files to read>"]}   (only when nothing above can measure it)
Reply with JSON only: {"criteria": [{"text": "...", "check": {...}}]}'''

JUDGE_PROMPT = '''You are an independent grader. You did NOT do this work and you have not seen the worker's messages or claims.
Decide each criterion strictly from the file contents below. If the files do not clearly show it, it fails.
Reply with JSON only: {"results": [{"id": <id>, "passed": true|false, "reason": "<one short sentence citing the file>"}]}'''


def enabled():
    return (env_get('AGENTIC_GRADER', '') or config.GRADER).strip().lower() != 'off'


def max_rounds():
    try:
        value = int(env_get('AGENTIC_GRADER_ROUNDS', '') or config.GRADER_ROUNDS)
    except ValueError:
        value = config.GRADER_ROUNDS
    return max(1, min(5, value))


def model_grader(cfg, max_tokens=1500):
    """The grader's own model call: a new conversation every time (fresh context)."""
    from agenticbot import providers
    return lambda messages: providers.complete_once(cfg, messages, max_tokens=max_tokens)


# ---------------------------------------------------------------- workspace state

def snapshot(root):
    root = pathlib.Path(root)
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.'))
        for name in sorted(filenames):
            if name.startswith('.') or name.endswith('.pyc'):
                continue
            path = pathlib.Path(dirpath, name)
            if path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            try:
                size = path.stat().st_size
                out[rel] = ('size:%d:%d' % (size, path.stat().st_mtime_ns) if size > MAX_HASH_BYTES
                            else hashlib.sha256(path.read_bytes()).hexdigest())
            except OSError:
                continue
            if len(out) >= MAX_SNAPSHOT_FILES:
                return out
    return out


def start(ctx):
    """Taken when the turn starts: what the workspace held before the builder touched it."""
    return {'before': snapshot(ctx.workspace.root), 'criteria': None, 'rounds': 0, 'versions': None,
            'started': time.strftime('%Y%m%d-%H%M%S')}


def changed(state, ctx):
    now = snapshot(ctx.workspace.root)
    before = state['before']
    return sorted(k for k in set(now) | set(before) if now.get(k) != before.get(k))


def _versions_dir(state, ctx):
    if state.get('versions'):
        return state['versions']
    base = env_get('AGENTIC_GRADER_DIR', '').strip()
    if base:
        base = pathlib.Path(base)
    else:
        data = env_get('XDG_DATA_HOME', '').strip() or str(pathlib.Path.home() / '.local' / 'share')
        base = pathlib.Path(data) / 'agenticbot' / 'grader-runs'
    slug = re.sub(r'[^\w.-]', '_', '%s-%s-%s' % (state['started'], ctx.session_id or 'session', ctx.turn_id or 'turn'))
    state['versions'] = str(base / slug)
    return state['versions']


def keep_version(state, ctx, files, verdict):
    """Copy this round's changed files and the verdict aside, so every version survives the next fix."""
    target = pathlib.Path(_versions_dir(state, ctx)) / ('round-%d' % state['rounds'])
    try:
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for rel in files:
            src = ctx.workspace.root / rel
            if not src.is_file() or src.is_symlink():
                continue
            size = src.stat().st_size
            if size > MAX_HASH_BYTES or copied + size > 5 * MAX_HASH_BYTES:
                continue
            dest = target / 'files' / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied += size
        (target / 'verdict.json').write_text(json.dumps(verdict, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        return str(target)
    except OSError:
        return ''


# ---------------------------------------------------------------- reading files

def _read_text(ctx, rel, limit=MAX_FILE_CHARS):
    path = ctx.workspace.path(rel)
    if not path.is_file():
        return None
    suffix = path.suffix.lower()
    if suffix == '.xlsx':
        try:
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                lines = []
                for ws in book.worksheets:
                    lines.append('# sheet ' + ws.title)
                    for row in ws.iter_rows(values_only=True):
                        lines.append(','.join('' if v is None else str(v) for v in row))
                        if len(lines) > 400:
                            break
                text = '\n'.join(lines)
            finally:
                book.close()
        except Exception as exc:  # noqa: BLE001
            return '(could not read xlsx: %s)' % type(exc).__name__
    elif suffix == '.docx':
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read('word/document.xml').decode('utf-8', 'replace')
            xml = re.sub(r'</w:p>', '\n', xml)
            text = re.sub(r'<[^>]+>', '', xml)
        except Exception as exc:  # noqa: BLE001
            return '(could not read docx: %s)' % type(exc).__name__
    else:
        raw = path.read_bytes()[:limit * 4]
        if b'\x00' in raw[:4096]:
            return '(binary file, %d bytes)' % path.stat().st_size
        text = raw.decode('utf-8', 'replace')
    return text if len(text) <= limit else text[:limit] + '\n... (cut at %d chars)' % limit


def _parse_json(text):
    decoder = json.JSONDecoder()
    text = text or ''
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError('grader reply had no JSON object')


# ---------------------------------------------------------------- criteria

def _clean_check(check, files):
    if not isinstance(check, dict) or check.get('kind') not in KINDS:
        return {'kind': 'judge', 'paths': []}
    kind = check['kind']
    if kind in ('sort', 'mobile', 'file'):
        path = check.get('path')
        if not isinstance(path, str) or not path.strip():
            return {'kind': 'judge', 'paths': []}
        out = {'kind': kind, 'path': path.strip()}
        if kind == 'sort':
            column = check.get('column')
            if not isinstance(column, str) or not column.strip():
                return {'kind': 'judge', 'paths': [out['path']]}
            out['column'] = column.strip()
        return out
    if kind == 'tests':
        command = check.get('command')
        if not isinstance(command, str) or not _TEST_COMMAND.match(command.strip()):
            return {'kind': 'judge', 'paths': [], 'note': 'proposed command was not a plain test runner'}
        return {'kind': 'tests', 'command': command.strip()}
    paths = [p for p in (check.get('paths') or []) if isinstance(p, str) and p in files][:6]
    return {'kind': 'judge', 'paths': paths}


def make_criteria(request, ctx, files, changed_files, grader_fn):
    listing = '\n'.join('%s%s' % (f, '  (changed this turn)' if f in changed_files else '') for f in files[:200])
    messages = [{'role': 'system', 'content': CRITERIA_PROMPT},
                {'role': 'user', 'content': 'User request:\n' + json.dumps(request, ensure_ascii=False)
                 + '\n\nWorkspace files:\n' + (listing or '(none)')}]
    data = _parse_json(grader_fn(messages))
    raw = data.get('criteria')
    if not isinstance(raw, list) or not raw:
        raise ValueError('grader returned no criteria')
    criteria = []
    for item in raw[:MAX_CRITERIA]:
        if isinstance(item, str):
            item = {'text': item}
        if not isinstance(item, dict) or not str(item.get('text', '')).strip():
            continue
        criteria.append({'id': len(criteria) + 1, 'text': str(item['text']).strip()[:300],
                         'check': _clean_check(item.get('check'), set(files))})
    if not criteria:
        raise ValueError('grader returned no usable criteria')
    return criteria


# ---------------------------------------------------------------- host checks

def _result(criterion, passed, evidence_text, fixable=True, by='host'):
    return {'id': criterion['id'], 'requirement': criterion['text'], 'passed': bool(passed),
            'evidence': evidence_text[:600], 'fixable': fixable, 'by': by, 'check': criterion['check']['kind']}


def _check_file(ctx, c):
    try:
        path = ctx.workspace.path(c['check']['path'])
    except Exception as exc:  # noqa: BLE001
        return _result(c, False, 'Path not allowed: %s' % exc)
    ok = path.is_file() and path.stat().st_size > 0
    return _result(c, ok, ('%s exists (%d bytes).' % (c['check']['path'], path.stat().st_size)) if ok
                   else '%s is missing or empty.' % c['check']['path'])


def _check_sort(ctx, c):
    from agenticbot.tools import ToolError
    from agenticbot.tools.sortcheck import read_column
    try:
        values = read_column(ctx, c['check']['path'], c['check']['column'])
    except ToolError as exc:
        return _result(c, False, str(exc))
    except Exception as exc:  # noqa: BLE001
        return _result(c, False, 'Could not read %s: %s' % (c['check']['path'], type(exc).__name__))
    keys = [evidence.sort_key(v) for v in values]
    bad = [(i + 3, values[i], values[i + 1]) for i in range(len(keys) - 1) if keys[i] > keys[i + 1]]
    if bad:
        row, prev, cur = bad[0]
        return _result(c, False, 'Not A-Z: row %d "%s" comes after "%s" (%d inversion%s, accent-insensitive).'
                       % (row, cur, prev, len(bad), '' if len(bad) == 1 else 's'))
    return _result(c, True, '%d rows in accent-insensitive A-Z order (measured).' % len(values))


def _check_mobile(ctx, c):
    try:
        from agenticbot.preview import PreviewSession, resolve_html_path
    except Exception as exc:  # noqa: BLE001
        return _result(c, False, 'Not checked: no headless browser (%s).' % type(exc).__name__, fixable=False)
    try:
        rel, _ = resolve_html_path(ctx, c['check']['path'])
    except Exception as exc:  # noqa: BLE001
        return _result(c, False, str(exc))
    session = None
    try:
        session = PreviewSession(ctx.workspace.root)
        layout = session.capture(rel, 'mobile')['layout']
    except Exception as exc:  # noqa: BLE001
        return _result(c, False, 'Not checked: the page could not be rendered (%s).' % str(exc)[:200], fixable=False)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
    width, client = layout.get('scroll_width'), layout.get('client_width')
    outside = layout.get('outside_viewport') or []
    if layout.get('horizontal_overflow'):
        extra = ''
        if outside:
            first = outside[0]
            extra = ' First element outside: %s.' % (json.dumps(first, ensure_ascii=False)[:160] if not isinstance(first, str) else first)
        return _result(c, False, 'Rendered at 390 px: page is %spx wide vs %spx viewport (horizontal overflow).%s'
                       % (width, client, extra))
    return _result(c, True, 'Rendered at 390 px: scrollWidth %s = clientWidth %s, no overflow (measured).' % (width, client))


def _check_tests(ctx, c, state):
    before = state['before']
    now = snapshot(ctx.workspace.root)
    edited = sorted(rel for rel in before if _TEST_FILE.search(rel) and now.get(rel) != before[rel])
    if edited:
        return _result(c, False, 'Existing test file(s) were changed this turn: %s. Edited tests are not accepted '
                       'as proof; put them back as they were and fix the code instead.' % ', '.join(edited[:5]))
    run = sandbox.get_backend().run(c['check']['command'], ctx.workspace.root, config.COMMAND_TIMEOUT,
                                    network=False, read_only=True, stop=ctx.stop)
    passed = run.exit_code == 0 and not run.timed_out and not getattr(run, 'stopped', False)
    tail = (run.render() or '').strip()[-400:]
    return _result(c, passed, 'Grader ran `%s`: %s' % (c['check']['command'], tail))


def _judge(ctx, request, criteria, files, grader_fn):
    if not criteria:
        return []
    wanted = []
    for c in criteria:
        wanted.extend(c['check'].get('paths') or [])
    wanted = list(dict.fromkeys(wanted or files))[:8]
    parts, total = [], 0
    for rel in wanted:
        try:
            text = _read_text(ctx, rel)
        except Exception:  # noqa: BLE001
            text = None
        if text is None:
            continue
        if total + len(text) > MAX_TOTAL_CHARS:
            text = text[:max(0, MAX_TOTAL_CHARS - total)] + '\n... (cut)'
        total += len(text)
        parts.append('=== %s ===\n%s' % (rel, text))
        if total >= MAX_TOTAL_CHARS:
            break
    listing = '\n'.join('%d. %s' % (c['id'], c['text']) for c in criteria)
    messages = [{'role': 'system', 'content': JUDGE_PROMPT},
                {'role': 'user', 'content': 'User request:\n' + json.dumps(request, ensure_ascii=False)
                 + '\n\nCriteria:\n' + listing + '\n\nFiles:\n' + ('\n\n'.join(parts) or '(no readable files)')}]
    data = _parse_json(grader_fn(messages))
    answers = {}
    for item in data.get('results') or []:
        if isinstance(item, dict):
            try:
                answers[int(item.get('id'))] = item
            except (TypeError, ValueError):
                continue
    out = []
    for c in criteria:
        item = answers.get(c['id'])
        if item is None:
            out.append(_result(c, False, 'Grader gave no answer for this criterion.', fixable=False, by='judge'))
        else:
            out.append(_result(c, item.get('passed') is True, str(item.get('reason') or '').strip() or 'no reason given',
                               by='judge'))
    return out


def grade(state, ctx, request, grader_fn):
    """One grading round. Returns a completion_check-shaped verdict with status
    completed (all passed), incomplete (something the builder can fix failed) or
    unverified (the grader itself could not check)."""
    state['rounds'] += 1
    files = sorted(snapshot(ctx.workspace.root))
    changed_files = changed(state, ctx)
    try:
        if state['criteria'] is None:
            state['criteria'] = make_criteria(request, ctx, files, changed_files, grader_fn)
        criteria = state['criteria']
        results, judged = {}, []
        for c in criteria:
            kind = c['check']['kind']
            if kind == 'file':
                results[c['id']] = _check_file(ctx, c)
            elif kind == 'sort':
                results[c['id']] = _check_sort(ctx, c)
            elif kind == 'mobile':
                results[c['id']] = _check_mobile(ctx, c)
            elif kind == 'tests':
                results[c['id']] = _check_tests(ctx, c, state)
            else:
                judged.append(c)
        for r in _judge(ctx, request, judged, changed_files or files, grader_fn):
            results[r['id']] = r
        checks = [results[c['id']] for c in criteria]
    except Exception as exc:  # noqa: BLE001 - a broken grader fails closed
        verdict = {'passed': False, 'missing': ['Independent grade'], 'checks': [], 'status': 'unverified',
                   'fixable': False, 'grader': True, 'round': state['rounds'],
                   'error': type(exc).__name__ + ': ' + str(exc)[:300],
                   'note': 'The grader could not run (%s), so this is not verified.' % type(exc).__name__}
        verdict['versions'] = keep_version(state, ctx, changed_files, verdict)
        return verdict
    failed = [r for r in checks if not r['passed']]
    fixable = any(r['fixable'] for r in failed)
    status = 'completed' if not failed else ('incomplete' if fixable else 'unverified')
    measured = sum(1 for r in checks if r['by'] == 'host')
    verdict = {'passed': not failed, 'missing': [r['requirement'] for r in failed], 'checks': checks,
               'status': status, 'fixable': fixable, 'grader': True, 'round': state['rounds'],
               'note': 'Graded separately (round %d): %d/%d criteria passed, %d measured by the host.'
                       % (state['rounds'], len(checks) - len(failed), len(checks), measured)}
    verdict['versions'] = keep_version(state, ctx, changed_files, verdict)
    return verdict


def repair_note(verdict):
    """Only what failed goes back to the builder: no passing items, no grader reasoning beyond the evidence."""
    lines = ['GRADER (an independent check by Agentic Bot, not you) did not accept this as done. '
             'Fix ONLY these failed criteria, then finish. Do not edit existing tests or the checks; '
             'the grader runs them itself.']
    for r in verdict.get('checks') or []:
        if not r['passed'] and r.get('fixable', True):
            lines.append('- %s: %s' % (r['requirement'], r['evidence']))
    return '\n'.join(lines)
