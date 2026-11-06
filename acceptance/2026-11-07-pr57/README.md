# PR 57 live acceptance artifacts

Tested source: e8dfc1e4b7f59a281d05416510041f70035ec634.
Model: nvidia/nemotron-3.5-super-vl-120b-a12b via live NVIDIA NIM.
Total: 670.6 seconds (11m 11s), 85 tool calls, 4 context compactions.
Controlled cooperative stop after 8 evolution tool results, then resume with the completion contract retained. Not a hard-crash/restart test.
Independent build verification: 18/18. Final verification: 32/32, including execution of the generated 40-test suite, archive/date boundaries, migration, persistence and required docs.
Two completion verification callbacks occurred (build and resume).

## Comparison limits
The original November 6 task/harness was lost. This is explicitly a RECONSTRUCTED task, not a bit-for-bit rerun. Baseline recorded 805 seconds, 115 tools, 5 compactions, 57 own tests passing and 28/36 independent checks. The changed task/check count means these are descriptive results, not a controlled causal benchmark or proof of multi-hour unattended reliability.
The acceptance callback supplies independent checks to the completion gate. This validates the gate with an external verifier, not an autonomous oracle able to infer all requirements without one.

## Contents and reproduction
nvidia/requirements.txt contains exact reconstructed prompts; summary.json has phase outcomes; events.jsonl has redacted/truncated event records; conversation files and per-phase compressed code snapshots are retained. Final generated source is in nvidia/taskforge/.
harness/nim_acceptance.py ROOT evolve reruns independent checks. From nvidia/taskforge, python -m unittest discover runs generated tests.
The live harnesses use absolute Codespace paths and require adaptation of workspace paths and an independently provisioned API key. Keys are NOT included. No paid provider access was purchased.
Groq fix5 artifacts record browser open, text-only fallback after image rejection, takeover/Done and recovery through five rate-limit waits. This is a bounded integration test, not long-horizon Groq evidence.

## Local/CI validation
56 targeted tests passed. Full local Python run: 713 passed with one test_vetproxy flake, which passed twice in isolation. JavaScript, compile and smoke checks passed. GitHub CI was green on tested source; artifact commit is checked separately before merge.
