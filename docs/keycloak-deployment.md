# Keycloak deployment

This standalone deployment runs central identity year-round, independently of
the main platform and seasonal customer instances. It does **not** enable OIDC
login in AI Audit by itself. For opt-in application integration, migration,
invitation registration, and customer admission, see
[Shared OIDC identity](shared-identity.md). Local login remains the default.

## Topology and prerequisites

- One Linux host with Docker Engine, Docker Compose v2, and host-installed Nginx.
- A permanent DNS name, such as `login.example.com`, and a valid TLS certificate.
- Allow capacity for Keycloak (2 GiB container limit), PostgreSQL (512 MiB
  container limit), Docker, Nginx, and the host OS; these are not load-tested
  sizing guarantees. This is a single-host deployment, not high availability.
- Keycloak 26.8.1 is built in production/optimized mode. PostgreSQL uses the 17
  image series and its own named volume; it must not use AI Audit's database.
- Only host Nginx can reach Keycloak's loopback-published HTTP port `18080`.
  The management port `19000` is also loopback-only and must not be proxied
  publicly. PostgreSQL has no published port and uses an internal network.

Follow the official [container](https://www.keycloak.org/server/containers) and
[reverse-proxy](https://www.keycloak.org/server/reverseproxy) guidance when
changing this topology. In particular, the example assumes Nginx is the direct
edge proxy and overwrites forwarded headers. A CDN or another proxy in front
requires its own carefully restricted real-IP/trusted-proxy configuration.

## Configure and start

Run from the repository root on the deployment host:

```bash
sudo install -d -m 700 /etc/ai-audit
sudo install -m 600 docker/keycloak/deployment.env.example /etc/ai-audit/keycloak.env
sudoedit /etc/ai-audit/keycloak.env
```

Set the public HTTPS origin and two independent, high-entropy passwords using a
password manager or `openssl rand -hex 32`. The empty template passwords cause
Compose to refuse startup. Keep the completed environment file outside source
control. Container environment values are visible to Docker administrators;
restrict host/Docker access and protect backups accordingly.

```bash
sudo docker compose --env-file /etc/ai-audit/keycloak.env -f docker/keycloak/compose.yaml config --quiet
sudo docker compose --env-file /etc/ai-audit/keycloak.env -f docker/keycloak/compose.yaml up -d --build
sudo docker compose --env-file /etc/ai-audit/keycloak.env -f docker/keycloak/compose.yaml ps
curl --fail http://127.0.0.1:19000/health/ready
```

Use `config --quiet`, not plain `config`, to avoid printing interpolated secrets.
Copy `docker/nginx/keycloak.conf.example` into the host's Nginx sites directory,
replace its hostname and certificate paths to match `KEYCLOAK_PUBLIC_URL`, and
enable the site using the host's normal configuration workflow. Then validate
and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

Open `https://login.example.com/admin/` and sign in with the bootstrap account.
Create a permanent administrator with MFA, verify it in a separate session,
then remove the temporary bootstrap administrator. The bootstrap environment
variables initialize an empty database; changing them does not reset an
existing administrator password. Keep the env file protected even afterwards.

The public origin must remain stable because it becomes part of the OIDC issuer.
HTTP behind Nginx is deliberate; public browser traffic must use HTTPS.

## Initial identity configuration

Use `master` for administration only. Create one shared realm, for example
`ai-audit`, for all business customers; a separate realm per customer would not
enforce a global username namespace across customers.

Before adding production users:

1. Leave self-registration and username editing disabled. Globally unique,
   canonical, immutable usernames are required to preserve username-based Micu
   token naming. Resolve cross-instance collisions during a separate reviewed
   account migration; do not infer identity from a matching name alone.
2. In the realm's user-profile configuration, define a single-valued `customerId`
   attribute with administrator-only edit permission. Use stable customer IDs,
   not editable group names. Populate it before allowing application access.
3. Create a separate confidential
   OIDC client for the main platform and each dedicated instance. Use Standard
   Flow (authorization code), require PKCE S256, disable Implicit Flow and Direct
   Access Grants, and register the exact platform callback
   `https://<platform-host>/api/oidc/callback` and HTTPS web origin. Never use
   wildcard redirects.
4. Attach a user-attribute protocol mapper to those clients: map `customerId` to
   a single string claim of the same name in the ID token and UserInfo response.
   It is an application-specific claim, not a built-in tenancy boundary.

No realm import, sample users, or client secrets are shipped. Configure the
additional admin-only provisioning attributes and service account described
in the application integration guide before enabling OIDC.

After creating the realm, check discovery:

```bash
curl --fail https://login.example.com/realms/ai-audit/.well-known/openid-configuration
```

The issuer must be `https://login.example.com/realms/ai-audit`, never localhost or
an HTTP address. Verify readiness, admin access, and discovery on the deployment
host; no containers or integration tests were started on the local development
machine for this change.

## Backup, upgrades, and seasonal shutdown

Back up the Keycloak PostgreSQL database independently of workspace storage:

```bash
sudo docker compose --env-file /etc/ai-audit/keycloak.env -f docker/keycloak/compose.yaml exec -T postgres pg_dump -U keycloak -d keycloak -Fc > /secure/backup/keycloak.dump
```

Choose a real protected backup directory, restrict the dump's permissions,
encrypt it off-host, and test restoration on an isolated host. The dump contains
sensitive identity data and client secrets. Back up the protected environment
file and Nginx configuration too. Realm exports are not a substitute for a full
database backup.

Before upgrading, review upstream release/security notes, back up PostgreSQL,
and test the upgrade on a restored copy. Change both Keycloak image tags in the
Dockerfile and the local image label together. The PostgreSQL 17 tag follows its
minor releases; use a reviewed digest if deployment reproducibility requires it.
Database migrations can prevent rolling back just the container image.

For a maintenance stop:

```bash
sudo docker compose --env-file /etc/ai-audit/keycloak.env -f docker/keycloak/compose.yaml stop
```

Stopping preserves the volume. Never use `down -v` unless intentionally deleting
the identity database. Keep this identity deployment running when a seasonal
platform stops; once OIDC is integrated, all platforms depend on it for new login.
