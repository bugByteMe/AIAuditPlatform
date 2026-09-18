# Chat Runtime

## Session Model

A chat session is the user-facing conversation attached to a workspace. A session may have multiple runs when it is stopped and later resumed.

A run is one execution attempt inside one Codex Docker container.

## Lifecycle

Chat sessions and runs use these states:

- `queued`: waiting for budget approval, workspace lock, or worker capacity.
- `running`: assigned to a worker and executing in a container.
- `stopping`: user, admin, budget, or system requested graceful termination.
- `stopped`: execution ended before task completion.
- `resumable`: transcript and workspace checkpoint are available for a future run.
- `completed`: task finished normally.
- `failed`: run ended unexpectedly or cannot be recovered.

## Starting a Chat

When a user starts a chat:

1. Backend checks authentication, workspace permission, workspace lock, and budget.
2. Backend creates a chat session and run record.
3. Scheduler reserves CPU and memory on the least-utilized compatible healthy worker (stable configuration order breaks ties).
4. Worker launches a Docker container with the workspace mounted.
5. Worker streams events back to the backend.
6. Backend persists events and fans them out to the frontend.

Users can select model and reasoning effort before starting a run. These settings are stored on the run record.

## Real-Time Events

The frontend receives events through websocket or SSE. Events should include:

- Run queued, started, stopped, completed, and failed.
- Model output.
- Reasoning/progress updates when available.
- Tool call start, output, error, and completion.
- Token usage updates.
- Budget warnings and budget stop events.
- Artifact discovery after run completion.

The backend should persist the event stream so a user can reload the page and recover the visible run history.

## Stopping a Chat

Stopping a chat should be graceful when possible:

1. User or admin requests stop.
2. Backend records the request and sets the run to `stopping`.
3. Backend sends stop command to the worker.
4. Worker asks the containerized Codex process to terminate.
5. Worker captures final events and exit status.
6. Backend records usage and creates a workspace checkpoint.
7. Session becomes `resumable` if checkpointing succeeds.

If graceful stop times out, the worker may force-stop the container and mark the run recoverability based on available transcript and workspace state.

## Resuming a Chat

Resuming does not keep the old container alive. The backend creates a new run using:

- Existing chat transcript.
- Latest workspace checkpoint.
- Previous model settings by default, unless the user changes them.

The scheduler assigns the new run like any other run. The resumed run appends to the same chat session history.

## Forking a Chat

Forking creates a persisted chat session with a new application session ID, copied stable history, and independent future events and titles. The first run uses Codex native fork against the source conversation; later runs resume the fork's new native session. If the source is active, the fork uses the credential-free Codex-state checkpoint captured immediately before the active run and omits that run's prompt and partial output.

Codex-state copies exclude authentication, generated configuration, skills, temporary files, and logs. Copied history does not duplicate charged usage; the fork starts its own token counter at zero.

## Completion

When a run completes:

1. Worker reports completion.
2. Backend records final usage.
3. Backend scans the final workspace state and records a lightweight checkpoint.
4. Backend compares the pre-run current files with the final scan, records artifacts, and retains at most one previous version per path.
5. Frontend updates the chat and artifact views.

## Failure Handling

Failures should preserve as much recoverable state as possible:

- If the worker reports an error, persist the error event.
- If the container exits unexpectedly, mark the run failed and checkpoint the workspace if possible.
- If contact with a worker is lost, stop renewing its run lease, wait for the bounded lease interval, then mark the run failed after attempting a final workspace checkpoint. The run remains resumable when its persisted Codex transcript/checkpoint is usable; it is not automatically retried.
- If budget is exhausted, stop the run and record the stop reason as budget-related.

## V1 Implementation Surface

The prototype backend exposes chat runtime APIs under the workspace boundary:

- `GET /api/workspaces/{workspace_id}/chat/sessions`
- `POST /api/workspaces/{workspace_id}/chat/sessions`
- `POST /api/workspaces/{workspace_id}/chat/sessions/{session_id}/fork`
- `POST /api/workspaces/{workspace_id}/chat/runs`
- `POST /api/workspaces/{workspace_id}/chat/runs/{run_id}/stop`
- `GET /api/workspaces/{workspace_id}/chat/events?sessionId={session_id}&after={event_id}`
- `GET /api/workspaces/{workspace_id}/chat/stream?sessionId={session_id}&after={event_id}`

`PATCH /api/workspaces/{workspace_id}` accepts owner-only `runLockEnabled` changes while the workspace is idle. Workspace responses expose `runLockEnabled` and `activeRunCount`. A run request that overlaps another session while locking is disabled must include `confirmConcurrent: true`; otherwise it receives `concurrent_confirmation_required`. Same-session overlap is always rejected.

The implementation uses the statically configured compute-node inventory whenever that inventory is nonempty. The control-plane scheduler polls enabled workers over authenticated HTTPS, reserves the per-run `run_cpus` and `run_memory`, and queues work until a compatible healthy node has capacity. It uses local Docker with `local_run_capacity` only when no compute nodes are configured; configuring every node as disabled intentionally prevents execution rather than falling back locally. It persists the assigned worker, container, event cursor, chat sessions, runs, and events; holds the operational workspace write lock until every active run is terminal; and records a checkpoint/artifact refresh after completion, stop, or failure. Workspace metadata keeps only workspace-level state and lightweight chat session references.

The worker persists its run state and event stream beneath `<workspace_storage_dir>/worker_state/<node_id>/`. Start is idempotent by run ID, and the control plane can reconnect from its last persisted event cursor after restart. A worker restart marks its interrupted work failed rather than launching a duplicate container. The lease watchdog stops orphaned containers when the control plane disappears. After the control plane has checkpointed a terminal run, it acknowledges the result so the worker removes its duplicate run/event record.

Live updates use Server-Sent Events. A lightweight event-list reconciliation poll runs alongside SSE so proxy buffering, silent connection stalls, or reconnect races cannot leave the visible history stale. Both paths merge by persisted event ID into the session they were opened for, and terminal status is returned even when no new event payload is available. Event rendering follows new output only when the reader is already near the bottom; otherwise it preserves the visible event anchor across refreshes.

The production runner launches one Docker container per run from the `docker/codex-runner/Dockerfile` image and executes:

```text
codex --ask-for-approval never --sandbox danger-full-access exec --json --skip-git-repo-check -C /workspace -m <model> <prompt>
```

For follow-up prompts in an existing chat session, the runner uses Codex native resume:

```text
codex --ask-for-approval never --sandbox danger-full-access exec resume --json --skip-git-repo-check -m <model> <codex_session_id> <prompt>
```

If Codex does not expose a session id in JSON events, the backend falls back to `codex exec resume --last` within that chat session's isolated `CODEX_HOME`.

Each user account stores its own Codex `baseUrl` and API key in account storage. Before every run, the backend generates a writable session-scoped `CODEX_HOME` under `workspace_storage/codex/homes/<username>/<chat_session_id>/` with:

- `config.toml`: model provider, base URL, model, reasoning effort, and trusted `/workspace`.
- `auth.json`: API key authentication for the selected user.

The API key is write-only through the backend API and is never returned in public account/session responses. Run records do not persist Codex credentials or generated `CODEX_HOME` paths.

The chat header shows available/total CPU and memory for the assigned worker. While a run is queued it shows the aggregate capacity of healthy workers. The system-admin view exposes all configured nodes, heartbeat state, reservations, and the same resource meters through `GET /api/workers`.

Docker is not required for unit tests. Tests use a fake runner so lifecycle, lock, budget, and event behavior can be validated on machines where Docker is unavailable or broken.
