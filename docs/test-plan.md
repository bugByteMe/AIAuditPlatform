# Test Plan

## Permissions

- A user cannot open, chat against, or download artifacts from another user's private workspace.
- A group member can access a group-shared workspace.
- A user outside the group cannot access a group-shared workspace.
- A disabled user cannot start or resume a chat.

## Budget Enforcement

- A managed-provider run is rejected when the MicuAPI balance is zero or cannot be freshly verified.
- A custom-provider run does not query or consume the preserved MicuAPI balance.
- A run is rejected when the group budget is exhausted.
- MicuAPI quota errors during an active managed run are classified as budget failures.
- Usage is recorded for completed, stopped, and failed runs.
- Admin budget increases allow new runs after exhaustion.
- `turn.completed` input and output usage is persisted once even if the terminal event is duplicated; cached input is not double-counted.
- Adding a CNY amount increases the existing MicuAPI balance and preserves cumulative `usedTokens`.
- Activation provisions or reuses exactly one MicuAPI token named by username; failure preserves the invite.
- Startup reconciliation backfills missing active-account bindings without duplicating exact-name tokens.
- Regular users receive only a remaining-budget percentage, while exact CNY balances remain available only to system administrators.

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
- Exclusive workspace locking prevents two active runs, while disabled locking requires explicit confirmation for cross-session concurrency and always rejects same-session overlap.

## Workspace Lifecycle

- User can upload files and folders into a new workspace.
- Large upload limits are enforced.
- Upload manifests are rejected before byte transfer for unsafe paths, blocked types, count/size limits, permission failures, and quota exhaustion.
- Chunk reads stay bounded independently of total folder size; duplicate chunks are idempotent and conflicting retries fail.
- Uploads resume from persisted offsets after browser, control-plane, or worker restart, and can move to another healthy worker over shared storage.
- Existing workspaces remain mutation-locked until commit or cancellation; new workspaces are not visible before commit.
- Concurrent chat runs keep uploads, replacement, deletion, and forking locked until the final active run exits.
- Upload progress distinguishes transfer, processing, and commit, and does not show 100% before metadata commit.
- Worker upload endpoints require cluster authentication and no worker credential or internal upload URL is returned to browsers.
- Workspace fork creates an independent workspace from the selected snapshot.
- Group-shared workspace is visible to group members.
- Snapshot comparison detects added, modified, and deleted files.
- Repeated checkpoints retain current plus at most one previous distinct content version independently for each file path.
- Deleting a file retains its last content as the previous version; deleting the workspace collects that blob unless another workspace references it.
- Ordinary workspace deletion collects exclusive blobs without deleting content shared by another workspace or fork.
- Fresh storage initializes SQLite metadata, while populated legacy `metadata.json` storage fails safely with no mutation.
- A worker failure during a streamed upload releases its compute reservation immediately, preserves resumable offsets, and returns a retryable service-unavailable response after consuming the request body.
- Concurrent chunks for different files can stream into one upload without blocking behind a process-wide worker upload lock.

## Chat Lifecycle

- Start creates a session, a run, and a container assignment.
- Fork creates a distinct persisted session, copies stable history without duplicating usage, and uses an independent native Codex branch.
- Forking an active session branches from its pre-run checkpoint and excludes the current prompt and partial output.
- Forked chat messages, event cursors, and title edits do not mutate the source session.
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
- Chat deletion is limited to the workspace owner or a system administrator, rejects active runs, permits an empty workspace, and removes terminal run, event, Codex-home, and fork-checkpoint state without deleting independent forks.
- Live event refreshes follow the bottom only when the reader is already there and preserve position while older history is being read.

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

## Recharge

- The recharge endpoint requires authentication and returns only the current user's managed MicuAPI URL and key.
- The frontend offers exactly CNY 50, CNY 100, and CNY 200 choices and resolves them to the expected on-disk QR assets.
- Missing QR assets produce a visible configuration message rather than a broken-image-only state.
- The payment dialog displays the signed-in username and instructs the payer to include it in the payment note.
- Logging out removes the managed API key from in-memory frontend state and the credential fields.
