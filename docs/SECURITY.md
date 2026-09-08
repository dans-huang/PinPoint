# Security

PinPoint handles long-lived credentials, raw meeting audio, transcripts,
summaries, device identifiers, and recorder ownership. Treat a deployment as a
sensitive application, not a desktop demo.

## Secrets

Never commit or embed these values in the app:

- Plaud Partner client secret or API key;
- PinPoint session and stable user-ID secrets;
- intelligence-provider API key;
- Apple private key or refresh-token encryption key;
- activation or invitation codes;
- populated `.env`, local xcconfig, SQLite ledger, audio, or logs.

The repository ignores these paths and CI scans for common credential shapes,
but that is only a backstop. Review every staged file and its history before
publishing.

## Self-hosted mode

Self-hosted authentication is deliberately loopback-only. The backend requires
an explicit `PINPOINT_AUTH_MODE=self_hosted`, accepts `/v1/session/local` only
from loopback with a local Host header, rejects forwarding headers, and uses a
single-use ten-minute activation code. Never expose this mode to LAN, tunnel,
container port forwarding, reverse proxy, or the public Internet.

The setup script therefore runs uvicorn directly as the signed-in macOS user,
not inside Docker. A container bridge would not appear as a direct loopback
peer and must not be used to weaken that check.

## Hosted mode

Keep open signup disabled. Treat invited users as trusted because the official
device SDK needs a raw, short-lived Plaud user token on the client. Backend
quotas cannot contain a bearer after it reaches a user's process. Use the
shortest Plaud-supported token lifetime and revoke membership when access ends.

Recorder binding is exclusive. Preserve the durable lifecycle ledger and never
delete or rewrite ownership history to resolve a conflict. An ambiguous bind,
release, upload completion, or transcription submission must resume through
its saved checkpoint rather than repeat the external side effect.

## Meeting content and AI

Transcripts and summaries are private user content. PinPoint sends them only to
the configured Plaud and intelligence services required for the selected
workflow. The intelligence prompt treats transcript text and button-marked
moments as untrusted data, not commands or authorization. Wording improvements
remain proposals until the user approves them.

Before using this software with regulated or confidential meetings, assess
retention, consent, data residency, provider settings, access control, and
applicable law for your environment.

## Reporting a vulnerability

Do not place credentials, recordings, transcripts, device serial numbers, or
private logs in a public issue. Use the repository owner's private security
reporting channel if one is enabled. Until a report is acknowledged, stop the
affected service and rotate any credential that may have been exposed.
