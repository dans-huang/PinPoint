# Self-hosting PinPoint

Self-hosted mode is the default and simplest supported deployment: one macOS
user, one Apple silicon Mac, one private backend, and that user's authorized
Plaud recorder.

## Trust boundary

The backend listens only on `127.0.0.1:8787`. The local session endpoint also
rejects forwarded requests and non-loopback peers. Do not place it behind a
reverse proxy, bind it to another interface, or expose it through a tunnel.
This protects the service from the network, not from other software running as
the same macOS user. Use this mode only on a trusted account and trusted Mac.

Plaud Partner credentials and the optional intelligence-provider key live only
in ignored `PublicBackend/.env`. The generated Xcode configuration contains a
bundle identifier and signing team, not service secrets.

## Install

1. Install Xcode 16 or newer, its command-line tools, Python 3.10 or newer,
   and XcodeGen.
2. Obtain Plaud Partner client ID, client secret, and API key.
3. Run `./scripts/setup.sh` and answer the local prompts. This defaults to BLE
   and works with a free Xcode Personal Team.
4. Build the `PinPoint` scheme for My Mac (Designed for iPad).
5. With the app running, execute `./scripts/activate.sh`. It creates the code
   and opens PinPoint automatically.

Fast Wi-Fi is optional. Apple restricts its required Hotspot Configuration and
Access WiFi Information capabilities to paid developer teams. If your team has
both, run `./scripts/setup.sh --fast-wifi`; otherwise keep the BLE default. A
free Personal Team build may need to be rebuilt periodically.

The activation code is one-time and valid for ten minutes. If macOS cannot
open the activation link, the script displays the same code so you can paste it
into PinPoint. A successful local session receives the same bounded, revocable
backend session used by ordinary authenticated API calls; the app never
receives the Partner client secret or API key.

## Background service

Setup installs a private per-user background service. It starts after login and
restarts after an unexpected failure.

```bash
./scripts/backend-service.sh status
./scripts/backend-service.sh restart
./scripts/backend-service.sh stop
./scripts/backend-service.sh start
./scripts/backend-service.sh logs
```

The private ledger is stored under
`~/Library/Application Support/PinPoint/`. Back up that directory together
with `PublicBackend/.env`. Losing or changing `PINPOINT_USER_ID_SECRET_V1` can
change the Partner identity derived for the local user.

## Update

Pull a reviewed revision, read its release notes, then run
`./scripts/setup.sh` again. Setup preserves existing ignored credentials and
signing configuration, refreshes dependencies, regenerates the Xcode project,
and restarts the private backend.

The Plaud SDK checkout is pinned. If it contains local edits, bootstrap stops
instead of overwriting them. Move the ignored `.vendor/plaud-sdk-public`
checkout aside if you intentionally want a clean re-download.

## Common failures

- **Backend does not become healthy:** run `./scripts/backend-service.sh logs`.
- **Activation expired:** run `./scripts/activate.sh` again; do not reuse a code.
- **Signing fails:** choose your Apple team and a globally unique bundle ID in
  ignored `Config/PinPoint.local.xcconfig`. If you use a free Personal Team,
  keep `PINPOINT_CODE_SIGN_ENTITLEMENTS` empty.
- **Fast Wi-Fi is unavailable:** confirm the connected recorder reports Wi-Fi
  support and the signing profile contains the required capabilities. Continue
  over BLE otherwise.
- **Recorder is owned elsewhere:** release it from its current Plaud owner
  first. PinPoint does not guess ownership, factory-reset, or silently unbind.
