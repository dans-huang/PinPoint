#!/usr/bin/env python3
"""Load the ignored backend environment, then run its server or admin CLI."""

from __future__ import annotations

import os
from pathlib import Path
import runpy
import shlex
import sys


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "PublicBackend"
ENV_FILE = BACKEND / ".env"


def load_environment() -> None:
    if not ENV_FILE.is_file():
        raise SystemExit("PublicBackend/.env is missing; run ./scripts/setup.sh")
    for line_number, raw_line in enumerate(
        ENV_FILE.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SystemExit(f"Invalid .env line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "A").isalnum() or not key[0].isalpha():
            raise SystemExit(f"Invalid .env key on line {line_number}")
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as error:
            raise SystemExit(f"Invalid .env value on line {line_number}") from error
        value = "" if not parts else " ".join(parts)
        os.environ[key] = value


def main() -> None:
    load_environment()
    sys.path.insert(0, str(BACKEND))
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        import uvicorn

        uvicorn.run(
            "pinpoint_backend.app:app",
            host="127.0.0.1",
            port=8787,
            workers=1,
            access_log=False,
        )
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "admin":
        sys.argv = ["pinpoint_backend.admin", *sys.argv[2:]]
        runpy.run_module("pinpoint_backend.admin", run_name="__main__")
        return
    raise SystemExit("Usage: backend_runtime.py serve|admin [arguments]")


if __name__ == "__main__":
    main()
