# Identity and Budget

## Account Model

The first version uses internal accounts with username and password login. Administrators create users and groups, including batch creation for classroom, project, or department onboarding.

Users may belong to one group. Groups are used for shared workspace access, shared budget policy, and admin reporting.

## Roles

The system should support these roles:

- User: can create workspaces, use chat, access permitted shared workspaces, fork workspaces, and download permitted artifacts.
- Group admin: can inspect group usage and manage group workspace access if enabled.
- System admin: can create users and groups, set budgets, disable accounts or groups, inspect usage, and view audit logs.

## Authentication

Login uses username and password. Passwords must be stored with a modern password hashing algorithm. Backend sessions should be short enough to limit risk and long enough for normal audit tasks.

The initial design does not require SSO, device binding, or multi-factor authentication.

## Account Sharing Controls

The system discourages one person sharing an account with others through:

- A configurable concurrent-session limit per account.
- Logging IP address, user agent, and device/session identifiers.
- Admin-visible suspicious usage reports.
- Optional automatic blocking when concurrent session limits are exceeded.

This is not strong identity proof. It is an operational control suitable for v1.

## Budget Model

Budgets are token allowances assigned to users and groups. A run consumes budget from the user and may also count against the group.

Token usage should be recorded by:

- User.
- Group.
- Workspace.
- Chat session.
- Run.
- Model.
- Time range.

## Enforcement

The system uses hard budget limits:

- Before a run starts, the backend checks the user and group budget.
- While a run is active, streamed usage updates decrement the remaining budget.
- If either the user or group budget is exhausted, the backend requests graceful stop of the active run.
- Exhausted users or groups cannot start or resume runs until an admin increases budget.

Budget checks belong in the backend control plane. Workers may report usage but should not be the source of truth for authorization.

## Admin Workflows

System admins can:

- Create users and groups individually or in batch.
- Set and increase token budgets.
- Disable or re-enable users and groups.
- View usage summaries by user, group, workspace, model, and date range.
- Inspect budget stop events and failed run records.

All admin actions should be written to the audit log.
