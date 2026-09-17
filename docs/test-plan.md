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
- `turn.completed` input and output usage is persisted once even if the terminal event is duplicated; cached input is not double-counted.
- Resetting a user's budget replaces the allowance and clears consumed tokens.

## Disk Quotas

- Per-user and per-group usage sums current logical sizes of owned workspaces.
- Unlimited groups accept workspace creation, uploads, forks, and runs.
- Limited groups reject operations whose projected logical size exceeds the limit.
- Same-size replacements and deletion remain possible at or above the limit.
- A post-run overshoot is reflected in admin usage and blocks subsequent growth and runs.

## Scheduling

- Config parsing rejects duplicate IDs, invalid endpoints, and non-positive CPU or memory and accepts byte/unit memory values.
- Runs are assigned only to workers with fresh heartbeats.
- Runs stay queued when all workers are full.
- New runs are not assigned to unhealthy workers.
- CPU and memory reservations are atomic and the least-utilized compatible node wins with a stable tie-break.
- A configured remote pool never silently falls back to local Docker; an empty pool retains local execution.
- Start is idempotent by run ID and restart recovery continues from the persisted worker event cursor.
- Lost control-plane leases stop orphaned worker containers; worker loss fails the run without automatic retry after the lease fence.
- Worker HTTPS rejects missing/incorrect bearer credentials and the control plane verifies the configured CA.
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
- Admin can create an empty group and set, clear, or lower its disk limit.
- User deletion stops active runs, removes owned workspace/chat/Codex-home data, and invalidates sessions.
- Group deletion cascades through active and pending members and their owned resources.
- The signed-in administrator and the last remaining system administrator cannot be deleted.
- A run stop timeout rejects cascading deletion without removing the account or group.
- Only system admins can read compute-worker health and resource summaries.
- The admin worker cards and chat header render available/total CPU and memory without restoring the removed large console heading.
