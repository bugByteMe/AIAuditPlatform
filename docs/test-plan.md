# Test Plan

## Permissions

- A user cannot open, chat against, or download artifacts from another user's private workspace.
- A group member can access a group-shared workspace.
- A user outside the group cannot access a group-shared workspace.
- A disabled user cannot start or resume a chat.

## Budget Enforcement

- A run is rejected when the user budget is exhausted.
- A run is rejected when the group budget is exhausted.
- An active run is stopped when usage exhausts the user or group budget.
- Usage is recorded for completed, stopped, and failed runs.
- Admin budget increases allow new runs after exhaustion.

## Scheduling

- Runs are assigned only to workers with fresh heartbeats.
- Runs stay queued when all workers are full.
- New runs are not assigned to unhealthy workers.
- Worker capacity updates affect scheduling decisions.
- Workspace lock prevents two active mutating runs on the same workspace.

## Workspace Lifecycle

- User can upload files and folders into a new workspace.
- Large upload limits are enforced.
- Workspace fork creates an independent workspace from the selected snapshot.
- Group-shared workspace is visible to group members.
- Snapshot comparison detects added, modified, and deleted files.

## Chat Lifecycle

- Start creates a session, a run, and a container assignment.
- Stop transitions through `stopping` and creates a resumable checkpoint.
- Resume creates a new run from the latest checkpoint.
- Completion creates a final snapshot and artifact records.
- Container failure records an error and preserves recoverable state when possible.

## Real-Time Streaming

- Frontend receives queued, started, running, stopped, completed, and failed events.
- Model output appears incrementally.
- Tool call events are streamed and persisted.
- Token usage and budget warnings are streamed.
- Page reload can reconstruct chat history from persisted events.

## Artifacts

- Created files are listed as artifacts.
- Modified files are listed as artifacts.
- Deleted files are recorded in history but are not downloadable.
- Single-file download enforces permissions.
- Bundle download includes only expected files and a metadata manifest.

## Admin

- Admin can batch-create users and groups.
- Batch-created accounts have unique immutable IDs and invite tokens but no username or password hash.
- Only system admins can list or revoke unused invite tokens.
- Invite registration enforces single-use tokens, case-insensitive username uniqueness, and the minimum password length.
- Successful registration preserves group and budget settings and creates a login session.
- Admin can assign and increase budgets.
- Admin can disable users and groups.
- Admin can inspect usage by user, group, workspace, model, and date range.
- Admin actions are written to the audit log.
