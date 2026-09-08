# PinPoint

PinPoint turns a supported Plaud recorder into an automatic meeting pipeline on
an Apple silicon Mac: connect, copy, upload, transcribe, summarize, review, and
continue the conversation in Codex or Claude.

> **Developer Preview.** PinPoint is independent open-source software, not an official Plaud product.
> It requires your own Plaud Partner developer credentials and an
> Apple signing identity. Expect setup work and breaking changes before a
> packaged release is available. Self-hosted mode assumes one trusted macOS
> account on one trusted Mac; other software running as that user can reach the
> loopback-only backend.

PinPoint does not redistribute Plaud's proprietary SDK binaries. Its bootstrap
script downloads the [official Plaud SDK repository](https://github.com/Plaud-AI/plaud-sdk-public)
at one reviewed commit. Your use of that SDK and Plaud's services remains
subject to Plaud's separate terms.

## What it does

- Connects a Plaud NotePin S or Note Pro through Plaud's official Partner SDK.
- Detects finished recordings and copies them automatically over BLE.
- Offers a ten-second Fast Wi-Fi choice for a new batch, with safe BLE fallback,
  and lets you switch an unfinished transfer to Wi-Fi later.
- Uploads through a user-scoped backend, submits transcription, and resumes
  interrupted upload or transcription work without deleting recorder files.
- Shows each generated summary inside PinPoint.
- Supports Automatic flow, summary templates, Custom Words, and button-marked
  moments as relevance signals.
- Creates a reviewable **Improve wording** proposal; approval replaces the
  canonical PinPoint summary and adds selected spellings to Custom Words.
- Opens a selected meeting as a prepared task in Codex or Claude and remembers
  the chosen project folder.
- Keeps recorder association, local recordings, sessions, and cloud jobs
  isolated by user in hosted mode.

## Hardware status

| Recorder | Status |
|---|---|
| Plaud NotePin S | Physically verified with automatic BLE copy and optional Fast Wi-Fi |
| Plaud Note Pro | Supported by the model and Partner-SDK code path; physical end-to-end verification is still pending |

The connected recorder's runtime capability is authoritative. PinPoint offers
Fast Wi-Fi only when the official SDK reports that the current device supports
it; otherwise transfer continues over BLE.

## Requirements

- Apple silicon Mac
- Xcode 16 or newer and its command-line tools
- [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`)
- Python 3.10 or newer
- A Plaud Partner developer account with client ID, client secret, and API key
- An authorized supported Plaud recorder
- An Apple account for local signing and a unique bundle identifier

The default self-hosted build uses BLE and can be signed with a free Personal
Team in Xcode. Free development provisioning may need periodic rebuilding.
Fast Wi-Fi additionally requires a paid Apple Developer team because Apple does
not make Hotspot Configuration or Access WiFi Information available to free
accounts. If your team has both capabilities, enable them during setup:

```bash
./scripts/setup.sh --fast-wifi
```

Without that option, the app stays on the supported BLE path. See Apple's
[current capability table](https://developer.apple.com/help/account/reference/supported-capabilities-ios/).

## Quick start: one person, one Mac

```bash
git clone https://github.com/dans-huang/PinPoint.git
cd PinPoint
brew install xcodegen
./scripts/setup.sh
```

Setup asks for your Plaud Partner credentials without placing them in the app
or Git. It then:

1. creates a private backend environment and cryptographically secure random secrets;
2. installs a user-level background service bound only to `127.0.0.1`;
3. downloads the pinned official Plaud SDK into ignored `.vendor/` storage;
4. creates ignored local signing configuration; and
5. generates and opens `PinPoint.xcodeproj`.

In Xcode, select the **PinPoint** scheme, choose your signing team if needed,
and run it on **My Mac (Designed for iPad)**. When the app asks to activate this
Mac, create a fresh single-use code:

```bash
./scripts/activate.sh
```

The script opens PinPoint with a fresh activation code automatically. If macOS
cannot hand off the link, copy the displayed fallback code into the app. The
code expires after ten minutes and cannot be reused. It is never committed or
stored in the app after enrollment. See
[Self-hosting](docs/SELF-HOSTING.md) for service controls, updates, and
troubleshooting.

## Optional meeting intelligence

Transcription uses the Plaud Partner service. PinPoint's canonical summaries,
templates, Custom Words, and wording improvement additionally require an
OpenAI-compatible `/responses` provider. Add these backend-only values to the
ignored `PublicBackend/.env`, then restart the service:

```dotenv
PINPOINT_INTELLIGENCE_BASE_URL=https://api.openai.com/v1
PINPOINT_INTELLIGENCE_API_KEY=your-provider-key
PINPOINT_INTELLIGENCE_MODEL=your-model
```

```bash
./scripts/backend-service.sh restart
```

No model key is embedded in the app. Meeting transcripts are treated as quoted,
untrusted evidence rather than instructions, and model changes remain bounded
by server-side schemas and explicit approval where required.

## Multi-user hosted mode

The **PinPointHosted** scheme retains Sign in with Apple, one-time invitations,
per-user state, lifecycle controls, and shared-cost limits. It requires a stable
HTTPS origin, a paid Apple Developer setup, and competent service operation.
Start with [Hosted mode](docs/HOSTED-MODE.md); do not expose the self-hosted mode
to a network.

## Repository map

| Path | Purpose |
|---|---|
| `App/` | PinPoint interface, recorder transfer, summaries, and assistant handoff |
| `Shared/` | Supported-device domain model |
| `PublicBackend/` | Partner credential custody, user sessions, uploads, transcription, and intelligence |
| `Config/` | Non-secret Xcode defaults and ignored local overrides |
| `scripts/` | SDK bootstrap, build, local service, setup, and activation |
| `Tests/` | App contracts, security invariants, and compile harnesses |

## Build and test

```bash
./scripts/build.sh --mac
./scripts/build.sh --hosted --ios
python3 -m unittest discover -s Tests -v
python3 -m unittest discover -s PublicBackend/tests -v
```

`--ios` is an unsigned compile check. Running on a Mac requires Apple silicon,
an appropriate signing profile, Bluetooth permission, and real hardware.
Passing CI is not evidence that a physical recorder or live Plaud account was
tested.

## Security, data, and licensing

- Read [Security](docs/SECURITY.md) before entering credentials or exposing a
  backend.
- Read [Architecture](docs/ARCHITECTURE.md) for trust and data-flow boundaries.
- Read [Third-party notices](docs/THIRD-PARTY-NOTICES.md) before redistribution.
- This repository's original source is licensed under [Apache 2.0](LICENSE).
  Plaud SDK binaries are not covered by this repository's license.

PinPoint is not affiliated with or endorsed by Plaud. Plaud and its product
names are trademarks of their respective owner.
