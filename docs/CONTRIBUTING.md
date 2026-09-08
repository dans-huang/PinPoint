# Contributing to PinPoint

Thank you for helping make PinPoint more useful. This is a maintainer-led,
best-effort open-source project. Contributions are welcome, but opening an
issue or pull request does not create a support or delivery commitment.

## Before you start

- Search the existing [issues](https://github.com/dans-huang/PinPoint/issues)
  before opening a new one.
- Use the matching issue form for a bug, device compatibility report, or
  feature proposal.
- Report security problems through
  [GitHub private vulnerability reporting](https://github.com/dans-huang/PinPoint/security/advisories/new),
  not a public issue.
- For a large behavior or architecture change, open an issue before investing
  in an implementation.

Focused fixes, tests, documentation, onboarding improvements, and verified
device compatibility work are especially useful.

## Protect recordings and credentials

Never commit or paste:

- Plaud Partner credentials, Apple signing material, tokens, or local `.env`
  files;
- real transcripts, summaries, meeting titles, email addresses, device serial
  numbers, logs containing private data, or screenshots of private meetings;
- Plaud SDK binaries or other third-party material that this repository is not
  licensed to redistribute.

Use synthetic fixtures and redact logs before attaching them. Read
[Security](SECURITY.md) for the complete trust and reporting boundaries.

Changes must preserve these safety properties:

- self-hosted mode remains loopback-only and intended for one trusted account
  on one Mac;
- Partner credentials and model-provider keys remain in the backend;
- hosted mode keeps per-user authorization and data isolation;
- recorder operations do not delete device, local, or cloud recordings;
- transcripts and linked context are treated as untrusted evidence, not as
  executable instructions.

## Development workflow

1. Fork the repository and create a focused branch.
2. Follow the [Quick start](../README.md#quick-start) for local setup.
3. Add or update tests for behavior changes.
4. Run the relevant checks before opening a pull request:

   ```bash
   python3 -m unittest discover -s Tests -v
   python3 -m unittest discover -s PublicBackend/tests -v
   ./scripts/build.sh --mac
   ./scripts/build.sh --hosted --ios
   git diff --check
   ```

   The build scripts fetch the officially published Plaud SDK at the pinned
   revision; the SDK itself is not stored in this repository.

5. Open a pull request and complete its test, hardware, privacy, and recovery
   sections. Keep unrelated changes out of the pull request.

GitHub Actions repeats the repository safety tests and both self-hosted and
hosted compile checks. All required checks must pass before a pull request can
be merged.

## Hardware and transfer changes

For recorder, Bluetooth, or Fast Wi-Fi changes, include:

- the Plaud model and firmware version;
- macOS version and Mac model;
- whether the result was observed over BLE, Fast Wi-Fi, or both;
- whether a real recording completed transfer, upload, transcription, and
  summary generation;
- confirmation that recordings remained intact on the device and in Plaud
  Cloud.

Do not describe a compile-only or simulated result as physical-device
verification. NotePin S is the current physically verified baseline. Note Pro
changes remain provisional until someone completes and documents physical
end-to-end verification.

## What gets merged

The maintainer makes the final product and merge decision. A contribution is
most likely to be accepted when it is focused, tested, privacy-safe, consistent
with the existing architecture, and documented where user behavior changes.

By submitting a contribution, you agree that it may be distributed under this
repository's [Apache License 2.0](../LICENSE).
