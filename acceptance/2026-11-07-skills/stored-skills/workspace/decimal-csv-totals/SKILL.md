---
name: decimal-csv-totals
description: "Compute exact-decimal group totals from a CSV and emit totals.json with a method marker and two-place decimal strings"
---
# decimal-csv-totals

## When to use
Sum an amount column grouped by a key column from a CSV using exact `Decimal` arithmetic, then write a JSON artifact with a method marker and each total as a decimal string with exactly two places.

## Method marker
The output JSON always includes `"method": "decimal-group-v1"` so consumers can identify the exact-decimal grouping method.

## Steps
1. Read the input CSV with `csv.DictReader`.
2. For each row, parse the amount with `Decimal(row["amount"])` — never `float`.
3. Accumulate per-group sums in a dict: `totals[key] = totals.get(key, Decimal("0")) + amount`.
4. Quantize every total to `Decimal("0.01")` and serialize as `str(...)` so each value is a string with exactly two decimal places.
5. Write `{"method": "decimal-group-v1", "totals": {<group>: "<two-place string>"}}` to `totals.json` (indent 2, trailing newline).

## Output schema
- Top-level keys: exactly `method` and `totals`.
- `method`: string `"decimal-group-v1"`.
- `totals`: object mapping group key to string; each string parses via `Decimal` and equals `str(total.quantize(Decimal("0.01")))`, with exactly two digits after the decimal point.
- No exponent notation, no `NaN`, no float artifacts.

## Verification (run independently before persisting)
- Reload `totals.json` with `json.load`; assert top-level keys == `{"method", "totals"}`.
- Assert `data["method"] == "decimal-group-v1"`.
- For each value: `isinstance(v, str)`, `Decimal(v)` parses exactly, `Decimal(v) == expected_sum.quantize(Decimal("0.01"))`, and `v == str(Decimal(v).quantize(Decimal("0.01")))` with exactly two fractional digits.
- Cross-check against the source CSV by re-grouping with `Decimal` and comparing every group total.
- Assert the file text contains no `e+` / `NaN` / float literals.

## Pitfalls
- Do not use `float` for parsing or accumulation; it breaks exact two-place decimal guarantees.
- Do not hardcode source data or credentials in the skill; keep column names and file paths as parameters.
- Always quantize to `0.01` before `str()` so trailing zeros are preserved (e.g. `"3.75"` not `"3.8"`).
