# PinPoint hosted backend deployment

This package is intentionally a **single-replica SQLite service**. Do not scale
it horizontally or run separate replicas with separate volumes. Invitation
consumption, quotas, recorder ownership, and session revocation all depend on
one durable ledger.

## 1. Configure secrets and public links

Copy `.env.example` to an untracked `.env`, replace every example value, and
keep the file readable only by the deployment operator. At minimum:

- `PINPOINT_STATE_DB_PATH` stays `/var/lib/pinpoint/pinpoint.sqlite3` in the
  container.
- `PINPOINT_APPLE_AUDIENCE` is the exact hosted-app bundle ID.
- `PINPOINT_APPLE_APP_ID` is `APPLE_TEAM_ID.BUNDLE_ID`.
- `PINPOINT_INVITE_BASE_URL` is the public lowercase HTTPS URL ending exactly
  in `/invite`, for example `https://pinpoint.example.com/invite`.
- Plaud Partner credentials remain only on the backend.

The service refuses to start when only one of the two public-link settings is
present or when the Apple App ID does not match the Apple audience.

## 2. Start one private origin

```bash
cd PublicBackend
docker compose up --build -d
docker compose ps
curl --fail --silent http://127.0.0.1:8787/healthz
```

The image runs as UID/GID `10001`, uses a read-only root filesystem, drops all
Linux capabilities, and mounts the named `pinpoint-state` volume at
`/var/lib/pinpoint`. The health check verifies both SQLite integrity and its
ability to acquire the write lock required by real requests.

The build compiles `cryptography` from source against Debian's OpenSSL and
keeps its Rust/compiler toolchain out of the final image. This avoids depending
on prebuilt ARM64 wheel CPU features that are not available in every
virtualized Apple-silicon Docker environment.

Back up the database using SQLite's online backup operation from the running
service; never copy only the main file while WAL writes may be active. Test a
private restore before inviting a tester. Keep the database backup together
with the stable identity secret, encrypted and access-controlled.

## 3. Put a trusted HTTPS edge in front

Keep port `8787` bound to loopback. The public reverse proxy must:

- terminate TLS and forward to `127.0.0.1:8787`;
- serve `/.well-known/apple-app-site-association` without a redirect;
- preserve its `application/json` content type;
- never cache `/v1/*` or `/invite`;
- avoid logging the `/invite` query string because it contains a one-time
  invitation secret;
- overwrite client forwarding headers and be the only network peer allowed to
  reach the origin;
- set `FORWARDED_ALLOW_IPS` to the proxy's exact origin-facing address if
  Uvicorn must consume proxy headers. Never use `*` on an Internet-reachable
  origin.

Verify through the public hostname:

```bash
curl --fail --silent --show-error \
  https://pinpoint.example.com/.well-known/apple-app-site-association
curl --fail --silent --show-error \
  https://pinpoint.example.com/healthz
```

The Apple App ID must have **Associated Domains** enabled, and the signed
PinPoint hosted app must include `applinks:pinpoint.example.com`. Apple must be
able to reach the AASA file directly over HTTPS.

## 4. Create one private invitation

Run the operator CLI inside the only live replica so it reads the same volume
and environment as the service:

```bash
docker compose exec pinpoint-backend \
  python -m pinpoint_backend.admin invite create \
  --label "Tester name or email" \
  --expires-in-hours 168
```

With public links configured, `activation_url` is the HTTPS Universal Link and
`custom_scheme_activation_url` is the installed-app fallback. Both contain the
same secret, so copy only the HTTPS URL into a private delivery channel and do
not paste the JSON record into logs or tickets. The code is printed only once.

The landing page loads no JavaScript or external asset. If macOS does not hand
the Universal Link to PinPoint, its single button opens the custom-scheme
fallback. The app independently validates the configured
HTTPS host, port, path, and one-code query before accepting it.
