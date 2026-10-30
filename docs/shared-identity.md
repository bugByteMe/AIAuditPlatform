# Shared OIDC identity

Shared identity is opt-in: `AI_AUDIT_AUTH_MODE=local` remains the default.
Rebuild the control-plane image to install the new dependency. Deploying Keycloak
alone does not change login; this change performs no production migration.

## User journeys

| Journey | Result |
| --- | --- |
| Existing account, current platform | Keycloak verifies the existing password; an explicit issuer/subject link loads the same local ID, group, role, resources, and Micu binding. |
| Existing identity, new platform | A matching customer on dedicated (any enabled registered customer on main) gets an ordinary local profile in the configured default group, or the first-created existing group when no default group ID is configured. No invitation or credit grant. |
| Invitation registration | Existing form reserves the invitation, centrally provisions identity/Micu, activates the invitation's local group and limits, then redirects through OIDC login. Registration itself issues no session. |
| Existing identity with invitation | Enter the code and choose **Already registered? Sign in**. A new local profile receives its group/limits; an existing profile is unchanged and does not consume the invitation. |

Keycloak manages credentials and one admin-assigned `customerId`. Each platform
manages groups, roles, sessions, and resources. Customer admission has no admin
bypass. Local disabling/deletion never revokes central identity or shared keys.
Local account/group deletion removes identity links atomically with profiles,
allowing later ordinary-user readmission in the resolved default group or an
invitation's group. Disable rather than delete to block returning users.

New usernames are stripped/case-folded and globally unique. Migration preserves
old platform/Micu spelling using the admin-only `platformUsername` attribute,
even when Keycloak normalizes login case. Do not edit linked usernames, this
attribute, or customer assignments outside a reviewed migration.

The built-in seed profiles are an explicit exception to ordinary customer
membership. Their existing Keycloak identities must have `customerId=universal`.
Every OIDC-enabled platform reconciles those local profiles and issuer/subject
links before serving requests; it never creates or resets the Keycloak accounts.
A dedicated platform admits its own customer plus `universal`, while workspace
owners must still belong to one of those two scopes. The `universal` customer is
reserved: it is hidden from customer selection and cannot be used for invitations,
reassignment, or deletion. Startup fails closed if a central identity is missing,
disabled, assigned elsewhere, or conflicts with an existing local identity link.

## Configuration

Identity settings are environment-only. Keep completed files outside Git.

| Variable (`AI_AUDIT_` prefix) | Purpose |
| --- | --- |
| `AUTH_MODE` | `local` (default) or `oidc`. |
| `OIDC_ISSUER` | Stable HTTPS realm issuer. |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | Separate confidential login client for each platform. |
| `PUBLIC_URL` | Platform HTTPS origin, no path. |
| `OIDC_DEFAULT_GROUP_ID` | Optional existing local group for automatic admission. If unset, startup selects the first-created existing group (group ID breaks timestamp ties). |
| `DEPLOYMENT_CUSTOMER_ID` | Empty on main; fixed business customer on dedicated. |
| `OIDC_REVALIDATE_SECONDS` | Default 300; allowed range 30–900. |
| `REGISTRATION_PAUSED` | `true` pauses registration for maintenance/migration. |
| `KEYCLOAK_ADMIN_CLIENT_ID`, `KEYCLOAK_ADMIN_CLIENT_SECRET` | Main only: separate service account with user query/view/manage access, not realm-admin. |
| `IDENTITY_CENTRAL_URL`, `IDENTITY_CENTRAL_TOKEN` | Dedicated only: main's trusted HTTPS origin and per-instance service token. |
| `IDENTITY_CLIENTS_JSON` | Main only: scoped service-token directory below. |

Configure Keycloak following [deployment](keycloak-deployment.md):

1. Disable self-registration and username editing. Keep `customerId`,
   `platformUsername`, and `platformProvisioningId` administrator-only editable
   user-profile attributes. Do not let ordinary account-console users write them.
2. Map `customerId` to a single string in ID tokens **and UserInfo**, in default
   client scopes. The other two attributes are internal, not user claims.
   Optionally expose an editable `displayName` user-profile attribute as the
   `display_name` string claim in ID tokens and UserInfo. It is presentation-only
   and never replaces the immutable username or subject for authorization.
3. Use authorization code, mandatory PKCE S256, asymmetric signing (RS256 by
   default), and disable implicit/password grants. Register the exact callback
   `https://<platform-host>/api/oidc/callback` and exact HTTPS web origin.
4. Configure password policy centrally. Do not require password changes for
   imported users. Do not mandate email/first/last-name fields unless the
   username/password onboarding form is extended.

Create at least one group and an initial local administrator before switching a
new installation to OIDC; migrate that administrator explicitly. Set a default
group ID to select it explicitly, or leave it unset to use the first-created
existing group. Automatic admission never grants administrator permissions.

Dedicated instances use their own Micu management credentials for direct balance
and pre-run checks. Application guards prevent dedicated recharge/key-lifecycle
mutations, but cannot make a broad provider credential read-only. Protect those
hosts. Central Keycloak service-account credentials must exist on main only.

Each control-plane process keeps one pooled HTTP client for OIDC, central
identity, and Keycloak administration traffic. The main platform also caches its
Keycloak service-account access token until shortly before expiry, so concurrent
administration checks share one token request. A Keycloak HTTP 401 invalidates
that process-local token and retries the administration call once. Multiple
server workers have independent connection pools and token caches.

## Customers and invitations

Management roles use `GET /api/customers`; main also accepts
`POST /api/customers` with `{id, name}`. IDs are immutable lowercase letters,
digits, underscores/hyphens, up to 64 characters. `public` is reserved for
non-business users and created automatically; `universal` is reserved for the
built-in platform seed identities; groups are independent.

In OIDC mode, system/platform admins issue batches and revoke unused invitations.
`POST /api/accounts/batch` adds `customerId`. Main administrators choose a
registered customer; dedicated instances force their own ID and reject overrides.
The batch dialog provides customer selection and main-only customer creation.
Group admins retain their existing lack of invitation-creation access. Local-mode
privileges remain unchanged.

Public registration still uses `inviteToken`, `username`, and `password`.
Injected customer/group/role fields never override the invitation. Success in
OIDC mode returns `{"redirectUrl":"/api/oidc/login"}`, not a bearer token or
password hash. Passwords exist transiently in memory and outbound HTTPS only.

Reservations persist as `provisioning`, bound to username. Retry the same request
after interruption; settings/revocation cannot force-cancel this state. The
central journal records subject and idempotency result. Exact-name Micu reuse
does not add initial credit twice. Authenticated login can finish interrupted
local activation. A confirmed username conflict releases an unused reservation
so the user can choose another name. An unrelated existing central username is
never claimed or password-reset; its owner must use OIDC. Review unfinished reservations before
reuse; do not delete central identities to roll them back.

Dedicated servers use authenticated main-platform endpoints:

- `POST /api/internal/identity/provision`: fixed customer, operation ID, and
  configured per-client initial-budget ceiling.
- `POST /api/internal/identity/binding`: enabled, customer-matching subject's
  existing Micu binding; universal bindings are accepted for dedicated clients.
- `POST /api/internal/identity/universal-seed`: resolves only a configured seed
  username, verifies its existing Keycloak assignment, and idempotently ensures
  the central Micu binding. It cannot create a Keycloak identity.

There is **no balance-proxy endpoint**. Dedicated recharge orders/imports,
reset-budget requests, callback reconciliation, and key mutations are blocked.
Recharge displays credentials/usage and directs payment to main.

`AI_AUDIT_IDENTITY_CLIENTS_JSON` maps stable instance IDs to objects containing
`customerId`, unique random `token` (at least 32 characters), and
`maxInitialBudgetCny` (default zero). Put real values in a protected environment
file. Retain instance IDs across redeploys because they namespace provisioning
operations. Dedicated tokens never authorize another customer or arbitrary IdP
administration. Tokens and passwords must not be captured by proxy/APM logs.

Keep internal APIs private/firewalled with HTTPS and per-instance tokens. The
Nginx example denies them until explicit private source IPs are allowed. It also
disables access logs for invitation/OIDC URLs. Use `backend/server.py` or launch
Uvicorn with `--no-access-log` to keep callback codes out of server access logs.

Provider tokens stay server-side; app sessions use Secure, HttpOnly cookies and
same-origin frontend/backend deployment. Identity/customer status is rechecked
on the next request after the interval, failing closed on provider failure.
Local disabling takes effect on the next request. Open streams/runs are not
forcibly stopped by polling. Local logout revokes its refresh token but leaves
Keycloak browser SSO available to other platforms. As before, run one control
process per platform: sessions, flows, rate limits, and profile caches are local
to that process.

## Offline migration

### Account deletion and customer administration

Normal account deletion (including group-cascade deletion) removes the local
profile and identity link in one database transaction. It does not delete the
Keycloak identity or Micu key. A later valid login can create a fresh ordinary
profile; old roles, chats, and workspaces are not restored. Local disabling
continues to block login. Use disabling when removal must prevent readmission.

On main, system administrators can choose **Full deletion** beside local deletion.
`POST /api/accounts/{id}/full-delete` requires `{confirmSubject}` matching the
linked Keycloak subject. Self/last-administrator protections still apply. It
disables the local profile and invalidates sessions before deleting the Keycloak
identity, then performs normal local cleanup. Remote deletion treats 404 as an
already-completed step so retry can finish after partial failure. Failures leave
the local profile disabled. Dedicated platforms cannot call this operation.
Other platforms' data and Micu keys are not deleted. The central Micu binding
remains and its username is reserved against new provisioning to avoid handing
an old key to a different identity. Reusing that username requires a separately
reviewed provider/binding cleanup, not merely deleting the Keycloak user.

Main system/platform administrators can change a linked active user's customer
through the account-settings customer control:
`POST /api/accounts/{id}/customer` with `{customerId}`. The destination must be an
enabled central customer. The operation updates Keycloak attributes, the central
Micu binding metadata, and the main local profile without changing groups or
roles. Pending invitations remain immutable. Main sessions are invalidated;
other platforms reject changed membership at their next identity revalidation.
Their local profiles/resources are not reassigned automatically. If a remote
update succeeds but local persistence fails, retry the same customer assignment;
admission fails closed until the central binding/profile is consistent.

For a legacy stale local link, stop the relevant platform and back up its DB.
In PostgreSQL inspect the exact issuer/subject and missing profile first:

```sql
SELECT l.issuer, l.subject, l.user_id
FROM identity_local_links l
LEFT JOIN account_users u ON u.id = l.user_id
WHERE u.id IS NULL;
```

Delete only the reviewed link, retaining the live-profile safeguard:

```sql
DELETE FROM identity_local_links AS l
WHERE l.issuer = 'https://login.example.com/realms/ai-audit'
  AND l.subject = '<reviewed-keycloak-user-id>'
  AND NOT EXISTS (SELECT 1 FROM account_users u WHERE u.id = l.user_id)
RETURNING issuer, subject, user_id;
```

Restart the platform afterward. This permits fresh readmission, not recovery of
deleted data. Do not delete central bindings or use an unfiltered links deletion.

### Migration procedure

1. Back up complete application and Keycloak databases. Audit **all instances**
   for case-insensitive username collisions and shared Micu IDs; resolve different
   people sharing names/keys before allowing new central provisioning.
2. Prepare private JSON outside Git. `users` must include every local account as
   `{localUserId, customerId}`; add `subject` only for an explicitly reviewed link
   to an existing central identity. Optional `customers` are `{id, name}`.
   `invitations` maps each legacy pending invite lacking a customer as
   `{invitationId, customerId}`. Do not infer membership from groups/email domains.
   For a main-platform migration where every account is explicitly approved as
   non-business (`public`), generate the file using the existing deployment's
   database/storage environment and installed backend dependencies:

   ```bash
   python backend/generate_oidc_mapping.py --output /secure/identity-map.json
   ```

   The parent directory must already exist. The generator only reads existing
   account tables, includes disabled/admin accounts and pending invitations,
   preserves existing public identity subjects, and writes a new mode-0600 file.
   It exports IDs and customer assignments only, never credentials or invitation
   tokens. It refuses empty account data, existing output paths, dedicated
   deployments, and existing non-public assignments rather than overwriting
   reviewed customer membership. It does not contact Keycloak, verify migration
   readiness, or run migration. Review its output and run preflight below.
3. Stop app processes and pause registration/password changes. Set
   `AI_AUDIT_REGISTRATION_PAUSED=true`, source storage/database configuration, and
   protected identity credentials for the offline tool.
4. Run `python backend/migrate_oidc_accounts.py --mapping /secure/identity-map.json`
   for local preflight. Review, then repeat with `--apply`. Preflight does not
   check remote Keycloak connectivity. Apply adds identity SQL tables and imports
   PBKDF2-SHA256 credentials, converting salt/hash hex bytes to Base64 without
   changing iterations or needing plaintext passwords.
5. Migrate main first. Dedicated migration requires explicit central subjects
   and matching existing Micu token IDs; it never imports/resets central
   passwords. Local IDs, roles, groups, ownership, and usage stay unchanged;
   linked profiles lose their local password hashes.
6. Partial migrations can rerun with the same mapping. Operation markers identify
   imported users; unrelated central identities require explicit subject maps.
   Rollback requires reviewed database backups, not just an application downgrade.
7. Verify synthetic credentials against the deployed Keycloak image (including
   Unicode/long passwords), then a pilot migrated user. Enable OIDC, restart,
   verify isolation and direct Micu balances, and unpause registration. Keycloak
   can upgrade password hashes transparently after successful login.

Passwords and resources remain the same, but expect one sign-in after cutover;
legacy app sessions have no OIDC refresh tokens. Do not switch migrated data
back to local mode without restoring its backup. Never commit customer mappings,
credential exports, or real customer data. CI tests cover admission, linking,
reservation/retry races, token validation, hash conversion, and provider
boundaries; real Keycloak login/restore drills remain deployment gates.
