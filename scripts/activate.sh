#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
project_dir=$(cd "$script_dir/.." && pwd)
backend_dir="$project_dir/PublicBackend"
runtime="$script_dir/backend_runtime.py"

if [[ ! -x "$backend_dir/.venv/bin/python" ]]; then
  echo "The private backend environment is missing. Run ./scripts/setup.sh first." >&2
  exit 1
fi

if ! activation_json=$("$backend_dir/.venv/bin/python" "$runtime" admin activation create); then
  echo "Could not create an activation code. Run ./scripts/setup.sh first." >&2
  exit 1
fi

if ! activation_code=$("$backend_dir/.venv/bin/python" -c '
import json
import re
import sys

payload = json.load(sys.stdin)
code = payload.get("activation_code")
if not isinstance(code, str) or re.fullmatch(r"ppl_[A-Za-z0-9_-]{32,100}", code) is None:
    raise SystemExit("invalid activation response")
print(code, end="")
' <<<"$activation_json"); then
  echo "The backend returned an invalid activation response. Create a new code and try again." >&2
  exit 1
fi

activation_url="pinpoint://local?code=$activation_code"
if open "$activation_url" 2>/dev/null; then
  echo "PinPoint is opening with a single-use activation code valid for 10 minutes."
else
  echo "PinPoint could not be opened automatically. Open it on this Mac and paste the code below." >&2
fi

echo
echo "Paste this code into PinPoint if automatic activation does not finish:"
echo "$activation_code"
