# Agent Rules

- Keep `docs/main.md` high level; put detailed design updates in focused files under `docs/`.
- Refer to `docs/` for current system design before changing implementation or architecture.
- Record development notes in `docs/dev/<yyyy-mm-dd>.md` for meaningful design, code, or test changes.
- Add or update unit tests for behavior changes, especially permissions, budget enforcement, workspace snapshots, artifact diffs, and chat lifecycle.
- Keep changes scoped and consistent with the existing design docs.
- Do not store secrets, credentials, private customer data, or sensitive audit data in docs, tests, or source files.
