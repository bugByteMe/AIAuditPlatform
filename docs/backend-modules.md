# Backend Module Boundaries

## Purpose

The backend is organized around small, behavior-focused Python modules. Files
should target roughly 400 physical lines and must remain at or below 500 lines.
The automated module-size test applies this limit to production and test code.

## Dependency Direction

Compatibility façade modules retain the established imports used by entry
points and integrations:

- `server.py` exposes the FastAPI application, request handler, and executable
  entry point.
- `chat_runtime.py`, `workspace_store.py`, and `upload_store.py` expose the
  existing public runtime and store classes.

Implementation modules sit behind those façades:

- Server modules separate application state, HTTP transport and ASGI streaming,
  identity, administration, recharge, and workspace/chat endpoints.
- Chat modules separate Codex container execution, session lifecycle, and run
  execution/event handling.
- Workspace modules separate common validation, metadata access, lifecycle
  mutations, snapshots/artifacts, and file delivery.
- Upload modules separate compute-worker staging from control-plane upload
  session coordination.
- Account modules separate account/group/invitation persistence from recharge
  order and payment persistence.

Domain modules may depend on persistence adapters and shared validation
utilities. Transport modules may call domain façades. Domain and persistence
modules must not depend on HTTP handlers or ASGI response types.

## Compatibility and State

The refactor does not change HTTP routes, event payloads, database schemas,
filesystem layouts, worker protocol, or configuration keys. Thin façades
re-export established classes and helpers. The server façade also forwards
legacy test-time dependency patches to their owning modules while new code
should patch or inject the owning module directly.

Shared locks, database handles, worker registries, and runtime callbacks remain
single instances assembled by the server state module. Splitting a service must
not duplicate those resources or weaken existing transaction and lock
boundaries.

## Tests

Tests are grouped by behavior rather than by façade file. Shared fixtures live
in support modules that are not themselves discovered as test cases. Changes
to permissions, budgets, workspace snapshots, artifact diffs, uploads, or chat
lifecycle must continue to receive focused regression coverage.
