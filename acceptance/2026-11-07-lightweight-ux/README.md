# Lightweight activity UX acceptance — 7 Nov 2026

Base: main `931bb53` (includes PR #59). Candidate changes only UI presentation and a bounded workspace file-preview endpoint. No extra dependencies, screenshot polling, model calls, or training.

## Verification
- Python: 745 tests, OK, 41 skipped.
- All `test_*.cjs` JavaScript suites passed, including quiet/resume watchdog, capped logs, failed subprocess exit status, inert lazy previews and reduced motion.
- Wheel build and `git diff --check` passed.
- Real saved Gemini provider: file-write, terminal command and independent output-file checks passed. Real browser navigate/JavaScript/screenshot, takeover and return passed. Browser runs used the local proxy-isolation test seam because this Linux host lacks usable namespace isolation. No claim that these validate production sandbox security.
- Desktop and 390px mobile screenshots visually inspected. No horizontal overflow, takeover controls visible.
- Stream fixtures (not live provider faults) cover approval, failure, quiet 45s and resumption, step budget, EOF/disconnect and honest unknown cleanup.
- Normal completion checks can pass explicit file checks while coverage stays limited and verification status remains unverified. This is intentional, not proof of semantic correctness.

## Controlled browser resource comparison
`resources.json` contains five alternating paired runs in isolated headless Linux Chromium. Workload: 100 completed tool events, ~50KB output each, parsed as JSON. Medians:

| Measurement | Baseline | Candidate |
|---|---:|---:|
| Retained rendered tool cards | 100 | 24 |
| DOM nodes | 2737 | 1108 |
| Browser process PSS | 188259 KiB | 177775 KiB |
| Browser process RSS | 375028 KiB | 360184 KiB |
| Incremental retained JS heap | 73064 B | 375108 B |
| Processing task CPU | 0.044870 s | 0.063161 s |
| Completed-idle CPU, next 3 seconds | 0.001745 s | 0.001761 s |
| Animations after completion | 0 | 0 |

The bounded DOM reduced retained rendered cards/nodes and process PSS (~10 MiB in this synthetic workload), but candidate JS heap grew ~295 KiB and processing cost increased ~18ms. Do not claim a universal RAM/CPU reduction. Older exploratory runs used a different string/event setup and are superseded by this reproducible parsed-event comparison. Process memory varies; this is not whole-agent memory, laptop memory, battery life, or a provider benchmark.

Reproduce with `python acceptance/lightweight_ux_benchmark.py --baseline <baseline-url> --candidate <candidate-url> --output resources.json` after starting the respective Web UIs. Playwright is an optional test-only requirement, not a runtime dependency.

## Limits
Latest 24 tool cards per turn, arguments capped at 2k chars, output at 12k chars, screenshots bounded and lazy, text previews fetched only on click and capped at 64KiB. Preview rejects traversal, escaping symlinks, binary/non-UTF8 data, and unsafe paths. It uses the existing workspace-scoped access model. No automatic preview of arbitrary HTML. Old conversation turns are not globally virtualized.
