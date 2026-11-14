"""Narrow, host-owned receipts, not a semantic correctness oracle.

A command exiting zero (or model-written checks) does not certify prose logic.
Receipts last one turn and are invalidated by changes to the checked artifact.
"""
import hashlib
import re
import unicodedata

KEY = '_claim_evidence'


def digest(ctx, path):
    p = ctx.workspace.path(path)
    if not p.is_file() or p.stat().st_size > 20_000_000:
        raise ValueError('Verification requires a file under 20 MB.')
    return hashlib.sha256(p.read_bytes()).hexdigest()


def visual_digest(ctx):
    """Include local styles/scripts/images: editing a dependency invalidates a preview."""
    h = hashlib.sha256()
    total = 0
    for p in sorted(ctx.workspace.root.rglob('*')):
        if '.git' in p.parts or p.suffix.lower() not in {'.html', '.htm', '.css', '.js', '.svg', '.png', '.jpg', '.jpeg', '.webp', '.woff', '.woff2'}:
            continue
        if p.is_symlink():
            raise ValueError('Cannot certify visual dependencies through symlinks.')
        if not p.is_file():
            continue
        total += p.stat().st_size
        if total > 40_000_000:
            raise ValueError('Visual dependency set exceeds 40 MB; no verification receipt issued.')
        h.update(str(p.relative_to(ctx.workspace.root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def sort_key(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value).casefold())
                   if not unicodedata.combining(c))


def record(ctx, kind, path, passed, **details):
    receipt = {'kind': kind, 'path': path, 'passed': passed, 'digest': digest(ctx, path), **details}
    ctx.extras.setdefault(KEY, []).append(receipt)
    return receipt


def valid(ctx, kind):
    out = []
    for r in ctx.extras.get(KEY, []):
        if r.get('kind') != kind or not r.get('passed'):
            continue
        try:
            if digest(ctx, r['path']) != r['digest']:
                continue
            if kind == 'mobile' and visual_digest(ctx) != r.get('dependencies'):
                continue
            out.append(r)
        except (ValueError, OSError):
            continue
    return out


# Intentionally narrow language checks. These are safeguards for known failure
# modes, NOT a general semantic verifier. Negative/qualified claims are allowed.
_SORT = re.compile(r'\b(?:sorted\b[^\n.!?]{0,90}(?:a\s*[-–,→]\s*z|alphabet|surname)|(?:a\s*[-–,→]\s*z|alphabetical(?:ly)?)\s+(?:sort(?:ed|ing)?|order)|(?:correctly|properly)\s+sorted)\b', re.I)
_MOBILE = re.compile(r'\b(?:no\s+(?:(?:horizontal|mobile)\s+)?overflow|(?:fits?|works?|verified|tested|checked|passes?|responsive)\b[^\n.!?]{0,65}\bmobile|mobile\b[^\n.!?]{0,65}\b(?:fits?|works?|verified|tested|checked|passes?|responsive|overflow.free)|overflow\s*:\s*(?:none|no|false))\b', re.I)
_ALL = re.compile(r'\b(?:(?:all|every)\s+(?:the\s+)?(?:checks?|tests?|requirements?)\b[^\n.!?]{0,50}\b(?:pass(?:ed)?|met|satisfied|complete)|(?:fully|independently)\s+verified|(?:verified|checked)\s+(?:everything|all)|self[- ]check\s*:\s*(?:pass|ok)|logic\b[^\n.!?]{0,30}\b(?:verified|validated|correct))\b', re.I)
# "Verified by reviewing the CSS" names reading as the check: reading source is not a check.
_READ_VERIFY = re.compile(r"\b(?:verified|confirmed|checked|tested|validated|ensured)\b[^\n.!?]{0,40}?\bby\s+(?:just\s+)?(?:re-?)?(?:review(?:ing)?|read(?:ing)?|inspect(?:ing|ion)?|look(?:ing)?\s+(?:at|over|through)|examin(?:ing|ation)|eyeball(?:ing)?|scan(?:ning)?)\b", re.I)
_READ_NEG = re.compile(r"\b(?:not|never|cannot|can't|couldn't|unable|haven't|hasn't|wasn't|isn't)\s+(?:been\s+|be\s+)?$", re.I)
READ_MSG = ('Reading or reviewing the source is not verification: run the matching check '
            '(verify_sort, preview_page viewport="mobile") or say plainly that it is not verified.')

_NEGATIVE = re.compile(r"\b(?:not|never|cannot|can't|couldn't|unverified|unconfirmed|unable|needs?|pending|still|remaining|failed|failure|without|before|don't|do not|must|should|whether|if)\b", re.I)


def supports(ctx, kind, sentence):
    receipts = valid(ctx, kind)
    mentioned = re.findall(r'[\w./-]+\.(?:csv|xlsx|html|htm)\b', sentence, re.I)
    if mentioned:
        return all(any(r['path'] == path for r in receipts) for path in mentioned)
    # Without a named target, ambiguity must not let an unrelated receipt certify work.
    suffixes = {'.csv', '.xlsx'} if kind == 'sort' else {'.html', '.htm'}
    candidates = [str(p.relative_to(ctx.workspace.root)) for p in ctx.workspace.root.rglob('*')
                  if p.is_file() and '.git' not in p.parts and p.suffix.lower() in suffixes]
    return len(candidates) == 1 and any(r['path'] == candidates[0] for r in receipts)


def issues(ctx, text, independent=False):
    """Check affirmative sentences only, ignoring fenced code and explicit caveats."""
    text = re.sub(r'```[\s\S]*?```', '', text)
    missing = set()
    for sentence in re.split(r'(?<=[.!?])\s+|\n', text):
        m = _READ_VERIFY.search(sentence)
        if m and not independent and not _READ_NEG.search(sentence[:m.start()]):
            missing.add(READ_MSG)
        if re.search(r"\b(?:not|never|cannot|can't|couldn't|unverified|unconfirmed|unable|pending|failed|without)\b[^.!?]{0,35}(?:verified|checked|sorted|fits?|overflow|pass|correct)", sentence, re.I):
            continue
        if _SORT.search(sentence) and not supports(ctx, 'sort', sentence):
            missing.add('A–Z sorting needs verify_sort on the current output file.')
        if _MOBILE.search(sentence) and not supports(ctx, 'mobile', sentence):
            missing.add('Mobile fit needs a fresh mobile preview with zero measured horizontal overflow.')
        if _ALL.search(sentence) and not independent:
            missing.add('Blanket self-check/logic claims need an independent owner-supplied verifier; model self-review is not proof.')
    return sorted(missing)


PROMPT = '''\nEvidence rules (host enforced for known claim patterns):
- For CSV/XLSX surname A–Z claims, run verify_sort(path, column) on the FINAL artifact. It uses accent-insensitive Unicode ordering (Álvarez before Reyes); disclose that convention. It checks ordering only, not completeness or other columns.
- For mobile-fit/no-overflow claims, use preview_page(viewport="mobile") AFTER your last HTML/CSS/JS edit and inspect its image and measured widths. A desktop screenshot or a successful shell command is not a mobile check. After Chrome/thread failures in run_command, use preview_page instead of launching another sandbox browser. If preview is unavailable, say mobile layout is not verified.
- Never say "all checks passed", "logic verified", "fully verified" or equivalent on the strength of your own self-review or tests you wrote. Give specific evidence and limitations. Essay logic requires actual review of the argument, not a success label. Say "not independently verified" when no trusted verifier exists.
- Reading or reviewing code/CSS is not a check. Never write "verified by reviewing ..."; run the real check or say it is not verified.
- A failed check means unfinished work: fix and recheck within budget, or report the exact unresolved issue. Do not conceal overflow with overflow-x:hidden and call it fixed.
'''
