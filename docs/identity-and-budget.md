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

Registration requires an invite token, a globally unique case-insensitive username, and a password of at least eight characters. Before consuming the invitation, the backend idempotently creates or reuses a MicuAPI token named exactly with the chosen username and retrieves its generated key. Successful registration preserves the group, initial CNY balance, and session limit; provisioning failure leaves the invitation valid.

Startup reconciliation also verifies existing bindings. A stale local `tokenName` is synchronized with the username, and legacy tokens still named by generated user ID are renamed to the username when no conflicting MicuAPI token exists. Conflicts fail closed without switching or deleting either token.

Named groups are persisted independently from accounts. Each group may have a nullable logical workspace disk limit; `null` means unlimited. Each group also has a positive concurrent live-run limit, defaulting to one for new and migrated groups. Group-level token budget enforcement is not part of the current implementation; each invited account receives the per-user token budget selected for its batch.

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

The current runtime persists the input, cached-input, output, and total token counts reported by Codex's terminal `turn.completed` event. Total reported usage is input plus output tokens because cached-input tokens are already included in the input count. Run totals roll up into the chat session and the owning user's cumulative `usedTokens`. This counter is reporting-only and is never used to authorize a run.

## Enforcement

Managed MicuAPI mode uses the external token balance as its hard limit:

- Before every run, the backend fetches the corresponding MicuAPI token's current balance. Zero balance rejects the run and provider failure fails closed.
- MicuAPI deducts model cost and enforces exhaustion while the run is active.
- Administrators add a CNY amount to the token's existing balance; this does not reset cumulative local token usage.
- Users may select custom-provider mode and supply their own URL and key. Custom mode does not use or gate on the preserved MicuAPI balance.

Regular user responses expose only the remaining-budget percentage, calculated as remaining quota divided by remaining plus consumed quota. Exact CNY balances are restricted to system-administrator account responses. Cumulative token counts remain visible to users as usage reporting.

The user sidebar combines these values in one readable label, for example `剩余 10%，平台token总计使用 1.9M`. Percentages and compact token values use at most one decimal place and omit a trailing `.0`; token totals use decimal `K` and `M` units. Custom-provider and unavailable-balance states replace the percentage text but continue to show cumulative platform token usage.

Budget checks belong in the backend control plane. Workers may report usage but should not be the source of truth for authorization.

## Admin Workflows

System admins can:

- Create users and persistent named groups individually or in invitation batches.
- View and revoke unused invitation tokens.
- Set an initial MicuAPI CNY balance and add funds to an existing balance.
- Disable or re-enable users and groups.
- View usage summaries by user, group, workspace, model, and date range.
- Inspect budget stop events and failed run records.
- Inspect cumulative reported tokens separately from the authoritative MicuAPI balance.
- Set or clear a group's logical workspace disk limit.
- Set a group's concurrent live-run limit.
- Permanently delete users or groups and their owned workspaces and chat data.

All admin actions should be written to the audit log.

Permanent deletion protects the signed-in administrator and must leave at least one system administrator. Active runs are stopped and checkpointed before deletion; if they do not reach a terminal state within the bounded shutdown period, deletion is rejected without removing the account. Security audit entries are retained after deletion.
