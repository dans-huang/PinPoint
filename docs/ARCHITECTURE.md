# Architecture

```text
Plaud recorder
   │  official Partner SDK: BLE or user-approved Fast Wi-Fi
   ▼
PinPoint on Apple silicon Mac
   │  authenticated control requests
   ├──────────────────────────────▶ PinPoint backend
   │                                  │ Partner credential custody
   │  direct multipart audio upload   │ ownership and recovery ledger
   ├──────────────────────────────▶ Plaud Partner cloud
   │                                  │ transcription
   │                                  ▼
   ◀────────────────────────────── transcript and job state
   │
   ├──────────────────────────────▶ configured intelligence provider
   │                                  summary / wording proposal
   ▼
Summary review, templates, Custom Words, marked moments, Codex / Claude handoff
```

The Mac owns proximity, Bluetooth, local transfer, and user interaction. The
backend owns Plaud Partner credentials, stable opaque user identity, revocable
sessions, recorder lifecycle, upload/transcription ownership, quotas, and
optional model credentials. Large audio parts travel from the app to
server-issued Plaud upload URLs rather than through the backend process.

Local recording metadata and checkpoints are namespaced by backend user. A
restart resumes the same logical source and external job. PinPoint never uses a
changed duration to invent a new recording identity, never exports the active
session, and never deletes audio from the recorder as part of sync.

Self-hosted mode replaces Apple enrollment with a loopback-only activation
exchange; the resulting session and downstream authorization remain bounded.
Hosted mode keeps Sign in with Apple and one-time invitations. They share App
sources but use separate schemes, configuration, and entitlements.
