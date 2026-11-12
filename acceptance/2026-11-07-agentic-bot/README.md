# Agentic Bot rename acceptance

Branch: rename/agentic-bot, based on main after PR #60.

## Regression
- Python 3.11 (CI version): 752 tests, 39 skips, exit 0.
- Seven rename-focused tests: new/legacy environment precedence, module identity, CLI module names, preserved old data directory, instruction filenames, web assets, compatible backup format.
- Installed wheel smoke and legacy command/import aliases passed.
- All JavaScript test_*.cjs checks passed.

## Live acceptance
- NVIDIA-backed CLI wrote and read a real file under the new package name.
- Web UI performed a real file task. Independent filesystem read verified AGENTIC_UI_RENAME_OK.
- Agentic Bot title rendered. Mobile viewport 390x844 had no sidebar overlay.
- Browser opened example.com after approval, showed a 1280x800 screenshot, requested takeover, accepted a user scroll, returned to the bot and emitted a fresh screenshot (43px to 843px scroll).
- These live checks used a local test seam forcing proxy browser isolation because Codespaces cannot create the Linux namespace. No production isolation setting was relaxed. This is not a Linux namespace security proof. CI runs the strict isolation and Windows/macOS suites separately.
- Status remained honest/unverified without a supplied verifier.

## Limits
- Python 3.14 full discovery exposed an existing GC cleanup deadlock in Workspace.__del__ -> browser_tools.close. The same suite passed on supported CI Python 3.11; no out-of-scope cleanup change was made.
- Telegram branding and gateway regression tests passed offline. No real Telegram token or live Telegram conversation was used.
- xAI/Grok provider model identifiers are intentionally unchanged. Legacy cookies, archive manifests, data paths, theme fallback and Docker volume keys remain for compatibility.

Logs are sanitized; provider credentials and local login password are omitted.
