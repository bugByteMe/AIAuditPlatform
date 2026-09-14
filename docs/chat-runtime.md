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
3. Scheduler assigns the run to a healthy worker with capacity.
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

## Completion

When a run completes:

1. Worker reports completion.
2. Backend records final usage.
3. Backend creates a final workspace snapshot.
4. Backend compares snapshots and records artifacts.
5. Frontend updates the chat and artifact views.

## Failure Handling

Failures should preserve as much recoverable state as possible:

- If the worker reports an error, persist the error event.
- If the container exits unexpectedly, mark the run failed and checkpoint the workspace if possible.
- If the worker disappears, mark active runs as uncertain until heartbeat timeout handling resolves them.
- If budget is exhausted, stop the run and record the stop reason as budget-related.
