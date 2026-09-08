#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
vendor_root="$project_dir/.vendor"
vendor_dir="$vendor_root/plaud-sdk-public"
official_remote="https://github.com/Plaud-AI/plaud-sdk-public.git"
expected_commit="81c7cecbcf7476e8263abbf3b937a261a4ea8893"

for command_name in git xcodegen; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required." >&2
    [[ "$command_name" == "xcodegen" ]] && echo "Install it with: brew install xcodegen" >&2
    exit 1
  fi
done

if [[ -e "$vendor_dir" && ! -d "$vendor_dir/.git" ]]; then
  echo "$vendor_dir exists but is not a Git checkout. Move it aside and retry." >&2
  exit 1
fi

if [[ ! -d "$vendor_dir/.git" ]]; then
  mkdir -p "$vendor_root"
  # Use a complete checkout. The SDK repository includes binary frameworks;
  # partial/promisor clones can appear healthy and fail only during Xcode's
  # framework copy phase.
  git clone "$official_remote" "$vendor_dir"
fi

remote_url=$(git -C "$vendor_dir" remote get-url origin)
if [[ "$remote_url" != "$official_remote" && "$remote_url" != "https://github.com/Plaud-AI/plaud-sdk-public" ]]; then
  echo "Refusing an unexpected Plaud SDK origin: $remote_url" >&2
  exit 1
fi

if [[ -n "$(git -C "$vendor_dir" status --porcelain)" ]]; then
  echo "The managed SDK checkout has local changes. Move it aside and retry." >&2
  exit 1
fi

git -C "$vendor_dir" fetch --depth 1 origin "$expected_commit"
git -C "$vendor_dir" checkout --detach "$expected_commit"

actual_commit=$(git -C "$vendor_dir" rev-parse HEAD)
if [[ "$actual_commit" != "$expected_commit" ]]; then
  echo "SDK commit mismatch: expected $expected_commit, got $actual_commit" >&2
  exit 1
fi

required_sdk_items=(
  "sdk/ios/PlaudDeviceBasicSDK.framework"
  "sdk/ios/PlaudBleSDK.framework"
  "sdk/ios/PlaudWiFiSDK.framework"
  "sdk/ios/PlaudDeviceBasicSDK.bundle"
)
for relative_path in "${required_sdk_items[@]}"; do
  if [[ ! -e "$vendor_dir/$relative_path" ]]; then
    echo "The pinned Plaud SDK checkout is missing $relative_path." >&2
    exit 1
  fi
done

cd "$project_dir"
xcodegen generate --spec project.yml
echo "Prepared PinPoint with Plaud's official SDK at pinned commit $actual_commit."
