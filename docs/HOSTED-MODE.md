# Hosted multi-user mode

`PinPointHosted` is an operator deployment, not the quick-start path. It uses
the same product interface and recorder workflow, with Sign in with Apple,
managed one-time invitations, per-user storage, revocable sessions, recorder
ownership claims, quotas, and abuse controls.

Use hosted mode only if you can operate a security-sensitive public service and
have the necessary Plaud and Apple agreements for your users.

## Required components

- stable HTTPS origin and trusted reverse proxy;
- paid Apple Developer membership;
- unique App ID with Sign in with Apple, Associated Domains, Hotspot
  Configuration, and Wi-Fi Information capabilities as applicable;
- matching `Config/PinPointHosted.local.xcconfig` values;
- persistent single-replica backend storage;
- Plaud Partner credentials authorized for the intended users and devices;
- monitoring, encrypted backups, credential rotation, and an incident process.

Copy both examples before deployment:

```bash
cp Config/PinPointHosted.local.xcconfig.example Config/PinPointHosted.local.xcconfig
cp PublicBackend/.env.example PublicBackend/.env
```

Set `PINPOINT_AUTH_MODE=hosted`. Configure the exact Apple audience, App ID,
HTTPS invitation URL, and backend secrets. Follow
`PublicBackend/DEPLOYMENT.md` for the backend's authoritative environment and
container requirements.

The supplied Compose file binds the origin to loopback for a reverse proxy and
uses one persistent SQLite volume. Do not scale it to multiple independent
replicas or filesystems. The proxy must replace forwarding headers, enforce
TLS, and preserve the backend's request-size limits.

Create and distribute an invitation only through the backend operator CLI.
Invitation plaintext is shown once, expires, and must never enter source
control, analytics, crash reports, or support logs.

Hosted mode is not enabled merely by building the scheme. Validate Sign in with
Apple, Universal Links, invitation consumption, returning-user login, account
disablement, recorder bind/release recovery, quotas, and a real device against
the final production origin before inviting anyone.
