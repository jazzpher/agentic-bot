"""Immutable request anchors and an optional, host-owned semantic completion gate.

A model-written test suite is NOT independent acceptance. Callers that supply
requirements must also supply a trusted verifier: missing or failed evidence
fails closed. No verifier, code or command is ever read from conversation history.

Lifecycle: every turn anchors the newest user request verbatim on the first
message, outside any model-written summary, so compaction cannot drop it. A turn
that passes requirements starts a new acceptance-bound task. Only an explicit
resume (completion_resume=True, e.g. "continue" after max_steps or an incomplete
result) carries an earlier contract forward, so one task's acceptance never
leaks into an unrelated later request.
"""
import copy
import json

KEY = '_completion_contract'
REPAIR_NOTE_LIMIT = 4000


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return ' '.join(p.get('text', '') for p in content
                        if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def _latest_request(messages):
    for message in reversed(messages):
        if message.get('role') == 'user' and not message.get('_preview_image'):
            return _text(message.get('content', ''))
    return ''


def attach(messages, requirements=None, resume=False):
    """Anchor this turn's contract on messages[0] (mutates the given dicts) and return it."""
    existing = next((m[KEY] for m in messages if isinstance(m.get(KEY), dict)), None)
    if requirements is not None:
        if not isinstance(requirements, (list, tuple)) or not requirements or any(
                not isinstance(r, str) or not r.strip() for r in requirements):
            raise ValueError('requirements must be a nonempty list of nonempty strings')
        contract = {'version': 1, 'request': _latest_request(messages), 'requirements': list(requirements)}
    elif resume and existing:
        contract = copy.deepcopy(existing)
    else:
        contract = {'version': 1, 'request': _latest_request(messages), 'requirements': []}
    for message in messages:
        message.pop(KEY, None)
    if messages:
        messages[0][KEY] = copy.deepcopy(contract)
    return contract

def _visible_text(content):
    """User text as the model sees it, ignoring the note a text-only resend appends to an image turn."""
    text = _text(content)
    cut = text.find(' [image not sent:')
    return text[:cut] if cut >= 0 else text


def prompt(contract, messages=()):
    """System-prompt addition. Empty for plain chat whose request is still visible verbatim."""
    requirements = contract.get('requirements') or []
    request = contract.get('request') or ''
    visible = any(m.get('role') == 'user' and _visible_text(m.get('content')) == request for m in messages)
    parts = []
    if request and not visible:
        parts.append('Original request (verbatim; earlier context was compacted, this is not a summary): '
                     + json.dumps(request, ensure_ascii=False))
    if requirements:
        parts.append('Acceptance requirements an independent check verifies before this turn may finish: '
                     + json.dumps(requirements, ensure_ascii=False)
                     + '. Check every one, including edge cases and documentation. Passing your own tests '
                       'alone is not proof of completion. Report missing or unverified work explicitly.')
    return ('\n\n' + '\n'.join(parts)) if parts else ''


def check(contract, verifier, ctx):
    requirements = contract.get('requirements') or []
    if not requirements:
        return None  # ordinary chat has no independently configured acceptance suite
    if verifier is None:
        return {'passed': False, 'missing': list(requirements), 'error': 'No independent completion verifier configured.'}
    try:
        evidence = verifier(copy.deepcopy(contract), ctx)
        if not isinstance(evidence, dict):
            raise ValueError('verifier must return requirement -> evidence mappings')
        missing = [r for r in requirements if not isinstance(evidence.get(r), dict)
                   or evidence[r].get('passed') is not True
                   or not isinstance(evidence[r].get('evidence'), str)
                   or not evidence[r]['evidence'].strip()]
        return {'passed': not missing, 'missing': missing, 'evidence': evidence}
    except Exception as exc:  # noqa: BLE001 - a broken verifier must fail closed
        return {'passed': False, 'missing': list(requirements), 'error': type(exc).__name__ + ': ' + str(exc)[:500]}


def repair_note(verdict):
    """Bounded feedback for the model after a failed acceptance check."""
    detail = {'missing': verdict.get('missing', []), 'error': verdict.get('error', '')}
    evidence = verdict.get('evidence')
    if isinstance(evidence, dict):
        detail['evidence'] = {r: evidence.get(r) for r in detail['missing']}
    return ('Independent acceptance failed. Fix these original requirements before finishing: '
            + json.dumps(detail, ensure_ascii=False, default=str)[:REPAIR_NOTE_LIMIT])


def automatic_check(contract, ctx, text):
    """Limited host evidence, never a semantic oracle or skill-learning approval.

    Even passing literal checks remain unverified: file existence and a title
    cannot establish correctness of a whole natural-language request.
    """
    import re
    from agenticbot.tools import ToolError
    request = contract.get('request', '')
    checks = []
    # Restrict extraction to explicit output clauses, not referenced input files.
    clauses = re.findall(r'\b(?:create|write|save|generate|produce|export)\b[^\n;!?]*', request, re.I)
    paths = []
    for clause in clauses:
        paths.extend(re.findall(r'(?<![\w/])([\w./-]+\.(?:csv|md|txt|json|html|py|pdf|docx|xlsx))\b', clause))
    for name in dict.fromkeys(paths):
        try:
            path = ctx.workspace.path(name)
            passed = path.is_file() and path.stat().st_size > 0
            checks.append({'requirement': 'Output file: ' + name, 'passed': passed,
                           'evidence': 'Nonempty file exists (content correctness not checked).' if passed else 'File missing or empty.'})
        except (ToolError, ValueError, OSError):
            checks.append({'requirement': 'Output file: ' + name, 'passed': False,
                           'evidence': 'Output path unavailable or outside the workspace.'})
    titles = re.findall(r'\b(?:title(?:d)?|heading)(?:\s+(?:it|is|must be|should be|exactly|the report|the document))*\s*[:=]?\s*["\u201c]([^"\u201d]+)["\u201d]', request, re.I)
    output_text = text
    for name in dict.fromkeys(paths):
        if name.endswith(('.md', '.txt', '.html')):
            try:
                path = ctx.workspace.path(name)
                if path.is_file() and path.stat().st_size <= 200_000:
                    output_text += '\n' + path.read_text(encoding='utf-8')
            except (ToolError, ValueError, OSError, UnicodeError):
                pass
    for title in dict.fromkeys(titles):
        passed = title in output_text
        checks.append({'requirement': 'Exact title: ' + title, 'passed': passed,
                       'evidence': 'Literal title found in response/output.' if passed else 'Literal title missing.'})
    missing = [c['requirement'] for c in checks if not c['passed']]
    return {'passed': not missing, 'missing': missing, 'checks': checks,
            'coverage': 'limited', 'status': 'incomplete' if missing else 'unverified',
            'note': 'Only explicit files and quoted titles are checked. Full request correctness is not independently verified.'}


def command_verifier(command, timeout=30):
    """Capture an owner-supplied acceptance command, never model/history content.

    The owner's check must protect its own expected values; a test file in a
    model-writable workspace is not tamper-proof independent evidence.
    """
    from agenticbot import sandbox
    def verify(contract, ctx):
        result = sandbox.get_backend().run(command, ctx.workspace.root, timeout,
                                           network=False, read_only=True, stop=ctx.stop)
        passed = result.exit_code == 0 and not result.timed_out and not result.stopped
        return {r: {'passed': passed, 'evidence': result.render()} for r in contract['requirements']}
    return verify
