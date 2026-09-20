# AI Audit System

## Definition

AI Audit is a private, multi-tenant agentic AI system for financial auditing tasks. Users upload audit workspaces, interact with a Codex-based agent through a web chat interface, monitor execution in real time, and download files changed or produced by the agent.

The system is designed for sensitive enterprise data. It prioritizes workspace isolation, permission checks, token budget enforcement, operational audit logs, and recoverable chat sessions.

## Product Workflow

The primary user workflow is:

1. Log in with an internal account.
2. Create, select, fork, or open a shared workspace.
3. Start a chat session against the workspace.
4. Watch real-time progress, including model output, reasoning/progress events, tool calls, errors, and budget warnings.
5. Stop, resume, or complete the task.
6. Download generated or modified artifacts.

Administrators can create users and groups, reset per-user token budgets, set group disk limits, inspect usage and compute-node health, delete accounts or groups, and review audit logs.

Authenticated users can view their managed MicuAPI credentials and fixed payment options in the Recharge panel. See [recharge.md](recharge.md) for payment-asset and reconciliation details.

## Architecture Overview

The first version targets a single private cluster:

- Main node: runs the web frontend, backend API, authentication, metadata database access, scheduling, websocket/SSE streaming, budget checks, and admin workflows.
- Compute workers: run worker agents that launch isolated Codex Docker containers.
- Shared filesystem: stores uploaded workspaces, content-addressed current/previous file blobs, and artifacts. It is mounted by the main node and every compute worker.
- Metadata database: stores users, groups, budgets, workspaces, chat sessions, runs, artifacts, usage records, and audit logs.

One active chat run maps to one isolated Codex container. The scheduler assigns runs to healthy compute workers with available capacity.

See [architecture.md](architecture.md) for the control-plane and worker design.

## Core Capabilities

### Accounts and Budgets

The system uses internal username/password accounts. Each user can belong to a group. Managed accounts receive a dedicated MicuAPI key and use its CNY balance as the authoritative run budget; users may instead select their own API URL and key. Token counts remain visible as usage reporting. Each group can also have a logical workspace disk limit.

Account sharing is discouraged through concurrent session limits and audit logs for IP/device changes. Strong device binding and SSO are not part of the initial design.

See [identity-and-budget.md](identity-and-budget.md).

### Workspaces and Artifacts

A workspace is a managed directory on the shared filesystem. Users create workspaces by uploading files or folders. Workspaces can be shared with a group, collaboratively accessed, and forked.

Workspace owners can enable an exclusive run lock or allow confirmed runs from different chat sessions to share the live workspace concurrently. Uploads and manual file mutations remain blocked while any run is active.

Generated and modified files are exposed as downloadable artifacts.

See [workspace-and-artifacts.md](workspace-and-artifacts.md).

### Chat Runtime

Each chat session records a transcript, run events, selected model, reasoning effort, token usage, and workspace checkpoint references.

Stopping a chat requests graceful termination of the active container. The backend persists the transcript, usage, event stream, and latest workspace checkpoint. Resuming a chat creates a new container from that checkpoint instead of keeping the original container alive.

See [chat-runtime.md](chat-runtime.md).

Deployment and runtime settings are documented in [configuration.md](configuration.md).

## Design Constraints

- Data is sensitive enterprise audit data.
- Docker is the v1 execution isolation boundary.
- Shared filesystem storage is the v1 workspace and artifact storage layer.
- Kubernetes, cloud object storage, external SSO, and collaborative simultaneous file mutation are outside the v1 scope.
- All access to workspace files, artifacts, and chat histories must pass backend permission checks.

## Validation

The design should be validated through focused tests for permissions, budget enforcement, scheduling, workspace lifecycle, chat lifecycle, artifact detection, and real-time streaming.

See [test-plan.md](test-plan.md).
