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

System administrators may batch-create pending accounts for an existing or newly named group. A pending account has an immutable generated user ID and a unique invite token, but no username or password. Invite tokens remain valid until used or revoked and are visible only through system-admin account APIs.

Registration requires an invite token, a globally unique case-insensitive username, and a password of at least eight characters. Successful registration preserves the pending account's user ID, group, budget, and session limit, consumes the token, stores the password hash, activates the account, and creates a login session.

Named groups are persisted independently from accounts. Each group may have a nullable logical workspace disk limit; `null` means unlimited. Group-level token budget enforcement is not part of the current implementation; each invited account receives the per-user token budget selected for its batch.

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

The current runtime persists the input, cached-input, output, and total token counts reported by Codex's terminal `turn.completed` event. Total charged usage is input plus output tokens because cached-input tokens are already included in the input count. Run totals roll up into the chat session and the owning user's persisted `usedTokens`; API reads do not recalculate historical usage.

## Enforcement

The system uses hard budget limits:

- Before a run starts, the backend checks the user and group budget.
- While a run is active, streamed usage updates decrement the remaining budget.
- If either the user or group budget is exhausted, the backend requests graceful stop of the active run.
- Exhausted users or groups cannot start or resume runs until an admin increases budget.

Budget checks belong in the backend control plane. Workers may report usage but should not be the source of truth for authorization.

## Admin Workflows

System admins can:

- Create users and persistent named groups individually or in invitation batches.
- View and revoke unused invitation tokens.
- Set and increase token budgets.
- Disable or re-enable users and groups.
- View usage summaries by user, group, workspace, model, and date range.
- Inspect budget stop events and failed run records.
- Reset a user's consumed-token counter while replacing the user's allowance.
- Set or clear a group's logical workspace disk limit.
- Permanently delete users or groups and their owned workspaces and chat data.

All admin actions should be written to the audit log.

Permanent deletion protects the signed-in administrator and must leave at least one system administrator. Active runs are stopped and checkpointed before deletion; if they do not reach a terminal state within the bounded shutdown period, deletion is rejected without removing the account. Security audit entries are retained after deletion.
