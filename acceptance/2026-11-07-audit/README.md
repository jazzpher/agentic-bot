# PR #59 live acceptance (2026-11-07)

Provider: NVIDIA NIM, model nvidia/nemotron-3.5-super-vl-120b-a12b, Linux Codespace, temporary key (revoked after).

## Live CLI
1. Write notes.md: file correct, exit 0, verification `unverified`.
2. Read-only sandbox with -y: escalation auto-approved, exit 0, `unverified`.
3. Read-only sandbox, no -y, stdin /dev/null: nothing written, `incomplete`, exit 1.

## Live web UI (desktop 1280x800)
1. "Write summary.md titled "UI Live Check" with 2 bullets": UI shows Verification unverified, Output file summary.md, Exact title UI Live Check, limitation line.
2. Browser off: agent said it could not verify; UI showed unverified, never completed.
3. Browser on: approval card (Allow once / Always / Deny) -> browse_open ok.
   Bug found live: CSP had no img-src, so data:image previews rendered broken. Fixed in this PR (img-src 'self' data: blob:) with a regression test; after restart preview and live view both render 1280x800.
4. Takeover: request_takeover card "Your turn in the browser", Live browser connected, Scroll and click sent through live view (input 200), Done -> "Handed back to the agent", agent resumed with browse_see and answered "Example Domain", verification line shown.

## Mobile (390x844)
No horizontal overflow (scrollWidth 390). Drawer off-canvas at x=-250, menu opens to x=0.
Bug found live: Escape and outside tap did not close the drawer. Fixed in this PR; rechecked live: Escape -> -250, outside tap -> -250. Empty-state hero hides after first send.

## Known nits (not fixed here)
- Two verification lines are shown together (somewhat redundant).
- Streamed text sometimes drops a first character; reasoning chunks join without a space.
- New chat keeps the previous /c/ URL.
- Model occasionally ends a browse turn with no reply text (model side; UI still says unverified).

## Limits
Automatic verification only checks explicit output files and quoted titles. It is not proof the full request is correct.
