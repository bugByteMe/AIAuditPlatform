# Interactive user input

The runner's built-in MCP server `ai_audit_input` exposes `user_input_tool` in
every Codex run. One call asks one question and waits for the run initiator to
answer or skip it in the chat history. Other authorized workspace users can view
the card. Stopping the run cancels outstanding questions.

## Tool contract

```json
{
  "type": "multiple_choice",
  "title": "Which periods should be compared?",
  "text": "Choose the periods for the **comparison**.",
  "images": ["/workspace/workdir/comparison.png"],
  "multiple": true,
  "options": [
    {"id": "current", "label": "Current year"},
    {"id": "prior", "label": "Prior year"}
  ]
}
```

`type` is `yes_no`, `multiple_choice`, or `text`. `title` is required; `text`
and `images` are optional. `multiple` defaults to false and, like `options`,
applies only to multiple-choice questions. Options require 2–20 unique IDs.
Combined title/body text is limited to 16 KiB of UTF-8; text answers have the
same limit and cannot be blank. Option IDs and labels are nonempty, each bounded
to 16 KiB. Image paths can be workspace-relative or begin with `/workspace/`.

The result is a JSON text MCP result:

```json
{"requestId":"<request-id>","status":"answered","value":["current","prior"]}
```

Yes/no returns a boolean (including false), choices return an array of IDs in
option order, and text returns a string. Skip returns
`{"requestId":"<request-id>","status":"skipped"}` without a value. Invalid
image paths or image content produce an error result so Codex can correct the
question. User image uploads and grouped questions are not supported.

## Execution and persistence

- The stdio MCP server is installed under `/opt/ai-audit` in the runner image.
  Its reserved configuration is injected independently of capability selections;
  external MCP configuration cannot redefine `ai_audit_input`.
- Only the current run's `input_bridge/<run-id>` directory is mounted at
  `/run/ai-audit-input`. Requests and responses are published with atomic file
  replacement. No platform or worker token is given to the MCP server.
- The runner monitors stdout, stderr, mailbox requests, and execution timeout
  concurrently. Time with one or more outstanding questions does not consume
  the execution timeout. CPU/memory reservations, worker leases, workspace
  locks, and ordinary chat lifecycle restrictions remain active while waiting.
- The built-in MCP timeout is `2147483647` seconds, providing no practical
  response deadline. Parallel calls have independent request IDs and cards.
- `chat_input_requests` is created additively by normal chat-store initialization
  on SQLite/PostgreSQL. Request publication and its runner event commit together.
  Answers commit before delivery; a durable outbox retries delivery locally or
  through the existing authenticated worker connection. The first answer wins;
  identical retries are idempotent while the run is accepting input.
- Requests/resolutions appear as `user_input_requested` and
  `user_input_resolved` events with an `inputRequest` payload. Historical event
  tuples carry it in optional metadata at index 7. Existing event tuple shapes
  remain unchanged for other events. Sessions expose `pendingInputCount` and
  `waitingForInput` without adding a new run lifecycle status.
  The browser explicitly subscribes to both named SSE event types so cards and
  resolved answers update without reloading the page.
- Stop and terminal finalization cancel unanswered requests. Browser reloads do
  not interrupt execution. Existing remote-run recovery and worker lease rules
  apply after control-plane restart. Local-run interruption or worker restart
  fails the run; pending questions are cancelled by finalization.

## Website API and images

All routes require the existing website authentication and workspace access:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/workspaces/{workspaceId}/chat/input-requests?sessionId={sessionId}` | Restore canonical request state |
| POST | `/api/workspaces/{workspaceId}/chat/input-requests/{requestId}?sessionId={sessionId}` | Submit `{"status":"answered","value":...}` or `{"status":"skipped"}` |
| GET | `/api/workspaces/{workspaceId}/chat/input-requests/{requestId}?sessionId={sessionId}&image={index}` | Read a question image |

Submission additionally requires the run initiator's username, matching session
and workspace identity, and an active accepting run. Conflicting answers and
closed runs return HTTP 409. A successful submission means the answer is saved;
delivery retries continue independently of the browser.

The worker adds `POST /v1/runs/{runId}/input/{requestId}`, protected by the
existing worker authentication, and advertises `userInputProtocol: 1` in health
responses. Interactive runs are scheduled only onto compatible workers.

Images are restricted to PNG, JPEG, and WebP, at most four per question and
5 MiB each. The runner checks signatures and opens workspace paths with
directory-relative, no-follow reads, rejecting symlinks and traversal. Image
bytes are copied into the existing content-addressed blob store before the
question is published. Authorized image routes use those immutable references;
model-provided URLs and host paths are never used as image fetch targets.

## Rollout and validation

Rebuild the runner image and coordinate backend/worker deployment with that
image. Older images do not contain the required MCP server and must not be used
with the updated backend. Older workers do not advertise the required protocol.
The feature is automatically available to new runs after this coordinated
upgrade. No production configuration or running service is changed by the code
change itself.

Focused backend tests: `test_user_input`, `test_input_runtime`, `test_input_api`,
`test_codex_mcp_config`, `test_compute_nodes`, `test_worker_agent`, and
`test_capabilities`. Frontend tests include `userInput.test.mjs`,
`chatEvents.test.mjs`, and `tests/user-input.browser.mjs`. The browser test accepts
`PLAYWRIGHT_MODULE` and `CHROMIUM_EXECUTABLE` to use installed dependencies.

`backend/smoke_user_input_codex.py` runs inside the rebuilt runner image with an
isolated Codex home and synthetic Responses provider. It verifies real Codex
configuration parsing, MCP discovery, waiting, and answer continuation without
using credentials or making a paid model request. Website request persistence
and response delivery are exercised separately by the runtime tests.

`backend/smoke_user_input_platform.py` combines the real Docker runner and Codex
with the persisted website request API and durable response delivery. It needs
an isolated control-plane test container with the Docker CLI/socket, host
networking for its loopback synthetic provider, and a fresh `SMOKE_ROOT` bind
mounted at the same absolute path on the Docker host. `SMOKE_IMAGE` selects the
rebuilt runner image. All identities, questions, answers, and provider responses
are synthetic; it does not use the running production website or its database.
