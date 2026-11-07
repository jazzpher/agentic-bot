# Lightweight skills in the unified runtime

Skills are bounded Markdown workflows, not trained weights or executable plugins. The existing session-private SkillStore and retrieval index are reused. No GPU, embedding model or vector database is added.

## Retrieval and learning

The runtime exposes list_skills and read_skill and includes the relevance-ranked, byte-bounded skill index in the system context. Skill content is untrusted guidance: it grants no tools or sandbox permissions. Read only the relevant skill to avoid unnecessary context tokens.

save_skill stages up to three candidates per turn, each at most 12 KB including front matter. It does not write immediately. The host must supply run_turn requirements and a completion_verifier callback. The callback must independently check the task artifacts and return requirement -> {passed: true, evidence: nonempty string}. A model claiming success cannot bypass this check.

Candidates persist only when the turn completes and every acceptance requirement passes. Failed, truncated, stopped and step-limited turns discard candidates. Without a host verifier, ordinary chat can still retrieve existing skills but cannot learn new ones. This feature is not autonomous semantic verification and does not silently install an oracle in the CLI or UI.

The runtime emits skill_saved only after persistence succeeds. Updating a skill requires its current revision, obtained with read_skill. A stale revision or storage error leaves the existing skill intact and emits a status message.

Use generic placeholders, never personal data, credentials or task-specific secrets. Known credential patterns are rejected, but pattern matching cannot detect every secret or personal detail. Hosts should review sensitive deployments. Existing path/symlink checks, atomic saves and rollback remain enforced. The subprocess sandbox is not a security boundary; use Docker for untrusted repositories.

## Validation

Run python -m unittest test_skill_runtime -v for lifecycle tests. Scripted model tests establish runtime behavior, not live-model learning quality. Live acceptance evidence, when recorded, is separate and must show both independent artifact checks and a second task reading and using the saved skill.
