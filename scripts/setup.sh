#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
backend_dir="$project_dir/PublicBackend"
env_file="$backend_dir/.env"
local_config="$project_dir/Config/PinPoint.local.xcconfig"
venv_dir="$backend_dir/.venv"
non_interactive=false
open_project=true
fast_wifi=false

usage() {
  cat <<'USAGE'
Usage: ./scripts/setup.sh [--non-interactive] [--no-open] [--fast-wifi]

Starts the loopback-only backend, fetches Plaud's pinned official SDK, and
generates the PinPoint Xcode project. Existing local configuration is kept.
Use --fast-wifi only with an Apple team that supports Hotspot Configuration
and Access WiFi Information; the default BLE path also works with free signing.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --non-interactive) non_interactive=true ;;
    --no-open) open_project=false ;;
    --fast-wifi) fast_wifi=true ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "PinPoint currently runs as a Designed-for-iPad app on an Apple silicon Mac." >&2
  exit 1
fi

for command_name in git openssl python3 xcodebuild xcodegen curl launchctl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required before setup can continue." >&2
    [[ "$command_name" == "xcodegen" ]] && echo "Install it with: brew install xcodegen" >&2
    exit 1
  fi
done

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or newer is required." >&2
  exit 1
fi

xcode_major=$(xcodebuild -version 2>/dev/null | awk '/^Xcode / { split($2, version, "."); print version[1]; exit }')
if [[ ! "$xcode_major" =~ ^[0-9]+$ ]] || (( xcode_major < 16 )); then
  echo "Xcode 16 or newer is required. Select a full Xcode installation and retry." >&2
  exit 1
fi

if [[ ! -f "$env_file" ]]; then
  cp "$backend_dir/.env.example" "$env_file"
fi
chmod 600 "$env_file"

read_env_value() {
  local key=$1
  python3 - "$env_file" "$key" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
for raw_line in path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if line.startswith(key + "="):
        value = line.split("=", 1)[1].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        print(value)
        break
PY
}

write_env_value() {
  local key=$1
  local value=$2
  PINPOINT_SETUP_VALUE="$value" python3 - "$env_file" "$key" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = os.environ["PINPOINT_SETUP_VALUE"]
if "\n" in value or "\r" in value:
    raise SystemExit(f"{key} cannot contain a newline")
encoded = value.replace("\\", "\\\\").replace('"', '\\"')
replacement = f'{key}="{encoded}"'
lines = path.read_text(encoding="utf-8").splitlines()
for index, line in enumerate(lines):
    if line.strip().startswith(key + "="):
        lines[index] = replacement
        break
else:
    lines.append(replacement)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

is_placeholder() {
  local value=${1:-}
  [[ -z "$value" || "$value" == *replace* || "$value" == *REPLACE* || "$value" == *example* ]]
}

prompt_value() {
  local key=$1
  local label=$2
  local secret=${3:-false}
  local current
  current=$(read_env_value "$key")
  if ! is_placeholder "$current"; then
    return
  fi
  if [[ "$non_interactive" == true || ! -t 0 ]]; then
    echo "$key is not configured in PublicBackend/.env." >&2
    exit 2
  fi
  local value=""
  if [[ "$secret" == true ]]; then
    read -r -s -p "$label: " value
    echo
  else
    read -r -p "$label: " value
  fi
  if [[ -z "$value" ]]; then
    echo "$key cannot be empty." >&2
    exit 2
  fi
  write_env_value "$key" "$value"
}

write_env_value PINPOINT_AUTH_MODE self_hosted
state_dir="$HOME/Library/Application Support/PinPoint"
mkdir -p "$state_dir"
chmod 700 "$state_dir"
write_env_value PINPOINT_STATE_DB_PATH "$state_dir/pinpoint.sqlite3"

session_secret=$(read_env_value PINPOINT_SESSION_SECRET)
if is_placeholder "$session_secret"; then
  write_env_value PINPOINT_SESSION_SECRET "$(openssl rand -hex 32)"
fi
user_secret=$(read_env_value PINPOINT_USER_ID_SECRET_V1)
if is_placeholder "$user_secret"; then
  write_env_value PINPOINT_USER_ID_SECRET_V1 "$(openssl rand -hex 32)"
fi

prompt_value PLAUD_CLIENT_ID "Plaud Partner client ID"
prompt_value PLAUD_CLIENT_SECRET "Plaud Partner client secret" true
prompt_value PLAUD_API_KEY "Plaud Partner API key" true

if [[ ! -f "$local_config" ]]; then
  user_slug=$(id -un | tr -cd '[:alnum:]-' | tr '[:upper:]' '[:lower:]')
  [[ -n "$user_slug" ]] || user_slug="user"
  default_bundle="dev.pinpoint.$user_slug.app"
  bundle_id="$default_bundle"
  team_id=""
  if [[ "$non_interactive" == false && -t 0 ]]; then
    read -r -p "Unique bundle identifier [$default_bundle]: " entered_bundle
    bundle_id=${entered_bundle:-$default_bundle}
    read -r -p "Apple Team ID (optional; you can choose it in Xcode): " team_id
  fi
  {
    echo "// Generated locally by scripts/setup.sh. Do not commit."
    echo "PINPOINT_BUNDLE_ID = $bundle_id"
    echo "PINPOINT_DEVELOPMENT_TEAM = $team_id"
    echo "PINPOINT_CODE_SIGN_ENTITLEMENTS ="
  } > "$local_config"
fi

if [[ "$fast_wifi" == true ]]; then
  PINPOINT_SETUP_VALUE="App/PinPoint.entitlements" python3 - "$local_config" <<'PY'
from pathlib import Path
import os
import sys

path = Path(sys.argv[1])
key = "PINPOINT_CODE_SIGN_ENTITLEMENTS"
replacement = f"{key} = {os.environ['PINPOINT_SETUP_VALUE']}"
lines = path.read_text(encoding="utf-8").splitlines()
for index, line in enumerate(lines):
    normalized = line.strip()
    if normalized.startswith(key + " ") or normalized.startswith(key + "="):
        lines[index] = replacement
        break
else:
    lines.append(replacement)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

"$script_dir/bootstrap-sdk.sh"

if [[ ! -x "$venv_dir/bin/python" ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install --disable-pip-version-check -r "$backend_dir/requirements.txt"

launch_agents_dir="$HOME/Library/LaunchAgents"
launch_agent="$launch_agents_dir/org.pinpoint.community.backend.plist"
logs_dir="$HOME/Library/Logs/PinPoint"
mkdir -p "$launch_agents_dir" "$logs_dir"
python3 - "$launch_agent" "$project_dir" "$venv_dir/bin/python" "$script_dir/backend_runtime.py" "$logs_dir" <<'PY'
from pathlib import Path
import plistlib
import sys

path = Path(sys.argv[1])
project = sys.argv[2]
python = sys.argv[3]
runtime = sys.argv[4]
logs = Path(sys.argv[5])
payload = {
    "Label": "org.pinpoint.community.backend",
    "ProgramArguments": [python, runtime, "serve"],
    "WorkingDirectory": project,
    "RunAtLoad": True,
    "KeepAlive": {"SuccessfulExit": False},
    "ProcessType": "Background",
    "StandardOutPath": str(logs / "backend.log"),
    "StandardErrorPath": str(logs / "backend-error.log"),
}
with path.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=True)
PY
chmod 600 "$launch_agent"
"$script_dir/backend-service.sh" restart

healthy=false
for _ in {1..45}; do
  if curl --fail --silent http://127.0.0.1:8787/healthz \
    | python3 -c 'import json,sys; assert json.load(sys.stdin) == {"status": "ok"}' 2>/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done
if [[ "$healthy" != true ]]; then
  "$script_dir/backend-service.sh" logs >&2
  echo "The PinPoint backend did not become healthy." >&2
  exit 1
fi

echo
echo "PinPoint is prepared and its private backend is listening only on this Mac."
echo "Build and run the PinPoint scheme in Xcode, then create a 10-minute activation code with:"
echo "  ./scripts/activate.sh"

if [[ "$open_project" == true ]]; then
  open "$project_dir/PinPoint.xcodeproj"
fi
