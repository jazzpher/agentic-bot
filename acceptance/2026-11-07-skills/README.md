# Live skill learning and reuse acceptance

Live NVIDIA nemotron-3.5-super-vl-120b-a12b, 7 November 2026. Both phases completed and independent exact artifact checks passed. No mocked model calls. The fresh second conversation retrieved the saved workflow and reproduced its method marker on different data including a negative amount. The harness explicitly requests staging and retrieval: this establishes procedural reuse, not spontaneous learning or Hermes parity.

Temporary key was revoked and local key file removed. No GPU, training, embeddings or database added. Host-supplied requirements and independent verifier are mandatory for learning; ordinary CLI/UI can retrieve but do not acquire this verifier automatically.

Rerun from repo root with a new output path: `SKILLS_ACCEPTANCE_DIR=/path/to/new-run NVIDIA_API_KEY_FILE=/path/to/key python acceptance/2026-11-07-skills/run_live.py`. The output directory must not already exist. `stored-skills` is the persisted skill after reuse. events.jsonl records the artifact before persistence and tool order; summary.json records both phase assertions.
