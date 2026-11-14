"""A read-only, deterministic order check for actual tabular artifacts."""
import csv
import json
from agenticbot import evidence
from agenticbot.tools import ToolError


def register(registry):
    @registry.tool('verify_sort', 'Check an existing CSV/XLSX column in accent-insensitive Unicode A–Z order. '
                   'Álvarez sorts before Reyes. Checks ordering only, not row completeness or content correctness. '
                   'Use after the final edit; a later edit invalidates the receipt.',
                   {'type': 'object', 'properties': {
                       'path': {'type': 'string'}, 'column': {'type': 'string'},
                       'sheet': {'type': 'string', 'description': 'XLSX sheet name, default first sheet'}},
                    'required': ['path', 'column']}, category='verify', parallel_safe=False)
    def verify_sort(ctx, path, column, sheet=None):
        p = ctx.workspace.path(path)
        before = evidence.digest(ctx, path)
        if p.suffix.lower() == '.csv':
            with p.open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                if column not in (reader.fieldnames or []):
                    raise ToolError('Column missing: ' + column)
                values = [row.get(column) for row in reader]
        elif p.suffix.lower() == '.xlsx':
            try:
                import openpyxl
            except ImportError as exc:
                raise ToolError('XLSX checking requires openpyxl.') from exc
            book = openpyxl.load_workbook(p, read_only=True, data_only=False)
            try:
                ws = book[sheet] if sheet else book.worksheets[0]
                rows = ws.iter_rows(values_only=True)
                header = list(next(rows, ()))
                if column not in header:
                    raise ToolError('Column missing: ' + column)
                idx = header.index(column)
                values = [row[idx] if idx < len(row) else None for row in rows]
            finally:
                book.close()
        else:
            raise ToolError('verify_sort supports CSV and XLSX only.')
        if not values or any(v is None or not str(v).strip() or str(v).startswith('=') for v in values):
            raise ToolError('No rows, blank keys, or formula keys: order not verified.')
        if evidence.digest(ctx, path) != before:
            raise ToolError('File changed during verification; recheck.')
        keys = [evidence.sort_key(v) for v in values]
        inversions = [{'row': i + 3, 'previous': str(values[i]), 'value': str(values[i + 1])}
                      for i in range(len(keys) - 1) if keys[i] > keys[i + 1]]
        passed = not inversions
        evidence.record(ctx, 'sort', path, passed, column=column, sheet=sheet, rows=len(values))
        return json.dumps({'passed': passed, 'path': path, 'column': column, 'rows': len(values),
                           'convention': 'Unicode NFKD, casefold, combining accents removed',
                           'inversions': inversions[:10], 'scope': 'Ordering only; content/completeness not verified.'}, ensure_ascii=False)
