# Third-party notices

## Plaud SDK

PinPoint builds against PlaudDeviceBasicSDK, PlaudBleSDK, and PlaudWiFiSDK from
Plaud's [official public SDK repository](https://github.com/Plaud-AI/plaud-sdk-public).
The bootstrap script fetches exact commit
`81c7cecbcf7476e8263abbf3b937a261a4ea8893` into ignored `.vendor/` storage.

Those SDK binaries are proprietary and distributed under a separate Plaud
license. They are not included in this repository and are not covered by
PinPoint's Apache-2.0 license. Review the upstream license and your Plaud
Partner agreement before use or redistribution.

## Service terms and trademarks

Plaud Partner APIs, transcription, device access, quotas, and commercial use
remain subject to Plaud's current developer terms and the agreement attached to
your credentials. A consumer Plaud subscription does not itself grant Partner
API or SDK rights.

Plaud and its product names are trademarks of their respective owner. PinPoint
is an independent open-source project and is not affiliated with or endorsed by
Plaud.

## Other dependencies

The Python backend dependencies are listed in `PublicBackend/requirements.txt`.
XcodeGen generates the local Xcode project. Their respective upstream licenses
continue to apply. Before distributing a compiled build, inventory the resolved
versions and include every notice required by those licenses and by the Plaud
SDK agreement.
