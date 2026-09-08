#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
scheme="PinPoint"
destination_kind="mac"

usage() {
  echo "Usage: $0 [--hosted] [--ios|--mac]" >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosted)
      scheme="PinPointHosted"
      ;;
    --ios)
      destination_kind="ios"
      ;;
    --mac)
      destination_kind="mac"
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
done

if ! command -v xcodebuild >/dev/null 2>&1; then
  echo "Xcode is required." >&2
  exit 1
fi

"$script_dir/bootstrap-sdk.sh"
cd "$project_dir"

destination="generic/platform=iOS"
if [[ "$destination_kind" == "mac" ]]; then
  if [[ "$(uname -m)" != "arm64" ]]; then
    echo "Designed-for-iPad apps require an Apple silicon Mac. Use --ios for a compile-only build." >&2
    exit 1
  fi
  destinations=$(xcodebuild -project PinPoint.xcodeproj -scheme "$scheme" -showdestinations)
  destination_id=$(printf '%s\n' "$destinations" \
    | sed -nE '/platform:macOS.*variant:Designed for \[iPad,iPhone\]/s/.*id:([^,}]+).*/\1/p' \
    | head -1 \
    | xargs)
  if [[ -z "$destination_id" ]]; then
    echo "No Designed for iPad/iPhone destination was found on this Mac." >&2
    printf '%s\n' "$destinations" >&2
    exit 1
  fi
  destination="id=$destination_id"
fi

derived_name=${scheme//[^A-Za-z0-9]/-}
xcodebuild \
  -project PinPoint.xcodeproj \
  -scheme "$scheme" \
  -configuration Debug \
  -destination "$destination" \
  -derivedDataPath "DerivedData-$derived_name" \
  CODE_SIGNING_ALLOWED=NO \
  CODE_SIGNING_REQUIRED=NO \
  build

echo "$scheme compiled successfully for $destination_kind."
